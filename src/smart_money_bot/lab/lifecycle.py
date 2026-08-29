"""Authoritative per-mint lifecycle, re-alert suppression and re-entry (J, K, L).

The rule this module exists to enforce: **an old pump is never a fresh setup**.
A mint that surfaced at $32k, ran to $150k and fell back to $38k must come back
as a retraced old winner, not as a first discovery — and a Railway restart must
not change that, because the record is persisted and rehydrated, never rebuilt
from the current poll.

Cheap again is also not good again.  A re-entry requires *new* evidence: a base,
returning momentum, recovering volume, renewed independent buyers, stable
liquidity and no worsening coordination.  Anything less stays REENTRY_WATCH.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from .config import DEFAULT_LAB_CONFIG, LabConfig

ZERO = Decimal("0")

# --- lifecycle states (section J) -------------------------------------------
FIRST_DISCOVERY = "FIRST_DISCOVERY"
SILENT_WATCH = "SILENT_WATCH"
FIRST_QUALIFIED = "FIRST_QUALIFIED"
ACTIVE_SETUP = "ACTIVE_SETUP"
WINNER = "WINNER"
EXHAUSTED = "EXHAUSTED"
RETRACED = "RETRACED"
COOLDOWN = "COOLDOWN"
REENTRY_WATCH = "REENTRY_WATCH"
REENTRY_QUALIFIED = "REENTRY_QUALIFIED"
INVALIDATED = "INVALIDATED"

LIFECYCLE_STATES: tuple[str, ...] = (
    FIRST_DISCOVERY,
    SILENT_WATCH,
    FIRST_QUALIFIED,
    ACTIVE_SETUP,
    WINNER,
    EXHAUSTED,
    RETRACED,
    COOLDOWN,
    REENTRY_WATCH,
    REENTRY_QUALIFIED,
    INVALIDATED,
)

#: States that mean "this mint already had its cycle".
POST_CYCLE_STATES = frozenset(
    {EXHAUSTED, RETRACED, COOLDOWN, REENTRY_WATCH, REENTRY_QUALIFIED}
)

#: Republication triggers (section K).
TRIGGER_LIFECYCLE = "LIFECYCLE_TRANSITION"
TRIGGER_OPPORTUNITY = "OPPORTUNITY_IMPROVED"
TRIGGER_MOMENTUM = "MOMENTUM_ACCELERATED"
TRIGGER_ORGANIC = "ORGANIC_DEMAND_IMPROVED"
TRIGGER_SAFETY = "SAFETY_IMPROVED"
TRIGGER_BUYERS = "INDEPENDENT_BUYERS_INCREASED"
TRIGGER_LIQUIDITY = "LIQUIDITY_IMPROVED"
TRIGGER_SMART_MONEY = "SMART_WALLET_EVENT"
TRIGGER_REENTRY = "REENTRY_QUALIFIED"
TRIGGER_RISK = "RISK_DETERIORATED"
TRIGGER_PAPER_EXIT = "PAPER_EXIT_RELEVANT"


@dataclass(frozen=True, slots=True)
class LifecycleObservation:
    """One market observation the lifecycle may advance on."""

    observed_at: int
    price_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_usd: Decimal | None = None
    independent_buyers: int | None = None
    momentum_score: Decimal | None = None
    opportunity_score: Decimal | None = None
    organic_score: Decimal | None = None
    safety_status: str = "UNKNOWN"
    qualified: bool = False
    surfaced: bool = False
    liquidity_removed: bool = False
    rugged: bool = False
    cluster_supply_percent: Decimal | None = None
    top10_percent: Decimal | None = None


@dataclass(frozen=True, slots=True)
class TokenLifecycle:
    """The durable memory of everything this mint already did.

    Persisted verbatim, so a restart rehydrates the same history instead of
    re-discovering the token as new.
    """

    mint: str
    state: str = FIRST_DISCOVERY
    first_discovered_at: int = 0
    first_seen_at: int = 0
    first_surfaced_at: int | None = None
    first_surface_price_usd: Decimal | None = None
    first_surface_market_cap_usd: Decimal | None = None
    historical_high_price_usd: Decimal | None = None
    historical_high_market_cap_usd: Decimal | None = None
    historical_high_at: int | None = None
    max_return_from_surface_percent: Decimal | None = None
    max_drawdown_percent: Decimal | None = None
    current_drawdown_percent: Decimal | None = None
    last_price_usd: Decimal | None = None
    last_market_cap_usd: Decimal | None = None
    last_liquidity_usd: Decimal | None = None
    last_observed_at: int = 0
    last_qualified_at: int | None = None
    last_alert_at: int | None = None
    cooldown_until: int | None = None
    publications: int = 0
    qualification_count: int = 0
    cycle_count: int = 0
    paper_entries: int = 0
    paper_exits: int = 0
    realized_net_pnl_usd: Decimal = ZERO
    stable_observations: int = 0
    lower_lows: int = 0
    trough_price_usd: Decimal | None = None
    trough_at: int | None = None
    volume_at_trough_usd: Decimal | None = None
    buyers_at_trough: int | None = None
    safety_history: tuple[str, ...] = ()
    state_history: tuple[tuple[int, str], ...] = ()
    invalidated_reason: str = ""
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def is_fresh_setup(self) -> bool:
        """True only when this mint has never completed a cycle."""

        return self.cycle_count == 0 and self.state not in POST_CYCLE_STATES

    @property
    def is_reentry(self) -> bool:
        return not self.is_fresh_setup

    def cooldown_active(self, now: int) -> bool:
        return bool(self.cooldown_until and now < self.cooldown_until)

    def drawdown_from_peak(self, price: Decimal | None) -> Decimal | None:
        peak = self.historical_high_price_usd
        if peak is None or peak <= 0 or price is None:
            return None
        return max(ZERO, ((peak - price) / peak * 100)).quantize(Decimal("0.01"))

    def return_from_surface(self, price: Decimal | None) -> Decimal | None:
        base = self.first_surface_price_usd
        if base is None or base <= 0 or price is None:
            return None
        return ((price - base) / base * 100).quantize(Decimal("0.01"))


def new_lifecycle(mint: str, *, now: int) -> TokenLifecycle:
    return TokenLifecycle(
        mint=mint,
        state=FIRST_DISCOVERY,
        first_discovered_at=now,
        first_seen_at=now,
        last_observed_at=now,
        state_history=((now, FIRST_DISCOVERY),),
    )


def advance_lifecycle(
    record: TokenLifecycle,
    observation: LifecycleObservation,
    *,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> TokenLifecycle:
    """Fold one observation into the durable record.

    ``first_discovered_at``, ``first_surface_*`` and the historical high are
    write-once/high-water fields: nothing in this function can reset them, which
    is what makes an old pump stay an old pump across restarts.
    """

    now = observation.observed_at
    price = observation.price_usd
    market_cap = observation.market_cap_usd

    peak_price = _max(record.historical_high_price_usd, price)
    peak_market_cap = _max(record.historical_high_market_cap_usd, market_cap)
    peak_at = record.historical_high_at
    if price is not None and (
        record.historical_high_price_usd is None or price > record.historical_high_price_usd
    ):
        peak_at = now

    first_surfaced_at = record.first_surfaced_at
    surface_price = record.first_surface_price_usd
    surface_market_cap = record.first_surface_market_cap_usd
    if observation.surfaced and first_surfaced_at is None:
        first_surfaced_at = now
        surface_price = price
        surface_market_cap = market_cap

    drawdown = None
    if peak_price is not None and peak_price > 0 and price is not None:
        drawdown = max(ZERO, ((peak_price - price) / peak_price * 100)).quantize(Decimal("0.01"))
    max_drawdown = _max(record.max_drawdown_percent, drawdown)

    max_return = record.max_return_from_surface_percent
    if surface_price is not None and surface_price > 0 and peak_price is not None:
        candidate = ((peak_price - surface_price) / surface_price * 100).quantize(Decimal("0.01"))
        max_return = _max(max_return, candidate)

    # Base-formation tracking used by the re-entry engine.
    lower_lows = record.lower_lows
    stable = record.stable_observations
    trough_price = record.trough_price_usd
    trough_at = record.trough_at
    volume_at_trough = record.volume_at_trough_usd
    buyers_at_trough = record.buyers_at_trough
    if price is not None:
        if trough_price is None or price < trough_price:
            trough_price = price
            trough_at = now
            volume_at_trough = observation.volume_usd
            buyers_at_trough = observation.independent_buyers
            if record.last_price_usd is not None and price < record.last_price_usd:
                lower_lows += 1
                stable = 0
        elif record.last_price_usd is not None and price >= record.last_price_usd:
            stable += 1
        else:
            stable = 0

    safety_history = (*record.safety_history, observation.safety_status)[-20:]

    updated = replace(
        record,
        first_seen_at=record.first_seen_at or now,
        first_surfaced_at=first_surfaced_at,
        first_surface_price_usd=surface_price,
        first_surface_market_cap_usd=surface_market_cap,
        historical_high_price_usd=peak_price,
        historical_high_market_cap_usd=peak_market_cap,
        historical_high_at=peak_at,
        max_return_from_surface_percent=max_return,
        max_drawdown_percent=max_drawdown,
        current_drawdown_percent=drawdown,
        last_price_usd=price if price is not None else record.last_price_usd,
        last_market_cap_usd=market_cap if market_cap is not None else record.last_market_cap_usd,
        last_liquidity_usd=(
            observation.liquidity_usd
            if observation.liquidity_usd is not None
            else record.last_liquidity_usd
        ),
        last_observed_at=now,
        last_qualified_at=now if observation.qualified else record.last_qualified_at,
        qualification_count=record.qualification_count + (1 if observation.qualified else 0),
        stable_observations=stable,
        lower_lows=lower_lows,
        trough_price_usd=trough_price,
        trough_at=trough_at,
        volume_at_trough_usd=volume_at_trough,
        buyers_at_trough=buyers_at_trough,
        safety_history=safety_history,
    )

    next_state = _next_state(updated, observation, config=config)
    if next_state == updated.state:
        return updated

    cooldown_until = updated.cooldown_until
    cycle_count = updated.cycle_count
    if next_state == COOLDOWN:
        cooldown_until = now + config.cooldown_seconds
    if next_state in {EXHAUSTED, RETRACED, INVALIDATED} and updated.state not in POST_CYCLE_STATES:
        # The mint has now completed one full cycle.  Nothing later can make it
        # look like a first discovery again.
        cycle_count += 1
    if next_state == REENTRY_WATCH:
        cooldown_until = None

    return replace(
        updated,
        state=next_state,
        cooldown_until=cooldown_until,
        cycle_count=cycle_count,
        state_history=(*updated.state_history, (now, next_state))[-40:],
        invalidated_reason=(
            "liquidity removed or rug evidence"
            if next_state == INVALIDATED
            else updated.invalidated_reason
        ),
    )


def _next_state(
    record: TokenLifecycle,
    observation: LifecycleObservation,
    *,
    config: LabConfig,
) -> str:
    now = observation.observed_at
    state = record.state

    if observation.rugged or observation.liquidity_removed:
        return INVALIDATED
    if state == INVALIDATED:
        return INVALIDATED

    drawdown = record.current_drawdown_percent or ZERO
    gain = record.return_from_surface(record.last_price_usd)

    if state in {FIRST_DISCOVERY, SILENT_WATCH}:
        if observation.qualified:
            return FIRST_QUALIFIED
        if observation.surfaced:
            return SILENT_WATCH
        return state

    if state == FIRST_QUALIFIED:
        if gain is not None and gain >= config.winner_return_percent:
            return WINNER
        if observation.qualified or observation.surfaced:
            return ACTIVE_SETUP
        return state

    if state == ACTIVE_SETUP:
        if gain is not None and gain >= config.winner_return_percent:
            return WINNER
        if drawdown >= config.retraced_drawdown_percent:
            return RETRACED
        if drawdown >= config.exhaustion_drawdown_percent:
            return EXHAUSTED
        return state

    if state == WINNER:
        if drawdown >= config.retraced_drawdown_percent:
            return RETRACED
        if drawdown >= config.exhaustion_drawdown_percent:
            return EXHAUSTED
        return state

    if state == EXHAUSTED:
        if drawdown >= config.retraced_drawdown_percent:
            return RETRACED
        return state

    if state == RETRACED:
        return COOLDOWN

    if state == COOLDOWN:
        if record.cooldown_active(now):
            return COOLDOWN
        return REENTRY_WATCH

    if state in {REENTRY_WATCH, REENTRY_QUALIFIED}:
        if (
            drawdown >= config.retraced_drawdown_percent
            and record.lower_lows > config.reentry_max_lower_lows
        ):
            return RETRACED
        return state

    return state


@dataclass(frozen=True, slots=True)
class ReentryAssessment:
    """Whether a retraced token has earned a *new* look (section L)."""

    qualified: bool = False
    dead_cat: bool = False
    satisfied: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        return REENTRY_QUALIFIED if self.qualified else REENTRY_WATCH


def assess_reentry(
    record: TokenLifecycle,
    observation: LifecycleObservation,
    *,
    recent_prices: Sequence[Decimal] = (),
    liquidity_removed: bool = False,
    cluster_worsening: bool = False,
    concentration_worsening: bool = False,
    smart_money_accumulating: bool = False,
    distribution_fading: bool = False,
    route_healthy: bool = False,
    expected_net_edge_percent: Decimal | None = None,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> ReentryAssessment:
    """A legitimate re-entry needs new evidence, not just a lower price."""

    satisfied: list[str] = []
    missing: list[str] = []
    notes: list[str] = []

    if record.stable_observations >= config.reentry_min_stable_observations:
        satisfied.append("STABILIZED_BASE")
    else:
        missing.append("STABILIZED_BASE")

    if record.lower_lows <= config.reentry_max_lower_lows:
        satisfied.append("NO_CONTINUING_LOWER_LOWS")
    else:
        missing.append("NO_CONTINUING_LOWER_LOWS")

    momentum = observation.momentum_score
    if momentum is not None and momentum >= 40:
        satisfied.append("MOMENTUM_RETURNING")
    else:
        missing.append("MOMENTUM_RETURNING")

    if (
        observation.volume_usd is not None
        and record.volume_at_trough_usd
        and record.volume_at_trough_usd > 0
        and observation.volume_usd / record.volume_at_trough_usd
        >= config.reentry_min_volume_recovery_ratio
    ):
        satisfied.append("VOLUME_REACCELERATION")
    else:
        missing.append("VOLUME_REACCELERATION")

    if (
        observation.independent_buyers is not None
        and record.buyers_at_trough is not None
        and observation.independent_buyers > record.buyers_at_trough
    ):
        satisfied.append("INDEPENDENT_BUYER_GROWTH")
    else:
        missing.append("INDEPENDENT_BUYER_GROWTH")

    if observation.organic_score is not None and observation.organic_score >= 50:
        satisfied.append("ORGANIC_DEMAND_IMPROVING")
    else:
        missing.append("ORGANIC_DEMAND_IMPROVING")

    if not liquidity_removed and observation.liquidity_usd is not None:
        if (
            record.last_liquidity_usd is None
            or observation.liquidity_usd >= record.last_liquidity_usd * Decimal("0.9")
        ):
            satisfied.append("LIQUIDITY_STABLE_OR_GROWING")
        else:
            missing.append("LIQUIDITY_STABLE_OR_GROWING")
    else:
        missing.append("LIQUIDITY_STABLE_OR_GROWING")

    if not cluster_worsening:
        satisfied.append("NO_WORSENING_CLUSTERING")
    else:
        missing.append("NO_WORSENING_CLUSTERING")

    if not concentration_worsening:
        satisfied.append("NO_WORSENING_CONCENTRATION")
    else:
        missing.append("NO_WORSENING_CONCENTRATION")

    if smart_money_accumulating:
        satisfied.append("VERIFIED_SMART_ACCUMULATION")
    else:
        notes.append("No financially verified smart-wallet accumulation yet")

    if distribution_fading:
        satisfied.append("PRIOR_DISTRIBUTION_FADING")
    else:
        missing.append("PRIOR_DISTRIBUTION_FADING")

    if observation.safety_status == "PASS":
        satisfied.append("SAFETY_PASS")
    else:
        missing.append("SAFETY_PASS")

    if route_healthy:
        satisfied.append("HEALTHY_ROUTE")
    else:
        missing.append("HEALTHY_ROUTE")

    if (
        expected_net_edge_percent is not None
        and expected_net_edge_percent >= config.min_expected_net_edge_percent
    ):
        satisfied.append("SUFFICIENT_NET_EDGE")
    else:
        missing.append("SUFFICIENT_NET_EDGE")

    dead_cat = _is_dead_cat(record, observation, recent_prices, config=config)
    if dead_cat:
        notes.append("Bounce is small relative to the collapse and is not stabilizing")

    qualified = not missing and not dead_cat
    return ReentryAssessment(
        qualified=qualified,
        dead_cat=dead_cat,
        satisfied=tuple(satisfied),
        missing=tuple(missing),
        notes=tuple(notes),
    )


def _is_dead_cat(
    record: TokenLifecycle,
    observation: LifecycleObservation,
    recent_prices: Sequence[Decimal],
    *,
    config: LabConfig,
) -> bool:
    """A bounce that is small, unstable, or unsupported is a dead cat."""

    price = observation.price_usd
    trough = record.trough_price_usd
    if price is None or trough is None or trough <= 0:
        return False
    bounce = (price - trough) / trough * 100
    if bounce <= 0:
        return False
    if bounce > config.dead_cat_max_bounce_percent:
        return False
    if record.stable_observations >= config.reentry_min_stable_observations:
        return False
    if len(recent_prices) >= 3 and all(
        recent_prices[index] <= recent_prices[index - 1] for index in range(1, len(recent_prices))
    ):
        return True
    return record.lower_lows > config.reentry_max_lower_lows


@dataclass(frozen=True, slots=True)
class PublicationState:
    """What was last published for a mint, so identical cards are suppressed."""

    mint: str
    published_at: int = 0
    lifecycle_state: str = FIRST_DISCOVERY
    opportunity_score: Decimal = ZERO
    momentum_score: Decimal = ZERO
    organic_score: Decimal = ZERO
    safety_status: str = "UNKNOWN"
    independent_buyers: int = 0
    liquidity_usd: Decimal | None = None
    smart_wallets: int = 0
    decision: str = "WAIT"
    fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class RepublishDecision:
    should_publish: bool
    triggers: tuple[str, ...] = ()
    reason: str = ""


def should_republish(
    previous: PublicationState | None,
    *,
    now: int,
    lifecycle_state: str,
    opportunity_score: Decimal,
    momentum_score: Decimal,
    organic_score: Decimal,
    safety_status: str,
    independent_buyers: int,
    liquidity_usd: Decimal | None,
    smart_wallets: int,
    decision: str,
    risk_deteriorated: bool = False,
    paper_exit_event: bool = False,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> RepublishDecision:
    """Rediscovery alone is never enough — something must have really changed."""

    if previous is None:
        return RepublishDecision(True, (TRIGGER_LIFECYCLE,), "first publication")

    triggers: list[str] = []
    if lifecycle_state != previous.lifecycle_state:
        triggers.append(TRIGGER_LIFECYCLE)
    if lifecycle_state == REENTRY_QUALIFIED and previous.lifecycle_state == REENTRY_WATCH:
        triggers.append(TRIGGER_REENTRY)
    if opportunity_score - previous.opportunity_score >= config.republish_min_opportunity_gain:
        triggers.append(TRIGGER_OPPORTUNITY)
    if momentum_score - previous.momentum_score >= config.republish_min_momentum_gain:
        triggers.append(TRIGGER_MOMENTUM)
    if organic_score - previous.organic_score >= config.republish_min_organic_gain:
        triggers.append(TRIGGER_ORGANIC)
    if safety_status == "PASS" and previous.safety_status != "PASS":
        triggers.append(TRIGGER_SAFETY)
    if independent_buyers - previous.independent_buyers >= config.republish_min_buyer_gain:
        triggers.append(TRIGGER_BUYERS)
    if (
        liquidity_usd is not None
        and previous.liquidity_usd is not None
        and previous.liquidity_usd > 0
        and (liquidity_usd - previous.liquidity_usd) / previous.liquidity_usd * 100
        >= config.republish_min_liquidity_gain_percent
    ):
        triggers.append(TRIGGER_LIQUIDITY)
    if smart_wallets > previous.smart_wallets:
        triggers.append(TRIGGER_SMART_MONEY)
    if risk_deteriorated:
        triggers.append(TRIGGER_RISK)
    if paper_exit_event:
        triggers.append(TRIGGER_PAPER_EXIT)

    if not triggers:
        return RepublishDecision(False, (), "no material change since the last card")

    # Risk and lifecycle news is always worth publishing; quality improvements
    # still respect a minimum spacing so polling cannot spam the channel.
    urgent = {TRIGGER_RISK, TRIGGER_LIFECYCLE, TRIGGER_REENTRY, TRIGGER_PAPER_EXIT}
    if (
        not urgent.intersection(triggers)
        and now - previous.published_at < config.republish_min_seconds
    ):
        return RepublishDecision(
            False,
            tuple(triggers),
            "change is real but the minimum re-publication spacing has not elapsed",
        )
    return RepublishDecision(True, tuple(dict.fromkeys(triggers)), "material change detected")


def apply_reentry(
    record: TokenLifecycle,
    assessment: ReentryAssessment,
    *,
    now: int,
) -> TokenLifecycle:
    """Move a watched token to REENTRY_QUALIFIED only on a qualifying assessment."""

    if record.state not in {REENTRY_WATCH, REENTRY_QUALIFIED, COOLDOWN}:
        return record
    target = assessment.state
    if record.state == COOLDOWN and record.cooldown_active(now):
        return record
    if target == record.state:
        return record
    return replace(
        record,
        state=target,
        state_history=(*record.state_history, (now, target))[-40:],
    )


def record_publication(
    record: TokenLifecycle,
    *,
    now: int,
) -> TokenLifecycle:
    return replace(record, publications=record.publications + 1, last_alert_at=now)


def record_paper_entry(record: TokenLifecycle, *, now: int) -> TokenLifecycle:
    return replace(
        record,
        paper_entries=record.paper_entries + 1,
        state=(
            ACTIVE_SETUP
            if record.state in {REENTRY_QUALIFIED, FIRST_QUALIFIED}
            else record.state
        ),
        state_history=(
            (*record.state_history, (now, ACTIVE_SETUP))[-40:]
            if record.state in {REENTRY_QUALIFIED, FIRST_QUALIFIED}
            else record.state_history
        ),
    )


def record_paper_exit(
    record: TokenLifecycle,
    *,
    now: int,
    net_pnl_usd: Decimal,
) -> TokenLifecycle:
    del now
    return replace(
        record,
        paper_exits=record.paper_exits + 1,
        realized_net_pnl_usd=record.realized_net_pnl_usd + net_pnl_usd,
    )


def lifecycle_to_json(record: TokenLifecycle) -> str:
    return json.dumps(
        {
            "mint": record.mint,
            "state": record.state,
            "first_discovered_at": record.first_discovered_at,
            "first_seen_at": record.first_seen_at,
            "first_surfaced_at": record.first_surfaced_at,
            "first_surface_price_usd": _text(record.first_surface_price_usd),
            "first_surface_market_cap_usd": _text(record.first_surface_market_cap_usd),
            "historical_high_price_usd": _text(record.historical_high_price_usd),
            "historical_high_market_cap_usd": _text(record.historical_high_market_cap_usd),
            "historical_high_at": record.historical_high_at,
            "max_return_from_surface_percent": _text(record.max_return_from_surface_percent),
            "max_drawdown_percent": _text(record.max_drawdown_percent),
            "current_drawdown_percent": _text(record.current_drawdown_percent),
            "last_price_usd": _text(record.last_price_usd),
            "last_market_cap_usd": _text(record.last_market_cap_usd),
            "last_liquidity_usd": _text(record.last_liquidity_usd),
            "last_observed_at": record.last_observed_at,
            "last_qualified_at": record.last_qualified_at,
            "last_alert_at": record.last_alert_at,
            "cooldown_until": record.cooldown_until,
            "publications": record.publications,
            "qualification_count": record.qualification_count,
            "cycle_count": record.cycle_count,
            "paper_entries": record.paper_entries,
            "paper_exits": record.paper_exits,
            "realized_net_pnl_usd": str(record.realized_net_pnl_usd),
            "stable_observations": record.stable_observations,
            "lower_lows": record.lower_lows,
            "trough_price_usd": _text(record.trough_price_usd),
            "trough_at": record.trough_at,
            "volume_at_trough_usd": _text(record.volume_at_trough_usd),
            "buyers_at_trough": record.buyers_at_trough,
            "safety_history": list(record.safety_history),
            "state_history": [list(item) for item in record.state_history],
            "invalidated_reason": record.invalidated_reason,
            "notes": record.notes,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def lifecycle_from_json(raw: str) -> TokenLifecycle:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("lifecycle payload must be an object")
    state = str(payload.get("state") or FIRST_DISCOVERY)
    if state not in LIFECYCLE_STATES:
        state = FIRST_DISCOVERY
    return TokenLifecycle(
        mint=str(payload.get("mint") or ""),
        state=state,
        first_discovered_at=int(payload.get("first_discovered_at") or 0),
        first_seen_at=int(payload.get("first_seen_at") or 0),
        first_surfaced_at=_int_or_none(payload.get("first_surfaced_at")),
        first_surface_price_usd=_decimal(payload.get("first_surface_price_usd")),
        first_surface_market_cap_usd=_decimal(payload.get("first_surface_market_cap_usd")),
        historical_high_price_usd=_decimal(payload.get("historical_high_price_usd")),
        historical_high_market_cap_usd=_decimal(payload.get("historical_high_market_cap_usd")),
        historical_high_at=_int_or_none(payload.get("historical_high_at")),
        max_return_from_surface_percent=_decimal(payload.get("max_return_from_surface_percent")),
        max_drawdown_percent=_decimal(payload.get("max_drawdown_percent")),
        current_drawdown_percent=_decimal(payload.get("current_drawdown_percent")),
        last_price_usd=_decimal(payload.get("last_price_usd")),
        last_market_cap_usd=_decimal(payload.get("last_market_cap_usd")),
        last_liquidity_usd=_decimal(payload.get("last_liquidity_usd")),
        last_observed_at=int(payload.get("last_observed_at") or 0),
        last_qualified_at=_int_or_none(payload.get("last_qualified_at")),
        last_alert_at=_int_or_none(payload.get("last_alert_at")),
        cooldown_until=_int_or_none(payload.get("cooldown_until")),
        publications=int(payload.get("publications") or 0),
        qualification_count=int(payload.get("qualification_count") or 0),
        cycle_count=int(payload.get("cycle_count") or 0),
        paper_entries=int(payload.get("paper_entries") or 0),
        paper_exits=int(payload.get("paper_exits") or 0),
        realized_net_pnl_usd=_decimal(payload.get("realized_net_pnl_usd")) or ZERO,
        stable_observations=int(payload.get("stable_observations") or 0),
        lower_lows=int(payload.get("lower_lows") or 0),
        trough_price_usd=_decimal(payload.get("trough_price_usd")),
        trough_at=_int_or_none(payload.get("trough_at")),
        volume_at_trough_usd=_decimal(payload.get("volume_at_trough_usd")),
        buyers_at_trough=_int_or_none(payload.get("buyers_at_trough")),
        safety_history=tuple(str(item) for item in payload.get("safety_history") or ()),
        state_history=tuple(
            (int(item[0]), str(item[1]))
            for item in payload.get("state_history") or ()
            if isinstance(item, list | tuple) and len(item) == 2
        ),
        invalidated_reason=str(payload.get("invalidated_reason") or ""),
        notes=dict(payload.get("notes") or {}),
    )


def _max(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None
