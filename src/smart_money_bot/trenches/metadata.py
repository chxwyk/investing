"""Token metadata: socials, paid DEX placement, and reuse across mints.

Three separate things that all get mistaken for quality.

**Socials (section 25).**  Whether a token exposes a website, X or Telegram is
recorded because their *absence* is informative.  Their presence is not: adding a
link costs nothing and every serious scam has one.

**DEX paid / boosted (section 26).**  DEX Screener's documented public endpoints
expose enhanced token info and boosts.  Those are *purchases*, and treating a
purchase as organic trend is precisely the flaw v2.42's proxy had.  It is
recorded as one small feature and is capped so it can never drive a ranking.

**Reuse (section 27).**  The same image, website, description or socials showing
up under a different mint is checkable evidence of copying.  It is not proof of
malice — legitimate community re-launches exist — so it is reported as
``COPYCAT / REUSE`` evidence for a human to weigh.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlsplit

ZERO = Decimal("0")

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class TokenMetadata:
    """Whatever public metadata the token exposes."""

    mint: str
    name: str = ""
    symbol: str = ""
    description: str = ""
    image_url: str = ""
    website: str = ""
    twitter: str = ""
    telegram: str = ""
    discord: str = ""

    @property
    def has_socials(self) -> bool:
        return bool(self.twitter or self.telegram or self.discord)

    @property
    def social_count(self) -> int:
        return sum(1 for item in (self.website, self.twitter, self.telegram, self.discord) if item)

    def fingerprints(self) -> dict[str, str]:
        """Stable hashes for reuse detection across mints (section 27).

        Hashing rather than storing the raw values keeps the comparison cheap and
        avoids retaining copies of arbitrary third-party text.
        """

        prints: dict[str, str] = {}
        if self.image_url:
            prints["image"] = _digest(_normalise_url(self.image_url))
        if self.website:
            prints["website"] = _digest(_normalise_url(self.website))
        if self.twitter:
            prints["twitter"] = _digest(_normalise_url(self.twitter))
        if self.telegram:
            prints["telegram"] = _digest(_normalise_url(self.telegram))
        if self.description and len(self.description.strip()) >= 24:
            prints["description"] = _digest(
                _WHITESPACE.sub(" ", self.description).strip().casefold()
            )
        return prints

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "name": self.name,
            "symbol": self.symbol,
            "image_url": self.image_url,
            "website": self.website,
            "twitter": self.twitter,
            "telegram": self.telegram,
            "discord": self.discord,
            "has_socials": self.has_socials,
            "social_count": self.social_count,
            "fingerprints": self.fingerprints(),
        }


def _normalise_url(value: str) -> str:
    """Compare URLs by what they point at, not by how they were written."""

    text = (value or "").strip().casefold()
    if not text:
        return ""
    try:
        parts = urlsplit(text if "//" in text else f"https://{text}")
    except ValueError:
        return text
    host = (parts.hostname or "").removeprefix("www.")
    path = parts.path.rstrip("/")
    return f"{host}{path}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class DexPlacement:
    """Paid DEX Screener placement.  A purchase, not a trend (section 26)."""

    mint: str
    dex_paid: bool = False
    boosts_active: int = 0
    has_profile: bool = False

    @property
    def purchased_attention(self) -> bool:
        return self.dex_paid or self.boosts_active > 0 or self.has_profile

    def operator_line(self) -> str:
        if not self.purchased_attention:
            return "DEX placement: none"
        parts = []
        if self.dex_paid:
            parts.append("paid info")
        if self.boosts_active:
            parts.append(f"{self.boosts_active} boost(s)")
        if self.has_profile:
            parts.append("profile")
        return f"DEX placement: {', '.join(parts)} — purchased, not organic"

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "dex_paid": self.dex_paid,
            "boosts_active": self.boosts_active,
            "has_profile": self.has_profile,
            "purchased_attention": self.purchased_attention,
        }


REUSE_NONE = "NONE"
REUSE_PARTIAL = "PARTIAL_REUSE"
REUSE_HEAVY = "HEAVY_REUSE"


@dataclass(frozen=True, slots=True)
class ReuseEvidence:
    """Metadata this mint shares with other mints (section 27)."""

    mint: str
    state: str = REUSE_NONE
    shared_fields: tuple[str, ...] = ()
    other_mints: tuple[str, ...] = ()
    #: True when an earlier mint used this metadata first.
    predated_by_other: bool = False

    @property
    def suspicious(self) -> bool:
        return self.state == REUSE_HEAVY

    def warning_line(self) -> str:
        if self.state == REUSE_NONE:
            return ""
        return (
            f"⚠ metadata reuse (`{self.state}`): shares "
            f"{', '.join(self.shared_fields)} with {len(self.other_mints)} other mint(s). "
            "Copying is evidence, not proof of malice."
        )

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "state": self.state,
            "shared_fields": list(self.shared_fields),
            "other_mints": list(self.other_mints),
            "predated_by_other": self.predated_by_other,
            "suspicious": self.suspicious,
        }


def detect_reuse(
    subject: TokenMetadata,
    others: Sequence[tuple[str, dict[str, str], int]],
    *,
    subject_created_at: int | None = None,
    heavy_threshold: int = 2,
) -> ReuseEvidence:
    """Compare this mint's fingerprints against other mints'.

    ``others`` is ``(mint, fingerprints, created_at)`` for candidates we have
    already seen.  Only *different* mints are compared — a token never collides
    with itself.
    """

    mine = subject.fingerprints()
    if not mine:
        return ReuseEvidence(mint=subject.mint)

    shared: set[str] = set()
    matched: set[str] = set()
    predated = False
    for other_mint, prints, created_at in others:
        if other_mint == subject.mint or not prints:
            continue
        overlap = {
            field
            for field, digest in mine.items()
            if prints.get(field) == digest
        }
        if overlap:
            shared |= overlap
            matched.add(other_mint)
            if (
                subject_created_at is not None
                and created_at
                and created_at < subject_created_at
            ):
                predated = True

    if not shared:
        return ReuseEvidence(mint=subject.mint)
    state = REUSE_HEAVY if len(shared) >= heavy_threshold else REUSE_PARTIAL
    return ReuseEvidence(
        mint=subject.mint,
        state=state,
        shared_fields=tuple(sorted(shared)),
        other_mints=tuple(sorted(matched)),
        predated_by_other=predated,
    )
