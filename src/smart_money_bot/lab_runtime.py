"""Wires the PAPER research laboratory into the existing runner pipeline.

The lab package holds the strategy; :mod:`lab_store` holds the SQL.  This module
is the thin adapter between them and the live ``RunnerCandidate`` the Fomo runner
already produces, plus the report builders the Discord commands render.

It performs **no** provider calls of its own.  Every input is evidence the runner
already paid for, which is what keeps counterfactual simulation free.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .constants import BOT_VERSION
from .lab.authenticity import (
    AuthenticityAssessment,
    aggregate_sol_activity,
    assess_economic_authenticity,
)
from .lab.bankroll import BankrollState, apply_entry
from .lab.bankroll import apply_exit as apply_bankroll_exit
from .lab.config import DEFAULT_LAB_CONFIG, STRATEGY_VERSION, LabConfig
from .lab.decision import EvidenceQuality, TradeDecision
from .lab.entry import EntryContext, EntryEvaluation, evaluate_entry
from .lab.evidence import confidence_cap
from .lab.exits import (
    ExitContext,
    PaperPosition,
    apply_exit,
    observe,
    open_position,
    plan_exit,
)
from .lab.identity import TokenIdentity, build_token_identity, identity_from_payload
from .lab.lifecycle import (
    LifecycleObservation,
    PublicationState,
    ReentryAssessment,
    TokenLifecycle,
    advance_lifecycle,
    apply_reentry,
    assess_reentry,
    record_paper_entry,
    record_paper_exit,
    record_publication,
    should_republish,
)
from .lab.regime import MarketRegime, RegimeSample, classify_regime
from .lab.registry import registry_snapshot
from .lab.replay import (
    PerformanceReport,
    TradeRecord,
    attribute_loss,
    summarize_trades,
)
from .lab.smartmoney import (
    SmartMoneyAssessment,
    assess_smart_money,
    build_reputation,
    decay_reputation,
)
from .lab.timeline import (
    ALERTED,
    LIFECYCLE_CHANGED,
    PAPER_ENTRY,
    PAPER_EXIT,
    PAPER_PARTIAL_EXIT,
    QUALIFIED,
    TOKEN_DISCOVERED,
    Provenance,
    TokenEvent,
    observation_events,
)
from .lab_store import LabStore

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
HUNDRED = Decimal("100")

#: Runner stages that count as "qualified" for the lab's entry gate.
QUALIFIED_STAGES = frozenset(
    {"QUALIFIED_RESEARCH", "HEATING_UP", "ENTRY_CANDIDATE", "STRONG_RUNNER"}
)


class LabRuntime:
    """Evaluate candidates, manage simulated positions, and build reports.

    Every method is safe to call repeatedly for the same candidate: the store's
    keys make the writes idempotent, so a Railway restart replays cleanly.
    """

    def __init__(
        self,
        store: LabStore,
        *,
        config: LabConfig = DEFAULT_LAB_CONFIG,
        enabled: bool = True,
    ) -> None:
        self.store = store
        self.config = config
        self.enabled = enabled
        self._regime = MarketRegime()

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

    # ------------------------------------------------------------------
    # candidate evaluation
    # ------------------------------------------------------------------
    async def evaluate_candidate(
        self,
        candidate: Any,
        *,
        now: int | None = None,
        metadata: dict[str, Any] | None = None,
        wallet_activity: tuple[Any, ...] = (),
        surfaced: bool = False,
    ) -> LabEvaluation:
        """Run the full lab pipeline for one already-analysed runner candidate."""

        moment = now if now is not None else int(time.time())
        mint = getattr(candidate, "mint", "")
        current = getattr(candidate, "current", None)
        quality = getattr(candidate, "quality", None)
        safety = getattr(candidate, "safety", None)
        forensics = getattr(candidate, "forensics", None)
        demand = getattr(quality, "demand", None)

        identity = await self._identity(
            mint,
            candidate=candidate,
            metadata=metadata,
            forensics=forensics,
            now=moment,
        )

        lifecycle = await self.store.load_lifecycle(mint, now=moment)
        stage = str(getattr(candidate, "stage", "RAW_DISCOVERY"))
        qualified = stage in QUALIFIED_STAGES
        observation = LifecycleObservation(
            observed_at=moment,
            price_usd=_field(current, "price_usd"),
            market_cap_usd=_field(current, "market_cap_usd"),
            liquidity_usd=_field(current, "liquidity_usd"),
            volume_usd=_field(current, "volume_5m_usd"),
            independent_buyers=getattr(demand, "estimated_independent_buyers", None),
            momentum_score=getattr(quality, "momentum_score", None),
            opportunity_score=getattr(quality, "opportunity_score", None),
            organic_score=getattr(quality, "organic_score", None),
            safety_status=str(getattr(safety, "status", "UNKNOWN")),
            qualified=qualified,
            surfaced=surfaced or qualified,
            rugged=bool(getattr(current, "rugged", False)),
            cluster_supply_percent=getattr(demand, "cluster_supply_percent", None),
            top10_percent=_field(current, "top10_percent"),
        )
        previous_state = lifecycle.state
        lifecycle = advance_lifecycle(lifecycle, observation, config=self.config)

        authenticity = self._authenticity(wallet_activity, demand, forensics)
        smart_money = await self._smart_money(candidate, demand, forensics, moment)
        reentry = self._reentry(lifecycle, observation, candidate, authenticity)
        if reentry is not None:
            lifecycle = apply_reentry(lifecycle, reentry, now=moment)

        context = self._entry_context(
            candidate,
            lifecycle=lifecycle,
            authenticity=authenticity,
            smart_money=smart_money,
            qualified=qualified,
            now=moment,
        )
        bankroll = await self.bankroll()
        existing = await self.store.open_position_for(
            mint, strategy_version=self.config.strategy_version
        )
        evaluation = evaluate_entry(
            context,
            lifecycle=lifecycle,
            bankroll=bankroll,
            reentry=reentry,
            exposure_in_token_usd=(
                existing.cost_basis_remaining_usd if existing is not None else ZERO
            ),
            config=self.config,
        )

        events = self._events(
            mint,
            candidate=candidate,
            lifecycle=lifecycle,
            previous_state=previous_state,
            qualified=qualified,
            now=moment,
        )
        await self.store.append_events(events)
        await self.store.save_lifecycle(lifecycle, now=moment)
        if metadata:
            # Only a metadata-bearing pass may rewrite the stored identity, so a
            # cheap re-evaluation cannot erase a good image or ABOUT.
            await self.store.save_identity(identity)
        await self.store.record_decision(evaluation.decision)

        return LabEvaluation(
            mint=mint,
            identity=identity,
            lifecycle=lifecycle,
            evaluation=evaluation,
            authenticity=authenticity,
            smart_money=smart_money,
            reentry=reentry,
            position=existing,
        )

    async def maybe_open_position(
        self,
        result: LabEvaluation,
        *,
        now: int | None = None,
    ) -> PaperPosition | None:
        """Open a simulated position when — and only when — the decision says so.

        There is no signing path anywhere in this call.  It writes rows.
        """

        if not self.enabled:
            return None
        decision = result.evaluation.decision
        if not decision.entry_eligible or decision.price_usd is None:
            return None
        moment = now if now is not None else int(time.time())
        existing = await self.store.open_position_for(
            result.mint, strategy_version=self.config.strategy_version
        )
        if existing is not None:
            return None

        position = open_position(
            # A deterministic id makes a replayed entry a no-op instead of a
            # second simulated position.
            position_id=f"{result.mint}:{decision.timestamp}:{self.config.strategy_version}",
            mint=result.mint,
            now=moment,
            decision_price_usd=decision.price_usd,
            size_usd=decision.size_usd,
            market_cap_usd=decision.market_cap_usd,
            # The entry leg pays its own observed impact, not the whole
            # round-trip cost, which the exit leg charges separately.
            price_impact_percent=_decimal_or_none(
                decision.evidence.get("buy_price_impact_percent")
            ),
            strategy_version=self.config.strategy_version,
            config_hash=decision.config_hash,
            lifecycle_state=result.lifecycle.state,
            is_reentry=result.lifecycle.is_reentry,
            entry_reason_codes=decision.reason_codes,
            config=self.config,
        )
        stored = await self.store.save_position(position, now=moment)
        if not stored:
            return None

        bankroll = apply_entry(await self.bankroll(), size_usd=position.size_usd)
        await self.save_bankroll(bankroll, now=moment)
        lifecycle = record_paper_entry(result.lifecycle, now=moment)
        await self.store.save_lifecycle(lifecycle, now=moment)
        await self.store.append_events(
            (
                TokenEvent(
                    mint=result.mint,
                    event_type=PAPER_ENTRY,
                    occurred_at=moment,
                    payload={
                        "position_id": position.position_id,
                        "size_usd": str(position.size_usd),
                        "reason_codes": list(decision.reason_codes),
                    },
                    provenance=Provenance(source="lab", observed_at=moment),
                    price_usd=position.entry_price_usd,
                    market_cap_usd=position.entry_market_cap_usd,
                ),
            )
        )
        return position

    async def manage_position(
        self,
        position: PaperPosition,
        context: ExitContext,
    ) -> tuple[PaperPosition, str]:
        """Advance one open simulated position by one observation."""

        updated = observe(position, context, config=self.config)
        plan = plan_exit(updated, context, config=self.config)
        updated, journal = apply_exit(updated, plan, context, config=self.config)
        await self.store.save_position(updated, now=context.now)
        if journal is None:
            return updated, plan.reason_code

        await self.store.record_exit(journal)
        bankroll = apply_bankroll_exit(
            await self.bankroll(),
            cost_basis_usd=journal.cost_basis_usd,
            net_proceeds_usd=journal.net_proceeds_usd,
            closed=journal.final,
            day_key=_day_key(context.now),
        )
        await self.save_bankroll(bankroll, now=context.now)

        lifecycle = await self.store.load_lifecycle(position.mint, now=context.now)
        lifecycle = record_paper_exit(
            lifecycle, now=context.now, net_pnl_usd=journal.realized_net_pnl_usd
        )
        await self.store.save_lifecycle(lifecycle, now=context.now)
        await self.store.append_events(
            (
                TokenEvent(
                    mint=position.mint,
                    event_type=PAPER_EXIT if journal.final else PAPER_PARTIAL_EXIT,
                    occurred_at=context.now,
                    payload={
                        "position_id": position.position_id,
                        "sequence": journal.sequence,
                        "reason_code": journal.reason_code,
                        "net_pnl_usd": str(journal.realized_net_pnl_usd),
                    },
                    provenance=Provenance(source="lab", observed_at=context.now),
                    price_usd=journal.quote_price_usd,
                ),
            )
        )
        return updated, plan.reason_code

    # ------------------------------------------------------------------
    # publication gating
    # ------------------------------------------------------------------
    async def may_publish(
        self,
        result: LabEvaluation,
        *,
        now: int | None = None,
        risk_deteriorated: bool = False,
        paper_exit_event: bool = False,
    ) -> tuple[bool, tuple[str, ...], str]:
        """Whether a card is worth publishing again (section K)."""

        moment = now if now is not None else int(time.time())
        previous = await self.store.load_publication(result.mint)
        evidence = result.evaluation.decision.evidence
        verdict = should_republish(
            previous,
            now=moment,
            lifecycle_state=result.lifecycle.state,
            opportunity_score=_decimal(evidence.get("opportunity_score")),
            momentum_score=_decimal(evidence.get("momentum_score")),
            organic_score=_decimal(evidence.get("organic_score")),
            safety_status=str(result.evaluation.decision.safety),
            independent_buyers=int(evidence.get("independent_buyers") or 0),
            liquidity_usd=_decimal_or_none(evidence.get("liquidity_usd")),
            smart_wallets=(
                result.smart_money.proven_early + result.smart_money.useful_confirmation
                if result.smart_money
                else 0
            ),
            decision=str(result.evaluation.decision.decision),
            risk_deteriorated=risk_deteriorated,
            paper_exit_event=paper_exit_event,
            config=self.config,
        )
        return verdict.should_publish, verdict.triggers, verdict.reason

    async def mark_published(self, result: LabEvaluation, *, now: int | None = None) -> None:
        moment = now if now is not None else int(time.time())
        evidence = result.evaluation.decision.evidence
        await self.store.save_publication(
            PublicationState(
                mint=result.mint,
                published_at=moment,
                lifecycle_state=result.lifecycle.state,
                opportunity_score=_decimal(evidence.get("opportunity_score")),
                momentum_score=_decimal(evidence.get("momentum_score")),
                organic_score=_decimal(evidence.get("organic_score")),
                safety_status=str(result.evaluation.decision.safety),
                independent_buyers=int(evidence.get("independent_buyers") or 0),
                liquidity_usd=_decimal_or_none(evidence.get("liquidity_usd")),
                smart_wallets=(
                    result.smart_money.proven_early + result.smart_money.useful_confirmation
                    if result.smart_money
                    else 0
                ),
                decision=str(result.evaluation.decision.decision),
                fingerprint=result.evaluation.decision.config_hash,
            )
        )
        lifecycle = record_publication(result.lifecycle, now=moment)
        await self.store.save_lifecycle(lifecycle, now=moment)
        await self.store.append_events(
            (
                TokenEvent(
                    mint=result.mint,
                    event_type=ALERTED,
                    occurred_at=moment,
                    payload={"decision": str(result.evaluation.decision.decision)},
                    provenance=Provenance(source="lab", observed_at=moment),
                ),
            )
        )

    # ------------------------------------------------------------------
    # reports
    # ------------------------------------------------------------------
    async def performance(self) -> dict[str, Any]:
        """The `/fomo performance` payload, computed only from stored trades."""

        closed = await self.store.closed_positions(strategy_version=self.config.strategy_version)
        open_positions = await self.store.open_positions(
            strategy_version=self.config.strategy_version
        )
        bankroll = await self.bankroll()
        trades = tuple(_trade_record(position) for position in closed)
        report = summarize_trades(
            trades, strategy_version=self.config.strategy_version, config=self.config
        )
        unrealized = sum(
            (
                position.unrealized_value_usd(position.last_price_usd)
                - position.cost_basis_remaining_usd
                for position in open_positions
            ),
            ZERO,
        )
        return {
            "starting_bankroll_usd": bankroll.starting_usd,
            "current_bankroll_usd": bankroll.equity_usd,
            "cash_usd": bankroll.cash_usd,
            "realized_net_pnl_usd": bankroll.realized_net_pnl_usd,
            "unrealized_net_pnl_usd": unrealized.quantize(Decimal("0.000001")),
            "open_positions": len(open_positions),
            "report": report,
            "strategy_version": self.config.strategy_version,
            "bot_version": BOT_VERSION,
            "paused_reason": bankroll.paused_reason,
        }

    async def opportunities(self, *, limit: int = 5) -> list[TradeDecision]:
        """The strongest real setups the lab currently sees, whatever the verdict."""

        return await self.store.recent_decisions(
            limit=limit,
            strategy_version=self.config.strategy_version,
        )

    async def trades(self, *, limit: int = 10) -> list[PaperPosition]:
        open_positions = await self.store.open_positions(
            strategy_version=self.config.strategy_version
        )
        closed = await self.store.closed_positions(
            strategy_version=self.config.strategy_version, limit=limit
        )
        return [*open_positions, *closed][:limit]

    async def loss_attribution(self) -> dict[str, int]:
        closed = await self.store.closed_positions(
            strategy_version=self.config.strategy_version, limit=200
        )
        counts: dict[str, int] = {}
        for position in closed:
            record = _trade_record(position)
            if record.net_pnl_usd >= 0:
                continue
            cause = attribute_loss(record, config=self.config)
            counts[cause] = counts.get(cause, 0) + 1
        return counts

    def registry(self) -> dict[str, tuple[str, ...]]:
        return registry_snapshot()

    def update_regime(self, samples: list[RegimeSample]) -> MarketRegime:
        self._regime = classify_regime(samples)
        return self._regime

    @property
    def regime(self) -> MarketRegime:
        return self._regime

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    async def _identity(
        self,
        mint: str,
        *,
        candidate: Any,
        metadata: dict[str, Any] | None,
        forensics: Any,
        now: int,
    ) -> TokenIdentity:
        """Resolve identity from fresh metadata, else from what was persisted.

        Re-reading the stored row keeps `/fomo opportunities` able to show the
        image and ABOUT without paying for another provider request, and stops
        a metadata-free pass from erasing a good identity.
        """

        if not metadata:
            stored = await self.store.identity_payload(mint)
            if stored:
                return identity_from_payload(stored)
        return build_token_identity(
            mint,
            metadata=metadata,
            name=getattr(candidate, "name", None),
            symbol=getattr(candidate, "symbol", None),
            resolved_at=now,
            token_age_seconds=_age(getattr(candidate, "chain_created_at", None), now),
            pair_age_seconds=_age(getattr(candidate, "pair_created_at", None), now),
            creator=getattr(forensics, "creator_wallet", None),
            extra_links=(
                (("dexscreener", getattr(candidate, "pair_url", "")),)
                if getattr(candidate, "pair_url", "")
                else ()
            ),
            sources=("runner",),
        )

    def _authenticity(
        self,
        wallet_activity: tuple[Any, ...],
        demand: Any,
        forensics: Any,
    ) -> AuthenticityAssessment:
        profile = aggregate_sol_activity(
            wallet_activity,
            independent_buyers=getattr(demand, "estimated_independent_buyers", None),
        )
        return assess_economic_authenticity(
            profile,
            demand=demand,
            forensics=forensics,
            config=self.config,
        )

    async def _smart_money(
        self,
        candidate: Any,
        demand: Any,
        forensics: Any,
        now: int,
    ) -> SmartMoneyAssessment:
        """Use the persisted, decayed reputation for each public wallet.

        A wallet with no recorded forward outcomes stays ``UNKNOWN`` — being on
        the list is not evidence, which is exactly why "large wallet" and "smart
        wallet" are kept apart.
        """

        wallets = tuple(getattr(candidate, "smart_wallets", ()) or ())
        if not wallets:
            return SmartMoneyAssessment()
        stored = await self.store.load_reputations(list(wallets))
        reputations = tuple(
            decay_reputation(
                stored.get(wallet) or build_reputation(wallet, (), now=now),
                now=now,
            )
            for wallet in wallets
        )
        earliest = getattr(candidate, "earliest_smart_entry_age_seconds", None)
        return assess_smart_money(
            reputations,
            independent_clusters=int(getattr(demand, "independent_smart_clusters", 0) or 0),
            shared_funding=bool(getattr(forensics, "shared_funder_groups", ())),
            synchronized_entries=bool(getattr(forensics, "time_linked_groups", ())),
            entry_ages_seconds=(earliest,) if isinstance(earliest, int) else (),
            buy_events=len(wallets),
            config=self.config,
        )

    def _reentry(
        self,
        lifecycle: TokenLifecycle,
        observation: LifecycleObservation,
        candidate: Any,
        authenticity: AuthenticityAssessment,
    ) -> ReentryAssessment | None:
        if lifecycle.is_fresh_setup:
            return None
        current = getattr(candidate, "current", None)
        return assess_reentry(
            lifecycle,
            observation,
            liquidity_removed=bool(getattr(current, "rugged", False)),
            cluster_worsening=authenticity.looks_manufactured,
            concentration_worsening=False,
            smart_money_accumulating=False,
            distribution_fading=not authenticity.looks_manufactured,
            route_healthy=bool(getattr(current, "route_available", False))
            and str(getattr(current, "sell_route_status", "UNKNOWN")) == "PASS",
            expected_net_edge_percent=None,
            config=self.config,
        )

    def _entry_context(
        self,
        candidate: Any,
        *,
        lifecycle: TokenLifecycle,
        authenticity: AuthenticityAssessment,
        smart_money: SmartMoneyAssessment,
        qualified: bool,
        now: int,
    ) -> EntryContext:
        current = getattr(candidate, "current", None)
        first = getattr(candidate, "first", None)
        quality = getattr(candidate, "quality", None)
        safety = getattr(candidate, "safety", None)
        demand = getattr(quality, "demand", None)

        price = _field(current, "price_usd")
        degraded = bool(getattr(getattr(candidate, "forensics", None), "degraded", False))
        liquidity = _field(current, "liquidity_usd")
        first_liquidity = _field(first, "liquidity_usd")
        move_since_surface = lifecycle.return_from_surface(price)

        return EntryContext(
            mint=getattr(candidate, "mint", ""),
            now=now,
            price_usd=price,
            market_cap_usd=_field(current, "market_cap_usd"),
            liquidity_usd=liquidity,
            liquidity_change_percent=_change(liquidity, first_liquidity),
            volume_usd=_field(current, "volume_5m_usd"),
            qualified=qualified,
            stage=str(getattr(candidate, "stage", "RAW_DISCOVERY")),
            opportunity_score=_decimal(getattr(quality, "opportunity_score", ZERO)),
            momentum_score=_decimal(getattr(quality, "momentum_score", ZERO)),
            organic_score=_decimal(getattr(quality, "organic_score", ZERO)),
            overextended=bool(getattr(candidate, "overextended", False)),
            move_since_first_surface_percent=move_since_surface,
            move_since_signal_percent=_change(price, _field(first, "price_usd")),
            signal_age_seconds=_age(getattr(candidate, "first_seen_at", None), now),
            token_age_seconds=_age(getattr(candidate, "chain_created_at", None), now),
            independent_buyers=getattr(demand, "estimated_independent_buyers", None),
            independence_ratio=getattr(demand, "independence_ratio", None),
            cluster_supply_percent=getattr(demand, "cluster_supply_percent", None),
            fresh_wallet_percent=getattr(demand, "fresh_wallet_percent", None),
            top10_percent=_field(current, "top10_percent"),
            synchronized_funding=bool(
                getattr(getattr(candidate, "forensics", None), "time_linked_groups", ())
            ),
            buys=int(getattr(current, "buys_5m", 0) or 0),
            sells=int(getattr(current, "sells_5m", 0) or 0),
            safety_status=str(getattr(safety, "status", "UNKNOWN")),
            safety_entry_eligible=bool(getattr(safety, "entry_eligible", False)),
            route_available=bool(getattr(current, "route_available", False)),
            sell_route_available=str(getattr(current, "sell_route_status", "UNKNOWN")) == "PASS",
            buy_price_impact_percent=_field(current, "route_price_impact_percent"),
            sell_price_impact_percent=_field(current, "sell_route_price_impact_percent"),
            authenticity=authenticity,
            smart_money=smart_money,
            regime=self._regime,
            expected_upside_percent=_expected_upside(quality, lifecycle),
            expected_downside_percent=_expected_downside(quality),
            edge_confidence=_edge_confidence(
                quality,
                authenticity,
                safety_status=str(getattr(safety, "status", "UNKNOWN")),
                data_degraded=degraded,
            ),
            data_degraded=degraded,
        )

    def _events(
        self,
        mint: str,
        *,
        candidate: Any,
        lifecycle: TokenLifecycle,
        previous_state: str,
        qualified: bool,
        now: int,
    ) -> tuple[TokenEvent, ...]:
        current = getattr(candidate, "current", None)
        provenance = Provenance(source="fomo_runner", observed_at=now, source_timestamp=now)
        events: list[TokenEvent] = list(
            observation_events(
                mint,
                occurred_at=now,
                price_usd=_field(current, "price_usd"),
                market_cap_usd=_field(current, "market_cap_usd"),
                liquidity_usd=_field(current, "liquidity_usd"),
                holder_count=getattr(current, "holder_count", None),
                buys=int(getattr(current, "buys_5m", 0) or 0),
                sells=int(getattr(current, "sells_5m", 0) or 0),
                volume_usd=_field(current, "volume_5m_usd"),
                provenance=provenance,
            )
        )
        if lifecycle.first_discovered_at == now:
            events.append(
                TokenEvent(
                    mint=mint,
                    event_type=TOKEN_DISCOVERED,
                    occurred_at=now,
                    payload={"source": str(getattr(candidate, "graduation_source", ""))},
                    provenance=provenance,
                )
            )
        if qualified:
            events.append(
                TokenEvent(
                    mint=mint,
                    event_type=QUALIFIED,
                    occurred_at=now,
                    payload={"stage": str(getattr(candidate, "stage", ""))},
                    provenance=provenance,
                )
            )
        if lifecycle.state != previous_state:
            events.append(
                TokenEvent(
                    mint=mint,
                    event_type=LIFECYCLE_CHANGED,
                    occurred_at=now,
                    payload={"from": previous_state, "to": lifecycle.state},
                    provenance=provenance,
                )
            )
        return tuple(events)


class LabEvaluation:
    """The complete lab view of one candidate at one instant."""

    __slots__ = (
        "authenticity",
        "evaluation",
        "identity",
        "lifecycle",
        "mint",
        "position",
        "reentry",
        "smart_money",
    )

    def __init__(
        self,
        *,
        mint: str,
        identity: TokenIdentity,
        lifecycle: TokenLifecycle,
        evaluation: EntryEvaluation,
        authenticity: AuthenticityAssessment,
        smart_money: SmartMoneyAssessment,
        reentry: ReentryAssessment | None,
        position: PaperPosition | None,
    ) -> None:
        self.mint = mint
        self.identity = identity
        self.lifecycle = lifecycle
        self.evaluation = evaluation
        self.authenticity = authenticity
        self.smart_money = smart_money
        self.reentry = reentry
        self.position = position

    @property
    def decision(self) -> TradeDecision:
        return self.evaluation.decision

    @property
    def entry_eligible(self) -> bool:
        return self.evaluation.entry_eligible

    @property
    def why_not_entry(self) -> tuple[str, ...]:
        if self.entry_eligible:
            return ()
        return self.decision.human_reasons


def _trade_record(position: PaperPosition) -> TradeRecord:
    gross = sum((entry.realized_gross_pnl_usd for entry in position.exits), ZERO)
    costs = sum((entry.costs.total_cost_usd for entry in position.exits), ZERO)
    costs += position.entry_costs.total_cost_usd
    return TradeRecord(
        mint=position.mint,
        opened_at=position.opened_at,
        closed_at=position.closed_at or position.last_observed_at,
        strategy_version=position.strategy_version,
        net_pnl_usd=position.realized_net_pnl_usd,
        gross_pnl_usd=gross,
        cost_usd=costs,
        size_usd=position.size_usd,
        max_favourable_percent=position.max_favourable_percent,
        max_adverse_percent=position.max_adverse_percent,
        is_reentry=position.is_reentry,
        lifecycle_state=position.lifecycle_state,
        close_reason=position.close_reason,
    )


def _expected_upside(quality: Any, lifecycle: TokenLifecycle) -> Decimal | None:
    """A bounded, evidence-derived upside estimate — never a hopeful guess."""

    if quality is None:
        return None
    opportunity = _decimal(getattr(quality, "opportunity_score", None))
    momentum = _decimal(getattr(quality, "momentum_score", None))
    organic = _decimal(getattr(quality, "organic_score", None))
    if opportunity <= 0 and momentum <= 0:
        return None
    blended = (opportunity * 2 + momentum + organic) / 4
    upside = blended.quantize(Decimal("0.01"))
    if lifecycle.is_reentry:
        # A re-entry is worth less than a fresh setup at the same score.
        upside = (upside * Decimal("0.7")).quantize(Decimal("0.01"))
    return upside


def _expected_downside(quality: Any) -> Decimal | None:
    if quality is None:
        return None
    organic = _decimal(getattr(quality, "organic_score", None))
    return (Decimal("60") - organic / 4).quantize(Decimal("0.01"))


def _edge_confidence(
    quality: Any,
    authenticity: AuthenticityAssessment,
    *,
    safety_status: str | None = None,
    data_degraded: bool = False,
) -> Decimal | None:
    """Confidence in the edge estimate, ceilinged by evidence completeness.

    The organic-demand score measures *visible* demand.  On its own it can read
    100/100 while economic authenticity is UNKNOWN and the bounded SOL activity
    sample is missing, which is exactly the state in which a 100% confidence
    reading is unjustified.  The cap makes the claim match the evidence.
    """

    if quality is None:
        return None
    demand = getattr(quality, "demand", None)
    confidence = _decimal(getattr(quality, "organic_score", None))
    if authenticity.quality is not EvidenceQuality.UNKNOWN:
        confidence = (confidence + authenticity.score) / 2
    if getattr(demand, "confidence", "UNKNOWN") == "UNKNOWN":
        # Kept from before the cap existed: an untraced candidate halves its
        # confidence outright.  The ceiling below is applied on top, so this can
        # only ever be more conservative, never less.
        confidence = confidence / 2
    cap = confidence_cap(
        authenticity_quality=authenticity.quality,
        activity_available=authenticity.activity.available,
        safety_status=safety_status,
        demand_confidence=getattr(demand, "confidence", None),
        data_degraded=data_degraded,
    )
    return cap.apply(confidence.quantize(Decimal("0.01")))


def _field(source: Any, name: str) -> Decimal | None:
    value = getattr(source, name, None)
    return value if isinstance(value, Decimal) else None


def _change(current: Decimal | None, base: Decimal | None) -> Decimal | None:
    if current is None or base is None or base <= 0:
        return None
    return ((current - base) / base * HUNDRED).quantize(Decimal("0.01"))


def _age(started_at: int | None, now: int) -> int | None:
    if not started_at or now <= 0:
        return None
    return max(0, now - started_at)


def _day_key(now: int) -> str:
    return datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%d")


def _decimal(value: Any, default: Decimal = ZERO) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


__all__ = ["STRATEGY_VERSION", "LabEvaluation", "LabRuntime", "PerformanceReport"]
