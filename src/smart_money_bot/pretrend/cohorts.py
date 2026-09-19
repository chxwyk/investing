"""Cohorts and relative attention: 40 buyers is a different fact at $30K and $900K.

Two related problems live here.

**Absolute counts do not transfer.**  Forty new buyers in a minute is
extraordinary for a four-minute-old $30K token and unremarkable for a $900K one
that has been trading for an hour.  A model trained on absolute counts will
therefore learn "big tokens trend", which is true, useless, and already priced
in.  So every attention metric is additionally expressed as a **percentile
against the currently active population** and against the token's own market-cap
and age cohort.

**Cohorts have different base rates.**  A 20-50K token and a 500K-1M token do
not enter Trending at the same rate, so a single global baseline would
systematically over-claim lift in one cohort and under-claim it in the other.
Base rates are therefore computed per cohort and carried with every claim.

The percentile is computed against a snapshot of the population *at that
instant*.  Ranking today's token against a population that includes tomorrow's
tokens would be leakage as surely as reading tomorrow's price.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

ZERO = Decimal("0")
HUNDRED = Decimal("100")

# --- market-cap cohorts (section 37) ----------------------------------------
#: The research universe floor and ceiling.  Both are configurable; these are
#: the defaults the operator specified.
DEFAULT_UNIVERSE_MIN_USD = Decimal("20000")
DEFAULT_UNIVERSE_MAX_USD = Decimal("1000000")

MC_COHORT_BOUNDS: tuple[tuple[str, Decimal, Decimal], ...] = (
    ("MC_20K_50K", Decimal("20000"), Decimal("50000")),
    ("MC_50K_100K", Decimal("50000"), Decimal("100000")),
    ("MC_100K_250K", Decimal("100000"), Decimal("250000")),
    ("MC_250K_500K", Decimal("250000"), Decimal("500000")),
    ("MC_500K_1M", Decimal("500000"), Decimal("1000000")),
)

MC_COHORT_BELOW = "MC_BELOW_UNIVERSE"
MC_COHORT_ABOVE = "MC_ABOVE_UNIVERSE"
MC_COHORT_UNKNOWN = "MC_UNKNOWN"

MC_COHORTS: tuple[str, ...] = tuple(name for name, _, _ in MC_COHORT_BOUNDS) + (
    MC_COHORT_BELOW,
    MC_COHORT_ABOVE,
    MC_COHORT_UNKNOWN,
)

# --- token-age cohorts (section 38) -----------------------------------------
AGE_COHORT_BOUNDS: tuple[tuple[str, int, int], ...] = (
    ("AGE_LT_2M", 0, 120),
    ("AGE_2_5M", 120, 300),
    ("AGE_5_15M", 300, 900),
    ("AGE_15_30M", 900, 1_800),
    ("AGE_30_60M", 1_800, 3_600),
    ("AGE_1H_PLUS", 3_600, 2**31),
)

AGE_COHORT_UNKNOWN = "AGE_UNKNOWN"

AGE_COHORTS: tuple[str, ...] = tuple(name for name, _, _ in AGE_COHORT_BOUNDS) + (
    AGE_COHORT_UNKNOWN,
)


def market_cap_cohort(
    market_cap_usd: Decimal | None,
    *,
    minimum: Decimal = DEFAULT_UNIVERSE_MIN_USD,
    maximum: Decimal = DEFAULT_UNIVERSE_MAX_USD,
) -> str:
    """Which market-cap cohort a token is in.  Unknown stays unknown."""

    if market_cap_usd is None:
        return MC_COHORT_UNKNOWN
    if market_cap_usd < minimum:
        return MC_COHORT_BELOW
    if market_cap_usd >= maximum:
        return MC_COHORT_ABOVE
    for name, low, high in MC_COHORT_BOUNDS:
        if low <= market_cap_usd < high:
            return name
    return MC_COHORT_UNKNOWN


def age_cohort(token_age_seconds: int | None) -> str:
    """Which age cohort a token is in.  A missing age is not assumed young."""

    if token_age_seconds is None or token_age_seconds < 0:
        return AGE_COHORT_UNKNOWN
    for name, low, high in AGE_COHORT_BOUNDS:
        if low <= token_age_seconds < high:
            return name
    return AGE_COHORT_UNKNOWN


def in_universe(
    market_cap_usd: Decimal | None,
    *,
    minimum: Decimal = DEFAULT_UNIVERSE_MIN_USD,
    maximum: Decimal = DEFAULT_UNIVERSE_MAX_USD,
) -> bool:
    """Whether a token is inside the research universe.

    A token with an unknown market cap is **not** in the universe.  Admitting it
    would silently populate the dataset with rows whose cohort, baseline and
    percentile are all undefined.
    """

    if market_cap_usd is None:
        return False
    return minimum <= market_cap_usd < maximum


@dataclass(frozen=True, slots=True)
class PopulationSnapshot:
    """Every active token's value for one metric at one instant.

    Built from observations at or before ``at`` only.  This class is where the
    "relative" in relative attention actually happens, so its time bound is part
    of the leakage perimeter.
    """

    metric: str
    at: int
    values: Mapping[str, Decimal] = field(default_factory=dict)
    #: Optional cohort label per mint, for cohort-relative percentiles.
    cohorts: Mapping[str, str] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.values)

    def percentile(self, mint: str) -> Decimal | None:
        """The token's percentile in the whole active population, 0-100.

        Uses the midrank convention, so a token tied with others sits in the
        middle of the tie rather than at its top — the optimistic convention
        would inflate every tied token's apparent standing.
        """

        value = self.values.get(mint)
        if value is None or self.size < 2:
            return None
        below = sum(1 for other in self.values.values() if other < value)
        equal = sum(1 for other in self.values.values() if other == value)
        rank = Decimal(below) + Decimal(equal - 1) / 2
        return (rank / Decimal(self.size - 1) * HUNDRED).quantize(Decimal("0.01"))

    def cohort_percentile(self, mint: str) -> Decimal | None:
        """The token's percentile within its own cohort."""

        cohort = self.cohorts.get(mint)
        if cohort is None:
            return None
        peers = {
            other: value
            for other, value in self.values.items()
            if self.cohorts.get(other) == cohort
        }
        if len(peers) < 2:
            return None
        value = peers.get(mint)
        if value is None:
            return None
        below = sum(1 for other in peers.values() if other < value)
        equal = sum(1 for other in peers.values() if other == value)
        rank = Decimal(below) + Decimal(equal - 1) / 2
        return (rank / Decimal(len(peers) - 1) * HUNDRED).quantize(Decimal("0.01"))

    def share(self, mint: str) -> Decimal | None:
        """This token's share of the population total for the metric."""

        value = self.values.get(mint)
        if value is None:
            return None
        total = sum(self.values.values(), ZERO)
        if total <= ZERO:
            return None
        return (value / total).quantize(Decimal("0.000001"))


@dataclass(frozen=True, slots=True)
class RelativeAttention:
    """The section-21 block for one mint at one instant."""

    mint: str
    at: int
    population_size: int = 0
    #: metric -> percentile against all active tokens
    percentiles: dict[str, Decimal | None] = field(default_factory=dict)
    #: metric -> percentile against the token's own market-cap cohort
    cohort_percentiles: dict[str, Decimal | None] = field(default_factory=dict)
    #: metric -> this token's share of the population total
    shares: dict[str, Decimal | None] = field(default_factory=dict)
    mc_cohort: str = MC_COHORT_UNKNOWN
    age_cohort: str = AGE_COHORT_UNKNOWN

    def to_json(self) -> dict[str, Any]:
        def block(values: dict[str, Decimal | None]) -> dict[str, str | None]:
            return {
                key: (None if value is None else str(value))
                for key, value in sorted(values.items())
            }

        return {
            "mint": self.mint,
            "at": self.at,
            "population_size": self.population_size,
            "mc_cohort": self.mc_cohort,
            "age_cohort": self.age_cohort,
            "percentiles": block(self.percentiles),
            "cohort_percentiles": block(self.cohort_percentiles),
            "shares": block(self.shares),
        }


def build_relative_attention(
    mint: str,
    snapshots: Sequence[PopulationSnapshot],
    *,
    at: int,
    mc_cohort: str = MC_COHORT_UNKNOWN,
    token_age_cohort: str = AGE_COHORT_UNKNOWN,
) -> RelativeAttention:
    """Percentiles and shares for one mint across several metric populations."""

    percentiles: dict[str, Decimal | None] = {}
    cohort_percentiles: dict[str, Decimal | None] = {}
    shares: dict[str, Decimal | None] = {}
    size = 0
    for snapshot in snapshots:
        percentiles[snapshot.metric] = snapshot.percentile(mint)
        cohort_percentiles[snapshot.metric] = snapshot.cohort_percentile(mint)
        shares[snapshot.metric] = snapshot.share(mint)
        size = max(size, snapshot.size)
    return RelativeAttention(
        mint=mint,
        at=at,
        population_size=size,
        percentiles=percentiles,
        cohort_percentiles=cohort_percentiles,
        shares=shares,
        mc_cohort=mc_cohort,
        age_cohort=token_age_cohort,
    )


@dataclass(frozen=True, slots=True)
class CohortBaseline:
    """The base rate for one cohort, with the sample it rests on."""

    cohort: str
    horizon_seconds: int
    observations: int
    positives: int

    @property
    def rate(self) -> Decimal | None:
        if self.observations <= 0:
            return None
        return (Decimal(self.positives) / Decimal(self.observations)).quantize(
            Decimal("0.000001")
        )

    @property
    def sufficient(self) -> bool:
        """Whether this cohort has enough rows for its base rate to mean anything."""

        return self.observations >= 100 and self.positives >= 5

    def to_json(self) -> dict[str, Any]:
        return {
            "cohort": self.cohort,
            "horizon_seconds": self.horizon_seconds,
            "observations": self.observations,
            "positives": self.positives,
            "rate": None if self.rate is None else str(self.rate),
            "sufficient": self.sufficient,
        }


def cohort_baselines(
    rows: Sequence[tuple[str, bool]], *, horizon_seconds: int
) -> dict[str, CohortBaseline]:
    """Base rate per cohort from ``(cohort, was_positive)`` pairs."""

    totals: dict[str, int] = {}
    positives: dict[str, int] = {}
    for cohort, positive in rows:
        totals[cohort] = totals.get(cohort, 0) + 1
        if positive:
            positives[cohort] = positives.get(cohort, 0) + 1
    return {
        cohort: CohortBaseline(
            cohort=cohort,
            horizon_seconds=horizon_seconds,
            observations=total,
            positives=positives.get(cohort, 0),
        )
        for cohort, total in sorted(totals.items())
    }
