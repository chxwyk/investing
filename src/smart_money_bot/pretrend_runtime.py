"""The pre-trend lane's runtime: collect, label, infer, and mostly stay quiet.

This runtime is deliberately boring, because the interesting work happens in
:mod:`smart_money_bot.pretrend` where it can be tested without a network or a
database.  What lives here is the orchestration and, more importantly, the
safety properties that only exist at the boundary:

**Collection is unconditional; alerting is not.**  Every cycle records board
snapshots and candidate observations whether or not a model exists.  This is the
one part of the system that must run from day one, because a model trained later
can only ever learn from data collected earlier.  Alerting, by contrast, is off
until a model has been trained, validated walk-forward, and has enough positives
behind it to quote a number.

**A prediction is recorded even when it is silent.**  Every scored candidate is
persisted with its probability and its feature vector, alerted or not.  A
scoreboard assembled only from published alerts measures the publishing rule.

**The lane is shadow-only by construction.**  :class:`PretrendRuntime` has no
reference to the executor, the paper engine or any trading surface, and no
configuration flag in this module can give it one.  Connecting the prediction
layer to execution requires a separate, deliberate change elsewhere — which is
the point of section 89.

**Nothing here can make the bot louder by accident.**  Every ping passes through
one :class:`~smart_money_bot.pretrend.states.AlertGate`, whose hourly ceiling is
restored from the database on startup so a redeploy cannot reset a cooldown.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from .pretrend.affinity import (
    AFFINITY_HORIZONS_SECONDS,
    AffinityRecord,
    build_population,
    quality_weight,
    rank_actors,
)
from .pretrend.cohorts import (
    DEFAULT_UNIVERSE_MAX_USD,
    DEFAULT_UNIVERSE_MIN_USD,
    in_universe,
)
from .pretrend.features import FEATURE_VERSION, FeatureVector, build_features
from .pretrend.groundtruth import (
    AUTHORISED_SOURCE_KINDS,
    DEFAULT_GROUND_TRUTH_CONFIG,
    FOMO_TREND_ENTER,
    GRADE_FOMO,
    GRADE_PROXY,
    GroundTruthConfig,
    TrendingGroundTruth,
    TrendingSnapshot,
)
from .pretrend.labels import LABEL_HORIZONS_SECONDS
from .pretrend.model import LogisticModel, Prediction
from .pretrend.providers import COLLECTOR_VERSION, BoardObserver
from .pretrend.states import (
    STATE_DISCOVERED,
    AlertDecision,
    AlertGate,
    GateConfig,
    TokenState,
)
from .pretrend_store import PretrendStore

ZERO = Decimal("0")

#: How far back a live scoring read goes.  Twice the longest feature window
#: (15m) plus headroom for sparse polling.
STATE_LOOKBACK_SECONDS = 3_600

#: Publish callbacks.  Returning False means the surface declined to send, and
#: the runtime records that rather than claiming an alert went out.
PublishFn = Callable[["PretrendSignal"], Awaitable[bool]]
ConfirmFn = Callable[["TrendConfirmation"], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class PretrendSignal:
    """Everything a PRE_TREND card needs, assembled once."""

    mint: str
    at: int
    probability: Decimal
    horizon_seconds: int
    reason: str
    model_version: str
    feature_version: str
    vector: FeatureVector
    state: TokenState
    prediction: Prediction
    #: Actors on this mint whose measured affinity is meaningful.
    notable_actors: tuple[AffinityRecord, ...] = ()
    base_rate: Decimal | None = None
    lift: Decimal | None = None
    sample_support: int = 0

    @property
    def market_cap_usd(self) -> Decimal | None:
        return self.vector.get("market_cap_usd")

    def to_json(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "at": self.at,
            "probability": str(self.probability),
            "horizon_seconds": self.horizon_seconds,
            "reason": self.reason,
            "model_version": self.model_version,
            "feature_version": self.feature_version,
            "base_rate": None if self.base_rate is None else str(self.base_rate),
            "lift": None if self.lift is None else str(self.lift),
            "sample_support": self.sample_support,
            "completeness": str(self.vector.completeness),
        }


@dataclass(frozen=True, slots=True)
class TrendConfirmation:
    """Ground truth arrived.  Includes whether we called it, and how early."""

    mint: str
    entered_at: int
    initial_rank: int | None
    market_cap_usd: Decimal | None
    liquidity_usd: Decimal | None
    volume_usd: Decimal | None
    holders: int | None
    token_age_seconds: int | None
    tier: str = ""
    symbol: str = ""
    name: str = ""
    predicted: bool = False
    lead_seconds: int | None = None
    first_alert_at: int | None = None
    first_alert_probability: Decimal | None = None
    first_alert_market_cap_usd: Decimal | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "entered_at": self.entered_at,
            "initial_rank": self.initial_rank,
            "market_cap_usd": (
                None if self.market_cap_usd is None else str(self.market_cap_usd)
            ),
            "predicted": self.predicted,
            "lead_seconds": self.lead_seconds,
            "first_alert_at": self.first_alert_at,
            "first_alert_probability": (
                None
                if self.first_alert_probability is None
                else str(self.first_alert_probability)
            ),
        }


@dataclass(frozen=True, slots=True)
class CycleResult:
    """What one runtime cycle did.  Counts, not prose."""

    snapshot_accepted: bool = False
    snapshot_reason: str = ""
    entered: tuple[str, ...] = ()
    reentered: tuple[str, ...] = ()
    left: tuple[str, ...] = ()
    present_unproven: tuple[str, ...] = ()
    #: Which namespace this cycle advanced: ``FOMO`` or ``PROXY``.
    grade: str = GRADE_PROXY
    after_coverage_gap: bool = False
    activity_events: int = 0
    observations_recorded: int = 0
    candidates_scored: int = 0
    signals: tuple[PretrendSignal, ...] = ()
    confirmations: tuple[TrendConfirmation, ...] = ()
    suppressed: dict[str, int] = field(default_factory=dict)
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "snapshot_accepted": self.snapshot_accepted,
            "snapshot_reason": self.snapshot_reason,
            "entered": list(self.entered),
            "reentered": list(self.reentered),
            "left": list(self.left),
            "present_unproven": list(self.present_unproven),
            "grade": self.grade,
            "after_coverage_gap": self.after_coverage_gap,
            "activity_events": self.activity_events,
            "observations_recorded": self.observations_recorded,
            "candidates_scored": self.candidates_scored,
            "signals": len(self.signals),
            "confirmations": len(self.confirmations),
            "suppressed": dict(self.suppressed),
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class PretrendConfig:
    """The lane's configuration.  Defaults are chosen to be quiet and honest."""

    enabled: bool = True
    #: Collection runs even when inference is off.  This is the important flag:
    #: turning it off means the research programme cannot start.
    collection_enabled: bool = True
    #: Inference stays off until a validated model exists.
    inference_enabled: bool = False
    #: Alerting stays off even when inference is on, until deliberately enabled.
    alerting_enabled: bool = False
    horizon_seconds: int = 300
    universe_min_usd: Decimal = DEFAULT_UNIVERSE_MIN_USD
    universe_max_usd: Decimal = DEFAULT_UNIVERSE_MAX_USD
    max_candidates_per_cycle: int = 60
    activity_page_limit: int = 200
    gate: GateConfig = field(default_factory=GateConfig)
    ground_truth: GroundTruthConfig = field(default_factory=lambda: DEFAULT_GROUND_TRUTH_CONFIG)
    #: Affinity is recomputed at most this often; it is a full-table pass.
    affinity_refresh_seconds: int = 900
    #: Minimum positives before the lane will alert at all, whatever the model
    #: says.  A model fitted on twelve board entries is a memoriser.
    min_positives_to_alert: int = 30


def _prediction_id(mint: str, at: int, lane: str, model_version: str) -> str:
    digest = hashlib.sha256(f"{lane}:{model_version}:{mint}:{at}".encode()).hexdigest()
    return digest[:32]


class PretrendRuntime:
    """Drives collection, ground truth, shadow inference and the alert budget."""

    def __init__(
        self,
        store: PretrendStore,
        *,
        observer: BoardObserver | None = None,
        activity_provider: Any = None,
        config: PretrendConfig | None = None,
        publish: PublishFn | None = None,
        confirm: ConfirmFn | None = None,
        model: LogisticModel | None = None,
        lane: str = "production",
    ) -> None:
        self.store = store
        self.observer = observer
        self.activity_provider = activity_provider
        self.config = config or PretrendConfig()
        self._publish = publish
        self._confirm = confirm
        self.model = model
        self.lane = lane

        self.ground_truth = TrendingGroundTruth(
            config=self.config.ground_truth, collector_version=COLLECTOR_VERSION
        )
        self.gate = AlertGate(self.config.gate)
        self._states: dict[str, TokenState] = {}
        self._affinities: dict[str, AffinityRecord] = {}
        self._baselines: dict[int, Decimal] = {}
        self._affinity_computed_at = 0
        self._activity_cursor = 0
        self._restored = False
        self.cycles = 0
        #: Confirmations withheld because the lane is in collect-quietly mode.
        self.suppressed_confirmations = 0
        #: Confirmations the surface declined to deliver.
        self.undelivered_confirmations = 0
        #: PRE_TREND signals withheld because alerting is disabled.
        self.suppressed_signals = 0
        self.last_cycle_at: int | None = None
        self.last_error = ""

    # ------------------------------------------------------------------
    async def restore(self, *, now: int | None = None) -> None:
        """Rebuild in-memory state from storage.

        Both restorations here exist to prevent a redeploy from silently
        undoing a decision: membership restores the write-once entry timestamps,
        and the alert-time window restores the hourly budget and the cooldowns.
        """

        if self._restored:
            return
        moment = now if now is not None else int(time.time())
        records = await self.store.load_membership()
        self.ground_truth = TrendingGroundTruth(
            config=self.config.ground_truth,
            records=records,
            collector_version=COLLECTOR_VERSION,
        )
        # Restoring membership is not enough: the tracker also needs to know
        # when coverage was last good, or the first snapshot after any restart
        # looks like a coverage gap and voids a legitimate entry.  A restart
        # that took longer than the gap bound SHOULD void it, which is exactly
        # what comparing against the real last-accepted time achieves.
        for grade, last_at in (
            (GRADE_FOMO, await self.store.last_accepted_snapshot_at(grade=GRADE_FOMO)),
            (GRADE_PROXY, await self.store.last_accepted_snapshot_at(grade=GRADE_PROXY)),
        ):
            if last_at is not None:
                self.ground_truth.note_coverage(grade=grade, at=last_at)
        for state in await self.store.load_states():
            self._states[state.mint] = state
        self.gate.restore(await self.store.recent_alert_times(since=moment - 3600))
        self._restored = True

    # ------------------------------------------------------------------
    async def collect_board(self, *, now: int | None = None) -> CycleResult:
        """One board reading, stored and diffed into ground truth."""

        if self.observer is None:
            return CycleResult(snapshot_reason="NO_OBSERVER_CONFIGURED")
        await self.restore(now=now)
        moment = now if now is not None else int(time.time())
        snapshot = await self.observer.observe(now=moment)
        return await self.ingest_snapshot(snapshot)

    async def ingest_snapshot(self, snapshot: TrendingSnapshot) -> CycleResult:
        """Apply a snapshot.  An invalid one changes nothing but is still stored."""

        await self.restore(now=snapshot.observed_at)
        outcome = self.ground_truth.ingest(snapshot)
        await self.store.record_snapshot(snapshot, outcome.validity)
        if not outcome.accepted:
            self.last_error = outcome.validity.detail
            return CycleResult(
                snapshot_accepted=False,
                snapshot_reason=outcome.validity.reason,
                grade=outcome.grade,
            )

        # Every event is persisted, proxy and unproven included: raw evidence is
        # kept so a gap or a source change is explicable later.  What differs is
        # which of them may become a label, and the event KIND carries that.
        await self.store.record_trend_events(outcome.events)
        touched = (
            set(outcome.entered)
            | set(outcome.reentered)
            | set(outcome.left)
            | set(outcome.present_unproven)
        )
        grade_records = (
            self.ground_truth.records
            if outcome.grade == GRADE_FOMO
            else self.ground_truth.proxy_records()
        )
        await self.store.upsert_membership(
            [record for mint, record in grade_records.items() if mint in touched]
        )
        # First-seen is recorded for any board sighting; it is an observation,
        # not a claim about entry.
        for row in snapshot.rows:
            await self.store.note_first_seen(
                row.mint,
                "fomo_trending" if outcome.grade == GRADE_FOMO else "trending_proxy",
                at=snapshot.observed_at,
                market_cap_usd=row.market_cap_usd,
            )

        confirmations = await self._confirm_entries(
            outcome.events, at=snapshot.observed_at
        )
        return CycleResult(
            snapshot_accepted=True,
            snapshot_reason="VALID",
            entered=outcome.entered,
            reentered=outcome.reentered,
            left=outcome.left,
            present_unproven=outcome.present_unproven,
            grade=outcome.grade,
            after_coverage_gap=outcome.after_coverage_gap,
            confirmations=confirmations,
        )

    @property
    def may_send(self) -> bool:
        """Whether this lane may put ANYTHING in front of a human.

        One predicate, consulted by every publish path in this class.  The first
        version of this lane gated the PRE_TREND card on ``alerting_enabled``
        and then published the confirmation card unconditionally, so the
        "collect quietly" default still pinged once per board entry -- and on a
        proxy board, once per row of the very first snapshot.  Routing every
        send through a single predicate is what stops the next publish path
        somebody adds from reintroducing that.
        """

        return self.config.enabled and self.config.alerting_enabled

    async def _confirm_entries(
        self, events: Sequence[Any], *, at: int
    ) -> tuple[TrendConfirmation, ...]:
        """Advance state for each witnessed FOMO entry; publish only if permitted.

        Ground truth is recorded either way -- that is the whole point of the
        collect-quietly default. What is conditional is the message.

        Two separate gates apply, and both are about honesty rather than taste:

        * Only a **witnessed FOMO entry** produces a confirmation at all. A
          proxy-board arrival is a different event on a different board, and a
          presence we never saw arrive is not an entry we can date.
        * An alert row is written **only when the card was actually delivered**.
          Recording an unsent confirmation as a delivered alert would corrupt
          the alert-rate metric and, worse, would make ``/pretrend missed``
          believe a token had been covered when nobody was told anything.
        """

        confirmations: list[TrendConfirmation] = []
        for event in events:
            # grade + kind together: a PROXY_BOARD_ENTER must never reach here,
            # and neither must a FOMO_BOARD_PRESENT_UNPROVEN.
            if not getattr(event, "establishes_label", False):
                continue
            state = self._states.get(event.mint) or TokenState(
                mint=event.mint, state=STATE_DISCOVERED, first_seen_at=at
            )
            decision = self.gate.confirm(state, at=event.occurred_at)
            self._states[event.mint] = decision.state
            await self.store.save_state(decision.state)
            confirmation = TrendConfirmation(
                mint=event.mint,
                entered_at=event.occurred_at,
                initial_rank=event.initial_rank,
                market_cap_usd=event.market_cap_usd,
                liquidity_usd=event.liquidity_usd,
                volume_usd=event.volume_usd,
                holders=event.holders,
                token_age_seconds=event.token_age_seconds,
                tier=event.tier,
                symbol=event.symbol,
                name=event.name,
                predicted=decision.state.predicted,
                lead_seconds=decision.state.lead_seconds,
                first_alert_at=decision.state.first_pretrend_alert_at,
                first_alert_probability=decision.state.first_pretrend_probability,
                first_alert_market_cap_usd=decision.state.first_pretrend_market_cap_usd,
            )
            confirmations.append(confirmation)

            if not self.may_send:
                self.suppressed_confirmations += 1
                continue

            delivered = True
            if self._confirm is not None:
                delivered = await self._confirm(confirmation)
            if not delivered:
                self.undelivered_confirmations += 1
                continue

            await self.store.record_alert(
                alert_id=_prediction_id(event.mint, event.occurred_at, "confirm", "gt"),
                mint=event.mint,
                sent_at=event.occurred_at,
                kind="TRENDING_CONFIRMED",
                reason=FOMO_TREND_ENTER,
                probability=decision.state.first_pretrend_probability,
                market_cap_usd=event.market_cap_usd,
                payload=confirmation.to_json(),
            )
        return tuple(confirmations)

    # ------------------------------------------------------------------
    async def collect_activity(self, *, now: int | None = None) -> int:
        """Pull the FOMO-native tape forward.  Returns events newly stored."""

        provider = self.activity_provider
        if provider is None or not getattr(provider, "available", False):
            return 0
        try:
            events = await provider.fetch_since(
                since=self._activity_cursor, limit=self.config.activity_page_limit
            )
        except Exception as exc:  # pragma: no cover - the loop must survive
            self.last_error = str(exc)[:200]
            return 0
        if not events:
            return 0
        stored = await self.store.record_activity(events)
        # Advance the cursor to the newest event we actually accepted, not to
        # "now" — advancing past unread events would silently drop tape.
        self._activity_cursor = max(event.occurred_at for event in events)
        for event in events:
            await self.store.note_first_seen(
                event.mint,
                "fomo_activity",
                at=event.occurred_at,
                market_cap_usd=event.market_cap_usd,
            )
        return stored

    async def record_candidate(
        self,
        *,
        mint: str,
        at: int,
        market_cap_usd: Decimal | None = None,
        price_usd: Decimal | None = None,
        liquidity_usd: Decimal | None = None,
        volume_usd: Decimal | None = None,
        buys: int | None = None,
        sells: int | None = None,
        unique_buyers: int | None = None,
        holders: int | None = None,
        net_flow_usd: Decimal | None = None,
        social_engagement: Decimal | None = None,
        token_age_seconds: int | None = None,
        pair_age_seconds: int | None = None,
        provider: str = "",
        source_at: int | None = None,
        source: str = "",
    ) -> bool:
        """Append one point-in-time candidate observation.

        Called by the existing discovery lanes.  This is the cheapest and most
        valuable thing the whole system does: without these rows there is no
        history to reconstruct a pre-entry state from, and no prediction is
        possible at all.
        """

        if not self.config.collection_enabled:
            return False
        await self.store.record_observation(
            mint=mint,
            observed_at=at,
            provider=provider,
            collector_version=COLLECTOR_VERSION,
            source_at=source_at,
            price_usd=price_usd,
            market_cap_usd=market_cap_usd,
            liquidity_usd=liquidity_usd,
            volume_usd=volume_usd,
            buys=buys,
            sells=sells,
            unique_buyers=unique_buyers,
            holders=holders,
            net_flow_usd=net_flow_usd,
            social_engagement=social_engagement,
            token_age_seconds=token_age_seconds,
            pair_age_seconds=pair_age_seconds,
        )
        if source:
            await self.store.note_first_seen(
                mint, source, at=at, market_cap_usd=market_cap_usd
            )
        return True

    # ------------------------------------------------------------------
    async def refresh_affinity(self, *, now: int | None = None, force: bool = False) -> int:
        """Recompute pre-trend affinity from the observation record."""

        moment = now if now is not None else int(time.time())
        if (
            not force
            and moment - self._affinity_computed_at < self.config.affinity_refresh_seconds
        ):
            return len(self._affinities)
        observations = await self.store.actor_observations()
        if not observations:
            self._affinity_computed_at = moment
            return 0
        handles = await self.store.actor_handles()
        records, baselines = build_population(
            observations,
            horizons=AFFINITY_HORIZONS_SECONDS,
            recent_since=moment - 7 * 86_400,
            handles=handles,
        )
        self._affinities = records
        self._baselines = baselines
        self._affinity_computed_at = moment
        await self.store.save_affinity(records.values(), computed_at=moment)
        return len(records)

    # ------------------------------------------------------------------
    async def score_candidates(
        self, mints: Sequence[str], *, now: int | None = None
    ) -> CycleResult:
        """Score candidates, record every prediction, and alert only if earned."""

        await self.restore(now=now)
        moment = now if now is not None else int(time.time())
        if not self.config.inference_enabled or self.model is None:
            return CycleResult(candidates_scored=0, error="inference is disabled")

        model_positives = getattr(self.model, "trained_positives", 0)
        may_alert = (
            self.may_send and model_positives >= self.config.min_positives_to_alert
        )

        signals: list[PretrendSignal] = []
        scored = 0
        on_board = self.ground_truth.on_board()

        for mint in mints[: self.config.max_candidates_per_cycle]:
            # A mint already on the board is not a prediction opportunity.
            if mint in on_board or self.ground_truth.first_trending_at(mint) is not None:
                continue
            # The feature ladder never looks back further than twice its
            # longest window, so the read is bounded rather than loading a
            # token's whole life on every cycle.
            state = replace(
                await self.store.rebuild_state(
                    mint, until=moment, since=moment - STATE_LOOKBACK_SECONDS
                ),
                affinities=self._affinities,
            )
            vector = build_features(state)
            market_cap = vector.get("market_cap_usd")
            if not in_universe(
                market_cap,
                minimum=self.config.universe_min_usd,
                maximum=self.config.universe_max_usd,
            ):
                continue

            prediction = self.model.score_vector(vector)
            scored += 1

            token_state = self._states.get(mint) or TokenState(
                mint=mint, state=STATE_DISCOVERED, first_seen_at=moment
            )
            independent = vector.get("independent_quality_buyers")
            decision: AlertDecision = self.gate.evaluate(
                token_state,
                probability=prediction.probability,
                now=moment,
                independent_quality_traders=int(independent or 0),
                market_cap_usd=market_cap,
            )
            # The gate classifies regardless; only delivery is gated by config.
            send = decision.send and may_alert
            if decision.send and not may_alert:
                self.suppressed_signals += 1
            self._states[mint] = decision.state
            await self.store.save_state(decision.state)

            await self.store.record_prediction(
                prediction_id=_prediction_id(
                    mint, moment, self.lane, prediction.model_version
                ),
                mint=mint,
                predicted_at=moment,
                model_version=prediction.model_version,
                feature_version=prediction.feature_version,
                lane=self.lane,
                horizon_seconds=self.config.horizon_seconds,
                probability=prediction.probability,
                calibration_bucket=prediction.calibration_bucket,
                sample_support=prediction.sample_support,
                missing_features=prediction.missing_features,
                market_cap_usd=market_cap,
                state=decision.state.state,
                alerted=send,
                reason_codes=prediction.reason_codes,
                vector=vector,
            )

            if not send:
                continue

            notable = tuple(
                record
                for record in (
                    self._affinities.get(arrival.actor_id) for arrival in state.arrivals
                )
                if record is not None and record.statistically_meaningful
            )
            base_rate = self._baselines.get(self.config.horizon_seconds)
            signal = PretrendSignal(
                mint=mint,
                at=moment,
                probability=prediction.probability,
                horizon_seconds=self.config.horizon_seconds,
                reason=decision.reason,
                model_version=prediction.model_version,
                feature_version=prediction.feature_version,
                vector=vector,
                state=decision.state,
                prediction=prediction,
                notable_actors=notable,
                base_rate=base_rate,
                lift=(
                    None
                    if base_rate is None or base_rate <= ZERO
                    else (prediction.probability / base_rate).quantize(Decimal("0.01"))
                ),
                sample_support=prediction.sample_support,
            )
            delivered = True
            if self._publish is not None:
                delivered = await self._publish(signal)
            if delivered:
                signals.append(signal)
                await self.store.record_alert(
                    alert_id=_prediction_id(mint, moment, "alert", prediction.model_version),
                    mint=mint,
                    sent_at=moment,
                    kind="PRE_TREND",
                    reason=decision.reason,
                    probability=prediction.probability,
                    market_cap_usd=market_cap,
                    payload=signal.to_json(),
                )
                await self.store.open_paper_observation(
                    observation_id=_prediction_id(mint, moment, "paper", "v1"),
                    mint=mint,
                    signalled_at=moment,
                    entry_price_usd=vector.get("price_usd_level_1m"),
                    entry_market_cap_usd=market_cap,
                    entry_liquidity_usd=vector.get("liquidity_usd"),
                    probability=prediction.probability,
                )

        self.cycles += 1
        self.last_cycle_at = moment
        return CycleResult(
            candidates_scored=scored,
            signals=tuple(signals),
            suppressed=dict(self.gate.suppressed),
        )

    # ------------------------------------------------------------------
    @property
    def affinities(self) -> Mapping[str, AffinityRecord]:
        """The current affinity table.  Read-only view for surfaces and forensics."""

        return dict(self._affinities)

    @property
    def baselines(self) -> Mapping[int, Decimal]:
        """Population base rates per horizon, so no rate is quoted without one."""

        return dict(self._baselines)

    async def track_paper_observations(
        self, *, now: int | None = None, max_age_seconds: int = 7_200
    ) -> int:
        """Advance every open paper signal: excursions, outcome, resolution.

        Each open signal is marked to the latest observation we have for its
        mint, so maximum favourable and adverse excursion are recorded from real
        readings rather than reconstructed later from a price the token no
        longer has.  A signal resolves as ``TRENDED`` when ground truth says the
        mint entered the board after the signal, and as ``EXPIRED`` once the
        window has elapsed without that happening.

        ``EXPIRED`` is a loss and is stored as one.  Leaving losing signals
        permanently ``OPEN`` would be the most comfortable possible bug: the
        scoreboard would contain only winners and whatever had not failed yet.
        """

        moment = now if now is not None else int(time.time())
        updated = 0
        for row in await self.store.open_paper_observations():
            mint = row["mint"]
            signalled_at = int(row["signalled_at"])
            entered_at = self.ground_truth.first_trending_at(mint)

            observations = await self.store.observations_for(mint, until=moment)
            latest = observations[-1] if observations else None
            market_cap = (
                None
                if latest is None or latest["market_cap_usd"] is None
                else Decimal(str(latest["market_cap_usd"]))
            )

            outcome: str | None = None
            market_cap_at_trend: Decimal | None = None
            if entered_at is not None and entered_at > signalled_at:
                outcome = "TRENDED"
                entry = await self.store.trend_entry(mint)
                market_cap_at_trend = None if entry is None else entry.market_cap_usd
            elif moment - signalled_at >= max_age_seconds:
                outcome = "EXPIRED"

            await self.store.update_paper_observation(
                row["observation_id"],
                market_cap_usd=market_cap,
                trend_entered_at=(
                    entered_at if outcome == "TRENDED" else None
                ),
                market_cap_at_trend_usd=market_cap_at_trend,
                outcome=outcome,
                now=moment,
            )
            updated += 1
        return updated

    async def resolve_outcomes(self, *, now: int | None = None) -> int:
        """Settle elapsed predictions against ground truth."""

        moment = now if now is not None else int(time.time())
        return await self.store.resolve_predictions(
            now=moment, max_horizon=max(LABEL_HORIZONS_SECONDS)
        )

    async def status(self, *, now: int | None = None) -> dict[str, Any]:
        """The lane's honest state, including what it cannot currently measure."""

        moment = now if now is not None else int(time.time())
        activity = self.activity_provider
        activity_available = bool(getattr(activity, "available", False))
        model = await self.store.active_model(lane=self.lane)
        label_health = await self.store.label_health()
        source_kind = getattr(self.observer, "source_kind", "") if self.observer else ""
        authorised = source_kind in AUTHORISED_SOURCE_KINDS
        return {
            # --- what is actually switched on, read from config, not asserted --
            "enabled": self.config.enabled,
            "collection_enabled": self.config.collection_enabled,
            "inference_enabled": self.config.inference_enabled,
            "alerting_enabled": self.config.alerting_enabled,
            "may_send": self.may_send,
            "feature_version": FEATURE_VERSION,
            # --- is the collector actually collecting? -------------------------
            "cycles": self.cycles,
            "last_cycle_at": self.last_cycle_at,
            "board": (
                self.observer.health()
                if self.observer is not None
                else {"configured": False}
            ),
            # --- can this source establish labels at all? ----------------------
            "label_source": {
                "source_kind": source_kind or "UNKNOWN",
                "authorised_for_fomo_labels": authorised,
                "detail": (
                    "Authorised FOMO Trending feed; board entries establish labels."
                    if authorised
                    else (
                        "This source is a PROXY approximation, not the FOMO "
                        "Trending board. Its rows are collected and stored, but "
                        "they cannot establish FOMO labels, confirmations or "
                        "training targets. Set FOMO_TRENDING_API_URL to an "
                        "authorised feed to produce usable labels."
                    )
                ),
            },
            "ground_truth": self.ground_truth.health(now=moment),
            "labels": label_health,
            "snapshot_health": await self.store.snapshot_health(since=moment - 86_400),
            "activity_lane": {
                "configured": activity_available,
                "provider": getattr(activity, "name", "none"),
                "last_error": getattr(activity, "last_error", ""),
                "cursor": self._activity_cursor,
            },
            "affinity_actors": len(self._affinities),
            "affinity_computed_at": self._affinity_computed_at,
            "gate": self.gate.stats(now=moment),
            "suppressed": {
                "confirmations": self.suppressed_confirmations,
                "signals": self.suppressed_signals,
                "undelivered_confirmations": self.undelivered_confirmations,
            },
            "alert_rate_24h": await self.store.alert_rate(since=moment - 86_400),
            "model": (
                None
                if model is None
                else {
                    "model_id": model["model_id"],
                    "name": model["name"],
                    "trained_at": model["trained_at"],
                    "training_cutoff_at": model["training_cutoff_at"],
                    "trained_rows": model["trained_rows"],
                    "trained_positives": model["trained_positives"],
                    "threshold": model["threshold"],
                    "feature_version": model["feature_version"],
                }
            ),
            "metrics": await self.store.prediction_metrics(
                since=moment - 7 * 86_400, lane=self.lane
            ),
        }

    def notable_actors(self, *, limit: int = 20) -> tuple[AffinityRecord, ...]:
        return rank_actors(
            self._affinities, horizon_seconds=self.config.horizon_seconds, limit=limit
        )

    def actor_weight(self, actor_id: str) -> Decimal:
        return quality_weight(
            self._affinities.get(actor_id), horizon_seconds=self.config.horizon_seconds
        )
