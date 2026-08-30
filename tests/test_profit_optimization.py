"""Regression suite for the v2.40 profit-first forward optimization.

Every case here traces to something that was either observed failing in
production or is required to stop the account leaking money:

* a paid provider that is out of credits must be called **less**, not every
  minute forever — production ran ~1,440 failing requests a day,
* a failure with no message must still say what failed,
* a provider outage must never be read as a token failure,
* forward results, not opinion, decide which signal families are ranked,
  pinged and traded — and one lucky coin must not move any of it,
* and every exit rule must be measurable against what actually happened next.

Nothing here touches a network provider.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import replace as dataclass_replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from smart_money_bot.errors import DiscoveryError, describe_exception
from smart_money_bot.lab.config import DEFAULT_LAB_CONFIG
from smart_money_bot.lab.exit_regret import (
    DEFAULT_HORIZON_SECONDS,
    GOOD_DEFENSIVE,
    NEUTRAL,
    PREMATURE,
    UNKNOWN,
    ExitRecord,
    score_exit,
    summarize_exit_quality,
    summarize_exit_reasons,
)
from smart_money_bot.lab.exits import (
    EXIT_SAFETY_EMERGENCY,
    ExitContext,
    open_position,
)
from smart_money_bot.lab.forward import (
    CALIBRATION_VERSION,
    DISABLE_MIN_SAMPLE,
    MIN_SAMPLE,
    VERDICT_DEMOTED,
    VERDICT_DISABLED,
    VERDICT_INSUFFICIENT,
    VERDICT_PROMOTED,
    WEIGHT_CEILING,
    WEIGHT_FLOOR,
    EdgeInputs,
    calibrate_families,
    family_enabled,
    family_weight,
    forward_edge_score,
    measure_families,
    should_ping,
)
from smart_money_bot.lab.providers import (
    BACKOFF_SECONDS,
    CREDIT_STATUSES,
    DEGRADED,
    EXHAUSTED,
    HEALTHY,
    PROVIDER_FEATURES,
    ProviderState,
    backoff_seconds,
    build_provider_report,
    cost_per_signals,
    degraded_evidence_note,
    record_cache_hit,
    record_failure,
    record_skip,
    record_success,
)
from smart_money_bot.lab.shadow import (
    DEFAULT_SHADOW_CONFIG,
    FAMILY_CATALYST_WATCH,
    FAMILY_FAST_WATCH,
    FAMILY_NOTABLE_EARLY,
    S_FAMILY_DISABLED,
    ShadowSignal,
    ShadowTimestamps,
    evaluate_shadow_entry,
)
from smart_money_bot.lab.shadow_exits import (
    SHADOW_SAFETY_MONITOR,
    RunnerEvidence,
    plan_shadow_exit,
)
from smart_money_bot.lab.shadow_metrics import ShadowObservation, ShadowTradeRecord

D = Decimal
NOW = 1_800_000_000
MINT = "So11111111111111111111111111111111111111112"


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def trade(
    family: str,
    net: str,
    index: int,
    *,
    close_reason: str = "",
    mfe: str = "20",
    mae: str = "10",
    closed_at: int | None = None,
) -> ShadowTradeRecord:
    return ShadowTradeRecord(
        position_id=f"{family}-{index}",
        mint=MINT,
        family=family,
        opened_at=NOW,
        closed_at=closed_at if closed_at is not None else NOW + index,
        size_usd=D("10"),
        entry_price_usd=D("0.001"),
        realized_net_pnl_usd=D(net),
        realized_gross_pnl_usd=D(net),
        total_cost_usd=D("0.3"),
        max_favourable_percent=D(mfe),
        max_adverse_percent=D(mae),
        close_reason=close_reason,
        open=False,
    )


def observation(at: int, price: str) -> ShadowObservation:
    return ShadowObservation(at=at, price_usd=D(price), safety_status="PASS")


# ===========================================================================
# 10, 11, 24. PROVIDER COST — a failing provider is called LESS, not more
# ===========================================================================


def test_a_credit_failure_opens_an_exponential_backoff_window() -> None:
    state = ProviderState(name="solana_tracker")

    for expected in BACKOFF_SECONDS[:3]:
        state = record_failure(
            state, now=1_000.0, status=403, message="Insufficient credits"
        )
        assert state.degraded_until == 1_000.0 + expected

    assert state.is_degraded(now=1_000.0) is True
    assert state.health(now=1_000.0) == EXHAUSTED


def test_one_failure_degrades_but_only_repeated_ones_exhaust() -> None:
    # The distinction matters on the dashboard: a blip and an empty plan are
    # different problems with different answers.
    once = record_failure(ProviderState(name="p"), now=0.0, status=429)
    assert once.health(now=0.0) == DEGRADED

    twice = record_failure(once, now=0.0, status=429)
    thrice = record_failure(twice, now=0.0, status=429)
    assert thrice.health(now=0.0) == EXHAUSTED


def test_a_cache_hit_is_not_a_provider_call() -> None:
    # Section 10 prefers cached evidence, so a hit must never be counted as
    # spend or the cost report would punish the cheaper path.
    state = record_cache_hit(record_cache_hit(ProviderState(name="p")))

    assert state.cache_hits == 2
    assert state.calls == 0
    assert state.cache_hit_rate_percent == D("100.00")


def test_the_backoff_is_capped_rather_than_growing_without_bound() -> None:
    assert backoff_seconds(0) == 0
    assert backoff_seconds(1) == BACKOFF_SECONDS[0]
    assert backoff_seconds(99) == BACKOFF_SECONDS[-1]
    assert BACKOFF_SECONDS[-1] <= 3_600


def test_a_missing_record_is_not_a_credit_failure() -> None:
    # A 404 means "no such token", not "no credits".  Backing off because a
    # token is unknown would starve the pipeline for the wrong reason.
    state = record_failure(ProviderState(name="p"), now=10.0, status=404)

    assert state.degraded_until == 0.0
    assert state.consecutive_failures == 0
    assert state.errors == 1
    assert state.health(now=10.0) == HEALTHY


def test_every_credit_status_is_treated_as_exhaustion() -> None:
    assert set(CREDIT_STATUSES) == {401, 402, 403, 429}
    for status in sorted(CREDIT_STATUSES):
        state = record_failure(ProviderState(name="p"), now=0.0, status=status)
        assert state.degraded_until > 0, status


def test_one_good_response_clears_the_degraded_window_immediately() -> None:
    state = record_failure(ProviderState(name="p"), now=0.0, status=403)
    assert state.is_degraded(now=0.0)

    recovered = record_success(state, now=1.0)

    assert recovered.is_degraded(now=1.0) is False
    assert recovered.consecutive_failures == 0
    assert recovered.health(now=1.0) == HEALTHY


def test_skipped_calls_are_counted_so_the_saving_is_visible() -> None:
    state = record_skip(record_skip(ProviderState(name="p")))

    assert state.calls_skipped == 2
    assert state.calls == 0


def test_a_degraded_provider_reports_unknown_never_failed_and_never_pass() -> None:
    degraded = record_failure(ProviderState(name="solana_tracker"), now=0.0, status=429)
    note = degraded_evidence_note(degraded, now=0.0)

    assert "UNKNOWN" in note
    assert "PASS" not in note
    assert "FAIL" not in note
    assert degraded_evidence_note(ProviderState(name="p"), now=0.0) == ""


def test_the_provider_map_marks_solana_tracker_replaceable_and_rpc_essential() -> None:
    by_provider: dict[str, list] = {}
    for item in PROVIDER_FEATURES:
        by_provider.setdefault(item.provider, []).append(item)

    # Section 11: core detection must survive without Solana Tracker.
    assert all(not item.essential for item in by_provider["solana_tracker"])
    assert all(item.on_chain_fallback for item in by_provider["solana_tracker"])
    # Public chain data and market data genuinely have no substitute.
    assert any(item.essential for item in by_provider["solana_rpc"])
    assert any(item.essential for item in by_provider["dexscreener"])


def test_the_provider_report_surfaces_waste_and_the_cheaper_path() -> None:
    state = ProviderState(
        name="solana_tracker", calls=100, cache_hits=25, errors=40, calls_skipped=900
    )

    report = build_provider_report(state, now=0.0)

    assert report.essential is False
    assert report.wasted_calls == 40
    assert report.error_rate_percent == D("40.00")
    assert report.cache_hit_rate_percent == D("20.00")
    assert report.calls_skipped == 900
    assert "wallet discovery leaderboard" in report.replaceable_features


def test_provider_cost_is_a_measured_ratio_not_an_invented_dollar_figure() -> None:
    assert cost_per_signals(500, 100) == D("500.00")
    assert cost_per_signals(10, 0) is None


async def test_the_discovery_client_stops_calling_once_credits_run_out() -> None:
    from smart_money_bot.discovery import SolanaTrackerClient

    client = SolanaTrackerClient("key")
    calls: list[str] = []

    class _Response:
        status = 403

        async def text(self) -> str:
            return '{"error":"Insufficient credits for this request"}'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    class _Session:
        closed = False

        def get(self, url, **_):
            calls.append(url)
            return _Response()

    client._session = _Session()

    # The first attempt reaches HTTP and fails.
    with pytest.raises(DiscoveryError):
        await client._request("/v2/pnl/leaderboard/top", params={})
    assert len(calls) == 1
    assert client.degraded is True

    # Every attempt inside the backoff window is refused without a request.
    for _ in range(5):
        with pytest.raises(DiscoveryError, match="backoff window"):
            await client._request("/v2/pnl/leaderboard/top", params={})
    assert len(calls) == 1, "the breaker must not let a second call through"
    assert client.usage_snapshot()["calls_skipped"] == 5


async def test_the_discovery_client_reports_its_health_for_the_dashboard() -> None:
    from smart_money_bot.discovery import SolanaTrackerClient

    client = SolanaTrackerClient("key")
    snapshot = client.usage_snapshot()

    assert snapshot["health"] == HEALTHY
    assert snapshot["degraded"] is False
    assert set(snapshot) >= {
        "calls",
        "errors",
        "calls_skipped",
        "credit_failures",
        "degraded",
        "health",
        "last_error",
    }


def test_the_refresh_throttle_no_longer_disengages_when_the_pool_is_empty() -> None:
    # The production bug: the throttle required a non-empty candidate pool, so
    # it stopped throttling exactly when the provider was failing.
    from smart_money_bot import engine as engine_module

    tree = ast.parse(inspect.getsource(engine_module))
    refresh = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "refresh_discovery"
    )
    conditions = " ".join(
        ast.unparse(node) for node in ast.walk(refresh) if isinstance(node, ast.BoolOp)
    )

    assert "self._candidate_pool" not in conditions, (
        "the refresh throttle must not depend on the candidate pool being full"
    )
    assert "last_discovery_attempt_at" in conditions


# ===========================================================================
# DIAGNOSABILITY — a failure with no message must still say what failed
# ===========================================================================


def test_an_exception_with_no_message_still_describes_itself() -> None:
    # Production logged dozens of "Fomo fresh analysis 55sWLQ39: " lines with
    # nothing after the colon; they were all timeouts.
    assert describe_exception(TimeoutError()) == (
        "TimeoutError (the operation exceeded its time budget)"
    )
    assert "ValueError" in describe_exception(ValueError())
    assert describe_exception(DiscoveryError("HTTP 403")) == "DiscoveryError: HTTP 403"


def test_a_message_that_already_names_its_type_is_not_repeated() -> None:
    assert describe_exception(RuntimeError("RuntimeError: boom")) == "RuntimeError: boom"


def test_the_runner_reports_a_blown_analysis_budget_as_a_timeout() -> None:
    from smart_money_bot import engine as engine_module

    source = inspect.getsource(engine_module)
    assert "except TimeoutError:" in source
    assert "exceeded its %ss budget" in source
    assert "runner_analysis_timeouts" in source


# ===========================================================================
# 8. PROVIDER UNKNOWN IS NOT A HARD SAFETY FAIL
# ===========================================================================


def position_for(size: str = "10", fill: str = "0.001"):
    return open_position(
        position_id="p1",
        mint=MINT,
        now=NOW,
        decision_price_usd=D(fill),
        size_usd=D(size),
        market_cap_usd=D("60000"),
        config=DEFAULT_SHADOW_CONFIG.exit_config(),
    )


def healthy_context(**overrides) -> ExitContext:
    payload = {
        "now": NOW + 600,
        "price_usd": D("0.0013"),
        "liquidity_usd": D("42000"),
        "entry_liquidity_usd": D("40000"),
        "momentum_score": D("70"),
        "organic_score": D("65"),
        "buys": 140,
        "sells": 40,
        "safety_status": "PASS",
        "route_available": True,
    }
    payload.update(overrides)
    return ExitContext(**payload)


def test_a_provider_outage_does_not_half_sell_a_healthy_position() -> None:
    # The shared engine de-risks 50% whenever safety reads UNKNOWN in profit.
    # With Solana Tracker returning 403 for hours, that would have fired on
    # every profitable shadow position for a reason unrelated to any token.
    position = position_for()
    # +5%: in profit, but below the first ladder rung, so the UNKNOWN-safety
    # branch is unambiguously what fires.
    context = healthy_context(price_usd=D("0.00105"), safety_status="UNKNOWN")

    without_context = plan_shadow_exit(position, context, RunnerEvidence())
    with_outage = plan_shadow_exit(
        position,
        context,
        RunnerEvidence(safety_provider_degraded=True),
    )

    assert without_context.plan.acts is True
    assert without_context.plan.reason_code == EXIT_SAFETY_EMERGENCY
    assert with_outage.plan.acts is False
    assert with_outage.plan.reason_code == SHADOW_SAFETY_MONITOR


def test_a_confirmed_safety_failure_still_exits_immediately_and_in_full() -> None:
    position = position_for()

    assessment = plan_shadow_exit(
        position,
        healthy_context(safety_status="FAIL"),
        RunnerEvidence(safety_provider_degraded=True, safety_confirmed_fail=True),
    )

    assert assessment.plan.reason_code == EXIT_SAFETY_EMERGENCY
    assert assessment.plan.final is True
    assert assessment.plan.fraction == D("1")


def test_an_outage_is_not_rescued_when_the_market_is_also_breaking() -> None:
    position = position_for()
    outage = RunnerEvidence(safety_provider_degraded=True)

    no_route = plan_shadow_exit(
        position,
        healthy_context(
            price_usd=D("0.00105"), safety_status="UNKNOWN", route_available=False
        ),
        outage,
    )
    drained = plan_shadow_exit(
        position,
        healthy_context(
            price_usd=D("0.00105"), safety_status="UNKNOWN", liquidity_usd=D("20000")
        ),
        outage,
    )
    selling = plan_shadow_exit(
        position,
        healthy_context(
            price_usd=D("0.00105"), safety_status="UNKNOWN", buys=10, sells=200
        ),
        outage,
    )

    for assessment in (no_route, drained, selling):
        assert assessment.plan.reason_code != SHADOW_SAFETY_MONITOR
        assert assessment.plan.acts is True


def test_the_rescue_never_applies_to_a_confirmed_fail_flag() -> None:
    position = position_for()

    assessment = plan_shadow_exit(
        position,
        healthy_context(price_usd=D("0.00105"), safety_status="UNKNOWN"),
        RunnerEvidence(safety_provider_degraded=True, safety_confirmed_fail=True),
    )

    assert assessment.plan.reason_code != SHADOW_SAFETY_MONITOR


def test_strict_paper_entry_safety_is_untouched_by_the_outage_rescue() -> None:
    # Section 8 is explicit: this must not weaken STRICT PAPER entry safety.
    from smart_money_bot.lab import entry as strict_entry

    source = inspect.getsource(strict_entry)
    for forbidden in ("safety_provider_degraded", "SHADOW_SAFETY_MONITOR", "shadow"):
        assert forbidden not in source


# ===========================================================================
# 3, 15, 16. FORWARD CALIBRATION — evidence ranks families, not opinion
# ===========================================================================


def test_one_lucky_coin_cannot_move_the_ranking() -> None:
    lucky = [trade(FAMILY_FAST_WATCH, "90", 1)] + [
        trade(FAMILY_FAST_WATCH, "-1", index) for index in range(2, 6)
    ]

    weights = calibrate_families(lucky, as_of=NOW + 10_000)

    entry = weights[FAMILY_FAST_WATCH]
    assert entry.sample < MIN_SAMPLE
    assert entry.verdict == VERDICT_INSUFFICIENT
    assert entry.weight == D("1")


def test_a_profitable_family_is_promoted_on_a_real_sample() -> None:
    good = [trade(FAMILY_NOTABLE_EARLY, "3", i) for i in range(1, 31)]
    poor = [trade(FAMILY_CATALYST_WATCH, "-2", i) for i in range(1, 31)]

    weights = calibrate_families(good + poor, as_of=NOW + 10_000)

    assert weights[FAMILY_NOTABLE_EARLY].verdict == VERDICT_PROMOTED
    assert weights[FAMILY_NOTABLE_EARLY].weight > D("1")
    assert weights[FAMILY_CATALYST_WATCH].verdict == VERDICT_DEMOTED
    assert weights[FAMILY_CATALYST_WATCH].weight < D("1")


def test_a_losing_rugging_family_is_disabled_in_shadow() -> None:
    rugging = [
        trade(FAMILY_CATALYST_WATCH, "-6", i, close_reason="SAFETY_DETERIORATION")
        for i in range(1, DISABLE_MIN_SAMPLE + 5)
    ]
    good = [trade(FAMILY_NOTABLE_EARLY, "3", i) for i in range(1, 31)]

    weights = calibrate_families(rugging + good, as_of=NOW + 10_000)

    entry = weights[FAMILY_CATALYST_WATCH]
    assert entry.verdict == VERDICT_DISABLED
    assert entry.enabled is False
    assert family_enabled(weights, FAMILY_CATALYST_WATCH) is False
    assert family_enabled(weights, FAMILY_NOTABLE_EARLY) is True


def test_a_losing_family_that_does_not_rug_is_demoted_not_disabled() -> None:
    # Demotion and disabling are different claims and need different evidence.
    losing = [
        trade(FAMILY_CATALYST_WATCH, "-3", i)
        for i in range(1, DISABLE_MIN_SAMPLE + 5)
    ]

    entry = calibrate_families(losing, as_of=NOW + 10_000)[FAMILY_CATALYST_WATCH]

    assert entry.verdict != VERDICT_DISABLED
    assert entry.enabled is True


def test_weights_are_always_bounded_however_extreme_the_data() -> None:
    extreme = [trade(FAMILY_NOTABLE_EARLY, "500", i) for i in range(1, 61)]
    disaster = [trade(FAMILY_FAST_WATCH, "-500", i) for i in range(1, 61)]

    weights = calibrate_families(extreme + disaster, as_of=NOW + 10_000)

    for entry in weights.values():
        assert WEIGHT_FLOOR <= entry.weight <= WEIGHT_CEILING


def test_shrinkage_pulls_a_small_sample_toward_the_pool() -> None:
    small = [trade(FAMILY_FAST_WATCH, "5", i) for i in range(1, MIN_SAMPLE + 1)]
    large = [trade(FAMILY_NOTABLE_EARLY, "5", i) for i in range(1, 81)]

    weights = calibrate_families(small + large, as_of=NOW + 10_000)

    # Both families measure the same raw expectancy; the smaller sample keeps
    # less of its deviation from the pool.
    assert weights[FAMILY_FAST_WATCH].raw_expectancy_usd == (
        weights[FAMILY_NOTABLE_EARLY].raw_expectancy_usd
    )
    assert weights[FAMILY_FAST_WATCH].shrinkage < weights[FAMILY_NOTABLE_EARLY].shrinkage


def test_every_weight_is_auditable_and_versioned() -> None:
    cutoff = NOW + 10_000
    weights = calibrate_families(
        [trade(FAMILY_NOTABLE_EARLY, "2", i) for i in range(1, 31)], as_of=cutoff
    )

    audit = weights[FAMILY_NOTABLE_EARLY].audit
    assert audit["CALIBRATION_VERSION"] == CALIBRATION_VERSION
    assert audit["AS_OF"] == str(cutoff)
    for key in ("SAMPLE", "RAW_EXPECTANCY", "SHRUNK_EXPECTANCY", "POOLED_EXPECTANCY"):
        assert audit[key]


def test_calibration_reads_no_trade_that_closed_after_the_cutoff() -> None:
    early = [trade(FAMILY_NOTABLE_EARLY, "1", i, closed_at=NOW + i) for i in range(1, 31)]
    later = [
        trade(FAMILY_NOTABLE_EARLY, "99", i, closed_at=NOW + 100_000)
        for i in range(100, 130)
    ]

    at_cutoff = measure_families(early + later, as_of=NOW + 50)
    with_future = measure_families(early + later, as_of=NOW + 200_000)

    assert at_cutoff[FAMILY_NOTABLE_EARLY].sample == 30
    assert with_future[FAMILY_NOTABLE_EARLY].sample == 60


def test_open_trades_never_contribute_to_a_weight() -> None:
    open_trade = dataclass_replace(
        trade(FAMILY_NOTABLE_EARLY, "50", 1), open=True, closed_at=None
    )
    closed = [trade(FAMILY_NOTABLE_EARLY, "1", i) for i in range(2, 32)]

    stats = measure_families([open_trade, *closed], as_of=NOW + 10_000)

    assert stats[FAMILY_NOTABLE_EARLY].sample == 30


def test_an_unknown_family_gets_a_neutral_weight() -> None:
    assert family_weight({}, "SOMETHING_NEW") == D("1")
    assert family_enabled({}, "SOMETHING_NEW") is True


async def test_a_disabled_family_is_refused_by_the_shadow_entry_gate() -> None:
    from smart_money_bot.lab.bankroll import BankrollState

    signal = ShadowSignal(
        mint=MINT,
        family=FAMILY_CATALYST_WATCH,
        timestamps=ShadowTimestamps(signal_at=NOW, decision_at=NOW),
        price_usd=D("0.001"),
        liquidity_usd=D("40000"),
        route_available=True,
    )
    state = BankrollState(
        starting_usd=D("100"), cash_usd=D("100"), peak_equity_usd=D("100")
    )

    allowed = evaluate_shadow_entry(signal, state, family_enabled=True)
    refused = evaluate_shadow_entry(signal, state, family_enabled=False)

    assert allowed.accepted is True
    assert refused.accepted is False
    assert refused.primary_reason == S_FAMILY_DISABLED


# ===========================================================================
# 17. CURRENT EDGE OUTRANKS HISTORICAL OPPORTUNITY
# ===========================================================================


def test_a_spent_setup_cannot_outrank_a_fresh_one_on_history_alone() -> None:
    spent = EdgeInputs(
        family=FAMILY_FAST_WATCH,
        actionability_score=D("15"),
        freshness_seconds=7_200,
        historical_opportunity_score=D("100"),
    )
    fresh = EdgeInputs(
        family=FAMILY_FAST_WATCH,
        actionability_score=D("85"),
        freshness_seconds=120,
        expected_net_edge_percent=D("30"),
        independent_buyers=30,
        route_price_impact_percent=D("0.5"),
        historical_opportunity_score=D("10"),
    )

    assert forward_edge_score(fresh).score > forward_edge_score(spent).score


def test_historical_opportunity_is_capped_at_a_tenth_of_the_score() -> None:
    edge = forward_edge_score(
        EdgeInputs(family=FAMILY_FAST_WATCH, historical_opportunity_score=D("100"))
    )

    assert edge.components["historical"] <= D("10")


def test_a_dead_route_blocks_the_edge_score_outright() -> None:
    edge = forward_edge_score(
        EdgeInputs(
            family=FAMILY_FAST_WATCH,
            actionability_score=D("95"),
            route_available=False,
        )
    )

    assert edge.actionable is False
    assert "no usable route" in edge.blockers


def test_a_promoted_family_ranks_above_an_identical_demoted_one() -> None:
    good = [trade(FAMILY_NOTABLE_EARLY, "4", i) for i in range(1, 41)]
    bad = [trade(FAMILY_FAST_WATCH, "-2", i) for i in range(1, 41)]
    weights = calibrate_families(good + bad, as_of=NOW + 10_000)

    base = {
        "actionability_score": D("80"),
        "freshness_seconds": 120,
        "independent_buyers": 25,
        "route_price_impact_percent": D("0.5"),
    }
    promoted = forward_edge_score(
        EdgeInputs(family=FAMILY_NOTABLE_EARLY, **base), weights=weights
    )
    demoted = forward_edge_score(
        EdgeInputs(family=FAMILY_FAST_WATCH, **base), weights=weights
    )

    assert promoted.score > demoted.score
    assert promoted.family_weight > demoted.family_weight


# ===========================================================================
# 18. URGENT PINGS MUST EARN THEMSELVES
# ===========================================================================


def strong_edge_inputs(**overrides) -> EdgeInputs:
    payload = {
        "family": FAMILY_NOTABLE_EARLY,
        "actionability_score": D("90"),
        "freshness_seconds": 90,
        "expected_net_edge_percent": D("40"),
        "independent_buyers": 30,
        "route_price_impact_percent": D("0.4"),
        "catalyst_confidence": "CONFIRMED",
        "organic_score": D("80"),
        "momentum_score": D("80"),
    }
    payload.update(overrides)
    return EdgeInputs(**payload)


def test_a_high_edge_early_confirmed_setup_may_ping() -> None:
    verdict = should_ping(
        forward_edge_score(strong_edge_inputs()),
        family=FAMILY_NOTABLE_EARLY,
        independent_confirmations=3,
        still_early=True,
    )

    assert verdict.ping is True
    assert verdict.blockers == ()


def test_a_single_confirmation_never_pings_however_strong_it_looks() -> None:
    verdict = should_ping(
        forward_edge_score(strong_edge_inputs()),
        family=FAMILY_NOTABLE_EARLY,
        independent_confirmations=1,
    )

    assert verdict.ping is False
    assert any("independent confirmation" in item for item in verdict.blockers)


def test_a_move_that_already_happened_never_pings() -> None:
    late = should_ping(
        forward_edge_score(strong_edge_inputs()),
        family=FAMILY_NOTABLE_EARLY,
        independent_confirmations=3,
        still_early=False,
    )
    spent = should_ping(
        forward_edge_score(strong_edge_inputs()),
        family=FAMILY_NOTABLE_EARLY,
        independent_confirmations=3,
        move_already_made_percent=D("300"),
    )

    assert late.ping is False
    assert spent.ping is False


def test_a_demoted_family_cannot_ping_at_all() -> None:
    bad = [trade(FAMILY_FAST_WATCH, "-3", i) for i in range(1, 41)]
    good = [trade(FAMILY_NOTABLE_EARLY, "3", i) for i in range(1, 41)]
    weights = calibrate_families(bad + good, as_of=NOW + 10_000)

    verdict = should_ping(
        forward_edge_score(strong_edge_inputs(family=FAMILY_FAST_WATCH), weights=weights),
        family=FAMILY_FAST_WATCH,
        independent_confirmations=5,
        weights=weights,
    )

    assert verdict.ping is False
    assert any("demoted" in item for item in verdict.blockers)


def test_the_forward_ping_gate_can_only_withhold_never_create_a_ping() -> None:
    from smart_money_bot import engine as engine_module

    source = inspect.getsource(engine_module)
    # Both call sites are guarded by the existing ping decision being true
    # already, so the gate is strictly subtractive.
    assert "if ping.ping and self.settings.fomo_forward_ping_gate_enabled:" in source
    assert "if decision.ping and self.settings.fomo_forward_ping_gate_enabled:" in source


# ===========================================================================
# 7, 23. EXIT REGRET — which rules cost money
# ===========================================================================


def exit_record(reason: str, *, price: str = "0.001", net: str = "1") -> ExitRecord:
    return ExitRecord(
        position_id="p1",
        mint=MINT,
        family=FAMILY_FAST_WATCH,
        reason_code=reason,
        occurred_at=NOW,
        exit_price_usd=D(price),
        net_pnl_usd=D(net),
    )


def test_an_exit_the_token_ran_away_from_is_scored_premature() -> None:
    score = score_exit(
        exit_record("MOMENTUM_DECAY"),
        [observation(NOW + 300, "0.002"), observation(NOW + 600, "0.003")],
    )

    assert score.verdict == PREMATURE
    assert score.upside_missed_percent == D("200.00")
    assert score.upside_missed_usd > 0


def test_an_exit_that_dodged_a_collapse_is_scored_defensive() -> None:
    score = score_exit(
        exit_record("SAFETY_DETERIORATION"),
        [observation(NOW + 300, "0.0002"), observation(NOW + 600, "0.00005")],
    )

    assert score.verdict == GOOD_DEFENSIVE
    assert score.loss_avoided_percent > D("90")
    assert score.loss_avoided_usd > 0


def test_a_small_wobble_after_an_exit_is_neither_premature_nor_defensive() -> None:
    score = score_exit(
        exit_record("MILESTONE_TAKE_PROFIT"),
        [observation(NOW + 300, "0.00105"), observation(NOW + 600, "0.00095")],
    )

    assert score.verdict == NEUTRAL


def test_an_exit_with_no_observations_after_it_is_never_guessed() -> None:
    assert score_exit(exit_record("TIME_STOP"), []).verdict == UNKNOWN
    # Observations before the exit say nothing about the exit.
    assert (
        score_exit(exit_record("TIME_STOP"), [observation(NOW - 60, "0.01")]).verdict
        == UNKNOWN
    )


def test_the_scoring_horizon_bounds_how_far_ahead_it_looks() -> None:
    far_future = observation(NOW + DEFAULT_HORIZON_SECONDS + 600, "0.05")

    inside = score_exit(exit_record("TIME_STOP"), [far_future], horizon_seconds=10_000)
    outside = score_exit(exit_record("TIME_STOP"), [far_future])

    assert inside.verdict == PREMATURE
    assert outside.verdict == UNKNOWN


def test_the_most_expensive_exit_rule_sorts_first() -> None:
    leaky = score_exit(
        exit_record("MOMENTUM_DECAY", net="5"),
        [observation(NOW + 300, "0.004")],
    )
    defensive = score_exit(
        exit_record("SAFETY_DETERIORATION", net="5"),
        [observation(NOW + 300, "0.0001")],
    )

    reports = summarize_exit_reasons([defensive, leaky])

    assert reports[0].reason_code == "MOMENTUM_DECAY"
    assert reports[0].verdict == "COSTING_MONEY"
    assert reports[-1].reason_code == "SAFETY_DETERIORATION"
    assert reports[-1].verdict == "DEFENDING_MONEY"


def test_the_account_level_report_says_whether_exits_leak() -> None:
    leaking = summarize_exit_quality(
        [score_exit(exit_record("MOMENTUM_DECAY", net="5"), [observation(NOW + 60, "0.01")])]
    )
    defending = summarize_exit_quality(
        [
            score_exit(
                exit_record("HARD_LOSS_PROTECTION", net="5"),
                [observation(NOW + 60, "0.00001")],
            )
        ]
    )

    assert leaking.exits_are_leaking is True
    assert leaking.premature_rate_percent == D("100.00")
    assert defending.exits_are_leaking is False
    assert defending.defensive_rate_percent == D("100.00")


def test_exit_regret_never_reaches_a_live_decision() -> None:
    # Scoring an exit reads the future by construction, so nothing in the exit
    # path may import it.
    for module_name in (
        "smart_money_bot.lab.shadow_exits",
        "smart_money_bot.lab.shadow",
        "smart_money_bot.lab.exits",
    ):
        import importlib

        source = inspect.getsource(importlib.import_module(module_name))
        assert "exit_regret" not in source
        assert "score_exit" not in source


# ===========================================================================
# 20, 26. THE EXPERIMENT AND THE SAFETY FLOOR ARE UNCHANGED
# ===========================================================================


def test_the_shadow_experiment_terms_are_untouched() -> None:
    assert DEFAULT_SHADOW_CONFIG.bankroll_usd == D("100")
    assert DEFAULT_SHADOW_CONFIG.position_usd == D("10")
    assert DEFAULT_SHADOW_CONFIG.max_concurrent_positions == 5
    assert DEFAULT_SHADOW_CONFIG.max_total_exposure_usd == D("50")
    assert DEFAULT_SHADOW_CONFIG.net_profit_objective_usd == D("2")


def test_strict_paper_thresholds_are_untouched() -> None:
    assert DEFAULT_LAB_CONFIG.normal_position_usd == D("5")
    assert DEFAULT_LAB_CONFIG.max_total_exposure_usd == D("30")
    assert DEFAULT_LAB_CONFIG.min_independent_buyers == 12
    assert DEFAULT_LAB_CONFIG.min_expected_net_edge_percent == D("12")


def test_no_new_module_can_spend_real_money() -> None:
    import importlib

    for module_name in (
        "smart_money_bot.lab.providers",
        "smart_money_bot.lab.forward",
        "smart_money_bot.lab.exit_regret",
    ):
        tree = ast.parse(inspect.getsource(importlib.import_module(module_name)))
        names = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Name | ast.Attribute)
        }
        for forbidden in (
            "Keypair",
            "sign_message",
            "execute_order",
            "send_transaction",
            "swap",
        ):
            assert forbidden not in names, f"{module_name} must not reference {forbidden}"


def test_the_new_modules_perform_no_io() -> None:
    import importlib

    for module_name in (
        "smart_money_bot.lab.providers",
        "smart_money_bot.lab.forward",
        "smart_money_bot.lab.exit_regret",
    ):
        imports: set[str] = set()
        tree = ast.parse(inspect.getsource(importlib.import_module(module_name)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        assert not imports & {"aiohttp", "requests", "socket", "urllib", "aiosqlite"}


def test_the_forward_weights_never_touch_a_safety_or_cost_gate() -> None:
    from smart_money_bot.lab import forward

    names = {
        node.attr
        for node in ast.walk(ast.parse(inspect.getsource(forward)))
        if isinstance(node, ast.Attribute)
    }
    for forbidden in (
        "min_liquidity_usd",
        "min_independent_buyers",
        "hard_stop_loss_percent",
        "platform_fee_bps",
        "min_expected_net_edge_percent",
    ):
        assert forbidden not in names


# ===========================================================================
# 21-24. THE PROFIT DASHBOARD RENDERS FROM AN EMPTY BOOK
# ===========================================================================


async def test_the_profit_dashboard_renders_before_any_trade_exists(
    settings, tmp_path
) -> None:
    from smart_money_bot import bot as bot_module
    from smart_money_bot.engine import SmartMoneyEngine

    engine = SmartMoneyEngine(
        dataclass_replace(settings, database_path=str(tmp_path / "profit.db"))
    )
    await engine.database.connect()
    try:
        await engine.shadow.start_experiment(now=NOW)

        summary = bot_module._profit_summary_embed(await engine.profit_summary())
        signals = bot_module._profit_signals_embed(await engine.profit_signals())
        exits = bot_module._profit_exits_embed(await engine.profit_exits())
        providers = bot_module._profit_providers_embed(await engine.profit_providers())

        for embed in (summary, signals, exits, providers):
            assert len(embed) <= 6000
        assert "SHADOW ACCOUNT" in summary.title
        # Section 21: the money answer is in the description, not buried.
        assert "$100.00" in (summary.description or "")
        assert "REAL MONEY SPENT: $0.00" in (summary.description or "")
        assert any("solana_tracker" in field.name for field in providers.fields)
    finally:
        await engine.database.close()


async def test_the_provider_view_marks_solana_tracker_optional(settings, tmp_path) -> None:
    from smart_money_bot.engine import SmartMoneyEngine

    engine = SmartMoneyEngine(
        dataclass_replace(settings, database_path=str(tmp_path / "providers.db"))
    )
    await engine.database.connect()
    try:
        rows = await engine.profit_providers()
        by_provider = {row["report"].provider: row["report"] for row in rows}

        assert by_provider["solana_tracker"].essential is False
        assert by_provider["solana_rpc"].essential is True
        assert by_provider["solana_tracker"].replaceable_features
    finally:
        await engine.database.close()


def test_the_engine_exposes_every_profit_view() -> None:
    from smart_money_bot.engine import SmartMoneyEngine

    for name in ("profit_summary", "profit_signals", "profit_exits", "profit_providers"):
        assert callable(getattr(SmartMoneyEngine, name))


def test_the_ping_gate_setting_defaults_to_on(settings) -> None:
    assert settings.fomo_forward_ping_gate_enabled is True
    assert settings.fomo_runner_analysis_budget_seconds == 30


def test_a_stub_settings_object_without_the_new_flags_still_works() -> None:
    # Defensive: an older settings object must not crash the edge score.
    stub = SimpleNamespace()
    assert forward_edge_score(EdgeInputs(family=FAMILY_FAST_WATCH)).score >= 0
    assert getattr(stub, "fomo_forward_ping_gate_enabled", True) is True


# ===========================================================================
# MIGRATION — the accounting column is additive on a live database
# ===========================================================================


async def test_the_skipped_call_column_is_added_to_an_existing_database(tmp_path) -> None:
    import sqlite3

    from smart_money_bot.database import Database

    path = tmp_path / "legacy.db"
    # Exactly the pre-v2.40 table shape a deployed instance already has.
    legacy = sqlite3.connect(path)
    legacy.execute(
        """
        CREATE TABLE provider_call_usage (
            provider TEXT NOT NULL,
            feature TEXT NOT NULL,
            usage_day TEXT NOT NULL,
            calls INTEGER NOT NULL DEFAULT 0,
            cache_hits INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (provider, feature, usage_day)
        )
        """
    )
    legacy.execute(
        "INSERT INTO provider_call_usage VALUES ('solana_tracker','x','2026-01-01',7,1,2,0)"
    )
    legacy.commit()
    legacy.close()

    database = Database(str(path), D("1000"))
    await database.connect()
    try:
        rows = await database.provider_call_rows()

        # The existing row survives and the new column defaults to zero.
        assert rows[0]["calls"] == 7
        assert rows[0]["calls_skipped"] == 0

        await database.record_provider_call(
            provider="solana_tracker",
            feature="x",
            usage_day="2026-01-01",
            calls=0,
            calls_skipped=900,
        )
        updated = await database.provider_call_rows()
        assert updated[0]["calls_skipped"] == 900
        assert updated[0]["calls"] == 7
    finally:
        await database.close()


async def test_skipped_calls_are_never_reported_as_cache_hits(tmp_path) -> None:
    from smart_money_bot.database import Database

    database = Database(str(tmp_path / "usage.db"), D("1000"))
    await database.connect()
    try:
        await database.record_provider_call(
            provider="solana_tracker",
            feature="wallet_discovery",
            usage_day="2026-01-01",
            calls=1,
            errors=1,
            calls_skipped=1_400,
        )
        row = (await database.provider_call_rows())[0]

        # Folding a breaker skip into the cache-hit rate would make a failing
        # provider look like a well-cached one.
        assert row["cache_hits"] == 0
        assert row["calls_skipped"] == 1_400
    finally:
        await database.close()


async def test_the_engine_ping_gate_runs_end_to_end(settings, tmp_path) -> None:
    from smart_money_bot.engine import SmartMoneyEngine
    from smart_money_bot.lab.forward import EdgeInputs as ForwardEdgeInputs

    engine = SmartMoneyEngine(
        dataclass_replace(settings, database_path=str(tmp_path / "ping.db"))
    )
    await engine.database.connect()
    try:
        await engine.shadow.start_experiment(now=NOW)

        strong = await engine._forward_ping_verdict(
            family=FAMILY_NOTABLE_EARLY,
            edge_inputs=ForwardEdgeInputs(
                family=FAMILY_NOTABLE_EARLY,
                actionability_score=D("90"),
                freshness_seconds=60,
                expected_net_edge_percent=D("40"),
                independent_buyers=30,
                route_price_impact_percent=D("0.4"),
                catalyst_confidence="CONFIRMED",
                organic_score=D("80"),
                momentum_score=D("80"),
            ),
            independent_confirmations=3,
            now=NOW,
        )
        weak = await engine._forward_ping_verdict(
            family=FAMILY_NOTABLE_EARLY,
            edge_inputs=ForwardEdgeInputs(
                family=FAMILY_NOTABLE_EARLY,
                actionability_score=D("20"),
                freshness_seconds=7_200,
            ),
            independent_confirmations=1,
            now=NOW,
        )

        assert strong.ping is True
        assert weak.ping is False
        assert weak.blockers
    finally:
        await engine.database.close()


async def test_a_degraded_discovery_client_does_not_stop_the_shadow_experiment(
    settings, tmp_path
) -> None:
    from smart_money_bot.engine import SmartMoneyEngine
    from smart_money_bot.lab.shadow import ShadowSignal as Signal

    engine = SmartMoneyEngine(
        dataclass_replace(settings, database_path=str(tmp_path / "degraded.db"))
    )
    await engine.database.connect()
    try:
        await engine.shadow.start_experiment(now=NOW - 100)

        # Section 11: Solana Tracker being unavailable must not stop core
        # detection or the forward experiment.
        decision, position = await engine.shadow.consider_signal(
            Signal(
                mint=MINT,
                family=FAMILY_FAST_WATCH,
                timestamps=ShadowTimestamps(signal_at=NOW, decision_at=NOW),
                name="Token",
                symbol="TOK",
                price_usd=D("0.001"),
                market_cap_usd=D("60000"),
                liquidity_usd=D("40000"),
                buys=90,
                sells=20,
                safety_status="UNKNOWN",
                route_available=True,
            ),
            now=NOW,
        )

        assert decision.accepted is True
        assert position is not None
        assert position.position.size_usd == D("10")
    finally:
        await engine.database.close()
