"""Ultra-early operator visibility from cheap evidence (sections 1-17, 43-52).

The product failure this module exists to fix, stated exactly:

    The bot recorded Grok Pocket at a ~$31K market cap.  The operator did not
    get useful visibility until ~$61K.  "First seen $31.18K" was printed on the
    card as historical trivia next to a +97% move that had already happened.

The cause is architectural, not a threshold.  Every operator-visible alert sat
behind ``analyze_runner`` — deep enrichment with a 30-second budget, gathered
across a whole batch, so the slowest mint in the batch delayed all of them, and
a mint that did not clear the bar on its first pass was not looked at again for
the full recheck window.  By then the move was gone.

So this module is deliberately *cheap*.  Everything it reads comes from one DEX
snapshot the pipeline already fetches, plus timestamps already persisted.  It
touches no wallet forensics, no Solana Tracker, no social lookup and no safety
provider, because first visibility must never wait on any of them (sections 4,
49).  Safety is reported honestly as ``UNKNOWN``; it is never implied to be PASS.

The second half of the job is restraint.  Being early is worthless if the
channel fills with noise, so a tier that pings requires a *named serious
evidence category* (section 15), a large buy must be corroborated by independent
follow-on demand before it counts as demand (section 8), and an alert that
arrives after the move is labelled ``EDGE_CONSUMED`` rather than dressed up as
early (sections 10, 47).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal("0")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")

# --- operator visibility tiers (section 5) -----------------------------------
#: Nothing worth the operator's attention yet.
TIER_NONE = "NONE"
#: "This is beginning to move.  Watch."  Radar only; no ping by default.
TIER_EARLY_HEADS_UP = "EARLY_HEADS_UP"
#: "Serious early evidence — look now."  May ping.
TIER_EARLY_RUNNER = "EARLY_RUNNER"
#: An EARLY_RUNNER whose case is pure market structure, with no story or wallet.
TIER_ORGANIC_RUNNER = "ORGANIC_RUNNER"

TIERS: tuple[str, ...] = (
    TIER_NONE,
    TIER_EARLY_HEADS_UP,
    TIER_EARLY_RUNNER,
    TIER_ORGANIC_RUNNER,
)

#: Tiers that are allowed to interrupt a person, before any other gate.
PINGABLE_TIERS: frozenset[str] = frozenset({TIER_EARLY_RUNNER, TIER_ORGANIC_RUNNER})

# --- edge state (sections 10, 11, 47) ----------------------------------------
EDGE_AVAILABLE = "EDGE_AVAILABLE"
EDGE_NARROWING = "EDGE_NARROWING"
EDGE_CONSUMED = "EDGE_CONSUMED"
MOVE_EXTENDED = "MOVE_ALREADY_EXTENDED"

#: Above this much move since the bot first saw the token, an alert can no
#: longer honestly call itself early.
EDGE_NARROWING_PERCENT = Decimal("35")
EDGE_CONSUMED_PERCENT = Decimal("80")
MOVE_EXTENDED_PERCENT = Decimal("150")

# --- the operator-visibility timeline (section 2) ----------------------------
#: Every one of these is persisted with its timestamp *and* the market cap at
#: that moment, write-once.  The question the product has to be able to answer
#: is "what did the bot know, when did it know it, and when did the human see
#: it?" — which is unanswerable unless each stage keeps its own numbers.
STAGE_SOURCE_CREATED = "SOURCE_CREATED"
STAGE_BOT_FIRST_SEEN = "BOT_FIRST_SEEN"
STAGE_CHEAP_SIGNAL = "CHEAP_SIGNAL_TRIGGER"
STAGE_OPERATOR_HEADS_UP = "OPERATOR_HEADS_UP_SENT"
STAGE_EARLY_RUNNER = "EARLY_RUNNER_TRIGGER"
STAGE_URGENT_PING = "URGENT_PING_SENT"
STAGE_DEEP_ENRICHMENT = "DEEP_ENRICHMENT_COMPLETE"
STAGE_QUALIFIED_RESEARCH = "QUALIFIED_RESEARCH"
STAGE_SHADOW_DECISION = "SHADOW_DECISION"
STAGE_SHADOW_FILL = "SHADOW_FILL"

TIMELINE_STAGES: tuple[str, ...] = (
    STAGE_SOURCE_CREATED,
    STAGE_BOT_FIRST_SEEN,
    STAGE_CHEAP_SIGNAL,
    STAGE_OPERATOR_HEADS_UP,
    STAGE_EARLY_RUNNER,
    STAGE_URGENT_PING,
    STAGE_DEEP_ENRICHMENT,
    STAGE_QUALIFIED_RESEARCH,
    STAGE_SHADOW_DECISION,
    STAGE_SHADOW_FILL,
)

#: The stages that mean a human actually saw something.
OPERATOR_VISIBLE_STAGES: frozenset[str] = frozenset(
    {STAGE_OPERATOR_HEADS_UP, STAGE_EARLY_RUNNER, STAGE_URGENT_PING}
)


# --- serious evidence categories (section 15) --------------------------------
EV_ORGANIC = "ORGANIC_MARKET_EVIDENCE"
EV_STORY = "STORY_NARRATIVE"
EV_WALLET = "NOTABLE_PROVEN_WALLET"
EV_CATALYST = "CATALYST_EVENT"
EV_CONFLUENCE = "MULTI_SOURCE_CONFLUENCE"
EV_STRUCTURE = "EXCEPTIONAL_MARKET_STRUCTURE"

SERIOUS_EVIDENCE: tuple[str, ...] = (
    EV_ORGANIC,
    EV_STORY,
    EV_WALLET,
    EV_CATALYST,
    EV_CONFLUENCE,
    EV_STRUCTURE,
)

# --- large-buy quality (section 8) -------------------------------------------
BUY_NONE = "NO_LARGE_BUY"
BUY_UNCONFIRMED = "LARGE_BUY_UNCONFIRMED"
BUY_INSIDER = "LARGE_BUY_CREATOR_LINKED"
BUY_CONFIRMED = "LARGE_BUY_WITH_FOLLOW_ON_DEMAND"

BUY_QUALITIES: tuple[str, ...] = (
    BUY_NONE,
    BUY_UNCONFIRMED,
    BUY_INSIDER,
    BUY_CONFIRMED,
)

#: A buy worth this share of a token's liquidity moves the market by itself.
IMPULSE_LIQUIDITY_SHARE_PERCENT = Decimal("5")
#: ...or this many times the recent average trade.
IMPULSE_TRADE_MULTIPLE = Decimal("8")
#: ...or produces a market-cap jump this large in the short window.
IMPULSE_MARKET_CAP_JUMP_PERCENT = Decimal("8")
#: Independent buyers that must follow before an impulse counts as demand.
IMPULSE_MIN_FOLLOW_ON_BUYERS = 8

# --- why the operator was not pinged (section 12) ----------------------------
WHY_NO_EARLY_SIGNAL = "NO_EARLY_SIGNAL"
WHY_NOT_SERIOUS = "NO_SERIOUS_EVIDENCE_CATEGORY"
WHY_MOVE_CONSUMED = "MOVE_CONSUMED_BEFORE_GATE"
WHY_LIQUIDITY = "LIQUIDITY_TOO_LOW"
WHY_ROUTE = "NO_USABLE_ROUTE"
WHY_RUGGED = "RUG_EVIDENCE"
WHY_TOO_OLD = "TOKEN_NOT_FRESH"
WHY_MC_OUT_OF_RANGE = "MARKET_CAP_OUTSIDE_EARLY_RANGE"
WHY_INSIDER_ONLY = "LARGE_BUY_WAS_CREATOR_LINKED"
WHY_NO_DATA = "NO_CHEAP_MARKET_DATA"
WHY_DUPLICATE = "DUPLICATE_SUPPRESSED"
WHY_RATE_LIMITED = "RATE_LIMITED"
WHY_COOLDOWN = "TIER_COOLDOWN_ACTIVE"
WHY_DISABLED = "EARLY_LANE_DISABLED"

HUMAN_WHY: dict[str, str] = {
    WHY_NO_EARLY_SIGNAL: "Nothing in the cheap evidence was moving yet",
    WHY_NOT_SERIOUS: (
        "No serious evidence category was present — a score alone is not a reason "
        "to interrupt anyone"
    ),
    WHY_MOVE_CONSUMED: "The move was already spent by the time the gate was reached",
    WHY_LIQUIDITY: "Liquidity was below the floor a $10 trade needs",
    WHY_ROUTE: "No usable route existed",
    WHY_RUGGED: "Rug evidence was already present",
    WHY_TOO_OLD: "The token was past the early window",
    WHY_MC_OUT_OF_RANGE: "Market cap was outside the early-alpha range",
    WHY_INSIDER_ONLY: "The only large buy looked creator-linked, with no independent demand",
    WHY_NO_DATA: "No cheap market data was available for this mint",
    WHY_DUPLICATE: "An equivalent alert had already been published",
    WHY_RATE_LIMITED: "The hourly ceiling for this lane was reached",
    WHY_COOLDOWN: "This mint was inside its per-tier cooldown",
    WHY_DISABLED: "The early lane is switched off",
}


@dataclass(frozen=True, slots=True)
class EarlyConfig:
    """Thresholds for the cheap lane.  Every one is deliberately loose.

    This lane's job is *visibility*, not commitment: nothing here can authorise
    an entry, so being generous costs an operator a glance, while being strict
    costs them the trade.  The strict PAPER gates are untouched and unrelated.
    """

    enabled: bool = True

    # ---- the early window (section 6) ------------------------------------
    max_age_seconds: int = 3_600
    min_liquidity_usd: Decimal = Decimal("4000")
    min_market_cap_usd: Decimal = Decimal("8000")
    max_market_cap_usd: Decimal = Decimal("2000000")

    # ---- heads-up (level A) ----------------------------------------------
    heads_up_min_score: Decimal = Decimal("30")
    # ---- runner (level B) -------------------------------------------------
    runner_min_score: Decimal = Decimal("55")
    runner_min_buy_sell_ratio: Decimal = Decimal("1.6")
    runner_min_buys: int = 12

    # ---- organic runner (section 17) --------------------------------------
    organic_min_buyers: int = 20
    organic_min_buy_sell_ratio: Decimal = Decimal("2")
    organic_min_volume_to_liquidity: Decimal = Decimal("0.5")

    # ---- lateness (sections 10, 47) ---------------------------------------
    edge_narrowing_percent: Decimal = EDGE_NARROWING_PERCENT
    edge_consumed_percent: Decimal = EDGE_CONSUMED_PERCENT

    # ---- noise control ----------------------------------------------------
    heads_up_cooldown_seconds: int = 900
    runner_cooldown_seconds: int = 1_800
    max_heads_up_per_hour: int = 30
    max_runners_per_hour: int = 12


DEFAULT_EARLY_CONFIG = EarlyConfig()


def early_config_from_settings(settings: object) -> EarlyConfig:
    """Build an :class:`EarlyConfig` from deployment settings without coupling.

    Missing attributes fall back to the code default, so the early lane works on
    a deployment that has not defined a single new Railway variable.
    """

    def value(name: str, attribute: str) -> object:
        raw = getattr(settings, attribute, None)
        return raw if raw is not None else getattr(DEFAULT_EARLY_CONFIG, name)

    cooldown = int(value("runner_cooldown_seconds", "fomo_early_cooldown_seconds"))
    return EarlyConfig(
        enabled=bool(value("enabled", "fomo_early_lane_enabled")),
        max_age_seconds=int(value("max_age_seconds", "fomo_early_max_age_seconds")),
        min_liquidity_usd=Decimal(
            str(value("min_liquidity_usd", "fomo_early_min_liquidity_usd"))
        ),
        runner_min_score=Decimal(str(value("runner_min_score", "fomo_early_runner_min_score"))),
        max_runners_per_hour=int(
            value("max_runners_per_hour", "fomo_early_max_runners_per_hour")
        ),
        heads_up_cooldown_seconds=cooldown,
        runner_cooldown_seconds=cooldown,
    )


@dataclass(frozen=True, slots=True)
class EarlySignals:
    """Cheap evidence only.  One DEX snapshot plus timestamps already stored.

    Nothing on this record costs a wallet trace, a risk lookup or a social
    search, which is what lets the lane publish in seconds.
    """

    mint: str = ""
    now: int = 0
    first_seen_at: int | None = None
    pair_age_seconds: int | None = None

    market_cap_usd: Decimal | None = None
    first_seen_market_cap_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_5m_usd: Decimal | None = None
    price_change_5m_percent: Decimal | None = None
    buys_5m: int = 0
    sells_5m: int = 0
    buys_1h: int = 0
    sells_1h: int = 0
    liquidity_change_percent: Decimal | None = None
    route_available: bool = True
    rugged: bool = False

    #: Optional trade-level evidence from the realtime stream, when present.
    largest_buy_usd: Decimal | None = None
    largest_buy_is_creator_linked: bool = False
    independent_buyers_after_largest_buy: int = 0

    #: Corroboration from the other lanes, when it happens to be in hand.
    notable_wallet_count: int = 0
    proven_early_wallet_count: int = 0
    story_state: str = ""
    story_relationship: str = ""
    catalyst_confidence: str = ""

    @property
    def buy_sell_ratio(self) -> Decimal | None:
        if self.sells_5m <= 0:
            return Decimal("99") if self.buys_5m > 0 else None
        return (Decimal(self.buys_5m) / Decimal(self.sells_5m)).quantize(CENT)

    @property
    def volume_to_liquidity(self) -> Decimal | None:
        if not self.liquidity_usd or self.liquidity_usd <= 0 or self.volume_5m_usd is None:
            return None
        return (self.volume_5m_usd / self.liquidity_usd).quantize(Decimal("0.0001"))

    @property
    def average_trade_usd(self) -> Decimal | None:
        trades = self.buys_5m + self.sells_5m
        if trades <= 0 or self.volume_5m_usd is None:
            return None
        return (self.volume_5m_usd / Decimal(trades)).quantize(CENT)

    @property
    def move_since_first_seen_percent(self) -> Decimal | None:
        """How much of the move the bot has already watched go by."""

        base = self.first_seen_market_cap_usd
        if not base or base <= 0 or self.market_cap_usd is None:
            return None
        return ((self.market_cap_usd - base) / base * HUNDRED).quantize(CENT)

    @property
    def seconds_since_first_seen(self) -> int | None:
        if not self.first_seen_at or not self.now:
            return None
        return max(0, self.now - self.first_seen_at)


@dataclass(frozen=True, slots=True)
class LargeBuyImpulse:
    """Did one purchase actually change the market? (sections 7, 8)

    Deliberately not "trade value above $N".  A $2,000 buy is irrelevant against
    deep liquidity and enormous against a fresh $6K pool, so the measurement is
    always relative — to liquidity, to the recent average trade, and to the
    market-cap move it produced.
    """

    detected: bool = False
    quality: str = BUY_NONE
    buy_usd: Decimal | None = None
    liquidity_share_percent: Decimal | None = None
    trade_size_multiple: Decimal | None = None
    market_cap_jump_percent: Decimal | None = None
    follow_on_buyers: int = 0
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_demand(self) -> bool:
        """Only a corroborated impulse counts as demand (section 8)."""

        return self.quality == BUY_CONFIRMED


def detect_large_buy(signals: EarlySignals) -> LargeBuyImpulse:
    """Find a purchase that materially moved this market, and grade it.

    Grading matters more than detection.  A creator buying their own token
    produces the same chart shape as real demand and none of the meaning, so an
    impulse is only ``BUY_CONFIRMED`` once independent buyers have followed it.
    """

    reasons: list[str] = []
    share: Decimal | None = None
    multiple: Decimal | None = None

    buy = signals.largest_buy_usd
    if buy is not None and buy > 0:
        if signals.liquidity_usd and signals.liquidity_usd > 0:
            share = (buy / signals.liquidity_usd * HUNDRED).quantize(CENT)
        average = signals.average_trade_usd
        if average and average > 0:
            multiple = (buy / average).quantize(CENT)

    jump = signals.price_change_5m_percent
    detected = False
    if share is not None and share >= IMPULSE_LIQUIDITY_SHARE_PERCENT:
        detected = True
        reasons.append(f"one buy was {share}% of available liquidity")
    if multiple is not None and multiple >= IMPULSE_TRADE_MULTIPLE:
        detected = True
        reasons.append(f"one buy was {multiple}x the recent average trade")
    if (
        buy is None
        and jump is not None
        and jump >= IMPULSE_MARKET_CAP_JUMP_PERCENT
        and signals.buys_5m > 0
        and (signals.buy_sell_ratio or ZERO) >= Decimal("2")
    ):
        # No trade-level feed for this mint, so infer the impulse from the move
        # it produced.  Marked as an inference in the reasons, never as an
        # observed trade.
        detected = True
        reasons.append(f"market cap jumped {jump}% on buy-dominated flow (inferred)")

    if not detected:
        return LargeBuyImpulse(
            detected=False,
            quality=BUY_NONE,
            buy_usd=buy,
            liquidity_share_percent=share,
            trade_size_multiple=multiple,
            market_cap_jump_percent=jump,
            reasons=(),
        )

    follow_on = signals.independent_buyers_after_largest_buy
    if signals.largest_buy_is_creator_linked and follow_on < IMPULSE_MIN_FOLLOW_ON_BUYERS:
        quality = BUY_INSIDER
        reasons.append("the buyer looks creator-linked and nobody independent followed")
    elif follow_on >= IMPULSE_MIN_FOLLOW_ON_BUYERS:
        quality = BUY_CONFIRMED
        reasons.append(f"{follow_on} independent buyers followed the impulse")
    elif signals.buys_5m >= IMPULSE_MIN_FOLLOW_ON_BUYERS and (
        (signals.buy_sell_ratio or ZERO) >= Decimal("2")
    ):
        quality = BUY_CONFIRMED
        reasons.append(f"{signals.buys_5m} buys against {signals.sells_5m} sells followed")
    else:
        quality = BUY_UNCONFIRMED
        reasons.append("no independent follow-on demand yet")

    return LargeBuyImpulse(
        detected=True,
        quality=quality,
        buy_usd=buy,
        liquidity_share_percent=share,
        trade_size_multiple=multiple,
        market_cap_jump_percent=jump,
        follow_on_buyers=follow_on,
        reasons=tuple(reasons),
    )


@dataclass(frozen=True, slots=True)
class EarlyVerdict:
    """What the operator should see, when, and the reason it is justified."""

    tier: str = TIER_NONE
    score: Decimal = ZERO
    edge_state: str = EDGE_AVAILABLE
    move_since_first_seen_percent: Decimal | None = None
    evidence_categories: tuple[str, ...] = field(default_factory=tuple)
    reasons: tuple[str, ...] = field(default_factory=tuple)
    blockers: tuple[str, ...] = field(default_factory=tuple)
    why_not_pinged: tuple[str, ...] = field(default_factory=tuple)
    impulse: LargeBuyImpulse = field(default_factory=LargeBuyImpulse)

    @property
    def visible(self) -> bool:
        return self.tier != TIER_NONE

    @property
    def may_ping(self) -> bool:
        """A ping needs a pingable tier, a serious category and live edge."""

        return (
            self.tier in PINGABLE_TIERS
            and bool(self.evidence_categories)
            and self.edge_state == EDGE_AVAILABLE
            and not self.blockers
        )

    @property
    def entry_eligible(self) -> bool:
        """Structural guarantee: this lane can never authorise an entry."""

        return False

    @property
    def late(self) -> bool:
        return self.edge_state in {EDGE_CONSUMED, MOVE_EXTENDED}

    @property
    def label(self) -> str:
        if self.late:
            return "⚠ RUNNER — EDGE CONSUMED"
        return {
            TIER_ORGANIC_RUNNER: "🚨 ORGANIC RUNNER — LOOK NOW",
            TIER_EARLY_RUNNER: "🚨 EARLY RUNNER — LOOK NOW",
            TIER_EARLY_HEADS_UP: "👀 EARLY HEADS-UP",
        }.get(self.tier, "NOT SURFACED")


def classify_edge_state(
    move_since_first_seen_percent: Decimal | None,
    *,
    config: EarlyConfig = DEFAULT_EARLY_CONFIG,
) -> str:
    """Has the move the bot watched already spent the edge? (sections 10, 47)"""

    if move_since_first_seen_percent is None:
        return EDGE_AVAILABLE
    if move_since_first_seen_percent >= MOVE_EXTENDED_PERCENT:
        return MOVE_EXTENDED
    if move_since_first_seen_percent >= config.edge_consumed_percent:
        return EDGE_CONSUMED
    if move_since_first_seen_percent >= config.edge_narrowing_percent:
        return EDGE_NARROWING
    return EDGE_AVAILABLE


def evaluate_early_signal(
    signals: EarlySignals,
    *,
    config: EarlyConfig = DEFAULT_EARLY_CONFIG,
) -> EarlyVerdict:
    """Decide operator visibility from cheap evidence alone.

    Runs in microseconds on data already in hand.  It is the whole answer to
    "the bot knew at $31K and I found out at $61K": this can fire while the
    deep pipeline is still working, and its verdict says plainly whether the
    edge is still there.
    """

    if not config.enabled:
        return EarlyVerdict(why_not_pinged=(WHY_DISABLED,))

    blockers: list[str] = []
    why: list[str] = []

    if signals.market_cap_usd is None or signals.liquidity_usd is None:
        return EarlyVerdict(why_not_pinged=(WHY_NO_DATA,))
    if signals.rugged:
        blockers.append("rug evidence already present")
        why.append(WHY_RUGGED)
    if not signals.route_available:
        blockers.append("no usable route")
        why.append(WHY_ROUTE)
    if signals.liquidity_usd < config.min_liquidity_usd:
        blockers.append(f"liquidity ${signals.liquidity_usd} below the early floor")
        why.append(WHY_LIQUIDITY)
    if not (config.min_market_cap_usd <= signals.market_cap_usd <= config.max_market_cap_usd):
        blockers.append(f"market cap ${signals.market_cap_usd} outside the early range")
        why.append(WHY_MC_OUT_OF_RANGE)
    if (
        signals.pair_age_seconds is not None
        and signals.pair_age_seconds > config.max_age_seconds
    ):
        blockers.append(f"pair is {signals.pair_age_seconds}s old")
        why.append(WHY_TOO_OLD)

    impulse = detect_large_buy(signals)
    move = signals.move_since_first_seen_percent
    edge_state = classify_edge_state(move, config=config)

    score = ZERO
    reasons: list[str] = []
    categories: list[str] = []

    # --- market structure --------------------------------------------------
    ratio = signals.buy_sell_ratio
    if ratio is not None and ratio >= config.organic_min_buy_sell_ratio:
        score += 22
        reasons.append(f"{signals.buys_5m} buys against {signals.sells_5m} sells")
    elif ratio is not None and ratio >= config.runner_min_buy_sell_ratio:
        score += 14
        reasons.append(f"buy flow leading {ratio}:1")

    if signals.buys_5m >= config.organic_min_buyers:
        score += 20
        reasons.append(f"{signals.buys_5m} buys in the last five minutes")
    elif signals.buys_5m >= config.runner_min_buys:
        score += 12

    depth = signals.volume_to_liquidity
    if depth is not None and depth >= config.organic_min_volume_to_liquidity:
        score += 16
        reasons.append(f"five-minute volume is {depth}x liquidity")
    elif depth is not None and depth >= Decimal("0.2"):
        score += 8

    change = signals.price_change_5m_percent
    if change is not None and change >= 20:
        score += 14
        reasons.append(f"price up {change}% in five minutes")
    elif change is not None and change >= 8:
        score += 8

    if signals.liquidity_change_percent is not None and signals.liquidity_change_percent >= 15:
        score += 8
        reasons.append(f"liquidity up {signals.liquidity_change_percent}%")

    if signals.pair_age_seconds is not None and signals.pair_age_seconds <= 900:
        score += 12
        reasons.append("fresh pair")

    if impulse.detected:
        if impulse.is_demand:
            score += 20
            reasons.extend(impulse.reasons)
        elif impulse.quality == BUY_INSIDER:
            # Section 8: a creator self-buy is not demand, and pretending it is
            # would be the single easiest way to fill the channel with traps.
            score -= 10
            why.append(WHY_INSIDER_ONLY)
            blockers.append("the only large buy looked creator-linked")
        else:
            score += 6
            reasons.extend(impulse.reasons)

    bounded = max(ZERO, min(HUNDRED, score)).quantize(CENT)

    # --- serious evidence categories (section 15) --------------------------
    organic_strong = bool(
        signals.buys_5m >= config.organic_min_buyers
        and ratio is not None
        and ratio >= config.organic_min_buy_sell_ratio
        and depth is not None
        and depth >= config.organic_min_volume_to_liquidity
    )
    if organic_strong:
        categories.append(EV_ORGANIC)
    if impulse.is_demand:
        categories.append(EV_STRUCTURE)
    if signals.story_state in {"ACCELERATING", "STRONG", "VIRAL"} and (
        signals.story_relationship in {"PLAUSIBLE", "STRONG", "DIRECTLY_LINKED", "OFFICIAL"}
    ):
        categories.append(EV_STORY)
    if signals.proven_early_wallet_count > 0:
        categories.append(EV_WALLET)
    if signals.catalyst_confidence in {"CONFIRMED", "HIGH"}:
        categories.append(EV_CATALYST)
    # Confluence means *independent* evidence families agreeing (section 41).
    # Organic flow and a market-structure impulse are both market evidence, so
    # they are one family between them: counting them as two would let a single
    # market observation manufacture its own corroboration.
    market_only = set(categories) <= {EV_ORGANIC, EV_STRUCTURE}
    families = {
        "market" if item in {EV_ORGANIC, EV_STRUCTURE} else item for item in categories
    }
    if len(families) >= 2:
        categories.append(EV_CONFLUENCE)

    # --- tier ---------------------------------------------------------------
    if blockers:
        tier = TIER_NONE
    elif bounded >= config.runner_min_score and categories:
        # A runner whose whole case is market structure is an ORGANIC RUNNER,
        # and section 17 wants it named that way — no story is not a defect.
        tier = TIER_ORGANIC_RUNNER if market_only else TIER_EARLY_RUNNER
    elif bounded >= config.heads_up_min_score:
        tier = TIER_EARLY_HEADS_UP
        if bounded >= config.runner_min_score:
            # Section 43: a high score alone is never a reason to interrupt
            # anyone.  The token still shows on the radar, and the operator is
            # told exactly which bar it missed.
            why.append(WHY_NOT_SERIOUS)
    else:
        tier = TIER_NONE
        why.append(WHY_NO_EARLY_SIGNAL)

    if tier in PINGABLE_TIERS and edge_state != EDGE_AVAILABLE:
        why.append(WHY_MOVE_CONSUMED)

    return EarlyVerdict(
        tier=tier,
        score=bounded,
        edge_state=edge_state,
        move_since_first_seen_percent=move,
        evidence_categories=tuple(dict.fromkeys(categories)),
        reasons=tuple(dict.fromkeys(reasons)),
        blockers=tuple(dict.fromkeys(blockers)),
        why_not_pinged=tuple(dict.fromkeys(why)),
        impulse=impulse,
    )


@dataclass(frozen=True, slots=True)
class AlertTiming:
    """The immutable record of how early an alert actually was (sections 3, 14).

    ``first_seen_market_cap_usd`` and ``alert_market_cap_usd`` are written once
    and never rewritten.  Enrichment updates the *current* market cap and
    nothing else — overwriting the alert cap is precisely how a late alert gets
    to look early in hindsight.
    """

    mint: str = ""
    first_seen_at: int | None = None
    alert_at: int | None = None
    first_seen_market_cap_usd: Decimal | None = None
    alert_market_cap_usd: Decimal | None = None
    current_market_cap_usd: Decimal | None = None
    tier: str = TIER_NONE

    @property
    def first_seen_to_alert_seconds(self) -> int | None:
        if not self.first_seen_at or not self.alert_at:
            return None
        return max(0, self.alert_at - self.first_seen_at)

    @property
    def move_before_alert_percent(self) -> Decimal | None:
        """How much of the move happened while the operator knew nothing."""

        return _move(self.first_seen_market_cap_usd, self.alert_market_cap_usd)

    @property
    def move_after_alert_percent(self) -> Decimal | None:
        return _move(self.alert_market_cap_usd, self.current_market_cap_usd)

    @property
    def move_since_first_seen_percent(self) -> Decimal | None:
        return _move(self.first_seen_market_cap_usd, self.current_market_cap_usd)

    def edge_state(self, *, config: EarlyConfig = DEFAULT_EARLY_CONFIG) -> str:
        return classify_edge_state(self.move_before_alert_percent, config=config)

    @property
    def was_early(self) -> bool:
        """True only when the alert genuinely beat the move.

        A $31K first sight and a $61K alert is not an early alert, and section
        47 requires the system to say so rather than print both numbers and let
        the reader assume.
        """

        move = self.move_before_alert_percent
        return move is not None and move < EDGE_NARROWING_PERCENT


def _move(base: Decimal | None, current: Decimal | None) -> Decimal | None:
    if base is None or current is None or base <= 0:
        return None
    return ((current - base) / base * HUNDRED).quantize(CENT)


# --- alert KPIs (section 14) -------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlertPerformance:
    """Did the operator see the coin before it moved?"""

    alerts: int = 0
    early_alerts: int = 0
    late_alerts: int = 0
    median_first_seen_to_alert_seconds: int | None = None
    median_move_before_alert_percent: Decimal | None = None
    median_first_seen_market_cap_usd: Decimal | None = None
    median_alert_market_cap_usd: Decimal | None = None
    alerted_before_10_percent: Decimal | None = None
    alerted_before_25_percent: Decimal | None = None
    alerted_before_50_percent: Decimal | None = None
    alerted_before_100_percent: Decimal | None = None

    @property
    def early_rate_percent(self) -> Decimal | None:
        if self.alerts <= 0:
            return None
        return (Decimal(self.early_alerts) / Decimal(self.alerts) * HUNDRED).quantize(CENT)


def summarize_alert_performance(
    timings: Sequence[AlertTiming],
    *,
    config: EarlyConfig = DEFAULT_EARLY_CONFIG,
) -> AlertPerformance:
    """The headline KPI: how often was the operator shown the coin in time?"""

    if not timings:
        return AlertPerformance()
    moves = [
        item.move_before_alert_percent
        for item in timings
        if item.move_before_alert_percent is not None
    ]
    latencies = [
        item.first_seen_to_alert_seconds
        for item in timings
        if item.first_seen_to_alert_seconds is not None
    ]

    def before(threshold: Decimal) -> Decimal | None:
        if not moves:
            return None
        hits = sum(1 for value in moves if value < threshold)
        return (Decimal(hits) / Decimal(len(moves)) * HUNDRED).quantize(CENT)

    return AlertPerformance(
        alerts=len(timings),
        early_alerts=sum(1 for item in timings if item.was_early),
        late_alerts=sum(
            1
            for item in timings
            if item.edge_state(config=config) in {EDGE_CONSUMED, MOVE_EXTENDED}
        ),
        median_first_seen_to_alert_seconds=_median_int(latencies),
        median_move_before_alert_percent=_median(moves),
        median_first_seen_market_cap_usd=_median(
            [
                item.first_seen_market_cap_usd
                for item in timings
                if item.first_seen_market_cap_usd is not None
            ]
        ),
        median_alert_market_cap_usd=_median(
            [
                item.alert_market_cap_usd
                for item in timings
                if item.alert_market_cap_usd is not None
            ]
        ),
        alerted_before_10_percent=before(Decimal("10")),
        alerted_before_25_percent=before(Decimal("25")),
        alerted_before_50_percent=before(Decimal("50")),
        alerted_before_100_percent=before(Decimal("100")),
    )


@dataclass(frozen=True, slots=True)
class MissedRunner:
    """A token the bot saw early, that ran, that the operator never saw in time.

    Evaluation only (section 13).  Nothing here can reach an earlier decision;
    it exists so the alert architecture can be held to account after the fact.
    """

    mint: str
    first_seen_at: int
    first_seen_market_cap_usd: Decimal
    peak_market_cap_usd: Decimal
    alert_at: int | None = None
    alert_market_cap_usd: Decimal | None = None
    why_not_pinged: tuple[str, ...] = field(default_factory=tuple)

    @property
    def peak_move_percent(self) -> Decimal | None:
        return _move(self.first_seen_market_cap_usd, self.peak_market_cap_usd)

    @property
    def missed(self) -> bool:
        """Saw it early, it ran, and the operator got nothing useful in time."""

        move = self.peak_move_percent
        if move is None or move < Decimal("25"):
            return False
        if self.alert_at is None:
            return True
        timing = AlertTiming(
            mint=self.mint,
            first_seen_at=self.first_seen_at,
            alert_at=self.alert_at,
            first_seen_market_cap_usd=self.first_seen_market_cap_usd,
            alert_market_cap_usd=self.alert_market_cap_usd,
        )
        return not timing.was_early


def audit_missed_runners(
    candidates: Sequence[MissedRunner],
) -> tuple[MissedRunner, ...]:
    """The tokens worth being annoyed about, biggest move first."""

    missed = [item for item in candidates if item.missed]
    return tuple(
        sorted(missed, key=lambda item: item.peak_move_percent or ZERO, reverse=True)
    )


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return ((ordered[middle - 1] + ordered[middle]) / 2).quantize(CENT)


def _median_int(values: Sequence[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return round((ordered[middle - 1] + ordered[middle]) / 2)
