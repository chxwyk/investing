from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from smart_money_bot.bot import FomoCommands, FomoRunnerLabView, RunnerXVerificationConfirmationView
from smart_money_bot.callouts import parse_dex_snapshot
from smart_money_bot.database import Database
from smart_money_bot.engine import SmartMoneyEngine
from smart_money_bot.models import (
    CoinCallout,
    DetectedSwap,
    DexSnapshot,
    DiscoveryCandidate,
    RunnerMarketSnapshot,
    Side,
    SwapQuote,
    TokenInfo,
    TokenRiskSnapshot,
    XSocialSnapshot,
)
from smart_money_bot.runner import (
    forward_return_percent,
    runner_candidate_from_json,
    runner_candidate_to_json,
    runner_snapshot_from_callout,
    runner_snapshot_to_json,
    score_runner_candidate,
)

MINT = "So11111111111111111111111111111111111111112"
MINT_TWO = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _quote(*, impact: str = "1") -> SwapQuote:
    return SwapQuote(
        input_mint=MINT_TWO,
        output_mint=MINT,
        input_amount_raw=5_000_000,
        output_amount_raw=5_000_000,
        other_amount_threshold_raw=4_900_000,
        input_amount=Decimal("5"),
        output_amount=Decimal("5000000"),
        input_usd_value=Decimal("5"),
        output_usd_value=Decimal("5"),
        price_impact_percent=Decimal(impact),
        router="Jupiter",
        fee_bps=0,
        api_time_ms=10,
        observed_latency_ms=20,
        quoted_at=1_800_000_000,
    )


def _callout(
    *,
    price: str = "0.001",
    market_cap: str = "50000",
    liquidity: str = "15000",
    volume: str = "3000",
    buys: int = 30,
    sells: int = 10,
    holders: int = 120,
    pair_age: int = 6,
    change_5m: str = "8",
    risk: str = "3",
    bundlers: str = "2",
    insiders: str = "1",
    snipers: str = "3",
    dev: str = "2",
    top10: str = "25",
    route: bool = True,
    social: XSocialSnapshot | None = None,
    smart_wallets: tuple[str, ...] = (),
) -> CoinCallout:
    token = TokenInfo(
        mint=MINT,
        symbol="RUN",
        name="Real Runner",
        decimals=6,
        usd_price=Decimal(price),
        liquidity_usd=Decimal(liquidity),
        market_cap_usd=Decimal(market_cap),
        holder_count=holders,
        top_holders_percent=Decimal(top10),
        dev_balance_percent=Decimal(dev),
    )
    return CoinCallout(
        mint=MINT,
        symbol="RUN",
        name="Real Runner",
        score=Decimal("50"),
        verdict="WATCH",
        confidence="MEDIUM",
        smart_wallets=smart_wallets,
        token_info=token,
        dex=DexSnapshot(
            available=True,
            liquidity_usd=Decimal(liquidity),
            market_cap_usd=Decimal(market_cap),
            pair_age_minutes=pair_age,
            buys_5m=buys,
            sells_5m=sells,
            volume_5m_usd=Decimal(volume),
            price_change_5m_percent=Decimal(change_5m),
            pair_url=f"https://dexscreener.com/solana/{MINT}",
        ),
        social=social or XSocialSnapshot(available=False),
        tracker_risk=TokenRiskSnapshot(
            available=True,
            score=Decimal(risk),
            bundlers_percent=Decimal(bundlers),
            insiders_percent=Decimal(insiders),
            snipers_percent=Decimal(snipers),
            dev_percent=Decimal(dev),
            top10_percent=Decimal(top10),
        ),
        positives=(),
        warnings=(),
        hard_blockers=(),
        generated_at=1_800_000_000,
        executable_quote=_quote() if route else None,
        quote_error=None if route else "no route",
    )


def _snapshot(
    callout: CoinCallout,
    *,
    at: int,
    unique: int = 0,
    dominance: str | None = None,
) -> RunnerMarketSnapshot:
    return runner_snapshot_from_callout(
        callout,
        captured_at=at,
        verified_unique_buyers=unique,
        largest_verified_buyer_percent=(Decimal(dominance) if dominance else None),
    )


def _candidate(
    *,
    first_callout: CoinCallout | None = None,
    current_callout: CoinCallout | None = None,
    now: int = 1_800_000_600,
    graduated_at: int | None = 1_800_000_000,
    history: tuple[RunnerMarketSnapshot, ...] = (),
    unique: int = 0,
    dominance: str | None = None,
    smart_wallets: tuple[str, ...] = (),
    earliest_smart_entry_at: int | None = None,
):
    first_callout = first_callout or _callout()
    current_callout = current_callout or first_callout
    first = _snapshot(first_callout, at=1_800_000_000)
    current = _snapshot(
        current_callout,
        at=now,
        unique=unique,
        dominance=dominance,
    )
    return score_runner_candidate(
        current_callout,
        first=first,
        current=current,
        history=history,
        graduated_at=graduated_at,
        graduation_source="DEX_PAIR_CREATED_PROXY — not exact Pump graduation",
        earliest_smart_entry_at=earliest_smart_entry_at,
        smart_wallets=smart_wallets,
        now=now,
    )


def _discovery_wallet(address: str, alias: str, rank: int) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        address=address,
        alias=alias,
        realized_pnl_24h=Decimal("500"),
        previous_pnl_24h=None,
        roi_24h_percent=Decimal("20"),
        win_rate_percent=Decimal("70"),
        trades_24h=20,
        buys_24h=10,
        sells_24h=10,
        closed_tokens=5,
        invested_24h_usd=Decimal("1000"),
        volume_24h_usd=Decimal("2000"),
        last_trade_ms=1_800_000_000_000,
        score=Decimal("85"),
        rank=rank,
        realized_pnl_7d=Decimal("2000"),
        roi_7d_percent=Decimal("40"),
        win_rate_7d_percent=Decimal("70"),
        trades_7d=100,
        recent_swaps=5,
        pump_swaps=3,
        last_activity_at=1_800_000_000,
    )


def test_new_pair_recency_and_momentum_are_separate_runner_evidence() -> None:
    first_callout = _callout(price="0.001", market_cap="50000", volume="1000", buys=10, sells=10)
    current_callout = _callout(price="0.0012", market_cap="65000", volume="4000", buys=35, sells=10)
    prior = _snapshot(
        _callout(price="0.0011", market_cap="58000", volume="1500", buys=18, sells=12),
        at=1_800_000_300,
    )

    item = _candidate(
        first_callout=first_callout,
        current_callout=current_callout,
        history=(prior,),
    )

    assert item.breakdown.graduation_recency == 10
    assert item.breakdown.momentum >= 12
    assert item.breakdown.acceleration == 9
    assert item.current.dex_price_change_5m_percent == Decimal("8")
    assert item.momentum_windows[-1].seconds == 300
    assert item.momentum_windows[-1].price_change_percent > 0


def test_short_interval_windows_use_only_prior_snapshots() -> None:
    now = 1_800_000_600
    first_callout = _callout(price="0.001", market_cap="50000", volume="1000")
    current_callout = _callout(price="0.0015", market_cap="75000", volume="5000")
    history = tuple(
        _snapshot(
            _callout(
                price=str(Decimal("0.001") + Decimal(index) / Decimal("100000")),
                market_cap=str(50_000 + index * 500),
                volume=str(1_000 + index * 100),
            ),
            at=now - seconds,
        )
        for index, seconds in enumerate((15, 30, 60, 180, 300), start=1)
    )

    item = _candidate(
        first_callout=first_callout,
        current_callout=current_callout,
        history=history,
        now=now,
    )

    assert tuple(window.seconds for window in item.momentum_windows) == (15, 30, 60, 180, 300)
    assert all(window.price_change_percent is not None for window in item.momentum_windows)
    assert all(
        window.rolling_volume_change_percent is not None
        for window in item.momentum_windows
    )


def test_dex_pair_creation_age_is_explicit_proxy(monkeypatch) -> None:
    monkeypatch.setattr("smart_money_bot.callouts.time.time", lambda: 1_800_000_000)
    payload = {
        "pairs": [
            {
                "chainId": "solana",
                "baseToken": {"address": MINT},
                "pairCreatedAt": (1_800_000_000 - 8 * 60) * 1_000,
                "liquidity": {"usd": "10000"},
                "marketCap": "50000",
                "txns": {"m5": {"buys": 10, "sells": 4}},
                "volume": {"m5": "2500"},
                "priceChange": {"m5": "7"},
                "url": f"https://dexscreener.com/solana/{MINT}",
            }
        ]
    }

    snapshot = parse_dex_snapshot(payload, mint=MINT)

    assert snapshot.pair_age_minutes == 8
    assert snapshot.available is True


@pytest.mark.parametrize(
    ("age_seconds", "expected"),
    [(240, 12), (600, 10), (1_200, 7), (2_400, 4), (7_200, 1)],
)
def test_graduation_age_buckets_are_measured_not_claimed_exact(age_seconds, expected) -> None:
    now = 1_800_010_000
    item = _candidate(now=now, graduated_at=now - age_seconds)

    assert item.breakdown.graduation_recency == expected
    assert "not exact Pump graduation" in item.graduation_source


def test_unique_buyers_help_but_single_wallet_dominance_is_penalized() -> None:
    diverse = _candidate(unique=5, dominance="25")
    dominated = _candidate(unique=5, dominance="75")

    assert diverse.breakdown.buy_quality == dominated.breakdown.buy_quality
    assert dominated.breakdown.penalties == diverse.breakdown.penalties - 8
    assert any("dominates" in warning for warning in dominated.warnings)


def test_holder_growth_and_concentration_are_distinct() -> None:
    first_callout = _callout(holders=40, top10="25")
    current_callout = _callout(holders=125, top10="50")

    item = _candidate(first_callout=first_callout, current_callout=current_callout)

    assert item.breakdown.holders >= 7
    assert any("grew by 85" in positive for positive in item.positives)
    assert any("top holders concentration" in blocker for blocker in item.hard_blockers)


def test_early_smart_wallet_entry_beats_late_chasing() -> None:
    early = _candidate(
        smart_wallets=("alpha", "beta"),
        earliest_smart_entry_at=1_800_000_120,
    )
    late = _candidate(
        smart_wallets=("alpha", "beta"),
        earliest_smart_entry_at=1_800_002_100,
    )

    assert early.breakdown.smart_wallets == 11
    assert late.breakdown.smart_wallets == 8
    assert late.breakdown.penalties == early.breakdown.penalties - 3
    assert late.earliest_smart_entry_age_seconds == 2_100


def test_risk_bundler_insider_sniper_dev_and_route_blockers_are_explicit() -> None:
    item = _candidate(
        current_callout=_callout(
            risk="9",
            bundlers="25",
            insiders="25",
            snipers="40",
            dev="15",
            top10="50",
            route=False,
        )
    )

    joined = " | ".join(item.hard_blockers)
    assert "Tracker risk" in joined
    assert "bundlers" in joined
    assert "insiders" in joined
    assert "snipers" in joined
    assert "developer" in joined
    assert "top holders" in joined
    assert "Jupiter route" in joined
    assert item.tier == "BLOCKED — RESEARCH ONLY"


def test_overextension_and_liquidity_removal_prevent_late_chase() -> None:
    first_callout = _callout(price="0.001", market_cap="50000", liquidity="20000")
    current_callout = _callout(
        price="0.004",
        market_cap="210000",
        liquidity="1000",
        change_5m="120",
    )

    item = _candidate(first_callout=first_callout, current_callout=current_callout)

    assert item.overextended is True
    assert item.breakdown.penalties <= -15
    assert any("liquidity" in blocker for blocker in item.hard_blockers)


def test_exact_contract_x_can_add_evidence_but_spam_is_penalized() -> None:
    strong = XSocialSnapshot(
        available=True,
        contract_posts=8,
        contract_authors=6,
        credible_contract_authors=3,
        posts_per_minute=Decimal("0.5"),
        duplicate_percent=Decimal("5"),
    )
    spam = replace(strong, duplicate_percent=Decimal("70"))

    clean = _candidate(current_callout=_callout(social=strong))
    noisy = _candidate(current_callout=_callout(social=spam))

    assert clean.breakdown.x_social == 9
    assert noisy.breakdown.x_social == 9
    assert noisy.breakdown.penalties == clean.breakdown.penalties - 6


def test_runner_json_roundtrip_preserves_immutable_time_t_fields() -> None:
    item = _candidate(unique=3, dominance="40", smart_wallets=("alpha",))

    restored = runner_candidate_from_json(runner_candidate_to_json(item))

    assert restored == item
    assert restored.first.captured_at < restored.current.captured_at


@pytest.mark.asyncio
async def test_first_seen_snapshot_is_immutable_and_results_survive_restart(tmp_path) -> None:
    path = tmp_path / "runner.db"
    database = Database(str(path), Decimal("1000"))
    await database.connect()
    first = _candidate()
    updated = replace(
        _candidate(
            first_callout=_callout(price="0.002", market_cap="90000"),
            current_callout=_callout(price="0.003", market_cap="120000"),
            now=1_800_000_900,
        ),
        first_seen_at=1_800_000_900,
    )
    try:
        assert await database.store_runner_candidate(
            first,
            payload_json=runner_candidate_to_json(first),
            snapshot_json=runner_snapshot_to_json(first.current),
        ) is True
        assert await database.store_runner_candidate(
            updated,
            payload_json=runner_candidate_to_json(updated),
            snapshot_json=runner_snapshot_to_json(updated.current),
        ) is False
        await database.record_runner_outcome(
            mint=MINT,
            horizon_seconds=60,
            observed_at=1_800_000_900,
            price_return_percent=Decimal("20"),
            market_cap_return_percent=Decimal("25"),
            liquidity_return_percent=Decimal("5"),
            liquidity_disappeared=False,
            rugged=False,
            route_available=True,
        )
    finally:
        await database.close()

    reopened = Database(str(path), Decimal("1000"))
    await reopened.connect()
    try:
        rows = await reopened.runner_results_rows()
        stored = runner_candidate_from_json(
            str(await reopened.runner_candidate_payload(MINT))
        )
        assert rows[0]["first_seen_at"] == first.first_seen_at
        assert stored.first == first.first
        assert stored.first_seen_at == first.first_seen_at
        assert stored.current == updated.current
        assert rows[0]["horizon_seconds"] == 60
        assert rows[0]["price_return_percent"] == 20
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_verified_unique_buyer_calculation_uses_qualified_wallets(
    tmp_path,
) -> None:
    database = Database(str(tmp_path / "buyers.db"), Decimal("1000"))
    await database.connect()
    try:
        wallets = [
            _discovery_wallet("wallet-a", "alpha", 1),
            _discovery_wallet("wallet-b", "beta", 2),
        ]
        await database.apply_discovery(wallets)
        for signature, wallet, value, block_time in (
            ("a1", "wallet-a", "70", 1_800_000_100),
            ("a2", "wallet-a", "10", 1_800_000_110),
            ("b1", "wallet-b", "20", 1_800_000_120),
        ):
            await database.record_swap(
                DetectedSwap(
                    signature=signature,
                    trader_address=wallet,
                    block_time=block_time,
                    side=Side.BUY,
                    token_mint=MINT,
                    token_amount=Decimal("100"),
                    quote_mint=MINT_TWO,
                    quote_amount=Decimal(value),
                    usd_value=Decimal(value),
                    token_price_usd=Decimal("0.001"),
                )
            )
        evidence = await database.recent_verified_token_buy_evidence(MINT, 1_800_000_000)
        recent_mints = await database.recent_observed_token_mints()
    finally:
        await database.close()

    assert evidence["unique_buyers"] == 2
    assert len(evidence["wallets"]) == 2
    assert evidence["earliest_buy_at"] == 1_800_000_100
    assert evidence["largest_buyer_percent"] == Decimal("80")
    assert recent_mints == [MINT]


@pytest.mark.asyncio
async def test_forward_outcomes_are_time_based_and_never_feed_score(settings, tmp_path) -> None:
    configured = replace(settings, database_path=str(tmp_path / "outcomes.db"))
    engine = SmartMoneyEngine(configured)
    await engine.database.connect()
    first = _candidate(now=1_800_000_000, graduated_at=1_799_999_900)
    try:
        await engine.database.store_runner_candidate(
            first,
            payload_json=runner_candidate_to_json(first),
            snapshot_json=runner_snapshot_to_json(first.current),
        )
        latest = first
        for index, horizon in enumerate((60, 300, 900, 1_800, 3_600, 14_400, 86_400), start=1):
            latest = replace(
                first,
                current=replace(
                    first.current,
                    captured_at=first.first_seen_at + horizon,
                    price_usd=first.first.price_usd * (Decimal("1") + Decimal(index) / 10),
                    market_cap_usd=(
                        first.first.market_cap_usd * (Decimal("1") + Decimal(index) / 10)
                    ),
                ),
                generated_at=first.first_seen_at + horizon,
            )
            await engine.database.store_runner_candidate(
                latest,
                payload_json=runner_candidate_to_json(latest),
                snapshot_json=runner_snapshot_to_json(latest.current),
            )
            await engine._record_runner_outcomes(latest)
        results = await engine.runner_results()
    finally:
        await engine.close()

    assert results["by_horizon"][60]["count"] == 1
    assert results["by_horizon"][300]["count"] == 1
    assert results["by_horizon"][900]["count"] == 1
    assert results["by_horizon"][1_800]["count"] == 1
    assert results["by_horizon"][3_600]["count"] == 1
    assert results["by_horizon"][14_400]["count"] == 1
    assert results["by_horizon"][86_400]["count"] == 1
    assert results["breakdowns"]["score"]
    assert results["baselines"]["highest_5m_volume"]
    assert first.score == latest.score


def test_forward_return_uses_only_first_seen_and_current_values() -> None:
    assert forward_return_percent(Decimal("1.25"), Decimal("1")) == Decimal("25.00")
    assert forward_return_percent(None, Decimal("1")) is None


@pytest.mark.asyncio
async def test_late_restart_does_not_backfill_short_horizons(
    settings,
    tmp_path,
) -> None:
    engine = SmartMoneyEngine(replace(settings, database_path=str(tmp_path / "late.db")))
    await engine.database.connect()
    first = _candidate(now=1_800_000_000, graduated_at=1_799_999_900)
    late = replace(
        first,
        current=replace(
            first.current,
            captured_at=first.first_seen_at + 3_600,
            price_usd=first.first.price_usd * 2,
        ),
        generated_at=first.first_seen_at + 3_600,
    )
    try:
        for item in (first, late):
            await engine.database.store_runner_candidate(
                item,
                payload_json=runner_candidate_to_json(item),
                snapshot_json=runner_snapshot_to_json(item.current),
            )
        await engine._record_runner_outcomes(late)
        rows = await engine.database.runner_results_rows()
    finally:
        await engine.close()

    horizons = {row["horizon_seconds"] for row in rows if row["horizon_seconds"] is not None}
    assert horizons == {3_600}


@pytest.mark.asyncio
async def test_runner_manual_x_verify_uses_exact_contract_and_never_calls_j7(settings) -> None:
    candidate = _candidate()
    social = XSocialSnapshot(
        available=True,
        contract_posts=4,
        contract_authors=3,
        credible_contract_authors=2,
        verification_state="CHECKED",
    )
    engine = SmartMoneyEngine(settings)
    engine.x_social.snapshot = AsyncMock(return_value=social)
    engine.analyze_runner = AsyncMock(return_value=replace(candidate, x_evidence=social))
    engine.pump_launcher.j7.launch = AsyncMock()

    updated = await engine.verify_runner_x(candidate)

    engine.x_social.snapshot.assert_awaited_once_with(
        MINT,
        symbol="RUN",
        name="Real Runner",
        context="fomo_runner_manual",
        free_score=int(candidate.score),
    )
    engine.analyze_runner.assert_awaited_once_with(
        MINT,
        refresh_market=False,
        x_evidence=social,
        allow_automatic_x=False,
    )
    engine.pump_launcher.j7.launch.assert_not_awaited()
    assert updated.x_evidence.available is True


@pytest.mark.asyncio
async def test_free_runner_analysis_and_targeted_x_share_client(settings) -> None:
    async def prepare(engine: SmartMoneyEngine) -> None:
        engine.initialize = AsyncMock()
        engine.database.recent_verified_token_buy_evidence = AsyncMock(
            return_value={
                "wallets": ("alpha", "beta", "gamma"),
                "unique_buyers": 5,
                "earliest_buy_at": int(time.time()) - 60,
                "largest_buyer_percent": Decimal("34"),
            }
        )
        engine.database.recent_verified_token_buyers = AsyncMock(
            return_value=[("a", "alpha"), ("b", "beta"), ("c", "gamma")]
        )
        engine.database.runner_candidate_payload = AsyncMock(return_value=None)
        engine.database.runner_snapshot_payloads = AsyncMock(return_value=[])
        engine.database.store_runner_candidate = AsyncMock(return_value=True)
        engine.database.record_runner_outcome = AsyncMock(return_value=True)
        engine.analyze_coin = AsyncMock(
            return_value=_callout(
                liquidity="30000",
                holders=300,
                pair_age=2,
                change_5m="20",
                smart_wallets=("alpha", "beta", "gamma"),
            )
        )

    free_engine = SmartMoneyEngine(settings)
    await prepare(free_engine)
    free_engine.x_social.snapshot = AsyncMock()
    free = await free_engine.analyze_runner(MINT)
    free_engine.x_social.snapshot.assert_not_awaited()
    assert free.x_evidence.available is False

    configured = replace(
        settings,
        x_paid_search_enabled=True,
        x_api_bearer_token="configured-secret",
    )
    x_engine = SmartMoneyEngine(configured)
    await prepare(x_engine)
    strong_x = XSocialSnapshot(
        available=True,
        contract_posts=5,
        contract_authors=4,
        credible_contract_authors=2,
        posts_per_minute=Decimal("0.5"),
    )
    x_engine.x_social.snapshot = AsyncMock(return_value=strong_x)
    verified = await x_engine.analyze_runner(MINT)

    x_engine.x_social.snapshot.assert_awaited_once()
    assert verified.x_evidence.available is True
    assert free_engine.x_budget.database is free_engine.database
    assert x_engine.x_budget.database is x_engine.database


@pytest.mark.asyncio
async def test_two_stage_fast_watch_admits_only_young_promising_candidate(settings) -> None:
    engine = SmartMoneyEngine(settings)
    engine._fast_watch_runner = AsyncMock()
    young = replace(
        _candidate(now=1_800_000_600, graduated_at=1_800_000_000),
        score=Decimal("50"),
    )
    old = replace(
        young,
        mint=MINT_TWO,
        graduated_at=young.generated_at - 7_200,
    )

    engine._start_runner_fast_watch(young)
    engine._start_runner_fast_watch(old)
    await asyncio.sleep(0)

    engine._fast_watch_runner.assert_awaited_once_with(MINT)
    assert MINT_TWO not in engine._runner_fast_watch_tasks


@pytest.mark.asyncio
async def test_fomo_lab_is_admin_only_and_weak_real_candidate_can_display(settings) -> None:
    engine = SimpleNamespace(runner_lab_candidates=AsyncMock(return_value=(_candidate(),)))
    bot = SimpleNamespace(settings=settings, engine=engine)
    commands = FomoCommands(bot)
    unauthorized = SimpleNamespace(
        user=SimpleNamespace(id=999),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await FomoCommands.lab.callback(commands, unauthorized, mode="test")

    engine.runner_lab_candidates.assert_not_awaited()
    unauthorized.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_fomo_lab_test_bypasses_display_floor_only(settings) -> None:
    engine = SmartMoneyEngine(settings)
    weak = replace(_candidate(), score=Decimal("12"), research_only=True)
    engine.initialize = AsyncMock()
    engine.dex_screener.trending_mints = AsyncMock(return_value=(MINT,))
    engine.database.recent_observed_token_mints = AsyncMock(return_value=[])
    engine.database.recent_runner_candidate_payloads = AsyncMock(return_value=[])
    engine.analyze_runner = AsyncMock(return_value=weak)

    test_candidates = await engine.runner_lab_candidates(research_test=True)
    production_candidates = await engine.runner_lab_candidates(research_test=False)

    assert test_candidates == (weak,)
    assert production_candidates == ()
    assert engine.settings.fomo_runner_public_alert_min_score == Decimal("70")
    engine.pump_launcher.j7.launch = AsyncMock()
    engine.pump_launcher.j7.launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_fomo_lab_next_refresh_x_and_close_never_buy_or_call_j7(settings) -> None:
    first = _candidate()
    second = replace(_candidate(now=1_800_000_700), mint=MINT_TWO, name="Second Real Token")
    refreshed = replace(first, score=first.score + 1, generated_at=first.generated_at + 20)
    j7_launch = AsyncMock()
    buy = AsyncMock()
    engine = SimpleNamespace(
        analyze_runner=AsyncMock(return_value=refreshed),
        verify_runner_x=AsyncMock(
            return_value=replace(first, x_evidence=XSocialSnapshot(available=True))
        ),
        x_budget=SimpleNamespace(
            status=AsyncMock(
                return_value={
                    "estimated_spend_today": Decimal("0"),
                    "daily_budget": Decimal("0.50"),
                }
            )
        ),
        pump_launcher=SimpleNamespace(j7=SimpleNamespace(launch=j7_launch)),
        execute=buy,
    )
    configured = replace(
        settings,
        x_paid_search_enabled=True,
        x_api_bearer_token="configured-secret",
    )
    bot = SimpleNamespace(settings=configured, engine=engine)
    view = FomoRunnerLabView(bot, (first, second), owner_id=1, research_test=True)
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        response=SimpleNamespace(
            defer=AsyncMock(),
            edit_message=AsyncMock(),
            send_message=AsyncMock(),
        ),
        edit_original_response=AsyncMock(),
        message=SimpleNamespace(edit=AsyncMock()),
    )
    next_button = next(item for item in view.children if item.label == "NEXT CANDIDATE")
    refresh_button = next(item for item in view.children if item.label == "REFRESH")
    verify_button = next(item for item in view.children if item.label == "VERIFY ON X")
    close_button = next(item for item in view.children if item.label == "CLOSE")

    await next_button.callback(interaction)
    assert view.index == 1
    assert MINT_TWO in interaction.edit_original_response.await_args.kwargs["embed"].description
    await next_button.callback(interaction)
    await refresh_button.callback(interaction)
    assert view.candidate.score == refreshed.score
    engine.analyze_runner.assert_awaited_once_with(
        MINT,
        refresh_market=True,
        allow_automatic_x=False,
    )
    await verify_button.callback(interaction)
    engine.verify_runner_x.assert_not_awaited()

    confirmation = RunnerXVerificationConfirmationView(view)
    await confirmation.children[0].callback(interaction)
    engine.verify_runner_x.assert_awaited_once()
    interaction.message.edit.assert_not_awaited()
    await close_button.callback(interaction)

    j7_launch.assert_not_awaited()
    buy.assert_not_awaited()
    assert all("LAUNCH" not in str(getattr(item, "label", "")) for item in view.children)


def test_runner_defaults_do_not_change_launch_or_x_production_thresholds(settings) -> None:
    assert settings.no_x_launch_min_score == 78
    assert settings.news_x_verify_min_score == 70
    assert settings.pump_launch_min_score == 72
    assert settings.launch_lab_min_score == 60
    assert settings.fomo_runner_enabled is True
    assert settings.fomo_runner_fast_watch_seconds == 20
    assert settings.fomo_runner_public_alert_min_score == Decimal("70")
