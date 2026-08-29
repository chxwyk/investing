"""Realistic complete round-trip cost model and expected NET edge (AH, AI).

Only NET PnL determines profitability here.  Every simulated result carries the
full breakdown — platform fees, network fees, priority fees, price impact and
slippage — and no mandatory legitimate fee is ever bypassed to make a backtest
look better.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .config import DEFAULT_LAB_CONFIG, LabConfig
from .decision import EvidenceQuality

ZERO = Decimal("0")
CENT = Decimal("0.000001")
BPS = Decimal("10000")


@dataclass(frozen=True, slots=True)
class RoundTripCost:
    """Every cost a simulated position must clear before it is profitable."""

    notional_usd: Decimal
    platform_fees_usd: Decimal = ZERO
    network_fees_usd: Decimal = ZERO
    priority_fees_usd: Decimal = ZERO
    price_impact_usd: Decimal = ZERO
    slippage_usd: Decimal = ZERO

    @property
    def total_cost_usd(self) -> Decimal:
        return (
            self.platform_fees_usd
            + self.network_fees_usd
            + self.priority_fees_usd
            + self.price_impact_usd
            + self.slippage_usd
        ).quantize(CENT)

    @property
    def total_cost_percent(self) -> Decimal:
        if self.notional_usd <= 0:
            return ZERO
        return (self.total_cost_usd / self.notional_usd * 100).quantize(Decimal("0.01"))

    def as_dict(self) -> dict[str, str]:
        return {
            "NOTIONAL": str(self.notional_usd),
            "PLATFORM_FEES": str(self.platform_fees_usd),
            "NETWORK_FEES": str(self.network_fees_usd),
            "PRIORITY_FEES": str(self.priority_fees_usd),
            "PRICE_IMPACT": str(self.price_impact_usd),
            "SLIPPAGE": str(self.slippage_usd),
            "TOTAL_COST": str(self.total_cost_usd),
            "TOTAL_COST_PERCENT": str(self.total_cost_percent),
        }


def estimate_round_trip_cost(
    notional_usd: Decimal,
    *,
    buy_price_impact_percent: Decimal | None = None,
    sell_price_impact_percent: Decimal | None = None,
    slippage_bps: int | None = None,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> RoundTripCost:
    """Both legs, always.  A position that cannot exit has no edge to model."""

    if notional_usd <= 0:
        return RoundTripCost(notional_usd=ZERO)

    platform_rate = Decimal(config.platform_fee_bps) / BPS
    platform = (notional_usd * platform_rate * 2).quantize(CENT)

    network = (config.network_fee_usd * 2).quantize(CENT)
    priority = (config.priority_fee_usd * 2).quantize(CENT)

    buy_impact = buy_price_impact_percent if buy_price_impact_percent is not None else ZERO
    sell_impact = (
        sell_price_impact_percent
        if sell_price_impact_percent is not None
        else buy_impact
    )
    impact = (notional_usd * (buy_impact + sell_impact) / 100).quantize(CENT)

    slip_bps = Decimal(slippage_bps if slippage_bps is not None else config.slippage_bps)
    slippage = (notional_usd * slip_bps / BPS * 2).quantize(CENT)

    return RoundTripCost(
        notional_usd=notional_usd,
        platform_fees_usd=platform,
        network_fees_usd=network,
        priority_fees_usd=priority,
        price_impact_usd=max(ZERO, impact),
        slippage_usd=slippage,
    )


@dataclass(frozen=True, slots=True)
class ExpectedEdge:
    """The pre-trade estimate that a decision must clear (section AI)."""

    gross_upside_percent: Decimal = ZERO
    downside_percent: Decimal = ZERO
    cost_percent: Decimal = ZERO
    net_edge_percent: Decimal = ZERO
    confidence: Decimal = ZERO
    quality: EvidenceQuality = EvidenceQuality.UNKNOWN
    cost: RoundTripCost | None = None

    @property
    def clears_cushion(self) -> bool:
        return self.cost_percent > 0 and self.gross_upside_percent >= self.cost_percent

    def meets(self, config: LabConfig) -> bool:
        """Require both an absolute floor and a multiple of realistic costs."""

        if self.quality is EvidenceQuality.UNKNOWN:
            return False
        if self.confidence < config.min_edge_confidence:
            return False
        if self.net_edge_percent < config.min_expected_net_edge_percent:
            return False
        cushion = self.cost_percent * config.edge_cushion_multiple
        return self.gross_upside_percent >= cushion


def estimate_expected_edge(
    *,
    notional_usd: Decimal,
    gross_upside_percent: Decimal | None,
    downside_percent: Decimal | None,
    buy_price_impact_percent: Decimal | None = None,
    sell_price_impact_percent: Decimal | None = None,
    slippage_bps: int | None = None,
    confidence: Decimal | None = None,
    quality: EvidenceQuality = EvidenceQuality.UNKNOWN,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> ExpectedEdge:
    """Estimate the net edge after every realistic cost.

    Unknown upside is not optimism — it produces a zero edge and an UNKNOWN
    quality, which the entry gate refuses.
    """

    cost = estimate_round_trip_cost(
        notional_usd,
        buy_price_impact_percent=buy_price_impact_percent,
        sell_price_impact_percent=sell_price_impact_percent,
        slippage_bps=slippage_bps,
        config=config,
    )
    if gross_upside_percent is None:
        return ExpectedEdge(
            cost_percent=cost.total_cost_percent,
            quality=EvidenceQuality.UNKNOWN,
            confidence=ZERO,
            cost=cost,
        )
    upside = max(ZERO, gross_upside_percent)
    downside = max(ZERO, downside_percent if downside_percent is not None else ZERO)
    net = (upside - cost.total_cost_percent).quantize(Decimal("0.01"))
    return ExpectedEdge(
        gross_upside_percent=upside.quantize(Decimal("0.01")),
        downside_percent=downside.quantize(Decimal("0.01")),
        cost_percent=cost.total_cost_percent,
        net_edge_percent=net,
        confidence=(confidence if confidence is not None else ZERO).quantize(Decimal("0.01")),
        quality=quality,
        cost=cost,
    )


@dataclass(frozen=True, slots=True)
class RealizedPnl:
    """The persisted breakdown for a completed or partial simulated exit."""

    gross_pnl_usd: Decimal = ZERO
    platform_fees_usd: Decimal = ZERO
    network_fees_usd: Decimal = ZERO
    priority_fees_usd: Decimal = ZERO
    price_impact_usd: Decimal = ZERO
    slippage_usd: Decimal = ZERO

    @property
    def total_cost_usd(self) -> Decimal:
        return (
            self.platform_fees_usd
            + self.network_fees_usd
            + self.priority_fees_usd
            + self.price_impact_usd
            + self.slippage_usd
        ).quantize(CENT)

    @property
    def net_pnl_usd(self) -> Decimal:
        return (self.gross_pnl_usd - self.total_cost_usd).quantize(CENT)

    def as_dict(self) -> dict[str, str]:
        return {
            "GROSS_PNL": str(self.gross_pnl_usd),
            "PLATFORM_FEES": str(self.platform_fees_usd),
            "NETWORK_FEES": str(self.network_fees_usd),
            "PRIORITY_FEES": str(self.priority_fees_usd),
            "PRICE_IMPACT": str(self.price_impact_usd),
            "SLIPPAGE": str(self.slippage_usd),
            "TOTAL_COST": str(self.total_cost_usd),
            "NET_PNL": str(self.net_pnl_usd),
        }

    def merged(self, other: RealizedPnl) -> RealizedPnl:
        return RealizedPnl(
            gross_pnl_usd=self.gross_pnl_usd + other.gross_pnl_usd,
            platform_fees_usd=self.platform_fees_usd + other.platform_fees_usd,
            network_fees_usd=self.network_fees_usd + other.network_fees_usd,
            priority_fees_usd=self.priority_fees_usd + other.priority_fees_usd,
            price_impact_usd=self.price_impact_usd + other.price_impact_usd,
            slippage_usd=self.slippage_usd + other.slippage_usd,
        )


def leg_costs(
    notional_usd: Decimal,
    *,
    price_impact_percent: Decimal | None = None,
    slippage_bps: int | None = None,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> RealizedPnl:
    """Costs for one leg (buy or sell) of a simulated trade."""

    if notional_usd <= 0:
        return RealizedPnl()
    platform = (notional_usd * Decimal(config.platform_fee_bps) / BPS).quantize(CENT)
    impact = (notional_usd * max(ZERO, price_impact_percent or ZERO) / 100).quantize(CENT)
    slip_bps = Decimal(slippage_bps if slippage_bps is not None else config.slippage_bps)
    slippage = (notional_usd * slip_bps / BPS).quantize(CENT)
    return RealizedPnl(
        platform_fees_usd=platform,
        network_fees_usd=config.network_fee_usd,
        priority_fees_usd=config.priority_fee_usd,
        price_impact_usd=impact,
        slippage_usd=slippage,
    )


def quote_deterioration_percent(
    decision_price: Decimal | None,
    fill_price: Decimal | None,
) -> Decimal | None:
    """How much worse the simulated fill was than the decision-time quote."""

    if not decision_price or decision_price <= 0 or fill_price is None:
        return None
    return ((fill_price - decision_price) / decision_price * 100).quantize(Decimal("0.01"))
