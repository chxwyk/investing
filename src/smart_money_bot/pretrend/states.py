"""The token state machine, and the rules that stop it from talking too much.

The operator's complaint was not "the bot is wrong", it was "the bot is loud".
Those need different fixes.  Being right is the model's job.  Being quiet is
this module's, and it does it with three mechanisms.

**States, not scores.**  A token is in exactly one state, transitions are
explicit and recorded, and only one state is allowed to ping.  ``WATCH`` exists
precisely so that "interesting" has somewhere to live that is not a Discord
message — the single largest source of noise in the previous design was that
mildly interesting and genuinely notable shared one channel.

**Escalation, not repetition.**  A token going 41% → 42% → 43% is not four
pieces of news, it is one.  A re-alert requires a *material* change: a state
transition, a probability escalation past a band boundary, a new independent
high-affinity trader, or the ground-truth entry itself.  Everything else is
suppressed and counted.

**Cooldowns that survive restarts.**  A cooldown held only in memory is a
cooldown that resets on every Railway redeploy, which is how a token gets
alerted five times in an afternoon by a system that believes it alerted once.
The state carries its own timestamps so persistence can restore it exactly.

The ordering constraint worth stating: ``TRENDING_CONFIRMED`` is driven by
ground truth, not by the model, so it always fires — it is the one message that
is never suppressed, because it is the message that tells the operator whether
the earlier ones were right.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

ZERO = Decimal("0")

# --- states (section 49) -----------------------------------------------------
STATE_DISCOVERED = "DISCOVERED"
STATE_OBSERVING = "OBSERVING"
STATE_WATCH = "WATCH"
STATE_PRE_TREND = "PRE_TREND"
STATE_TRENDING_CONFIRMED = "TRENDING_CONFIRMED"
STATE_COOLDOWN = "COOLDOWN"
STATE_DEAD = "DEAD"

TOKEN_STATES: tuple[str, ...] = (
    STATE_DISCOVERED,
    STATE_OBSERVING,
    STATE_WATCH,
    STATE_PRE_TREND,
    STATE_TRENDING_CONFIRMED,
    STATE_COOLDOWN,
    STATE_DEAD,
)

#: Only this state may ping a human, and only through :class:`AlertGate`.
PINGING_STATES: frozenset[str] = frozenset({STATE_PRE_TREND, STATE_TRENDING_CONFIRMED})

#: Legal transitions.  Anything else is a bug and is rejected, loudly.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_DISCOVERED: frozenset({STATE_OBSERVING, STATE_DEAD, STATE_TRENDING_CONFIRMED}),
    STATE_OBSERVING: frozenset(
        {STATE_WATCH, STATE_PRE_TREND, STATE_DEAD, STATE_TRENDING_CONFIRMED}
    ),
    STATE_WATCH: frozenset(
        {STATE_PRE_TREND, STATE_OBSERVING, STATE_DEAD, STATE_TRENDING_CONFIRMED}
    ),
    STATE_PRE_TREND: frozenset(
        {STATE_TRENDING_CONFIRMED, STATE_COOLDOWN, STATE_DEAD}
    ),
    STATE_TRENDING_CONFIRMED: frozenset({STATE_COOLDOWN, STATE_DEAD}),
    STATE_COOLDOWN: frozenset({STATE_OBSERVING, STATE_WATCH, STATE_DEAD}),
    STATE_DEAD: frozenset(),
}

# --- why an alert was or was not sent ---------------------------------------
REASON_STATE_ENTERED = "STATE_ENTERED_PRE_TREND"
REASON_PROBABILITY_ESCALATION = "PROBABILITY_ESCALATION"
REASON_NEW_QUALITY_TRADERS = "NEW_INDEPENDENT_QUALITY_TRADERS"
REASON_WALLET_CONVERGENCE = "PRETREND_WALLET_CONVERGENCE"
REASON_TREND_CONFIRMED = "FOMO_TREND_ENTER"

SUPPRESSED_COOLDOWN = "COOLDOWN_ACTIVE"
SUPPRESSED_NO_MATERIAL_CHANGE = "NO_MATERIAL_CHANGE"
SUPPRESSED_RATE_LIMIT = "HOURLY_RATE_LIMIT"
SUPPRESSED_BELOW_THRESHOLD = "BELOW_THRESHOLD"
SUPPRESSED_NOT_PINGING_STATE = "NOT_A_PINGING_STATE"

#: Probability bands.  A re-alert requires crossing into a higher band, so
#: intra-band drift is silent by construction.
PROBABILITY_BANDS: tuple[Decimal, ...] = (
    Decimal("0.05"),
    Decimal("0.10"),
    Decimal("0.20"),
    Decimal("0.35"),
    Decimal("0.50"),
    Decimal("0.75"),
)


def probability_band(probability: Decimal) -> int:
    """Which escalation band a probability falls in.  Higher is stronger."""

    band = 0
    for index, edge in enumerate(PROBABILITY_BANDS, start=1):
        if probability >= edge:
            band = index
    return band


class IllegalTransition(ValueError):
    """A transition the state machine does not allow."""


@dataclass(frozen=True, slots=True)
class TokenState:
    """One mint's lifecycle position, with everything persistence needs."""

    mint: str
    state: str = STATE_DISCOVERED
    entered_state_at: int = 0
    first_seen_at: int = 0
    last_evaluated_at: int = 0
    #: Highest calibrated probability ever assigned while in a pre-trend lane.
    best_probability: Decimal = ZERO
    last_probability: Decimal = ZERO
    last_band: int = 0
    #: When we last actually sent something to a human.
    last_alert_at: int | None = None
    alerts_sent: int = 0
    suppressed: int = 0
    #: The independent quality traders already reflected in a sent alert.
    alerted_quality_traders: int = 0
    cooldown_until: int = 0
    #: Ground truth, once it happens.  Write-once.
    trend_confirmed_at: int | None = None
    #: The first PRE_TREND alert, used to measure lead time honestly.
    first_pretrend_alert_at: int | None = None
    first_pretrend_probability: Decimal | None = None
    first_pretrend_market_cap_usd: Decimal | None = None
    history: tuple[tuple[int, str, str], ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "state": self.state,
            "entered_state_at": self.entered_state_at,
            "first_seen_at": self.first_seen_at,
            "last_evaluated_at": self.last_evaluated_at,
            "best_probability": str(self.best_probability),
            "last_probability": str(self.last_probability),
            "last_band": self.last_band,
            "last_alert_at": self.last_alert_at,
            "alerts_sent": self.alerts_sent,
            "suppressed": self.suppressed,
            "alerted_quality_traders": self.alerted_quality_traders,
            "cooldown_until": self.cooldown_until,
            "trend_confirmed_at": self.trend_confirmed_at,
            "first_pretrend_alert_at": self.first_pretrend_alert_at,
            "first_pretrend_probability": (
                None
                if self.first_pretrend_probability is None
                else str(self.first_pretrend_probability)
            ),
            "first_pretrend_market_cap_usd": (
                None
                if self.first_pretrend_market_cap_usd is None
                else str(self.first_pretrend_market_cap_usd)
            ),
            "history": [list(entry) for entry in self.history[-20:]],
        }

    @property
    def lead_seconds(self) -> int | None:
        """How early the first PRE_TREND alert was.  Negative means we were late."""

        if self.trend_confirmed_at is None or self.first_pretrend_alert_at is None:
            return None
        return self.trend_confirmed_at - self.first_pretrend_alert_at

    @property
    def predicted(self) -> bool:
        """Did we alert BEFORE the board entry?  Ties count as not predicted."""

        lead = self.lead_seconds
        return lead is not None and lead > 0


def transition(
    state: TokenState, *, to: str, at: int, reason: str = ""
) -> TokenState:
    """Move a token to a new state, recording why.  Illegal moves raise."""

    if to == state.state:
        return replace(state, last_evaluated_at=at)
    allowed = ALLOWED_TRANSITIONS.get(state.state, frozenset())
    if to not in allowed:
        raise IllegalTransition(
            f"{state.mint}: {state.state} -> {to} is not an allowed transition"
        )
    return replace(
        state,
        state=to,
        entered_state_at=at,
        last_evaluated_at=at,
        history=state.history + ((at, to, reason),),
    )


@dataclass(frozen=True, slots=True)
class AlertDecision:
    """Whether to send, and the named reason either way."""

    send: bool
    reason: str
    state: TokenState
    #: True only for the ground-truth confirmation, which is never suppressed.
    confirmation: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "send": self.send,
            "reason": self.reason,
            "confirmation": self.confirmation,
            "mint": self.state.mint,
            "state": self.state.state,
        }


@dataclass(frozen=True, slots=True)
class GateConfig:
    """The alert budget.  Every field is an explicit product decision."""

    #: Calibrated probability required to enter PRE_TREND.
    pretrend_threshold: Decimal = Decimal("0.20")
    #: Calibrated probability required to enter WATCH (silent).
    watch_threshold: Decimal = Decimal("0.05")
    #: No second alert for the same mint inside this window, whatever changes.
    cooldown_seconds: int = 1_800
    #: Hard ceiling across all mints.  Prefer three good alerts to eighty.
    max_alerts_per_hour: int = 4
    #: A re-alert also needs this many NEW independent quality traders.
    new_quality_traders_for_realert: int = 2
    #: After confirmation, how long a mint stays in COOLDOWN before re-observing.
    post_confirm_cooldown_seconds: int = 3_600


DEFAULT_GATE_CONFIG = GateConfig()


class AlertGate:
    """Decides what reaches a human.  Holds the cross-mint rate budget.

    The gate is deliberately the only place a ping can originate, so the hourly
    ceiling is a real ceiling rather than a per-lane suggestion — the previous
    design's alert rate was the *sum* of several lanes' independent limits.
    """

    def __init__(self, config: GateConfig = DEFAULT_GATE_CONFIG) -> None:
        self.config = config
        self._alert_times: list[int] = []
        self.sent = 0
        self.suppressed: dict[str, int] = {}

    def restore(self, alert_times: Sequence[int]) -> None:
        """Rebuild the rate window after a restart, from persisted timestamps."""

        self._alert_times = sorted(alert_times)

    def alerts_last_hour(self, *, now: int) -> int:
        self._alert_times = [at for at in self._alert_times if now - at < 3600]
        return len(self._alert_times)

    def _suppress(self, state: TokenState, reason: str) -> AlertDecision:
        self.suppressed[reason] = self.suppressed.get(reason, 0) + 1
        return AlertDecision(
            send=False, reason=reason, state=replace(state, suppressed=state.suppressed + 1)
        )

    # ------------------------------------------------------------------
    def evaluate(
        self,
        state: TokenState,
        *,
        probability: Decimal,
        now: int,
        independent_quality_traders: int = 0,
        market_cap_usd: Decimal | None = None,
        wallet_convergence: bool = False,
    ) -> AlertDecision:
        """Classify a candidate and decide whether it earns a message."""

        config = self.config
        updated = replace(
            state,
            last_evaluated_at=now,
            last_probability=probability,
            best_probability=max(state.best_probability, probability),
        )

        # State classification comes first and is independent of the alert
        # budget: a token is in PRE_TREND because the evidence says so, even if
        # we then decline to say so out loud.
        if probability >= config.pretrend_threshold:
            target = STATE_PRE_TREND
        elif probability >= config.watch_threshold:
            target = STATE_WATCH
        else:
            target = STATE_OBSERVING

        if updated.state == STATE_COOLDOWN and now < updated.cooldown_until:
            return self._suppress(updated, SUPPRESSED_COOLDOWN)

        if updated.state in {STATE_TRENDING_CONFIRMED, STATE_DEAD}:
            return self._suppress(updated, SUPPRESSED_NOT_PINGING_STATE)

        if updated.state == STATE_COOLDOWN:
            updated = transition(updated, to=STATE_OBSERVING, at=now, reason="cooldown elapsed")
        if updated.state == STATE_DISCOVERED:
            updated = transition(updated, to=STATE_OBSERVING, at=now, reason="first evaluation")

        entering_pretrend = target == STATE_PRE_TREND and updated.state != STATE_PRE_TREND
        if target != updated.state and target in ALLOWED_TRANSITIONS.get(
            updated.state, frozenset()
        ):
            updated = transition(
                updated,
                to=target,
                at=now,
                reason=f"probability {probability} crossed the {target} threshold",
            )

        if updated.state != STATE_PRE_TREND:
            return self._suppress(updated, SUPPRESSED_BELOW_THRESHOLD)

        # --- material-change test (section 53) ------------------------------
        band = probability_band(probability)
        new_traders = independent_quality_traders - updated.alerted_quality_traders
        material_reason = ""
        if entering_pretrend:
            material_reason = REASON_STATE_ENTERED
        elif band > updated.last_band:
            material_reason = REASON_PROBABILITY_ESCALATION
        elif new_traders >= config.new_quality_traders_for_realert:
            material_reason = REASON_NEW_QUALITY_TRADERS
        elif wallet_convergence:
            material_reason = REASON_WALLET_CONVERGENCE

        updated = replace(updated, last_band=max(band, updated.last_band))

        if not material_reason:
            return self._suppress(updated, SUPPRESSED_NO_MATERIAL_CHANGE)

        if updated.last_alert_at is not None and (
            now - updated.last_alert_at < config.cooldown_seconds
        ):
            return self._suppress(updated, SUPPRESSED_COOLDOWN)

        if self.alerts_last_hour(now=now) >= config.max_alerts_per_hour:
            return self._suppress(updated, SUPPRESSED_RATE_LIMIT)

        self._alert_times.append(now)
        self.sent += 1
        sent_state = replace(
            updated,
            last_alert_at=now,
            alerts_sent=updated.alerts_sent + 1,
            alerted_quality_traders=max(
                updated.alerted_quality_traders, independent_quality_traders
            ),
            first_pretrend_alert_at=(
                updated.first_pretrend_alert_at
                if updated.first_pretrend_alert_at is not None
                else now
            ),
            first_pretrend_probability=(
                updated.first_pretrend_probability
                if updated.first_pretrend_probability is not None
                else probability
            ),
            first_pretrend_market_cap_usd=(
                updated.first_pretrend_market_cap_usd
                if updated.first_pretrend_market_cap_usd is not None
                else market_cap_usd
            ),
        )
        return AlertDecision(send=True, reason=material_reason, state=sent_state)

    # ------------------------------------------------------------------
    def confirm(self, state: TokenState, *, at: int) -> AlertDecision:
        """Ground truth arrived.  This message is never suppressed.

        It is the only alert exempt from the budget, because it is the alert
        that tells the operator whether every other alert was right.  Making it
        compete for the hourly ceiling would mean losing the scoreboard exactly
        when the system is busiest.
        """

        confirmed = replace(
            state,
            trend_confirmed_at=(
                state.trend_confirmed_at if state.trend_confirmed_at is not None else at
            ),
            last_evaluated_at=at,
        )
        if state.state != STATE_TRENDING_CONFIRMED:
            try:
                confirmed = transition(
                    confirmed,
                    to=STATE_TRENDING_CONFIRMED,
                    at=at,
                    reason=REASON_TREND_CONFIRMED,
                )
            except IllegalTransition:
                # DEAD is terminal; a board entry for a mint we wrote off is a
                # finding about our own classifier, not a reason to corrupt the
                # state machine.
                return AlertDecision(
                    send=False,
                    reason=SUPPRESSED_NOT_PINGING_STATE,
                    state=confirmed,
                    confirmation=True,
                )
        confirmed = replace(
            confirmed,
            cooldown_until=at + self.config.post_confirm_cooldown_seconds,
            alerts_sent=confirmed.alerts_sent + 1,
            last_alert_at=at,
        )
        self.sent += 1
        return AlertDecision(
            send=True, reason=REASON_TREND_CONFIRMED, state=confirmed, confirmation=True
        )

    def stats(self, *, now: int) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "alerts_last_hour": self.alerts_last_hour(now=now),
            "max_alerts_per_hour": self.config.max_alerts_per_hour,
            "suppressed": dict(sorted(self.suppressed.items())),
            "suppressed_total": sum(self.suppressed.values()),
        }
