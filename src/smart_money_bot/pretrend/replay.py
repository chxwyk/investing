"""Historical replay: run the past through the production path, one tick at a time.

A backtest that reads a dataframe and vectorises a rule is not a simulation of
this system.  It is a simulation of a *different* system that happens to share
some formulas, and the gap between the two is where imaginary performance lives.

So replay here is a clock.  It advances in fixed steps; at each step it
truncates every input to that instant, calls the same
:func:`~smart_money_bot.pretrend.features.build_features`, the same model, and
the same :class:`~smart_money_bot.pretrend.states.AlertGate` that production
uses, and records what would have been sent.  The alert budget, the cooldowns
and the material-change rule all apply, because they change *which* alerts
happen and therefore the measured precision.

The no-look-ahead guarantee is structural:
:func:`~smart_money_bot.pretrend.forensics.truncate_state` rebuilds every
container bounded at the tick, so a feature cannot reach past it even if a
future collector appended to the underlying series.  The replay additionally
asserts this rather than trusting it — :attr:`ReplayResult.leakage` is populated
by the same audit the training path uses, and a non-empty list invalidates the
run.

Replay costs no provider credits: it reads only what was already persisted, so
comparing eight thresholds costs exactly what comparing one costs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Protocol

from .features import FeatureVector, PretrendState, build_features
from .forensics import truncate_state
from .leakage import LeakageFinding, LeakageReport, audit_dataset
from .model import Prediction
from .states import (
    STATE_DISCOVERED,
    AlertDecision,
    AlertGate,
    GateConfig,
    TokenState,
)

ZERO = Decimal("0")


class Scorer(Protocol):
    """Anything that turns feature values into a probability."""

    def predict(self, values: Mapping[str, Decimal | None]) -> Prediction: ...


@dataclass(frozen=True, slots=True)
class ReplayAlert:
    """One alert the replay would have sent, with the evidence behind it."""

    mint: str
    at: int
    probability: Decimal
    reason: str
    market_cap_usd: Decimal | None
    #: Filled in after the fact from ground truth; never visible to the decision.
    trend_entered_at: int | None = None

    @property
    def correct(self) -> bool | None:
        return None if self.trend_entered_at is None else self.trend_entered_at > self.at

    @property
    def lead_seconds(self) -> int | None:
        if self.trend_entered_at is None:
            return None
        return self.trend_entered_at - self.at

    def to_json(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "at": self.at,
            "probability": str(self.probability),
            "reason": self.reason,
            "market_cap_usd": (
                None if self.market_cap_usd is None else str(self.market_cap_usd)
            ),
            "trend_entered_at": self.trend_entered_at,
            "correct": self.correct,
            "lead_seconds": self.lead_seconds,
        }


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """What the replay produced, and whether it may be believed."""

    started_at: int
    ended_at: int
    tick_seconds: int
    ticks: int = 0
    evaluations: int = 0
    alerts: tuple[ReplayAlert, ...] = ()
    confirmations: int = 0
    suppressed: dict[str, int] = field(default_factory=dict)
    states: dict[str, TokenState] = field(default_factory=dict)
    leakage: tuple[LeakageFinding, ...] = ()
    vectors_audited: int = 0

    @property
    def clean(self) -> bool:
        return not self.leakage

    @property
    def span_seconds(self) -> int:
        return max(0, self.ended_at - self.started_at)

    @property
    def alerts_per_hour(self) -> Decimal | None:
        if self.span_seconds <= 0:
            return None
        return (
            Decimal(len(self.alerts)) * Decimal(3600) / Decimal(self.span_seconds)
        ).quantize(Decimal("0.01"))

    @property
    def precision(self) -> Decimal | None:
        """Share of alerts followed by a genuine first board entry.

        Alerts whose outcome is unknown (the token never entered, within our
        record) count as incorrect, not as excluded — excluding them would be
        the cherry-picking section 79 forbids.
        """

        if not self.alerts:
            return None
        correct = sum(1 for alert in self.alerts if alert.correct)
        return (Decimal(correct) / Decimal(len(self.alerts))).quantize(
            Decimal("0.000001")
        )

    @property
    def median_lead_seconds(self) -> int | None:
        leads = sorted(
            alert.lead_seconds
            for alert in self.alerts
            if alert.lead_seconds is not None and alert.lead_seconds > 0
        )
        if not leads:
            return None
        middle = len(leads) // 2
        if len(leads) % 2:
            return leads[middle]
        return (leads[middle - 1] + leads[middle]) // 2

    def to_json(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "span_seconds": self.span_seconds,
            "tick_seconds": self.tick_seconds,
            "ticks": self.ticks,
            "evaluations": self.evaluations,
            "alerts": len(self.alerts),
            "confirmations": self.confirmations,
            "precision": None if self.precision is None else str(self.precision),
            "alerts_per_hour": (
                None if self.alerts_per_hour is None else str(self.alerts_per_hour)
            ),
            "median_lead_seconds": self.median_lead_seconds,
            "suppressed": dict(sorted(self.suppressed.items())),
            "clean": self.clean,
            "vectors_audited": self.vectors_audited,
            "leakage": [finding.to_json() for finding in self.leakage[:20]],
            "alert_rows": [alert.to_json() for alert in self.alerts[:50]],
        }


def replay(
    states: Mapping[str, PretrendState],
    *,
    scorer: Scorer,
    started_at: int,
    ended_at: int,
    tick_seconds: int = 30,
    gate_config: GateConfig | None = None,
    trend_entries: Mapping[str, int] | None = None,
    audit: bool = True,
    on_alert: Callable[[ReplayAlert, FeatureVector], None] | None = None,
) -> ReplayResult:
    """Walk the clock forward, evaluating every mint at every tick.

    ``states`` maps mint to its *full* collected state; the replay truncates each
    one per tick.  ``trend_entries`` supplies ground truth and is used **only**
    after a decision is made — to confirm, and to label the alert — never as an
    input to the decision.
    """

    gate = AlertGate(gate_config or GateConfig())
    entries = dict(trend_entries or {})
    token_states: dict[str, TokenState] = {
        mint: TokenState(mint=mint, state=STATE_DISCOVERED, first_seen_at=started_at)
        for mint in states
    }
    alerts: list[ReplayAlert] = []
    audited: list[FeatureVector] = []
    confirmations = 0
    ticks = 0
    evaluations = 0

    moment = started_at
    while moment <= ended_at:
        ticks += 1
        for mint, full_state in states.items():
            entry_at = entries.get(mint)

            # Ground truth first: a mint already on the board is confirmed, and
            # confirmation removes it from the prediction population, exactly as
            # live.  This is also what stops the replay scoring a token it can
            # already see trending.
            if (
                entry_at is not None
                and entry_at <= moment
                and token_states[mint].trend_confirmed_at is None
            ):
                decision = gate.confirm(token_states[mint], at=entry_at)
                token_states[mint] = decision.state
                if decision.send:
                    confirmations += 1
                continue
            if token_states[mint].trend_confirmed_at is not None:
                continue

            truncated = truncate_state(full_state, at=moment)
            vector = build_features(truncated)
            if audit:
                audited.append(vector)
            prediction = scorer.predict(vector.values)
            evaluations += 1

            independent = vector.get("independent_quality_buyers")
            decision: AlertDecision = gate.evaluate(
                token_states[mint],
                probability=prediction.probability,
                now=moment,
                independent_quality_traders=int(independent or 0),
                market_cap_usd=vector.get("market_cap_usd"),
            )
            token_states[mint] = decision.state
            if decision.send:
                alert = ReplayAlert(
                    mint=mint,
                    at=moment,
                    probability=prediction.probability,
                    reason=decision.reason,
                    market_cap_usd=vector.get("market_cap_usd"),
                    trend_entered_at=entry_at,
                )
                alerts.append(alert)
                if on_alert is not None:
                    on_alert(alert, vector)
        moment += tick_seconds

    report: LeakageReport = (
        audit_dataset(audited) if audit else LeakageReport(findings=(), rows_checked=0)
    )
    return ReplayResult(
        started_at=started_at,
        ended_at=ended_at,
        tick_seconds=tick_seconds,
        ticks=ticks,
        evaluations=evaluations,
        alerts=tuple(alerts),
        confirmations=confirmations,
        suppressed=dict(gate.suppressed),
        states=token_states,
        leakage=report.findings,
        vectors_audited=report.rows_checked,
    )


@dataclass(frozen=True, slots=True)
class ThresholdSweep:
    """One threshold's cost and benefit, so the trade-off is visible."""

    threshold: Decimal
    alerts: int
    correct: int
    precision: Decimal | None
    alerts_per_hour: Decimal | None
    median_lead_seconds: int | None

    def to_json(self) -> dict[str, Any]:
        return {
            "threshold": str(self.threshold),
            "alerts": self.alerts,
            "correct": self.correct,
            "precision": None if self.precision is None else str(self.precision),
            "alerts_per_hour": (
                None if self.alerts_per_hour is None else str(self.alerts_per_hour)
            ),
            "median_lead_seconds": self.median_lead_seconds,
        }


def sweep_thresholds(
    states: Mapping[str, PretrendState],
    *,
    scorer: Scorer,
    started_at: int,
    ended_at: int,
    thresholds: Sequence[Decimal],
    tick_seconds: int = 30,
    base_config: GateConfig | None = None,
    trend_entries: Mapping[str, int] | None = None,
) -> tuple[ThresholdSweep, ...]:
    """Replay once per threshold so precision and alert rate can be traded off.

    This is the only defensible way to pick an operating point: the threshold
    that maximises precision is usually the one that fires twice a week, and
    that is a product decision, not a modelling one.  Run it on a validation
    period, never on the period you then report.
    """

    template = base_config or GateConfig()
    results: list[ThresholdSweep] = []
    for threshold in thresholds:
        outcome = replay(
            states,
            scorer=scorer,
            started_at=started_at,
            ended_at=ended_at,
            tick_seconds=tick_seconds,
            gate_config=replace(template, pretrend_threshold=threshold),
            trend_entries=trend_entries,
            audit=False,
        )
        results.append(
            ThresholdSweep(
                threshold=threshold,
                alerts=len(outcome.alerts),
                correct=sum(1 for alert in outcome.alerts if alert.correct),
                precision=outcome.precision,
                alerts_per_hour=outcome.alerts_per_hour,
                median_lead_seconds=outcome.median_lead_seconds,
            )
        )
    return tuple(results)
