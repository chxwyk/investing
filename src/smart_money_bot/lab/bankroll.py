"""Simulated bankroll, position sizing, allocation and circuit breakers.

Sections AJ, AK and BB.  Every number here is simulated: the lab has no signing
path, so "capital" is a bookkeeping entry, never a wallet balance.

The sizing rules are deliberately one-directional — size *down* on weaker
evidence, never up on worse recent results.  There is no martingale, no revenge
sizing and no automatic averaging down anywhere in this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .config import DEFAULT_LAB_CONFIG, LabConfig
from .decision import Reason
from .regime import REGIME_SIZE_MULTIPLIER, UNKNOWN

ZERO = Decimal("0")
CENT = Decimal("0.01")

TRADING_PAUSED = "TRADING_PAUSED_DATA_CONTROL_RISK"
TRADING_ACTIVE = "TRADING_ACTIVE"


@dataclass(frozen=True, slots=True)
class BankrollState:
    """The simulated account, exactly as persisted."""

    starting_usd: Decimal = Decimal("100")
    cash_usd: Decimal = Decimal("100")
    realized_net_pnl_usd: Decimal = ZERO
    open_exposure_usd: Decimal = ZERO
    open_positions: int = 0
    peak_equity_usd: Decimal = Decimal("100")
    consecutive_losses: int = 0
    day_realized_net_pnl_usd: Decimal = ZERO
    day_key: str = ""
    paused_reason: str = ""

    @property
    def equity_usd(self) -> Decimal:
        return (self.cash_usd + self.open_exposure_usd).quantize(Decimal("0.000001"))

    @property
    def drawdown_percent(self) -> Decimal:
        if self.peak_equity_usd <= 0:
            return ZERO
        drop = self.peak_equity_usd - self.equity_usd
        return max(ZERO, (drop / self.peak_equity_usd * 100)).quantize(CENT)

    @property
    def is_paused(self) -> bool:
        return bool(self.paused_reason)


@dataclass(frozen=True, slots=True)
class SizingInputs:
    """The evidence that may only ever shrink a simulated position."""

    liquidity_usd: Decimal | None = None
    price_impact_percent: Decimal | None = None
    slippage_percent: Decimal | None = None
    independence_ratio: Decimal | None = None
    edge_confidence: Decimal | None = None
    is_reentry: bool = False
    volatility_percent: Decimal | None = None
    regime: str = UNKNOWN
    authenticity_score: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SizingResult:
    size_usd: Decimal = ZERO
    multiplier: Decimal = Decimal("1")
    reductions: tuple[str, ...] = ()
    blocked_reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.size_usd > 0 and not self.blocked_reason


def size_position(
    state: BankrollState,
    inputs: SizingInputs,
    *,
    config: LabConfig = DEFAULT_LAB_CONFIG,
    exposure_in_token_usd: Decimal = ZERO,
    narrative_exposure_usd: Decimal = ZERO,
) -> SizingResult:
    """Compute the simulated position size for one candidate.

    Returns a zero size with a blocking reason whenever a bankroll, exposure or
    breaker rule forbids the trade — the entry engine turns that into an explicit
    reason code rather than silently trading smaller.
    """

    if state.is_paused:
        return SizingResult(blocked_reason=Reason.TRADING_PAUSED_DATA_CONTROL_RISK)
    if state.open_positions >= config.max_concurrent_positions:
        return SizingResult(blocked_reason=Reason.MAX_POSITIONS_REACHED)
    if state.open_exposure_usd >= config.max_total_exposure_usd:
        return SizingResult(blocked_reason=Reason.MAX_EXPOSURE_REACHED)
    if exposure_in_token_usd > 0:
        # Adding to an existing simulated position is averaging down/up, which
        # is never automatic.
        return SizingResult(blocked_reason=Reason.NO_AVERAGE_DOWN)
    if state.day_realized_net_pnl_usd <= -config.daily_loss_cap_usd:
        return SizingResult(blocked_reason=Reason.DAILY_LOSS_CAP)
    if narrative_exposure_usd >= config.max_narrative_exposure_usd:
        return SizingResult(blocked_reason=Reason.MAX_EXPOSURE_REACHED)

    multiplier = Decimal("1")
    reductions: list[str] = []

    regime_multiplier = REGIME_SIZE_MULTIPLIER.get(inputs.regime, Decimal("0.6"))
    if regime_multiplier < 1:
        multiplier *= regime_multiplier
        reductions.append(f"regime {inputs.regime}")

    if inputs.liquidity_usd is not None:
        floor = config.min_liquidity_usd
        if inputs.liquidity_usd < floor * 2:
            multiplier *= Decimal("0.7")
            reductions.append("thin liquidity")
    else:
        multiplier *= Decimal("0.6")
        reductions.append("liquidity unknown")

    if inputs.price_impact_percent is not None and inputs.price_impact_percent > (
        config.max_price_impact_percent / 2
    ):
        multiplier *= Decimal("0.75")
        reductions.append("elevated price impact")

    if inputs.slippage_percent is not None and inputs.slippage_percent > (
        config.max_slippage_percent / 2
    ):
        multiplier *= Decimal("0.8")
        reductions.append("elevated slippage")

    if inputs.independence_ratio is not None and inputs.independence_ratio < Decimal("0.7"):
        multiplier *= Decimal("0.8")
        reductions.append("weaker buyer independence")

    if inputs.edge_confidence is not None and inputs.edge_confidence < Decimal("60"):
        multiplier *= Decimal("0.8")
        reductions.append("lower edge confidence")

    if inputs.authenticity_score is None:
        multiplier *= Decimal("0.85")
        reductions.append("economic authenticity unknown")
    elif inputs.authenticity_score < Decimal("60"):
        multiplier *= Decimal("0.85")
        reductions.append("mixed economic authenticity")

    if inputs.is_reentry:
        multiplier *= config.reentry_size_multiplier
        reductions.append("re-entry")

    if inputs.volatility_percent is not None and inputs.volatility_percent >= Decimal("60"):
        multiplier *= Decimal("0.75")
        reductions.append("extreme volatility")

    drawdown = state.drawdown_percent
    if drawdown >= config.max_bankroll_drawdown_percent / 2:
        multiplier *= Decimal("0.7")
        reductions.append("bankroll drawdown")

    if state.consecutive_losses >= 2:
        # Reduce, never increase, after losses.  This is the explicit
        # anti-martingale rule.
        multiplier *= Decimal("0.8")
        reductions.append("recent losing streak")

    size = (config.normal_position_usd * multiplier).quantize(CENT)
    size = min(size, config.max_position_usd)
    size = min(size, config.max_token_exposure_usd)
    size = min(size, config.max_total_exposure_usd - state.open_exposure_usd)
    size = min(size, state.cash_usd)
    if size < config.min_position_usd:
        # Reductions shrink a position; they never shrink it to nothing.  The
        # floor is the smallest simulated position that is still meaningful.
        if state.cash_usd < config.min_position_usd:
            return SizingResult(blocked_reason=Reason.BANKROLL_EXHAUSTED)
        if config.max_total_exposure_usd - state.open_exposure_usd < config.min_position_usd:
            return SizingResult(blocked_reason=Reason.MAX_EXPOSURE_REACHED)
        size = config.min_position_usd
        reductions.append("floored at the minimum simulated position")
    return SizingResult(
        size_usd=size,
        multiplier=multiplier.quantize(Decimal("0.0001")),
        reductions=tuple(reductions),
    )


@dataclass(frozen=True, slots=True)
class AllocationCandidate:
    """One candidate competing for the finite simulated bankroll (section AK)."""

    mint: str
    expected_net_edge_percent: Decimal
    edge_confidence: Decimal
    requested_usd: Decimal
    downside_percent: Decimal = ZERO
    lifecycle_state: str = ""

    @property
    def risk_adjusted_edge(self) -> Decimal:
        """Edge per unit of downside, so cheap-but-fragile ranks below solid."""

        downside = max(Decimal("5"), self.downside_percent)
        confidence = max(Decimal("10"), self.edge_confidence) / 100
        return (self.expected_net_edge_percent / downside * confidence).quantize(
            Decimal("0.0001")
        )


@dataclass(frozen=True, slots=True)
class Allocation:
    funded: tuple[tuple[str, Decimal], ...] = ()
    deferred: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


def allocate_capital(
    candidates: Sequence[AllocationCandidate],
    state: BankrollState,
    *,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> Allocation:
    """Rank competing candidates by risk-adjusted edge, not arrival order."""

    if not candidates:
        return Allocation()
    ordered = sorted(
        candidates,
        key=lambda item: (item.risk_adjusted_edge, item.expected_net_edge_percent),
        reverse=True,
    )
    available_cash = state.cash_usd
    available_exposure = config.max_total_exposure_usd - state.open_exposure_usd
    slots = config.max_concurrent_positions - state.open_positions

    funded: list[tuple[str, Decimal]] = []
    deferred: list[str] = []
    for candidate in ordered:
        if slots <= 0 or available_cash < config.min_position_usd or available_exposure <= 0:
            deferred.append(candidate.mint)
            continue
        size = min(candidate.requested_usd, available_cash, available_exposure)
        size = size.quantize(CENT)
        if size < config.min_position_usd:
            deferred.append(candidate.mint)
            continue
        funded.append((candidate.mint, size))
        available_cash -= size
        available_exposure -= size
        slots -= 1
    notes: list[str] = []
    if deferred:
        notes.append("Deferred candidates lost the capital competition, not the safety check")
    return Allocation(funded=tuple(funded), deferred=tuple(deferred), notes=tuple(notes))


@dataclass(frozen=True, slots=True)
class BreakerInputs:
    """Everything that can make trustworthy simulated operation impossible."""

    provider_outage: bool = False
    rpc_unstable: bool = False
    route_outage: bool = False
    abnormal_congestion: bool = False
    safety_provider_disagreement: bool = False
    persistence_failure: bool = False
    stale_critical_data_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class BreakerStatus:
    state: str = TRADING_ACTIVE
    reasons: tuple[str, ...] = ()

    @property
    def paused(self) -> bool:
        return self.state == TRADING_PAUSED


def evaluate_circuit_breakers(
    state: BankrollState,
    inputs: BreakerInputs,
    *,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> BreakerStatus:
    """Pause simulated trading whenever trustworthy operation is impossible."""

    reasons: list[str] = []
    if state.consecutive_losses >= config.consecutive_loss_limit:
        reasons.append("CONSECUTIVE_LOSS_LIMIT")
    if state.drawdown_percent >= config.max_bankroll_drawdown_percent:
        reasons.append("ROLLING_DRAWDOWN")
    if state.day_realized_net_pnl_usd <= -config.daily_loss_cap_usd:
        reasons.append("DAILY_LOSS_CAP")
    if inputs.provider_outage:
        reasons.append("PROVIDER_OUTAGE")
    if inputs.rpc_unstable:
        reasons.append("RPC_INSTABILITY")
    if inputs.route_outage:
        reasons.append("ROUTE_OUTAGE")
    if inputs.abnormal_congestion:
        reasons.append("ABNORMAL_CONGESTION")
    if inputs.safety_provider_disagreement:
        reasons.append("SAFETY_PROVIDER_DISAGREEMENT")
    if inputs.persistence_failure:
        reasons.append("PERSISTENCE_FAILURE")
    if (
        inputs.stale_critical_data_seconds is not None
        and inputs.stale_critical_data_seconds > config.stale_data_seconds
    ):
        reasons.append("STALE_CRITICAL_DATA")
    if reasons:
        return BreakerStatus(state=TRADING_PAUSED, reasons=tuple(reasons))
    return BreakerStatus()


def apply_entry(
    state: BankrollState,
    *,
    size_usd: Decimal,
) -> BankrollState:
    """Book a simulated entry against the simulated bankroll."""

    from dataclasses import replace

    return replace(
        state,
        cash_usd=(state.cash_usd - size_usd).quantize(Decimal("0.000001")),
        open_exposure_usd=(state.open_exposure_usd + size_usd).quantize(Decimal("0.000001")),
        open_positions=state.open_positions + 1,
    )


def apply_exit(
    state: BankrollState,
    *,
    cost_basis_usd: Decimal,
    net_proceeds_usd: Decimal,
    closed: bool,
    day_key: str,
) -> BankrollState:
    """Book a simulated (partial or full) exit, including the loss streak."""

    from dataclasses import replace

    realized = (net_proceeds_usd - cost_basis_usd).quantize(Decimal("0.000001"))
    day_realized = (
        realized
        if day_key != state.day_key
        else (state.day_realized_net_pnl_usd + realized)
    )
    consecutive = state.consecutive_losses
    if closed:
        consecutive = state.consecutive_losses + 1 if realized < 0 else 0
    cash = (state.cash_usd + net_proceeds_usd).quantize(Decimal("0.000001"))
    exposure = max(ZERO, (state.open_exposure_usd - cost_basis_usd)).quantize(Decimal("0.000001"))
    updated = replace(
        state,
        cash_usd=cash,
        open_exposure_usd=exposure,
        open_positions=max(0, state.open_positions - (1 if closed else 0)),
        realized_net_pnl_usd=(state.realized_net_pnl_usd + realized).quantize(Decimal("0.000001")),
        consecutive_losses=consecutive,
        day_realized_net_pnl_usd=day_realized.quantize(Decimal("0.000001")),
        day_key=day_key,
    )
    return replace(updated, peak_equity_usd=max(updated.peak_equity_usd, updated.equity_usd))
