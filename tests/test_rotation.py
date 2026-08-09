from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from smart_money_bot.models import DetectedSwap, DiscoveryCandidate, Side
from smart_money_bot.rotation import (
    PUMP_PROGRAM_ID,
    CandidateRotator,
    is_pump_mint,
    is_pump_trade,
)
from smart_money_bot.stream import derive_ws_url


def candidate(address: str, *, last_trade_ms: int) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        address=address,
        alias=f"Wallet {address[:4]}",
        realized_pnl_24h=Decimal("500"),
        previous_pnl_24h=None,
        roi_24h_percent=Decimal("20"),
        win_rate_percent=Decimal("70"),
        trades_24h=20,
        buys_24h=10,
        sells_24h=10,
        closed_tokens=10,
        invested_24h_usd=Decimal("2000"),
        volume_24h_usd=Decimal("5000"),
        last_trade_ms=last_trade_ms,
        score=Decimal("80"),
        rank=1,
        realized_pnl_7d=Decimal("2500"),
        roi_7d_percent=Decimal("35"),
        win_rate_7d_percent=Decimal("68"),
        trades_7d=80,
    )


class FakeRPC:
    async def get_signatures_for_address(self, address: str, **kwargs):
        return [
            {"signature": f"sig-{address}", "blockTime": 1_000_000, "err": None}
        ]

    async def get_transaction(self, signature: str):
        mint = "Memecoin111111111111111111111111111111pump"
        if signature.endswith("nonpump"):
            mint = "OrdinaryToken111111111111111111111111111111"
        return {"blockTime": 1_000_000, "mint": mint}


class FakeDetector:
    async def detect(self, transaction, *, wallet, signature, block_time):
        return DetectedSwap(
            signature=signature,
            trader_address=wallet,
            block_time=block_time,
            side=Side.BUY,
            token_mint=transaction["mint"],
            token_amount=Decimal("100"),
            quote_mint="SOL",
            quote_amount=Decimal("1"),
            usd_value=Decimal("150"),
            token_price_usd=Decimal("1.5"),
        )


@pytest.mark.asyncio
async def test_rotation_selects_recent_pump_wallet_and_rejects_nonpump(settings) -> None:
    tuned = replace(
        settings,
        discovery_max_wallets=25,
        rotation_max_idle_seconds=3600,
        rotation_probe_transactions=6,
        rotation_min_recent_swaps=1,
        rotation_min_pump_swaps=1,
        rotation_require_pump_activity=True,
    )
    rotator = CandidateRotator(tuned, FakeRPC(), FakeDetector())

    result = await rotator.evaluate(
        [
            candidate("pump", last_trade_ms=1_000_000_000),
            candidate("nonpump", last_trade_ms=1_000_000_000),
        ],
        now=1_000_000,
    )

    assert [item.address for item in result.selected] == ["pump"]
    assert result.selected[0].pump_swaps == 1
    assert "not Pump-verified" in result.rejection_reasons["nonpump"]


@pytest.mark.asyncio
async def test_rotation_uses_rpc_activity_even_if_leaderboard_timestamp_is_stale(
    settings,
) -> None:
    tuned = replace(
        settings,
        rotation_max_idle_seconds=3600,
        rotation_probe_transactions=6,
        rotation_min_recent_swaps=1,
        rotation_min_pump_swaps=1,
        rotation_require_pump_activity=True,
    )
    rotator = CandidateRotator(tuned, FakeRPC(), FakeDetector())

    result = await rotator.evaluate(
        [candidate("pump", last_trade_ms=1)],
        now=1_000_000,
    )

    assert [item.address for item in result.selected] == ["pump"]


def test_pump_mint_and_websocket_url_detection() -> None:
    assert is_pump_mint("AbCdPump") is True
    assert is_pump_mint("AbCdElse") is False
    assert is_pump_trade(
        {
            "transaction": {
                "message": {"instructions": [{"programId": PUMP_PROGRAM_ID}]}
            }
        },
        "NoSuffix",
    )
    assert (
        derive_ws_url("https://mainnet.helius-rpc.com/?api-key=secret")
        == "wss://mainnet.helius-rpc.com/?api-key=secret"
    )
