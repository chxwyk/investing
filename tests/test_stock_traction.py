"""Catching the climb toward fifty holders, not waiting for 265.

The operator's actual complaint about this lane: they saw SOL on INDA near $30K
and had no way to know whether it was real, and by the time the board showed it
leading at +7,081% the trade was somebody else's. A system that only speaks once
a token has 265 holders only ever describes history.

Covers specification tests 15 and 28-34.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

from smart_money_bot.stocks.traction import (
    MILESTONES,
    OUT_HOLDER_CONFLICT,
    OUT_NO_TRACTION,
    STAGE_0_VERIFIED,
    STAGE_1_SPARK,
    STAGE_2_TRACTION,
    STAGE_3_ENTRY,
    HolderSnapshot,
    TractionConfig,
    climb,
    measure,
    traction_score,
)

NOW = 1_700_000_000
LAUNCH = NOW - 900


def _snap(at: int, holders: int, buyers: int, sellers: int, liquidity: str, **overrides):
    values = dict(
        at=at,
        economic_holders=holders,
        raw_holders=holders + 4,
        independent_buyers=buyers,
        independent_sellers=sellers,
        liquidity_usd=Decimal(liquidity),
        cluster_adjusted_top10=Decimal("0.30"),
        volume_usd=Decimal("50000"),
    )
    values.update(overrides)
    return HolderSnapshot(**values)


def _climb_series() -> list[HolderSnapshot]:
    """A real token filling up: six holders to a hundred and twenty-four."""

    return [
        _snap(LAUNCH + 60, 6, 4, 0, "3000", volume_usd=Decimal("2000")),
        _snap(LAUNCH + 300, 28, 12, 2, "11000", volume_usd=Decimal("18000")),
        _snap(LAUNCH + 600, 62, 24, 6, "17000", volume_usd=Decimal("40000")),
        _snap(NOW, 124, 41, 11, "26000", volume_usd=Decimal("90000")),
    ]


def _at(series, index):
    subset = series[: index + 1]
    return subset[-1], measure(subset, launched_at=LAUNCH, now=subset[-1].at)


# ===========================================================================
# 15 — the alert fires at the historical snapshot, not the final one
# ===========================================================================


def test_15_a_token_climbing_emits_each_stage_when_it_was_actually_reached() -> None:
    """The SOL-on-INDA regression.

    Each rung must be reached at the moment the evidence supported it, not
    retroactively at the end when everything looks obvious.
    """

    series = _climb_series()
    ages = (60, 300, 600, 900)
    reached = []
    for index, age in enumerate(ages):
        snapshot, metrics = _at(series, index)
        reached.append(climb(snapshot, metrics, age_seconds=age).stage)

    assert reached == [
        STAGE_0_VERIFIED,   # six holders: verified, nobody has arrived
        STAGE_1_SPARK,      # twenty-eight: real people are arriving
        STAGE_2_TRACTION,   # sixty-two: it held up and broadened
        STAGE_3_ENTRY,      # a hundred and twenty-four
    ]


def test_15b_the_earliest_rung_is_reachable_at_twenty_five_holders() -> None:
    # The whole reason Stage 1 exists: something visible before 100 holders.
    snapshot, metrics = _at(_climb_series(), 1)
    assert snapshot.economic_holders == 28
    assert climb(snapshot, metrics, age_seconds=300).stage == STAGE_1_SPARK


def test_15c_milestone_times_are_recorded_from_the_launch() -> None:
    metrics = measure(_climb_series(), launched_at=LAUNCH, now=NOW)
    assert set(metrics.milestone_seconds) <= set(MILESTONES)
    assert metrics.milestone_seconds[25] == 300
    assert metrics.milestone_seconds[50] == 600
    assert metrics.milestone_seconds[100] == 900
    # 250 was never reached, so it is absent rather than zero.
    assert 250 not in metrics.milestone_seconds


def test_velocity_distinguishes_a_fast_climb_from_a_slow_one() -> None:
    # Twenty-five holders in ninety seconds and twenty-five over a day are
    # different tokens; a threshold on the count alone cannot tell them apart.
    fast = measure(
        [_snap(NOW - 120, 2, 1, 0, "9000"), _snap(NOW, 40, 20, 3, "16000")],
        launched_at=NOW - 120, now=NOW,
    )
    slow = measure(
        [_snap(NOW - 86_400, 2, 1, 0, "9000"), _snap(NOW, 40, 20, 3, "16000")],
        launched_at=NOW - 86_400, now=NOW,
    )
    assert fast.holder_velocity_per_minute > slow.holder_velocity_per_minute


def test_missing_history_is_unknown_rather_than_zero() -> None:
    # "We have not been watching long enough" and "nothing happened" are
    # different findings, and only one is a reason to refuse.
    metrics = measure([_snap(NOW, 40, 20, 3, "16000")], launched_at=NOW, now=NOW)
    assert metrics.holder_delta_5m is None
    assert metrics.holder_growth_pct_15m is None
    assert measure([], now=NOW).holder_velocity_per_minute is None


# ===========================================================================
# 28-31 — each rung requires its own evidence
# ===========================================================================


def test_28_a_verified_launch_with_nobody_in_it_stops_at_stage_zero() -> None:
    result = climb(_snap(NOW, 3, 1, 0, "500"), measure([], now=NOW), age_seconds=30)
    assert result.stage == STAGE_0_VERIFIED
    assert result.outcome == OUT_NO_TRACTION
    assert any("economic holders" in item for item in result.blocked_by)


def test_29_scout_thresholds_reach_stage_one_and_no_further() -> None:
    snapshot, metrics = _at(_climb_series(), 1)
    result = climb(snapshot, metrics, age_seconds=300)
    assert result.stage == STAGE_1_SPARK
    assert result.stage != STAGE_3_ENTRY
    assert result.blocked_by, "the card must say what is holding it at Spark"


def test_30_traction_needs_growth_as_well_as_size() -> None:
    """Fifty holders that arrived and then stopped is not traction."""

    stalled = [
        _snap(NOW - 1200, 60, 24, 6, "17000"),
        _snap(NOW - 600, 60, 24, 6, "17000"),
        _snap(NOW, 60, 24, 6, "17000"),
    ]
    metrics = measure(stalled, launched_at=NOW - 1200, now=NOW)
    result = climb(stalled[-1], metrics, age_seconds=1200)
    assert result.stage == STAGE_1_SPARK
    assert any("growth" in item for item in result.blocked_by)


def test_31_a_hundred_holders_is_not_enough_on_its_own() -> None:
    growing = measure(_climb_series(), launched_at=LAUNCH, now=NOW)
    for label, overrides in (
        ("concentrated", {"cluster_adjusted_top10": Decimal("0.62")}),
        ("no sellers", {"independent_sellers": 1}),
        ("thin liquidity", {"liquidity_usd": Decimal("9000")}),
        ("few buyers", {"independent_buyers": 12}),
        ("concentration unread", {"cluster_adjusted_top10": None}),
    ):
        snapshot = _snap(NOW, 140, 45, 12, "26000", **overrides)
        result = climb(snapshot, growing, age_seconds=900)
        assert result.stage != STAGE_3_ENTRY, label
        assert result.blocked_by, label


def test_an_unread_concentration_cannot_pass_the_entry_ceiling() -> None:
    # UNKNOWN is not PASS, restated where it is easiest to get wrong.
    growing = measure(_climb_series(), launched_at=LAUNCH, now=NOW)
    result = climb(
        _snap(NOW, 140, 45, 12, "26000", cluster_adjusted_top10=None),
        growing, age_seconds=900,
    )
    assert result.stage == STAGE_2_TRACTION
    assert any("unread" in item for item in result.blocked_by)


# ===========================================================================
# The young-token carve-out, and the conflict short circuit
# ===========================================================================


def test_a_token_under_three_minutes_may_substitute_a_verified_sell_route() -> None:
    """Nobody has sold yet because nobody has had time to.

    That is different from nobody being able to, so a verified route stands in
    — and the card is required to say the history is still forming.
    """

    snapshot = _snap(NOW, 30, 12, 0, "12000")
    metrics = measure(_climb_series(), launched_at=LAUNCH, now=NOW)
    result = climb(snapshot, metrics, age_seconds=90, sell_route_ok=True)

    assert result.stage == STAGE_1_SPARK
    assert any("SELL HISTORY FORMING" in note for note in result.notes)


def test_the_carve_out_does_not_apply_without_a_verified_route() -> None:
    snapshot = _snap(NOW, 30, 12, 0, "12000")
    metrics = measure(_climb_series(), launched_at=LAUNCH, now=NOW)
    assert climb(snapshot, metrics, age_seconds=90, sell_route_ok=False).stage == (
        STAGE_0_VERIFIED
    )


def test_the_carve_out_expires_with_age() -> None:
    snapshot = _snap(NOW, 30, 12, 0, "12000")
    metrics = measure(_climb_series(), launched_at=LAUNCH, now=NOW)
    assert climb(snapshot, metrics, age_seconds=1_800, sell_route_ok=True).stage == (
        STAGE_0_VERIFIED
    )


def test_a_contested_holder_count_supports_no_claim_built_on_holders() -> None:
    snapshot, metrics = _at(_climb_series(), 3)
    result = climb(snapshot, metrics, age_seconds=900, holder_conflict=True)
    assert result.stage == STAGE_0_VERIFIED
    assert result.outcome == OUT_HOLDER_CONFLICT


# ===========================================================================
# 32 — the score ranks; it cannot open a gate
# ===========================================================================


def test_32_the_traction_score_cannot_promote_anything() -> None:
    """It orders verified watches. There is no path from it to a stage."""

    snapshot = _snap(NOW, 30, 12, 1, "9000")
    metrics = measure(_climb_series(), launched_at=LAUNCH, now=NOW)
    score, breakdown = traction_score(snapshot, metrics, organic=True)

    assert score > 0
    assert dict(breakdown)
    # A high score with failing thresholds is still held at the lower rung.
    assert climb(snapshot, metrics, age_seconds=900).stage == STAGE_0_VERIFIED

    source = inspect.getsource(climb)
    assert "traction_score" not in source, "the ladder must not consult the score"
    assert "score" not in source.split('"""')[-1], "no score reaches the rungs"


def test_the_score_breakdown_names_every_component() -> None:
    snapshot, metrics = _at(_climb_series(), 3)
    _, breakdown = traction_score(snapshot, metrics, organic=True)
    names = {name for name, _ in breakdown}
    assert "holder velocity and retention" in names
    assert "distribution quality" in names
    assert len(names) == 6


# ===========================================================================
# Configuration and standing rules
# ===========================================================================


def test_every_threshold_is_configuration_rather_than_a_buried_literal() -> None:
    fields = set(TractionConfig.__dataclass_fields__)
    for expected in (
        "scout_min_economic_holders", "scout_min_independent_buyers",
        "traction_min_holder_delta_5m", "traction_min_holder_growth_pct_15m",
        "entry_min_economic_holders", "entry_max_cluster_top10",
    ):
        assert expected in fields
    strict = TractionConfig(scout_min_economic_holders=100)
    snapshot, metrics = _at(_climb_series(), 1)
    assert climb(snapshot, metrics, age_seconds=300, config=strict).stage == STAGE_0_VERIFIED


def test_the_module_holds_no_provider_database_or_signer() -> None:
    import smart_money_bot.stocks.traction as module

    source = inspect.getsource(module)
    for forbidden in ("import aiohttp", "aiosqlite", "private_key", "send_transaction"):
        assert forbidden not in source


def test_every_result_is_marked_research_only() -> None:
    snapshot, metrics = _at(_climb_series(), 3)
    assert climb(snapshot, metrics, age_seconds=900).to_json()["research_only"] is True


# --- configuration wiring (specification section 6) --------------------------


def test_the_ladder_is_built_from_operator_configuration(settings) -> None:
    from smart_money_bot.stocks.traction import config_from_settings

    config = config_from_settings(settings)
    assert config.scout_min_economic_holders == 25
    assert config.traction_min_economic_holders == 50
    assert config.entry_min_economic_holders == 100
    # Percentages arrive as whole numbers and are converted once, here.
    assert config.scout_max_cluster_top10 == Decimal("0.7")
    assert config.entry_max_cluster_top10 == Decimal("0.4")


def test_the_rungs_cannot_be_configured_into_a_ladder_that_goes_down(settings) -> None:
    """A misconfiguration that inverted the rungs would let a token reach Entry
    without ever passing Traction."""

    import dataclasses

    import pytest

    with pytest.raises(ValueError, match="must not decrease"):
        dataclasses.replace(settings, stonks_entry_min_economic_holders=10).validate()
    with pytest.raises(ValueError, match="must not increase"):
        dataclasses.replace(settings, stonks_entry_max_cluster_top10_pct=Decimal("90")).validate()


def test_the_documented_defaults_match_the_specification(settings) -> None:
    assert settings.stonks_scout_min_independent_buyers == 10
    assert settings.stonks_scout_min_independent_sellers == 2
    assert settings.stonks_traction_min_holder_delta_5m == 10
    assert settings.stonks_traction_min_holder_growth_pct_15m == Decimal("25")
    assert settings.stonks_entry_min_independent_sellers == 8
    assert settings.stonks_entry_min_liquidity_usd == Decimal("20000")
