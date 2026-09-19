"""The prediction target: did this exact mint FIRST enter Trending within H?

One definition, applied identically everywhere:

    At observation time ``T``, ``TREND_H`` is true when the exact mint's
    ``first_trending_at`` falls in the half-open interval ``(T, T + H]``.

Every clause is load-bearing.

**First.**  The target is the first entry, not any entry.  Using "any entry"
would let a token that trended yesterday be labelled positive for an observation
made today purely because it came back — the model would then be learning "this
token trends sometimes", which it cannot act on.

**Strictly after T.**  A mint already on the board at ``T`` is not a prediction
opportunity, it is a fact.  Labelling it positive is the single most common way
a pre-trend backtest reports spectacular precision: the model learns to
recognise "already trending" and scores 95%.  :func:`eligible_at` refuses those
rows outright, and :func:`label_observation` refuses them again.

**Half-open at T, closed at T+H.**  An entry exactly at ``T`` belongs to the
past; an entry exactly at ``T + H`` is inside the horizon.  Stated so the two
boundaries are never resolved differently in training and in live scoring.

**Censoring is not a negative.**  An observation made ten minutes before our
data ends cannot have its 20-minute label determined — the token may enter the
board after our records stop.  Calling that a negative would teach the model
that the most recent, most relevant rows never trend.  Such rows are marked
``censored`` and excluded from training for that horizon, not silently zeroed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

#: The horizons every observation is labelled at (section 10).
LABEL_HORIZONS_SECONDS: tuple[int, ...] = (120, 300, 600, 1_200)

LABEL_NAMES: dict[int, str] = {
    120: "TREND_2M",
    300: "TREND_5M",
    600: "TREND_10M",
    1_200: "TREND_20M",
}

#: Why a row is not usable as a training example.
INELIGIBLE_ALREADY_TRENDING = "ALREADY_TRENDING"
INELIGIBLE_OUT_OF_UNIVERSE = "OUT_OF_UNIVERSE"
INELIGIBLE_NO_MARKET_CAP = "NO_MARKET_CAP"
INELIGIBLE_CENSORED = "CENSORED"


@dataclass(frozen=True, slots=True)
class Label:
    """One horizon's outcome for one observation."""

    horizon_seconds: int
    #: ``None`` when censored — the outcome is genuinely unknown, not negative.
    positive: bool | None
    censored: bool = False
    #: Seconds from the observation to the board entry, when it happened.
    lead_seconds: int | None = None

    @property
    def name(self) -> str:
        return LABEL_NAMES.get(self.horizon_seconds, f"TREND_{self.horizon_seconds}S")

    @property
    def usable(self) -> bool:
        return self.positive is not None and not self.censored

    def to_json(self) -> dict[str, Any]:
        return {
            "horizon_seconds": self.horizon_seconds,
            "name": self.name,
            "positive": self.positive,
            "censored": self.censored,
            "lead_seconds": self.lead_seconds,
        }


@dataclass(frozen=True, slots=True)
class LabelSet:
    """Every horizon's outcome for one observation, plus eligibility."""

    mint: str
    observed_at: int
    eligible: bool
    ineligible_reason: str = ""
    labels: dict[int, Label] = field(default_factory=dict)
    first_trending_at: int | None = None

    def label(self, horizon_seconds: int) -> Label | None:
        return self.labels.get(horizon_seconds)

    def positive_at(self, horizon_seconds: int) -> bool | None:
        found = self.labels.get(horizon_seconds)
        return None if found is None else found.positive

    @property
    def any_positive(self) -> bool:
        return any(label.positive for label in self.labels.values() if label.positive)

    def to_json(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "observed_at": self.observed_at,
            "eligible": self.eligible,
            "ineligible_reason": self.ineligible_reason,
            "first_trending_at": self.first_trending_at,
            "labels": {
                LABEL_NAMES.get(key, str(key)): value.to_json()
                for key, value in sorted(self.labels.items())
            },
        }


def eligible_at(
    *,
    observed_at: int,
    first_trending_at: int | None,
    market_cap_usd: Decimal | None,
    universe_min_usd: Decimal,
    universe_max_usd: Decimal,
) -> tuple[bool, str]:
    """Is this observation a genuine prediction opportunity?

    Returns ``(eligible, reason)``.  A mint already on the board at ``T`` is
    ineligible even if it later re-enters: there is nothing left to predict.
    """

    if first_trending_at is not None and first_trending_at <= observed_at:
        return (False, INELIGIBLE_ALREADY_TRENDING)
    if market_cap_usd is None:
        return (False, INELIGIBLE_NO_MARKET_CAP)
    if not (universe_min_usd <= market_cap_usd < universe_max_usd):
        return (False, INELIGIBLE_OUT_OF_UNIVERSE)
    return (True, "")


def label_observation(
    *,
    mint: str,
    observed_at: int,
    first_trending_at: int | None,
    market_cap_usd: Decimal | None,
    universe_min_usd: Decimal,
    universe_max_usd: Decimal,
    #: The last instant our board record is complete to.  Anything after this is
    #: unobserved, so horizons extending past it are censored.
    data_complete_until: int,
    horizons: Sequence[int] = LABEL_HORIZONS_SECONDS,
) -> LabelSet:
    """Label one point-in-time observation at every horizon."""

    eligible, reason = eligible_at(
        observed_at=observed_at,
        first_trending_at=first_trending_at,
        market_cap_usd=market_cap_usd,
        universe_min_usd=universe_min_usd,
        universe_max_usd=universe_max_usd,
    )
    if not eligible:
        return LabelSet(
            mint=mint,
            observed_at=observed_at,
            eligible=False,
            ineligible_reason=reason,
            first_trending_at=first_trending_at,
        )

    labels: dict[int, Label] = {}
    for horizon in horizons:
        horizon_end = observed_at + horizon
        entered_in_window = (
            first_trending_at is not None
            and observed_at < first_trending_at <= horizon_end
        )
        if entered_in_window:
            # A positive is determinable even when the horizon runs past the end
            # of our data: we already saw the entry.
            labels[horizon] = Label(
                horizon_seconds=horizon,
                positive=True,
                censored=False,
                lead_seconds=(first_trending_at or 0) - observed_at,
            )
            continue
        if horizon_end > data_complete_until:
            # We cannot know whether it entered after our record stops.
            labels[horizon] = Label(
                horizon_seconds=horizon, positive=None, censored=True
            )
            continue
        labels[horizon] = Label(
            horizon_seconds=horizon,
            positive=False,
            censored=False,
            lead_seconds=(
                None
                if first_trending_at is None
                else first_trending_at - observed_at
            ),
        )

    return LabelSet(
        mint=mint,
        observed_at=observed_at,
        eligible=True,
        labels=labels,
        first_trending_at=first_trending_at,
    )


@dataclass(frozen=True, slots=True)
class LabelStats:
    """The counts a human needs before believing any metric computed on them."""

    horizon_seconds: int
    total: int = 0
    positives: int = 0
    negatives: int = 0
    censored: int = 0
    ineligible: int = 0

    @property
    def usable(self) -> int:
        return self.positives + self.negatives

    @property
    def base_rate(self) -> Decimal | None:
        """P(enters Trending within H | eligible observation).  Section 46."""

        if self.usable <= 0:
            return None
        return (Decimal(self.positives) / Decimal(self.usable)).quantize(
            Decimal("0.000001")
        )

    @property
    def sufficient(self) -> bool:
        """Whether this is enough data to quote a precision figure at all."""

        return self.positives >= 30 and self.usable >= 500

    def to_json(self) -> dict[str, Any]:
        return {
            "horizon_seconds": self.horizon_seconds,
            "name": LABEL_NAMES.get(self.horizon_seconds, str(self.horizon_seconds)),
            "total": self.total,
            "positives": self.positives,
            "negatives": self.negatives,
            "censored": self.censored,
            "ineligible": self.ineligible,
            "usable": self.usable,
            "base_rate": None if self.base_rate is None else str(self.base_rate),
            "sufficient": self.sufficient,
        }


def summarise_labels(
    label_sets: Sequence[LabelSet], *, horizons: Sequence[int] = LABEL_HORIZONS_SECONDS
) -> dict[int, LabelStats]:
    """Per-horizon counts across a dataset.  Always reported next to any metric."""

    stats: dict[int, LabelStats] = {}
    for horizon in horizons:
        total = positives = negatives = censored = ineligible = 0
        for label_set in label_sets:
            total += 1
            if not label_set.eligible:
                ineligible += 1
                continue
            label = label_set.labels.get(horizon)
            if label is None:
                continue
            if label.censored:
                censored += 1
            elif label.positive:
                positives += 1
            else:
                negatives += 1
        stats[horizon] = LabelStats(
            horizon_seconds=horizon,
            total=total,
            positives=positives,
            negatives=negatives,
            censored=censored,
            ineligible=ineligible,
        )
    return stats
