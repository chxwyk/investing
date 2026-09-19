"""The whole P0 chain, end to end, on a synthetic market.

This is the test that answers "is the core actually done?" in the sense section
85 means: can a token that trends later be reconstructed afterwards, with its
first sighting, its pre-entry states, the entry itself, and an honest account of
whether we called it?

The market here is synthetic and small.  It is not evidence that an edge exists
-- only real collected data can be that -- but it does prove the machinery that
would find one is wired together and does not cheat.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from smart_money_bot.database import Database
from smart_money_bot.pretrend.activity import ACTIVITY_BUY, ActivityEvent
from smart_money_bot.pretrend.forensics import build_forensics
from smart_money_bot.pretrend.groundtruth import BoardRow, TrendingSnapshot
from smart_money_bot.pretrend.model import Prediction
from smart_money_bot.pretrend_cards import render_forensics
from smart_money_bot.pretrend_runtime import PretrendConfig, PretrendRuntime
from smart_money_bot.pretrend_store import PretrendStore
from smart_money_bot.pretrend_training import collect_vectors, train_once

WINNER = ("W" * 44)[:44]
LOSER = ("L" * 44)[:44]


def filler(count: int) -> list[str]:
    return [(chr(ord("d") + index) * 44)[:44] for index in range(count)]


@pytest.fixture
async def store(tmp_path):
    database = Database(str(tmp_path / "e2e.db"), Decimal("1000"))
    await database.connect()
    try:
        yield PretrendStore(database)
    finally:
        await database.close()


class _Scorer:
    """A transparent stand-in for a trained model, so the plumbing is testable."""

    trained_positives = 100
    trained_rows = 5_000

    def predict(self, values):
        buyers = values.get("unique_fomo_buyers_1m")
        strong = buyers is not None and buyers >= Decimal("4")
        return Prediction(
            probability=Decimal("0.65") if strong else Decimal("0.01"),
            model_version="stub_v1",
            feature_version="pretrend.v1",
            sample_support=5_000,
        )

    def score_vector(self, vector):
        return self.predict(vector.values)


async def _run_market(store: PretrendStore, *, runtime: PretrendRuntime) -> dict:
    """A winner that heats up and enters the board, and a loser that does not."""

    base = 20 * 86_400
    signals = []
    confirmations = []

    async def publish(signal):
        signals.append(signal)
        return True

    async def confirm(confirmation):
        confirmations.append(confirmation)
        return True

    runtime._publish = publish
    runtime._confirm = confirm
    runtime.model = _Scorer()

    # Minute by minute, both tokens are observed.  The winner attracts FOMO
    # buyers from minute 5; the loser never does.
    for step in range(12):
        at = base + step * 60
        for mint, cap in ((WINNER, 40_000 + step * 3_000), (LOSER, 45_000)):
            await runtime.record_candidate(
                mint=mint,
                at=at,
                market_cap_usd=Decimal(str(cap)),
                liquidity_usd=Decimal("25000"),
                volume_usd=Decimal("8000"),
                unique_buyers=20 + step,
                holders=200 + step * 4,
                token_age_seconds=600 + step * 60,
                provider="dexscreener",
                source="dexscreener",
            )
        if step >= 5:
            await store.record_activity(
                [
                    ActivityEvent(
                        event_id=f"w{step}-{index}",
                        mint=WINNER,
                        occurred_at=at + index * 5,
                        event_type=ACTIVITY_BUY,
                        trader_id=f"trader-{index}",
                        amount_usd=Decimal("60"),
                        market_cap_usd=Decimal(str(40_000 + step * 3_000)),
                    )
                    for index in range(5)
                ]
            )
        await runtime.score_candidates([WINNER, LOSER], now=at)

    # The winner reaches the board at minute 12.
    entry_at = base + 12 * 60
    snapshot = TrendingSnapshot(
        observed_at=entry_at,
        rows=tuple(
            BoardRow(mint=item, rank=index, market_cap_usd=Decimal("120000"), holders=700)
            for index, item in enumerate([WINNER, *filler(6)], start=1)
        ),
        provider="test",
        source_kind="FOMO_TRENDING",
    )
    await runtime.ingest_snapshot(snapshot)
    return {
        "base": base,
        "entry_at": entry_at,
        "signals": signals,
        "confirmations": confirmations,
    }


async def test_the_core_chain_produces_a_reconstructable_record(store) -> None:
    runtime = PretrendRuntime(
        store,
        config=PretrendConfig(
            inference_enabled=True,
            alerting_enabled=True,
            min_positives_to_alert=1,
        ),
    )
    result = await _run_market(store, runtime=runtime)

    # 1. The winner was alerted BEFORE it reached the board.
    winner_signals = [signal for signal in result["signals"] if signal.mint == WINNER]
    assert winner_signals, "the lane never fired on the token that trended"
    assert winner_signals[0].at < result["entry_at"]

    # 2. The loser was never alerted.
    assert not [signal for signal in result["signals"] if signal.mint == LOSER]

    # 3. Ground truth recorded the first entry, immutably.
    first = await store.first_trending_map()
    assert first[WINNER] == result["entry_at"]
    assert LOSER not in first

    # 4. The confirmation knows we called it, and how early.
    confirmation = next(
        item for item in result["confirmations"] if item.mint == WINNER
    )
    assert confirmation.predicted
    assert confirmation.lead_seconds == result["entry_at"] - winner_signals[0].at
    assert confirmation.first_alert_market_cap_usd is not None

    # 5. Predictions resolve against ground truth, and the metrics carry the
    #    base rate and refuse to claim sufficiency on this sample.
    await runtime.resolve_outcomes(now=result["entry_at"] + 2_000)
    metrics = await store.prediction_metrics(threshold=0.2)
    assert metrics["resolved"] > 0
    assert metrics["base_rate"] is not None
    assert not metrics["sufficient"]

    # 6. The whole pre-entry state is reconstructable afterwards.
    entry = await store.trend_entry(WINNER)
    assert entry is not None
    state = await store.rebuild_state(WINNER, until=entry.occurred_at)
    states = {item.mint: item for item in await store.load_states()}
    token_state = states[WINNER]
    report = build_forensics(
        state,
        entry=entry,
        affinities=runtime.affinities,
        predicted=token_state.predicted,
        first_alert_at=token_state.first_pretrend_alert_at,
        first_alert_probability=token_state.first_pretrend_probability,
        first_alert_market_cap_usd=token_state.first_pretrend_market_cap_usd,
    )

    assert report.data_available
    assert report.snapshots
    assert report.early_actors, "the accounts that acted first are recoverable"
    assert report.lead_seconds is not None and report.lead_seconds > 0

    body = render_forensics(report)
    assert "FIRST FOMO TRENDING" in body
    assert "PRE-ENTRY TIMELINE" in body
    assert "EARLIEST NOTABLE FOMO ACCOUNTS" in body
    assert "OUR CALL" in body

    # 7. The reconstruction reads only the past: the T-5m row cannot know the
    #    market cap the token had at entry.
    early = next(snap for snap in report.snapshots if snap.offset_seconds == 300)
    late = next(snap for snap in report.snapshots if snap.offset_seconds == 0)
    assert early.value("market_cap_usd") < late.value("market_cap_usd")


async def test_the_alert_budget_holds_across_the_whole_run(store) -> None:
    """Whatever the model says, the lane cannot become loud."""

    runtime = PretrendRuntime(
        store,
        config=PretrendConfig(
            inference_enabled=True,
            alerting_enabled=True,
            min_positives_to_alert=1,
        ),
    )
    result = await _run_market(store, runtime=runtime)
    # Twelve scoring cycles on a token that stays above the threshold the whole
    # time must not produce twelve messages.
    winner_signals = [signal for signal in result["signals"] if signal.mint == WINNER]
    assert len(winner_signals) == 1
    assert runtime.gate.suppressed


async def test_a_dataset_built_from_the_run_is_leakage_clean(store) -> None:
    runtime = PretrendRuntime(
        store,
        config=PretrendConfig(inference_enabled=True, alerting_enabled=False),
    )
    result = await _run_market(store, runtime=runtime)

    cutoff = result["entry_at"] + 1_200
    vectors = await collect_vectors(
        store, since=result["base"] - 60, until=cutoff, stride_seconds=60
    )
    assert vectors

    outcome = train_once(
        vectors,
        first_trending_at=await store.first_trending_map(),
        horizon_seconds=300,
        data_complete_until=cutoff,
        universe_min_usd=Decimal("20000"),
        universe_max_usd=Decimal("1000000"),
    )
    assert outcome.dataset is not None
    assert outcome.dataset.clean, "the assembled dataset must be leakage-free"
    # And on a sample this small, promotion is refused.
    assert not outcome.promoted
    assert outcome.refusal_reason


async def test_a_token_already_on_the_board_is_never_scored_again(store) -> None:
    runtime = PretrendRuntime(
        store,
        config=PretrendConfig(
            inference_enabled=True, alerting_enabled=True, min_positives_to_alert=1
        ),
    )
    result = await _run_market(store, runtime=runtime)

    after = await runtime.score_candidates(
        [WINNER, LOSER], now=result["entry_at"] + 600
    )
    scored_mints = {
        row["mint"]
        for row in await store.recent_predictions(mint=WINNER, limit=50)
        if row["predicted_at"] > result["entry_at"]
    }
    assert WINNER not in scored_mints, "a trending token is not a prediction opportunity"
    assert after.signals == ()
