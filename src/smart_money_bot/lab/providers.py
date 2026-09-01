"""Provider health, backoff and cost accounting (sections 10, 11, 24).

Production evidence is what motivated this module.  A live log window showed
Solana Tracker returning ``HTTP 403 {"error":"Insufficient credits"}`` on *every*
discovery refresh, once per minute, indefinitely — roughly 1,440 failing paid
requests a day against an intended budget of about 40.  Two defects combined to
produce it:

* the refresh throttle only engaged when the candidate pool was **non-empty**, so
  it disengaged exactly when the provider was failing, and
* the discovery client had no degraded window at all, so nothing slowed the
  retries down.

The rule this module encodes is simple: **a provider that is failing should be
called less, not more**, and a provider that is unavailable must degrade the
evidence to UNKNOWN rather than take the system down or fabricate a pass.

Nothing here performs I/O.  It is pure state and arithmetic that a client owns
and a report reads, which is what keeps it testable without a network.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal

ZERO = Decimal("0")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")

# --- health states -----------------------------------------------------------
HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
EXHAUSTED = "EXHAUSTED"

HEALTH_STATES: tuple[str, ...] = (HEALTHY, DEGRADED, EXHAUSTED)

#: HTTP statuses that mean "the plan is exhausted or throttled", as opposed to
#: "this record does not exist".  Only these open a degraded window.
CREDIT_STATUSES: frozenset[int] = frozenset({401, 402, 403, 429})

#: Backoff schedule, in seconds, indexed by consecutive credit failures.  It
#: tops out at an hour: a plan that is out of credits will not refill inside a
#: retry loop, so hammering it is pure waste.
BACKOFF_SECONDS: tuple[int, ...] = (60, 300, 900, 1_800, 3_600)

#: Phrases that mean the plan is out of credits rather than momentarily busy.
#: A quota does not refill in sixty seconds, so climbing the backoff ladder from
#: the bottom just means an hour of pointless 403s — production logged exactly
#: that on every discovery refresh.  These jump straight to the long window.
EXHAUSTION_PHRASES: tuple[str, ...] = (
    "insufficient credits",
    "credit limit",
    "quota exceeded",
    "out of credits",
    "plan limit",
)


def is_exhaustion(message: str) -> bool:
    lowered = (message or "").lower()
    return any(phrase in lowered for phrase in EXHAUSTION_PHRASES)

#: After this many consecutive credit failures the provider is reported as
#: EXHAUSTED rather than merely degraded, which is the state an operator needs
#: to see on the dashboard.
EXHAUSTED_AFTER_FAILURES = 3


@dataclass(frozen=True, slots=True)
class ProviderState:
    """One provider's live health.  Owned by the client, read by reports."""

    name: str
    calls: int = 0
    cache_hits: int = 0
    errors: int = 0
    credit_failures: int = 0
    consecutive_failures: int = 0
    degraded_until: float = 0.0
    last_error: str = ""
    last_success_at: float = 0.0
    #: Calls the breaker refused to make.  This is the saving, made visible.
    calls_skipped: int = 0

    def health(self, *, now: float) -> str:
        if self.consecutive_failures >= EXHAUSTED_AFTER_FAILURES and now < self.degraded_until:
            return EXHAUSTED
        if now < self.degraded_until:
            return DEGRADED
        return HEALTHY

    def is_degraded(self, *, now: float) -> bool:
        return now < self.degraded_until

    @property
    def cache_hit_rate_percent(self) -> Decimal | None:
        total = self.calls + self.cache_hits
        if total <= 0:
            return None
        return (Decimal(self.cache_hits) / Decimal(total) * HUNDRED).quantize(CENT)

    @property
    def error_rate_percent(self) -> Decimal | None:
        if self.calls <= 0:
            return None
        return (Decimal(self.errors) / Decimal(self.calls) * HUNDRED).quantize(CENT)


def record_success(state: ProviderState, *, now: float) -> ProviderState:
    """A good response clears the degraded window immediately."""

    return replace(
        state,
        calls=state.calls + 1,
        consecutive_failures=0,
        degraded_until=0.0,
        last_error="",
        last_success_at=now,
    )


def record_cache_hit(state: ProviderState) -> ProviderState:
    return replace(state, cache_hits=state.cache_hits + 1)


def record_skip(state: ProviderState) -> ProviderState:
    """A call the breaker refused to make — the saving, counted."""

    return replace(state, calls_skipped=state.calls_skipped + 1)


def record_failure(
    state: ProviderState,
    *,
    now: float,
    status: int | None = None,
    message: str = "",
    credit_related: bool | None = None,
) -> ProviderState:
    """Record a failure and, when it is a credit failure, open a backoff window.

    A 404 is *not* a credit failure — it means the record does not exist, and
    backing off because a token is unknown would be wrong.  Only the statuses in
    :data:`CREDIT_STATUSES` (or an explicit flag) slow the client down.
    """

    is_credit = (
        credit_related
        if credit_related is not None
        else (status is not None and status in CREDIT_STATUSES)
    )
    if not is_credit:
        return replace(
            state,
            calls=state.calls + 1,
            errors=state.errors + 1,
            last_error=message or state.last_error,
        )

    failures = state.consecutive_failures + 1
    # A stable "insufficient credits" is not a transient throttle: it is the
    # plan being spent, and it will still be spent in sixty seconds.  Open the
    # long window immediately rather than rediscovering it five times (§30).
    window = (
        BACKOFF_SECONDS[-1] if is_exhaustion(message) else backoff_seconds(failures)
    )
    return replace(
        state,
        calls=state.calls + 1,
        errors=state.errors + 1,
        credit_failures=state.credit_failures + 1,
        consecutive_failures=failures,
        degraded_until=now + window,
        last_error=message or state.last_error,
    )


def backoff_seconds(consecutive_failures: int) -> int:
    """Exponential backoff, capped.  Never zero once a failure has happened."""

    if consecutive_failures <= 0:
        return 0
    index = min(consecutive_failures, len(BACKOFF_SECONDS)) - 1
    return BACKOFF_SECONDS[index]


@dataclass(frozen=True, slots=True)
class ProviderFeature:
    """One thing a provider is used for, and whether it is worth paying for.

    ``essential`` answers "does the bot stop working without this?".  Only a
    feature with no on-chain substitute should ever be marked essential, because
    section 11 requires core detection and the SHADOW experiment to keep running
    when a paid provider is unavailable.
    """

    provider: str
    feature: str
    essential: bool = False
    on_chain_fallback: str = ""
    #: Whether the evidence this feature supplies actually changes a decision.
    decision_impact: str = "UNKNOWN"

    @property
    def replaceable(self) -> bool:
        return bool(self.on_chain_fallback) and not self.essential


#: The provider map this build actually uses.  It is written down so
#: `/fomo profit providers` can answer "where am I wasting money?" without the
#: operator reading the source.
PROVIDER_FEATURES: tuple[ProviderFeature, ...] = (
    ProviderFeature(
        provider="solana_tracker",
        feature="wallet discovery leaderboard",
        essential=False,
        on_chain_fallback="operator-tracked wallets + the realtime wallet stream",
        decision_impact=(
            "Adds new candidate wallets; the tracked set keeps working without it"
        ),
    ),
    ProviderFeature(
        provider="solana_tracker",
        feature="token risk enrichment",
        essential=False,
        on_chain_fallback="mint/freeze authority and holder concentration from RPC",
        decision_impact="Sharpens safety; absence must read UNKNOWN, never PASS",
    ),
    ProviderFeature(
        provider="dexscreener",
        feature="price, liquidity, volume, flow",
        essential=True,
        on_chain_fallback="",
        decision_impact="The market evidence every score and every SHADOW fill needs",
    ),
    ProviderFeature(
        provider="jupiter",
        feature="executable route quotes",
        essential=False,
        on_chain_fallback="Pump bonding-curve and pool-depth simulation",
        decision_impact="Best-quality fills; the venue model prices the trade without it",
    ),
    ProviderFeature(
        provider="solana_rpc",
        feature="transactions, wallet stream, on-chain state",
        essential=True,
        on_chain_fallback="",
        decision_impact="Public chain data the realtime lane and forensics read directly",
    ),
    ProviderFeature(
        provider="x",
        feature="social confirmation",
        essential=False,
        on_chain_fallback="",
        decision_impact="Narrative context only; never an entry gate",
    ),
)


@dataclass(frozen=True, slots=True)
class ProviderReport:
    """One row of `/fomo profit providers` (section 24)."""

    provider: str
    health: str = HEALTHY
    calls: int = 0
    cache_hits: int = 0
    errors: int = 0
    calls_skipped: int = 0
    cache_hit_rate_percent: Decimal | None = None
    error_rate_percent: Decimal | None = None
    degraded_seconds_remaining: int = 0
    last_error: str = ""
    features: tuple[ProviderFeature, ...] = field(default_factory=tuple)

    @property
    def essential(self) -> bool:
        return any(item.essential for item in self.features)

    @property
    def replaceable_features(self) -> tuple[str, ...]:
        return tuple(item.feature for item in self.features if item.replaceable)

    @property
    def wasted_calls(self) -> int:
        """Calls that returned nothing useful.  The number to drive to zero."""

        return self.errors


def build_provider_report(
    state: ProviderState,
    *,
    now: float,
    features: Sequence[ProviderFeature] = (),
) -> ProviderReport:
    return ProviderReport(
        provider=state.name,
        health=state.health(now=now),
        calls=state.calls,
        cache_hits=state.cache_hits,
        errors=state.errors,
        calls_skipped=state.calls_skipped,
        cache_hit_rate_percent=state.cache_hit_rate_percent,
        error_rate_percent=state.error_rate_percent,
        degraded_seconds_remaining=max(0, int(state.degraded_until - now)),
        last_error=state.last_error,
        features=tuple(
            features or [item for item in PROVIDER_FEATURES if item.provider == state.name]
        ),
    )


def cost_per_signals(calls: int, signals: int, *, per: int = 100) -> Decimal | None:
    """Provider calls per N published signals (section 21).

    Expressed as a *ratio* rather than dollars: request pricing differs per plan,
    and inventing a dollar figure the bot cannot verify would be exactly the kind
    of fabricated number the rest of this codebase refuses to produce.
    """

    if signals <= 0:
        return None
    return (Decimal(calls) / Decimal(signals) * Decimal(per)).quantize(CENT)


def degraded_evidence_note(state: ProviderState, *, now: float) -> str:
    """What a card should say when this provider is unavailable.

    Never "failed", never "PASS": an absent provider makes evidence *unknown*,
    and the difference is the whole of section 8.
    """

    health = state.health(now=now)
    if health == HEALTHY:
        return ""
    if health == EXHAUSTED:
        return f"UNKNOWN — {state.name} plan exhausted"
    return f"UNKNOWN — {state.name} temporarily unavailable"
