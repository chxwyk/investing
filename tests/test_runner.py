from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from smart_money_bot import bot as bot_module
from smart_money_bot.bot import (
    FomoCommands,
    FomoRunnerLabView,
    RunnerAlertView,
    RunnerXVerificationConfirmationView,
    SmartMoneyBot,
    _runner_digest_embed,
    _runner_embed,
    _runner_forensic_embed,
    _runner_fresh_embed,
)
from smart_money_bot.callouts import CoinCalloutAnalyzer, parse_dex_snapshot
from smart_money_bot.database import Database
from smart_money_bot.engine import SmartMoneyEngine
from smart_money_bot.models import (
    CoinCallout,
    DetectedSwap,
    DexSnapshot,
    DiscoveryCandidate,
    RunnerForensics,
    RunnerFundingObservation,
    RunnerMarketSnapshot,
    RunnerSafetyAssessment,
    Side,
    SwapQuote,
    TokenInfo,
    TokenRiskSnapshot,
    XSocialSnapshot,
)
from smart_money_bot.quality import STAGE_RAW, STAGE_SILENT_WATCH
from smart_money_bot.runner import (
    assess_runner_safety,
    build_funding_clusters,
    forward_return_percent,
    fresh_watch_schedule,
    is_fresh_research_worthy,
    runner_candidate_from_json,
    runner_candidate_to_json,
    runner_path_metrics,
    runner_snapshot_from_callout,
    runner_snapshot_to_json,
    score_runner_candidate,
    summarize_forensics,
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
        engine.database.store_runner_forensics = AsyncMock()
        engine.database.record_runner_outcome = AsyncMock(return_value=True)
        base_callout = _callout(
            liquidity="30000",
            holders=300,
            pair_age=2,
            change_5m="20",
            smart_wallets=("alpha", "beta", "gamma"),
        )
        engine.analyze_coin = AsyncMock(
            return_value=replace(
                base_callout,
                token_info=replace(
                    base_callout.token_info,
                    mint_authority_disabled=True,
                    freeze_authority_disabled=True,
                ),
                sell_quote=_quote(),
            )
        )
        engine._collect_runner_forensics = AsyncMock(
            return_value=summarize_forensics(
                (
                    RunnerFundingObservation(wallet="a", funder="funder-a"),
                    RunnerFundingObservation(wallet="b", funder="funder-b"),
                ),
                raw_unique_buyers=5,
                raw_top10_percent=Decimal("25"),
                checked_at=int(time.time()),
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
    verified = await x_engine.analyze_runner(MINT, deep_forensics=True)

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

    engine._fast_watch_runner.assert_awaited_once_with(MINT, fresh=False)
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
    # v2.35: the production pool admits anything that reached a user-facing
    # funnel stage even at a low legacy score, so a "weak" fixture now has to be
    # genuinely unqualified rather than merely low-scoring.
    weak = replace(
        _candidate(),
        score=Decimal("12"),
        research_only=True,
        stage="SILENT_WATCH",
        best_stage="SILENT_WATCH",
    )
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
async def test_fomo_lab_command_resolves_deferred_original_response(settings) -> None:
    candidate = _candidate()
    engine = SimpleNamespace(runner_lab_candidates=AsyncMock(return_value=(candidate,)))
    bot = SimpleNamespace(settings=settings, engine=engine)
    commands = FomoCommands(bot)
    commands._require_admin = AsyncMock(return_value=True)
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    await FomoCommands.lab.callback(commands, interaction, mode="production")

    interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
    interaction.edit_original_response.assert_awaited_once()
    kwargs = interaction.edit_original_response.await_args.kwargs
    assert kwargs["embed"] is not None, kwargs
    assert MINT in kwargs["embed"].description
    assert isinstance(kwargs["view"], FomoRunnerLabView)


@pytest.mark.asyncio
async def test_fomo_lab_test_answers_immediately_from_persisted_observation(settings) -> None:
    candidate = _candidate()
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    async def cached_after_defer(**_kwargs):
        interaction.response.defer.assert_awaited_once_with(
            thinking=True,
            ephemeral=True,
        )
        return (candidate,)

    engine = SimpleNamespace(
        runner_lab_cached_candidates=AsyncMock(side_effect=cached_after_defer),
        runner_lab_candidates=AsyncMock(),
    )
    bot = SimpleNamespace(settings=settings, engine=engine)
    commands = FomoCommands(bot)
    commands._require_admin = AsyncMock(return_value=True)

    await FomoCommands.lab.callback(commands, interaction, mode="test")

    engine.runner_lab_cached_candidates.assert_awaited_once_with(
        research_test=True,
        max_age_seconds=86_400,
    )
    engine.runner_lab_candidates.assert_not_awaited()
    interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
    interaction.response.send_message.assert_not_awaited()
    interaction.edit_original_response.assert_awaited_once()
    kwargs = interaction.edit_original_response.await_args.kwargs
    assert MINT in kwargs["embed"].description
    assert isinstance(kwargs["view"], FomoRunnerLabView)


@pytest.mark.asyncio
async def test_fomo_lab_empty_cache_shows_refresh_then_candidate(settings) -> None:
    candidate = _candidate()
    engine = SimpleNamespace(
        runner_lab_cached_candidates=AsyncMock(return_value=()),
        runner_lab_candidates=AsyncMock(return_value=(candidate,)),
    )
    bot = SimpleNamespace(settings=settings, engine=engine)
    commands = FomoCommands(bot)
    commands._require_admin = AsyncMock(return_value=True)
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    await FomoCommands.lab.callback(commands, interaction, mode="test")

    interaction.response.defer.assert_awaited_once()
    interaction.response.send_message.assert_not_awaited()
    assert interaction.edit_original_response.await_count == 2
    loading = interaction.edit_original_response.await_args_list[0].kwargs
    assert loading["content"] == "Refreshing one real public candidate..."
    final = interaction.edit_original_response.await_args_list[1].kwargs
    assert isinstance(final["view"], FomoRunnerLabView)


@pytest.mark.asyncio
async def test_fomo_lab_provider_timeout_is_visible_after_defer(settings) -> None:
    engine = SimpleNamespace(
        runner_lab_cached_candidates=AsyncMock(return_value=()),
        runner_lab_candidates=AsyncMock(side_effect=TimeoutError),
    )
    bot = SimpleNamespace(settings=settings, engine=engine)
    commands = FomoCommands(bot)
    commands._require_admin = AsyncMock(return_value=True)
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    await FomoCommands.lab.callback(commands, interaction, mode="test")

    interaction.response.defer.assert_awaited_once()
    interaction.response.send_message.assert_not_awaited()
    error = interaction.edit_original_response.await_args.kwargs["content"]
    assert "timed out" in error
    assert "no X credits, SOL, buy, or J7" in error


@pytest.mark.asyncio
async def test_runner_lab_refreshes_bounded_candidates_concurrently(settings) -> None:
    engine = SmartMoneyEngine(settings)
    engine.initialize = AsyncMock()
    engine.dex_screener.trending_mints = AsyncMock(return_value=(MINT, MINT_TWO))
    engine.database.recent_observed_token_mints = AsyncMock(return_value=[])
    engine.database.recent_runner_candidate_payloads = AsyncMock(return_value=[])
    active = 0
    peak = 0
    both_started = asyncio.Event()

    async def analyze(mint: str, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if peak >= 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        active -= 1
        return replace(_candidate(), mint=mint)

    engine.analyze_runner = AsyncMock(side_effect=analyze)

    candidates = await engine.runner_lab_candidates(research_test=True)

    assert peak == 2
    assert {item.mint for item in candidates} == {MINT, MINT_TWO}


@pytest.mark.asyncio
async def test_runner_lab_uses_fresh_persisted_candidate_without_live_wait(settings) -> None:
    now = int(time.time())
    cached = replace(
        _candidate(now=now, graduated_at=now - 60),
        first_seen_at=now - 60,
        generated_at=now,
    )
    engine = SmartMoneyEngine(settings)
    engine.initialize = AsyncMock()
    engine.dex_screener.trending_mints = AsyncMock(return_value=(MINT_TWO,))
    engine.database.recent_observed_token_mints = AsyncMock(return_value=[])
    engine.database.recent_runner_candidate_payloads = AsyncMock(
        return_value=[runner_candidate_to_json(cached)]
    )
    engine.analyze_runner = AsyncMock()

    candidates = await engine.runner_lab_candidates(research_test=True)

    assert candidates == (cached,)
    engine.analyze_runner.assert_not_awaited()


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
    assert settings.fomo_runner_fast_watch_seconds == 15
    assert settings.fomo_runner_fast_watch_min_score == Decimal("20")
    assert settings.fomo_runner_max_fast_watch == 12
    assert settings.fomo_runner_public_alert_min_score == Decimal("70")
    assert settings.fomo_runner_digest_enabled is True
    assert settings.fomo_runner_digest_seconds == 180
    assert settings.fomo_runner_digest_min_score == Decimal("15")
    assert settings.fomo_runner_digest_max_candidates == 10


@pytest.mark.asyncio
async def test_runner_digest_is_cached_deduplicated_and_never_buys(settings, tmp_path) -> None:
    configured = replace(settings, database_path=str(tmp_path / "digest.db"))
    notifier = SimpleNamespace(on_runner_digest=AsyncMock(), on_error=AsyncMock())
    engine = SmartMoneyEngine(configured, notifier=notifier)
    engine.x_social.snapshot = AsyncMock()
    engine.pump_launcher.j7.launch = AsyncMock()
    engine.executor.execute = AsyncMock()
    candidate = replace(_candidate(), score=Decimal("48"), research_only=True)
    await engine.initialize()
    try:
        await engine.database.store_runner_candidate(
            candidate,
            payload_json=runner_candidate_to_json(candidate),
            snapshot_json=runner_snapshot_to_json(candidate.current),
        )

        assert await engine._publish_runner_digest() is True
        assert await engine._publish_runner_digest() is False

        notifier.on_runner_digest.assert_awaited_once()
        sent, floor = notifier.on_runner_digest.await_args.args
        assert sent == (candidate,)
        assert floor == Decimal("70")
        assert await engine.database.get_setting("runner_digest_fingerprint")
        assert await engine.database.get_setting("runner_last_digest_at")
        engine.x_social.snapshot.assert_not_awaited()
        engine.pump_launcher.j7.launch.assert_not_awaited()
        engine.executor.execute.assert_not_awaited()
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_runner_digest_excludes_strong_public_alerts(settings, tmp_path) -> None:
    configured = replace(settings, database_path=str(tmp_path / "strong-digest.db"))
    notifier = SimpleNamespace(on_runner_digest=AsyncMock(), on_error=AsyncMock())
    engine = SmartMoneyEngine(configured, notifier=notifier)
    candidate = replace(_candidate(), score=Decimal("72"), research_only=False)
    await engine.initialize()
    try:
        await engine.database.store_runner_candidate(
            candidate,
            payload_json=runner_candidate_to_json(candidate),
            snapshot_json=runner_snapshot_to_json(candidate.current),
        )
        assert await engine._publish_runner_digest() is False
        notifier.on_runner_digest.assert_not_awaited()
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_runner_digest_discord_delivery_never_pings(settings) -> None:
    candidate = replace(_candidate(), score=Decimal("48"), research_only=True)
    fake_bot = SimpleNamespace(_send_alert=AsyncMock())

    await SmartMoneyBot.on_runner_digest(fake_bot, (candidate,), Decimal("70"))

    fake_bot._send_alert.assert_awaited_once()
    kwargs = fake_bot._send_alert.await_args.kwargs
    assert kwargs["ping_user"] is False
    assert "RESEARCH" in fake_bot._send_alert.await_args.args[0].title


@pytest.mark.asyncio
async def test_runner_results_exposes_empirical_score_distribution(settings, tmp_path) -> None:
    configured = replace(settings, database_path=str(tmp_path / "distribution.db"))
    engine = SmartMoneyEngine(configured)
    await engine.initialize()
    scores = (Decimal("20"), Decimal("40"), Decimal("55"), Decimal("65"), Decimal("75"))
    try:
        for index, score in enumerate(scores):
            item = replace(
                _candidate(now=1_800_000_600 + index),
                mint=f"score-mint-{index}",
                score=score,
                research_only=score < 70,
            )
            await engine.database.store_runner_candidate(
                item,
                payload_json=runner_candidate_to_json(item),
                snapshot_json=runner_snapshot_to_json(item.current),
            )

        result = await engine.runner_results()
    finally:
        await engine.close()

    distribution = result["score_distribution"]
    assert distribution["max"] == Decimal("75")
    assert distribution["median"] == Decimal("55")
    assert distribution["p90"] == Decimal("71.0")
    assert distribution["p95"] == Decimal("73.00")
    assert distribution["gte_35"] == 4
    assert distribution["gte_50"] == 3
    assert distribution["gte_60"] == 2
    assert distribution["gte_70"] == 1
    assert len(result["best_current_candidates"]) == 3


def test_entry_safety_is_separate_fail_closed_and_sell_route_required() -> None:
    base = replace(
        _snapshot(_callout(), at=1_800_000_000),
        buy_route_status="PASS",
        sell_route_status="PASS",
        mint_authority_disabled=True,
        freeze_authority_disabled=True,
    )
    forensics = summarize_forensics(
        (
            RunnerFundingObservation(wallet="holder-a", funder="funder-a"),
            RunnerFundingObservation(wallet="holder-b", funder="funder-b"),
        ),
        raw_top10_percent=base.top10_percent,
        checked_at=base.captured_at,
    )

    safe = assess_runner_safety(base, forensics)
    unknown = assess_runner_safety(replace(base, sell_route_status="UNKNOWN"), forensics)
    concentrated = assess_runner_safety(replace(base, top10_percent=Decimal("86.55")), forensics)
    thin = assess_runner_safety(replace(base, liquidity_usd=Decimal("1000")), forensics)
    rugged = assess_runner_safety(replace(base, rugged=True), forensics)
    unsellable = assess_runner_safety(replace(base, sell_route_status="FAIL"), forensics)

    assert safe.status == "PASS"
    assert safe.entry_eligible is True
    assert unknown.status == "UNKNOWN"
    assert unknown.entry_eligible is False
    assert concentrated.status == "FAIL"
    assert thin.status == "FAIL"
    assert rugged.scam_risk_score == Decimal("100.00")
    assert unsellable.status == "FAIL"


def test_shared_funding_time_links_and_cluster_adjusted_ownership() -> None:
    observations = (
        RunnerFundingObservation(
            wallet="wallet-a",
            funder="shared-funder",
            funded_at=1_000,
            amount_sol=Decimal("0.035"),
            bought_at=1_100,
            supply_percent=Decimal("22"),
        ),
        RunnerFundingObservation(
            wallet="wallet-b",
            funder="shared-funder",
            funded_at=1_500,
            amount_sol=Decimal("0.038"),
            bought_at=1_115,
            supply_percent=Decimal("24"),
        ),
        RunnerFundingObservation(
            wallet="wallet-c",
            funder="independent-funder",
            funded_at=900,
            amount_sol=Decimal("1"),
            bought_at=1_120,
            supply_percent=Decimal("2"),
        ),
    )

    clusters = build_funding_clusters(observations)
    forensics = summarize_forensics(
        observations,
        raw_unique_buyers=3,
        raw_top10_percent=Decimal("28"),
        checked_at=2_000,
    )

    assert len(clusters) == 1
    assert clusters[0].wallet_count == 2
    assert clusters[0].funding_interval_seconds == 500
    assert clusters[0].similar_amounts is True
    assert clusters[0].time_linked is True
    assert clusters[0].supply_percent == Decimal("46.00")
    assert forensics.cluster_adjusted_percent == Decimal("46.00")
    assert forensics.estimated_independent_clusters == 2
    assert len(forensics.time_linked_groups) == 1


def test_smart_wallet_score_counts_confirmed_funding_cluster_once() -> None:
    forensic = summarize_forensics(
        (
            RunnerFundingObservation(wallet="wallet-a", funder="same"),
            RunnerFundingObservation(wallet="wallet-b", funder="same"),
        ),
        raw_top10_percent=Decimal("25"),
        checked_at=1_800_000_600,
    )
    item = score_runner_candidate(
        _callout(),
        first=_snapshot(_callout(), at=1_800_000_000),
        current=_snapshot(_callout(), at=1_800_000_600),
        graduated_at=1_800_000_000,
        graduation_source="DEX_PAIR_CREATED_PROXY",
        smart_wallets=("alpha", "beta"),
        smart_wallet_addresses=("wallet-a", "wallet-b"),
        forensics=forensic,
        now=1_800_000_600,
    )

    assert item.raw_smart_wallet_count == 2
    assert item.estimated_independent_smart_wallets == 1
    assert item.breakdown.smart_wallets == 4


def test_post_detection_path_preserves_peak_then_collapse() -> None:
    first = replace(
        _snapshot(_callout(price="1", market_cap="100"), at=1_000),
        price_usd=Decimal("1"),
        market_cap_usd=Decimal("100"),
    )
    series = (
        first,
        replace(first, captured_at=1_060, price_usd=Decimal("1.30")),
        replace(first, captured_at=1_120, price_usd=Decimal("1.60")),
        replace(first, captured_at=1_180, price_usd=Decimal("0.40")),
    )

    path = runner_path_metrics(first, series)

    assert path["peak_return"] == Decimal("60.00")
    assert path["time_to_peak_seconds"] == 120
    assert path["maximum_adverse_excursion"] == Decimal("-60.00")
    assert path["max_drawdown_from_peak"] == Decimal("75.00")
    assert path["plus_25_before_minus_25"] is True
    assert path["plus_50_before_minus_50"] is True


def test_fresh_candidate_uses_exact_links_and_runner_view_has_no_trade_control() -> None:
    candidate = _candidate(
        current_callout=_callout(pair_age=1, buys=12, sells=4, volume="3000"),
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(fomo_referral_code="ref-code"),
        engine=SimpleNamespace(runner_forensic=AsyncMock()),
    )

    assert is_fresh_research_worthy(candidate) is True
    assert fresh_watch_schedule() == (0, 15, 30, 60, 120, 180, 300, 600, 900)
    embed = _runner_fresh_embed(candidate, "ref-code")
    forensic_embed = _runner_forensic_embed(candidate, "ref-code")
    view = RunnerAlertView(bot, candidate)
    buttons = {item.label: item for item in view.children}

    assert candidate.mint in embed.description
    assert f"address={candidate.mint}" in embed.description
    assert candidate.mint in forensic_embed.description
    assert buttons["OPEN FOMO"].url.endswith("r=ref-code&source=share_link")
    assert buttons["OPEN PUMP"].url == f"https://pump.fun/coin/{candidate.mint}"
    assert buttons["SOLSCAN"].url == f"https://solscan.io/token/{candidate.mint}"
    assert set(buttons) == {"OPEN FOMO", "OPEN PUMP", "DEXSCREENER", "SOLSCAN", "FORENSICS"}
    assert all(
        forbidden not in " ".join(buttons).upper()
        for forbidden in ("BUY", "SELL", "J7", "LAUNCH")
    )


def test_runner_digest_links_are_isolated_by_exact_mint() -> None:
    first = _candidate(current_callout=_callout(pair_age=1))
    second = replace(
        _candidate(current_callout=_callout(pair_age=2)),
        mint=MINT_TWO,
        name="Second Token",
        symbol="TWO",
        pair_url="https://not-dex.example/search/TWO",
    )

    embed = _runner_digest_embed((first, second), Decimal("70"), "ref")
    joined = "\n".join(field.value for field in embed.fields)

    assert f"address={MINT}" in joined
    assert f"address={MINT_TWO}" in joined
    assert f"https://pump.fun/coin/{MINT}" in joined
    assert f"https://pump.fun/coin/{MINT_TWO}" in joined
    assert f"https://dexscreener.com/solana/{MINT_TWO}" in joined
    assert "search/TWO" not in joined


@pytest.mark.asyncio
async def test_fresh_alert_bypasses_digest_and_persists_actual_visibility(
    settings,
    tmp_path,
) -> None:
    notifier = SimpleNamespace(on_runner_fresh=AsyncMock(return_value=True))
    engine = SmartMoneyEngine(
        replace(settings, database_path=str(tmp_path / "fresh.db")),
        notifier=notifier,
    )
    candidate = _candidate(
        current_callout=_callout(pair_age=1, buys=12, sells=4, volume="3000"),
    )
    await engine.initialize()
    try:
        await engine.database.store_runner_candidate(
            candidate,
            payload_json=runner_candidate_to_json(candidate),
            snapshot_json=runner_snapshot_to_json(candidate.current),
        )

        assert await engine._maybe_publish_fresh(candidate) is True
        assert await engine._maybe_publish_fresh(candidate) is False
        rows = await engine.database.runner_latency_rows(limit=50)
    finally:
        await engine.close()

    notifier.on_runner_fresh.assert_awaited_once_with(candidate)
    assert rows[0]["first_discord_visible_at"] is not None
    assert rows[0]["first_visible_market_cap_usd"] == float(
        candidate.current.market_cap_usd
    )


@pytest.mark.asyncio
async def test_latency_reports_source_visibility_and_mc_appreciation(settings, tmp_path) -> None:
    engine = SmartMoneyEngine(replace(settings, database_path=str(tmp_path / "latency.db")))
    candidate = replace(
        _candidate(),
        first_seen_at=1_050,
        radar_first_seen_at=1_050,
        pair_created_at=1_000,
        first=replace(_candidate().first, captured_at=1_050, market_cap_usd=Decimal("100")),
        current=replace(
            _candidate().current,
            captured_at=1_050,
            market_cap_usd=Decimal("100"),
        ),
        generated_at=1_050,
    )
    await engine.initialize()
    try:
        await engine.database.store_runner_candidate(
            candidate,
            payload_json=runner_candidate_to_json(candidate),
            snapshot_json=runner_snapshot_to_json(candidate.current),
        )
        await engine.database.mark_runner_visible(
            mint=candidate.mint,
            visible_at=1_090,
            market_cap_usd=Decimal("200"),
        )
        result = await engine.runner_latency(limit=50)
    finally:
        await engine.close()

    assert result["source_to_first_seen_median"] == Decimal("50")
    assert result["first_seen_to_discord_median"] == Decimal("40")
    assert result["discovered_within"]["60s"] == Decimal("100.00")
    assert result["median_mc_appreciation_to_visible"] == Decimal("100.00")


@pytest.mark.asyncio
async def test_risk_escalation_and_invalidation_are_meaningful_and_deduplicated(
    settings,
    tmp_path,
) -> None:
    notifier = SimpleNamespace(
        on_runner_risk_escalation=AsyncMock(return_value=True),
        on_runner_invalidated=AsyncMock(return_value=True),
    )
    engine = SmartMoneyEngine(
        replace(settings, database_path=str(tmp_path / "risk-events.db")),
        notifier=notifier,
    )
    previous = replace(
        _candidate(),
        first_discord_visible_at=1_800_000_610,
        current=replace(_candidate().current, top10_percent=Decimal("25")),
        safety=RunnerSafetyAssessment(status="UNKNOWN"),
    )
    current = replace(
        previous,
        generated_at=previous.generated_at + 60,
        current=replace(previous.current, top10_percent=Decimal("61")),
        safety=RunnerSafetyAssessment(
            scam_risk_score=Decimal("85"),
            scam_risk_level="CRITICAL",
            status="FAIL",
            failures=("Top10 concentration is 61.0%",),
        ),
    )
    await engine.initialize()
    try:
        for item in (previous, current):
            await engine.database.store_runner_candidate(
                item,
                payload_json=runner_candidate_to_json(item),
                snapshot_json=runner_snapshot_to_json(item.current),
            )
        await engine._evaluate_runner_transitions(previous, current)
        await engine._evaluate_runner_transitions(previous, current)
    finally:
        await engine.close()

    notifier.on_runner_risk_escalation.assert_awaited_once()
    risk_changes = notifier.on_runner_risk_escalation.await_args.args[1]
    assert any("Top10" in item for item in risk_changes)
    notifier.on_runner_invalidated.assert_awaited_once()


@pytest.mark.asyncio
async def test_detection_safety_and_forensics_never_gain_future_knowledge(
    settings,
    tmp_path,
) -> None:
    database = Database(str(tmp_path / "no-lookahead.db"), Decimal("1000"))
    await database.connect()
    detected = _candidate()
    future_forensics = RunnerForensics(
        available=True,
        cluster_adjusted_percent=Decimal("60"),
        checked_at=detected.generated_at + 60,
    )
    future = replace(
        detected,
        generated_at=detected.generated_at + 60,
        score=Decimal("90"),
        safety=RunnerSafetyAssessment(status="FAIL"),
        forensics=future_forensics,
        detection_safety=RunnerSafetyAssessment(status="FAIL"),
        detection_forensics=future_forensics,
        detection_score=Decimal("90"),
    )
    try:
        for item in (detected, future):
            await database.store_runner_candidate(
                item,
                payload_json=runner_candidate_to_json(item),
                snapshot_json=runner_snapshot_to_json(item.current),
            )
        stored = runner_candidate_from_json(
            str(await database.runner_candidate_payload(detected.mint))
        )
    finally:
        await database.close()

    assert stored.score == Decimal("90")
    assert stored.detection_score == detected.detection_score
    assert stored.detection_safety == detected.detection_safety
    assert stored.detection_forensics == detected.detection_forensics


@pytest.mark.asyncio
async def test_sellability_check_is_quote_only_and_uses_bought_token_amount() -> None:
    market = SimpleNamespace(api_key="configured", quote_order=AsyncMock(return_value=_quote()))
    analyzer = CoinCalloutAnalyzer(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        market,
    )
    token = _callout().token_info
    buy_quote = _quote()

    sell_quote, error = await analyzer._sell_quote(token, buy_quote)

    assert error is None
    assert sell_quote is not None
    market.quote_order.assert_awaited_once_with(
        input_mint=MINT,
        output_mint=MINT_TWO,
        amount_raw=buy_quote.output_amount_raw,
        input_decimals=6,
        output_decimals=6,
    )
    assert not hasattr(market, "execute_order")


def test_high_signal_with_failed_safety_is_unsafe_momentum_not_entry() -> None:
    strong_social = XSocialSnapshot(
        available=True,
        contract_posts=8,
        contract_authors=6,
        credible_contract_authors=3,
        posts_per_minute=Decimal("0.5"),
    )
    first_callout = _callout(
        price="0.001",
        market_cap="50000",
        volume="1000",
        buys=10,
        sells=5,
    )
    risky_callout = replace(
        _callout(
            price="0.0014",
            market_cap="70000",
            liquidity="30000",
            volume="7000",
            buys=60,
            sells=10,
            holders=400,
            pair_age=2,
            change_5m="25",
            top10="86.55",
            social=strong_social,
            smart_wallets=("alpha", "beta", "gamma"),
        ),
        sell_quote=_quote(),
    )
    forensics = RunnerForensics(
        available=True,
        cluster_adjusted_percent=Decimal("86.55"),
        checked_at=1_800_000_600,
    )

    candidate = score_runner_candidate(
        risky_callout,
        first=_snapshot(first_callout, at=1_800_000_000),
        current=_snapshot(risky_callout, at=1_800_000_600, unique=5),
        history=(
            _snapshot(
                _callout(volume="2000", buys=25, sells=8),
                at=1_800_000_300,
            ),
        ),
        graduated_at=1_800_000_480,
        graduation_source="DEX_PAIR_CREATED_PROXY",
        smart_wallets=("alpha", "beta", "gamma"),
        forensics=forensics,
        now=1_800_000_600,
    )

    assert candidate.score >= 50
    assert candidate.safety.status == "FAIL"
    assert candidate.safety.entry_eligible is False
    assert candidate.state == "⚠️ UNSAFE MOMENTUM"


# --- v2.35.1 `/fomo lab mode:test` permanent-spinner regression ---------------


def _unqualified_cached_candidate(now: int):
    """A real observed candidate that production rightly refuses to surface."""

    return replace(
        _candidate(now=now, graduated_at=now - 60),
        stage=STAGE_RAW,
        score=Decimal("5"),
        hard_blockers=("liquidity pulled",),
        first_seen_at=now - 60,
        generated_at=now,
    )


def _lab_interaction() -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=1),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def _lab_commands(settings, engine) -> FomoCommands:
    commands = FomoCommands(SimpleNamespace(settings=settings, engine=engine))
    commands._require_admin = AsyncMock(return_value=True)
    return commands


@pytest.mark.asyncio
async def test_fomo_lab_test_mode_displays_cached_silent_watch_candidate(settings) -> None:
    """A silent-watch observation renders immediately; it never reaches the live path."""

    now = int(time.time())
    silent = replace(_candidate(now=now), stage=STAGE_SILENT_WATCH, generated_at=now)
    engine = SimpleNamespace(
        runner_lab_cached_candidates=AsyncMock(return_value=(silent,)),
        runner_lab_candidates=AsyncMock(),
    )
    interaction = _lab_interaction()

    await FomoCommands.lab.callback(_lab_commands(settings, engine), interaction, mode="test")

    engine.runner_lab_candidates.assert_not_awaited()
    interaction.edit_original_response.assert_awaited_once()
    kwargs = interaction.edit_original_response.await_args.kwargs
    assert kwargs["embed"] is not None
    assert MINT in kwargs["embed"].description
    assert "SILENT WATCH" in kwargs["embed"].description
    assert isinstance(kwargs["view"], FomoRunnerLabView)


@pytest.mark.asyncio
async def test_fomo_lab_test_mode_displays_rejected_unqualified_candidate(settings) -> None:
    """Test mode inspects real rejected candidates without weakening production."""

    now = int(time.time())
    rejected = _unqualified_cached_candidate(now)
    engine = SmartMoneyEngine(settings)
    engine.initialize = AsyncMock()
    engine.database.recent_runner_candidate_payloads = AsyncMock(
        return_value=[runner_candidate_to_json(rejected)]
    )

    shown = await engine.runner_lab_cached_candidates(research_test=True)
    withheld = await engine.runner_lab_cached_candidates(research_test=False)

    assert [item.mint for item in shown] == [rejected.mint]
    assert shown[0].stage == STAGE_RAW
    # Production qualification is unchanged: the same row stays hidden.
    assert withheld == ()


@pytest.mark.asyncio
async def test_fomo_lab_test_mode_makes_no_provider_calls_when_cache_exists(settings) -> None:
    now = int(time.time())
    engine = SmartMoneyEngine(settings)
    engine.initialize = AsyncMock()
    engine.database.recent_runner_candidate_payloads = AsyncMock(
        return_value=[runner_candidate_to_json(_unqualified_cached_candidate(now))]
    )
    engine.dex_screener.trending_mints = AsyncMock()
    engine.analyze_runner = AsyncMock()
    engine.database.recent_observed_token_mints = AsyncMock()
    interaction = _lab_interaction()

    await FomoCommands.lab.callback(_lab_commands(settings, engine), interaction, mode="test")

    engine.dex_screener.trending_mints.assert_not_awaited()
    engine.analyze_runner.assert_not_awaited()
    engine.database.recent_observed_token_mints.assert_not_awaited()
    assert interaction.edit_original_response.await_args.kwargs["embed"] is not None


@pytest.mark.asyncio
async def test_fomo_lab_resolves_deferred_response_when_discord_rejects_card(
    settings,
) -> None:
    """A rejected card degrades to visible text instead of a permanent spinner."""

    now = int(time.time())
    engine = SimpleNamespace(
        runner_lab_cached_candidates=AsyncMock(
            return_value=(replace(_candidate(now=now), stage=STAGE_SILENT_WATCH),)
        ),
        runner_lab_candidates=AsyncMock(),
    )
    interaction = _lab_interaction()
    interaction.edit_original_response = AsyncMock(
        side_effect=[RuntimeError("400 Bad Request (embeds)"), None]
    )

    await FomoCommands.lab.callback(_lab_commands(settings, engine), interaction, mode="test")

    assert interaction.edit_original_response.await_count == 2
    fallback = interaction.edit_original_response.await_args_list[1].kwargs
    assert fallback["embed"] is None
    assert fallback["view"] is None
    assert "Discord rejected the rendered card" in fallback["content"]


@pytest.mark.asyncio
async def test_fomo_lab_always_resolves_when_render_raises(settings) -> None:
    """Any unexpected failure after the defer still replaces the spinner."""

    engine = SimpleNamespace(
        runner_lab_cached_candidates=AsyncMock(side_effect=BufferError("boom")),
        runner_lab_candidates=AsyncMock(),
    )
    interaction = _lab_interaction()

    await FomoCommands.lab.callback(_lab_commands(settings, engine), interaction, mode="test")

    interaction.edit_original_response.assert_awaited()
    content = interaction.edit_original_response.await_args.kwargs["content"]
    assert "BufferError" in content
    assert "No buy or launch was attempted" in content


@pytest.mark.asyncio
async def test_fomo_lab_hard_deadline_replaces_spinner_with_visible_error(
    settings, monkeypatch
) -> None:
    """A stage that hangs past the hard deadline reports visibly, never spins forever."""

    monkeypatch.setattr(bot_module, "FOMO_LAB_TOTAL_DEADLINE_SECONDS", 0.05)

    async def never_returns(**_kwargs):
        await asyncio.sleep(30)

    engine = SimpleNamespace(
        runner_lab_cached_candidates=AsyncMock(side_effect=never_returns),
        runner_lab_candidates=AsyncMock(),
    )
    interaction = _lab_interaction()

    await FomoCommands.lab.callback(_lab_commands(settings, engine), interaction, mode="test")

    interaction.edit_original_response.assert_awaited_once()
    kwargs = interaction.edit_original_response.await_args.kwargs
    assert "hard deadline" in kwargs["content"]
    assert kwargs["embed"] is None
    assert kwargs["view"] is None


def test_runner_embed_clamps_attacker_controlled_token_metadata() -> None:
    """On-chain metadata cannot push the card past Discord's embed limits."""

    junk = replace(_candidate(), stage=STAGE_RAW, name="A" * 5_000, symbol="B" * 400)

    embed = _runner_embed(junk, research_test=True)

    assert len(embed.description) <= bot_module.DISCORD_EMBED_DESCRIPTION_LIMIT
    assert len(embed) <= bot_module.DISCORD_EMBED_TOTAL_LIMIT
    assert len(embed.title) <= bot_module.DISCORD_EMBED_TITLE_LIMIT
    assert junk.mint in embed.description
