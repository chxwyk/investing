from __future__ import annotations

import time
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from smart_money_bot.engine import SmartMoneyEngine
from smart_money_bot.models import (
    DetectedSwap,
    DiscoveryRefresh,
    ExecutionResult,
    RiskDecision,
    Side,
    Signal,
    TokenInfo,
    TrackedTrader,
)


class CaptureNotifier:
    def __init__(self) -> None:
        self.swaps: list[DetectedSwap] = []
        self.signals: list[Signal] = []
        self.executions: list[ExecutionResult] = []

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

    async def on_error(self, context: str, error: Exception) -> None:
        raise AssertionError(f"{context}: {error}")


def _swap(side: Side, signature: str, price: str) -> DetectedSwap:
    return DetectedSwap(
        signature=signature,
        trader_address="wallet-a",
        block_time=int(time.time()),
        side=side,
        token_mint="mint",
        token_amount=Decimal("100"),
        quote_mint="quote",
        quote_amount=Decimal("100"),
        usd_value=Decimal("100"),
        token_price_usd=Decimal(price),
    )


@pytest.mark.asyncio
async def test_new_raw_swaps_immediately_buy_and_sell_in_paper_mode(settings) -> None:
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(settings, notifier=notifier)
    await engine.initialize()
    try:
        engine.market.price = AsyncMock(
            side_effect=(Decimal("1"), Decimal("1.2"))
        )
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
async def test_raw_paper_entry_filter_blocks_suspicious_token(settings) -> None:
    notifier = CaptureNotifier()
    engine = SmartMoneyEngine(settings, notifier=notifier)
    await engine.initialize()
    try:
        engine.market.price = AsyncMock(return_value=Decimal("1"))
        engine.market.token_info = AsyncMock(
            return_value=TokenInfo(mint="mint", suspicious=True)
        )
        trader = TrackedTrader(address="wallet-a", alias="Auto wallet-a")
        await engine.database.add_trader(trader.address, trader.alias)

        await engine._handle_new_swap(_swap(Side.BUY, "unsafe-buy", "1"), trader)

        assert notifier.executions[-1].success is False
        assert "suspicious" in notifier.executions[-1].message
        assert await engine.database.paper_mirror_positions() == []
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
