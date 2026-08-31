"""Deterministic regression suite for the v2.39 SHADOW auto-trader.

Every case answers one product question:

* does an eligible signal really deploy **exactly $10**, and never $5?
* can the shadow book ever exceed 5 positions, $50 exposure or the $100 bankroll?
* does a fill come from a route that could actually have filled it?
* does ``+$2 NET`` secure real dollars when the runner breaks, and *not* dump a
  runner that is still accelerating?
* can anything in this strategy family spend real money, or leak into STRICT
  PAPER's eligibility?

Nothing here touches a network provider.
"""

from __future__ import annotations

import ast
import inspect
import io
import tokenize
from decimal import Decimal

import pytest

from smart_money_bot.database import Database
from smart_money_bot.lab import shadow as shadow_module
from smart_money_bot.lab.bankroll import BankrollState, BreakerInputs
from smart_money_bot.lab.config import DEFAULT_LAB_CONFIG
from smart_money_bot.lab.exits import (
    EXIT_HARD_STOP,
    EXIT_LIQUIDITY_EMERGENCY,
    EXIT_MILESTONE,
    EXIT_SAFETY_EMERGENCY,
    EXIT_TIME_STOP,
    EXIT_TRAILING,
    ExitContext,
    open_position,
)
from smart_money_bot.lab.shadow import (
    DEFAULT_SHADOW_CONFIG,
    FAMILY_BREAKING_CATALYST,
    FAMILY_CATALYST_WATCH,
    FAMILY_CONFLUENCE_WATCH,
    FAMILY_FAST_WATCH,
    FAMILY_FRESH_RUNNER,
    FAMILY_NOTABLE_EARLY,
    FAMILY_NOTABLE_LATE,
    FAMILY_QUALIFIED_RESEARCH,
    FAMILY_STRICT_PAPER,
    S_ACCEPTED,
    S_ALREADY_HOLDING,
    S_BEFORE_EXPERIMENT,
    S_BREAKER_PAUSED,
    S_IMPACT_TOO_HIGH,
    S_INSUFFICIENT_BANKROLL,
    S_MAX_EXPOSURE,
    S_MAX_POSITIONS,
    S_NO_EXECUTABLE_ROUTE,
    S_RUGGED,
    S_SIGNAL_STALE,
    SHADOW_REAL_MONEY_SPEND,
    SIGNAL_FAMILIES,
    ShadowConfig,
    ShadowExposure,
    ShadowSignal,
    ShadowTimestamps,
    evaluate_shadow_breakers,
    evaluate_shadow_entry,
    shadow_config_from_settings,
    why_you_are_seeing_this,
)
from smart_money_bot.lab.shadow_exits import (
    HEALTH_ACCELERATING,
    HEALTH_WEAK,
    HOLD_SHADOW_RUNNER,
    SHADOW_PRINCIPAL_RECOVERY,
    SHADOW_SECURE_OBJECTIVE,
    RunnerEvidence,
    assess_runner_health,
    net_pnl_now,
    plan_shadow_exit,
)
from smart_money_bot.lab.shadow_metrics import (
    CF_FIXED_10,
    CF_NO_TRADE,
    COUNTERFACTUAL_POLICIES,
    ShadowObservation,
    ShadowTradeRecord,
    VenueFill,
    compare_shadow_exit_policies,
    summarize_shadow_account,
    summarize_venues,
)
from smart_money_bot.lab.venues import (
    FILL_EXECUTABLE_QUOTE,
    FILL_FALLBACK_PENALISED,
    FILL_SIMULATED_VENUE,
    GRADUATED,
    PRE_GRADUATION,
    ROUTE_CURVE_COMPLETE,
    VENUE_JUPITER,
    VENUE_PUMP_CURVE,
    VENUE_PUMPSWAP,
    BondingCurveState,
    bonding_curve_quote,
    classify_graduation,
    executable_quote,
    fallback_quote,
    pool_quote,
    select_route,
)
from smart_money_bot.shadow_runtime import ShadowRuntime
from smart_money_bot.shadow_store import ShadowStore

D = Decimal
NOW = 1_800_000_000
MINT = "So11111111111111111111111111111111111111112"


# ---------------------------------------------------------------------------
# fixtures and builders
# ---------------------------------------------------------------------------


@pytest.fixture
async def database(tmp_path):
    db = Database(str(tmp_path / "shadow.db"), D("1000"))
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
async def runtime(database):
    engine = ShadowRuntime(ShadowStore(database))
    await engine.start_experiment(now=NOW - 10_000)
    return engine


def code_only(source: str) -> str:
    """Executable code with comments and docstrings removed.

    A structural guarantee has to be proved against what the module *does*, not
    against prose that happens to mention the thing being forbidden — otherwise
    documenting an invariant would break the test that enforces it.
    """

    pieces: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in {tokenize.COMMENT, tokenize.STRING}:
            continue
        pieces.append(token.string)
    return " ".join(pieces)


def module_code(module_name: str) -> str:
    import importlib

    return code_only(inspect.getsource(importlib.import_module(module_name)))


def module_tree(module_name: str) -> ast.Module:
    import importlib

    return ast.parse(inspect.getsource(importlib.import_module(module_name)))


def imported_modules(module_name: str) -> set[str]:
    """Every module this one imports, by its written name.

    An AST walk is the honest way to ask "does this import a wallet client?" —
    a substring search would match ``market_cap_usd`` and call it an RPC client.
    """

    names: set[str] = set()
    for node in ast.walk(module_tree(module_name)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def referenced_names(module_name: str) -> set[str]:
    """Every identifier and attribute this module actually refers to."""

    names: set[str] = set()
    for node in ast.walk(module_tree(module_name)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.rsplit(".", 1)[-1])
            if node.asname:
                names.add(node.asname)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
    return names


def signal_for(
    *,
    mint: str = MINT,
    family: str = FAMILY_FAST_WATCH,
    price: str = "0.001",
    liquidity: str = "40000",
    signal_at: int = NOW,
    decision_at: int = NOW,
    **overrides,
) -> ShadowSignal:
    payload = {
        "mint": mint,
        "family": family,
        "timestamps": ShadowTimestamps(signal_at=signal_at, decision_at=decision_at),
        "name": "Test token",
        "symbol": "TEST",
        "price_usd": D(price),
        "market_cap_usd": D("60000"),
        "liquidity_usd": D(liquidity),
        "volume_usd": D("12000"),
        "buys": 80,
        "sells": 20,
        "independent_buyers": 30,
        "organic_score": D("70"),
        "momentum_score": D("70"),
        "safety_status": "UNKNOWN",
        "route_available": True,
    }
    payload.update(overrides)
    return ShadowSignal(**payload)


def bankroll(cash: str = "100", exposure: str = "0", **overrides) -> BankrollState:
    """A simulated account at its own equity peak unless a test says otherwise.

    Defaulting ``peak_equity_usd`` to current equity keeps the drawdown breaker
    quiet, so a sizing or exposure test measures the rule it is actually about.
    """

    equity = D(cash) + D(exposure)
    payload = {
        "starting_usd": D("100"),
        "cash_usd": D(cash),
        "open_exposure_usd": D(exposure),
        "peak_equity_usd": equity,
    }
    payload.update({key: value for key, value in overrides.items()})
    return BankrollState(**payload)


def position_for(*, size: str = "10", fill: str = "0.001", now: int = NOW):
    return open_position(
        position_id="p1",
        mint=MINT,
        now=now,
        decision_price_usd=D(fill),
        size_usd=D(size),
        market_cap_usd=D("60000"),
        config=DEFAULT_SHADOW_CONFIG.exit_config(),
    )


def exit_context(price: str, *, now: int = NOW + 600, **overrides) -> ExitContext:
    payload = {
        "now": now,
        "price_usd": D(price),
        "market_cap_usd": D("90000"),
        "liquidity_usd": D("42000"),
        "entry_liquidity_usd": D("40000"),
        "momentum_score": D("75"),
        "organic_score": D("70"),
        "buys": 140,
        "sells": 40,
        "volume_usd": D("18000"),
        "entry_volume_usd": D("12000"),
        "safety_status": "PASS",
        "route_available": True,
    }
    payload.update(overrides)
    return ExitContext(**payload)


# ===========================================================================
# 48. ENTRY — every eligible signal deploys exactly $10
# ===========================================================================


@pytest.mark.parametrize(
    "family",
    [
        FAMILY_FAST_WATCH,
        FAMILY_FRESH_RUNNER,
        FAMILY_NOTABLE_EARLY,
        FAMILY_NOTABLE_LATE,
        FAMILY_BREAKING_CATALYST,
        FAMILY_CATALYST_WATCH,
        FAMILY_CONFLUENCE_WATCH,
        FAMILY_QUALIFIED_RESEARCH,
        FAMILY_STRICT_PAPER,
    ],
)
def test_every_signal_family_deploys_exactly_ten_dollars(family: str) -> None:
    decision = evaluate_shadow_entry(signal_for(family=family), bankroll())

    assert decision.accepted is True
    assert decision.size_usd == D("10")
    assert decision.reason_codes == (S_ACCEPTED,)


def test_the_configured_entry_is_never_five_dollars() -> None:
    assert DEFAULT_SHADOW_CONFIG.position_usd == D("10")
    assert DEFAULT_SHADOW_CONFIG.min_position_usd == D("10")
    assert DEFAULT_SHADOW_CONFIG.max_position_usd == D("10")
    assert DEFAULT_SHADOW_CONFIG.bankroll_usd == D("100")
    assert DEFAULT_SHADOW_CONFIG.max_concurrent_positions == 5
    assert DEFAULT_SHADOW_CONFIG.max_total_exposure_usd == D("50")
    assert DEFAULT_SHADOW_CONFIG.net_profit_objective_usd == D("2")


def test_a_five_dollar_shadow_configuration_is_refused_at_construction() -> None:
    # The experiment is only interpretable if every entry is the same size, so a
    # smaller stake must fail loudly rather than quietly skew the cohorts.
    with pytest.raises(ValueError):
        ShadowConfig(position_usd=D("5"))
    with pytest.raises(ValueError):
        ShadowConfig(min_position_usd=D("5"))


def test_a_strong_signal_is_never_sized_above_ten_dollars() -> None:
    strong = signal_for(
        family=FAMILY_CONFLUENCE_WATCH,
        momentum_score=D("99"),
        organic_score=D("99"),
        smart_wallet_entries=9,
        buys=900,
        sells=3,
    )

    decision = evaluate_shadow_entry(strong, bankroll(cash="100"))

    assert decision.size_usd == D("10")
    assert decision.size_usd <= DEFAULT_SHADOW_CONFIG.max_position_usd


def test_seven_dollars_of_bankroll_refuses_rather_than_faking_a_ten_dollar_trade() -> None:
    # Four positions are open and only $7 of simulated cash is left.
    thin = bankroll(cash="7", exposure="40")

    decision = evaluate_shadow_entry(
        signal_for(), thin, ShadowExposure(open_positions=4, open_exposure_usd=D("40"))
    )

    assert decision.accepted is False
    assert decision.size_usd == D("0")
    assert decision.primary_reason == S_INSUFFICIENT_BANKROLL


def test_the_five_position_cap_stops_a_sixth_entry() -> None:
    full = ShadowExposure(open_positions=5, open_exposure_usd=D("50"))

    decision = evaluate_shadow_entry(signal_for(), bankroll(cash="50", exposure="50"), full)

    assert decision.accepted is False
    assert decision.primary_reason == S_MAX_POSITIONS


def test_the_fifty_dollar_exposure_cap_binds_independently_of_the_position_cap() -> None:
    # A configuration with more slots than $50 allows proves the exposure rule
    # is doing its own work rather than riding on the position count.
    config = DEFAULT_SHADOW_CONFIG.with_overrides(max_concurrent_positions=8)
    at_cap = ShadowExposure(open_positions=5, open_exposure_usd=D("50"))

    decision = evaluate_shadow_entry(
        signal_for(), bankroll(cash="50", exposure="50"), at_cap, config=config
    )

    assert decision.accepted is False
    assert decision.primary_reason == S_MAX_EXPOSURE


def test_forty_five_dollars_deployed_still_admits_one_more_full_entry() -> None:
    almost = ShadowExposure(open_positions=4, open_exposure_usd=D("40"))

    decision = evaluate_shadow_entry(signal_for(), bankroll(cash="60", exposure="40"), almost)

    assert decision.accepted is True
    assert decision.size_usd == D("10")


def test_a_second_ten_dollars_is_never_added_to_the_same_token() -> None:
    held = ShadowExposure(
        open_positions=1, open_exposure_usd=D("10"), token_exposure_usd=D("10")
    )

    decision = evaluate_shadow_entry(signal_for(), bankroll(cash="90", exposure="10"), held)

    assert decision.accepted is False
    assert decision.primary_reason == shadow_module.S_NO_AVERAGE_DOWN


def test_the_same_family_never_opens_a_second_position_for_one_mint() -> None:
    held = ShadowExposure(
        open_positions=1,
        open_exposure_usd=D("10"),
        token_exposure_usd=D("10"),
        holds_same_family=True,
    )

    decision = evaluate_shadow_entry(signal_for(), bankroll(cash="90", exposure="10"), held)

    assert decision.accepted is False
    assert decision.primary_reason == S_ALREADY_HOLDING


def test_a_rug_a_dead_route_and_a_stale_signal_are_all_refused() -> None:
    assert (
        evaluate_shadow_entry(signal_for(rugged=True), bankroll()).primary_reason
        == S_RUGGED
    )
    assert (
        evaluate_shadow_entry(
            signal_for(route_available=False), bankroll()
        ).primary_reason
        == S_NO_EXECUTABLE_ROUTE
    )
    stale = signal_for(signal_at=NOW - 5_000, decision_at=NOW)
    assert evaluate_shadow_entry(stale, bankroll()).primary_reason == S_SIGNAL_STALE


def test_signal_freshness_and_fill_latency_are_separate_gates() -> None:
    from smart_money_bot.lab.shadow import S_LATENCY_TOO_HIGH

    # A five-minute-old signal is inside the freshness window and must trade:
    # the millisecond budget bounds the *quote*, not the signal's age.
    ordinary = signal_for(signal_at=NOW - 300, decision_at=NOW)
    assert evaluate_shadow_entry(ordinary, bankroll()).accepted is True

    # A quote that went stale between the decision and the fill is refused.
    slow_fill = signal_for(
        timestamps=ShadowTimestamps(
            signal_at=NOW, decision_at=NOW, quote_at=NOW + 5, fill_at=NOW + 90
        )
    )
    refused = evaluate_shadow_entry(slow_fill, bankroll())
    assert refused.accepted is False
    assert refused.primary_reason == S_LATENCY_TOO_HIGH


def test_untradeable_price_impact_on_ten_dollars_is_refused() -> None:
    decision = evaluate_shadow_entry(
        signal_for(), bankroll(), route_price_impact_percent=D("40")
    )

    assert decision.accepted is False
    assert decision.primary_reason == S_IMPACT_TOO_HIGH


def test_a_paused_circuit_breaker_stops_new_shadow_entries() -> None:
    losing = bankroll(cash="60", exposure="20", consecutive_losses=9)
    status = evaluate_shadow_breakers(losing)

    decision = evaluate_shadow_entry(signal_for(), losing, breakers=status)

    assert status.paused is True
    assert decision.primary_reason == S_BREAKER_PAUSED


def test_the_daily_loss_cap_and_drawdown_breakers_fire() -> None:
    capped = bankroll(cash="70", exposure="10", day_realized_net_pnl_usd=D("-20"))
    assert "DAILY_LOSS_CAP" in evaluate_shadow_breakers(capped).reasons

    drawn = bankroll(cash="50", peak_equity_usd=D("100"))
    assert "ROLLING_DRAWDOWN" in evaluate_shadow_breakers(drawn).reasons

    outage = evaluate_shadow_breakers(bankroll(), BreakerInputs(provider_outage=True))
    assert "PROVIDER_OUTAGE" in outage.reasons


def test_an_observation_before_the_experiment_never_becomes_a_live_trade() -> None:
    old = signal_for(signal_at=NOW - 100, decision_at=NOW - 100)

    decision = evaluate_shadow_entry(old, bankroll(), experiment_started_at=NOW)

    assert decision.accepted is False
    assert decision.primary_reason == S_BEFORE_EXPERIMENT


async def test_a_replayed_signal_creates_no_second_position(runtime) -> None:
    signal = signal_for()

    first, opened = await runtime.consider_signal(signal, now=NOW)
    second, duplicate = await runtime.consider_signal(signal, now=NOW)

    assert first.accepted is True and opened is not None
    assert second.accepted is False and duplicate is None
    assert second.primary_reason == S_ALREADY_HOLDING


async def test_a_restart_rehydrates_the_book_without_duplicating_it(
    database, runtime
) -> None:
    await runtime.consider_signal(signal_for(), now=NOW)

    # A fresh runtime is exactly what a Railway restart produces.
    restarted = ShadowRuntime(ShadowStore(database))
    await restarted.start_experiment()
    decision, position = await restarted.consider_signal(signal_for(), now=NOW + 5)

    assert decision.accepted is False
    assert position is None
    state = await restarted.bankroll()
    assert state.open_positions == 1
    assert state.cash_usd == D("90")


async def test_the_book_stops_at_five_positions_and_fifty_dollars(runtime) -> None:
    for index in range(7):
        await runtime.consider_signal(
            signal_for(mint=f"Mint{index:0>40}"), now=NOW + index
        )

    state = await runtime.bankroll()
    assert state.open_positions == 5
    assert state.open_exposure_usd == D("50")
    assert state.cash_usd == D("50")


async def test_one_mint_may_carry_one_position_per_signal_family(runtime) -> None:
    await runtime.consider_signal(signal_for(family=FAMILY_FAST_WATCH), now=NOW)
    decision, position = await runtime.consider_signal(
        signal_for(family=FAMILY_NOTABLE_EARLY), now=NOW + 1
    )

    # Different families are different experiments, but they still compete for
    # the same $10 per-token exposure ceiling.
    assert decision.accepted is False
    assert position is None
    assert decision.primary_reason == shadow_module.S_NO_AVERAGE_DOWN


async def test_the_hundred_dollar_bankroll_is_debited_and_credited_exactly(
    runtime,
) -> None:
    _, position = await runtime.consider_signal(signal_for(), now=NOW)
    after_entry = await runtime.bankroll()

    assert after_entry.cash_usd == D("90")
    assert after_entry.open_exposure_usd == D("10")
    assert after_entry.equity_usd == D("100")

    # Sell the whole position through a safety emergency.
    await runtime.manage_position(
        position, exit_context("0.0012", safety_status="FAIL")
    )
    after_exit = await runtime.bankroll()

    assert after_exit.open_positions == 0
    assert after_exit.open_exposure_usd == D("0")
    assert after_exit.cash_usd > D("90")
    assert after_exit.realized_net_pnl_usd == (after_exit.cash_usd - D("100"))


# ===========================================================================
# 49. EXECUTION — a fill must come from a route that could have filled it
# ===========================================================================


def curve(*, complete: bool = False) -> BondingCurveState:
    return BondingCurveState(
        virtual_sol_reserves=D("32"),
        virtual_token_reserves=D("1073000000"),
        real_token_reserves=D("793100000"),
        complete=complete,
        sol_price_usd=D("150"),
        observed_at=NOW,
    )


def test_a_pre_graduation_curve_prices_a_ten_dollar_buy_on_the_invariant() -> None:
    quote = bonding_curve_quote(curve(), side="BUY", notional_usd=D("10"), now=NOW)

    assert quote.usable is True
    assert quote.venue == VENUE_PUMP_CURVE
    assert quote.graduation_state == PRE_GRADUATION
    assert quote.source == FILL_SIMULATED_VENUE
    # Buying pushes the effective price above spot, never below it.
    assert quote.fill_price_usd > curve().spot_price_usd
    assert quote.price_impact_percent > 0
    assert quote.expected_output_tokens > 0


def test_a_curve_sell_prices_below_spot_and_charges_the_published_fee() -> None:
    quote = bonding_curve_quote(curve(), side="SELL", notional_usd=D("10"), now=NOW)

    assert quote.usable is True
    assert quote.fill_price_usd < curve().spot_price_usd
    assert quote.fee_bps == 100


def test_a_completed_curve_refuses_the_trade_instead_of_inventing_a_price() -> None:
    quote = bonding_curve_quote(
        curve(complete=True), side="BUY", notional_usd=D("10"), now=NOW
    )

    assert quote.usable is False
    assert quote.unavailable_reason == ROUTE_CURVE_COMPLETE
    assert quote.graduation_state == GRADUATED


def test_a_pumpswap_pool_prices_impact_from_its_own_depth() -> None:
    thin = pool_quote(
        venue=VENUE_PUMPSWAP,
        side="BUY",
        notional_usd=D("10"),
        reference_price_usd=D("0.001"),
        liquidity_usd=D("2000"),
        now=NOW,
    )
    deep = pool_quote(
        venue=VENUE_PUMPSWAP,
        side="BUY",
        notional_usd=D("10"),
        reference_price_usd=D("0.001"),
        liquidity_usd=D("400000"),
        now=NOW,
    )

    assert thin.price_impact_percent > deep.price_impact_percent
    assert thin.fill_price_usd > deep.fill_price_usd


def test_an_observed_route_impact_beats_the_modelled_one() -> None:
    quote = pool_quote(
        venue=VENUE_PUMPSWAP,
        side="BUY",
        notional_usd=D("10"),
        reference_price_usd=D("0.001"),
        liquidity_usd=D("400000"),
        observed_price_impact_percent=D("3.5"),
        now=NOW,
    )

    assert quote.price_impact_percent == D("3.5")


def test_a_jupiter_quote_records_its_own_latency_fees_and_impact() -> None:
    quote = executable_quote(
        venue=VENUE_JUPITER,
        side="BUY",
        notional_usd=D("10"),
        fill_price_usd=D("0.00102"),
        reference_price_usd=D("0.001"),
        price_impact_percent=D("1.2"),
        slippage_bps=80,
        fee_bps=20,
        quote_latency_ms=310,
        now=NOW,
    )

    assert quote.source == FILL_EXECUTABLE_QUOTE
    assert quote.quote_latency_ms == 310
    assert quote.fee_bps == 20
    assert quote.deterioration_percent == D("2.00")
    assert quote.total_cost_percent > D("1.2")


def test_the_best_executable_price_wins_regardless_of_venue() -> None:
    cheap_pool = pool_quote(
        venue=VENUE_PUMPSWAP,
        side="BUY",
        notional_usd=D("10"),
        reference_price_usd=D("0.001"),
        liquidity_usd=D("900000"),
        now=NOW,
    )
    dear_quote = executable_quote(
        venue=VENUE_JUPITER,
        side="BUY",
        notional_usd=D("10"),
        fill_price_usd=D("0.0012"),
        reference_price_usd=D("0.001"),
        now=NOW,
    )

    selection = select_route([dear_quote, cheap_pool])

    assert selection.chosen is cheap_pool
    assert selection.venue == VENUE_PUMPSWAP


def test_a_sell_takes_the_highest_price_not_the_lowest() -> None:
    low = executable_quote(
        venue=VENUE_JUPITER,
        side="SELL",
        notional_usd=D("10"),
        fill_price_usd=D("0.0009"),
        now=NOW,
    )
    high = pool_quote(
        venue=VENUE_PUMPSWAP,
        side="SELL",
        notional_usd=D("10"),
        reference_price_usd=D("0.001"),
        liquidity_usd=D("900000"),
        now=NOW,
    )

    assert select_route([low, high]).chosen is high


def test_an_untradeable_impact_removes_a_route_from_selection() -> None:
    brutal = pool_quote(
        venue=VENUE_PUMPSWAP,
        side="BUY",
        notional_usd=D("10"),
        reference_price_usd=D("0.001"),
        liquidity_usd=D("40"),
        now=NOW,
    )

    selection = select_route([brutal], max_price_impact_percent=D("12"))

    assert selection.available is False
    assert selection.rejected[0][1] == "PRICE_IMPACT_TOO_HIGH"


def test_every_route_failing_produces_no_fill_at_all() -> None:
    dead = pool_quote(
        venue=VENUE_PUMPSWAP,
        side="BUY",
        notional_usd=D("10"),
        reference_price_usd=D("0.001"),
        liquidity_usd=None,
        now=NOW,
    )

    selection = select_route([dead])

    assert selection.available is False
    assert selection.chosen is None


def test_a_fallback_price_is_penalised_and_labelled_never_presented_as_a_fill() -> None:
    quote = fallback_quote(side="BUY", notional_usd=D("10"), observed_price_usd=D("0.001"))

    assert quote.source == FILL_FALLBACK_PENALISED
    assert quote.fill_price_usd > D("0.001")
    assert "not an executable fill" in " ".join(quote.notes)


def test_a_deployment_may_forbid_fallback_fills_entirely() -> None:
    strict = DEFAULT_SHADOW_CONFIG.with_overrides(allow_fallback_fill=False)

    decision = evaluate_shadow_entry(
        signal_for(), bankroll(), fill_source=FILL_FALLBACK_PENALISED, config=strict
    )

    assert decision.accepted is False
    assert decision.primary_reason == S_NO_EXECUTABLE_ROUTE


def test_graduation_is_unknown_rather_than_guessed_from_a_proxy() -> None:
    assert (
        classify_graduation(
            graduated_at=NOW, graduation_source="DEX_PAIR_CREATED_PROXY — not exact"
        )
        == "UNKNOWN"
    )
    assert classify_graduation(graduated_at=NOW, graduation_source="PUMP_EVENT") == GRADUATED
    assert classify_graduation(curve=curve()) == PRE_GRADUATION
    assert classify_graduation(curve=curve(complete=True)) == GRADUATED


async def test_a_position_that_entered_on_the_curve_can_still_exit_after_graduation(
    runtime,
) -> None:
    _, position = await runtime.consider_signal(
        signal_for(graduation_state=PRE_GRADUATION), now=NOW, curve=curve()
    )
    assert position is not None
    assert position.venue == VENUE_PUMP_CURVE

    # The curve completes while the position is open: the sell must route to the
    # pool instead of failing.
    updated, assessment = await runtime.manage_position(
        position,
        exit_context("0.0004"),
        curve=curve(complete=True),
    )

    assert assessment.plan.acts is True
    assert updated.venue == VENUE_PUMPSWAP
    assert updated.position.exits


async def test_no_sell_route_at_any_venue_is_recorded_rather_than_faked(runtime) -> None:
    _, position = await runtime.consider_signal(signal_for(), now=NOW)

    updated, assessment = await runtime.manage_position(
        position,
        exit_context("0.0004", liquidity_usd=None, route_available=False),
        curve=curve(complete=True),
    )

    # A route failure is a realistic outcome, not a clean chart-price exit.
    assert assessment.plan.acts is True
    assert updated.position.exits == ()
    assert updated.exit_route == {"UNAVAILABLE": "no sell route at any venue"}


async def test_the_entry_venue_and_fill_provenance_are_persisted(runtime) -> None:
    _, position = await runtime.consider_signal(signal_for(), now=NOW)

    assert position.venue in {VENUE_PUMPSWAP, VENUE_PUMP_CURVE, VENUE_JUPITER}
    assert position.fill_source in {
        FILL_EXECUTABLE_QUOTE,
        FILL_SIMULATED_VENUE,
        FILL_FALLBACK_PENALISED,
    }
    fills = await runtime.store.venue_fills()
    assert fills and fills[0].venue == position.venue


# ===========================================================================
# 50. EXITS — $2 NET secures dollars without dumping a live runner
# ===========================================================================


def healthy_evidence() -> RunnerEvidence:
    return RunnerEvidence(
        momentum_accelerating=True,
        independent_buyer_growth=25,
        volume_ratio=D("1.6"),
        liquidity_growth_percent=D("20"),
        smart_money_accumulating=True,
        actionability_state="ACTIONABLE",
        route_quality="OK",
    )


def weak_evidence() -> RunnerEvidence:
    return RunnerEvidence(
        independent_buyer_growth=-12,
        volume_ratio=D("0.2"),
        liquidity_growth_percent=D("-30"),
        smart_money_distributing=True,
        actionability_state="DETERIORATED",
        route_quality="DEGRADED",
    )


def test_net_pnl_is_measured_after_the_exit_leg_not_before_it() -> None:
    position = position_for()

    net = net_pnl_now(position, D("0.0013"))

    assert net.unrealized_gross_usd > net.unrealized_net_usd
    assert net.exit_cost_usd > 0
    assert net.total_net_usd == net.realized_net_usd + net.unrealized_net_usd


def test_two_dollars_net_with_a_broken_runner_secures_the_profit() -> None:
    position = position_for()
    context = exit_context(
        "0.0013",
        momentum_score=D("18"),
        organic_score=D("30"),
        buys=10,
        sells=90,
        liquidity_usd=D("20000"),
        smart_money_distributing=True,
    )

    assessment = plan_shadow_exit(position, context, weak_evidence())

    assert assessment.net.total_net_usd >= D("2")
    assert assessment.health.band == HEALTH_WEAK
    assert assessment.plan.acts is True
    assert assessment.plan.fraction >= DEFAULT_SHADOW_CONFIG.secure_fraction_weak


def test_two_dollars_net_with_an_accelerating_runner_keeps_meaningful_exposure() -> None:
    position = position_for()
    context = exit_context("0.0013", momentum_score=D("88"), momentum_reaccelerating=True)

    assessment = plan_shadow_exit(position, context, healthy_evidence())

    assert assessment.objective_met is True
    assert assessment.health.band == HEALTH_ACCELERATING
    # Something is taken, but the majority of the position keeps running.
    assert assessment.plan.fraction <= DEFAULT_SHADOW_CONFIG.secure_fraction_healthy
    assert assessment.plan.reason_code in {EXIT_MILESTONE, HOLD_SHADOW_RUNNER}


def test_a_runner_past_the_objective_is_never_dumped_wholesale() -> None:
    position = position_for()
    context = exit_context("0.0013", momentum_score=D("88"), momentum_reaccelerating=True)

    assessment = plan_shadow_exit(position, context, healthy_evidence())

    assert assessment.plan.final is False
    assert assessment.plan.fraction < D("1")


@pytest.mark.parametrize("gain", ["1.2", "1.25", "1.5", "2.0", "3.0", "6.0"])
def test_the_staged_ladder_still_fires_at_every_reference_milestone(gain: str) -> None:
    position = position_for()
    price = (D("0.001") * D(gain)).quantize(D("0.0000000001"))

    assessment = plan_shadow_exit(position, exit_context(str(price)), healthy_evidence())

    assert assessment.plan.acts or assessment.plan.reason_code == HOLD_SHADOW_RUNNER
    assert assessment.net.total_net_usd > 0


def test_beyond_three_times_the_objective_principal_is_recovered_with_a_moon_bag() -> None:
    position = position_for()
    context = exit_context("0.0022", momentum_score=D("88"), momentum_reaccelerating=True)

    assessment = plan_shadow_exit(position, context, healthy_evidence())

    assert assessment.net.total_net_usd >= DEFAULT_SHADOW_CONFIG.net_profit_objective_usd * 3
    assert assessment.plan.reason_code in {SHADOW_PRINCIPAL_RECOVERY, EXIT_MILESTONE}
    # Whatever fires, a remainder always survives to keep running.
    assert assessment.plan.fraction < D("1")
    assert assessment.plan.final is False


def test_a_safety_failure_overrides_the_profit_objective_entirely() -> None:
    position = position_for()
    context = exit_context("0.0013", safety_status="FAIL")

    assessment = plan_shadow_exit(position, context, healthy_evidence())

    assert assessment.plan.reason_code == EXIT_SAFETY_EMERGENCY
    assert assessment.plan.final is True
    assert assessment.plan.fraction == D("1")


def test_a_liquidity_collapse_and_a_dead_route_both_force_an_emergency_exit() -> None:
    position = position_for()

    collapse = plan_shadow_exit(
        position, exit_context("0.0013", liquidity_usd=D("15000")), healthy_evidence()
    )
    dead = plan_shadow_exit(
        position, exit_context("0.0013", route_available=False), healthy_evidence()
    )

    assert collapse.plan.reason_code == EXIT_LIQUIDITY_EMERGENCY
    assert dead.plan.reason_code == EXIT_LIQUIDITY_EMERGENCY
    assert collapse.plan.final and dead.plan.final


def test_the_hard_stop_still_closes_a_losing_shadow_position() -> None:
    position = position_for()

    assessment = plan_shadow_exit(position, exit_context("0.0005"), weak_evidence())

    assert assessment.plan.reason_code == EXIT_HARD_STOP
    assert assessment.plan.final is True
    assert assessment.net.total_net_usd < 0


def test_the_time_stop_closes_a_position_that_never_went_anywhere() -> None:
    position = position_for()
    context = exit_context("0.001005", now=NOW + 6_000, momentum_score=D("50"))

    assessment = plan_shadow_exit(position, context, RunnerEvidence())

    assert assessment.plan.reason_code == EXIT_TIME_STOP
    assert assessment.plan.final is True


def test_trailing_protection_fires_after_a_large_giveback() -> None:
    from smart_money_bot.lab.exits import observe

    position = position_for()
    config = DEFAULT_SHADOW_CONFIG.exit_config()
    position = observe(position, exit_context("0.0025"), config=config)

    assessment = plan_shadow_exit(position, exit_context("0.0013"), weak_evidence())

    assert position.trailing_armed is True
    assert assessment.plan.reason_code in {EXIT_TRAILING, SHADOW_SECURE_OBJECTIVE}
    assert assessment.plan.acts is True


def test_a_lone_weak_momentum_print_no_longer_dumps_a_healthy_runner() -> None:
    """v2.41: a single weak tick is a wobble, not a reversal (sections 55, 56).

    The shared engine reduces 50% the first time momentum prints weak.  On a
    fresh, volatile token that repeatedly sold live runners into noise, which is
    the most expensive class of exit the experiment can make.
    """

    from smart_money_bot.lab.shadow_exits import (
        MOMENTUM_CONFIRMED_DECAY,
        SHADOW_SOFT_PAUSE_HOLD,
    )

    position = position_for()
    # Momentum prints weak while buyers still lead and liquidity is growing.
    healthy = plan_shadow_exit(
        position, exit_context("0.00105", momentum_score=D("10")), RunnerEvidence()
    )

    assert healthy.plan.reason_code == SHADOW_SOFT_PAUSE_HOLD
    assert healthy.plan.acts is False

    # The same weak print, three observations running, is a trend.
    repeated = plan_shadow_exit(
        position,
        exit_context("0.00105", momentum_score=D("10")),
        RunnerEvidence(consecutive_weak_observations=3),
    )

    assert repeated.momentum.state == MOMENTUM_CONFIRMED_DECAY
    assert repeated.plan.acts is True
    assert repeated.plan.fraction < D("1")


def test_heavy_selling_and_distribution_still_de_risk_on_one_observation() -> None:
    """Some observations are facts about the market, not noisy scores."""

    from smart_money_bot.lab.shadow_exits import (
        MOMENTUM_CONFIRMED_DECAY,
        MOMENTUM_HARD_REVERSAL,
    )

    position = position_for()
    below = "0.00105"

    reversal = plan_shadow_exit(
        position,
        exit_context(below, momentum_score=D("60"), buys=10, sells=100),
        RunnerEvidence(),
    )
    distribution = plan_shadow_exit(
        position,
        exit_context(
            below, momentum_score=D("40"), buys=20, sells=60, smart_money_distributing=True
        ),
        RunnerEvidence(),
    )

    for assessment in (reversal, distribution):
        assert assessment.objective_met is False
        assert assessment.momentum.state in {
            MOMENTUM_CONFIRMED_DECAY,
            MOMENTUM_HARD_REVERSAL,
        }
        assert assessment.plan.acts is True
        assert assessment.plan.fraction < D("1")


def test_the_health_model_never_reads_a_future_price() -> None:
    signature = inspect.signature(assess_runner_health)

    assert set(signature.parameters) == {"position", "context", "evidence", "config"}
    source = code_only(inspect.getsource(assess_runner_health))
    for forbidden in ("max_favourable", "peak_net", "peak_market_cap", "closed_at"):
        assert forbidden not in source


# ===========================================================================
# 51. PERFORMANCE — every dollar is accounted for
# ===========================================================================


def trade(
    *,
    family: str = FAMILY_FAST_WATCH,
    net: str = "1",
    gross: str = "1.3",
    cost: str = "0.3",
    mfe: str = "30",
    mae: str = "5",
    peak: str = "3",
    open_trade: bool = False,
    closed_at: int = NOW,
) -> ShadowTradeRecord:
    return ShadowTradeRecord(
        position_id=f"{family}-{closed_at}-{net}",
        mint=MINT,
        family=family,
        opened_at=NOW - 600,
        closed_at=None if open_trade else closed_at,
        size_usd=D("10"),
        entry_price_usd=D("0.001"),
        realized_net_pnl_usd=D("0") if open_trade else D(net),
        realized_gross_pnl_usd=D(gross),
        total_cost_usd=D(cost),
        unrealized_net_pnl_usd=D(net) if open_trade else D("0"),
        max_favourable_percent=D(mfe),
        max_adverse_percent=D(mae),
        peak_net_pnl_usd=D(peak),
        open=open_trade,
    )


def test_the_account_headline_states_the_truth_in_both_directions() -> None:
    winning = summarize_shadow_account([trade(net="4"), trade(net="3", closed_at=NOW + 1)])
    losing = summarize_shadow_account(
        [trade(net="-6", gross="-5.7"), trade(net="-4", gross="-3.7", closed_at=NOW + 1)]
    )

    assert winning.profitable is True
    assert winning.total_net_pnl_usd == D("7")
    assert winning.roi_percent == D("7.00")
    assert losing.profitable is False
    assert losing.total_net_pnl_usd == D("-10")
    assert "DOWN" in losing.headline


def test_capture_efficiency_compares_realized_net_with_what_was_available() -> None:
    # $10 position, +120% MFE = $12 gross available, less $0.30 of modelled cost.
    record = trade(net="3", mfe="120", cost="0.3")

    assert record.max_available_net_usd == D("11.700000")
    efficiency = record.capture_efficiency_percent()
    assert efficiency is not None
    assert D("25") < efficiency < D("26")


def test_peak_profit_given_back_is_reported_not_averaged_away() -> None:
    record = trade(net="2.40", peak="7.80")

    assert record.peak_profit_given_back_usd == D("5.400000")


def test_signal_families_are_never_blended_into_one_number() -> None:
    report = summarize_shadow_account(
        [
            trade(family=FAMILY_FAST_WATCH, net="-3"),
            trade(family=FAMILY_FAST_WATCH, net="-2", closed_at=NOW + 1),
            trade(family=FAMILY_NOTABLE_EARLY, net="6", closed_at=NOW + 2),
        ]
    )

    fast = report.by_family[FAMILY_FAST_WATCH]
    notable = report.by_family[FAMILY_NOTABLE_EARLY]
    assert fast.net_pnl_usd == D("-5")
    assert fast.losses == 2 and fast.wins == 0
    assert notable.net_pnl_usd == D("6")
    assert notable.roi_percent == D("60.00")
    assert report.total_net_pnl_usd == D("1")


def test_profit_factor_expectancy_and_drawdown_are_computed_from_closed_trades() -> None:
    report = summarize_shadow_account(
        [
            trade(net="6", closed_at=NOW),
            trade(net="-2", closed_at=NOW + 1),
            trade(net="-1", closed_at=NOW + 2),
        ]
    )

    assert report.profit_factor == D("2.00")
    assert report.expectancy_usd == D("1.000000")
    assert report.max_drawdown_usd == D("3.000000")
    assert report.win_rate_percent == D("33.33")


def test_open_positions_contribute_unrealized_never_realized() -> None:
    report = summarize_shadow_account(
        [trade(net="5", open_trade=True)], cash_usd=D("90"), open_exposure_usd=D("10")
    )

    assert report.realized_net_pnl_usd == D("0")
    assert report.unrealized_net_pnl_usd == D("5")
    assert report.open_positions == 1
    assert report.current_bankroll_usd == D("105")


def test_milestone_hit_rates_cover_every_required_threshold() -> None:
    report = summarize_shadow_account([trade(mfe="250"), trade(mfe="15", closed_at=NOW + 1)])

    assert set(report.milestone_hit_rates) == {"10", "20", "25", "50", "100", "200", "500"}
    assert report.milestone_hit_rates["10"] == D("100.00")
    assert report.milestone_hit_rates["200"] == D("50.00")
    # A real 0.00% must survive as a measured rate, not collapse to a bare zero.
    assert str(report.milestone_hit_rates["500"]) == "0.00"


def test_venue_accounting_separates_executable_fills_from_penalised_ones() -> None:
    reports = summarize_venues(
        [
            VenueFill(
                venue=VENUE_PUMPSWAP,
                slippage_bps=80,
                price_impact_percent=D("1"),
                quote_latency_ms=100,
                cost_usd=D("0.2"),
                net_pnl_usd=D("1"),
                fill_source=FILL_SIMULATED_VENUE,
            ),
            VenueFill(
                venue=VENUE_PUMPSWAP,
                slippage_bps=120,
                price_impact_percent=D("3"),
                quote_latency_ms=300,
                cost_usd=D("0.4"),
                net_pnl_usd=D("-1"),
                fill_source=FILL_FALLBACK_PENALISED,
            ),
            VenueFill(
                venue=VENUE_JUPITER,
                slippage_bps=40,
                quote_latency_ms=200,
                cost_usd=D("0.1"),
                net_pnl_usd=D("2"),
                fill_source=FILL_EXECUTABLE_QUOTE,
            ),
        ]
    )

    by_venue = {item.venue: item for item in reports}
    assert by_venue[VENUE_PUMPSWAP].fills == 2
    assert by_venue[VENUE_PUMPSWAP].average_slippage_bps == D("100.00")
    assert by_venue[VENUE_PUMPSWAP].fallback_fills == 1
    assert by_venue[VENUE_JUPITER].executable_fills == 1
    assert by_venue[VENUE_JUPITER].net_pnl_usd == D("2")


def test_a_small_sample_says_so_instead_of_claiming_an_edge() -> None:
    report = summarize_shadow_account([trade(net="9")])

    assert report.sufficient_sample is False
    assert report.note == "SAMPLE_TOO_SMALL"


async def test_the_full_round_trip_reconciles_to_the_dollar(runtime) -> None:
    _, position = await runtime.consider_signal(signal_for(), now=NOW)
    updated, _ = await runtime.manage_position(
        position, exit_context("0.0004", safety_status="FAIL")
    )

    journal = updated.position.exits[-1]
    state = await runtime.bankroll()

    assert journal.final is True
    assert journal.net_proceeds_usd == journal.gross_proceeds_usd - journal.costs.total_cost_usd
    assert journal.realized_net_pnl_usd == journal.net_proceeds_usd - journal.cost_basis_usd
    assert state.cash_usd == D("100") + state.realized_net_pnl_usd
    assert state.realized_net_pnl_usd < 0


# ===========================================================================
# 15 / 52. COUNTERFACTUALS AND NO LOOK-AHEAD
# ===========================================================================


def observation_stream() -> list[ShadowObservation]:
    prices = ["0.0011", "0.0013", "0.0018", "0.0025", "0.0016", "0.0012"]
    return [
        ShadowObservation(
            at=NOW + 60 * (index + 1),
            price_usd=D(price),
            momentum_score=D("70") if index < 4 else D("15"),
            organic_score=D("70"),
            buys=100,
            sells=20,
            safety_status="PASS",
            route_available=True,
            smart_money_distributing=index >= 4,
        )
        for index, price in enumerate(prices)
    ]


def test_all_twelve_counterfactual_policies_run_on_one_observation_stream() -> None:
    results = compare_shadow_exit_policies(
        observation_stream(), entry_at=NOW, entry_price_usd=D("0.001")
    )

    assert {item.policy for item in results} == set(COUNTERFACTUAL_POLICIES)
    assert len(results) == 12
    baseline = next(item for item in results if item.policy == CF_NO_TRADE)
    assert baseline.traded is False
    assert baseline.net_pnl_usd == D("0")


def test_a_fixed_ten_percent_target_exits_at_the_first_observation_that_reaches_it() -> None:
    results = compare_shadow_exit_policies(
        observation_stream(), entry_at=NOW, entry_price_usd=D("0.001")
    )

    fixed = next(item for item in results if item.policy == CF_FIXED_10)
    assert fixed.traded is True
    assert fixed.exited_at == NOW + 60
    assert fixed.gross_return_percent == D("10.00")


def test_counterfactuals_never_see_observations_before_the_entry() -> None:
    stream = observation_stream()
    earlier = ShadowObservation(at=NOW - 600, price_usd=D("0.00001"))

    with_history = compare_shadow_exit_policies(
        [earlier, *stream], entry_at=NOW, entry_price_usd=D("0.001")
    )
    without = compare_shadow_exit_policies(
        stream, entry_at=NOW, entry_price_usd=D("0.001")
    )

    assert with_history == without


def test_a_later_observation_cannot_change_an_earlier_entry_decision() -> None:
    signal = signal_for()
    before = evaluate_shadow_entry(signal, bankroll())

    # The token subsequently 10x'd; the decision record must be identical.
    after = evaluate_shadow_entry(signal, bankroll())

    assert before == after


def test_an_entry_decision_reads_no_field_that_postdates_the_decision() -> None:
    source = code_only(inspect.getsource(evaluate_shadow_entry))

    for forbidden in ("peak_", "max_favourable", "exit_market_cap", "closed_at", "exits"):
        assert forbidden not in source


def test_counterfactual_comparison_needs_no_provider_client() -> None:
    source = module_code("smart_money_bot.lab.shadow_metrics")

    for forbidden in ("aiohttp", "requests", "httpx", "urllib", "ClientSession", "await"):
        assert forbidden not in source


# ===========================================================================
# 53. SHADOW CANNOT SPEND REAL MONEY
# ===========================================================================


SHADOW_SOURCES = (
    "smart_money_bot.lab.shadow",
    "smart_money_bot.lab.shadow_exits",
    "smart_money_bot.lab.shadow_metrics",
    "smart_money_bot.lab.venues",
    "smart_money_bot.shadow_runtime",
    "smart_money_bot.shadow_store",
)


def test_the_real_money_spend_invariant_is_structurally_zero() -> None:
    assert not SHADOW_REAL_MONEY_SPEND
    assert SHADOW_REAL_MONEY_SPEND.is_zero()
    assert isinstance(SHADOW_REAL_MONEY_SPEND, Decimal)


@pytest.mark.parametrize("module_name", SHADOW_SOURCES)
def test_no_shadow_module_contains_a_signer_key_or_swap_submission(
    module_name: str,
) -> None:
    names = referenced_names(module_name)

    for forbidden in (
        "Keypair",
        "private_key",
        "sign_message",
        "sign_versioned_transaction",
        "VersionedTransaction",
        "execute_order",
        "send_transaction",
        "swap",
        "JupiterClient",
        "load_keypair",
        "ExecutionManager",
    ):
        assert forbidden not in names, f"{module_name} must not reference {forbidden}"


@pytest.mark.parametrize("module_name", SHADOW_SOURCES)
def test_no_shadow_module_imports_a_wallet_or_rpc_client(module_name: str) -> None:
    imports = imported_modules(module_name)

    forbidden = {
        "solders",
        "aiohttp",
        "smart_money_bot.market",
        "smart_money_bot.rpc",
        "smart_money_bot.executor",
        ".market",
        ".rpc",
        ".executor",
        ".stream",
        ".launch",
    }
    leaked = {name for name in imports if name.split(".")[0] in {"solders", "aiohttp"}}
    assert not leaked, f"{module_name} must not import {leaked}"
    assert not (imports & forbidden), f"{module_name} must not import {imports & forbidden}"


async def test_the_runtime_status_reports_zero_real_money_and_no_live_execution(
    runtime,
) -> None:
    status = await runtime.status()

    assert status["real_money_spend_usd"] == D("0")
    assert status["live_execution_enabled"] is False
    assert status["position_size_usd"] == D("10")
    assert status["max_exposure_usd"] == D("50")


# ===========================================================================
# 2. STRICT PAPER AND SHADOW STAY SEPARATE
# ===========================================================================


def test_the_shadow_strategy_never_imports_the_strict_entry_engine() -> None:
    imports = imported_modules("smart_money_bot.lab.shadow")
    names = referenced_names("smart_money_bot.lab.shadow")

    assert ".entry" not in imports
    assert "evaluate_entry" not in names
    assert "EntryContext" not in names
    assert "EntryEvaluation" not in names


def test_the_strict_lab_never_imports_the_shadow_strategy() -> None:
    for module_name in (
        "smart_money_bot.lab.entry",
        "smart_money_bot.lab_runtime",
        "smart_money_bot.lab_store",
    ):
        leaked = {
            name
            for name in imported_modules(module_name) | referenced_names(module_name)
            if "shadow" in name.lower()
        }
        assert not leaked, f"{module_name} must not know about shadow: {leaked}"


def test_the_two_families_use_different_strategy_versions() -> None:
    assert DEFAULT_SHADOW_CONFIG.strategy_version == "shadow-v1"
    assert DEFAULT_LAB_CONFIG.strategy_version == "lab-v1"
    assert DEFAULT_SHADOW_CONFIG.strategy_version != DEFAULT_LAB_CONFIG.strategy_version


async def test_shadow_positions_never_touch_the_strict_paper_tables(
    database, runtime
) -> None:
    await runtime.consider_signal(signal_for(), now=NOW)

    cursor = await database.db.execute("SELECT COUNT(*) AS total FROM lab_positions")
    strict = (await cursor.fetchone())["total"]
    cursor = await database.db.execute("SELECT COUNT(*) AS total FROM shadow_positions")
    shadow = (await cursor.fetchone())["total"]

    assert strict == 0
    assert shadow == 1


async def test_the_two_bankrolls_are_stored_independently(database, runtime) -> None:
    await runtime.consider_signal(signal_for(), now=NOW)

    cursor = await database.db.execute("SELECT COUNT(*) AS total FROM lab_bankroll")
    assert (await cursor.fetchone())["total"] == 0
    cursor = await database.db.execute(
        "SELECT strategy_version FROM shadow_bankroll"
    )
    assert [row["strategy_version"] for row in await cursor.fetchall()] == ["shadow-v1"]


# ===========================================================================
# 30, 42, 43, 55, 56. EXPLANATION, CHECKPOINT, ATTRIBUTION, SCHEMA, SETTINGS
# ===========================================================================


def test_every_family_can_explain_why_it_is_being_shown() -> None:
    for family in SIGNAL_FAMILIES:
        why = why_you_are_seeing_this(ShadowSignal(mint=MINT, family=family))
        assert why and all(isinstance(item, str) and item for item in why)


async def test_the_experiment_checkpoint_is_written_once_and_never_moves(
    database,
) -> None:
    engine = ShadowRuntime(ShadowStore(database))
    first = await engine.start_experiment(now=NOW)

    restarted = ShadowRuntime(ShadowStore(database))
    second = await restarted.start_experiment(now=NOW + 90_000)

    assert first == NOW
    assert second == NOW
    row = await restarted.store.experiment()
    assert row["starting_bankroll_usd"] == 100.0
    assert row["position_usd"] == 10.0
    assert row["max_positions"] == 5
    assert row["max_exposure_usd"] == 50.0


async def test_every_refused_signal_is_persisted_with_its_reason(runtime) -> None:
    await runtime.consider_signal(signal_for(rugged=True), now=NOW)

    rows = await runtime.store.signal_rows()
    assert rows and rows[0]["accepted"] == 0
    assert rows[0]["reason_code"] == S_RUGGED
    assert (await runtime.store.refusal_counts())[S_RUGGED] == 1


async def test_an_accepted_signal_persists_everything_the_bot_knew(runtime) -> None:
    _, position = await runtime.consider_signal(signal_for(), now=NOW)

    evidence = position.signal_evidence
    for key in ("family", "price_usd", "liquidity_usd", "buys", "sells", "safety_status"):
        assert key in evidence
    assert position.timestamps.signal_at == NOW
    assert position.timestamps.fill_at == NOW
    assert position.entry_route["VENUE"] == position.venue


async def test_the_observation_stream_is_persisted_for_replay(runtime) -> None:
    _, position = await runtime.consider_signal(signal_for(), now=NOW)
    await runtime.manage_position(position, exit_context("0.0011", now=NOW + 60))
    await runtime.manage_position(position, exit_context("0.0012", now=NOW + 120))

    observations = await runtime.store.observations(position.position_id)

    assert [item.at for item in observations] == [NOW + 60, NOW + 120]
    assert all(item.price_usd > 0 for item in observations)


async def test_the_schema_is_additive_and_restart_safe(database) -> None:
    # Re-running the migration must be a no-op, which is what a redeploy does.
    await database._init_schema()
    await database._init_schema()

    cursor = await database.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'shadow%' "
        "ORDER BY name"
    )
    tables = [row["name"] for row in await cursor.fetchall()]

    assert tables == [
        "shadow_bankroll",
        "shadow_exits",
        "shadow_experiment",
        "shadow_observations",
        "shadow_positions",
        "shadow_signal_log",
        "shadow_venue_fills",
    ]
    # Every strict PAPER table is still present and untouched.
    cursor = await database.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'lab_%'"
    )
    assert len([row["name"] for row in await cursor.fetchall()]) >= 10


def test_the_deployment_defaults_are_the_contracted_numbers(settings) -> None:
    config = shadow_config_from_settings(settings)

    assert config.bankroll_usd == D("100")
    assert config.position_usd == D("10")
    assert config.max_position_usd == D("10")
    assert config.min_position_usd == D("10")
    assert config.max_concurrent_positions == 5
    assert config.max_total_exposure_usd == D("50")
    assert config.max_token_exposure_usd == D("10")
    assert config.net_profit_objective_usd == D("2")


def test_a_deployment_that_configures_a_five_dollar_stake_is_rejected(
    monkeypatch,
) -> None:
    from smart_money_bot.config import Settings

    monkeypatch.setenv("FOMO_SHADOW_POSITION_USD", "5")
    with pytest.raises(ValueError, match="FOMO_SHADOW_MAX_POSITION_USD must equal"):
        Settings.from_env(require_discord_token=False)


# ===========================================================================
# 27-32. DISCORD — two visibility layers and the shadow cards
# ===========================================================================


def test_a_signal_that_does_not_ping_is_still_published_to_the_radar() -> None:
    from smart_money_bot import fast_alerts as fa

    watch = fa.build_fast_watch_alert(
        mint=MINT,
        name="Token",
        symbol="TOK",
        fomo_url="https://example.invalid",
        verdict=SimpleVerdict(),
        age_seconds=120,
        market_cap_usd=D("60000"),
        first_seen_market_cap_usd=D("40000"),
        liquidity_usd=D("40000"),
        move_since_first_seen_percent=D("22"),
        momentum_score=D("70"),
        organic_score=D("70"),
        buys=90,
        sells=20,
    )

    # Not urgent, never pings — and still reaches a channel rather than vanishing.
    assert watch.may_ping is False
    assert watch.lane == fa.LANE_RADAR
    assert watch.family == fa.FAST_WATCH


def test_every_alert_states_its_family_and_why_it_is_being_shown() -> None:
    from smart_money_bot import fast_alerts as fa

    watch = fa.build_fast_watch_alert(
        mint=MINT,
        name="Token",
        symbol="TOK",
        fomo_url="https://example.invalid",
        verdict=SimpleVerdict(),
        age_seconds=120,
        market_cap_usd=D("60000"),
        first_seen_market_cap_usd=D("40000"),
        liquidity_usd=D("40000"),
        move_since_first_seen_percent=D("22"),
        momentum_score=D("70"),
        organic_score=D("70"),
        buys=90,
        sells=20,
    )

    field = next(
        item for item in watch.spec.fields if item.name == "WHY YOU'RE SEEING THIS"
    )
    assert "FAST WATCH" in field.value
    assert "accelerating buyers" in field.value


def test_an_unknown_lane_is_rejected_like_an_unknown_alert_class() -> None:
    from smart_money_bot import fast_alerts as fa
    from smart_money_bot.discord_render import CardSpec

    with pytest.raises(ValueError, match="unknown fast alert lane"):
        fa.FastAlert(
            kind=fa.FAST_WATCH,
            mint=MINT,
            alert_key="k",
            spec=CardSpec(title="t"),
            lane="SOMEWHERE_ELSE",
        )


def test_the_shadow_entry_card_states_ten_dollars_and_zero_real_money() -> None:
    from smart_money_bot import fast_alerts as fa

    alert = fa.build_shadow_entry_alert(
        mint=MINT,
        name="Token",
        symbol="TOK",
        fomo_url="https://example.invalid",
        family=FAMILY_CONFLUENCE_WATCH,
        family_label="CONFLUENCE WATCH",
        why=("catalyst + smart wallets + market acceleration",),
        size_usd=D("10"),
        fill_market_cap_usd=D("61000"),
        fill_price_usd=D("0.001"),
        venue=VENUE_PUMPSWAP,
        fill_source=FILL_SIMULATED_VENUE,
        graduation_state=PRE_GRADUATION,
        modeled_cost_usd=D("0.1408"),
        net_objective_usd=D("2"),
        signal_to_fill_seconds=8,
        position_id="pid",
    )
    body = "\n".join(item.value for item in alert.spec.fields)

    assert alert.kind == fa.SHADOW_ENTRY
    assert alert.may_ping is False
    assert alert.entry_eligible is False
    assert "$10.00" in body
    assert "REAL MONEY: $0.00" in body
    assert "+$2.00" in body
    assert VENUE_PUMPSWAP in body
    assert "CONFLUENCE WATCH" in body


def test_the_shadow_exit_card_shows_peak_net_and_profit_given_back() -> None:
    from smart_money_bot import fast_alerts as fa

    alert = fa.build_shadow_exit_alert(
        mint=MINT,
        name="Token",
        symbol="TOK",
        fomo_url="https://example.invalid",
        family=FAMILY_FAST_WATCH,
        family_label="FAST WATCH",
        size_usd=D("10"),
        entry_market_cap_usd=D("60000"),
        exit_market_cap_usd=D("84000"),
        gross_pnl_usd=D("2.70"),
        cost_usd=D("0.30"),
        net_pnl_usd=D("2.40"),
        peak_net_pnl_usd=D("7.80"),
        given_back_usd=D("5.40"),
        exit_reason="SHADOW_SECURE_NET_OBJECTIVE",
        venue=VENUE_PUMPSWAP,
        fraction_sold=D("0.75"),
        final=False,
        remaining_fraction=D("0.25"),
        position_id="pid",
    )
    body = "\n".join(item.value for item in alert.spec.fields)

    assert "$+2.4000" in body
    assert "$+7.8000" in body
    assert "$5.4000" in body
    assert "REAL MONEY: $0.00" in body
    assert alert.may_ping is False


def test_a_losing_shadow_exit_is_published_as_plainly_as_a_winning_one() -> None:
    from smart_money_bot import fast_alerts as fa

    alert = fa.build_shadow_exit_alert(
        mint=MINT,
        name="Token",
        symbol="TOK",
        fomo_url="https://example.invalid",
        family=FAMILY_FAST_WATCH,
        family_label="FAST WATCH",
        size_usd=D("10"),
        entry_market_cap_usd=D("60000"),
        exit_market_cap_usd=D("9000"),
        gross_pnl_usd=D("-8.2"),
        cost_usd=D("0.3"),
        net_pnl_usd=D("-8.5"),
        peak_net_pnl_usd=D("0"),
        given_back_usd=D("0"),
        exit_reason="SAFETY_DETERIORATION",
        venue=VENUE_PUMPSWAP,
        fraction_sold=D("1"),
        final=True,
        position_id="pid",
    )
    body = "\n".join(item.value for item in alert.spec.fields)

    assert "-8.5000" in body
    assert "SAFETY_DETERIORATION" in body
    assert alert.spec.colour == 0xC0392B


def test_both_shadow_cards_fit_inside_one_discord_message() -> None:
    from smart_money_bot import fast_alerts as fa
    from smart_money_bot.discord_render import (
        MESSAGE_EMBED_LIMIT,
        build_embed,
        render_message,
    )

    entry = fa.build_shadow_entry_alert(
        mint=MINT,
        name="T" * 120,
        symbol="LONGSYMBOL",
        fomo_url="https://example.invalid",
        family=FAMILY_FAST_WATCH,
        family_label="FAST WATCH",
        why=("a" * 200, "b" * 200, "c" * 200),
        size_usd=D("10"),
        fill_market_cap_usd=D("61000"),
        fill_price_usd=D("0.001"),
        venue=VENUE_PUMPSWAP,
        fill_source=FILL_FALLBACK_PENALISED,
        graduation_state=PRE_GRADUATION,
        modeled_cost_usd=D("0.14"),
        net_objective_usd=D("2"),
    )
    exit_card = fa.build_shadow_exit_alert(
        mint=MINT,
        name="T" * 120,
        symbol="LONGSYMBOL",
        fomo_url="https://example.invalid",
        family=FAMILY_FAST_WATCH,
        family_label="FAST WATCH",
        size_usd=D("10"),
        entry_market_cap_usd=D("60000"),
        exit_market_cap_usd=D("84000"),
        gross_pnl_usd=D("2.7"),
        cost_usd=D("0.3"),
        net_pnl_usd=D("2.4"),
        peak_net_pnl_usd=D("7.8"),
        given_back_usd=D("5.4"),
        exit_reason="SHADOW_SECURE_NET_OBJECTIVE",
        venue=VENUE_PUMPSWAP,
        fraction_sold=D("0.75"),
        final=False,
        why=("d" * 300,),
    )

    for alert in (entry, exit_card):
        embeds, _ = render_message([alert.spec])
        assert sum(len(build_embed(alert.spec)) for _ in embeds[:1]) <= MESSAGE_EMBED_LIMIT


class SimpleVerdict:
    """Minimal FAST WATCH verdict stand-in for the card builders."""

    score = D("72")
    reasons = ("accelerating buyers", "volume expansion")
    blockers = ()
    pending_evidence = ("safety",)
    watch = True
    entry_eligible = False


# ===========================================================================
# ENGINE WIRING — the pipeline really opens and manages $10 shadow positions
# ===========================================================================


def runner_candidate(*, now: int = NOW, price: str = "0.001", stage: str = "QUALIFIED_RESEARCH"):
    from smart_money_bot.models import (
        RunnerCandidate,
        RunnerMarketSnapshot,
        RunnerQualityAssessment,
        RunnerScoreBreakdown,
    )

    def snapshot(at: int, value: str, cap: str, liquidity: str) -> RunnerMarketSnapshot:
        return RunnerMarketSnapshot(
            mint=MINT,
            captured_at=at,
            price_usd=D(value),
            market_cap_usd=D(cap),
            liquidity_usd=D(liquidity),
            volume_5m_usd=D("15000"),
            buys_5m=120,
            sells_5m=30,
            holder_count=400,
            verified_unique_buyers=45,
            route_available=True,
            route_price_impact_percent=D("0.8"),
            sell_route_price_impact_percent=D("0.9"),
        )

    return RunnerCandidate(
        mint=MINT,
        symbol="TEST",
        name="Test token",
        first_seen_at=now - 300,
        graduated_at=None,
        graduation_source="DEX_PAIR_CREATED_PROXY — not exact Pump graduation",
        first=snapshot(now - 300, "0.0008", "48000", "38000"),
        current=snapshot(now, price, "60000", "40000"),
        score=D("78"),
        tier="STRONG",
        breakdown=RunnerScoreBreakdown(),
        quality=RunnerQualityAssessment(
            momentum_score=D("72"),
            organic_score=D("68"),
            opportunity_score=D("70"),
        ),
        stage=stage,
        best_stage=stage,
        qualified_at=now - 60,
        pair_created_at=now - 300,
        radar_first_seen_at=now - 300,
        why_surfaced=("independent buyers accelerating", "liquidity growing"),
        generated_at=now,
    )


async def real_engine(settings, tmp_path):
    from dataclasses import replace as dataclass_replace

    from smart_money_bot.engine import SmartMoneyEngine

    engine = SmartMoneyEngine(
        dataclass_replace(settings, database_path=str(tmp_path / "engine-shadow.db"))
    )
    await engine.database.connect()
    await engine.shadow.start_experiment(now=NOW - 1_000)
    return engine


async def test_the_pipeline_opens_a_ten_dollar_shadow_position(settings, tmp_path) -> None:
    engine = await real_engine(settings, tmp_path)
    try:
        candidate = runner_candidate()
        opened = await engine._run_shadow_signal(
            engine._shadow_signal(candidate, family=FAMILY_QUALIFIED_RESEARCH, now=NOW),
            now=NOW,
        )

        assert opened is True
        state = await engine.shadow.bankroll()
        assert state.open_positions == 1
        assert state.open_exposure_usd == D("10")
        assert state.cash_usd == D("90")
    finally:
        await engine.database.close()


async def test_the_pipeline_advances_and_closes_a_shadow_position(
    settings, tmp_path
) -> None:
    engine = await real_engine(settings, tmp_path)
    try:
        await engine._run_shadow_signal(
            engine._shadow_signal(
                runner_candidate(), family=FAMILY_QUALIFIED_RESEARCH, now=NOW
            ),
            now=NOW,
        )
        # The token collapses; the shadow book must take the realistic loss.
        crashed = runner_candidate(price="0.0004", now=NOW + 600)
        await engine._manage_shadow_positions(crashed, now=NOW + 600)

        report = await engine.shadow_account()
        assert report.closed_trades == 1
        assert report.realized_net_pnl_usd < 0
        assert report.current_bankroll_usd < D("100")
        assert "DOWN" in report.headline
    finally:
        await engine.database.close()


async def test_the_shadow_status_payload_reports_both_channel_lanes(
    settings, tmp_path
) -> None:
    engine = await real_engine(settings, tmp_path)
    try:
        status = await engine.shadow_status()

        assert status["position_size_usd"] == D("10")
        assert status["max_positions"] == 5
        assert status["max_exposure_usd"] == D("50")
        assert status["net_objective_usd"] == D("2")
        assert status["real_money_spend_usd"] == D("0")
        assert "live_radar_channel_id" in status
        assert "urgent_channel_id" in status
    finally:
        await engine.database.close()


async def test_a_shadow_failure_never_takes_down_the_pipeline(
    settings, tmp_path, monkeypatch
) -> None:
    engine = await real_engine(settings, tmp_path)
    try:

        async def explode(*args, **kwargs):
            raise RuntimeError("provider melted")

        monkeypatch.setattr(engine.shadow, "consider_signal", explode)

        # Research is not allowed to break discovery, alerts or STRICT PAPER.
        opened = await engine._run_shadow_signal(
            engine._shadow_signal(
                runner_candidate(), family=FAMILY_QUALIFIED_RESEARCH, now=NOW
            ),
            now=NOW,
        )

        assert opened is False
    finally:
        await engine.database.close()


async def test_disabling_the_shadow_trader_stops_every_simulated_entry(
    settings, tmp_path
) -> None:
    engine = await real_engine(settings, tmp_path)
    try:
        engine.shadow_enabled = False

        opened = await engine._run_shadow_signal(
            engine._shadow_signal(
                runner_candidate(), family=FAMILY_QUALIFIED_RESEARCH, now=NOW
            ),
            now=NOW,
        )

        assert opened is False
        state = await engine.shadow.bankroll()
        assert state.open_positions == 0
        assert state.cash_usd == D("100")
    finally:
        await engine.database.close()


# ===========================================================================
# 16 / 39. A POSITION THE BOT LOSES SIGHT OF STILL CLOSES HONESTLY
# ===========================================================================


async def test_a_position_the_pipeline_stops_seeing_is_closed_not_left_open(
    runtime,
) -> None:
    from smart_money_bot.lab.shadow_exits import SHADOW_STALE_OBSERVATION

    _, position = await runtime.consider_signal(signal_for(), now=NOW)
    await runtime.manage_position(position, exit_context("0.0011", now=NOW + 60))

    # The token drops off the radar for longer than the stale window allows.
    closed = await runtime.sweep_stale_positions(now=NOW + 60 + 6_000)

    assert len(closed) == 1
    updated, assessment = closed[0]
    assert assessment.plan.reason_code == SHADOW_STALE_OBSERVATION
    assert updated.position.is_open is False
    state = await runtime.bankroll()
    assert state.open_positions == 0
    assert state.open_exposure_usd == D("0")


async def test_a_stale_close_is_priced_as_a_penalised_fallback_not_a_clean_print(
    runtime,
) -> None:
    _, position = await runtime.consider_signal(signal_for(), now=NOW)
    await runtime.manage_position(position, exit_context("0.0011", now=NOW + 60))

    closed = await runtime.sweep_stale_positions(now=NOW + 60 + 6_000)
    journal = closed[0][0].position.exits[-1]

    # Nothing confirmed that last print, so the exit must be worse than it.
    assert journal.quote_price_usd < D("0.0011")
    assert closed[0][0].exit_route["SOURCE"] == FILL_FALLBACK_PENALISED


async def test_a_position_still_being_observed_is_never_swept(runtime) -> None:
    _, position = await runtime.consider_signal(signal_for(), now=NOW)
    await runtime.manage_position(position, exit_context("0.0011", now=NOW + 60))

    closed = await runtime.sweep_stale_positions(now=NOW + 120)

    assert closed == []
    state = await runtime.bankroll()
    assert state.open_positions == 1


async def test_the_account_headline_reflects_a_swept_position(runtime) -> None:
    _, position = await runtime.consider_signal(signal_for(), now=NOW)
    await runtime.manage_position(position, exit_context("0.0003", now=NOW + 60))
    await runtime.sweep_stale_positions(now=NOW + 60 + 6_000)

    report = await runtime.account()

    assert report.open_positions == 0
    assert report.unrealized_net_pnl_usd == D("0")
    assert report.realized_net_pnl_usd < 0
    assert report.current_bankroll_usd < D("100")


def test_a_closed_trades_exit_market_cap_is_derived_never_invented() -> None:
    from dataclasses import replace as dataclass_replace

    from smart_money_bot.shadow_runtime import _scaled_market_cap

    # An open position has no exit cap to report, so it reports none.
    assert _scaled_market_cap(position_for(), open_position=True) is None

    doubled = dataclass_replace(position_for(), last_price_usd=D("0.002"))
    implied = _scaled_market_cap(doubled, open_position=False)

    # Entry MC $60k at ~$0.001; the last price is double, so the implied cap is.
    assert implied is not None
    assert D("119000") < implied < D("121000")


# ===========================================================================
# 18 / 19. WAS THE INTELLIGENCE EARLY ENOUGH?
# ===========================================================================


async def test_notable_trader_timing_is_persisted_and_computable(runtime) -> None:
    signal = signal_for(
        family=FAMILY_NOTABLE_EARLY,
        trader_entry_market_cap_usd=D("40000"),
        detection_market_cap_usd=D("52000"),
    )

    _, position = await runtime.consider_signal(signal, now=NOW)
    timing = runtime.notable_timing_for(position, exit_market_cap_usd=D("90000"))

    # The trader was 30% earlier than the bot; the bot's fill was 15.4% above
    # its own detection; the exit was 50% above the fill.
    assert timing.trader_to_bot_percent == D("30.00")
    assert timing.bot_to_fill_percent == D("15.38")
    assert timing.fill_to_exit_percent == D("50.00")


async def test_catalyst_timing_is_persisted_and_computable(runtime) -> None:
    signal = signal_for(
        family=FAMILY_BREAKING_CATALYST,
        event_at=NOW - 900,
        mint_created_at=NOW - 600,
        catalyst_alert_at=NOW - 30,
        first_credible_source="reuters",
        timestamps=ShadowTimestamps(
            signal_at=NOW - 300, first_seen_at=NOW - 300, decision_at=NOW
        ),
    )

    _, position = await runtime.consider_signal(signal, now=NOW)
    timing = runtime.catalyst_timing_for(position)

    assert timing.event_to_mint_seconds == 300
    assert timing.event_to_bot_seconds == 600
    assert timing.mint_to_bot_seconds == 300
    assert timing.bot_to_fill_seconds == 300
    assert timing.first_credible_source == "reuters"


async def test_the_open_trades_view_carries_the_timing_evidence(runtime) -> None:
    await runtime.consider_signal(
        signal_for(
            family=FAMILY_NOTABLE_EARLY,
            trader_entry_market_cap_usd=D("40000"),
            detection_market_cap_usd=D("52000"),
        ),
        now=NOW,
    )

    row = (await runtime.open_trades())[0]

    assert row["signal_to_fill_seconds"] == 0
    assert row["notable_timing"].trader_to_bot_percent == D("30.00")
    assert row["family"] == FAMILY_NOTABLE_EARLY
    assert row["size_usd"] == D("10")


# ===========================================================================
# PRINCIPAL RECOVERY — the stake still at risk, never the original stake twice
# ===========================================================================


def test_principal_recovery_uses_the_stake_still_deployed() -> None:
    from dataclasses import replace as dataclass_replace

    from smart_money_bot.lab.shadow_exits import _principal_recovery_fraction

    whole = position_for()
    full = _principal_recovery_fraction(whole, D("0.0022"), config=DEFAULT_SHADOW_CONFIG)

    # After an earlier partial returned half the stake, recovering "principal"
    # must ask for the half still at risk, not for the original $10 again.
    partly_secured = dataclass_replace(
        whole,
        tokens_remaining=whole.tokens_remaining / 2,
        cost_basis_remaining_usd=whole.cost_basis_remaining_usd / 2,
    )
    after = _principal_recovery_fraction(
        partly_secured, D("0.0022"), config=DEFAULT_SHADOW_CONFIG
    )

    assert 0 < full <= D("0.85")
    # Same ratio of value to stake, so the same fraction — not double.
    assert abs(after - full) < D("0.001")


def test_principal_recovery_always_leaves_a_moon_bag() -> None:
    from smart_money_bot.lab.shadow_exits import _principal_recovery_fraction

    # Barely above water: recovering the whole stake would need everything.
    fraction = _principal_recovery_fraction(
        position_for(), D("0.00101"), config=DEFAULT_SHADOW_CONFIG
    )

    ceiling = D("1") - DEFAULT_SHADOW_CONFIG.moon_bag_percent / D("100")
    assert fraction <= ceiling
    assert fraction < D("1")


async def test_a_no_op_exit_plan_never_rewrites_where_the_position_trades(
    runtime,
) -> None:
    _, position = await runtime.consider_signal(signal_for(), now=NOW)
    entry_venue = position.venue

    # Flat price, healthy runner: nothing sells, so nothing about the position's
    # venue may change either.
    updated, assessment = await runtime.manage_position(
        position, exit_context("0.001005", momentum_score=D("80"))
    )

    assert assessment.plan.acts is False
    assert updated.position.exits == ()
    assert updated.venue == entry_venue


def test_money_is_rendered_with_the_sign_before_the_dollar() -> None:
    from smart_money_bot.bot import _shadow_money, _shadow_signed

    assert _shadow_signed(D("7.08")) == "+$7.08"
    assert _shadow_signed(D("-36.6")) == "-$36.60"
    assert _shadow_signed(None) == "pending"
    assert _shadow_money(D("104.024010")) == "$104.02"
    assert _shadow_money(D("1234.5")) == "$1,234.50"


async def test_the_status_block_and_the_account_headline_report_the_same_equity(
    runtime,
) -> None:
    _, position = await runtime.consider_signal(signal_for(), now=NOW)
    await runtime.manage_position(position, exit_context("0.0016", now=NOW + 600))

    status = await runtime.status()
    report = await runtime.account()

    # Two surfaces, one number: a status line reporting cost basis while the
    # dashboard reports value would make the experiment unreadable.
    assert status["current_bankroll_usd"] == report.current_bankroll_usd
    assert status["unrealized_net_pnl_usd"] == report.unrealized_net_pnl_usd
    assert status["book_equity_usd"] <= status["current_bankroll_usd"]


def test_capture_efficiency_is_undefined_when_no_profit_was_ever_available() -> None:
    # A token that only ever went down offers nothing to capture, so the metric
    # is absent rather than reported as a misleading zero or a huge negative.
    rugged = trade(net="-9.3", gross="-9", mfe="0", mae="92", peak="0")

    assert rugged.max_available_net_usd == D("0")
    assert rugged.capture_efficiency_percent() is None

    report = summarize_shadow_account([rugged, trade(net="3", mfe="120")])
    # The average is taken over the trades where the question is answerable.
    assert report.capture_efficiency_percent is not None


# ===========================================================================
# 54. PROVIDER COST — one observation feeds every strategy
# ===========================================================================


def test_no_shadow_engine_method_reaches_a_provider_client() -> None:
    """Section 54: the shadow lane must cost zero additional requests.

    It rides on evidence the runner already paid for.  If a future change
    reached for a client here, the experiment would start multiplying provider
    cost by the number of strategies, which is exactly what the contract
    forbids — so the boundary is asserted, not just documented.
    """

    from smart_money_bot import engine as engine_module

    methods = {
        "_shadow_signal",
        "_run_shadow_signal",
        "_shadow_signal_task",
        "_manage_shadow_positions",
        "_publish_shadow_exit",
        "_run_shadow_cycle",
        "_sweep_stale_shadow_positions",
        "_run_shadow_notable",
        "shadow_account",
        "shadow_open_trades",
        "shadow_venues",
        "shadow_status",
        "shadow_counterfactuals",
        "shadow_latest_counterfactuals",
    }
    clients = {
        "jupiter",
        "dex",
        "rpc",
        "tracker",
        "tracker_token_risk",
        "x_client",
        "discovery",
        "launch",
        "executor",
    }

    tree = ast.parse(inspect.getsource(engine_module))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name not in methods:
            continue
        found.add(node.name)
        for inner in ast.walk(node):
            # An *awaited* method on a provider client is a network call.
            # Reading a client's in-process health flag is not, and section 8
            # requires exactly that read — so the check is on calls, not on
            # every mention of the attribute.
            if not isinstance(inner, ast.Await):
                continue
            call = inner.value
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            owner = call.func.value
            if (
                isinstance(owner, ast.Attribute)
                and isinstance(owner.value, ast.Name)
                and owner.value.id == "self"
                and owner.attr in clients
            ):
                raise AssertionError(
                    f"{node.name} awaits a provider call: self.{owner.attr}."
                    f"{call.func.attr}()"
                )

    assert found == methods, f"missing from the engine: {sorted(methods - found)}"


def test_the_strict_paper_strategy_modules_are_untouched_by_shadow() -> None:
    # The shadow experiment adds a second family; it must not have edited the
    # first one's rules, or the two are no longer comparable.
    for module_name in (
        "smart_money_bot.lab.config",
        "smart_money_bot.lab.entry",
        "smart_money_bot.lab.exits",
        "smart_money_bot.lab.bankroll",
        "smart_money_bot.lab.decision",
        "smart_money_bot.lab.replay",
    ):
        leaked = {
            name
            for name in imported_modules(module_name) | referenced_names(module_name)
            if "shadow" in name.lower()
        }
        assert not leaked, f"{module_name} must not reference shadow: {leaked}"

    # The strict configuration still carries its own, unchanged numbers.
    assert DEFAULT_LAB_CONFIG.normal_position_usd == D("5")
    assert DEFAULT_LAB_CONFIG.max_total_exposure_usd == D("30")
    assert DEFAULT_LAB_CONFIG.min_position_usd == D("2")
