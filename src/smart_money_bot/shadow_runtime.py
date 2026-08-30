"""Wires the SHADOW auto-trader into the runner pipeline.

:mod:`smart_money_bot.lab.shadow` holds the strategy, :mod:`shadow_store` holds
the SQL, and this module is the thin adapter between them and the live evidence
the Fomo runner already produced.

Two properties matter more than anything else here:

* **It performs no provider call of its own.**  Every input is evidence the
  runner already paid for, which is what keeps the shadow experiment — and the
  twelve counterfactual policies it feeds — free (section 54).
* **It cannot spend.**  There is no signer, no keypair, no RPC client, no
  transaction builder and no swap submission anywhere in this module or in
  anything it imports.  ``SHADOW_REAL_MONEY_SPEND`` is zero by construction and
  the test suite asserts it by scanning this source.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from decimal import Decimal
from typing import Any

from .constants import BOT_VERSION
from .lab.bankroll import BankrollState, BreakerInputs, BreakerStatus, apply_entry
from .lab.bankroll import apply_exit as apply_bankroll_exit
from .lab.costs import leg_costs
from .lab.exits import (
    ExitContext,
    ExitPlan,
    PaperPosition,
    apply_exit,
    observe,
    open_position,
)
from .lab.shadow import (
    DEFAULT_SHADOW_CONFIG,
    SHADOW_EXPERIMENT_VERSION,
    SHADOW_REAL_MONEY_SPEND,
    ShadowConfig,
    ShadowEntryDecision,
    ShadowExposure,
    ShadowPosition,
    ShadowSignal,
    deterministic_position_id,
    evaluate_shadow_breakers,
    evaluate_shadow_entry,
)
from .lab.shadow_exits import (
    NO_RUNNER_EVIDENCE,
    SHADOW_STALE_OBSERVATION,
    RunnerEvidence,
    ShadowExitAssessment,
    net_pnl_now,
    plan_shadow_exit,
)
from .lab.shadow_metrics import (
    CatalystTimingReport,
    CounterfactualResult,
    NotableTimingReport,
    ShadowAccountReport,
    ShadowObservation,
    ShadowTradeRecord,
    VenueReport,
    catalyst_timing,
    compare_shadow_exit_policies,
    notable_timing,
    summarize_shadow_account,
    summarize_venues,
)
from .lab.venues import (
    GRADUATION_UNKNOWN,
    PUMPSWAP_FEE_BPS,
    VENUE_JUPITER,
    VENUE_PUMPSWAP,
    BondingCurveState,
    RouteQuote,
    RouteSelection,
    bonding_curve_quote,
    executable_quote,
    fallback_quote,
    pool_quote,
    select_route,
)
from .shadow_store import ShadowStore

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
UNIT = Decimal("0.000001")

#: Structural re-export so an operator-facing status line can state the
#: invariant without importing the strategy package.
REAL_MONEY_SPEND_USD = SHADOW_REAL_MONEY_SPEND


class ShadowRuntime:
    """Evaluate signals, manage simulated $10 positions, and build reports."""

    def __init__(
        self,
        store: ShadowStore,
        *,
        config: ShadowConfig = DEFAULT_SHADOW_CONFIG,
        enabled: bool = True,
        experiment_version: str = SHADOW_EXPERIMENT_VERSION,
    ) -> None:
        self.store = store
        self.config = config
        self.enabled = enabled
        self.experiment_version = experiment_version
        self._experiment_started_at: int | None = None
        self.last_entry_at: int | None = None
        self.last_exit_at: int | None = None
        self.last_entry_mint: str = ""
        self.last_exit_mint: str = ""
        self.entries_opened = 0
        self.exits_recorded = 0
        self.signals_refused = 0

    # ------------------------------------------------------------------
    # experiment checkpoint
    # ------------------------------------------------------------------
    async def start_experiment(self, *, now: int | None = None) -> int | None:
        """Write (once) and cache the forward-experiment start time."""

        row = await self.store.ensure_experiment(
            self.config, experiment_version=self.experiment_version, now=now
        )
        started = row.get("started_at") if row else None
        self._experiment_started_at = int(started) if started is not None else None
        return self._experiment_started_at

    async def experiment_started_at(self) -> int | None:
        if self._experiment_started_at is None:
            await self.start_experiment()
        return self._experiment_started_at

    # ------------------------------------------------------------------
    # bankroll
    # ------------------------------------------------------------------
    async def bankroll(self) -> BankrollState:
        payload = await self.store.load_bankroll_payload(
            strategy_version=self.config.strategy_version
        )
        if not payload:
            return BankrollState(
                starting_usd=self.config.bankroll_usd,
                cash_usd=self.config.bankroll_usd,
                peak_equity_usd=self.config.bankroll_usd,
            )
        return BankrollState(
            starting_usd=_decimal(payload.get("starting_usd"), self.config.bankroll_usd),
            cash_usd=_decimal(payload.get("cash_usd"), self.config.bankroll_usd),
            realized_net_pnl_usd=_decimal(payload.get("realized_net_pnl_usd")),
            open_exposure_usd=_decimal(payload.get("open_exposure_usd")),
            open_positions=int(payload.get("open_positions") or 0),
            peak_equity_usd=_decimal(payload.get("peak_equity_usd"), self.config.bankroll_usd),
            consecutive_losses=int(payload.get("consecutive_losses") or 0),
            day_realized_net_pnl_usd=_decimal(payload.get("day_realized_net_pnl_usd")),
            day_key=str(payload.get("day_key") or ""),
            paused_reason=str(payload.get("paused_reason") or ""),
        )

    async def save_bankroll(self, state: BankrollState, *, now: int | None = None) -> None:
        await self.store.save_bankroll_payload(
            {
                "starting_usd": str(state.starting_usd),
                "cash_usd": str(state.cash_usd),
                "realized_net_pnl_usd": str(state.realized_net_pnl_usd),
                "open_exposure_usd": str(state.open_exposure_usd),
                "open_positions": state.open_positions,
                "peak_equity_usd": str(state.peak_equity_usd),
                "consecutive_losses": state.consecutive_losses,
                "day_realized_net_pnl_usd": str(state.day_realized_net_pnl_usd),
                "day_key": state.day_key,
                "paused_reason": state.paused_reason,
            },
            strategy_version=self.config.strategy_version,
            now=now,
        )

    async def breakers(self, inputs: BreakerInputs | None = None) -> BreakerStatus:
        return evaluate_shadow_breakers(
            await self.bankroll(),
            inputs if inputs is not None else BreakerInputs(),
            config=self.config,
        )

    # ------------------------------------------------------------------
    # routing (sections 7, 21-24)
    # ------------------------------------------------------------------
    def build_routes(
        self,
        *,
        side: str,
        notional_usd: Decimal,
        observed_price_usd: Decimal | None,
        liquidity_usd: Decimal | None = None,
        curve: BondingCurveState | None = None,
        executable_price_usd: Decimal | None = None,
        executable_venue: str = VENUE_JUPITER,
        executable_impact_percent: Decimal | None = None,
        executable_fee_bps: int = 0,
        executable_latency_ms: int = 0,
        observed_route_impact_percent: Decimal | None = None,
        graduation_state: str = GRADUATION_UNKNOWN,
        route_available: bool = True,
        now: int = 0,
    ) -> RouteSelection:
        """Quote this exact trade on every venue that can price it.

        Preference is section 7's hierarchy — a real executable quote, then a
        simulation from live venue state, then an explicitly penalised fallback
        — but the *choice* between usable routes is made on the price the trade
        would actually get, never on a venue preference.
        """

        quotes: list[RouteQuote] = []

        if executable_price_usd is not None and executable_price_usd > 0:
            quotes.append(
                executable_quote(
                    venue=executable_venue,
                    side=side,
                    notional_usd=notional_usd,
                    fill_price_usd=executable_price_usd,
                    reference_price_usd=observed_price_usd,
                    price_impact_percent=executable_impact_percent,
                    slippage_bps=self.config.slippage_bps,
                    fee_bps=executable_fee_bps,
                    liquidity_usd=liquidity_usd,
                    quote_latency_ms=executable_latency_ms,
                    now=now,
                    graduation_state=graduation_state,
                )
            )

        if curve is not None and curve.known:
            quotes.append(
                bonding_curve_quote(
                    curve,
                    side=side,
                    notional_usd=notional_usd,
                    slippage_bps=self.config.slippage_bps,
                    now=now,
                )
            )

        if liquidity_usd is not None and liquidity_usd > 0 and observed_price_usd:
            quotes.append(
                pool_quote(
                    venue=VENUE_PUMPSWAP,
                    side=side,
                    notional_usd=notional_usd,
                    reference_price_usd=observed_price_usd,
                    liquidity_usd=liquidity_usd,
                    fee_bps=PUMPSWAP_FEE_BPS,
                    slippage_bps=self.config.slippage_bps,
                    observed_price_impact_percent=observed_route_impact_percent,
                    now=now,
                    graduation_state=graduation_state,
                )
            )

        selection = select_route(
            quotes, max_price_impact_percent=self.config.max_price_impact_percent
        )
        if selection.available or not self.config.allow_fallback_fill:
            return selection
        if not route_available:
            # The evidence says there is no route at all.  A fallback price is
            # for a missing *quote*, never for a missing *market* — pricing this
            # off the last chart print would be exactly the fantasy fill section
            # 39 forbids.
            return selection

        # Nothing executable priced this trade.  Fall back only if the strategy
        # allows it, and label the result so no report can mistake it for real.
        fallback = fallback_quote(
            side=side,
            notional_usd=notional_usd,
            observed_price_usd=observed_price_usd,
            liquidity_usd=liquidity_usd,
            now=now,
            graduation_state=graduation_state,
        )
        if not fallback.usable:
            return selection
        return RouteSelection(
            chosen=fallback,
            considered=(*selection.considered, fallback),
            rejected=selection.rejected,
        )

    # ------------------------------------------------------------------
    # entry (sections 4, 6, 7, 40)
    # ------------------------------------------------------------------
    async def consider_signal(
        self,
        signal: ShadowSignal,
        *,
        now: int | None = None,
        curve: BondingCurveState | None = None,
        executable_price_usd: Decimal | None = None,
        executable_venue: str = VENUE_JUPITER,
        executable_impact_percent: Decimal | None = None,
        executable_latency_ms: int = 0,
        observed_route_impact_percent: Decimal | None = None,
        breaker_inputs: BreakerInputs | None = None,
    ) -> tuple[ShadowEntryDecision, ShadowPosition | None]:
        """Decide and, when accepted, open exactly one $10 simulated position.

        Idempotent: the position id is derived from the signal, so a replayed
        signal or a restart mid-write produces a no-op rather than a second
        simulated trade.
        """

        moment = now if now is not None else int(time.time())
        signal = replace(
            signal,
            timestamps=replace(
                signal.timestamps,
                decision_at=signal.timestamps.decision_at or moment,
            ),
        )

        selection = self.build_routes(
            side="BUY",
            notional_usd=self.config.position_usd,
            observed_price_usd=signal.price_usd,
            liquidity_usd=signal.liquidity_usd,
            curve=curve,
            executable_price_usd=executable_price_usd,
            executable_venue=executable_venue,
            executable_impact_percent=executable_impact_percent,
            executable_latency_ms=executable_latency_ms,
            observed_route_impact_percent=observed_route_impact_percent,
            graduation_state=signal.graduation_state,
            route_available=signal.route_available,
            now=moment,
        )
        quote = selection.chosen
        quote_at = quote.quoted_at if quote is not None else None
        signal = replace(
            signal,
            timestamps=replace(signal.timestamps, quote_at=quote_at or moment),
        )

        state = await self.bankroll()
        exposure = await self._exposure(signal)
        decision = evaluate_shadow_entry(
            signal,
            state,
            exposure,
            route_price_impact_percent=(
                quote.price_impact_percent if quote is not None else None
            ),
            route_available=selection.available,
            fill_source=quote.source if quote is not None else None,
            experiment_started_at=await self.experiment_started_at(),
            breakers=(
                evaluate_shadow_breakers(state, breaker_inputs, config=self.config)
                if breaker_inputs is not None
                else None
            ),
            config=self.config,
        )
        await self.store.record_signal(
            decision, evidence=signal.evidence(), route=quote
        )
        if not decision.accepted or quote is None or not self.enabled:
            if not decision.accepted:
                self.signals_refused += 1
            return decision, None

        position = await self._open(
            signal, decision, selection, now=moment
        )
        return decision, position

    async def _exposure(self, signal: ShadowSignal) -> ShadowExposure:
        open_positions = await self.store.open_positions(
            strategy_version=self.config.strategy_version
        )
        token = [item for item in open_positions if item.mint == signal.mint]
        return ShadowExposure(
            open_positions=len(open_positions),
            open_exposure_usd=sum(
                (item.position.cost_basis_remaining_usd for item in open_positions), ZERO
            ),
            token_exposure_usd=sum(
                (item.position.cost_basis_remaining_usd for item in token), ZERO
            ),
            holds_same_family=any(item.family == signal.family for item in token),
        )

    async def _open(
        self,
        signal: ShadowSignal,
        decision: ShadowEntryDecision,
        selection: RouteSelection,
        *,
        now: int,
    ) -> ShadowPosition | None:
        quote = selection.chosen
        if quote is None or quote.fill_price_usd is None:
            return None
        position_id = deterministic_position_id(
            mint=signal.mint,
            family=signal.family,
            signal_at=signal.timestamps.signal_at or decision.decided_at,
            strategy_version=self.config.strategy_version,
        )
        paper = open_position(
            position_id=position_id,
            mint=signal.mint,
            now=now,
            decision_price_usd=signal.price_usd or quote.fill_price_usd,
            fill_price_usd=quote.fill_price_usd,
            size_usd=decision.size_usd,
            market_cap_usd=signal.market_cap_usd,
            price_impact_percent=quote.price_impact_percent,
            slippage_bps=quote.slippage_bps,
            strategy_version=self.config.strategy_version,
            config_hash=decision.config_hash,
            lifecycle_state=signal.lifecycle_state,
            entry_reason_codes=decision.reason_codes,
            config=self.config.exit_config(),
        )
        shadow = ShadowPosition(
            position=paper,
            family=signal.family,
            experiment_version=self.experiment_version,
            venue=quote.venue,
            fill_source=quote.source,
            graduation_state=quote.graduation_state,
            peak_net_pnl_usd=ZERO,
            signal_evidence=signal.evidence(),
            timestamps=replace(signal.timestamps, fill_at=now),
            entry_route=quote.as_dict(),
        )
        stored = await self.store.save_position(shadow, now=now)
        if not stored:
            # The duplicate-entry lock refused this write; a position for this
            # mint and family already exists.
            return None

        state = apply_entry(await self.bankroll(), size_usd=decision.size_usd)
        await self.save_bankroll(state, now=now)
        await self.store.record_venue_fill(
            position_id=position_id,
            sequence=0,
            selection=selection,
            filled_at=now,
            cost_usd=paper.entry_costs.total_cost_usd,
        )
        self.entries_opened += 1
        self.last_entry_at = now
        self.last_entry_mint = signal.mint
        return shadow

    # ------------------------------------------------------------------
    # management (sections 9-14)
    # ------------------------------------------------------------------
    async def manage_position(
        self,
        shadow: ShadowPosition,
        context: ExitContext,
        evidence: RunnerEvidence = NO_RUNNER_EVIDENCE,
        *,
        curve: BondingCurveState | None = None,
        executable_price_usd: Decimal | None = None,
        executable_venue: str = VENUE_JUPITER,
    ) -> tuple[ShadowPosition, ShadowExitAssessment]:
        """Advance one open simulated position by one observation.

        The observation is persisted first, so the counterfactual stream stays
        complete even when no exit fires — and so a policy comparison can never
        need a provider request of its own.
        """

        updated_paper = observe(shadow.position, context, config=self.config.exit_config())
        await self._record_observation(shadow.position_id, context)

        assessment = plan_shadow_exit(
            updated_paper, context, evidence, config=self.config
        )
        peak = max(shadow.peak_net_pnl_usd, assessment.net.total_net_usd)

        # A sell needs its own route: a position that entered on the bonding
        # curve must still be able to exit after graduation (section 22).
        selection: RouteSelection | None = None
        exit_price = context.price_usd
        if assessment.plan.acts and context.price_usd is not None:
            selection = self.build_routes(
                side="SELL",
                notional_usd=(
                    updated_paper.tokens_remaining
                    * assessment.plan.fraction
                    * context.price_usd
                ),
                observed_price_usd=context.price_usd,
                liquidity_usd=context.liquidity_usd,
                curve=curve,
                executable_price_usd=executable_price_usd,
                executable_venue=executable_venue,
                observed_route_impact_percent=context.price_impact_percent,
                graduation_state=shadow.graduation_state,
                route_available=context.route_available,
                now=context.now,
            )
            if not selection.available:
                # No sell route at any venue is a realistic failure, not a
                # reason to pretend a clean chart-price exit (section 39).
                updated = replace(
                    shadow,
                    position=updated_paper,
                    peak_net_pnl_usd=peak,
                    exit_route={"UNAVAILABLE": "no sell route at any venue"},
                )
                await self.store.save_position(updated, now=context.now)
                return updated, assessment
            if selection.chosen is not None and selection.chosen.fill_price_usd is not None:
                exit_price = selection.chosen.fill_price_usd

        sell_context = (
            replace(
                context,
                price_usd=exit_price,
                price_impact_percent=(
                    selection.chosen.price_impact_percent
                    if selection is not None and selection.chosen is not None
                    else context.price_impact_percent
                ),
                slippage_bps=(
                    selection.chosen.slippage_bps
                    if selection is not None and selection.chosen is not None
                    else context.slippage_bps
                ),
            )
            if selection is not None
            else context
        )
        after, journal = apply_exit(
            updated_paper, assessment.plan, sell_context, config=self.config.exit_config()
        )
        updated = replace(
            shadow,
            position=after,
            peak_net_pnl_usd=peak,
            # The venue only migrates when a fill actually happened — a plan
            # that ended up selling nothing must not rewrite where the position
            # trades (section 22: a curve entry can exit on the pool later).
            venue=(
                selection.venue
                if journal is not None and selection is not None and selection.available
                else shadow.venue
            ),
            exit_route=(
                selection.chosen.as_dict()
                if selection is not None and selection.chosen is not None
                else shadow.exit_route
            ),
        )
        await self.store.save_position(updated, now=context.now)
        if journal is None:
            return updated, assessment

        await self.store.record_exit(
            journal,
            family=shadow.family,
            venue=selection.venue if selection is not None else shadow.venue,
        )
        if selection is not None:
            await self.store.record_venue_fill(
                position_id=shadow.position_id,
                sequence=journal.sequence,
                selection=selection,
                filled_at=context.now,
                cost_usd=journal.costs.total_cost_usd,
                net_pnl_usd=journal.realized_net_pnl_usd,
            )
        state = apply_bankroll_exit(
            await self.bankroll(),
            cost_basis_usd=journal.cost_basis_usd,
            net_proceeds_usd=journal.net_proceeds_usd,
            closed=journal.final,
            day_key=_day_key(context.now),
        )
        await self.save_bankroll(state, now=context.now)
        self.exits_recorded += 1
        self.last_exit_at = context.now
        self.last_exit_mint = shadow.mint
        return updated, assessment

    async def sweep_stale_positions(
        self,
        *,
        now: int | None = None,
    ) -> list[tuple[ShadowPosition, ShadowExitAssessment]]:
        """Close positions the bot has lost sight of, at a price it could verify.

        A token that stops appearing in the runner pipeline would otherwise sit
        in the book forever, and the account headline would keep reporting an
        unrealized number nobody could still trade out of.  That is not a
        cosmetic problem: the whole experiment is the claim that
        ``$100 → $X`` is true.

        The close is deliberately pessimistic.  The last *observed* price is not
        a price the bot could still get — nothing has confirmed it since — so it
        is routed as an explicitly penalised fallback fill, exactly like any
        other price the bot could not verify.
        """

        moment = now if now is not None else int(time.time())
        closed: list[tuple[ShadowPosition, ShadowExitAssessment]] = []
        try:
            positions = await self.store.open_positions(
                strategy_version=self.config.strategy_version
            )
        except Exception:
            logger.exception("Could not load shadow positions for the stale sweep")
            return closed

        for shadow in positions:
            position = shadow.position
            last_seen = position.last_observed_at or position.opened_at
            if moment - last_seen < self.config.stale_position_seconds:
                continue
            price = position.last_price_usd
            if price is None or price <= 0:
                continue
            context = ExitContext(
                now=moment,
                price_usd=price,
                safety_status="UNKNOWN",
                route_available=True,
            )
            net = net_pnl_now(position, price, config=self.config)
            plan = ExitPlan(
                fraction=Decimal("1"),
                reason_code=SHADOW_STALE_OBSERVATION,
                final=True,
                notes=(
                    f"no observation for {moment - last_seen}s — closed at the last "
                    "price the bot could verify, priced as a penalised fallback",
                ),
            )
            fallback = fallback_quote(
                side="SELL",
                notional_usd=(position.tokens_remaining * price),
                observed_price_usd=price,
                now=moment,
                graduation_state=shadow.graduation_state,
                reason="the position went unobserved",
            )
            if fallback.fill_price_usd is None:
                continue
            sell_context = replace(
                context,
                price_usd=fallback.fill_price_usd,
                price_impact_percent=fallback.price_impact_percent,
            )
            after, journal = apply_exit(
                position, plan, sell_context, config=self.config.exit_config()
            )
            if journal is None:
                continue
            updated = replace(shadow, position=after, exit_route=fallback.as_dict())
            await self.store.save_position(updated, now=moment)
            await self.store.record_exit(
                journal, family=shadow.family, venue=fallback.venue
            )
            state = apply_bankroll_exit(
                await self.bankroll(),
                cost_basis_usd=journal.cost_basis_usd,
                net_proceeds_usd=journal.net_proceeds_usd,
                closed=journal.final,
                day_key=_day_key(moment),
            )
            await self.save_bankroll(state, now=moment)
            self.exits_recorded += 1
            self.last_exit_at = moment
            self.last_exit_mint = shadow.mint
            closed.append(
                (
                    updated,
                    ShadowExitAssessment(
                        plan=plan,
                        base_plan=plan,
                        net=net,
                        why=plan.notes,
                    ),
                )
            )
        return closed

    async def _record_observation(self, position_id: str, context: ExitContext) -> None:
        if context.price_usd is None or context.price_usd <= 0:
            return
        await self.store.record_observation(
            position_id,
            ShadowObservation(
                at=context.now,
                price_usd=context.price_usd,
                market_cap_usd=context.market_cap_usd,
                liquidity_usd=context.liquidity_usd,
                volume_usd=context.volume_usd,
                momentum_score=context.momentum_score,
                organic_score=context.organic_score,
                buys=context.buys,
                sells=context.sells,
                safety_status=context.safety_status,
                route_available=context.route_available,
                smart_money_distributing=context.smart_money_distributing,
                smart_money_accumulating=context.smart_money_accumulating,
            ),
        )

    # ------------------------------------------------------------------
    # reports (sections 16, 17, 33-37, 44)
    # ------------------------------------------------------------------
    async def trade_records(self) -> list[ShadowTradeRecord]:
        open_positions = await self.store.open_positions(
            strategy_version=self.config.strategy_version
        )
        closed = await self.store.closed_positions(
            strategy_version=self.config.strategy_version
        )
        return [
            self._trade_record(item, open_position=True) for item in open_positions
        ] + [self._trade_record(item, open_position=False) for item in closed]

    def _trade_record(
        self, shadow: ShadowPosition, *, open_position: bool
    ) -> ShadowTradeRecord:
        position = shadow.position
        net = net_pnl_now(
            position,
            position.last_price_usd,
            config=self.config,
        )
        costs = position.entry_costs.total_cost_usd + sum(
            (item.costs.total_cost_usd for item in position.exits), ZERO
        )
        return ShadowTradeRecord(
            position_id=position.position_id,
            mint=position.mint,
            family=shadow.family,
            symbol=shadow.signal_evidence.get("symbol", ""),
            opened_at=position.opened_at,
            closed_at=position.closed_at,
            size_usd=position.size_usd,
            entry_price_usd=position.entry_price_usd,
            entry_market_cap_usd=position.entry_market_cap_usd,
            exit_market_cap_usd=_scaled_market_cap(position, open_position=open_position),
            realized_net_pnl_usd=position.realized_net_pnl_usd,
            realized_gross_pnl_usd=position.realized_gross_pnl_usd,
            total_cost_usd=costs.quantize(UNIT),
            unrealized_net_pnl_usd=net.unrealized_net_usd if open_position else ZERO,
            max_favourable_percent=position.max_favourable_percent,
            max_adverse_percent=position.max_adverse_percent,
            peak_net_pnl_usd=shadow.peak_net_pnl_usd,
            close_reason=position.close_reason,
            venue=shadow.venue,
            fill_source=shadow.fill_source,
            graduation_state=shadow.graduation_state,
            open=open_position,
            forward=shadow.experiment_version == self.experiment_version,
        )

    async def account(self) -> ShadowAccountReport:
        """The headline: is the $100 shadow account making money? (§16, §44)"""

        state = await self.bankroll()
        return summarize_shadow_account(
            await self.trade_records(),
            starting_bankroll_usd=state.starting_usd,
            cash_usd=state.cash_usd,
            open_exposure_usd=state.open_exposure_usd,
            config=self.config,
        )

    async def open_trades(self) -> list[dict[str, Any]]:
        """`/fomo shadow view:trades` — every open $10 position and why it is held."""

        rows: list[dict[str, Any]] = []
        for shadow in await self.store.open_positions(
            strategy_version=self.config.strategy_version
        ):
            position = shadow.position
            price = position.last_price_usd
            net = net_pnl_now(position, price, config=self.config)
            rows.append(
                {
                    "mint": position.mint,
                    "symbol": shadow.signal_evidence.get("symbol", ""),
                    "family": shadow.family,
                    "label": shadow.label,
                    "opened_at": position.opened_at,
                    "entry_market_cap_usd": position.entry_market_cap_usd,
                    "current_market_cap_usd": _decimal_or_none(
                        shadow.signal_evidence.get("market_cap_usd")
                    ),
                    "size_usd": position.size_usd,
                    "net_pnl_usd": net.total_net_usd,
                    "unrealized_net_usd": net.unrealized_net_usd,
                    "realized_net_usd": net.realized_net_usd,
                    "mfe_percent": position.max_favourable_percent,
                    "mae_percent": position.max_adverse_percent,
                    "peak_net_usd": shadow.peak_net_pnl_usd,
                    "giveback_usd": max(
                        ZERO, shadow.peak_net_pnl_usd - net.total_net_usd
                    ).quantize(UNIT),
                    "drawdown_from_peak_percent": position.drawdown_from_peak_percent(price),
                    "remaining_fraction": position.remaining_fraction,
                    "venue": shadow.venue,
                    "fill_source": shadow.fill_source,
                    "graduation_state": shadow.graduation_state,
                    "milestones_taken": list(position.milestones_taken),
                    "objective_met": net.total_net_usd >= self.config.net_profit_objective_usd,
                    "notable_timing": self.notable_timing_for(shadow),
                    "catalyst_timing": self.catalyst_timing_for(shadow),
                    "signal_to_fill_seconds": (
                        shadow.timestamps.fill_at - shadow.timestamps.signal_at
                        if shadow.timestamps.fill_at and shadow.timestamps.signal_at
                        else None
                    ),
                }
            )
        return rows

    async def venues(self) -> tuple[VenueReport, ...]:
        return summarize_venues(await self.store.venue_fills())

    async def counterfactuals(self, position_id: str) -> tuple[CounterfactualResult, ...]:
        """All twelve alternative exit policies, on persisted rows only (§15)."""

        for shadow in await self.store.open_positions(
            strategy_version=self.config.strategy_version
        ) + await self.store.closed_positions(strategy_version=self.config.strategy_version):
            if shadow.position_id != position_id:
                continue
            observations = await self.store.observations(position_id)
            return compare_shadow_exit_policies(
                observations,
                entry_at=shadow.position.opened_at,
                entry_price_usd=shadow.position.entry_price_usd,
                size_usd=shadow.position.size_usd,
                config=self.config,
            )
        return ()

    def notable_timing_for(
        self,
        shadow: ShadowPosition,
        *,
        exit_market_cap_usd: Decimal | None = None,
    ) -> NotableTimingReport:
        """Did smart-money intelligence arrive early enough? (section 18)

        Trader entry → bot detection → simulated fill → exit, each as a market
        cap and each as a move, so "we saw it, but 4x too late" is visible.
        """

        evidence = shadow.signal_evidence
        return notable_timing(
            trader_entry_market_cap_usd=_decimal_or_none(
                evidence.get("trader_entry_market_cap_usd")
            ),
            detection_market_cap_usd=_decimal_or_none(
                evidence.get("detection_market_cap_usd")
            ),
            fill_market_cap_usd=shadow.position.entry_market_cap_usd,
            exit_market_cap_usd=exit_market_cap_usd,
        )

    def catalyst_timing_for(self, shadow: ShadowPosition) -> CatalystTimingReport:
        """Event → mint → bot → fill (section 19)."""

        evidence = shadow.signal_evidence
        return catalyst_timing(
            event_at=_int_or_none(evidence.get("event_at")),
            mint_created_at=_int_or_none(evidence.get("mint_created_at")),
            detected_at=shadow.timestamps.first_seen_at or shadow.timestamps.signal_at,
            alerted_at=_int_or_none(evidence.get("catalyst_alert_at")),
            filled_at=shadow.timestamps.fill_at,
            first_credible_source=evidence.get("first_credible_source", ""),
        )

    async def latest_counterfactuals(
        self,
    ) -> tuple[str, str, tuple[CounterfactualResult, ...]]:
        """The twelve policies for the most recently opened shadow trade.

        Returns ``(position_id, symbol, results)`` so a card can name the trade
        it is comparing.  Costs no provider request.
        """

        rows = await self.store.position_rows(
            strategy_version=self.config.strategy_version, limit=1
        )
        if not rows:
            return "", "", ()
        position_id = str(rows[0].get("position_id") or "")
        results = await self.counterfactuals(position_id)
        return position_id, str(rows[0].get("family") or ""), results

    async def status(self) -> dict[str, Any]:
        """The `/smartmoney status` and `/fomo realtime` extension (section 38)."""

        state = await self.bankroll()
        breakers = await self.breakers()
        experiment = await self.store.experiment(experiment_version=self.experiment_version)
        # Mark the open book to its last verified price so this number agrees
        # with the `/fomo shadow` headline instead of quietly reporting cost
        # basis while the dashboard reports value.  Only open positions are
        # read, so this stays a cheap local query.
        open_positions = await self.store.open_positions(
            strategy_version=self.config.strategy_version
        )
        unrealized = sum(
            (
                net_pnl_now(
                    item.position, item.position.last_price_usd, config=self.config
                ).unrealized_net_usd
                for item in open_positions
            ),
            ZERO,
        )
        return {
            "enabled": self.enabled and self.config.enabled,
            "strategy_version": self.config.strategy_version,
            "experiment_version": self.experiment_version,
            "experiment_started_at": (experiment or {}).get("started_at"),
            "position_size_usd": self.config.position_usd,
            "starting_bankroll_usd": state.starting_usd,
            "current_bankroll_usd": (state.equity_usd + unrealized).quantize(UNIT),
            "book_equity_usd": state.equity_usd,
            "unrealized_net_pnl_usd": unrealized.quantize(UNIT),
            "max_positions": self.config.max_concurrent_positions,
            "max_exposure_usd": self.config.max_total_exposure_usd,
            "net_objective_usd": self.config.net_profit_objective_usd,
            "open_positions": state.open_positions,
            "open_exposure_usd": state.open_exposure_usd,
            "paused": breakers.paused or state.is_paused,
            "paused_reasons": breakers.reasons,
            "last_entry_at": self.last_entry_at,
            "last_exit_at": self.last_exit_at,
            "last_entry_mint": self.last_entry_mint,
            "last_exit_mint": self.last_exit_mint,
            "entries_opened": self.entries_opened,
            "exits_recorded": self.exits_recorded,
            "signals_refused": self.signals_refused,
            "real_money_spend_usd": REAL_MONEY_SPEND_USD,
            "live_execution_enabled": False,
            "bot_version": BOT_VERSION,
        }

    async def refusals(self, *, since: int = 0) -> dict[str, int]:
        return await self.store.refusal_counts(since=since)


def _scaled_market_cap(
    position: PaperPosition,
    *,
    open_position: bool,
) -> Decimal | None:
    """The market cap implied by the last price, for the entry-vs-exit view.

    Derived from the entry market cap and the price move rather than stored,
    because the exit journal records a price and not a supply.  It is an
    inference from a fixed supply, so it is only offered for a closed position
    where the last price *is* the exit price — never presented as an observed
    reading, and never used in any P&L calculation.
    """

    if open_position:
        return None
    if position.entry_market_cap_usd is None or position.entry_price_usd <= 0:
        return None
    price = position.last_price_usd
    if price is None or price <= 0:
        return None
    return (position.entry_market_cap_usd * price / position.entry_price_usd).quantize(UNIT)


def exit_cost_usd(
    position: PaperPosition,
    price: Decimal | None,
    *,
    config: ShadowConfig = DEFAULT_SHADOW_CONFIG,
) -> Decimal:
    """The modelled cost of closing the remainder right now."""

    if price is None or price <= 0 or position.tokens_remaining <= 0:
        return ZERO
    value = position.tokens_remaining * price
    return leg_costs(value, config=config).total_cost_usd


def _day_key(now: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def _decimal(value: Any, default: Decimal = ZERO) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
