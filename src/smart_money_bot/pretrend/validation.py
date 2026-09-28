"""Walk-forward validation, and metrics that refuse to flatter themselves.

There is no random split in this module, and there is no way to ask for one.
A random split on token observations is not a mild methodological preference —
it is a guarantee of a wrong answer, for two compounding reasons.  Market regime
is shared across all tokens alive at the same moment, so a random split trains on
the same afternoon it tests on.  And a single token produces dozens of correlated
observations, so a random split puts the same token's 10:04 row in training and
its 10:05 row in test and then reports the memory as skill.

So folds are chronological: train on everything before a boundary, test on the
window after it, advance, repeat.  Each fold is additionally checked for mint
contamination, because a mint straddling the boundary re-creates the same problem
inside an otherwise correct temporal split.

The metrics are chosen to be hard to fool:

* **Precision, recall and precision@K** at an operating threshold — precision@K
  matters because the product ships a fixed number of alerts an hour, not a
  fixed threshold.
* **PR-AUC**, not ROC-AUC.  At a 1% base rate, ROC-AUC looks respectable for a
  model that is useless, because the enormous negative class makes the false
  positive rate tiny by construction.
* **Brier score** and **reliability buckets** for calibration, so "70%" can be
  checked against how often those tokens actually trended.
* **Base rate and lift** on every single result (section 46).  A precision
  figure without its base rate is not a result.
* **Alerts per hour** — the operational cost of the threshold, reported next to
  its benefit.

Every metric carries ``n``.  :attr:`FoldMetrics.sufficient` is false when the
sample is too small to quote, and the report says so rather than printing a
confident number (section 47).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .affinity import wilson_interval
from .leakage import LeakageFinding, check_split
from .model import TrainingRow

ZERO = Decimal("0")

#: Below this many positives in a test fold, metrics are computed but flagged.
MIN_TEST_POSITIVES = 10


@dataclass(frozen=True, slots=True)
class ScoredRow:
    """One test-fold row with its prediction and truth."""

    mint: str
    observed_at: int
    probability: Decimal
    label: bool


@dataclass(frozen=True, slots=True)
class ReliabilityBucket:
    """Predicted vs actual for one probability band."""

    bucket: str
    count: int
    mean_predicted: Decimal | None
    observed_rate: Decimal | None

    @property
    def gap(self) -> Decimal | None:
        if self.mean_predicted is None or self.observed_rate is None:
            return None
        return (self.observed_rate - self.mean_predicted).quantize(Decimal("0.0001"))

    def to_json(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "count": self.count,
            "mean_predicted": (
                None if self.mean_predicted is None else str(self.mean_predicted)
            ),
            "observed_rate": (
                None if self.observed_rate is None else str(self.observed_rate)
            ),
            "gap": None if self.gap is None else str(self.gap),
        }


@dataclass(frozen=True, slots=True)
class FoldMetrics:
    """Everything one test fold produced, with the counts behind it."""

    fold: int
    train_rows: int
    train_positives: int
    test_rows: int
    test_positives: int
    threshold: Decimal
    alerts: int = 0
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    precision: Decimal | None = None
    precision_ci: tuple[Decimal | None, Decimal | None] = (None, None)
    recall: Decimal | None = None
    pr_auc: Decimal | None = None
    brier: Decimal | None = None
    base_rate: Decimal | None = None
    lift: Decimal | None = None
    precision_at_k: dict[int, Decimal | None] = field(default_factory=dict)
    alerts_per_hour: Decimal | None = None
    test_span_seconds: int = 0
    reliability: tuple[ReliabilityBucket, ...] = ()
    leakage: tuple[LeakageFinding, ...] = ()

    @property
    def sufficient(self) -> bool:
        """Whether this fold has enough positives for its numbers to be quotable."""

        return self.test_positives >= MIN_TEST_POSITIVES

    def to_json(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "fold": self.fold,
            "train_rows": self.train_rows,
            "train_positives": self.train_positives,
            "test_rows": self.test_rows,
            "test_positives": self.test_positives,
            "threshold": str(self.threshold),
            "alerts": self.alerts,
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "precision": s(self.precision),
            "precision_ci_low": s(self.precision_ci[0]),
            "precision_ci_high": s(self.precision_ci[1]),
            "recall": s(self.recall),
            "pr_auc": s(self.pr_auc),
            "brier": s(self.brier),
            "base_rate": s(self.base_rate),
            "lift": s(self.lift),
            "precision_at_k": {
                str(key): s(value) for key, value in sorted(self.precision_at_k.items())
            },
            "alerts_per_hour": s(self.alerts_per_hour),
            "test_span_seconds": self.test_span_seconds,
            "sufficient": self.sufficient,
            "reliability": [bucket.to_json() for bucket in self.reliability],
            "leakage": [finding.to_json() for finding in self.leakage],
        }


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    """The aggregate across folds, plus the honest verdict."""

    model_name: str
    horizon_seconds: int
    folds: tuple[FoldMetrics, ...] = ()
    skipped_folds: int = 0
    skip_reasons: tuple[str, ...] = ()

    @property
    def total_test_positives(self) -> int:
        return sum(fold.test_positives for fold in self.folds)

    @property
    def total_alerts(self) -> int:
        return sum(fold.alerts for fold in self.folds)

    @property
    def pooled_precision(self) -> Decimal | None:
        """Precision pooled across folds — not the mean of per-fold precisions.

        Averaging per-fold precisions gives a quiet fold with two alerts the same
        weight as a busy one with two hundred, which flatters a model that is
        accidentally precise when it barely fires.
        """

        alerts = sum(fold.alerts for fold in self.folds)
        if alerts <= 0:
            return None
        hits = sum(fold.true_positives for fold in self.folds)
        return (Decimal(hits) / Decimal(alerts)).quantize(Decimal("0.000001"))

    @property
    def pooled_base_rate(self) -> Decimal | None:
        rows = sum(fold.test_rows for fold in self.folds)
        if rows <= 0:
            return None
        positives = sum(fold.test_positives for fold in self.folds)
        return (Decimal(positives) / Decimal(rows)).quantize(Decimal("0.000001"))

    @property
    def pooled_lift(self) -> Decimal | None:
        precision = self.pooled_precision
        base = self.pooled_base_rate
        if precision is None or base is None or base <= ZERO:
            return None
        return (precision / base).quantize(Decimal("0.01"))

    @property
    def clean(self) -> bool:
        return all(not fold.leakage for fold in self.folds)

    @property
    def sufficient(self) -> bool:
        return self.total_test_positives >= 30 and len(self.folds) >= 3

    @property
    def verdict(self) -> str:
        """A one-word answer that is allowed to be bad news."""

        if not self.folds:
            return "NO_FOLDS"
        if not self.clean:
            return "LEAKAGE_DETECTED"
        if not self.sufficient:
            return "INSUFFICIENT_SAMPLE"
        lift = self.pooled_lift
        if lift is None:
            return "UNMEASURABLE"
        if lift < Decimal("1.5"):
            return "NO_EDGE"
        if lift < Decimal("3"):
            return "WEAK_EDGE"
        return "EDGE_PRESENT"

    def to_json(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "model": self.model_name,
            "horizon_seconds": self.horizon_seconds,
            "folds": [fold.to_json() for fold in self.folds],
            "fold_count": len(self.folds),
            "skipped_folds": self.skipped_folds,
            "skip_reasons": list(self.skip_reasons),
            "total_test_positives": self.total_test_positives,
            "total_alerts": self.total_alerts,
            "pooled_precision": s(self.pooled_precision),
            "pooled_base_rate": s(self.pooled_base_rate),
            "pooled_lift": s(self.pooled_lift),
            "clean": self.clean,
            "sufficient": self.sufficient,
            "verdict": self.verdict,
        }


def precision_recall_auc(rows: Sequence[ScoredRow]) -> Decimal | None:
    """Area under the precision-recall curve, by the trapezoid rule.

    PR rather than ROC: at a 1% base rate the ROC curve is dominated by the
    negative class and a useless model still scores 0.7.
    """

    positives = sum(1 for row in rows if row.label)
    if positives == 0 or len(rows) < 2:
        return None
    ordered = sorted(rows, key=lambda row: row.probability, reverse=True)
    true_positives = 0
    previous_recall = 0.0
    area = 0.0
    for seen, row in enumerate(ordered, start=1):
        if row.label:
            true_positives += 1
        precision = true_positives / seen
        recall = true_positives / positives
        area += precision * (recall - previous_recall)
        previous_recall = recall
    return Decimal(str(round(area, 6)))


def brier_score(rows: Sequence[ScoredRow]) -> Decimal | None:
    """Mean squared error of the probabilities.  Lower is better."""

    if not rows:
        return None
    total = sum(
        (float(row.probability) - (1.0 if row.label else 0.0)) ** 2 for row in rows
    )
    return Decimal(str(round(total / len(rows), 6)))


def reliability_buckets(
    rows: Sequence[ScoredRow],
    *,
    edges: Sequence[float] = (0.0, 0.01, 0.05, 0.1, 0.25, 0.5, 1.01),
) -> tuple[ReliabilityBucket, ...]:
    """Predicted vs actual per probability band — the calibration check."""

    buckets: list[ReliabilityBucket] = []
    for index in range(len(edges) - 1):
        low, high = edges[index], edges[index + 1]
        members = [row for row in rows if low <= float(row.probability) < high]
        if not members:
            buckets.append(
                ReliabilityBucket(
                    bucket=f"{low:.0%}-{min(high, 1.0):.0%}",
                    count=0,
                    mean_predicted=None,
                    observed_rate=None,
                )
            )
            continue
        mean_predicted = sum(float(row.probability) for row in members) / len(members)
        observed = sum(1 for row in members if row.label) / len(members)
        buckets.append(
            ReliabilityBucket(
                bucket=f"{low:.0%}-{min(high, 1.0):.0%}",
                count=len(members),
                mean_predicted=Decimal(str(round(mean_predicted, 6))),
                observed_rate=Decimal(str(round(observed, 6))),
            )
        )
    return tuple(buckets)


def precision_at_k(rows: Sequence[ScoredRow], k: int) -> Decimal | None:
    """Precision among the top-``k`` scored rows — the shape the product ships."""

    if k <= 0 or not rows:
        return None
    ordered = sorted(rows, key=lambda row: row.probability, reverse=True)[:k]
    if not ordered:
        return None
    hits = sum(1 for row in ordered if row.label)
    return (Decimal(hits) / Decimal(len(ordered))).quantize(Decimal("0.000001"))


def evaluate_fold(
    *,
    fold: int,
    train: Sequence[TrainingRow],
    test: Sequence[TrainingRow],
    scored: Sequence[ScoredRow],
    threshold: Decimal,
    k_values: Sequence[int] = (5, 10, 25),
) -> FoldMetrics:
    """Score one fold, and check it for the leakage a correct split still allows."""

    positives = sum(1 for row in scored if row.label)
    alerts = [row for row in scored if row.probability >= threshold]
    hits = sum(1 for row in alerts if row.label)
    misses = positives - hits

    precision = (
        None
        if not alerts
        else (Decimal(hits) / Decimal(len(alerts))).quantize(Decimal("0.000001"))
    )
    recall = (
        None
        if positives == 0
        else (Decimal(hits) / Decimal(positives)).quantize(Decimal("0.000001"))
    )
    base_rate = (
        None
        if not scored
        else (Decimal(positives) / Decimal(len(scored))).quantize(Decimal("0.000001"))
    )
    lift = (
        None
        if precision is None or base_rate is None or base_rate <= ZERO
        else (precision / base_rate).quantize(Decimal("0.01"))
    )

    timestamps = [row.observed_at for row in scored]
    span = (max(timestamps) - min(timestamps)) if len(timestamps) >= 2 else 0
    alerts_per_hour = (
        None
        if span <= 0
        else (Decimal(len(alerts)) * Decimal(3600) / Decimal(span)).quantize(
            Decimal("0.01")
        )
    )

    return FoldMetrics(
        fold=fold,
        train_rows=len(train),
        train_positives=sum(1 for row in train if row.label),
        test_rows=len(scored),
        test_positives=positives,
        threshold=threshold,
        alerts=len(alerts),
        true_positives=hits,
        false_positives=len(alerts) - hits,
        false_negatives=misses,
        precision=precision,
        precision_ci=(
            wilson_interval(hits=hits, observations=len(alerts))
            if alerts
            else (None, None)
        ),
        recall=recall,
        pr_auc=precision_recall_auc(scored),
        brier=brier_score(scored),
        base_rate=base_rate,
        lift=lift,
        precision_at_k={k: precision_at_k(scored, k) for k in k_values},
        alerts_per_hour=alerts_per_hour,
        test_span_seconds=span,
        reliability=reliability_buckets(scored),
        leakage=check_split(
            train=[(row.mint, row.observed_at) for row in train],
            test=[(row.mint, row.observed_at) for row in test],
        ),
    )


def temporal_folds(
    rows: Sequence[TrainingRow],
    *,
    fold_seconds: int = 86_400,
    min_train_folds: int = 1,
    embargo_seconds: int = 0,
) -> list[tuple[list[TrainingRow], list[TrainingRow]]]:
    """Chronological folds: train on the past, test on the next window.

    ``embargo_seconds`` drops rows immediately after the boundary from training.
    With a 20-minute label horizon, a training row 5 minutes before the boundary
    has an outcome that resolves *inside* the test window — its label is
    therefore partly determined by the period being tested, which is a subtle
    but real leak.  Setting the embargo to the label horizon removes it.
    """

    if not rows:
        return []
    ordered = sorted(rows, key=lambda row: (row.observed_at, row.mint))
    start = ordered[0].observed_at
    end = ordered[-1].observed_at
    if end <= start:
        return []

    folds: list[tuple[list[TrainingRow], list[TrainingRow]]] = []
    boundary = start + fold_seconds * min_train_folds
    while boundary < end:
        test_end = boundary + fold_seconds
        train = [
            row for row in ordered if row.observed_at <= boundary - embargo_seconds
        ]
        test = [row for row in ordered if boundary < row.observed_at <= test_end]
        if train and test:
            folds.append((train, test))
        boundary = test_end
    return folds


def walk_forward(
    rows: Sequence[TrainingRow],
    *,
    fit: Callable[[Sequence[TrainingRow]], Any],
    model_name: str,
    horizon_seconds: int,
    threshold: Decimal = Decimal("0.2"),
    fold_seconds: int = 86_400,
    min_train_folds: int = 1,
    embargo_seconds: int | None = None,
    drop_contaminating_mints: bool = True,
) -> WalkForwardReport:
    """Run the whole walk-forward evaluation.  There is no random-split option.

    ``fit`` receives only the training fold and must return an object with a
    ``predict(values)`` method.  The harness never hands it the test fold, so a
    model cannot tune on what it is scored against.
    """

    embargo = horizon_seconds if embargo_seconds is None else embargo_seconds
    folds = temporal_folds(
        rows,
        fold_seconds=fold_seconds,
        min_train_folds=min_train_folds,
        embargo_seconds=embargo,
    )
    metrics: list[FoldMetrics] = []
    skipped = 0
    reasons: list[str] = []

    for index, (train, test) in enumerate(folds, start=1):
        if drop_contaminating_mints:
            # A mint present in both folds is removed from the TEST side.
            # Removing it from training instead would throw away history the
            # live system genuinely would have had.
            train_mints = {row.mint for row in train}
            test = [row for row in test if row.mint not in train_mints]
        if not test:
            skipped += 1
            reasons.append(f"fold {index}: no test rows after contamination removal")
            continue
        positives = sum(1 for row in train if row.label)
        if positives == 0:
            skipped += 1
            reasons.append(f"fold {index}: training fold has no positives")
            continue

        model = fit(train)
        scored = [
            ScoredRow(
                mint=row.mint,
                observed_at=row.observed_at,
                probability=model.predict(row.values).probability,
                label=row.label,
            )
            for row in test
        ]
        metrics.append(
            evaluate_fold(
                fold=index,
                train=train,
                test=test,
                scored=scored,
                threshold=threshold,
            )
        )

    return WalkForwardReport(
        model_name=model_name,
        horizon_seconds=horizon_seconds,
        folds=tuple(metrics),
        skipped_folds=skipped,
        skip_reasons=tuple(reasons),
    )


def choose_threshold(
    scored: Sequence[ScoredRow],
    *,
    target_alerts_per_hour: Decimal,
    span_seconds: int,
    min_precision: Decimal | None = None,
) -> tuple[Decimal, dict[str, Any]]:
    """Pick the operating threshold from a **validation** fold, never the test one.

    The product constraint is an alert budget, so the threshold is chosen as the
    score that produces that many alerts per hour, then raised if a minimum
    precision was requested and not met.  Tuning it on the fold it is then
    reported against is the single most common way a backtest becomes fiction;
    the caller is responsible for passing validation rows, and the returned
    diagnostics record how many rows it saw so that can be checked.
    """

    if not scored or span_seconds <= 0:
        return (Decimal("1"), {"reason": "no rows or no span; threshold closed"})
    hours = Decimal(span_seconds) / Decimal(3600)
    budget = max(1, int((target_alerts_per_hour * hours).to_integral_value()))
    ordered = sorted(scored, key=lambda row: row.probability, reverse=True)
    chosen = ordered[min(budget, len(ordered)) - 1].probability

    if min_precision is not None:
        for index in range(min(budget, len(ordered)), 0, -1):
            window = ordered[:index]
            hits = sum(1 for row in window if row.label)
            precision = Decimal(hits) / Decimal(len(window))
            if precision >= min_precision:
                chosen = window[-1].probability
                break
        else:
            chosen = Decimal("1")

    alerts = [row for row in scored if row.probability >= chosen]
    hits = sum(1 for row in alerts if row.label)
    # A saturated model assigns the same probability to many rows, so a
    # quantile threshold can admit far more than the budget.  Reporting the
    # overshoot is not a detail: an operating point that promises four alerts
    # an hour and delivers twenty-five is the noise problem, restated.
    overshoot = len(alerts) > budget
    return (
        chosen,
        {
            "rows_considered": len(scored),
            "alert_budget": budget,
            "alerts": len(alerts),
            "budget_overshoot": overshoot,
            "overshoot_reason": (
                f"{len(alerts) - budget} extra rows tie at probability {chosen}; "
                "the score cannot separate them, so the alert budget is not "
                "enforceable at this threshold"
                if overshoot
                else ""
            ),
            "precision": (
                None
                if not alerts
                else str((Decimal(hits) / Decimal(len(alerts))).quantize(Decimal("0.0001")))
            ),
            "alerts_per_hour": str(
                (Decimal(len(alerts)) / hours).quantize(Decimal("0.01"))
            ),
        },
    )
