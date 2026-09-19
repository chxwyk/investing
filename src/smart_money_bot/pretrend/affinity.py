"""PRE-TREND AFFINITY: how often does this account show up before a board entry?

The question is narrow and falsifiable: of the tokens this FOMO account (or
on-chain wallet) was observed entering, what fraction subsequently entered FOMO
Trending inside a horizon?  That is it.  This is **not** an insider score, and
the module refuses to call it one — everything measured here is public
behaviour, and a public account being repeatedly early is evidence of taste,
speed, or a shared information source, none of which we can distinguish and none
of which we will allege (sections 17, 24).

The hard part is not the ratio, it is not fooling ourselves with it.

**Three of three is not a 100% hit rate.**  It is three observations.  With a
base rate of 1%, a random account reaching 3/3 is unlikely but a population of
50,000 accounts produces such accounts by the dozen every day, and ranking by
raw precision surfaces exactly those accounts — pure selection bias, dressed as
alpha.  So the headline number is a **shrunk** estimate: a Beta-Binomial
posterior whose prior is the population base rate with a configurable strength.
An account with three observations barely moves off the base rate; an account
with two hundred is allowed to.

**Lift, not precision.**  A 4% hit rate means nothing without knowing that the
base rate is 0.8%.  Every result carries the baseline it was measured against
and the lift over it.

**A confidence interval, always.**  The Wilson interval is reported next to the
point estimate so a thin sample looks thin on the card instead of looking
confident.

**Recent and lifetime are separate.**  Markets change (section 48).  An account
that was early for a month and has been late for a week is not the same asset as
one that is early now, so both windows are carried and neither is blended away.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

ZERO = Decimal("0")
ONE = Decimal("1")

#: The horizons every affinity record is scored over (section 17).
AFFINITY_HORIZONS_SECONDS: tuple[int, ...] = (120, 300, 600, 1_200)

HORIZON_LABELS: dict[int, str] = {120: "2m", 300: "5m", 600: "10m", 1_200: "20m"}

#: Below this many observations an account is reported but never ranked as
#: statistically meaningful.  It is a display rule, not a filter: hiding thin
#: accounts would make the population look stronger than it is.
DEFAULT_MIN_SAMPLE = 8


def _sqrt(value: Decimal) -> Decimal:
    if value <= ZERO:
        return ZERO
    return value.sqrt()


@dataclass(frozen=True, slots=True)
class AffinityObservation:
    """One time this account was seen entering one exact mint."""

    actor_id: str
    mint: str
    observed_at: int
    #: ``None`` means the mint never entered Trending in our record — which is
    #: the *common* case and is a negative, not missing data.
    trend_entered_at: int | None = None
    market_cap_at_observation_usd: Decimal | None = None
    market_cap_at_entry_usd: Decimal | None = None
    token_age_seconds: int | None = None
    #: Where the observation came from (``fomo_activity``, ``onchain`` …).
    surface: str = ""

    @property
    def lead_seconds(self) -> int | None:
        """Seconds between the observation and the board entry, if it happened."""

        if self.trend_entered_at is None:
            return None
        return self.trend_entered_at - self.observed_at

    def hit(self, horizon_seconds: int) -> bool:
        """Did the mint FIRST enter Trending within ``horizon_seconds`` of this?

        A *negative* lead time is not a hit.  The mint was already on the board
        when this account acted, so the account followed the board rather than
        preceding it — counting that as a hit is the single easiest way to
        manufacture a spectacular and entirely fake affinity score.
        """

        lead = self.lead_seconds
        return lead is not None and 0 <= lead <= horizon_seconds


@dataclass(frozen=True, slots=True)
class HorizonResult:
    """The affinity result for one horizon, with everything needed to judge it."""

    horizon_seconds: int
    observations: int
    hits: int
    #: hits / observations.  Reported, never ranked on.
    raw_rate: Decimal | None
    #: Beta-Binomial posterior mean, shrunk toward the baseline.
    adjusted_rate: Decimal | None
    baseline: Decimal
    #: adjusted_rate / baseline.  The number that actually means something.
    lift: Decimal | None
    ci_low: Decimal | None
    ci_high: Decimal | None

    @property
    def label(self) -> str:
        return HORIZON_LABELS.get(self.horizon_seconds, f"{self.horizon_seconds}s")

    def to_json(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "horizon_seconds": self.horizon_seconds,
            "horizon": self.label,
            "observations": self.observations,
            "hits": self.hits,
            "raw_rate": s(self.raw_rate),
            "adjusted_rate": s(self.adjusted_rate),
            "baseline": str(self.baseline),
            "lift": s(self.lift),
            "ci_low": s(self.ci_low),
            "ci_high": s(self.ci_high),
        }


@dataclass(frozen=True, slots=True)
class AffinityRecord:
    """One actor's full pre-trend record.  Never called an insider score."""

    actor_id: str
    surface: str = ""
    handle: str = ""
    observations: int = 0
    horizons: dict[int, HorizonResult] = field(default_factory=dict)
    median_lead_seconds: int | None = None
    median_entry_market_cap_usd: Decimal | None = None
    median_token_age_seconds: int | None = None
    first_observed_at: int | None = None
    last_observed_at: int | None = None
    #: Same computation restricted to a recent window (section 48).
    recent_horizons: dict[int, HorizonResult] = field(default_factory=dict)
    recent_observations: int = 0
    min_sample: int = DEFAULT_MIN_SAMPLE

    @property
    def statistically_meaningful(self) -> bool:
        """Whether this record has enough observations to rank on at all."""

        return self.observations >= self.min_sample

    def primary(self, horizon_seconds: int = 300) -> HorizonResult | None:
        return self.horizons.get(horizon_seconds)

    def rank_key(self, horizon_seconds: int = 300) -> Decimal:
        """Sort key: the shrunk rate, zero when the sample is too thin.

        Thin records sort last by construction, which is the whole point.  A
        raw-rate sort would put every 2/2 account above every 40/200 account.
        """

        if not self.statistically_meaningful:
            return ZERO
        result = self.horizons.get(horizon_seconds)
        if result is None or result.adjusted_rate is None:
            return ZERO
        return result.adjusted_rate

    def to_json(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "surface": self.surface,
            "handle": self.handle,
            "observations": self.observations,
            "recent_observations": self.recent_observations,
            "statistically_meaningful": self.statistically_meaningful,
            "min_sample": self.min_sample,
            "median_lead_seconds": self.median_lead_seconds,
            "median_entry_market_cap_usd": (
                None
                if self.median_entry_market_cap_usd is None
                else str(self.median_entry_market_cap_usd)
            ),
            "median_token_age_seconds": self.median_token_age_seconds,
            "first_observed_at": self.first_observed_at,
            "last_observed_at": self.last_observed_at,
            "horizons": {
                HORIZON_LABELS.get(key, str(key)): value.to_json()
                for key, value in sorted(self.horizons.items())
            },
            "recent_horizons": {
                HORIZON_LABELS.get(key, str(key)): value.to_json()
                for key, value in sorted(self.recent_horizons.items())
            },
        }


def shrunk_rate(
    *,
    hits: int,
    observations: int,
    baseline: Decimal,
    prior_strength: Decimal = Decimal("40"),
) -> Decimal | None:
    """Beta-Binomial posterior mean with the population base rate as the prior.

    ``prior_strength`` is the number of pseudo-observations the prior is worth.
    At the default of 40, an account needs roughly 40 real observations before
    its own record outweighs the population's — which is about where a rate
    estimate stops being dominated by luck at these base rates.
    """

    if observations <= 0:
        return None
    alpha = baseline * prior_strength
    beta = (ONE - baseline) * prior_strength
    return ((Decimal(hits) + alpha) / (Decimal(observations) + alpha + beta)).quantize(
        Decimal("0.000001")
    )


def wilson_interval(
    *, hits: int, observations: int, z: Decimal = Decimal("1.96")
) -> tuple[Decimal, Decimal] | tuple[None, None]:
    """Wilson score interval — well-behaved at 0/n and n/n, unlike the normal one."""

    if observations <= 0:
        return (None, None)
    n = Decimal(observations)
    p = Decimal(hits) / n
    z2 = z * z
    denominator = ONE + z2 / n
    centre = (p + z2 / (2 * n)) / denominator
    margin = (z * _sqrt(p * (ONE - p) / n + z2 / (4 * n * n))) / denominator
    low = max(ZERO, centre - margin)
    high = min(ONE, centre + margin)
    return (low.quantize(Decimal("0.000001")), high.quantize(Decimal("0.000001")))


def _median_int(values: Sequence[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2


def _median_decimal(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return ((ordered[middle - 1] + ordered[middle]) / 2).quantize(Decimal("0.01"))


def baseline_rate(
    observations: Sequence[AffinityObservation], *, horizon_seconds: int
) -> Decimal:
    """The population base rate: P(entered Trending within horizon | observed).

    This is the denominator for every claim the engine makes.  Without it,
    "18% precision" is not a number a human can act on (section 46).
    """

    if not observations:
        return ZERO
    hits = sum(1 for observation in observations if observation.hit(horizon_seconds))
    return (Decimal(hits) / Decimal(len(observations))).quantize(Decimal("0.000001"))


def build_affinity(
    actor_id: str,
    observations: Sequence[AffinityObservation],
    *,
    baselines: dict[int, Decimal],
    horizons: Sequence[int] = AFFINITY_HORIZONS_SECONDS,
    surface: str = "",
    handle: str = "",
    prior_strength: Decimal = Decimal("40"),
    min_sample: int = DEFAULT_MIN_SAMPLE,
    recent_since: int | None = None,
) -> AffinityRecord:
    """Assemble one actor's record.  Every horizon gets the full treatment."""

    rows = sorted(observations, key=lambda item: item.observed_at)
    recent = (
        [row for row in rows if row.observed_at >= recent_since]
        if recent_since is not None
        else []
    )

    def horizon_results(sample: Sequence[AffinityObservation]) -> dict[int, HorizonResult]:
        results: dict[int, HorizonResult] = {}
        for horizon in horizons:
            hits = sum(1 for row in sample if row.hit(horizon))
            total = len(sample)
            baseline = baselines.get(horizon, ZERO)
            adjusted = shrunk_rate(
                hits=hits,
                observations=total,
                baseline=baseline,
                prior_strength=prior_strength,
            )
            low, high = wilson_interval(hits=hits, observations=total)
            results[horizon] = HorizonResult(
                horizon_seconds=horizon,
                observations=total,
                hits=hits,
                raw_rate=(
                    None
                    if total == 0
                    else (Decimal(hits) / Decimal(total)).quantize(Decimal("0.000001"))
                ),
                adjusted_rate=adjusted,
                baseline=baseline,
                lift=(
                    None
                    if adjusted is None or baseline <= ZERO
                    else (adjusted / baseline).quantize(Decimal("0.01"))
                ),
                ci_low=low,
                ci_high=high,
            )
        return results

    leads = [
        row.lead_seconds
        for row in rows
        if row.lead_seconds is not None and row.lead_seconds >= 0
    ]
    entry_caps = [
        row.market_cap_at_entry_usd for row in rows if row.market_cap_at_entry_usd is not None
    ]
    ages = [row.token_age_seconds for row in rows if row.token_age_seconds is not None]

    return AffinityRecord(
        actor_id=actor_id,
        surface=surface or (rows[0].surface if rows else ""),
        handle=handle,
        observations=len(rows),
        horizons=horizon_results(rows),
        median_lead_seconds=_median_int(leads),
        median_entry_market_cap_usd=_median_decimal(entry_caps),
        median_token_age_seconds=_median_int(ages),
        first_observed_at=rows[0].observed_at if rows else None,
        last_observed_at=rows[-1].observed_at if rows else None,
        recent_horizons=horizon_results(recent) if recent_since is not None else {},
        recent_observations=len(recent),
        min_sample=min_sample,
    )


def build_population(
    observations: Iterable[AffinityObservation],
    *,
    horizons: Sequence[int] = AFFINITY_HORIZONS_SECONDS,
    prior_strength: Decimal = Decimal("40"),
    min_sample: int = DEFAULT_MIN_SAMPLE,
    recent_since: int | None = None,
    handles: dict[str, str] | None = None,
) -> tuple[dict[str, AffinityRecord], dict[int, Decimal]]:
    """Build every actor's record against a baseline drawn from the same pool.

    The baseline is computed over *all* observations, including the actor's own.
    That is deliberate and slightly conservative: an actor cannot inflate their
    own lift by being excluded from the denominator they are measured against.
    """

    rows = list(observations)
    baselines = {
        horizon: baseline_rate(rows, horizon_seconds=horizon) for horizon in horizons
    }
    grouped: dict[str, list[AffinityObservation]] = {}
    for row in rows:
        if not row.actor_id:
            continue
        grouped.setdefault(row.actor_id, []).append(row)

    records = {
        actor_id: build_affinity(
            actor_id,
            actor_rows,
            baselines=baselines,
            horizons=horizons,
            prior_strength=prior_strength,
            min_sample=min_sample,
            recent_since=recent_since,
            handle=(handles or {}).get(actor_id, ""),
        )
        for actor_id, actor_rows in grouped.items()
    }
    return records, baselines


def rank_actors(
    records: dict[str, AffinityRecord],
    *,
    horizon_seconds: int = 300,
    limit: int = 20,
    meaningful_only: bool = True,
) -> tuple[AffinityRecord, ...]:
    """Rank by shrunk rate.  Thin records are excluded, not quietly promoted."""

    candidates = [
        record
        for record in records.values()
        if not meaningful_only or record.statistically_meaningful
    ]
    candidates.sort(
        key=lambda record: (record.rank_key(horizon_seconds), record.observations),
        reverse=True,
    )
    return tuple(candidates[:limit])


def quality_weight(
    record: AffinityRecord | None, *, horizon_seconds: int = 300, cap: Decimal = Decimal("5")
) -> Decimal:
    """The learned weight for one actor's flow (section 56).

    The weight is the measured lift over the baseline, bounded.  It is *not* an
    invented importance multiplier: an actor with no measured edge weighs
    exactly 1, and an actor with no usable sample also weighs exactly 1, because
    "we have not measured this person" and "this person is average" should
    produce the same, neutral, contribution.
    """

    if record is None or not record.statistically_meaningful:
        return ONE
    result = record.horizons.get(horizon_seconds)
    if result is None or result.lift is None:
        return ONE
    return min(max(result.lift, ZERO), cap)
