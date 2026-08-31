"""HOT WATCH: the bounded, fast second look at a strong near-miss.

The concrete production failure this exists to fix (section 42): a candidate
with roughly 1532 buys, 789 sells, a 1.94 buy/sell ratio and heavy volume
against liquidity scored about 50, while the runner gate wanted 55 and an organic
ratio of 2.0.  It produced an ``EARLY_HEADS_UP``, no ping, and the token then
continued materially higher.

Two separate bugs live in that story and both are fixed here:

1.  **The cliff.** 1.94 and 2.00 are the same world.  :mod:`.score` fixes that
    with continuous ramps instead of boolean gates.
2.  **The wait.** Even with better scoring, a candidate that missed by a hair was
    not looked at again for a full recheck window — 15 or 30 minutes — by which
    time the information was worthless.  That is what this module fixes.

HOT WATCH means exactly: *not enough evidence to interrupt a human yet, but
important enough to reevaluate aggressively for a bounded period.*  It does not
ping on entry (section 44).  It re-checks on a short cadence using cheap cached
evidence, and it promotes — once — when the evidence genuinely improves.  If it
does not improve, it expires silently (section 104).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from decimal import Decimal

ZERO = Decimal("0")

# --- lifecycle (section 47) ---------------------------------------------------
HOT_WATCH_ACTIVE = "ACTIVE"
HOT_WATCH_PROMOTED = "PROMOTED"
HOT_WATCH_EXPIRED = "EXPIRED"
HOT_WATCH_DROPPED = "DROPPED"

HOT_WATCH_STATES: tuple[str, ...] = (
    HOT_WATCH_ACTIVE,
    HOT_WATCH_PROMOTED,
    HOT_WATCH_EXPIRED,
    HOT_WATCH_DROPPED,
)

#: Why a candidate was put on HOT WATCH in the first place.
ORIGIN_TRENDING_NEAR_MISS = "TRENDING_NEAR_MISS"
ORIGIN_LEGACY_NEAR_MISS = "LEGACY_NEAR_MISS"
ORIGIN_NEW_ENTRY = "TRENDING_NEW_ENTRY"
ORIGIN_STRONG_THESIS = "STRONG_THESIS"
ORIGIN_STRONG_STORY = "STRONG_STORY"
ORIGIN_NOTABLE_WALLET = "NOTABLE_WALLET"
ORIGIN_HOLDER_ACCELERATION = "HOLDER_ACCELERATION"
ORIGIN_SOCIAL_ACCELERATION = "SOCIAL_ACCELERATION"
ORIGIN_MARKET_STRUCTURE = "EXCEPTIONAL_MARKET_STRUCTURE"

HOT_WATCH_ORIGINS: tuple[str, ...] = (
    ORIGIN_TRENDING_NEAR_MISS,
    ORIGIN_LEGACY_NEAR_MISS,
    ORIGIN_NEW_ENTRY,
    ORIGIN_STRONG_THESIS,
    ORIGIN_STRONG_STORY,
    ORIGIN_NOTABLE_WALLET,
    ORIGIN_HOLDER_ACCELERATION,
    ORIGIN_SOCIAL_ACCELERATION,
    ORIGIN_MARKET_STRUCTURE,
)


@dataclass(frozen=True, slots=True)
class HotWatchConfig:
    """Bounded so a hot watch can never become an unbounded provider bill."""

    #: How long a candidate may stay on HOT WATCH.
    ttl_seconds: int = 900
    #: How often it is reevaluated.  Deliberately far below the 1800s legacy
    #: recheck: the whole point is not waiting 15-30 minutes (section 46).
    recheck_seconds: int = 45
    #: Maximum simultaneous hot watches, so cost stays bounded (section 112).
    max_entries: int = 12
    #: How many points below the alpha threshold still counts as a near miss.
    near_miss_band: Decimal = Decimal("12")
    #: Improvement required before promotion — stops a candidate oscillating
    #: across the line from pinging on noise.
    promotion_margin: Decimal = Decimal("0")
    #: A hot watch that never improves is dropped after this many rechecks.
    max_rechecks: int = 24


DEFAULT_HOT_WATCH_CONFIG = HotWatchConfig()


@dataclass(frozen=True, slots=True)
class HotWatchEntry:
    """One candidate under rapid reevaluation.

    Every market cap on this record is written once at the moment it happened, so
    "was the promotion late?" is answerable afterwards with real numbers rather
    than a reconstruction (sections 48, 49).
    """

    mint: str
    origin: str
    entered_at: int
    expires_at: int
    state: str = HOT_WATCH_ACTIVE

    # ---- write-once timing evidence (section 48) -------------------------
    first_seen_market_cap_usd: Decimal | None = None
    trending_entry_market_cap_usd: Decimal | None = None
    heads_up_market_cap_usd: Decimal | None = None
    hot_watch_market_cap_usd: Decimal | None = None
    promotion_market_cap_usd: Decimal | None = None
    urgent_ping_market_cap_usd: Decimal | None = None

    entry_score: Decimal = ZERO
    best_score: Decimal = ZERO
    last_score: Decimal = ZERO
    rechecks: int = 0
    last_recheck_at: int = 0
    promoted_at: int | None = None
    resolved_at: int | None = None
    promotion_reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def active(self) -> bool:
        return self.state == HOT_WATCH_ACTIVE

    def due(self, *, now: int, config: HotWatchConfig = DEFAULT_HOT_WATCH_CONFIG) -> bool:
        if not self.active:
            return False
        return now - self.last_recheck_at >= config.recheck_seconds

    def promotion_delay_seconds(self) -> int | None:
        if self.promoted_at is None:
            return None
        return max(0, self.promoted_at - self.entered_at)

    def promotion_move_percent(self) -> Decimal | None:
        """How much the market cap moved between heads-up and promotion.

        This is the honesty metric from section 49: a heads-up at $500K and a
        promotion at $1M means the promotion was late, and the number says so
        rather than the card implying it was early.
        """

        start = self.heads_up_market_cap_usd or self.hot_watch_market_cap_usd
        end = self.promotion_market_cap_usd
        if start is None or end is None or start <= ZERO:
            return None
        return ((end - start) / start * Decimal("100")).quantize(Decimal("0.1"))

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "origin": self.origin,
            "entered_at": self.entered_at,
            "expires_at": self.expires_at,
            "state": self.state,
            "first_seen_market_cap_usd": _s(self.first_seen_market_cap_usd),
            "trending_entry_market_cap_usd": _s(self.trending_entry_market_cap_usd),
            "heads_up_market_cap_usd": _s(self.heads_up_market_cap_usd),
            "hot_watch_market_cap_usd": _s(self.hot_watch_market_cap_usd),
            "promotion_market_cap_usd": _s(self.promotion_market_cap_usd),
            "urgent_ping_market_cap_usd": _s(self.urgent_ping_market_cap_usd),
            "entry_score": str(self.entry_score),
            "best_score": str(self.best_score),
            "last_score": str(self.last_score),
            "rechecks": self.rechecks,
            "last_recheck_at": self.last_recheck_at,
            "promoted_at": self.promoted_at,
            "resolved_at": self.resolved_at,
            "promotion_reasons": list(self.promotion_reasons),
            "notes": list(self.notes),
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def entry_from_json(payload: dict[str, object]) -> HotWatchEntry:
    return HotWatchEntry(
        mint=str(payload["mint"]),
        origin=str(payload.get("origin") or ORIGIN_TRENDING_NEAR_MISS),
        entered_at=int(payload.get("entered_at") or 0),
        expires_at=int(payload.get("expires_at") or 0),
        state=str(payload.get("state") or HOT_WATCH_ACTIVE),
        first_seen_market_cap_usd=_d(payload.get("first_seen_market_cap_usd")),
        trending_entry_market_cap_usd=_d(payload.get("trending_entry_market_cap_usd")),
        heads_up_market_cap_usd=_d(payload.get("heads_up_market_cap_usd")),
        hot_watch_market_cap_usd=_d(payload.get("hot_watch_market_cap_usd")),
        promotion_market_cap_usd=_d(payload.get("promotion_market_cap_usd")),
        urgent_ping_market_cap_usd=_d(payload.get("urgent_ping_market_cap_usd")),
        entry_score=_d(payload.get("entry_score")) or ZERO,
        best_score=_d(payload.get("best_score")) or ZERO,
        last_score=_d(payload.get("last_score")) or ZERO,
        rechecks=int(payload.get("rechecks") or 0),
        last_recheck_at=int(payload.get("last_recheck_at") or 0),
        promoted_at=_i(payload.get("promoted_at")),
        resolved_at=_i(payload.get("resolved_at")),
        promotion_reasons=tuple(payload.get("promotion_reasons") or ()),  # type: ignore[arg-type]
        notes=tuple(payload.get("notes") or ()),  # type: ignore[arg-type]
    )


def _d(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _i(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def open_hot_watch(
    mint: str,
    *,
    origin: str,
    now: int,
    score: Decimal,
    market_cap_usd: Decimal | None = None,
    first_seen_market_cap_usd: Decimal | None = None,
    trending_entry_market_cap_usd: Decimal | None = None,
    heads_up_market_cap_usd: Decimal | None = None,
    config: HotWatchConfig = DEFAULT_HOT_WATCH_CONFIG,
    note: str = "",
) -> HotWatchEntry:
    """Put a strong near-miss under rapid review.  This never pings (section 44)."""

    return HotWatchEntry(
        mint=mint,
        origin=origin,
        entered_at=now,
        expires_at=now + config.ttl_seconds,
        first_seen_market_cap_usd=first_seen_market_cap_usd,
        trending_entry_market_cap_usd=trending_entry_market_cap_usd,
        heads_up_market_cap_usd=heads_up_market_cap_usd,
        hot_watch_market_cap_usd=market_cap_usd,
        entry_score=score,
        best_score=score,
        last_score=score,
        last_recheck_at=now,
        notes=(note,) if note else (),
    )


@dataclass(frozen=True, slots=True)
class HotWatchOutcome:
    """The result of one recheck."""

    entry: HotWatchEntry
    promoted: bool = False
    expired: bool = False
    dropped: bool = False
    reasons: tuple[str, ...] = ()
    detail: str = ""

    @property
    def should_ping(self) -> bool:
        """Exactly one escalation ping, at the moment of promotion (section 47)."""

        return self.promoted


def recheck_hot_watch(
    entry: HotWatchEntry,
    *,
    now: int,
    score: Decimal,
    reasons: Iterable[str],
    market_cap_usd: Decimal | None = None,
    alpha_threshold: Decimal,
    blocked: bool = False,
    actionable: bool = True,
    config: HotWatchConfig = DEFAULT_HOT_WATCH_CONFIG,
) -> HotWatchOutcome:
    """Reevaluate a hot watch against fresh evidence.

    Promotion requires three things at once: the score has reached the alpha
    threshold, the candidate carries at least one *named* serious reason, and it
    is still actionable.  A score that arrives without a reason does not promote
    — that is section 57 holding even inside the fast lane.
    """

    named = tuple(dict.fromkeys(reasons))
    best = max(entry.best_score, score)
    updated = replace(
        entry,
        last_score=score,
        best_score=best,
        rechecks=entry.rechecks + 1,
        last_recheck_at=now,
    )

    if blocked:
        return HotWatchOutcome(
            entry=replace(updated, state=HOT_WATCH_DROPPED, resolved_at=now),
            dropped=True,
            detail="hard safety failure — dropped, not promoted",
        )

    if (
        score >= alpha_threshold + config.promotion_margin
        and named
        and actionable
    ):
        promoted = replace(
            updated,
            state=HOT_WATCH_PROMOTED,
            promoted_at=now,
            resolved_at=now,
            promotion_market_cap_usd=market_cap_usd,
            urgent_ping_market_cap_usd=market_cap_usd,
            promotion_reasons=named,
        )
        return HotWatchOutcome(
            entry=promoted,
            promoted=True,
            reasons=named,
            detail="evidence strengthened past the alpha threshold",
        )

    if now >= entry.expires_at:
        return HotWatchOutcome(
            entry=replace(updated, state=HOT_WATCH_EXPIRED, resolved_at=now),
            expired=True,
            detail="evidence never strengthened — expired without a ping",
        )

    if updated.rechecks >= config.max_rechecks:
        return HotWatchOutcome(
            entry=replace(updated, state=HOT_WATCH_EXPIRED, resolved_at=now),
            expired=True,
            detail="recheck budget exhausted",
        )

    return HotWatchOutcome(entry=updated, reasons=named, detail="still under review")


def prune(
    entries: Iterable[HotWatchEntry],
    *,
    now: int,
    config: HotWatchConfig = DEFAULT_HOT_WATCH_CONFIG,
) -> tuple[HotWatchEntry, ...]:
    """Expire anything past its TTL and cap the population (section 112)."""

    live: list[HotWatchEntry] = []
    for entry in entries:
        if not entry.active:
            continue
        if now >= entry.expires_at:
            continue
        live.append(entry)
    live.sort(key=lambda item: (item.best_score, item.entered_at), reverse=True)
    return tuple(live[: config.max_entries])


@dataclass(frozen=True, slots=True)
class HotWatchStatus:
    """What `/fomo trending view:hotwatch` reports (section 90)."""

    active: int = 0
    promoted: int = 0
    expired: int = 0
    dropped: int = 0
    last_promotion_at: int | None = None
    median_promotion_delay_seconds: int | None = None
    promotion_miss_rate: Decimal = ZERO
    expired_without_promotion: int = 0

    def to_json(self) -> dict[str, object]:
        return {
            "active": self.active,
            "promoted": self.promoted,
            "expired": self.expired,
            "dropped": self.dropped,
            "last_promotion_at": self.last_promotion_at,
            "median_promotion_delay_seconds": self.median_promotion_delay_seconds,
            "promotion_miss_rate": str(self.promotion_miss_rate),
            "expired_without_promotion": self.expired_without_promotion,
        }


def summarise(entries: Iterable[HotWatchEntry]) -> HotWatchStatus:
    rows = list(entries)
    promoted = [row for row in rows if row.state == HOT_WATCH_PROMOTED]
    expired = [row for row in rows if row.state == HOT_WATCH_EXPIRED]
    dropped = [row for row in rows if row.state == HOT_WATCH_DROPPED]
    active = [row for row in rows if row.active]

    delays = sorted(
        delay
        for delay in (row.promotion_delay_seconds() for row in promoted)
        if delay is not None
    )
    median = delays[len(delays) // 2] if delays else None

    resolved = len(promoted) + len(expired)
    miss_rate = (
        (Decimal(len(expired)) / Decimal(resolved)).quantize(Decimal("0.01"))
        if resolved
        else ZERO
    )
    return HotWatchStatus(
        active=len(active),
        promoted=len(promoted),
        expired=len(expired),
        dropped=len(dropped),
        last_promotion_at=max((row.promoted_at or 0 for row in promoted), default=0) or None,
        median_promotion_delay_seconds=median,
        promotion_miss_rate=miss_rate,
        expired_without_promotion=len(expired),
    )
