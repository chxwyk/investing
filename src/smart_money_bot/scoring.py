from __future__ import annotations

from decimal import Decimal

from .models import ScoredTrader, TraderMetrics


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def score_trader(metrics_24h: TraderMetrics, metrics_7d: TraderMetrics) -> Decimal:
    """Return a transparent 0-100 risk-adjusted score.

    The formula deliberately rewards repeatable closed trades and penalizes drawdown.
    Raw profit alone cannot dominate the ranking.
    """

    closed = metrics_24h.wins + metrics_24h.losses
    activity = _clamp(Decimal(metrics_24h.trades) / Decimal(12), Decimal("0"), Decimal("1"))
    closure_reliability = _clamp(Decimal(closed) / Decimal(6), Decimal("0"), Decimal("1"))
    win_component = metrics_24h.win_rate * Decimal("30")

    # 25% realized ROI fills this component; losses can reduce it to zero.
    roi_scaled = _clamp(
        metrics_24h.realized_roi / Decimal("0.25"), Decimal("-1"), Decimal("1")
    )
    roi_component = max(Decimal("0"), roi_scaled) * Decimal("25")

    consistency_component = Decimal("0")
    if metrics_24h.realized_pnl_usd > 0:
        consistency_component += Decimal("8")
    if metrics_7d.realized_pnl_usd > 0:
        consistency_component += Decimal("10")
    if metrics_24h.realized_pnl_usd > 0 and metrics_7d.realized_pnl_usd > 0:
        consistency_component += Decimal("7")

    pnl_scale = abs(metrics_7d.realized_pnl_usd) + Decimal("10")
    drawdown_ratio = _clamp(
        metrics_7d.max_drawdown_usd / pnl_scale, Decimal("0"), Decimal("1")
    )
    drawdown_component = (Decimal("1") - drawdown_ratio) * Decimal("10")
    activity_component = activity * Decimal("10")

    raw = (
        win_component
        + roi_component
        + consistency_component
        + drawdown_component
        + activity_component
    )
    reliability = Decimal("0.35") + (closure_reliability * Decimal("0.65"))
    return _clamp(raw * reliability, Decimal("0"), Decimal("100")).quantize(
        Decimal("0.01")
    )


def rank_traders(
    metrics_24h: list[TraderMetrics], metrics_7d: list[TraderMetrics]
) -> list[ScoredTrader]:
    weekly = {item.address: item for item in metrics_7d}
    scored = [
        ScoredTrader(
            metrics_24h=item,
            metrics_7d=weekly.get(item.address, item),
            score=score_trader(item, weekly.get(item.address, item)),
        )
        for item in metrics_24h
    ]
    return sorted(
        scored,
        key=lambda item: (
            item.score,
            item.metrics_24h.realized_pnl_usd,
            item.metrics_24h.trades,
        ),
        reverse=True,
    )

