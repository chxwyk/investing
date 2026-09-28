"""Features, windows, affinity, independence and leakage.

The theme running through this file: a number that cannot be computed must come
back as UNKNOWN, a number computed from the future must be impossible to
compute at all, and a rate computed from three observations must not outrank one
computed from two hundred.
"""

from __future__ import annotations

from decimal import Decimal

from smart_money_bot.pretrend.activity import (
    ACTIVITY_BUY,
    ACTIVITY_SELL,
    ACTIVITY_THESIS,
    ActivityEvent,
    ActivityTape,
    activity_window_features,
)
from smart_money_bot.pretrend.affinity import (
    AffinityObservation,
    baseline_rate,
    build_affinity,
    build_population,
    quality_weight,
    rank_actors,
    shrunk_rate,
    wilson_interval,
)
from smart_money_bot.pretrend.cohorts import (
    AGE_COHORT_UNKNOWN,
    MC_COHORT_UNKNOWN,
    PopulationSnapshot,
    age_cohort,
    in_universe,
    market_cap_cohort,
)
from smart_money_bot.pretrend.disagreement import ProviderReading, reconcile
from smart_money_bot.pretrend.features import (
    FEATURE_VERSION,
    MarketSeries,
    PretrendState,
    build_features,
    feature_names,
)
from smart_money_bot.pretrend.forensics import truncate_state
from smart_money_bot.pretrend.independence import Arrival, build_independence
from smart_money_bot.pretrend.labels import (
    INELIGIBLE_ALREADY_TRENDING,
    label_observation,
    summarise_labels,
)
from smart_money_bot.pretrend.leakage import (
    DUPLICATE_OBSERVATION,
    FUTURE_OBSERVATION,
    FUTURE_SOURCE_TIMESTAMP,
    LABEL_LEAKAGE,
    RETROACTIVE_UPDATE,
    SPLIT_CONTAMINATION,
    TEMPORAL_DISORDER,
    audit_dataset,
    check_duplicates,
    check_feature_names,
    check_retroactive_updates,
    check_row_timestamps,
    check_series_bounds,
    check_split,
)
from smart_money_bot.pretrend.windows import (
    Series,
    counter_dynamics,
    first_acceleration_at,
    level_dynamics,
    sum_dynamics,
)

MINT = ("M" * 44)[:44]
OTHER = ("N" * 44)[:44]


def buy(index: int, at: int, trader: str, usd: str = "25") -> ActivityEvent:
    return ActivityEvent(
        event_id=f"e{index}",
        mint=MINT,
        occurred_at=at,
        event_type=ACTIVITY_BUY,
        trader_id=trader,
        amount_usd=Decimal(usd),
    )


# --- windows -----------------------------------------------------------------
def test_a_window_never_reads_past_its_own_end() -> None:
    series = Series("buys")
    for at in range(0, 400, 10):
        series.add(at, 1)

    assert series.latest(200).at == 200
    assert all(sample.at <= 200 for sample in series.before(200))
    assert all(sample.at <= 150 for sample in series.slice(90, 150))


def test_counter_velocity_and_acceleration_describe_a_burst() -> None:
    series = Series("buys")
    for at in range(0, 120, 20):  # slow: 1 per 20s
        series.add(at, 1)
    for at in range(120, 180, 5):  # fast: 1 per 5s
        series.add(at, 1)

    calm = counter_dynamics(series, end=120, seconds=60)
    burst = counter_dynamics(series, end=180, seconds=60)

    assert burst.level > calm.level
    assert burst.velocity > calm.velocity
    assert burst.acceleration > 0
    assert burst.classify() == "ACCELERATING"


def test_a_flat_series_is_stable_not_accelerating() -> None:
    series = Series("buys")
    for at in range(0, 600, 10):
        series.add(at, 1)
    assert counter_dynamics(series, end=500, seconds=60).classify() == "STABLE"


def test_a_missing_comparison_is_unknown_not_stable() -> None:
    """UNKNOWN and STABLE justify different actions and must stay distinct."""

    assert counter_dynamics(Series("x"), end=100, seconds=60).classify() == "UNKNOWN"


def test_growth_from_zero_is_unknown_rather_than_an_enormous_ratio() -> None:
    """A synthetic huge ratio would top every percentile it entered."""

    series = Series("buys")
    for at in range(130, 190, 10):
        series.add(at, 1)
    dynamics = counter_dynamics(series, end=180, seconds=60)
    # The prior window (60, 120] holds nothing at all, so there is no ratio to
    # report -- as opposed to a ratio of "everything over nothing".
    assert dynamics.prior_samples == 0
    assert dynamics.ratio_to_prior is None


def test_level_velocity_divides_by_real_elapsed_time_not_the_nominal_window() -> None:
    series = Series("holders")
    series.add(0, 100)
    series.add(90, 190)
    dynamics = level_dynamics(series, end=90, seconds=120)
    # 90 units over the 90s actually between the readings, not over 120s.
    assert dynamics.velocity == Decimal("1")


def test_first_acceleration_ignores_a_series_own_birth() -> None:
    """Otherwise every metric 'moves first', at the moment collection began."""

    flat = Series("x")
    for at in range(0, 600, 10):
        flat.add(at, 1)
    assert first_acceleration_at(flat, until=600) is None

    spiking = Series("y")
    for at in range(0, 300, 20):
        spiking.add(at, 1)
    for at in range(300, 360, 2):
        spiking.add(at, 1)
    onset = first_acceleration_at(spiking, until=400)
    assert onset is not None and onset >= 300


def test_sum_dynamics_adds_notional_rather_than_counting_events() -> None:
    series = Series("usd")
    series.add(10, 100)
    series.add(20, 400)
    assert sum_dynamics(series, end=60, seconds=60).level == Decimal("500")


# --- FOMO activity -----------------------------------------------------------
def test_duplicate_activity_events_are_counted_once() -> None:
    """A provider retry must not double a buy, or every velocity inflates."""

    tape = ActivityTape(MINT)
    event = buy(1, 1_000, "u1")
    assert tape.add(event)
    assert not tape.add(event)
    assert len(tape) == 1


def test_a_foreign_mint_cannot_enter_another_tokens_tape() -> None:
    tape = ActivityTape(MINT)
    foreign = ActivityEvent(
        event_id="x", mint=OTHER, occurred_at=1_000, event_type=ACTIVITY_BUY
    )
    assert not tape.add(foreign)
    assert len(tape) == 0


def test_activity_windows_count_buyers_sellers_and_new_arrivals() -> None:
    tape = ActivityTape(MINT)
    for index in range(6):
        tape.add(buy(index, 1_000 + index * 5, f"u{index}"))
    tape.add(
        ActivityEvent(
            event_id="s1",
            mint=MINT,
            occurred_at=1_020,
            event_type=ACTIVITY_SELL,
            trader_id="u0",
            amount_usd=Decimal("10"),
        )
    )

    block = activity_window_features(tape, at=1_040, window_seconds=60)
    assert block.fomo_buys == 6
    assert block.fomo_sells == 1
    assert block.unique_fomo_buyers == 6
    assert block.new_fomo_buyers == 6
    assert block.fomo_buy_usd == Decimal("150")
    assert block.fomo_net_buy_usd == Decimal("140")
    assert block.fomo_buy_sell_tx_ratio == Decimal("6")


def test_a_one_sided_tape_reports_unknown_not_an_infinite_ratio() -> None:
    tape = ActivityTape(MINT)
    for index in range(4):
        tape.add(buy(index, 1_000 + index, f"u{index}"))
    block = activity_window_features(tape, at=1_020, window_seconds=60)
    assert block.fomo_buy_sell_tx_ratio is None


def test_thesis_windows_track_authors_and_velocity() -> None:
    tape = ActivityTape(MINT)
    for index in range(3):
        tape.add(
            ActivityEvent(
                event_id=f"t{index}",
                mint=MINT,
                occurred_at=1_000 + index * 10,
                event_type=ACTIVITY_THESIS,
                trader_id=f"author{index}",
                thesis_text="it does a thing",
            )
        )
    block = activity_window_features(tape, at=1_040, window_seconds=60)
    assert block.fomo_thesis_count == 3
    assert block.unique_thesis_authors == 3
    assert block.thesis_velocity is not None and block.thesis_velocity > 0


def test_buys_without_a_usd_amount_are_reported_not_treated_as_zero() -> None:
    tape = ActivityTape(MINT)
    tape.add(
        ActivityEvent(
            event_id="a", mint=MINT, occurred_at=1_000, event_type=ACTIVITY_BUY, trader_id="u"
        )
    )
    block = activity_window_features(tape, at=1_010, window_seconds=60)
    assert block.buys_missing_amount == 1
    assert block.fomo_buy_usd is None, "missing notional must not become $0"


def test_an_activity_event_without_a_mint_is_rejected() -> None:
    assert ActivityEvent.from_payload({"symbol": "CAT", "timestamp": 1}) is None


# --- affinity ----------------------------------------------------------------
def test_three_of_three_does_not_beat_eighty_of_one_hundred_and_twenty() -> None:
    """The headline anti-selection-bias property."""

    baseline = Decimal("0.01")
    lucky = shrunk_rate(hits=3, observations=3, baseline=baseline)
    solid = shrunk_rate(hits=80, observations=120, baseline=baseline)
    assert lucky is not None and solid is not None
    assert solid > lucky


def test_a_thin_record_is_never_ranked() -> None:
    baselines = {300: Decimal("0.01")}
    thin = build_affinity(
        "lucky",
        [
            AffinityObservation(actor_id="lucky", mint=MINT, observed_at=t, trend_entered_at=t + 60)
            for t in (100, 200, 300)
        ],
        baselines=baselines,
    )
    assert not thin.statistically_meaningful
    assert thin.rank_key(300) == Decimal("0")


def test_an_action_after_the_board_entry_is_not_a_hit() -> None:
    """Counting a reaction as a prediction manufactures spectacular affinity."""

    late = AffinityObservation(
        actor_id="a", mint=MINT, observed_at=1_500, trend_entered_at=1_000
    )
    assert late.lead_seconds == -500
    assert not late.hit(300)


def test_a_token_that_never_trended_is_a_negative_not_missing_data() -> None:
    rows = [
        AffinityObservation(actor_id="a", mint=MINT, observed_at=100, trend_entered_at=200),
        AffinityObservation(actor_id="a", mint=OTHER, observed_at=100, trend_entered_at=None),
    ]
    assert baseline_rate(rows, horizon_seconds=300) == Decimal("0.5")


def test_wilson_intervals_are_wide_when_the_sample_is_thin() -> None:
    thin_low, thin_high = wilson_interval(hits=3, observations=3)
    thick_low, thick_high = wilson_interval(hits=80, observations=120)
    assert (thin_high - thin_low) > (thick_high - thick_low)


def test_an_unmeasured_actor_weighs_exactly_one() -> None:
    """'Not measured' and 'measured as average' must contribute identically."""

    assert quality_weight(None) == Decimal("1")


def test_ranking_prefers_a_large_sample_over_a_lucky_streak() -> None:
    rows: list[AffinityObservation] = []
    for index in range(3):
        rows.append(
            AffinityObservation(
                actor_id="lucky", mint=f"{index}", observed_at=index, trend_entered_at=index + 60
            )
        )
    for index in range(120):
        rows.append(
            AffinityObservation(
                actor_id="solid",
                mint=f"s{index}",
                observed_at=index,
                trend_entered_at=index + 60 if index < 80 else None,
            )
        )
    for index in range(400):
        rows.append(
            AffinityObservation(actor_id=f"noise{index}", mint=f"n{index}", observed_at=index)
        )

    records, _ = build_population(rows)
    ranked = rank_actors(records, limit=5)
    assert ranked[0].actor_id == "solid"
    assert "lucky" not in {record.actor_id for record in ranked}


# --- independence ------------------------------------------------------------
def test_a_burst_of_followers_counts_as_one_independent_buyer() -> None:
    arrivals = [Arrival("leader", 1_000, Decimal("50"), weight=Decimal("3"))] + [
        Arrival(f"f{index}", 1_004 + index * 3, Decimal("10"), weight=Decimal("3"))
        for index in range(4)
    ]
    profile = build_independence(MINT, arrivals, at=1_100)
    assert profile.raw_buyers == 5
    assert profile.independent_quality_buyers == 1
    assert profile.possible_follow_cluster_count == 1


def test_spread_out_arrivals_all_count_as_independent() -> None:
    arrivals = [
        Arrival(f"s{index}", 1_000 + index * 120, Decimal("20"), weight=Decimal("3"))
        for index in range(5)
    ]
    profile = build_independence(MINT, arrivals, at=1_800)
    assert profile.independent_quality_buyers == 5
    assert profile.possible_follow_cluster_count == 0


def test_wallets_sharing_a_funder_are_one_independent_actor() -> None:
    """Five wallets with one funding source are not five confirmations."""

    arrivals = [
        Arrival(f"w{index}", 1_000 + index * 300, Decimal("20"), weight=Decimal("3"))
        for index in range(5)
    ]
    lookup = {f"w{index}": "funder-1" for index in range(5)}
    profile = build_independence(MINT, arrivals, at=3_000, cluster_lookup=lookup)
    assert profile.independent_quality_buyers == 1
    assert profile.distinct_source_clusters == 1


def test_concentration_is_unknown_when_no_amount_was_reported() -> None:
    arrivals = [Arrival(f"w{index}", 1_000 + index, None) for index in range(3)]
    assert build_independence(MINT, arrivals, at=1_100).buyer_concentration is None


# --- cohorts and universe ----------------------------------------------------
def test_cohorts_keep_unknown_values_unknown() -> None:
    assert market_cap_cohort(None) == MC_COHORT_UNKNOWN
    assert age_cohort(None) == AGE_COHORT_UNKNOWN
    assert market_cap_cohort(Decimal("30000")) == "MC_20K_50K"
    assert age_cohort(90) == "AGE_LT_2M"


def test_a_token_with_no_market_cap_is_not_in_the_universe() -> None:
    """Admitting it would fill the dataset with undefined cohorts and baselines."""

    assert not in_universe(None)
    assert in_universe(Decimal("50000"))
    assert not in_universe(Decimal("5000"))


def test_percentiles_use_midrank_so_ties_do_not_all_sit_at_the_top() -> None:
    snapshot = PopulationSnapshot(
        metric="buyers",
        at=100,
        values={"a": Decimal("1"), "b": Decimal("9"), "c": Decimal("9")},
    )
    assert snapshot.percentile("b") == snapshot.percentile("c")
    assert snapshot.percentile("b") < Decimal("100")


# --- provider disagreement ---------------------------------------------------
def test_disagreeing_providers_produce_the_median_and_a_flag() -> None:
    result = reconcile(
        "market_cap_usd",
        [
            ProviderReading("fomo", Decimal("61000")),
            ProviderReading("dex", Decimal("74000")),
            ProviderReading("gmgn", Decimal("68000")),
        ],
    )
    assert result.value == Decimal("68000"), "never the most flattering value"
    assert result.confidence == "DISPUTED"
    assert len(result.readings) == 3, "every reading is kept"


def test_one_provider_is_never_reported_as_agreement() -> None:
    single = reconcile("mc", [ProviderReading("a", Decimal("100"))])
    assert single.confidence == "SINGLE_SOURCE"
    assert single.spread is None


def test_a_stale_reading_is_dropped_rather_than_treated_as_a_dissent() -> None:
    result = reconcile(
        "mc",
        [
            ProviderReading("fresh", Decimal("100"), source_at=990, received_at=1_000),
            ProviderReading("stale", Decimal("500"), source_at=100, received_at=1_000),
        ],
        max_staleness_seconds=60,
    )
    assert result.value == Decimal("100")
    assert result.providers == ("fresh",)


# --- labels ------------------------------------------------------------------
def test_a_mint_already_on_the_board_is_not_a_prediction_opportunity() -> None:
    """The easiest way to fake 95% precision is to score tokens already trending."""

    labelled = label_observation(
        mint=MINT,
        observed_at=1_500,
        first_trending_at=1_000,
        market_cap_usd=Decimal("50000"),
        universe_min_usd=Decimal("20000"),
        universe_max_usd=Decimal("1000000"),
        data_complete_until=99_999,
    )
    assert not labelled.eligible
    assert labelled.ineligible_reason == INELIGIBLE_ALREADY_TRENDING
    assert labelled.labels == {}


def test_horizons_resolve_on_the_right_side_of_the_boundary() -> None:
    labelled = label_observation(
        mint=MINT,
        observed_at=1_000,
        first_trending_at=1_300,  # exactly 300s later
        market_cap_usd=Decimal("50000"),
        universe_min_usd=Decimal("20000"),
        universe_max_usd=Decimal("1000000"),
        data_complete_until=99_999,
    )
    assert labelled.positive_at(120) is False
    assert labelled.positive_at(300) is True, "T+H is inside the horizon"
    assert labelled.positive_at(600) is True


def test_an_unresolvable_horizon_is_censored_not_negative() -> None:
    """Calling it negative teaches the model the newest rows never trend."""

    labelled = label_observation(
        mint=MINT,
        observed_at=1_000,
        first_trending_at=None,
        market_cap_usd=Decimal("50000"),
        universe_min_usd=Decimal("20000"),
        universe_max_usd=Decimal("1000000"),
        data_complete_until=1_400,
    )
    assert labelled.positive_at(120) is False
    assert labelled.label(600).censored
    assert labelled.label(600).positive is None
    assert not labelled.label(600).usable


def test_label_stats_carry_the_base_rate_and_refuse_to_claim_sufficiency() -> None:
    sets = [
        label_observation(
            mint=f"m{index}",
            observed_at=1_000,
            first_trending_at=1_100 if index < 3 else None,
            market_cap_usd=Decimal("50000"),
            universe_min_usd=Decimal("20000"),
            universe_max_usd=Decimal("1000000"),
            data_complete_until=99_999,
        )
        for index in range(20)
    ]
    stats = summarise_labels(sets)[300]
    assert stats.positives == 3
    assert stats.usable == 20
    assert stats.base_rate == Decimal("0.15")
    assert not stats.sufficient


# --- leakage -----------------------------------------------------------------
def test_feature_names_that_could_only_be_known_afterwards_are_rejected() -> None:
    findings = check_feature_names(
        ["fomo_buys_1m", "trending_rank", "peak_market_cap_usd", "future_price"]
    )
    assert {finding.kind for finding in findings} == {LABEL_LEAKAGE}
    assert len(findings) == 3


def test_the_real_feature_list_contains_no_leaky_names() -> None:
    assert check_feature_names(feature_names()) == ()


def test_a_source_timestamp_after_the_decision_is_leakage() -> None:
    findings = check_row_timestamps(
        mint=MINT, observed_at=1_000, source_timestamps={"holders": 1_050}
    )
    assert findings[0].kind == FUTURE_SOURCE_TIMESTAMP


def test_a_series_containing_a_future_sample_is_leakage() -> None:
    findings = check_series_bounds(
        mint=MINT, observed_at=1_000, series_latest={"volume_usd": 1_060}
    )
    assert findings[0].kind == FUTURE_OBSERVATION


def test_the_same_mint_on_both_sides_of_a_split_is_contamination() -> None:
    findings = check_split(train=[(MINT, 1), (OTHER, 2)], test=[(MINT, 30)])
    assert any(finding.kind == SPLIT_CONTAMINATION for finding in findings)


def test_a_test_fold_that_predates_training_is_temporal_disorder() -> None:
    findings = check_split(train=[(MINT, 500)], test=[(OTHER, 100)])
    assert any(finding.kind == TEMPORAL_DISORDER for finding in findings)


def test_duplicate_observations_are_caught() -> None:
    findings = check_duplicates([(MINT, 100), (MINT, 100)])
    assert findings[0].kind == DUPLICATE_OBSERVATION


def test_a_value_changing_for_one_instant_proves_the_store_is_rewritten() -> None:
    findings = check_retroactive_updates(
        [
            (MINT, 1_000, "market_cap_usd", Decimal("50000")),
            (MINT, 1_000, "market_cap_usd", Decimal("90000")),
        ]
    )
    assert findings[0].kind == RETROACTIVE_UPDATE


# --- the feature pipeline ----------------------------------------------------
def test_features_never_read_past_the_decision_instant() -> None:
    market = MarketSeries()
    for at in range(0, 2_000, 30):
        market.market_cap_usd.add(at, 40_000 + at)
        market.holders.add(at, 100 + at // 10)

    full = PretrendState(mint=MINT, at=1_800, market=market)
    early = build_features(truncate_state(full, at=600))
    late = build_features(truncate_state(full, at=1_800))

    assert early.get("market_cap_usd") < late.get("market_cap_usd")
    assert early.get("market_cap_usd") == Decimal("40600")
    assert audit_dataset([early, late]).clean


def test_the_auditor_catches_a_state_that_was_never_truncated() -> None:
    """Defence in depth: bounded reads are not enough if the inputs are not.

    ``build_features`` bounds every read at ``state.at``, so the VALUES are
    right even here.  The auditor still refuses, because a state carrying
    samples from after its own instant means some caller skipped
    ``truncate_state``, and the next feature added to the pipeline might not be
    as careful as the current ones.
    """

    market = MarketSeries()
    for at in range(0, 2_000, 30):
        market.market_cap_usd.add(at, 40_000 + at)

    leaky = build_features(PretrendState(mint=MINT, at=600, market=market))
    assert leaky.get("market_cap_usd") == Decimal("40600"), "values stay correct"

    report = audit_dataset([leaky])
    assert not report.clean
    assert FUTURE_OBSERVATION in report.by_kind()


def test_an_absent_fomo_tape_yields_unknown_not_zero() -> None:
    """'No FOMO buying' and 'no FOMO feed' justify opposite actions."""

    vector = build_features(PretrendState(mint=MINT, at=1_000))
    assert vector.get("unique_fomo_buyers_1m") is None
    assert "unique_fomo_buyers_1m" in vector.missing
    assert vector.completeness < Decimal("1")


def test_a_feature_vector_declares_its_version() -> None:
    vector = build_features(PretrendState(mint=MINT, at=1_000))
    assert vector.feature_version == FEATURE_VERSION


def test_quality_weighted_flow_reduces_to_the_raw_count_without_measurements() -> None:
    """Weights are measured lift, never invented importance multipliers."""

    tape = ActivityTape(MINT)
    for index in range(5):
        tape.add(buy(index, 1_000 + index, f"u{index}"))
    vector = build_features(
        PretrendState(mint=MINT, at=1_010, tape=tape, market=MarketSeries())
    )
    assert vector.get("raw_fomo_flow_3m") == Decimal("5")
    assert vector.get("quality_weighted_fomo_flow_3m") == Decimal("5")
