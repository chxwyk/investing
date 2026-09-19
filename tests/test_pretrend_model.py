"""Models, validation, the state machine, replay and training promotion.

The property under test throughout is restraint: the system must refuse to fit
on a thin sample, refuse to promote a model that does not beat a transparent
rule, refuse a random split, and refuse to say the same thing twice.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from smart_money_bot.pretrend.dataset import build_dataset, compare_features
from smart_money_bot.pretrend.features import FeatureVector, MarketSeries, PretrendState
from smart_money_bot.pretrend.model import (
    MIN_POSITIVES_FOR_MODEL,
    FeatureVersionMismatch,
    GradientBoostedTrees,
    HeuristicBaseline,
    IsotonicCalibrator,
    LogisticModel,
    Prediction,
    TrainingRow,
    can_fit_model,
    sample_supports_trees,
)
from smart_money_bot.pretrend.replay import replay, sweep_thresholds
from smart_money_bot.pretrend.states import (
    STATE_PRE_TREND,
    STATE_TRENDING_CONFIRMED,
    STATE_WATCH,
    SUPPRESSED_COOLDOWN,
    SUPPRESSED_NO_MATERIAL_CHANGE,
    SUPPRESSED_RATE_LIMIT,
    AlertGate,
    GateConfig,
    IllegalTransition,
    TokenState,
    probability_band,
    transition,
)
from smart_money_bot.pretrend.validation import (
    ScoredRow,
    brier_score,
    choose_threshold,
    precision_recall_auc,
    reliability_buckets,
    temporal_folds,
    walk_forward,
)
from smart_money_bot.pretrend_training import train_once

MINT = ("M" * 44)[:44]


def rows(count: int, *, positives: int, start: int = 1_000, step: int = 60) -> list[TrainingRow]:
    built: list[TrainingRow] = []
    for index in range(count):
        label = index < positives
        built.append(
            TrainingRow(
                mint=f"mint{index}",
                observed_at=start + index * step,
                values={"signal": Decimal("1") if label else Decimal("0")},
                label=label,
                feature_version="pretrend.v1",
            )
        )
    return built


# --- fitting restraint -------------------------------------------------------
def test_a_thin_sample_is_refused_rather_than_fitted() -> None:
    can_fit, why = can_fit_model(rows(40, positives=5))
    assert not can_fit
    assert str(MIN_POSITIVES_FOR_MODEL) in why


def test_trees_need_far_more_positives_than_a_linear_model() -> None:
    sample = rows(400, positives=50)
    assert can_fit_model(sample)[0]
    assert not sample_supports_trees(sample)

    trees = GradientBoostedTrees().fit(sample)
    assert trees.trees == [], "refusing to fit is a result, not a failure"
    prediction = trees.predict({"signal": Decimal("1")})
    assert prediction.model_version.endswith(":unfitted")


# --- logistic model ----------------------------------------------------------
def test_the_model_learns_the_signal_and_ignores_the_noise() -> None:
    random.seed(17)
    sample: list[TrainingRow] = []
    for index in range(600):
        signal = random.random()
        label = random.random() < (0.02 + 0.6 * signal**3)
        sample.append(
            TrainingRow(
                mint=f"m{index}",
                observed_at=1_000 + index * 60,
                values={
                    "signal": Decimal(str(round(signal, 4))),
                    "noise": Decimal(str(round(random.random(), 4))),
                },
                label=label,
                feature_version="pretrend.v1",
            )
        )

    model = LogisticModel(epochs=150).fit(sample)
    strong = model.predict({"signal": Decimal("0.95"), "noise": Decimal("0.5")})
    weak = model.predict({"signal": Decimal("0.05"), "noise": Decimal("0.5")})
    assert strong.probability > weak.probability

    contributions = dict(strong.reason_codes)
    assert abs(contributions["signal"]) > abs(contributions.get("noise", Decimal("0")))


def test_a_model_round_trips_through_json_unchanged() -> None:
    model = LogisticModel(epochs=40).fit(rows(200, positives=60))
    restored = LogisticModel.loads(model.dumps())
    values = {"signal": Decimal("1")}
    assert restored.predict(values).probability == model.predict(values).probability


def test_a_model_refuses_a_vector_from_a_different_feature_version() -> None:
    """A stored coefficient would silently multiply a different quantity."""

    model = LogisticModel(epochs=20).fit(rows(200, positives=60))
    vector = FeatureVector(
        mint=MINT, observed_at=1_000, feature_version="pretrend.v99", values={}
    )
    with pytest.raises(FeatureVersionMismatch):
        model.score_vector(vector)


def test_an_unfittable_model_returns_the_base_rate_not_zero() -> None:
    model = LogisticModel()
    model.trained_rows = 100
    model.trained_positives = 5
    assert model.predict({}).probability == Decimal("0.05")


def test_a_missing_value_is_learnable_as_its_own_state() -> None:
    model = LogisticModel(epochs=20).fit(rows(200, positives=60))
    prediction = model.predict({"signal": None})
    assert prediction.missing_features == 1


# --- calibration -------------------------------------------------------------
def test_isotonic_calibration_is_monotone() -> None:
    scores = [index / 100 for index in range(100)]
    labels = [index > 70 for index in range(100)]
    calibrator = IsotonicCalibrator().fit(scores, labels)
    outputs = [calibrator.transform(score) for score in scores]
    assert outputs == sorted(outputs)
    assert calibrator.transform(0.95) > calibrator.transform(0.05)


def test_reliability_buckets_expose_a_miscalibrated_model() -> None:
    scored = [
        ScoredRow(mint=f"m{index}", observed_at=index, probability=Decimal("0.8"), label=False)
        for index in range(50)
    ]
    buckets = [bucket for bucket in reliability_buckets(scored) if bucket.count]
    assert buckets[0].observed_rate == Decimal("0")
    assert buckets[0].gap < 0, "the gap must show the model was over-confident"


def test_brier_rewards_honest_uncertainty_over_confident_error() -> None:
    wrong = [ScoredRow(mint="a", observed_at=1, probability=Decimal("0.99"), label=False)]
    humble = [ScoredRow(mint="a", observed_at=1, probability=Decimal("0.5"), label=False)]
    assert brier_score(wrong) > brier_score(humble)


def test_pr_auc_is_none_without_a_positive() -> None:
    scored = [
        ScoredRow(mint=f"m{i}", observed_at=i, probability=Decimal("0.5"), label=False)
        for i in range(5)
    ]
    assert precision_recall_auc(scored) is None


# --- walk-forward ------------------------------------------------------------
def test_folds_are_chronological_and_never_random() -> None:
    sample = rows(300, positives=100, step=600)
    folds = temporal_folds(sample, fold_seconds=20_000, embargo_seconds=0)
    assert folds
    for train, test in folds:
        assert max(row.observed_at for row in train) <= min(
            row.observed_at for row in test
        )


def test_the_embargo_removes_rows_whose_labels_resolve_inside_the_test_window() -> None:
    sample = rows(300, positives=100, step=600)
    with_embargo = temporal_folds(sample, fold_seconds=20_000, embargo_seconds=1_200)
    without = temporal_folds(sample, fold_seconds=20_000, embargo_seconds=0)
    assert len(with_embargo[0][0]) < len(without[0][0])


def test_a_mint_spanning_the_boundary_is_removed_from_the_test_fold() -> None:
    recurring = [
        TrainingRow(
            mint="recurring",
            observed_at=1_000 + index * 600,
            values={"signal": Decimal("1")},
            label=index % 5 == 0,
            feature_version="pretrend.v1",
        )
        for index in range(100)
    ]
    report = walk_forward(
        recurring,
        fit=lambda train: HeuristicBaseline().fit(train),
        model_name="heuristic_baseline",
        horizon_seconds=300,
        fold_seconds=20_000,
    )
    assert report.folds == ()
    assert report.skipped_folds > 0
    assert any("contamination" in reason for reason in report.skip_reasons)


def test_pooled_precision_is_not_the_mean_of_fold_precisions() -> None:
    """Averaging lets a quiet fold with two alerts outweigh a busy one."""

    random.seed(3)
    sample: list[TrainingRow] = []
    for index in range(700):
        signal = random.random()
        sample.append(
            TrainingRow(
                mint=f"m{index}",
                observed_at=1_000 + index * 60,
                values={"signal": Decimal(str(round(signal, 4)))},
                label=random.random() < (0.01 + 0.4 * signal**4),
                feature_version="pretrend.v1",
            )
        )
    report = walk_forward(
        sample,
        fit=lambda train: LogisticModel(epochs=30).fit(train),
        model_name="logistic_v1",
        horizon_seconds=300,
        threshold=Decimal("0.3"),
        fold_seconds=25_000,
    )
    assert report.folds
    alerts = sum(fold.alerts for fold in report.folds)
    hits = sum(fold.true_positives for fold in report.folds)
    if alerts:
        assert report.pooled_precision == (
            Decimal(hits) / Decimal(alerts)
        ).quantize(Decimal("0.000001"))
    assert report.pooled_base_rate is not None


def test_the_verdict_is_allowed_to_be_bad_news() -> None:
    sample = rows(600, positives=200, step=600)
    report = walk_forward(
        sample,
        fit=lambda train: HeuristicBaseline().fit(train),
        model_name="heuristic_baseline",
        horizon_seconds=300,
        fold_seconds=40_000,
    )
    assert report.verdict in {
        "NO_FOLDS",
        "INSUFFICIENT_SAMPLE",
        "NO_EDGE",
        "WEAK_EDGE",
        "EDGE_PRESENT",
        "UNMEASURABLE",
        "LEAKAGE_DETECTED",
    }


def test_a_threshold_that_cannot_hold_the_alert_budget_says_so() -> None:
    scored = [
        ScoredRow(
            mint=f"m{index}",
            observed_at=index * 60,
            probability=Decimal("1.0") if index < 50 else Decimal("0.1"),
            label=index < 30,
        )
        for index in range(200)
    ]
    _, diagnostics = choose_threshold(
        scored, target_alerts_per_hour=Decimal("4"), span_seconds=200 * 60
    )
    assert diagnostics["budget_overshoot"]
    assert "tie" in diagnostics["overshoot_reason"]


# --- the state machine and alert budget --------------------------------------
def test_a_drifting_probability_does_not_produce_four_alerts() -> None:
    """41%, 42%, 43%, 44% is one piece of news, not four."""

    gate = AlertGate()
    state = TokenState(mint=MINT, first_seen_at=0)
    first = gate.evaluate(state, probability=Decimal("0.41"), now=100)
    assert first.send

    state = first.state
    for step, probability in enumerate(("0.42", "0.43", "0.44"), start=1):
        decision = gate.evaluate(state, probability=Decimal(probability), now=100 + step)
        assert not decision.send
        assert decision.reason == SUPPRESSED_NO_MATERIAL_CHANGE
        state = decision.state


def test_a_band_escalation_after_the_cooldown_does_re_alert() -> None:
    gate = AlertGate()
    first = gate.evaluate(TokenState(mint=MINT), probability=Decimal("0.22"), now=100)
    assert first.send
    escalated = gate.evaluate(first.state, probability=Decimal("0.60"), now=100 + 3_600)
    assert escalated.send
    assert escalated.reason == "PROBABILITY_ESCALATION"


def test_the_cooldown_blocks_even_a_real_escalation() -> None:
    gate = AlertGate(GateConfig(cooldown_seconds=1_800))
    first = gate.evaluate(TokenState(mint=MINT), probability=Decimal("0.22"), now=100)
    blocked = gate.evaluate(first.state, probability=Decimal("0.80"), now=200)
    assert not blocked.send
    assert blocked.reason == SUPPRESSED_COOLDOWN


def test_the_hourly_ceiling_is_a_ceiling_across_every_mint() -> None:
    gate = AlertGate(GateConfig(max_alerts_per_hour=2))
    sent = 0
    for index in range(6):
        decision = gate.evaluate(
            TokenState(mint=f"mint{index}"), probability=Decimal("0.5"), now=1_000 + index
        )
        sent += 1 if decision.send else 0
    assert sent == 2
    assert gate.suppressed[SUPPRESSED_RATE_LIMIT] == 4


def test_a_watch_candidate_is_classified_but_stays_silent() -> None:
    gate = AlertGate()
    decision = gate.evaluate(TokenState(mint=MINT), probability=Decimal("0.08"), now=100)
    assert not decision.send
    assert decision.state.state == STATE_WATCH


def test_the_confirmation_is_never_rate_limited() -> None:
    """It is the message that says whether the others were right."""

    gate = AlertGate(GateConfig(max_alerts_per_hour=1))
    gate.evaluate(TokenState(mint="other"), probability=Decimal("0.9"), now=1_000)
    confirmed = gate.confirm(TokenState(mint=MINT), at=1_001)
    assert confirmed.send
    assert confirmed.confirmation
    assert confirmed.state.state == STATE_TRENDING_CONFIRMED


def test_lead_time_is_measured_from_the_first_alert_and_ties_are_not_predictions() -> None:
    gate = AlertGate()
    alerted = gate.evaluate(TokenState(mint=MINT), probability=Decimal("0.5"), now=1_000)
    early = gate.confirm(alerted.state, at=1_500)
    assert early.state.lead_seconds == 500
    assert early.state.predicted

    simultaneous = AlertGate().confirm(
        TokenState(mint=MINT, first_pretrend_alert_at=2_000), at=2_000
    )
    assert simultaneous.state.lead_seconds == 0
    assert not simultaneous.state.predicted, "a tie is not a prediction"


def test_the_first_alert_facts_are_written_once() -> None:
    gate = AlertGate(GateConfig(cooldown_seconds=0))
    first = gate.evaluate(
        TokenState(mint=MINT), probability=Decimal("0.25"), now=1_000,
        market_cap_usd=Decimal("50000"),
    )
    second = gate.evaluate(
        first.state, probability=Decimal("0.80"), now=2_000,
        market_cap_usd=Decimal("250000"),
    )
    assert second.send
    assert second.state.first_pretrend_alert_at == 1_000
    assert second.state.first_pretrend_probability == Decimal("0.25")
    assert second.state.first_pretrend_market_cap_usd == Decimal("50000")


def test_an_illegal_transition_raises_rather_than_corrupting_state() -> None:
    confirmed = TokenState(mint=MINT, state=STATE_TRENDING_CONFIRMED)
    with pytest.raises(IllegalTransition):
        transition(confirmed, to=STATE_PRE_TREND, at=1_000)


def test_probability_bands_are_monotone() -> None:
    bands = [probability_band(Decimal(str(value))) for value in (0.01, 0.06, 0.15, 0.3, 0.6, 0.9)]
    assert bands == sorted(bands)
    assert bands[0] < bands[-1]


def test_the_gate_budget_survives_a_restart() -> None:
    """An in-memory cooldown resets on every redeploy; a restored one does not."""

    gate = AlertGate(GateConfig(max_alerts_per_hour=2))
    gate.restore([1_000, 1_100])
    decision = gate.evaluate(TokenState(mint=MINT), probability=Decimal("0.9"), now=1_200)
    assert not decision.send
    assert decision.reason == SUPPRESSED_RATE_LIMIT


# --- replay ------------------------------------------------------------------
class _HolderScorer:
    """Fires on holder acceleration, so the replay has something to find."""

    def predict(self, values):
        velocity = values.get("holders_velocity_1m")
        accelerating = velocity is not None and velocity > Decimal("0.05")
        probability = Decimal("0.9") if accelerating else Decimal("0.01")
        return Prediction(
            probability=probability, model_version="test", feature_version="pretrend.v1"
        )


def _accelerating_state() -> PretrendState:
    market = MarketSeries()
    for at in range(0, 1_200, 30):
        market.holders.add(at, 50 if at < 600 else 50 + (at - 600) // 5)
        market.market_cap_usd.add(at, 40_000)
    return PretrendState(mint=MINT, at=1_200, market=market)


def test_replay_alerts_before_the_entry_and_confirms_at_it() -> None:
    result = replay(
        {MINT: _accelerating_state()},
        scorer=_HolderScorer(),
        started_at=0,
        ended_at=1_200,
        tick_seconds=30,
        trend_entries={MINT: 1_000},
    )
    assert result.clean, "the replay audits its own inputs for leakage"
    assert len(result.alerts) == 1
    alert = result.alerts[0]
    assert alert.at < 1_000
    assert alert.correct
    assert result.confirmations == 1
    assert result.median_lead_seconds == alert.lead_seconds


def test_replay_never_scores_a_token_already_on_the_board() -> None:
    result = replay(
        {MINT: _accelerating_state()},
        scorer=_HolderScorer(),
        started_at=0,
        ended_at=1_200,
        tick_seconds=30,
        trend_entries={MINT: 30},  # on the board almost immediately
    )
    assert result.alerts == ()


def test_an_alert_on_a_token_that_never_trends_counts_against_precision() -> None:
    """Excluding unresolved alerts would be the cherry-picking section 79 bans."""

    result = replay(
        {MINT: _accelerating_state()},
        scorer=_HolderScorer(),
        started_at=0,
        ended_at=1_200,
        tick_seconds=30,
        trend_entries={},
    )
    assert len(result.alerts) == 1
    assert result.precision == Decimal("0")


def test_a_threshold_sweep_shows_the_precision_versus_volume_trade_off() -> None:
    sweeps = sweep_thresholds(
        {MINT: _accelerating_state()},
        scorer=_HolderScorer(),
        started_at=0,
        ended_at=1_200,
        thresholds=(Decimal("0.005"), Decimal("0.95")),
        trend_entries={MINT: 1_000},
    )
    loose, strict = sweeps
    assert loose.alerts >= strict.alerts


# --- training promotion ------------------------------------------------------
def _synthetic_vectors(*, strength: float, seed: int) -> tuple[list[FeatureVector], dict[str, int]]:
    random.seed(seed)
    vectors: list[FeatureVector] = []
    entries: dict[str, int] = {}
    base = 1_700_000_000
    at = base
    token = 0
    # Four folds' worth, sampled coarsely.  The point of these fixtures is the
    # promotion DECISION, not statistical power, so they stay small enough to
    # run in the ordinary suite.
    while at < base + 4 * 20_000:
        for _ in range(2):
            token += 1
            mint = f"mint{token}"
            signal = random.random()
            for step in range(8):
                vectors.append(
                    FeatureVector(
                        mint=mint,
                        observed_at=at + step * 60,
                        feature_version="pretrend.v1",
                        values={
                            "market_cap_usd": Decimal("60000"),
                            "token_age_seconds": Decimal(str(300 + step * 60)),
                            "new_fomo_buyer_velocity_1m": Decimal(
                                str(round(signal + random.gauss(0, 0.03), 4))
                            ),
                        },
                    )
                )
            if random.random() < (0.05 + strength * signal**3):
                entries[mint] = at + 5 * 60
        at += 300
    return vectors, entries


def test_training_refuses_to_promote_when_there_is_no_edge() -> None:
    vectors, entries = _synthetic_vectors(strength=0.0, seed=21)
    outcome = train_once(
        vectors,
        first_trending_at=entries,
        horizon_seconds=300,
        data_complete_until=max(v.observed_at for v in vectors) + 5_000,
        universe_min_usd=Decimal("20000"),
        universe_max_usd=Decimal("1000000"),
        fold_seconds=20_000,
    )
    assert not outcome.promoted
    assert outcome.refusal_reason
    assert outcome.dataset is not None and outcome.dataset.clean


def test_training_reports_the_counts_behind_every_claim() -> None:
    vectors, entries = _synthetic_vectors(strength=0.8, seed=5)
    outcome = train_once(
        vectors,
        first_trending_at=entries,
        horizon_seconds=300,
        data_complete_until=max(v.observed_at for v in vectors) + 5_000,
        universe_min_usd=Decimal("20000"),
        universe_max_usd=Decimal("1000000"),
        fold_seconds=20_000,
    )
    assert outcome.dataset is not None
    payload = outcome.to_json()
    assert payload["dataset"]["distinct_positive_mints"] >= 0
    assert payload["dataset"]["base_rate"] is not None
    if outcome.model_report is not None and outcome.model_report.folds:
        for fold in outcome.model_report.to_json()["folds"]:
            assert fold["base_rate"] is not None, "no precision without its base rate"


def test_a_leaky_dataset_is_never_trained_on() -> None:
    vectors = [
        FeatureVector(
            mint=MINT,
            observed_at=1_000,
            feature_version="pretrend.v1",
            values={"market_cap_usd": Decimal("50000"), "trending_rank": Decimal("3")},
        )
    ]
    outcome = train_once(
        vectors,
        first_trending_at={MINT: 1_200},
        horizon_seconds=300,
        data_complete_until=99_999,
        universe_min_usd=Decimal("20000"),
        universe_max_usd=Decimal("1000000"),
    )
    assert not outcome.promoted
    assert "leakage" in outcome.refusal_reason


def test_matched_controls_compare_winners_with_look_alikes() -> None:
    vectors, entries = _synthetic_vectors(strength=0.8, seed=11)
    dataset = build_dataset(
        vectors,
        first_trending_at=entries,
        horizon_seconds=300,
        data_complete_until=max(v.observed_at for v in vectors) + 5_000,
        universe_min_usd=Decimal("20000"),
        universe_max_usd=Decimal("1000000"),
    )
    assert dataset.controls is not None
    assert dataset.controls.positives > 0
    # Controls must never be drawn from mints that eventually trended.
    trended = set(entries)
    for pair in dataset.controls.pairs:
        for control in pair.controls:
            assert control.mint not in trended

    comparisons = compare_features(dataset, features=["new_fomo_buyer_velocity_1m"])
    assert comparisons[0].positive_n > 0 and comparisons[0].negative_n > 0
