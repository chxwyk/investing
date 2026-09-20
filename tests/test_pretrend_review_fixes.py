"""Regression tests for the v2.55 code-review findings.

Each test below fails against the code as originally submitted. They are kept
together, and named after the defect rather than the feature, so that a future
change that reintroduces one is reported as the specific mistake it is.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import smart_money_bot.alert_policy as policy
from smart_money_bot.database import Database
from smart_money_bot.pretrend.groundtruth import (
    FOMO_TREND_ENTER,
    GRADE_PROXY,
    PROXY_BOARD_ENTER,
    BoardRow,
    GroundTruthConfig,
    TrendingGroundTruth,
    TrendingSnapshot,
)
from smart_money_bot.pretrend.labels import (
    INELIGIBLE_ENTRY_UNPROVEN,
    label_observation,
)
from smart_money_bot.pretrend.providers import BoardObserver
from smart_money_bot.pretrend_runtime import PretrendConfig, PretrendRuntime
from smart_money_bot.pretrend_store import PretrendStore


def mint(seed: str) -> str:
    return (seed * 44)[:44]


ALPHA = mint("A")
BRAVO = mint("B")


def filler(count: int) -> list[str]:
    return [mint(chr(ord("d") + index)) for index in range(count)]


def board(mints: list[str], *, at: int, kind: str = "FOMO_TRENDING") -> TrendingSnapshot:
    return TrendingSnapshot(
        observed_at=at,
        rows=tuple(
            BoardRow(mint=item, rank=index, market_cap_usd=Decimal("50000"))
            for index, item in enumerate(mints, start=1)
        ),
        provider="test",
        source_kind=kind,
    )


@pytest.fixture
async def store(tmp_path):
    database = Database(str(tmp_path / "review.db"), Decimal("1000"))
    await database.connect()
    try:
        yield PretrendStore(database)
    finally:
        await database.close()


class _Observation:
    def __init__(self, mint_value: str, rank: int) -> None:
        self.mint = mint_value
        self.rank = rank
        self.symbol = "SYM"
        self.name = "Name"
        self.market_cap_usd = Decimal("50000")
        self.price_usd = Decimal("0.0001")
        self.liquidity_usd = Decimal("20000")
        self.holder_count = 300
        self.source = None


class _FakeClient:
    def __init__(self, pages) -> None:
        self._pages = list(pages)
        self.last_error = ""

    async def fetch_board(self, *, limit: int):
        return self._pages.pop(0) if self._pages else []


def _observer(pages, *, kind: str = "FOMO_TRENDING") -> BoardObserver:
    return BoardObserver(_FakeClient(pages), provider="test", source_kind=kind)


def _rows(mints: list[str]):
    return [_Observation(item, index) for index, item in enumerate(mints, start=1)]


# =========================================================================
# FINDING 1 — collection-only mode must be genuinely silent
# =========================================================================
async def test_finding1_the_silent_default_publishes_no_confirmation(store) -> None:
    """Originally: ``_confirm_entries`` published regardless of the setting.

    The shipped default has alerting off, yet the confirmation card was pushed
    for every board entry -- and on a first snapshot, once per row.
    """

    observer = _observer([_rows(filler(6)), _rows([ALPHA, *filler(6)])])
    published: list = []

    async def confirm(confirmation):
        published.append(confirmation)
        return True

    runtime = PretrendRuntime(
        store, observer=observer, config=PretrendConfig(), confirm=confirm
    )
    await runtime.collect_board(now=1_000)
    result = await runtime.collect_board(now=1_030)

    assert ALPHA in result.entered, "ground truth is still collected"
    assert published == [], "but nothing reached the notifier"
    assert runtime.suppressed_confirmations == 1
    assert not runtime.may_send


async def test_finding1_a_silent_confirmation_is_not_logged_as_delivered(store) -> None:
    """Originally: the alert row was written before the send was even attempted."""

    observer = _observer([_rows(filler(6)), _rows([ALPHA, *filler(6)])])
    runtime = PretrendRuntime(store, observer=observer, config=PretrendConfig())
    await runtime.collect_board(now=1_000)
    await runtime.collect_board(now=1_030)

    assert await store.recent_alert_times(since=0) == ()
    rate = await store.alert_rate(since=0)
    assert rate["by_kind"] == {}, "an unsent card must not enter the alert-rate metric"


async def test_finding1_a_refused_delivery_is_not_logged_as_delivered(store) -> None:
    observer = _observer([_rows(filler(6)), _rows([ALPHA, *filler(6)])])

    async def refuse(confirmation):
        return False

    runtime = PretrendRuntime(
        store,
        observer=observer,
        config=PretrendConfig(alerting_enabled=True),
        confirm=refuse,
    )
    await runtime.collect_board(now=1_000)
    await runtime.collect_board(now=1_030)

    assert await store.recent_alert_times(since=0) == ()
    assert runtime.undelivered_confirmations == 1


async def test_finding1_enabling_alerting_does_publish(store) -> None:
    """The gate must be a gate, not an off switch."""

    observer = _observer([_rows(filler(6)), _rows([ALPHA, *filler(6)])])
    published: list = []

    async def confirm(confirmation):
        published.append(confirmation)
        return True

    runtime = PretrendRuntime(
        store,
        observer=observer,
        config=PretrendConfig(alerting_enabled=True),
        confirm=confirm,
    )
    await runtime.collect_board(now=1_000)
    await runtime.collect_board(now=1_030)

    assert [item.mint for item in published] == [ALPHA]
    assert len(await store.recent_alert_times(since=0)) == 0, "confirmations are their own kind"
    rate = await store.alert_rate(since=0)
    assert rate["by_kind"]["TRENDING_CONFIRMED"]["count"] == 1


# =========================================================================
# FINDING 2 — proxy data must never become FOMO ground truth
# =========================================================================
def test_finding2_a_proxy_board_emits_proxy_events_only() -> None:
    """Originally: a TRENDING_PROXY snapshot emitted FOMO_TREND_ENTER."""

    truth = TrendingGroundTruth()
    truth.ingest(board(filler(6), at=1_000, kind="TRENDING_PROXY"))
    outcome = truth.ingest(board([ALPHA, *filler(6)], at=1_030, kind="TRENDING_PROXY"))

    kinds = {event.kind for event in outcome.events}
    assert kinds == {PROXY_BOARD_ENTER}
    assert FOMO_TREND_ENTER not in kinds
    assert outcome.grade == GRADE_PROXY
    assert not outcome.establishes_labels
    assert outcome.label_events == ()


def test_finding2_a_proxy_sighting_yields_no_fomo_label() -> None:
    truth = TrendingGroundTruth()
    truth.ingest(board(filler(6), at=1_000, kind="TRENDING_PROXY"))
    truth.ingest(board([ALPHA, *filler(6)], at=1_030, kind="TRENDING_PROXY"))

    assert truth.first_trending_at(ALPHA) is None
    assert truth.label_map() == {}
    assert ALPHA in truth.proxy_only_mints()
    assert "PROXY" in truth.describe(ALPHA)


async def test_finding2_proxy_rows_are_preserved_as_evidence(store) -> None:
    """Excluded from labels, but not discarded: raw evidence is kept."""

    observer = _observer(
        [_rows(filler(6)), _rows([ALPHA, *filler(6)])], kind="TRENDING_PROXY"
    )
    runtime = PretrendRuntime(store, observer=observer, config=PretrendConfig())
    await runtime.collect_board(now=1_000)
    await runtime.collect_board(now=1_030)

    assert await store.first_trending_map() == {}, "no FOMO labels from proxy"
    cursor = await store.database.db.execute(
        "SELECT kind, grade, COUNT(*) AS n FROM pretrend_trend_events GROUP BY kind, grade"
    )
    rows = {(r["kind"], r["grade"]): int(r["n"]) for r in await cursor.fetchall()}
    assert rows, "proxy events are still recorded"
    assert all(grade == GRADE_PROXY for _, grade in rows)

    cursor = await store.database.db.execute(
        "SELECT COUNT(*) FROM pretrend_board_rows"
    )
    assert (await cursor.fetchone())[0] > 0, "raw board rows are preserved"


async def test_finding2_switching_proxy_to_fomo_does_not_inherit_proxy_state(
    store,
) -> None:
    """A source change must start the FOMO namespace clean."""

    truth = TrendingGroundTruth()
    truth.ingest(board(filler(6), at=1_000, kind="TRENDING_PROXY"))
    truth.ingest(board([ALPHA, *filler(6)], at=1_030, kind="TRENDING_PROXY"))

    # The operator configures an authorised feed. ALPHA is on that board too,
    # but we have never witnessed it arrive THERE.
    first_fomo = truth.ingest(board([ALPHA, *filler(6)], at=1_060, kind="FOMO_TRENDING"))
    assert first_fomo.entered == (), "a proxy sighting is not a FOMO entry"
    assert ALPHA in first_fomo.present_unproven
    assert truth.first_trending_at(ALPHA) is None

    # A genuine, witnessed FOMO arrival does produce a label.
    second = truth.ingest(board([ALPHA, BRAVO, *filler(6)], at=1_090, kind="FOMO_TRENDING"))
    assert BRAVO in second.entered
    assert truth.first_trending_at(BRAVO) == 1_090
    assert truth.label_map() == {BRAVO: 1_090}


async def test_finding2_a_proxy_only_deployment_reports_zero_usable_labels(
    store,
) -> None:
    observer = _observer(
        [_rows(filler(6)), _rows([ALPHA, *filler(6)])], kind="TRENDING_PROXY"
    )
    runtime = PretrendRuntime(store, observer=observer, config=PretrendConfig())
    await runtime.collect_board(now=1_000)
    await runtime.collect_board(now=1_030)

    status = await runtime.status(now=2_000)
    assert not status["label_source"]["authorised_for_fomo_labels"]
    assert status["labels"]["usable_labels"] == 0
    assert "PROXY" in status["label_source"]["detail"]


# =========================================================================
# FINDING 3 — presence at startup is not entry
# =========================================================================
def test_finding3_the_first_snapshot_claims_no_entries() -> None:
    """Originally: every mint on the first snapshot got a fabricated entry."""

    truth = TrendingGroundTruth()
    outcome = truth.ingest(board([ALPHA, *filler(6)], at=1_000))

    assert outcome.entered == ()
    assert len(outcome.present_unproven) == 7
    assert outcome.after_coverage_gap
    assert truth.label_map() == {}


def test_finding3_a_coverage_gap_voids_the_claim() -> None:
    config = GroundTruthConfig(max_coverage_gap_seconds=180)
    truth = TrendingGroundTruth(config=config)
    truth.ingest(board(filler(6), at=1_000))
    truth.ingest(board(filler(6), at=1_030))

    # 400s later: the token could have arrived at any point in between.
    outcome = truth.ingest(board([ALPHA, *filler(6)], at=1_430))
    assert outcome.after_coverage_gap
    assert outcome.entered == ()
    assert ALPHA in outcome.present_unproven
    assert truth.first_trending_at(ALPHA) is None
    assert "coverage gap" in truth.describe(ALPHA)


def test_finding3_a_short_gap_still_witnesses_the_entry() -> None:
    """The guard must not void ordinary missed beats."""

    config = GroundTruthConfig(max_coverage_gap_seconds=180)
    truth = TrendingGroundTruth(config=config)
    truth.ingest(board(filler(6), at=1_000))
    outcome = truth.ingest(board([ALPHA, *filler(6)], at=1_090))  # 90s

    assert not outcome.after_coverage_gap
    assert ALPHA in outcome.entered
    assert truth.first_trending_at(ALPHA) == 1_090


def test_finding3_first_observed_is_recorded_even_when_entry_is_not() -> None:
    truth = TrendingGroundTruth()
    truth.ingest(board([ALPHA, *filler(6)], at=1_000))
    record = truth.record(ALPHA)

    assert record is not None
    assert record.first_observed_on_board_at == 1_000
    assert record.first_trending_at is None
    assert not record.entry_proven
    assert not record.usable_as_label


def test_finding3_an_unproven_mint_is_excluded_from_labels_in_both_directions() -> None:
    """Not a positive (entry unknown), and not a control (it WAS on the board)."""

    labelled = label_observation(
        mint=ALPHA,
        observed_at=900,
        first_trending_at=None,
        market_cap_usd=Decimal("50000"),
        universe_min_usd=Decimal("20000"),
        universe_max_usd=Decimal("1000000"),
        data_complete_until=99_999,
        entry_unproven=True,
    )
    assert not labelled.eligible
    assert labelled.ineligible_reason == INELIGIBLE_ENTRY_UNPROVEN
    assert labelled.labels == {}


def test_finding3_an_unproven_mint_is_never_used_as_a_control() -> None:
    from smart_money_bot.pretrend.controls import CandidateRow, build_matched_controls

    positive = CandidateRow(
        mint=ALPHA,
        observed_at=1_000,
        market_cap_usd=Decimal("40000"),
        token_age_seconds=200,
        eligible=True,
        first_trending_at=1_200,
    )
    # Looks like a perfect control, but we saw it sitting on the board.
    tainted = CandidateRow(
        mint=BRAVO,
        observed_at=1_010,
        market_cap_usd=Decimal("41000"),
        token_age_seconds=210,
        eligible=True,
        seen_on_board=True,
    )
    clean = [
        CandidateRow(
            mint=mint(chr(ord("m") + index)),
            observed_at=1_000 + index * 10,
            market_cap_usd=Decimal("41000"),
            token_age_seconds=210,
            eligible=True,
        )
        for index in range(6)
    ]

    sample = build_matched_controls([positive, tainted, *clean], horizon_seconds=300)
    chosen = {row.mint for pair in sample.pairs for row in pair.controls}
    assert BRAVO not in chosen
    assert chosen, "the clean controls are still used"


async def test_finding3_a_restart_within_the_gap_keeps_witnessing(store) -> None:
    """A quick redeploy must not void a genuine entry..."""

    observer = _observer([_rows(filler(6))])
    first = PretrendRuntime(store, observer=observer, config=PretrendConfig())
    await first.collect_board(now=1_000)

    resumed = PretrendRuntime(
        store,
        observer=_observer([_rows([ALPHA, *filler(6)])]),
        config=PretrendConfig(),
    )
    result = await resumed.collect_board(now=1_060)  # 60s later
    assert ALPHA in result.entered
    assert (await store.first_trending_map())[ALPHA] == 1_060


async def test_finding3_a_long_outage_across_a_restart_voids_the_claim(store) -> None:
    """...but a long one must, because the token may have arrived meanwhile."""

    first = PretrendRuntime(
        store, observer=_observer([_rows(filler(6))]), config=PretrendConfig()
    )
    await first.collect_board(now=1_000)

    resumed = PretrendRuntime(
        store,
        observer=_observer([_rows([ALPHA, *filler(6)])]),
        config=PretrendConfig(),
    )
    result = await resumed.collect_board(now=1_000 + 3_600)
    assert result.after_coverage_gap
    assert result.entered == ()
    assert await store.first_trending_map() == {}


async def test_finding3_a_missed_first_entry_stays_missed_permanently(
    store,
) -> None:
    """A witnessed RETURN is not a first entry, and must not be promoted to one.

    Tempting shortcut: a mint we found on the board leaves and comes back, we
    see the return, so we call that its entry. Wrong -- its real first entry
    happened before we were watching. Recording the return would understate how
    long it had been on the board and flatter every lead time measured against
    it. The honest outcome is that this mint is permanently unusable as a label.
    """

    runtime = PretrendRuntime(
        store,
        observer=_observer(
            [
                _rows([ALPHA, *filler(6)]),   # found there; arrival never seen
                _rows(filler(6)),             # leaves
                _rows([ALPHA, *filler(6)]),   # returns, witnessed
            ]
        ),
        config=PretrendConfig(),
    )
    await runtime.collect_board(now=1_000)
    await runtime.collect_board(now=1_030)
    result = await runtime.collect_board(now=1_060)

    assert ALPHA in result.reentered, "the return itself is real and recorded"
    record = runtime.ground_truth.record(ALPHA)
    assert record is not None
    assert record.entries == 2, "the stint is counted"
    assert not record.entry_proven, "but the FIRST entry is still unknown"
    assert record.first_trending_at is None
    assert await store.first_trending_map() == {}
    assert ALPHA in await store.entry_unproven_mints()


async def test_finding3_no_sql_anywhere_updates_the_first_entry_timestamp() -> None:
    """The strictest form of write-once: no UPDATE sets the column at all."""

    import inspect
    from pathlib import Path

    import smart_money_bot.pretrend_store as store_module

    sql = Path(inspect.getfile(store_module)).read_text()
    assert "SET first_trending_at" not in sql
    assert "promote_membership_entry" not in sql


# =========================================================================
# FINDING 4 — a documented alert policy across every lane
# =========================================================================
def test_finding4_every_alert_class_has_a_disposition_in_every_mode() -> None:
    from smart_money_bot.fast_alerts import ALERT_CLASSES

    for mode in policy.ALERT_MODES:
        described = policy.describe_policy(mode)
        covered = set(
            described["may_ping"] + described["radar_only"] + described["suppressed"]
        )
        assert covered == set(ALERT_CLASSES), f"{mode} does not classify every class"


def test_finding4_curated_lets_only_the_pretrend_lane_interrupt() -> None:
    described = policy.describe_policy(policy.MODE_CURATED)
    assert set(described["may_ping"]) == policy.PRETREND_CLASSES
    assert described["suppressed"] == [], "legacy lanes stay visible, just quiet"
    assert len(described["radar_only"]) > 10


def test_finding4_silent_publishes_nothing_and_ground_truth_publishes_one() -> None:
    assert policy.describe_policy(policy.MODE_SILENT)["may_ping"] == []
    assert policy.describe_policy(policy.MODE_SILENT)["radar_only"] == []
    ground = policy.describe_policy(policy.MODE_GROUND_TRUTH)
    assert ground["may_ping"] == ["TRENDING_CONFIRMED"]


def test_finding4_legacy_is_the_default_and_preserves_prior_behaviour() -> None:
    """Shipping the policy must not silently change any existing lane."""

    from smart_money_bot.fast_alerts import PINGABLE

    assert policy.DEFAULT_ALERT_MODE == policy.MODE_LEGACY
    described = policy.describe_policy(policy.MODE_LEGACY)
    assert set(described["may_ping"]) == set(PINGABLE)
    assert described["suppressed"] == []


def test_finding4_an_unknown_mode_fails_open_to_legacy() -> None:
    """A typo must not silently switch an operator's alerting off."""

    assert policy.normalise_mode("CURATD") == policy.MODE_LEGACY
    assert policy.normalise_mode(None) == policy.MODE_LEGACY
    assert policy.normalise_mode("curated") == policy.MODE_CURATED
    assert policy.normalise_mode("ground-truth") == policy.MODE_GROUND_TRUTH


def test_finding4_no_mode_ever_affects_collection() -> None:
    for mode in policy.ALERT_MODES:
        assert policy.describe_policy(mode)["collection_affected"] is False


def test_finding4_watch_has_no_publisher_in_any_mode() -> None:
    """"Silent WATCH" means the state has no card, not that its card is hidden."""

    from smart_money_bot.fast_alerts import ALERT_CLASSES

    assert not any(kind == "WATCH" for kind in ALERT_CLASSES)
    from smart_money_bot.pretrend.states import PINGING_STATES, STATE_WATCH

    assert STATE_WATCH not in PINGING_STATES


def test_finding4_the_policy_is_applied_at_the_single_choke_point() -> None:
    import inspect
    from pathlib import Path

    import smart_money_bot.engine as engine_module

    source = Path(inspect.getfile(engine_module)).read_text()
    dispatch = source[source.index("async def _dispatch_card(") :]
    dispatch = dispatch[: dispatch.index("\n    def _resolve_alert_mode(")]
    assert "alert_policy_decide(" in dispatch, (
        "the policy must be applied where every card passes, not per-lane"
    )
    assert source.count("alert_policy_decide(") == 1
