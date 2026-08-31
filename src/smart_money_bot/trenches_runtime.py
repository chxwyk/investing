"""The Trenches runtime: discovery union, cadence tiers, and event-driven promotion.

Orchestration only.  Every decision comes from :mod:`smart_money_bot.trenches`,
every read from :mod:`smart_money_bot.pump_chain`, and every write from
:mod:`smart_money_bot.trenches_store`, so this module can be driven identically
by the engine and by a test with no network at all.

The shape is the same staged pipeline the Trending lane uses, for the same
reason — nothing an operator sees may wait on enrichment:

    DISCOVERY (stream + poll)  →  PERSIST FIRST OBSERVATION  →  CHEAP SCORE
    →  TIER DECISION  →  TARGETED ENRICHMENT (budgeted)

Discovery is a **union** of lanes (section 33), so no vendor can decide what the
bot is allowed to see; rechecks run in **cadence tiers** (section 42) rather than
one interval for everything; and a meaningful event recomputes a candidate
immediately instead of waiting for its tier's timer (section 43).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .pump_chain import PumpChainReader
from .trenches import (
    CADENCE_HOT,
    CADENCE_NORMAL,
    CADENCE_WARM,
    DISCOVERY_LANES,
    LANE_PUBLIC_MODEL,
    LANE_PUMP_BONDING,
    LANE_PUMP_NEW,
    PINGING_TIERS,
    SUPPRESS_LATE_DISCOVERY,
    TRENCH_ALMOST_BONDED_STAGES,
    TRENCH_NEW_STAGES,
    TRENCH_RECENTLY_BONDED_STAGES,
    BundleProfile,
    CadenceConfig,
    ConsensusResult,
    DevProfile,
    LaneHealth,
    LifecycleState,
    MarketObservation,
    Nomination,
    ParticipantProfile,
    PromotionEvent,
    PublicTrendScore,
    RiskProfile,
    SourceRef,
    TierDecision,
    TimeframeProfile,
    TrenchScore,
    UniverseHealth,
    assess_concentration_trend,
    assess_depth,
    assess_related_exposure,
    bonding_milestones,
    build_consensus,
    build_risk_profile,
    build_timeframe_profile,
    cadence_tier,
    classify_lifecycle,
    decide_trench_tier,
    pump_onchain,
    rank_public_trend,
    score_public_trend,
    score_pump_trench,
)
from .trenches.provenance import DEXSCREENER_PUBLIC, SOLANA_RPC
from .trenches_store import TrenchesStore

ZERO = Decimal("0")

#: How a candidate first reached us, recorded so latency is attributable.
SOURCE_CREATION_STREAM = "PUMP_CREATION_STREAM"
SOURCE_POLL = "PUMP_POLL"
SOURCE_EXTERNAL = "EXTERNAL_LANE"


@dataclass(frozen=True, slots=True)
class TrenchCandidate:
    """Everything the card and alert layers need about one exact mint."""

    mint: str
    lifecycle: LifecycleState
    score: TrenchScore
    decision: TierDecision
    timeframes: TimeframeProfile | None = None
    participants: ParticipantProfile | None = None
    dev: DevProfile | None = None
    bundles: BundleProfile | None = None
    risk: RiskProfile | None = None
    consensus: ConsensusResult | None = None
    public_trend: PublicTrendScore | None = None
    name: str = ""
    symbol: str = ""
    market_cap_usd: Decimal | None = None
    first_market_cap_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    holders: int | None = None
    top10_percent: Decimal | None = None
    age_seconds: int | None = None
    cadence: str = CADENCE_NORMAL

    @property
    def ping(self) -> bool:
        return self.decision.ping and self.decision.tier in PINGING_TIERS

    def to_json(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "lifecycle": self.lifecycle.to_json(),
            "score": self.score.to_json(),
            "decision": self.decision.to_json(),
            "timeframes": self.timeframes.to_json() if self.timeframes else None,
            "participants": self.participants.to_json() if self.participants else None,
            "dev": self.dev.to_json() if self.dev else None,
            "bundles": self.bundles.to_json() if self.bundles else None,
            "risk": self.risk.to_json() if self.risk else None,
            "consensus": self.consensus.to_json() if self.consensus else None,
            "public_trend": self.public_trend.to_json() if self.public_trend else None,
            "cadence": self.cadence,
        }


@dataclass(frozen=True, slots=True)
class ScanResult:
    """What one Trenches pass produced."""

    observed: int = 0
    new_tokens: tuple[str, ...] = ()
    graduated: tuple[str, ...] = ()
    candidates: tuple[TrenchCandidate, ...] = ()
    alerts: tuple[TrenchCandidate, ...] = ()
    events: tuple[PromotionEvent, ...] = ()
    error: str = ""


@dataclass(slots=True)
class _Tracked:
    """In-memory state for one tracked mint."""

    mint: str
    first_observed_at: int
    source: str
    created_at: int | None = None
    last_scored_at: int = 0
    last_bonding_percent: Decimal | None = None
    cadence: str = CADENCE_NORMAL
    alerted_at: int | None = None
    name: str = ""
    symbol: str = ""
    creator: str = ""
    nominations: list[Nomination] = field(default_factory=list)


EnrichmentFn = Callable[[str], Awaitable[dict[str, Any]]]
PublishFn = Callable[[TrenchCandidate], Awaitable[bool]]


class TrenchesRuntime:
    """Drives Pump.fun discovery, scoring and the public Trending model."""

    def __init__(
        self,
        store: TrenchesStore,
        chain: PumpChainReader,
        *,
        enabled: bool = True,
        max_tracked: int = 80,
        runner_threshold: Decimal = Decimal("62"),
        heads_up_threshold: Decimal = Decimal("38"),
        max_alerts_per_hour: int = 8,
        cooldown_seconds: int = 1800,
        cadence_config: CadenceConfig | None = None,
        max_enrichment_per_scan: int = 12,
        wallet_lookups_per_token: int = 25,
        holder_reads_per_scan: int = 10,
        public_model_enabled: bool = True,
        public_model_min_score: Decimal = Decimal("10"),
        enrich: EnrichmentFn | None = None,
        publish: PublishFn | None = None,
    ) -> None:
        self.store = store
        self.chain = chain
        self.enabled = enabled
        self.max_tracked = max_tracked
        self.runner_threshold = runner_threshold
        self.heads_up_threshold = heads_up_threshold
        self.max_alerts_per_hour = max_alerts_per_hour
        self.cooldown_seconds = cooldown_seconds
        self.cadence = cadence_config or CadenceConfig()
        self.max_enrichment_per_scan = max_enrichment_per_scan
        self.wallet_lookups_per_token = wallet_lookups_per_token
        self.holder_reads_per_scan = holder_reads_per_scan
        self.public_model_enabled = public_model_enabled
        self.public_model_min_score = public_model_min_score
        self._enrich = enrich
        self._publish = publish

        self._tracked: dict[str, _Tracked] = {}
        self._pending_events: list[PromotionEvent] = []
        self._alert_times: list[int] = []
        self._public_ranks: dict[str, int] = {}
        self.scans = 0
        self.alerts_published = 0
        self.alerts_suppressed = 0
        self.creations_seen = 0
        self.last_scan_at: int | None = None
        self.last_alert_at: int | None = None
        self.last_error = ""
        self._restored = False

    # ------------------------------------------------------------------
    async def restore(self) -> None:
        """Rebuild tracking state after a restart, entry numbers intact."""

        if self._restored:
            return
        cutoff = int(time.time()) - 86_400
        for row in await self.store.recent_tokens(limit=self.max_tracked * 2, since=cutoff):
            mint = str(row["mint"])
            self._tracked[mint] = _Tracked(
                mint=mint,
                first_observed_at=int(row["first_observed_at"]),
                source=str(row["first_observed_source"] or ""),
                created_at=row["created_at"],
                last_bonding_percent=(
                    Decimal(str(row["bonding_percent"]))
                    if row["bonding_percent"] is not None
                    else None
                ),
                name=str(row["name"] or ""),
                symbol=str(row["symbol"] or ""),
                creator=str(row["creator"] or ""),
            )
        self._public_ranks = await self.store.previous_public_ranks()
        self._restored = True

    # ------------------------------------------------------------------
    # discovery (sections 33, 73, 74)
    # ------------------------------------------------------------------
    async def observe_creation(
        self,
        mint: str,
        *,
        at: int,
        created_at: int | None = None,
        source: str = SOURCE_CREATION_STREAM,
    ) -> bool:
        """Persist a brand-new mint the instant it is seen.

        This runs before any enrichment on purpose: the first-observation stamp
        is the number every latency metric is measured against, and letting it
        wait on a curve read would corrupt the very thing being measured.
        """

        if mint in self._tracked:
            return False
        self._tracked[mint] = _Tracked(
            mint=mint, first_observed_at=at, source=source, created_at=created_at
        )
        self.creations_seen += 1
        await self.store.record_token(mint, now=at, source=source, created_at=created_at)
        await self.store.record_discovery_latency(
            mint, observed_at=at, created_at=created_at, source=source
        )
        await self.store.record_nomination(
            mint, lane=LANE_PUMP_NEW, source_kind=SOLANA_RPC, at=at, detail=source
        )
        return True

    async def nominate(
        self,
        mint: str,
        *,
        lane: str,
        source: SourceRef,
        at: int,
        detail: str = "",
    ) -> None:
        """Record that a lane proposed this mint (section 33)."""

        tracked = self._tracked.get(mint)
        if tracked is None:
            tracked = _Tracked(mint=mint, first_observed_at=at, source=SOURCE_EXTERNAL)
            self._tracked[mint] = tracked
            await self.store.record_token(mint, now=at, source=SOURCE_EXTERNAL)
            await self.store.record_discovery_latency(
                mint, observed_at=at, created_at=None, source=SOURCE_EXTERNAL
            )
        tracked.nominations.append(
            Nomination(mint=mint, lane=lane, source=source, at=at, detail=detail)
        )
        await self.store.record_nomination(
            mint, lane=lane, source_kind=source.kind, at=at, detail=detail
        )

    def note_event(self, event: PromotionEvent) -> None:
        """Queue an event that should recompute its candidate now (section 43)."""

        self._pending_events.append(event)

    # ------------------------------------------------------------------
    # the scan
    # ------------------------------------------------------------------
    async def scan_once(self, *, now: int | None = None) -> ScanResult:
        """One pass: read curves in batch, score, decide, enrich the shortlist."""

        if not self.enabled:
            return ScanResult(error="Trenches lane disabled by configuration")
        await self.restore()
        moment = now if now is not None else int(time.time())
        self.scans += 1
        self.last_scan_at = moment

        due = self._due_mints(moment)
        if not due:
            return ScanResult(events=tuple(self._drain_events()))

        curves = await self.chain.bonding_curves(due)
        candidates: list[TrenchCandidate] = []
        graduated: list[str] = []
        new_tokens: list[str] = []
        events = self._drain_events()
        event_mints = {event.mint for event in events}

        # Enrichment budget: the shortlist is the highest-cadence candidates and
        # anything an event just fired on, never the whole board (section 71).
        shortlist = self._enrichment_shortlist(due, event_mints, moment)

        for mint in due:
            tracked = self._tracked.get(mint)
            if tracked is None:
                continue
            curve = curves.get(mint)
            if curve is None:
                continue

            previous = tracked.last_bonding_percent
            lifecycle = classify_lifecycle(
                curve,
                now=moment,
                created_at=tracked.created_at,
                first_observed_at=tracked.first_observed_at,
                graduated_at=None,
            )
            if lifecycle.progress_percent is not None:
                for milestone in bonding_milestones(previous, lifecycle.progress_percent):
                    self._pending_events.append(
                        PromotionEvent(
                            mint=mint,
                            kind="BONDING_MILESTONE",
                            at=moment,
                            detail=f"crossed {milestone}%",
                        )
                    )
                tracked.last_bonding_percent = lifecycle.progress_percent

            if curve.complete:
                graduated.append(mint)

            candidate = await self._evaluate(
                mint,
                tracked=tracked,
                lifecycle=lifecycle,
                now=moment,
                enrich=mint in shortlist,
            )
            if candidate is None:
                continue
            candidates.append(candidate)
            if tracked.first_observed_at >= moment - 120:
                new_tokens.append(mint)

        # Newest first: an early candidate is only interesting for minutes.
        candidates.sort(
            key=lambda item: (
                item.mint in event_mints,
                item.lifecycle.stage in TRENCH_NEW_STAGES,
                item.score.score,
            ),
            reverse=True,
        )

        alerts: list[TrenchCandidate] = []
        for candidate in candidates:
            if candidate.ping:
                if await self._emit(candidate, now=moment):
                    alerts.append(candidate)
            else:
                await self._record_suppression(candidate, now=moment)

        if self.public_model_enabled:
            await self._rank_public(candidates, now=moment)

        return ScanResult(
            observed=len(candidates),
            new_tokens=tuple(new_tokens),
            graduated=tuple(graduated),
            candidates=tuple(candidates),
            alerts=tuple(alerts),
            events=tuple(events),
            error=self.last_error,
        )

    def _due_mints(self, now: int) -> list[str]:
        """Whichever tracked mints their cadence tier says are due."""

        due: list[tuple[int, str]] = []
        for mint, tracked in self._tracked.items():
            interval = self.cadence.seconds_for(tracked.cadence)
            if now - tracked.last_scored_at >= interval:
                # Sort key puts the hottest first so a truncated pass keeps the
                # most time-critical candidates.
                rank = {CADENCE_HOT: 0, CADENCE_WARM: 1, CADENCE_NORMAL: 2}.get(
                    tracked.cadence, 3
                )
                due.append((rank, mint))
        due.sort()
        return [mint for _, mint in due[: self.max_tracked]]

    def _enrichment_shortlist(
        self,
        due: list[str],
        event_mints: set[str],
        now: int,
    ) -> set[str]:
        """Who gets the expensive reads this pass."""

        shortlist = set(event_mints)
        for mint in due:
            if len(shortlist) >= self.max_enrichment_per_scan:
                break
            tracked = self._tracked.get(mint)
            if tracked is None:
                continue
            if tracked.cadence in {CADENCE_HOT, CADENCE_WARM}:
                shortlist.add(mint)
        for mint in due:
            if len(shortlist) >= self.max_enrichment_per_scan:
                break
            shortlist.add(mint)
        return shortlist

    def _drain_events(self) -> list[PromotionEvent]:
        events = list(self._pending_events)
        self._pending_events.clear()
        return events

    # ------------------------------------------------------------------
    async def _evaluate(
        self,
        mint: str,
        *,
        tracked: _Tracked,
        lifecycle: LifecycleState,
        now: int,
        enrich: bool,
    ) -> TrenchCandidate | None:
        """Score one candidate, enriching only if it earned the budget."""

        tracked.last_scored_at = now
        extra: dict[str, Any] = {}
        if enrich and self._enrich is not None:
            extra = await self._enrich(mint) or {}

        market_cap = extra.get("market_cap_usd")
        liquidity = extra.get("liquidity_usd")
        observation = MarketObservation(
            at=now,
            price_usd=extra.get("price_usd"),
            market_cap_usd=market_cap,
            liquidity_usd=liquidity,
            buys=int(extra.get("buys") or 0),
            sells=int(extra.get("sells") or 0),
            volume_usd=extra.get("volume_usd") or ZERO,
            unique_buyers=extra.get("unique_buyers"),
            unique_sellers=extra.get("unique_sellers"),
            independent_buyers=extra.get("independent_buyers"),
            holders=extra.get("holders"),
        )
        await self.store.record_observation(
            mint, observation, bonding_percent=lifecycle.progress_percent
        )

        history = await self.store.observations(mint, since=now - 3600)
        timeframes = build_timeframe_profile(mint, history, now=now)
        depth = assess_depth(
            market_cap_usd=market_cap,
            liquidity_usd=liquidity,
            volume_usd=extra.get("volume_usd"),
        )

        holders = extra.get("holder_snapshot")
        if holders is not None:
            await self.store.record_holder_snapshot(holders)
        concentration = assess_concentration_trend(
            mint, await self.store.holder_snapshots(mint)
        )

        participants = extra.get("participants")
        dev = extra.get("dev")
        bundles = extra.get("bundles")

        # Related-wallet exposure (section 22) is derived from the clusters we
        # already detected, so it costs nothing beyond what participation
        # analysis computed.  "Related" is a statement about the transaction
        # graph; it never claims to know who anybody is.
        related_percent = extra.get("related_percent")
        if related_percent is None and participants is not None and participants.clusters:
            related_wallets = [
                wallet for cluster in participants.clusters for wallet in cluster.wallets
            ]
            holdings = extra.get("wallet_holdings") or {}
            if holdings:
                exposure = assess_related_exposure(
                    mint,
                    related_wallets=related_wallets,
                    holdings=holdings,
                    circulating_supply=extra.get("circulating_supply"),
                    evidence=tuple(
                        f"{cluster.kind} ({cluster.size} wallets)"
                        for cluster in participants.clusters
                    ),
                )
                related_percent = exposure.related_percent
            else:
                # No per-wallet holdings available, so the *share* is unknown even
                # though the relationship is not.  Saying so beats implying zero.
                related_percent = None

        risk = build_risk_profile(
            mint,
            liquidity_usd=liquidity,
            liquidity_to_market_cap=depth.liquidity_to_market_cap,
            dev_selling=(dev.holding.selling if dev else None),
            dev_history_label=(dev.history.label if dev else ""),
            dev_percent=(dev.holding.current_percent if dev else None),
            top10_percent=(holders.top10_percent if holders else None),
            concentration_worsening=(concentration.worsening if concentration.samples else None),
            bundle_risk=(bundles.risk if bundles else "UNKNOWN"),
            bundle_distributing=(bundles.distributing if bundles else False),
            related_percent=related_percent,
            clustered_percent=(participants.clustered_percent if participants else None),
            route_available=extra.get("route_available"),
            sell_verified=extra.get("sell_verified"),
            story_verified=extra.get("story_verified"),
            thesis_supported=extra.get("thesis_supported"),
            sell_failed=bool(extra.get("sell_failed")),
            liquidity_collapsed=bool(extra.get("liquidity_collapsed")),
            malicious_evidence=bool(extra.get("malicious_evidence")),
        )

        score = score_pump_trench(
            mint,
            lifecycle=lifecycle,
            participants=participants,
            timeframes=timeframes,
            depth=depth,
            holders=holders,
            concentration=concentration,
            dev=dev,
            bundles=bundles,
            risk=risk,
            story_verified=bool(extra.get("story_verified")),
            thesis_supported=bool(extra.get("thesis_supported")),
            proven_wallets=int(extra.get("proven_wallets") or 0),
            smart_money_accumulating=bool(extra.get("smart_money_accumulating")),
        )

        consensus = build_consensus(tracked.nominations).get(mint)
        decision = decide_trench_tier(
            mint,
            score=score.score,
            reasons=score.reasons,
            almost_bonded=lifecycle.almost_bonded,
            runner_threshold=self.runner_threshold,
            heads_up_threshold=self.heads_up_threshold,
            risk_blocked=risk.blocked,
            clustered_demand=bool(
                participants
                and participants.clustered_percent is not None
                and participants.clustered_percent > Decimal("50")
            ),
            thin_liquidity=depth.thin,
            bundle_high=bool(bundles and bundles.risk == "HIGH"),
            dev_selling=bool(dev and dev.holding.selling),
            already_alerted=tracked.alerted_at is not None,
            rate_limited=self._rate_limited(now),
            in_cooldown=self._in_cooldown(tracked, now),
            confluence=bool(consensus and consensus.strong),
        )

        tracked.cadence = cadence_tier(
            score=score.score,
            alpha_threshold=self.runner_threshold,
            almost_bonded=lifecycle.almost_bonded,
            buyer_burst=bool(extra.get("buyer_burst")),
            momentum_increasing=timeframes.momentum_curve == "INCREASING",
        )
        self._enforce_cadence_caps()

        await self.store.record_token(
            mint,
            now=now,
            name=str(extra.get("name") or tracked.name),
            symbol=str(extra.get("symbol") or tracked.symbol),
            creator=str(extra.get("creator") or tracked.creator),
            created_at=tracked.created_at,
            source=tracked.source,
            stage=lifecycle.stage,
            bonding_percent=lifecycle.progress_percent,
            market_cap_usd=market_cap,
            liquidity_usd=liquidity,
            holders=extra.get("holders"),
            top10_percent=(holders.top10_percent if holders else None),
            special_mode=lifecycle.special_mode,
        )

        public_trend = None
        if self.public_model_enabled:
            public_trend = score_public_trend(
                mint,
                timeframes=timeframes,
                depth=depth,
                independent_buyers=(
                    participants.independent_buyers if participants else None
                ),
                holder_velocity=(
                    timeframes.window("5m").holder_velocity
                    if timeframes.window("5m")
                    else None
                ),
                dex_paid=bool(extra.get("dex_paid")),
                dex_boosts=int(extra.get("dex_boosts") or 0),
                sources=(
                    pump_onchain("bonding curve and program state"),
                    *(
                        (SourceRef(kind=DEXSCREENER_PUBLIC, detail="market data"),)
                        if market_cap is not None
                        else ()
                    ),
                ),
            )

        token_row = await self.store.token(mint)
        first_market_cap = (
            Decimal(str(token_row["first_market_cap_usd"]))
            if token_row and token_row.get("first_market_cap_usd") is not None
            else None
        )

        return TrenchCandidate(
            mint=mint,
            lifecycle=lifecycle,
            score=score,
            decision=decision,
            timeframes=timeframes,
            participants=participants,
            dev=dev,
            bundles=bundles,
            risk=risk,
            consensus=consensus,
            public_trend=public_trend,
            name=str(extra.get("name") or tracked.name),
            symbol=str(extra.get("symbol") or tracked.symbol),
            market_cap_usd=market_cap,
            first_market_cap_usd=first_market_cap,
            liquidity_usd=liquidity,
            holders=extra.get("holders"),
            top10_percent=(holders.top10_percent if holders else None),
            age_seconds=lifecycle.age_seconds,
            cadence=tracked.cadence,
        )

    def _enforce_cadence_caps(self) -> None:
        """Keep the fast tiers bounded in population, not just in interval."""

        hot = [item for item in self._tracked.values() if item.cadence == CADENCE_HOT]
        if len(hot) > self.cadence.max_hot:
            hot.sort(key=lambda item: item.last_scored_at)
            for item in hot[: len(hot) - self.cadence.max_hot]:
                item.cadence = CADENCE_WARM
        warm = [item for item in self._tracked.values() if item.cadence == CADENCE_WARM]
        if len(warm) > self.cadence.max_warm:
            warm.sort(key=lambda item: item.last_scored_at)
            for item in warm[: len(warm) - self.cadence.max_warm]:
                item.cadence = CADENCE_NORMAL

    # ------------------------------------------------------------------
    def _rate_limited(self, now: int) -> bool:
        self._alert_times = [at for at in self._alert_times if now - at < 3600]
        return len(self._alert_times) >= self.max_alerts_per_hour

    def _in_cooldown(self, tracked: _Tracked, now: int) -> bool:
        return (
            tracked.alerted_at is not None
            and now - tracked.alerted_at < self.cooldown_seconds
        )

    async def _emit(self, candidate: TrenchCandidate, *, now: int) -> bool:
        if not candidate.score.has_named_reason:
            await self._record_suppression(candidate, now=now)
            return False
        published = True
        if self._publish is not None:
            published = bool(await self._publish(candidate))
        if not published:
            self.alerts_suppressed += 1
            return False
        tracked = self._tracked.get(candidate.mint)
        if tracked is not None:
            tracked.alerted_at = now
        self._alert_times.append(now)
        self.alerts_published += 1
        self.last_alert_at = now
        await self.store.record_alert(
            candidate.mint,
            tier=candidate.decision.tier,
            at=now,
            score=candidate.score.score,
            stage=candidate.lifecycle.stage,
            bonding_percent=candidate.lifecycle.progress_percent,
            market_cap_usd=candidate.market_cap_usd,
            reasons=candidate.score.reasons,
        )
        return True

    async def _record_suppression(self, candidate: TrenchCandidate, *, now: int) -> None:
        reason = candidate.decision.suppression
        if not reason:
            return
        self.alerts_suppressed += 1
        await self.store.record_suppression(
            candidate.mint,
            reason_code=reason,
            at=now,
            score=candidate.score.score,
            stage=candidate.lifecycle.stage,
            detail=candidate.decision.detail,
        )

    async def _rank_public(self, candidates: list[TrenchCandidate], *, now: int) -> None:
        """Publish our own ranking — never anyone else's (sections 31, 32)."""

        scores = [item.public_trend for item in candidates if item.public_trend is not None]
        if not scores:
            return
        ranked = rank_public_trend(
            scores, previous_ranks=self._public_ranks, min_score=self.public_model_min_score
        )
        fresh: dict[str, int] = {}
        for row in ranked:
            fresh[row.mint] = row.rank
            await self.store.record_public_rank(
                row.mint,
                at=now,
                rank=row.rank,
                score=row.score.score,
                shape=row.score.shape,
                momentum_curve=row.score.momentum_curve,
                model=row.score.model,
            )
        self._public_ranks = fresh

    # ------------------------------------------------------------------
    # operator surfaces
    # ------------------------------------------------------------------
    async def sections(self, *, limit: int = 8) -> dict[str, list[dict[str, Any]]]:
        """The Trenches sections an operator browses (section 78).

        ``hot`` is the cadence tiers rather than a lifecycle stage: a mid-curve
        token nothing else classifies is exactly the kind the engine is watching
        most closely, and it would otherwise be invisible between "new" and
        "almost bonded".
        """

        hot_mints = [
            tracked.mint
            for tracked in sorted(
                (
                    item
                    for item in self._tracked.values()
                    if item.cadence in {CADENCE_HOT, CADENCE_WARM}
                ),
                key=lambda item: (item.cadence != CADENCE_HOT, -item.last_scored_at),
            )[:limit]
        ]
        hot_rows: list[dict[str, Any]] = []
        for mint in hot_mints:
            row = await self.store.token(mint)
            if row is not None:
                tracked = self._tracked.get(mint)
                row = dict(row)
                row["cadence"] = tracked.cadence if tracked else CADENCE_NORMAL
                hot_rows.append(row)

        return {
            "new": await self.store.tokens_by_stage(tuple(TRENCH_NEW_STAGES), limit=limit),
            "almost_bonded": await self.store.tokens_by_stage(
                tuple(TRENCH_ALMOST_BONDED_STAGES), limit=limit
            ),
            "recently_bonded": await self.store.tokens_by_stage(
                tuple(TRENCH_RECENTLY_BONDED_STAGES), limit=limit
            ),
            "hot": hot_rows,
        }

    async def public_board(self, *, limit: int = 12) -> list[dict[str, Any]]:
        cursor = await self.store._db.execute(
            """
            SELECT mint, rank, score, shape, momentum_curve, model FROM public_trend_ranks
            WHERE observed_at = (SELECT MAX(observed_at) FROM public_trend_ranks)
            ORDER BY rank ASC LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def status(self, *, now: int | None = None) -> dict[str, Any]:
        """What `/fomo realtime` reports about this lane (section 80)."""

        moment = now if now is not None else int(time.time())
        cadences = {tier: 0 for tier in (CADENCE_HOT, CADENCE_WARM, CADENCE_NORMAL)}
        for tracked in self._tracked.values():
            cadences[tracked.cadence] = cadences.get(tracked.cadence, 0) + 1

        # Section 4: report every lane's state, so an outage in one vendor is
        # visible rather than silently narrowing the candidate universe.  The
        # self-sufficient flag answers "are we still finding tokens without any
        # third party?" — which is the whole point of building on-chain.
        lane_counts = await self.store.lane_counts(since=moment - 3600)
        universe = UniverseHealth(
            lanes=tuple(
                LaneHealth(
                    lane=lane,
                    enabled=True,
                    configured=True,
                    nominations=lane_counts.get(lane, 0),
                )
                for lane in DISCOVERY_LANES
            )
        )
        return {
            "enabled": self.enabled,
            "tracked": len(self._tracked),
            "scans": self.scans,
            "last_scan_at": self.last_scan_at,
            "last_scan_age": (
                None if self.last_scan_at is None else max(0, moment - self.last_scan_at)
            ),
            "creations_seen": self.creations_seen,
            "alerts_published": self.alerts_published,
            "alerts_suppressed": self.alerts_suppressed,
            "last_alert_at": self.last_alert_at,
            "cadence_tiers": cadences,
            "public_model_enabled": self.public_model_enabled,
            "public_ranked": len(self._public_ranks),
            "chain": self.chain.usage_snapshot(),
            "lane_counts": lane_counts,
            "universe_health": universe.to_json(),
            "discovery_latency": await self.store.discovery_latency_by_source(
                since=moment - 86_400
            ),
            "live_execution": False,
        }


__all__ = [
    "LANE_PUBLIC_MODEL",
    "LANE_PUMP_BONDING",
    "SOURCE_CREATION_STREAM",
    "SOURCE_EXTERNAL",
    "SOURCE_POLL",
    "SUPPRESS_LATE_DISCOVERY",
    "EnrichmentFn",
    "PublishFn",
    "ScanResult",
    "TrenchCandidate",
    "TrenchesRuntime",
]
