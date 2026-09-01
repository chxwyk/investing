"""The execution abstraction, and the only implementation that exists: SHADOW.

Read :mod:`.` for why this is being built before it is needed.  The mechanics
that matter:

**A mode is a property of the record.**  ``ExecutionIntent.mode`` is set when the
intent is created and is part of its identity.  A shadow entry recorded today
stays ``SHADOW`` if every gate in the deployment is flipped tomorrow, because
nothing re-reads the mode off a live flag (section 82).

**The id is derived, not generated.**  ``client_order_id`` hashes the signal,
strategy, mint and attempt, so replaying the same signal after a restart yields
the same id.  A broker that honours idempotency keys then rejects the duplicate
instead of filling it twice (section 81).

**The precheck is a list, not a boolean.**  :func:`evaluate_precheck` returns
every unmet requirement.  A future live path that only knew "not ok" would tell
an operator nothing about which of eleven conditions failed (section 79).

There is no signer here, no key material, no RPC client and no swap call. The
package's own tests assert their absence, and so does the deploy self-check.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal

from .gates import LiveTradingGates

ZERO = Decimal("0")

#: Record the intent; move nothing.  The only mode this release implements.
MODE_SHADOW = "SHADOW"
#: A human approves each order.  Named for completeness; not implemented.
MODE_MANUAL_CONFIRM = "MANUAL_CONFIRM"
#: Unattended execution.  Named for completeness; not implemented.
MODE_LIVE_AUTO = "LIVE_AUTO"

MODES: tuple[str, ...] = (MODE_SHADOW, MODE_MANUAL_CONFIRM, MODE_LIVE_AUTO)

#: Modes that would move real funds if they were implemented.  They are not.
LIVE_MODES: frozenset[str] = frozenset({MODE_MANUAL_CONFIRM, MODE_LIVE_AUTO})

REJECT_MODE_UNSUPPORTED = "EXECUTION_MODE_NOT_IMPLEMENTED"
REJECT_LIVE_DISABLED = "LIVE_TRADING_DISABLED"
REJECT_NO_PROVIDER = "NO_EXECUTION_PROVIDER"
REJECT_PRECHECK_FAILED = "LIVE_PRECHECK_FAILED"


def client_order_id(
    *,
    signal_id: str,
    strategy_id: str,
    mint: str,
    side: str,
    attempt: int = 1,
) -> str:
    """A deterministic id for one intent.

    Derived rather than random on purpose: a process that dies between deciding
    and recording must, on restart, produce the identical id for the identical
    decision.  A random id would make that retry a second order.
    """

    material = f"{strategy_id}|{signal_id}|{mint}|{side.upper()}|{attempt}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"smb-{digest[:32]}"


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    """What we would do, recorded before anything is attempted."""

    mint: str
    side: str
    size_usd: Decimal
    #: The signal that produced this. Part of the idempotency key.
    signal_id: str = ""
    strategy_id: str = ""
    family: str = ""
    #: Persisted with the record, never re-read from a live flag.
    mode: str = MODE_SHADOW
    attempt: int = 1
    max_slippage_bps: int | None = None
    created_at: int = 0
    chain: str = "solana"

    @property
    def client_order_id(self) -> str:
        return client_order_id(
            signal_id=self.signal_id,
            strategy_id=self.strategy_id,
            mint=self.mint,
            side=self.side,
            attempt=self.attempt,
        )

    @property
    def simulated(self) -> bool:
        return self.mode == MODE_SHADOW

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "chain": self.chain,
            "side": self.side,
            "size_usd": str(self.size_usd),
            "signal_id": self.signal_id,
            "strategy_id": self.strategy_id,
            "family": self.family,
            "mode": self.mode,
            "attempt": self.attempt,
            "client_order_id": self.client_order_id,
            "max_slippage_bps": self.max_slippage_bps,
            "created_at": self.created_at,
            "simulated": self.simulated,
        }


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    """What happened.  ``real_money_spent_usd`` is zero and asserted to be."""

    intent: ExecutionIntent
    accepted: bool
    mode: str = MODE_SHADOW
    reason: str = ""
    provider: str = "shadow"
    #: Always ``0``.  A non-zero value here would mean this release shipped a
    #: live path, which its tests and deploy check both forbid.
    real_money_spent_usd: Decimal = ZERO
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def simulated(self) -> bool:
        return self.mode == MODE_SHADOW

    def to_json(self) -> dict[str, object]:
        return {
            "intent": self.intent.to_json(),
            "accepted": self.accepted,
            "mode": self.mode,
            "reason": self.reason,
            "provider": self.provider,
            "real_money_spent_usd": str(self.real_money_spent_usd),
            "simulated": self.simulated,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class LiveOrderPrecheck:
    """The eleven conditions a real buy would have to satisfy (section 79).

    Every field defaults to the unsafe answer, so a precheck built from partial
    information fails.  Nothing evaluates this in the live sense today; it
    exists so the requirements are written down and testable before they matter.
    """

    exact_mint: str = ""
    signal_age_seconds: int | None = None
    buy_route_available: bool = False
    sell_route_available: bool = False
    liquidity_usd: Decimal | None = None
    slippage_bps: int | None = None
    price_impact_bps: int | None = None
    position_size_usd: Decimal = ZERO
    open_exposure_usd: Decimal = ZERO
    daily_spend_usd: Decimal = ZERO
    daily_loss_usd: Decimal = ZERO
    provider_healthy: bool = False
    hard_safety_passed: bool = False

    # ---- limits ----------------------------------------------------------
    max_signal_age_seconds: int = 120
    min_liquidity_usd: Decimal = Decimal("15000")
    max_slippage_bps: int = 300
    max_price_impact_bps: int = 300
    max_position_usd: Decimal = Decimal("10")
    max_open_exposure_usd: Decimal = Decimal("50")
    max_daily_spend_usd: Decimal = Decimal("100")
    max_daily_loss_usd: Decimal = Decimal("50")


def evaluate_precheck(precheck: LiveOrderPrecheck) -> tuple[str, ...]:
    """Every unmet requirement, not just the first.

    An empty tuple means the *conditions* are met — it does not mean an order
    may be placed.  That still requires all three gates and an execution
    provider that can trade, and no such provider exists in this codebase.
    """

    failures: list[str] = []
    if not precheck.exact_mint:
        failures.append("NO_EXACT_MINT")
    if (
        precheck.signal_age_seconds is None
        or precheck.signal_age_seconds > precheck.max_signal_age_seconds
    ):
        failures.append("SIGNAL_STALE")
    if not precheck.buy_route_available:
        failures.append("NO_BUY_ROUTE")
    if not precheck.sell_route_available:
        # A position you cannot exit is not a position, it is a donation.
        failures.append("NO_SELL_ROUTE")
    if precheck.liquidity_usd is None or precheck.liquidity_usd < precheck.min_liquidity_usd:
        failures.append("LIQUIDITY_BELOW_FLOOR")
    if precheck.slippage_bps is None or precheck.slippage_bps > precheck.max_slippage_bps:
        failures.append("SLIPPAGE_ABOVE_LIMIT")
    if (
        precheck.price_impact_bps is None
        or precheck.price_impact_bps > precheck.max_price_impact_bps
    ):
        failures.append("PRICE_IMPACT_ABOVE_LIMIT")
    if precheck.position_size_usd > precheck.max_position_usd:
        failures.append("POSITION_ABOVE_LIMIT")
    if (
        precheck.open_exposure_usd + precheck.position_size_usd
        > precheck.max_open_exposure_usd
    ):
        failures.append("EXPOSURE_ABOVE_LIMIT")
    if (
        precheck.daily_spend_usd + precheck.position_size_usd
        > precheck.max_daily_spend_usd
    ):
        failures.append("DAILY_SPEND_ABOVE_LIMIT")
    if precheck.daily_loss_usd > precheck.max_daily_loss_usd:
        failures.append("DAILY_LOSS_ABOVE_LIMIT")
    if not precheck.provider_healthy:
        failures.append("PROVIDER_UNHEALTHY")
    if not precheck.hard_safety_passed:
        failures.append("HARD_SAFETY_NOT_PASSED")
    return tuple(failures)


class ExecutionProvider(ABC):
    """What a future broker would have to implement.

    Deliberately small.  A wide interface invites a partial implementation that
    happens to be able to send an order; this one cannot express anything but
    "record this intent" until somebody writes a second subclass on purpose.
    """

    name: str = "abstract"
    #: Whether this provider is capable of moving real funds.  The only
    #: implementation in this codebase answers ``False``.
    can_trade: bool = False

    @abstractmethod
    async def submit(self, intent: ExecutionIntent) -> ExecutionReceipt:
        """Record or place an intent, returning what actually happened."""

    def supports(self, mode: str) -> bool:
        return mode == MODE_SHADOW


class ShadowExecutionProvider(ExecutionProvider):
    """Records the intent and returns.  There is nothing behind it.

    This is not "live trading with a flag turned off" — there is no order path
    in this class to switch on.  A live mode is refused rather than executed,
    and the refusal names the gates that are shut so the eventual switch-on is
    an explicit act rather than a discovery.
    """

    name = "shadow"
    can_trade = False

    def __init__(self, *, gates: LiveTradingGates | None = None) -> None:
        self.gates = gates or LiveTradingGates()
        self.recorded: list[ExecutionIntent] = []

    async def submit(self, intent: ExecutionIntent) -> ExecutionReceipt:
        if intent.mode in LIVE_MODES:
            blocked = self.gates.blocked_by()
            return ExecutionReceipt(
                intent=intent,
                accepted=False,
                mode=intent.mode,
                provider=self.name,
                reason=REJECT_LIVE_DISABLED if blocked else REJECT_MODE_UNSUPPORTED,
                notes=(
                    "no execution provider in this build can place an order",
                    *(f"gate closed: {name}" for name in blocked),
                ),
            )
        if intent.mode != MODE_SHADOW:
            return ExecutionReceipt(
                intent=intent,
                accepted=False,
                mode=intent.mode,
                provider=self.name,
                reason=REJECT_MODE_UNSUPPORTED,
            )

        self.recorded.append(intent)
        return ExecutionReceipt(
            intent=intent,
            accepted=True,
            mode=MODE_SHADOW,
            provider=self.name,
            reason="",
            real_money_spent_usd=ZERO,
            notes=("simulated only — no wallet, no signer, no swap",),
        )
