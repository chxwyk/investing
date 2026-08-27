from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .constants import USDC_MINT
from .errors import JupiterError
from .market import JupiterClient
from .models import (
    CoinCallout,
    DexSnapshot,
    SwapQuote,
    TokenInfo,
    TokenRiskSnapshot,
    XSocialSnapshot,
)

CRYPTO_PROFILE_TERMS = {
    "bitcoin",
    "blockchain",
    "crypto",
    "degen",
    "memecoin",
    "meme coin",
    "onchain",
    "pump.fun",
    "solana",
    "token",
    "trader",
    "web3",
}

COIN_PROMOTION_PHRASES = {
    "ape in",
    "ape",
    "bullish",
    "buy now",
    "buying",
    "ca:",
    "contract address",
    "cto",
    "fair launch",
    "gem",
    "just launched",
    "launching",
    "meme coin",
    "memecoin",
    "moon",
    "pump.fun",
    "sendor",
    "send it",
    "sending",
    "ticker",
}

CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9])\$[A-Za-z][A-Za-z0-9_]{1,14}\b")


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


def _phrase_present(text: str, phrase: str) -> bool:
    escaped = re.escape(phrase.casefold())
    return bool(re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text))


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
                headers={"User-Agent": "SmartMoneyCopyBot/2.25 coin-intelligence"},
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
    website_url = next(
        (
            str(item.get("url") or "")
            for item in websites
            if isinstance(item, dict) and item.get("url")
        ),
        "",
    )
    x_url = next(
        (
            str(item.get("url") or "")
            for item in socials
            if isinstance(item, dict)
            and str(item.get("type") or "").lower() in {"twitter", "x"}
        ),
        "",
    )
    x_path = urlparse(x_url).path.strip("/").split("/")[0] if x_url else ""
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
        website_url=website_url,
        x_handle=x_path.lstrip("@"),
        pair_url=str(pair.get("url") or ""),
    )


class XRecentSearchClient:
    """Official X API v2 recent-search client; no scraping or private endpoints."""

    BASE_URL = "https://api.x.com"

    def __init__(
        self,
        bearer_token: str | None,
        *,
        timeout_seconds: int = 15,
        max_results: int = 10,
        cache_seconds: int = 60,
        trusted_crypto_accounts: tuple[str, ...] = (),
        budget_reserver: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self.bearer_token = bearer_token
        self.max_results = max(10, min(100, max_results))
        self.cache_seconds = max(30, cache_seconds)
        self.trusted_crypto_accounts = frozenset(
            item.casefold().lstrip("@") for item in trusted_crypto_accounts if item.strip()
        )
        self.budget_reserver = budget_reserver
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, XSocialSnapshot]] = {}
        self.last_success_at: int | None = None
        self.last_error: str | None = None
        self.last_status_code: int | None = None
        self.requests_attempted = 0
        self.budget_rejections = 0

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

    async def _reserve_request(self) -> bool:
        if self.budget_reserver is None:
            self.requests_attempted += 1
            return True
        if await self.budget_reserver():
            self.requests_attempted += 1
            return True
        self.budget_rejections += 1
        self.last_status_code = None
        self.last_error = "daily X search budget exhausted"
        return False

    async def snapshot(
        self,
        mint: str,
        *,
        symbol: str | None = None,
        name: str | None = None,
    ) -> XSocialSnapshot:
        if not self.bearer_token:
            return XSocialSnapshot(available=False, error="X_API_BEARER_TOKEN not configured")
        query = build_x_query(mint, symbol=symbol, name=name)
        cached = self._cache.get(query)
        now = time.monotonic()
        if cached and now - cached[0] <= self.cache_seconds:
            return cached[1]
        if not await self._reserve_request():
            return XSocialSnapshot(
                available=False,
                query=query,
                error=self.last_error,
            )
        session = await self._get_session()
        params = {
            "query": query,
            "max_results": str(self.max_results),
            "tweet.fields": "author_id,created_at,public_metrics,text",
            "expansions": "author_id",
            "user.fields": (
                "username,description,location,created_at,public_metrics,verified,verified_type"
            ),
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
                    self.last_status_code = response.status
                    self.last_error = f"HTTP {response.status}: {detail[:120]}"
                    return XSocialSnapshot(
                        available=False,
                        query=query,
                        error=self.last_error,
                    )
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            self.last_status_code = None
            self.last_error = f"request failed: {str(exc)[:120]}"
            return XSocialSnapshot(available=False, query=query, error=self.last_error)
        snapshot = parse_x_snapshot(
            body,
            query=query,
            contract=mint,
            trusted_crypto_accounts=self.trusted_crypto_accounts,
        )
        self.last_status_code = 200
        self.last_error = None
        self.last_success_at = int(time.time())
        self._cache[query] = (now, snapshot)
        return snapshot

    async def narrative_snapshot(self, narrative: str) -> XSocialSnapshot:
        """Measure public X activity for one narrative using the official recent-search API."""

        cleaned = re.sub(r"[\"\\]", "", narrative).strip()
        if not cleaned:
            return XSocialSnapshot(available=False, error="empty narrative")
        if not self.bearer_token:
            return XSocialSnapshot(
                available=False,
                error="X_API_BEARER_TOKEN not configured",
            )
        query = build_x_narrative_query(cleaned)
        cached = self._cache.get(query)
        now = time.monotonic()
        if cached and now - cached[0] <= self.cache_seconds:
            return cached[1]

        if not await self._reserve_request():
            return XSocialSnapshot(
                available=False,
                query=query,
                error=self.last_error,
            )

        session = await self._get_session()
        params = {
            "query": query,
            "max_results": str(self.max_results),
            "tweet.fields": "author_id,created_at,public_metrics,text",
            "expansions": "author_id",
            "user.fields": (
                "username,description,location,created_at,public_metrics,verified,verified_type"
            ),
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
                    self.last_status_code = response.status
                    self.last_error = f"HTTP {response.status}: {detail[:120]}"
                    return XSocialSnapshot(
                        available=False,
                        query=query,
                        error=self.last_error,
                    )
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            self.last_status_code = None
            self.last_error = f"request failed: {str(exc)[:120]}"
            return XSocialSnapshot(available=False, query=query, error=self.last_error)

        snapshot = parse_x_snapshot(
            body,
            query=query,
            trusted_crypto_accounts=self.trusted_crypto_accounts,
        )
        self.last_status_code = 200
        self.last_error = None
        self.last_success_at = int(time.time())
        self._cache[query] = (now, snapshot)
        return snapshot


def build_x_query(mint: str, *, symbol: str | None = None, name: str | None = None) -> str:
    """Search only the exact mint; ticker/name matches are too easy to spoof."""

    del symbol, name
    return f'"{mint}" -is:retweet lang:en'


def build_x_narrative_query(narrative: str) -> str:
    """Search one idea only where X users also use explicit crypto/coin language."""

    cleaned = re.sub(r"[\"\\]", "", narrative).strip()[:80]
    return (
        f'"{cleaned}" (crypto OR memecoin OR "meme coin" OR solana OR pumpfun OR '
        '"pump.fun" OR token OR ticker OR "contract address" OR "CA:") '
        "-is:retweet lang:en"
    )


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


def parse_x_snapshot(
    payload: Any,
    *,
    query: str = "",
    contract: str = "",
    trusted_crypto_accounts: frozenset[str] | None = None,
) -> XSocialSnapshot:
    if not isinstance(payload, dict):
        return XSocialSnapshot(available=False, query=query, error="invalid X response")
    posts = payload.get("data") or []
    users = (payload.get("includes") or {}).get("users") or []
    if not isinstance(posts, list) or not isinstance(users, list):
        return XSocialSnapshot(available=False, query=query, error="invalid X response")
    trusted_crypto_accounts = trusted_crypto_accounts or frozenset()
    by_id = {str(item.get("id")): item for item in users if isinstance(item, dict)}
    author_ids: set[str] = set()
    established: set[str] = set()
    influential: set[str] = set()
    suspicious: set[str] = set()
    crypto_authors: set[str] = set()
    credible_crypto_authors: set[str] = set()
    contract_authors: set[str] = set()
    credible_contract_authors: set[str] = set()
    trusted_crypto_authors: set[str] = set()
    million_follower_authors: set[str] = set()
    notable_by_author: dict[str, tuple[int, str]] = {}
    engagements = 0
    normalized_texts: list[str] = []
    contract_posts = 0
    coin_intent_posts = 0
    promoter_posts = 0
    timestamps: list[datetime] = []
    now = datetime.now(UTC)
    for post in posts:
        if not isinstance(post, dict):
            continue
        author_id = str(post.get("author_id") or "")
        if author_id:
            author_ids.add(author_id)
        raw_text = str(post.get("text") or "")
        lowered_text = raw_text.casefold()
        metrics = post.get("public_metrics") or {}
        engagements += sum(
            _integer(metrics.get(key))
            for key in ("like_count", "retweet_count", "reply_count", "quote_count")
        )
        normalized = normalize_post_text(raw_text)
        has_contract = bool(contract and contract.casefold() in lowered_text)
        if has_contract:
            contract_posts += 1
        if normalized:
            normalized_texts.append(normalized)
        created = _parse_time(post.get("created_at"))
        if created:
            timestamps.append(created)
        user = by_id.get(author_id) or {}
        username = str(user.get("username") or "").casefold().lstrip("@")
        profile_text = (
            f"{user.get('description') or ''} {user.get('location') or ''}"
        ).casefold()
        user_metrics = user.get("public_metrics") or {}
        followers = _integer(user_metrics.get("followers_count"))
        following = _integer(user_metrics.get("following_count"))
        account_created = _parse_time(user.get("created_at"))
        age_days = (now - account_created).days if account_created else 0
        profile_is_crypto = username in trusted_crypto_accounts or any(
            _phrase_present(profile_text, term) for term in CRYPTO_PROFILE_TERMS
        )
        post_has_crypto_language = any(
            _phrase_present(lowered_text, term) for term in CRYPTO_PROFILE_TERMS
        )
        has_coin_intent = (
            has_contract
            or bool(CASHTAG_RE.search(raw_text))
            or any(_phrase_present(lowered_text, phrase) for phrase in COIN_PROMOTION_PHRASES)
        )
        if has_coin_intent:
            coin_intent_posts += 1
        if author_id and (profile_is_crypto or (post_has_crypto_language and has_coin_intent)):
            crypto_authors.add(author_id)
            if has_coin_intent:
                promoter_posts += 1
                if username:
                    notable_by_author[author_id] = (followers, username)
            if username in trusted_crypto_accounts:
                trusted_crypto_authors.add(author_id)
            if followers >= 1_000_000:
                million_follower_authors.add(author_id)
            credible_profile = username in trusted_crypto_accounts or (
                age_days >= 90 and followers >= 500
            )
            if credible_profile:
                credible_crypto_authors.add(author_id)
            if has_contract:
                contract_authors.add(author_id)
                if credible_profile:
                    credible_contract_authors.add(author_id)
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
        contract_posts=contract_posts,
        identity_posts=max(0, len(posts) - contract_posts),
        unique_authors=len(author_ids),
        established_authors=len(established),
        influential_authors=len(influential),
        suspicious_authors=len(suspicious),
        crypto_authors=len(crypto_authors),
        credible_crypto_authors=len(credible_crypto_authors),
        contract_authors=len(contract_authors),
        credible_contract_authors=len(credible_contract_authors),
        trusted_crypto_authors=len(trusted_crypto_authors),
        million_follower_authors=len(million_follower_authors),
        coin_intent_posts=coin_intent_posts,
        promoter_posts=promoter_posts,
        engagements=engagements,
        duplicate_percent=duplicate_percent.quantize(Decimal("0.01")),
        posts_per_minute=velocity.quantize(Decimal("0.01")),
        notable_accounts=tuple(
            f"@{username} ({followers:,})"
            for followers, username in sorted(
                notable_by_author.values(),
                key=lambda item: (item[0], item[1]),
                reverse=True,
            )[:5]
        ),
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
        market: JupiterClient | None = None,
        prefilter_min_score: Decimal = Decimal("35"),
    ) -> None:
        self.dex = dex
        self.social = social
        self.tracker_risk = tracker_risk
        self.market = market
        self.prefilter_min_score = prefilter_min_score

    async def _executable_quote(
        self,
        token_info: TokenInfo | None,
    ) -> tuple[SwapQuote | None, str | None]:
        if self.market is None or not self.market.api_key:
            return None, "Jupiter executable quote is not configured"
        if token_info is None or token_info.decimals is None:
            return None, "token decimals are unavailable for executable quote verification"
        try:
            quote = await self.market.quote_order(
                input_mint=USDC_MINT,
                output_mint=token_info.mint,
                amount_raw=5_000_000,
                input_decimals=6,
                output_decimals=token_info.decimals,
            )
        except (JupiterError, ValueError) as exc:
            return None, str(exc)[:180]
        return quote, None

    async def analyze(
        self,
        *,
        mint: str,
        token_info: TokenInfo | None,
        smart_wallets: tuple[str, ...],
        force_x_search: bool = False,
    ) -> CoinCallout:
        dex, tracker_risk, quote_result = await asyncio.gather(
            self.dex.snapshot(mint),
            self.tracker_risk.snapshot(mint),
            self._executable_quote(token_info),
        )
        executable_quote, quote_error = quote_result
        prefilter = score_callout(
            mint=mint,
            token_info=token_info,
            dex=dex,
            social=XSocialSnapshot(
                available=False,
                error="paid X check deferred until free evidence passes",
            ),
            tracker_risk=tracker_risk,
            smart_wallets=smart_wallets,
            executable_quote=executable_quote,
            quote_error=quote_error,
        )
        allowed, reason = should_request_x_search(
            prefilter,
            configured_score_floor=self.prefilter_min_score,
        )
        if not allowed and not force_x_search:
            return replace(
                prefilter,
                prefilter_score=prefilter.score,
                x_search_attempted=False,
                scan_stage="FREE_REJECTED",
                scan_reason=reason,
            )

        social = await self.social.snapshot(
            mint,
            symbol=token_info.symbol if token_info else None,
            name=token_info.name if token_info else None,
        )
        final = score_callout(
            mint=mint,
            token_info=token_info,
            dex=dex,
            social=social,
            tracker_risk=tracker_risk,
            smart_wallets=smart_wallets,
            executable_quote=executable_quote,
            quote_error=quote_error,
        )
        return replace(
            final,
            prefilter_score=prefilter.score,
            x_search_attempted=True,
            scan_stage="X_CHECKED" if social.available else "X_UNAVAILABLE",
            scan_reason=(
                "manual X check"
                if force_x_search and social.available
                else "free evidence passed; paid X verification completed"
                if social.available
                else social.error or "paid X verification did not return evidence"
            ),
        )


def score_callout(
    *,
    mint: str,
    token_info: TokenInfo | None,
    dex: DexSnapshot,
    social: XSocialSnapshot,
    tracker_risk: TokenRiskSnapshot | None = None,
    smart_wallets: tuple[str, ...],
    executable_quote: SwapQuote | None = None,
    quote_error: str | None = None,
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
        if tracker_risk.bundlers_percent is not None and tracker_risk.bundlers_percent > 10:
            blockers.append(f"bundlers control {tracker_risk.bundlers_percent:.1f}% of supply")
        if tracker_risk.insiders_percent is not None and tracker_risk.insiders_percent > 10:
            blockers.append(f"insiders control {tracker_risk.insiders_percent:.1f}% of supply")
        if tracker_risk.snipers_percent is not None and tracker_risk.snipers_percent > 20:
            blockers.append(f"snipers still control {tracker_risk.snipers_percent:.1f}% of supply")
        if tracker_risk.top10_percent is not None and tracker_risk.top10_percent > 35:
            blockers.append(f"Tracker top 10 control {tracker_risk.top10_percent:.1f}% of supply")
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
        if token_info.dev_balance_percent is not None and token_info.dev_balance_percent > 5:
            blockers.append(f"developer controls {token_info.dev_balance_percent:.1f}%")
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
        if concentration > 35:
            blockers.append(f"top holders control {concentration:.1f}%")

    provider_liquidity = token_info.liquidity_usd if token_info else None
    dex_liquidity = dex.liquidity_usd if dex.available else None
    liquidity_cross_checked = False
    if provider_liquidity is not None and dex_liquidity is not None:
        lower = min(provider_liquidity, dex_liquidity)
        upper = max(provider_liquidity, dex_liquidity)
        if lower <= 0 or upper > lower * Decimal("4"):
            blockers.append(
                "Jupiter and DEX liquidity disagree by more than 4x; reported liquidity "
                "is not trusted"
            )
        else:
            liquidity_cross_checked = True
            score += 3
            positives.append("liquidity agrees across Jupiter and DEX data")
    else:
        warnings.append("liquidity could not be cross-checked across Jupiter and DEX")

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
        social_points = Decimal(min(6, social.crypto_authors * 2))
        social_points += Decimal(min(6, social.credible_crypto_authors * 3))
        social_points += Decimal(min(6, social.credible_contract_authors * 3))
        social_points += Decimal(min(5, social.promoter_posts))
        social_points += Decimal(min(4, social.contract_posts * 2))
        if social.posts_per_minute >= Decimal("1"):
            social_points += 4
        elif social.posts_per_minute >= Decimal("0.25"):
            social_points += 2
        if social.engagements >= 250:
            social_points += 4
        elif social.engagements >= 50:
            social_points += 2
        if social.posts and social.contract_posts == 0:
            social_points = (social_points * Decimal("0.50")).quantize(Decimal("0.01"))
            warnings.append(
                "X activity matched the verified name/ticker but not the contract address"
            )
        score += social_points
        if social.contract_authors >= 3 and social.credible_contract_authors >= 2:
            positives.append(
                f"{social.contract_authors} crypto-native X authors posted the exact contract; "
                f"{social.credible_contract_authors} passed account-quality checks"
            )
        if social.duplicate_percent >= 35:
            score -= 8
            warnings.append(
                f"{social.duplicate_percent:.0f}% duplicate X text suggests coordination"
            )
        if social.unique_authors and social.suspicious_authors * 3 >= social.unique_authors:
            score -= 6
            warnings.append("at least one-third of X authors look newly created or low-quality")
    else:
        warnings.append("official X evidence is unavailable")

    quote_verified = False
    if executable_quote is not None:
        if executable_quote.price_impact_percent <= Decimal("2"):
            quote_verified = True
            score += 8
            positives.append(
                f"$5 Jupiter route is executable at "
                f"{executable_quote.price_impact_percent:.2f}% price impact"
            )
        else:
            blockers.append(
                f"$5 Jupiter route has {executable_quote.price_impact_percent:.2f}% "
                "price impact"
            )
    else:
        warnings.append(
            "executable $5 Jupiter route is unavailable"
            + (f": {quote_error}" if quote_error else "")
        )

    tracker_complete = bool(
        tracker_risk.available
        and tracker_risk.score is not None
        and tracker_risk.bundlers_percent is not None
        and tracker_risk.insiders_percent is not None
        and tracker_risk.snipers_percent is not None
    )
    token_safety_complete = bool(
        token_info
        and token_info.holder_count is not None
        and token_info.top_holders_percent is not None
        and token_info.dev_balance_percent is not None
        and token_info.mint_authority_disabled is True
        and token_info.freeze_authority_disabled is True
    )
    market_flow_ready = bool(
        dex.available
        and dex.liquidity_usd is not None
        and dex.liquidity_usd >= Decimal("5000")
        and token_info
        and token_info.liquidity_usd is not None
        and token_info.liquidity_usd >= Decimal("5000")
        and dex.buys_5m + dex.sells_5m >= 10
        and dex.buys_5m >= dex.sells_5m
        and dex.volume_5m_usd >= Decimal("1000")
    )
    authentic_x_push = bool(
        social.available
        and social.contract_posts >= 3
        and social.contract_authors >= 3
        and social.unique_authors >= 4
        and social.credible_contract_authors >= 2
        and social.crypto_authors >= 3
        and social.promoter_posts >= 3
        and social.duplicate_percent < Decimal("35")
        and (
            not social.unique_authors
            or social.suspicious_authors * 3 < social.unique_authors
        )
        and (
            social.posts_per_minute >= Decimal("0.10")
            or social.engagements >= 50
        )
        and (
            social.trusted_crypto_authors >= 1
            or social.million_follower_authors >= 1
            or social.credible_contract_authors >= 3
            or smart_count >= 2
        )
    )
    public_alert_eligible = bool(
        not blockers
        and token_safety_complete
        and tracker_complete
        and liquidity_cross_checked
        and market_flow_ready
        and authentic_x_push
        and quote_verified
    )

    if not token_safety_complete:
        warnings.append(
            "complete authority, holder, concentration, and developer proof is required"
        )
    if not tracker_complete:
        warnings.append("complete Tracker risk, bundler, insider, and sniper proof is required")
    if not market_flow_ready:
        warnings.append(
            "verified liquidity and active buy/volume flow are below the public-alert gate"
        )
    if not authentic_x_push:
        warnings.append(
            "exact-contract promotion by multiple credible crypto accounts is below the "
            "public-alert gate"
        )

    score = max(Decimal("0"), min(Decimal("100"), score)).quantize(Decimal("0.01"))
    if public_alert_eligible:
        verdict = "VERIFIED TREND"
    elif blockers:
        verdict = "BLOCKED"
    elif score >= 60:
        verdict = "DEVELOPING — NOT PUBLIC"
    else:
        verdict = "INCOMPLETE — NOT PUBLIC"
    source_count = 1 + int(dex.available) + int(social.available) + int(tracker_risk.available)
    confidence = (
        "HIGH"
        if public_alert_eligible
        else "MEDIUM"
        if source_count >= 3
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
        executable_quote=executable_quote,
        quote_error=quote_error,
        public_alert_eligible=public_alert_eligible,
    )


def should_publish_coin_callout(
    callout: CoinCallout,
    *,
    configured_score_floor: Decimal,
) -> bool:
    """Keep blocked, incomplete, and low-confidence research out of public Discord."""

    return bool(
        callout.public_alert_eligible
        and callout.verdict == "VERIFIED TREND"
        and callout.score >= max(configured_score_floor, Decimal("70"))
    )


def should_request_x_search(
    callout: CoinCallout,
    *,
    configured_score_floor: Decimal,
) -> tuple[bool, str]:
    """Spend X credits only after the free safety, market, and route checks pass."""

    if callout.hard_blockers:
        return False, callout.hard_blockers[0]
    if callout.score < configured_score_floor:
        return False, (
            f"free evidence score {callout.score}/100 is below the "
            f"{configured_score_floor}/100 paid-X threshold"
        )
    token = callout.token_info
    if not token:
        return False, "complete token metadata is unavailable"
    if (
        token.holder_count is None
        or token.top_holders_percent is None
        or token.dev_balance_percent is None
        or token.mint_authority_disabled is not True
        or token.freeze_authority_disabled is not True
    ):
        return False, "complete authority, holder, concentration, and developer proof is missing"
    tracker = callout.tracker_risk
    if not (
        tracker.available
        and tracker.score is not None
        and tracker.bundlers_percent is not None
        and tracker.insiders_percent is not None
        and tracker.snipers_percent is not None
    ):
        return False, "complete Tracker rug, bundler, insider, and sniper proof is missing"
    dex = callout.dex
    if not dex.available or dex.liquidity_usd is None:
        return False, "DEX liquidity and activity are unavailable"
    if token.liquidity_usd is None:
        return False, "provider liquidity is unavailable"
    if min(token.liquidity_usd, dex.liquidity_usd) < Decimal("2000"):
        return False, "cross-source liquidity is below $2,000"
    if dex.buys_5m + dex.sells_5m < 3 or dex.buys_5m < dex.sells_5m:
        return False, "early five-minute market flow does not favor buyers"
    if dex.volume_5m_usd < Decimal("250"):
        return False, "five-minute trading volume is below $250"
    if not callout.smart_wallets:
        return False, "no financially verified tracked wallet bought in the live window"
    quote = callout.executable_quote
    if quote is None:
        return False, "the $5 executable Jupiter route is unavailable"
    if quote.price_impact_percent > Decimal("3"):
        return False, "the $5 executable route has more than 3% price impact"
    return True, "free evidence passed"


def should_publish_coin_watch(
    callout: CoinCallout,
    *,
    configured_score_floor: Decimal,
) -> bool:
    """Expose credible developing X activity without presenting it as a buy signal."""

    social = callout.social
    return bool(
        callout.x_search_attempted
        and callout.scan_stage == "X_CHECKED"
        and social.available
        and not callout.hard_blockers
        and callout.score >= max(configured_score_floor, Decimal("50"))
        and social.contract_posts >= 2
        and social.contract_authors >= 2
        and social.crypto_authors >= 1
        and social.promoter_posts >= 1
        and social.duplicate_percent < Decimal("50")
        and callout.executable_quote is not None
    )
