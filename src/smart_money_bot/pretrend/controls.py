"""Matched controls: the tokens that looked the same and did nothing.

A dataset of tokens that reached Trending, studied on its own, will confirm any
hypothesis you bring to it.  Every one of them had rising volume, some buyers, a
social account and a story, because *every token has those*.  The only question
that separates signal from decoration is: **how did the ones that trended differ
from the ones that looked the same and didn't?**

So every positive is matched against controls drawn from the same conditions.
Matching is on things that are true at the observation instant and independent
of the outcome:

* market-cap cohort — a $30K token and a $700K token are different games,
* token-age cohort — a 90-second-old token and a two-hour-old one are different,
* the same *time window* — this is the one that matters most, because it
  controls for market regime, Solana conditions, time of day and whatever was
  happening that afternoon.  A positive from a hot Tuesday compared against
  controls from a dead Sunday would "discover" that activity predicts trending.

Two further rules protect the split.

**A control must be genuinely eligible.**  It has to be a real prediction
opportunity at that instant — in-universe, not already on the board — or the
comparison is against tokens that were never in the running.

**A mint contributes to one side only.**  A mint that trends at some point is
never used as a control at any time, even from hours earlier when it "had not
trended yet".  Those rows are legitimately negative, but reusing a future
positive's earlier life as a control leaks the population structure across the
comparison and quietly shrinks the measured difference.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .cohorts import age_cohort, market_cap_cohort

#: How far from the positive's timestamp a control may be drawn.
DEFAULT_TIME_WINDOW_SECONDS = 3_600
#: How many controls per positive.  More controls sharpen the comparison but
#: skew the class balance; the model layer handles that with class weights.
DEFAULT_CONTROLS_PER_POSITIVE = 4


@dataclass(frozen=True, slots=True)
class CandidateRow:
    """The minimum needed to match a row, before any features are computed."""

    mint: str
    observed_at: int
    market_cap_usd: Decimal | None
    token_age_seconds: int | None
    eligible: bool
    #: ``None`` when this mint never entered Trending in our record.
    first_trending_at: int | None = None
    launch_source: str = ""
    liquidity_usd: Decimal | None = None

    @property
    def mc_cohort(self) -> str:
        return market_cap_cohort(self.market_cap_usd)

    @property
    def age_cohort(self) -> str:
        return age_cohort(self.token_age_seconds)

    @property
    def ever_trended(self) -> bool:
        return self.first_trending_at is not None


@dataclass(frozen=True, slots=True)
class MatchedPair:
    """One positive and the controls drawn to match it."""

    positive: CandidateRow
    controls: tuple[CandidateRow, ...]
    #: Which criteria the match actually satisfied, for auditing.
    matched_on: tuple[str, ...] = ()
    shortfall: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "positive_mint": self.positive.mint,
            "positive_at": self.positive.observed_at,
            "controls": [
                {"mint": row.mint, "at": row.observed_at} for row in self.controls
            ],
            "matched_on": list(self.matched_on),
            "shortfall": self.shortfall,
        }


@dataclass(frozen=True, slots=True)
class ControlSample:
    """The assembled comparison set, with an honest account of what it cost."""

    pairs: tuple[MatchedPair, ...] = ()
    positives: int = 0
    controls: int = 0
    unmatched_positives: tuple[str, ...] = ()
    #: Controls requested minus controls found, summed.  A large number means
    #: the comparison is thinner than it looks.
    total_shortfall: int = 0

    @property
    def controls_per_positive(self) -> Decimal | None:
        if self.positives <= 0:
            return None
        return (Decimal(self.controls) / Decimal(self.positives)).quantize(
            Decimal("0.01")
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "positives": self.positives,
            "controls": self.controls,
            "controls_per_positive": (
                None if self.controls_per_positive is None else str(self.controls_per_positive)
            ),
            "unmatched_positives": list(self.unmatched_positives),
            "total_shortfall": self.total_shortfall,
        }


def _stable_order(rows: Sequence[CandidateRow], seed: str) -> list[CandidateRow]:
    """Deterministic shuffle.

    Deterministic because a control set that changes between runs makes every
    measured difference unreproducible, and "it was better last time" is not a
    finding.  Seeded per positive so different positives do not all draw the
    same controls.
    """

    def key(row: CandidateRow) -> str:
        digest = hashlib.sha256(
            f"{seed}:{row.mint}:{row.observed_at}".encode()
        ).hexdigest()
        return digest

    return sorted(rows, key=key)


def build_matched_controls(
    rows: Sequence[CandidateRow],
    *,
    horizon_seconds: int,
    controls_per_positive: int = DEFAULT_CONTROLS_PER_POSITIVE,
    time_window_seconds: int = DEFAULT_TIME_WINDOW_SECONDS,
    require_age_cohort: bool = True,
) -> ControlSample:
    """Pair each positive with comparable eligible tokens that did not trend.

    Matching relaxes in a fixed order — market cap + age + time, then market cap
    + time, then time alone — and the criteria that actually applied are recorded
    on the pair.  A pair matched on time alone is a weaker comparison and the
    audit trail says so instead of hiding it.
    """

    eligible = [row for row in rows if row.eligible]
    positives = [
        row
        for row in eligible
        if row.first_trending_at is not None
        and row.observed_at < row.first_trending_at <= row.observed_at + horizon_seconds
    ]
    # A mint that ever trended is excluded from the control pool entirely.
    trended_mints = {row.mint for row in rows if row.ever_trended}
    control_pool = [row for row in eligible if row.mint not in trended_mints]

    pairs: list[MatchedPair] = []
    unmatched: list[str] = []
    used: set[tuple[str, int]] = set()
    total_shortfall = 0

    for positive in sorted(positives, key=lambda row: (row.observed_at, row.mint)):
        window = [
            row
            for row in control_pool
            if abs(row.observed_at - positive.observed_at) <= time_window_seconds
            and (row.mint, row.observed_at) not in used
        ]
        selected: list[CandidateRow] = []
        matched_on: tuple[str, ...] = ()

        tiers: list[tuple[tuple[str, ...], list[CandidateRow]]] = []
        strict = [
            row
            for row in window
            if row.mc_cohort == positive.mc_cohort
            and (not require_age_cohort or row.age_cohort == positive.age_cohort)
        ]
        tiers.append((("mc_cohort", "age_cohort", "time_window"), strict))
        loose = [row for row in window if row.mc_cohort == positive.mc_cohort]
        tiers.append((("mc_cohort", "time_window"), loose))
        tiers.append((("time_window",), window))

        for criteria, pool in tiers:
            if len(selected) >= controls_per_positive:
                break
            ordered = _stable_order(pool, seed=f"{positive.mint}:{positive.observed_at}")
            for row in ordered:
                if len(selected) >= controls_per_positive:
                    break
                key = (row.mint, row.observed_at)
                if key in used or any(
                    row.mint == chosen.mint and row.observed_at == chosen.observed_at
                    for chosen in selected
                ):
                    continue
                selected.append(row)
                used.add(key)
            if selected and not matched_on:
                matched_on = criteria

        if not selected:
            unmatched.append(positive.mint)
            continue
        shortfall = controls_per_positive - len(selected)
        total_shortfall += max(0, shortfall)
        pairs.append(
            MatchedPair(
                positive=positive,
                controls=tuple(selected),
                matched_on=matched_on,
                shortfall=max(0, shortfall),
            )
        )

    return ControlSample(
        pairs=tuple(pairs),
        positives=len(pairs),
        controls=sum(len(pair.controls) for pair in pairs),
        unmatched_positives=tuple(unmatched),
        total_shortfall=total_shortfall,
    )
