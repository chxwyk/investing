"""The $2 NET objective layered onto the existing staged exit engine.

Sections 8-12 of the Shadow contract.

This module deliberately does **not** reimplement exits.  The staged ladder,
break-even arming, trailing protection, momentum decay, flow reversal,
smart-money distribution, liquidity deterioration, safety failure, the hard stop
and the time stop already exist in :mod:`smart_money_bot.lab.exits` and are
already tested; SHADOW runs that same engine through
:meth:`ShadowConfig.exit_config`.

What is added here is the one thing a $10 position needs that a percentage-based
ladder cannot express: *meaningful dollars*.  On a $10 stake a "+25%" milestone
is $2.50 gross and can be under a dollar net.  So SHADOW asks a second question
on top of the staged plan:

    Has this position cleared **$2 NET**, and if so, is the runner still healthy
    enough to justify holding it?

The rules that follow from that are asymmetric on purpose:

* ``+$2 NET`` is **never** a reason to hold a broken setup — every emergency in
  the underlying engine still fires first and unmodified.
* ``+$2 NET`` is **never** an automatic full sell either (section 9).  A healthy,
  accelerating runner takes a small partial and keeps meaningful exposure.
* Below ``+$2 NET`` the underlying staged plan is returned untouched, so a
  losing or breaking position de-risks exactly as it does today.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .config import LabConfig
from .costs import leg_costs
from .exits import (
    EXIT_MILESTONE,
    EXIT_MOON_BAG,
    EXIT_SAFETY_EMERGENCY,
    HOLD_NO_TRIGGER,
    ExitContext,
    ExitPlan,
    PaperPosition,
    plan_exit,
)
from .shadow import DEFAULT_SHADOW_CONFIG, ShadowConfig

ZERO = Decimal("0")
UNIT = Decimal("0.000001")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")

# --- shadow-specific exit reason codes (append-only) -------------------------
SHADOW_SECURE_OBJECTIVE = "SHADOW_SECURE_NET_OBJECTIVE"
SHADOW_RUNNER_PARTIAL = "SHADOW_RUNNER_PARTIAL_TAKE"
SHADOW_PRINCIPAL_RECOVERY = "SHADOW_PRINCIPAL_RECOVERY"
HOLD_SHADOW_RUNNER = "SHADOW_HEALTHY_RUNNER_HOLD"
SHADOW_STALE_OBSERVATION = "SHADOW_STALE_OBSERVATION"
SHADOW_SAFETY_MONITOR = "SHADOW_SAFETY_UNKNOWN_MONITORING"
SHADOW_SOFT_PAUSE_HOLD = "SHADOW_SOFT_PAUSE_HOLD"

#: Reasons the overlay must never touch: they are emergencies or protection of
#: a profit that has already started giving back.
PROTECTED_REASONS: frozenset[str] = frozenset(
    {
        "SAFETY_DETERIORATION",
        "LIQUIDITY_COLLAPSE_EMERGENCY",
        "HARD_LOSS_PROTECTION",
        "TRAILING_PROFIT_PROTECTION",
        "BREAK_EVEN_PROTECTION",
        "TIME_STOP",
    }
)

# --- momentum change classes (sections 55, 56) -------------------------------
#: Momentum slowed, but demand, flow, liquidity and route are all still fine and
#: price is near its recent high.  A pause, not a death.
MOMENTUM_SOFT_PAUSE = "SOFT_PAUSE"
#: Several independent things are weakening together.
MOMENTUM_CONFIRMED_DECAY = "CONFIRMED_DECAY"
#: Sellers in control, drawdown, liquidity leaving, distribution.
MOMENTUM_HARD_REVERSAL = "HARD_REVERSAL"
#: Nothing is wrong.
MOMENTUM_HEALTHY = "HEALTHY"

MOMENTUM_STATES: tuple[str, ...] = (
    MOMENTUM_HEALTHY,
    MOMENTUM_SOFT_PAUSE,
    MOMENTUM_CONFIRMED_DECAY,
    MOMENTUM_HARD_REVERSAL,
)

#: Non-emergency de-risk reasons the momentum classifier is allowed to soften.
#: Safety, liquidity emergencies, the hard stop, trailing and break-even are
#: deliberately absent: section 56 permits patience with momentum, never with
#: an emergency.
SOFTENABLE_REASONS: frozenset[str] = frozenset(
    {
        "MOMENTUM_DECAY",
        "BUY_FLOW_REVERSAL",
        "VOLUME_EXHAUSTION",
    }
)

#: How many consecutive weak observations before a slowdown counts as decay.
MIN_WEAK_OBSERVATIONS_FOR_DECAY = 2
#: How many independent negative signals make a single observation conclusive.
MIN_INDEPENDENT_NEGATIVES_FOR_DECAY = 2
#: Drawdown from the post-entry high that is material on its own.
MATERIAL_PEAK_DRAWDOWN_PERCENT = Decimal("22")


# --- runner health bands (section 9) -----------------------------------------
HEALTH_ACCELERATING = "ACCELERATING"
HEALTH_HEALTHY = "HEALTHY"
HEALTH_MIXED = "MIXED"
HEALTH_WEAK = "WEAK"

HEALTH_BANDS: tuple[str, ...] = (
    HEALTH_ACCELERATING,
    HEALTH_HEALTHY,
    HEALTH_MIXED,
    HEALTH_WEAK,
)


@dataclass(frozen=True, slots=True)
class RunnerEvidence:
    """The extra current evidence the $2 objective consults (section 9).

    All of it is already collected by the pipeline for other purposes, so
    consulting it costs no additional provider request.
    """

    momentum_accelerating: bool = False
    independent_buyer_growth: int | None = None
    volume_ratio: Decimal | None = None
    liquidity_growth_percent: Decimal | None = None
    smart_money_accumulating: bool = False
    smart_money_distributing: bool = False
    catalyst_fresh: bool = False
    actionability_state: str = ""
    route_quality: str = ""
    #: A safety provider is unavailable, so "UNKNOWN" says nothing about the
    #: token (section 8).  This is emphatically NOT a confirmed failure.
    safety_provider_degraded: bool = False
    #: A provider actively confirmed the token is unsafe.  Only this exits.
    safety_confirmed_fail: bool = False
    #: How many consecutive observations have looked weak.  One weak tick is a
    #: wobble; three in a row is a trend (section 56).
    consecutive_weak_observations: int = 0
    #: A strong or accelerating story with healthy flow is evidence that a
    #: momentum cooldown is cooling rather than dying (section 57).  It never
    #: overrides a confirmed hard failure.
    story_state: str = ""


#: No extra runner evidence beyond what the exit context already carries.
NO_RUNNER_EVIDENCE = RunnerEvidence()


@dataclass(frozen=True, slots=True)
class ShadowNetPnl:
    """NET dollars for one open simulated position, at this price."""

    realized_net_usd: Decimal = ZERO
    unrealized_gross_usd: Decimal = ZERO
    exit_cost_usd: Decimal = ZERO
    unrealized_net_usd: Decimal = ZERO
    total_net_usd: Decimal = ZERO
    remaining_value_usd: Decimal = ZERO

    @property
    def meets(self) -> bool:  # pragma: no cover - trivial, kept for symmetry
        return self.total_net_usd > 0


def net_pnl_now(
    position: PaperPosition,
    price: Decimal | None,
    *,
    price_impact_percent: Decimal | None = None,
    slippage_bps: int | None = None,
    config: ShadowConfig = DEFAULT_SHADOW_CONFIG,
) -> ShadowNetPnl:
    """What this position is worth NET if the remainder were sold right now.

    "NET" means after the exit leg's platform fee, network fee, priority fee,
    price impact and slippage — the same model the realized journal uses, so a
    held position and a closed one are measured on identical terms.
    """

    realized = position.realized_net_pnl_usd
    if price is None or price <= 0 or position.tokens_remaining <= 0:
        return ShadowNetPnl(
            realized_net_usd=realized,
            total_net_usd=realized,
        )
    value = (position.tokens_remaining * price).quantize(UNIT)
    costs = leg_costs(
        value,
        price_impact_percent=price_impact_percent,
        slippage_bps=slippage_bps,
        config=config,
    )
    gross = (value - position.cost_basis_remaining_usd).quantize(UNIT)
    net = (gross - costs.total_cost_usd).quantize(UNIT)
    return ShadowNetPnl(
        realized_net_usd=realized,
        unrealized_gross_usd=gross,
        exit_cost_usd=costs.total_cost_usd,
        unrealized_net_usd=net,
        total_net_usd=(realized + net).quantize(UNIT),
        remaining_value_usd=value,
    )


@dataclass(frozen=True, slots=True)
class RunnerHealth:
    """Is this runner still worth holding? (section 9)"""

    band: str = HEALTH_WEAK
    score: Decimal = ZERO
    positives: tuple[str, ...] = field(default_factory=tuple)
    negatives: tuple[str, ...] = field(default_factory=tuple)

    @property
    def healthy(self) -> bool:
        return self.band in {HEALTH_ACCELERATING, HEALTH_HEALTHY}

    @property
    def accelerating(self) -> bool:
        return self.band == HEALTH_ACCELERATING

    @property
    def weak(self) -> bool:
        return self.band == HEALTH_WEAK


def assess_runner_health(
    position: PaperPosition,
    context: ExitContext,
    evidence: RunnerEvidence = NO_RUNNER_EVIDENCE,
    *,
    config: ShadowConfig = DEFAULT_SHADOW_CONFIG,
) -> RunnerHealth:
    """Grade the current runner from evidence available *now*.

    The future peak is never an input.  This function may only read what the
    position and the current observation already contain, which is what makes
    the no-look-ahead guarantee hold for exits as well as entries.
    """

    positives: list[str] = []
    negatives: list[str] = []
    score = Decimal("50")

    if context.safety_status == "FAIL":
        return RunnerHealth(HEALTH_WEAK, ZERO, (), ("safety failed",))
    if context.safety_status == "UNKNOWN":
        score -= 10
        negatives.append("safety unknown")
    if not context.route_available:
        return RunnerHealth(HEALTH_WEAK, ZERO, (), ("no sell route",))

    momentum = context.momentum_score
    if momentum is not None:
        if momentum >= 65:
            score += 15
            positives.append("momentum strong")
        elif momentum >= 50:
            score += 5
        else:
            score -= 15
            negatives.append("momentum weak")
    if context.momentum_reaccelerating or evidence.momentum_accelerating:
        score += 12
        positives.append("momentum accelerating")

    ratio = context.buy_sell_ratio
    if ratio is not None:
        if ratio >= Decimal("1.5"):
            score += 12
            positives.append("buyers outnumber sellers")
        elif ratio >= Decimal("1.1"):
            score += 4
        else:
            score -= 15
            negatives.append("buy flow fading")

    organic = context.organic_score
    if organic is not None:
        if organic >= 60:
            score += 8
            positives.append("organic demand holding")
        elif organic < 40:
            score -= 10
            negatives.append("organic demand weak")

    growth = evidence.independent_buyer_growth
    if growth is not None:
        if growth > 0:
            score += 8
            positives.append("independent buyers still growing")
        elif growth < 0:
            score -= 8
            negatives.append("independent buyers leaving")

    if evidence.volume_ratio is not None:
        if evidence.volume_ratio >= Decimal("1"):
            score += 6
            positives.append("volume sustained")
        elif evidence.volume_ratio <= Decimal("0.4"):
            score -= 10
            negatives.append("volume drying up")

    liquidity_change = context.liquidity_change_percent
    if liquidity_change is not None:
        if liquidity_change >= Decimal("10"):
            score += 8
            positives.append("liquidity growing")
        elif liquidity_change <= Decimal("-15"):
            score -= 15
            negatives.append("liquidity draining")
    if evidence.liquidity_growth_percent is not None:
        if evidence.liquidity_growth_percent >= Decimal("10"):
            score += 4
        elif evidence.liquidity_growth_percent <= Decimal("-15"):
            score -= 6

    if context.smart_money_distributing or evidence.smart_money_distributing:
        score -= 20
        negatives.append("smart wallets distributing")
    elif context.smart_money_accumulating or evidence.smart_money_accumulating:
        score += 12
        positives.append("smart wallets still accumulating")

    if evidence.catalyst_fresh:
        score += 6
        positives.append("catalyst still fresh")

    state = evidence.actionability_state.upper()
    if state == "ACTIONABLE":
        score += 8
        positives.append("still actionable")
    elif state in {"DETERIORATED", "EDGE_CONSUMED", "EXPIRED", "INVALIDATED"}:
        score -= 15
        negatives.append(f"actionability {state.lower()}")

    drawdown = position.drawdown_from_peak_percent(context.price_usd)
    if drawdown is not None:
        if drawdown >= Decimal("30"):
            score -= 18
            negatives.append(f"down {drawdown}% from the post-entry high")
        elif drawdown >= Decimal("15"):
            score -= 8
            negatives.append(f"down {drawdown}% from the post-entry high")

    quality = evidence.route_quality.upper()
    if quality in {"DEGRADED", "POOR"}:
        score -= 10
        negatives.append("route quality degraded")

    bounded = max(ZERO, min(HUNDRED, score)).quantize(CENT)
    if bounded >= 75 and not negatives:
        band = HEALTH_ACCELERATING
    elif bounded >= 60:
        band = HEALTH_HEALTHY
    elif bounded >= 42:
        band = HEALTH_MIXED
    else:
        band = HEALTH_WEAK
    return RunnerHealth(
        band=band,
        score=bounded,
        positives=tuple(positives),
        negatives=tuple(negatives),
    )


@dataclass(frozen=True, slots=True)
class MomentumVerdict:
    """Is this a pause, a decay or a reversal? (sections 55, 56)"""

    state: str = MOMENTUM_HEALTHY
    negatives: tuple[str, ...] = field(default_factory=tuple)
    positives: tuple[str, ...] = field(default_factory=tuple)
    consecutive_weak: int = 0
    peak_drawdown_percent: Decimal | None = None

    @property
    def soft(self) -> bool:
        return self.state == MOMENTUM_SOFT_PAUSE

    @property
    def conclusive(self) -> bool:
        return self.state in {MOMENTUM_CONFIRMED_DECAY, MOMENTUM_HARD_REVERSAL}


def classify_momentum(
    position: PaperPosition,
    context: ExitContext,
    evidence: RunnerEvidence = NO_RUNNER_EVIDENCE,
    *,
    config: ShadowConfig = DEFAULT_SHADOW_CONFIG,
) -> MomentumVerdict:
    """Separate a momentum pause from a real reversal.

    The shared engine de-risks 50% the first time momentum prints weak.  On a
    volatile fresh token that fires constantly, and it is the single most
    expensive class of exit: it sells a live runner into a wobble.

    Section 56's rule is applied here — a non-emergency reduction wants
    *repeated* weakness, or *several independent* negatives, or a material
    drawdown from the post-entry high — while leaving every emergency untouched.
    """

    negatives: list[str] = []
    positives: list[str] = []
    # Some observations are not wobbles.  Heavy realized selling or smart-money
    # distribution is a fact about the market, not a noisy score, so either is
    # conclusive on its own and does not have to wait for a second opinion.
    strong_negatives: list[str] = []

    ratio = context.buy_sell_ratio
    liquidity_change = context.liquidity_change_percent
    drawdown = position.drawdown_from_peak_percent(context.price_usd)

    if ratio is not None and ratio <= Decimal("0.5"):
        strong_negatives.append(f"heavy selling: {context.buys} buys to {context.sells} sells")
    elif ratio is not None and ratio < Decimal("0.8"):
        negatives.append("sellers now outnumber buyers")
    elif ratio is not None and ratio >= Decimal("1.2"):
        positives.append("buyers still lead")

    if liquidity_change is not None and liquidity_change <= Decimal("-20"):
        negatives.append(f"liquidity down {liquidity_change}%")
    elif liquidity_change is not None and liquidity_change >= ZERO:
        positives.append("liquidity holding")

    if context.smart_money_distributing or evidence.smart_money_distributing:
        strong_negatives.append("smart wallets distributing")
    elif context.smart_money_accumulating or evidence.smart_money_accumulating:
        positives.append("smart wallets still accumulating")

    if evidence.independent_buyer_growth is not None:
        if evidence.independent_buyer_growth < 0:
            negatives.append("independent buyers leaving")
        elif evidence.independent_buyer_growth > 0:
            positives.append("independent buyers still arriving")

    if evidence.volume_ratio is not None and evidence.volume_ratio <= Decimal("0.3"):
        negatives.append("volume collapsed")

    if drawdown is not None and drawdown >= MATERIAL_PEAK_DRAWDOWN_PERCENT:
        negatives.append(f"down {drawdown}% from the post-entry high")

    if not context.route_available:
        negatives.append("no sell route")
    if context.safety_status == "FAIL":
        negatives.append("safety failed")

    if evidence.story_state in {"ACCELERATING", "STRONG", "VIRAL"}:
        # Section 57: a live story is evidence that a cooldown is cooling, not
        # dying.  It is a positive, never a veto over a confirmed failure.
        positives.append(f"story still {evidence.story_state.lower()}")

    weak = evidence.consecutive_weak_observations

    material_drawdown = (
        drawdown is not None and drawdown >= MATERIAL_PEAK_DRAWDOWN_PERCENT
    )
    broken = context.safety_status == "FAIL" or not context.route_available
    total = len(negatives) + len(strong_negatives)
    if (
        broken
        or len(strong_negatives) >= 2
        or total >= 3
        or (total >= 2 and material_drawdown)
    ):
        state = MOMENTUM_HARD_REVERSAL
    elif (
        strong_negatives
        or total >= MIN_INDEPENDENT_NEGATIVES_FOR_DECAY
        or weak >= MIN_WEAK_OBSERVATIONS_FOR_DECAY
        or material_drawdown
    ):
        state = MOMENTUM_CONFIRMED_DECAY
    elif negatives:
        state = MOMENTUM_SOFT_PAUSE
    else:
        state = MOMENTUM_HEALTHY

    return MomentumVerdict(
        state=state,
        negatives=tuple((*strong_negatives, *negatives)),
        positives=tuple(positives),
        consecutive_weak=weak,
        peak_drawdown_percent=drawdown,
    )


@dataclass(frozen=True, slots=True)
class ShadowExitAssessment:
    """The plan plus the reasoning behind it, for the card and `/fomo shadow`."""

    plan: ExitPlan = field(default_factory=ExitPlan)
    base_plan: ExitPlan = field(default_factory=ExitPlan)
    health: RunnerHealth = field(default_factory=RunnerHealth)
    net: ShadowNetPnl = field(default_factory=ShadowNetPnl)
    objective_met: bool = False
    momentum: MomentumVerdict = field(default_factory=MomentumVerdict)
    why: tuple[str, ...] = field(default_factory=tuple)

    @property
    def acts(self) -> bool:
        return self.plan.acts

    @property
    def holding_reason(self) -> str:
        return self.why[0] if self.why else "no exit trigger"


def plan_shadow_exit(
    position: PaperPosition,
    context: ExitContext,
    evidence: RunnerEvidence = NO_RUNNER_EVIDENCE,
    *,
    config: ShadowConfig = DEFAULT_SHADOW_CONFIG,
    lab_config: LabConfig | None = None,
) -> ShadowExitAssessment:
    """Decide what SHADOW does with this position now.

    Priority order is exactly section 12: safety, route/liquidity, hard stop and
    the other protections come from the underlying engine and are returned
    unchanged; only after those does the $2 NET objective get a say.
    """

    exit_config = lab_config if lab_config is not None else config.exit_config()
    base = plan_exit(position, context, config=exit_config)
    health = assess_runner_health(position, context, evidence, config=config)
    momentum = classify_momentum(position, context, evidence, config=config)
    net = net_pnl_now(
        position,
        context.price_usd,
        price_impact_percent=context.price_impact_percent,
        slippage_bps=context.slippage_bps,
        config=config,
    )
    objective_met = net.total_net_usd >= config.net_profit_objective_usd

    def result(
        plan: ExitPlan,
        why: tuple[str, ...],
    ) -> ShadowExitAssessment:
        return ShadowExitAssessment(
            plan=plan,
            base_plan=base,
            health=health,
            net=net,
            objective_met=objective_met,
            momentum=momentum,
            why=why,
        )

    # 1. Emergencies and profit protection are never overridden (section 12) —
    #    with one exception the production log made unavoidable.
    if base.reason_code in PROTECTED_REASONS:
        rescued = _rescue_provider_outage(base, context, evidence, health)
        if rescued is not None:
            return result(
                rescued,
                (
                    "safety is UNKNOWN because a provider is unavailable, not "
                    "because the token failed",
                    "route, liquidity and flow are still healthy — monitoring "
                    "instead of selling",
                ),
            )
        return result(base, (f"protective exit: {base.reason_code.lower().replace('_', ' ')}",))

    price = context.price_usd
    if price is None or price <= 0:
        return result(base, ("no usable current price",))

    # 1b. A momentum *pause* is not a momentum *reversal* (sections 55, 56).
    #     The shared engine reduces on the first weak print; on a fresh, volatile
    #     token that repeatedly sells live runners into wobbles.  A reduction now
    #     needs repeated weakness, several independent negatives, or a material
    #     drawdown from the post-entry high.  Every emergency above is untouched,
    #     and trailing and break-even protection stay armed, so a real giveback
    #     still exits without waiting for momentum to agree.
    if base.acts and base.reason_code in SOFTENABLE_REASONS and not momentum.conclusive:
        return result(
            ExitPlan(
                fraction=ZERO,
                reason_code=SHADOW_SOFT_PAUSE_HOLD,
                final=False,
                notes=(
                    f"{base.reason_code.lower().replace('_', ' ')} on a single "
                    "observation, with demand still intact",
                    *momentum.positives[:2],
                ),

            ),
            (
                "momentum paused rather than reversed — holding",
                *momentum.positives[:2],
                *(
                    (f"one negative so far: {momentum.negatives[0]}",)
                    if momentum.negatives
                    else ()
                ),
            ),
        )

    if not objective_met:
        # Below the objective the staged engine decides alone, so a broken setup
        # still de-risks and a loser still stops out.
        why = (
            f"NET {net.total_net_usd:+.2f} is below the ${config.net_profit_objective_usd} "
            "objective",
            f"runner health {health.band}",
        )
        return result(base, why)

    # 2. The objective is met.  Decide by structure, not by the number alone.
    multiple = (
        net.total_net_usd / config.net_profit_objective_usd
        if config.net_profit_objective_usd > 0
        else ZERO
    )

    if health.weak:
        fraction = max(base.fraction, config.secure_fraction_weak)
        return result(
            ExitPlan(
                fraction=fraction,
                reason_code=SHADOW_SECURE_OBJECTIVE,
                final=False,
                notes=(
                    f"+${net.total_net_usd} NET secured — structure is weak",
                    *health.negatives[:3],
                ),
            ),
            (
                f"secured +${net.total_net_usd} NET because the runner weakened",
                *health.negatives[:2],
            ),
        )

    if health.band == HEALTH_MIXED:
        fraction = max(base.fraction, config.secure_fraction_mixed)
        return result(
            ExitPlan(
                fraction=fraction,
                reason_code=SHADOW_SECURE_OBJECTIVE,
                final=False,
                notes=(
                    f"+${net.total_net_usd} NET, mixed structure — half secured",
                    *health.negatives[:2],
                ),
            ),
            (
                f"took half at +${net.total_net_usd} NET on mixed evidence",
                *health.negatives[:2],
            ),
        )

    # 3. Healthy runner past the objective (sections 9, 10).
    if multiple >= config.principal_recovery_multiple:
        # Big gain: recover principal, lock profit, keep a funded moon bag.
        recovery_fraction = _principal_recovery_fraction(position, price, config=config)
        fraction = max(base.fraction, recovery_fraction)
        if fraction > 0:
            return result(
                ExitPlan(
                    fraction=fraction,
                    reason_code=SHADOW_PRINCIPAL_RECOVERY,
                    final=False,
                    notes=(
                        f"+${net.total_net_usd} NET — principal recovered, moon bag runs",
                        *health.positives[:2],
                    ),
                ),
                (
                    f"recovered principal at +${net.total_net_usd} NET and kept a runner",
                    *health.positives[:2],
                ),
            )

    if base.acts and base.reason_code == EXIT_MILESTONE:
        # The ladder wants to take profit while the runner is genuinely
        # accelerating: take a smaller slice and keep meaningful exposure.
        fraction = (
            min(base.fraction, config.secure_fraction_healthy)
            if health.accelerating
            else base.fraction
        )
        return result(
            ExitPlan(
                fraction=fraction,
                reason_code=EXIT_MILESTONE,
                final=False,
                notes=(
                    *base.notes,
                    f"+${net.total_net_usd} NET while the runner is {health.band.lower()}",
                ),
            ),
            (
                f"staged take-profit trimmed because the runner is {health.band.lower()}",
                *health.positives[:2],
            ),
        )

    if base.acts and base.reason_code != EXIT_MOON_BAG:
        # A de-risk trigger fired (momentum decay, flow reversal, distribution,
        # liquidity, concentration).  With the objective already banked, honour
        # it: securing real dollars beats defending a thesis.
        return result(
            base,
            (
                f"de-risked at +${net.total_net_usd} NET on "
                f"{base.reason_code.lower().replace('_', ' ')}",
            ),
        )

    if health.accelerating:
        return result(
            ExitPlan(reason_code=HOLD_SHADOW_RUNNER, notes=base.notes),
            (
                f"holding: +${net.total_net_usd} NET and the runner is still accelerating",
                *health.positives[:3],
            ),
        )

    # Healthy but not accelerating, and nothing else fired: take the standard
    # slice so the meaningful dollars are not purely theoretical.
    fraction = max(base.fraction, config.secure_fraction_healthy)
    if fraction <= 0:
        return result(
            ExitPlan(reason_code=HOLD_SHADOW_RUNNER, notes=base.notes),
            (f"holding: +${net.total_net_usd} NET and the runner is healthy",),
        )
    return result(
        ExitPlan(
            fraction=fraction,
            reason_code=SHADOW_RUNNER_PARTIAL,
            final=False,
            notes=(f"+${net.total_net_usd} NET banked; the rest keeps running",),
        ),
        (
            f"banked part of +${net.total_net_usd} NET and kept the runner",
            *health.positives[:2],
        ),
    )


def _rescue_provider_outage(
    base: ExitPlan,
    context: ExitContext,
    evidence: RunnerEvidence,
    health: RunnerHealth,
) -> ExitPlan | None:
    """Stop a dead provider from being read as a dead token (section 8).

    The shared exit engine de-risks 50% whenever safety reads ``UNKNOWN`` while a
    position is in profit.  That is right when the evidence is genuinely missing.
    It is wrong when a *provider* is down: production ran for hours with Solana
    Tracker returning ``403 Insufficient credits``, which would have half-sold
    every profitable shadow position for a reason that had nothing to do with any
    token.

    So a partial safety de-risk is downgraded to monitoring only when all of
    these hold:

    * the safety verdict is ``UNKNOWN``, never ``FAIL`` — a confirmed failure
      still exits immediately and in full,
    * the reason it is unknown is a named provider outage,
    * a sell route still exists and liquidity has not collapsed, and
    * buy flow is not reversing.

    Anything less and the original defensive plan stands.  This never touches
    STRICT PAPER: it lives in the shadow overlay and the strict entry gates are
    unchanged.
    """

    if base.reason_code != EXIT_SAFETY_EMERGENCY:
        return None
    if base.final or context.safety_status == "FAIL":
        # A confirmed hard fail is never rescued.
        return None
    if evidence.safety_confirmed_fail:
        return None
    if not evidence.safety_provider_degraded:
        return None
    if not context.route_available:
        return None

    liquidity_change = context.liquidity_change_percent
    if liquidity_change is not None and liquidity_change <= Decimal("-25"):
        return None
    ratio = context.buy_sell_ratio
    if ratio is not None and ratio < Decimal("1"):
        return None
    if health.weak:
        return None

    return ExitPlan(
        fraction=ZERO,
        reason_code=SHADOW_SAFETY_MONITOR,
        final=False,
        notes=(
            "safety provider unavailable — evidence is UNKNOWN, not FAIL",
            "route, liquidity and buy flow still healthy",
        ),
    )


def _principal_recovery_fraction(
    position: PaperPosition,
    price: Decimal,
    *,
    config: ShadowConfig,
) -> Decimal:
    """Fraction of the remainder that returns the stake still at risk.

    Uses the *remaining* cost basis rather than the original $10, because an
    earlier partial has already returned part of the stake — charging for it
    twice would over-sell the runner this rule exists to preserve.

    Bounded so a moon bag always survives: recovering principal must not become
    a disguised full exit.
    """

    value = position.tokens_remaining * price
    if value <= 0:
        return ZERO
    at_risk = position.cost_basis_remaining_usd
    if at_risk <= 0:
        return ZERO
    # Sell enough to bring back the stake still deployed, allowing for the exit leg.
    costs = leg_costs(value, config=config)
    net_ratio = (
        (value - costs.total_cost_usd) / value if value > 0 else Decimal("1")
    )
    if net_ratio <= 0:
        return ZERO
    needed = at_risk / net_ratio
    fraction = (needed / value).quantize(Decimal("0.0001"))
    ceiling = Decimal("1") - (config.moon_bag_percent / HUNDRED)
    return max(ZERO, min(fraction, ceiling))


def shadow_exit_priority(reason_code: str) -> int:
    """Rank an exit reason by urgency, for display and for tests (section 12)."""

    order = {
        "SAFETY_DETERIORATION": 0,
        "LIQUIDITY_COLLAPSE_EMERGENCY": 1,
        "HARD_LOSS_PROTECTION": 2,
        "SMART_MONEY_DISTRIBUTION": 3,
        "MOMENTUM_DECAY": 4,
        "BUY_FLOW_REVERSAL": 4,
        "VOLUME_EXHAUSTION": 5,
        "LIQUIDITY_DETERIORATION": 5,
        "CONCENTRATION_DETERIORATION": 5,
        SHADOW_SECURE_OBJECTIVE: 6,
        "TRAILING_PROFIT_PROTECTION": 7,
        "BREAK_EVEN_PROTECTION": 7,
        EXIT_MILESTONE: 8,
        SHADOW_PRINCIPAL_RECOVERY: 8,
        SHADOW_RUNNER_PARTIAL: 9,
        SHADOW_STALE_OBSERVATION: 9,
        SHADOW_SAFETY_MONITOR: 12,
        SHADOW_SOFT_PAUSE_HOLD: 12,
        "TIME_STOP": 10,
        EXIT_MOON_BAG: 11,
        HOLD_SHADOW_RUNNER: 12,
        HOLD_NO_TRIGGER: 13,
    }
    return order.get(reason_code, 14)
