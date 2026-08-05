from __future__ import annotations

from decimal import Decimal

from smart_money_bot.models import TraderMetrics
from smart_money_bot.scoring import score_trader


def _metrics(
    *,
    pnl: str,
    cost: str,
    wins: int,
    losses: int,
    trades: int,
    drawdown: str,
    window: int,
) -> TraderMetrics:
    return TraderMetrics(
        address="wallet",
        alias="Trader",
        window_seconds=window,
        trades=trades,
        buys=trades // 2,
        sells=trades - trades // 2,
        wins=wins,
        losses=losses,
        realized_pnl_usd=Decimal(pnl),
        matched_cost_usd=Decimal(cost),
        volume_usd=Decimal("10000"),
        max_drawdown_usd=Decimal(drawdown),
    )


def test_consistent_trader_beats_one_lucky_trade() -> None:
    consistent_24 = _metrics(
        pnl="300", cost="1500", wins=8, losses=2, trades=20, drawdown="40", window=86400
    )
    consistent_7d = _metrics(
        pnl="900", cost="6000", wins=25, losses=8, trades=70, drawdown="120", window=604800
    )
    lucky_24 = _metrics(
        pnl="1000", cost="100", wins=1, losses=0, trades=2, drawdown="0", window=86400
    )
    lucky_7d = _metrics(
        pnl="1000", cost="100", wins=1, losses=0, trades=2, drawdown="0", window=604800
    )
    assert score_trader(consistent_24, consistent_7d) > score_trader(lucky_24, lucky_7d)


def test_score_is_bounded() -> None:
    metrics = _metrics(
        pnl="100000", cost="1", wins=100, losses=0, trades=500, drawdown="0", window=86400
    )
    score = score_trader(metrics, metrics)
    assert Decimal("0") <= score <= Decimal("100")

