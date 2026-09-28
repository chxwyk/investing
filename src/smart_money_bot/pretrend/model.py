"""Models, in the order they should be tried: baseline, then linear, then trees.

Starting with a neural network on a few thousand rows would be a way of hiding
from the actual question, which is whether *any* signal exists.  A regularized
linear model that beats a well-built heuristic on walk-forward data is evidence.
A deep model that beats it on a random split is not.

Three estimators live here, sharing one interface so the validation harness can
score them identically:

:class:`HeuristicBaseline`
    A transparent rule built from a handful of features, with its thresholds set
    from the training fold's own quantiles rather than from taste.  It exists to
    be beaten.  If the learned model cannot beat it, the honest report is that
    the learned model adds nothing — not that the heuristic is "just a baseline".

:class:`LogisticModel`
    L2-regularized logistic regression, fitted by gradient descent, with
    standardisation statistics learned on the training fold only.  Class
    imbalance is handled with sample weights rather than by resampling, so no row
    is duplicated into both folds.

:class:`GradientBoostedTrees`
    Small depth-limited regression trees on the logistic loss gradient.  It is
    only used when :func:`sample_supports_trees` says the fold has enough
    positives to fit one without memorising them.

Every estimator outputs a **probability**, and probabilities get calibrated
(:class:`IsotonicCalibrator`) and checked (Brier score, reliability buckets).
A number the operator can multiply by a base rate is worth more than a score out
of 100 that means whatever the weights happened to be that week.

Missing values are handled explicitly: each feature is mean-imputed using the
*training fold's* mean, and an accompanying ``__missing`` indicator column tells
the model the value was absent.  That way "no FOMO tape" is learnable as its own
state instead of being confused with "no FOMO buying".
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

ZERO = Decimal("0")

#: Below this many positives a learned model is not fitted at all.  Fitting one
#: anyway produces a model that memorises a handful of tokens and a backtest
#: that reports it as skill.
MIN_POSITIVES_FOR_MODEL = 30
#: Trees need more, because they can memorise faster.
MIN_POSITIVES_FOR_TREES = 150


def sigmoid(z: float) -> float:
    # Split to avoid overflow on large-magnitude inputs.
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    exp_z = math.exp(z)
    return exp_z / (1.0 + exp_z)


@dataclass(frozen=True, slots=True)
class TrainingRow:
    """One labelled example.  ``values`` may contain ``None`` for unknown."""

    mint: str
    observed_at: int
    values: Mapping[str, Decimal | None]
    label: bool
    feature_version: str = ""
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class Prediction:
    """A probability plus everything needed to audit it later."""

    probability: Decimal
    model_version: str
    feature_version: str
    #: Named, signed contributions, strongest first.
    reason_codes: tuple[tuple[str, Decimal], ...] = ()
    #: Which calibration bucket the raw score fell in.
    calibration_bucket: str = ""
    #: Training rows behind the model that produced this.
    sample_support: int = 0
    missing_features: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "probability": str(self.probability),
            "model_version": self.model_version,
            "feature_version": self.feature_version,
            "calibration_bucket": self.calibration_bucket,
            "sample_support": self.sample_support,
            "missing_features": self.missing_features,
            "reason_codes": [
                {"feature": name, "contribution": str(value)}
                for name, value in self.reason_codes
            ],
        }


class Estimator(Protocol):
    name: str
    feature_version: str
    trained_rows: int
    trained_positives: int

    def predict(self, values: Mapping[str, Decimal | None]) -> Prediction: ...


class FeatureVersionMismatch(ValueError):
    """Raised when a vector's feature version differs from the model's.

    Scoring across versions is the quiet way a model starts reading a feature
    that now means something else.  Refusing is the only safe behaviour.
    """


# --- preprocessing -----------------------------------------------------------
@dataclass
class Standardiser:
    """Mean/scale per feature, learned on the training fold only."""

    means: dict[str, float] = field(default_factory=dict)
    scales: dict[str, float] = field(default_factory=dict)
    names: tuple[str, ...] = ()

    def fit(self, rows: Sequence[TrainingRow]) -> Standardiser:
        names = sorted({name for row in rows for name in row.values})
        means: dict[str, float] = {}
        scales: dict[str, float] = {}
        for name in names:
            observed = [
                float(row.values[name])  # type: ignore[arg-type]
                for row in rows
                if row.values.get(name) is not None
            ]
            if not observed:
                means[name] = 0.0
                scales[name] = 1.0
                continue
            mean = sum(observed) / len(observed)
            variance = sum((value - mean) ** 2 for value in observed) / len(observed)
            deviation = math.sqrt(variance)
            means[name] = mean
            # A constant feature gets scale 1 so it contributes 0 after centring
            # rather than exploding.
            scales[name] = deviation if deviation > 1e-9 else 1.0
        self.names = tuple(names)
        self.means = means
        self.scales = scales
        return self

    def transform(self, values: Mapping[str, Decimal | None]) -> tuple[list[float], int]:
        """Standardise, mean-impute, and append a missing indicator per feature."""

        row: list[float] = []
        missing = 0
        for name in self.names:
            raw = values.get(name)
            if raw is None:
                row.append(0.0)  # the mean, after centring
                row.append(1.0)  # missing indicator
                missing += 1
            else:
                row.append((float(raw) - self.means[name]) / self.scales[name])
                row.append(0.0)
        return row, missing

    @property
    def expanded_names(self) -> tuple[str, ...]:
        expanded: list[str] = []
        for name in self.names:
            expanded.append(name)
            expanded.append(f"{name}__missing")
        return tuple(expanded)

    def to_json(self) -> dict[str, Any]:
        return {"names": list(self.names), "means": self.means, "scales": self.scales}

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> Standardiser:
        instance = cls()
        instance.names = tuple(payload.get("names") or ())
        instance.means = dict(payload.get("means") or {})
        instance.scales = dict(payload.get("scales") or {})
        return instance


# --- calibration -------------------------------------------------------------
@dataclass
class IsotonicCalibrator:
    """Pool-adjacent-violators isotonic regression on held-out scores.

    Isotonic rather than Platt because the raw score's relationship to the true
    rate at these base rates is rarely a clean sigmoid, and a mis-specified
    calibration curve produces confident wrong numbers, which is worse than an
    uncalibrated score honestly labelled.
    """

    thresholds: list[float] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    fitted: bool = False

    def fit(self, scores: Sequence[float], labels: Sequence[bool]) -> IsotonicCalibrator:
        if not scores or len(scores) != len(labels):
            self.fitted = False
            return self
        pairs = sorted(zip(scores, labels, strict=True), key=lambda pair: pair[0])
        blocks: list[list[float]] = [[float(label), 1.0, score] for score, label in pairs]
        # Pool adjacent violators.
        index = 0
        while index < len(blocks) - 1:
            if blocks[index][0] / blocks[index][1] > blocks[index + 1][0] / blocks[index + 1][1]:
                blocks[index][0] += blocks[index + 1][0]
                blocks[index][1] += blocks[index + 1][1]
                blocks[index][2] = blocks[index + 1][2]
                del blocks[index + 1]
                if index > 0:
                    index -= 1
            else:
                index += 1
        self.thresholds = [block[2] for block in blocks]
        self.values = [block[0] / block[1] for block in blocks]
        self.fitted = True
        return self

    def transform(self, score: float) -> float:
        if not self.fitted or not self.thresholds:
            return score
        for threshold, value in zip(self.thresholds, self.values, strict=True):
            if score <= threshold:
                return value
        return self.values[-1]

    def to_json(self) -> dict[str, Any]:
        return {
            "thresholds": self.thresholds,
            "values": self.values,
            "fitted": self.fitted,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> IsotonicCalibrator:
        instance = cls()
        instance.thresholds = list(payload.get("thresholds") or [])
        instance.values = list(payload.get("values") or [])
        instance.fitted = bool(payload.get("fitted"))
        return instance


def calibration_bucket(probability: float) -> str:
    """Coarse label for a probability, so outcomes can be pooled per bucket."""

    for low, high in ((0.0, 0.01), (0.01, 0.05), (0.05, 0.1), (0.1, 0.25), (0.25, 0.5)):
        if low <= probability < high:
            return f"{low:.0%}-{high:.0%}"
    return "50%-100%"


# --- the heuristic baseline --------------------------------------------------
#: The features the heuristic is allowed to look at.  Deliberately few, and all
#: of them are "is this accelerating relative to its peers", because that is the
#: only shape a transparent rule can express without becoming a hidden model.
BASELINE_FEATURES: tuple[str, ...] = (
    "new_fomo_buyer_velocity_1m",
    "unique_buyers_velocity_1m",
    "volume_over_market_cap_5m",
    "holders_velocity_3m",
    "independent_quality_buyers",
)


@dataclass
class HeuristicBaseline:
    """A transparent rule whose thresholds come from the training fold.

    Scores the fraction of its features that exceed the training fold's own
    upper quantile.  It has no tuned weights, so it cannot be overfitted — which
    is exactly what makes beating it meaningful.
    """

    name: str = "heuristic_baseline"
    feature_version: str = ""
    quantile: float = 0.9
    thresholds: dict[str, float] = field(default_factory=dict)
    trained_rows: int = 0
    trained_positives: int = 0
    features: tuple[str, ...] = BASELINE_FEATURES

    def fit(self, rows: Sequence[TrainingRow]) -> HeuristicBaseline:
        self.trained_rows = len(rows)
        self.trained_positives = sum(1 for row in rows if row.label)
        if rows:
            self.feature_version = rows[0].feature_version
        for name in self.features:
            observed = sorted(
                float(row.values[name])  # type: ignore[arg-type]
                for row in rows
                if row.values.get(name) is not None
            )
            if not observed:
                continue
            index = min(
                len(observed) - 1, int(round(self.quantile * (len(observed) - 1)))
            )
            self.thresholds[name] = observed[index]
        return self

    def predict(self, values: Mapping[str, Decimal | None]) -> Prediction:
        hits: list[tuple[str, Decimal]] = []
        considered = 0
        for name in self.features:
            threshold = self.thresholds.get(name)
            raw = values.get(name)
            if threshold is None or raw is None:
                continue
            considered += 1
            if float(raw) >= threshold:
                hits.append((name, Decimal("1")))
        if considered == 0:
            # No usable input is not a zero probability, it is no opinion.  The
            # base rate is the honest answer.
            base = Decimal(self.trained_positives) / Decimal(max(self.trained_rows, 1))
            return Prediction(
                probability=base.quantize(Decimal("0.000001")),
                model_version=self.name,
                feature_version=self.feature_version,
                sample_support=self.trained_rows,
                calibration_bucket=calibration_bucket(float(base)),
            )
        share = len(hits) / considered
        base_rate = self.trained_positives / max(self.trained_rows, 1)
        # A crude but monotone mapping: every satisfied condition multiplies the
        # base rate, bounded so the rule cannot claim certainty.
        probability = min(0.95, base_rate * (1.0 + 4.0 * share) if base_rate > 0 else share * 0.1)
        return Prediction(
            probability=Decimal(str(round(probability, 6))),
            model_version=self.name,
            feature_version=self.feature_version,
            reason_codes=tuple(hits),
            sample_support=self.trained_rows,
            calibration_bucket=calibration_bucket(probability),
        )


# --- logistic regression -----------------------------------------------------
@dataclass
class LogisticModel:
    """L2-regularized logistic regression fitted by full-batch gradient descent.

    Full-batch rather than stochastic so a fit is deterministic given its inputs:
    a model whose coefficients change between identical runs makes every
    comparison between folds unreproducible.
    """

    name: str = "logistic_v1"
    feature_version: str = ""
    l2: float = 1.0
    learning_rate: float = 0.1
    epochs: int = 300
    weights: list[float] = field(default_factory=list)
    bias: float = 0.0
    standardiser: Standardiser = field(default_factory=Standardiser)
    calibrator: IsotonicCalibrator = field(default_factory=IsotonicCalibrator)
    trained_rows: int = 0
    trained_positives: int = 0
    enforce_feature_version: bool = True

    def fit(
        self,
        rows: Sequence[TrainingRow],
        *,
        calibration_rows: Sequence[TrainingRow] = (),
    ) -> LogisticModel:
        self.trained_rows = len(rows)
        self.trained_positives = sum(1 for row in rows if row.label)
        if not rows:
            return self
        self.feature_version = rows[0].feature_version

        self.standardiser = Standardiser().fit(rows)
        design = [self.standardiser.transform(row.values)[0] for row in rows]
        labels = [1.0 if row.label else 0.0 for row in rows]

        # Class weights rather than resampling: resampling would duplicate rows,
        # and a duplicated positive is the same token counted twice.
        positives = max(1, self.trained_positives)
        negatives = max(1, len(rows) - self.trained_positives)
        positive_weight = len(rows) / (2.0 * positives)
        negative_weight = len(rows) / (2.0 * negatives)
        weights = [
            row.weight * (positive_weight if row.label else negative_weight)
            for row in rows
        ]

        width = len(design[0]) if design else 0
        self.weights = [0.0] * width
        self.bias = 0.0
        total_weight = sum(weights) or 1.0

        for _ in range(self.epochs):
            gradient = [0.0] * width
            bias_gradient = 0.0
            for features, label, weight in zip(design, labels, weights, strict=True):
                z = self.bias + sum(
                    coefficient * value
                    for coefficient, value in zip(self.weights, features, strict=True)
                )
                error = (sigmoid(z) - label) * weight
                bias_gradient += error
                for index, value in enumerate(features):
                    gradient[index] += error * value
            for index in range(width):
                penalty = self.l2 * self.weights[index]
                self.weights[index] -= self.learning_rate * (
                    gradient[index] / total_weight + penalty / total_weight
                )
            self.bias -= self.learning_rate * (bias_gradient / total_weight)

        if calibration_rows:
            scores = [self._raw_score(row.values) for row in calibration_rows]
            self.calibrator = IsotonicCalibrator().fit(
                scores, [row.label for row in calibration_rows]
            )
        return self

    def _raw_score(self, values: Mapping[str, Decimal | None]) -> float:
        features, _ = self.standardiser.transform(values)
        if not self.weights:
            return 0.0
        z = self.bias + sum(
            coefficient * value
            for coefficient, value in zip(self.weights, features, strict=True)
        )
        return sigmoid(z)

    def predict(self, values: Mapping[str, Decimal | None]) -> Prediction:
        features, missing = self.standardiser.transform(values)
        if not self.weights:
            base = Decimal(self.trained_positives) / Decimal(max(self.trained_rows, 1))
            return Prediction(
                probability=base.quantize(Decimal("0.000001")),
                model_version=self.name,
                feature_version=self.feature_version,
                sample_support=self.trained_rows,
                missing_features=missing,
            )
        z = self.bias + sum(
            coefficient * value
            for coefficient, value in zip(self.weights, features, strict=True)
        )
        raw = sigmoid(z)
        calibrated = self.calibrator.transform(raw)

        contributions = sorted(
            (
                (name, coefficient * value)
                for name, coefficient, value in zip(
                    self.standardiser.expanded_names, self.weights, features, strict=True
                )
                if abs(coefficient * value) > 1e-9
            ),
            key=lambda pair: abs(pair[1]),
            reverse=True,
        )[:8]

        return Prediction(
            probability=Decimal(str(round(calibrated, 6))),
            model_version=self.name,
            feature_version=self.feature_version,
            reason_codes=tuple(
                (name, Decimal(str(round(value, 6)))) for name, value in contributions
            ),
            calibration_bucket=calibration_bucket(calibrated),
            sample_support=self.trained_rows,
            missing_features=missing,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "feature_version": self.feature_version,
            "l2": self.l2,
            "learning_rate": self.learning_rate,
            "epochs": self.epochs,
            "weights": self.weights,
            "bias": self.bias,
            "standardiser": self.standardiser.to_json(),
            "calibrator": self.calibrator.to_json(),
            "trained_rows": self.trained_rows,
            "trained_positives": self.trained_positives,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> LogisticModel:
        model = cls(
            name=str(payload.get("name") or "logistic_v1"),
            feature_version=str(payload.get("feature_version") or ""),
            l2=float(payload.get("l2", 1.0)),
            learning_rate=float(payload.get("learning_rate", 0.1)),
            epochs=int(payload.get("epochs", 300)),
        )
        model.weights = [float(value) for value in payload.get("weights") or []]
        model.bias = float(payload.get("bias", 0.0))
        model.standardiser = Standardiser.from_json(payload.get("standardiser") or {})
        model.calibrator = IsotonicCalibrator.from_json(payload.get("calibrator") or {})
        model.trained_rows = int(payload.get("trained_rows", 0))
        model.trained_positives = int(payload.get("trained_positives", 0))
        return model

    def dumps(self) -> str:
        return json.dumps(self.to_json(), separators=(",", ":"), sort_keys=True)

    @classmethod
    def loads(cls, payload: str) -> LogisticModel:
        return cls.from_json(json.loads(payload))

    def score_vector(self, vector: Any) -> Prediction:
        """Score a :class:`~smart_money_bot.pretrend.features.FeatureVector`.

        Refuses a vector whose feature version differs from the training one.
        """

        version = getattr(vector, "feature_version", "")
        if (
            self.enforce_feature_version
            and self.feature_version
            and version
            and version != self.feature_version
        ):
            raise FeatureVersionMismatch(
                f"model was trained on {self.feature_version!r} but the vector is "
                f"{version!r}; refusing to score across feature versions"
            )
        return self.predict(getattr(vector, "values", {}))


# --- gradient-boosted trees --------------------------------------------------
def sample_supports_trees(rows: Sequence[TrainingRow]) -> bool:
    """Whether the fold has enough positives to justify a tree model at all."""

    return sum(1 for row in rows if row.label) >= MIN_POSITIVES_FOR_TREES


@dataclass
class _TreeNode:
    feature: int | None = None
    threshold: float = 0.0
    left: _TreeNode | None = None
    right: _TreeNode | None = None
    value: float = 0.0

    def predict(self, features: Sequence[float]) -> float:
        if self.feature is None or self.left is None or self.right is None:
            return self.value
        branch = self.left if features[self.feature] <= self.threshold else self.right
        return branch.predict(features)


def _fit_tree(
    design: Sequence[Sequence[float]],
    residuals: Sequence[float],
    *,
    indices: Sequence[int],
    depth: int,
    max_depth: int,
    min_samples: int,
    candidate_features: Sequence[int],
) -> _TreeNode:
    if depth >= max_depth or len(indices) < 2 * min_samples:
        mean = sum(residuals[index] for index in indices) / max(len(indices), 1)
        return _TreeNode(value=mean)

    best_gain = 0.0
    best_feature: int | None = None
    best_threshold = 0.0
    parent_mean = sum(residuals[index] for index in indices) / len(indices)
    parent_sse = sum((residuals[index] - parent_mean) ** 2 for index in indices)

    for feature in candidate_features:
        values = sorted({design[index][feature] for index in indices})
        if len(values) < 2:
            continue
        # A handful of evenly spaced candidate splits keeps the fit bounded
        # without materially changing the tree at this depth.
        step = max(1, len(values) // 8)
        for position in range(step, len(values), step):
            threshold = values[position]
            left = [index for index in indices if design[index][feature] <= threshold]
            right = [index for index in indices if design[index][feature] > threshold]
            if len(left) < min_samples or len(right) < min_samples:
                continue
            left_mean = sum(residuals[index] for index in left) / len(left)
            right_mean = sum(residuals[index] for index in right) / len(right)
            sse = sum((residuals[index] - left_mean) ** 2 for index in left) + sum(
                (residuals[index] - right_mean) ** 2 for index in right
            )
            gain = parent_sse - sse
            if gain > best_gain:
                best_gain = gain
                best_feature = feature
                best_threshold = threshold

    if best_feature is None:
        return _TreeNode(value=parent_mean)

    left_indices = [
        index for index in indices if design[index][best_feature] <= best_threshold
    ]
    right_indices = [
        index for index in indices if design[index][best_feature] > best_threshold
    ]
    return _TreeNode(
        feature=best_feature,
        threshold=best_threshold,
        left=_fit_tree(
            design,
            residuals,
            indices=left_indices,
            depth=depth + 1,
            max_depth=max_depth,
            min_samples=min_samples,
            candidate_features=candidate_features,
        ),
        right=_fit_tree(
            design,
            residuals,
            indices=right_indices,
            depth=depth + 1,
            max_depth=max_depth,
            min_samples=min_samples,
            candidate_features=candidate_features,
        ),
    )


@dataclass
class GradientBoostedTrees:
    """Depth-limited boosted trees on the logistic loss.  Used only when justified."""

    name: str = "gbt_v1"
    feature_version: str = ""
    rounds: int = 40
    learning_rate: float = 0.1
    max_depth: int = 3
    min_samples: int = 20
    max_features: int = 24
    seed: int = 20240101
    trees: list[_TreeNode] = field(default_factory=list)
    base_score: float = 0.0
    standardiser: Standardiser = field(default_factory=Standardiser)
    calibrator: IsotonicCalibrator = field(default_factory=IsotonicCalibrator)
    trained_rows: int = 0
    trained_positives: int = 0
    used_features: tuple[str, ...] = ()

    def fit(
        self,
        rows: Sequence[TrainingRow],
        *,
        calibration_rows: Sequence[TrainingRow] = (),
    ) -> GradientBoostedTrees:
        self.trained_rows = len(rows)
        self.trained_positives = sum(1 for row in rows if row.label)
        if not rows or not sample_supports_trees(rows):
            # Refusing to fit is a result, not a failure.  A tree model on 40
            # positives memorises them.
            return self
        self.feature_version = rows[0].feature_version
        self.standardiser = Standardiser().fit(rows)
        design = [self.standardiser.transform(row.values)[0] for row in rows]
        labels = [1.0 if row.label else 0.0 for row in rows]
        width = len(design[0]) if design else 0
        if width == 0:
            return self

        rate = sum(labels) / len(labels)
        rate = min(max(rate, 1e-6), 1 - 1e-6)
        self.base_score = math.log(rate / (1 - rate))

        rng = random.Random(self.seed)
        all_features = list(range(width))
        candidates = (
            all_features
            if width <= self.max_features
            else rng.sample(all_features, self.max_features)
        )
        self.used_features = tuple(
            self.standardiser.expanded_names[index] for index in candidates
        )

        scores = [self.base_score] * len(rows)
        indices = list(range(len(rows)))
        for _ in range(self.rounds):
            residuals = [
                labels[index] - sigmoid(scores[index]) for index in range(len(rows))
            ]
            tree = _fit_tree(
                design,
                residuals,
                indices=indices,
                depth=0,
                max_depth=self.max_depth,
                min_samples=self.min_samples,
                candidate_features=candidates,
            )
            self.trees.append(tree)
            for index in indices:
                scores[index] += self.learning_rate * tree.predict(design[index])

        if calibration_rows:
            raw = [self._raw_score(row.values) for row in calibration_rows]
            self.calibrator = IsotonicCalibrator().fit(
                raw, [row.label for row in calibration_rows]
            )
        return self

    def _raw_score(self, values: Mapping[str, Decimal | None]) -> float:
        if not self.trees:
            return sigmoid(self.base_score)
        features, _ = self.standardiser.transform(values)
        score = self.base_score + self.learning_rate * sum(
            tree.predict(features) for tree in self.trees
        )
        return sigmoid(score)

    def predict(self, values: Mapping[str, Decimal | None]) -> Prediction:
        _, missing = self.standardiser.transform(values)
        if not self.trees:
            base = Decimal(self.trained_positives) / Decimal(max(self.trained_rows, 1))
            return Prediction(
                probability=base.quantize(Decimal("0.000001")),
                model_version=f"{self.name}:unfitted",
                feature_version=self.feature_version,
                sample_support=self.trained_rows,
                missing_features=missing,
            )
        raw = self._raw_score(values)
        calibrated = self.calibrator.transform(raw)
        return Prediction(
            probability=Decimal(str(round(calibrated, 6))),
            model_version=self.name,
            feature_version=self.feature_version,
            calibration_bucket=calibration_bucket(calibrated),
            sample_support=self.trained_rows,
            missing_features=missing,
        )


def can_fit_model(rows: Sequence[TrainingRow]) -> tuple[bool, str]:
    """Whether a learned model should be fitted on this fold, and why not."""

    positives = sum(1 for row in rows if row.label)
    if positives < MIN_POSITIVES_FOR_MODEL:
        return (
            False,
            f"{positives} positives is below the {MIN_POSITIVES_FOR_MODEL}-positive floor",
        )
    if len(rows) - positives < MIN_POSITIVES_FOR_MODEL:
        return (False, "too few negatives to fit against")
    return (True, "")
