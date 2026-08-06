from __future__ import annotations

import time
from decimal import Decimal

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
