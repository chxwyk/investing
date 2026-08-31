"""PUMP_TRENCH_SCORE — grading a pre-graduation candidate on its own terms.

This is deliberately a *separate* score from the Trending edge score (section 29),
because the two answer different questions.  Trending asks "is this attention
still tradeable?"; a Trenches candidate usually has no attention yet, and the
question is "is the early participation in this token real?"

Which is why the heaviest weights here are on **participation quality** rather
than on price: buyer independence, holder distribution, dev behaviour, bundle
exposure and fresh-wallet quality.  A token can be up 300% on the curve and score
badly, because 300% bought by nine wallets funded from one source is not a
finding — it is one person's spending.

Same anti-cliff discipline as v2.42: every component is a continuous ramp, and
everything is bounded and auditable.  A hard safety failure zeroes the score
outright and clears every reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..trending.score import ScoreComponent, ramp
from .bundles import (
    BUNDLE_RISK_HIGH,
    BUNDLE_RISK_LOW,
    BUNDLE_RISK_MODERATE,
    BUNDLE_RISK_NONE,
    BundleProfile,
)
from .dev import DevProfile
from .holders import ConcentrationTrend, HolderSnapshot
from .lifecycle import (
    STAGE_ALMOST_BONDED,
    STAGE_EARLY_CURVE,
    STAGE_GRADUATING,
    STAGE_NEW,
    LifecycleState,
)
from .participants import ParticipantProfile
from .risk import RiskProfile
from .timeframes import (
    SHAPE_SUSTAINED_TREND,
    SHAPE_VERY_EARLY_ACCELERATION,
    DepthProfile,
    TimeframeProfile,
)

ZERO = Decimal("0")
HUNDRED = Decimal("100")

# --- named reasons (sections 35, 37) -----------------------------------------
REASON_BUYER_ACCELERATION = "BUYER_ACCELERATION"
REASON_INDEPENDENT_DEMAND = "INDEPENDENT_DEMAND"
REASON_HEALTHY_DISTRIBUTION = "HEALTHY_DISTRIBUTION"
REASON_BONDING_MOMENTUM = "BONDING_MOMENTUM"
REASON_MARKET_ACCELERATION = "MARKET_ACCELERATION"
REASON_LIQUIDITY_QUALITY = "LIQUIDITY_QUALITY"
REASON_HOLDER_EXPANSION = "HOLDER_EXPANSION"
REASON_CLEAN_DEV = "CLEAN_DEV_PROFILE"
REASON_STORY = "STORY"
REASON_THESIS = "THESIS"
REASON_SMART_MONEY = "SMART_MONEY"
REASON_CONFLUENCE = "CONFLUENCE"

TRENCH_REASONS: tuple[str, ...] = (
    REASON_BUYER_ACCELERATION,
    REASON_INDEPENDENT_DEMAND,
    REASON_HEALTHY_DISTRIBUTION,
    REASON_BONDING_MOMENTUM,
    REASON_MARKET_ACCELERATION,
    REASON_LIQUIDITY_QUALITY,
    REASON_HOLDER_EXPANSION,
    REASON_CLEAN_DEV,
    REASON_STORY,
    REASON_THESIS,
    REASON_SMART_MONEY,
    REASON_CONFLUENCE,
)


@dataclass(frozen=True, slots=True)
class TrenchWeights:
    """Participation quality outweighs price, on purpose."""

    independent_demand: Decimal = Decimal("20")
    buyer_acceleration: Decimal = Decimal("14")
    holder_distribution: Decimal = Decimal("14")
    market_acceleration: Decimal = Decimal("12")
    liquidity: Decimal = Decimal("10")
    bonding_progress: Decimal = Decimal("8")
    dev_quality: Decimal = Decimal("8")
    story: Decimal = Decimal("6")
    thesis: Decimal = Decimal("6")
    smart_money: Decimal = Decimal("8")
    # Penalties.
    bundle_penalty: Decimal = Decimal("22")
    cluster_penalty: Decimal = Decimal("18")
    dev_penalty: Decimal = Decimal("18")
    concentration_penalty: Decimal = Decimal("14")
    thin_liquidity_penalty: Decimal = Decimal("12")


DEFAULT_TRENCH_WEIGHTS = TrenchWeights()


@dataclass(frozen=True, slots=True)
class TrenchScore:
    """A bounded 0-100 score with a fully printable derivation."""

    mint: str
    score: Decimal = ZERO
    components: tuple[ScoreComponent, ...] = ()
    reasons: tuple[str, ...] = ()
    stage: str = ""
    actionable: bool = True

    @property
    def has_named_reason(self) -> bool:
        return bool(self.reasons)

    def breakdown_lines(self) -> tuple[str, ...]:
        return tuple(
            f"{item.name}: {item.points}/{item.maximum}"
            + (f" — {item.detail}" if item.detail else "")
            for item in self.components
            if item.points != ZERO
        )

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "score": str(self.score),
            "components": [item.to_json() for item in self.components],
            "reasons": list(self.reasons),
            "stage": self.stage,
            "actionable": self.actionable,
        }


def score_pump_trench(
    mint: str,
    *,
    lifecycle: LifecycleState,
    participants: ParticipantProfile | None = None,
    timeframes: TimeframeProfile | None = None,
    depth: DepthProfile | None = None,
    holders: HolderSnapshot | None = None,
    concentration: ConcentrationTrend | None = None,
    dev: DevProfile | None = None,
    bundles: BundleProfile | None = None,
    risk: RiskProfile | None = None,
    story_verified: bool = False,
    thesis_supported: bool = False,
    proven_wallets: int = 0,
    smart_money_accumulating: bool = False,
    weights: TrenchWeights = DEFAULT_TRENCH_WEIGHTS,
) -> TrenchScore:
    """Grade a Trenches candidate on the quality of its early participation."""

    components: list[ScoreComponent] = []
    reasons: list[str] = []

    # --- independent demand: the single heaviest input --------------------
    if participants is not None and participants.unique_buyers > 0:
        ratio = participants.independence_ratio or ZERO
        points = ramp(
            Decimal(participants.independent_buyers),
            floor=Decimal("5"),
            target=Decimal("120"),
            weight=weights.independent_demand,
        )
        # Scale by how independent those actors actually are, so 200 buyers that
        # collapse to 6 actors cannot earn the full weight.
        points = (points * ratio).quantize(Decimal("0.01"))
        components.append(
            ScoreComponent(
                "independent_demand",
                points,
                weights.independent_demand,
                f"{participants.independent_buyers} actors from "
                f"{participants.unique_buyers} wallets (ratio {ratio})",
            )
        )
        if participants.organic:
            reasons.append(REASON_INDEPENDENT_DEMAND)
    else:
        components.append(
            ScoreComponent("independent_demand", ZERO, weights.independent_demand, "unknown")
        )

    # --- buyer acceleration ------------------------------------------------
    short = timeframes.window("1m") if timeframes else None
    medium = timeframes.window("5m") if timeframes else None
    buyer_window = short or medium
    if buyer_window is not None and buyer_window.independent_buyers is not None:
        points = ramp(
            Decimal(buyer_window.independent_buyers),
            floor=Decimal("2"),
            target=Decimal("40"),
            weight=weights.buyer_acceleration,
        )
        components.append(
            ScoreComponent(
                "buyer_acceleration",
                points,
                weights.buyer_acceleration,
                f"{buyer_window.independent_buyers} independent buyers in "
                f"{buyer_window.timeframe}",
            )
        )
        if points >= weights.buyer_acceleration * Decimal("0.4"):
            reasons.append(REASON_BUYER_ACCELERATION)
    elif buyer_window is not None and buyer_window.buys:
        # Raw buys earn a *capped fraction* only: activity is not demand.
        points = ramp(
            Decimal(buyer_window.buys),
            floor=Decimal("5"),
            target=Decimal("200"),
            weight=weights.buyer_acceleration * Decimal("0.4"),
        )
        components.append(
            ScoreComponent(
                "buyer_acceleration",
                points,
                weights.buyer_acceleration,
                f"{buyer_window.buys} raw buys, independence unknown — capped",
            )
        )
    else:
        components.append(
            ScoreComponent("buyer_acceleration", ZERO, weights.buyer_acceleration, "unknown")
        )

    # --- holder distribution ----------------------------------------------
    if holders is not None and holders.top10_percent is not None:
        # Lower concentration earns more; this is an inverted ramp.
        points = ramp(
            HUNDRED - holders.top10_percent,
            floor=Decimal("40"),
            target=Decimal("85"),
            weight=weights.holder_distribution,
        )
        detail = f"top 10 {holders.top10_percent}%"
        if concentration is not None and concentration.state == "IMPROVING":
            points = min(weights.holder_distribution, points * Decimal("1.2"))
            detail += ", broadening"
            reasons.append(REASON_HEALTHY_DISTRIBUTION)
        elif holders.top10_percent <= Decimal("30"):
            reasons.append(REASON_HEALTHY_DISTRIBUTION)
        components.append(
            ScoreComponent(
                "holder_distribution", points.quantize(Decimal("0.01")),
                weights.holder_distribution, detail,
            )
        )
    else:
        components.append(
            ScoreComponent("holder_distribution", ZERO, weights.holder_distribution, "unknown")
        )

    # --- market acceleration ----------------------------------------------
    if timeframes is not None and timeframes.usable_windows:
        velocity = None
        for candidate in (timeframes.window("1m"), timeframes.window("5m")):
            if candidate is not None and candidate.market_cap_velocity is not None:
                velocity = candidate.market_cap_velocity
                break
        points = ramp(
            velocity,
            floor=ZERO,
            target=Decimal("6"),
            weight=weights.market_acceleration,
        )
        components.append(
            ScoreComponent(
                "market_acceleration",
                points,
                weights.market_acceleration,
                timeframes.headline(),
            )
        )
        if timeframes.shape in {SHAPE_VERY_EARLY_ACCELERATION, SHAPE_SUSTAINED_TREND}:
            reasons.append(REASON_MARKET_ACCELERATION)
    else:
        components.append(
            ScoreComponent("market_acceleration", ZERO, weights.market_acceleration, "unknown")
        )

    # --- liquidity, judged against the stage ------------------------------
    if depth is not None and depth.liquidity_usd is not None:
        # A brand-new curve legitimately has little liquidity; the bar rises with
        # the stage rather than punishing a token for being early.
        target = (
            Decimal("8000")
            if lifecycle.stage in {STAGE_NEW, STAGE_EARLY_CURVE}
            else Decimal("40000")
        )
        points = ramp(
            depth.liquidity_usd, floor=Decimal("500"), target=target, weight=weights.liquidity
        )
        components.append(
            ScoreComponent(
                "liquidity", points, weights.liquidity, f"${depth.liquidity_usd:,.0f}"
            )
        )
        if points >= weights.liquidity * Decimal("0.6") and not depth.thin:
            reasons.append(REASON_LIQUIDITY_QUALITY)
    else:
        components.append(ScoreComponent("liquidity", ZERO, weights.liquidity, "unknown"))

    # --- bonding progress: a feature, never a buy signal (section 8) ------
    if lifecycle.progress_percent is not None:
        points = ramp(
            lifecycle.progress_percent,
            floor=Decimal("10"),
            target=Decimal("90"),
            weight=weights.bonding_progress,
        )
        components.append(
            ScoreComponent(
                "bonding_progress",
                points,
                weights.bonding_progress,
                f"{lifecycle.progress_percent}% ({lifecycle.label})",
            )
        )
        if lifecycle.stage in {STAGE_ALMOST_BONDED, STAGE_GRADUATING}:
            reasons.append(REASON_BONDING_MOMENTUM)
    else:
        components.append(
            ScoreComponent("bonding_progress", ZERO, weights.bonding_progress, "unknown")
        )

    # --- dev quality --------------------------------------------------------
    if dev is not None and dev.wallet:
        if dev.concerns:
            points = ZERO
        else:
            points = weights.dev_quality
            reasons.append(REASON_CLEAN_DEV)
        components.append(
            ScoreComponent(
                "dev_quality",
                points,
                weights.dev_quality,
                "; ".join(dev.concerns) if dev.concerns else "no adverse creator evidence",
            )
        )
    else:
        components.append(ScoreComponent("dev_quality", ZERO, weights.dev_quality, "unknown"))

    # --- soft evidence ------------------------------------------------------
    if story_verified:
        components.append(
            ScoreComponent("story", weights.story, weights.story, "corroborated for this mint")
        )
        reasons.append(REASON_STORY)
    else:
        components.append(ScoreComponent("story", ZERO, weights.story, "none or unverified"))

    if thesis_supported:
        components.append(
            ScoreComponent("thesis", weights.thesis, weights.thesis, "externally supported")
        )
        reasons.append(REASON_THESIS)
    else:
        components.append(ScoreComponent("thesis", ZERO, weights.thesis, "none or unsupported"))

    if proven_wallets > 0 and smart_money_accumulating:
        points = ramp(
            Decimal(proven_wallets), floor=ZERO, target=Decimal("3"), weight=weights.smart_money
        )
        components.append(
            ScoreComponent(
                "smart_money", points, weights.smart_money, f"{proven_wallets} wallet(s)"
            )
        )
        reasons.append(REASON_SMART_MONEY)
    else:
        components.append(ScoreComponent("smart_money", ZERO, weights.smart_money, "none"))

    total = sum((item.points for item in components), ZERO)

    # --- penalties ----------------------------------------------------------
    def penalise(name: str, points: Decimal, maximum: Decimal, detail: str) -> None:
        nonlocal total
        if points <= ZERO:
            return
        total -= points
        components.append(ScoreComponent(name, -points, maximum, detail))

    if bundles is not None:
        bundle_points = {
            BUNDLE_RISK_HIGH: weights.bundle_penalty,
            BUNDLE_RISK_MODERATE: weights.bundle_penalty * Decimal("0.5"),
            BUNDLE_RISK_LOW: weights.bundle_penalty * Decimal("0.15"),
            BUNDLE_RISK_NONE: ZERO,
        }.get(bundles.risk, ZERO)
        if bundles.distributing:
            bundle_points = weights.bundle_penalty
        penalise(
            "bundle_penalty", bundle_points, weights.bundle_penalty, bundles.operator_line()
        )

    if participants is not None:
        clustered = participants.clustered_percent
        penalise(
            "cluster_penalty",
            ramp(
                clustered,
                floor=Decimal("20"),
                target=Decimal("80"),
                weight=weights.cluster_penalty,
            ),
            weights.cluster_penalty,
            f"{clustered}% of demand from coordinated wallets" if clustered else "",
        )

    if dev is not None and dev.concerns:
        penalise(
            "dev_penalty",
            weights.dev_penalty if dev.holding.selling else weights.dev_penalty * Decimal("0.5"),
            weights.dev_penalty,
            "; ".join(dev.concerns),
        )

    if holders is not None and holders.top10_percent is not None:
        penalise(
            "concentration_penalty",
            ramp(
                holders.top10_percent,
                floor=Decimal("45"),
                target=Decimal("90"),
                weight=weights.concentration_penalty,
            ),
            weights.concentration_penalty,
            f"top 10 {holders.top10_percent}%",
        )

    if depth is not None and depth.thin:
        penalise(
            "thin_liquidity_penalty",
            weights.thin_liquidity_penalty,
            weights.thin_liquidity_penalty,
            f"liquidity is {depth.liquidity_to_market_cap:.2%} of market cap",
        )

    # Section 28: a documented special mode changes how the token behaves, so it
    # is never scored as an ordinary one.
    if lifecycle.special_mode:
        total = min(total, Decimal("40"))
        components.append(
            ScoreComponent(
                "special_mode_cap",
                ZERO,
                ZERO,
                f"{lifecycle.special_mode} mode — capped pending understanding of the state",
            )
        )

    families = {
        reason
        for reason in reasons
        if reason
        in {
            REASON_INDEPENDENT_DEMAND,
            REASON_HEALTHY_DISTRIBUTION,
            REASON_STORY,
            REASON_THESIS,
            REASON_SMART_MONEY,
            REASON_MARKET_ACCELERATION,
        }
    }
    if len(families) >= 3:
        reasons.append(REASON_CONFLUENCE)

    if risk is not None and risk.blocked:
        total = ZERO
        reasons.clear()
        components.append(
            ScoreComponent(
                "hard_safety_block", ZERO, ZERO, ", ".join(risk.hard_failures)
            )
        )

    return TrenchScore(
        mint=mint,
        score=max(ZERO, min(HUNDRED, total)).quantize(Decimal("0.1")),
        components=tuple(components),
        reasons=tuple(dict.fromkeys(reasons)),
        stage=lifecycle.stage,
        actionable=not (risk is not None and risk.blocked),
    )
