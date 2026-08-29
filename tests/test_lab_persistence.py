"""Restart-safety, idempotency and Discord-surface tests for the v2.36 lab.

The questions these answer are the ones a Railway restart used to get wrong:
does an old mint come back as an old mint, does a retried write duplicate an
alert / entry / exit, and does a new command ever strand a Discord spinner?
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from smart_money_bot.database import Database
from smart_money_bot.lab.bankroll import BankrollState
from smart_money_bot.lab.config import DEFAULT_LAB_CONFIG
from smart_money_bot.lab.decision import Decision, EvidenceQuality, SafetyStatus, TradeDecision
from smart_money_bot.lab.exits import ExitContext, apply_exit, observe, open_position, plan_exit
from smart_money_bot.lab.identity import build_token_identity
from smart_money_bot.lab.lifecycle import (
    FIRST_DISCOVERY,
    LifecycleObservation,
    PublicationState,
    advance_lifecycle,
    new_lifecycle,
)
from smart_money_bot.lab.registry import MENTIONED, build_signal
from smart_money_bot.lab.smartmoney import WalletReputation
from smart_money_bot.lab.timeline import observation_events
from smart_money_bot.lab_runtime import LabRuntime
from smart_money_bot.lab_store import LabStore
from smart_money_bot.models import (
    RunnerDemandProfile,
    RunnerForensics,
    RunnerMarketSnapshot,
    RunnerQualityAssessment,
    RunnerSafetyAssessment,
    RunnerScoreBreakdown,
)

MINT = "So11111111111111111111111111111111111111112"
OTHER_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
D = Decimal


@pytest.fixture
async def store(tmp_path):
    database = Database(str(tmp_path / "lab.db"), D("100"))
    await database.connect()
    try:
        yield LabStore(database)
    finally:
        await database.close()


# ---------------------------------------------------------------------------
# lifecycle persistence
# ---------------------------------------------------------------------------


async def test_lifecycle_survives_a_restart(tmp_path) -> None:
    """A reconnect must rehydrate the old pump, not re-discover the token."""

    path = str(tmp_path / "restart.db")
    database = Database(path, D("100"))
    await database.connect()
    store = LabStore(database)

    record = new_lifecycle(MINT, now=10)
    record = advance_lifecycle(
        record,
        LifecycleObservation(
            observed_at=20,
            price_usd=D("0.000032"),
            market_cap_usd=D("32000"),
            surfaced=True,
            qualified=True,
        ),
    )
    record = advance_lifecycle(
        record,
        LifecycleObservation(
            observed_at=60, price_usd=D("0.00015"), market_cap_usd=D("150000")
        ),
    )
    record = advance_lifecycle(
        record,
        LifecycleObservation(
            observed_at=120, price_usd=D("0.000038"), market_cap_usd=D("38000")
        ),
    )
    await store.save_lifecycle(record)
    await database.close()

    reopened = Database(path, D("100"))
    await reopened.connect()
    try:
        rehydrated = await LabStore(reopened).load_lifecycle(MINT)
        assert rehydrated.state != FIRST_DISCOVERY
        assert rehydrated.first_surface_market_cap_usd == D("32000")
        assert rehydrated.historical_high_market_cap_usd == D("150000")
        assert not rehydrated.is_fresh_setup
        assert rehydrated.cycle_count == 1
    finally:
        await reopened.close()


async def test_unknown_mint_is_the_only_way_to_be_first_discovery(store) -> None:
    fresh = await store.load_lifecycle(OTHER_MINT, now=500)
    assert fresh.state == FIRST_DISCOVERY
    assert fresh.first_discovered_at == 500


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


async def test_repeated_event_writes_are_a_no_op(store) -> None:
    events = observation_events(MINT, occurred_at=100, price_usd=D("1"))
    assert await store.append_events(events) == len(events)
    assert await store.append_events(events) == 0
    assert await store.event_count(MINT) == len(events)


async def test_repeated_decision_writes_are_a_no_op(store) -> None:
    decision = TradeDecision(
        mint=MINT,
        decision=Decision.WAIT,
        timestamp=100,
        strategy_version=DEFAULT_LAB_CONFIG.strategy_version,
    )
    assert await store.record_decision(decision) is True
    assert await store.record_decision(decision) is False
    assert len(await store.recent_decisions(strategy_version=decision.strategy_version)) == 1


async def test_only_one_open_simulated_position_per_mint(store) -> None:
    first = open_position(
        position_id="a",
        mint=MINT,
        now=1,
        decision_price_usd=D("1"),
        size_usd=D("5"),
        strategy_version="lab-v1",
    )
    duplicate = open_position(
        position_id="b",
        mint=MINT,
        now=2,
        decision_price_usd=D("1"),
        size_usd=D("5"),
        strategy_version="lab-v1",
    )
    assert await store.save_position(first) is True
    assert await store.save_position(duplicate) is False
    assert len(await store.open_positions(strategy_version="lab-v1")) == 1


async def test_repeated_exit_writes_cannot_double_sell(store) -> None:
    position = open_position(
        position_id="a",
        mint=MINT,
        now=1,
        decision_price_usd=D("1"),
        size_usd=D("5"),
        strategy_version="lab-v1",
    )
    await store.save_position(position)
    context = ExitContext(
        now=100,
        price_usd=D("1.2"),
        momentum_score=D("70"),
        organic_score=D("70"),
        buys=50,
        sells=10,
        safety_status="PASS",
    )
    position = observe(position, context)
    position, journal = apply_exit(position, plan_exit(position, context), context)
    assert journal is not None
    assert await store.record_exit(journal) is True
    assert await store.record_exit(journal) is False
    assert len(await store.exit_rows()) == 1


async def test_position_state_survives_a_round_trip(store) -> None:
    position = open_position(
        position_id="a",
        mint=MINT,
        now=1,
        decision_price_usd=D("1"),
        size_usd=D("5"),
        strategy_version="lab-v1",
    )
    context = ExitContext(
        now=100,
        price_usd=D("2.2"),
        momentum_score=D("70"),
        organic_score=D("70"),
        buys=50,
        sells=10,
        safety_status="PASS",
    )
    position = observe(position, context)
    position, _ = apply_exit(position, plan_exit(position, context), context)
    await store.save_position(position)

    restored = await store.open_position_for(MINT, strategy_version="lab-v1")
    assert restored is not None
    assert restored.max_favourable_percent == position.max_favourable_percent
    assert restored.milestones_taken == position.milestones_taken
    assert restored.realized_net_pnl_usd == position.realized_net_pnl_usd
    assert len(restored.exits) == 1


async def test_bankroll_survives_a_round_trip(store) -> None:
    runtime = LabRuntime(store, config=DEFAULT_LAB_CONFIG)
    state = dataclasses.replace(
        BankrollState(),
        cash_usd=D("87.5"),
        realized_net_pnl_usd=D("-2.5"),
        consecutive_losses=2,
        day_key="2026-01-01",
    )
    await runtime.save_bankroll(state)
    restored = await runtime.bankroll()
    assert restored.cash_usd == D("87.5")
    assert restored.consecutive_losses == 2
    assert restored.day_key == "2026-01-01"


async def test_publication_state_survives_a_restart(store) -> None:
    await store.save_publication(
        PublicationState(
            mint=MINT,
            published_at=1_000,
            lifecycle_state="FIRST_QUALIFIED",
            opportunity_score=D("70"),
            safety_status="PASS",
            independent_buyers=20,
            liquidity_usd=D("50000"),
        )
    )
    restored = await store.load_publication(MINT)
    assert restored is not None
    assert restored.published_at == 1_000
    assert restored.opportunity_score == D("70")
    assert restored.liquidity_usd == D("50000")


async def test_wallet_reputation_survives_a_restart(store) -> None:
    reputation = WalletReputation(
        wallet="Wallet1111111111111111111111111111111111111",
        samples=12,
        score=D("81.5"),
        state="PROVEN_EARLY",
        hit_50_percent=D("60"),
        updated_at=1_000,
    )
    await store.save_reputation(reputation)
    restored = await store.load_reputations([reputation.wallet])
    assert restored[reputation.wallet].state == "PROVEN_EARLY"
    assert restored[reputation.wallet].score == D("81.5")
    assert restored[reputation.wallet].samples == 12


async def test_duplicate_social_signals_are_stored_once(store) -> None:
    signal = build_signal(
        platform="x",
        account="lookonchain",
        url="https://x.com/lookonchain/status/1",
        observed_at=100,
        source_timestamp=100,
        mint=MINT,
        classification=MENTIONED,
        text_hash="hash-1",
    )
    assert await store.record_social_signal(signal) is True
    assert await store.record_social_signal(signal) is False
    assert len(await store.social_signals_for(MINT)) == 1


async def test_social_budget_usage_accumulates(store) -> None:
    await store.record_social_usage(usage_day="2026-01-01", calls=2, posts_processed=20)
    await store.record_social_usage(usage_day="2026-01-01", calls=3, cache_hits=4)
    rows = await store.social_usage(usage_day="2026-01-01")
    assert rows[0]["calls"] == 5
    assert rows[0]["posts_processed"] == 20
    assert rows[0]["cache_hits"] == 4


async def test_identity_is_persisted_and_reused(store) -> None:
    identity = build_token_identity(
        MINT,
        metadata={"source": "dexscreener", "image": "https://cdn.example.com/a.png"},
        name="Real Token",
        symbol="REAL",
        resolved_at=100,
    )
    await store.save_identity(identity)
    payload = await store.identity_payload(MINT)
    assert payload is not None
    assert payload["name"] == "Real Token"
    assert payload["image_url"] == "https://cdn.example.com/a.png"


# ---------------------------------------------------------------------------
# runtime end-to-end
# ---------------------------------------------------------------------------


def _snapshot(**overrides) -> RunnerMarketSnapshot:
    base = {
        "mint": MINT,
        "captured_at": 1_000,
        "price_usd": D("0.001"),
        "market_cap_usd": D("40000"),
        "liquidity_usd": D("60000"),
        "volume_5m_usd": D("9000"),
        "buys_5m": 120,
        "sells_5m": 40,
        "holder_count": 400,
        "route_available": True,
        "buy_route_status": "PASS",
        "sell_route_status": "PASS",
        "route_price_impact_percent": D("0.5"),
        "sell_route_price_impact_percent": D("0.6"),
        "top10_percent": D("12"),
    }
    base.update(overrides)
    return RunnerMarketSnapshot(**base)


def _candidate(**overrides) -> SimpleNamespace:
    demand = RunnerDemandProfile(
        raw_buyers=40,
        estimated_independent_buyers=30,
        independence_ratio=D("0.8"),
        cluster_supply_percent=D("8"),
        confidence="COMPLETE",
    )
    quality = RunnerQualityAssessment(
        momentum_score=D("70"),
        opportunity_score=D("72"),
        organic_score=D("70"),
        stage="ENTRY_CANDIDATE",
        qualified=True,
        demand=demand,
        evaluated_at=1_000,
    )
    base = {
        "mint": MINT,
        "symbol": "REAL",
        "name": "Real Token",
        "first_seen_at": 900,
        "graduated_at": None,
        "graduation_source": "TEST",
        "first": _snapshot(captured_at=900, price_usd=D("0.0009")),
        "current": _snapshot(),
        "score": D("70"),
        "tier": "TEST",
        "breakdown": RunnerScoreBreakdown(),
        "quality": quality,
        "safety": RunnerSafetyAssessment(status="PASS", entry_eligible=True),
        "forensics": RunnerForensics(available=True),
        "smart_wallets": (),
        "stage": "ENTRY_CANDIDATE",
        "overextended": False,
        "chain_created_at": 500,
        "pair_created_at": 600,
        "pair_url": "",
        "generated_at": 1_000,
        "first_discord_visible_at": None,
        "earliest_smart_entry_age_seconds": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_runtime_evaluates_and_opens_one_simulated_position(store) -> None:
    runtime = LabRuntime(store, config=DEFAULT_LAB_CONFIG)
    candidate = _candidate()

    result = await runtime.evaluate_candidate(candidate, now=1_000)
    assert result.decision.decision is Decision.ENTRY
    assert result.decision.safety is SafetyStatus.PASS
    assert result.identity.name == "Real Token"

    position = await runtime.maybe_open_position(result, now=1_000)
    assert position is not None
    # An unknown regime and an unsampled activity profile size the position
    # down; nothing is allowed to size it up.
    assert D("0") < position.size_usd < DEFAULT_LAB_CONFIG.normal_position_usd

    # A replayed cycle must not open a second simulated position.
    again = await runtime.evaluate_candidate(candidate, now=1_000)
    assert await runtime.maybe_open_position(again, now=1_000) is None
    assert len(await store.open_positions(strategy_version="lab-v1")) == 1

    bankroll = await runtime.bankroll()
    assert bankroll.open_positions == 1
    assert bankroll.cash_usd == DEFAULT_LAB_CONFIG.bankroll_usd - position.size_usd


async def test_runtime_never_enters_on_unknown_safety(store) -> None:
    runtime = LabRuntime(store, config=DEFAULT_LAB_CONFIG)
    candidate = _candidate(
        safety=RunnerSafetyAssessment(status="UNKNOWN", entry_eligible=False)
    )
    result = await runtime.evaluate_candidate(candidate, now=1_000)
    assert not result.entry_eligible
    assert await runtime.maybe_open_position(result, now=1_000) is None
    assert await store.open_positions(strategy_version="lab-v1") == []


async def test_runtime_records_the_decision_and_the_lifecycle(store) -> None:
    runtime = LabRuntime(store, config=DEFAULT_LAB_CONFIG)
    await runtime.evaluate_candidate(_candidate(), now=1_000)
    decisions = await store.recent_decisions(strategy_version="lab-v1")
    assert len(decisions) == 1
    lifecycle = await store.load_lifecycle(MINT)
    assert lifecycle.state != FIRST_DISCOVERY
    assert await store.event_count(MINT) > 0


async def test_runtime_suppresses_an_identical_republication(store) -> None:
    runtime = LabRuntime(store, config=DEFAULT_LAB_CONFIG)
    # Let the lifecycle settle first: a real state transition is allowed to
    # publish, so the suppression case is the *unchanged* observation after it.
    for at in (1_000, 1_100):
        result = await runtime.evaluate_candidate(_candidate(), now=at)
        allowed, _, _ = await runtime.may_publish(result, now=at)
        assert allowed
        await runtime.mark_published(result, now=at)

    again = await runtime.evaluate_candidate(_candidate(), now=1_200)
    allowed, triggers, reason = await runtime.may_publish(again, now=1_200)
    assert not allowed
    assert triggers == ()
    assert reason


async def test_runtime_performance_report_starts_honest(store) -> None:
    runtime = LabRuntime(store, config=DEFAULT_LAB_CONFIG)
    payload = await runtime.performance()
    assert payload["starting_bankroll_usd"] == D("100")
    assert payload["open_positions"] == 0
    assert payload["report"].sample_too_small


async def test_runtime_manages_an_open_position_to_a_partial_exit(store) -> None:
    runtime = LabRuntime(store, config=DEFAULT_LAB_CONFIG)
    result = await runtime.evaluate_candidate(_candidate(), now=1_000)
    position = await runtime.maybe_open_position(result, now=1_000)
    assert position is not None

    updated, reason = await runtime.manage_position(
        position,
        ExitContext(
            now=1_100,
            price_usd=position.entry_price_usd * D("1.2"),
            momentum_score=D("70"),
            organic_score=D("70"),
            buys=60,
            sells=15,
            entry_liquidity_usd=D("60000"),
            liquidity_usd=D("60000"),
            safety_status="PASS",
        ),
    )
    assert reason == "MILESTONE_TAKE_PROFIT"
    assert updated.realized_net_pnl_usd != D("0")
    assert len(await store.exit_rows()) == 1
    bankroll = await runtime.bankroll()
    assert bankroll.cash_usd > DEFAULT_LAB_CONFIG.bankroll_usd - position.size_usd


async def test_counterfactuals_cost_no_provider_calls(store) -> None:
    """Simulating many policies must never multiply live requests."""

    from smart_money_bot.lab.replay import compare_policies
    from smart_money_bot.lab.timeline import TokenTimeline

    timeline = TokenTimeline(MINT)
    observations = []
    from smart_money_bot.lab.replay import ReplayObservation

    for index, price in enumerate(["1", "1.3", "1.8", "1.1"]):
        at = index * 60
        observations.append(
            ReplayObservation(at=at, price_usd=D(price), qualified=True, safety_status="PASS")
        )
        timeline.extend(observation_events(MINT, occurred_at=at, price_usd=D(price)))

    before = await store.event_count()
    results = compare_policies(timeline, observations)
    after = await store.event_count()
    assert len(results) > 20
    assert before == after


# ---------------------------------------------------------------------------
# Discord surfaces
# ---------------------------------------------------------------------------


def test_new_commands_are_registered_on_the_fomo_group() -> None:
    from smart_money_bot.bot import FomoCommands

    names = {command.name for command in FomoCommands.__cog_app_commands__}
    assert {"lab", "results", "quality", "calibration", "latency", "forensic"} <= names
    assert {
        "opportunities",
        "trades",
        "performance",
        "exits",
        "lifecycle",
        "smartmoney",
        "sources",
    } <= names
    # Discord allows at most 25 subcommands in one group.
    assert len(names) <= 25


def test_lab_cards_stay_inside_discord_limits() -> None:
    from smart_money_bot.bot import (
        DISCORD_EMBED_TOTAL_LIMIT,
        _lab_exits_embed,
        _lab_lifecycle_embed,
        _lab_performance_embed,
        _lab_smartmoney_embed,
        _lab_trades_embed,
    )
    from smart_money_bot.lab.replay import summarize_trades

    trades = _lab_trades_embed(())
    assert len(trades) <= DISCORD_EMBED_TOTAL_LIMIT

    performance = _lab_performance_embed(
        {
            "starting_bankroll_usd": D("100"),
            "current_bankroll_usd": D("100"),
            "realized_net_pnl_usd": D("0"),
            "unrealized_net_pnl_usd": D("0"),
            "open_positions": 0,
            "strategy_version": "lab-v1",
            "report": summarize_trades(()),
        }
    )
    assert len(performance) <= DISCORD_EMBED_TOTAL_LIMIT
    assert any("SAMPLE_TOO_SMALL" in field.name for field in performance.fields)

    assert len(_lab_exits_embed(())) <= DISCORD_EMBED_TOTAL_LIMIT

    lifecycle = _lab_lifecycle_embed(
        {
            "mint": MINT,
            "lifecycle": new_lifecycle(MINT, now=0),
            "identity": None,
            "event_count": 0,
            "social_signals": (),
        },
        referral_code=None,
    )
    assert len(lifecycle) <= DISCORD_EMBED_TOTAL_LIMIT

    assert len(_lab_smartmoney_embed({"mint": MINT, "available": False})) <= (
        DISCORD_EMBED_TOTAL_LIMIT
    )


def test_hostile_metadata_cannot_oversize_an_opportunity_card() -> None:
    from smart_money_bot.bot import DISCORD_EMBED_TOTAL_LIMIT, _lab_opportunity_embed
    from smart_money_bot.lab.authenticity import AuthenticityAssessment
    from smart_money_bot.lab.entry import EntryContext, evaluate_entry
    from smart_money_bot.lab.smartmoney import SmartMoneyAssessment
    from smart_money_bot.lab_runtime import LabEvaluation

    candidate = _candidate(name="Z" * 5_000, symbol="S" * 500)
    identity = build_token_identity(
        MINT,
        metadata={
            "source": "dexscreener",
            "name": "Z" * 5_000,
            "description": "Q" * 9_000,
            "image": "https://cdn.example.com/" + "a" * 900 + ".png",
        },
    )
    evaluation = evaluate_entry(
        EntryContext(mint=MINT, now=1_000),
        lifecycle=new_lifecycle(MINT, now=0),
        bankroll=BankrollState(),
    )
    result = LabEvaluation(
        mint=MINT,
        identity=identity,
        lifecycle=new_lifecycle(MINT, now=0),
        evaluation=evaluation,
        authenticity=AuthenticityAssessment(),
        smart_money=SmartMoneyAssessment(),
        reentry=None,
        position=None,
    )
    embed = _lab_opportunity_embed(
        candidate, result, index=0, total=1, referral_code=None
    )
    assert len(embed) <= DISCORD_EMBED_TOTAL_LIMIT
    assert embed.thumbnail.url is None


async def test_opportunities_command_resolves_when_nothing_is_stored() -> None:
    """A new command must never leave the deferred interaction spinning."""

    from smart_money_bot.bot import FomoCommands

    bot = SimpleNamespace(
        engine=SimpleNamespace(lab_opportunities=AsyncMock(return_value=())),
        settings=SimpleNamespace(fomo_referral_code=None),
    )
    cog = FomoCommands.__new__(FomoCommands)
    cog.bot = bot
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
        user=SimpleNamespace(id=1),
    )
    cog._require_admin = AsyncMock(return_value=True)
    await FomoCommands.opportunities.callback(cog, interaction, 3)
    interaction.edit_original_response.assert_awaited()
    content = interaction.edit_original_response.await_args.kwargs["content"]
    assert "no persisted candidate" in content


async def test_opportunities_command_degrades_when_discord_rejects_the_card() -> None:
    from smart_money_bot.bot import FomoCommands

    candidate = _candidate()
    bot = SimpleNamespace(
        engine=SimpleNamespace(lab_opportunities=AsyncMock(return_value=())),
        settings=SimpleNamespace(fomo_referral_code=None),
    )
    cog = FomoCommands.__new__(FomoCommands)
    cog.bot = bot
    cog._require_admin = AsyncMock(return_value=True)

    bot.engine.lab_opportunities = AsyncMock(side_effect=RuntimeError("provider down"))
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
        user=SimpleNamespace(id=1),
    )
    await FomoCommands.opportunities.callback(cog, interaction, 3)
    content = interaction.edit_original_response.await_args.kwargs["content"]
    assert "RuntimeError" in content
    assert "No buy or launch" in content
    assert candidate.mint == MINT


def test_evidence_quality_reports_partial_when_something_is_missing() -> None:
    from smart_money_bot.lab.entry import EntryContext, evaluate_entry

    result = evaluate_entry(
        EntryContext(mint=MINT, now=1_000, price_usd=D("1")),
        lifecycle=new_lifecycle(MINT, now=0),
        bankroll=BankrollState(),
    )
    assert result.decision.evidence_quality is EvidenceQuality.PARTIAL


# ---------------------------------------------------------------------------
# migration safety
# ---------------------------------------------------------------------------


async def test_lab_schema_is_additive_and_reentrant(tmp_path) -> None:
    """An existing production database upgrades without losing a single row."""

    path = str(tmp_path / "existing.db")
    database = Database(path, D("100"))
    await database.connect()
    await database.add_trader("Wallet1111111111111111111111111111111111111", alias="w")
    await database.set_setting("alert_channel_id", "123")
    await database.close()

    # Reconnecting runs the whole schema script again, exactly as a Railway
    # redeploy does.
    reopened = Database(path, D("100"))
    await reopened.connect()
    try:
        traders = await reopened.list_traders()
        assert len(traders) == 1
        assert await reopened.get_setting("alert_channel_id") == "123"

        cursor = await reopened.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'lab_%'"
        )
        tables = {str(row["name"]) for row in await cursor.fetchall()}
        assert {
            "lab_token_lifecycle",
            "lab_token_events",
            "lab_decisions",
            "lab_positions",
            "lab_exits",
            "lab_bankroll",
            "lab_publications",
            "lab_wallet_reputation",
            "lab_social_signals",
            "lab_account_performance",
            "lab_account_cache",
            "lab_social_budget",
            "lab_strategy_registry",
            "lab_token_identity",
        } <= tables
    finally:
        await reopened.close()


async def test_stored_reputation_is_used_for_smart_money(store) -> None:
    wallet = "Wallet1111111111111111111111111111111111111"
    await store.save_reputation(
        WalletReputation(
            wallet=wallet,
            samples=12,
            score=D("90"),
            state="PROVEN_EARLY",
            hit_50_percent=D("60"),
            updated_at=1_000,
        )
    )
    runtime = LabRuntime(store, config=DEFAULT_LAB_CONFIG)
    result = await runtime.evaluate_candidate(
        _candidate(smart_wallets=(wallet,)), now=1_000
    )
    assert result.smart_money.proven_early == 1
    assert wallet in result.smart_money.wallets


async def test_engine_lab_cycle_never_breaks_the_research_pipeline(tmp_path, monkeypatch) -> None:
    """A laboratory failure must be contained, not take the runner down."""

    from smart_money_bot.config import Settings
    from smart_money_bot.engine import SmartMoneyEngine

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "engine.db"))
    monkeypatch.setenv("COIN_CALLOUTS_ENABLED", "false")
    settings = Settings.from_env(require_discord_token=False)
    engine = SmartMoneyEngine(settings)
    await engine.initialize()
    try:
        engine.lab.evaluate_candidate = AsyncMock(side_effect=RuntimeError("boom"))
        assert await engine._run_lab_cycle(_candidate(), now=1_000) is None
    finally:
        await engine.database.close()


async def test_engine_registers_the_champion_strategy(tmp_path, monkeypatch) -> None:
    from smart_money_bot.config import Settings
    from smart_money_bot.engine import SmartMoneyEngine

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "engine2.db"))
    monkeypatch.setenv("COIN_CALLOUTS_ENABLED", "false")
    settings = Settings.from_env(require_discord_token=False)
    engine = SmartMoneyEngine(settings)
    await engine.initialize()
    try:
        rows = await engine.lab_store.strategy_rows()
        assert rows
        assert rows[0]["role"] == "CHAMPION"
        assert rows[0]["config_hash"] == engine._lab_config.config_hash()

        status = await engine.lab_status()
        assert status["live_execution"] is False
        assert status["broad_social_radar"] is False
    finally:
        await engine.database.close()
