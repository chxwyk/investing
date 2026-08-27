from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any
from urllib.parse import unquote

from .models import DiscoveryCandidate

logger = logging.getLogger(__name__)

_BASE58_ADDRESS = r"[1-9A-HJ-NP-Za-km-z]{32,44}"
_PROFILE_LINK = re.compile(
    r"(?:https?://(?:www\.)?pump\.fun)?/profile/([^\"'?#<\\]+)",
    re.IGNORECASE,
)
_FOLLOWERS_AFTER = re.compile(r"([0-9][0-9,]*)\s*followers?", re.IGNORECASE)
_SOLSCAN_ACCOUNT = re.compile(
    rf"(?:https?://)?(?:www\.)?solscan\.io/account/({_BASE58_ADDRESS})",
    re.IGNORECASE,
)
_WALLET_FIELD = re.compile(
    rf"[\"'](?:wallet|walletAddress|publicKey|userAddress|address)[\"']\s*"
    rf":\s*[\"']({_BASE58_ADDRESS})[\"']",
    re.IGNORECASE,
)
_EXACT_WALLET = re.compile(rf"^{_BASE58_ADDRESS}$")


@dataclass(frozen=True, slots=True)
class SocialNomination:
    wallet: str
    alias: str
    followers: int
    source: str = "Pump profile"
    profile_url: str = ""


@dataclass(frozen=True, slots=True)
class PumpProfileSummary:
    slug: str
    alias: str
    followers: int


class PumpProfileDiscovery:
    """Low-frequency discovery from Pump's official public profile pages.

    A profile is only a nomination. The engine later requires the same wallet to
    pass its complete strict 24H and 7D PnL feeds before it can enter the hot set.
    """

    BASE_URL = "https://pump.fun"

    def __init__(self, *, timeout_seconds: int = 20) -> None:
        try:
            import aiohttp
        except ImportError as exc:  # pragma: no cover - production dependency
            raise RuntimeError("aiohttp is required for Pump profile discovery") from exc
        self._aiohttp = aiohttp
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: Any = None

    async def _get_session(self) -> Any:
        if self._session is None or self._session.closed:
            self._session = self._aiohttp.ClientSession(
                timeout=self.timeout,
                headers={
                    "User-Agent": (
                        "SmartMoneyCopyBot/2.23 (+public profile verification; "
                        "contact via deployed Discord bot)"
                    )
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def nominations(
        self,
        *,
        pages: int,
        minimum_followers: int,
        limit: int,
        max_profile_fetches: int,
    ) -> list[SocialNomination]:
        summaries: dict[str, PumpProfileSummary] = {}
        for page in range(1, pages + 1):
            page_html = await self._request_html(
                "/profiles", params={"page": str(page)} if page > 1 else None
            )
            for item in parse_pump_profile_index(page_html):
                if item.followers < minimum_followers:
                    continue
                previous = summaries.get(item.slug)
                if previous is None or item.followers > previous.followers:
                    summaries[item.slug] = item

        ordered = sorted(summaries.values(), key=lambda item: item.followers, reverse=True)[:limit]
        semaphore = asyncio.Semaphore(4)

        async def resolve(item: PumpProfileSummary) -> SocialNomination | None:
            wallet = wallet_from_profile_slug(item.slug)
            if wallet is None:
                async with semaphore:
                    try:
                        profile_html = await self._request_html(
                            f"/profile/{item.slug}", params=None
                        )
                    except Exception as exc:  # one public profile must not abort the pool
                        logger.debug("Pump profile %s could not be resolved: %s", item.slug, exc)
                        return None
                wallet = parse_pump_profile_wallet(profile_html)
            if wallet is None:
                return None
            return SocialNomination(
                wallet=wallet,
                alias=item.alias,
                followers=item.followers,
                profile_url=f"{self.BASE_URL}/profile/{item.slug}",
            )

        direct = [item for item in ordered if wallet_from_profile_slug(item.slug)]
        unresolved = [item for item in ordered if not wallet_from_profile_slug(item.slug)]
        selected = direct + unresolved[:max_profile_fetches]
        resolved = await asyncio.gather(*(resolve(item) for item in selected))
        by_wallet: dict[str, SocialNomination] = {}
        for item in resolved:
            if item is None:
                continue
            previous = by_wallet.get(item.wallet)
            if previous is None or item.followers > previous.followers:
                by_wallet[item.wallet] = item
        return sorted(by_wallet.values(), key=lambda item: item.followers, reverse=True)

    async def _request_html(self, path: str, *, params: dict[str, str] | None) -> str:
        session = await self._get_session()
        async with session.get(f"{self.BASE_URL}{path}", params=params) as response:
            body = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"Pump public page HTTP {response.status}")
            return body


def parse_pump_profile_index(raw_html: str) -> list[PumpProfileSummary]:
    """Extract public profile slugs and follower counts from HTML/RSC text."""

    normalized = html.unescape(raw_html).replace("\\/", "/")
    results: dict[str, PumpProfileSummary] = {}
    matches = list(_PROFILE_LINK.finditer(normalized))
    for index, match in enumerate(matches):
        slug = unquote(match.group(1)).strip().strip("/")
        if not slug or slug in {"undefined", "null"}:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        nearby = normalized[match.end() : min(end, match.end() + 2_000)]
        follower_match = _FOLLOWERS_AFTER.search(_strip_tags(nearby))
        if follower_match is None:
            follower_match = _FOLLOWERS_AFTER.search(nearby)
        if follower_match is None:
            continue
        followers = int(follower_match.group(1).replace(",", ""))
        alias = _profile_alias(slug, nearby)
        item = PumpProfileSummary(slug=slug, alias=alias, followers=followers)
        previous = results.get(slug)
        if previous is None or followers > previous.followers:
            results[slug] = item
    return sorted(results.values(), key=lambda item: item.followers, reverse=True)


def wallet_from_profile_slug(slug: str) -> str | None:
    slug = unquote(slug).strip().strip("/")
    return slug if _EXACT_WALLET.fullmatch(slug) else None


def parse_pump_profile_wallet(raw_html: str) -> str | None:
    normalized = html.unescape(raw_html).replace("\\/", "/")
    for pattern in (_SOLSCAN_ACCOUNT, _WALLET_FIELD):
        match = pattern.search(normalized)
        if match:
            return match.group(1)
    return None


def annotate_social_nominations(
    candidates: list[DiscoveryCandidate],
    nominations: list[SocialNomination],
) -> tuple[list[DiscoveryCandidate], int]:
    """Attach social evidence only after complete financial verification.

    This function cannot create a candidate and does not alter its financial score.
    """

    by_wallet = {item.wallet: item for item in nominations}
    annotated: list[DiscoveryCandidate] = []
    matched = 0
    for candidate in candidates:
        nomination = by_wallet.get(candidate.address)
        if nomination is None:
            annotated.append(candidate)
            continue
        matched += 1
        social_note = f"; Pump public profile nomination ({nomination.followers:,} followers)"
        annotated.append(
            replace(
                candidate,
                selection_reason=f"{candidate.selection_reason}{social_note}",
            )
        )
    return annotated, matched


def _strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value)


def _profile_alias(slug: str, nearby: str) -> str:
    text = " ".join(_strip_tags(nearby).split())
    if text:
        first = text.split(" followers", 1)[0].strip()
        if 0 < len(first) <= 80:
            return first
    return slug[:32]


def verified_social_share(
    candidates: list[DiscoveryCandidate], nominations: list[SocialNomination]
) -> Decimal:
    """Return a diagnostics-only ratio; it never participates in ranking."""

    if not candidates:
        return Decimal("0")
    nominated = {item.wallet for item in nominations}
    matched = sum(candidate.address in nominated for candidate in candidates)
    return (Decimal(matched) / Decimal(len(candidates)) * Decimal("100")).quantize(Decimal("0.01"))
