"""Spend the provider budget deliberately, and stop pushing when it pushes back.

Three failure modes this exists to prevent, in the order they actually happen:

1. **One token becomes twenty calls.**  A candidate that reaches the scoring
   lane, the alert builder and the detail view asks for its own security row
   three times in the same second.  A short-TTL cache plus request coalescing
   collapses that to one call — coalescing is the half people forget, and it is
   the one that matters under load, because a cache only helps *after* the first
   call returns.
2. **A 429 becomes a storm.**  Retrying immediately after a rate limit is how a
   limit becomes a ban.  The window is respected, and the breaker opens.
3. **A dead provider costs a second per call.**  When it is failing, calls are
   skipped rather than attempted, and the skip is counted so the operator can
   see it in `/fomo profit view:providers`.

Pure logic and asyncio only: no HTTP client lives here, so the policy is
testable without a network.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

#: Result of asking the budget whether a call may proceed.
ALLOW = "ALLOW"
DENY_DISABLED = "DENY_DISABLED"
DENY_BUDGET = "DENY_BUDGET"
DENY_RATE_LIMITED = "DENY_RATE_LIMITED"
DENY_BREAKER_OPEN = "DENY_BREAKER_OPEN"


@dataclass(frozen=True, slots=True)
class BudgetConfig:
    """How much provider to use, and how quickly to stop using it."""

    #: Hard ceiling on calls per rolling hour.  A safety net, not a target.
    max_calls_per_hour: int = 900
    #: Ceiling per minute, so a burst cannot spend the hour in ninety seconds.
    max_calls_per_minute: int = 60
    #: How long a cached answer stays fresh, per endpoint class.
    default_ttl_seconds: int = 20
    #: Consecutive failures before the breaker opens.
    breaker_threshold: int = 5
    #: How long the breaker stays open before one probe is allowed through.
    breaker_seconds: int = 120
    #: Bounded retries for a transient failure (never for a rate limit).
    max_retries: int = 1
    #: Base delay for the bounded backoff.
    retry_base_seconds: float = 0.5


DEFAULT_BUDGET_CONFIG = BudgetConfig()

#: Per-endpoint freshness.  A trending board goes stale in seconds; a creator's
#: prior-token history does not change at all on the timescale of a trade.
TTL_BY_KIND: dict[str, int] = {
    "rank": 20,
    "trenches": 20,
    "hot_searches": 60,
    "token_signal": 15,
    "smartmoney": 900,
    "kol": 900,
    "token_info": 30,
    "token_security": 300,
    "pool_info": 300,
    "top_holders": 60,
    "top_traders": 60,
    "kline": 60,
    "created_tokens": 3_600,
    "wallet_stats": 900,
    "user_info": 3_600,
}


@dataclass(slots=True)
class _Entry:
    value: Any
    expires_at: float


class RequestBudget:
    """Cache, coalesce, ration and break the circuit for one provider.

    Not thread-safe and not meant to be: it belongs to one asyncio loop, which
    is where every caller in this codebase already lives.
    """

    def __init__(self, *, config: BudgetConfig = DEFAULT_BUDGET_CONFIG) -> None:
        self.config = config
        self._cache: dict[str, _Entry] = {}
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self._minute_calls: list[float] = []
        self._hour_calls: list[float] = []
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0
        self._rate_limited_until = 0.0
        # ---- counters, surfaced in provider accounting (section 86) -------
        self.cache_hits = 0
        self.coalesced = 0
        self.breaker_skips = 0
        self.budget_skips = 0
        self.calls_made = 0

    # ---- admission ----------------------------------------------------

    def admit(self, *, enabled: bool = True, now: float | None = None) -> str:
        """Whether one more call may be made right now, and if not, why not."""

        moment = now if now is not None else time.monotonic()
        if not enabled:
            return DENY_DISABLED
        if moment < self._rate_limited_until:
            return DENY_RATE_LIMITED
        if moment < self._breaker_open_until:
            return DENY_BREAKER_OPEN
        self._prune(moment)
        if len(self._minute_calls) >= self.config.max_calls_per_minute:
            return DENY_BUDGET
        if len(self._hour_calls) >= self.config.max_calls_per_hour:
            return DENY_BUDGET
        return ALLOW

    def _prune(self, moment: float) -> None:
        self._minute_calls = [item for item in self._minute_calls if moment - item < 60]
        self._hour_calls = [item for item in self._hour_calls if moment - item < 3_600]

    def note_call(self, *, now: float | None = None) -> None:
        moment = now if now is not None else time.monotonic()
        self._prune(moment)
        self._minute_calls.append(moment)
        self._hour_calls.append(moment)
        self.calls_made += 1

    # ---- outcome ------------------------------------------------------

    def note_success(self) -> None:
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    def note_failure(self, *, now: float | None = None) -> None:
        moment = now if now is not None else time.monotonic()
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.config.breaker_threshold:
            self._breaker_open_until = moment + self.config.breaker_seconds

    def note_rate_limited(
        self,
        *,
        reset_at_unix: int | None = None,
        now: float | None = None,
        wall_now: int | None = None,
    ) -> None:
        """Respect the window the provider named, or back off a default minute.

        ``x-ratelimit-reset`` is wall-clock unix seconds while the budget runs
        on a monotonic clock, so the wait is computed as a *duration* and then
        applied monotonically — that way an NTP step cannot turn a one-minute
        wait into an hour or into nothing.
        """

        moment = now if now is not None else time.monotonic()
        wall = wall_now if wall_now is not None else int(time.time())
        wait = 60.0
        if reset_at_unix is not None:
            wait = max(0.0, float(reset_at_unix - wall)) + 1.0
        self._rate_limited_until = moment + min(wait, 3_600.0)

    @property
    def rate_limited_for(self) -> float:
        return max(0.0, self._rate_limited_until - time.monotonic())

    @property
    def breaker_open(self) -> bool:
        return time.monotonic() < self._breaker_open_until

    def headroom(self, *, now: float | None = None) -> float:
        """Fraction of the minute budget still unspent, 0..1.

        The minute window is the one that bites first under a burst, so it is
        the one tier shedding reacts to.  Reported rather than acted on here:
        *which* call to drop is the endpoint registry's decision.
        """

        moment = now if now is not None else time.monotonic()
        self._prune(moment)
        ceiling = max(1, self.config.max_calls_per_minute)
        return max(0.0, 1.0 - (len(self._minute_calls) / ceiling))

    # ---- cache and coalescing -----------------------------------------

    def cache_key(self, kind: str, **params: object) -> str:
        """A stable key.  Sorted so two callers phrasing it differently agree."""

        parts = [f"{key}={params[key]!r}" for key in sorted(params)]
        return f"{kind}:" + "&".join(parts)

    def cached(self, key: str, *, now: float | None = None) -> tuple[bool, Any]:
        moment = now if now is not None else time.monotonic()
        entry = self._cache.get(key)
        if entry is None:
            return False, None
        if entry.expires_at <= moment:
            self._cache.pop(key, None)
            return False, None
        self.cache_hits += 1
        return True, entry.value

    def store(self, key: str, value: Any, *, ttl: int, now: float | None = None) -> None:
        moment = now if now is not None else time.monotonic()
        self._cache[key] = _Entry(value=value, expires_at=moment + max(1, ttl))

    def ttl_for(self, kind: str) -> int:
        return TTL_BY_KIND.get(kind, self.config.default_ttl_seconds)

    async def run(
        self,
        key: str,
        factory: Callable[[], Awaitable[Any]],
        *,
        ttl: int,
    ) -> Any:
        """Serve from cache, join an in-flight identical call, or make one.

        The in-flight join is what stops a burst of callers asking the same
        question at the same instant from becoming a burst of requests: the
        first one calls, the rest await its future.
        """

        hit, value = self.cached(key)
        if hit:
            return value

        pending = self._inflight.get(key)
        if pending is not None:
            self.coalesced += 1
            return await asyncio.shield(pending)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._inflight[key] = future
        try:
            result = await factory()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            if not future.done():
                future.set_exception(exc)
            # A failed future nobody awaits would log "exception never
            # retrieved"; this consumes it deliberately.
            future.exception()
            raise
        else:
            self.store(key, result, ttl=ttl)
            if not future.done():
                future.set_result(result)
            return result
        finally:
            self._inflight.pop(key, None)

    def retry_delay(self, attempt: int) -> float:
        """Bounded exponential backoff.  Never used for a rate limit."""

        return self.config.retry_base_seconds * (2 ** max(0, attempt - 1))

    def snapshot(self) -> dict[str, object]:
        return {
            "calls_made": self.calls_made,
            "cache_hits": self.cache_hits,
            "cache_entries": len(self._cache),
            "coalesced": self.coalesced,
            "breaker_skips": self.breaker_skips,
            "budget_skips": self.budget_skips,
            "breaker_open": self.breaker_open,
            "rate_limited_for_seconds": int(self.rate_limited_for),
            "calls_last_minute": len(self._minute_calls),
            "calls_last_hour": len(self._hour_calls),
            "consecutive_failures": self._consecutive_failures,
        }
