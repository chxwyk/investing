"""The token's own About section: summarised, classified, and never believed.

A project description is *marketing written by the person who benefits from you
buying*.  It is evidence about what the token claims, and nothing else.  This
module therefore does exactly two jobs:

1.  Summarise the About text into something an operator can read in three
    seconds, instead of dumping raw promotional copy onto a card (section 16).
2.  Keep the claim and the corroboration in two separate boxes (section 17), so
    a card can never print developer copy in a way that reads as verified.

The validation half (section 18) answers a narrow, checkable question: does the
project this token names actually mention *this exact mint*?  A project that
exists, is real, is famous, and has never heard of the token is the single most
common trap in this category, and "the project is real" is not evidence that
the token is connected to it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# --- claim categories (section 18) -------------------------------------------
CLAIM_AI = "AI"
CLAIM_AGENT = "AI_AGENT"
CLAIM_PRODUCT = "PRODUCT"
CLAIM_PROTOCOL = "PROTOCOL"
CLAIM_APP = "APP"
CLAIM_GAME = "GAME"
CLAIM_ROBOT = "ROBOT"
CLAIM_COMPANY = "COMPANY"
CLAIM_CREATOR = "CREATOR_PROJECT"
CLAIM_MEME = "MEME"
CLAIM_COMMUNITY = "COMMUNITY"
CLAIM_NONE = "NO_CLAIM"

CLAIM_CATEGORIES: tuple[str, ...] = (
    CLAIM_AI,
    CLAIM_AGENT,
    CLAIM_PRODUCT,
    CLAIM_PROTOCOL,
    CLAIM_APP,
    CLAIM_GAME,
    CLAIM_ROBOT,
    CLAIM_COMPANY,
    CLAIM_CREATOR,
    CLAIM_MEME,
    CLAIM_COMMUNITY,
    CLAIM_NONE,
)

#: Claims that assert an external, checkable thing exists.  These are the ones
#: worth validating, because they can be wrong in a way that costs money.
CHECKABLE_CLAIMS: frozenset[str] = frozenset(
    {CLAIM_AI, CLAIM_AGENT, CLAIM_PRODUCT, CLAIM_PROTOCOL, CLAIM_APP, CLAIM_GAME,
     CLAIM_ROBOT, CLAIM_COMPANY, CLAIM_CREATOR}
)

_CLAIM_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (CLAIM_AGENT, ("ai agent", "autonomous agent", "agentic", "swarm agent")),
    (CLAIM_AI, ("artificial intelligence", "machine learning", "neural", "llm", " ai ",
                "ai-powered", "ai powered", "quantum ai", "gpt")),
    (CLAIM_ROBOT, ("robot", "robotics", "humanoid", "android")),
    (CLAIM_GAME, ("game", "gaming", "play-to-earn", "p2e", "metaverse")),
    (CLAIM_PROTOCOL, ("protocol", "defi", "staking", "liquid staking", "perp", "dex ")),
    (CLAIM_APP, ("app", "platform", "dashboard", "terminal", "bot ")),
    (CLAIM_PRODUCT, ("product", "hardware", "device", "launching soon", "beta")),
    (CLAIM_COMPANY, ("company", "corporation", "inc.", "ltd", "startup", "partnership",
                     "backed by", "partnered with")),
    (CLAIM_CREATOR, ("creator", "youtuber", "streamer", "artist", "official token of")),
    (CLAIM_MEME, ("meme", "memecoin", "for the culture", "just a dog", "pepe", "wif")),
    (CLAIM_COMMUNITY, ("community", "cto", "community takeover", "dao")),
)

#: Phrases that assert an official relationship.  These raise the bar for
#: corroboration rather than lowering it — an unbacked "official" claim is a
#: bigger red flag than no claim at all.
_OFFICIAL_PHRASES: tuple[str, ...] = (
    "official token",
    "official coin",
    "officially partnered",
    "in partnership with",
    "backed by",
    "endorsed by",
    "owned by",
    "built by the team at",
)

_SOCIAL_HOSTS: dict[str, str] = {
    "x.com": "x",
    "twitter.com": "x",
    "t.me": "telegram",
    "telegram.me": "telegram",
    "discord.gg": "discord",
    "discord.com": "discord",
    "github.com": "github",
    "youtube.com": "youtube",
    "tiktok.com": "tiktok",
    "instagram.com": "instagram",
}

#: Aggregators and chart sites are not the project's own website.
_NON_PROJECT_HOSTS: frozenset[str] = frozenset(
    {
        "dexscreener.com",
        "dextools.io",
        "birdeye.so",
        "pump.fun",
        "fomo.family",
        "fomo.biz",
        "solscan.io",
        "solana.fm",
        "jup.ag",
        "raydium.io",
    }
)

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"https?://[^\s)>\]]+", re.IGNORECASE)


# --- external corroboration states (section 17) ------------------------------
EXTERNAL_SUPPORTED = "SUPPORTED"
EXTERNAL_UNVERIFIED = "UNVERIFIED"
EXTERNAL_CONTRADICTED = "CONTRADICTED"
EXTERNAL_NOT_APPLICABLE = "NOT_APPLICABLE"

#: How the token relates to the project it names.
LINK_CONFIRMED_OFFICIAL = "CONFIRMED_OFFICIAL"
LINK_COMMUNITY_MADE = "COMMUNITY_MADE"
LINK_UNVERIFIED = "UNVERIFIED"
LINK_CONTRADICTED = "CONTRADICTED"
LINK_NO_CLAIM = "NO_CLAIM"


@dataclass(frozen=True, slots=True)
class ProjectLink:
    kind: str
    url: str
    host: str


@dataclass(frozen=True, slots=True)
class AboutSummary:
    """A short, honest reading of the token's own description."""

    mint: str
    summary: str = ""
    claims: tuple[str, ...] = ()
    claimed_entities: tuple[str, ...] = ()
    links: tuple[ProjectLink, ...] = ()
    website: str = ""
    has_official_claim: bool = False
    raw_length: int = 0

    @property
    def primary_claim(self) -> str:
        return self.claims[0] if self.claims else CLAIM_NONE

    @property
    def checkable(self) -> bool:
        return any(claim in CHECKABLE_CLAIMS for claim in self.claims)

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "summary": self.summary,
            "claims": list(self.claims),
            "claimed_entities": list(self.claimed_entities),
            "links": [
                {"kind": link.kind, "url": link.url, "host": link.host}
                for link in self.links
            ],
            "website": self.website,
            "has_official_claim": self.has_official_claim,
            "raw_length": self.raw_length,
        }


def _clean(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text or "").strip()


def _extract_entities(text: str) -> tuple[str, ...]:
    """Capitalised multi-word names the About text asserts a relationship with.

    Deliberately conservative: we only pull names out of an explicit relationship
    phrase ("partnered with X", "official token of X"), because a general
    capitalised-word sweep produces noise that then gets "validated" and printed.
    """

    entities: list[str] = []
    pattern = re.compile(
        r"(?:official (?:token|coin) of|partnered with|in partnership with|backed by|"
        r"endorsed by|built by the team at|powered by|connected (?:to|with))\s+"
        r"([A-Z][\w.&'-]*(?:\s+[A-Z][\w.&'-]*){0,3})"
    )
    for match in pattern.finditer(text or ""):
        name = _clean(match.group(1))
        if name and name.casefold() not in {item.casefold() for item in entities}:
            entities.append(name)
    return tuple(entities[:5])


def parse_about(
    mint: str,
    description: str | None,
    *,
    website: str | None = None,
    links: tuple[str, ...] = (),
    max_summary_chars: int = 240,
) -> AboutSummary:
    """Summarise the About text.  Never dump raw promotional copy (section 16)."""

    raw = description or ""
    text = _clean(raw)
    folded = f" {text.casefold()} "

    claims: list[str] = []
    for category, terms in _CLAIM_TERMS:
        if any(term in folded for term in terms) and category not in claims:
            claims.append(category)

    collected: list[ProjectLink] = []
    seen: set[str] = set()
    for url in (*(_URL_RE.findall(raw)), *links, *( (website,) if website else () )):
        candidate = (url or "").strip().rstrip(".,);")
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            host = (urlsplit(candidate).hostname or "").casefold()
        except ValueError:
            continue
        if not host:
            continue
        host = host[4:] if host.startswith("www.") else host
        kind = _SOCIAL_HOSTS.get(host)
        if kind is None:
            kind = "aggregator" if host in _NON_PROJECT_HOSTS else "website"
        collected.append(ProjectLink(kind=kind, url=candidate, host=host))

    project_site = next(
        (link.url for link in collected if link.kind == "website"),
        "",
    )

    # Summary: the first couple of sentences, trimmed — enough to know what is
    # being claimed, short enough that nobody reads it as a prospectus.
    sentences = [item for item in _SENTENCE_RE.split(text) if item]
    summary = ""
    for sentence in sentences:
        if len(summary) + len(sentence) + 1 > max_summary_chars:
            break
        summary = f"{summary} {sentence}".strip()
    if not summary:
        summary = text[:max_summary_chars]
    if len(text) > len(summary):
        summary = f"{summary.rstrip('. ')}…"

    return AboutSummary(
        mint=mint,
        summary=summary,
        claims=tuple(claims),
        claimed_entities=_extract_entities(text),
        links=tuple(collected),
        website=project_site,
        has_official_claim=any(phrase in folded for phrase in _OFFICIAL_PHRASES),
        raw_length=len(text),
    )


@dataclass(frozen=True, slots=True)
class ProjectValidation:
    """Does the named project exist, and does it know this mint exists?"""

    mint: str
    claim: str = CLAIM_NONE
    external_state: str = EXTERNAL_NOT_APPLICABLE
    token_link: str = LINK_NO_CLAIM
    project_exists: bool | None = None
    mentions_exact_mint: bool = False
    sources_checked: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def supported(self) -> bool:
        return self.external_state == EXTERNAL_SUPPORTED and self.mentions_exact_mint

    def operator_lines(self) -> tuple[str, str]:
        """The two-box rendering claims and facts must always keep (section 17)."""

        claim_line = "none" if self.claim == CLAIM_NONE else self.claim
        if self.external_state == EXTERNAL_NOT_APPLICABLE:
            return claim_line, "no external claim to check"
        if self.external_state == EXTERNAL_CONTRADICTED:
            return claim_line, "CONTRADICTED — the project denies or excludes this token"
        if self.mentions_exact_mint:
            return claim_line, "SUPPORTED — the project publishes this exact mint"
        if self.project_exists:
            return (
                claim_line,
                "UNVERIFIED — the project exists but does not mention this mint",
            )
        return claim_line, "UNVERIFIED — no external confirmation found"

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "claim": self.claim,
            "external_state": self.external_state,
            "token_link": self.token_link,
            "project_exists": self.project_exists,
            "mentions_exact_mint": self.mentions_exact_mint,
            "sources_checked": list(self.sources_checked),
            "notes": list(self.notes),
        }


def validate_project_claim(
    about: AboutSummary,
    *,
    project_exists: bool | None = None,
    official_sources_mentioning_mint: tuple[str, ...] = (),
    official_sources_checked: tuple[str, ...] = (),
    community_made: bool = False,
    contradicted: bool = False,
) -> ProjectValidation:
    """Grade the token's project claim against what could actually be confirmed.

    The caller supplies the external observations; this function decides what
    they are worth.  The rule that matters: a mention of the *exact mint* by an
    official source is the only thing that upgrades a claim to SUPPORTED.  A real
    project, a real website and a plausible story do not.
    """

    notes: list[str] = []
    claim = about.primary_claim

    if not about.checkable:
        state = EXTERNAL_NOT_APPLICABLE
        link = LINK_NO_CLAIM if claim == CLAIM_NONE else LINK_UNVERIFIED
        if about.has_official_claim:
            notes.append("claims an official relationship without naming a checkable project")
            link = LINK_UNVERIFIED
            state = EXTERNAL_UNVERIFIED
        return ProjectValidation(
            mint=about.mint,
            claim=claim,
            external_state=state,
            token_link=link,
            project_exists=project_exists,
            sources_checked=tuple(official_sources_checked),
            notes=tuple(notes),
        )

    if contradicted:
        notes.append("an official source excludes this token")
        return ProjectValidation(
            mint=about.mint,
            claim=claim,
            external_state=EXTERNAL_CONTRADICTED,
            token_link=LINK_CONTRADICTED,
            project_exists=project_exists,
            sources_checked=tuple(official_sources_checked),
            notes=tuple(notes),
        )

    if official_sources_mentioning_mint:
        notes.append(
            f"exact mint published by {len(official_sources_mentioning_mint)} official source(s)"
        )
        return ProjectValidation(
            mint=about.mint,
            claim=claim,
            external_state=EXTERNAL_SUPPORTED,
            token_link=LINK_CONFIRMED_OFFICIAL,
            project_exists=True,
            mentions_exact_mint=True,
            sources_checked=tuple(official_sources_checked or official_sources_mentioning_mint),
            notes=tuple(notes),
        )

    if community_made:
        notes.append("community-made token; the project has not adopted it")
        link = LINK_COMMUNITY_MADE
    else:
        link = LINK_UNVERIFIED

    if project_exists:
        notes.append("the project exists but does not publish this mint — that is not a link")
    else:
        notes.append("no external confirmation that the named project exists")
    if about.has_official_claim:
        notes.append("the About text claims an OFFICIAL relationship that is not corroborated")

    return ProjectValidation(
        mint=about.mint,
        claim=claim,
        external_state=EXTERNAL_UNVERIFIED,
        token_link=link,
        project_exists=project_exists,
        mentions_exact_mint=False,
        sources_checked=tuple(official_sources_checked),
        notes=tuple(notes),
    )
