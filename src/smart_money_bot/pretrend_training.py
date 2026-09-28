"""Training: assemble, audit, validate walk-forward, and usually refuse to ship.

The default outcome of this module is **no model**, and that is correct.  A
model is promoted to production only when every one of these holds:

1. the dataset is clean — the leakage audit found nothing,
2. there are enough *distinct positive mints*, not merely positive rows,
3. walk-forward validation ran on at least three chronological folds,
4. the pooled lift over the base rate clears a floor,
5. the learned model beat the transparent heuristic baseline.

Point 5 is the one most systems skip.  A learned model that does not beat a
five-rule heuristic has not found anything; it has just fitted the same signal
less legibly, and shipping it trades explainability for nothing.  So the
baseline is trained and scored on identical folds, and
:attr:`TrainingOutcome.beat_baseline` gates promotion.

The threshold is chosen on a **validation** fold — the last training fold, not
the reported test folds — because picking the operating point on the data you
then quote is how a backtest becomes fiction (section 78).

Everything this module returns is a record: the counts, the folds, the verdict,
and, when it declines, the specific reason. ``/modelhealth`` renders it.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from .pretrend.affinity import AffinityRecord, build_population
from .pretrend.dataset import Dataset, build_dataset, compare_features
from .pretrend.features import FEATURE_VERSION, FeatureVector, build_features
from .pretrend.forensics import truncate_state
from .pretrend.labels import LABEL_HORIZONS_SECONDS
from .pretrend.model import (
    HeuristicBaseline,
    LogisticModel,
    can_fit_model,
)
from .pretrend.validation import (
    ScoredRow,
    WalkForwardReport,
    choose_threshold,
    walk_forward,
)
from .pretrend_store import PretrendStore

ZERO = Decimal("0")

#: Pooled lift below this is not an edge worth alerting on.
MIN_PROMOTION_LIFT = Decimal("3")
#: Distinct positive mints below this is not a sample.
MIN_PROMOTION_POSITIVE_MINTS = 30
#: Folds below this is not a walk-forward evaluation.
MIN_PROMOTION_FOLDS = 3


@dataclass(frozen=True, slots=True)
class TrainingOutcome:
    """What a training run produced, and whether it may be used."""

    horizon_seconds: int
    dataset: Dataset | None = None
    model: LogisticModel | None = None
    baseline_report: WalkForwardReport | None = None
    model_report: WalkForwardReport | None = None
    threshold: Decimal = Decimal("1")
    threshold_diagnostics: dict[str, Any] = field(default_factory=dict)
    promoted: bool = False
    refusal_reason: str = ""
    feature_comparisons: tuple[Any, ...] = ()
    trained_at: int = 0
    training_cutoff_at: int = 0

    @property
    def beat_baseline(self) -> bool:
        """Did the learned model actually improve on the transparent rule?"""

        if self.model_report is None or self.baseline_report is None:
            return False
        model_lift = self.model_report.pooled_lift
        baseline_lift = self.baseline_report.pooled_lift
        if model_lift is None:
            return False
        if baseline_lift is None:
            return True
        return model_lift > baseline_lift

    def to_json(self) -> dict[str, Any]:
        return {
            "horizon_seconds": self.horizon_seconds,
            "trained_at": self.trained_at,
            "training_cutoff_at": self.training_cutoff_at,
            "promoted": self.promoted,
            "refusal_reason": self.refusal_reason,
            "beat_baseline": self.beat_baseline,
            "threshold": str(self.threshold),
            "threshold_diagnostics": self.threshold_diagnostics,
            "dataset": None if self.dataset is None else self.dataset.to_json(),
            "baseline": (
                None if self.baseline_report is None else self.baseline_report.to_json()
            ),
            "model": None if self.model_report is None else self.model_report.to_json(),
            "feature_comparisons": [
                comparison.to_json() for comparison in self.feature_comparisons[:15]
            ],
        }


async def build_affinity_buckets(
    store: PretrendStore,
    *,
    since: int,
    until: int,
    horizon_seconds: int,
    bucket_seconds: int = 86_400,
) -> dict[int, dict[str, AffinityRecord]]:
    """Affinity as it *would have been known* at the start of each bucket.

    This function exists because of a leak that is easy to write and hard to
    see.  The ``quality_fomo_buyers`` family depends on each actor's pre-trend
    affinity, and affinity is computed from *outcomes* — whether the tokens they
    entered subsequently trended.  So using one affinity table, built from the
    whole dataset, to generate features for every training row would feed the
    labels back into the inputs: the model would learn to recognise actors it
    has already been told are winners, and the backtest would report that
    circularity as skill.

    The fix is to make affinity strictly backward-looking.  Time is cut into
    buckets, and the affinity used for a row in bucket *N* is built only from
    actor observations whose outcome had already RESOLVED before bucket *N*
    began — that is, ``observed_at + horizon <= bucket_start``.  An actor's
    record therefore grows across the dataset exactly as it would have in
    production, and never contains an outcome from the row's own future.
    """

    observations = await store.actor_observations()
    handles = await store.actor_handles()
    buckets: dict[int, dict[str, AffinityRecord]] = {}
    if not observations:
        return buckets

    boundary = (since // bucket_seconds) * bucket_seconds
    while boundary <= until + bucket_seconds:
        visible = [
            observation
            for observation in observations
            if observation.observed_at + horizon_seconds <= boundary
        ]
        if visible:
            records, _ = build_population(visible, handles=handles)
            buckets[boundary] = records
        else:
            buckets[boundary] = {}
        boundary += bucket_seconds
    return buckets


def affinity_for(
    buckets: Mapping[int, dict[str, AffinityRecord]],
    *,
    at: int,
    bucket_seconds: int = 86_400,
) -> dict[str, AffinityRecord]:
    """The affinity table that was knowable at ``at``.  Empty is a valid answer."""

    if not buckets:
        return {}
    key = (at // bucket_seconds) * bucket_seconds
    if key in buckets:
        return buckets[key]
    earlier = [boundary for boundary in buckets if boundary <= at]
    return buckets[max(earlier)] if earlier else {}


async def collect_vectors(
    store: PretrendStore,
    *,
    since: int,
    until: int,
    stride_seconds: int = 60,
    max_mints: int = 500,
    horizon_seconds: int = 300,
    affinity_bucket_seconds: int = 86_400,
    with_affinity: bool = True,
) -> tuple[FeatureVector, ...]:
    """Rebuild point-in-time feature vectors from stored observations.

    Each vector is built by truncating that mint's state at the observation
    instant and calling the production feature function — the same code path the
    live lane uses, so a model trained here cannot depend on anything production
    lacks.  Affinity is attached from the backward-only buckets above, which is
    what makes offline/online parity real rather than merely claimed: the live
    lane scores with a populated affinity table, so a training path that left it
    empty would compute a systematically different ``quality_fomo_buyers`` and
    ship a model that has never seen its own production inputs.
    """

    buckets = (
        await build_affinity_buckets(
            store,
            since=since,
            until=until,
            horizon_seconds=horizon_seconds,
            bucket_seconds=affinity_bucket_seconds,
        )
        if with_affinity
        else {}
    )

    vectors: list[FeatureVector] = []
    mints = await store.observation_mints(since=since, limit=max_mints)
    for mint in mints:
        rows = await store.observations_for(mint, until=until)
        if not rows:
            continue
        full_state = await store.rebuild_state(mint, until=until)
        last_at = 0
        for row in rows:
            at = int(row["observed_at"])
            if at < since or at > until:
                continue
            if last_at and at - last_at < stride_seconds:
                continue
            last_at = at
            truncated = truncate_state(full_state, at=at)
            if buckets:
                truncated = replace(
                    truncated,
                    affinities=affinity_for(
                        buckets, at=at, bucket_seconds=affinity_bucket_seconds
                    ),
                )
            vectors.append(build_features(truncated))
    return tuple(vectors)


def train_once(
    vectors: Sequence[FeatureVector],
    *,
    first_trending_at: dict[str, int],
    horizon_seconds: int,
    data_complete_until: int,
    entry_unproven_mints: frozenset[str] = frozenset(),
    universe_min_usd: Decimal,
    universe_max_usd: Decimal,
    fold_seconds: int = 86_400,
    target_alerts_per_hour: Decimal = Decimal("4"),
    now: int | None = None,
) -> TrainingOutcome:
    """Assemble, audit, validate and decide.  Refusal is a normal outcome."""

    moment = now if now is not None else int(time.time())
    dataset = build_dataset(
        vectors,
        first_trending_at=first_trending_at,
        horizon_seconds=horizon_seconds,
        data_complete_until=data_complete_until,
        entry_unproven_mints=entry_unproven_mints,
        universe_min_usd=universe_min_usd,
        universe_max_usd=universe_max_usd,
    )

    if not first_trending_at:
        return TrainingOutcome(
            horizon_seconds=horizon_seconds,
            dataset=dataset,
            refusal_reason=(
                "no witnessed FOMO board entries exist, so there are no supervised "
                "targets to fit. This is the expected state on a deployment whose "
                "Trending source is a PROXY approximation rather than an authorised "
                "FOMO feed: proxy rows are collected but cannot establish FOMO labels."
            ),
            trained_at=moment,
            training_cutoff_at=data_complete_until,
        )
    if not dataset.clean:
        return TrainingOutcome(
            horizon_seconds=horizon_seconds,
            dataset=dataset,
            refusal_reason=(
                f"leakage audit found {len(dataset.leakage)} finding(s): "
                f"{sorted({finding.kind for finding in dataset.leakage})}"
            ),
            trained_at=moment,
            training_cutoff_at=data_complete_until,
        )

    can_fit, why_not = can_fit_model(dataset.rows)
    if not can_fit:
        return TrainingOutcome(
            horizon_seconds=horizon_seconds,
            dataset=dataset,
            refusal_reason=f"not enough data to fit: {why_not}",
            trained_at=moment,
            training_cutoff_at=data_complete_until,
        )

    baseline_report = walk_forward(
        dataset.rows,
        fit=lambda rows: HeuristicBaseline().fit(rows),
        model_name="heuristic_baseline",
        horizon_seconds=horizon_seconds,
        threshold=Decimal("0.2"),
        fold_seconds=fold_seconds,
    )
    model_report = walk_forward(
        dataset.rows,
        fit=lambda rows: LogisticModel().fit(rows),
        model_name="logistic_v1",
        horizon_seconds=horizon_seconds,
        threshold=Decimal("0.2"),
        fold_seconds=fold_seconds,
    )

    # Fit the shippable model on everything up to the cutoff, and calibrate it
    # on the most recent slice, which is the closest available proxy for the
    # regime it will actually face.
    ordered = sorted(dataset.rows, key=lambda row: row.observed_at)
    split = max(1, int(len(ordered) * 0.8))
    model = LogisticModel().fit(ordered[:split], calibration_rows=ordered[split:])

    # The operating threshold comes from the held-out calibration slice, never
    # from the folds reported above.
    validation_scored = [
        ScoredRow(
            mint=row.mint,
            observed_at=row.observed_at,
            probability=model.predict(row.values).probability,
            label=row.label,
        )
        for row in ordered[split:]
    ]
    span = (
        validation_scored[-1].observed_at - validation_scored[0].observed_at
        if len(validation_scored) >= 2
        else 0
    )
    threshold, diagnostics = choose_threshold(
        validation_scored,
        target_alerts_per_hour=target_alerts_per_hour,
        span_seconds=span,
    )

    comparisons = compare_features(dataset)

    outcome = TrainingOutcome(
        horizon_seconds=horizon_seconds,
        dataset=dataset,
        model=model,
        baseline_report=baseline_report,
        model_report=model_report,
        threshold=threshold,
        threshold_diagnostics=diagnostics,
        feature_comparisons=comparisons,
        trained_at=moment,
        training_cutoff_at=data_complete_until,
    )

    refusal = _promotion_refusal(outcome)
    return replace(outcome, promoted=not refusal, refusal_reason=refusal)


def _promotion_refusal(outcome: TrainingOutcome) -> str:
    """The named reason a model may not be promoted, or an empty string."""

    dataset = outcome.dataset
    report = outcome.model_report
    if dataset is None or report is None:
        return "no dataset or no validation report"
    if dataset.distinct_positive_mints < MIN_PROMOTION_POSITIVE_MINTS:
        return (
            f"only {dataset.distinct_positive_mints} distinct positive mints; "
            f"{MIN_PROMOTION_POSITIVE_MINTS} required. Rows are not a sample — "
            "one token producing forty rows is one token."
        )
    if len(report.folds) < MIN_PROMOTION_FOLDS:
        return (
            f"only {len(report.folds)} walk-forward fold(s); "
            f"{MIN_PROMOTION_FOLDS} required to claim out-of-sample performance"
        )
    if not report.clean:
        return "walk-forward folds contain leakage or split contamination"
    lift = report.pooled_lift
    if lift is None:
        return "pooled lift is unmeasurable (no alerts fired in validation)"
    if lift < MIN_PROMOTION_LIFT:
        return (
            f"pooled lift {lift}x is below the {MIN_PROMOTION_LIFT}x floor — "
            f"verdict {report.verdict}. The hypothesis is not supported by this data."
        )
    if outcome.threshold_diagnostics.get("budget_overshoot"):
        return (
            "the chosen threshold cannot enforce the alert budget: "
            f"{outcome.threshold_diagnostics.get('overshoot_reason', '')}. "
            "Promoting it would reproduce the alert-volume problem this lane exists to fix."
        )
    if not outcome.beat_baseline:
        baseline_lift = (
            None if outcome.baseline_report is None else outcome.baseline_report.pooled_lift
        )
        return (
            f"the learned model's lift ({lift}x) did not beat the transparent "
            f"heuristic baseline ({baseline_lift}x). Shipping it would trade "
            "explainability for nothing."
        )
    return ""


async def train_and_store(
    store: PretrendStore,
    *,
    horizon_seconds: int,
    universe_min_usd: Decimal,
    universe_max_usd: Decimal,
    lookback_seconds: int = 14 * 86_400,
    fold_seconds: int = 86_400,
    target_alerts_per_hour: Decimal = Decimal("4"),
    lane: str = "production",
    now: int | None = None,
) -> TrainingOutcome:
    """Run a full training pass and persist the result, promoted or not.

    A refused run is stored too, inactive, so ``/modelhealth`` can show *why*
    the lane is still silent rather than merely that it is.
    """

    moment = now if now is not None else int(time.time())
    # The cutoff is pulled back by the longest label horizon: rows newer than
    # that cannot have determined labels, and including them would silently
    # label every recent positive as a negative.
    cutoff = moment - max(LABEL_HORIZONS_SECONDS)
    vectors = await collect_vectors(
        store,
        since=moment - lookback_seconds,
        until=cutoff,
        horizon_seconds=horizon_seconds,
    )
    first_trending = await store.first_trending_map()
    unproven = await store.entry_unproven_mints()

    outcome = train_once(
        vectors,
        first_trending_at=first_trending,
        horizon_seconds=horizon_seconds,
        data_complete_until=cutoff,
        entry_unproven_mints=unproven,
        universe_min_usd=universe_min_usd,
        universe_max_usd=universe_max_usd,
        fold_seconds=fold_seconds,
        target_alerts_per_hour=target_alerts_per_hour,
        now=moment,
    )

    if outcome.model is not None:
        model_id = hashlib.sha256(
            f"{lane}:{horizon_seconds}:{moment}:{FEATURE_VERSION}".encode()
        ).hexdigest()[:32]
        await store.save_model(
            model_id=model_id,
            name=outcome.model.name,
            lane=lane,
            horizon_seconds=horizon_seconds,
            feature_version=FEATURE_VERSION,
            trained_at=moment,
            training_cutoff_at=cutoff,
            trained_rows=outcome.model.trained_rows,
            trained_positives=outcome.model.trained_positives,
            threshold=outcome.threshold,
            active=outcome.promoted,
            metrics=outcome.to_json(),
            payload=outcome.model.dumps(),
        )
    return outcome


async def load_active_model(
    store: PretrendStore, *, lane: str = "production"
) -> tuple[LogisticModel | None, Decimal, str]:
    """Load the promoted model, refusing one built on a different feature version.

    A feature-version mismatch means a stored coefficient now multiplies a
    different quantity.  Refusing is the only safe behaviour; the lane simply
    stays silent until the next training run.
    """

    row = await store.active_model(lane=lane)
    if row is None:
        return (None, Decimal("1"), "no active model for this lane")
    if row["feature_version"] != FEATURE_VERSION:
        return (
            None,
            Decimal("1"),
            f"stored model was built on features {row['feature_version']!r} but "
            f"this build produces {FEATURE_VERSION!r}; refusing to score across "
            "feature versions",
        )
    try:
        model = LogisticModel.loads(row["payload_json"])
    except Exception as exc:
        return (None, Decimal("1"), f"stored model failed to deserialise: {exc}")
    return (model, Decimal(str(row["threshold"])), "")
