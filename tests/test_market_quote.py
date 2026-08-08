from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from smart_money_bot.errors import JupiterError
from smart_money_bot.market import JupiterClient


@pytest.mark.asyncio
async def test_quote_order_normalizes_swap_v2_response() -> None:
    client = JupiterClient("jup-test")
    client._request = AsyncMock(
        return_value={
            "inputMint": "usdc",
            "outputMint": "mint",
            "inAmount": "10000000",
            "outAmount": "4000000",
            "otherAmountThreshold": "3980000",
            "inUsdValue": 10,
            "outUsdValue": 9.98,
            "priceImpact": -0.2,
            "router": "metis",
            "feeBps": 10,
            "totalTime": 42,
            "transaction": None,
        }
    )

    quote = await client.quote_order(
        input_mint="usdc",
        output_mint="mint",
        amount_raw=10_000_000,
        input_decimals=6,
        output_decimals=6,
    )

    assert quote.input_amount == Decimal("10")
    assert quote.output_amount == Decimal("4")
    assert quote.other_amount_threshold_raw == 3_980_000
    assert quote.price_impact_percent == Decimal("0.2")
    assert quote.router == "metis"
    assert quote.fee_bps == 10
    assert quote.api_time_ms == 42


@pytest.mark.asyncio
async def test_quote_order_requires_api_key() -> None:
    client = JupiterClient(None)
    with pytest.raises(JupiterError, match="JUPITER_API_KEY"):
        await client.quote_order(
            input_mint="usdc",
            output_mint="mint",
            amount_raw=1,
            input_decimals=6,
            output_decimals=6,
        )
