"""Alert tiers, cadence tiers, and the events that skip the timer.

Section 36 states the product requirement plainly: *I should not need to stare at
Discord all day.*  Radar is broad, pings are selective, and the gap between them
is where this module lives.

Two mechanisms replace v2.42's single 45-second hot-watch cadence:

**Cadence tiers (section 42).**  A candidate seconds from graduation with buyers
arriving every block deserves a faster look than one drifting at 30% for an hour.
Both are bounded, and the fastest tier is capped in population so the cost stays
flat.

**Event-driven promotion (section 43).**  Waiting for a timer is the wrong shape
for events that are themselves the news — a large independent buy, a buyer burst,
a bonding milestone, a notable wallet, a new supported thesis.  Those recompute
the candidate immediately.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")

# --- alert tiers (section 35) -------------------------------------------------
#: Quiet radar visibility for an early Trenches candidate.
TRENCH_HEADS_UP = "TRENCH_HEADS_UP"
#: A pre-graduation candidate whose evidence earned an interruption.
TRENCH_RUNNER = "TRENCH_RUNNER"
#: Strengthening Trending candidate; radar only.
TRENDING_WATCH = "TRENDING_WATCH"
#: Trending candidate that earned an interruption.
TRENDING_ALPHA = "TRENDING_ALPHA"
#: An already-moved token with genuinely new evidence.
CONTINUATION_WATCH = "CONTINUATION_WATCH"
#: Several independent evidence families agreeing at once.
HIGH_CONFLUENCE = "HIGH_CONFLUENCE"

ALERT_TIERS: tuple[str, ...] = (
    TRENCH_HEADS_UP,
    TRENCH_RUNNER,
    TRENDING_WATCH,
    TRENDING_ALPHA,
    CONTINUATION_WATCH,
    HIGH_CONFLUENCE,
)

#: Tiers allowed to interrupt a person.  The two "watch" tiers are deliberately
#: absent — they publish to radar where they can be read on the operator's time.
PINGING_TIERS: frozenset[str] = frozenset(
    {TRENCH_RUNNER, TRENDING_ALPHA, HIGH_CONFLUENCE, CONTINUATION_WATCH}
)

TIER_LABELS: dict[str, str] = {
    TRENCH_HEADS_UP: "TRENCH HEADS-UP",
    TRENCH_RUNNER: "PUMP TRENCH RUNNER",
    TRENDING_WATCH: "TRENDING WATCH",
    TRENDING_ALPHA: "PUBLIC TRENDING",
    CONTINUATION_WATCH: "TRENDING CONTINUATION",
    HIGH_CONFLUENCE: "HIGH CONFLUENCE",
}


# --- recheck cadence tiers (section 42) --------------------------------------
CADENCE_HOT = "HOT"
CADENCE_WARM = "WARM"
CADENCE_NORMAL = "NORMAL"

CADENCE_TIERS: tuple[str, ...] = (CADENCE_HOT, CADENCE_WARM, CADENCE_NORMAL)


@dataclass(frozen=True, slots=True)
class CadenceConfig:
    """Bounded tiers.  The hot tier is capped in population, not just in speed.

    Every tier reads *cached* state — the observation history, the last curve
    read, persisted holder snapshots — so a faster cadence costs CPU rather than
    provider calls (section 71).
    """

    hot_seconds: int = 15
    warm_seconds: int = 45
    normal_seconds: int = 120
    #: How many candidates may sit in the hot tier at once.
    max_hot: int = 6
    max_warm: int = 16

    def seconds_for(self, tier: str) -> int:
        return {
            CADENCE_HOT: self.hot_seconds,
            CADENCE_WARM: self.warm_seconds,
            CADENCE_NORMAL: self.normal_seconds,
        }.get(tier, self.normal_seconds)


DEFAULT_CADENCE_CONFIG = CadenceConfig()


def cadence_tier(
    *,
    score: Decimal,
    alpha_threshold: Decimal,
    almost_bonded: bool = False,
    buyer_burst: bool = False,
    momentum_increasing: bool = False,
) -> str:
    """Pick how often this candidate should be reconsidered.

    Closeness to the decision *and* closeness to a route change both make a
    candidate time-critical; a token about to graduate can change character
    inside a minute.
    """

    gap = alpha_threshold - score
    if almost_bonded or buyer_burst or gap <= Decimal("6"):
        return CADENCE_HOT
    if momentum_increasing or gap <= Decimal("18"):
        return CADENCE_WARM
    return CADENCE_NORMAL


# --- events that skip the timer (section 43) ---------------------------------
EVENT_LARGE_BUY = "LARGE_INDEPENDENT_BUY"
EVENT_BUYER_BURST = "BUYER_BURST"
EVENT_NOTABLE_WALLET = "NOTABLE_WALLET_ENTERED"
EVENT_STORY_MATCH = "STORY_MATCH"
EVENT_NEW_THESIS = "NEW_QUALITY_THESIS"
EVENT_HOLDER_ACCELERATION = "HOLDER_ACCELERATION"
EVENT_RANK_JUMP = "RANK_JUMP"
EVENT_BONDING_MILESTONE = "BONDING_MILESTONE"
EVENT_GRADUATED = "GRADUATED"

PROMOTION_EVENTS: tuple[str, ...] = (
    EVENT_LARGE_BUY,
    EVENT_BUYER_BURST,
    EVENT_NOTABLE_WALLET,
    EVENT_STORY_MATCH,
    EVENT_NEW_THESIS,
    EVENT_HOLDER_ACCELERATION,
    EVENT_RANK_JUMP,
    EVENT_BONDING_MILESTONE,
    EVENT_GRADUATED,
)


@dataclass(frozen=True, slots=True)
class PromotionEvent:
    """Something happened that is worth recomputing a candidate for, now."""

    mint: str
    kind: str
    at: int
    detail: str = ""

    def __post_init__(self) -> None:
        if self.kind not in PROMOTION_EVENTS:
            raise ValueError(f"unknown promotion event: {self.kind}")

    def to_json(self) -> dict[str, object]:
        return {"mint": self.mint, "kind": self.kind, "at": self.at, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class TierDecision:
    """Which tier a candidate belongs in, and whether it may interrupt anyone."""

    mint: str
    tier: str
    ping: bool = False
    reasons: tuple[str, ...] = ()
    suppression: str = ""
    detail: str = ""

    @property
    def label(self) -> str:
        return TIER_LABELS.get(self.tier, self.tier)

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "tier": self.tier,
            "ping": self.ping,
            "reasons": list(self.reasons),
            "suppression": self.suppression,
            "detail": self.detail,
        }


# --- why nothing was sent (sections 36, 82) ----------------------------------
SUPPRESS_NOT_STRONG_ENOUGH = "NOT_STRONG_ENOUGH"
SUPPRESS_NO_NAMED_REASON = "NO_NAMED_SERIOUS_REASON"
SUPPRESS_HARD_SAFETY = "HARD_SAFETY_FAILURE"
SUPPRESS_RISK_GATE = "RISK_GATE"
SUPPRESS_ALREADY_ALERTED = "ALREADY_ALERTED"
SUPPRESS_RATE_LIMIT = "RATE_LIMIT"
SUPPRESS_COOLDOWN = "COOLDOWN"
SUPPRESS_CLUSTERED_DEMAND = "DEMAND_IS_CLUSTERED"
SUPPRESS_THIN_LIQUIDITY = "LIQUIDITY_TOO_THIN"
SUPPRESS_BUNDLE_RISK = "BUNDLE_RISK"
SUPPRESS_DEV_RISK = "DEV_RISK"
SUPPRESS_HOT_WATCH = "HOT_WATCH"
SUPPRESS_LATE_DISCOVERY = "LATE_DISCOVERY"
SUPPRESS_QUEUE_DELAY = "QUEUE_DELAY"
SUPPRESS_FADING = "MOMENTUM_FADING"

TRENCH_SUPPRESSIONS: tuple[str, ...] = (
    SUPPRESS_NOT_STRONG_ENOUGH,
    SUPPRESS_NO_NAMED_REASON,
    SUPPRESS_HARD_SAFETY,
    SUPPRESS_RISK_GATE,
    SUPPRESS_ALREADY_ALERTED,
    SUPPRESS_RATE_LIMIT,
    SUPPRESS_COOLDOWN,
    SUPPRESS_CLUSTERED_DEMAND,
    SUPPRESS_THIN_LIQUIDITY,
    SUPPRESS_BUNDLE_RISK,
    SUPPRESS_DEV_RISK,
    SUPPRESS_HOT_WATCH,
    SUPPRESS_LATE_DISCOVERY,
    SUPPRESS_QUEUE_DELAY,
    SUPPRESS_FADING,
)


def decide_trench_tier(
    mint: str,
    *,
    score: Decimal,
    reasons: tuple[str, ...],
    almost_bonded: bool = False,
    runner_threshold: Decimal = Decimal("62"),
    heads_up_threshold: Decimal = Decimal("38"),
    hot_watch_band: Decimal = Decimal("12"),
    risk_blocked: bool = False,
    clustered_demand: bool = False,
    thin_liquidity: bool = False,
    bundle_high: bool = False,
    dev_selling: bool = False,
    already_alerted: bool = False,
    rate_limited: bool = False,
    in_cooldown: bool = False,
    confluence: bool = False,
) -> TierDecision:
    """Decide the tier, and record *why* whenever nothing is sent.

    The risk gates below are hard: a candidate whose demand is coordinated, whose
    pool is too thin to exit, whose launch was heavily bundled or whose creator is
    selling does not ping regardless of how good its score looks.  A high score
    built on those inputs is a measurement of the wrong thing.
    """

    def build(tier: str, ping: bool, suppression: str = "", detail: str = "") -> TierDecision:
        return TierDecision(
            mint=mint,
            tier=tier,
            ping=ping,
            reasons=reasons,
            suppression=suppression,
            detail=detail,
        )

    if risk_blocked:
        return build(TRENCH_HEADS_UP, False, SUPPRESS_HARD_SAFETY, "confirmed hard failure")
    if already_alerted:
        return build(TRENCH_HEADS_UP, False, SUPPRESS_ALREADY_ALERTED, "one ping per candidate")
    if rate_limited:
        return build(TRENCH_HEADS_UP, False, SUPPRESS_RATE_LIMIT)
    if in_cooldown:
        return build(TRENCH_HEADS_UP, False, SUPPRESS_COOLDOWN)

    if score >= runner_threshold:
        if not reasons:
            return build(
                TRENCH_HEADS_UP,
                False,
                SUPPRESS_NO_NAMED_REASON,
                "a score is not a reason to interrupt anyone",
            )
        if clustered_demand:
            return build(
                TRENCH_HEADS_UP,
                False,
                SUPPRESS_CLUSTERED_DEMAND,
                "the demand behind this score is coordinated",
            )
        if thin_liquidity:
            return build(
                TRENCH_HEADS_UP, False, SUPPRESS_THIN_LIQUIDITY, "no realistic exit at this depth"
            )
        if bundle_high:
            return build(TRENCH_HEADS_UP, False, SUPPRESS_BUNDLE_RISK, "heavy launch bundling")
        if dev_selling:
            return build(TRENCH_HEADS_UP, False, SUPPRESS_DEV_RISK, "creator is distributing")
        tier = HIGH_CONFLUENCE if confluence else TRENCH_RUNNER
        return build(
            tier,
            True,
            detail=(
                "approaching graduation with healthy participation"
                if almost_bonded
                else "independent early demand with current edge"
            ),
        )

    if runner_threshold - score <= hot_watch_band:
        return build(
            TRENCH_HEADS_UP,
            False,
            SUPPRESS_HOT_WATCH,
            f"{runner_threshold - score} points short — rechecking fast",
        )

    if score >= heads_up_threshold:
        return build(TRENCH_HEADS_UP, False, SUPPRESS_NOT_STRONG_ENOUGH, "radar visibility")

    return build(TRENCH_HEADS_UP, False, SUPPRESS_NOT_STRONG_ENOUGH, "below the radar bar")
