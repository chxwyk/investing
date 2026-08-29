"""The one canonical decision representation used across the whole lab.

Section G of the product contract: trade eligibility must never be re-derived in
a Discord handler or a provider client.  Everything that wants to know "should
this be entered?" reads a :class:`TradeDecision` produced by the strategy layer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from ..constants import BOT_VERSION
from .config import STRATEGY_VERSION


class Decision(StrEnum):
    ENTRY = "ENTRY"
    WAIT = "WAIT"
    REJECT = "REJECT"
    COOLDOWN = "COOLDOWN"
    REENTRY_WATCH = "REENTRY_WATCH"
    REENTRY_QUALIFIED = "REENTRY_QUALIFIED"


class EvidenceQuality(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class SafetyStatus(StrEnum):
    PASS = "PASS"
    UNKNOWN = "UNKNOWN"
    FAIL = "FAIL"


#: Decisions that permit a simulated PAPER fill.  Nothing else may buy.
ENTRY_DECISIONS = frozenset({Decision.ENTRY, Decision.REENTRY_QUALIFIED})


class Reason(StrEnum):
    """Stable machine-readable reason identifiers (section BG).

    These strings are persisted with every decision, so they are append-only:
    renaming one would silently rewrite the meaning of historical decisions.
    """

    SAFETY_UNKNOWN = "SAFETY_UNKNOWN"
    SAFETY_FAIL = "SAFETY_FAIL"
    EDGE_CONSUMED = "EDGE_CONSUMED"
    ALREADY_EXTENDED = "ALREADY_EXTENDED"
    OLD_WINNER_HEAVY_DRAWDOWN = "OLD_WINNER_HEAVY_DRAWDOWN"
    REENTRY_NOT_STABILIZED = "REENTRY_NOT_STABILIZED"
    DEAD_CAT_BOUNCE = "DEAD_CAT_BOUNCE"
    CLUSTERED_SMART_MONEY = "CLUSTERED_SMART_MONEY"
    MANUFACTURED_ACTIVITY = "MANUFACTURED_ACTIVITY"
    SOCIAL_SIGNAL_LATE = "SOCIAL_SIGNAL_LATE"
    LIQUIDITY_TOO_WEAK = "LIQUIDITY_TOO_WEAK"
    PRICE_IMPACT_TOO_HIGH = "PRICE_IMPACT_TOO_HIGH"
    SLIPPAGE_TOO_HIGH = "SLIPPAGE_TOO_HIGH"
    EXPECTED_NET_EDGE_TOO_LOW = "EXPECTED_NET_EDGE_TOO_LOW"
    MOMENTUM_EXHAUSTED = "MOMENTUM_EXHAUSTED"
    REGIME_UNFAVOURABLE = "REGIME_UNFAVOURABLE"
    DATA_DEGRADED = "DATA_DEGRADED"
    DATA_UNKNOWN = "DATA_UNKNOWN"
    NOT_QUALIFIED = "NOT_QUALIFIED"
    ROUTE_UNAVAILABLE = "ROUTE_UNAVAILABLE"
    INDEPENDENT_BUYERS_TOO_FEW = "INDEPENDENT_BUYERS_TOO_FEW"
    CONCENTRATION_TOO_HIGH = "CONCENTRATION_TOO_HIGH"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    ALREADY_HOLDING = "ALREADY_HOLDING"
    NO_AVERAGE_DOWN = "NO_AVERAGE_DOWN"
    MAX_POSITIONS_REACHED = "MAX_POSITIONS_REACHED"
    MAX_EXPOSURE_REACHED = "MAX_EXPOSURE_REACHED"
    DAILY_LOSS_CAP = "DAILY_LOSS_CAP"
    BANKROLL_EXHAUSTED = "BANKROLL_EXHAUSTED"
    TRADING_PAUSED_DATA_CONTROL_RISK = "TRADING_PAUSED_DATA_CONTROL_RISK"
    LATENCY_TOO_HIGH = "LATENCY_TOO_HIGH"
    SIGNAL_STALE = "SIGNAL_STALE"
    OPPORTUNITY_COST = "OPPORTUNITY_COST"

    # Positive / supporting reasons
    SETUP_FRESH = "SETUP_FRESH"
    ORGANIC_DEMAND_CONFIRMED = "ORGANIC_DEMAND_CONFIRMED"
    INDEPENDENT_BUYERS_CONFIRMED = "INDEPENDENT_BUYERS_CONFIRMED"
    SMART_MONEY_INDEPENDENT = "SMART_MONEY_INDEPENDENT"
    AUTHENTIC_ECONOMIC_ACTIVITY = "AUTHENTIC_ECONOMIC_ACTIVITY"
    LIQUIDITY_SUFFICIENT = "LIQUIDITY_SUFFICIENT"
    NET_EDGE_SUFFICIENT = "NET_EDGE_SUFFICIENT"
    SAFETY_PASS = "SAFETY_PASS"
    REENTRY_STABILIZED = "REENTRY_STABILIZED"


HUMAN_REASONS: dict[str, str] = {
    Reason.SAFETY_UNKNOWN: "Safety evidence is incomplete — unknown never becomes pass",
    Reason.SAFETY_FAIL: "Safety checks failed",
    Reason.EDGE_CONSUMED: "The useful signal is already priced in",
    Reason.ALREADY_EXTENDED: "Move already largely completed",
    Reason.OLD_WINNER_HEAVY_DRAWDOWN: "Old winner still deep under its peak",
    Reason.REENTRY_NOT_STABILIZED: "Re-entry needs a base, not just a lower price",
    Reason.DEAD_CAT_BOUNCE: "Bounce looks like distribution, not recovery",
    Reason.CLUSTERED_SMART_MONEY: "Smart wallets share funding or timing",
    Reason.MANUFACTURED_ACTIVITY: "Activity looks manufactured, not organic",
    Reason.SOCIAL_SIGNAL_LATE: "The public signal arrived after the move",
    Reason.LIQUIDITY_TOO_WEAK: "Liquidity too thin to enter and exit",
    Reason.PRICE_IMPACT_TOO_HIGH: "Route price impact above the limit",
    Reason.SLIPPAGE_TOO_HIGH: "Expected slippage above the limit",
    Reason.EXPECTED_NET_EDGE_TOO_LOW: "Expected edge does not clear realistic costs",
    Reason.MOMENTUM_EXHAUSTED: "Momentum has exhausted",
    Reason.REGIME_UNFAVOURABLE: "Market regime is hostile to new risk",
    Reason.DATA_DEGRADED: "Providers disagree or evidence is stale",
    Reason.DATA_UNKNOWN: "Required evidence is unavailable",
    Reason.NOT_QUALIFIED: "Candidate has not qualified in the research funnel",
    Reason.ROUTE_UNAVAILABLE: "No usable buy or sell route",
    Reason.INDEPENDENT_BUYERS_TOO_FEW: "Too few independently funded buyers",
    Reason.CONCENTRATION_TOO_HIGH: "Holder or cluster concentration too high",
    Reason.COOLDOWN_ACTIVE: "Lifecycle cooldown is still active",
    Reason.ALREADY_HOLDING: "A simulated position is already open",
    Reason.NO_AVERAGE_DOWN: "Averaging down is never automatic",
    Reason.MAX_POSITIONS_REACHED: "Maximum concurrent simulated positions reached",
    Reason.MAX_EXPOSURE_REACHED: "Maximum simulated exposure reached",
    Reason.DAILY_LOSS_CAP: "Daily simulated loss cap reached",
    Reason.BANKROLL_EXHAUSTED: "Simulated bankroll cannot fund a position",
    Reason.TRADING_PAUSED_DATA_CONTROL_RISK: (
        "Trading paused — data, control or risk state is not trustworthy"
    ),
    Reason.LATENCY_TOO_HIGH: "Decision-to-fill latency too high to trust the quote",
    Reason.SIGNAL_STALE: "Signal is older than the freshness window",
    Reason.OPPORTUNITY_COST: "A better-ranked candidate has the remaining capital",
    Reason.SETUP_FRESH: "Fresh setup with no prior exhausted cycle",
    Reason.ORGANIC_DEMAND_CONFIRMED: "Organic demand confirmed",
    Reason.INDEPENDENT_BUYERS_CONFIRMED: "Independent buyer growth confirmed",
    Reason.SMART_MONEY_INDEPENDENT: "Independent high-quality wallets accumulating",
    Reason.AUTHENTIC_ECONOMIC_ACTIVITY: "Economic activity looks authentic",
    Reason.LIQUIDITY_SUFFICIENT: "Liquidity supports entry and exit",
    Reason.NET_EDGE_SUFFICIENT: "Expected net edge clears realistic costs",
    Reason.SAFETY_PASS: "Safety checks pass",
    Reason.REENTRY_STABILIZED: "Re-entry base confirmed by new evidence",
}


def human_reason(code: str) -> str:
    return HUMAN_REASONS.get(code, code.replace("_", " ").capitalize())


@dataclass(frozen=True, slots=True)
class TradeDecision:
    """One authoritative, immutable evaluation of one mint at one instant."""

    mint: str
    decision: Decision
    reason_codes: tuple[str, ...] = ()
    evidence_quality: EvidenceQuality = EvidenceQuality.UNKNOWN
    safety: SafetyStatus = SafetyStatus.UNKNOWN
    lifecycle_state: str = "FIRST_DISCOVERY"
    expected_net_edge_percent: Decimal | None = None
    edge_confidence: Decimal | None = None
    size_usd: Decimal = Decimal("0")
    price_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    strategy_version: str = STRATEGY_VERSION
    bot_version: str = BOT_VERSION
    config_hash: str = ""
    timestamp: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def human_reasons(self) -> tuple[str, ...]:
        return tuple(human_reason(code) for code in self.reason_codes)

    @property
    def entry_eligible(self) -> bool:
        """A simulated fill is allowed only for an explicit entry decision."""

        return self.decision in ENTRY_DECISIONS and self.size_usd > 0

    def with_reasons(self, *codes: str) -> TradeDecision:
        merged = tuple(dict.fromkeys((*self.reason_codes, *codes)))
        return TradeDecision(
            mint=self.mint,
            decision=self.decision,
            reason_codes=merged,
            evidence_quality=self.evidence_quality,
            safety=self.safety,
            lifecycle_state=self.lifecycle_state,
            expected_net_edge_percent=self.expected_net_edge_percent,
            edge_confidence=self.edge_confidence,
            size_usd=self.size_usd,
            price_usd=self.price_usd,
            market_cap_usd=self.market_cap_usd,
            strategy_version=self.strategy_version,
            bot_version=self.bot_version,
            config_hash=self.config_hash,
            timestamp=self.timestamp,
            evidence=dict(self.evidence),
        )


def decision_to_json(decision: TradeDecision) -> str:
    return json.dumps(
        {
            "mint": decision.mint,
            "decision": str(decision.decision),
            "reason_codes": list(decision.reason_codes),
            "evidence_quality": str(decision.evidence_quality),
            "safety": str(decision.safety),
            "lifecycle_state": decision.lifecycle_state,
            "expected_net_edge_percent": _decimal_text(decision.expected_net_edge_percent),
            "edge_confidence": _decimal_text(decision.edge_confidence),
            "size_usd": _decimal_text(decision.size_usd),
            "price_usd": _decimal_text(decision.price_usd),
            "market_cap_usd": _decimal_text(decision.market_cap_usd),
            "strategy_version": decision.strategy_version,
            "bot_version": decision.bot_version,
            "config_hash": decision.config_hash,
            "timestamp": decision.timestamp,
            "evidence": _jsonable(decision.evidence),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def decision_from_json(raw: str) -> TradeDecision:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("decision payload must be an object")
    return TradeDecision(
        mint=str(payload.get("mint") or ""),
        decision=Decision(str(payload.get("decision") or Decision.WAIT)),
        reason_codes=tuple(str(item) for item in payload.get("reason_codes") or ()),
        evidence_quality=EvidenceQuality(
            str(payload.get("evidence_quality") or EvidenceQuality.UNKNOWN)
        ),
        safety=SafetyStatus(str(payload.get("safety") or SafetyStatus.UNKNOWN)),
        lifecycle_state=str(payload.get("lifecycle_state") or "FIRST_DISCOVERY"),
        expected_net_edge_percent=_decimal_or_none(payload.get("expected_net_edge_percent")),
        edge_confidence=_decimal_or_none(payload.get("edge_confidence")),
        size_usd=_decimal_or_none(payload.get("size_usd")) or Decimal("0"),
        price_usd=_decimal_or_none(payload.get("price_usd")),
        market_cap_usd=_decimal_or_none(payload.get("market_cap_usd")),
        strategy_version=str(payload.get("strategy_version") or STRATEGY_VERSION),
        bot_version=str(payload.get("bot_version") or BOT_VERSION),
        config_hash=str(payload.get("config_hash") or ""),
        timestamp=int(payload.get("timestamp") or 0),
        evidence=dict(payload.get("evidence") or {}),
    )


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list | set):
        return [_jsonable(item) for item in value]
    return value
