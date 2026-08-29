"""Bounded market-regime classification (section AO).

A hostile regime makes the lab smaller and more selective.  It never makes the
lab less safe: safety gates are untouched by regime, by construction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")

RISK_ON = "RISK_ON"
NORMAL = "NORMAL"
CHOP = "CHOP"
RISK_OFF = "RISK_OFF"
LIQUIDITY_STRESS = "LIQUIDITY_STRESS"
UNKNOWN = "UNKNOWN"

REGIMES: tuple[str, ...] = (RISK_ON, NORMAL, CHOP, RISK_OFF, LIQUIDITY_STRESS, UNKNOWN)

#: Position-size multipliers per regime.  Weak regimes size down; none of them
#: ever size above the normal position.
REGIME_SIZE_MULTIPLIER: dict[str, Decimal] = {
    RISK_ON: Decimal("1"),
    NORMAL: Decimal("1"),
    CHOP: Decimal("0.7"),
    RISK_OFF: Decimal("0.5"),
    LIQUIDITY_STRESS: Decimal("0.4"),
    UNKNOWN: Decimal("0.6"),
}


@dataclass(frozen=True, slots=True)
class RegimeSample:
    """One recently observed token used to characterise the current market."""

    mint: str
    observed_at: int
    liquidity_usd: Decimal | None = None
    forward_return_percent: Decimal | None = None
    max_favourable_percent: Decimal | None = None
    max_adverse_percent: Decimal | None = None
    route_available: bool = True
    rugged: bool = False
    graduated: bool = False


@dataclass(frozen=True, slots=True)
class MarketRegime:
    state: str = UNKNOWN
    samples: int = 0
    median_liquidity_usd: Decimal | None = None
    median_forward_return_percent: Decimal | None = None
    sustained_move_percent: Decimal | None = None
    quick_dump_percent: Decimal | None = None
    route_health_percent: Decimal | None = None
    graduation_rate_percent: Decimal | None = None
    notes: tuple[str, ...] = ()

    @property
    def size_multiplier(self) -> Decimal:
        return REGIME_SIZE_MULTIPLIER.get(self.state, Decimal("0.6"))

    @property
    def is_hostile(self) -> bool:
        return self.state in {RISK_OFF, LIQUIDITY_STRESS}


def classify_regime(samples: Sequence[RegimeSample], *, min_samples: int = 12) -> MarketRegime:
    """Classify the current regime; too small a sample stays UNKNOWN."""

    if len(samples) < min_samples:
        return MarketRegime(
            state=UNKNOWN,
            samples=len(samples),
            notes=("Not enough recent observations to classify the regime",),
        )

    liquidity = sorted(
        item.liquidity_usd for item in samples if item.liquidity_usd is not None
    )
    forwards = sorted(
        item.forward_return_percent
        for item in samples
        if item.forward_return_percent is not None
    )
    peaks = [
        item.max_favourable_percent
        for item in samples
        if item.max_favourable_percent is not None
    ]
    troughs = [
        item.max_adverse_percent for item in samples if item.max_adverse_percent is not None
    ]

    sustained = _rate(sum(1 for value in peaks if value >= 50), len(peaks))
    dumped = _rate(sum(1 for value in troughs if value >= 50), len(troughs))
    routes = _rate(sum(1 for item in samples if item.route_available), len(samples))
    graduated = _rate(sum(1 for item in samples if item.graduated), len(samples))
    median_liquidity = _median(liquidity)
    median_forward = _median(forwards)

    notes: list[str] = []
    state = NORMAL
    if routes is not None and routes < 70:
        state = LIQUIDITY_STRESS
        notes.append("Route availability is degraded across recent candidates")
    elif median_liquidity is not None and median_liquidity < Decimal("8000"):
        state = LIQUIDITY_STRESS
        notes.append("Median new-token liquidity is very thin")
    elif dumped is not None and dumped >= 60:
        state = RISK_OFF
        notes.append("Most recent candidates dumped hard")
    elif median_forward is not None and median_forward <= Decimal("-15"):
        state = RISK_OFF
        notes.append("Median forward return is clearly negative")
    elif sustained is not None and sustained >= 35 and (median_forward or ZERO) > 0:
        state = RISK_ON
        notes.append("A healthy share of candidates sustained their move")
    elif sustained is not None and sustained < 15:
        state = CHOP
        notes.append("Few candidates sustain a move")

    return MarketRegime(
        state=state,
        samples=len(samples),
        median_liquidity_usd=median_liquidity,
        median_forward_return_percent=median_forward,
        sustained_move_percent=sustained,
        quick_dump_percent=dumped,
        route_health_percent=routes,
        graduation_rate_percent=graduated,
        notes=tuple(notes),
    )


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return ((ordered[middle - 1] + ordered[middle]) / 2).quantize(Decimal("0.01"))


def _rate(count: int, total: int) -> Decimal | None:
    if total <= 0:
        return None
    return (Decimal(count) / Decimal(total) * 100).quantize(Decimal("0.01"))
