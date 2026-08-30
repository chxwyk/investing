"""What the $100 shadow account actually did, and what it left on the table.

Sections 13-19, 35-37, 43 and 44 of the Shadow contract.

Three things separate this from the strict lab's report:

* **Meaningful dollars.**  Every cohort is measured in NET dollars on a fixed
  $10 stake, so a "+40%" that was $0.30 net cannot hide behind a percentage.
* **What was available.**  Capture efficiency and peak-profit-given-back answer
  "was the exit too slow?", which a realized-only report structurally cannot.
* **Which signal family earned it.**  A blended number cannot tell you whether
  to keep watching FAST WATCH or only notable wallets, so nothing is blended.

The maximum favourable excursion used for capture efficiency is **evaluation
only**.  It is computed after the fact from persisted observations and is never
an input to :mod:`smart_money_bot.lab.shadow_exits`; the no-look-ahead tests
assert that separation directly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .costs import estimate_round_trip_cost
from .shadow import (
    DEFAULT_SHADOW_CONFIG,
    SIGNAL_FAMILIES,
    ShadowConfig,
)

ZERO = Decimal("0")
UNIT = Decimal("0.000001")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")

SAMPLE_TOO_SMALL = "SAMPLE_TOO_SMALL"

#: Return milestones the experiment reports on (section 10).
RETURN_MILESTONES: tuple[Decimal, ...] = (
    Decimal("10"),
    Decimal("20"),
    Decimal("25"),
    Decimal("50"),
    Decimal("100"),
    Decimal("200"),
    Decimal("500"),
)

# --- counterfactual exit policies (section 15) -------------------------------
CF_EXISTING_STAGED = "A_EXISTING_STAGED"
CF_NET_OBJECTIVE = "B_NET_2_DYNAMIC"
CF_FIXED_10 = "C_FIXED_10"
CF_FIXED_20 = "D_FIXED_20"
CF_FIXED_25 = "E_FIXED_25"
CF_FIXED_50 = "F_FIXED_50"
CF_FIXED_100 = "G_FIXED_100"
CF_TRAILING = "H_TRAILING_RUNNER"
CF_STAGED_LADDER = "I_STAGED_10_25_50_100"
CF_MOMENTUM = "J_MOMENTUM_ADAPTIVE"
CF_SMART_MONEY = "K_SMART_MONEY_AWARE"
CF_NO_TRADE = "L_NO_TRADE"

COUNTERFACTUAL_POLICIES: tuple[str, ...] = (
    CF_EXISTING_STAGED,
    CF_NET_OBJECTIVE,
    CF_FIXED_10,
    CF_FIXED_20,
    CF_FIXED_25,
    CF_FIXED_50,
    CF_FIXED_100,
    CF_TRAILING,
    CF_STAGED_LADDER,
    CF_MOMENTUM,
    CF_SMART_MONEY,
    CF_NO_TRADE,
)

_FIXED_TARGETS: dict[str, Decimal] = {
    CF_FIXED_10: Decimal("10"),
    CF_FIXED_20: Decimal("20"),
    CF_FIXED_25: Decimal("25"),
    CF_FIXED_50: Decimal("50"),
    CF_FIXED_100: Decimal("100"),
}

# --- loss attribution (section 43) -------------------------------------------
CAUSE_BAD_SIGNAL = "BAD_SIGNAL"
CAUSE_LATE_DETECTION = "LATE_DETECTION"
CAUSE_BAD_EXECUTION = "BAD_EXECUTION"
CAUSE_RUG = "RUG"
CAUSE_LIQUIDITY = "LIQUIDITY"
CAUSE_EXIT_STRATEGY = "EXIT_STRATEGY"
CAUSE_PROVIDER_FAILURE = "PROVIDER_FAILURE"

LOSS_CAUSES: tuple[str, ...] = (
    CAUSE_BAD_SIGNAL,
    CAUSE_LATE_DETECTION,
    CAUSE_BAD_EXECUTION,
    CAUSE_RUG,
    CAUSE_LIQUIDITY,
    CAUSE_EXIT_STRATEGY,
    CAUSE_PROVIDER_FAILURE,
)


@dataclass(frozen=True, slots=True)
class ShadowObservation:
    """One persisted post-entry observation.  Feeds every counterfactual.

    Section 54: this is the *single* observation stream.  Twelve policies read
    the same rows, so evaluating all of them costs zero additional provider
    requests.
    """

    at: int
    price_usd: Decimal
    market_cap_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_usd: Decimal | None = None
    momentum_score: Decimal | None = None
    organic_score: Decimal | None = None
    buys: int = 0
    sells: int = 0
    independent_buyers: int | None = None
    safety_status: str = "UNKNOWN"
    route_available: bool = True
    smart_money_distributing: bool = False
    smart_money_accumulating: bool = False


@dataclass(frozen=True, slots=True)
class ShadowTradeRecord:
    """One completed (or still open) $10 simulated trade, as persisted."""

    position_id: str
    mint: str
    family: str
    symbol: str = ""
    opened_at: int = 0
    closed_at: int | None = None
    size_usd: Decimal = Decimal("10")
    entry_price_usd: Decimal = ZERO
    entry_market_cap_usd: Decimal | None = None
    exit_market_cap_usd: Decimal | None = None
    realized_net_pnl_usd: Decimal = ZERO
    realized_gross_pnl_usd: Decimal = ZERO
    total_cost_usd: Decimal = ZERO
    unrealized_net_pnl_usd: Decimal = ZERO
    max_favourable_percent: Decimal = ZERO
    max_adverse_percent: Decimal = ZERO
    peak_net_pnl_usd: Decimal = ZERO
    close_reason: str = ""
    venue: str = "UNKNOWN"
    fill_source: str = ""
    graduation_state: str = "UNKNOWN"
    open: bool = False
    #: Section 41: only trades opened after the checkpoint count as the forward
    #: experiment.  A replayed or backfilled trade is reported separately.
    forward: bool = True

    @property
    def net_pnl_usd(self) -> Decimal:
        return (self.realized_net_pnl_usd + self.unrealized_net_pnl_usd).quantize(UNIT)

    @property
    def net_return_percent(self) -> Decimal:
        if self.size_usd <= 0:
            return ZERO
        return (self.net_pnl_usd / self.size_usd * HUNDRED).quantize(CENT)

    @property
    def peak_profit_given_back_usd(self) -> Decimal:
        """Section 14: peak NET minus final NET, floored at zero."""

        return max(ZERO, (self.peak_net_pnl_usd - self.net_pnl_usd)).quantize(UNIT)

    @property
    def max_available_net_usd(self) -> Decimal:
        """The best NET this position could have realized after entry.

        Uses the observed maximum favourable excursion less one modelled
        round-trip cost, so "available" means available *after costs* — never a
        gross chart number the strategy could not have banked.
        """

        if self.size_usd <= 0 or self.max_favourable_percent <= 0:
            return ZERO
        gross = self.size_usd * self.max_favourable_percent / HUNDRED
        return max(ZERO, gross - self.total_cost_usd).quantize(UNIT)

    def capture_efficiency_percent(self) -> Decimal | None:
        """Section 13: realized NET as a share of the best NET available."""

        available = self.max_available_net_usd
        if available <= 0:
            return None
        return (self.net_pnl_usd / available * HUNDRED).quantize(CENT)


@dataclass(frozen=True, slots=True)
class ShadowCohortReport:
    """One signal family's forward record (section 17)."""

    family: str = ""
    trades: int = 0
    open_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate_percent: Decimal | None = None
    net_pnl_usd: Decimal = ZERO
    gross_pnl_usd: Decimal = ZERO
    cost_usd: Decimal = ZERO
    deployed_usd: Decimal = ZERO
    roi_percent: Decimal | None = None
    profit_factor: Decimal | None = None
    expectancy_usd: Decimal | None = None
    max_drawdown_usd: Decimal = ZERO
    max_drawdown_percent: Decimal | None = None
    average_mfe_percent: Decimal | None = None
    average_mae_percent: Decimal | None = None
    best_trade_usd: Decimal | None = None
    worst_trade_usd: Decimal | None = None

    @property
    def label(self) -> str:
        return self.family.replace("_", " ")


@dataclass(frozen=True, slots=True)
class ShadowAccountReport:
    """The headline answer: is the $100 shadow account making money? (§16, §44)"""

    starting_bankroll_usd: Decimal = Decimal("100")
    current_bankroll_usd: Decimal = Decimal("100")
    cash_usd: Decimal = Decimal("100")
    realized_net_pnl_usd: Decimal = ZERO
    unrealized_net_pnl_usd: Decimal = ZERO
    open_positions: int = 0
    open_exposure_usd: Decimal = ZERO
    closed_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate_percent: Decimal | None = None
    roi_percent: Decimal = ZERO
    profit_factor: Decimal | None = None
    expectancy_usd: Decimal | None = None
    average_trade_usd: Decimal | None = None
    median_trade_usd: Decimal | None = None
    average_winner_usd: Decimal | None = None
    average_loser_usd: Decimal | None = None
    max_drawdown_percent: Decimal = ZERO
    max_drawdown_usd: Decimal = ZERO
    objective_hit_rate_percent: Decimal | None = None
    milestone_hit_rates: Mapping[str, Decimal] = field(default_factory=dict)
    average_mfe_percent: Decimal | None = None
    average_mae_percent: Decimal | None = None
    capture_efficiency_percent: Decimal | None = None
    profit_giveback_usd: Decimal = ZERO
    total_cost_usd: Decimal = ZERO
    by_family: Mapping[str, ShadowCohortReport] = field(default_factory=dict)
    sufficient_sample: bool = False
    note: str = SAMPLE_TOO_SMALL

    @property
    def profitable(self) -> bool:
        return self.total_net_pnl_usd > 0

    @property
    def total_net_pnl_usd(self) -> Decimal:
        return (self.realized_net_pnl_usd + self.unrealized_net_pnl_usd).quantize(UNIT)

    @property
    def headline(self) -> str:
        """One line an operator can read without any other context."""

        delta = self.total_net_pnl_usd
        verdict = "UP" if delta > 0 else "DOWN" if delta < 0 else "FLAT"
        return (
            f"${self.starting_bankroll_usd} → ${self.current_bankroll_usd} "
            f"({verdict} ${abs(delta):.2f}, {self.roi_percent:+.2f}%)"
        )


def summarize_shadow_cohort(
    trades: Sequence[ShadowTradeRecord],
    *,
    family: str = "",
    config: ShadowConfig = DEFAULT_SHADOW_CONFIG,
) -> ShadowCohortReport:
    """Aggregate one family.  No losing trade is ever excluded (section 39)."""

    if not trades:
        return ShadowCohortReport(family=family)

    closed = [trade for trade in trades if not trade.open]
    nets = [trade.net_pnl_usd for trade in trades]
    wins = [value for value in (t.net_pnl_usd for t in closed) if value > 0]
    losses = [value for value in (t.net_pnl_usd for t in closed) if value <= 0]
    deployed = sum((trade.size_usd for trade in trades), ZERO)
    gross = sum((trade.realized_gross_pnl_usd for trade in trades), ZERO)
    cost = sum((trade.total_cost_usd for trade in trades), ZERO)
    net = sum(nets, ZERO)

    equity = ZERO
    peak = ZERO
    drawdown = ZERO
    for trade in sorted(closed, key=lambda item: item.closed_at or item.opened_at):
        equity += trade.net_pnl_usd
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)

    loss_total = sum((abs(value) for value in losses), ZERO)
    mfes = [trade.max_favourable_percent for trade in trades]
    maes = [trade.max_adverse_percent for trade in trades]
    return ShadowCohortReport(
        family=family or (trades[0].family if trades else ""),
        trades=len(trades),
        open_trades=sum(1 for trade in trades if trade.open),
        wins=len(wins),
        losses=len(losses),
        win_rate_percent=_rate(len(wins), len(closed)),
        net_pnl_usd=net.quantize(UNIT),
        gross_pnl_usd=gross.quantize(UNIT),
        cost_usd=cost.quantize(UNIT),
        deployed_usd=deployed.quantize(UNIT),
        roi_percent=(
            (net / deployed * HUNDRED).quantize(CENT) if deployed > 0 else None
        ),
        profit_factor=(
            (sum(wins, ZERO) / loss_total).quantize(CENT) if loss_total > 0 else None
        ),
        expectancy_usd=(
            (sum((t.net_pnl_usd for t in closed), ZERO) / Decimal(len(closed))).quantize(UNIT)
            if closed
            else None
        ),
        max_drawdown_usd=drawdown.quantize(UNIT),
        max_drawdown_percent=(
            (drawdown / config.bankroll_usd * HUNDRED).quantize(CENT)
            if config.bankroll_usd > 0
            else None
        ),
        average_mfe_percent=_mean(mfes),
        average_mae_percent=_mean(maes),
        best_trade_usd=max(nets) if nets else None,
        worst_trade_usd=min(nets) if nets else None,
    )


def summarize_shadow_account(
    trades: Sequence[ShadowTradeRecord],
    *,
    starting_bankroll_usd: Decimal | None = None,
    cash_usd: Decimal | None = None,
    open_exposure_usd: Decimal | None = None,
    config: ShadowConfig = DEFAULT_SHADOW_CONFIG,
) -> ShadowAccountReport:
    """The whole experiment, honestly.  Losers included, rugs included."""

    starting = (
        starting_bankroll_usd if starting_bankroll_usd is not None else config.bankroll_usd
    )
    if not trades:
        return ShadowAccountReport(
            starting_bankroll_usd=starting,
            current_bankroll_usd=cash_usd if cash_usd is not None else starting,
            cash_usd=cash_usd if cash_usd is not None else starting,
            by_family={},
        )

    closed = [trade for trade in trades if not trade.open]
    open_trades = [trade for trade in trades if trade.open]
    realized = sum((trade.realized_net_pnl_usd for trade in trades), ZERO)
    unrealized = sum((trade.unrealized_net_pnl_usd for trade in open_trades), ZERO)
    exposure = (
        open_exposure_usd
        if open_exposure_usd is not None
        else sum((trade.size_usd for trade in open_trades), ZERO)
    )
    cash = cash_usd if cash_usd is not None else (starting + realized - exposure)
    equity_now = (cash + exposure + unrealized).quantize(UNIT)

    closed_nets = [trade.net_pnl_usd for trade in closed]
    wins = [value for value in closed_nets if value > 0]
    losses = [value for value in closed_nets if value <= 0]
    loss_total = sum((abs(value) for value in losses), ZERO)

    equity = starting
    peak = starting
    drawdown_usd = ZERO
    for trade in sorted(closed, key=lambda item: item.closed_at or item.opened_at):
        equity += trade.net_pnl_usd
        peak = max(peak, equity)
        drawdown_usd = max(drawdown_usd, peak - equity)

    by_family: dict[str, ShadowCohortReport] = {}
    for name in SIGNAL_FAMILIES:
        cohort = [trade for trade in trades if trade.family == name]
        if cohort:
            by_family[name] = summarize_shadow_cohort(cohort, family=name, config=config)

    captures = [
        value
        for value in (trade.capture_efficiency_percent() for trade in trades)
        if value is not None
    ]
    giveback = sum((trade.peak_profit_given_back_usd for trade in trades), ZERO)
    objective_hits = sum(
        1 for trade in closed if trade.net_pnl_usd >= config.net_profit_objective_usd
    )
    milestones = {
        # An explicit ``is None`` check, because ``Decimal("0.00") or ZERO``
        # would silently rewrite a real 0.00% hit rate as a bare 0.
        str(milestone): (
            rate
            if (
                rate := _rate(
                    sum(
                        1
                        for trade in trades
                        if trade.max_favourable_percent >= milestone
                    ),
                    len(trades),
                )
            )
            is not None
            else ZERO
        )
        for milestone in RETURN_MILESTONES
    }
    total_net = (realized + unrealized).quantize(UNIT)
    return ShadowAccountReport(
        starting_bankroll_usd=starting,
        current_bankroll_usd=equity_now,
        cash_usd=cash.quantize(UNIT),
        realized_net_pnl_usd=realized.quantize(UNIT),
        unrealized_net_pnl_usd=unrealized.quantize(UNIT),
        open_positions=len(open_trades),
        open_exposure_usd=exposure.quantize(UNIT),
        closed_trades=len(closed),
        wins=len(wins),
        losses=len(losses),
        win_rate_percent=_rate(len(wins), len(closed)),
        roi_percent=(
            (total_net / starting * HUNDRED).quantize(CENT) if starting > 0 else ZERO
        ),
        profit_factor=(
            (sum(wins, ZERO) / loss_total).quantize(CENT) if loss_total > 0 else None
        ),
        expectancy_usd=(
            (sum(closed_nets, ZERO) / Decimal(len(closed))).quantize(UNIT) if closed else None
        ),
        average_trade_usd=(
            (sum(closed_nets, ZERO) / Decimal(len(closed))).quantize(UNIT) if closed else None
        ),
        median_trade_usd=_median(closed_nets),
        average_winner_usd=(
            (sum(wins, ZERO) / Decimal(len(wins))).quantize(UNIT) if wins else None
        ),
        average_loser_usd=(
            (sum(losses, ZERO) / Decimal(len(losses))).quantize(UNIT) if losses else None
        ),
        max_drawdown_usd=drawdown_usd.quantize(UNIT),
        max_drawdown_percent=(
            (drawdown_usd / starting * HUNDRED).quantize(CENT) if starting > 0 else ZERO
        ),
        objective_hit_rate_percent=_rate(objective_hits, len(closed)),
        milestone_hit_rates=milestones,
        average_mfe_percent=_mean([trade.max_favourable_percent for trade in trades]),
        average_mae_percent=_mean([trade.max_adverse_percent for trade in trades]),
        capture_efficiency_percent=_mean(captures),
        profit_giveback_usd=giveback.quantize(UNIT),
        total_cost_usd=sum((trade.total_cost_usd for trade in trades), ZERO).quantize(UNIT),
        by_family=by_family,
        sufficient_sample=len(closed) >= config.min_forward_sample,
        note="" if len(closed) >= config.min_forward_sample else SAMPLE_TOO_SMALL,
    )


@dataclass(frozen=True, slots=True)
class VenueReport:
    """Fill quality per venue (section 37)."""

    venue: str = ""
    fills: int = 0
    average_slippage_bps: Decimal | None = None
    average_impact_percent: Decimal | None = None
    average_latency_ms: int | None = None
    total_cost_usd: Decimal = ZERO
    net_pnl_usd: Decimal = ZERO
    average_deterioration_percent: Decimal | None = None
    executable_fills: int = 0
    fallback_fills: int = 0


@dataclass(frozen=True, slots=True)
class VenueFill:
    """One persisted simulated fill on one venue."""

    venue: str
    side: str = "BUY"
    slippage_bps: int = 0
    price_impact_percent: Decimal = ZERO
    quote_latency_ms: int = 0
    cost_usd: Decimal = ZERO
    net_pnl_usd: Decimal = ZERO
    deterioration_percent: Decimal | None = None
    fill_source: str = ""


def summarize_venues(fills: Sequence[VenueFill]) -> tuple[VenueReport, ...]:
    """Compare the same $10 trade across every venue that quoted it."""

    grouped: dict[str, list[VenueFill]] = {}
    for fill in fills:
        grouped.setdefault(fill.venue, []).append(fill)
    reports: list[VenueReport] = []
    for venue, group in sorted(grouped.items()):
        deteriorations = [
            item.deterioration_percent
            for item in group
            if item.deterioration_percent is not None
        ]
        reports.append(
            VenueReport(
                venue=venue,
                fills=len(group),
                average_slippage_bps=_mean([Decimal(item.slippage_bps) for item in group]),
                average_impact_percent=_mean(
                    [item.price_impact_percent for item in group]
                ),
                average_latency_ms=(
                    round(sum(item.quote_latency_ms for item in group) / len(group))
                    if group
                    else None
                ),
                total_cost_usd=sum((item.cost_usd for item in group), ZERO).quantize(UNIT),
                net_pnl_usd=sum((item.net_pnl_usd for item in group), ZERO).quantize(UNIT),
                average_deterioration_percent=_mean(deteriorations),
                executable_fills=sum(
                    1 for item in group if item.fill_source == "EXECUTABLE_QUOTE"
                ),
                fallback_fills=sum(
                    1 for item in group if item.fill_source == "FALLBACK_PENALISED"
                ),
            )
        )
    return tuple(reports)


@dataclass(frozen=True, slots=True)
class CounterfactualResult:
    """What one alternative exit policy would have realized (section 15)."""

    policy: str
    traded: bool = False
    exited_at: int | None = None
    exit_price_usd: Decimal | None = None
    gross_return_percent: Decimal = ZERO
    net_return_percent: Decimal = ZERO
    net_pnl_usd: Decimal = ZERO
    cost_percent: Decimal = ZERO
    notes: tuple[str, ...] = ()


def compare_shadow_exit_policies(
    observations: Sequence[ShadowObservation],
    *,
    entry_at: int,
    entry_price_usd: Decimal,
    size_usd: Decimal | None = None,
    price_impact_percent: Decimal | None = None,
    config: ShadowConfig = DEFAULT_SHADOW_CONFIG,
) -> tuple[CounterfactualResult, ...]:
    """Run all twelve policies over one persisted observation stream.

    No look-ahead: every policy walks the series forward and may only decide
    from the observation it is standing on.  No provider request is made — this
    is arithmetic on rows that already exist (section 54).
    """

    notional = size_usd if size_usd is not None else config.position_usd
    series = tuple(
        sorted((item for item in observations if item.at >= entry_at), key=lambda i: i.at)
    )
    cost = estimate_round_trip_cost(
        notional,
        buy_price_impact_percent=price_impact_percent,
        sell_price_impact_percent=price_impact_percent,
        config=config,
    )
    cost_percent = cost.total_cost_percent

    def build(
        policy: str,
        exited_at: int | None,
        exit_price: Decimal | None,
        gross: Decimal,
        notes: tuple[str, ...] = (),
        traded: bool = True,
    ) -> CounterfactualResult:
        net = (gross - cost_percent).quantize(CENT) if traded else ZERO
        return CounterfactualResult(
            policy=policy,
            traded=traded,
            exited_at=exited_at,
            exit_price_usd=exit_price,
            gross_return_percent=gross.quantize(CENT) if traded else ZERO,
            net_return_percent=net,
            net_pnl_usd=(notional * net / HUNDRED).quantize(UNIT) if traded else ZERO,
            cost_percent=cost_percent if traded else ZERO,
            notes=notes,
        )

    results: list[CounterfactualResult] = [
        build(CF_NO_TRADE, None, None, ZERO, ("baseline: never entered",), traded=False)
    ]
    if not series or entry_price_usd <= 0:
        for policy in COUNTERFACTUAL_POLICIES:
            if policy == CF_NO_TRADE:
                continue
            results.append(
                build(policy, None, None, ZERO, ("no observations after entry",), traded=False)
            )
        return tuple(results)

    for policy in COUNTERFACTUAL_POLICIES:
        if policy == CF_NO_TRADE:
            continue
        exited_at, exit_price, gross, notes = _run_policy(
            policy, series, entry_price_usd, config=config
        )
        results.append(build(policy, exited_at, exit_price, gross, notes))
    return tuple(results)


def _run_policy(
    policy: str,
    series: Sequence[ShadowObservation],
    entry_price: Decimal,
    *,
    config: ShadowConfig,
) -> tuple[int | None, Decimal | None, Decimal, tuple[str, ...]]:
    """Walk the series forward for one policy.  Never reads ahead."""

    peak = entry_price
    realized = ZERO
    remaining = Decimal("1")
    ladder = tuple((Decimal(gain), Decimal(fraction)) for gain, fraction in config.exit_ladder)
    taken: set[str] = set()
    objective_percent = config.objective_percent
    stop = -config.hard_stop_loss_percent

    for item in series:
        price = item.price_usd
        if price <= 0:
            continue
        peak = max(peak, price)
        gain = ((price - entry_price) / entry_price * HUNDRED).quantize(CENT)

        if item.safety_status == "FAIL" or not item.route_available:
            return item.at, price, (realized + remaining * gain).quantize(CENT), (
                "emergency exit: safety or route failed",
            )
        if gain <= stop:
            return item.at, price, (realized + remaining * gain).quantize(CENT), (
                "hard stop",
            )

        target = _FIXED_TARGETS.get(policy)
        if target is not None:
            if gain >= target:
                return item.at, price, gain, (f"fixed +{target}% target",)
            continue

        if policy in {CF_EXISTING_STAGED, CF_STAGED_LADDER}:
            for milestone, fraction in ladder:
                key = str(milestone)
                if key in taken or gain < milestone:
                    continue
                taken.add(key)
                sold = remaining * fraction
                realized += sold * gain
                remaining -= sold
            if remaining <= Decimal("0.01"):
                return item.at, price, realized.quantize(CENT), ("staged ladder completed",)
            if policy == CF_EXISTING_STAGED:
                drop = (peak - price) / peak * HUNDRED if peak > 0 else ZERO
                if gain >= config.trailing_arm_percent and drop >= config.trailing_giveback_percent:
                    return item.at, price, (realized + remaining * gain).quantize(CENT), (
                        "trailing protection after staged takes",
                    )
            continue

        if policy == CF_NET_OBJECTIVE:
            healthy = _observation_healthy(item)
            if gain >= objective_percent and not healthy:
                return item.at, price, (realized + remaining * gain).quantize(CENT), (
                    f"+{objective_percent}% NET objective met with weak structure",
                )
            if gain >= objective_percent and healthy:
                for milestone, fraction in ladder:
                    key = str(milestone)
                    if key in taken or gain < milestone:
                        continue
                    taken.add(key)
                    sold = remaining * fraction / 2
                    realized += sold * gain
                    remaining -= sold
                if remaining <= Decimal("0.01"):
                    return item.at, price, realized.quantize(CENT), ("objective ladder done",)
            continue

        if policy == CF_TRAILING:
            drop = (peak - price) / peak * HUNDRED if peak > 0 else ZERO
            if gain >= config.trailing_arm_percent and drop >= config.trailing_giveback_percent:
                return item.at, price, gain, ("trailing runner stopped",)
            continue

        if policy == CF_MOMENTUM:
            if (
                item.momentum_score is not None
                and item.momentum_score <= config.momentum_decay_exit_score
            ):
                return item.at, price, gain, ("momentum decayed",)
            continue

        if policy == CF_SMART_MONEY:
            if item.smart_money_distributing:
                return item.at, price, gain, ("smart money distributing",)
            continue

    last = series[-1]
    final_gain = ((last.price_usd - entry_price) / entry_price * HUNDRED).quantize(CENT)
    return last.at, last.price_usd, (realized + remaining * final_gain).quantize(CENT), (
        "held to the end of the observed stream",
    )


def _observation_healthy(item: ShadowObservation) -> bool:
    if item.safety_status == "FAIL" or not item.route_available:
        return False
    if item.smart_money_distributing:
        return False
    if item.momentum_score is not None and item.momentum_score < 55:
        return False
    if item.organic_score is not None and item.organic_score < 55:
        return False
    return not (
        item.sells > 0 and Decimal(item.buys) / Decimal(item.sells) < Decimal("1.2")
    )


@dataclass(frozen=True, slots=True)
class NotableTimingReport:
    """Did smart-money intelligence arrive early enough? (section 18)"""

    trader_entry_market_cap_usd: Decimal | None = None
    detection_market_cap_usd: Decimal | None = None
    fill_market_cap_usd: Decimal | None = None
    exit_market_cap_usd: Decimal | None = None
    trader_to_bot_percent: Decimal | None = None
    bot_to_fill_percent: Decimal | None = None
    fill_to_exit_percent: Decimal | None = None


def notable_timing(
    *,
    trader_entry_market_cap_usd: Decimal | None,
    detection_market_cap_usd: Decimal | None,
    fill_market_cap_usd: Decimal | None,
    exit_market_cap_usd: Decimal | None,
) -> NotableTimingReport:
    return NotableTimingReport(
        trader_entry_market_cap_usd=trader_entry_market_cap_usd,
        detection_market_cap_usd=detection_market_cap_usd,
        fill_market_cap_usd=fill_market_cap_usd,
        exit_market_cap_usd=exit_market_cap_usd,
        trader_to_bot_percent=_move(trader_entry_market_cap_usd, detection_market_cap_usd),
        bot_to_fill_percent=_move(detection_market_cap_usd, fill_market_cap_usd),
        fill_to_exit_percent=_move(fill_market_cap_usd, exit_market_cap_usd),
    )


@dataclass(frozen=True, slots=True)
class CatalystTimingReport:
    """How the event, the mint and the bot lined up (section 19)."""

    event_at: int | None = None
    mint_created_at: int | None = None
    detected_at: int | None = None
    alerted_at: int | None = None
    filled_at: int | None = None
    first_credible_source: str = ""
    event_to_mint_seconds: int | None = None
    event_to_bot_seconds: int | None = None
    mint_to_bot_seconds: int | None = None
    bot_to_fill_seconds: int | None = None


def catalyst_timing(
    *,
    event_at: int | None,
    mint_created_at: int | None,
    detected_at: int | None,
    alerted_at: int | None = None,
    filled_at: int | None = None,
    first_credible_source: str = "",
) -> CatalystTimingReport:
    return CatalystTimingReport(
        event_at=event_at,
        mint_created_at=mint_created_at,
        detected_at=detected_at,
        alerted_at=alerted_at,
        filled_at=filled_at,
        first_credible_source=first_credible_source,
        event_to_mint_seconds=_span(event_at, mint_created_at),
        event_to_bot_seconds=_span(event_at, detected_at),
        mint_to_bot_seconds=_span(mint_created_at, detected_at),
        bot_to_fill_seconds=_span(detected_at, filled_at),
    )


def attribute_shadow_loss(
    trade: ShadowTradeRecord,
    *,
    signal_age_seconds: int | None = None,
    fill_deterioration_percent: Decimal | None = None,
    liquidity_change_percent: Decimal | None = None,
    provider_degraded: bool = False,
    config: ShadowConfig = DEFAULT_SHADOW_CONFIG,
) -> str:
    """Why did this $10 lose? (section 43)"""

    if trade.close_reason == "SAFETY_DETERIORATION":
        return CAUSE_RUG
    if trade.close_reason in {"LIQUIDITY_COLLAPSE_EMERGENCY", "LIQUIDITY_DETERIORATION"}:
        return CAUSE_LIQUIDITY
    if provider_degraded:
        return CAUSE_PROVIDER_FAILURE
    if (
        fill_deterioration_percent is not None
        and fill_deterioration_percent >= config.max_price_impact_percent
    ):
        return CAUSE_BAD_EXECUTION
    if trade.fill_source == "FALLBACK_PENALISED" and trade.net_pnl_usd < 0:
        return CAUSE_BAD_EXECUTION
    if (
        signal_age_seconds is not None
        and signal_age_seconds > config.max_signal_age_seconds
    ):
        return CAUSE_LATE_DETECTION
    if liquidity_change_percent is not None and liquidity_change_percent <= Decimal("-25"):
        return CAUSE_LIQUIDITY
    if trade.peak_net_pnl_usd >= config.net_profit_objective_usd:
        # It was worth two dollars at some point and was not banked.
        return CAUSE_EXIT_STRATEGY
    if trade.max_favourable_percent >= Decimal("25"):
        return CAUSE_EXIT_STRATEGY
    return CAUSE_BAD_SIGNAL


def _move(base: Decimal | None, current: Decimal | None) -> Decimal | None:
    if base is None or current is None or base <= 0:
        return None
    return ((current - base) / base * HUNDRED).quantize(CENT)


def _span(start: int | None, end: int | None) -> int | None:
    if not start or not end:
        return None
    return end - start


def _rate(count: int, total: int) -> Decimal | None:
    if total <= 0:
        return None
    return (Decimal(count) / Decimal(total) * HUNDRED).quantize(CENT)


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return (sum(values, ZERO) / Decimal(len(values))).quantize(CENT)


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return ((ordered[middle - 1] + ordered[middle]) / 2).quantize(UNIT)
