from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import aiohttp

from .models import (
    CoinCallout,
    DexSnapshot,
    TokenInfo,
    TokenRiskSnapshot,
    XSocialSnapshot,
)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class DexScreenerClient:
    """Public, documented DEX Screener token-pair lookup with a short cache."""

    BASE_URL = "https://api.dexscreener.com"

    def __init__(self, *, timeout_seconds: int = 12) -> None:
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, DexSnapshot]] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={"User-Agent": "SmartMoneyCopyBot/2.19 coin-intelligence"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def snapshot(self, mint: str) -> DexSnapshot:
        cached = self._cache.get(mint)
        now = time.monotonic()
        if cached and now - cached[0] <= 20:
            return cached[1]
        session = await self._get_session()
        try:
            async with session.get(f"{self.BASE_URL}/latest/dex/tokens/{mint}") as response:
                if response.status >= 400:
                    return DexSnapshot(available=False)
                payload = await response.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError):
            return DexSnapshot(available=False)
        snapshot = parse_dex_snapshot(payload, mint=mint)
        self._cache[mint] = (now, snapshot)
        return snapshot


def parse_dex_snapshot(payload: Any, *, mint: str) -> DexSnapshot:
    pairs = payload.get("pairs") if isinstance(payload, dict) else None
    if not isinstance(pairs, list):
        return DexSnapshot(available=False)
    matches = [
        item
        for item in pairs
        if isinstance(item, dict)
        and str((item.get("baseToken") or {}).get("address") or "") == mint
        and str(item.get("chainId") or "").lower() == "solana"
    ]
    if not matches:
        return DexSnapshot(available=False)
    pair = max(
        matches,
        key=lambda item: _decimal((item.get("liquidity") or {}).get("usd")) or Decimal("0"),
    )
    txns = pair.get("txns") or {}
    volumes = pair.get("volume") or {}
    changes = pair.get("priceChange") or {}
    info = pair.get("info") or {}
    websites = info.get("websites") or []
    socials = info.get("socials") or []
    created_ms = _integer(pair.get("pairCreatedAt"))
    age_minutes = (
        max(0, int((time.time() * 1000 - created_ms) / 60_000)) if created_ms > 0 else None
    )
    return DexSnapshot(
        available=True,
        liquidity_usd=_decimal((pair.get("liquidity") or {}).get("usd")),
        market_cap_usd=_decimal(pair.get("marketCap") or pair.get("fdv")),
        pair_age_minutes=age_minutes,
        buys_5m=_integer((txns.get("m5") or {}).get("buys")),
        sells_5m=_integer((txns.get("m5") or {}).get("sells")),
        buys_1h=_integer((txns.get("h1") or {}).get("buys")),
        sells_1h=_integer((txns.get("h1") or {}).get("sells")),
        volume_5m_usd=_decimal(volumes.get("m5")) or Decimal("0"),
        volume_1h_usd=_decimal(volumes.get("h1")) or Decimal("0"),
        price_change_5m_percent=_decimal(changes.get("m5")),
        price_change_1h_percent=_decimal(changes.get("h1")),
        active_boosts=_integer((pair.get("boosts") or {}).get("active")),
        has_website=bool(websites),
        has_x_profile=any(
            str(item.get("type") or "").lower() in {"twitter", "x"}
            for item in socials
            if isinstance(item, dict)
        ),
        pair_url=str(pair.get("url") or ""),
    )


class XRecentSearchClient:
    """Official X API v2 recent-search client; no scraping or private endpoints."""

    BASE_URL = "https://api.x.com"

    def __init__(self, bearer_token: str | None, *, timeout_seconds: int = 15) -> None:
        self.bearer_token = bearer_token
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, XSocialSnapshot]] = {}

    @property
    def configured(self) -> bool:
        return bool(self.bearer_token)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def snapshot(self, mint: str) -> XSocialSnapshot:
        if not self.bearer_token:
            return XSocialSnapshot(available=False, error="X_API_BEARER_TOKEN not configured")
        cached = self._cache.get(mint)
        now = time.monotonic()
        if cached and now - cached[0] <= 60:
            return cached[1]
        query = f'"{mint}" -is:retweet'
        session = await self._get_session()
        params = {
            "query": query,
            "max_results": "100",
            "tweet.fields": "author_id,created_at,public_metrics,text",
            "expansions": "author_id",
            "user.fields": "created_at,public_metrics,verified,verified_type",
        }
        try:
            async with session.get(
                f"{self.BASE_URL}/2/tweets/search/recent",
                params=params,
                headers={"Authorization": f"Bearer {self.bearer_token}"},
            ) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    detail = str(body.get("detail") or body.get("title") or response.status)
                    return XSocialSnapshot(available=False, query=query, error=detail[:160])
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            return XSocialSnapshot(available=False, query=query, error=str(exc)[:160])
        snapshot = parse_x_snapshot(body, query=query)
        self._cache[mint] = (now, snapshot)
        return snapshot


class SolanaTrackerTokenRiskClient:
    """Documented token-info risk lookup using the bot's existing Tracker key."""

    BASE_URL = "https://data.solanatracker.io"

    def __init__(self, api_key: str | None, *, timeout_seconds: int = 15) -> None:
        self.api_key = api_key
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, TokenRiskSnapshot]] = {}

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def snapshot(self, mint: str) -> TokenRiskSnapshot:
        if not self.api_key:
            return TokenRiskSnapshot(available=False, error="SOLANA_TRACKER_API_KEY not configured")
        cached = self._cache.get(mint)
        now = time.monotonic()
        if cached and now - cached[0] <= 60:
            return cached[1]
        session = await self._get_session()
        try:
            async with session.get(
                f"{self.BASE_URL}/tokens/{mint}",
                headers={"x-api-key": self.api_key},
            ) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    return TokenRiskSnapshot(
                        available=False, error=f"Tracker HTTP {response.status}"
                    )
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            return TokenRiskSnapshot(available=False, error=str(exc)[:160])
        snapshot = parse_tracker_risk(body)
        self._cache[mint] = (now, snapshot)
        return snapshot


def parse_tracker_risk(payload: Any) -> TokenRiskSnapshot:
    if not isinstance(payload, dict) or not isinstance(payload.get("risk"), dict):
        return TokenRiskSnapshot(available=False, error="risk object unavailable")
    risk = payload["risk"]
    danger_flags: list[str] = []
    for item in risk.get("risks") or []:
        if isinstance(item, dict) and str(item.get("level") or "").lower() == "danger":
            name = str(item.get("name") or item.get("description") or "danger flag")
            danger_flags.append(name[:120])
    return TokenRiskSnapshot(
        available=True,
        score=_decimal(risk.get("score")),
        rugged=bool(risk.get("rugged", False)),
        snipers_percent=_decimal((risk.get("snipers") or {}).get("totalPercentage")),
        insiders_percent=_decimal((risk.get("insiders") or {}).get("totalPercentage")),
        bundlers_percent=_decimal((risk.get("bundlers") or {}).get("totalPercentage")),
        top10_percent=_decimal(risk.get("top10")),
        dev_percent=_decimal((risk.get("dev") or {}).get("percentage")),
        danger_flags=tuple(dict.fromkeys(danger_flags)),
        jupiter_verified=(
            bool(risk.get("jupiterVerified")) if risk.get("jupiterVerified") is not None else None
        ),
    )


def parse_x_snapshot(payload: Any, *, query: str = "") -> XSocialSnapshot:
    if not isinstance(payload, dict):
        return XSocialSnapshot(available=False, query=query, error="invalid X response")
    posts = payload.get("data") or []
    users = (payload.get("includes") or {}).get("users") or []
    if not isinstance(posts, list) or not isinstance(users, list):
        return XSocialSnapshot(available=False, query=query, error="invalid X response")
    by_id = {str(item.get("id")): item for item in users if isinstance(item, dict)}
    author_ids: set[str] = set()
    established: set[str] = set()
    influential: set[str] = set()
    suspicious: set[str] = set()
    engagements = 0
    normalized_texts: list[str] = []
    timestamps: list[datetime] = []
    now = datetime.now(UTC)
    for post in posts:
        if not isinstance(post, dict):
            continue
        author_id = str(post.get("author_id") or "")
        if author_id:
            author_ids.add(author_id)
        metrics = post.get("public_metrics") or {}
        engagements += sum(
            _integer(metrics.get(key))
            for key in ("like_count", "retweet_count", "reply_count", "quote_count")
        )
        normalized = normalize_post_text(str(post.get("text") or ""))
        if normalized:
            normalized_texts.append(normalized)
        created = _parse_time(post.get("created_at"))
        if created:
            timestamps.append(created)
        user = by_id.get(author_id) or {}
        user_metrics = user.get("public_metrics") or {}
        followers = _integer(user_metrics.get("followers_count"))
        following = _integer(user_metrics.get("following_count"))
        account_created = _parse_time(user.get("created_at"))
        age_days = (now - account_created).days if account_created else 0
        if author_id and age_days >= 90 and followers >= 100:
            established.add(author_id)
        if author_id and (followers >= 5_000 or bool(user.get("verified"))):
            influential.add(author_id)
        if author_id and (
            age_days < 30
            or (followers < 25 and following > 500)
            or (followers == 0 and following > 100)
        ):
            suspicious.add(author_id)
    unique_texts = len(set(normalized_texts))
    duplicate_percent = (
        (Decimal(len(normalized_texts) - unique_texts) / Decimal(len(normalized_texts)) * 100)
        if normalized_texts
        else Decimal("0")
    )
    if timestamps:
        earliest = min(timestamps)
        minutes = max(Decimal("1"), Decimal(str((now - earliest).total_seconds() / 60)))
        velocity = Decimal(len(posts)) / minutes
    else:
        velocity = Decimal("0")
    return XSocialSnapshot(
        available=True,
        posts=len(posts),
        unique_authors=len(author_ids),
        established_authors=len(established),
        influential_authors=len(influential),
        suspicious_authors=len(suspicious),
        engagements=engagements,
        duplicate_percent=duplicate_percent.quantize(Decimal("0.01")),
        posts_per_minute=velocity.quantize(Decimal("0.01")),
        query=query,
    )


def normalize_post_text(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text.lower())
    text = re.sub(r"@[a-z0-9_]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


class CoinCalloutAnalyzer:
    def __init__(
        self,
        dex: DexScreenerClient,
        social: XRecentSearchClient,
        tracker_risk: SolanaTrackerTokenRiskClient,
    ) -> None:
        self.dex = dex
        self.social = social
        self.tracker_risk = tracker_risk

    async def analyze(
        self,
        *,
        mint: str,
        token_info: TokenInfo | None,
        smart_wallets: tuple[str, ...],
    ) -> CoinCallout:
        dex, social, tracker_risk = await asyncio.gather(
            self.dex.snapshot(mint),
            self.social.snapshot(mint),
            self.tracker_risk.snapshot(mint),
        )
        return score_callout(
            mint=mint,
            token_info=token_info,
            dex=dex,
            social=social,
            tracker_risk=tracker_risk,
            smart_wallets=smart_wallets,
        )


def score_callout(
    *,
    mint: str,
    token_info: TokenInfo | None,
    dex: DexSnapshot,
    social: XSocialSnapshot,
    tracker_risk: TokenRiskSnapshot | None = None,
    smart_wallets: tuple[str, ...],
) -> CoinCallout:
    score = Decimal("0")
    positives: list[str] = []
    warnings: list[str] = []
    blockers: list[str] = []
    tracker_risk = tracker_risk or TokenRiskSnapshot(available=False)

    if tracker_risk.available:
        if tracker_risk.rugged:
            blockers.append("Solana Tracker marks the token as rugged")
        if tracker_risk.score is not None:
            if tracker_risk.score >= 8:
                blockers.append(f"Solana Tracker risk is {tracker_risk.score}/10")
            elif tracker_risk.score <= 3:
                score += 10
                positives.append(f"low Tracker risk score ({tracker_risk.score}/10)")
            elif tracker_risk.score <= 6:
                score += 5
            else:
                warnings.append(f"high Tracker risk score ({tracker_risk.score}/10)")
        if tracker_risk.danger_flags:
            blockers.extend(f"Tracker danger: {item}" for item in tracker_risk.danger_flags)
        if tracker_risk.bundlers_percent is not None and tracker_risk.bundlers_percent > 20:
            blockers.append(f"bundlers control {tracker_risk.bundlers_percent:.1f}% of supply")
        if tracker_risk.insiders_percent is not None and tracker_risk.insiders_percent > 15:
            blockers.append(f"insiders control {tracker_risk.insiders_percent:.1f}% of supply")
        if tracker_risk.snipers_percent is not None and tracker_risk.snipers_percent > 20:
            warnings.append(f"snipers still control {tracker_risk.snipers_percent:.1f}% of supply")
    else:
        warnings.append("Solana Tracker rug/bundler evidence is unavailable")

    liquidity = (
        token_info.liquidity_usd
        if token_info and token_info.liquidity_usd is not None
        else dex.liquidity_usd
    )
    holders = token_info.holder_count if token_info else None
    concentration = token_info.top_holders_percent if token_info else None
    if token_info:
        if token_info.suspicious:
            blockers.append("token provider flags it as suspicious")
        else:
            score += 2
        if token_info.mint_authority_disabled is False:
            blockers.append("mint authority is still enabled")
        elif token_info.mint_authority_disabled is True:
            score += 3
        if token_info.freeze_authority_disabled is False:
            blockers.append("freeze authority is still enabled")
        elif token_info.freeze_authority_disabled is True:
            score += 3
        if token_info.verified:
            score += 2
            positives.append("token metadata is verified")
        if token_info.dev_balance_percent is not None and token_info.dev_balance_percent > 10:
            warnings.append(f"developer controls {token_info.dev_balance_percent:.1f}%")
    else:
        warnings.append("complete Jupiter token safety metadata is unavailable")

    if liquidity is not None:
        if liquidity >= 100_000:
            score += 10
        elif liquidity >= 50_000:
            score += 8
        elif liquidity >= 25_000:
            score += 6
        elif liquidity >= 10_000:
            score += 4
        elif liquidity >= 2_000:
            score += 2
            warnings.append("launch-stage liquidity")
        else:
            blockers.append("liquidity is below $2,000")
    else:
        warnings.append("liquidity is unavailable")

    if holders is not None:
        score += Decimal(
            8
            if holders >= 500
            else 6
            if holders >= 250
            else 4
            if holders >= 100
            else 2
            if holders >= 50
            else 0
        )
        if holders < 50:
            warnings.append(f"only {holders} holders")
    else:
        warnings.append("holder count is unavailable")
    if concentration is not None:
        score += Decimal(
            6
            if concentration <= 20
            else 4
            if concentration <= 35
            else 2
            if concentration <= 50
            else 0
        )
        if concentration > 50:
            warnings.append(f"top holders control {concentration:.1f}%")

    smart_count = len(set(smart_wallets))
    score += Decimal(
        15
        if smart_count >= 4
        else 12
        if smart_count == 3
        else 9
        if smart_count == 2
        else 4
        if smart_count == 1
        else 0
    )
    if smart_count >= 2:
        positives.append(f"{smart_count} independently verified wallets bought")
    elif smart_count == 1:
        warnings.append("only one verified wallet has bought so far")

    if dex.available:
        total_5m = dex.buys_5m + dex.sells_5m
        buy_ratio = Decimal(dex.buys_5m) / Decimal(total_5m) if total_5m else Decimal("0")
        if total_5m >= 30:
            score += 4
        elif total_5m >= 10:
            score += 2
        if buy_ratio >= Decimal("0.60"):
            score += 5
            positives.append(f"5m flow favors buys ({dex.buys_5m}/{total_5m})")
        elif total_5m and buy_ratio < Decimal("0.45"):
            warnings.append(f"5m flow favors sells ({dex.sells_5m}/{total_5m})")
        if dex.volume_5m_usd >= 10_000:
            score += 4
        elif dex.volume_5m_usd >= 2_500:
            score += 2
        if dex.has_website and dex.has_x_profile:
            score += 3
        elif dex.has_website or dex.has_x_profile:
            score += 1
        if dex.active_boosts:
            warnings.append("DEX boosts are paid visibility, not organic proof")
        if dex.price_change_5m_percent is not None and dex.price_change_5m_percent >= 40:
            warnings.append("price already ran over 40% in five minutes")
    else:
        warnings.append("DEX pair activity is unavailable")

    if social.available:
        score += Decimal(min(8, social.unique_authors // 2))
        score += Decimal(min(5, social.established_authors))
        score += Decimal(min(4, social.influential_authors * 2))
        if social.posts_per_minute >= Decimal("1"):
            score += 4
        elif social.posts_per_minute >= Decimal("0.25"):
            score += 2
        if social.engagements >= 250:
            score += 4
        elif social.engagements >= 50:
            score += 2
        if social.unique_authors >= 10:
            positives.append(f"{social.unique_authors} unique X authors mention the contract")
        if social.duplicate_percent >= 50:
            score -= 8
            warnings.append(
                f"{social.duplicate_percent:.0f}% duplicate X text suggests coordination"
            )
        if social.unique_authors and social.suspicious_authors * 2 >= social.unique_authors:
            score -= 6
            warnings.append("at least half of X authors look newly created or low-quality")
    else:
        warnings.append("official X evidence is unavailable")

    score = max(Decimal("0"), min(Decimal("100"), score)).quantize(Decimal("0.01"))
    if blockers:
        verdict = "BLOCKED"
    elif score >= 70 and smart_count >= 2 and social.available and social.unique_authors >= 8:
        verdict = "STRONG WATCH"
    elif score >= 55 and smart_count >= 1:
        verdict = "WATCH"
    elif score >= 40:
        verdict = "EARLY — NEEDS CONFIRMATION"
    else:
        verdict = "AVOID / TOO WEAK"
    source_count = 1 + int(dex.available) + int(social.available)
    confidence = (
        "HIGH"
        if source_count == 3 and smart_count >= 2
        else "MEDIUM"
        if source_count >= 2
        else "LOW"
    )
    return CoinCallout(
        mint=mint,
        symbol=token_info.symbol if token_info else None,
        name=token_info.name if token_info else None,
        score=score,
        verdict=verdict,
        confidence=confidence,
        smart_wallets=tuple(dict.fromkeys(smart_wallets)),
        token_info=token_info,
        dex=dex,
        social=social,
        tracker_risk=tracker_risk,
        positives=tuple(positives),
        warnings=tuple(warnings),
        hard_blockers=tuple(blockers),
        generated_at=int(time.time()),
    )
