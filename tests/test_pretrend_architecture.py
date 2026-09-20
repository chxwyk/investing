"""Architecture invariants for the pre-trend lane.

These do not test behaviour, they test that certain behaviour remains
*impossible*.  Each one guards a property that a future change could plausibly
break by accident, and that would be expensive and quiet if it did.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import smart_money_bot.pretrend as pretrend_package
import smart_money_bot.pretrend_cards as cards
import smart_money_bot.pretrend_runtime as runtime
import smart_money_bot.pretrend_store as store
import smart_money_bot.pretrend_training as training

SOURCE_ROOT = Path(inspect.getfile(pretrend_package)).parent
LANE_MODULES = (runtime, store, cards, training)

#: Anything that could place, price, sign or send an order.
EXECUTION_TOKENS = (
    "executor",
    "Executor",
    "Keypair",
    "send_transaction",
    "sendTransaction",
    "place_order",
    "submit_swap",
    "jupiter_swap",
    "sign_transaction",
    "live_execution",
)


def _pretrend_sources() -> list[tuple[str, str]]:
    sources = [
        (str(path), path.read_text())
        for path in sorted(SOURCE_ROOT.glob("*.py"))
    ]
    for module in LANE_MODULES:
        path = Path(inspect.getfile(module))
        sources.append((str(path), path.read_text()))
    return sources


def _code_only(source: str) -> str:
    """The module's executable code, with comments and docstrings removed.

    Prose *about* execution is not only allowed, it is how the constraint is
    documented -- this module's own docstrings say the lane must never trade.
    What the invariant forbids is a real reference, so docstrings are stripped
    before the check rather than the check being weakened to tolerate them.
    """

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_the_prediction_lane_cannot_reach_execution() -> None:
    """Section 89: prediction and trading stay separate, structurally.

    The lane is allowed to be wrong.  It is not allowed to be wrong with money,
    and the way to guarantee that is for the code to have no way to spend any.
    """

    offenders: list[str] = []
    for path, source in _pretrend_sources():
        for token in EXECUTION_TOKENS:
            if token in _code_only(source):
                offenders.append(f"{path}: {token}")
    assert not offenders, f"the pre-trend lane referenced execution: {offenders}"


def test_the_lane_never_imports_a_trading_module() -> None:
    forbidden = {"executor", "execution", "shadow_runtime", "lab_runtime"}
    offenders: list[str] = []
    for path, source in _pretrend_sources():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                tail = node.module.split(".")[-1]
                if tail in forbidden:
                    offenders.append(f"{path}: from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[-1] in forbidden:
                        offenders.append(f"{path}: import {alias.name}")
    assert not offenders, offenders


def test_every_ping_passes_through_the_single_publication_choke_point() -> None:
    """The pre-trend cards must not have invented a second door to Discord."""

    engine_source = (SOURCE_ROOT.parent / "engine.py").read_text()
    call_sites = engine_source.count("notifier.on_fast_alert(")
    assert call_sites == 1, (
        "a card builder reached the notifier directly instead of going through "
        "_dispatch_card, bypassing the publication guard"
    )
    for name in ("_publish_pretrend", "_publish_trend_confirmation"):
        start = engine_source.index(f"async def {name}(")
        body = engine_source[start : start + 2_500]
        assert "_dispatch_card(" in body, f"{name} must publish via _dispatch_card"


def test_first_observation_columns_appear_in_no_unguarded_update_clause() -> None:
    """Write-once is enforced by the SQL, not by callers remembering.

    ``first_trending_at`` is the timestamp every lead-time claim is measured
    against. Exactly one UPDATE in the codebase may set it -- the guarded
    promotion in :meth:`promote_membership_entry`, which fires only while the
    column is still NULL, so a witnessed entry can be recorded once and can
    never overwrite one that was already proven.
    """

    sql = Path(inspect.getfile(store)).read_text()

    protected = (
        "first_trending_at",
        "first_observed_on_board_at",
        "first_rank",
        "first_market_cap_usd",
        "first_seen_at",
    )
    lowered = sql.lower()
    cursor = 0
    while True:
        index = lowered.find("do update set", cursor)
        if index == -1:
            break
        clause = lowered[index : index + 600]
        end = clause.find('"""')
        clause = clause[: end if end != -1 else len(clause)]
        for column in protected:
            assert column not in clause, (
                f"{column} appears in an upsert's UPDATE SET clause; it must be write-once"
            )
        cursor = index + 1

    # The strictest form of the rule: NOTHING updates the entry timestamp.
    # It is set once by an INSERT or it stays NULL forever.  A mint we found
    # already on the board entered before we were watching, so a later
    # witnessed arrival is a re-entry and must not be back-filled as the first.
    assert "SET first_trending_at" not in sql, (
        "no UPDATE may set first_trending_at; it is INSERT-only"
    )


def test_a_presence_we_never_witnessed_cannot_be_stored_as_an_entry() -> None:
    """The column is nullable on purpose, and the schema says why."""

    schema = (SOURCE_ROOT.parent / "database.py").read_text()
    block = schema[schema.index("CREATE TABLE IF NOT EXISTS pretrend_membership") :]
    block = block[: block.index(");")]

    assert "first_observed_on_board_at INTEGER NOT NULL" in block
    # NOT nullable would force a fabricated entry time for every token that was
    # already on the board when collection started.
    assert "first_trending_at INTEGER," in block
    assert "first_trending_at INTEGER NOT NULL" not in block
    assert "PRIMARY KEY (mint, grade)" in block, (
        "membership must be keyed by grade so a proxy board cannot occupy the "
        "FOMO row for a mint"
    )


def test_only_an_authorised_source_can_establish_a_label() -> None:
    """Provenance is a membership test against a frozen set, not a heuristic."""

    from smart_money_bot.pretrend.groundtruth import (
        AUTHORISED_SOURCE_KINDS,
        GRADE_FOMO,
        GRADE_PROXY,
        grade_for_source,
    )

    assert frozenset({"FOMO_TRENDING"}) == AUTHORISED_SOURCE_KINDS
    assert grade_for_source("FOMO_TRENDING") == GRADE_FOMO
    for unauthorised in (
        "TRENDING_PROXY",
        "NO_SOURCE_CONFIGURED",
        "",
        "fomo_trending",
        "FOMO_TRENDING_V2",
    ):
        assert grade_for_source(unauthorised) == GRADE_PROXY, unauthorised

    # The label-grade queries live in SQL so a new caller inherits them.
    sql = Path(inspect.getfile(store)).read_text()
    label_map = sql[sql.index("async def first_trending_map(") :]
    label_map = label_map[: label_map.index("\n    async def ")]
    assert "grade = 'FOMO'" in label_map
    assert "first_trending_at IS NOT NULL" in label_map


def test_the_pretrend_observation_insert_is_append_only() -> None:
    sql = Path(inspect.getfile(store)).read_text()
    start = sql.index("async def record_observation(")
    body = sql[start : sql.index("async def observations_for(")]
    assert "INSERT OR IGNORE INTO pretrend_observations" in body
    assert "ON CONFLICT" not in body
    assert "INSERT OR REPLACE INTO pretrend_observations" not in body


def test_the_schema_additions_alter_no_existing_table() -> None:
    """A rollback must lose only the new lane, never production history."""

    schema = (SOURCE_ROOT.parent / "database.py").read_text()
    marker = "FOMO PRE-TREND INTELLIGENCE (v2.55)"
    block = schema[schema.index(marker) :]
    block = block[: block.index('"""')]
    assert "DROP TABLE" not in block
    assert "ALTER TABLE" not in block
    assert "DELETE FROM" not in block
    # Every statement in the block is a guarded create.
    creates = block.count("CREATE TABLE")
    assert creates >= 15
    assert block.count("CREATE TABLE IF NOT EXISTS") == creates


def test_the_validation_module_offers_no_random_split() -> None:
    """A random split on token observations guarantees a wrong answer."""

    source = (SOURCE_ROOT / "validation.py").read_text()
    for token in ("train_test_split", "random.shuffle", "random.sample", "shuffle("):
        assert token not in source, f"validation must not offer {token}"
    assert "def temporal_folds(" in source
    assert "def walk_forward(" in source
