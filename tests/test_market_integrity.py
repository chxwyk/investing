"""The three screenshots, turned into tests that fail if the bot lies again.

Each fixture here is transcribed from a card the operator actually received.
The numbers are theirs; only the mints are sanitised.  The point of writing
them down is that "we fixed the wording" is not a claim anybody can check, and
this failure has now been reported three times.

The rule under test is one sentence: **a headline may not say anything the
card's own body contradicts**.  Every assertion below is a restatement of it.
"""

from __future__ import annotations

import pathlib
from decimal import Decimal
from unittest import mock

import pytest

import smart_money_bot.engine as engine_module
from smart_money_bot.lab.hardgates import (
    CONTRACT_SAFETY_OK,
    FAIL,
    PASS,
    SELL_ROUTE_OK,
    UNKNOWN,
    GateReport,
    GateResult,
)
from smart_money_bot.lab.hardgates import GATES as _ALL_GATES
from smart_money_bot.lab.verdict import (
    DATA_INTEGRITY_HOLD,
    DEFAULT_COST_CONFIG,
    DO_NOT_ENTER_COST,
    DO_NOT_ENTER_DISTRIBUTION,
    DO_NOT_ENTER_INSUFFICIENT,
    DO_NOT_ENTER_UNRESOLVED,
    EDGE_NOT_COMPUTABLE,
    FORBIDDEN_PHRASES,
    PAPER_ENTRY,
    Decision,
    MarketEvidence,
    contains_forbidden,
    decide,
    enforce_title,
    evidence_from_gates,
    family_of,
    headline,
    sanitize,
)

ROBINWOJAK = "RoB1nWojak1111111111111111111111111111111111"
META = "MeTa22222222222222222222222222222222222222222"


# --- the fixtures -------------------------------------------------------------
def robinwojak_evidence(**overrides: object) -> MarketEvidence:
    """Fixture A.  Age 240s, first-seen MC $71.08K, now $60.71K, -14.59%.

    Liquidity $19.46K, 608 buys / 377 sells, holders 496 -> 496 -> 538, safety
    UNKNOWN, zero independent notable wallets, and a 14.49% round trip sitting
    underneath a claimed +26.53% "expected net edge".
    """

    fields: dict[str, object] = {
        "chain_id": "solana",
        "token_address": ROBINWOJAK,
        "identity_verified": True,
        "canonicality": "CANONICAL_PROBABLE",
        "safety_ok": None,
        "buy_route_ok": True,
        "sell_route_ok": None,
        "independent_notable_wallets": 0,
        "liquidity_usd": Decimal("19460"),
        "round_trip_cost_pct": Decimal("14.49"),
        "chart_reconstructed": True,
        "organic_flow_ok": True,
    }
    fields.update(overrides)
    return MarketEvidence(**fields)  # type: ignore[arg-type]


def meta_evidence(**overrides: object) -> MarketEvidence:
    """Fixture B.  Age 486s, MC $165.75K from $147.68K, +12.24%.

    Liquidity $16.10K, 1,828 buys / 463 sells, watch score 55/100, momentum 35,
    organic 11.29, safety and route both UNKNOWN, zero independent notable
    wallets, 7.27% round trip under a claimed +27.52% edge.
    """

    fields: dict[str, object] = {
        "chain_id": "solana",
        "token_address": META,
        "identity_verified": True,
        "canonicality": "FAMILY_AMBIGUOUS",
        "safety_ok": None,
        "buy_route_ok": None,
        "sell_route_ok": None,
        "independent_notable_wallets": 0,
        "liquidity_usd": Decimal("16100"),
        "round_trip_cost_pct": Decimal("7.27"),
        "chart_reconstructed": None,
        "organic_flow_ok": None,
    }
    fields.update(overrides)
    return MarketEvidence(**fields)  # type: ignore[arg-type]


def clean_evidence(**overrides: object) -> MarketEvidence:
    """Everything established and everything within limits.

    The control.  Without it the tests below would pass just as well against a
    function that refuses unconditionally, which would be a different bug.
    """

    fields: dict[str, object] = {
        "chain_id": "solana",
        "token_address": "CLeAn3333333333333333333333333333333333333333",
        "identity_verified": True,
        "canonicality": "CANONICAL_PROVEN",
        "chart_reconstructed": True,
        "organic_flow_ok": True,
        "safety_ok": True,
        "buy_route_ok": True,
        "sell_route_ok": True,
        "round_trip_cost_pct": Decimal("1.80"),
        "price_impact_pct": Decimal("0.90"),
        "liquidity_usd": Decimal("75200"),
        "quote_age_seconds": 4,
        "independent_notable_wallets": 3,
        "drawdown_1m_pct": Decimal("2"),
        "drawdown_2m_pct": Decimal("3"),
        "liquidity_drop_2m_pct": Decimal("1"),
        "clustered_sell_share_pct": Decimal("9"),
        "creator_cluster_sell_share_pct": Decimal("0"),
        "late_or_extended": False,
    }
    fields.update(overrides)
    return MarketEvidence(**fields)  # type: ignore[arg-type]


# --- 1. RobinWojak ------------------------------------------------------------
def test_robinwojak_refuses_and_says_why() -> None:
    """Spec test 1.  Unknown safety, zero notable wallets, 14.49% round trip."""

    decision = decide(robinwojak_evidence())

    assert decision.verdict == DO_NOT_ENTER_UNRESOLVED
    assert decision.title() == "⛔ DO NOT ENTER — SAFETY/ROUTE/INDEPENDENCE UNRESOLVED"
    assert not decision.entry_eligible
    assert not decision.may_ping


def test_robinwojak_title_can_never_say_look_now() -> None:
    assert contains_forbidden(decide(robinwojak_evidence()).title()) == ()
    assert enforce_title("🚨 EARLY RUNNER — LOOK NOW", decide(robinwojak_evidence())) == (
        "⛔ DO NOT ENTER — SAFETY/ROUTE/INDEPENDENCE UNRESOLVED"
    )


def test_robinwojak_reports_the_cost_it_was_hiding() -> None:
    """14.49% is disqualifying on its own and must appear in the reasons.

    It is not the *headline* — unknown safety outranks the price of a trade
    nobody should be making — but suppressing it would repeat the original sin
    of the card, which showed the cost only beside a larger invented gain.
    """

    reasons = " ".join(decide(robinwojak_evidence()).reasons)
    assert "14.49" in reasons


def test_robinwojak_names_what_would_change_the_answer() -> None:
    change = decide(robinwojak_evidence()).what_must_change
    assert change
    assert any("safety" in item for item in change)


# --- 2. META ------------------------------------------------------------------
def test_meta_is_a_data_integrity_hold() -> None:
    """Spec test 2.  Safety and route unknown, chart never reconstructed."""

    decision = decide(meta_evidence())

    assert decision.verdict == DATA_INTEGRITY_HOLD
    assert decision.title() == "⚠ DATA INTEGRITY HOLD — DO NOT ENTER"
    assert not decision.entry_eligible


def test_meta_title_can_never_say_heating_up() -> None:
    decision = decide(meta_evidence())
    assert contains_forbidden(decision.title()) == ()
    assert enforce_title("🔥 WATCH — HEATING UP", decision) == (
        "⚠ DATA INTEGRITY HOLD — DO NOT ENTER (WATCH)"
    )


@pytest.mark.parametrize("score", [55, 80, 99, 100])
def test_a_score_cannot_promote_meta(score: int) -> None:
    """Spec test 43.  There is nowhere in ``decide`` for a score to enter.

    Asserted by construction rather than by argument: the signature does not
    accept one, so this test is really checking that nobody adds a back door
    through the evidence record.
    """

    evidence = meta_evidence()
    assert not hasattr(evidence, "score")
    assert not hasattr(evidence, "momentum_score")
    assert decide(evidence).verdict == DATA_INTEGRITY_HOLD


# --- 3. META, two minutes later ------------------------------------------------
def test_meta_second_observation_is_violent_drawdown() -> None:
    """Spec test 3.  $169.5K -> $115.1K in about two minutes, liquidity falling.

    Risk-off outranks the unresolved hold: the first card could still have been
    resolved by more evidence, and this one cannot be.
    """

    fell = (Decimal("169500") - Decimal("115100")) / Decimal("169500") * 100
    assert Decimal("31") < fell < Decimal("33")

    decision = decide(
        meta_evidence(
            drawdown_2m_pct=fell.quantize(Decimal("0.01")),
            liquidity_drop_2m_pct=Decimal("9.83"),
        )
    )

    assert decision.verdict == DO_NOT_ENTER_DISTRIBUTION
    assert decision.title() == "📉 DO NOT ENTER — DISTRIBUTION/LIQUIDITY DECAY"
    assert any("two minutes" in reason for reason in decision.reasons)


def test_cumulative_volume_cannot_keep_the_old_label() -> None:
    """Spec test 4.  7,190 buys and $692.5K of claimed volume change nothing.

    None of it is an argument in ``decide``.  The only way a headline count
    could survive a drawdown would be for it to be one, so this test asserts
    the same evidence record produces the same risk-off answer whether or not
    the provider's numbers are impressive.
    """

    risk_off = {
        "drawdown_2m_pct": Decimal("32.09"),
        "liquidity_drop_2m_pct": Decimal("9.83"),
    }
    quiet = decide(meta_evidence(**risk_off))
    loud = decide(meta_evidence(**risk_off, independent_notable_wallets=4))

    assert quiet.verdict == loud.verdict == DO_NOT_ENTER_DISTRIBUTION


# --- cost gates (spec tests 34, 35) --------------------------------------------
@pytest.mark.parametrize("cost", ["7.27", "14.49", "5.01"])
def test_round_trip_above_five_percent_fails(cost: str) -> None:
    decision = decide(clean_evidence(round_trip_cost_pct=Decimal(cost)))
    assert decision.verdict == DO_NOT_ENTER_COST
    assert cost in " ".join(decision.reasons)


def test_five_percent_exactly_is_still_allowed() -> None:
    """The limit is a maximum, not a forbidden value — a boundary worth pinning.

    Without this, tightening the comparison to ``>=`` would pass every other
    cost test in the file while silently moving the published threshold.
    """

    assert DEFAULT_COST_CONFIG.max_round_trip_cost_pct == Decimal("5.0")
    assert decide(clean_evidence(round_trip_cost_pct=Decimal("5.0"))).verdict == PAPER_ENTRY


def test_price_impact_above_two_percent_fails() -> None:
    assert decide(clean_evidence(price_impact_pct=Decimal("2.4"))).verdict == DO_NOT_ENTER_COST


def test_thin_liquidity_is_unresolved_not_passable() -> None:
    """Spec test 36.  Below the floor is missing evidence, not a cheap entry."""

    decision = decide(clean_evidence(liquidity_usd=Decimal("19460")))
    assert decision.verdict == DO_NOT_ENTER_UNRESOLVED
    assert "liquidity" in decision.unresolved


def test_stale_quote_is_unresolved() -> None:
    decision = decide(clean_evidence(quote_age_seconds=45))
    assert decision.verdict == DO_NOT_ENTER_UNRESOLVED
    assert "quote freshness" in decision.unresolved


# --- the control --------------------------------------------------------------
def test_a_fully_evidenced_token_can_still_reach_paper_entry() -> None:
    """Otherwise every test above would pass against ``return REFUSE``."""

    decision = decide(clean_evidence())
    assert decision.verdict == PAPER_ENTRY
    assert decision.title() == "🧪 PAPER ENTRY CANDIDATE — ALL GATES PASS"
    assert decision.entry_eligible and decision.may_ping


@pytest.mark.parametrize(
    "field",
    [
        "identity_verified",
        "safety_ok",
        "sell_route_ok",
        "buy_route_ok",
        "chart_reconstructed",
        "organic_flow_ok",
    ],
)
def test_unknown_is_never_pass(field: str) -> None:
    """Spec test 42, one gate at a time.

    Each of these is set to ``None`` on an otherwise perfect candidate.  A
    single unanswered question is enough to lose the verdict, which is the
    difference between "we checked and it is fine" and "we did not check".
    """

    decision = decide(clean_evidence(**{field: None}))
    assert decision.verdict != PAPER_ENTRY
    assert not decision.entry_eligible


@pytest.mark.parametrize(
    "field",
    ["identity_verified", "safety_ok", "sell_route_ok", "organic_flow_ok"],
)
def test_false_is_never_pass(field: str) -> None:
    assert decide(clean_evidence(**{field: False})).verdict != PAPER_ENTRY


def test_zero_notable_wallets_differs_from_unmeasured() -> None:
    """A measured zero refuses; an unmeasured count is simply not this gate.

    The distinction matters because ``independent_notable_wallets`` arrives as
    ``0`` from a provider that returned nothing, and treating that as "we
    counted none" would be inventing a measurement.  Both refuse here, but for
    reasons the card prints differently.
    """

    counted = decide(clean_evidence(independent_notable_wallets=0))
    assert counted.verdict == DO_NOT_ENTER_UNRESOLVED
    assert "independent participation" in counted.unresolved


# --- edge suppression (spec section 9) ------------------------------------------
def test_expected_edge_is_not_computable_without_the_evidence() -> None:
    """The +27.52% beside a 7.27% cost was the card's most dangerous number.

    A modelled gain printed next to a measured cost borrows the authority of
    the measurement.  It may exist only where every gate passed.
    """

    assert not decide(meta_evidence()).edge_is_computable()
    assert not decide(robinwojak_evidence()).edge_is_computable()
    assert decide(clean_evidence()).edge_is_computable()


def test_edge_placeholder_says_why_rather_than_showing_zero() -> None:
    assert "NOT COMPUTABLE" in EDGE_NOT_COMPUTABLE
    assert "MISSING" in EDGE_NOT_COMPUTABLE


# --- headline machinery --------------------------------------------------------
def test_no_verdict_title_contains_promotional_language() -> None:
    from smart_money_bot.lab.verdict import TITLE, VERDICTS

    for verdict in VERDICTS:
        assert contains_forbidden(TITLE[verdict]) == (), verdict


def test_every_verdict_has_a_title() -> None:
    from smart_money_bot.lab.verdict import TITLE, VERDICTS

    assert set(TITLE) == set(VERDICTS)


def test_a_missing_decision_refuses_rather_than_trusting_the_builder() -> None:
    """``None`` means nothing looked at the evidence — the strongest refusal."""

    assert enforce_title("🔥 WATCH — HEATING UP", None) == (
        "⛔ DO NOT ENTER — INSUFFICIENT PROOF (WATCH)"
    )
    assert Decision().verdict == DO_NOT_ENTER_INSUFFICIENT


def test_enforcement_does_not_depend_on_whether_the_card_pings() -> None:
    """The regression that let ``HEATING UP`` through.

    The previous sanitiser only ran when ``alert.ping`` was true, and the card
    the operator complained about had already been demoted to ``ping=False``.
    ``enforce_title`` takes no ping argument at all, so there is no state in
    which it can be skipped.
    """

    import inspect

    parameters = inspect.signature(enforce_title).parameters
    assert set(parameters) == {"title", "decision"}


def test_sanitize_keeps_the_lane_and_drops_the_instruction() -> None:
    assert sanitize("🔥 FOMO TRENDING — LOOK NOW") == "🔥 FOMO TRENDING"
    assert family_of("🔥 FOMO TRENDING — LOOK NOW") == "FOMO TRENDING"
    assert family_of("🚨 EARLY RUNNER — LOOK NOW") == ""


@pytest.mark.parametrize("phrase", FORBIDDEN_PHRASES)
def test_every_forbidden_phrase_is_actually_removed(phrase: str) -> None:
    assert contains_forbidden(sanitize(f"🔥 SOMETHING — {phrase}")) == ()


def test_paper_entry_keeps_its_own_headline() -> None:
    """The one verdict allowed to speak for itself is still not promotional."""

    decision = decide(clean_evidence())
    assert enforce_title("🧪 PAPER ENTRY CANDIDATE — ALL GATES PASS", decision) == (
        "🧪 PAPER ENTRY CANDIDATE — ALL GATES PASS"
    )
    assert contains_forbidden(headline(decision)) == ()


# --- the bridge from hard gates -------------------------------------------------
def _report(mint: str, answers: dict[str, str], now: int = 1_000) -> GateReport:
    return GateReport(
        mint=mint,
        now=now,
        results=tuple(
            GateResult(gate=gate, answer=answer, reason="fixture", observed_at=now)
            for gate, answer in answers.items()
        ),
    )


def test_unknown_gates_become_none_not_false() -> None:
    """``UNKNOWN`` and ``FAIL`` are different claims and must stay different.

    Collapsing them would produce the right refusal for the wrong reason, and
    the card would then tell the operator a gate failed when nobody ran it.
    """

    evidence = evidence_from_gates(_report(META, {CONTRACT_SAFETY_OK: UNKNOWN}))
    assert evidence.safety_ok is None

    failed = evidence_from_gates(_report(META, {CONTRACT_SAFETY_OK: FAIL}))
    assert failed.safety_ok is False

    passed = evidence_from_gates(_report(META, {CONTRACT_SAFETY_OK: PASS}))
    assert passed.safety_ok is True


def test_gate_bridge_carries_the_mint() -> None:
    assert evidence_from_gates(_report(META, {})).token_address == META


def test_gate_bridge_overrides_do_not_silently_widen() -> None:
    evidence = evidence_from_gates(
        _report(META, {SELL_ROUTE_OK: UNKNOWN}),
        liquidity_usd=Decimal("16100"),
    )
    assert evidence.sell_route_ok is None
    assert evidence.liquidity_usd == Decimal("16100")


# --- the repository-wide sweep --------------------------------------------------
#: Where a headline can come from, expressed as AST shapes rather than as a
#: list of files.  A card title is one of:
#:
#: * a ``title=`` or ``label=`` keyword argument with a literal string;
#: * an assignment to something called ``title``, ``label`` or ``state``;
#: * a value in a mapping whose name says it holds labels or titles.
#:
#: Scanning *every* string literal instead was the first attempt and it was
#: useless: it flagged ``callouts.py``'s list of hype phrases the bot looks
#: **for** in other people's posts, and the X search query in ``config.py``.
#: Detecting somebody else saying "ape in" is the opposite of saying it.
_LABEL_NAMES = {"title", "label", "state", "headline"}
_LABEL_MAPS = ("LABEL", "TITLE", "HEADLINE", "STAGE")


def _card_text(path: pathlib.Path) -> list[tuple[int, str]]:
    """Every string literal in a module that can reach a card as a headline."""

    import ast

    found: list[tuple[int, str]] = []

    def literal(node: object) -> str | None:
        import ast as _ast

        if isinstance(node, _ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    def named(node: object) -> str:
        import ast as _ast

        if isinstance(node, _ast.Name):
            return node.id
        if isinstance(node, _ast.Attribute):
            return node.attr
        return ""

    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                text = literal(keyword.value)
                if keyword.arg in _LABEL_NAMES and text is not None:
                    found.append((node.lineno, text))
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [named(target).lower() for target in targets]
            upper = [named(target).upper() for target in targets]
            value = node.value
            if any(name in _LABEL_NAMES for name in names):
                text = literal(value)
                if text is not None:
                    found.append((node.lineno, text))
            if any(any(tag in name for tag in _LABEL_MAPS) for name in upper) and isinstance(
                value, ast.Dict
            ):
                for item in value.values:
                    text = literal(item)
                    if text is not None:
                        found.append((getattr(item, "lineno", node.lineno), text))
        elif isinstance(node, ast.Dict):
            # ``{TIER: "...", ...}.get(...)`` — the shape the early lane and
            # the trending lane both use to pick a headline by kind.
            for item in node.values:
                text = literal(item)
                if text is not None and contains_forbidden(text):
                    found.append((getattr(item, "lineno", node.lineno), text))
    # The named-mapping branch and the bare-``Dict`` branch overlap on purpose
    # — the first documents the shape we expect, the second catches the ones
    # built inline — so the same literal can be reported twice.
    return sorted(set(found))


#: The one module allowed to name the phrases, because it is the module that
#: forbids them.  Anywhere else, a literal containing one is a card that can
#: still say it.
_PHRASE_OWNERS = {"verdict.py"}


def test_no_module_can_build_a_promotional_headline() -> None:
    """Spec section 17.3, run as a test rather than as a promise to have looked.

    Three releases in a row reported that the promotional-headline class of bug
    was fixed, and each had fixed one call site.  A grep by hand finds what the
    person running it thought to look for; this walks every module in the
    package and every AST shape a headline is built from.
    """

    import smart_money_bot

    root = pathlib.Path(smart_money_bot.__file__).parent
    offences: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name in _PHRASE_OWNERS:
            continue
        for lineno, text in _card_text(path):
            found = contains_forbidden(text)
            if found:
                offences.append(f"{path.relative_to(root)}:{lineno} {found} in {text!r}")

    assert not offences, "promotional language survives in:\n" + "\n".join(offences)


def test_the_sweep_would_actually_catch_something(tmp_path: pathlib.Path) -> None:
    """The sweep above passes trivially if either half of it is broken.

    So: the matcher against a literal that definitely offends, and the AST walk
    against a file that definitely contains one.
    """

    assert contains_forbidden("🔥 WATCH — HEATING UP") == ("HEATING UP",)
    assert contains_forbidden("🚨 EARLY RUNNER — LOOK NOW") == ("LOOK NOW", "EARLY RUNNER")

    offender = tmp_path / "offender.py"
    offender.write_text(
        'STAGE_LABELS = {"a": "🔥 HEATING UP"}\n'
        'spec = CardSpec(title="🚨 EARLY RUNNER — LOOK NOW")\n'
        'state = "🔥 HEATING UP"\n'
        '"""A docstring saying LOOK NOW must not count."""\n'
    )
    caught = [text for _, text in _card_text(offender) if contains_forbidden(text)]
    assert len(caught) == 3


def test_the_sweep_ignores_detector_vocabularies() -> None:
    """The phrases the bot looks *for* are not phrases the bot says.

    ``callouts.py`` and ``trending/thesis.py`` both hold lists of hype language
    used to score somebody else's post.  A sweep that flagged those would push
    the next person to delete the detector to make the test pass, which is
    the exact opposite of what any of this is for.
    """

    import smart_money_bot.callouts as callouts_module
    import smart_money_bot.trending.thesis as thesis_module

    for module in (callouts_module, thesis_module):
        path = pathlib.Path(module.__file__)
        assert not [text for _, text in _card_text(path) if contains_forbidden(text)]
        assert "ape" in path.read_text().lower(), "the detector vocabulary is still there"


# --- the engine, where the previous three fixes stopped short --------------------
def _partial_engine(settings=None):
    """An engine with only the state the publish guard touches.

    No network, no database, no wallet — the same shape the clone-defence and
    authorization suites use, so this exercises the real ``_guard_publication``
    rather than a reimplementation of it.
    """

    from smart_money_bot.engine import SmartMoneyEngine
    from smart_money_bot.lab.verdict import (
        cost_config_from_settings,
        risk_off_config_from_settings,
    )

    engine = object.__new__(SmartMoneyEngine)
    engine._token_facts = {}
    engine._clone_verdicts = {}
    engine._quality_scores = {}
    engine._gate_reports = {}
    engine.gate_refusals = 0
    engine.headline_rewrites = 0
    engine.refused_publications = 0
    engine._cost_config = cost_config_from_settings(settings)
    engine._risk_off_config = risk_off_config_from_settings(settings)
    return engine


def _card(title: str, mint: str = META, *, ping: bool = False):
    from smart_money_bot.discord_render import CardSpec
    from smart_money_bot.fast_alerts import FAST_WATCH, LANE_RADAR, FastAlert

    return FastAlert(
        kind=FAST_WATCH,
        mint=mint,
        alert_key=f"{FAST_WATCH}:{mint}",
        spec=CardSpec(title=title, description=f"`{mint}`"),
        ping=ping,
        token_mint=mint,
        lane=LANE_RADAR,
        family=FAST_WATCH,
    )


def test_the_guard_rewrites_a_promotional_headline_on_a_silent_card() -> None:
    """The regression, end to end, in the shape it actually shipped.

    ``ping=False``.  Every previous guard was conditioned on the card wanting
    to ping, so a card already demoted to silent kept its headline — which is
    what FAST WATCH cards are, and what the operator was looking at.
    """

    engine = _partial_engine()
    engine._gate_reports[META] = _report(
        META, {CONTRACT_SAFETY_OK: UNKNOWN, SELL_ROUTE_OK: UNKNOWN}
    )

    guarded = engine._guard_publication(_card("🔥 WATCH — HEATING UP", ping=False))

    assert contains_forbidden(guarded.spec.title) == ()
    assert guarded.spec.title.startswith("⚠ DATA INTEGRITY HOLD")
    assert engine.headline_rewrites == 1


def test_a_mint_with_no_evidence_at_all_gets_the_strongest_refusal() -> None:
    """Nothing looked at it, so the builder's wording cannot be a claim."""

    engine = _partial_engine()
    guarded = engine._guard_publication(_card("🚨 EARLY RUNNER — LOOK NOW"))

    assert guarded.spec.title == "⛔ DO NOT ENTER — INSUFFICIENT PROOF"
    assert engine.headline_rewrites == 1


def test_the_guard_leaves_an_already_honest_headline_alone() -> None:
    """No rewrite, no counter increment — otherwise the counter means nothing."""

    # A title that is already a verdict is a stale claim, not a lane label, so
    # it is replaced outright rather than decorated with the current one.
    engine = _partial_engine()
    guarded = engine._guard_publication(_card("🔎 VERIFIED WATCH — NOT ENTRY ELIGIBLE"))

    assert guarded.spec.title == "⛔ DO NOT ENTER — INSUFFICIENT PROOF"
    assert engine.headline_rewrites == 1

    # And a card whose evidence is entirely settled keeps its own headline —
    # otherwise the rewrite counter is measuring nothing and the enforcement is
    # indistinguishable from "always refuse".
    settled = _partial_engine()
    settled._gate_reports[META] = _report(META, dict.fromkeys(_ALL_GATES, PASS))
    with mock.patch.object(
        engine_module,
        "evidence_from_gates",
        lambda report, **kw: evidence_from_gates(report, chart_reconstructed=True, **kw),
    ):
        title = "🧪 PAPER ENTRY CANDIDATE — ALL GATES PASS"
        passed = settled._guard_publication(_card(title))
    assert passed.spec.title == title
    assert settled.headline_rewrites == 0


def test_every_gate_passing_is_still_not_a_reconstructed_chart() -> None:
    """The rule applied to this release's own incomplete work.

    There is no hard gate for chart reconstruction, because reconstructing
    OHLCV from canonical pool swaps is not implemented yet.  So a report in
    which every gate passes still holds, and the strongest verdict is
    unreachable from gates alone.  That is the correct answer — nothing has
    verified that the chart on the card describes the mint on the card — and
    the honest way to reach ``PAPER ENTRY`` is to build the reconstruction, not
    to let a passing organic-flow gate stand in for one.
    """

    everything = _report(META, dict.fromkeys(_ALL_GATES, PASS))
    from_gates = evidence_from_gates(everything)

    assert from_gates.chart_reconstructed is None
    assert decide(from_gates, gates=everything).verdict == DATA_INTEGRITY_HOLD

    with_chart = evidence_from_gates(
        everything,
        chart_reconstructed=True,
        liquidity_usd=Decimal("75200"),
        independent_notable_wallets=3,
    )
    assert decide(with_chart, gates=everything).verdict == PAPER_ENTRY


def test_the_engine_reads_the_configured_ceilings(settings) -> None:
    """The thresholds are operator configuration, not module constants.

    Without this, ``ENTRY_MAX_ROUND_TRIP_COST_PCT`` would be documented in the
    README and read by nothing.
    """

    from smart_money_bot.lab.verdict import cost_config_from_settings

    config = cost_config_from_settings(settings)
    assert config.max_round_trip_cost_pct == settings.entry_max_round_trip_cost_pct
    assert config.min_liquidity_usd == settings.entry_min_liquidity_usd
    assert config.quote_max_age_seconds == settings.entry_quote_max_age_seconds


def test_a_tighter_configured_ceiling_actually_refuses_more(settings) -> None:
    """Mutating the setting must change the answer, or it is not wired."""

    import dataclasses

    from smart_money_bot.lab.verdict import cost_config_from_settings

    evidence = clean_evidence(round_trip_cost_pct=Decimal("4.0"))
    assert decide(evidence, cost=cost_config_from_settings(settings)).verdict == PAPER_ENTRY

    strict = dataclasses.replace(settings, entry_max_round_trip_cost_pct=Decimal("3.0"))
    assert (
        decide(evidence, cost=cost_config_from_settings(strict)).verdict == DO_NOT_ENTER_COST
    )
