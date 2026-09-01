"""One name, one symbol, one image — per exact mint, for the token's whole life.

The production failure this exists to fix: cards were reaching the operator as

    RESEARCH CANDIDATE — VALIDATION PENDING (EARLY RUNNER)
    ?
    $?
    Mint: 2Gx...

with otherwise perfect market evidence.  The bot knew the exact mint the entire
time.  What it did not have was anywhere to *keep* the name, and the one lookup
it did have — the legacy graduated-runner row — has no entry for a token
discovered by GMGN or by the Pump creation stream.  So the lookup returned
nothing, the fallbacks collapsed to ``"?"``, and a token that GMGN had already
told us was called ``$MDR`` displayed as a question mark.

Three rules shape this module.

**Mint is the key.**  Never the name, never the ticker.  A presentation cache
keyed by symbol is a same-name substitution waiting to happen, and this codebase
has already shipped one wrong-token incident (v2.43.1) to learn that from.

**Never move backwards.**  A field that is known stays known.  Enrichment merges
*into* what exists rather than replacing it, so a later observation that happens
to omit the image cannot turn a card that had one back into a blank.

**Absent is not a value.**  Nothing here invents a name, and nothing substitutes
a same-symbol token's metadata.  A mint we cannot describe yet renders as
``metadata pending`` — which is honest, brief, and unmistakably not a token
called "?".

Pure logic: no provider, no database, no signer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace

#: Rendered when the exact mint's metadata has not resolved *yet*.  A fast card
#: publishes with this rather than waiting, and is edited in place later.
PENDING_NAME = "Metadata pending"
PENDING_SYMBOL = "PENDING"
#: Rendered when resolution was attempted for this exact mint and failed.  The
#: distinction from PENDING matters to an operator: one will improve on its own.
UNAVAILABLE_NAME = "Metadata unavailable"
UNAVAILABLE_SYMBOL = "UNKNOWN"

#: Where a presentation record came from, most trustworthy first.  All of these
#: are exact-mint responses: none of them is a name or ticker search.
SOURCE_GMGN_TOKEN_INFO = "gmgn_token_info"
SOURCE_GMGN_BOARD = "gmgn_board"
SOURCE_PUMP_METADATA = "pump_metadata"
SOURCE_DEX_SNAPSHOT = "dex_snapshot"
SOURCE_RUNNER_ROW = "runner_candidate"
SOURCE_TRENCH = "trench_metadata"
SOURCE_UNKNOWN = ""

#: Higher wins when two sources disagree about the same field.  Ranking is by
#: *how directly the source names the exact mint*, not by how fresh it is: a
#: token-info response is about one address, while a board row is a list entry
#: that happens to include one.
SOURCE_RANK: dict[str, int] = {
    SOURCE_GMGN_TOKEN_INFO: 50,
    SOURCE_PUMP_METADATA: 45,
    SOURCE_DEX_SNAPSHOT: 40,
    SOURCE_TRENCH: 35,
    SOURCE_GMGN_BOARD: 30,
    SOURCE_RUNNER_ROW: 20,
    SOURCE_UNKNOWN: 0,
}

#: Schemes we will put in a Discord thumbnail.  Everything else is refused —
#: a thumbnail is a URL the operator's client will fetch, so a local path or a
#: signed session URL has no business being in one (section 10).
_ALLOWED_SCHEMES = ("https://",)
#: IPFS references are legitimate token metadata; they are rewritten to a public
#: gateway rather than handed to Discord as an unfetchable ``ipfs://`` URL.
_IPFS_PREFIX = "ipfs://"
IPFS_GATEWAY = "https://ipfs.io/ipfs/"

#: Substrings that mean a URL carries a credential or a session.  A thumbnail is
#: rendered to everyone who can see the channel; none of this may travel in one.
_CREDENTIAL_MARKERS = (
    "api_key=",
    "apikey=",
    "api-key=",
    "access_token=",
    "auth=",
    "signature=",
    "x-amz-signature",
    "sessionid=",
    "session_id=",
    "token=",
)


def safe_image_url(raw: object) -> str:
    """Return a URL that is safe to render, or an empty string.

    No thumbnail is better than a wrong or unsafe one.  This refuses anything
    that is not https (after rewriting ``ipfs://`` through the public gateway),
    and anything that looks like it carries a credential or a signed session.
    """

    url = str(raw or "").strip()
    if not url:
        return ""
    if url.startswith(_IPFS_PREFIX):
        url = IPFS_GATEWAY + url[len(_IPFS_PREFIX) :].lstrip("/")
    if not url.startswith(_ALLOWED_SCHEMES):
        return ""
    lowered = url.lower()
    if any(marker in lowered for marker in _CREDENTIAL_MARKERS):
        return ""
    return url


@dataclass(frozen=True, slots=True)
class TokenPresentation:
    """How one exact mint is displayed, everywhere, for its whole life.

    Every consumer reads this rather than re-deriving a name, so a heads-up, a
    promotion, a smart-money card and a shadow exit cannot disagree about what
    the token is called (section 40).
    """

    mint: str
    chain: str = "solana"
    name: str = ""
    symbol: str = ""
    image_url: str = ""
    description: str = ""
    website: str = ""
    twitter: str = ""
    telegram: str = ""
    #: Which source supplied the *identity* fields currently held.
    source: str = SOURCE_UNKNOWN
    source_at: int = 0
    resolved_at: int = 0
    #: True only when the record came from an exact-mint response.  It is never
    #: set by a name or ticker lookup, because no such lookup exists here.
    identity_verified: bool = True
    #: Set once an exact-mint resolution has been attempted and failed, so the
    #: card can say "unavailable" rather than "pending" forever.
    resolution_failed: bool = False
    #: Every source that has contributed, for the detail view.
    sources: tuple[str, ...] = field(default_factory=tuple)

    @property
    def resolved(self) -> bool:
        """Whether we have anything real to show."""

        return bool(self.name or self.symbol)

    @property
    def complete(self) -> bool:
        return bool(self.name and self.symbol and self.image_url)

    @property
    def display_name(self) -> str:
        """Never ``?``.  Either the real name, or an honest placeholder."""

        if self.name:
            return self.name
        if self.symbol:
            return self.symbol
        return UNAVAILABLE_NAME if self.resolution_failed else PENDING_NAME

    @property
    def display_symbol(self) -> str:
        if self.symbol:
            return self.symbol
        return UNAVAILABLE_SYMBOL if self.resolution_failed else PENDING_SYMBOL

    @property
    def thumbnail(self) -> str:
        """A safe image URL, or nothing.  Never another token's image."""

        return safe_image_url(self.image_url)

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "chain": self.chain,
            "name": self.name,
            "symbol": self.symbol,
            "image_url": self.image_url,
            "description": self.description,
            "website": self.website,
            "twitter": self.twitter,
            "telegram": self.telegram,
            "source": self.source,
            "source_at": self.source_at,
            "resolved_at": self.resolved_at,
            "identity_verified": self.identity_verified,
            "resolution_failed": self.resolution_failed,
            "sources": list(self.sources),
            "display_name": self.display_name,
            "display_symbol": self.display_symbol,
            "resolved": self.resolved,
            "complete": self.complete,
        }


def presentation_from_json(payload: dict[str, object]) -> TokenPresentation:
    return TokenPresentation(
        mint=str(payload.get("mint") or ""),
        chain=str(payload.get("chain") or "solana"),
        name=str(payload.get("name") or ""),
        symbol=str(payload.get("symbol") or ""),
        image_url=str(payload.get("image_url") or ""),
        description=str(payload.get("description") or ""),
        website=str(payload.get("website") or ""),
        twitter=str(payload.get("twitter") or ""),
        telegram=str(payload.get("telegram") or ""),
        source=str(payload.get("source") or SOURCE_UNKNOWN),
        source_at=int(payload.get("source_at") or 0),
        resolved_at=int(payload.get("resolved_at") or 0),
        identity_verified=bool(payload.get("identity_verified", True)),
        resolution_failed=bool(payload.get("resolution_failed")),
        sources=tuple(str(item) for item in (payload.get("sources") or ())),
    )


def _clean(value: object) -> str:
    text = str(value or "").strip()
    # A provider that literally sends "?" or "unknown" has told us nothing, and
    # storing it would defeat the whole point of this module.
    if text.lower() in {"?", "unknown", "unknown token", "n/a", "none", "null"}:
        return ""
    return text


def build_presentation(
    mint: str,
    *,
    name: object = "",
    symbol: object = "",
    image_url: object = "",
    description: object = "",
    website: object = "",
    twitter: object = "",
    telegram: object = "",
    source: str = SOURCE_UNKNOWN,
    at: int = 0,
    chain: str = "solana",
) -> TokenPresentation:
    """Capture whatever an exact-mint response already told us.

    Called at the point of discovery rather than later: the fields are usually
    right there in the response that found the token, and throwing them away in
    order to re-fetch them is what produced ``?`` in the first place (section 6).
    """

    return TokenPresentation(
        mint=mint,
        chain=chain,
        name=_clean(name),
        symbol=_clean(symbol),
        image_url=safe_image_url(image_url),
        description=_clean(description),
        website=_clean(website),
        twitter=_clean(twitter),
        telegram=_clean(telegram),
        source=source,
        source_at=at,
        resolved_at=at,
        sources=(source,) if source else (),
    )


def merge(
    current: TokenPresentation | None,
    incoming: TokenPresentation,
) -> TokenPresentation:
    """Fold a new observation into what we already know.  Never lose a field.

    Field-by-field, a value is taken when we do not have one, or when the new
    source outranks the one that supplied it.  Both halves matter: the first
    stops a partial response from blanking a good record, and the second lets a
    token-info response correct a board row's abbreviated name.
    """

    if incoming.mint and current is not None and current.mint != incoming.mint:
        # Two different tokens are two different records.  Merging them is the
        # exact substitution this codebase exists to prevent.
        raise ValueError(
            f"refusing to merge presentation for {incoming.mint[:8]}… into "
            f"{current.mint[:8]}… — a presentation record belongs to one mint"
        )
    if current is None:
        return incoming

    incoming_rank = SOURCE_RANK.get(incoming.source, 0)
    current_rank = SOURCE_RANK.get(current.source, 0)

    def pick(field_name: str) -> str:
        held = getattr(current, field_name)
        fresh = getattr(incoming, field_name)
        if not fresh:
            return held
        if not held:
            return fresh
        return fresh if incoming_rank > current_rank else held

    identity_source = current.source
    identity_at = current.source_at
    if incoming.source and (incoming_rank > current_rank or not current.resolved):
        identity_source = incoming.source
        identity_at = incoming.source_at

    merged_name = pick("name")
    merged_symbol = pick("symbol")
    return replace(
        current,
        name=merged_name,
        symbol=merged_symbol,
        image_url=pick("image_url"),
        description=pick("description"),
        website=pick("website"),
        twitter=pick("twitter"),
        telegram=pick("telegram"),
        source=identity_source,
        source_at=identity_at,
        resolved_at=max(current.resolved_at, incoming.resolved_at),
        identity_verified=current.identity_verified and incoming.identity_verified,
        # Once anything resolved, we are no longer in the failed state.
        resolution_failed=(
            False if (merged_name or merged_symbol) else current.resolution_failed
        ),
        sources=tuple(
            dict.fromkeys((*current.sources, *incoming.sources))
        ),
    )


def mark_unresolved(
    current: TokenPresentation | None,
    mint: str,
    *,
    at: int = 0,
) -> TokenPresentation:
    """Record that exact-mint resolution was tried and did not work.

    Section 7: the alternative — searching the ticker and taking the youngest or
    largest match — is how a card ends up describing a different token, so it is
    not available here at any price.  Failure is safer than substitution.
    """

    if current is None:
        return TokenPresentation(mint=mint, resolved_at=at, resolution_failed=True)
    if current.resolved:
        # Something already resolved; a later failure does not un-know it.
        return current
    return replace(current, resolution_failed=True, resolved_at=max(current.resolved_at, at))


def needs_enrichment(presentation: TokenPresentation | None) -> bool:
    """Whether an async metadata pass would add anything.

    Used to decide what to enrich *after* publishing, never to decide whether to
    publish — the alert goes out either way (sections 4, 45).
    """

    if presentation is None:
        return True
    return not presentation.complete


@dataclass(frozen=True, slots=True)
class PresentationDelta:
    """What changed between the card as published and the card as it should be.

    An edit is worth making only when the operator would see a difference, and
    it must never be a reason to ping again (section 13).
    """

    mint: str
    changed_fields: tuple[str, ...] = ()

    @property
    def worth_editing(self) -> bool:
        return bool(self.changed_fields)


def diff(
    before: TokenPresentation | None,
    after: TokenPresentation | None,
) -> PresentationDelta:
    """Name the visible fields that improved, so an edit can be justified."""

    if after is None:
        return PresentationDelta(mint=before.mint if before else "")
    changed: list[str] = []
    for name in ("name", "symbol", "image_url"):
        old = getattr(before, name, "") if before else ""
        new = getattr(after, name, "")
        if new and new != old:
            changed.append(name)
    return PresentationDelta(mint=after.mint, changed_fields=tuple(changed))


def collides(
    presentation: TokenPresentation,
    others: Iterable[TokenPresentation],
) -> tuple[str, ...]:
    """Other mints presenting under the same symbol.  Informational only.

    It never picks between them — that is the whole rule — but an operator
    looking at ``$MDR`` deserves to know three live tokens answer to it.
    """

    wanted = presentation.symbol.strip().casefold()
    if not wanted:
        return ()
    return tuple(
        sorted(
            {
                item.mint
                for item in others
                if item.mint != presentation.mint
                and item.symbol.strip().casefold() == wanted
            }
        )
    )
