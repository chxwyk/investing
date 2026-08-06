from __future__ import annotations

from decimal import Decimal

import pytest
from smart_money_bot.constants import WRAPPED_SOL_MINT
from smart_money_bot.detector import SwapDetector
from smart_money_bot.models import Side

WALLET = "7hER9xFakeWalletForUnitTesting111111111111111111"
TOKEN = "TokenMintForUnitTesting11111111111111111111111"


class FakeMarket:
    async def price(self, mint: str) -> Decimal | None:
        return Decimal("100") if mint == WRAPPED_SOL_MINT else Decimal("1")


def _token_balance(mint: str, owner: str, amount: int, decimals: int) -> dict:
    return {
        "mint": mint,
        "owner": owner,
        "uiTokenAmount": {"amount": str(amount), "decimals": decimals},
    }


def _tx(
    *,
    pre_lamports: int,
    post_lamports: int,
    pre_tokens: int,
    post_tokens: int,
    fee: int = 5000,
) -> dict:
    return {
        "transaction": {"message": {"accountKeys": [{"pubkey": WALLET}]}},
        "meta": {
            "err": None,
            "fee": fee,
            "preBalances": [pre_lamports],
            "postBalances": [post_lamports],
            "preTokenBalances": [_token_balance(TOKEN, WALLET, pre_tokens, 6)],
            "postTokenBalances": [_token_balance(TOKEN, WALLET, post_tokens, 6)],
        },
    }


@pytest.mark.asyncio
async def test_detects_sol_buy() -> None:
    detector = SwapDetector(FakeMarket(), Decimal("10"))  # type: ignore[arg-type]
    transaction = _tx(
        pre_lamports=10_000_000_000,
        post_lamports=8_999_995_000,
        pre_tokens=0,
        post_tokens=100_000_000,
    )
    swap = await detector.detect(
        transaction, wallet=WALLET, signature="sig-buy", block_time=1_700_000_000
    )
    assert swap is not None
    assert swap.side is Side.BUY
    assert swap.token_amount == Decimal("100")
    assert swap.quote_amount == Decimal("1")
    assert swap.usd_value == Decimal("100")
    assert swap.token_price_usd == Decimal("1")


@pytest.mark.asyncio
async def test_detects_sol_sell() -> None:
    detector = SwapDetector(FakeMarket(), Decimal("10"))  # type: ignore[arg-type]
    transaction = _tx(
        pre_lamports=9_000_000_000,
        post_lamports=9_499_995_000,
        pre_tokens=100_000_000,
        post_tokens=50_000_000,
    )
    swap = await detector.detect(
        transaction, wallet=WALLET, signature="sig-sell", block_time=1_700_000_001
    )
    assert swap is not None
    assert swap.side is Side.SELL
    assert swap.token_amount == Decimal("50")
    assert swap.quote_amount == Decimal("0.5")
    assert swap.usd_value == Decimal("50.0")


@pytest.mark.asyncio
async def test_ignores_plain_token_transfer() -> None:
    detector = SwapDetector(FakeMarket(), Decimal("10"))  # type: ignore[arg-type]
    transaction = _tx(
        pre_lamports=10_000_000_000,
        post_lamports=9_999_995_000,
        pre_tokens=0,
        post_tokens=10_000_000,
    )
    swap = await detector.detect(
        transaction, wallet=WALLET, signature="sig-transfer", block_time=1_700_000_002
    )
    assert swap is None

