"""Deterministic replay, counterfactuals and walk-forward validation.

Sections AP, AQ, AR, AS, AT, AU, AV, AW, BH and BI.

The single structural guarantee here is **no look-ahead**: every policy is
handed a :class:`~smart_money_bot.lab.timeline.TokenTimeline` and may only read
``timeline.before(t)``.  A policy physically cannot see the future peak it is
being scored against.

Counterfactuals also never cost provider credits: they run entirely on already
persisted observations, so simulating eight policies costs exactly as many
requests as simulating one — none.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .config import DEFAULT_LAB_CONFIG, LabConfig
from .costs import estimate_round_trip_cost
from .timeline import TokenTimeline

ZERO = Decimal("0")
HUNDRED = Decimal("100")

# --- entry policies (section AP) ---------------------------------------------
POLICY_IMMEDIATE = "IMMEDIATE"
POLICY_QUALIFICATION = "QUALIFICATION"
POLICY_CONFIRMATION = "CONFIRMATION"
POLICY_ACCELERATION = "ACCELERATION"
POLICY_PULLBACK = "PULLBACK"
POLICY_BREAKOUT = "BREAKOUT_RECLAIM"
POLICY_STABILIZED_REENTRY = "STABILIZED_REENTRY"
POLICY_NO_TRADE = "NO_TRADE"

ENTRY_POLICIES: tuple[str, ...] = (
    POLICY_IMMEDIATE,
    POLICY_QUALIFICATION,
    POLICY_CONFIRMATION,
    POLICY_ACCELERATION,
    POLICY_PULLBACK,
    POLICY_BREAKOUT,
    POLICY_STABILIZED_REENTRY,
    POLICY_NO_TRADE,
)

# --- exit policies (section AQ) ----------------------------------------------
EXIT_FIXED_TP = "FIXED_TP"
EXIT_STAGED_TP = "STAGED_TP"
EXIT_TRAILING = "TRAILING"
EXIT_MOMENTUM = "MOMENTUM"
EXIT_FLOW = "FLOW"
EXIT_STAGED_MOMENTUM = "STAGED_PLUS_MOMENTUM"
EXIT_STAGED_TRAILING = "STAGED_PLUS_TRAILING"
EXIT_TIME_BASED = "TIME_BASED"
EXIT_FULL_HOLD = "FULL_HOLD"

EXIT_POLICIES: tuple[str, ...] = (
    EXIT_FIXED_TP,
    EXIT_STAGED_TP,
    EXIT_TRAILING,
    EXIT_MOMENTUM,
    EXIT_FLOW,
    EXIT_STAGED_MOMENTUM,
    EXIT_STAGED_TRAILING,
    EXIT_TIME_BASED,
    EXIT_FULL_HOLD,
)

# --- loser attribution (section AS) ------------------------------------------
LOSS_BAD_SELECTION = "BAD_SELECTION"
LOSS_LATE_ENTRY = "LATE_ENTRY"
LOSS_EDGE_CONSUMED = "EDGE_CONSUMED"
LOSS_FEES = "FEES"
LOSS_SLIPPAGE = "SLIPPAGE"
LOSS_LIQUIDITY = "LIQUIDITY"
LOSS_RUG_SAFETY = "RUG_SAFETY"
LOSS_MOMENTUM_REVERSAL = "MOMENTUM_REVERSAL"
LOSS_CONCENTRATION = "CONCENTRATION"
LOSS_BAD_EXIT = "BAD_EXIT"
LOSS_LATENCY = "LATENCY"
LOSS_BAD_REGIME = "BAD_REGIME"
LOSS_DATA_QUALITY = "DATA_QUALITY"

SAMPLE_TOO_SMALL = "SAMPLE_TOO_SMALL"


@dataclass(frozen=True, slots=True)
class ReplayObservation:
    """One persisted observation replayed exactly as it was recorded."""

    at: int
    price_usd: Decimal
    market_cap_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_usd: Decimal | None = None
    momentum_score: Decimal | None = None
    organic_score: Decimal | None = None
    buys: int = 0
    sells: int = 0
    qualified: bool = False
    safety_status: str = "UNKNOWN"
    independent_buyers: int | None = None


@dataclass(frozen=True, slots=True)
class ReplayTrade:
    policy: str
    exit_policy: str
    entered_at: int | None = None
    entry_price_usd: Decimal | None = None
    exited_at: int | None = None
    exit_price_usd: Decimal | None = None
    gross_return_percent: Decimal = ZERO
    net_return_percent: Decimal = ZERO
    cost_percent: Decimal = ZERO
    max_favourable_percent: Decimal = ZERO
    max_adverse_percent: Decimal = ZERO
    traded: bool = False
    notes: tuple[str, ...] = ()


def _series(timeline: TokenTimeline, observations: Sequence[ReplayObservation]) -> tuple[
    ReplayObservation, ...
]:
    """Observations, ordered, and never beyond what the timeline recorded."""

    if timeline is not None and len(timeline):
        horizon = timeline.events[-1].occurred_at
        return tuple(
            sorted((item for item in observations if item.at <= horizon), key=lambda i: i.at)
        )
    return tuple(sorted(observations, key=lambda item: item.at))


def _entry_index(policy: str, series: Sequence[ReplayObservation]) -> int | None:
    """Choose an entry index using only observations at or before that index."""

    if not series:
        return None
    if policy == POLICY_NO_TRADE:
        return None
    if policy == POLICY_IMMEDIATE:
        return 0
    if policy == POLICY_QUALIFICATION:
        for index, item in enumerate(series):
            if item.qualified:
                return index
        return None
    if policy == POLICY_CONFIRMATION:
        qualified_at: int | None = None
        for index, item in enumerate(series):
            if item.qualified and qualified_at is None:
                qualified_at = index
                continue
            if qualified_at is not None and index > qualified_at and item.buys > item.sells:
                return index
        return None
    if policy == POLICY_ACCELERATION:
        for index in range(2, len(series)):
            window = series[index - 2 : index + 1]
            if all(window[step].price_usd > window[step - 1].price_usd for step in (1, 2)) and (
                series[index].momentum_score is not None
                and series[index].momentum_score >= Decimal("60")
            ):
                return index
        return None
    if policy == POLICY_PULLBACK:
        peak = ZERO
        for index, item in enumerate(series):
            peak = max(peak, item.price_usd)
            if peak > 0 and item.price_usd <= peak * Decimal("0.85") and index >= 2:
                return index
        return None
    if policy == POLICY_BREAKOUT:
        peak = ZERO
        broke = False
        for index, item in enumerate(series):
            if index and item.price_usd < peak * Decimal("0.9"):
                broke = True
            if broke and peak > 0 and item.price_usd >= peak:
                return index
            peak = max(peak, item.price_usd)
        return None
    if policy == POLICY_STABILIZED_REENTRY:
        stable = 0
        for index in range(1, len(series)):
            if series[index].price_usd >= series[index - 1].price_usd:
                stable += 1
            else:
                stable = 0
            if stable >= 3 and series[index].safety_status == "PASS":
                return index
        return None
    return None


def _exit_index(
    policy: str,
    series: Sequence[ReplayObservation],
    entry_index: int,
    *,
    config: LabConfig,
) -> tuple[int, Decimal]:
    """Return the exit index and the realized gross return for that policy."""

    entry_price = series[entry_index].price_usd
    if entry_price <= 0:
        return entry_index, ZERO

    peak = entry_price
    realized = ZERO
    remaining = Decimal("1")
    milestones = {str(gain): False for gain, _ in config.exit_milestones}

    for index in range(entry_index + 1, len(series)):
        item = series[index]
        price = item.price_usd
        peak = max(peak, price)
        gain = (price - entry_price) / entry_price * HUNDRED

        if policy == EXIT_FIXED_TP and gain >= Decimal("10"):
            return index, gain
        if policy == EXIT_FULL_HOLD:
            continue
        if (
            policy == EXIT_TIME_BASED
            and item.at - series[entry_index].at >= config.time_stop_seconds
        ):
            return index, gain
        if policy in {EXIT_TRAILING, EXIT_STAGED_TRAILING}:
            drop = (peak - price) / peak * HUNDRED if peak > 0 else ZERO
            if gain >= config.trailing_arm_percent and drop >= config.trailing_giveback_percent:
                return index, (realized + remaining * gain).quantize(Decimal("0.01"))
        if (
            policy in {EXIT_MOMENTUM, EXIT_STAGED_MOMENTUM}
            and item.momentum_score is not None
            and item.momentum_score <= config.momentum_decay_exit_score
        ):
            return index, (realized + remaining * gain).quantize(Decimal("0.01"))
        if policy == EXIT_FLOW and item.sells > item.buys * 2:
            return index, (realized + remaining * gain).quantize(Decimal("0.01"))
        if policy in {EXIT_STAGED_TP, EXIT_STAGED_MOMENTUM, EXIT_STAGED_TRAILING}:
            for milestone, fraction in config.exit_milestones:
                key = str(milestone)
                if milestones[key] or gain < milestone:
                    continue
                milestones[key] = True
                sold = remaining * fraction
                realized += sold * gain
                remaining -= sold
            if remaining <= Decimal("0.01"):
                return index, realized.quantize(Decimal("0.01"))
        if gain <= -config.hard_stop_loss_percent:
            return index, (realized + remaining * gain).quantize(Decimal("0.01"))

    last = len(series) - 1
    final_gain = (series[last].price_usd - entry_price) / entry_price * HUNDRED
    return last, (realized + remaining * final_gain).quantize(Decimal("0.01"))


def replay_policy(
    timeline: TokenTimeline,
    observations: Sequence[ReplayObservation],
    *,
    entry_policy: str,
    exit_policy: str,
    notional_usd: Decimal | None = None,
    price_impact_percent: Decimal | None = None,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> ReplayTrade:
    """Replay one (entry, exit) policy pair using only recorded evidence."""

    series = _series(timeline, observations)
    entry_index = _entry_index(entry_policy, series)
    if entry_index is None:
        return ReplayTrade(
            policy=entry_policy,
            exit_policy=exit_policy,
            traded=False,
            notes=("policy never entered",),
        )

    exit_index, gross = _exit_index(exit_policy, series, entry_index, config=config)
    entry_price = series[entry_index].price_usd
    window = series[entry_index : exit_index + 1]
    peak = max((item.price_usd for item in window), default=entry_price)
    trough = min((item.price_usd for item in window), default=entry_price)
    mfe = ((peak - entry_price) / entry_price * HUNDRED).quantize(Decimal("0.01"))
    mae = ((entry_price - trough) / entry_price * HUNDRED).quantize(Decimal("0.01"))

    cost = estimate_round_trip_cost(
        notional_usd or config.normal_position_usd,
        buy_price_impact_percent=price_impact_percent,
        sell_price_impact_percent=price_impact_percent,
        config=config,
    )
    net = (gross - cost.total_cost_percent).quantize(Decimal("0.01"))
    return ReplayTrade(
        policy=entry_policy,
        exit_policy=exit_policy,
        entered_at=series[entry_index].at,
        entry_price_usd=entry_price,
        exited_at=series[exit_index].at,
        exit_price_usd=series[exit_index].price_usd,
        gross_return_percent=gross.quantize(Decimal("0.01")),
        net_return_percent=net,
        cost_percent=cost.total_cost_percent,
        max_favourable_percent=mfe,
        max_adverse_percent=max(ZERO, mae),
        traded=True,
    )


def compare_policies(
    timeline: TokenTimeline,
    observations: Sequence[ReplayObservation],
    *,
    entry_policies: Sequence[str] = ENTRY_POLICIES,
    exit_policies: Sequence[str] = EXIT_POLICIES,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> tuple[ReplayTrade, ...]:
    """Every counterfactual pair, on identical persisted observations."""

    return tuple(
        replay_policy(
            timeline,
            observations,
            entry_policy=entry_policy,
            exit_policy=exit_policy,
            config=config,
        )
        for entry_policy in entry_policies
        for exit_policy in exit_policies
    )


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """One completed simulated trade, as persisted."""

    mint: str
    opened_at: int
    closed_at: int
    strategy_version: str
    net_pnl_usd: Decimal
    gross_pnl_usd: Decimal
    cost_usd: Decimal
    size_usd: Decimal
    max_favourable_percent: Decimal = ZERO
    max_adverse_percent: Decimal = ZERO
    is_reentry: bool = False
    regime: str = "UNKNOWN"
    lifecycle_state: str = ""
    close_reason: str = ""
    out_of_sample: bool = False

    @property
    def net_return_percent(self) -> Decimal:
        if self.size_usd <= 0:
            return ZERO
        return (self.net_pnl_usd / self.size_usd * HUNDRED).quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class PerformanceReport:
    """Walk-forward metrics (section BH).  Small samples say so."""

    strategy_version: str = ""
    sample: int = 0
    wins: int = 0
    losses: int = 0
    win_rate_percent: Decimal | None = None
    gross_return_usd: Decimal = ZERO
    net_return_usd: Decimal = ZERO
    total_cost_usd: Decimal = ZERO
    expectancy_usd: Decimal | None = None
    median_return_percent: Decimal | None = None
    average_win_usd: Decimal | None = None
    average_loss_usd: Decimal | None = None
    profit_factor: Decimal | None = None
    max_drawdown_usd: Decimal = ZERO
    best_trade_usd: Decimal | None = None
    worst_trade_usd: Decimal | None = None
    reach_10_percent: Decimal | None = None
    reach_25_percent: Decimal | None = None
    reach_50_percent: Decimal | None = None
    reach_100_percent: Decimal | None = None
    rug_avoidance_percent: Decimal | None = None
    cost_to_gross_percent: Decimal | None = None
    fresh_sample: int = 0
    reentry_sample: int = 0
    fresh_expectancy_usd: Decimal | None = None
    reentry_expectancy_usd: Decimal | None = None
    by_regime: Mapping[str, Decimal] = field(default_factory=dict)
    by_lifecycle: Mapping[str, Decimal] = field(default_factory=dict)
    sufficient: bool = False
    note: str = SAMPLE_TOO_SMALL

    @property
    def sample_too_small(self) -> bool:
        return not self.sufficient


def summarize_trades(
    trades: Sequence[TradeRecord],
    *,
    strategy_version: str = "",
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> PerformanceReport:
    """Honest performance summary.  No losing trade is ever excluded."""

    if not trades:
        return PerformanceReport(strategy_version=strategy_version)

    nets = [trade.net_pnl_usd for trade in trades]
    wins = [value for value in nets if value > 0]
    losses = [value for value in nets if value <= 0]
    gross = sum((trade.gross_pnl_usd for trade in trades), ZERO)
    net = sum(nets, ZERO)
    costs = sum((trade.cost_usd for trade in trades), ZERO)
    peaks = [trade.max_favourable_percent for trade in trades]

    equity = ZERO
    peak_equity = ZERO
    max_drawdown = ZERO
    for trade in sorted(trades, key=lambda item: item.closed_at):
        equity += trade.net_pnl_usd
        peak_equity = max(peak_equity, equity)
        max_drawdown = max(max_drawdown, peak_equity - equity)

    fresh = [trade for trade in trades if not trade.is_reentry]
    reentry = [trade for trade in trades if trade.is_reentry]

    by_regime: dict[str, Decimal] = {}
    for trade in trades:
        by_regime.setdefault(trade.regime, ZERO)
        by_regime[trade.regime] += trade.net_pnl_usd
    by_lifecycle: dict[str, Decimal] = {}
    for trade in trades:
        key = trade.lifecycle_state or "UNKNOWN"
        by_lifecycle.setdefault(key, ZERO)
        by_lifecycle[key] += trade.net_pnl_usd

    sufficient = len(trades) >= config.min_forward_sample
    loss_total = sum((abs(value) for value in losses), ZERO)
    return PerformanceReport(
        strategy_version=strategy_version,
        sample=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate_percent=_rate(len(wins), len(trades)),
        gross_return_usd=gross.quantize(Decimal("0.000001")),
        net_return_usd=net.quantize(Decimal("0.000001")),
        total_cost_usd=costs.quantize(Decimal("0.000001")),
        expectancy_usd=(net / Decimal(len(trades))).quantize(Decimal("0.000001")),
        median_return_percent=_median([trade.net_return_percent for trade in trades]),
        average_win_usd=(
            (sum(wins, ZERO) / Decimal(len(wins))).quantize(Decimal("0.000001")) if wins else None
        ),
        average_loss_usd=(
            (sum(losses, ZERO) / Decimal(len(losses))).quantize(Decimal("0.000001"))
            if losses
            else None
        ),
        profit_factor=(
            (sum(wins, ZERO) / loss_total).quantize(Decimal("0.01")) if loss_total > 0 else None
        ),
        max_drawdown_usd=max_drawdown.quantize(Decimal("0.000001")),
        best_trade_usd=max(nets),
        worst_trade_usd=min(nets),
        reach_10_percent=_hit(peaks, Decimal("10")),
        reach_25_percent=_hit(peaks, Decimal("25")),
        reach_50_percent=_hit(peaks, Decimal("50")),
        reach_100_percent=_hit(peaks, Decimal("100")),
        rug_avoidance_percent=_rate(
            sum(1 for trade in trades if trade.close_reason != "SAFETY_DETERIORATION"),
            len(trades),
        ),
        cost_to_gross_percent=(
            (costs / abs(gross) * HUNDRED).quantize(Decimal("0.01")) if gross else None
        ),
        fresh_sample=len(fresh),
        reentry_sample=len(reentry),
        fresh_expectancy_usd=(
            (sum((item.net_pnl_usd for item in fresh), ZERO) / Decimal(len(fresh))).quantize(
                Decimal("0.000001")
            )
            if fresh
            else None
        ),
        reentry_expectancy_usd=(
            (sum((item.net_pnl_usd for item in reentry), ZERO) / Decimal(len(reentry))).quantize(
                Decimal("0.000001")
            )
            if reentry
            else None
        ),
        by_regime=by_regime,
        by_lifecycle=by_lifecycle,
        sufficient=sufficient,
        note="" if sufficient else SAMPLE_TOO_SMALL,
    )


@dataclass(frozen=True, slots=True)
class PromotionVerdict:
    """Whether a challenger may replace the champion (section AV)."""

    promote: bool = False
    reasons: tuple[str, ...] = ()
    champion: PerformanceReport = field(default_factory=PerformanceReport)
    challenger: PerformanceReport = field(default_factory=PerformanceReport)


def evaluate_promotion(
    champion: PerformanceReport,
    challenger: PerformanceReport,
    *,
    out_of_sample_trades: int,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> PromotionVerdict:
    """Promotion requires forward evidence.  In-sample replay is never enough."""

    reasons: list[str] = []
    if out_of_sample_trades < config.challenger_min_forward_sample:
        reasons.append(
            f"only {out_of_sample_trades} out-of-sample trades; "
            f"{config.challenger_min_forward_sample} required"
        )
    if not challenger.sufficient:
        reasons.append("challenger sample is too small")
    if challenger.expectancy_usd is None or champion.expectancy_usd is None:
        reasons.append("expectancy is not measurable on both strategies")
    else:
        gain = challenger.expectancy_usd - champion.expectancy_usd
        if gain < config.challenger_min_expectancy_gain:
            reasons.append(f"expectancy gain {gain} below the required improvement")
    if (
        challenger.profit_factor is not None
        and champion.profit_factor is not None
        and challenger.profit_factor <= champion.profit_factor
    ):
        reasons.append("profit factor did not improve")
    slack = config.challenger_max_drawdown_slack
    if challenger.max_drawdown_usd > champion.max_drawdown_usd + slack:
        reasons.append("drawdown got materially worse")
    if (
        challenger.rug_avoidance_percent is not None
        and champion.rug_avoidance_percent is not None
        and challenger.rug_avoidance_percent < champion.rug_avoidance_percent
    ):
        reasons.append("rug avoidance regressed")

    return PromotionVerdict(
        promote=not reasons,
        reasons=tuple(reasons) or ("all promotion criteria met on forward data",),
        champion=champion,
        challenger=challenger,
    )


@dataclass(frozen=True, slots=True)
class DriftReport:
    """Rolling degradation detection (section AW).  Never raises risk."""

    recent: PerformanceReport = field(default_factory=PerformanceReport)
    baseline: PerformanceReport = field(default_factory=PerformanceReport)
    degrading: bool = False
    signals: tuple[str, ...] = ()
    recommended_action: str = "NO_CHANGE"


def detect_drift(
    recent: PerformanceReport,
    baseline: PerformanceReport,
) -> DriftReport:
    signals: list[str] = []
    if not recent.sufficient or not baseline.sufficient:
        return DriftReport(recent=recent, baseline=baseline, signals=(SAMPLE_TOO_SMALL,))
    if (
        recent.expectancy_usd is not None
        and baseline.expectancy_usd is not None
        and recent.expectancy_usd < baseline.expectancy_usd
    ):
        signals.append("expectancy fell")
    if (
        recent.win_rate_percent is not None
        and baseline.win_rate_percent is not None
        and recent.win_rate_percent < baseline.win_rate_percent - Decimal("10")
    ):
        signals.append("win rate fell materially")
    if (
        recent.profit_factor is not None
        and baseline.profit_factor is not None
        and recent.profit_factor < baseline.profit_factor
    ):
        signals.append("profit factor fell")
    if recent.max_drawdown_usd > baseline.max_drawdown_usd:
        signals.append("drawdown deepened")
    if (
        recent.cost_to_gross_percent is not None
        and baseline.cost_to_gross_percent is not None
        and recent.cost_to_gross_percent > baseline.cost_to_gross_percent
    ):
        signals.append("cost burden rose")
    degrading = len(signals) >= 2
    return DriftReport(
        recent=recent,
        baseline=baseline,
        degrading=degrading,
        signals=tuple(signals),
        # Never compensate for degradation by taking more risk.
        recommended_action="REDUCE_SELECTIVITY_RISK" if degrading else "NO_CHANGE",
    )


@dataclass(frozen=True, slots=True)
class MissedWinner:
    """A rejected or silent token that later ran (section AR)."""

    mint: str
    rejecting_gate: str
    rejection_reason: str
    evidence_at_rejection: Mapping[str, str] = field(default_factory=dict)
    later_max_favourable_percent: Decimal = ZERO
    alternate_policy: str = POLICY_NO_TRADE
    relaxation_cost: str = ""


def analyze_missed_winners(
    missed: Sequence[MissedWinner],
    *,
    additional_losers_if_relaxed: int,
    additional_rugs_if_relaxed: int,
) -> tuple[MissedWinner, ...]:
    """Attach the cost of relaxing each gate across the whole sample.

    A single anecdotal moonshot is never a reason to weaken a gate, so every
    entry carries what relaxation would have cost on the rest of the sample.
    """

    note = (
        f"relaxing this gate across the sample also admits {additional_losers_if_relaxed} "
        f"more losers and {additional_rugs_if_relaxed} more rugs"
    )
    return tuple(
        MissedWinner(
            mint=item.mint,
            rejecting_gate=item.rejecting_gate,
            rejection_reason=item.rejection_reason,
            evidence_at_rejection=item.evidence_at_rejection,
            later_max_favourable_percent=item.later_max_favourable_percent,
            alternate_policy=item.alternate_policy,
            relaxation_cost=note,
        )
        for item in missed
    )


def attribute_loss(
    trade: TradeRecord,
    *,
    entry_move_since_signal_percent: Decimal | None = None,
    signal_age_seconds: int | None = None,
    liquidity_change_percent: Decimal | None = None,
    slippage_percent: Decimal | None = None,
    decision_latency_ms: int | None = None,
    cluster_supply_change_percent: Decimal | None = None,
    data_degraded: bool = False,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> str:
    """Classify the most likely cause of one losing simulated trade."""

    if trade.close_reason == "SAFETY_DETERIORATION":
        return LOSS_RUG_SAFETY
    if trade.close_reason in {"LIQUIDITY_COLLAPSE_EMERGENCY", "LIQUIDITY_DETERIORATION"}:
        return LOSS_LIQUIDITY
    if data_degraded:
        return LOSS_DATA_QUALITY
    if (
        decision_latency_ms is not None
        and decision_latency_ms > config.max_decision_latency_ms
    ):
        return LOSS_LATENCY
    if (
        entry_move_since_signal_percent is not None
        and entry_move_since_signal_percent >= config.max_move_since_signal_percent
    ):
        return LOSS_EDGE_CONSUMED
    if signal_age_seconds is not None and signal_age_seconds > config.max_signal_age_seconds:
        return LOSS_LATE_ENTRY
    if slippage_percent is not None and slippage_percent > config.max_slippage_percent:
        return LOSS_SLIPPAGE
    if (
        cluster_supply_change_percent is not None
        and cluster_supply_change_percent >= Decimal("10")
    ):
        return LOSS_CONCENTRATION
    if liquidity_change_percent is not None and liquidity_change_percent <= Decimal("-25"):
        return LOSS_LIQUIDITY
    if trade.regime in {"RISK_OFF", "LIQUIDITY_STRESS"}:
        return LOSS_BAD_REGIME
    if trade.size_usd > 0 and trade.cost_usd >= abs(trade.gross_pnl_usd):
        return LOSS_FEES
    if trade.max_favourable_percent >= Decimal("25"):
        return LOSS_BAD_EXIT
    if trade.close_reason in {"MOMENTUM_DECAY", "BUY_FLOW_REVERSAL", "TRAILING_PROFIT_PROTECTION"}:
        return LOSS_MOMENTUM_REVERSAL
    return LOSS_BAD_SELECTION


def split_walk_forward(
    trades: Sequence[TradeRecord],
    *,
    calibration_cutoff_at: int,
) -> tuple[tuple[TradeRecord, ...], tuple[TradeRecord, ...]]:
    """Split into calibration/training and out-of-sample forward sets."""

    calibration = tuple(trade for trade in trades if trade.closed_at <= calibration_cutoff_at)
    forward = tuple(trade for trade in trades if trade.closed_at > calibration_cutoff_at)
    return calibration, forward


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return ((ordered[middle - 1] + ordered[middle]) / 2).quantize(Decimal("0.01"))


def _hit(values: Sequence[Decimal], threshold: Decimal) -> Decimal | None:
    if not values:
        return None
    hits = sum(1 for value in values if value >= threshold)
    return (Decimal(hits) / Decimal(len(values)) * HUNDRED).quantize(Decimal("0.01"))


def _rate(count: int, total: int) -> Decimal | None:
    if total <= 0:
        return None
    return (Decimal(count) / Decimal(total) * HUNDRED).quantize(Decimal("0.01"))
