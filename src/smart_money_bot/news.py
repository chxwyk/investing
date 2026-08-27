from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from datetime import datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import aiohttp
from solders.pubkey import Pubkey

from .models import NarrativeCompetition, NarrativePairMatch, NewsAlert

logger = logging.getLogger(__name__)

SOLANA_MINT_RE = re.compile(
    r"(?<![1-9A-HJ-NP-Za-km-z])"
    r"([1-9A-HJ-NP-Za-km-z]{32,44})"
    r"(?![1-9A-HJ-NP-Za-km-z])"
)
TAG_RE = re.compile(r"[$#]([A-Za-z][A-Za-z0-9_]{2,20})")
QUOTED_RE = re.compile(r"[\"“]([^\"”]{3,40})[\"”]")
UPPER_RE = re.compile(r"\b([A-Z][A-Z0-9]{2,19})\b")
TITLE_PHRASE_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9'’-]{2,}(?:\s+[A-Z][A-Za-z0-9'’-]{2,}){0,2})\b"
)

STOP_TERMS = {
    "BREAKING",
    "CRYPTO",
    "NEWS",
    "SOLANA",
    "TOKEN",
    "COIN",
    "OFFICIAL",
    "UPDATE",
    "JUST",
    "THIS",
    "THAT",
    "WITH",
    "FROM",
    "WILL",
    "HAVE",
    "THEY",
    "ABOUT",
    "PRESIDENT",
    "LIVE",
    "REPORT",
    "REPORTS",
    "WATCH",
    "TODAY",
    "WORLD",
    "FIRST",
    "NEW",
    "SAYS",
    "SAID",
}

TITLE_STOP_WORDS = {
    "A",
    "An",
    "And",
    "As",
    "At",
    "Breaking",
    "By",
    "Exclusive",
    "For",
    "From",
    "How",
    "In",
    "Into",
    "Live",
    "New",
    "News",
    "Of",
    "On",
    "Report",
    "Reports",
    "Says",
    "The",
    "This",
    "To",
    "Update",
    "Watch",
    "Why",
    "With",
}

HIGH_IMPACT_WORDS = {
    "announce",
    "breaking",
    "launch",
    "approve",
    "ban",
    "lawsuit",
    "hack",
    "exploit",
    "resign",
    "arrest",
    "president",
    "trump",
    "musk",
    "elon",
    "listing",
    "partnership",
    "acquire",
    "merger",
    "emergency",
    "war",
    "ceasefire",
    "dies",
    "died",
    "death",
    "wins",
    "winner",
    "record",
    "upset",
    "trade",
    "fired",
    "suspended",
    "injured",
    "viral",
    "recall",
    "reveal",
    "unveil",
    "release",
    "cancel",
    "scores",
    "breaks",
    "shatters",
    "beats",
    "marries",
    "divorce",
    "pregnant",
    "feud",
    "fight",
    "collapse",
    "crash",
    "fires",
    "hired",
    "signs",
    "sues",
    "sued",
    "boycott",
    "challenge",
    "trend",
}

COIN_INTENT_PHRASES = {
    "coin",
    "token",
    "memecoin",
    "meme coin",
    "contract address",
    "ticker",
    "pump.fun",
}

TECHNICAL_NEWS_PHRASES = {
    "taxable",
    "tax reporting",
    "reporting framework",
    "regulatory reporting",
    "compliance framework",
    "accounting standard",
}

VIRAL_SOURCE_ACCOUNTS = {
    "@realdonaldtrump",
    "@elonmusk",
    "@whitehouse",
    "@pumpdotfun",
    "@solana",
    "@watcherguru",
    "@coindesk",
    "@cointelegraph",
    "@lookonchain",
    "@arkhamintel",
}

CRYPTO_NEWS_TERMS = {
    "bitcoin",
    "blockchain",
    "crypto",
    "ethereum",
    "memecoin",
    "meme coin",
    "onchain",
    "pump.fun",
    "solana",
    "token",
    "wallet",
}

MAJOR_EVENT_PHRASES = {
    "arrested",
    "assassination",
    "attack",
    "banned",
    "ceasefire",
    "collapse",
    "declares emergency",
    "died",
    "dies",
    "emergency",
    "explosion",
    "fired",
    "indicted",
    "resigned",
    "resigns",
    "shutdown",
    "supreme court",
    "suspended",
    "war",
}

# Named narratives that routinely seed fast copycat tokens. This is intentionally
# small and explicit: treating every capitalized word as a coin identity creates
# far more false matches than useful early leads.
NAMED_NARRATIVES = (
    "Trump",
    "Elon",
    "Musk",
    "DOGE",
    "Bitcoin",
    "Ethereum",
    "OpenAI",
    "SpaceX",
    "Tesla",
    "TikTok",
    "Federal Reserve",
    "Fed",
    "SEC",
    "Iran",
    "Israel",
    "Gaza",
    "China",
    "Russia",
    "Ukraine",
    "NATO",
    "Greenland",
    "Sprite",
    "Coca-Cola",
    "Pepsi",
    "Nike",
    "Apple",
    "Disney",
    "Netflix",
    "Fortnite",
    "Roblox",
    "MrBeast",
    "Taylor Swift",
    "Drake",
    "NBA",
    "NFL",
    "MLB",
    "FIFA",
)


def extract_solana_mints(text: str) -> tuple[str, ...]:
    mints: list[str] = []
    for candidate in SOLANA_MINT_RE.findall(text):
        try:
            normalized = str(Pubkey.from_string(candidate))
        except ValueError:
            continue
        if normalized not in mints:
            mints.append(normalized)
    return tuple(mints[:5])


def extract_narrative_terms(text: str) -> tuple[str, ...]:
    candidates: list[str] = []
    candidates.extend(TAG_RE.findall(text))
    candidates.extend(QUOTED_RE.findall(text))
    candidates.extend(UPPER_RE.findall(text))
    candidates.extend(
        term
        for term in NAMED_NARRATIVES
        if re.search(rf"\b{re.escape(term)}\b", text, flags=re.IGNORECASE)
    )
    candidates.extend(
        phrase
        for phrase in TITLE_PHRASE_RE.findall(text)
        if phrase not in TITLE_STOP_WORDS
        and not all(word in TITLE_STOP_WORDS for word in phrase.split())
    )
    terms: list[str] = []
    for raw in candidates:
        cleaned = re.sub(r"\s+", " ", raw).strip(" .,:;!?-_/@")
        if not 3 <= len(cleaned) <= 40:
            continue
        if cleaned.upper() in STOP_TERMS:
            continue
        key = cleaned.casefold()
        if any(item.casefold() == key for item in terms):
            continue
        terms.append(cleaned)
    return tuple(terms[:8])


def score_news(
    text: str,
    *,
    followers: int = 0,
    verified: bool = False,
    token_mints: Iterable[str] = (),
    narrative_terms: Iterable[str] = (),
    trusted_source: bool = False,
) -> tuple[int, str]:
    lowered = text.casefold()
    score = 10
    if verified:
        score += 15
    if trusted_source:
        score += 5
    if followers >= 1_000_000:
        score += 20
    elif followers >= 100_000:
        score += 14
    elif followers >= 10_000:
        score += 8
    if any(token_mints):
        score += 25
    if any(narrative_terms):
        score += 8
    hits = sum(1 for word in HIGH_IMPACT_WORDS if word in lowered)
    score += min(20, hits * 5)
    score = max(0, min(100, score))
    urgency = "HIGH" if score >= 55 else "MEDIUM" if score >= 30 else "LOW"
    return score, urgency


def is_coin_actionable_news(alert: NewsAlert) -> bool:
    """Cost-control gate for crypto-native or genuinely exceptional event leads."""

    if alert.token_mints:
        return True
    if not alert.narrative_terms:
        return False

    text = f"{alert.headline}\n{alert.summary}".casefold()
    has_coin_intent = any(_phrase_present(text, phrase) for phrase in COIN_INTENT_PHRASES)
    crypto_native = any(_phrase_present(text, term) for term in CRYPTO_NEWS_TERMS)
    crypto_source = alert.author.casefold() in VIRAL_SOURCE_ACCOUNTS
    major_event = any(_phrase_present(text, phrase) for phrase in MAJOR_EVENT_PHRASES)
    major_event = major_event or (
        _phrase_present(text, "breaking")
        and any(
            _phrase_present(text, subject)
            for subject in {"trump", "white house", "president", "elon musk"}
        )
        and any(
            _phrase_present(text, action)
            for action in {"announce", "announces", "ban", "bans", "sign", "signs"}
        )
    )
    technical_only = any(_phrase_present(text, phrase) for phrase in TECHNICAL_NEWS_PHRASES)

    if technical_only and not has_coin_intent:
        return False
    return has_coin_intent or crypto_native or crypto_source or major_event


def _phrase_present(text: str, phrase: str) -> bool:
    escaped = re.escape(phrase.casefold())
    return bool(re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text))


def _parse_iso_time(value: Any) -> int:
    if not value:
        return 0
    raw = str(value)
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except ValueError:
        try:
            return int(parsedate_to_datetime(raw).timestamp())
        except (TypeError, ValueError, OverflowError):
            return 0


def parse_x_news_payload(payload: Any) -> NewsAlert | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return None
    post = payload["data"]
    text = str(post.get("text") or "").strip()
    post_id = str(post.get("id") or "")
    if not text or not post_id:
        return None
    users = (payload.get("includes") or {}).get("users") or []
    user = next(
        (
            item
            for item in users
            if isinstance(item, dict) and str(item.get("id") or "") == str(post.get("author_id"))
        ),
        {},
    )
    username = str(user.get("username") or post.get("author_id") or "unknown")
    followers = int((user.get("public_metrics") or {}).get("followers_count") or 0)
    verified = bool(user.get("verified"))
    mints = extract_solana_mints(text)
    terms = extract_narrative_terms(text)
    score, urgency = score_news(
        text,
        followers=followers,
        verified=verified,
        token_mints=mints,
        narrative_terms=terms,
    )
    matched = payload.get("matching_rules") or []
    matched_rule = ", ".join(
        str(item.get("tag") or item.get("id") or "")
        for item in matched
        if isinstance(item, dict)
    )
    return NewsAlert(
        source="X filtered stream",
        headline=text[:240],
        summary=text,
        url=f"https://x.com/{username}/status/{post_id}",
        author=f"@{username}",
        author_followers=followers,
        author_verified=verified,
        score=score,
        urgency=urgency,
        matched_rule=matched_rule,
        narrative_terms=terms,
        token_mints=mints,
        created_at=_parse_iso_time(post.get("created_at")),
        received_at=int(time.time()),
    )


class XFilteredNewsStream:
    BASE_URL = "https://api.x.com"
    RULE_TAG = "smart-money-news-v240"

    def __init__(self, bearer_token: str | None, rule: str) -> None:
        self.bearer_token = bearer_token
        self.rule = rule.strip()
        self._session: aiohttp.ClientSession | None = None
        self._stop = asyncio.Event()
        self.connected = False
        self.rule_active = False
        self.last_event_at: int | None = None
        self.last_error: str | None = None
        self.reconnects = 0

    @property
    def configured(self) -> bool:
        return bool(self.bearer_token and self.rule)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=90),
                headers={
                    "Authorization": f"Bearer {self.bearer_token}",
                    "User-Agent": "SmartMoneyCopyBot/2.24 news-radar",
                },
            )
        return self._session

    async def close(self) -> None:
        self._stop.set()
        self.connected = False
        if self._session and not self._session.closed:
            await self._session.close()

    async def _response_body(self, response: aiohttp.ClientResponse) -> Any:
        try:
            return await response.json(content_type=None)
        except (ValueError, aiohttp.ClientError):
            return {"detail": (await response.text())[:160]}

    async def ensure_rule(self) -> None:
        if not self.configured:
            return
        session = await self._get_session()
        endpoint = f"{self.BASE_URL}/2/tweets/search/stream/rules"
        async with session.get(endpoint) as response:
            body = await self._response_body(response)
            if response.status >= 400:
                raise RuntimeError(_api_error("X rule lookup", response.status, body))
        rows = body.get("data") or [] if isinstance(body, dict) else []
        tagged = [
            item
            for item in rows
            if isinstance(item, dict) and str(item.get("tag") or "") == self.RULE_TAG
        ]
        if any(str(item.get("value") or "") == self.rule for item in tagged):
            self.rule_active = True
            return
        stale_ids = [str(item.get("id")) for item in tagged if item.get("id")]
        if stale_ids:
            async with session.post(endpoint, json={"delete": {"ids": stale_ids}}) as response:
                body = await self._response_body(response)
                if response.status >= 400:
                    raise RuntimeError(_api_error("X rule delete", response.status, body))
        async with session.post(
            endpoint,
            json={"add": [{"value": self.rule, "tag": self.RULE_TAG}]},
        ) as response:
            body = await self._response_body(response)
            if response.status >= 400:
                raise RuntimeError(_api_error("X rule create", response.status, body))
        self.rule_active = True

    async def run(self, callback: Callable[[NewsAlert], Awaitable[None]]) -> None:
        if not self.configured:
            return
        backoff = 2
        while not self._stop.is_set():
            try:
                await self.ensure_rule()
                await self._consume(callback)
                backoff = 2
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)[:200]
                self.reconnects += 1
                logger.warning("X news stream disconnected: %s", exc)
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                backoff = min(60, backoff * 2)

    async def _consume(self, callback: Callable[[NewsAlert], Awaitable[None]]) -> None:
        session = await self._get_session()
        params = {
            "tweet.fields": "author_id,created_at,public_metrics,text",
            "expansions": "author_id",
            "user.fields": "username,name,verified,verified_type,public_metrics,created_at",
        }
        async with session.get(
            f"{self.BASE_URL}/2/tweets/search/stream",
            params=params,
        ) as response:
            if response.status >= 400:
                body = await self._response_body(response)
                raise RuntimeError(_api_error("X filtered stream", response.status, body))
            self.connected = True
            self.last_error = None
            async for raw in response.content:
                if self._stop.is_set():
                    return
                line = raw.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except (TypeError, ValueError):
                    continue
                alert = parse_x_news_payload(payload)
                if alert is None:
                    continue
                self.last_event_at = int(time.time())
                try:
                    await callback(alert)
                except Exception:
                    logger.exception("Could not process X news alert")


def _api_error(context: str, status: int, body: Any) -> str:
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("detail") or body.get("title") or body.get("errors") or "")
    return f"{context} HTTP {status}: {detail[:140] or 'request rejected'}"


def _text(node: ET.Element, names: tuple[str, ...]) -> str:
    for child in node.iter():
        tag = child.tag.rsplit("}", 1)[-1].lower()
        if tag in names and child.text:
            return child.text.strip()
    return ""


def parse_feed(xml_text: str, *, source_url: str) -> list[NewsAlert]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    source = _text(root, ("title",)) or urlparse(source_url).netloc
    rows = [
        node
        for node in root.iter()
        if node.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}
    ]
    alerts: list[NewsAlert] = []
    for row in rows[:25]:
        headline = _text(row, ("title",))
        summary = _text(row, ("description", "summary", "content"))
        link = _text(row, ("link", "guid"))
        if not link:
            link_node = next(
                (
                    child
                    for child in row.iter()
                    if child.tag.rsplit("}", 1)[-1].lower() == "link"
                ),
                None,
            )
            link = str(link_node.attrib.get("href") or "") if link_node is not None else ""
        published = _text(row, ("pubdate", "published", "updated", "date"))
        combined = f"{headline}\n{summary}"
        mints = extract_solana_mints(combined)
        terms = extract_narrative_terms(combined)
        score, urgency = score_news(
            combined,
            token_mints=mints,
            narrative_terms=terms,
            trusted_source=True,
        )
        if not headline:
            continue
        alerts.append(
            NewsAlert(
                source=source,
                headline=headline[:240],
                summary=re.sub(r"<[^>]+>", " ", summary)[:1000],
                url=link,
                score=score,
                urgency=urgency,
                narrative_terms=terms,
                token_mints=mints,
                created_at=_parse_iso_time(published),
                received_at=int(time.time()),
            )
        )
    return alerts


class RssNewsPoller:
    def __init__(self, urls: tuple[str, ...], *, poll_seconds: int) -> None:
        self.urls = urls
        self.poll_seconds = poll_seconds
        self._session: aiohttp.ClientSession | None = None
        self._stop = asyncio.Event()
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()
        self.ready = False
        self.last_refresh_at: int | None = None
        self.last_error: str | None = None
        self.feed_health: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return bool(self.urls)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "SmartMoneyCopyBot/2.24 news-radar"},
            )
        return self._session

    async def close(self) -> None:
        self._stop.set()
        if self._session and not self._session.closed:
            await self._session.close()

    def _remember(self, key: str) -> None:
        if key in self._seen:
            return
        self._seen.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > 2_000:
            expired = self._seen_order.popleft()
            self._seen.discard(expired)

    async def run(self, callback: Callable[[NewsAlert], Awaitable[None]]) -> None:
        if not self.configured:
            return
        while not self._stop.is_set():
            try:
                await self._poll_once(callback)
                self.last_error = None
                self.last_refresh_at = int(time.time())
                self.ready = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)[:200]
                logger.warning("News RSS refresh failed: %s", exc)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)

    async def _poll_once(self, callback: Callable[[NewsAlert], Awaitable[None]]) -> None:
        session = await self._get_session()
        now = int(time.time())
        first_refresh = not self.ready
        for url in self.urls:
            try:
                async with session.get(url) as response:
                    if response.status >= 400:
                        self.feed_health[url] = f"HTTP {response.status}"
                        continue
                    text = await response.text(errors="ignore")
                    self.feed_health[url] = "ok"
            except (TimeoutError, aiohttp.ClientError) as exc:
                self.feed_health[url] = str(exc)[:120] or type(exc).__name__
                continue
            alerts = parse_feed(text, source_url=url)
            for alert in reversed(alerts):
                key = alert.url or f"{alert.source}:{alert.headline}"
                if key in self._seen:
                    continue
                self._remember(key)
                if first_refresh and (not alert.created_at or now - alert.created_at > 300):
                    continue
                await callback(alert)


class DexNarrativeMatcher:
    BASE_URL = "https://api.dexscreener.com"

    def __init__(
        self,
        *,
        min_liquidity_usd: Decimal,
        max_age_minutes: int,
    ) -> None:
        self.min_liquidity_usd = min_liquidity_usd
        self.max_age_minutes = max_age_minutes
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12),
                headers={"User-Agent": "SmartMoneyCopyBot/2.24 narrative-match"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def search(self, narrative: str) -> NarrativePairMatch | None:
        query = narrative.strip()
        if not query:
            return None
        pairs = await self._search_pairs(query)
        if pairs is None:
            return None
        matches: list[NarrativePairMatch] = []
        wanted = _normalize_term(query)
        now_ms = int(time.time() * 1000)
        for pair in pairs:
            if not isinstance(pair, dict) or str(pair.get("chainId") or "").lower() != "solana":
                continue
            token = pair.get("baseToken") or {}
            mint = str(token.get("address") or "")
            symbol = str(token.get("symbol") or "")
            name = str(token.get("name") or "")
            identity = {_normalize_term(symbol), _normalize_term(name)}
            if wanted not in identity and not any(
                wanted and (wanted in item or item in wanted) for item in identity if item
            ):
                continue
            liquidity = _decimal_or_none((pair.get("liquidity") or {}).get("usd"))
            if liquidity is None or liquidity < self.min_liquidity_usd:
                continue
            created_ms = int(pair.get("pairCreatedAt") or 0)
            age_minutes = max(0, int((now_ms - created_ms) / 60_000)) if created_ms else None
            if age_minutes is None or age_minutes > self.max_age_minutes:
                continue
            txns = pair.get("txns") or {}
            volumes = pair.get("volume") or {}
            matches.append(
                NarrativePairMatch(
                    narrative=query,
                    mint=mint,
                    symbol=symbol,
                    name=name,
                    liquidity_usd=liquidity,
                    market_cap_usd=_decimal_or_none(pair.get("marketCap") or pair.get("fdv")),
                    pair_age_minutes=age_minutes,
                    buys_5m=int((txns.get("m5") or {}).get("buys") or 0),
                    sells_5m=int((txns.get("m5") or {}).get("sells") or 0),
                    volume_5m_usd=_decimal_or_none(volumes.get("m5")) or Decimal("0"),
                    pair_url=str(pair.get("url") or ""),
                )
            )
        return max(
            matches,
            key=lambda item: (
                -(item.pair_age_minutes or 0),
                item.liquidity_usd or Decimal("0"),
            ),
            default=None,
        )

    async def competition(self, narrative: str) -> NarrativeCompetition:
        query = narrative.strip()
        if not query:
            return NarrativeCompetition(query=query, error="empty narrative")
        pairs = await self._search_pairs(query)
        if pairs is None:
            return NarrativeCompetition(query=query, error="DEX search unavailable")

        wanted = _normalize_term(query)
        matches: list[dict[str, Any]] = []
        for pair in pairs:
            if not isinstance(pair, dict) or str(pair.get("chainId") or "").lower() != "solana":
                continue
            token = pair.get("baseToken") or {}
            identities = {
                _normalize_term(str(token.get("symbol") or "")),
                _normalize_term(str(token.get("name") or "")),
            }
            if wanted not in identities and not any(
                wanted and (wanted in item or item in wanted) for item in identities if item
            ):
                continue
            matches.append(pair)

        def liquidity(pair: dict[str, Any]) -> Decimal:
            return _decimal_or_none((pair.get("liquidity") or {}).get("usd")) or Decimal("0")

        strongest = max(matches, key=liquidity, default=None)
        strongest_token = (strongest or {}).get("baseToken") or {}
        strongest_liquidity = liquidity(strongest) if strongest else None
        return NarrativeCompetition(
            query=query,
            matching_pairs=len(matches),
            liquid_pairs=sum(1 for pair in matches if liquidity(pair) >= self.min_liquidity_usd),
            strongest_liquidity_usd=strongest_liquidity,
            strongest_market_cap_usd=(
                _decimal_or_none((strongest or {}).get("marketCap") or (strongest or {}).get("fdv"))
                if strongest
                else None
            ),
            strongest_mint=str(strongest_token.get("address") or ""),
            strongest_symbol=str(strongest_token.get("symbol") or ""),
            strongest_pair_url=str((strongest or {}).get("url") or ""),
        )

    async def _search_pairs(self, query: str) -> list[Any] | None:
        session = await self._get_session()
        try:
            async with session.get(
                f"{self.BASE_URL}/latest/dex/search",
                params={"q": query},
            ) as response:
                if response.status >= 400:
                    return None
                payload = await response.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        pairs = payload.get("pairs") or []
        return pairs if isinstance(pairs, list) else None


def _normalize_term(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
