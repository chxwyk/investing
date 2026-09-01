"""Per-endpoint health and priority, so one optional feed cannot mute the rest.

The production failure this exists to fix, exactly: GMGN's **hot searches**
endpoint returned ``HTTP 429 RATE_LIMIT_EXCEEDED``, and because provider health
was a single global record, the whole integration flipped to ``RATE_LIMITED``.
Trending, trenches and market signals were all answering fine; the bot stopped
asking them anyway.

Hot search is *attention* evidence.  It is the least important thing GMGN tells
us, and it took the most important things down with it.

Two mechanisms fix that:

**Health is per endpoint.**  A 429 on hot searches cools hot searches.  The
overall state becomes a *summary* — ``CORE_ACTIVE`` while every tier-A feed is
answering, ``PARTIAL_DEGRADATION`` when something optional is cooling — so the
operator sees the truth rather than a single misleading word.

**Calls are tiered.**  Under quota pressure the cheap-to-lose endpoints are shed
first and the ones the product depends on are protected.  A budget that spends
its last call on a hot-search refresh has spent it on the wrong thing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

from .health import (
    ACTIVE,
    ACTIVE_NO_EVENTS,
    DISABLED_BY_CONFIG,
    RATE_LIMIT_BANNED,
    RATE_LIMITED,
    UNKNOWN,
)

# --- call tiers (section 18) -------------------------------------------------
#: Protect at all costs: without these the product has no discovery.
TIER_A = "A"
#: Valuable enrichment for candidates that already earned attention.
TIER_B = "B"
#: Nice to have.  First to be shed, and never at the expense of tier A.
TIER_C = "C"

TIERS: tuple[str, ...] = (TIER_A, TIER_B, TIER_C)

#: Endpoint kind → tier.  Kinds match the ``kind=`` argument the client passes,
#: so the mapping is checkable against the call sites rather than aspirational.
ENDPOINT_TIER: dict[str, str] = {
    # A — discovery.  Losing these is losing the release.
    "rank": TIER_A,
    "trenches": TIER_A,
    "token_signal": TIER_A,
    # B — enrichment for candidates that already matter.
    "smartmoney": TIER_B,
    "kol": TIER_B,
    "top_holders": TIER_B,
    "top_traders": TIER_B,
    "token_security": TIER_B,
    "token_info": TIER_B,
    # C — supplemental.  Hot search is the endpoint that caused the outage.
    "hot_searches": TIER_C,
    "kline": TIER_C,
    "pool_info": TIER_C,
    "created_tokens": TIER_C,
    "user_info": TIER_C,
}

#: Below this fraction of the minute budget remaining, tier C stops asking.
SHED_C_AT = 0.35
#: Below this, tier B stops too.  Tier A is never shed: if there is a call left,
#: discovery gets it.
SHED_B_AT = 0.15


def tier_for(kind: str) -> str:
    """The tier of an endpoint kind.  Unknown kinds are treated as tier B.

    Not tier A — a kind nobody classified has not earned protection — and not
    tier C, because silently shedding an unrecognised call would hide a feed
    somebody added and forgot to rank.
    """

    return ENDPOINT_TIER.get(kind, TIER_B)


@dataclass(frozen=True, slots=True)
class EndpointHealth:
    """What one GMGN endpoint is doing, independently of the others."""

    kind: str
    tier: str = TIER_B
    state: str = UNKNOWN
    last_success_at: int | None = None
    last_failure_at: int | None = None
    cooldown_until: int = 0
    rate_limit_hits: int = 0
    calls: int = 0
    failures: int = 0
    last_error: str = ""

    def cooling(self, *, now: int | None = None) -> bool:
        moment = now if now is not None else int(time.time())
        return self.cooldown_until > moment

    def cooldown_seconds(self, *, now: int | None = None) -> int:
        moment = now if now is not None else int(time.time())
        return max(0, self.cooldown_until - moment)

    @property
    def healthy(self) -> bool:
        return self.state in {ACTIVE, ACTIVE_NO_EVENTS}

    def to_json(self, *, now: int | None = None) -> dict[str, object]:
        return {
            "kind": self.kind,
            "tier": self.tier,
            "state": self.state,
            "healthy": self.healthy,
            "cooling": self.cooling(now=now),
            "cooldown_seconds": self.cooldown_seconds(now=now),
            "cooldown_until": self.cooldown_until,
            "rate_limit_hits": self.rate_limit_hits,
            "calls": self.calls,
            "failures": self.failures,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "last_error": self.last_error,
        }


# --- overall summaries (section 17) ------------------------------------------
#: Every tier-A endpoint is answering.
CORE_ACTIVE = "CORE_ACTIVE"
#: Core is fine; something optional is cooling or failing.
PARTIAL_DEGRADATION = "PARTIAL_DEGRADATION"
#: A tier-A endpoint is rate limited.  This one is worth shouting about.
CORE_RATE_LIMITED = "CORE_RATE_LIMITED"
#: A tier-A endpoint is failing for another reason.
CORE_DEGRADED = "CORE_DEGRADED"
#: Nothing has been called yet.
CORE_UNKNOWN = "CORE_UNKNOWN"

SUMMARY_LABELS: dict[str, str] = {
    CORE_ACTIVE: "core feeds answering normally",
    PARTIAL_DEGRADATION: "core feeds fine, an optional endpoint is degraded",
    CORE_RATE_LIMITED: "a core discovery feed is rate limited",
    CORE_DEGRADED: "a core discovery feed is failing",
    CORE_UNKNOWN: "no endpoint has been called yet",
}


class EndpointRegistry:
    """Per-endpoint health, and the admission decisions that follow from it.

    Deliberately small and synchronous.  Persisting cooldowns across a restart
    (section 21) is the caller's job — the registry exposes and accepts them so
    a redeploy does not walk straight back into a limit it was already serving.
    """

    def __init__(self) -> None:
        self._endpoints: dict[str, EndpointHealth] = {}
        #: Kinds an operator switched off.  Distinct from a cooldown: one is a
        #: choice and the other is a consequence, and a card should say which.
        self._disabled: set[str] = set()

    def get(self, kind: str) -> EndpointHealth:
        record = self._endpoints.get(kind)
        if record is None:
            record = EndpointHealth(kind=kind, tier=tier_for(kind))
            self._endpoints[kind] = record
        return record

    def all(self) -> tuple[EndpointHealth, ...]:
        return tuple(
            sorted(self._endpoints.values(), key=lambda item: (item.tier, item.kind))
        )

    def disable(self, kind: str) -> None:
        self._disabled.add(kind)
        self._endpoints[kind] = replace(
            self.get(kind), state=DISABLED_BY_CONFIG, cooldown_until=0
        )

    def note_success(self, kind: str, *, rows: int = 1, now: int | None = None) -> None:
        moment = now if now is not None else int(time.time())
        current = self.get(kind)
        self._endpoints[kind] = replace(
            current,
            state=ACTIVE if rows > 0 else ACTIVE_NO_EVENTS,
            last_success_at=moment,
            cooldown_until=0,
            calls=current.calls + 1,
            last_error="",
        )

    def note_failure(
        self,
        kind: str,
        *,
        state: str,
        error: str = "",
        cooldown_seconds: int = 0,
        now: int | None = None,
    ) -> None:
        """Record a failure against **this endpoint only**.

        A rate limit sets a cooldown that stops us probing during the window —
        section 19: the provider's answer is the truth, and continuing to knock
        is how a limit becomes a ban.
        """

        moment = now if now is not None else int(time.time())
        current = self.get(kind)
        limited = state in {RATE_LIMITED, RATE_LIMIT_BANNED}
        self._endpoints[kind] = replace(
            current,
            state=state,
            last_failure_at=moment,
            cooldown_until=(
                moment + max(1, cooldown_seconds) if cooldown_seconds else current.cooldown_until
            ),
            rate_limit_hits=current.rate_limit_hits + (1 if limited else 0),
            calls=current.calls + 1,
            failures=current.failures + 1,
            last_error=error[:200],
        )

    def restore(self, kind: str, *, state: str, cooldown_until: int, error: str = "") -> None:
        """Reload a persisted cooldown after a restart (section 21)."""

        current = self.get(kind)
        self._endpoints[kind] = replace(
            current, state=state, cooldown_until=cooldown_until, last_error=error[:200]
        )

    def admits(
        self,
        kind: str,
        *,
        budget_headroom: float = 1.0,
        now: int | None = None,
    ) -> tuple[bool, str]:
        """Whether this endpoint may be called, and why not when it may not.

        ``budget_headroom`` is the fraction of the minute budget still unspent.
        Tier C stops first, tier B next, tier A never — a call we can only make
        once should go to discovery, not to a hot-search refresh.
        """

        if kind in self._disabled:
            return False, DISABLED_BY_CONFIG
        record = self.get(kind)
        if record.cooling(now=now):
            return False, record.state if record.state != UNKNOWN else RATE_LIMITED
        tier = record.tier
        if tier == TIER_C and budget_headroom < SHED_C_AT:
            return False, "SHED_TIER_C"
        if tier == TIER_B and budget_headroom < SHED_B_AT:
            return False, "SHED_TIER_B"
        return True, ""

    def summary(self, *, now: int | None = None) -> str:
        """One honest word for the whole provider (section 17)."""

        records = [item for item in self._endpoints.values() if item.state != UNKNOWN]
        if not records:
            return CORE_UNKNOWN
        core = [item for item in records if item.tier == TIER_A]
        for item in core:
            if item.state in {RATE_LIMITED, RATE_LIMIT_BANNED} or (
                item.cooling(now=now) and item.state != DISABLED_BY_CONFIG
            ):
                return CORE_RATE_LIMITED
        if any(not item.healthy and item.state != DISABLED_BY_CONFIG for item in core):
            return CORE_DEGRADED
        optional = [item for item in records if item.tier != TIER_A]
        if any(
            not item.healthy and item.state != DISABLED_BY_CONFIG for item in optional
        ):
            return PARTIAL_DEGRADATION
        return CORE_ACTIVE if core else PARTIAL_DEGRADATION

    def to_json(self, *, now: int | None = None) -> dict[str, object]:
        summary = self.summary(now=now)
        return {
            "summary": summary,
            "summary_label": SUMMARY_LABELS.get(summary, summary),
            "endpoints": [item.to_json(now=now) for item in self.all()],
            "disabled": sorted(self._disabled),
        }


@dataclass(frozen=True, slots=True)
class ShedDecision:
    """What the budget decided to stop asking for, and why."""

    kind: str
    allowed: bool
    reason: str = ""
    tier: str = TIER_B
    notes: tuple[str, ...] = field(default_factory=tuple)
