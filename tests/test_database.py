from __future__ import annotations

import time
from decimal import Decimal

import pytest

from smart_money_bot.database import Database
from smart_money_bot.models import Side, Signal


@pytest.mark.asyncio
async def test_paper_round_trip_includes_costs(tmp_path) -> None:
    database = Database(str(tmp_path / "paper.db"), Decimal("1000"))
    await database.connect()
    try:
        signal = Signal(
            token_mint="mint",
            side=Side.BUY,
            created_at=int(time.time()),
            trader_addresses=("a", "b"),
            trader_aliases=("A", "B"),
            source_signatures=("one", "two"),
            combined_score=Decimal("75"),
            reference_price_usd=Decimal("1"),
        )
        signal_id = await database.record_signal(signal)
        buy = await database.paper_execute(
            signal_id=signal_id,
            token_mint="mint",
            side=Side.BUY,
            market_price_usd=Decimal("1"),
            size_usd=Decimal("100"),
            fee_bps=50,
            slippage_bps=100,
        )
        assert buy is not None

        sell = await database.paper_execute(
            signal_id=signal_id,
            token_mint="mint",
            side=Side.SELL,
            market_price_usd=Decimal("1"),
            size_usd=Decimal("100"),
            fee_bps=50,
            slippage_bps=100,
        )
        assert sell is not None
        assert sell["realized_pnl"] < 0

        summary = await database.paper_summary({})
        assert summary.equity_usd < Decimal("1000")
        assert summary.realized_pnl_usd < 0
    finally:
        await database.close()

