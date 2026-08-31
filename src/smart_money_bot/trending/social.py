"""Public social intelligence: velocity, independence, and honest source health.

Two rules shape this module.

**Never invent engagement (section 33).**  Likes, views and reposts are recorded
only when a source actually supplies them.  A missing count is ``None``, never
zero, and a surface that receives ``None`` prints "not available" rather than a
confident-looking `0`.

**Never show green because a class exists (section 34).**  A lane with no
configured source reports ``NO_SOURCE_CONFIGURED``.  A configured lane that has
produced nothing reports ``ACTIVE_NO_EVENTS``.  These are different problems and
an operator needs to be able to tell them apart at a glance.

Mentions are resolved against the *exact mint* wherever the source gives one.  A
name-only or ticker-only mention is kept, but it is labelled as unresolved and
never treated as evidence about a specific mint when a collision exists
(section 32).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")

# --- how confidently is this mention attached to *this* mint? ----------------
MATCH_EXACT_MINT = "EXACT_MINT"
MATCH_RESOLVED_UNIQUE = "RESOLVED_UNIQUE_NAME"
MATCH_AMBIGUOUS = "AMBIGUOUS_NAME"

# --- lane health (section 34) ------------------------------------------------
SOCIAL_NO_SOURCE = "NO_SOURCE_CONFIGURED"
SOCIAL_DISABLED = "DISABLED_BY_CONFIG"
SOCIAL_AUTH_MISSING = "AUTH_MISSING"
SOCIAL_RATE_LIMITED = "RATE_LIMITED"
SOCIAL_DEGRADED = "PROVIDER_DEGRADED"
SOCIAL_ACTIVE_NO_EVENTS = "ACTIVE_NO_EVENTS"
SOCIAL_ACTIVE = "ACTIVE"

SOCIAL_HEALTH_STATES: tuple[str, ...] = (
    SOCIAL_NO_SOURCE,
    SOCIAL_DISABLED,
    SOCIAL_AUTH_MISSING,
    SOCIAL_RATE_LIMITED,
    SOCIAL_DEGRADED,
    SOCIAL_ACTIVE_NO_EVENTS,
    SOCIAL_ACTIVE,
)


@dataclass(frozen=True, slots=True)
class SocialMention:
    """One public post about a token.  Public chatter only — never private data.

    The operator's wording matters here and is kept in the product: this is
    ``PUBLIC EARLY CHATTER``, not "insider info" (section 23).  Nothing in this
    pipeline reads private messages, leaked material or any non-public source.
    """

    mint: str
    author: str
    posted_at: int
    source: str = ""
    text: str = ""
    match: str = MATCH_EXACT_MINT
    #: Only set when the source supplies it.  ``None`` means unavailable.
    likes: int | None = None
    views: int | None = None
    reposts: int | None = None
    is_project_account: bool = False

    @property
    def resolved_to_mint(self) -> bool:
        return self.match in {MATCH_EXACT_MINT, MATCH_RESOLVED_UNIQUE}


@dataclass(frozen=True, slots=True)
class SocialVelocity:
    """How fast independent public attention is arriving."""

    mint: str
    window_seconds: int = 0
    mentions: int = 0
    independent_authors: int = 0
    new_authors: int = 0
    mentions_per_minute: Decimal = ZERO
    ambiguous_mentions: int = 0
    project_account_mentions: int = 0
    #: ``None`` when no source supplied engagement — never a fabricated zero.
    total_likes: int | None = None
    total_views: int | None = None

    @property
    def accelerating(self) -> bool:
        """Independent people, arriving quickly.  One loud account is not this."""

        return self.independent_authors >= 3 and self.mentions_per_minute >= Decimal("1")

    @property
    def organic_ratio(self) -> Decimal | None:
        """Fraction of mentions that are not the project talking about itself."""

        if self.mentions <= 0:
            return None
        organic = self.mentions - self.project_account_mentions
        return (Decimal(organic) / Decimal(self.mentions)).quantize(Decimal("0.01"))

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "window_seconds": self.window_seconds,
            "mentions": self.mentions,
            "independent_authors": self.independent_authors,
            "new_authors": self.new_authors,
            "mentions_per_minute": str(self.mentions_per_minute),
            "ambiguous_mentions": self.ambiguous_mentions,
            "project_account_mentions": self.project_account_mentions,
            "total_likes": self.total_likes,
            "total_views": self.total_views,
            "accelerating": self.accelerating,
            "organic_ratio": None if self.organic_ratio is None else str(self.organic_ratio),
        }


def measure_social_velocity(
    mint: str,
    mentions: Sequence[SocialMention],
    *,
    now: int,
    window_seconds: int = 900,
    known_authors: frozenset[str] = frozenset(),
) -> SocialVelocity:
    """Count independent public voices in a bounded window, for one exact mint."""

    own = [
        mention
        for mention in mentions
        if mention.mint == mint and 0 <= now - mention.posted_at <= window_seconds
    ]
    if not own:
        return SocialVelocity(mint=mint, window_seconds=window_seconds)

    # Only mentions actually resolved to this mint count as evidence about it.
    resolved = [mention for mention in own if mention.resolved_to_mint]
    authors = {mention.author for mention in resolved if not mention.is_project_account}
    span = max(1, min(window_seconds, now - min(mention.posted_at for mention in own)))

    likes = [mention.likes for mention in resolved if mention.likes is not None]
    views = [mention.views for mention in resolved if mention.views is not None]

    return SocialVelocity(
        mint=mint,
        window_seconds=window_seconds,
        mentions=len(resolved),
        independent_authors=len(authors),
        new_authors=len(authors - known_authors),
        mentions_per_minute=(
            Decimal(len(resolved)) * Decimal(60) / Decimal(span)
        ).quantize(Decimal("0.01")),
        ambiguous_mentions=sum(1 for mention in own if mention.match == MATCH_AMBIGUOUS),
        project_account_mentions=sum(1 for mention in resolved if mention.is_project_account),
        total_likes=sum(likes) if likes else None,
        total_views=sum(views) if views else None,
    )


@dataclass(frozen=True, slots=True)
class SocialSourceHealth:
    state: str = SOCIAL_NO_SOURCE
    provider: str = ""
    configured: bool = False
    enabled: bool = False
    events: int = 0
    last_event_at: int | None = None
    last_error: str = ""

    @property
    def healthy(self) -> bool:
        return self.state == SOCIAL_ACTIVE

    def to_json(self) -> dict[str, object]:
        return {
            "state": self.state,
            "provider": self.provider,
            "configured": self.configured,
            "enabled": self.enabled,
            "events": self.events,
            "last_event_at": self.last_event_at,
            "last_error": self.last_error,
        }


def assess_social_health(
    *,
    provider: str,
    configured: bool,
    enabled: bool,
    events: int,
    last_event_at: int | None = None,
    last_error: str = "",
    authenticated: bool = True,
) -> SocialSourceHealth:
    """Tell the truth about the social lane (section 34)."""

    def build(state: str) -> SocialSourceHealth:
        return SocialSourceHealth(
            state=state,
            provider=provider,
            configured=configured,
            enabled=enabled,
            events=events,
            last_event_at=last_event_at,
            last_error=last_error,
        )

    if not provider or not configured:
        return build(SOCIAL_NO_SOURCE)
    if not authenticated:
        return build(SOCIAL_AUTH_MISSING)
    if not enabled:
        return build(SOCIAL_DISABLED)
    folded = last_error.casefold()
    if "rate" in folded or "429" in folded:
        return build(SOCIAL_RATE_LIMITED)
    if last_error:
        return build(SOCIAL_DEGRADED)
    if events <= 0:
        return build(SOCIAL_ACTIVE_NO_EVENTS)
    return build(SOCIAL_ACTIVE)
