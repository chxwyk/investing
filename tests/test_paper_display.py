from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from smart_money_bot.bot import PaperPositionsView, PaperTradesView, SmartMoneyBot, _return_percent
from smart_money_bot.models import Side


def test_return_percent_handles_profit_loss_and_zero_basis() -> None:
    assert _return_percent(Decimal("130"), Decimal("100")) == Decimal("30.0")
    assert _return_percent(Decimal("88"), Decimal("100")) == Decimal("-12.00")
    assert _return_percent(Decimal("10"), Decimal("0")) == Decimal("0")


@pytest.mark.asyncio
async def test_paper_views_paginate_without_posting_long_text(settings) -> None:
    bot = SmartMoneyBot(settings)
    await bot.engine.initialize()
    try:
        await bot.engine.database.add_trader("wallet-a", "Wallet A")
        await bot.engine.database.paper_mirror_execute(
            trader_address="wallet-a",
            source_signature="page-buy",
            token_mint="mint-a",
            side=Side.BUY,
            source_token_amount=Decimal("100"),
            market_price_usd=Decimal("1"),
            size_usd=Decimal("10"),
            fee_bps=0,
            slippage_bps=0,
        )
        bot.engine.market.prices = AsyncMock(
            return_value={"mint-a": Decimal("1.25")}
        )

        positions = await PaperPositionsView.create(bot, 123, can_sell=True)
        trades = await PaperTradesView.create(bot, 123, page_size=5)

        assert len(positions.positions) == 1
        assert positions.page_button.label == "1/1"
        assert positions.sell_button.disabled is False
        assert "Unrealized P&L" in {field.name for field in positions.embed().fields}
        assert trades.total == 1
        assert trades.page_button.label == "1/1"
        assert len(trades.embed().fields) == 1
    finally:
        await bot.engine.close()
