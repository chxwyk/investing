"""The simulated PAPER position and its exit engine (sections AL, AM, AN).

"Sell everything at +10%" is deliberately not the strategy.  The engine stages
profit-taking, protects break-even, trails a real peak, and de-risks when the
evidence that justified the entry stops holding — while allowing a genuinely
healthy runner to keep more upside.

Every partial and full exit writes an immutable journal entry with its own
realistic cost breakdown, so unrealized gain is never mistaken for realized
profit, and "was +110%, now +20%" is never scored like "never went green".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from .config import DEFAULT_LAB_CONFIG, LabConfig
from .costs import RealizedPnl, leg_costs

ZERO = Decimal("0")
UNIT = Decimal("0.000001")
HUNDRED = Decimal("100")

# --- exit reason codes -------------------------------------------------------
EXIT_MILESTONE = "MILESTONE_TAKE_PROFIT"
EXIT_BREAK_EVEN = "BREAK_EVEN_PROTECTION"
EXIT_TRAILING = "TRAILING_PROFIT_PROTECTION"
EXIT_MOMENTUM_DECAY = "MOMENTUM_DECAY"
EXIT_FLOW_REVERSAL = "BUY_FLOW_REVERSAL"
EXIT_VOLUME_EXHAUSTION = "VOLUME_EXHAUSTION"
EXIT_SMART_DISTRIBUTION = "SMART_MONEY_DISTRIBUTION"
EXIT_LIQUIDITY_DETERIORATION = "LIQUIDITY_DETERIORATION"
EXIT_LIQUIDITY_EMERGENCY = "LIQUIDITY_COLLAPSE_EMERGENCY"
EXIT_CONCENTRATION = "CONCENTRATION_DETERIORATION"
EXIT_SAFETY_EMERGENCY = "SAFETY_DETERIORATION"
EXIT_TIME_STOP = "TIME_STOP"
EXIT_HARD_STOP = "HARD_LOSS_PROTECTION"
EXIT_MOON_BAG = "MOON_BAG_REMAINDER"
HOLD_HEALTHY = "HEALTHY_RUNNER_HOLD"
HOLD_NO_TRIGGER = "NO_EXIT_TRIGGER"

#: Reference milestones the strategy is measured against.  They are reference
#: points, not an assertion that they are optimal.
REFERENCE_MILESTONES: tuple[Decimal, ...] = (
    Decimal("10"),
    Decimal("25"),
    Decimal("50"),
    Decimal("100"),
)


@dataclass(frozen=True, slots=True)
class ExitJournalEntry:
    """One immutable partial or full simulated exit (section AN)."""

    position_id: str
    mint: str
    sequence: int
    occurred_at: int
    quote_price_usd: Decimal
    fraction_sold: Decimal
    tokens_sold: Decimal
    tokens_remaining: Decimal
    gross_proceeds_usd: Decimal
    cost_basis_usd: Decimal
    costs: RealizedPnl = field(default_factory=RealizedPnl)
    reason_code: str = HOLD_NO_TRIGGER
    final: bool = False

    @property
    def net_proceeds_usd(self) -> Decimal:
        return (self.gross_proceeds_usd - self.costs.total_cost_usd).quantize(UNIT)

    @property
    def realized_net_pnl_usd(self) -> Decimal:
        return (self.net_proceeds_usd - self.cost_basis_usd).quantize(UNIT)

    @property
    def realized_gross_pnl_usd(self) -> Decimal:
        return (self.gross_proceeds_usd - self.cost_basis_usd).quantize(UNIT)


@dataclass(frozen=True, slots=True)
class PaperPosition:
    """A simulated position.  No signing path exists for it, by construction."""

    position_id: str
    mint: str
    opened_at: int
    entry_price_usd: Decimal
    entry_market_cap_usd: Decimal | None
    size_usd: Decimal
    tokens: Decimal
    strategy_version: str = ""
    config_hash: str = ""
    lifecycle_state: str = ""
    is_reentry: bool = False
    entry_reason_codes: tuple[str, ...] = ()
    entry_costs: RealizedPnl = field(default_factory=RealizedPnl)

    # live tracking (section AM)
    tokens_remaining: Decimal = ZERO
    cost_basis_remaining_usd: Decimal = ZERO
    realized_net_pnl_usd: Decimal = ZERO
    realized_gross_pnl_usd: Decimal = ZERO
    secured_proceeds_usd: Decimal = ZERO
    peak_price_usd: Decimal | None = None
    peak_market_cap_usd: Decimal | None = None
    peak_at: int | None = None
    trough_price_usd: Decimal | None = None
    max_favourable_percent: Decimal = ZERO
    max_adverse_percent: Decimal = ZERO
    milestones_taken: tuple[str, ...] = ()
    break_even_armed: bool = False
    trailing_armed: bool = False
    closed_at: int | None = None
    close_reason: str = ""
    last_price_usd: Decimal | None = None
    last_observed_at: int = 0
    exits: tuple[ExitJournalEntry, ...] = ()

    @property
    def is_open(self) -> bool:
        return self.closed_at is None and self.tokens_remaining > 0

    @property
    def remaining_fraction(self) -> Decimal:
        if self.tokens <= 0:
            return ZERO
        return (self.tokens_remaining / self.tokens).quantize(Decimal("0.0001"))

    def unrealized_percent(self, price: Decimal | None) -> Decimal | None:
        if price is None or self.entry_price_usd <= 0:
            return None
        return ((price - self.entry_price_usd) / self.entry_price_usd * HUNDRED).quantize(
            Decimal("0.01")
        )

    def unrealized_value_usd(self, price: Decimal | None) -> Decimal:
        if price is None:
            return ZERO
        return (self.tokens_remaining * price).quantize(UNIT)

    def drawdown_from_peak_percent(self, price: Decimal | None) -> Decimal | None:
        if price is None or self.peak_price_usd is None or self.peak_price_usd <= 0:
            return None
        return max(
            ZERO, ((self.peak_price_usd - price) / self.peak_price_usd * HUNDRED)
        ).quantize(Decimal("0.01"))


def open_position(
    *,
    position_id: str,
    mint: str,
    now: int,
    decision_price_usd: Decimal,
    fill_price_usd: Decimal | None = None,
    size_usd: Decimal,
    market_cap_usd: Decimal | None = None,
    price_impact_percent: Decimal | None = None,
    slippage_bps: int | None = None,
    strategy_version: str = "",
    config_hash: str = "",
    lifecycle_state: str = "",
    is_reentry: bool = False,
    entry_reason_codes: tuple[str, ...] = (),
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> PaperPosition:
    """Open a simulated position at a realistic fill, net of entry-leg costs."""

    fill = fill_price_usd if fill_price_usd is not None else decision_price_usd
    if fill <= 0:
        raise ValueError("fill price must be positive")
    costs = leg_costs(
        size_usd,
        price_impact_percent=price_impact_percent,
        slippage_bps=slippage_bps,
        config=config,
    )
    deployed = max(ZERO, (size_usd - costs.total_cost_usd))
    tokens = (deployed / fill).quantize(UNIT)
    return PaperPosition(
        position_id=position_id,
        mint=mint,
        opened_at=now,
        entry_price_usd=fill,
        entry_market_cap_usd=market_cap_usd,
        size_usd=size_usd,
        tokens=tokens,
        strategy_version=strategy_version,
        config_hash=config_hash,
        lifecycle_state=lifecycle_state,
        is_reentry=is_reentry,
        entry_reason_codes=entry_reason_codes,
        entry_costs=costs,
        tokens_remaining=tokens,
        cost_basis_remaining_usd=size_usd,
        peak_price_usd=fill,
        peak_market_cap_usd=market_cap_usd,
        peak_at=now,
        trough_price_usd=fill,
        last_price_usd=fill,
        last_observed_at=now,
    )


@dataclass(frozen=True, slots=True)
class ExitContext:
    """Current evidence for one open simulated position."""

    now: int
    price_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    entry_liquidity_usd: Decimal | None = None
    momentum_score: Decimal | None = None
    momentum_reaccelerating: bool = False
    organic_score: Decimal | None = None
    buys: int = 0
    sells: int = 0
    volume_usd: Decimal | None = None
    entry_volume_usd: Decimal | None = None
    smart_money_distributing: bool = False
    smart_money_accumulating: bool = False
    cluster_supply_percent: Decimal | None = None
    entry_cluster_supply_percent: Decimal | None = None
    safety_status: str = "PASS"
    route_available: bool = True
    price_impact_percent: Decimal | None = None
    slippage_bps: int | None = None

    @property
    def buy_sell_ratio(self) -> Decimal | None:
        if self.sells <= 0:
            return None if self.buys <= 0 else Decimal("99")
        return (Decimal(self.buys) / Decimal(self.sells)).quantize(Decimal("0.01"))

    @property
    def liquidity_change_percent(self) -> Decimal | None:
        if not self.entry_liquidity_usd or self.entry_liquidity_usd <= 0:
            return None
        if self.liquidity_usd is None:
            return None
        return (
            (self.liquidity_usd - self.entry_liquidity_usd) / self.entry_liquidity_usd * HUNDRED
        ).quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class ExitPlan:
    """What the engine wants to do with this position right now."""

    fraction: Decimal = ZERO
    reason_code: str = HOLD_NO_TRIGGER
    final: bool = False
    notes: tuple[str, ...] = ()

    @property
    def acts(self) -> bool:
        return self.fraction > 0


def plan_exit(
    position: PaperPosition,
    context: ExitContext,
    *,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> ExitPlan:
    """Decide the next simulated exit action for one open position."""

    if not position.is_open:
        return ExitPlan(reason_code=HOLD_NO_TRIGGER, notes=("position is closed",))

    price = context.price_usd
    if price is None or price <= 0:
        return ExitPlan(reason_code=HOLD_NO_TRIGGER, notes=("no usable current price",))

    gain = position.unrealized_percent(price) or ZERO

    # --- emergencies first -------------------------------------------------
    if context.safety_status == "FAIL":
        return ExitPlan(Decimal("1"), EXIT_SAFETY_EMERGENCY, True, ("safety deteriorated",))
    liquidity_change = context.liquidity_change_percent
    if (
        liquidity_change is not None
        and liquidity_change <= -config.liquidity_emergency_decline_percent
    ):
        return ExitPlan(
            Decimal("1"),
            EXIT_LIQUIDITY_EMERGENCY,
            True,
            (f"liquidity down {liquidity_change}% since entry",),
        )
    if not context.route_available:
        return ExitPlan(
            Decimal("1"), EXIT_LIQUIDITY_EMERGENCY, True, ("no sell route available",)
        )
    if gain <= -config.hard_stop_loss_percent:
        return ExitPlan(Decimal("1"), EXIT_HARD_STOP, True, (f"loss {gain}%",))

    # --- staged profit taking ---------------------------------------------
    for milestone, fraction in config.exit_milestones:
        key = str(milestone)
        if key in position.milestones_taken:
            continue
        if gain < milestone:
            continue
        healthy = _runner_is_healthy(context, config=config)
        if healthy and milestone >= Decimal("50"):
            # A genuinely healthy runner keeps more upside; the milestone is
            # still recorded so it is only ever skipped once.
            return ExitPlan(
                fraction / 2,
                EXIT_MILESTONE,
                False,
                (f"+{milestone}% reached while the runner is still healthy",),
            )
        return ExitPlan(fraction, EXIT_MILESTONE, False, (f"+{milestone}% reached",))

    # --- protection of an existing profit ---------------------------------
    drawdown = position.drawdown_from_peak_percent(price)
    if (
        position.trailing_armed
        and drawdown is not None
        and drawdown >= config.trailing_giveback_percent
    ):
        return ExitPlan(
            Decimal("1"),
            EXIT_TRAILING,
            True,
            (f"gave back {drawdown}% from the post-entry high",),
        )
    if position.break_even_armed and gain <= ZERO:
        return ExitPlan(
            Decimal("1"), EXIT_BREAK_EVEN, True, ("profit gave back to break-even",)
        )

    # --- evidence deterioration -------------------------------------------
    if context.smart_money_distributing and _flow_weakening(context, config=config):
        return ExitPlan(
            Decimal("0.5"),
            EXIT_SMART_DISTRIBUTION,
            False,
            ("smart wallets distributing into weakening flow",),
        )
    if (
        context.momentum_score is not None
        and context.momentum_score <= config.momentum_decay_exit_score
        and not context.momentum_reaccelerating
    ):
        return ExitPlan(Decimal("0.5"), EXIT_MOMENTUM_DECAY, False, ("momentum decayed",))
    if _flow_reversed(context, config=config):
        return ExitPlan(Decimal("0.5"), EXIT_FLOW_REVERSAL, False, ("buy flow reversed",))
    if _volume_exhausted(context):
        return ExitPlan(Decimal("0.4"), EXIT_VOLUME_EXHAUSTION, False, ("volume exhausted",))
    if liquidity_change is not None and liquidity_change <= Decimal("-25"):
        return ExitPlan(
            Decimal("0.5"),
            EXIT_LIQUIDITY_DETERIORATION,
            False,
            (f"liquidity down {liquidity_change}%",),
        )
    if (
        context.cluster_supply_percent is not None
        and context.entry_cluster_supply_percent is not None
        and context.cluster_supply_percent - context.entry_cluster_supply_percent >= Decimal("10")
    ):
        return ExitPlan(
            Decimal("0.5"), EXIT_CONCENTRATION, False, ("cluster concentration worsened",)
        )
    if context.safety_status == "UNKNOWN" and position.tokens_remaining > 0 and gain > ZERO:
        return ExitPlan(
            Decimal("0.5"),
            EXIT_SAFETY_EMERGENCY,
            False,
            ("safety evidence became unknown while in profit",),
        )

    # --- moon-bag remainder ------------------------------------------------
    ladder_complete = len(position.milestones_taken) >= len(config.exit_milestones)
    if (
        ladder_complete
        and config.moon_bag_percent > 0
        and not _runner_is_healthy(context, config=config)
    ):
        # The staged ladder already secured this position; what is left is the
        # optional moon bag.  Every partial de-risk trigger above fires first,
        # so this only releases a remainder that nothing else claimed and that
        # is no longer backed by a healthy runner.
        return ExitPlan(
            Decimal("1"), EXIT_MOON_BAG, True, ("moon-bag remainder released",)
        )

    # --- time stop ---------------------------------------------------------
    held = context.now - position.opened_at
    if held >= config.time_stop_seconds and gain < config.time_stop_min_progress_percent:
        return ExitPlan(Decimal("1"), EXIT_TIME_STOP, True, ("time stop with no progress",))

    if _runner_is_healthy(context, config=config) and gain > ZERO:
        return ExitPlan(ZERO, HOLD_HEALTHY, False, ("independent demand still healthy",))
    return ExitPlan(ZERO, HOLD_NO_TRIGGER, False, ())


def observe(
    position: PaperPosition,
    context: ExitContext,
    *,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> PaperPosition:
    """Update peak / drawdown / arming state from one observation."""

    price = context.price_usd
    if price is None or price <= 0:
        return position

    peak = position.peak_price_usd
    peak_market_cap = position.peak_market_cap_usd
    peak_at = position.peak_at
    if peak is None or price > peak:
        peak = price
        peak_at = context.now
        peak_market_cap = context.market_cap_usd or peak_market_cap
    trough = position.trough_price_usd
    if trough is None or price < trough:
        trough = price

    gain = position.unrealized_percent(price) or ZERO
    mfe = max(position.max_favourable_percent, gain)
    mae = max(position.max_adverse_percent, -gain if gain < 0 else ZERO)

    break_even = position.break_even_armed or mfe >= config.break_even_arm_percent
    trailing = position.trailing_armed or mfe >= config.trailing_arm_percent

    return replace(
        position,
        peak_price_usd=peak,
        peak_market_cap_usd=peak_market_cap,
        peak_at=peak_at,
        trough_price_usd=trough,
        max_favourable_percent=mfe,
        max_adverse_percent=mae,
        break_even_armed=break_even,
        trailing_armed=trailing,
        last_price_usd=price,
        last_observed_at=context.now,
    )


def apply_exit(
    position: PaperPosition,
    plan: ExitPlan,
    context: ExitContext,
    *,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> tuple[PaperPosition, ExitJournalEntry | None]:
    """Execute a planned simulated exit and append its immutable journal entry."""

    if not plan.acts or not position.is_open:
        return position, None
    price = context.price_usd
    if price is None or price <= 0:
        return position, None

    fraction = min(Decimal("1"), max(ZERO, plan.fraction))
    tokens_sold = (position.tokens_remaining * fraction).quantize(UNIT)
    if not plan.final and config.moon_bag_percent > 0:
        # Staged profit taking always leaves the optional moon bag behind; only
        # a final or emergency exit may sell the remainder.
        moon_bag = (position.tokens * config.moon_bag_percent / HUNDRED).quantize(UNIT)
        tokens_sold = min(tokens_sold, max(ZERO, position.tokens_remaining - moon_bag))
    if tokens_sold <= 0:
        return position, None
    fraction = (
        (tokens_sold / position.tokens_remaining)
        if position.tokens_remaining > 0
        else ZERO
    )

    remaining_after = (position.tokens_remaining - tokens_sold).quantize(UNIT)
    gross = (tokens_sold * price).quantize(UNIT)
    costs = leg_costs(
        gross,
        price_impact_percent=context.price_impact_percent,
        slippage_bps=context.slippage_bps,
        config=config,
    )
    basis_fraction = (
        (tokens_sold / position.tokens_remaining) if position.tokens_remaining > 0 else ZERO
    )
    cost_basis = (position.cost_basis_remaining_usd * basis_fraction).quantize(UNIT)

    entry = ExitJournalEntry(
        position_id=position.position_id,
        mint=position.mint,
        sequence=len(position.exits) + 1,
        occurred_at=context.now,
        quote_price_usd=price,
        fraction_sold=fraction.quantize(Decimal("0.0001")),
        tokens_sold=tokens_sold,
        tokens_remaining=remaining_after,
        gross_proceeds_usd=gross,
        cost_basis_usd=cost_basis,
        costs=costs,
        reason_code=plan.reason_code,
        final=plan.final or remaining_after <= 0,
    )

    milestones = position.milestones_taken
    if plan.reason_code == EXIT_MILESTONE:
        gain = position.unrealized_percent(price) or ZERO
        reached = [
            str(milestone)
            for milestone, _ in config.exit_milestones
            if gain >= milestone and str(milestone) not in milestones
        ]
        milestones = (*milestones, *reached)

    updated = replace(
        position,
        tokens_remaining=remaining_after,
        cost_basis_remaining_usd=(position.cost_basis_remaining_usd - cost_basis).quantize(UNIT),
        realized_net_pnl_usd=(position.realized_net_pnl_usd + entry.realized_net_pnl_usd).quantize(
            UNIT
        ),
        realized_gross_pnl_usd=(
            position.realized_gross_pnl_usd + entry.realized_gross_pnl_usd
        ).quantize(UNIT),
        secured_proceeds_usd=(position.secured_proceeds_usd + entry.net_proceeds_usd).quantize(
            UNIT
        ),
        milestones_taken=milestones,
        exits=(*position.exits, entry),
        closed_at=context.now if entry.final else position.closed_at,
        close_reason=plan.reason_code if entry.final else position.close_reason,
    )
    return updated, entry


def position_to_json(position: PaperPosition) -> str:
    return json.dumps(_position_payload(position), separators=(",", ":"), sort_keys=True)


def position_from_json(raw: str) -> PaperPosition:
    payload = json.loads(raw)
    exits = tuple(_journal_from_payload(item) for item in payload.get("exits") or ())
    return PaperPosition(
        position_id=str(payload.get("position_id") or ""),
        mint=str(payload.get("mint") or ""),
        opened_at=int(payload.get("opened_at") or 0),
        entry_price_usd=_decimal(payload.get("entry_price_usd")) or ZERO,
        entry_market_cap_usd=_decimal(payload.get("entry_market_cap_usd")),
        size_usd=_decimal(payload.get("size_usd")) or ZERO,
        tokens=_decimal(payload.get("tokens")) or ZERO,
        strategy_version=str(payload.get("strategy_version") or ""),
        config_hash=str(payload.get("config_hash") or ""),
        lifecycle_state=str(payload.get("lifecycle_state") or ""),
        is_reentry=bool(payload.get("is_reentry")),
        entry_reason_codes=tuple(str(item) for item in payload.get("entry_reason_codes") or ()),
        entry_costs=_pnl_from_payload(payload.get("entry_costs")),
        tokens_remaining=_decimal(payload.get("tokens_remaining")) or ZERO,
        cost_basis_remaining_usd=_decimal(payload.get("cost_basis_remaining_usd")) or ZERO,
        realized_net_pnl_usd=_decimal(payload.get("realized_net_pnl_usd")) or ZERO,
        realized_gross_pnl_usd=_decimal(payload.get("realized_gross_pnl_usd")) or ZERO,
        secured_proceeds_usd=_decimal(payload.get("secured_proceeds_usd")) or ZERO,
        peak_price_usd=_decimal(payload.get("peak_price_usd")),
        peak_market_cap_usd=_decimal(payload.get("peak_market_cap_usd")),
        peak_at=_int(payload.get("peak_at")),
        trough_price_usd=_decimal(payload.get("trough_price_usd")),
        max_favourable_percent=_decimal(payload.get("max_favourable_percent")) or ZERO,
        max_adverse_percent=_decimal(payload.get("max_adverse_percent")) or ZERO,
        milestones_taken=tuple(str(item) for item in payload.get("milestones_taken") or ()),
        break_even_armed=bool(payload.get("break_even_armed")),
        trailing_armed=bool(payload.get("trailing_armed")),
        closed_at=_int(payload.get("closed_at")),
        close_reason=str(payload.get("close_reason") or ""),
        last_price_usd=_decimal(payload.get("last_price_usd")),
        last_observed_at=int(payload.get("last_observed_at") or 0),
        exits=exits,
    )


def _position_payload(position: PaperPosition) -> dict[str, Any]:
    return {
        "position_id": position.position_id,
        "mint": position.mint,
        "opened_at": position.opened_at,
        "entry_price_usd": str(position.entry_price_usd),
        "entry_market_cap_usd": _text(position.entry_market_cap_usd),
        "size_usd": str(position.size_usd),
        "tokens": str(position.tokens),
        "strategy_version": position.strategy_version,
        "config_hash": position.config_hash,
        "lifecycle_state": position.lifecycle_state,
        "is_reentry": position.is_reentry,
        "entry_reason_codes": list(position.entry_reason_codes),
        "entry_costs": _pnl_payload(position.entry_costs),
        "tokens_remaining": str(position.tokens_remaining),
        "cost_basis_remaining_usd": str(position.cost_basis_remaining_usd),
        "realized_net_pnl_usd": str(position.realized_net_pnl_usd),
        "realized_gross_pnl_usd": str(position.realized_gross_pnl_usd),
        "secured_proceeds_usd": str(position.secured_proceeds_usd),
        "peak_price_usd": _text(position.peak_price_usd),
        "peak_market_cap_usd": _text(position.peak_market_cap_usd),
        "peak_at": position.peak_at,
        "trough_price_usd": _text(position.trough_price_usd),
        "max_favourable_percent": str(position.max_favourable_percent),
        "max_adverse_percent": str(position.max_adverse_percent),
        "milestones_taken": list(position.milestones_taken),
        "break_even_armed": position.break_even_armed,
        "trailing_armed": position.trailing_armed,
        "closed_at": position.closed_at,
        "close_reason": position.close_reason,
        "last_price_usd": _text(position.last_price_usd),
        "last_observed_at": position.last_observed_at,
        "exits": [_journal_payload(item) for item in position.exits],
    }


def _journal_payload(entry: ExitJournalEntry) -> dict[str, Any]:
    return {
        "position_id": entry.position_id,
        "mint": entry.mint,
        "sequence": entry.sequence,
        "occurred_at": entry.occurred_at,
        "quote_price_usd": str(entry.quote_price_usd),
        "fraction_sold": str(entry.fraction_sold),
        "tokens_sold": str(entry.tokens_sold),
        "tokens_remaining": str(entry.tokens_remaining),
        "gross_proceeds_usd": str(entry.gross_proceeds_usd),
        "cost_basis_usd": str(entry.cost_basis_usd),
        "costs": _pnl_payload(entry.costs),
        "reason_code": entry.reason_code,
        "final": entry.final,
    }


def _journal_from_payload(payload: Any) -> ExitJournalEntry:
    data = payload if isinstance(payload, dict) else {}
    return ExitJournalEntry(
        position_id=str(data.get("position_id") or ""),
        mint=str(data.get("mint") or ""),
        sequence=int(data.get("sequence") or 0),
        occurred_at=int(data.get("occurred_at") or 0),
        quote_price_usd=_decimal(data.get("quote_price_usd")) or ZERO,
        fraction_sold=_decimal(data.get("fraction_sold")) or ZERO,
        tokens_sold=_decimal(data.get("tokens_sold")) or ZERO,
        tokens_remaining=_decimal(data.get("tokens_remaining")) or ZERO,
        gross_proceeds_usd=_decimal(data.get("gross_proceeds_usd")) or ZERO,
        cost_basis_usd=_decimal(data.get("cost_basis_usd")) or ZERO,
        costs=_pnl_from_payload(data.get("costs")),
        reason_code=str(data.get("reason_code") or HOLD_NO_TRIGGER),
        final=bool(data.get("final")),
    )


def _pnl_payload(value: RealizedPnl) -> dict[str, str]:
    return {
        "gross_pnl_usd": str(value.gross_pnl_usd),
        "platform_fees_usd": str(value.platform_fees_usd),
        "network_fees_usd": str(value.network_fees_usd),
        "priority_fees_usd": str(value.priority_fees_usd),
        "price_impact_usd": str(value.price_impact_usd),
        "slippage_usd": str(value.slippage_usd),
    }


def _pnl_from_payload(payload: Any) -> RealizedPnl:
    data = payload if isinstance(payload, dict) else {}
    return RealizedPnl(
        gross_pnl_usd=_decimal(data.get("gross_pnl_usd")) or ZERO,
        platform_fees_usd=_decimal(data.get("platform_fees_usd")) or ZERO,
        network_fees_usd=_decimal(data.get("network_fees_usd")) or ZERO,
        priority_fees_usd=_decimal(data.get("priority_fees_usd")) or ZERO,
        price_impact_usd=_decimal(data.get("price_impact_usd")) or ZERO,
        slippage_usd=_decimal(data.get("slippage_usd")) or ZERO,
    )


def _runner_is_healthy(context: ExitContext, *, config: LabConfig) -> bool:
    """Independent demand + liquidity + momentum + controlled sellers."""

    ratio = context.buy_sell_ratio
    liquidity_change = context.liquidity_change_percent
    return bool(
        context.organic_score is not None
        and context.organic_score >= 55
        and context.momentum_score is not None
        and context.momentum_score >= 55
        and (liquidity_change is None or liquidity_change > Decimal("-10"))
        and ratio is not None
        and ratio >= Decimal("1.2")
        and not context.smart_money_distributing
        and context.safety_status == "PASS"
        and config is not None
    )


def _flow_weakening(context: ExitContext, *, config: LabConfig) -> bool:
    ratio = context.buy_sell_ratio
    if ratio is not None and ratio < Decimal("1"):
        return True
    return (
        context.momentum_score is not None
        and context.momentum_score < config.momentum_decay_exit_score * 2
    )


def _flow_reversed(context: ExitContext, *, config: LabConfig) -> bool:
    ratio = context.buy_sell_ratio
    return ratio is not None and ratio <= config.flow_reversal_ratio


def _volume_exhausted(context: ExitContext) -> bool:
    if context.volume_usd is None or not context.entry_volume_usd:
        return False
    if context.entry_volume_usd <= 0:
        return False
    return context.volume_usd / context.entry_volume_usd <= Decimal("0.25")


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None
