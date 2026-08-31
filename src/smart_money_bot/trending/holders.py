"""Holder analysis: participant growth beats transaction count.

The distinction this module exists to enforce (section 36): a thousand
transactions from ten wallets is wash activity wearing volume's clothes, while
five hundred *new independent holders* is genuine distribution of ownership.
Both look like "activity" on a chart; only one of them is demand.

Concentration is tracked as a trend rather than a level, because "top 10 hold
40%" is meaningless without knowing whether it was 25% ten minutes ago (getting
worse) or 60% (getting better).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")
HUNDRED = Decimal("100")

CONCENTRATION_IMPROVING = "IMPROVING"
CONCENTRATION_STABLE = "STABLE"
CONCENTRATION_WORSENING = "WORSENING"
CONCENTRATION_UNKNOWN = "UNKNOWN"

GROWTH_ACCELERATING = "ACCELERATING"
GROWTH_GROWING = "GROWING"
GROWTH_FLAT = "FLAT"
GROWTH_SHRINKING = "SHRINKING"
GROWTH_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class HolderProfile:
    """What the holder base is doing, stated only as far as the data allows."""

    mint: str
    holder_count: int | None = None
    first_holder_count: int | None = None
    holders_added: int | None = None
    holders_per_minute: Decimal | None = None
    growth_state: str = GROWTH_UNKNOWN
    top10_percent: Decimal | None = None
    top10_change: Decimal | None = None
    concentration_trend: str = CONCENTRATION_UNKNOWN
    #: Independent buyers seen in the window, when the market data supplies them.
    independent_buyers: int | None = None
    #: Buy transactions in the same window, for the ratio below.
    buys: int | None = None

    @property
    def participant_quality(self) -> Decimal | None:
        """Independent buyers per buy transaction.  Near 1.0 is healthy."""

        if self.independent_buyers is None or not self.buys:
            return None
        return (Decimal(self.independent_buyers) / Decimal(self.buys)).quantize(Decimal("0.01"))

    @property
    def genuinely_expanding(self) -> bool:
        """Growth that is real participants, not repeat trades from few wallets."""

        if self.growth_state not in {GROWTH_GROWING, GROWTH_ACCELERATING}:
            return False
        return self.concentration_trend != CONCENTRATION_WORSENING

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "holder_count": self.holder_count,
            "first_holder_count": self.first_holder_count,
            "holders_added": self.holders_added,
            "holders_per_minute": _s(self.holders_per_minute),
            "growth_state": self.growth_state,
            "top10_percent": _s(self.top10_percent),
            "top10_change": _s(self.top10_change),
            "concentration_trend": self.concentration_trend,
            "independent_buyers": self.independent_buyers,
            "buys": self.buys,
            "participant_quality": _s(self.participant_quality),
            "genuinely_expanding": self.genuinely_expanding,
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def assess_holders(
    mint: str,
    *,
    holder_count: int | None,
    first_holder_count: int | None,
    seconds_elapsed: int,
    top10_percent: Decimal | None = None,
    first_top10_percent: Decimal | None = None,
    independent_buyers: int | None = None,
    buys: int | None = None,
    accelerating_per_minute: Decimal = Decimal("3"),
    growing_per_minute: Decimal = Decimal("0.5"),
    concentration_tolerance: Decimal = Decimal("2"),
) -> HolderProfile:
    """Grade holder growth and concentration without inventing missing numbers."""

    added: int | None = None
    per_minute: Decimal | None = None
    growth = GROWTH_UNKNOWN
    if holder_count is not None and first_holder_count is not None:
        added = holder_count - first_holder_count
        if seconds_elapsed > 0:
            per_minute = (
                Decimal(added) * Decimal(60) / Decimal(seconds_elapsed)
            ).quantize(Decimal("0.01"))
            if per_minute >= accelerating_per_minute:
                growth = GROWTH_ACCELERATING
            elif per_minute >= growing_per_minute:
                growth = GROWTH_GROWING
            elif per_minute <= -growing_per_minute:
                growth = GROWTH_SHRINKING
            else:
                growth = GROWTH_FLAT
        else:
            growth = GROWTH_GROWING if added > 0 else GROWTH_FLAT

    change: Decimal | None = None
    trend = CONCENTRATION_UNKNOWN
    if top10_percent is not None and first_top10_percent is not None:
        change = top10_percent - first_top10_percent
        if change <= -concentration_tolerance:
            trend = CONCENTRATION_IMPROVING
        elif change >= concentration_tolerance:
            trend = CONCENTRATION_WORSENING
        else:
            trend = CONCENTRATION_STABLE

    return HolderProfile(
        mint=mint,
        holder_count=holder_count,
        first_holder_count=first_holder_count,
        holders_added=added,
        holders_per_minute=per_minute,
        growth_state=growth,
        top10_percent=top10_percent,
        top10_change=change,
        concentration_trend=trend,
        independent_buyers=independent_buyers,
        buys=buys,
    )
