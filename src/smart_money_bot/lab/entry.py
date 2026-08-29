"""The single authoritative PAPER entry engine (sections P, Q, AE, AI).

Everything that decides "may this be bought?" lives here.  Discord handlers,
providers and radars consume the resulting :class:`TradeDecision`; none of them
re-derive eligibility.

The gates are fail-closed in the strongest sense the contract asks for:

* ``SAFETY PASS`` is required.  ``UNKNOWN`` and ``FAIL`` both block automatic
  entry, and missing evidence never becomes ``PASS``.
* Social evidence, fame and smart money are *supporting* only.  None of them can
  lift a safety, overextension, liquidity, cost or lifecycle block.
* A candidate that is already extended, whose signal is stale, or whose expected
  edge does not clear realistic round-trip costs, is rejected — preferring to
  miss a trade over becoming exit liquidity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .authenticity import AuthenticityAssessment
from .bankroll import BankrollState, SizingInputs, SizingResult, size_position
from .config import DEFAULT_LAB_CONFIG, LabConfig
from .costs import ExpectedEdge, estimate_expected_edge
from .decision import (
    Decision,
    EvidenceQuality,
    Reason,
    SafetyStatus,
    TradeDecision,
)
from .evidence import confidence_cap, organic_demand_state
from .lifecycle import (
    COOLDOWN,
    REENTRY_QUALIFIED,
    REENTRY_WATCH,
    ReentryAssessment,
    TokenLifecycle,
)
from .regime import UNKNOWN as REGIME_UNKNOWN
from .regime import MarketRegime
from .smartmoney import SmartMoneyAssessment

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class EntryContext:
    """All evidence known at decision time.  Nothing later may be consulted."""

    mint: str
    now: int
    price_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    liquidity_change_percent: Decimal | None = None
    volume_usd: Decimal | None = None
    volume_acceleration_ratio: Decimal | None = None

    # research funnel
    qualified: bool = False
    stage: str = "RAW_DISCOVERY"
    opportunity_score: Decimal = ZERO
    momentum_score: Decimal = ZERO
    organic_score: Decimal = ZERO
    momentum_acceleration_ratio: Decimal | None = None
    price_acceleration_ratio: Decimal | None = None
    buyer_acceleration_ratio: Decimal | None = None
    overextended: bool = False
    move_since_first_surface_percent: Decimal | None = None
    move_since_signal_percent: Decimal | None = None
    signal_age_seconds: int | None = None
    token_age_seconds: int | None = None

    # independence / concentration
    independent_buyers: int | None = None
    independence_ratio: Decimal | None = None
    cluster_supply_percent: Decimal | None = None
    fresh_wallet_percent: Decimal | None = None
    top10_percent: Decimal | None = None
    synchronized_funding: bool = False

    # flow
    buys: int = 0
    sells: int = 0
    flow_acceleration_ratio: Decimal | None = None

    # safety and execution
    safety_status: str = "UNKNOWN"
    safety_entry_eligible: bool = False
    route_available: bool = False
    sell_route_available: bool = False
    buy_price_impact_percent: Decimal | None = None
    sell_price_impact_percent: Decimal | None = None
    slippage_percent: Decimal | None = None
    decision_latency_ms: int | None = None

    # supporting evidence
    authenticity: AuthenticityAssessment | None = None
    smart_money: SmartMoneyAssessment | None = None
    social_edge_state: str = ""
    regime: MarketRegime = field(default_factory=MarketRegime)

    # projection inputs
    expected_upside_percent: Decimal | None = None
    expected_downside_percent: Decimal | None = None
    edge_confidence: Decimal | None = None

    # data quality
    data_degraded: bool = False
    data_unknown: bool = False
    provider_disagreement: bool = False

    @property
    def buy_sell_ratio(self) -> Decimal | None:
        if self.sells <= 0:
            return None if self.buys <= 0 else Decimal("99")
        return (Decimal(self.buys) / Decimal(self.sells)).quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class EntryEvaluation:
    """The decision plus the sizing and edge work that produced it."""

    decision: TradeDecision
    sizing: SizingResult = field(default_factory=SizingResult)
    edge: ExpectedEdge = field(default_factory=ExpectedEdge)
    blockers: tuple[str, ...] = ()
    supporting: tuple[str, ...] = ()

    @property
    def entry_eligible(self) -> bool:
        return self.decision.entry_eligible


def evaluate_entry(
    context: EntryContext,
    *,
    lifecycle: TokenLifecycle,
    bankroll: BankrollState,
    reentry: ReentryAssessment | None = None,
    exposure_in_token_usd: Decimal = ZERO,
    narrative_exposure_usd: Decimal = ZERO,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> EntryEvaluation:
    """Produce the one authoritative decision for this mint at this instant."""

    blockers: list[str] = []
    supporting: list[str] = []

    safety = _safety_status(context.safety_status)
    quality = _evidence_quality(context)

    # --- 1. hard, non-negotiable gates ------------------------------------
    if safety is SafetyStatus.FAIL:
        blockers.append(Reason.SAFETY_FAIL)
    elif safety is SafetyStatus.UNKNOWN or not context.safety_entry_eligible:
        blockers.append(Reason.SAFETY_UNKNOWN)
    else:
        supporting.append(Reason.SAFETY_PASS)

    if context.data_unknown:
        blockers.append(Reason.DATA_UNKNOWN)
    if context.data_degraded or context.provider_disagreement:
        blockers.append(Reason.DATA_DEGRADED)

    if not context.route_available or not context.sell_route_available:
        blockers.append(Reason.ROUTE_UNAVAILABLE)

    # --- 2. execution feasibility -----------------------------------------
    if context.liquidity_usd is None or context.liquidity_usd < config.min_liquidity_usd:
        blockers.append(Reason.LIQUIDITY_TOO_WEAK)
    else:
        supporting.append(Reason.LIQUIDITY_SUFFICIENT)

    worst_impact = _worst(
        context.buy_price_impact_percent, context.sell_price_impact_percent
    )
    if worst_impact is not None and worst_impact > config.max_price_impact_percent:
        blockers.append(Reason.PRICE_IMPACT_TOO_HIGH)
    if (
        context.slippage_percent is not None
        and context.slippage_percent > config.max_slippage_percent
    ):
        blockers.append(Reason.SLIPPAGE_TOO_HIGH)
    if (
        context.decision_latency_ms is not None
        and context.decision_latency_ms > config.max_decision_latency_ms
    ):
        blockers.append(Reason.LATENCY_TOO_HIGH)

    # --- 3. funnel qualification ------------------------------------------
    if not context.qualified:
        blockers.append(Reason.NOT_QUALIFIED)

    # --- 4. overextension / edge decay (sections O and Q) ------------------
    blockers.extend(_overextension_blockers(context, config=config))

    # --- 5. independence and authenticity ---------------------------------
    if (
        context.independent_buyers is None
        or context.independent_buyers < config.min_independent_buyers
    ):
        blockers.append(Reason.INDEPENDENT_BUYERS_TOO_FEW)
    else:
        supporting.append(Reason.INDEPENDENT_BUYERS_CONFIRMED)

    if (
        context.independence_ratio is not None
        and context.independence_ratio < config.min_independence_ratio
    ):
        blockers.append(Reason.MANUFACTURED_ACTIVITY)
    elif context.independence_ratio is not None:
        supporting.append(Reason.ORGANIC_DEMAND_CONFIRMED)

    if (
        context.cluster_supply_percent is not None
        and context.cluster_supply_percent > config.max_cluster_supply_percent
    ):
        blockers.append(Reason.CONCENTRATION_TOO_HIGH)
    if context.synchronized_funding:
        blockers.append(Reason.MANUFACTURED_ACTIVITY)

    authenticity = context.authenticity
    if authenticity is not None:
        if authenticity.quality is EvidenceQuality.UNKNOWN:
            # A missing bounded SOL-activity sample downgrades the evidence
            # quality and the position size; it is never read as authentic.
            # Independence and safety are gated separately and still fail
            # closed, so absent activity data cannot smuggle a trade through.
            pass
        elif authenticity.score < config.min_authenticity_score:
            blockers.append(Reason.MANUFACTURED_ACTIVITY)
        else:
            supporting.append(Reason.AUTHENTIC_ECONOMIC_ACTIVITY)

    smart_money = context.smart_money
    if smart_money is not None:
        if smart_money.shared_funding or smart_money.synchronized_entries:
            blockers.append(Reason.CLUSTERED_SMART_MONEY)
        elif smart_money.is_supporting_evidence:
            # Supporting only.  This never removes a blocker above.
            supporting.append(Reason.SMART_MONEY_INDEPENDENT)

    if context.social_edge_state == "EDGE_CONSUMED":
        blockers.append(Reason.SOCIAL_SIGNAL_LATE)

    # --- 6. regime ---------------------------------------------------------
    if context.regime.is_hostile:
        blockers.append(Reason.REGIME_UNFAVOURABLE)

    # --- 7. lifecycle ------------------------------------------------------
    lifecycle_state = lifecycle.state
    is_reentry = lifecycle.is_reentry
    if lifecycle.cooldown_active(context.now) or lifecycle_state == COOLDOWN:
        blockers.append(Reason.COOLDOWN_ACTIVE)
    if is_reentry:
        assessment = reentry
        if assessment is None or not assessment.qualified:
            blockers.append(Reason.REENTRY_NOT_STABILIZED)
            if assessment is not None and assessment.dead_cat:
                blockers.append(Reason.DEAD_CAT_BOUNCE)
        else:
            supporting.append(Reason.REENTRY_STABILIZED)
        drawdown = lifecycle.current_drawdown_percent
        if (
            drawdown is not None
            and drawdown >= config.retraced_drawdown_percent
            and (assessment is None or not assessment.qualified)
        ):
            blockers.append(Reason.OLD_WINNER_HEAVY_DRAWDOWN)
    else:
        supporting.append(Reason.SETUP_FRESH)

    # --- 8. sizing and bankroll -------------------------------------------
    sizing = size_position(
        bankroll,
        SizingInputs(
            liquidity_usd=context.liquidity_usd,
            price_impact_percent=worst_impact,
            slippage_percent=context.slippage_percent,
            independence_ratio=context.independence_ratio,
            edge_confidence=context.edge_confidence,
            is_reentry=is_reentry,
            volatility_percent=context.move_since_signal_percent,
            regime=context.regime.state or REGIME_UNKNOWN,
            authenticity_score=(
                authenticity.score
                if authenticity is not None
                and authenticity.quality is not EvidenceQuality.UNKNOWN
                else None
            ),
        ),
        config=config,
        exposure_in_token_usd=exposure_in_token_usd,
        narrative_exposure_usd=narrative_exposure_usd,
    )
    if sizing.blocked_reason:
        blockers.append(sizing.blocked_reason)

    # --- 9. expected net edge ---------------------------------------------
    # Confidence is a claim about how much is known, so it is ceilinged by the
    # weakest evidence behind it.  A caller that hands in an over-stated number
    # cannot push an unjustified confidence into the persisted decision or the
    # rendered card.
    cap = confidence_cap(
        evidence_quality=quality,
        authenticity_quality=(authenticity.quality if authenticity is not None else None),
        activity_available=(
            authenticity.activity.available if authenticity is not None else None
        ),
        safety_status=str(safety),
        data_degraded=context.data_degraded or context.provider_disagreement,
    )
    notional = sizing.size_usd if sizing.size_usd > 0 else config.normal_position_usd
    edge = estimate_expected_edge(
        notional_usd=notional,
        gross_upside_percent=context.expected_upside_percent,
        downside_percent=context.expected_downside_percent,
        buy_price_impact_percent=context.buy_price_impact_percent,
        sell_price_impact_percent=context.sell_price_impact_percent,
        slippage_bps=(
            int(context.slippage_percent * 100) if context.slippage_percent is not None else None
        ),
        confidence=cap.apply(context.edge_confidence),
        quality=quality,
        config=config,
    )
    if not edge.meets(config):
        blockers.append(Reason.EXPECTED_NET_EDGE_TOO_LOW)
    else:
        supporting.append(Reason.NET_EDGE_SUFFICIENT)

    ordered_blockers = tuple(dict.fromkeys(blockers))
    decision = _resolve_decision(
        ordered_blockers,
        lifecycle=lifecycle,
        reentry=reentry,
        is_reentry=is_reentry,
    )
    reason_codes = ordered_blockers if ordered_blockers else tuple(dict.fromkeys(supporting))
    size = sizing.size_usd if decision in {Decision.ENTRY, Decision.REENTRY_QUALIFIED} else ZERO

    trade_decision = TradeDecision(
        mint=context.mint,
        decision=decision,
        reason_codes=reason_codes,
        evidence_quality=quality,
        safety=safety,
        lifecycle_state=lifecycle_state,
        expected_net_edge_percent=edge.net_edge_percent,
        edge_confidence=edge.confidence,
        size_usd=size,
        price_usd=context.price_usd,
        market_cap_usd=context.market_cap_usd,
        strategy_version=config.strategy_version,
        config_hash=config.config_hash(),
        timestamp=context.now,
        evidence={
            "stage": context.stage,
            "opportunity_score": str(context.opportunity_score),
            "momentum_score": str(context.momentum_score),
            "organic_score": str(context.organic_score),
            "independent_buyers": context.independent_buyers,
            "liquidity_usd": str(context.liquidity_usd) if context.liquidity_usd else None,
            "authenticity_score": str(authenticity.score) if authenticity else None,
            "smart_money_strength": str(smart_money.strength) if smart_money else None,
            "regime": context.regime.state,
            "is_reentry": is_reentry,
            "cost_percent": str(edge.cost_percent),
            "buy_price_impact_percent": (
                str(context.buy_price_impact_percent)
                if context.buy_price_impact_percent is not None
                else None
            ),
            "slippage_percent": (
                str(context.slippage_percent) if context.slippage_percent is not None else None
            ),
            "gross_upside_percent": str(edge.gross_upside_percent),
            "confidence_ceiling": str(cap.ceiling),
            "confidence_limited_by": list(cap.reasons),
            "organic_demand_state": organic_demand_state(
                authenticity_quality=(
                    authenticity.quality if authenticity is not None else None
                ),
            ),
            "sizing_multiplier": str(sizing.multiplier),
            "sizing_reductions": list(sizing.reductions),
            "supporting": list(dict.fromkeys(supporting)),
            "blockers": list(ordered_blockers),
        },
    )
    return EntryEvaluation(
        decision=trade_decision,
        sizing=sizing,
        edge=edge,
        blockers=ordered_blockers,
        supporting=tuple(dict.fromkeys(supporting)),
    )


def _resolve_decision(
    blockers: tuple[str, ...],
    *,
    lifecycle: TokenLifecycle,
    reentry: ReentryAssessment | None,
    is_reentry: bool,
) -> Decision:
    """Map blockers onto the canonical decision, preserving lifecycle nuance."""

    if not blockers:
        return Decision.REENTRY_QUALIFIED if is_reentry else Decision.ENTRY

    if Reason.COOLDOWN_ACTIVE in blockers:
        return Decision.COOLDOWN
    if lifecycle.state in {REENTRY_WATCH, REENTRY_QUALIFIED} or (
        is_reentry and reentry is not None and not reentry.qualified
    ):
        # A retraced old winner that is only missing *new* evidence is watched,
        # not rejected outright.
        hard = {
            Reason.SAFETY_FAIL,
            Reason.MANUFACTURED_ACTIVITY,
            Reason.CLUSTERED_SMART_MONEY,
            Reason.DEAD_CAT_BOUNCE,
            Reason.LIQUIDITY_TOO_WEAK,
        }
        if not hard.intersection(blockers):
            return Decision.REENTRY_WATCH

    rejecting = {
        Reason.SAFETY_FAIL,
        Reason.MANUFACTURED_ACTIVITY,
        Reason.CLUSTERED_SMART_MONEY,
        Reason.EDGE_CONSUMED,
        Reason.ALREADY_EXTENDED,
        Reason.MOMENTUM_EXHAUSTED,
        Reason.LIQUIDITY_TOO_WEAK,
        Reason.PRICE_IMPACT_TOO_HIGH,
        Reason.SLIPPAGE_TOO_HIGH,
        Reason.CONCENTRATION_TOO_HIGH,
        Reason.SOCIAL_SIGNAL_LATE,
        Reason.OLD_WINNER_HEAVY_DRAWDOWN,
        Reason.DEAD_CAT_BOUNCE,
        Reason.ROUTE_UNAVAILABLE,
        Reason.REGIME_UNFAVOURABLE,
        Reason.TRADING_PAUSED_DATA_CONTROL_RISK,
    }
    if rejecting.intersection(blockers):
        return Decision.REJECT
    return Decision.WAIT


def _overextension_blockers(
    context: EntryContext,
    *,
    config: LabConfig,
) -> list[str]:
    """Section Q: prefer missing a trade over becoming exit liquidity."""

    blockers: list[str] = []
    if context.overextended:
        blockers.append(Reason.ALREADY_EXTENDED)

    if (
        context.move_since_first_surface_percent is not None
        and context.move_since_first_surface_percent
        >= config.max_expansion_from_first_surface_percent
    ):
        blockers.append(Reason.ALREADY_EXTENDED)

    if (
        context.move_since_signal_percent is not None
        and context.move_since_signal_percent >= config.max_move_since_signal_percent
    ):
        blockers.append(Reason.EDGE_CONSUMED)

    if (
        context.signal_age_seconds is not None
        and context.signal_age_seconds > config.max_signal_age_seconds
    ):
        blockers.append(Reason.SIGNAL_STALE)

    acceleration = context.price_acceleration_ratio
    if acceleration is not None and acceleration >= config.max_price_acceleration_ratio:
        buyers = context.buyer_acceleration_ratio
        if buyers is None or buyers < config.min_buyer_acceleration_ratio:
            # Price accelerating without buyers accelerating is the classic
            # blow-off / exit-liquidity shape.
            blockers.append(Reason.ALREADY_EXTENDED)

    ratio = context.buy_sell_ratio
    if ratio is not None and ratio < Decimal("0.8") and context.momentum_score < 50:
        blockers.append(Reason.MOMENTUM_EXHAUSTED)

    if (
        context.flow_acceleration_ratio is not None
        and context.flow_acceleration_ratio < Decimal("0.5")
    ):
        blockers.append(Reason.MOMENTUM_EXHAUSTED)

    if (
        context.liquidity_change_percent is not None
        and context.liquidity_change_percent <= Decimal("-30")
    ):
        blockers.append(Reason.LIQUIDITY_TOO_WEAK)

    if context.momentum_score <= config.momentum_decay_exit_score and context.qualified:
        blockers.append(Reason.MOMENTUM_EXHAUSTED)

    return blockers


def _worst(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    """The harsher of the two route legs; an unknown leg never looks better."""

    values = [value for value in (left, right) if value is not None]
    return max(values) if values else None


def _safety_status(value: str) -> SafetyStatus:
    try:
        return SafetyStatus(value)
    except ValueError:
        return SafetyStatus.UNKNOWN


def _evidence_quality(context: EntryContext) -> EvidenceQuality:
    if context.data_unknown:
        return EvidenceQuality.UNKNOWN
    required = (
        context.price_usd,
        context.liquidity_usd,
        context.independent_buyers,
        context.expected_upside_percent,
    )
    if any(item is None for item in required):
        return EvidenceQuality.PARTIAL
    if context.data_degraded or context.provider_disagreement:
        return EvidenceQuality.PARTIAL
    if context.authenticity is not None and context.authenticity.quality is not (
        EvidenceQuality.COMPLETE
    ):
        return EvidenceQuality.PARTIAL
    return EvidenceQuality.COMPLETE


def why_not_entry(evaluation: EntryEvaluation) -> tuple[str, ...]:
    """Human-readable "WHY NOT ENTRY" lines for the Discord card (section BG)."""

    if evaluation.entry_eligible:
        return ()
    return evaluation.decision.human_reasons


__all__ = ["EntryContext", "EntryEvaluation", "evaluate_entry", "why_not_entry"]
