"""Assembling the labelled dataset, and refusing to hand over a dirty one.

This is the join: point-in-time feature vectors on one side, ground-truth board
entries on the other, matched controls filling out the negative class.  It is
also the last place a mistake is cheap, so the assembly is deliberately
suspicious of its own output:

* Every row's label comes from :mod:`~smart_money_bot.pretrend.labels`, which
  refuses rows for mints already on the board and marks unresolvable horizons as
  censored rather than negative.
* Every assembled dataset is run through
  :func:`~smart_money_bot.pretrend.leakage.audit_dataset`, and
  :attr:`Dataset.usable` is false when anything is found.  A dataset with
  leakage is not "mostly fine".
* The counts are part of the return value, not a log line.  A caller that wants
  to quote a precision figure has the positive count right next to it.

Sampling candidate instants deserves a note.  A token observed every 30 seconds
for an hour contributes 120 rows, all nearly identical, and all sharing one
outcome — which lets a single token dominate a fold and makes the effective
sample size far smaller than the row count suggests.  So rows are thinned to one
per ``stride_seconds`` per mint, and :attr:`Dataset.distinct_mints` is reported
next to :attr:`Dataset.rows` so the difference is visible.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .cohorts import DEFAULT_UNIVERSE_MAX_USD, DEFAULT_UNIVERSE_MIN_USD
from .controls import CandidateRow, ControlSample, build_matched_controls
from .features import FeatureVector
from .labels import (
    LabelSet,
    label_observation,
)
from .leakage import LeakageFinding, audit_dataset
from .model import TrainingRow

ZERO = Decimal("0")

#: One row per mint per this many seconds.  Denser sampling adds correlated
#: rows, not information.
DEFAULT_STRIDE_SECONDS = 60


@dataclass(frozen=True, slots=True)
class Dataset:
    """A labelled dataset plus every number needed to judge it."""

    horizon_seconds: int
    rows: tuple[TrainingRow, ...] = ()
    label_sets: tuple[LabelSet, ...] = ()
    leakage: tuple[LeakageFinding, ...] = ()
    controls: ControlSample | None = None
    distinct_mints: int = 0
    distinct_positive_mints: int = 0
    censored: int = 0
    ineligible: int = 0
    feature_version: str = ""

    @property
    def positives(self) -> int:
        return sum(1 for row in self.rows if row.label)

    @property
    def negatives(self) -> int:
        return len(self.rows) - self.positives

    @property
    def base_rate(self) -> Decimal | None:
        if not self.rows:
            return None
        return (Decimal(self.positives) / Decimal(len(self.rows))).quantize(
            Decimal("0.000001")
        )

    @property
    def clean(self) -> bool:
        return not self.leakage

    @property
    def usable(self) -> bool:
        """Whether this dataset may be trained on at all."""

        return self.clean and self.positives > 0 and self.negatives > 0

    @property
    def sufficient(self) -> bool:
        """Whether a metric computed on it may be quoted without a caveat.

        The binding constraint is *distinct positive mints*, not positive rows:
        thirty rows from three tokens is three tokens.
        """

        return self.distinct_positive_mints >= 30 and len(self.rows) >= 500

    def to_json(self) -> dict[str, Any]:
        return {
            "horizon_seconds": self.horizon_seconds,
            "feature_version": self.feature_version,
            "rows": len(self.rows),
            "positives": self.positives,
            "negatives": self.negatives,
            "distinct_mints": self.distinct_mints,
            "distinct_positive_mints": self.distinct_positive_mints,
            "censored": self.censored,
            "ineligible": self.ineligible,
            "base_rate": None if self.base_rate is None else str(self.base_rate),
            "clean": self.clean,
            "usable": self.usable,
            "sufficient": self.sufficient,
            "leakage": [finding.to_json() for finding in self.leakage[:20]],
            "controls": None if self.controls is None else self.controls.to_json(),
        }


def thin_by_stride(
    vectors: Sequence[FeatureVector], *, stride_seconds: int = DEFAULT_STRIDE_SECONDS
) -> tuple[FeatureVector, ...]:
    """Keep at most one vector per mint per stride, oldest first.

    Keeping the *oldest* in each bucket rather than the newest is deliberate: it
    biases the dataset toward earlier evidence, which is the thing we are trying
    to learn to act on.
    """

    kept: list[FeatureVector] = []
    last_kept: dict[str, int] = {}
    for vector in sorted(vectors, key=lambda item: (item.observed_at, item.mint)):
        previous = last_kept.get(vector.mint)
        if previous is not None and vector.observed_at - previous < stride_seconds:
            continue
        last_kept[vector.mint] = vector.observed_at
        kept.append(vector)
    return tuple(kept)


def build_dataset(
    vectors: Sequence[FeatureVector],
    *,
    first_trending_at: Mapping[str, int],
    horizon_seconds: int,
    data_complete_until: int,
    universe_min_usd: Decimal = DEFAULT_UNIVERSE_MIN_USD,
    universe_max_usd: Decimal = DEFAULT_UNIVERSE_MAX_USD,
    stride_seconds: int = DEFAULT_STRIDE_SECONDS,
    controls_per_positive: int = 4,
    match_controls: bool = True,
) -> Dataset:
    """Join vectors to ground truth, label, thin, audit, and report the counts."""

    thinned = thin_by_stride(vectors, stride_seconds=stride_seconds)
    label_sets: list[LabelSet] = []
    rows: list[TrainingRow] = []
    candidates: list[CandidateRow] = []
    censored = 0
    ineligible = 0

    for vector in thinned:
        market_cap = vector.get("market_cap_usd")
        entered = first_trending_at.get(vector.mint)
        label_set = label_observation(
            mint=vector.mint,
            observed_at=vector.observed_at,
            first_trending_at=entered,
            market_cap_usd=market_cap,
            universe_min_usd=universe_min_usd,
            universe_max_usd=universe_max_usd,
            data_complete_until=data_complete_until,
        )
        label_sets.append(label_set)

        age = vector.get("token_age_seconds")
        candidates.append(
            CandidateRow(
                mint=vector.mint,
                observed_at=vector.observed_at,
                market_cap_usd=market_cap,
                token_age_seconds=None if age is None else int(age),
                eligible=label_set.eligible,
                first_trending_at=entered,
                liquidity_usd=vector.get("liquidity_usd"),
            )
        )

        if not label_set.eligible:
            ineligible += 1
            continue
        label = label_set.label(horizon_seconds)
        if label is None or label.censored or label.positive is None:
            censored += 1
            continue
        rows.append(
            TrainingRow(
                mint=vector.mint,
                observed_at=vector.observed_at,
                values=dict(vector.values),
                label=bool(label.positive),
                feature_version=vector.feature_version,
            )
        )

    controls = (
        build_matched_controls(
            candidates,
            horizon_seconds=horizon_seconds,
            controls_per_positive=controls_per_positive,
        )
        if match_controls
        else None
    )

    report = audit_dataset(thinned)
    positive_mints = {row.mint for row in rows if row.label}

    return Dataset(
        horizon_seconds=horizon_seconds,
        rows=tuple(rows),
        label_sets=tuple(label_sets),
        leakage=report.findings,
        controls=controls,
        distinct_mints=len({row.mint for row in rows}),
        distinct_positive_mints=len(positive_mints),
        censored=censored,
        ineligible=ineligible,
        feature_version=thinned[0].feature_version if thinned else "",
    )


def restrict_to_matched(dataset: Dataset) -> Dataset:
    """Keep only rows that appear in the matched-control comparison.

    Use this for the winners-vs-look-alikes study (section 39).  It is a
    *smaller* and *harder* dataset than the full one, which is the point: a
    model that only beats the base rate on unmatched negatives has learned the
    cohort, not the behaviour.
    """

    if dataset.controls is None:
        return dataset
    allowed = {
        (pair.positive.mint, pair.positive.observed_at) for pair in dataset.controls.pairs
    } | {
        (control.mint, control.observed_at)
        for pair in dataset.controls.pairs
        for control in pair.controls
    }
    rows = tuple(
        row for row in dataset.rows if (row.mint, row.observed_at) in allowed
    )
    positive_mints = {row.mint for row in rows if row.label}
    return Dataset(
        horizon_seconds=dataset.horizon_seconds,
        rows=rows,
        label_sets=dataset.label_sets,
        leakage=dataset.leakage,
        controls=dataset.controls,
        distinct_mints=len({row.mint for row in rows}),
        distinct_positive_mints=len(positive_mints),
        censored=dataset.censored,
        ineligible=dataset.ineligible,
        feature_version=dataset.feature_version,
    )


@dataclass(frozen=True, slots=True)
class FeatureComparison:
    """One feature's behaviour in winners versus matched look-alikes."""

    feature: str
    positive_median: Decimal | None
    negative_median: Decimal | None
    positive_p75: Decimal | None
    negative_p75: Decimal | None
    #: Rank-biserial effect size from the Mann-Whitney U statistic, in [-1, 1].
    #: Distribution-free on purpose: these features are heavily skewed and a
    #: difference of means would be dominated by a handful of outliers.
    effect_size: Decimal | None
    positive_n: int
    negative_n: int
    missing_rate: Decimal | None

    @property
    def lift(self) -> Decimal | None:
        if (
            self.positive_median is None
            or self.negative_median is None
            or self.negative_median <= ZERO
        ):
            return None
        return (self.positive_median / self.negative_median).quantize(Decimal("0.01"))

    @property
    def meaningful(self) -> bool:
        """Whether this comparison rests on enough data to discuss."""

        return self.positive_n >= 20 and self.negative_n >= 20

    def to_json(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "feature": self.feature,
            "positive_median": s(self.positive_median),
            "negative_median": s(self.negative_median),
            "positive_p75": s(self.positive_p75),
            "negative_p75": s(self.negative_p75),
            "effect_size": s(self.effect_size),
            "lift": s(self.lift),
            "positive_n": self.positive_n,
            "negative_n": self.negative_n,
            "missing_rate": s(self.missing_rate),
            "meaningful": self.meaningful,
        }


def _quantile(values: Sequence[Decimal], fraction: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = int(
        (fraction * Decimal(len(ordered) - 1)).to_integral_value(rounding="ROUND_HALF_UP")
    )
    return ordered[max(0, min(index, len(ordered) - 1))]


def _rank_biserial(positive: Sequence[Decimal], negative: Sequence[Decimal]) -> Decimal | None:
    """Rank-biserial correlation: P(pos > neg) - P(neg > pos).

    0 means the two distributions are interchangeable; 1 means every positive
    exceeds every negative.  Ties count half to each side.
    """

    if not positive or not negative:
        return None
    wins = 0.0
    total = len(positive) * len(negative)
    ordered_negative = sorted(negative)
    import bisect

    for value in positive:
        lower = bisect.bisect_left(ordered_negative, value)
        upper = bisect.bisect_right(ordered_negative, value)
        wins += lower + (upper - lower) / 2
    probability = wins / total
    return Decimal(str(round(2 * probability - 1, 4)))


def compare_features(
    dataset: Dataset, *, features: Sequence[str] | None = None, limit: int = 40
) -> tuple[FeatureComparison, ...]:
    """Winners versus matched non-winners, per feature, strongest effect first.

    This is the section-39 study, and it is the first thing worth looking at:
    if no feature separates the two groups, no model built on them will either,
    and a model that appears to is overfitting.
    """

    if not dataset.rows:
        return ()
    names = features or sorted(dataset.rows[0].values)
    comparisons: list[FeatureComparison] = []
    positives = [row for row in dataset.rows if row.label]
    negatives = [row for row in dataset.rows if not row.label]

    for name in names[:limit] if features else names:
        positive_values = [
            value
            for row in positives
            if (value := row.values.get(name)) is not None
        ]
        negative_values = [
            value
            for row in negatives
            if (value := row.values.get(name)) is not None
        ]
        total = len(positives) + len(negatives)
        known = len(positive_values) + len(negative_values)
        comparisons.append(
            FeatureComparison(
                feature=name,
                positive_median=_quantile(positive_values, Decimal("0.5")),
                negative_median=_quantile(negative_values, Decimal("0.5")),
                positive_p75=_quantile(positive_values, Decimal("0.75")),
                negative_p75=_quantile(negative_values, Decimal("0.75")),
                effect_size=_rank_biserial(positive_values, negative_values),
                positive_n=len(positive_values),
                negative_n=len(negative_values),
                missing_rate=(
                    None
                    if total == 0
                    else (
                        Decimal(total - known) / Decimal(total)
                    ).quantize(Decimal("0.0001"))
                ),
            )
        )

    comparisons.sort(
        key=lambda item: abs(item.effect_size or ZERO),
        reverse=True,
    )
    return tuple(comparisons[:limit])
