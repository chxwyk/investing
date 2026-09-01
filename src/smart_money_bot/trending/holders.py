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


# --- the series, not the number (section 11) --------------------------------


@dataclass(frozen=True, slots=True)
class HolderSample:
    """One observation of the holder count, with the time it was taken."""

    at: int
    holder_count: int
    top10_percent: Decimal | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "at": self.at,
            "holder_count": self.holder_count,
            "top10_percent": _s(self.top10_percent),
        }


@dataclass(frozen=True, slots=True)
class HolderSeries:
    """``26 → 51 → 94`` and how long each step took.

    A single holder count is almost content-free: 26 holders is early promise on
    a two-minute-old token and a dead end on an hour-old one.  What a trader
    reads is the *shape* — and specifically whether the rate itself is rising,
    because steady growth and accelerating growth are different trades.
    """

    mint: str
    samples: tuple[HolderSample, ...] = ()

    @property
    def latest(self) -> HolderSample | None:
        return self.samples[-1] if self.samples else None

    @property
    def span_seconds(self) -> int:
        if len(self.samples) < 2:
            return 0
        return self.samples[-1].at - self.samples[0].at

    @property
    def added(self) -> int | None:
        if len(self.samples) < 2:
            return None
        return self.samples[-1].holder_count - self.samples[0].holder_count

    @property
    def per_minute(self) -> Decimal | None:
        added, span = self.added, self.span_seconds
        if added is None or span <= 0:
            return None
        return (Decimal(added) * Decimal(60) / Decimal(span)).quantize(Decimal("0.01"))

    @property
    def accelerating(self) -> bool | None:
        """Whether the most recent step grew faster than the one before it.

        Needs three samples: two give a rate, and it takes two rates to say
        anything about acceleration.  Fewer than three returns ``None`` rather
        than guessing, because an unknown trend must not read as a flat one.
        """

        if len(self.samples) < 3:
            return None
        recent = _rate(self.samples[-2], self.samples[-1])
        earlier = _rate(self.samples[-3], self.samples[-2])
        if recent is None or earlier is None:
            return None
        return recent > earlier

    def render(self, *, limit: int = 5) -> str:
        """``26 → 51 → 94`` — the tail of the series, most recent last."""

        tail = self.samples[-limit:]
        return " → ".join(str(item.holder_count) for item in tail)

    def record(self, sample: HolderSample, *, max_samples: int = 24) -> HolderSeries:
        """Append an observation.  Out-of-order samples are dropped, not sorted.

        A sample older than the last one we already have is a stale read racing a
        fresh one, and folding it in would invent a dip that never happened.
        """

        if self.samples and sample.at <= self.samples[-1].at:
            return self
        return HolderSeries(mint=self.mint, samples=(*self.samples, sample)[-max_samples:])

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "samples": [item.to_json() for item in self.samples],
            "added": self.added,
            "span_seconds": self.span_seconds,
            "per_minute": _s(self.per_minute),
            "accelerating": self.accelerating,
            "render": self.render(),
        }


def _rate(first: HolderSample, second: HolderSample) -> Decimal | None:
    span = second.at - first.at
    if span <= 0:
        return None
    return Decimal(second.holder_count - first.holder_count) * Decimal(60) / Decimal(span)


def series_from_json(payload: dict[str, object]) -> HolderSeries:
    """Rebuild a series from its persisted form, order preserved."""

    raw = payload.get("samples") or ()
    samples: list[HolderSample] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        top10 = item.get("top10_percent")
        samples.append(
            HolderSample(
                at=int(item.get("at") or 0),
                holder_count=int(item.get("holder_count") or 0),
                top10_percent=None if top10 in (None, "") else Decimal(str(top10)),
            )
        )
    return HolderSeries(mint=str(payload.get("mint") or ""), samples=tuple(samples))
