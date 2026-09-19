"""Persistence, migrations, restart safety and the runtime's end-to-end path.

The properties that only exist at the storage boundary: write-once columns that
no code path can move, append-only observations, a cooldown that survives a
redeploy, and a schema block that re-runs harmlessly on every boot.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from smart_money_bot.database import Database
from smart_money_bot.pretrend.activity import ACTIVITY_BUY, ActivityEvent
from smart_money_bot.pretrend.groundtruth import (
    BoardRow,
    TrendingGroundTruth,
    TrendingSnapshot,
)
from smart_money_bot.pretrend.providers import (
    BoardObserver,
    build_activity_provider,
)
from smart_money_bot.pretrend.states import STATE_PRE_TREND, TokenState
from smart_money_bot.pretrend_runtime import PretrendConfig, PretrendRuntime
from smart_money_bot.pretrend_store import PretrendStore


def mint(seed: str) -> str:
    return (seed * 44)[:44]


ALPHA = mint("A")
BRAVO = mint("B")


def filler(count: int) -> list[str]:
    return [mint(chr(ord("d") + index)) for index in range(count)]


@pytest.fixture
async def store(tmp_path):
    database = Database(str(tmp_path / "pretrend.db"), Decimal("1000"))
    await database.connect()
    try:
        yield PretrendStore(database)
    finally:
        await database.close()


def board(mints: list[str], *, at: int, **kwargs) -> TrendingSnapshot:
    return TrendingSnapshot(
        observed_at=at,
        rows=tuple(
            BoardRow(mint=item, rank=index, market_cap_usd=Decimal("50000"))
            for index, item in enumerate(mints, start=1)
        ),
        provider="test",
        source_kind="FOMO_TRENDING",
        **kwargs,
    )


# --- migrations --------------------------------------------------------------
async def test_the_schema_block_is_idempotent(tmp_path) -> None:
    """A redeploy re-runs it on every boot; it must be harmless every time."""

    path = str(tmp_path / "twice.db")
    for _ in range(2):
        database = Database(path, Decimal("1000"))
        await database.connect()
        cursor = await database.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'pretrend%'"
        )
        names = {row[0] for row in await cursor.fetchall()}
        await database.close()

    assert "pretrend_membership" in names
    assert "pretrend_observations" in names
    assert "pretrend_predictions" in names
    assert len(names) >= 15


async def test_the_new_tables_do_not_disturb_existing_ones(tmp_path) -> None:
    database = Database(str(tmp_path / "coexist.db"), Decimal("1000"))
    await database.connect()
    cursor = await database.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    names = {row[0] for row in await cursor.fetchall()}
    await database.close()
    # The pre-existing production tables are still present alongside the new ones.
    for table in ("trending_tokens", "pump_tokens", "paper_trades", "lab_positions"):
        assert table in names


# --- write-once discipline ---------------------------------------------------
async def test_first_trending_at_cannot_be_moved_by_a_later_write(store) -> None:
    truth = TrendingGroundTruth()
    others = filler(6)
    outcome = truth.ingest(board([ALPHA, *others], at=1_000))
    await store.record_trend_events(outcome.events)
    await store.upsert_membership(list(truth.records.values()))

    # Leave and return, then persist again -- the path a re-entry takes.
    truth.ingest(board(others + [mint("z")], at=1_060))
    again = truth.ingest(board([ALPHA, *others], at=1_120))
    await store.record_trend_events(again.events)
    await store.upsert_membership(list(truth.records.values()))

    assert (await store.first_trending_map())[ALPHA] == 1_000

    # And a direct attempt to write a different first entry is ignored.
    await store.upsert_membership(
        [
            type(truth.record(ALPHA))(
                mint=ALPHA,
                first_trending_at=9_999,
                first_rank=1,
                first_market_cap_usd=Decimal("1"),
            )
        ]
    )
    assert (await store.first_trending_map())[ALPHA] == 1_000


async def test_a_cross_source_first_seen_is_written_once(store) -> None:
    await store.note_first_seen(ALPHA, "pumpfun", at=500, market_cap_usd=Decimal("20000"))
    await store.note_first_seen(ALPHA, "pumpfun", at=900, market_cap_usd=Decimal("80000"))
    assert (await store.first_seen_for(ALPHA))["pumpfun"] == 500


async def test_observations_are_append_only(store) -> None:
    """Rewriting a past reading turns a history into a snapshot of now."""

    await store.record_observation(
        mint=ALPHA, observed_at=1_000, market_cap_usd=Decimal("50000")
    )
    await store.record_observation(
        mint=ALPHA, observed_at=1_000, market_cap_usd=Decimal("500000")
    )
    rows = await store.observations_for(ALPHA)
    assert len(rows) == 1
    assert rows[0]["market_cap_usd"] == 50_000.0


async def test_an_actors_first_action_on_a_mint_is_written_once(store) -> None:
    """A later action must not shrink the measured lead time."""

    early = ActivityEvent(
        event_id="e1",
        mint=ALPHA,
        occurred_at=1_000,
        event_type=ACTIVITY_BUY,
        trader_id="trader-1",
        amount_usd=Decimal("25"),
    )
    later = ActivityEvent(
        event_id="e2",
        mint=ALPHA,
        occurred_at=1_500,
        event_type=ACTIVITY_BUY,
        trader_id="trader-1",
        amount_usd=Decimal("25"),
    )
    await store.record_activity([early, later])
    observations = await store.actor_observations()
    assert len(observations) == 1
    assert observations[0].observed_at == 1_000


async def test_duplicate_tape_events_are_stored_once(store) -> None:
    event = ActivityEvent(
        event_id="dupe",
        mint=ALPHA,
        occurred_at=1_000,
        event_type=ACTIVITY_BUY,
        trader_id="t",
        amount_usd=Decimal("25"),
    )
    await store.record_activity([event])
    await store.record_activity([event])
    tape = await store.activity_tape(ALPHA)
    assert len(tape) == 1


# --- observations and reconstruction ----------------------------------------
async def test_a_rebuilt_state_is_truncated_in_sql_not_by_the_caller(store) -> None:
    for at in range(1_000, 2_000, 60):
        await store.record_observation(
            mint=ALPHA, observed_at=at, market_cap_usd=Decimal(str(40_000 + at))
        )
    state = await store.rebuild_state(ALPHA, until=1_300)
    samples = state.market.market_cap_usd.samples
    assert samples
    assert max(sample.at for sample in samples) <= 1_300


async def test_actor_observations_include_tokens_that_never_trended(store) -> None:
    """An inner join here would score every actor against only their winners."""

    for index, token in enumerate((ALPHA, BRAVO)):
        await store.record_activity(
            [
                ActivityEvent(
                    event_id=f"e{index}",
                    mint=token,
                    occurred_at=1_000,
                    event_type=ACTIVITY_BUY,
                    trader_id="trader-1",
                    amount_usd=Decimal("25"),
                )
            ]
        )
    truth = TrendingGroundTruth()
    outcome = truth.ingest(board([ALPHA, *filler(6)], at=1_200))
    await store.record_trend_events(outcome.events)
    await store.upsert_membership(list(truth.records.values()))

    observations = await store.actor_observations()
    assert len(observations) == 2
    outcomes = {row.mint: row.trend_entered_at for row in observations}
    assert outcomes[ALPHA] == 1_200
    assert outcomes[BRAVO] is None, "a non-trender is a negative, not missing data"


# --- predictions and resolution ---------------------------------------------
async def test_a_prediction_is_only_resolved_once_its_horizon_elapsed(store) -> None:
    await store.record_prediction(
        prediction_id="p1",
        mint=ALPHA,
        predicted_at=1_000,
        model_version="m",
        feature_version="pretrend.v1",
        lane="production",
        horizon_seconds=300,
        probability=Decimal("0.4"),
        calibration_bucket="25%-50%",
        sample_support=100,
        missing_features=0,
        market_cap_usd=Decimal("50000"),
        state=STATE_PRE_TREND,
        alerted=True,
    )
    assert await store.resolve_predictions(now=1_200) == 0, "resolved before the horizon"
    assert await store.resolve_predictions(now=1_400) == 1


async def test_a_silent_prediction_is_still_recorded(store) -> None:
    """A scoreboard built only from published alerts measures the publishing rule."""

    await store.record_prediction(
        prediction_id="quiet",
        mint=ALPHA,
        predicted_at=1_000,
        model_version="m",
        feature_version="pretrend.v1",
        lane="production",
        horizon_seconds=300,
        probability=Decimal("0.02"),
        calibration_bucket="1%-5%",
        sample_support=100,
        missing_features=3,
        market_cap_usd=Decimal("50000"),
        state="OBSERVING",
        alerted=False,
    )
    rows = await store.recent_predictions()
    assert len(rows) == 1
    assert rows[0]["alerted"] == 0


async def test_prediction_metrics_carry_the_base_rate_and_refuse_thin_samples(store) -> None:
    truth = TrendingGroundTruth()
    outcome = truth.ingest(board([ALPHA, *filler(6)], at=1_400))
    await store.record_trend_events(outcome.events)
    await store.upsert_membership(list(truth.records.values()))

    for index, token in enumerate((ALPHA, BRAVO)):
        await store.record_prediction(
            prediction_id=f"p{index}",
            mint=token,
            predicted_at=1_200,
            model_version="m",
            feature_version="pretrend.v1",
            lane="production",
            horizon_seconds=300,
            probability=Decimal("0.5"),
            calibration_bucket="50%-100%",
            sample_support=10,
            missing_features=0,
            market_cap_usd=Decimal("50000"),
            state=STATE_PRE_TREND,
            alerted=True,
        )
    await store.resolve_predictions(now=1_600)
    metrics = await store.prediction_metrics(threshold=0.2)

    assert metrics["resolved"] == 2
    assert metrics["positives"] == 1
    assert metrics["base_rate"] == 0.5
    assert metrics["precision"] == 0.5
    assert not metrics["sufficient"], "two predictions is not a measurement"


async def test_missed_entries_exclude_alerts_fired_after_the_entry(store) -> None:
    """An alert after the fact is a reaction, not a catch."""

    truth = TrendingGroundTruth()
    outcome = truth.ingest(board([ALPHA, *filler(6)], at=1_000))
    await store.record_trend_events(outcome.events)
    await store.upsert_membership(list(truth.records.values()))

    await store.record_alert(
        alert_id="late",
        mint=ALPHA,
        sent_at=1_500,  # after the entry
        kind="PRE_TREND",
        reason="STATE_ENTERED_PRE_TREND",
        probability=Decimal("0.4"),
        market_cap_usd=Decimal("80000"),
    )
    missed = await store.missed_trends()
    assert ALPHA in {row["mint"] for row in missed}

    await store.record_alert(
        alert_id="early",
        mint=ALPHA,
        sent_at=800,  # genuinely before
        kind="PRE_TREND",
        reason="STATE_ENTERED_PRE_TREND",
        probability=Decimal("0.4"),
        market_cap_usd=Decimal("40000"),
    )
    assert ALPHA not in {row["mint"] for row in await store.missed_trends()}


# --- restart safety ----------------------------------------------------------
async def test_state_and_cooldowns_survive_a_restart(store) -> None:
    state = TokenState(
        mint=ALPHA,
        state=STATE_PRE_TREND,
        entered_state_at=1_000,
        first_seen_at=900,
        last_alert_at=1_000,
        alerts_sent=1,
        last_band=3,
        first_pretrend_alert_at=1_000,
        first_pretrend_probability=Decimal("0.42"),
        first_pretrend_market_cap_usd=Decimal("68000"),
    )
    await store.save_state(state)

    restored = {item.mint: item for item in await store.load_states()}[ALPHA]
    assert restored.state == STATE_PRE_TREND
    assert restored.last_alert_at == 1_000
    assert restored.last_band == 3
    assert restored.first_pretrend_probability == Decimal("0.42")


async def test_the_runtime_restores_the_alert_budget_from_storage(store) -> None:
    for index in range(3):
        await store.record_alert(
            alert_id=f"a{index}",
            mint=mint(chr(ord("p") + index)),
            sent_at=10_000 + index,
            kind="PRE_TREND",
            reason="STATE_ENTERED_PRE_TREND",
            probability=Decimal("0.4"),
            market_cap_usd=Decimal("50000"),
        )
    runtime = PretrendRuntime(store, config=PretrendConfig())
    await runtime.restore(now=10_100)
    assert runtime.gate.alerts_last_hour(now=10_100) == 3


async def test_the_runtime_restores_membership_so_first_entries_are_not_re_emitted(
    store,
) -> None:
    truth = TrendingGroundTruth()
    mints = [ALPHA, *filler(6)]
    outcome = truth.ingest(board(mints, at=1_000))
    await store.record_trend_events(outcome.events)
    await store.upsert_membership(list(truth.records.values()))

    runtime = PretrendRuntime(store, config=PretrendConfig())
    await runtime.restore(now=1_100)
    assert runtime.ground_truth.first_trending_at(ALPHA) == 1_000


# --- the runtime path --------------------------------------------------------
class _FakeClient:
    """Stands in for the existing Trending client, including its failure modes."""

    def __init__(self, pages, error: str = "") -> None:
        self._pages = list(pages)
        self.last_error = error
        self.raise_next = False

    async def fetch_board(self, *, limit: int):
        if self.raise_next:
            raise RuntimeError("provider exploded")
        return self._pages.pop(0) if self._pages else []


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


async def test_the_observer_turns_a_raised_error_into_a_refused_snapshot(store) -> None:
    client = _FakeClient([])
    client.raise_next = True
    observer = BoardObserver(client, provider="test", source_kind="FOMO_TRENDING")
    snapshot = await observer.observe(now=1_000)
    assert snapshot.error
    assert snapshot.rows == ()

    runtime = PretrendRuntime(store, observer=observer, config=PretrendConfig())
    result = await runtime.ingest_snapshot(snapshot)
    assert not result.snapshot_accepted
    assert result.left == ()


async def test_a_board_cycle_records_ground_truth_and_confirms_the_entry(store) -> None:
    mints = [ALPHA, *filler(6)]
    client = _FakeClient(
        [[_Observation(item, index) for index, item in enumerate(mints, start=1)]]
    )
    observer = BoardObserver(client, provider="test", source_kind="FOMO_TRENDING")

    confirmations = []

    async def confirm(confirmation):
        confirmations.append(confirmation)
        return True

    runtime = PretrendRuntime(
        store, observer=observer, config=PretrendConfig(), confirm=confirm
    )
    result = await runtime.collect_board(now=5_000)

    assert result.snapshot_accepted
    assert ALPHA in result.entered
    assert len(confirmations) == 7
    assert (await store.first_trending_map())[ALPHA] == 5_000

    alpha_confirmation = next(item for item in confirmations if item.mint == ALPHA)
    assert not alpha_confirmation.predicted, "no alert preceded it, so it was missed"


async def test_an_unconfigured_activity_lane_is_reported_not_silently_empty(store) -> None:
    provider, status = build_activity_provider(url=None)
    assert not status.configured
    assert "FOMO_ACTIVITY" in status.detail or "not scrape" in status.detail

    runtime = PretrendRuntime(store, activity_provider=provider, config=PretrendConfig())
    assert await runtime.collect_activity(now=1_000) == 0

    report = await runtime.status(now=1_000)
    assert not report["activity_lane"]["configured"]


async def test_the_runtime_stays_silent_without_a_model(store) -> None:
    runtime = PretrendRuntime(
        store, config=PretrendConfig(inference_enabled=True, alerting_enabled=True)
    )
    result = await runtime.score_candidates([ALPHA], now=1_000)
    assert result.signals == ()
    assert result.candidates_scored == 0


async def test_status_reports_what_the_lane_cannot_currently_measure(store) -> None:
    runtime = PretrendRuntime(store, config=PretrendConfig())
    report = await runtime.status(now=1_000)
    assert report["collection_enabled"]
    assert not report["inference_enabled"], "inference is off until a model is validated"
    assert not report["alerting_enabled"]
    assert report["model"] is None
    assert report["feature_version"]


# --- Railway startup ---------------------------------------------------------
async def test_a_default_deployment_starts_quiet_and_collecting(tmp_path, monkeypatch) -> None:
    """What a Railway boot with no new env vars actually does.

    The release must be safe to deploy without touching configuration: the lane
    collects (so the research can start accumulating immediately) and says
    nothing (because no model has earned the right to).
    """

    from smart_money_bot.config import Settings

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "railway.db"))
    settings = Settings.from_env(require_discord_token=False)

    assert settings.pretrend_enabled
    assert settings.pretrend_collection_enabled
    assert not settings.pretrend_inference_enabled, "no model exists yet"
    assert not settings.pretrend_alerting_enabled, "and nothing may ping"
    assert settings.pretrend_activity_api_url is None, "no feed is ever assumed"
    # The alert budget is far tighter than the lanes it sits beside.
    assert settings.pretrend_max_alerts_per_hour <= 5


async def test_the_lane_boots_against_a_pre_existing_database(tmp_path) -> None:
    """A redeploy onto a live volume must not need a fresh database."""

    path = str(tmp_path / "existing.db")
    first = Database(path, Decimal("1000"))
    await first.connect()
    await first.db.execute(
        "INSERT INTO trending_tokens (mint, first_seen_at, last_observed_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (ALPHA, 1_000, 1_000, 1_000),
    )
    await first.db.commit()
    await first.close()

    second = Database(path, Decimal("1000"))
    await second.connect()
    store = PretrendStore(second)
    runtime = PretrendRuntime(store, config=PretrendConfig())
    await runtime.restore(now=2_000)
    report = await runtime.status(now=2_000)

    cursor = await second.db.execute("SELECT COUNT(*) FROM trending_tokens")
    preserved = (await cursor.fetchone())[0]
    await second.close()

    assert preserved == 1, "existing production rows survive the new schema block"
    assert report["collection_enabled"]


async def test_a_provider_outage_does_not_stop_the_lane(store) -> None:
    """One failed snapshot must not crash the loop or corrupt ground truth."""

    mints = [ALPHA, *filler(6)]
    client = _FakeClient(
        [
            [_Observation(item, index) for index, item in enumerate(mints, start=1)],
            [],  # outage
            [_Observation(item, index) for index, item in enumerate(mints, start=1)],
        ]
    )
    observer = BoardObserver(client, provider="test", source_kind="FOMO_TRENDING")
    runtime = PretrendRuntime(store, observer=observer, config=PretrendConfig())

    first = await runtime.collect_board(now=1_000)
    outage = await runtime.collect_board(now=1_060)
    recovered = await runtime.collect_board(now=1_120)

    assert first.snapshot_accepted
    assert not outage.snapshot_accepted
    assert outage.left == (), "an outage is not a mass departure"
    assert recovered.snapshot_accepted
    assert recovered.entered == (), "and recovery is not a mass arrival"
    assert (await store.first_trending_map())[ALPHA] == 1_000

    health = await store.snapshot_health()
    assert health["accepted"] == 2
    assert health["rejected"] == 1


# --- offline/online parity and affinity leakage ------------------------------
async def test_training_affinity_never_contains_the_rows_own_future(store) -> None:
    """The subtle leak: affinity is built from outcomes, so it must lag.

    An actor's record is a function of whether the tokens they entered went on
    to trend.  Building one table from the whole dataset and using it for every
    training row would feed the labels back into the features, and the backtest
    would report that circularity as skill.
    """

    from smart_money_bot.pretrend_training import (
        affinity_for,
        build_affinity_buckets,
    )

    day = 86_400
    base = 10 * day

    # One actor enters a token on day 0; that token trends shortly afterwards.
    await store.record_activity(
        [
            ActivityEvent(
                event_id="e1",
                mint=ALPHA,
                occurred_at=base + 100,
                event_type=ACTIVITY_BUY,
                trader_id="trader-1",
                amount_usd=Decimal("25"),
            )
        ]
    )
    truth = TrendingGroundTruth()
    outcome = truth.ingest(board([ALPHA, *filler(6)], at=base + 250))
    await store.record_trend_events(outcome.events)
    await store.upsert_membership(list(truth.records.values()))

    buckets = await build_affinity_buckets(
        store, since=base, until=base + 3 * day, horizon_seconds=300, bucket_seconds=day
    )

    # On the day the action happened, the outcome had not resolved yet, so the
    # actor has no record to speak of.
    same_day = affinity_for(buckets, at=base + 200, bucket_seconds=day)
    assert same_day.get("trader-1") is None

    # By the next bucket it has resolved and the record exists.
    next_day = affinity_for(buckets, at=base + day + 10, bucket_seconds=day)
    assert next_day.get("trader-1") is not None
    assert next_day["trader-1"].observations == 1


async def test_training_vectors_carry_the_same_feature_shape_as_live(store) -> None:
    """A training vector must contain every feature the live path produces.

    Offline/online parity is not a claim, it is this assertion: both paths call
    the same builder, so the name sets must be identical.  If they ever diverge,
    a coefficient learned offline multiplies a different quantity in production.
    """

    from smart_money_bot.pretrend.features import build_features
    from smart_money_bot.pretrend_training import collect_vectors

    for at in range(1_000, 1_600, 60):
        await store.record_observation(
            mint=ALPHA,
            observed_at=at,
            market_cap_usd=Decimal("50000"),
            liquidity_usd=Decimal("20000"),
            holders=200,
        )

    offline = await collect_vectors(store, since=900, until=1_600, stride_seconds=60)
    assert offline

    live_state = await store.rebuild_state(ALPHA, until=1_600)
    live = build_features(live_state)

    assert set(offline[0].values) == set(live.values)
    assert offline[0].feature_version == live.feature_version


# --- paper / shadow record ---------------------------------------------------
async def test_a_signal_that_never_trends_resolves_as_a_loss(store) -> None:
    """Leaving failures permanently OPEN would filter them out of the scoreboard."""

    runtime = PretrendRuntime(store, config=PretrendConfig())
    await store.open_paper_observation(
        observation_id="obs-1",
        mint=ALPHA,
        signalled_at=1_000,
        entry_price_usd=Decimal("0.0001"),
        entry_market_cap_usd=Decimal("50000"),
        entry_liquidity_usd=Decimal("20000"),
        probability=Decimal("0.4"),
    )
    await store.record_observation(
        mint=ALPHA, observed_at=1_200, market_cap_usd=Decimal("31000")
    )

    await runtime.track_paper_observations(now=1_300, max_age_seconds=7_200)
    assert "OPEN" in await store.paper_summary(), "still inside its window"

    await runtime.track_paper_observations(now=1_000 + 7_300, max_age_seconds=7_200)
    summary = await store.paper_summary()
    assert summary["EXPIRED"]["count"] == 1


async def test_a_signal_that_trends_resolves_with_its_lead_time(store) -> None:
    truth = TrendingGroundTruth()
    outcome = truth.ingest(board([ALPHA, *filler(6)], at=1_400))
    await store.record_trend_events(outcome.events)
    await store.upsert_membership(list(truth.records.values()))

    runtime = PretrendRuntime(store, config=PretrendConfig())
    await runtime.restore(now=1_500)
    await store.open_paper_observation(
        observation_id="obs-2",
        mint=ALPHA,
        signalled_at=1_000,
        entry_price_usd=Decimal("0.0001"),
        entry_market_cap_usd=Decimal("50000"),
        entry_liquidity_usd=Decimal("20000"),
        probability=Decimal("0.4"),
    )
    await runtime.track_paper_observations(now=1_500)

    summary = await store.paper_summary()
    assert summary["TRENDED"]["count"] == 1
    assert summary["TRENDED"]["avg_seconds_to_trend"] == 400.0


async def test_excursions_are_recorded_from_real_readings(store) -> None:
    runtime = PretrendRuntime(store, config=PretrendConfig())
    await store.open_paper_observation(
        observation_id="obs-3",
        mint=ALPHA,
        signalled_at=1_000,
        entry_price_usd=None,
        entry_market_cap_usd=Decimal("50000"),
        entry_liquidity_usd=None,
        probability=Decimal("0.4"),
    )
    for at, cap in ((1_100, "80000"), (1_200, "30000"), (1_300, "55000")):
        await store.record_observation(
            mint=ALPHA, observed_at=at, market_cap_usd=Decimal(cap)
        )
        await runtime.track_paper_observations(now=at, max_age_seconds=7_200)

    rows = await store.open_paper_observations()
    assert rows[0]["max_favourable_market_cap_usd"] == 80_000.0
    assert rows[0]["max_adverse_market_cap_usd"] == 30_000.0


# --- bounded reads -----------------------------------------------------------
async def test_the_row_limit_keeps_the_most_recent_history(store) -> None:
    """Taking the oldest rows would silently drop exactly what the features read.

    Every window feature reads recent history.  A limit that selected the
    *oldest* rows on a long-lived mint would degrade those features quietly
    rather than failing, which is the worst way for a bug to behave.
    """

    for at in range(1_000, 1_100):
        await store.record_observation(
            mint=ALPHA, observed_at=at, market_cap_usd=Decimal(str(at))
        )
    rows = await store.observations_for(ALPHA, limit=10)
    assert len(rows) == 10
    assert [int(row["observed_at"]) for row in rows] == list(range(1_090, 1_100))
    assert rows == tuple(sorted(rows, key=lambda row: row["observed_at"])), "ascending"


async def test_a_lookback_bound_is_applied_in_sql(store) -> None:
    for at in (1_000, 5_000, 9_000):
        await store.record_observation(
            mint=ALPHA, observed_at=at, market_cap_usd=Decimal("50000")
        )
    rows = await store.observations_for(ALPHA, since=4_000, until=8_000)
    assert [int(row["observed_at"]) for row in rows] == [5_000]

    state = await store.rebuild_state(ALPHA, until=8_000, since=4_000)
    samples = state.market.market_cap_usd.samples
    assert [sample.at for sample in samples] == [5_000]


async def test_the_activity_tape_limit_also_keeps_the_most_recent(store) -> None:
    await store.record_activity(
        [
            ActivityEvent(
                event_id=f"e{index}",
                mint=ALPHA,
                occurred_at=1_000 + index,
                event_type=ACTIVITY_BUY,
                trader_id=f"t{index}",
                amount_usd=Decimal("10"),
            )
            for index in range(50)
        ]
    )
    tape = await store.activity_tape(ALPHA, limit=5)
    assert len(tape) == 5
    assert [event.occurred_at for event in tape.events] == list(range(1_045, 1_050))
