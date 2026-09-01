"""HOT WATCH for the early lane, and promotion driven by *new* information.

The production failure this module exists to fix, exactly (section 1): a
three-minute-old token at $71.93K, $21.09K liquidity, 78 buys against 48 sells,
five-minute volume at 1.21x liquidity and price up 46.48%, scored **76/100** —
twenty-one points clear of the runner bar — and never interrupted anyone.

The reason is not the score.  It is that a runner needs a *serious evidence
category*, and at a 1.625 buy/sell ratio the organic category wanted 2.0, no
large buy had been observed, and no story, wallet or catalyst evidence existed
yet.  So the verdict was ``EARLY_HEADS_UP`` with ``NO_SERIOUS_EVIDENCE_CATEGORY``
— correct at that instant, and then never revisited.  The early lane published
one card and moved on.  HOT WATCH existed, but only the Trending board could
open one.

That is the whole gap: **a strong near-miss got a single look.**  This module
gives it a bounded second, third and fourth look, and promotes it the moment new
information — not a new opinion about the same information — makes the case.

Three disciplines hold it together:

* **Promotion needs new evidence, not a retry.**  Every decision compares the
  current picture against the baseline captured when the watch opened.  A score
  that drifts from 76 to 77 is not news.
* **Exactly one ping.**  Promotion latches.  A token cannot escalate twice, and
  a watch that already pinged is only ever pruned.
* **Everything is written down.**  Section 30 asks "why wasn't I pinged?" to be
  answerable after the fact, so the entry carries its baseline, its rechecks and
  its exact suppression reason, and those survive to the database.

Pure logic: no provider, no database, no signer.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal

from .toptraders import FLOW_DISTRIBUTING

ZERO = Decimal("0")
CENT = Decimal("0.01")

# --- serious evidence families (section 31) ---------------------------------
FAMILY_MARKET = "MARKET_ACCELERATION"
FAMILY_KNOWN_TRADER = "KNOWN_TRADER"
FAMILY_HOLDER = "HOLDER_EXPANSION"
FAMILY_STORY = "STORY"
FAMILY_THESIS = "THESIS"
FAMILY_CATALYST = "CATALYST"
FAMILY_TRENDING = "TRENDING"
FAMILY_CONFLUENCE = "MULTI_SOURCE_CONFLUENCE"

PROMOTION_FAMILIES: tuple[str, ...] = (
    FAMILY_MARKET,
    FAMILY_KNOWN_TRADER,
    FAMILY_HOLDER,
    FAMILY_STORY,
    FAMILY_THESIS,
    FAMILY_CATALYST,
    FAMILY_TRENDING,
    FAMILY_CONFLUENCE,
)

# --- suppression reasons (section 30) ---------------------------------------
WHY_NO_NEW_EVIDENCE = "NO_NEW_EVIDENCE_SINCE_HEADS_UP"
WHY_EDGE_CONSUMED = "EDGE_CONSUMED_BEFORE_PROMOTION"
WHY_ALREADY_PROMOTED = "ALREADY_PROMOTED_ONCE"
WHY_EXPIRED = "HOT_WATCH_WINDOW_EXPIRED"
WHY_KNOWN_MONEY_LEAVING = "KNOWN_MONEY_DISTRIBUTING"
WHY_CONCENTRATION_WORSENING = "OWNERSHIP_CONCENTRATION_WORSENING"
WHY_NOT_OPENED = "NOT_STRONG_ENOUGH_FOR_HOT_WATCH"

HUMAN_WHY: dict[str, str] = {
    WHY_NO_NEW_EVIDENCE: (
        "Nothing new arrived after the heads-up — the same evidence does not "
        "become a reason to interrupt anyone by being looked at again"
    ),
    WHY_EDGE_CONSUMED: "The move was already spent before the evidence arrived",
    WHY_ALREADY_PROMOTED: "This candidate was already escalated once",
    WHY_EXPIRED: "The hot-watch window closed without the evidence developing",
    WHY_KNOWN_MONEY_LEAVING: (
        "The known wallets on this token were selling into later buyers, not adding"
    ),
    WHY_CONCENTRATION_WORSENING: (
        "Ownership was concentrating while it grew, so the growth was not distribution"
    ),
    WHY_NOT_OPENED: "The candidate never reached the hot-watch bar",
}

#: Market-shaped families are one evidence family between them, so a single
#: market observation can never manufacture its own corroboration.
_MARKET_LIKE: frozenset[str] = frozenset({FAMILY_MARKET, FAMILY_TRENDING})

#: The holder modules' concentration vocabulary, restated here rather than
#: imported so this package stays independent of the trenches package.  The
#: values must match :mod:`..trenches.holders` exactly — a mismatch would make
#: the guard below silently unreachable, which is precisely the class of bug a
#: string comparison across package boundaries invites.
CONCENTRATION_IMPROVING = "IMPROVING"
CONCENTRATION_WORSENING = "WORSENING"


@dataclass(frozen=True, slots=True)
class EarlyWatchConfig:
    """How long, how often, and how much better it has to get."""

    enabled: bool = True
    #: How long a near-miss stays under review.
    ttl_seconds: int = 1_800
    #: Minimum gap between timer-driven rechecks.  Events bypass this entirely.
    recheck_seconds: int = 30
    #: Ceiling on simultaneous watches, so this cannot become a crawler.
    max_entries: int = 40

    # ---- what earns a watch in the first place (section 2) ---------------
    #: Score at which a heads-up is a near-miss rather than noise.
    min_entry_score: Decimal = Decimal("45")

    # ---- what earns a promotion (section 3) ------------------------------
    #: Score improvement that counts as the market actually accelerating.
    min_score_gain: Decimal = Decimal("6")
    #: New buys since the heads-up before flow counts as new information.
    min_new_buys: int = 25
    #: New independent buyers before participation counts as new information.
    min_new_independent_buyers: int = 6
    #: New holders before growth counts as expansion.
    min_new_holders: int = 15
    #: Holders per minute at which growth is genuinely fast.
    min_holders_per_minute: Decimal = Decimal("3")
    #: Proven, cluster-adjusted known wallets needed to confirm on their own.
    min_known_traders: int = 1
    #: A single new buy this large is itself news.
    large_buy_usd: Decimal = Decimal("2500")


DEFAULT_EARLY_WATCH_CONFIG = EarlyWatchConfig()


def early_watch_config_from_settings(settings: object) -> EarlyWatchConfig:
    """Build a config from deployment settings without coupling to them.

    Missing attributes fall back to the code default, so a deployment that has
    not defined a single new Railway variable still gets the fix.
    """

    def value(name: str, attribute: str) -> object:
        raw = getattr(settings, attribute, None)
        return raw if raw is not None else getattr(DEFAULT_EARLY_WATCH_CONFIG, name)

    return EarlyWatchConfig(
        enabled=bool(value("enabled", "fomo_early_watch_enabled")),
        ttl_seconds=int(value("ttl_seconds", "fomo_early_watch_seconds")),  # type: ignore[arg-type]
        recheck_seconds=int(
            value("recheck_seconds", "fomo_early_watch_recheck_seconds")  # type: ignore[arg-type]
        ),
        max_entries=int(value("max_entries", "fomo_early_watch_max")),  # type: ignore[arg-type]
        min_entry_score=Decimal(
            str(value("min_entry_score", "fomo_early_watch_min_score"))
        ),
        min_score_gain=Decimal(
            str(value("min_score_gain", "fomo_early_promotion_min_score_gain"))
        ),
        min_new_buys=int(
            value("min_new_buys", "fomo_early_promotion_min_new_buys")  # type: ignore[arg-type]
        ),
        min_new_holders=int(
            value("min_new_holders", "fomo_early_promotion_min_new_holders")  # type: ignore[arg-type]
        ),
        large_buy_usd=Decimal(
            str(value("large_buy_usd", "fomo_early_promotion_large_buy_usd"))
        ),
    )


@dataclass(frozen=True, slots=True)
class EarlyWatchEntry:
    """One near-miss under review, with the baseline it will be judged against.

    Every ``entry_*`` field is written once when the watch opens and never
    rewritten.  They are the record of what we knew at the heads-up, and
    promotion is defined as *the difference* between them and now — so allowing
    enrichment to overwrite them would quietly delete the very comparison this
    module exists to make.
    """

    mint: str
    origin: str = "early_lane"
    opened_at: int = 0
    expires_at: int = 0

    # ---- immutable baseline (section 30) ---------------------------------
    entry_tier: str = ""
    entry_score: Decimal = ZERO
    entry_market_cap_usd: Decimal | None = None
    first_seen_market_cap_usd: Decimal | None = None
    entry_liquidity_usd: Decimal | None = None
    entry_buys: int | None = None
    entry_independent_buyers: int | None = None
    entry_holder_count: int | None = None
    entry_evidence: tuple[str, ...] = ()
    entry_why_not_pinged: tuple[str, ...] = ()

    # ---- mutable review state --------------------------------------------
    rechecks: int = 0
    event_rechecks: int = 0
    last_recheck_at: int = 0
    last_score: Decimal = ZERO
    best_score: Decimal = ZERO
    best_market_cap_usd: Decimal | None = None
    promoted: bool = False
    promoted_at: int | None = None
    promotion_family: str = ""
    promotion_families: tuple[str, ...] = ()
    suppression_reason: str = WHY_NO_NEW_EVIDENCE
    notes: tuple[str, ...] = field(default_factory=tuple)

    def due(self, *, now: int, config: EarlyWatchConfig = DEFAULT_EARLY_WATCH_CONFIG) -> bool:
        return now - self.last_recheck_at >= config.recheck_seconds

    def expired(self, *, now: int) -> bool:
        return now >= self.expires_at

    @property
    def score_gain(self) -> Decimal:
        return (self.best_score - self.entry_score).quantize(CENT)

    def human_reason(self) -> str:
        if self.promoted:
            return f"Promoted on {self.promotion_family.replace('_', ' ').lower()}"
        return HUMAN_WHY.get(self.suppression_reason, self.suppression_reason)

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "origin": self.origin,
            "opened_at": self.opened_at,
            "expires_at": self.expires_at,
            "entry_tier": self.entry_tier,
            "entry_score": str(self.entry_score),
            "entry_market_cap_usd": _s(self.entry_market_cap_usd),
            "first_seen_market_cap_usd": _s(self.first_seen_market_cap_usd),
            "entry_liquidity_usd": _s(self.entry_liquidity_usd),
            "entry_buys": self.entry_buys,
            "entry_independent_buyers": self.entry_independent_buyers,
            "entry_holder_count": self.entry_holder_count,
            "entry_evidence": list(self.entry_evidence),
            "entry_why_not_pinged": list(self.entry_why_not_pinged),
            "rechecks": self.rechecks,
            "event_rechecks": self.event_rechecks,
            "last_recheck_at": self.last_recheck_at,
            "last_score": str(self.last_score),
            "best_score": str(self.best_score),
            "best_market_cap_usd": _s(self.best_market_cap_usd),
            "score_gain": str(self.score_gain),
            "promoted": self.promoted,
            "promoted_at": self.promoted_at,
            "promotion_family": self.promotion_family,
            "promotion_families": list(self.promotion_families),
            "suppression_reason": self.suppression_reason,
            "human_reason": self.human_reason(),
            "notes": list(self.notes),
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _d(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _i(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def entry_from_json(payload: dict[str, object]) -> EarlyWatchEntry:
    """Rebuild a watch from its persisted form, baseline intact."""

    return EarlyWatchEntry(
        mint=str(payload.get("mint") or ""),
        origin=str(payload.get("origin") or "early_lane"),
        opened_at=int(payload.get("opened_at") or 0),
        expires_at=int(payload.get("expires_at") or 0),
        entry_tier=str(payload.get("entry_tier") or ""),
        entry_score=_d(payload.get("entry_score")) or ZERO,
        entry_market_cap_usd=_d(payload.get("entry_market_cap_usd")),
        first_seen_market_cap_usd=_d(payload.get("first_seen_market_cap_usd")),
        entry_liquidity_usd=_d(payload.get("entry_liquidity_usd")),
        entry_buys=_i(payload.get("entry_buys")),
        entry_independent_buyers=_i(payload.get("entry_independent_buyers")),
        entry_holder_count=_i(payload.get("entry_holder_count")),
        entry_evidence=tuple(str(item) for item in (payload.get("entry_evidence") or ())),
        entry_why_not_pinged=tuple(
            str(item) for item in (payload.get("entry_why_not_pinged") or ())
        ),
        rechecks=int(payload.get("rechecks") or 0),
        event_rechecks=int(payload.get("event_rechecks") or 0),
        last_recheck_at=int(payload.get("last_recheck_at") or 0),
        last_score=_d(payload.get("last_score")) or ZERO,
        best_score=_d(payload.get("best_score")) or ZERO,
        best_market_cap_usd=_d(payload.get("best_market_cap_usd")),
        promoted=bool(payload.get("promoted")),
        promoted_at=_i(payload.get("promoted_at")),
        promotion_family=str(payload.get("promotion_family") or ""),
        promotion_families=tuple(
            str(item) for item in (payload.get("promotion_families") or ())
        ),
        suppression_reason=str(payload.get("suppression_reason") or WHY_NO_NEW_EVIDENCE),
        notes=tuple(str(item) for item in (payload.get("notes") or ())),
    )


def should_open_watch(
    verdict: object,
    *,
    config: EarlyWatchConfig = DEFAULT_EARLY_WATCH_CONFIG,
) -> bool:
    """Whether a heads-up is a strong enough near-miss to keep watching (section 2).

    A candidate that already pinged does not need a watch, and one that is
    already late is not going to become early.  What is left is exactly the
    interesting case: real evidence, not yet a serious category.
    """

    if not config.enabled:
        return False
    if not getattr(verdict, "visible", False):
        return False
    if getattr(verdict, "may_ping", False):
        return False
    if getattr(verdict, "late", False):
        return False
    if getattr(verdict, "blockers", ()):
        return False
    score = getattr(verdict, "score", ZERO)
    return bool(score >= config.min_entry_score)


def open_early_watch(
    mint: str,
    *,
    verdict: object,
    now: int,
    market_cap_usd: Decimal | None = None,
    first_seen_market_cap_usd: Decimal | None = None,
    liquidity_usd: Decimal | None = None,
    buys: int | None = None,
    independent_buyers: int | None = None,
    holder_count: int | None = None,
    origin: str = "early_lane",
    config: EarlyWatchConfig = DEFAULT_EARLY_WATCH_CONFIG,
) -> EarlyWatchEntry:
    """Put a strong near-miss under rapid review.  Opening never pings."""

    score = Decimal(str(getattr(verdict, "score", ZERO)))
    return EarlyWatchEntry(
        mint=mint,
        origin=origin,
        opened_at=now,
        expires_at=now + config.ttl_seconds,
        entry_tier=str(getattr(verdict, "tier", "")),
        entry_score=score,
        entry_market_cap_usd=market_cap_usd,
        first_seen_market_cap_usd=first_seen_market_cap_usd,
        entry_liquidity_usd=liquidity_usd,
        entry_buys=buys,
        entry_independent_buyers=independent_buyers,
        entry_holder_count=holder_count,
        entry_evidence=tuple(getattr(verdict, "evidence_categories", ()) or ()),
        entry_why_not_pinged=tuple(getattr(verdict, "why_not_pinged", ()) or ()),
        last_recheck_at=now,
        last_score=score,
        best_score=score,
        best_market_cap_usd=market_cap_usd,
    )


@dataclass(frozen=True, slots=True)
class PromotionEvidence:
    """The picture *now*, so promotion can be defined as the difference.

    Every field is optional, and ``None`` means unknown rather than zero.  An
    unknown holder count must not read as "no holders", because a missing
    provider would then look identical to a token nobody owns.
    """

    now: int = 0
    score: Decimal = ZERO
    edge_available: bool = True
    market_cap_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    buys: int | None = None
    sells: int | None = None
    independent_buyers: int | None = None
    largest_new_buy_usd: Decimal | None = None

    # ---- holder intelligence (sections 10-12) ----------------------------
    holder_count: int | None = None
    holders_per_minute: Decimal | None = None
    concentration_trend: str = ""

    # ---- known traders (sections 5-9) ------------------------------------
    proven_independent_traders: int = 0
    known_money_flow: str = ""

    # ---- story, thesis, catalyst, board (sections 22-26, 33) -------------
    story_state: str = ""
    story_relationship: str = ""
    thesis_grade: str = ""
    catalyst_confidence: str = ""
    trending_event: str = ""

    #: What triggered this evaluation: a timer, or a named event (section 29).
    trigger: str = "timer"


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    """Promote or not, and the exact reason either way."""

    promote: bool = False
    families: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    suppression_reason: str = WHY_NO_NEW_EVIDENCE

    @property
    def family(self) -> str:
        """The single family that leads the card."""

        if not self.families:
            return ""
        if FAMILY_CONFLUENCE in self.families:
            return FAMILY_CONFLUENCE
        return self.families[0]

    @property
    def should_ping(self) -> bool:
        """Exactly one operator interrupt, at the moment of promotion."""

        return self.promote


@dataclass(frozen=True, slots=True)
class PromotionOutcome:
    """The updated watch plus what was decided about it."""

    entry: EarlyWatchEntry
    decision: PromotionDecision
    expired: bool = False

    @property
    def should_ping(self) -> bool:
        return self.decision.should_ping


def evaluate_promotion(
    entry: EarlyWatchEntry,
    evidence: PromotionEvidence,
    *,
    config: EarlyWatchConfig = DEFAULT_EARLY_WATCH_CONFIG,
) -> PromotionOutcome:
    """Decide whether *new* information has made this candidate worth an interrupt.

    Order matters here.  Expiry and the one-ping latch are checked before any
    evidence is weighed, because a watch that already fired or has run out of
    window cannot be promoted no matter how good the picture looks — and a
    candidate whose edge is gone is not a candidate any more, however strong the
    evidence became (section 3: *while edge remains*).
    """

    now = evidence.now or entry.last_recheck_at
    best = max(entry.best_score, evidence.score)
    updated = replace(
        entry,
        rechecks=entry.rechecks + 1,
        event_rechecks=entry.event_rechecks + (0 if evidence.trigger == "timer" else 1),
        last_recheck_at=now,
        last_score=evidence.score,
        best_score=best,
        best_market_cap_usd=_max_optional(entry.best_market_cap_usd, evidence.market_cap_usd),
    )

    if entry.promoted:
        return PromotionOutcome(
            entry=replace(updated, suppression_reason=WHY_ALREADY_PROMOTED),
            decision=PromotionDecision(suppression_reason=WHY_ALREADY_PROMOTED),
        )
    if entry.expired(now=now):
        return PromotionOutcome(
            entry=replace(updated, suppression_reason=WHY_EXPIRED),
            decision=PromotionDecision(suppression_reason=WHY_EXPIRED),
            expired=True,
        )
    if not evidence.edge_available:
        return PromotionOutcome(
            entry=replace(updated, suppression_reason=WHY_EDGE_CONSUMED),
            decision=PromotionDecision(suppression_reason=WHY_EDGE_CONSUMED),
        )

    families: list[str] = []
    reasons: list[str] = []

    # --- market acceleration ---------------------------------------------
    gain = evidence.score - entry.entry_score
    new_buys = _delta(evidence.buys, entry.entry_buys)
    new_independent = _delta(evidence.independent_buyers, entry.entry_independent_buyers)
    market_moved = False
    if gain >= config.min_score_gain:
        market_moved = True
        reasons.append(f"signal strengthened {entry.entry_score:.0f} → {evidence.score:.0f}")
    if new_buys is not None and new_buys >= config.min_new_buys:
        market_moved = True
        reasons.append(f"{new_buys} new buys since the heads-up")
    if (
        evidence.largest_new_buy_usd is not None
        and evidence.largest_new_buy_usd >= config.large_buy_usd
    ):
        market_moved = True
        reasons.append(f"a single ${evidence.largest_new_buy_usd:,.0f} buy landed")
    if new_independent is not None and new_independent >= config.min_new_independent_buyers:
        market_moved = True
        reasons.append(f"{new_independent} new independent buyers")
    if market_moved:
        families.append(FAMILY_MARKET)

    # --- known traders (sections 6-9) -------------------------------------
    if evidence.proven_independent_traders >= config.min_known_traders:
        if evidence.known_money_flow == FLOW_DISTRIBUTING:
            # They are here, and they are leaving.  That is information, and it
            # is the opposite of a reason to buy.
            reasons.append("known wallets present but distributing")
        else:
            families.append(FAMILY_KNOWN_TRADER)
            reasons.append(
                f"{evidence.proven_independent_traders} proven independent known "
                "wallet(s) entered"
            )

    # --- holder expansion (sections 10-12) --------------------------------
    new_holders = _delta(evidence.holder_count, entry.entry_holder_count)
    holder_growth = (
        new_holders is not None and new_holders >= config.min_new_holders
    ) or (
        evidence.holders_per_minute is not None
        and evidence.holders_per_minute >= config.min_holders_per_minute
    )
    if holder_growth:
        if evidence.concentration_trend == CONCENTRATION_WORSENING:
            # Growth into fewer hands is accumulation by someone, not
            # distribution to many.  It does not earn a family.
            reasons.append("holders grew but ownership concentrated")
        else:
            families.append(FAMILY_HOLDER)
            if new_holders is not None:
                reasons.append(f"{new_holders} new holders since the heads-up")
            elif evidence.holders_per_minute is not None:
                reasons.append(f"{evidence.holders_per_minute} new holders per minute")

    # --- story, thesis, catalyst, board -----------------------------------
    if evidence.story_state in {"ACCELERATING", "STRONG", "VIRAL"} and (
        evidence.story_relationship in {"PLAUSIBLE", "STRONG", "DIRECTLY_LINKED", "OFFICIAL"}
    ):
        families.append(FAMILY_STORY)
        reasons.append(f"story {evidence.story_state.lower()} and linked to this exact mint")
    if evidence.thesis_grade in {"STRONG", "ACTIONABLE"}:
        families.append(FAMILY_THESIS)
        reasons.append("a checkable public thesis pre-dates the move")
    if evidence.catalyst_confidence in {"CONFIRMED", "HIGH"}:
        families.append(FAMILY_CATALYST)
        reasons.append("a confirmed catalyst is attached to this exact mint")
    if evidence.trending_event in {
        "TRENDING_ACCELERATION",
        "TRENDING_BREAKOUT",
        "NEW_ENTRY_CLIMBING",
    }:
        families.append(FAMILY_TRENDING)
        reasons.append(f"board state {evidence.trending_event.replace('_', ' ').lower()}")

    # Confluence means *independent* families agreeing (section 31).  Market
    # acceleration and a trending-board move are both market observations, so
    # they collapse to one family between them before the count.
    distinct = {FAMILY_MARKET if item in _MARKET_LIKE else item for item in families}
    if len(distinct) >= 2:
        families.append(FAMILY_CONFLUENCE)
        reasons.append(f"{len(distinct)} independent evidence families agree")

    if not families:
        reason = _suppression_for(evidence)
        return PromotionOutcome(
            entry=replace(updated, suppression_reason=reason),
            decision=PromotionDecision(reasons=tuple(reasons), suppression_reason=reason),
        )

    ordered = tuple(dict.fromkeys(families))
    decision = PromotionDecision(
        promote=True,
        families=ordered,
        reasons=tuple(dict.fromkeys(reasons)),
        suppression_reason="",
    )
    return PromotionOutcome(
        entry=replace(
            updated,
            promoted=True,
            promoted_at=now,
            promotion_family=decision.family,
            promotion_families=ordered,
            suppression_reason="",
        ),
        decision=decision,
    )


def _suppression_for(evidence: PromotionEvidence) -> str:
    """Name the most specific thing that stopped a promotion, not just 'no'."""

    if evidence.known_money_flow == FLOW_DISTRIBUTING:
        return WHY_KNOWN_MONEY_LEAVING
    if evidence.concentration_trend == CONCENTRATION_WORSENING:
        return WHY_CONCENTRATION_WORSENING
    return WHY_NO_NEW_EVIDENCE


def _delta(current: int | None, base: int | None) -> int | None:
    if current is None or base is None:
        return None
    return current - base


def _max_optional(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def prune(
    entries: Iterable[EarlyWatchEntry],
    *,
    now: int,
) -> tuple[EarlyWatchEntry, ...]:
    """Drop expired watches and any that already fired their single ping."""

    return tuple(
        entry for entry in entries if not entry.expired(now=now) and not entry.promoted
    )


@dataclass(frozen=True, slots=True)
class EarlyWatchStatus:
    """A glanceable summary for `/fomo trending view:whynotpinged` and status."""

    live: int = 0
    promoted: int = 0
    expired_without_promotion: int = 0
    best_score: Decimal = ZERO
    suppression_counts: tuple[tuple[str, int], ...] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "live": self.live,
            "promoted": self.promoted,
            "expired_without_promotion": self.expired_without_promotion,
            "best_score": str(self.best_score),
            "suppression_counts": [list(item) for item in self.suppression_counts],
        }


def summarise(entries: Sequence[EarlyWatchEntry], *, now: int) -> EarlyWatchStatus:
    counts: dict[str, int] = {}
    live = promoted = expired = 0
    best = ZERO
    for entry in entries:
        best = max(best, entry.best_score)
        if entry.promoted:
            promoted += 1
            continue
        if entry.expired(now=now):
            expired += 1
        else:
            live += 1
        counts[entry.suppression_reason] = counts.get(entry.suppression_reason, 0) + 1
    return EarlyWatchStatus(
        live=live,
        promoted=promoted,
        expired_without_promotion=expired,
        best_score=best,
        suppression_counts=tuple(sorted(counts.items(), key=lambda item: (-item[1], item[0]))),
    )
