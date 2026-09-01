"""A native async GMGN OpenAPI client — read operations only.

Built against the official ``GMGNAI/gmgn-skills`` TypeScript client at commit
``267ff6b`` (``src/client/OpenApiClient.ts``, ``src/client/signer.ts``,
``src/config.ts``, ``docs/cli-usage.md``).  Nothing here is guessed: the host,
the auth shape, the response envelope, the rate-limit header and every path
below are what that client actually sends.

**Two auth modes exist upstream, and this file implements exactly one.**
GMGN's "exist" auth is an API key plus a timestamp and a client id; its "signed"
auth additionally requires ``GMGN_PRIVATE_KEY``, a request-signing key, and is
what ``/v1/trade/swap`` and the order routes demand.  This client has no signer,
holds no private key, and does not implement a single signed path — so it is
structurally incapable of placing an order even if something tried to make it
(section 75, 83).  ``ORDER_PATHS`` below exists only so a test can assert their
continued absence.

**The credential never leaves this module.**  It is read from the environment,
sent as a header, and never logged, never formatted into an exception, never
persisted, and never returned.  :func:`redact` is applied to every error string
on the way out.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

import aiohttp

from .budget import (
    ALLOW,
    DENY_BREAKER_OPEN,
    DENY_BUDGET,
    DENY_DISABLED,
    DENY_RATE_LIMITED,
    BudgetConfig,
    RequestBudget,
)
from .endpoints import EndpointRegistry
from .health import (
    AUTH_MISSING,
    AUTH_REJECTED,
    DISABLED_BY_CONFIG,
    PROVIDER_DEGRADED,
    RATE_LIMIT_BANNED,
    RATE_LIMITED,
    TIMEOUT,
    ProviderHealth,
    record_failure,
    record_success,
)
from .models import (
    GmgnParticipant,
    GmgnSecurity,
    GmgnSignal,
    GmgnToken,
    GmgnWalletTrade,
    parse_hot_searches_response,
    parse_participants,
    parse_rank_response,
    parse_security,
    parse_signals,
    parse_trenches_response,
    parse_wallet_trades,
)
from .signals import (
    CHAIN_SOLANA,
    RANK_INTERVALS,
    TAG_GMGN_KOL,
    TAG_GMGN_SMART_MONEY,
    TRENCH_TYPES,
    TRENCHES_QUOTE_ADDRESS_TYPES_SOL,
)

logger = logging.getLogger(__name__)

#: Documented host (``src/config.ts``).
DEFAULT_HOST = "https://openapi.gmgn.ai"

#: Read paths this client uses.  All are "exist" auth — API key only.
PATH_RANK = "/v1/market/rank"
PATH_TRENCHES = "/v1/trenches"
PATH_HOT_SEARCHES = "/v1/market/hot_searches"
PATH_TOKEN_SIGNAL = "/v1/market/token_signal"
PATH_TOP_HOLDERS = "/v1/market/token_top_holders"
PATH_TOP_TRADERS = "/v1/market/token_top_traders"
PATH_KLINE = "/v1/market/token_kline"
PATH_TOKEN_INFO = "/v1/token/info"
PATH_TOKEN_SECURITY = "/v1/token/security"
PATH_POOL_INFO = "/v1/token/pool_info"
PATH_SMART_MONEY = "/v1/user/smartmoney"
PATH_KOL = "/v1/user/kol"
PATH_CREATED_TOKENS = "/v1/user/created_tokens"
PATH_USER_INFO = "/v1/user/info"

READ_PATHS: frozenset[str] = frozenset(
    {
        PATH_RANK,
        PATH_TRENCHES,
        PATH_HOT_SEARCHES,
        PATH_TOKEN_SIGNAL,
        PATH_TOP_HOLDERS,
        PATH_TOP_TRADERS,
        PATH_KLINE,
        PATH_TOKEN_INFO,
        PATH_TOKEN_SECURITY,
        PATH_POOL_INFO,
        PATH_SMART_MONEY,
        PATH_KOL,
        PATH_CREATED_TOKENS,
        PATH_USER_INFO,
    }
)

#: Upstream paths that move money.  Named here so a test can assert this client
#: never calls one, and so a future reader knows the omission is deliberate.
ORDER_PATHS: frozenset[str] = frozenset(
    {
        "/v1/trade/swap",
        "/v1/trade/multi_swap",
        "/v1/trade/strategy/create",
        "/v1/trade/strategy/cancel",
        "/v1/cooking/create_token",
    }
)

#: Provider error codes documented in the official client's retry logic.
ERROR_RATE_LIMIT = "RATE_LIMIT_EXCEEDED"
ERROR_RATE_LIMIT_BANNED = "RATE_LIMIT_BANNED"

_AUTH_ERROR_HINTS = ("api_key", "apikey", "unauthorized", "forbidden", "invalid key")


class GmgnError(RuntimeError):
    """A provider failure, already redacted and already classified."""

    def __init__(self, message: str, *, state: str, reset_at: int | None = None) -> None:
        super().__init__(message)
        self.state = state
        self.reset_at = reset_at


def redact(text: object, *secrets: str) -> str:
    """Strip credentials from anything about to be logged or raised.

    Belt and braces: the key is never deliberately formatted into a message, and
    this makes sure a provider echoing it back cannot leak it either.
    """

    value = str(text)
    for secret in secrets:
        if secret and len(secret) >= 6:
            value = value.replace(secret, "***")
    return value[:300]


class GmgnClient:
    """Read-only GMGN access with a budget, a cache and honest health.

    One instance per process.  It owns its aiohttp session and its budget, so
    every caller in the bot shares the same rate-limit accounting — which is the
    only way a per-token enrichment loop and a board poller can be prevented
    from independently exhausting the same quota.
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        enabled: bool = True,
        host: str = DEFAULT_HOST,
        chain: str = CHAIN_SOLANA,
        timeout_seconds: float = 12.0,
        budget_config: BudgetConfig | None = None,
        session: Any = None,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self.enabled = bool(enabled)
        self.host = host.rstrip("/")
        self.chain = chain
        self.timeout_seconds = timeout_seconds
        self.budget = RequestBudget(config=budget_config or BudgetConfig())
        # Health is per endpoint (section 17).  A 429 on an optional feed cools
        # that feed; it does not silence discovery, which is exactly what the
        # single global record did in production.
        self.endpoints = EndpointRegistry()
        self._session = session
        self._owns_session = session is None
        self.health = ProviderHealth(
            state=(
                DISABLED_BY_CONFIG
                if not self.enabled
                else (AUTH_MISSING if not self._api_key else "UNKNOWN")
            )
        )

    # ---- lifecycle ----------------------------------------------------

    @property
    def configured(self) -> bool:
        """Whether a call could even be attempted.  Never exposes the key."""

        return self.enabled and bool(self._api_key)

    async def _get_session(self) -> Any:
        if self._session is None or getattr(self._session, "closed", False):
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
                headers={"User-Agent": "SmartMoneyCopyBot/2.46 gmgn-research"},
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            closed = getattr(self._session, "closed", True)
            if not closed:
                await self._session.close()
        self._session = None

    # ---- the one request path -----------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        kind: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """Every call goes through here: admission, cache, coalesce, classify.

        ``path`` is asserted against :data:`READ_PATHS`.  That assertion is the
        structural guarantee behind section 83 — a caller cannot reach an order
        route through this client even by passing one in.
        """

        if path not in READ_PATHS:
            raise GmgnError(
                f"{path} is not a read path; this client performs research reads only",
                state=PROVIDER_DEGRADED,
            )
        if not self.enabled:
            raise GmgnError("GMGN is disabled by configuration", state=DISABLED_BY_CONFIG)
        if not self._api_key:
            raise GmgnError("GMGN_API_KEY is not configured", state=AUTH_MISSING)

        allowed, refusal = self.endpoints.admits(
            kind, budget_headroom=self.budget.headroom()
        )
        if not allowed:
            if refusal.startswith("SHED_"):
                self.budget.budget_skips += 1
                raise GmgnError(
                    f"{kind} shed to protect the core GMGN budget", state=PROVIDER_DEGRADED
                )
            cooling_state = (
                refusal
                if refusal in {RATE_LIMITED, RATE_LIMIT_BANNED, DISABLED_BY_CONFIG}
                else PROVIDER_DEGRADED
            )
            raise GmgnError(f"{kind} is cooling down ({refusal})", state=cooling_state)

        verdict = self.budget.admit(enabled=self.enabled)
        if verdict != ALLOW:
            if verdict == DENY_BREAKER_OPEN:
                self.budget.breaker_skips += 1
                raise GmgnError("GMGN circuit breaker is open", state=PROVIDER_DEGRADED)
            if verdict == DENY_RATE_LIMITED:
                raise GmgnError("GMGN rate-limit window has not reset", state=RATE_LIMITED)
            if verdict == DENY_BUDGET:
                self.budget.budget_skips += 1
                raise GmgnError("GMGN request budget exhausted", state=PROVIDER_DEGRADED)
            if verdict == DENY_DISABLED:
                raise GmgnError("GMGN is disabled", state=DISABLED_BY_CONFIG)

        key = self.budget.cache_key(kind, path=path, query=query, body=body)
        ttl = self.budget.ttl_for(kind)
        return await self.budget.run(
            key,
            lambda: self._execute(method, path, kind=kind, query=query, body=body),
            ttl=ttl,
        )

    async def _execute(
        self,
        method: str,
        path: str,
        *,
        kind: str,
        query: dict[str, Any] | None,
        body: dict[str, Any] | None,
    ) -> Any:
        attempts = self.budget.config.max_retries + 1
        last: GmgnError | None = None
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                data, rows = await self._fetch(method, path, query=query, body=body)
            except GmgnError as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                self.health = record_failure(
                    self.health,
                    state=exc.state,
                    error=str(exc),
                    latency_ms=elapsed,
                    rate_limit_reset_at=exc.reset_at,
                )
                if exc.state in {RATE_LIMITED, RATE_LIMIT_BANNED}:
                    # Never retry into a rate limit; that is how a 429 becomes
                    # a ban.  The window the provider named cools **this
                    # endpoint** — the global budget is only paused when a core
                    # discovery feed is the one being limited, because pausing
                    # everything for a hot-search 429 is the production bug.
                    cooldown = _cooldown_seconds(exc.reset_at)
                    self.endpoints.note_failure(
                        kind,
                        state=exc.state,
                        error=str(exc),
                        cooldown_seconds=cooldown,
                    )
                    if self.endpoints.get(kind).tier == "A":
                        self.budget.note_rate_limited(reset_at_unix=exc.reset_at)
                    raise
                self.budget.note_failure()
                self.endpoints.note_failure(kind, state=exc.state, error=str(exc))
                if exc.state in {AUTH_MISSING, AUTH_REJECTED, DISABLED_BY_CONFIG}:
                    raise
                last = exc
                if attempt < attempts:
                    await asyncio.sleep(self.budget.retry_delay(attempt))
                    continue
                raise
            else:
                elapsed = int((time.monotonic() - started) * 1000)
                self.budget.note_success()
                self.endpoints.note_success(kind, rows=rows)
                self.health = record_success(self.health, latency_ms=elapsed, rows=rows)
                return data
        raise last or GmgnError("GMGN request failed", state=PROVIDER_DEGRADED)

    async def _fetch(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None,
        body: dict[str, Any] | None,
    ) -> tuple[Any, int]:
        # Documented auth: X-APIKEY header, plus timestamp (unix seconds, server
        # tolerance +/-5s) and a per-request client_id UUID as query params.
        params: dict[str, Any] = dict(query or {})
        params["timestamp"] = int(time.time())
        params["client_id"] = str(uuid.uuid4())

        session = await self._get_session()
        self.budget.note_call()
        try:
            async with session.request(
                method,
                f"{self.host}{path}",
                params=_flatten(params),
                json=body if body is not None else None,
                headers={"X-APIKEY": self._api_key, "Content-Type": "application/json"},
            ) as response:
                reset_at = _int_or_none(response.headers.get("x-ratelimit-reset"))
                status = response.status
                text = await response.text()
        except TimeoutError as exc:
            raise GmgnError(
                redact(f"{method} {path} timed out: {exc}", self._api_key), state=TIMEOUT
            ) from exc
        except aiohttp.ClientError as exc:
            raise GmgnError(
                redact(f"{method} {path} failed: {exc}", self._api_key), state=PROVIDER_DEGRADED
            ) from exc

        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise GmgnError(
                redact(f"{method} {path} returned non-JSON (HTTP {status})", self._api_key),
                state=PROVIDER_DEGRADED,
            ) from exc
        if not isinstance(payload, dict):
            raise GmgnError(
                f"{method} {path} returned an unexpected payload shape",
                state=PROVIDER_DEGRADED,
            )

        code = payload.get("code")
        if code != 0:
            error = str(payload.get("error") or "")
            message = str(payload.get("message") or "")
            state = _classify_error(status, error, message)
            raise GmgnError(
                redact(
                    f"{method} {path} failed: HTTP {status} code={code} error={error}",
                    self._api_key,
                ),
                state=state,
                reset_at=reset_at,
            )

        data = payload.get("data")
        return data, _row_count(data)

    # ---- research reads ------------------------------------------------

    async def trending(
        self,
        *,
        interval: str,
        limit: int = 100,
        chain: str | None = None,
        **filters: Any,
    ) -> tuple[GmgnToken, ...]:
        """GMGN Trending for one documented interval (section 12).

        Unsupported intervals are refused rather than sent: an undocumented
        window is not a smaller answer, it is a different endpoint's error.
        """

        if interval not in RANK_INTERVALS:
            raise GmgnError(
                f"{interval!r} is not a documented GMGN interval {RANK_INTERVALS}",
                state=PROVIDER_DEGRADED,
            )
        data = await self._request(
            "GET",
            PATH_RANK,
            kind="rank",
            query={
                "chain": chain or self.chain,
                "interval": interval,
                "limit": min(100, max(1, limit)),
                **filters,
            },
        )
        return parse_rank_response(data, interval=interval)

    async def trenches(
        self,
        *,
        types: tuple[str, ...] = TRENCH_TYPES,
        limit: int = 80,
        chain: str | None = None,
    ) -> dict[str, tuple[GmgnToken, ...]]:
        """GMGN Pump.fun trenches (section 14).

        The body shape mirrors the official client's ``buildTrenchesBody``: one
        section per requested type, each carrying the documented filter set.
        Launchpad platforms are deliberately left to the service's own defaults
        — a client-side allow-list silently hides newly supported platforms.
        """

        selected = tuple(item for item in types if item in TRENCH_TYPES) or TRENCH_TYPES
        section: dict[str, Any] = {
            "filters": ["offchain", "onchain"],
            "launchpad_platform_v2": True,
            "limit": max(1, limit),
        }
        target = chain or self.chain
        if target == CHAIN_SOLANA:
            section["quote_address_type"] = list(TRENCHES_QUOTE_ADDRESS_TYPES_SOL)
        body: dict[str, Any] = {"version": "v2"}
        for name in selected:
            body[name] = dict(section)
        data = await self._request(
            "POST", PATH_TRENCHES, kind="trenches", query={"chain": target}, body=body
        )
        return parse_trenches_response(data)

    async def hot_searches(
        self,
        *,
        interval: str = "24h",
        limit: int = 50,
        chain: str | None = None,
    ) -> tuple[GmgnToken, ...]:
        """Attention, not demand (section 18)."""

        if interval not in RANK_INTERVALS:
            raise GmgnError(
                f"{interval!r} is not a documented GMGN interval", state=PROVIDER_DEGRADED
            )
        data = await self._request(
            "POST",
            PATH_HOT_SEARCHES,
            kind="hot_searches",
            body={
                "params": [
                    {"chain": chain or self.chain, "interval": interval, "limit": limit}
                ]
            },
        )
        return parse_hot_searches_response(data)

    async def market_signals(
        self,
        *,
        signal_types: tuple[int, ...] = (),
        market_cap_min: float | None = None,
        chain: str | None = None,
    ) -> tuple[GmgnSignal, ...]:
        """Documented signal families, mapped to explicit internal names."""

        group: dict[str, Any] = {}
        if signal_types:
            group["signal_type"] = list(signal_types)
        if market_cap_min is not None:
            group["mc_min"] = market_cap_min
        data = await self._request(
            "POST",
            PATH_TOKEN_SIGNAL,
            kind="token_signal",
            body={"chain": chain or self.chain, "groups": [group]},
        )
        return parse_signals(data)

    async def smart_money(
        self, *, limit: int = 100, chain: str | None = None
    ) -> tuple[GmgnWalletTrade, ...]:
        """Recent trades by wallets GMGN tags ``smart_degen``.

        This is a **trade feed**, not a wallet directory — the official skill is
        explicit about it.  v2.45 parsed it as a directory, found no top-level
        wallet address, and reported zero smart-money wallets forever.  A feed is
        also the better shape: it is the event the fast card wants.
        """

        data = await self._request(
            "GET",
            PATH_SMART_MONEY,
            kind="smartmoney",
            query={"chain": chain or self.chain, "limit": limit},
        )
        return parse_wallet_trades(data, tag=TAG_GMGN_SMART_MONEY)

    async def kols(
        self, *, limit: int = 100, chain: str | None = None
    ) -> tuple[GmgnWalletTrade, ...]:
        """Recent trades by wallets GMGN tags ``kol``/``renowned``.

        Kept separate from smart money on purpose: the same skill notes KOL
        trades "carry social/marketing signal, not necessarily alpha".
        """

        data = await self._request(
            "GET", PATH_KOL, kind="kol", query={"chain": chain or self.chain, "limit": limit}
        )
        return parse_wallet_trades(data, tag=TAG_GMGN_KOL)

    async def top_holders(
        self, mint: str, *, limit: int = 20, chain: str | None = None
    ) -> tuple[GmgnParticipant, ...]:
        data = await self._request(
            "GET",
            PATH_TOP_HOLDERS,
            kind="top_holders",
            query={"chain": chain or self.chain, "address": mint, "limit": limit},
        )
        return parse_participants(data, mint=mint)

    async def top_traders(
        self, mint: str, *, limit: int = 20, chain: str | None = None
    ) -> tuple[GmgnParticipant, ...]:
        data = await self._request(
            "GET",
            PATH_TOP_TRADERS,
            kind="top_traders",
            query={"chain": chain or self.chain, "address": mint, "limit": limit},
        )
        return parse_participants(data, mint=mint)

    async def security(self, mint: str, *, chain: str | None = None) -> GmgnSecurity:
        """One input to safety.  A provider outage yields UNKNOWN, never PASS."""

        try:
            data = await self._request(
                "GET",
                PATH_TOKEN_SECURITY,
                kind="token_security",
                query={"chain": chain or self.chain, "address": mint},
            )
        except GmgnError:
            return GmgnSecurity(mint=mint, provider_available=False)
        return parse_security(data, mint=mint)

    async def token_info(self, mint: str, *, chain: str | None = None) -> dict[str, Any]:
        data = await self._request(
            "GET",
            PATH_TOKEN_INFO,
            kind="token_info",
            query={"chain": chain or self.chain, "address": mint},
        )
        return data if isinstance(data, dict) else {}

    async def pool_info(self, mint: str, *, chain: str | None = None) -> dict[str, Any]:
        data = await self._request(
            "GET",
            PATH_POOL_INFO,
            kind="pool_info",
            query={"chain": chain or self.chain, "address": mint},
        )
        return data if isinstance(data, dict) else {}

    async def kline(
        self,
        mint: str,
        *,
        interval: str = "1m",
        limit: int = 100,
        chain: str | None = None,
    ) -> list[Any]:
        """Candles for trend shape.  Never read ahead of a decision (section 43)."""

        data = await self._request(
            "GET",
            PATH_KLINE,
            kind="kline",
            query={
                "chain": chain or self.chain,
                "address": mint,
                "interval": interval,
                "limit": limit,
            },
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("list", "klines", "candles", "data"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        return []

    async def created_tokens(
        self, wallet: str, *, limit: int = 20, chain: str | None = None
    ) -> tuple[GmgnToken, ...]:
        """A creator's prior launches — context about a record, never a verdict."""

        data = await self._request(
            "GET",
            PATH_CREATED_TOKENS,
            kind="created_tokens",
            query={"chain": chain or self.chain, "wallet_address": wallet, "limit": limit},
        )
        rows = data if isinstance(data, list) else (data or {}).get("list") or []
        from .models import parse_tokens

        return parse_tokens(rows, source="gmgn_created_tokens")

    # ---- accounting ----------------------------------------------------

    def usage_snapshot(self) -> dict[str, Any]:
        """Everything `/fomo profit view:providers` needs.  Never the key."""

        return {
            "provider": "gmgn",
            "configured": self.configured,
            "enabled": self.enabled,
            "host": self.host,
            "chain": self.chain,
            **self.health.to_json(),
            **self.budget.snapshot(),
            # The single ``state`` above is the last call's outcome; this is the
            # honest picture across every feed (sections 17, 54).
            "endpoint_health": self.endpoints.to_json(),
        }


def _cooldown_seconds(reset_at_unix: int | None, *, default: int = 60) -> int:
    """How long to leave this endpoint alone, from the provider's own answer.

    Section 19: configuration values are budgets, not statements about the
    plan.  When GMGN names a reset time we honour it; when it does not, one
    minute is a conservative guess that does not extend a ban.
    """

    if reset_at_unix is None:
        return default
    return max(1, min(3_600, int(reset_at_unix - time.time()) + 1))


def _classify_error(status: int, error: str, message: str) -> str:
    text = f"{error} {message}".lower()
    if error == ERROR_RATE_LIMIT_BANNED:
        return RATE_LIMIT_BANNED
    if error == ERROR_RATE_LIMIT or status == 429:
        return RATE_LIMITED
    if status in {401, 403} or any(hint in text for hint in _AUTH_ERROR_HINTS):
        return AUTH_REJECTED
    return PROVIDER_DEGRADED


def _row_count(data: Any) -> int:
    """How many rows came back, so ``ACTIVE_NO_EVENTS`` means what it says.

    A wrapper object whose only lists are empty is **zero** rows, not one.  The
    distinction matters: "the board answered and it is empty" and "the board
    answered with something" are different provider states, and conflating them
    would make an idle feed look busy.
    """

    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        total = 0
        saw_collection = False
        for value in data.values():
            if isinstance(value, list):
                saw_collection = True
                total += len(value)
            elif isinstance(value, dict):
                saw_collection = True
                total += _row_count(value)
        if saw_collection:
            return total
        return 1 if data else 0
    return 0


def _flatten(params: dict[str, Any]) -> list[tuple[str, str]]:
    """aiohttp wants scalars; repeat a key for each item in a list value."""

    flat: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            flat.extend((key, str(item)) for item in value)
        else:
            flat.append((key, str(value)))
    return flat


def _int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
