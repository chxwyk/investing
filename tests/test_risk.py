from __future__ import annotations

import time
from decimal import Decimal

import pytest

from smart_money_bot.database import Database
from smart_money_bot.models import ExecutionMode, Side, Signal, TokenInfo
from smart_money_bot.risk import RiskEngine


def _signal() -> Signal:
    return Signal(
        token_mint="mint",
        side=Side.BUY,
        created_at=int(time.time()),
        trader_addresses=("a", "b"),
        trader_aliases=("A", "B"),
        source_signatures=("one", "two"),
        combined_score=Decimal("70"),
        reference_price_usd=Decimal("1"),
    )


@pytest.mark.asyncio
async def test_blocks_low_liquidity_live_token(settings) -> None:
    database = Database(settings.database_path, settings.paper_starting_usd)
    await database.connect()
    try:
        risk = RiskEngine(settings, database)
        info = TokenInfo(
            mint="mint",
            decimals=6,
            liquidity_usd=Decimal("1000"),
            holder_count=1000,
            organic_score=Decimal("80"),
            mint_authority_disabled=True,
            freeze_authority_disabled=True,
        )
        decision = await risk.assess(
            signal=_signal(),
            mode=ExecutionMode.LIVE,
            token_info=info,
            market_price_usd=Decimal("1"),
        )
        assert decision.allowed is False
        assert any("Liquidity" in reason for reason in decision.reasons)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_allows_healthy_paper_signal(settings) -> None:
    database = Database(settings.database_path, settings.paper_starting_usd)
    await database.connect()
    try:
        risk = RiskEngine(settings, database)
        info = TokenInfo(
            mint="mint",
            decimals=6,
            liquidity_usd=Decimal("500000"),
            holder_count=5000,
            organic_score=Decimal("85"),
            mint_authority_disabled=True,
            freeze_authority_disabled=True,
            top_holders_percent=Decimal("20"),
        )
        decision = await risk.assess(
            signal=_signal(),
            mode=ExecutionMode.PAPER,
            token_info=info,
            market_price_usd=Decimal("1"),
        )
        assert decision.allowed is True
    finally:
        await database.close()
