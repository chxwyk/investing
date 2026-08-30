"""Event-first catalyst intelligence (sections 14-21).

The runner asks "here is a token — why might it matter?".  This module adds the
complementary direction: "here is a real event — is there a fresh token around
it?".

The distinction the product contract calls critical is enforced structurally:
**event confidence and token↔event connection confidence are separate values
that are never merged.**  TikTok genuinely releasing a product is VERIFIED; a
token called "TikTok Tako" is at best PLAUSIBLE and never official.  A verified
event does not make a token safe, legitimate, or officially connected, and every
card built from these types says so.

Nothing here can authorise a PAPER entry.  Catalysts raise research priority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .config import DEFAULT_LAB_CONFIG, LabConfig

ZERO = Decimal("0")
HUNDRED = Decimal("100")

# --- event authenticity (section 15) -----------------------------------------
NONE = "NONE"
UNVERIFIED = "UNVERIFIED"
WEAK = "WEAK"
PLAUSIBLE = "PLAUSIBLE"
VERIFIED = "VERIFIED"
STRONG = "STRONG"

EVENT_CONFIDENCE_ORDER: tuple[str, ...] = (NONE, UNVERIFIED, WEAK, PLAUSIBLE, VERIFIED, STRONG)

#: Confidence levels good enough to raise research priority.
CREDIBLE_EVENT = frozenset({PLAUSIBLE, VERIFIED, STRONG})

# --- token <-> event connection (section 16) ---------------------------------
CONNECTION_NONE = "NO_EVIDENCE"
CONNECTION_NAME_ONLY = "NAME_MATCH_ONLY"
CONNECTION_PLAUSIBLE = "PLAUSIBLE"
CONNECTION_STRONG = "STRONG"
CONNECTION_OFFICIAL = "OFFICIAL"

#: An official connection requires the event's own authoritative source to
#: publish the mint.  Nothing else may ever produce this value.
OFFICIAL_REQUIRES_PRIMARY_SOURCE = True

# --- event priority (section 18) ---------------------------------------------
LOW = "LOW"
NORMAL = "NORMAL"
HIGH = "HIGH"
BREAKING = "BREAKING"

PRIORITY_ORDER: tuple[str, ...] = (LOW, NORMAL, HIGH, BREAKING)

# --- manipulation markers ------------------------------------------------------
M_CIRCULAR_SOURCING = "CIRCULAR_SOURCING"
M_IMPERSONATION = "POSSIBLE_IMPERSONATION"
M_DUPLICATE_AGGREGATION = "DUPLICATE_AGGREGATOR_SPAM"
M_NO_PRIMARY_SOURCE = "NO_PRIMARY_SOURCE"
M_STALE = "STALE_EVENT"


@dataclass(frozen=True, slots=True)
class EventSource:
    """One publication of an event claim."""

    name: str
    url: str = ""
    published_at: int = 0
    is_primary: bool = False
    account_verified: bool = False
    tier: str = ""
    quotes_source: str = ""
    content_hash: str = ""

    @property
    def independent_of_primary(self) -> bool:
        """A source that merely quotes the primary is not a second confirmation."""

        return not self.is_primary and not self.quotes_source


@dataclass(frozen=True, slots=True)
class CatalystEvent:
    """A real-world event, graded independently of any token."""

    event_id: str
    headline: str
    detected_at: int
    occurred_at: int | None = None
    sources: tuple[EventSource, ...] = ()
    discussion_velocity: Decimal | None = None
    novelty: Decimal | None = None
    crypto_relevance: Decimal | None = None
    confidence: str = UNVERIFIED
    priority: str = NORMAL
    markers: tuple[str, ...] = field(default_factory=tuple)

    @property
    def primary_sources(self) -> tuple[EventSource, ...]:
        return tuple(item for item in self.sources if item.is_primary)

    @property
    def independent_confirmations(self) -> int:
        """Distinct non-primary sources that are not quoting the primary."""

        seen: set[str] = set()
        count = 0
        for item in self.sources:
            if not item.independent_of_primary:
                continue
            key = item.content_hash or item.name
            if key in seen:
                continue
            seen.add(key)
            count += 1
        return count

    @property
    def age_seconds_at(self) -> int | None:
        if self.occurred_at is None:
            return None
        return max(0, self.detected_at - self.occurred_at)

    @property
    def credible(self) -> bool:
        return self.confidence in CREDIBLE_EVENT


def assess_event(
    event: CatalystEvent,
    *,
    now: int,
    max_age_seconds: int = 3_600,
) -> CatalystEvent:
    """Grade an event's authenticity and importance from its sources.

    Defends against the specific failure modes the contract names: circular
    sourcing, aggregator spam, impersonation and stale re-posts.  A claim with
    no primary source and no independent confirmation never rises above WEAK.
    """

    from dataclasses import replace

    markers: list[str] = []
    primaries = event.primary_sources
    independent = event.independent_confirmations

    if not primaries:
        markers.append(M_NO_PRIMARY_SOURCE)
    if any(item.is_primary and not item.account_verified for item in event.sources):
        markers.append(M_IMPERSONATION)

    quoting = sum(1 for item in event.sources if item.quotes_source)
    if quoting and quoting >= max(1, len(event.sources) - 1) and independent == 0:
        markers.append(M_CIRCULAR_SOURCING)

    hashes = [item.content_hash for item in event.sources if item.content_hash]
    if hashes and len(set(hashes)) < len(hashes):
        markers.append(M_DUPLICATE_AGGREGATION)

    published = [item.published_at for item in event.sources if item.published_at]
    oldest = min(published) if published else event.occurred_at
    if oldest and now - oldest > max_age_seconds:
        markers.append(M_STALE)

    verified_primary = any(item.is_primary and item.account_verified for item in event.sources)
    if verified_primary and independent >= 2:
        confidence = STRONG
    elif verified_primary and independent >= 1:
        confidence = VERIFIED
    elif verified_primary or independent >= 2:
        confidence = PLAUSIBLE
    elif independent >= 1:
        confidence = WEAK
    elif event.sources:
        confidence = UNVERIFIED
    else:
        confidence = NONE

    if M_CIRCULAR_SOURCING in markers or M_IMPERSONATION in markers:
        confidence = _demote(confidence)
    if M_STALE in markers:
        confidence = _demote(confidence)

    return replace(
        event,
        confidence=confidence,
        priority=_priority(event, confidence=confidence, markers=tuple(markers)),
        markers=tuple(dict.fromkeys(markers)),
    )


def _demote(confidence: str) -> str:
    index = EVENT_CONFIDENCE_ORDER.index(confidence)
    return EVENT_CONFIDENCE_ORDER[max(0, index - 1)]


def _priority(event: CatalystEvent, *, confidence: str, markers: tuple[str, ...]) -> str:
    """Importance, not celebrity.  Credibility and novelty drive this."""

    if confidence in {NONE, UNVERIFIED} or M_STALE in markers:
        return LOW
    score = 0
    if confidence == STRONG:
        score += 3
    elif confidence == VERIFIED:
        score += 2
    elif confidence == PLAUSIBLE:
        score += 1
    if event.novelty is not None and event.novelty >= 70:
        score += 2
    if event.discussion_velocity is not None and event.discussion_velocity >= 70:
        score += 2
    if event.crypto_relevance is not None and event.crypto_relevance >= 60:
        score += 1
    age = event.age_seconds_at
    if age is not None and age <= 300:
        score += 1
    if score >= 7:
        return BREAKING
    if score >= 5:
        return HIGH
    if score >= 3:
        return NORMAL
    return LOW


@dataclass(frozen=True, slots=True)
class TokenEventLink:
    """How strongly a token is connected to an event — a separate question.

    A real event never upgrades this value.  Only evidence about *the token*
    does, and OFFICIAL requires the event's own authoritative source to have
    published the exact mint.
    """

    mint: str
    event_id: str
    connection: str = CONNECTION_NONE
    name_similarity: Decimal | None = None
    minted_after_event: bool | None = None
    seconds_after_event: int | None = None
    published_by_primary_source: bool = False
    official_channel_match: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def official(self) -> bool:
        return self.connection == CONNECTION_OFFICIAL

    @property
    def label(self) -> str:
        if self.official:
            return "OFFICIAL"
        return f"{self.connection.replace('_', ' ')} — NOT OFFICIAL"


def assess_token_link(
    *,
    mint: str,
    event: CatalystEvent,
    name_similarity: Decimal | None = None,
    minted_after_event: bool | None = None,
    seconds_after_event: int | None = None,
    published_by_primary_source: bool = False,
    official_channel_match: bool = False,
) -> TokenEventLink:
    """Grade the token↔event connection using token evidence only."""

    notes: list[str] = []
    if published_by_primary_source and official_channel_match:
        connection = CONNECTION_OFFICIAL
    elif minted_after_event and (name_similarity or ZERO) >= Decimal("70") and (
        seconds_after_event is not None and seconds_after_event <= 3_600
    ):
        connection = CONNECTION_PLAUSIBLE
        notes.append("minted shortly after the event with a closely matching name")
    elif (name_similarity or ZERO) >= Decimal("70"):
        connection = CONNECTION_NAME_ONLY
        notes.append("name match only — anyone can name a token after an event")
    elif minted_after_event:
        connection = CONNECTION_NAME_ONLY
        notes.append("timing only — no naming evidence")
    else:
        connection = CONNECTION_NONE

    if connection != CONNECTION_OFFICIAL:
        notes.append("no evidence this token is officially connected to the event")
    return TokenEventLink(
        mint=mint,
        event_id=event.event_id,
        connection=connection,
        name_similarity=name_similarity,
        minted_after_event=minted_after_event,
        seconds_after_event=seconds_after_event,
        published_by_primary_source=published_by_primary_source,
        official_channel_match=official_channel_match,
        notes=tuple(dict.fromkeys(notes)),
    )


# --- alert classes (sections 17, 20, 21) --------------------------------------
BREAKING_CATALYST = "BREAKING_CATALYST"
CATALYST_WATCH = "CATALYST_WATCH"
CONFLUENCE_WATCH = "CONFLUENCE_WATCH"
NO_ALERT = "NO_ALERT"


@dataclass(frozen=True, slots=True)
class ConfluenceInputs:
    """Everything the strongest research alert weighs."""

    event: CatalystEvent | None = None
    link: TokenEventLink | None = None
    token_age_seconds: int | None = None
    independent_notable_wallets: int = 0
    proven_early_wallets: int = 0
    earliest_notable_entry_market_cap_usd: Decimal | None = None
    current_market_cap_usd: Decimal | None = None
    independent_buyers_accelerating: bool = False
    liquidity_growing: bool = False
    organic_score: Decimal | None = None
    current_actionability: Decimal | None = None
    safety_status: str = "UNKNOWN"

    @property
    def move_since_earliest_notable_percent(self) -> Decimal | None:
        base = self.earliest_notable_entry_market_cap_usd
        if base is None or base <= 0 or self.current_market_cap_usd is None:
            return None
        return ((self.current_market_cap_usd - base) / base * HUNDRED).quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class CatalystAlert:
    """A research alert.  It never authorises a PAPER entry."""

    kind: str = NO_ALERT
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    ping: bool = False
    ping_reason: str = ""

    @property
    def entry_eligible(self) -> bool:
        """Structural guarantee: catalysts raise priority, never eligibility."""

        return False

    @property
    def alerts(self) -> bool:
        return self.kind != NO_ALERT


def classify_catalyst_alert(
    inputs: ConfluenceInputs,
    *,
    now: int = 0,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> CatalystAlert:
    """Decide between BREAKING CATALYST, CATALYST WATCH, CONFLUENCE WATCH.

    Safety is never a component of the decision to *show* something, and never
    a component of eligibility either — that stays with the PAPER engine.
    """

    event = inputs.event
    link = inputs.link
    if event is None or not event.credible:
        return CatalystAlert(kind=NO_ALERT, reasons=("no credible event",))

    warnings: list[str] = ["⚠ EVENT VERIFIED ≠ TOKEN VERIFIED"]
    if link is not None and not link.official:
        warnings.append(f"Token ↔ event: {link.label}")
    if inputs.safety_status != "PASS":
        warnings.append(f"Safety {inputs.safety_status} — research only")

    # Confluence: catalyst + young token + independent proven wallets + demand.
    move = inputs.move_since_earliest_notable_percent
    edge_left = move is None or move < config.max_move_since_signal_percent
    if (
        link is not None
        and link.connection in {CONNECTION_PLAUSIBLE, CONNECTION_STRONG, CONNECTION_OFFICIAL}
        and inputs.independent_notable_wallets >= 2
        and inputs.proven_early_wallets >= 1
        and inputs.independent_buyers_accelerating
        and edge_left
        and (inputs.current_actionability or ZERO) >= Decimal("55")
    ):
        reasons = [
            "real external catalyst detected",
            "token appeared shortly after the event",
            f"{inputs.proven_early_wallets} PROVEN_EARLY independent wallet(s) entered",
            "independent buyers accelerating",
        ]
        if inputs.liquidity_growing:
            reasons.append("liquidity increasing")
        if move is not None:
            reasons.append(f"only {move:+.1f}% since the earliest notable entry")
        return CatalystAlert(
            kind=CONFLUENCE_WATCH,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            ping=True,
            ping_reason="catalyst + early smart-money convergence",
        )

    # Catalyst watch: credible event + plausibly related fresh token + early
    # market confirmation, without waiting for deep safety evidence.
    if (
        link is not None
        and link.connection != CONNECTION_NONE
        and inputs.token_age_seconds is not None
        and inputs.token_age_seconds <= 3_600
        and (inputs.independent_buyers_accelerating or inputs.independent_notable_wallets >= 1)
    ):
        return CatalystAlert(
            kind=CATALYST_WATCH,
            reasons=(
                "credible event",
                "plausibly related fresh token",
                "early market confirmation",
            ),
            warnings=tuple(warnings),
            ping=event.priority in {HIGH, BREAKING},
            ping_reason="catalyst + fresh token with early confirmation",
        )

    # Breaking catalyst: the event itself is the news, tokens are secondary.
    if event.priority == BREAKING:
        return CatalystAlert(
            kind=BREAKING_CATALYST,
            reasons=("verified breaking event",),
            warnings=tuple(warnings),
            ping=True,
            ping_reason="verified breaking catalyst",
        )
    if event.priority == HIGH:
        return CatalystAlert(
            kind=BREAKING_CATALYST,
            reasons=("credible high-priority event",),
            warnings=tuple(warnings),
            ping=False,
            ping_reason="",
        )
    return CatalystAlert(kind=NO_ALERT, reasons=("event does not meet the alert bar",))


@dataclass(frozen=True, slots=True)
class LeadLagRecord:
    """Timestamps used to learn which source actually leads the move."""

    mint: str
    event_at: int | None = None
    social_at: int | None = None
    mint_created_at: int | None = None
    bot_seen_at: int | None = None
    fast_watch_at: int | None = None
    wallet_entry_at: int | None = None
    ping_at: int | None = None
    acceleration_at: int | None = None

    def gap(self, start: str, end: str) -> int | None:
        first = getattr(self, start, None)
        second = getattr(self, end, None)
        if first is None or second is None or second < first:
            return None
        return second - first

    @property
    def intervals(self) -> dict[str, int | None]:
        return {
            "event->mint": self.gap("event_at", "mint_created_at"),
            "social->mint": self.gap("social_at", "mint_created_at"),
            "mint->bot_seen": self.gap("mint_created_at", "bot_seen_at"),
            "bot_seen->ping": self.gap("bot_seen_at", "ping_at"),
            "wallet->ping": self.gap("wallet_entry_at", "ping_at"),
            "ping->acceleration": self.gap("ping_at", "acceleration_at"),
        }
