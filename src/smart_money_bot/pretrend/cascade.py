"""Which signal moved FIRST — measured, never assumed.

The tempting story is that a board entry is the end of a cascade: a sharp trader
buys, a thesis appears, independent buyers follow, on-chain volume lifts, holders
grow, social amplifies, price runs, and Trending notices.  It is a plausible
story.  It is also exactly the kind of story that, if hardcoded, will make the
system confidently early on the tokens that happen to fit it and blind on
everything else.

So this module hardcodes no order.  It records, for each signal family, the
first instant that signal measurably accelerated, and then reports the observed
ordering and the lead each signal had on the board entry.  Over enough entries
those leads become a distribution, and the distribution answers the question the
operator actually asked: *is FOMO-native attention early, or is it late?*

If a family's median lead is negative — it typically accelerates **after** the
board entry — then it is a confirmation signal, not a prediction signal, and
section 58 requires us to say so out loud rather than keep it in the model
because it correlates.  :func:`summarise_leads` produces exactly that verdict.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .windows import Series, first_acceleration_at

ZERO = Decimal("0")

# --- the signal families whose ordering we measure (section 22) --------------
SIGNAL_FOMO_QUALITY_TRADERS = "FOMO_QUALITY_TRADERS"
SIGNAL_FOMO_UNIQUE_BUYERS = "FOMO_UNIQUE_BUYERS"
SIGNAL_FOMO_BUY_NOTIONAL = "FOMO_BUY_NOTIONAL"
SIGNAL_FOMO_THESES = "FOMO_THESES"
SIGNAL_ONCHAIN_UNIQUE_BUYERS = "ONCHAIN_UNIQUE_BUYERS"
SIGNAL_ONCHAIN_VOLUME = "ONCHAIN_VOLUME"
SIGNAL_HOLDERS = "HOLDERS"
SIGNAL_GMGN_DISCOVERY = "GMGN_DISCOVERY"
SIGNAL_DEX_ACTIVITY = "DEX_ACTIVITY"
SIGNAL_SOCIAL = "SOCIAL"
SIGNAL_MARKET_CAP = "MARKET_CAP"
SIGNAL_PRICE = "PRICE"
SIGNAL_LIQUIDITY = "LIQUIDITY"

SIGNAL_FAMILIES: tuple[str, ...] = (
    SIGNAL_FOMO_QUALITY_TRADERS,
    SIGNAL_FOMO_UNIQUE_BUYERS,
    SIGNAL_FOMO_BUY_NOTIONAL,
    SIGNAL_FOMO_THESES,
    SIGNAL_ONCHAIN_UNIQUE_BUYERS,
    SIGNAL_ONCHAIN_VOLUME,
    SIGNAL_HOLDERS,
    SIGNAL_GMGN_DISCOVERY,
    SIGNAL_DEX_ACTIVITY,
    SIGNAL_SOCIAL,
    SIGNAL_MARKET_CAP,
    SIGNAL_PRICE,
    SIGNAL_LIQUIDITY,
)

#: How each family's series should be summarised when scanning for its first
#: acceleration.  Counters count events; sums add notional; levels track stocks.
SIGNAL_KINDS: dict[str, str] = {
    SIGNAL_FOMO_QUALITY_TRADERS: "counter",
    SIGNAL_FOMO_UNIQUE_BUYERS: "counter",
    SIGNAL_FOMO_BUY_NOTIONAL: "sum",
    SIGNAL_FOMO_THESES: "counter",
    SIGNAL_ONCHAIN_UNIQUE_BUYERS: "level",
    SIGNAL_ONCHAIN_VOLUME: "level",
    SIGNAL_HOLDERS: "level",
    SIGNAL_GMGN_DISCOVERY: "counter",
    SIGNAL_DEX_ACTIVITY: "counter",
    SIGNAL_SOCIAL: "level",
    SIGNAL_MARKET_CAP: "level",
    SIGNAL_PRICE: "level",
    SIGNAL_LIQUIDITY: "level",
}


@dataclass(frozen=True, slots=True)
class SignalOnset:
    """When one signal family first measurably accelerated for one mint."""

    family: str
    mint: str
    #: ``None`` means we never observed an acceleration — not that there was none.
    first_acceleration_at: int | None = None
    samples: int = 0
    reason: str = ""

    @property
    def observed(self) -> bool:
        return self.first_acceleration_at is not None

    def lead_seconds(self, reference_at: int) -> int | None:
        """Positive when this signal led the reference event."""

        if self.first_acceleration_at is None:
            return None
        return reference_at - self.first_acceleration_at

    def to_json(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "mint": self.mint,
            "first_acceleration_at": self.first_acceleration_at,
            "samples": self.samples,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CascadeProfile:
    """The observed ordering of signal onsets for one mint, plus shape features."""

    mint: str
    reference_at: int
    onsets: dict[str, SignalOnset] = field(default_factory=dict)
    #: Families whose onset we observed, earliest first.
    order: tuple[str, ...] = ()
    cascade_depth: int = 0
    cascade_width: int = 0
    independent_participants: int = 0
    quality_weighted_participants: Decimal | None = None
    seconds_between_stages: tuple[int, ...] = ()
    fomo_buy_notional_growth: Decimal | None = None
    thesis_growth: Decimal | None = None
    onchain_conversion_rate: Decimal | None = None
    social_conversion_rate: Decimal | None = None

    def lead(self, family: str) -> int | None:
        onset = self.onsets.get(family)
        return None if onset is None else onset.lead_seconds(self.reference_at)

    @property
    def first_mover(self) -> str | None:
        return self.order[0] if self.order else None

    def to_json(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "mint": self.mint,
            "reference_at": self.reference_at,
            "order": list(self.order),
            "first_mover": self.first_mover,
            "cascade_depth": self.cascade_depth,
            "cascade_width": self.cascade_width,
            "independent_participants": self.independent_participants,
            "quality_weighted_participants": s(self.quality_weighted_participants),
            "seconds_between_stages": list(self.seconds_between_stages),
            "fomo_buy_notional_growth": s(self.fomo_buy_notional_growth),
            "thesis_growth": s(self.thesis_growth),
            "onchain_conversion_rate": s(self.onchain_conversion_rate),
            "social_conversion_rate": s(self.social_conversion_rate),
            "leads": {
                family: onset.lead_seconds(self.reference_at)
                for family, onset in sorted(self.onsets.items())
            },
            "onsets": {
                family: onset.to_json() for family, onset in sorted(self.onsets.items())
            },
        }


def detect_onsets(
    mint: str,
    series_by_family: Mapping[str, Series],
    *,
    until: int,
    window_seconds: int = 60,
    ratio_threshold: Decimal = Decimal("2"),
) -> dict[str, SignalOnset]:
    """Find each family's first acceleration, bounded at ``until``.

    ``until`` is normally the board-entry timestamp.  Bounding the scan there is
    what stops this from becoming a hindsight feature: we are reconstructing
    what a family did *before* the event, not finding its all-time peak.
    """

    onsets: dict[str, SignalOnset] = {}
    for family, series in series_by_family.items():
        kind = SIGNAL_KINDS.get(family, "counter")
        if len(series) == 0:
            onsets[family] = SignalOnset(
                family=family, mint=mint, samples=0, reason="no samples collected"
            )
            continue
        at = first_acceleration_at(
            series,
            until=until,
            window_seconds=window_seconds,
            ratio_threshold=ratio_threshold,
            kind=kind,
        )
        onsets[family] = SignalOnset(
            family=family,
            mint=mint,
            first_acceleration_at=at,
            samples=len(series),
            reason="" if at is not None else "no acceleration observed before the reference",
        )
    return onsets


def build_cascade(
    mint: str,
    series_by_family: Mapping[str, Series],
    *,
    reference_at: int,
    window_seconds: int = 60,
    independent_participants: int = 0,
    quality_weighted_participants: Decimal | None = None,
    onchain_buyers: int | None = None,
    fomo_buyers: int | None = None,
    social_accounts: int | None = None,
) -> CascadeProfile:
    """Assemble the observed cascade for one mint at one reference instant."""

    onsets = detect_onsets(
        mint, series_by_family, until=reference_at, window_seconds=window_seconds
    )
    observed = sorted(
        (onset for onset in onsets.values() if onset.observed),
        key=lambda onset: (onset.first_acceleration_at or 0, onset.family),
    )
    order = tuple(onset.family for onset in observed)
    gaps = tuple(
        (observed[index].first_acceleration_at or 0)
        - (observed[index - 1].first_acceleration_at or 0)
        for index in range(1, len(observed))
    )

    notional = series_by_family.get(SIGNAL_FOMO_BUY_NOTIONAL)
    theses = series_by_family.get(SIGNAL_FOMO_THESES)

    def growth(series: Series | None, *, kind: str) -> Decimal | None:
        if series is None or len(series) == 0:
            return None
        from .windows import counter_dynamics, sum_dynamics

        fn = sum_dynamics if kind == "sum" else counter_dynamics
        return fn(series, end=reference_at, seconds=window_seconds).ratio_to_prior

    conversion: Decimal | None = None
    if fomo_buyers and onchain_buyers is not None and fomo_buyers > 0:
        conversion = (Decimal(onchain_buyers) / Decimal(fomo_buyers)).quantize(
            Decimal("0.0001")
        )
    social_conversion: Decimal | None = None
    if social_accounts and fomo_buyers is not None and social_accounts > 0:
        social_conversion = (Decimal(fomo_buyers) / Decimal(social_accounts)).quantize(
            Decimal("0.0001")
        )

    return CascadeProfile(
        mint=mint,
        reference_at=reference_at,
        onsets=onsets,
        order=order,
        cascade_depth=len(order),
        cascade_width=len([family for family in order if family.startswith("FOMO_")]),
        independent_participants=independent_participants,
        quality_weighted_participants=quality_weighted_participants,
        seconds_between_stages=gaps,
        fomo_buy_notional_growth=growth(notional, kind="sum"),
        thesis_growth=growth(theses, kind="counter"),
        onchain_conversion_rate=conversion,
        social_conversion_rate=social_conversion,
    )


@dataclass(frozen=True, slots=True)
class LeadSummary:
    """The distribution of one family's lead over the board entry, across tokens."""

    family: str
    samples: int
    median_lead_seconds: int | None
    p25_lead_seconds: int | None
    p75_lead_seconds: int | None
    #: Share of tokens where this family accelerated BEFORE the board entry.
    leads_share: Decimal | None
    #: Tokens where the family never accelerated before the entry at all.
    missing: int = 0

    @property
    def verdict(self) -> str:
        """A plain answer to "is this signal early, or is it late?" (section 58)."""

        if self.samples < 10:
            return "INSUFFICIENT_SAMPLE"
        if self.median_lead_seconds is None:
            return "UNKNOWN"
        if self.median_lead_seconds > 60:
            return "LEADS"
        if self.median_lead_seconds > 0:
            return "MARGINALLY_LEADS"
        return "LAGS"

    def to_json(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "samples": self.samples,
            "missing": self.missing,
            "median_lead_seconds": self.median_lead_seconds,
            "p25_lead_seconds": self.p25_lead_seconds,
            "p75_lead_seconds": self.p75_lead_seconds,
            "leads_share": None if self.leads_share is None else str(self.leads_share),
            "verdict": self.verdict,
        }


def _quantile_int(values: Sequence[int], fraction: Decimal) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = int(
        (fraction * Decimal(len(ordered) - 1)).to_integral_value(rounding="ROUND_HALF_UP")
    )
    return ordered[max(0, min(index, len(ordered) - 1))]


def summarise_leads(profiles: Sequence[CascadeProfile]) -> dict[str, LeadSummary]:
    """Aggregate lead times per family across many board entries.

    This is the function that is allowed to say "the FOMO tape is late".  It has
    no preference about the answer.
    """

    by_family: dict[str, list[int]] = {}
    missing: dict[str, int] = {}
    for profile in profiles:
        for family in SIGNAL_FAMILIES:
            onset = profile.onsets.get(family)
            lead = None if onset is None else onset.lead_seconds(profile.reference_at)
            if lead is None:
                missing[family] = missing.get(family, 0) + 1
            else:
                by_family.setdefault(family, []).append(lead)

    summaries: dict[str, LeadSummary] = {}
    for family in SIGNAL_FAMILIES:
        leads = by_family.get(family, [])
        ahead = len([lead for lead in leads if lead > 0])
        summaries[family] = LeadSummary(
            family=family,
            samples=len(leads),
            median_lead_seconds=_quantile_int(leads, Decimal("0.5")),
            p25_lead_seconds=_quantile_int(leads, Decimal("0.25")),
            p75_lead_seconds=_quantile_int(leads, Decimal("0.75")),
            leads_share=(
                None
                if not leads
                else (Decimal(ahead) / Decimal(len(leads))).quantize(Decimal("0.0001"))
            ),
            missing=missing.get(family, 0),
        )
    return summaries
