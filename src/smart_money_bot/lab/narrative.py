"""Real-world stories, and which exact mint (if any) actually belongs to one.

Sections 18-27 and 34-37.

The invariant everything here is built around is one line long:

    **MINT IS IDENTITY.**

Three tokens can all be called "Justice for HeeHaw" with the same ticker and the
same picture.  They are three different assets, and evidence earned by one must
never be spent by another.  So a narrative is modelled as its own durable
object, tokens are modelled separately, and the *link between them* is a graded,
directional claim that each mint has to earn on its own.

Direction is the part that carries most of the signal (section 23).  A token
pointing at a story proves nothing — anyone can paste a URL into token metadata.
A story source pointing at an exact mint is much harder to fake, because it
requires control of the source.  The two are stored as different things and
weighted differently, and only the second can ever reach ``OFFICIAL``.

Nothing in this module performs I/O, scrapes anything, or authenticates
anywhere.  It grades evidence a caller has already gathered from legitimate
sources, and it never invents engagement numbers it cannot see (section 32).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal("0")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")

# --- narrative -> token relationship grades (section 22) ---------------------
REL_UNRELATED = "UNRELATED"
REL_NAME_ONLY = "NAME_ONLY"
REL_WEAK = "WEAK"
REL_PLAUSIBLE = "PLAUSIBLE"
REL_STRONG = "STRONG"
REL_DIRECTLY_LINKED = "DIRECTLY_LINKED"
REL_OFFICIAL = "OFFICIAL"

RELATIONSHIPS: tuple[str, ...] = (
    REL_UNRELATED,
    REL_NAME_ONLY,
    REL_WEAK,
    REL_PLAUSIBLE,
    REL_STRONG,
    REL_DIRECTLY_LINKED,
    REL_OFFICIAL,
)

#: Rank for comparing two candidates for the same narrative.
_RELATIONSHIP_RANK: dict[str, int] = {
    name: index for index, name in enumerate(RELATIONSHIPS)
}

#: Relationships strong enough to let a token borrow the narrative's context on
#: a card.  Everything below this is shown as a coincidence of naming.
INHERITS_STORY: frozenset[str] = frozenset(
    {REL_STRONG, REL_DIRECTLY_LINKED, REL_OFFICIAL}
)

# --- link direction (section 23) ---------------------------------------------
#: The token's own metadata points at the story.  Cheap to fake.
DIR_TOKEN_TO_STORY = "TOKEN_TO_STORY"
#: A story source points at this exact mint.  Requires control of the source.
DIR_STORY_TO_TOKEN = "STORY_TO_TOKEN"
#: Both directions agree.
DIR_MUTUAL = "MUTUAL"

DIRECTIONS: tuple[str, ...] = (DIR_TOKEN_TO_STORY, DIR_STORY_TO_TOKEN, DIR_MUTUAL)

# --- story virality (section 33) ---------------------------------------------
VIRALITY_NONE = "NONE"
VIRALITY_EMERGING = "EMERGING"
VIRALITY_ACCELERATING = "ACCELERATING"
VIRALITY_STRONG = "STRONG"
VIRALITY_VIRAL = "VIRAL"
VIRALITY_DECLINING = "DECLINING"
VIRALITY_EXHAUSTED = "EXHAUSTED"

VIRALITY_STATES: tuple[str, ...] = (
    VIRALITY_NONE,
    VIRALITY_EMERGING,
    VIRALITY_ACCELERATING,
    VIRALITY_STRONG,
    VIRALITY_VIRAL,
    VIRALITY_DECLINING,
    VIRALITY_EXHAUSTED,
)

#: Virality states worth opening a launch watch for (section 34).
WATCHABLE: frozenset[str] = frozenset(
    {VIRALITY_EMERGING, VIRALITY_ACCELERATING, VIRALITY_STRONG, VIRALITY_VIRAL}
)

# --- warnings ----------------------------------------------------------------
W_TOKEN_PREDATES_STORY = "TOKEN_PREDATES_STORY"
W_NAME_COLLISION = "NAME_COLLISION"
W_METADATA_ONLY = "METADATA_ONLY_EVIDENCE"
W_NO_PRIMARY_SOURCE = "NO_PRIMARY_SOURCE"

#: A mint, as it appears in a URL or free text.  Base58, 32-44 characters.
_MINT_PATTERN = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")

#: Public explorers and launchpads whose URLs carry an exact mint.
_MINT_URL_HINTS: tuple[str, ...] = (
    "pump.fun/coin/",
    "dexscreener.com/solana/",
    "solscan.io/token/",
    "birdeye.so/token/",
    "jup.ag/swap/",
    "solana.fm/address/",
)


@dataclass(frozen=True, slots=True)
class StorySource:
    """One legitimate observation that a story exists.

    ``is_primary`` means this is the originating account, page or outlet rather
    than someone repeating it.  ``links_exact_mint`` is the strong signal: this
    source itself named a specific mint.
    """

    name: str
    url: str = ""
    observed_at: int = 0
    published_at: int | None = None
    is_primary: bool = False
    independent: bool = True
    links_exact_mint: str = ""
    #: Engagement is recorded only when a source genuinely exposes it.  It is
    #: never estimated (section 32).
    engagement: int | None = None

    @property
    def domain(self) -> str:
        cleaned = self.url.split("//")[-1]
        return cleaned.split("/")[0].casefold() if cleaned else ""


@dataclass(frozen=True, slots=True)
class NarrativeEntity:
    """A durable real-world story, independent of any token (section 21)."""

    narrative_id: str
    title: str
    aliases: tuple[str, ...] = field(default_factory=tuple)
    keywords: tuple[str, ...] = field(default_factory=tuple)
    entities: tuple[str, ...] = field(default_factory=tuple)
    first_seen_at: int = 0
    last_seen_at: int = 0
    sources: tuple[StorySource, ...] = field(default_factory=tuple)

    @property
    def primary_sources(self) -> tuple[StorySource, ...]:
        return tuple(item for item in self.sources if item.is_primary)

    @property
    def independent_domains(self) -> int:
        return len({item.domain for item in self.sources if item.independent and item.domain})

    @property
    def linked_mints(self) -> tuple[str, ...]:
        """Exact mints that a story source itself named (section 24)."""

        return tuple(
            dict.fromkeys(item.links_exact_mint for item in self.sources if item.links_exact_mint)
        )

    @property
    def matchable_terms(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                term.casefold()
                for term in (self.title, *self.aliases, *self.keywords, *self.entities)
                if term
            )
        )


def narrative_id_for(title: str) -> str:
    """A stable id from the canonical title, so a restart rejoins the same story."""

    normalized = " ".join(title.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def assess_virality(
    narrative: NarrativeEntity,
    *,
    now: int,
    recent_window_seconds: int = 3_600,
) -> str:
    """Grade a story from evidence actually in hand (section 33).

    Deliberately conservative: with one source and no corroboration a story is
    ``EMERGING`` at best.  Nothing here estimates reach it cannot observe.
    """

    if not narrative.sources:
        return VIRALITY_NONE

    independent = narrative.independent_domains
    recent = [
        item
        for item in narrative.sources
        if item.observed_at and now - item.observed_at <= recent_window_seconds
    ]
    age = max(0, now - narrative.first_seen_at) if narrative.first_seen_at else 0

    if age > 86_400 and not recent:
        return VIRALITY_EXHAUSTED
    if not recent and age > recent_window_seconds * 3:
        return VIRALITY_DECLINING

    velocity = len(recent)
    if independent >= 5 and velocity >= 5:
        return VIRALITY_VIRAL
    if independent >= 3 and velocity >= 3:
        return VIRALITY_STRONG
    if independent >= 2 and velocity >= 2:
        return VIRALITY_ACCELERATING
    return VIRALITY_EMERGING


@dataclass(frozen=True, slots=True)
class TokenIdentityClaim:
    """What one exact mint says about itself.  All of it can be copied."""

    mint: str
    name: str = ""
    symbol: str = ""
    description: str = ""
    website_url: str = ""
    x_handle: str = ""
    telegram_url: str = ""
    discord_url: str = ""
    image_url: str = ""
    extra_urls: tuple[str, ...] = field(default_factory=tuple)
    created_at: int | None = None
    creator: str = ""

    @property
    def urls(self) -> tuple[str, ...]:
        return tuple(
            item
            for item in (
                self.website_url,
                self.telegram_url,
                self.discord_url,
                *self.extra_urls,
            )
            if item
        )

    @property
    def text(self) -> str:
        return " ".join(
            part for part in (self.name, self.symbol, self.description) if part
        ).casefold()


@dataclass(frozen=True, slots=True)
class NarrativeLink:
    """One graded, directional claim that a mint belongs to a narrative."""

    narrative_id: str
    mint: str
    relationship: str = REL_UNRELATED
    direction: str = DIR_TOKEN_TO_STORY
    confidence: Decimal = ZERO
    matched_terms: tuple[str, ...] = field(default_factory=tuple)
    shared_urls: tuple[str, ...] = field(default_factory=tuple)
    seconds_after_story: int | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def inherits_story(self) -> bool:
        """Whether this mint may show the narrative as its own (section 26)."""

        return self.relationship in INHERITS_STORY

    @property
    def rank(self) -> int:
        return _RELATIONSHIP_RANK.get(self.relationship, 0)


def extract_mints(text: str) -> tuple[str, ...]:
    """Pull exact mints out of URLs and free text (section 24).

    Explorer and launchpad URLs are preferred, because a mint sitting in a
    ``pump.fun/coin/<mint>`` path is unambiguous, whereas a bare base58 run in
    prose might be any address at all.
    """

    if not text:
        return ()
    found: list[str] = []
    lowered = text.casefold()
    for hint in _MINT_URL_HINTS:
        start = 0
        while True:
            index = lowered.find(hint, start)
            if index < 0:
                break
            tail = text[index + len(hint) :]
            match = _MINT_PATTERN.match(tail)
            if match:
                found.append(match.group(0))
            start = index + len(hint)
    for match in _MINT_PATTERN.finditer(text):
        candidate = match.group(0)
        if candidate not in found:
            found.append(candidate)
    return tuple(dict.fromkeys(found))


def _normalize_url(url: str) -> str:
    cleaned = url.strip().casefold()
    for prefix in ("https://", "http://", "www."):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    return cleaned.rstrip("/")


def assess_narrative_link(
    narrative: NarrativeEntity,
    token: TokenIdentityClaim,
    *,
    now: int,
    story_first_seen_at: int | None = None,
) -> NarrativeLink:
    """Grade how strongly one exact mint belongs to one narrative.

    The grades escalate only on evidence a scammer would find progressively
    harder to fake: matching words (trivial), a shared URL (a copy-paste), and
    finally a story source that names this exact mint (requires the source).

    Hard ceiling: evidence that only the *token* asserts can never exceed
    ``PLAUSIBLE``.  Reaching ``STRONG`` or above requires the story side to
    point back, because that is the part a copycat cannot manufacture.
    """

    reasons: list[str] = []
    warnings: list[str] = []
    matched: list[str] = []

    text = token.text
    for term in narrative.matchable_terms:
        if term and term in text:
            matched.append(term)

    token_urls = {_normalize_url(item) for item in token.urls if item}
    source_urls = {_normalize_url(item.url) for item in narrative.sources if item.url}
    shared = tuple(sorted(token_urls & source_urls))

    # The strong direction: a story source named this exact mint.
    story_names_mint = token.mint in narrative.linked_mints
    # The weak direction: the token's own metadata points at a story source.
    token_names_story = bool(shared)

    story_at = story_first_seen_at if story_first_seen_at is not None else narrative.first_seen_at
    delta = (
        token.created_at - story_at
        if token.created_at is not None and story_at
        else None
    )
    if delta is not None and delta < 0:
        # Section 27: not automatically fake, but genuinely meaningful.
        warnings.append(W_TOKEN_PREDATES_STORY)

    if not narrative.primary_sources:
        warnings.append(W_NO_PRIMARY_SOURCE)

    if story_names_mint and token_names_story:
        relationship = REL_DIRECTLY_LINKED
        direction = DIR_MUTUAL
        confidence = Decimal("92")
        reasons.append("a story source names this exact mint and the token links back")
    elif story_names_mint:
        relationship = REL_DIRECTLY_LINKED
        direction = DIR_STORY_TO_TOKEN
        confidence = Decimal("85")
        reasons.append("a story source names this exact mint")
    elif token_names_story and matched:
        relationship = REL_PLAUSIBLE
        direction = DIR_TOKEN_TO_STORY
        confidence = Decimal("55")
        reasons.append("the token links a story source and matches its wording")
        warnings.append(W_METADATA_ONLY)
    elif token_names_story:
        relationship = REL_WEAK
        direction = DIR_TOKEN_TO_STORY
        confidence = Decimal("35")
        reasons.append("the token links a story source but shares no wording")
        warnings.append(W_METADATA_ONLY)
    elif matched:
        relationship = REL_NAME_ONLY
        direction = DIR_TOKEN_TO_STORY
        confidence = Decimal("18")
        reasons.append("the name matches and nothing else does")
    else:
        return NarrativeLink(
            narrative_id=narrative.narrative_id,
            mint=token.mint,
            relationship=REL_UNRELATED,
            direction=DIR_TOKEN_TO_STORY,
            confidence=ZERO,
            warnings=tuple(warnings),
        )

    # A token created shortly *after* a story started spreading, carrying that
    # story's specific wording, is stronger contextual evidence (section 27) —
    # but it is still only the token's own claim about itself.
    if (
        delta is not None
        and 0 <= delta <= 86_400
        and matched
        and relationship in {REL_PLAUSIBLE, REL_WEAK}
    ):
        relationship = REL_PLAUSIBLE
        confidence = min(Decimal("70"), confidence + Decimal("20"))
        reasons.append(f"launched {delta}s after the story began spreading, with its wording")

    # Section 23/26: metadata can be copied.  A link the *token* asserts about
    # itself can never reach the band that lets a mint wear the narrative's
    # credibility — otherwise pasting a campaign URL would be enough to
    # impersonate the real thing, which is exactly the attack.
    if direction == DIR_TOKEN_TO_STORY and relationship in INHERITS_STORY:
        relationship = REL_PLAUSIBLE
        confidence = min(confidence, Decimal("70"))
        warnings.append(W_METADATA_ONLY)
        reasons.append(
            "capped at PLAUSIBLE: only the token claims this link, and metadata can be copied"
        )

    if W_TOKEN_PREDATES_STORY in warnings and relationship not in {
        REL_DIRECTLY_LINKED,
        REL_OFFICIAL,
    }:
        confidence = max(ZERO, confidence - Decimal("15"))
        reasons.append("the token existed before the story did")

    return NarrativeLink(
        narrative_id=narrative.narrative_id,
        mint=token.mint,
        relationship=relationship,
        direction=direction,
        confidence=confidence.quantize(CENT),
        matched_terms=tuple(dict.fromkeys(matched))[:8],
        shared_urls=shared,
        seconds_after_story=delta,
        reasons=tuple(reasons),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def mark_official(link: NarrativeLink, *, authority: str) -> NarrativeLink:
    """Promote a link to OFFICIAL, which only real authority may do (section 22).

    A community token is never official just because it is popular or first.
    The caller must name the authority — an authenticated campaign source, an
    operator-verified statement — and that name is persisted with the claim.
    """

    from dataclasses import replace

    if not authority.strip():
        raise ValueError("OFFICIAL requires a named authority")
    if link.direction == DIR_TOKEN_TO_STORY:
        # Token-side metadata can be copied, so it can never make a token
        # official on its own (section 23).
        raise ValueError("token-to-story evidence can never establish OFFICIAL")
    return replace(
        link,
        relationship=REL_OFFICIAL,
        confidence=Decimal("99"),
        reasons=(*link.reasons, f"declared official by {authority}"),
    )


@dataclass(frozen=True, slots=True)
class CollisionGroup:
    """Every mint claiming one narrative, ranked by what it actually proved."""

    narrative_id: str
    title: str
    links: tuple[NarrativeLink, ...] = field(default_factory=tuple)

    @property
    def candidates(self) -> int:
        return len(self.links)

    @property
    def ranked(self) -> tuple[NarrativeLink, ...]:
        return tuple(
            sorted(self.links, key=lambda item: (item.rank, item.confidence), reverse=True)
        )

    @property
    def strongest(self) -> NarrativeLink | None:
        ranked = self.ranked
        return ranked[0] if ranked else None

    @property
    def has_collision(self) -> bool:
        return self.candidates > 1

    @property
    def contested(self) -> bool:
        """Two or more candidates with an equally good claim.

        When nothing separates them, the honest answer is to show the operator
        the collision rather than pick one.
        """

        ranked = self.ranked
        if len(ranked) < 2:
            return False
        return ranked[0].rank == ranked[1].rank and ranked[0].rank >= _RELATIONSHIP_RANK[
            REL_PLAUSIBLE
        ]


def build_collision_group(
    narrative: NarrativeEntity,
    links: Sequence[NarrativeLink],
) -> CollisionGroup:
    """Group every mint claiming this narrative (sections 25, 26).

    Unrelated mints are dropped; everything else is kept *with its own grade*,
    so a card can show "3 other tokens use this name" without any of them
    inheriting the real story's credibility.
    """

    return CollisionGroup(
        narrative_id=narrative.narrative_id,
        title=narrative.title,
        links=tuple(
            item
            for item in links
            if item.relationship != REL_UNRELATED
            and item.narrative_id == narrative.narrative_id
        ),
    )


@dataclass(frozen=True, slots=True)
class LaunchWatch:
    """A story worth watching for a token that does not exist yet (section 34)."""

    narrative_id: str
    title: str
    opened_at: int
    virality: str = VIRALITY_EMERGING
    terms: tuple[str, ...] = field(default_factory=tuple)
    expires_at: int = 0
    matched_mints: tuple[str, ...] = field(default_factory=tuple)

    def active(self, *, now: int) -> bool:
        return not self.expires_at or now < self.expires_at

    def matches(self, token: TokenIdentityClaim) -> bool:
        text = token.text
        return any(term and term in text for term in self.terms)


def open_launch_watch(
    narrative: NarrativeEntity,
    *,
    now: int,
    virality: str,
    ttl_seconds: int = 86_400,
) -> LaunchWatch | None:
    """Start watching launches for a story that has begun to spread.

    This is what makes a story-first discovery possible at all: by the time a
    matching token exists, the terms to match it against are already loaded.
    """

    if virality not in WATCHABLE:
        return None
    return LaunchWatch(
        narrative_id=narrative.narrative_id,
        title=narrative.title,
        opened_at=now,
        virality=virality,
        terms=narrative.matchable_terms,
        expires_at=now + ttl_seconds,
    )


def story_lead_seconds(narrative: NarrativeEntity, token: TokenIdentityClaim) -> int | None:
    """How long the story existed before the token did.

    A positive number is the headline on a story-runner card: "story detected
    47m before token".
    """

    if not narrative.first_seen_at or token.created_at is None:
        return None
    return token.created_at - narrative.first_seen_at
