"""The TRENDING EDGE SCORE: graded evidence, named reasons, auditable arithmetic.

This replaces the legacy opportunity score *as the primary Trending decision
input* (section 56).  The legacy score stays available as supporting context; it
was built for a different question ("is this graduated token a runner?") and it
is not the right primary lens for "is this attention still tradeable?".

The design constraint that matters most is section 43: **no threshold cliffs.**
The production failure was a candidate with ~1532 buys, ~789 sells, a 1.94
buy/sell ratio and heavy volume scoring ~50 against a 55 gate with a 2.00 organic
requirement — so it produced a silent heads-up and then ran.  1.94 and 2.00 do
not live in different universes, and a hard gate that says otherwise is wrong
about the world.

So every component here is a *continuous* ramp between a floor and a target
rather than a boolean, and strength in one dimension can compensate for a
marginal miss in another.  Everything stays bounded (each component has a
maximum), auditable (every contribution is returned with its own line) and
explainable (an alert must be able to name why it fired).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .events import (
    TRENDING_CONTINUATION,
    TRENDING_EDGE_CONSUMED,
    TRENDING_NEW_ENTRY,
    TRENDING_REENTRY,
    TrendingEvent,
)
from .holders import GROWTH_ACCELERATING, GROWTH_GROWING, HolderProfile
from .ledger import TrendingLedgerEntry
from .risk import TrendingRiskPanel
from .social import SocialVelocity
from .thesis import ThesisPanel

ZERO = Decimal("0")
HUNDRED = Decimal("100")


def ramp(
    value: Decimal | None,
    *,
    floor: Decimal,
    target: Decimal,
    weight: Decimal,
) -> Decimal:
    """Continuous 0→``weight`` credit between ``floor`` and ``target``.

    This is the anti-cliff primitive.  A value just under the target earns nearly
    the full weight instead of nothing, which is precisely the behaviour whose
    absence produced the missed runner described in the module docstring.
    """

    if value is None:
        return ZERO
    if value <= floor:
        return ZERO
    if value >= target:
        return weight
    span = target - floor
    if span <= ZERO:
        return weight
    return ((value - floor) / span * weight).quantize(Decimal("0.01"))


# --- named alert reasons (section 57) ----------------------------------------
REASON_TRENDING_ACCELERATION = "TRENDING_ACCELERATION"
REASON_NEW_ENTRY = "TRENDING_NEW_ENTRY"
REASON_STORY = "STORY"
REASON_THESIS = "THESIS"
REASON_AI_PROJECT = "AI_PROJECT"
REASON_SMART_MONEY = "SMART_MONEY"
REASON_PUBLIC_SOCIAL = "PUBLIC_SOCIAL"
REASON_HOLDER_EXPANSION = "HOLDER_EXPANSION"
REASON_CONFLUENCE = "CONFLUENCE"
REASON_MARKET_STRUCTURE = "EXCEPTIONAL_MARKET_STRUCTURE"
REASON_CONTINUATION = "TRENDING_CONTINUATION"

SERIOUS_REASONS: tuple[str, ...] = (
    REASON_TRENDING_ACCELERATION,
    REASON_NEW_ENTRY,
    REASON_STORY,
    REASON_THESIS,
    REASON_AI_PROJECT,
    REASON_SMART_MONEY,
    REASON_PUBLIC_SOCIAL,
    REASON_HOLDER_EXPANSION,
    REASON_CONFLUENCE,
    REASON_MARKET_STRUCTURE,
    REASON_CONTINUATION,
)

# --- why wasn't I pinged? (section 91) ---------------------------------------
SUPPRESS_NOT_STRONG_ENOUGH = "NOT_STRONG_ENOUGH"
SUPPRESS_HOT_WATCH = "HOT_WATCH"
SUPPRESS_EDGE_CONSUMED = "EDGE_CONSUMED"
SUPPRESS_THESIS_UNSUPPORTED = "THESIS_UNSUPPORTED"
SUPPRESS_STORY_UNVERIFIED = "STORY_UNVERIFIED"
SUPPRESS_SOCIAL_ONLY = "SOCIAL_ONLY"
SUPPRESS_MARKET_NOT_CONFIRMING = "MARKET_NOT_CONFIRMING"
SUPPRESS_LIQUIDITY_TOO_LOW = "LIQUIDITY_TOO_LOW"
SUPPRESS_HOLDER_CONCENTRATION = "HOLDER_CONCENTRATION"
SUPPRESS_CREATOR_DRIVEN = "CREATOR_DRIVEN"
SUPPRESS_RATE_LIMIT = "RATE_LIMIT"
SUPPRESS_HARD_SAFETY = "HARD_SAFETY_FAILURE"
SUPPRESS_NO_NAMED_REASON = "NO_NAMED_SERIOUS_REASON"
SUPPRESS_DUPLICATE = "ALREADY_ALERTED"
SUPPRESS_COOLDOWN = "COOLDOWN"

SUPPRESSION_REASONS: tuple[str, ...] = (
    SUPPRESS_NOT_STRONG_ENOUGH,
    SUPPRESS_HOT_WATCH,
    SUPPRESS_EDGE_CONSUMED,
    SUPPRESS_THESIS_UNSUPPORTED,
    SUPPRESS_STORY_UNVERIFIED,
    SUPPRESS_SOCIAL_ONLY,
    SUPPRESS_MARKET_NOT_CONFIRMING,
    SUPPRESS_LIQUIDITY_TOO_LOW,
    SUPPRESS_HOLDER_CONCENTRATION,
    SUPPRESS_CREATOR_DRIVEN,
    SUPPRESS_RATE_LIMIT,
    SUPPRESS_HARD_SAFETY,
    SUPPRESS_NO_NAMED_REASON,
    SUPPRESS_DUPLICATE,
    SUPPRESS_COOLDOWN,
)


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    name: str
    points: Decimal
    maximum: Decimal
    detail: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "points": str(self.points),
            "maximum": str(self.maximum),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class TrendingEdgeScore:
    """A bounded 0-100 score with a full, printable derivation."""

    mint: str
    score: Decimal = ZERO
    components: tuple[ScoreComponent, ...] = ()
    reasons: tuple[str, ...] = ()
    #: The legacy opportunity score, kept as supporting context only.
    legacy_score: Decimal | None = None
    actionable: bool = True
    edge_state: str = "EDGE_AVAILABLE"

    @property
    def has_named_reason(self) -> bool:
        """Section 57: "score 80 therefore ping" is not allowed."""

        return bool(self.reasons)

    def breakdown_lines(self) -> tuple[str, ...]:
        return tuple(
            f"{component.name}: {component.points}/{component.maximum}"
            + (f" — {component.detail}" if component.detail else "")
            for component in self.components
            if component.points > ZERO
        )

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "score": str(self.score),
            "components": [component.to_json() for component in self.components],
            "reasons": list(self.reasons),
            "legacy_score": None if self.legacy_score is None else str(self.legacy_score),
            "actionable": self.actionable,
            "edge_state": self.edge_state,
        }


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    """Every maximum is explicit so the total is auditable by inspection."""

    rank_velocity: Decimal = Decimal("18")
    new_entry: Decimal = Decimal("10")
    market_acceleration: Decimal = Decimal("14")
    holder_growth: Decimal = Decimal("14")
    liquidity: Decimal = Decimal("8")
    thesis: Decimal = Decimal("14")
    story: Decimal = Decimal("8")
    social: Decimal = Decimal("8")
    smart_money: Decimal = Decimal("12")
    concentration_penalty: Decimal = Decimal("12")
    consumed_penalty: Decimal = Decimal("25")

    def total_positive(self) -> Decimal:
        return (
            self.rank_velocity
            + self.new_entry
            + self.market_acceleration
            + self.holder_growth
            + self.liquidity
            + self.thesis
            + self.story
            + self.social
            + self.smart_money
        )


DEFAULT_WEIGHTS = ScoreWeights()


def score_trending_edge(
    entry: TrendingLedgerEntry,
    event: TrendingEvent,
    *,
    holders: HolderProfile | None = None,
    theses: ThesisPanel | None = None,
    social: SocialVelocity | None = None,
    risk: TrendingRiskPanel | None = None,
    story_verified: bool = False,
    story_present: bool = False,
    ai_project_supported: bool = False,
    proven_wallets: int = 0,
    smart_money_accumulating: bool = False,
    market_cap_velocity: Decimal | None = None,
    legacy_score: Decimal | None = None,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
) -> TrendingEdgeScore:
    """Grade a Trending candidate on continuous evidence, and name the reasons.

    Nothing here treats "trending" as a positive on its own: being on the board
    contributes zero points.  What earns credit is movement, participation and
    corroboration — and every one of those can be absent without producing a
    hard zero elsewhere, which is what stops one marginal metric from silently
    disqualifying an otherwise strong candidate.
    """

    components: list[ScoreComponent] = []
    reasons: list[str] = []

    # --- rank velocity ----------------------------------------------------
    velocity = event.rank_velocity
    if velocity is not None and velocity.climbing:
        points = ramp(
            velocity.per_minute,
            floor=Decimal("0"),
            target=Decimal("3"),
            weight=weights.rank_velocity,
        )
        # A large absolute climb over a slow poll still counts, even when the
        # per-minute rate is modest.
        points = max(
            points,
            ramp(
                Decimal(velocity.delta),
                floor=Decimal("1"),
                target=Decimal("20"),
                weight=weights.rank_velocity,
            ),
        )
        components.append(
            ScoreComponent(
                "rank_velocity",
                points,
                weights.rank_velocity,
                f"{velocity.from_rank}→{velocity.to_rank} ({velocity.delta:+d})",
            )
        )
        if points >= weights.rank_velocity * Decimal("0.5"):
            reasons.append(REASON_TRENDING_ACCELERATION)
    else:
        components.append(
            ScoreComponent("rank_velocity", ZERO, weights.rank_velocity, "flat or falling")
        )

    # --- new entry --------------------------------------------------------
    if event.state in {TRENDING_NEW_ENTRY, TRENDING_REENTRY}:
        components.append(
            ScoreComponent(
                "new_entry",
                weights.new_entry,
                weights.new_entry,
                f"{entry.seconds_trending(now=event.at)}s on the board",
            )
        )
        reasons.append(REASON_NEW_ENTRY)
    else:
        components.append(ScoreComponent("new_entry", ZERO, weights.new_entry, ""))

    # --- market acceleration ---------------------------------------------
    mc_points = ramp(
        market_cap_velocity,
        floor=Decimal("0"),
        target=Decimal("4"),
        weight=weights.market_acceleration,
    )
    components.append(
        ScoreComponent(
            "market_acceleration",
            mc_points,
            weights.market_acceleration,
            "unknown" if market_cap_velocity is None else f"{market_cap_velocity:+}%/min",
        )
    )

    # --- holder growth ----------------------------------------------------
    if holders is not None:
        holder_points = ramp(
            holders.holders_per_minute,
            floor=Decimal("0"),
            target=Decimal("5"),
            weight=weights.holder_growth,
        )
        # Genuine participant expansion is worth more than raw transaction count
        # (section 36): a good independent-buyer ratio lifts the floor.
        quality = holders.participant_quality
        if quality is not None and quality >= Decimal("0.6"):
            holder_points = max(holder_points, weights.holder_growth * Decimal("0.4"))
        components.append(
            ScoreComponent(
                "holder_growth",
                holder_points,
                weights.holder_growth,
                f"{holders.growth_state}"
                + (f", +{holders.holders_added}" if holders.holders_added is not None else ""),
            )
        )
        if (
            holders.growth_state in {GROWTH_GROWING, GROWTH_ACCELERATING}
            and holders.genuinely_expanding
        ):
            reasons.append(REASON_HOLDER_EXPANSION)
    else:
        components.append(ScoreComponent("holder_growth", ZERO, weights.holder_growth, "unknown"))

    # --- liquidity --------------------------------------------------------
    liquidity = entry.liquidity_usd if risk is None else risk.liquidity_usd
    liquidity_points = ramp(
        liquidity,
        floor=Decimal("5000"),
        target=Decimal("60000"),
        weight=weights.liquidity,
    )
    components.append(
        ScoreComponent(
            "liquidity",
            liquidity_points,
            weights.liquidity,
            "unknown" if liquidity is None else f"${liquidity:,.0f}",
        )
    )
    if liquidity_points >= weights.liquidity * Decimal("0.75") and mc_points > ZERO:
        reasons.append(REASON_MARKET_STRUCTURE)

    # --- thesis -----------------------------------------------------------
    if theses is not None and theses.total:
        thesis_points = ZERO
        if theses.has_serious_thesis:
            thesis_points = weights.thesis * Decimal("0.7")
            reasons.append(REASON_THESIS)
        if theses.independent_sources >= 2 and theses.supported >= 2:
            thesis_points = weights.thesis
        elif theses.speculative:
            thesis_points = max(thesis_points, weights.thesis * Decimal("0.2"))
        components.append(
            ScoreComponent("thesis", thesis_points, weights.thesis, theses.summary_line())
        )
    else:
        components.append(ScoreComponent("thesis", ZERO, weights.thesis, "no theses"))

    # --- story / project --------------------------------------------------
    story_points = ZERO
    story_detail = "none"
    if ai_project_supported:
        story_points = weights.story
        story_detail = "project publishes this exact mint"
        reasons.append(REASON_AI_PROJECT)
    elif story_verified:
        story_points = weights.story * Decimal("0.75")
        story_detail = "story corroborated"
        reasons.append(REASON_STORY)
    elif story_present:
        story_points = weights.story * Decimal("0.25")
        story_detail = "story present, unverified"
    components.append(ScoreComponent("story", story_points, weights.story, story_detail))

    # --- public social ----------------------------------------------------
    if social is not None and social.mentions:
        social_points = ramp(
            Decimal(social.independent_authors),
            floor=Decimal("1"),
            target=Decimal("8"),
            weight=weights.social,
        )
        components.append(
            ScoreComponent(
                "public_social",
                social_points,
                weights.social,
                f"{social.independent_authors} independent authors",
            )
        )
        if social.accelerating:
            reasons.append(REASON_PUBLIC_SOCIAL)
    else:
        components.append(ScoreComponent("public_social", ZERO, weights.social, "no mentions"))

    # --- smart money ------------------------------------------------------
    if proven_wallets > 0 and smart_money_accumulating:
        wallet_points = ramp(
            Decimal(proven_wallets),
            floor=Decimal("0"),
            target=Decimal("3"),
            weight=weights.smart_money,
        )
        components.append(
            ScoreComponent(
                "smart_money",
                wallet_points,
                weights.smart_money,
                f"{proven_wallets} proven wallets",
            )
        )
        reasons.append(REASON_SMART_MONEY)
    else:
        components.append(
            ScoreComponent(
                "smart_money",
                ZERO,
                weights.smart_money,
                "distributing" if proven_wallets and not smart_money_accumulating else "none",
            )
        )

    total = sum((component.points for component in components), ZERO)

    # --- penalties --------------------------------------------------------
    if holders is not None and holders.top10_percent is not None:
        penalty = ramp(
            holders.top10_percent,
            floor=Decimal("45"),
            target=Decimal("85"),
            weight=weights.concentration_penalty,
        )
        if penalty > ZERO:
            total -= penalty
            components.append(
                ScoreComponent(
                    "concentration_penalty",
                    -penalty,
                    weights.concentration_penalty,
                    f"top 10 {holders.top10_percent:.1f}%",
                )
            )

    edge_state = "EDGE_AVAILABLE"
    move = event.move_since_entry_percent
    if event.state == TRENDING_EDGE_CONSUMED:
        edge_state = "EDGE_CONSUMED"
        total -= weights.consumed_penalty
        components.append(
            ScoreComponent(
                "edge_consumed_penalty",
                -weights.consumed_penalty,
                weights.consumed_penalty,
                "the early move has already happened",
            )
        )
    elif move is not None and move >= Decimal("35"):
        edge_state = "EDGE_NARROWING"

    if event.state == TRENDING_CONTINUATION:
        reasons.append(REASON_CONTINUATION)

    # Confluence is earned by *independent* evidence families agreeing, which is
    # why it is derived from the reason set rather than asserted separately.
    families = {
        reason
        for reason in reasons
        if reason
        in {
            REASON_THESIS,
            REASON_STORY,
            REASON_AI_PROJECT,
            REASON_SMART_MONEY,
            REASON_PUBLIC_SOCIAL,
            REASON_HOLDER_EXPANSION,
        }
    }
    if len(families) >= 3:
        reasons.append(REASON_CONFLUENCE)

    if risk is not None and risk.blocked:
        # A hard failure zeroes the score outright; no amount of attention
        # survives a confirmed sell failure or a collapsed pool (section 71).
        total = ZERO
        reasons.clear()
        components.append(
            ScoreComponent(
                "hard_safety_block",
                ZERO,
                ZERO,
                ", ".join(risk.hard_failures) or "safety FAIL",
            )
        )

    bounded = max(ZERO, min(HUNDRED, total)).quantize(Decimal("0.1"))
    ordered_reasons = tuple(dict.fromkeys(reasons))

    return TrendingEdgeScore(
        mint=entry.mint,
        score=bounded,
        components=tuple(components),
        reasons=ordered_reasons,
        legacy_score=legacy_score,
        actionable=(
            edge_state != "EDGE_CONSUMED"
            and not (risk is not None and risk.blocked)
        ),
        edge_state=edge_state,
    )


@dataclass(frozen=True, slots=True)
class AlertVerdict:
    """Whether to interrupt a human, and the structured reason either way."""

    mint: str
    alert: bool
    tier: str
    reasons: tuple[str, ...] = ()
    suppression: str = ""
    score: Decimal = ZERO
    detail: str = ""
    hot_watch_candidate: bool = False
    near_miss_gap: Decimal = ZERO
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "alert": self.alert,
            "tier": self.tier,
            "reasons": list(self.reasons),
            "suppression": self.suppression,
            "score": str(self.score),
            "detail": self.detail,
            "hot_watch_candidate": self.hot_watch_candidate,
            "near_miss_gap": str(self.near_miss_gap),
            "notes": list(self.notes),
        }


# --- the three visibility tiers (section 58) ---------------------------------
TIER_RADAR = "TRENDING_RADAR"
TIER_WATCH = "TRENDING_WATCH"
TIER_ALPHA = "TRENDING_ALPHA"

TRENDING_TIERS: tuple[str, ...] = (TIER_RADAR, TIER_WATCH, TIER_ALPHA)


def decide_alert(
    score: TrendingEdgeScore,
    event: TrendingEvent,
    *,
    alpha_threshold: Decimal = Decimal("62"),
    watch_threshold: Decimal = Decimal("40"),
    hot_watch_band: Decimal = Decimal("12"),
    risk: TrendingRiskPanel | None = None,
    already_alerted: bool = False,
    rate_limited: bool = False,
    in_cooldown: bool = False,
) -> AlertVerdict:
    """Decide radar / watch / alpha, and record *why* when nothing is sent.

    The near-miss band is the APEUS-class fix (sections 42-44): a candidate
    within ``hot_watch_band`` of the alpha threshold is not discarded for another
    full recheck cycle — it is marked as a HOT WATCH candidate so the caller can
    re-examine it aggressively over a short bounded window.
    """

    gap = max(ZERO, alpha_threshold - score.score)
    near_miss = ZERO < gap <= hot_watch_band

    def build(
        alert: bool,
        tier: str,
        suppression: str = "",
        detail: str = "",
        hot_watch: bool = False,
    ) -> AlertVerdict:
        return AlertVerdict(
            mint=score.mint,
            alert=alert,
            tier=tier,
            reasons=score.reasons,
            suppression=suppression,
            score=score.score,
            detail=detail,
            hot_watch_candidate=hot_watch,
            near_miss_gap=gap,
        )

    if risk is not None and risk.blocked:
        return build(
            False,
            TIER_RADAR,
            SUPPRESS_HARD_SAFETY,
            ", ".join(risk.hard_failures) or "safety FAIL",
        )
    if already_alerted:
        return build(False, TIER_WATCH, SUPPRESS_DUPLICATE, "one escalation ping per candidate")
    if rate_limited:
        return build(False, TIER_WATCH, SUPPRESS_RATE_LIMIT, "", hot_watch=near_miss)
    if in_cooldown:
        return build(False, TIER_WATCH, SUPPRESS_COOLDOWN, "", hot_watch=near_miss)

    if score.edge_state == "EDGE_CONSUMED" and event.state != TRENDING_CONTINUATION:
        return build(
            False,
            TIER_WATCH,
            SUPPRESS_EDGE_CONSUMED,
            "the move is gone; a continuation needs new evidence",
        )

    if score.score >= alpha_threshold:
        if not score.has_named_reason:
            # Section 57 in one branch: a number is never a reason.
            return build(
                False,
                TIER_WATCH,
                SUPPRESS_NO_NAMED_REASON,
                "score alone is not a reason to interrupt anyone",
                hot_watch=True,
            )
        social_only = set(score.reasons) <= {REASON_PUBLIC_SOCIAL}
        if social_only:
            return build(
                False,
                TIER_WATCH,
                SUPPRESS_SOCIAL_ONLY,
                "chatter without market confirmation",
                hot_watch=True,
            )
        return build(True, TIER_ALPHA, detail="serious evidence, still actionable")

    if near_miss:
        return build(
            False,
            TIER_WATCH,
            SUPPRESS_HOT_WATCH,
            f"{gap} points below the alpha threshold — reevaluating quickly",
            hot_watch=True,
        )

    if score.score >= watch_threshold:
        return build(False, TIER_WATCH, SUPPRESS_NOT_STRONG_ENOUGH, "strengthening, not there yet")

    return build(False, TIER_RADAR, SUPPRESS_NOT_STRONG_ENOUGH, "radar visibility only")
