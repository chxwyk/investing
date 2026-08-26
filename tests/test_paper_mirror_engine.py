from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from smart_money_bot.engine import SmartMoneyEngine
from smart_money_bot.models import (
    DetectedSwap,
    DiscoveryRefresh,
    ExecutionResult,
    PaperDailyLockStatus,
    RiskDecision,
    Side,
    Signal,
    SwapQuote,
    TokenInfo,
    TrackedTrader,
)


class CaptureNotifier:
    def __init__(self) -> None:
        self.swaps: list[DetectedSwap] = []
        self.signals: list[Signal] = []
        self.executions: list[ExecutionResult] = []
        self.daily_locks: list[PaperDailyLockStatus] = []

    async def on_discovery(self, refresh: DiscoveryRefresh) -> None:
        del refresh

    async def on_swap(self, swap: DetectedSwap, trader: TrackedTrader) -> None:
        del trader
        self.swaps.append(swap)

    async def on_signal(
        self,
        signal: Signal,
        token_info: TokenInfo | None,
        decision: RiskDecision,
    ) -> None:
        del token_info, decision
        self.signals.append(signal)

    async def on_execution(self, result: ExecutionResult) -> None:
        self.executions.append(result)

    async def on_daily_profit_lock(self, status: PaperDailyLockStatus) -> None:
        self.daily_locks.append(status)

    async def on_error(self, context: str, error: Exception) -> None:
        raise AssertionError(f"{context}: {error}")


def _swap(
    side: Side,
    signature: str,
    price: str,
    *,
    mint: str = "mint",
) -> DetectedSwap:
    return DetectedSwap(
        signature=signature,
        trader_address="wallet-a",
        block_time=int(time.time()),
        side=side,
        token_mint=mint,
        token_amount=Decimal("100"),
        quote_mint="quote",
        quote_amount=Decimal("100"),
        usd_value=Decimal("100"),
        token_price_usd=Decimal(price),
    )


@pytest.mark.asyncio
async def test_realtime_and_polling_do_not_process_same_signature_twice(settings) -> None:
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(settings, notifier=notifier)
    await engine.initialize()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_detection(*args, **kwargs):
        del args, kwargs
        started.set()
        await release.wait()
        return None

    try:
        engine.detector.detect = AsyncMock(side_effect=slow_detection)
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        first = asyncio.create_task(
            engine._process_transaction(
                trader,
                signature="same-signature",
                transaction={},
                block_time=int(time.time()),
                is_bootstrap=False,
            )
        )
        await started.wait()

        duplicate = await engine._process_transaction(
            trader,
            signature="same-signature",
            transaction={},
            block_time=int(time.time()),
            is_bootstrap=False,
        )
        release.set()
        processed = await first

        assert duplicate == {"transactions": 0, "swaps": 0}
        assert processed == {"transactions": 1, "swaps": 0}
        engine.detector.detect.assert_awaited_once()
    finally:
        release.set()
        await engine.close()


@pytest.mark.asyncio
async def test_new_raw_swaps_immediately_buy_and_sell_in_paper_mode(settings) -> None:
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(settings, notifier=notifier)
    await engine.initialize()
    try:
        engine.market.price = AsyncMock(side_effect=(Decimal("1"), Decimal("1.2")))
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)

        await engine._handle_new_swap(_swap(Side.BUY, "buy", "1"), trader)
        assert notifier.executions[-1].success is True
        assert notifier.executions[-1].side is Side.BUY
        assert len(await engine.database.paper_mirror_positions()) == 1

        await engine._handle_new_swap(_swap(Side.SELL, "sell", "1.2"), trader)
        assert notifier.executions[-1].success is True
        assert notifier.executions[-1].side is Side.SELL
        assert await engine.database.paper_mirror_positions() == []
        assert notifier.signals == []

        summary = await engine.database.paper_summary({})
        assert summary.realized_pnl_usd > 0
        assert summary.trades == 1
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_raw_paper_buy_uses_current_price_not_source_price(settings) -> None:
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(settings, notifier=notifier)
    await engine.initialize()
    try:
        engine.market.price = AsyncMock(return_value=Decimal("2"))
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)

        await engine._handle_new_swap(_swap(Side.BUY, "late-buy", "0.5"), trader)

        positions = await engine.database.paper_mirror_positions()
        assert notifier.executions[-1].success is True
        assert len(positions) == 1
        average_entry = Decimal(str(positions[0]["average_entry_usd"]))
        assert Decimal("2") < average_entry < Decimal("2.1")
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_forced_observation_fills_without_market_or_risk_gates(settings) -> None:
    observation = replace(
        settings,
        paper_force_observation_mode=True,
        paper_observation_penalty_bps=300,
        paper_raw_entry_filter_enabled=True,
        paper_use_executable_quotes=True,
    )
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(observation, notifier=notifier)
    await engine.initialize()
    try:
        engine.market.price = AsyncMock()
        engine.market.token_info = AsyncMock()
        engine.market.quote_order = AsyncMock()
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)

        await engine._handle_new_swap(_swap(Side.BUY, "observe-buy", "1"), trader)
        assert notifier.executions[-1].success is True
        assert "Forced PAPER observation" in notifier.executions[-1].message
        positions = await engine.database.paper_mirror_positions()
        assert len(positions) == 1
        assert Decimal(str(positions[0]["average_entry_usd"])) > Decimal("1.03")

        await engine._handle_new_swap(_swap(Side.SELL, "observe-sell", "1.5"), trader)
        assert notifier.executions[-1].success is True
        assert await engine.database.paper_mirror_positions() == []

        engine.market.price.assert_not_awaited()
        engine.market.token_info.assert_not_awaited()
        engine.market.quote_order.assert_not_awaited()
        trades = await engine.database.paper_recent_trades()
        assert len(trades) == 2
        assert {item["execution_kind"] for item in trades} == {"FORCED_OBSERVATION"}
        assert all(item["quote_based"] == 0 for item in trades)
        assert Decimal(str(trades[0]["realized_pnl_usd"])) > 0
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_forced_observation_still_enforces_raw_risk_exits(
    settings,
) -> None:
    observation = replace(settings, paper_force_observation_mode=True)
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(observation, notifier=notifier)
    await engine.initialize()
    try:
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)
        for index in range(3):
            await engine._handle_new_swap(_swap(Side.BUY, f"observe-buy-{index}", "1"), trader)

        positions = await engine.database.paper_mirror_positions()
        assert len(positions) == 1
        assert Decimal(str(positions[0]["cost_basis_usd"])) == Decimal("30")

        engine.market.prices = AsyncMock(return_value={"mint": Decimal("0.01")})
        await engine._check_position_exits()
        assert await engine.database.paper_mirror_positions() == []
        assert "hard stop" in notifier.executions[-1].message
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_manual_paper_sell_closes_forced_observation_lot(settings) -> None:
    observation = replace(
        settings,
        paper_force_observation_mode=True,
        paper_observation_penalty_bps=300,
    )
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(observation, notifier=notifier)
    await engine.initialize()
    try:
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)
        await engine._handle_new_swap(_swap(Side.BUY, "manual-buy", "1"), trader)
        assert len(await engine.database.paper_mirror_positions()) == 1

        engine.market.price = AsyncMock(return_value=Decimal("1.5"))
        result = await engine.manual_paper_exit(
            position_kind="RAW_MIRROR",
            token_mint="mint",
            source_trader="wallet-a",
            requested_by="Discord admin",
        )

        assert result.success is True
        assert "Manual PAPER SELL" in result.message
        assert await engine.database.paper_mirror_positions() == []
        trades = await engine.database.paper_recent_trades()
        assert trades[0]["execution_kind"] == "MANUAL_OBSERVATION_EXIT"
        assert trades[0]["quote_based"] == 0
        assert Decimal(str(trades[0]["realized_pnl_usd"])) > 0
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_daily_profit_lock_liquidates_and_blocks_new_buys(settings) -> None:
    observation = replace(
        settings,
        paper_force_observation_mode=True,
        paper_daily_profit_lock_enabled=True,
        paper_daily_target_usd=Decimal("100"),
    )
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(observation, notifier=notifier)
    await engine.initialize()
    try:
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)
        await engine._handle_new_swap(_swap(Side.BUY, "lock-buy", "1"), trader)
        assert len(await engine.database.paper_mirror_positions()) == 1

        engine.market.prices = AsyncMock(return_value={"mint": Decimal("1")})
        armed = await engine.paper_daily_lock_status()
        assert armed.locked is False

        engine.market.prices = AsyncMock(return_value={"mint": Decimal("20")})
        assert await engine._enforce_daily_profit_lock() is True
        assert await engine.database.paper_mirror_positions() == []
        assert len(notifier.daily_locks) == 1
        assert notifier.daily_locks[0].marked_pnl_usd >= Decimal("100")
        assert notifier.daily_locks[0].lock_reason == "PROFIT_TARGET"

        trades = await engine.database.paper_recent_trades()
        assert trades[0]["execution_kind"] == "DAILY_PROFIT_LOCK_EXIT"
        assert Decimal(str(trades[0]["realized_pnl_usd"])) > Decimal("100")

        await engine._handle_new_swap(
            _swap(Side.BUY, "blocked-after-target", "1", mint="mint-2"),
            trader,
        )
        assert await engine.database.paper_mirror_positions() == []
        assert all(item.signature != "blocked-after-target" for item in notifier.swaps)
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_daily_loss_lock_liquidates_and_blocks_new_buys(settings) -> None:
    guarded = replace(
        settings,
        paper_force_observation_mode=True,
        paper_daily_profit_lock_enabled=True,
        paper_daily_loss_lock_enabled=True,
        paper_daily_loss_limit_usd=Decimal("5"),
    )
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(guarded, notifier=notifier)
    await engine.initialize()
    try:
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)
        await engine._handle_new_swap(_swap(Side.BUY, "loss-buy", "1"), trader)
        assert len(await engine.database.paper_mirror_positions()) == 1

        engine.market.prices = AsyncMock(return_value={"mint": Decimal("1")})
        armed = await engine.paper_daily_lock_status()
        assert armed.locked is False

        engine.market.prices = AsyncMock(return_value={"mint": Decimal("0.10")})
        assert await engine._enforce_daily_profit_lock() is True
        assert await engine.database.paper_mirror_positions() == []
        assert len(notifier.daily_locks) == 1
        assert notifier.daily_locks[0].lock_reason == "LOSS_LIMIT"
        assert notifier.daily_locks[0].marked_pnl_usd <= Decimal("-5")

        trades = await engine.database.paper_recent_trades()
        assert trades[0]["execution_kind"] == "DAILY_LOSS_LOCK_EXIT"

        await engine._handle_new_swap(
            _swap(Side.BUY, "blocked-after-loss", "1", mint="mint-2"),
            trader,
        )
        assert await engine.database.paper_mirror_positions() == []
        assert all(item.signature != "blocked-after-loss" for item in notifier.swaps)
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_unrouted_pump_swap_uses_guarded_source_price_paper_fallback(
    settings,
) -> None:
    quoted = replace(
        settings,
        paper_use_executable_quotes=True,
        paper_require_current_price=True,
        paper_allow_pump_source_fallback=True,
        paper_pump_source_fallback_bps=300,
    )
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(quoted, notifier=notifier)
    await engine.initialize()
    pump_mint = "A5G8K3qLzTKhmRL7nRTzG1f8eLTjSfY2uHEdpbt1pump"
    try:
        engine.market.price = AsyncMock(return_value=None)
        engine.market.token_info = AsyncMock(return_value=None)
        engine.market.quote_order = AsyncMock()
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)

        await engine._handle_new_swap(
            _swap(Side.BUY, "pump-buy", "0.00000282", mint=pump_mint),
            trader,
        )
        assert notifier.executions[-1].success is True
        assert "not proof" in notifier.executions[-1].message
        assert len(await engine.database.paper_mirror_positions()) == 1

        await engine._handle_new_swap(
            _swap(Side.SELL, "pump-sell", "0.00000628", mint=pump_mint),
            trader,
        )
        assert notifier.executions[-1].success is True
        assert "does not count" in notifier.executions[-1].message
        assert await engine.database.paper_mirror_positions() == []
        engine.market.quote_order.assert_not_awaited()

        trades = await engine.database.paper_recent_trades()
        assert len(trades) == 2
        assert {item["execution_kind"] for item in trades} == {"PUMP_SOURCE_FALLBACK"}
        assert all(item["quote_based"] == 0 for item in trades)
        assert Decimal(str(trades[0]["realized_pnl_usd"])) > 0
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_unrouted_nonpump_swap_still_fails_closed(settings) -> None:
    strict = replace(
        settings,
        paper_require_current_price=True,
        paper_allow_pump_source_fallback=True,
    )
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(strict, notifier=notifier)
    await engine.initialize()
    try:
        engine.market.price = AsyncMock(return_value=None)
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)

        await engine._handle_new_swap(_swap(Side.BUY, "nonpump-buy", "1"), trader)

        assert notifier.executions[-1].success is False
        assert "no current Jupiter price" in notifier.executions[-1].message
        assert await engine.database.paper_mirror_positions() == []
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_raw_paper_entry_filter_blocks_suspicious_token(settings) -> None:
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(settings, notifier=notifier)
    await engine.initialize()
    try:
        engine.market.price = AsyncMock(return_value=Decimal("1"))
        engine.market.token_info = AsyncMock(return_value=TokenInfo(mint="mint", suspicious=True))
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)

        await engine._handle_new_swap(_swap(Side.BUY, "unsafe-buy", "1"), trader)

        assert notifier.executions[-1].success is False
        assert "suspicious" in notifier.executions[-1].message
        assert await engine.database.paper_mirror_positions() == []
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_sniper_paper_lane_takes_small_low_liquidity_pump_position(
    settings,
) -> None:
    sniper = replace(
        settings,
        paper_sniper_test_enabled=True,
        paper_sniper_copy_usd=Decimal("2"),
    )
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(sniper, notifier=notifier)
    await engine.initialize()
    pump_mint = "A5G8K3qLzTKhmRL7nRTzG1f8eLTjSfY2uHEdpbt1pump"
    try:
        engine.market.price = AsyncMock(return_value=Decimal("1"))
        engine.market.token_info = AsyncMock(
            return_value=TokenInfo(
                mint=pump_mint,
                liquidity_usd=Decimal("5000"),
                holder_count=25,
                organic_score=Decimal("0"),
                top_holders_percent=Decimal("80"),
                mint_authority_disabled=True,
                freeze_authority_disabled=True,
            )
        )
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)

        await engine._handle_new_swap(_swap(Side.BUY, "sniper-buy", "1", mint=pump_mint), trader)

        assert notifier.executions[-1].success is True
        assert "Sniper PAPER" in notifier.executions[-1].message
        positions = await engine.database.paper_mirror_positions()
        assert len(positions) == 1
        assert Decimal(str(positions[0]["cost_basis_usd"])) == Decimal("2")
        trades = await engine.database.paper_recent_trades()
        assert trades[0]["execution_kind"] == "SNIPER_PAPER"
        assert trades[0]["quote_based"] == 0
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_sniper_paper_lane_keeps_absolute_floor_and_ownership_checks(
    settings,
) -> None:
    sniper = replace(settings, paper_sniper_test_enabled=True)
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(sniper, notifier=notifier)
    await engine.initialize()
    pump_mint = "A5G8K3qLzTKhmRL7nRTzG1f8eLTjSfY2uHEdpbt1pump"
    try:
        engine.market.price = AsyncMock(return_value=Decimal("1"))
        engine.market.token_info = AsyncMock(
            return_value=TokenInfo(
                mint=pump_mint,
                liquidity_usd=Decimal("1000"),
                holder_count=25,
                mint_authority_disabled=False,
            )
        )
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)

        await engine._handle_new_swap(_swap(Side.BUY, "sniper-reject", "1", mint=pump_mint), trader)

        assert notifier.executions[-1].success is False
        assert "sniper lane rejected" in notifier.executions[-1].message
        assert "Mint authority is enabled" in notifier.executions[-1].message
        assert await engine.database.paper_mirror_positions() == []
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_unmatched_source_sell_explains_if_buy_never_filled(settings) -> None:
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(settings, notifier=notifier)
    await engine.initialize()
    try:
        engine.market.price = AsyncMock(return_value=Decimal("1"))
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)

        await engine._handle_new_swap(_swap(Side.SELL, "orphan-sell", "1"), trader)

        assert notifier.executions[-1].success is False
        assert "no earlier BUY" in notifier.executions[-1].message
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_existing_source_holding_gets_forward_tracking_baseline(settings) -> None:
    baseline_settings = replace(
        settings,
        paper_seed_tracking_baselines=True,
        paper_force_observation_mode=False,
    )
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(baseline_settings, notifier=notifier)
    await engine.initialize()
    try:
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)
        inserted = await engine.database.record_swap(_swap(Side.BUY, "bootstrap-buy", "1"))
        assert inserted is True

        engine.market.price = AsyncMock(return_value=Decimal("1.50"))
        await engine._seed_tracking_baselines(trader)

        positions = await engine.database.paper_mirror_positions()
        assert len(positions) == 1
        assert Decimal(str(positions[0]["average_entry_usd"])) > Decimal("1.50")
        assert notifier.executions[-1].success is True
        assert "Tracking baseline" in notifier.executions[-1].message
        trades = await engine.database.paper_recent_trades()
        assert trades[0]["execution_kind"] == "TRACKING_BASELINE"

        engine.market.price = AsyncMock(return_value=Decimal("2"))
        await engine._handle_new_swap(_swap(Side.SELL, "future-sell", "2"), trader)
        assert notifier.executions[-1].success is True
        assert notifier.executions[-1].side is Side.SELL
        assert await engine.database.paper_mirror_positions() == []

        # Once the baseline lot has closed, later scans must never reopen it.
        await engine._seed_tracking_baselines(trader)
        assert await engine.database.paper_mirror_positions() == []
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_initial_bootstrap_inventory_is_seeded_before_future_sell(settings) -> None:
    baseline_settings = replace(
        settings,
        paper_seed_tracking_baselines=True,
        paper_force_observation_mode=False,
    )
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(baseline_settings, notifier=notifier)
    await engine.initialize()
    try:
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)
        now = int(time.time())
        engine.rpc.get_signatures_for_address = AsyncMock(
            return_value=[{"signature": "bootstrap-history-buy", "blockTime": now, "err": None}]
        )
        engine.rpc.get_transaction = AsyncMock(return_value={"blockTime": now})
        engine.detector.detect = AsyncMock(
            return_value=_swap(Side.BUY, "bootstrap-history-buy", "1")
        )
        engine.market.price = AsyncMock(return_value=Decimal("1.25"))

        counts = await engine._sync_trader(trader)

        assert counts == {"transactions": 1, "swaps": 1}
        assert len(await engine.database.paper_mirror_positions()) == 1
        assert notifier.executions[-1].success is True
        assert "Tracking baseline" in notifier.executions[-1].message
        refreshed = await engine.database.list_traders(enabled_only=False)
        tracked = next(item for item in refreshed if item.address == trader.address)
        assert tracked.last_signature == "bootstrap-history-buy"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_raw_paper_hard_stop_closes_source_linked_lot(settings) -> None:
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(settings, notifier=notifier)
    await engine.initialize()
    try:
        engine.market.price = AsyncMock(return_value=Decimal("1"))
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)
        await engine._handle_new_swap(_swap(Side.BUY, "buy-for-stop", "1"), trader)
        positions = await engine.database.paper_mirror_positions()

        await engine._check_raw_mirror_exits(
            positions,
            {"mint": Decimal("0.90")},
            int(time.time()),
        )

        assert await engine.database.paper_mirror_positions() == []
        assert notifier.executions[-1].success is True
        assert "hard stop" in notifier.executions[-1].message
        trades = await engine.database.paper_recent_trades()
        assert trades[0]["execution_kind"] == "RISK_EXIT"
        assert "hard stop" in trades[0]["exit_reason"]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_raw_paper_trailing_lock_protects_prior_gain(settings) -> None:
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(settings, notifier=notifier)
    await engine.initialize()
    try:
        engine.market.price = AsyncMock(return_value=Decimal("1"))
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)
        await engine._handle_new_swap(_swap(Side.BUY, "buy-for-trail", "1"), trader)

        positions = await engine.database.paper_mirror_positions()
        await engine._check_raw_mirror_exits(
            positions,
            {"mint": Decimal("1.15")},
            int(time.time()),
        )
        assert len(await engine.database.paper_mirror_positions()) == 1

        positions = await engine.database.paper_mirror_positions()
        await engine._check_raw_mirror_exits(
            positions,
            {"mint": Decimal("1.09")},
            int(time.time()),
        )

        assert await engine.database.paper_mirror_positions() == []
        assert "trailing-profit lock" in notifier.executions[-1].message
        summary = await engine.database.paper_summary({})
        assert summary.realized_pnl_usd > 0
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_scan_loop_closes_raw_lot_at_maximum_hold(settings) -> None:
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(settings, notifier=notifier)
    await engine.initialize()
    try:
        engine.market.price = AsyncMock(return_value=Decimal("1"))
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)
        await engine._handle_new_swap(_swap(Side.BUY, "buy-for-time", "1"), trader)
        await engine.database.db.execute(
            "UPDATE paper_mirror_positions SET opened_at = ?",
            (int(time.time()) - settings.raw_mirror_max_hold_seconds - 1,),
        )
        await engine.database.db.commit()
        engine.market.prices = AsyncMock(return_value={"mint": Decimal("1.02")})

        await engine._check_position_exits()

        assert await engine.database.paper_mirror_positions() == []
        assert "maximum raw hold time" in notifier.executions[-1].message
    finally:
        await engine.close()


def _quote(*, output: str, impact: str = "0.2", latency_ms: int = 100) -> SwapQuote:
    output_amount = Decimal(output)
    return SwapQuote(
        input_mint="base",
        output_mint="mint",
        input_amount_raw=10_000_000,
        output_amount_raw=int(output_amount * Decimal("1000000")),
        other_amount_threshold_raw=None,
        input_amount=Decimal("10"),
        output_amount=output_amount,
        input_usd_value=Decimal("10"),
        output_usd_value=Decimal("10"),
        price_impact_percent=Decimal(impact),
        router="metis",
        fee_bps=10,
        api_time_ms=50,
        observed_latency_ms=latency_ms,
        quoted_at=int(time.time()),
    )


@pytest.mark.asyncio
async def test_sniper_quote_lane_allows_moderate_drift_but_keeps_its_ceiling(
    settings,
) -> None:
    sniper = replace(
        settings,
        paper_sniper_test_enabled=True,
        paper_use_executable_quotes=True,
        jupiter_api_key="jup-test",
        live_base_mint="base",
    )
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(sniper, notifier=notifier)
    await engine.initialize()
    pump_mint = "A5G8K3qLzTKhmRL7nRTzG1f8eLTjSfY2uHEdpbt1pump"
    try:
        engine.market.price = AsyncMock(return_value=Decimal("1"))
        engine.market.token_info = AsyncMock(
            return_value=TokenInfo(
                mint=pump_mint,
                decimals=6,
                liquidity_usd=Decimal("5000"),
                holder_count=25,
                organic_score=Decimal("0"),
                mint_authority_disabled=True,
                freeze_authority_disabled=True,
            )
        )
        engine.market.quote_order = AsyncMock(
            side_effect=(_quote(output="1.8"), _quote(output="1.5"))
        )
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)

        await engine._handle_new_swap(
            _swap(Side.BUY, "sniper-quoted-buy", "1", mint=pump_mint), trader
        )
        assert notifier.executions[-1].success is True
        assert "Sniper quote-shadow BUY" in notifier.executions[-1].message

        await engine._handle_new_swap(
            _swap(
                Side.BUY,
                "sniper-quoted-too-late",
                "1",
                mint="B5G8K3qLzTKhmRL7nRTzG1f8eLTjSfY2uHEdpbt2pump",
            ),
            trader,
        )
        assert notifier.executions[-1].success is False
        assert "33.33%" in notifier.executions[-1].message
        trades = await engine.database.paper_recent_trades()
        assert len(trades) == 1
        assert trades[0]["execution_kind"] == "SNIPER_QUOTE"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_quote_shadow_blocks_25_percent_entry_chase(settings) -> None:
    quoted = replace(
        settings,
        paper_use_executable_quotes=True,
        jupiter_api_key="jup-test",
        live_base_mint="base",
    )
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(quoted, notifier=notifier)
    await engine.initialize()
    try:
        engine.market.price = AsyncMock(return_value=Decimal("1"))
        engine.market.token_info = AsyncMock(return_value=TokenInfo(mint="mint", decimals=6))
        engine.market.quote_order = AsyncMock(return_value=_quote(output="8"))
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)

        await engine._handle_new_swap(_swap(Side.BUY, "chased-buy", "1"), trader)

        assert notifier.executions[-1].success is False
        assert "entry drift +25.00%" in notifier.executions[-1].message
        assert await engine.database.paper_mirror_positions() == []
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_quote_shadow_records_buffered_buy_and_quoted_sell(settings) -> None:
    quoted = replace(
        settings,
        paper_use_executable_quotes=True,
        jupiter_api_key="jup-test",
        live_base_mint="base",
    )
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(quoted, notifier=notifier)
    await engine.initialize()
    try:
        engine.market.price = AsyncMock(return_value=Decimal("1"))
        engine.market.token_info = AsyncMock(return_value=TokenInfo(mint="mint", decimals=6))
        buy_quote = _quote(output="10")
        sell_quote = replace(
            _quote(output="12"),
            input_mint="mint",
            output_mint="base",
            input_amount=Decimal("9.95"),
            input_amount_raw=9_950_000,
            output_amount=Decimal("12"),
            output_amount_raw=12_000_000,
            output_usd_value=Decimal("12"),
        )
        engine.market.quote_order = AsyncMock(side_effect=(buy_quote, sell_quote))
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)

        await engine._handle_new_swap(_swap(Side.BUY, "quoted-buy", "1"), trader)
        positions = await engine.database.paper_mirror_positions()
        assert notifier.executions[-1].success is True
        assert len(positions) == 1
        assert float(positions[0]["paper_quantity"]) == pytest.approx(9.95)

        await engine._handle_new_swap(_swap(Side.SELL, "quoted-sell", "1.2"), trader)
        assert notifier.executions[-1].success is True
        assert await engine.database.paper_mirror_positions() == []
        trades = await engine.database.paper_recent_trades()
        assert trades[0]["quote_based"] == 1
        assert trades[0]["quote_router"] == "metis"
        assert Decimal(str(trades[0]["realized_pnl_usd"])) > 0
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_manual_quoted_exit_is_excluded_from_readiness(settings) -> None:
    quoted = replace(
        settings,
        paper_use_executable_quotes=True,
        jupiter_api_key="jup-test",
        live_base_mint="base",
    )
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(quoted, notifier=notifier)
    await engine.initialize()
    try:
        engine.market.price = AsyncMock(
            side_effect=(Decimal("1"), Decimal("1"), Decimal("1.2"), Decimal("1"))
        )
        engine.market.token_info = AsyncMock(return_value=TokenInfo(mint="mint", decimals=6))
        buy_quote = _quote(output="10")
        sell_quote = replace(
            _quote(output="12"),
            input_mint="mint",
            output_mint="base",
            input_amount=Decimal("9.95"),
            input_amount_raw=9_950_000,
            output_amount=Decimal("12"),
            output_amount_raw=12_000_000,
            output_usd_value=Decimal("12"),
        )
        engine.market.quote_order = AsyncMock(side_effect=(buy_quote, sell_quote))
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)

        await engine._handle_new_swap(_swap(Side.BUY, "quoted-manual-buy", "1"), trader)
        result = await engine.manual_paper_exit(
            position_kind="RAW_MIRROR",
            token_mint="mint",
            source_trader="wallet-a",
            requested_by="Discord admin",
        )

        assert result.success is True
        trades = await engine.database.paper_recent_trades()
        assert trades[0]["execution_kind"] == "MANUAL_EXIT"
        assert trades[0]["quote_based"] == 1
        readiness = await engine.paper_readiness()
        assert readiness.closed_trades == 0
        assert readiness.quote_attempts == 1
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_quote_shadow_blocks_high_entry_price_impact(settings) -> None:
    quoted = replace(
        settings,
        paper_use_executable_quotes=True,
        jupiter_api_key="jup-test",
        live_base_mint="base",
    )
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(quoted, notifier=notifier)
    await engine.initialize()
    try:
        engine.market.price = AsyncMock(return_value=Decimal("1"))
        engine.market.token_info = AsyncMock(return_value=TokenInfo(mint="mint", decimals=6))
        engine.market.quote_order = AsyncMock(return_value=_quote(output="10", impact="3"))
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)

        await engine._handle_new_swap(_swap(Side.BUY, "impact-buy", "1"), trader)

        assert notifier.executions[-1].success is False
        assert "price impact 3.00%" in notifier.executions[-1].message
    finally:
        await engine.close()
