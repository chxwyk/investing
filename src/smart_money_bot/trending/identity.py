"""Exact-mint identity guards for the Trending lane.

v2.41 established the rule and this module keeps it enforceable in the new
pipeline: **mint is identity** (section 13).  Two tokens can share a name, a
ticker, a story, an About blurb and an image, and they routinely do — a real
viral story attracts fifty tokens claiming to be it.  So every piece of evidence
carries the exact mint it belongs to, and crossing that boundary is a
programming error rather than a heuristic judgement.

The second job here is link integrity (section 14).  If a card says
``FOMO: TOKEN XYZ`` and links to a Fomo page, the page must be *this* mint's
page.  A link built from a name, a ticker or a cached URL for a different mint
is worse than no link at all, because it silently sends the operator to the
wrong token while the card says the right one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from ..constants import fomo_coin_url


class CrossMintEvidenceError(ValueError):
    """Raised when evidence for one mint is attached to another."""


def assert_same_mint(expected: str, actual: str, *, what: str = "evidence") -> None:
    """Fail loudly rather than silently merging two tokens."""

    if expected != actual:
        raise CrossMintEvidenceError(
            f"{what} belongs to mint {actual[:8]}… and may not be attached to {expected[:8]}…"
        )


def filter_to_mint(items: Iterable[object], mint: str, *, attribute: str = "mint") -> tuple:
    """Keep only the records that belong to this exact mint."""

    return tuple(item for item in items if getattr(item, attribute, None) == mint)


def fomo_link_for(mint: str, referral_code: str | None = None) -> str:
    """The one canonical Fomo link for this exact mint.

    Always derived from the mint, never from a name, a ticker or a cached URL,
    so the link on a card cannot drift to a different token.
    """

    return fomo_coin_url(mint, referral_code)


def link_matches_mint(url: str, mint: str) -> bool:
    """Verify a Fomo link actually points at this mint (section 14)."""

    if not url or not mint:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").casefold()
    if not (host.endswith("fomo.family") or host.endswith("fomo.biz")):
        return False
    query = parse_qs(parts.query)
    for key in ("address", "mint", "token"):
        values = query.get(key)
        if values and values[0] == mint:
            return True
    # Some Fomo link shapes carry the mint as the final path segment.
    segments = [segment for segment in parts.path.split("/") if segment]
    return bool(segments) and segments[-1] == mint


@dataclass(frozen=True, slots=True)
class CollisionGroup:
    """Other tokens sharing this one's name, ticker or story (section 15)."""

    mint: str
    name: str = ""
    symbol: str = ""
    same_name_mints: tuple[str, ...] = ()
    same_symbol_mints: tuple[str, ...] = ()
    same_story_mints: tuple[str, ...] = ()

    @property
    def collision_count(self) -> int:
        others = (
            set(self.same_name_mints)
            | set(self.same_symbol_mints)
            | set(self.same_story_mints)
        )
        others.discard(self.mint)
        return len(others)

    @property
    def has_collision(self) -> bool:
        return self.collision_count > 0

    def warning_line(self) -> str:
        if not self.has_collision:
            return ""
        return (
            f"⚠ {self.collision_count} OTHER TOKEN(S) SHARE THIS NAME/TICKER/STORY — "
            f"exact mint: {self.mint}"
        )

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "name": self.name,
            "symbol": self.symbol,
            "same_name_mints": list(self.same_name_mints),
            "same_symbol_mints": list(self.same_symbol_mints),
            "same_story_mints": list(self.same_story_mints),
            "collision_count": self.collision_count,
        }


@dataclass(frozen=True, slots=True)
class TokenLabel:
    mint: str
    name: str = ""
    symbol: str = ""
    story_key: str = ""


def find_collisions(subject: TokenLabel, others: Sequence[TokenLabel]) -> CollisionGroup:
    """Group the tokens that look like this one but are not this one."""

    name = subject.name.strip().casefold()
    symbol = subject.symbol.strip().casefold()
    story = subject.story_key.strip().casefold()

    return CollisionGroup(
        mint=subject.mint,
        name=subject.name,
        symbol=subject.symbol,
        same_name_mints=tuple(
            item.mint
            for item in others
            if item.mint != subject.mint and name and item.name.strip().casefold() == name
        ),
        same_symbol_mints=tuple(
            item.mint
            for item in others
            if item.mint != subject.mint and symbol and item.symbol.strip().casefold() == symbol
        ),
        same_story_mints=tuple(
            item.mint
            for item in others
            if item.mint != subject.mint and story and item.story_key.strip().casefold() == story
        ),
    )
