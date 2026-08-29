"""Token identity, image and ABOUT resolution (section I).

Two rules dominate this module:

* An ABOUT description is never invented.  When no provider returned one the
  card says ``No description available.`` and nothing else.
* A social account is never labelled *official* because its name matched.  Only
  a link that a token-metadata source published **for that exact mint** is
  authoritative; everything else is surfaced as unverified.

Image URLs are additionally bounded so a hostile token cannot strand a Discord
render: non-HTTPS, credentialed, private-network, oversized and unknown-shape
URLs are dropped in favour of a graceful fallback.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

NO_DESCRIPTION = "No description available."

#: Discord renders an embed thumbnail; anything longer is rejected outright.
MAX_IMAGE_URL_LENGTH = 512
MAX_DESCRIPTION_LENGTH = 280
MAX_NAME_LENGTH = 64
MAX_SYMBOL_LENGTH = 16

#: Sources that may publish an *authoritative* link for a mint.
AUTHORITATIVE_SOURCES = frozenset(
    {"metaplex", "solana_metadata", "pumpfun", "fomo", "solana_tracker", "dexscreener"}
)

_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏  ‪-‮﻿]")
_MARKDOWN = re.compile(r"[`*_~|\\<>\[\]]")
_WHITESPACE = re.compile(r"\s+")
_X_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

_X_HOSTS = frozenset({"x.com", "twitter.com", "www.x.com", "www.twitter.com"})
_DISCORD_HOSTS = frozenset({"discord.gg", "discord.com", "www.discord.com"})
_TELEGRAM_HOSTS = frozenset({"t.me", "telegram.me", "www.t.me"})


@dataclass(frozen=True, slots=True)
class SocialLink:
    """One public link with an explicit, honest verification state."""

    platform: str
    url: str
    source: str
    official: bool = False

    @property
    def label(self) -> str:
        return self.platform if self.official else f"{self.platform} (unverified)"


@dataclass(frozen=True, slots=True)
class TokenIdentity:
    """Everything needed to answer "what am I actually looking at?"."""

    mint: str
    name: str = "Unknown token"
    symbol: str = "UNKNOWN"
    description: str = NO_DESCRIPTION
    image_url: str = ""
    image_fallback_reason: str = ""
    links: tuple[SocialLink, ...] = ()
    creator: str | None = None
    token_age_seconds: int | None = None
    pair_age_seconds: int | None = None
    sources: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    resolved_at: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def has_description(self) -> bool:
        return self.description != NO_DESCRIPTION

    @property
    def official_links(self) -> tuple[SocialLink, ...]:
        return tuple(link for link in self.links if link.official)

    @property
    def unverified_links(self) -> tuple[SocialLink, ...]:
        return tuple(link for link in self.links if not link.official)

    def link(self, platform: str) -> SocialLink | None:
        for item in self.links:
            if item.platform == platform:
                return item
        return None


def safe_public_url(value: object) -> str:
    """Accept only credential-free public HTTPS URLs.

    Mirrors the news-feed hardening: private, loopback, link-local and
    credentialed hosts never reach a Discord card or an outbound request.
    """

    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_IMAGE_URL_LENGTH:
        return ""
    if _CONTROL.search(candidate) or any(char.isspace() for char in candidate):
        return ""
    parsed = urlparse(candidate)
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    host = parsed.hostname.casefold().rstrip(".")
    if not host or host == "localhost" or host.endswith(".localhost"):
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            return ""
    else:
        if not address.is_global:
            return ""
    return candidate


def safe_image_url(value: object) -> tuple[str, str]:
    """Return ``(url, fallback_reason)``; an empty url means use the fallback."""

    if not isinstance(value, str) or not value.strip():
        return "", "no image published"
    if len(value.strip()) > MAX_IMAGE_URL_LENGTH:
        return "", "image url too long"
    url = safe_public_url(value)
    if not url:
        return "", "image url is not a safe public https url"
    path = urlparse(url).path.casefold()
    if path.endswith(_IMAGE_SUFFIXES):
        return url, ""
    # Many legitimate CDNs serve extension-less image ids; allow those only when
    # the host looks like a content CDN rather than an arbitrary page.
    host = (urlparse(url).hostname or "").casefold()
    if any(marker in host for marker in ("cdn", "img", "image", "ipfs", "arweave", "static")):
        return url, ""
    return "", "image url does not look like an image"


def sanitize_text(value: object, *, limit: int) -> str:
    """Flatten untrusted token metadata into short, inert display text."""

    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = _CONTROL.sub(" ", text)
    text = _MARKDOWN.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if not text:
        return ""
    if len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def sanitize_description(value: object) -> str:
    """Never invent an ABOUT: unusable input becomes the honest fallback."""

    text = sanitize_text(value, limit=MAX_DESCRIPTION_LENGTH)
    return text or NO_DESCRIPTION


def classify_link(url: object, *, source: str) -> SocialLink | None:
    """Map a public URL to a platform, marking official only on real linkage."""

    safe = safe_public_url(url)
    if not safe:
        return None
    host = (urlparse(safe).hostname or "").casefold()
    official = source in AUTHORITATIVE_SOURCES
    if host in _X_HOSTS:
        return SocialLink("X", safe, source, official)
    if host in _DISCORD_HOSTS:
        return SocialLink("Discord", safe, source, official)
    if host in _TELEGRAM_HOSTS:
        return SocialLink("Telegram", safe, source, official)
    if host.endswith("pump.fun"):
        return SocialLink("Pump.fun", safe, source, official)
    if host.endswith("fomo.family") or host.endswith("fomo.biz"):
        return SocialLink("Fomo", safe, source, official)
    return SocialLink("Website", safe, source, official)


def x_profile_url(handle: object, *, source: str) -> SocialLink | None:
    """Build an X link from a bare handle without asserting it is official."""

    if not isinstance(handle, str):
        return None
    cleaned = handle.strip().lstrip("@")
    if not cleaned or not _X_HANDLE.match(cleaned):
        return None
    return SocialLink("X", f"https://x.com/{cleaned}", source, source in AUTHORITATIVE_SOURCES)


def build_token_identity(
    mint: str,
    *,
    metadata: Mapping[str, Any] | None = None,
    name: object = None,
    symbol: object = None,
    resolved_at: int = 0,
    token_age_seconds: int | None = None,
    pair_age_seconds: int | None = None,
    creator: object = None,
    extra_links: Iterable[tuple[str, str]] = (),
    sources: Iterable[str] = (),
) -> TokenIdentity:
    """Assemble the identity block from whatever providers legitimately returned.

    ``metadata`` is untrusted third-party content; every field is bounded and
    sanitized before it can reach Discord.
    """

    data: Mapping[str, Any] = metadata or {}
    source_name = str(data.get("source") or "provider")
    warnings: list[str] = []

    resolved_name = (
        sanitize_text(name, limit=MAX_NAME_LENGTH)
        or sanitize_text(data.get("name"), limit=MAX_NAME_LENGTH)
        or "Unknown token"
    )
    resolved_symbol = (
        sanitize_text(symbol, limit=MAX_SYMBOL_LENGTH)
        or sanitize_text(data.get("symbol"), limit=MAX_SYMBOL_LENGTH)
        or "UNKNOWN"
    )
    description = sanitize_description(data.get("description"))
    image_url, fallback_reason = safe_image_url(data.get("image") or data.get("image_url"))
    if fallback_reason and (data.get("image") or data.get("image_url")):
        warnings.append(f"Token image rejected: {fallback_reason}")

    links: list[SocialLink] = []
    seen: set[str] = set()

    def push(link: SocialLink | None) -> None:
        if link is None or link.url in seen:
            return
        seen.add(link.url)
        links.append(link)

    for key in ("website", "website_url", "url"):
        push(classify_link(data.get(key), source=source_name))
    for key in ("twitter", "x", "x_url"):
        push(classify_link(data.get(key), source=source_name))
    push(x_profile_url(data.get("x_handle"), source=source_name))
    push(classify_link(data.get("discord"), source=source_name))
    push(classify_link(data.get("telegram"), source=source_name))
    for link_source, url in extra_links:
        push(classify_link(url, source=link_source))

    # Navigation links are always safe: they are canonical explorer routes for
    # the exact mint, not a claim about who runs the project.
    push(SocialLink("Pump.fun", f"https://pump.fun/coin/{mint}", "canonical", official=False))
    push(SocialLink("Solscan", f"https://solscan.io/token/{mint}", "canonical", official=True))

    unverified = [
        link.platform for link in links if not link.official and link.source != "canonical"
    ]
    if unverified:
        warnings.append(
            "Unverified social links present - name matches are not proof of ownership"
        )

    return TokenIdentity(
        mint=mint,
        name=resolved_name,
        symbol=resolved_symbol,
        description=description,
        image_url=image_url,
        image_fallback_reason=fallback_reason,
        links=tuple(links),
        creator=sanitize_text(creator, limit=64) or None,
        token_age_seconds=token_age_seconds,
        pair_age_seconds=pair_age_seconds,
        sources=tuple(dict.fromkeys(str(item) for item in sources if item)),
        warnings=tuple(warnings),
        resolved_at=resolved_at,
    )


def identity_from_candidate(
    candidate: Any,
    *,
    metadata: Mapping[str, Any] | None = None,
    now: int = 0,
) -> TokenIdentity:
    """Convenience bridge from an existing ``RunnerCandidate``."""

    pair_created_at = getattr(candidate, "pair_created_at", None)
    chain_created_at = getattr(candidate, "chain_created_at", None)
    extra_links: list[tuple[str, str]] = []
    pair_url = getattr(candidate, "pair_url", "") or ""
    if pair_url:
        extra_links.append(("dexscreener", pair_url))
    return build_token_identity(
        getattr(candidate, "mint", ""),
        metadata=metadata,
        name=getattr(candidate, "name", None),
        symbol=getattr(candidate, "symbol", None),
        resolved_at=now or getattr(candidate, "generated_at", 0),
        token_age_seconds=(now - chain_created_at) if (now and chain_created_at) else None,
        pair_age_seconds=(now - pair_created_at) if (now and pair_created_at) else None,
        creator=getattr(getattr(candidate, "forensics", None), "creator_wallet", None),
        extra_links=extra_links,
        sources=("runner",),
    )


def identity_from_payload(payload: Mapping[str, Any]) -> TokenIdentity:
    """Rebuild a persisted identity without re-resolving any provider."""

    links = tuple(
        SocialLink(
            platform=str(item.get("platform") or "Website"),
            url=str(item.get("url") or ""),
            source=str(item.get("source") or "stored"),
            official=bool(item.get("official")),
        )
        for item in payload.get("links") or ()
        if isinstance(item, dict) and item.get("url")
    )
    return TokenIdentity(
        mint=str(payload.get("mint") or ""),
        name=str(payload.get("name") or "Unknown token"),
        symbol=str(payload.get("symbol") or "UNKNOWN"),
        description=str(payload.get("description") or NO_DESCRIPTION),
        image_url=str(payload.get("image_url") or ""),
        image_fallback_reason=str(payload.get("image_fallback_reason") or ""),
        links=links,
        creator=(str(payload["creator"]) if payload.get("creator") else None),
        token_age_seconds=_optional_int(payload.get("token_age_seconds")),
        pair_age_seconds=_optional_int(payload.get("pair_age_seconds")),
        sources=tuple(str(item) for item in payload.get("sources") or ()),
        warnings=tuple(str(item) for item in payload.get("warnings") or ()),
        resolved_at=int(payload.get("resolved_at") or 0),
    )


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def format_age(seconds: int | None) -> str:
    if seconds is None or seconds < 0:
        return "unknown"
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def short_money(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    amount = Decimal(value)
    for threshold, suffix in (
        (Decimal("1e9"), "B"),
        (Decimal("1e6"), "M"),
        (Decimal("1e3"), "K"),
    ):
        if abs(amount) >= threshold:
            return f"${amount / threshold:.2f}{suffix}"
    return f"${amount:.2f}"
