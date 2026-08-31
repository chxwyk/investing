"""The Trending lane's runtime: one lightweight loop, one fast hot-watch loop.

This is the orchestration layer between the pure :mod:`smart_money_bot.trending`
package and the outside world.  It owns no strategy of its own — every decision
it takes comes from that package — and it owns no Discord object, so the engine
can drive it and the tests can drive it identically.

**Why it is a separate loop (section 74).**  The legacy graduated radar polls at
60s, enriches at 30s and rechecks at 1800s.  Trending has completely different
economics: the board changes in seconds, a new entrant is only interesting for
minutes, and the snapshot that tells you so is cheap.  Sharing the legacy queue
would put a brand-new Trending entrant behind a backlog of deep graduated
analysis — which is exactly the "stale work sitting for tens of minutes" problem
in section 77.  So Trending gets its own loop, its own cadence, and its own
priority ordering where new entrants go first.

**The pipeline (section 76)** is deliberately staged cheapest-first:

    TRENDING SOURCE → PERSIST SNAPSHOT → CHEAP DELTA → RADAR / HOT WATCH / ALERT
    → TARGETED ENRICHMENT

Nothing that reaches an operator waits on enrichment.  Enrichment happens after
the cheap verdict, and only for the candidates that earned it.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .trending import (
    HOT_WATCH_PROMOTED,
    ORIGIN_NEW_ENTRY,
    ORIGIN_TRENDING_NEAR_MISS,
    TIER_ALPHA,
    TRENDING_CONTINUATION,
    TRENDING_NEW_ENTRY,
    TRENDING_REENTRY,
    AlertVerdict,
    HolderProfile,
    HotWatchConfig,
    HotWatchEntry,
    TrendingEdgeScore,
    TrendingEvent,
    TrendingLedgerEntry,
    TrendingObservation,
    TrendingRiskPanel,
    TrendingShadowConfig,
    UniverseComparison,
    UniverseTrade,
    assess_holders,
    assess_lane_health,
    board_diff,
    build_universe_report,
    classify_trending_event,
    compare_universes,
    decide_alert,
    market_cap_velocity,
    open_hot_watch,
    prune,
    rank_velocity,
    recheck_hot_watch,
    summarise,
)
from .trending.events import TrendingEventConfig
from .trending.latency import (
    STAGE_BOT_OBSERVATION,
    STAGE_CHEAP_VERDICT,
    STAGE_DISCORD_SEND,
    STAGE_SOURCE_APPEARANCE,
)
from .trending_source import TrendingClient
from .trending_store import TrendingStore

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class TrendingCandidate:
    """Everything the alerting and card layers need about one exact mint."""

    entry: TrendingLedgerEntry
    event: TrendingEvent
    score: TrendingEdgeScore
    verdict: AlertVerdict
    holders: HolderProfile | None = None
    risk: TrendingRiskPanel | None = None
    market_cap_velocity: Decimal | None = None

    @property
    def mint(self) -> str:
        return self.entry.mint

    def to_json(self) -> dict[str, Any]:
        return {
            "entry": self.entry.to_json(),
            "event": self.event.to_json(),
            "score": self.score.to_json(),
            "verdict": self.verdict.to_json(),
            "holders": self.holders.to_json() if self.holders else None,
            "risk": self.risk.to_json() if self.risk else None,
            "market_cap_velocity": (
                None if self.market_cap_velocity is None else str(self.market_cap_velocity)
            ),
        }


@dataclass(frozen=True, slots=True)
class PollResult:
    """What one Trending poll produced."""

    observed: int = 0
    new_entries: tuple[str, ...] = ()
    left_board: tuple[str, ...] = ()
    candidates: tuple[TrendingCandidate, ...] = ()
    alerts: tuple[TrendingCandidate, ...] = ()
    hot_watched: tuple[str, ...] = ()
    error: str = ""

    @property
    def rank_movers(self) -> int:
        return sum(
            1
            for candidate in self.candidates
            if candidate.event.rank_velocity is not None
            and candidate.event.rank_velocity.climbing
        )


#: Injected so a caller can supply real enrichment (holders, safety, theses,
#: wallets) without this module importing a provider.  It receives the cheap
#: ledger entry and returns whatever it could establish; returning ``None`` for
#: everything is a valid, honest answer and never becomes a fabricated value.
EnrichmentFn = Callable[[TrendingLedgerEntry], Awaitable[dict[str, Any]]]
PublishFn = Callable[[TrendingCandidate], Awaitable[bool]]


class TrendingRuntime:
    """Drives the Trending ledger, the hot-watch lane and the operator surfaces."""

    def __init__(
        self,
        store: TrendingStore,
        client: TrendingClient,
        *,
        max_tracked: int = 60,
        alpha_threshold: Decimal = Decimal("62"),
        watch_threshold: Decimal = Decimal("40"),
        hot_watch_config: HotWatchConfig | None = None,
        hot_watch_enabled: bool = True,
        event_config: TrendingEventConfig | None = None,
        shadow_config: TrendingShadowConfig | None = None,
        max_alerts_per_hour: int = 10,
        cooldown_seconds: int = 1800,
        stale_snapshot_seconds: int = 600,
        enabled: bool = True,
        enrich: EnrichmentFn | None = None,
        publish: PublishFn | None = None,
    ) -> None:
        self.store = store
        self.client = client
        self.max_tracked = max_tracked
        self.alpha_threshold = alpha_threshold
        self.watch_threshold = watch_threshold
        self.hot_watch_config = hot_watch_config or HotWatchConfig()
        self.hot_watch_enabled = hot_watch_enabled
        self.event_config = event_config or TrendingEventConfig()
        self.shadow_config = shadow_config
        self.max_alerts_per_hour = max_alerts_per_hour
        self.cooldown_seconds = cooldown_seconds
        self.stale_snapshot_seconds = stale_snapshot_seconds
        self.enabled = enabled
        self._enrich = enrich
        self._publish = publish

        self._board: dict[str, TrendingLedgerEntry] = {}
        self._hot_watches: dict[str, HotWatchEntry] = {}
        self._alerted: dict[str, int] = {}
        self._alert_times: list[int] = []
        self.polls = 0
        self.alerts_published = 0
        self.alerts_suppressed = 0
        self.promotions = 0
        self.last_poll_at: int | None = None
        self.last_alert_at: int | None = None
        self.last_error = ""
        self._restored = False

    # ------------------------------------------------------------------
    async def restore(self) -> None:
        """Rebuild in-memory state after a restart (section 111).

        Nothing is invented on restore: the ledger's immutable entry numbers and
        each hot watch's original timing evidence come back exactly as persisted,
        which is what stops a redeploy from resetting "when did we first see
        this?" — and therefore from making a late alert look early.
        """

        if self._restored:
            return
        for entry in await self.store.load_board(limit=self.max_tracked * 2):
            self._board[entry.mint] = entry
        now = int(time.time())
        for hot in await self.store.active_hot_watches():
            # A hot watch whose window elapsed while the process was down is
            # expired, not resurrected with a fresh clock.
            if now < hot.expires_at:
                self._hot_watches[hot.mint] = hot
        self._restored = True

    # ------------------------------------------------------------------
    async def poll_once(self, *, now: int | None = None) -> PollResult:
        """One cheap pass: fetch, diff, classify, decide.  No deep enrichment."""

        if not self.enabled:
            return PollResult(error="Trending lane disabled by configuration")
        await self.restore()
        moment = now if now is not None else int(time.time())

        try:
            observations = await self.client.fetch_board(limit=self.max_tracked)
        except Exception as exc:  # pragma: no cover - defensive; the loop must survive
            self.last_error = str(exc)[:200]
            return PollResult(error=self.last_error)

        self.polls += 1
        self.last_poll_at = moment
        self.last_error = getattr(self.client, "last_error", "") or ""
        if not observations:
            return PollResult(error=self.last_error)

        previous = tuple(mint for mint, entry in self._board.items() if entry.on_board)
        current = tuple(observation.mint for observation in observations)
        entered, _, left = board_diff(previous, current)

        if left:
            await self.store.mark_left_board(tuple(sorted(left)), at=moment)
            for mint in left:
                existing = self._board.get(mint)
                if existing is not None:
                    self._board[mint] = existing.mark_left_board(at=moment)

        candidates: list[TrendingCandidate] = []
        for observation in observations:
            candidate = await self._observe(observation, now=moment)
            if candidate is not None:
                candidates.append(candidate)

        # Mints that entered the board *on this poll* are surfaced first, ahead
        # of everything already being tracked.  This is the anti-backlog rule
        # (section 77): a brand-new entrant is only interesting for minutes, so
        # it must never queue behind older, higher-scoring board members.  Note
        # the key is board membership, not the derived event state — an
        # established token can still be inside its "new entry" window.
        candidates.sort(
            key=lambda item: (
                item.mint in entered,
                item.event.state in {TRENDING_NEW_ENTRY, TRENDING_REENTRY},
                item.score.score,
            ),
            reverse=True,
        )

        alerts: list[TrendingCandidate] = []
        hot_watched: list[str] = []
        for candidate in candidates:
            if candidate.verdict.alert:
                if await self._emit(candidate, now=moment):
                    alerts.append(candidate)
            else:
                await self._record_suppression(candidate, now=moment)
                if (
                    candidate.verdict.hot_watch_candidate
                    and self.hot_watch_enabled
                    and await self._open_hot_watch(candidate, now=moment)
                ):
                    hot_watched.append(candidate.mint)

        return PollResult(
            observed=len(observations),
            new_entries=tuple(sorted(entered)),
            left_board=tuple(sorted(left)),
            candidates=tuple(candidates),
            alerts=tuple(alerts),
            hot_watched=tuple(hot_watched),
            error=self.last_error,
        )

    # ------------------------------------------------------------------
    async def _observe(
        self,
        observation: TrendingObservation,
        *,
        now: int,
    ) -> TrendingCandidate | None:
        existing = self._board.get(observation.mint)
        if existing is None:
            existing = await self.store.load_entry(observation.mint)

        if existing is None:
            entry = TrendingLedgerEntry.from_first_observation(observation)
            # Stamp the first-observation latency the instant cheap discovery
            # sees the mint; everything after this is enrichment and may not
            # delay the number we measure ingestion with.
            await self.store.stamp_latency(
                observation.mint,
                STAGE_SOURCE_APPEARANCE,
                at=now,
                market_cap_usd=observation.market_cap_usd,
            )
            await self.store.stamp_latency(
                observation.mint,
                STAGE_BOT_OBSERVATION,
                at=now,
                market_cap_usd=observation.market_cap_usd,
            )
        else:
            entry = existing.observe(observation)

        previous_mc, gap = await self.store.previous_market_cap(
            observation.mint, before=observation.observed_at
        )
        await self.store.record_observation(entry, observation)
        self._board[entry.mint] = entry

        history = await self.store.rank_history(entry.mint, limit=32)
        velocity = rank_velocity(
            history or entry.rank_history,
            now=now,
            first_seen_at=entry.first_seen_at,
        )
        mc_velocity = market_cap_velocity(
            entry, previous_market_cap_usd=previous_mc, seconds=gap
        )

        enrichment: dict[str, Any] = {}
        if self._enrich is not None:
            enrichment = await self._enrich(entry) or {}

        holders = enrichment.get("holders")
        if holders is None:
            holders = assess_holders(
                entry.mint,
                holder_count=entry.holder_count,
                first_holder_count=entry.first_holder_count,
                seconds_elapsed=entry.seconds_trending(now=now),
                top10_percent=entry.top10_percent,
                first_top10_percent=entry.first_top10_percent,
                independent_buyers=enrichment.get("independent_buyers"),
                buys=enrichment.get("buys"),
            )

        event = classify_trending_event(
            entry,
            velocity,
            now=now,
            market_cap_velocity=mc_velocity,
            holder_growth=entry.holder_growth(),
            has_new_evidence=bool(enrichment.get("has_new_evidence")),
            config=self.event_config,
        )

        from .trending import score_trending_edge  # local import keeps the header short

        score = score_trending_edge(
            entry,
            event,
            holders=holders,
            theses=enrichment.get("theses"),
            social=enrichment.get("social"),
            risk=enrichment.get("risk"),
            story_verified=bool(enrichment.get("story_verified")),
            story_present=bool(enrichment.get("story_present")),
            ai_project_supported=bool(enrichment.get("ai_project_supported")),
            proven_wallets=int(enrichment.get("proven_wallets") or 0),
            smart_money_accumulating=bool(enrichment.get("smart_money_accumulating")),
            market_cap_velocity=mc_velocity,
            legacy_score=enrichment.get("legacy_score"),
        )

        verdict = decide_alert(
            score,
            event,
            alpha_threshold=self.alpha_threshold,
            watch_threshold=self.watch_threshold,
            hot_watch_band=self.hot_watch_config.near_miss_band,
            risk=enrichment.get("risk"),
            already_alerted=entry.mint in self._alerted,
            rate_limited=self._rate_limited(now=now),
            in_cooldown=self._in_cooldown(entry.mint, now=now),
        )

        await self.store.stamp_latency(
            entry.mint,
            STAGE_CHEAP_VERDICT,
            at=now,
            market_cap_usd=entry.current_market_cap_usd,
        )
        await self.store.record_event(
            entry.mint,
            state=event.state,
            occurred_at=now,
            rank=entry.current_rank,
            rank_delta=velocity.delta,
            market_cap_usd=entry.current_market_cap_usd,
            move_percent=event.move_since_entry_percent,
            score=score.score,
            reasons=score.reasons,
            payload={"tier": verdict.tier, "suppression": verdict.suppression},
        )

        return TrendingCandidate(
            entry=entry,
            event=event,
            score=score,
            verdict=verdict,
            holders=holders,
            risk=enrichment.get("risk"),
            market_cap_velocity=mc_velocity,
        )

    # ------------------------------------------------------------------
    def _rate_limited(self, *, now: int) -> bool:
        self._alert_times = [at for at in self._alert_times if now - at < 3600]
        return len(self._alert_times) >= self.max_alerts_per_hour

    def _in_cooldown(self, mint: str, *, now: int) -> bool:
        last = self._alerted.get(mint)
        return last is not None and now - last < self.cooldown_seconds

    async def _emit(self, candidate: TrendingCandidate, *, now: int) -> bool:
        """Send one urgent alert, and only with a named serious reason (§57)."""

        if not candidate.score.has_named_reason:
            await self._record_suppression(candidate, now=now)
            return False
        published = True
        if self._publish is not None:
            published = bool(await self._publish(candidate))
        if not published:
            self.alerts_suppressed += 1
            return False
        self._alerted[candidate.mint] = now
        self._alert_times.append(now)
        self.alerts_published += 1
        self.last_alert_at = now
        await self.store.stamp_latency(
            candidate.mint,
            STAGE_DISCORD_SEND,
            at=now,
            market_cap_usd=candidate.entry.current_market_cap_usd,
        )
        return True

    async def _record_suppression(self, candidate: TrendingCandidate, *, now: int) -> None:
        if not candidate.verdict.suppression:
            return
        self.alerts_suppressed += 1
        await self.store.record_suppression(
            candidate.mint,
            reason_code=candidate.verdict.suppression,
            at=now,
            score=candidate.score.score,
            market_cap_usd=candidate.entry.current_market_cap_usd,
            detail=candidate.verdict.detail,
        )

    async def _open_hot_watch(self, candidate: TrendingCandidate, *, now: int) -> bool:
        existing = self._hot_watches.get(candidate.mint)
        if existing is not None and existing.active:
            return False
        live = prune(self._hot_watches.values(), now=now, config=self.hot_watch_config)
        if len(live) >= self.hot_watch_config.max_entries:
            return False
        origin = (
            ORIGIN_NEW_ENTRY
            if candidate.event.state in {TRENDING_NEW_ENTRY, TRENDING_REENTRY}
            else ORIGIN_TRENDING_NEAR_MISS
        )
        entry = open_hot_watch(
            candidate.mint,
            origin=origin,
            now=now,
            score=candidate.score.score,
            market_cap_usd=candidate.entry.current_market_cap_usd,
            first_seen_market_cap_usd=candidate.entry.first_market_cap_usd,
            trending_entry_market_cap_usd=candidate.entry.first_market_cap_usd,
            heads_up_market_cap_usd=candidate.entry.current_market_cap_usd,
            config=self.hot_watch_config,
            note=candidate.verdict.detail,
        )
        self._hot_watches[candidate.mint] = entry
        await self.store.save_hot_watch(entry)
        return True

    # ------------------------------------------------------------------
    async def recheck_hot_watches(self, *, now: int | None = None) -> tuple[TrendingCandidate, ...]:
        """The fast lane (section 46).

        Reevaluates every due hot watch from *cached* state — the ledger entry,
        the persisted rank history and whatever the injected enrichment can
        supply cheaply — so a bounded fast cadence does not become a provider
        explosion (section 112).
        """

        if not self.hot_watch_enabled:
            return ()
        await self.restore()
        moment = now if now is not None else int(time.time())
        promoted: list[TrendingCandidate] = []

        for mint, hot in list(self._hot_watches.items()):
            if not hot.active:
                continue
            if not hot.due(now=moment, config=self.hot_watch_config):
                continue
            entry = self._board.get(mint) or await self.store.load_entry(mint)
            if entry is None:
                continue

            candidate = await self._rescore(entry, now=moment)
            outcome = recheck_hot_watch(
                hot,
                now=moment,
                score=candidate.score.score,
                reasons=candidate.score.reasons,
                market_cap_usd=entry.current_market_cap_usd,
                alpha_threshold=self.alpha_threshold,
                blocked=bool(candidate.risk and candidate.risk.blocked),
                actionable=candidate.score.actionable,
                config=self.hot_watch_config,
            )
            self._hot_watches[mint] = outcome.entry
            await self.store.save_hot_watch(outcome.entry)

            if outcome.promoted:
                self.promotions += 1
                if await self._emit(candidate, now=moment):
                    promoted.append(candidate)
            elif outcome.expired or outcome.dropped:
                # Expiry is silent by design: a candidate whose evidence never
                # strengthened is not worth a message (section 104).
                self._hot_watches.pop(mint, None)

        return tuple(promoted)

    async def _rescore(self, entry: TrendingLedgerEntry, *, now: int) -> TrendingCandidate:
        """Recompute a candidate from cached evidence — no new board fetch."""

        history = await self.store.rank_history(entry.mint, limit=32)
        velocity = rank_velocity(
            history or entry.rank_history, now=now, first_seen_at=entry.first_seen_at
        )
        previous_mc, gap = await self.store.previous_market_cap(entry.mint, before=now)
        mc_velocity = market_cap_velocity(
            entry, previous_market_cap_usd=previous_mc, seconds=gap
        )
        enrichment = await self._enrich(entry) if self._enrich is not None else {}
        enrichment = enrichment or {}
        holders = enrichment.get("holders") or assess_holders(
            entry.mint,
            holder_count=entry.holder_count,
            first_holder_count=entry.first_holder_count,
            seconds_elapsed=entry.seconds_trending(now=now),
            top10_percent=entry.top10_percent,
            first_top10_percent=entry.first_top10_percent,
            independent_buyers=enrichment.get("independent_buyers"),
            buys=enrichment.get("buys"),
        )
        event = classify_trending_event(
            entry,
            velocity,
            now=now,
            market_cap_velocity=mc_velocity,
            holder_growth=entry.holder_growth(),
            has_new_evidence=bool(enrichment.get("has_new_evidence")),
            config=self.event_config,
        )
        from .trending import score_trending_edge

        score = score_trending_edge(
            entry,
            event,
            holders=holders,
            theses=enrichment.get("theses"),
            social=enrichment.get("social"),
            risk=enrichment.get("risk"),
            story_verified=bool(enrichment.get("story_verified")),
            story_present=bool(enrichment.get("story_present")),
            ai_project_supported=bool(enrichment.get("ai_project_supported")),
            proven_wallets=int(enrichment.get("proven_wallets") or 0),
            smart_money_accumulating=bool(enrichment.get("smart_money_accumulating")),
            market_cap_velocity=mc_velocity,
            legacy_score=enrichment.get("legacy_score"),
        )
        verdict = decide_alert(
            score,
            event,
            alpha_threshold=self.alpha_threshold,
            watch_threshold=self.watch_threshold,
            hot_watch_band=self.hot_watch_config.near_miss_band,
            risk=enrichment.get("risk"),
            already_alerted=entry.mint in self._alerted,
            rate_limited=self._rate_limited(now=now),
            in_cooldown=self._in_cooldown(entry.mint, now=now),
        )
        return TrendingCandidate(
            entry=entry,
            event=event,
            score=score,
            verdict=verdict,
            holders=holders,
            risk=enrichment.get("risk"),
            market_cap_velocity=mc_velocity,
        )

    # ------------------------------------------------------------------
    # operator surfaces
    # ------------------------------------------------------------------
    def lane_health(self, *, now: int | None = None) -> dict[str, Any]:
        moment = now if now is not None else int(time.time())
        health = assess_lane_health(
            enabled=self.enabled,
            source=self.client.source,
            snapshots=int(getattr(self.client, "snapshots", 0) or 0),
            last_snapshot_at=getattr(self.client, "last_snapshot_at", None),
            last_error=getattr(self.client, "last_error", "") or "",
            tracked=sum(1 for entry in self._board.values() if entry.on_board),
            now=moment,
            stale_after_seconds=self.stale_snapshot_seconds,
        )
        return health.to_json()

    def hot_watch_status(self) -> dict[str, Any]:
        return summarise(self._hot_watches.values()).to_json()

    async def hot_watch_report(self) -> dict[str, Any]:
        """Section 90, computed over persisted history rather than memory alone."""

        history = await self.store.hot_watch_history(limit=500)
        status = summarise(history)
        payload = status.to_json()
        payload["active_mints"] = [
            entry.mint for entry in self._hot_watches.values() if entry.active
        ]
        payload["recent"] = [
            {
                "mint": entry.mint,
                "state": entry.state,
                "origin": entry.origin,
                "entry_score": str(entry.entry_score),
                "best_score": str(entry.best_score),
                "rechecks": entry.rechecks,
                "promotion_delay_seconds": entry.promotion_delay_seconds(),
                "promotion_move_percent": (
                    None
                    if entry.promotion_move_percent() is None
                    else str(entry.promotion_move_percent())
                ),
            }
            for entry in history[:10]
        ]
        payload["promotions_this_session"] = self.promotions
        return payload

    def board(self, *, limit: int = 15) -> tuple[TrendingLedgerEntry, ...]:
        live = [entry for entry in self._board.values() if entry.on_board]
        live.sort(
            key=lambda entry: (
                entry.current_rank if entry.current_rank is not None else 10_000
            )
        )
        return tuple(live[:limit])

    def entry_for(self, mint: str) -> TrendingLedgerEntry | None:
        return self._board.get(mint)

    def is_hot_watched(self, mint: str) -> bool:
        """Whether this mint reached an alert by way of the hot-watch lane.

        The card says "promoted from HOT WATCH" only when that is true, because
        a promotion and a first-pass alert are different events and an operator
        reading the timeline afterwards needs to be able to tell them apart.
        """

        entry = self._hot_watches.get(mint)
        return entry is not None and entry.promoted_at is not None

    async def status(self, *, now: int | None = None) -> dict[str, Any]:
        """What `/fomo realtime` reports about this lane (section 88)."""

        moment = now if now is not None else int(time.time())
        health = self.lane_health(now=moment)
        tracked = sum(1 for entry in self._board.values() if entry.on_board)
        return {
            "enabled": self.enabled,
            "source": self.client.source.to_json(),
            "source_label": self.client.source.label,
            "rank_caveat": self.client.source.rank_caveat(),
            "health": health,
            "polls": self.polls,
            "last_poll_at": self.last_poll_at,
            "last_poll_age": (
                None if self.last_poll_at is None else max(0, moment - self.last_poll_at)
            ),
            "tracked": tracked,
            "new_entries": sum(
                1 for entry in self._board.values() if entry.on_board and entry.is_new_entry_window
            ),
            "rank_movers": sum(
                1
                for entry in self._board.values()
                if entry.on_board
                and len(entry.rank_history) >= 2
                and entry.rank_history[-1].rank < entry.rank_history[0].rank
            ),
            "hot_watch": self.hot_watch_status(),
            "alerts_published": self.alerts_published,
            "alerts_suppressed": self.alerts_suppressed,
            "promotions": self.promotions,
            "last_alert_at": self.last_alert_at,
            "last_error": self.last_error,
            "live_execution": False,
        }

    # ------------------------------------------------------------------
    # the scoreboard (sections 66-68)
    # ------------------------------------------------------------------
    @staticmethod
    def compare(
        trending_trades: list[UniverseTrade],
        legacy_trades: list[UniverseTrade],
        *,
        trending_bankroll: Decimal | None = None,
        legacy_bankroll: Decimal | None = None,
    ) -> UniverseComparison:
        """$100 TRENDING versus $100 LEGACY, on identical arithmetic."""

        return compare_universes(
            build_universe_report(
                "TRENDING",
                trending_trades,
                current_bankroll_usd=trending_bankroll,
            ),
            build_universe_report(
                "LEGACY",
                legacy_trades,
                current_bankroll_usd=legacy_bankroll,
            ),
        )


__all__ = [
    "HOT_WATCH_PROMOTED",
    "TIER_ALPHA",
    "TRENDING_CONTINUATION",
    "EnrichmentFn",
    "PollResult",
    "PublishFn",
    "TrendingCandidate",
    "TrendingRuntime",
]
