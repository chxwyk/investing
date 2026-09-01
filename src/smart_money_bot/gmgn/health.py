"""Truthful provider states, and the one inference this module refuses to make.

A provider that is down tells you nothing about a token.  That sounds obvious
and it is the single easiest mistake to make in an alerting system: a timeout
becomes a missing safety field, a missing safety field becomes "no risk found",
and a card that should have said "we could not check" says nothing at all.

So every state here is about **the provider**, never about a token, and
:data:`UNKNOWN_STATES` names the ones where the honest downstream answer is
"unknown" rather than "clear" (section 41).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

#: Serving requests and returning rows.
ACTIVE = "ACTIVE"
#: Serving requests correctly and returning nothing.  An empty board is a real
#: answer — this exists so "no candidates" is never confused with "no provider".
ACTIVE_NO_EVENTS = "ACTIVE_NO_EVENTS"
#: No credential configured.
AUTH_MISSING = "AUTH_MISSING"
#: A credential was sent and refused.
AUTH_REJECTED = "AUTH_REJECTED"
#: Inside a rate-limit window; requests resume when it resets.
RATE_LIMITED = "RATE_LIMITED"
#: Banned for exceeding limits — a harder stop than a passing 429.
RATE_LIMIT_BANNED = "RATE_LIMIT_BANNED"
#: Requests are timing out.
TIMEOUT = "TIMEOUT"
#: Reachable but erroring, or the circuit breaker is open.
PROVIDER_DEGRADED = "PROVIDER_DEGRADED"
#: Switched off by configuration.  Not a fault.
DISABLED_BY_CONFIG = "DISABLED_BY_CONFIG"
#: Never called yet.
UNKNOWN = "UNKNOWN"

STATES: tuple[str, ...] = (
    ACTIVE,
    ACTIVE_NO_EVENTS,
    AUTH_MISSING,
    AUTH_REJECTED,
    RATE_LIMITED,
    RATE_LIMIT_BANNED,
    TIMEOUT,
    PROVIDER_DEGRADED,
    DISABLED_BY_CONFIG,
    UNKNOWN,
)

#: States in which the provider is answering questions.
HEALTHY_STATES: frozenset[str] = frozenset({ACTIVE, ACTIVE_NO_EVENTS})

#: States in which every field this provider would have supplied is UNKNOWN —
#: never "safe", never "clear", never zero (section 41).
UNKNOWN_STATES: frozenset[str] = frozenset(STATES) - HEALTHY_STATES

HUMAN_STATE: dict[str, str] = {
    ACTIVE: "answering normally",
    ACTIVE_NO_EVENTS: "answering normally, nothing to report",
    AUTH_MISSING: "no API key configured",
    AUTH_REJECTED: "the API key was refused",
    RATE_LIMITED: "rate limited — backing off until the window resets",
    RATE_LIMIT_BANNED: "rate-limit banned — stopped calling",
    TIMEOUT: "requests are timing out",
    PROVIDER_DEGRADED: "reachable but failing",
    DISABLED_BY_CONFIG: "switched off by configuration",
    UNKNOWN: "not called yet",
}


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """What the provider is doing, and what that does *not* let us conclude."""

    name: str = "gmgn"
    state: str = UNKNOWN
    last_success_at: int | None = None
    last_failure_at: int | None = None
    last_error: str = ""
    consecutive_failures: int = 0
    rate_limit_reset_at: int | None = None

    # ---- accounting (section 86) -----------------------------------------
    calls: int = 0
    cache_hits: int = 0
    coalesced: int = 0
    rate_limited: int = 0
    auth_errors: int = 0
    timeouts: int = 0
    breaker_skips: int = 0
    total_latency_ms: int = 0
    latencies_ms: tuple[int, ...] = field(default_factory=tuple)
    #: Times a GMGN answer changed a decision, and times it changed an alert.
    decision_impacts: int = 0
    alert_impacts: int = 0

    @property
    def healthy(self) -> bool:
        return self.state in HEALTHY_STATES

    @property
    def usable(self) -> bool:
        """Whether it is worth making a call right now."""

        return self.state not in {
            AUTH_MISSING,
            AUTH_REJECTED,
            RATE_LIMIT_BANNED,
            DISABLED_BY_CONFIG,
        }

    @property
    def mean_latency_ms(self) -> int | None:
        if not self.calls:
            return None
        return int(self.total_latency_ms / self.calls)

    @property
    def p95_latency_ms(self) -> int | None:
        """The slow tail, which is what actually costs an operator their edge."""

        if not self.latencies_ms:
            return None
        ordered = sorted(self.latencies_ms)
        index = min(len(ordered) - 1, int(len(ordered) * 0.95))
        return ordered[index]

    def human(self) -> str:
        return HUMAN_STATE.get(self.state, self.state)

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "state": self.state,
            "human": self.human(),
            "healthy": self.healthy,
            "usable": self.usable,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            # The error text is provider prose; it is truncated on the way in and
            # never carries a credential (see ``sanitise`` in the client).
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "rate_limit_reset_at": self.rate_limit_reset_at,
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "coalesced": self.coalesced,
            "rate_limited": self.rate_limited,
            "auth_errors": self.auth_errors,
            "timeouts": self.timeouts,
            "breaker_skips": self.breaker_skips,
            "mean_latency_ms": self.mean_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "decision_impacts": self.decision_impacts,
            "alert_impacts": self.alert_impacts,
        }


#: How many latency samples to keep for the percentile.  Bounded so a long-lived
#: process cannot grow this without limit.
_LATENCY_WINDOW = 256


def record_success(
    health: ProviderHealth,
    *,
    latency_ms: int,
    rows: int,
    now: int | None = None,
) -> ProviderHealth:
    """A successful call.  Zero rows is ``ACTIVE_NO_EVENTS``, not a failure."""

    moment = now if now is not None else int(time.time())
    return replace(
        health,
        state=ACTIVE if rows > 0 else ACTIVE_NO_EVENTS,
        last_success_at=moment,
        consecutive_failures=0,
        last_error="",
        rate_limit_reset_at=None,
        calls=health.calls + 1,
        total_latency_ms=health.total_latency_ms + max(0, latency_ms),
        latencies_ms=(*health.latencies_ms, max(0, latency_ms))[-_LATENCY_WINDOW:],
    )


def record_failure(
    health: ProviderHealth,
    *,
    state: str,
    error: str = "",
    latency_ms: int = 0,
    rate_limit_reset_at: int | None = None,
    now: int | None = None,
) -> ProviderHealth:
    """A failed call, recorded as what it was rather than as a token verdict."""

    moment = now if now is not None else int(time.time())
    return replace(
        health,
        state=state,
        last_failure_at=moment,
        last_error=error[:200],
        consecutive_failures=health.consecutive_failures + 1,
        rate_limit_reset_at=rate_limit_reset_at,
        calls=health.calls + 1,
        total_latency_ms=health.total_latency_ms + max(0, latency_ms),
        latencies_ms=(*health.latencies_ms, max(0, latency_ms))[-_LATENCY_WINDOW:],
        rate_limited=health.rate_limited
        + (1 if state in {RATE_LIMITED, RATE_LIMIT_BANNED} else 0),
        auth_errors=health.auth_errors
        + (1 if state in {AUTH_MISSING, AUTH_REJECTED} else 0),
        timeouts=health.timeouts + (1 if state == TIMEOUT else 0),
    )
