"""Fetching the Trending board — from an authorised feed, or an honest approximation.

**What this project can actually access, stated plainly.**  There is no
documented public Fomo Trending API available to this deployment, and there is no
authorised Fomo feed configured by default.  This module therefore does not
pretend to have one.  It has two adapters:

:class:`AuthorisedTrendingClient`
    Reads an administrator-supplied endpoint (``FOMO_TRENDING_API_URL``, with an
    optional ``FOMO_TRENDING_API_KEY``).  Only a deployment that configures that
    URL produces ``FOMO_TRENDING`` provenance.  The bot never discovers such a
    feed on its own.

:class:`ProxyTrendingClient`
    Builds an *approximation* of "what is getting attention right now" from DEX
    Screener's documented public endpoints — ``/token-boosts/top/v1``,
    ``/token-boosts/latest/v1``, ``/token-profiles/latest/v1`` for the ordering,
    and the documented batch endpoint ``/tokens/v1/solana/{addresses}`` (30
    addresses per request) for market data.  Its rank is a position in *our*
    ordering, not Fomo's rank, and every row it produces is stamped
    ``TRENDING_PROXY`` so no surface can print it as Fomo Trending.

What this module does not do, by construction: it does not read cookies, reuse a
browser session, replay credentials, call an undocumented or authenticated
endpoint it was not given, or work around a rate limit.  If no legitimate source
is configured or reachable, the lane reports ``NO_SOURCE_CONFIGURED`` and the
product says so out loud rather than fabricating a board.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any, Protocol

import aiohttp

from .constants import fomo_coin_url
from .news import extract_solana_mints
from .trending.ledger import (
    VERIFIED_NO,
    VERIFIED_UNKNOWN,
    VERIFIED_YES,
    TrendingObservation,
)
from .trending.source import (
    SOURCE_NONE,
    TrendingSourceInfo,
    normalise_change_window,
)

#: DEX Screener's documented batch limit for the token endpoint.
_BATCH_LIMIT = 30


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() else None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class TrendingClient(Protocol):
    """The shape both adapters share, so the engine never special-cases one."""

    source: TrendingSourceInfo
    snapshots: int
    last_snapshot_at: int | None
    last_error: str

    async def fetch_board(self, *, limit: int) -> tuple[TrendingObservation, ...]: ...

    async def close(self) -> None: ...


class NullTrendingClient:
    """Used when nothing legitimate is configured.  Returns no board, ever."""

    def __init__(self) -> None:
        self.source = TrendingSourceInfo(kind=SOURCE_NONE)
        self.snapshots = 0
        self.last_snapshot_at: int | None = None
        self.last_error = "no Trending source is configured"

    async def fetch_board(self, *, limit: int) -> tuple[TrendingObservation, ...]:
        return ()

    async def close(self) -> None:
        return None


class _HttpClient:
    def __init__(self, *, timeout_seconds: int = 12, user_agent: str) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._user_agent = user_agent
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout, headers={"User-Agent": self._user_agent}
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None


class AuthorisedTrendingClient(_HttpClient):
    """An administrator-configured, authorised Fomo Trending feed.

    The response shape is read defensively: a feed that omits a field produces a
    ``None``, never a substituted guess.  In particular, a feed that does not
    document what its percentage covers yields ``CHANGE_WINDOW_UNKNOWN`` — the
    operator can declare it with ``FOMO_TRENDING_CHANGE_WINDOW`` if they know.
    """

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None,
        source: TrendingSourceInfo,
        referral_code: str | None = None,
        timeout_seconds: int = 12,
    ) -> None:
        super().__init__(
            timeout_seconds=timeout_seconds,
            user_agent="SmartMoneyCopyBot/2.42.0 trending-intelligence",
        )
        self.url = url
        self.api_key = api_key
        self.source = source
        self.referral_code = referral_code
        self.snapshots = 0
        self.last_snapshot_at: int | None = None
        self.last_error = ""

    async def fetch_board(self, *, limit: int) -> tuple[TrendingObservation, ...]:
        session = await self._get_session()
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        try:
            async with session.get(self.url, headers=headers) as response:
                if response.status >= 400:
                    self.last_error = f"Trending feed HTTP {response.status}"
                    return ()
                payload = await response.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            self.last_error = str(exc)[:160] or "Trending feed request failed"
            return ()

        rows = payload
        if isinstance(payload, dict):
            for key in ("data", "tokens", "results", "trending"):
                if isinstance(payload.get(key), list):
                    rows = payload[key]
                    break
        if not isinstance(rows, list):
            self.last_error = "Trending feed returned an unexpected shape"
            return ()

        now = int(time.time())
        observations: list[TrendingObservation] = []
        for index, item in enumerate(rows[:limit], start=1):
            if not isinstance(item, dict):
                continue
            mint = str(
                item.get("mint") or item.get("address") or item.get("tokenAddress") or ""
            ).strip()
            if not mint or mint not in extract_solana_mints(mint):
                continue
            verification = item.get("verified")
            observations.append(
                TrendingObservation(
                    mint=mint,
                    observed_at=now,
                    rank=_int_or_none(item.get("rank")) or index,
                    name=str(item.get("name") or "")[:120],
                    symbol=str(item.get("symbol") or item.get("ticker") or "")[:32],
                    fomo_token_id=str(item.get("id") or item.get("tokenId") or "")[:120],
                    fomo_url=str(item.get("url") or "") or fomo_coin_url(mint, self.referral_code),
                    market_cap_usd=_decimal(item.get("marketCap") or item.get("market_cap")),
                    price_usd=_decimal(item.get("price") or item.get("priceUsd")),
                    displayed_change_percent=_decimal(
                        item.get("change") or item.get("priceChange")
                    ),
                    change_window=normalise_change_window(
                        item.get("changeWindow")
                        or item.get("change_window")
                        or self.source.change_window
                    ),
                    liquidity_usd=_decimal(item.get("liquidity")),
                    holder_count=_int_or_none(item.get("holders") or item.get("holderCount")),
                    top10_percent=_decimal(item.get("top10Percent")),
                    verification=(
                        VERIFIED_UNKNOWN
                        if verification is None
                        else (VERIFIED_YES if verification else VERIFIED_NO)
                    ),
                    source=self.source,
                )
            )
        self.snapshots += 1
        self.last_snapshot_at = now
        self.last_error = "" if observations else "Trending feed returned no rows"
        return tuple(observations)


class ProxyTrendingClient(_HttpClient):
    """A public approximation of Trending.  Never labelled as Fomo Trending.

    The ordering is built from DEX Screener's documented boost and profile
    endpoints, which measure paid promotion and freshly-updated profiles.  That
    correlates with attention — which is why it is a usable proxy — but a paid
    boost is not organic interest, so the ordering is treated as a *nomination*
    and the rank it produces is explicitly a proxy rank.
    """

    BASE_URL = "https://api.dexscreener.com"
    PROVIDER = "dexscreener_profiles_boosts"

    def __init__(
        self,
        *,
        source: TrendingSourceInfo,
        referral_code: str | None = None,
        timeout_seconds: int = 12,
    ) -> None:
        super().__init__(
            timeout_seconds=timeout_seconds,
            user_agent="SmartMoneyCopyBot/2.42.0 trending-intelligence",
        )
        self.source = source
        self.referral_code = referral_code
        self.snapshots = 0
        self.last_snapshot_at: int | None = None
        self.last_error = ""
        self.requests = 0

    async def _rows(self, path: str) -> tuple[dict[str, Any], ...]:
        session = await self._get_session()
        self.requests += 1
        try:
            async with session.get(f"{self.BASE_URL}{path}") as response:
                if response.status >= 400:
                    self.last_error = f"DEX Screener HTTP {response.status}"
                    return ()
                payload = await response.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            self.last_error = str(exc)[:160] or "DEX discovery request failed"
            return ()
        if not isinstance(payload, list):
            return ()
        return tuple(item for item in payload if isinstance(item, dict))

    async def _ordered_mints(self, *, limit: int) -> list[str]:
        """Nominate Solana mints, strongest attention signal first."""

        top_boosts, latest_boosts, profiles = await asyncio.gather(
            self._rows("/token-boosts/top/v1"),
            self._rows("/token-boosts/latest/v1"),
            self._rows("/token-profiles/latest/v1"),
        )
        ordered: list[str] = []
        for group in (top_boosts, latest_boosts, profiles):
            for item in group:
                if str(item.get("chainId") or "").casefold() != "solana":
                    continue
                address = str(item.get("tokenAddress") or "").strip()
                if not address or address not in extract_solana_mints(address):
                    continue
                if address not in ordered:
                    ordered.append(address)
                if len(ordered) >= limit:
                    return ordered
        return ordered

    async def _market_data(self, mints: list[str]) -> dict[str, dict[str, Any]]:
        """Batch market data.  30 mints per request keeps the cost bounded (§112)."""

        session = await self._get_session()
        results: dict[str, dict[str, Any]] = {}
        for start in range(0, len(mints), _BATCH_LIMIT):
            batch = mints[start : start + _BATCH_LIMIT]
            self.requests += 1
            try:
                async with session.get(
                    f"{self.BASE_URL}/tokens/v1/solana/{','.join(batch)}"
                ) as response:
                    if response.status >= 400:
                        self.last_error = f"DEX Screener HTTP {response.status}"
                        continue
                    payload = await response.json(content_type=None)
            except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
                self.last_error = str(exc)[:160] or "DEX batch request failed"
                continue
            if not isinstance(payload, list):
                continue
            for pair in payload:
                if not isinstance(pair, dict):
                    continue
                base = pair.get("baseToken") or {}
                address = str(base.get("address") or "")
                if not address:
                    continue
                liquidity = _decimal((pair.get("liquidity") or {}).get("usd")) or Decimal("0")
                existing = results.get(address)
                # Several pools can quote the same mint; the deepest one is the
                # honest reference for price and market cap.
                if existing is not None:
                    prior = _decimal((existing.get("liquidity") or {}).get("usd")) or Decimal("0")
                    if prior >= liquidity:
                        continue
                results[address] = pair
        return results

    async def fetch_board(self, *, limit: int) -> tuple[TrendingObservation, ...]:
        ordered = await self._ordered_mints(limit=limit)
        if not ordered:
            self.snapshots += 1
            self.last_snapshot_at = int(time.time())
            if not self.last_error:
                self.last_error = "no public attention candidates returned"
            return ()

        market = await self._market_data(ordered)
        now = int(time.time())
        observations: list[TrendingObservation] = []
        for index, mint in enumerate(ordered, start=1):
            pair = market.get(mint) or {}
            base = pair.get("baseToken") or {}
            changes = pair.get("priceChange") or {}
            # DEX Screener documents its own change windows, so this one *is*
            # known — but it is our proxy's window, not Fomo's displayed number.
            change = _decimal(changes.get("h1"))
            observations.append(
                TrendingObservation(
                    mint=mint,
                    observed_at=now,
                    rank=index,
                    name=str(base.get("name") or "")[:120],
                    symbol=str(base.get("symbol") or "")[:32],
                    fomo_url=fomo_coin_url(mint, self.referral_code),
                    market_cap_usd=_decimal(pair.get("marketCap") or pair.get("fdv")),
                    price_usd=_decimal(pair.get("priceUsd")),
                    displayed_change_percent=change,
                    change_window=(
                        "1H" if change is not None else self.source.change_window
                    ),
                    liquidity_usd=_decimal((pair.get("liquidity") or {}).get("usd")),
                    # The proxy has no holder or verification data at all, and
                    # says so rather than defaulting to a comfortable number.
                    holder_count=None,
                    top10_percent=None,
                    verification=VERIFIED_UNKNOWN,
                    source=self.source,
                )
            )
        self.snapshots += 1
        self.last_snapshot_at = now
        self.last_error = ""
        return tuple(observations)


def build_trending_client(
    source: TrendingSourceInfo,
    *,
    api_url: str | None,
    api_key: str | None,
    referral_code: str | None = None,
) -> TrendingClient:
    """Pick the adapter that matches the resolved provenance."""

    if source.is_exact_fomo and api_url:
        return AuthorisedTrendingClient(
            url=api_url,
            api_key=api_key,
            source=source,
            referral_code=referral_code,
        )
    if source.kind == SOURCE_NONE:
        return NullTrendingClient()
    return ProxyTrendingClient(source=source, referral_code=referral_code)
