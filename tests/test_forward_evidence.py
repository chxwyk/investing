from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from smart_money_bot.engine import SmartMoneyEngine
from smart_money_bot.models import DiscoveryCandidate


def candidate(address: str, *, score: str = "80") -> DiscoveryCandidate:
    return DiscoveryCandidate(
        address=address,
        alias=f"Wallet {address}",
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
        last_trade_ms=1_700_000_000_000,
        score=Decimal(score),
        rank=1,
        realized_pnl_7d=Decimal("2500"),
        roi_7d_percent=Decimal("35"),
        win_rate_7d_percent=Decimal("68"),
        trades_7d=80,
        selection_reason="dual-window public evidence",
    )


@pytest.mark.asyncio
async def test_forward_gate_does_not_reject_an_immature_sample(settings) -> None:
    engine = SmartMoneyEngine(settings)
    source = candidate("probation")
    engine.database.paper_wallet_performance = AsyncMock(
        return_value={
            source.address: {
                "closed_sells": 4,
                "pnl": Decimal("-50"),
                "profit_factor": Decimal("0.10"),
            }
        }
    )

    eligible, rejected, evaluated = await engine._apply_forward_paper_evidence([source])

    assert eligible == [source]
    assert rejected == {}
    assert evaluated == []


@pytest.mark.asyncio
async def test_forward_gate_rejects_mature_loser_and_rewards_mature_winner(
    settings,
) -> None:
    engine = SmartMoneyEngine(settings)
    loser = candidate("loser")
    winner = candidate("winner")
    engine.database.paper_wallet_performance = AsyncMock(
        return_value={
            loser.address: {
                "closed_sells": 5,
                "pnl": Decimal("-20"),
                "profit_factor": Decimal("0.50"),
            },
            winner.address: {
                "closed_sells": 5,
                "pnl": Decimal("12"),
                "profit_factor": Decimal("2.00"),
            },
        }
    )

    eligible, rejected, evaluated = await engine._apply_forward_paper_evidence(
        [loser, winner]
    )

    assert [item.address for item in eligible] == ["winner"]
    assert eligible[0].score == Decimal("82.00")
    assert "forward PAPER 5 exits" in eligible[0].selection_reason
    assert "breached -$10.00" in rejected["loser"]
    assert evaluated == [loser]
