"""The Early Traction runtime: push in, cheap filter, alert, then enrich.

The shape of this lane is dictated by one requirement — the alert must not wait
for anything it does not strictly need — so the pipeline is ordered by cost:

1. **Push intake.**  A new mint arrives from the creation stream and is stamped
   immediately.  That stamp is the number every latency figure is measured
   against, so nothing may happen before it.

2. **The young pool.**  A brand-new token has no traction yet: it cannot meet a
   $8,000 market cap or $5,000 volume floor in its first second.  So mints are
   held in a bounded in-memory pool and re-checked as they develop, which is what
   makes this a *screen* rather than a snapshot of the instant of creation.
   Tokens are evicted the moment a failure becomes permanent (wrong launchpad,
   too old) rather than being re-checked pointlessly.

3. **Cheap evaluation only.**  Pool re-checks use batched market reads and
   in-memory profile logic.  No safety provider, no holder read, no X API.

4. **Alert immediately.**  The card is sent the moment the profile matches, with
   the safety block rendered as explicitly pending.

5. **Enrich, then EDIT the same message.**  Safety data arrives late and edits the
   card already on screen.  It never sends a second message, because a second
   message for one token is the noise this lane is supposed to avoid.

Two failure modes get explicit handling because both are silent otherwise.
A **Discord rate limit** retries with bounded exponential backoff and only gives
up after releasing its dedupe claim, so a 429 costs a delay rather than the
alert.  And an **enrichment failure** leaves the card in place with safety marked
unavailable, rather than holding the alert hostage to it.

This lane is read-only. It has no reference to the executor, no signer, and no
path to spend SOL.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from .traction.candidate import (
    TractionCandidate,
    build_momentum,
    build_quality,
)
from .traction.latency import LatencyReport, build_report
from .traction.launchpads import (
    MIGRATION_UNKNOWN,
    LaunchpadRegistry,
    build_registry,
)
from .traction.profile import (
    DEFAULT_PROFILE,
    ProfileVerdict,
    TractionObservation,
    TractionProfile,
    evaluate,
)
from .traction.safety import SafetyReport, build_safety
from .traction.xlink import XLinkVerdict, assess_link
from .traction_store import TractionStore

logger = logging.getLogger(__name__)

ZERO = Decimal("0")

#: Reads a batch of mints and returns whatever market data it could get.
#: Injected so the runtime never names a provider and stays testable offline.
MarketReader = Callable[[Sequence[str]], Awaitable[Mapping[str, Mapping[str, Any]]]]
#: Returns the safety block for one mint.  Called only for qualifying mints.
Enricher = Callable[[TractionCandidate], Awaitable[SafetyReport]]
#: Publishes the card.  Returns the Discord coordinates, or None if not sent.
Publisher = Callable[[TractionCandidate], Awaitable[tuple[int, int] | None]]
#: Edits the already-sent card with the safety block.
Editor = Callable[[TractionCandidate], Awaitable[bool]]
#: Hands one alerted mint to the shared forward-observation history.
#: Injected rather than done here because the write belongs to the runner lane's
#: own writer -- this lane contributes a candidate, it does not author that
#: lane's typed payload.
ForwardTracker = Callable[[TractionCandidate], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class TractionConfig:
    """Everything tunable, with the operator's Axiom defaults."""

    enabled: bool = True
    profile: TractionProfile = field(default_factory=lambda: DEFAULT_PROFILE)
    #: How often the young pool sweep wakes up.
    poll_seconds: int = 5
    #: Floor on how often ONE mint is re-read from the market provider.
    #:
    #: Matched to ``DexScreenerClient.snapshot``'s own 20-second response cache:
    #: below this the client returns the identical bytes it returned last time, so
    #: a faster sweep buys no freshness at all and only costs request budget. It
    #: also stops the momentum block filling up with duplicate readings, which
    #: would make a flat token look like it had been measured repeatedly.
    recheck_seconds: int = 20
    #: Hard ceiling on provider reads per minute across the whole lane.
    #:
    #: The operator's instruction was explicit: no discovery loop may burn credits
    #: unattended. Without this, a launch storm filling the pool to ``max_pool``
    #: would issue one request per mint per ``recheck_seconds`` -- 1,800 a minute
    #: at 600 mints -- against a public endpoint documented at 300. The budget
    #: clamps the sweep instead, and ``reads_deferred_for_budget`` says it did.
    max_reads_per_minute: int = 240
    #: Bound on the pool, so a launch storm cannot grow it without limit.
    max_pool: int = 600
    #: Mints per batched market read.  30 is DEX Screener's documented limit.
    batch_size: int = 30
    #: Batches per pool pass.  Bounds the cost of one sweep.
    max_batches_per_pass: int = 6
    #: Alerts per hour across the whole lane.
    max_alerts_per_hour: int = 30
    #: Delivery retries before the claim is released.
    max_send_attempts: int = 5
    #: First backoff step; doubles each attempt.
    send_backoff_seconds: float = 1.0
    #: How far back the X-reuse query looks.
    x_reuse_window_seconds: int = 7 * 86_400
    #: Enrichment budget per candidate.  Expiring leaves safety unavailable,
    #: never blocks the card that was already sent.
    enrich_timeout_seconds: int = 20


@dataclass
class _PoolEntry:
    """One young mint being watched until it qualifies or ages out."""

    observation: TractionObservation
    detected_at: int
    #: ``(at, market_cap, volume)`` readings this lane took itself.
    readings: list[tuple[int, Decimal | None, Decimal | None]] = field(
        default_factory=list
    )
    checks: int = 0
    last_checked_at: int = 0


@dataclass(frozen=True, slots=True)
class PassResult:
    """What one pool pass did."""

    checked: int = 0
    qualified: tuple[str, ...] = ()
    alerted: tuple[str, ...] = ()
    evicted: tuple[str, ...] = ()
    pool_size: int = 0
    batches: int = 0
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "qualified": list(self.qualified),
            "alerted": list(self.alerted),
            "evicted": list(self.evicted),
            "pool_size": self.pool_size,
            "batches": self.batches,
            "error": self.error,
        }


class TractionRuntime:
    """Drives intake, the young pool, alerting and late enrichment."""

    def __init__(
        self,
        store: TractionStore,
        *,
        registry: LaunchpadRegistry | None = None,
        config: TractionConfig | None = None,
        market_reader: MarketReader | None = None,
        enricher: Enricher | None = None,
        publisher: Publisher | None = None,
        editor: Editor | None = None,
        forward_tracker: ForwardTracker | None = None,
    ) -> None:
        self.store = store
        self.registry = registry or build_registry()
        self.config = config or TractionConfig()
        self._market_reader = market_reader
        self._enricher = enricher
        self._publisher = publisher
        self._editor = editor
        self._forward_tracker = forward_tracker

        self._pool: dict[str, _PoolEntry] = {}
        self._alerted: set[str] = set()
        self._alert_times: list[int] = []
        self._enrich_tasks: set[asyncio.Task[None]] = set()
        self._restored = False

        self.intake_count = 0
        self.dropped_unknown_launchpad = 0
        self.alerts_sent = 0
        self.alerts_failed = 0
        self.enrichments_ok = 0
        self.enrichments_failed = 0
        self.forward_registered = 0
        self.forward_failed = 0
        self.reads_spent = 0
        self.reads_deferred_for_budget = 0
        #: Read timestamps inside the trailing minute, for the request budget.
        self._read_times: list[int] = []
        self.rate_limited = 0
        #: Wall-clock seconds the last delivery took, retries included.
        self.last_delivery_seconds: float | None = None
        self.last_pass_at: int | None = None
        self.last_error = ""

    # ------------------------------------------------------------------
    async def restore(self, *, now: int | None = None) -> None:
        """Rebuild the dedupe guard from the database.

        The guard is a cache of the database, never the authority: the claim
        itself is an atomic ``INSERT OR IGNORE`` in the store, so a cold cache
        after a restart costs one redundant query rather than a duplicate alert.
        """

        if self._restored:
            return
        moment = now if now is not None else int(time.time())
        self._alerted = set(await self.store.alerted_mints(since=moment - 7 * 86_400))
        self._alert_times = []
        self._restored = True

    # ------------------------------------------------------------------
    async def observe_creation(
        self,
        *,
        mint: str,
        launchpad: str,
        chain_created_at: int | None,
        now: int | None = None,
        name: str = "",
        symbol: str = "",
        x_link: str = "",
        migration_state: str = MIGRATION_UNKNOWN,
        source: str = "creation_stream",
    ) -> bool:
        """Intake from the push stream.  Stamps immediately, filters cheaply.

        Returns whether the mint entered the pool.  A mint from a launchpad this
        lane is not listening to is dropped here, before any storage or market
        read, because it can never qualify however it develops.
        """

        if not self.config.enabled or not mint:
            return False
        moment = now if now is not None else int(time.time())
        await self.restore(now=moment)
        self.intake_count += 1

        if not self.registry.accepts(launchpad):
            self.dropped_unknown_launchpad += 1
            return False
        if mint in self._alerted or mint in self._pool:
            return False

        # Stamp before anything else. This timestamp anchors every latency
        # figure, so enrichment, filtering or a slow query must not precede it.
        await self.store.record_detection(
            mint=mint,
            chain_created_at=chain_created_at,
            detected_at=moment,
            source=source,
            launchpad=launchpad,
        )

        observation = TractionObservation(
            mint=mint,
            launchpad=launchpad,
            chain_created_at=chain_created_at,
            migration_state=migration_state,
            x_link=x_link,
            name=name,
            symbol=symbol,
            first_seen_at=moment,
        )
        verdict = evaluate(
            observation, registry=self.registry, profile=self.config.profile, now=moment
        )
        if verdict.terminal:
            await self.store.record_rejection(verdict, at=moment)
            return False

        if len(self._pool) >= self.config.max_pool:
            # Evict the oldest first: it is closest to ageing out anyway, and an
            # unbounded pool under a launch storm is how a fast lane becomes a
            # slow one.
            oldest = min(self._pool.values(), key=lambda entry: entry.detected_at)
            self._pool.pop(oldest.observation.mint, None)

        self._pool[mint] = _PoolEntry(observation=observation, detected_at=moment)
        return True

    # ------------------------------------------------------------------
    def _alerts_last_hour(self, *, now: int) -> int:
        self._alert_times = [at for at in self._alert_times if now - at < 3600]
        return len(self._alert_times)

    def _read_budget(self, *, now: int) -> int:
        """Provider reads still allowed in the trailing minute.

        A plain sliding window rather than a token bucket, because the limit we
        are respecting is itself stated per minute and a window is the thing an
        operator can check against the provider's own dashboard.
        """

        self._read_times = [at for at in self._read_times if now - at < 60]
        return max(0, self.config.max_reads_per_minute - len(self._read_times))

    async def run_pass(self, *, now: int | None = None) -> PassResult:
        """One cheap sweep of the young pool."""

        if not self.config.enabled:
            return PassResult(error="Early Traction lane disabled by configuration")
        moment = now if now is not None else int(time.time())
        await self.restore(now=moment)
        self.last_pass_at = moment

        # Evict what can never qualify before spending a single read on it.
        evicted: list[str] = []
        for mint, entry in list(self._pool.items()):
            age = entry.observation.age_seconds(now=moment)
            if age is not None and age > self.config.profile.max_age_seconds:
                self._pool.pop(mint, None)
                evicted.append(mint)
        due = [
            entry
            for entry in self._pool.values()
            if moment - entry.last_checked_at >= self.config.recheck_seconds
        ]
        # Oldest first: within the age ceiling an older token has had longer to
        # accumulate the market cap and volume the profile requires, so it is the
        # one most likely to qualify on this pass -- and ordering by detection
        # time also means nothing in the pool can be starved by newer arrivals.
        due.sort(key=lambda entry: entry.detected_at)

        # Spend no more than the minute's remaining request budget. The overflow
        # is not dropped, it simply waits for the next sweep, which is why the
        # ordering above matters.
        budget = self._read_budget(now=moment)
        if len(due) > budget:
            self.reads_deferred_for_budget += len(due) - budget
            due = due[:budget]

        mints = [entry.observation.mint for entry in due]
        batches = 0
        market: dict[str, Mapping[str, Any]] = {}
        if mints and self._market_reader is not None:
            for start in range(0, len(mints), self.config.batch_size):
                if batches >= self.config.max_batches_per_pass:
                    break
                batch = mints[start : start + self.config.batch_size]
                batches += 1
                self.reads_spent += len(batch)
                self._read_times.extend(moment for _ in batch)
                try:
                    market.update(await self._market_reader(batch))
                except Exception as exc:  # the sweep must survive a bad read
                    self.last_error = str(exc)[:200]
                    logger.debug("Traction market read failed: %s", self.last_error)

        qualified: list[str] = []
        alerted: list[str] = []
        checked = 0

        for entry in due:
            mint = entry.observation.mint
            checked += 1
            entry.checks += 1
            entry.last_checked_at = moment

            row = market.get(mint) or {}
            observation = _merge_observation(entry.observation, row)
            entry.observation = observation
            entry.readings.append(
                (moment, observation.market_cap_usd, observation.volume_usd)
            )
            # Keep the reading history short: momentum only needs the recent
            # shape, and an unbounded list per mint is a slow leak.
            if len(entry.readings) > 12:
                entry.readings = entry.readings[-12:]

            verdict = evaluate(
                observation,
                registry=self.registry,
                profile=self.config.profile,
                now=moment,
            )
            if verdict.terminal:
                self._pool.pop(mint, None)
                evicted.append(mint)
                await self.store.record_rejection(verdict, at=moment)
                continue
            if not verdict.qualifies:
                continue

            qualified.append(mint)
            if self._alerts_last_hour(now=moment) >= self.config.max_alerts_per_hour:
                self.rate_limited += 1
                continue

            candidate = TractionCandidate(
                observation=observation,
                verdict=verdict,
                momentum=build_momentum(
                    entry.readings,
                    volume_window_seconds=_window_seconds(
                        self.config.profile.volume_window
                    ),
                ),
                quality=build_quality(observation),
                safety=build_safety(mint),  # pending by construction
                x_link=await self._assess_x_link(mint, observation.x_link, now=moment),
                detected_at=entry.detected_at,
                qualified_at=moment,
            )
            if await self._alert(candidate, now=moment):
                alerted.append(mint)
                self._pool.pop(mint, None)

        return PassResult(
            checked=checked,
            qualified=tuple(qualified),
            alerted=tuple(alerted),
            evicted=tuple(evicted),
            pool_size=len(self._pool),
            batches=batches,
        )

    # ------------------------------------------------------------------
    async def _assess_x_link(
        self, mint: str, raw: str, *, now: int
    ) -> XLinkVerdict:
        """Structural assessment plus reuse.  No network call on this path."""

        assessment = assess_link(raw)
        if assessment.handle or assessment.tweet_id:
            await self.store.note_x_link(mint, assessment, at=now)
        reuse = await self.store.x_link_reuse(
            mint,
            assessment,
            window_seconds=self.config.x_reuse_window_seconds,
            now=now,
        )
        return XLinkVerdict(assessment=assessment, reuse=reuse)

    async def _alert(self, candidate: TractionCandidate, *, now: int) -> bool:
        """Claim, send with backoff, then kick off enrichment.

        The claim comes first and is atomic, so two concurrent passes cannot both
        send. If delivery then fails permanently the claim is released, because a
        held claim on an undelivered card is permanent silent loss.
        """

        if not await self.store.claim_alert(candidate, now=now):
            return False  # already alerted, by this process or a previous one
        self._alerted.add(candidate.mint)

        delivery_started = time.monotonic()
        sent = await self._deliver(candidate)
        self.last_delivery_seconds = round(time.monotonic() - delivery_started, 3)
        if sent is None:
            self.alerts_failed += 1
            # Give the claim back so the mint can be alerted on a later pass.
            await self.store.release_alert(candidate.mint)
            self._alerted.discard(candidate.mint)
            return False

        channel_id, message_id = sent
        # The alert timestamp must come from the SAME clock as the detection
        # timestamp. Reading time.time() here instead would mix a caller-supplied
        # clock with the wall clock and produce latency figures that are
        # arithmetically meaningless -- which is exactly what the end-to-end test
        # caught before this line was fixed.
        alerted_at = now
        candidate = replace(candidate, alert_sent_at=alerted_at)
        self.alerts_sent += 1
        self._alert_times.append(now)
        await self.store.record_alert_sent(candidate.mint, at=alerted_at)
        await self.store.record_discord_message(
            candidate.mint, channel_id=channel_id, message_id=message_id
        )
        # Forward tracking into the existing v2.34 tables, so outcomes accrue at
        # +5m/+15m/+1h/+24h alongside every other lane's history.  It happens
        # after the send, and a failure here must not unsend a delivered card --
        # the unregistered mint is retried from the database instead.
        await self._register_forward(candidate, now=alerted_at)

        # Enrichment runs detached: the card is already on screen and must not
        # wait for it.
        task = asyncio.create_task(self._enrich(candidate))
        self._enrich_tasks.add(task)
        task.add_done_callback(self._enrich_tasks.discard)
        return True

    async def _register_forward(
        self, candidate: TractionCandidate, *, now: int
    ) -> bool:
        """Contribute this mint to the shared forward-observation history.

        Never raises into the alert path.  The card is already on screen, and a
        provider hiccup here is a missing forward row to retry -- not a reason to
        report the alert as failed and re-send it later.
        """

        if self._forward_tracker is None:
            # No tracker wired (tests, or a deployment with the runner lane off).
            # Still record the intent, so the retry list does not grow forever.
            await self.store.mark_forward_registered(candidate.mint, at=now)
            return True
        try:
            registered = await self._forward_tracker(candidate)
        except Exception as exc:
            self.forward_failed += 1
            self.last_error = str(exc)[:200]
            logger.debug("Traction forward registration failed: %s", self.last_error)
            return False
        if not registered:
            self.forward_failed += 1
            return False
        await self.store.mark_forward_registered(candidate.mint, at=now)
        self.forward_registered += 1
        return True

    async def retry_forward_registration(self, *, limit: int = 25) -> int:
        """Re-register alerted mints that never reached the shared history.

        Registration happens after the send, so a redeploy in between leaves a
        gap.  Without this, the forward record the operator wants to tune
        thresholds against would silently be missing exactly the alerts that
        landed around a restart.
        """

        if self._forward_tracker is None:
            return 0
        recovered = 0
        for mint in await self.store.pending_forward_registration(limit=limit):
            row = await self.store.alert_row(mint)
            if row is None:
                continue
            candidate = _candidate_from_row(row)
            if candidate is None:
                continue
            if await self._register_forward(candidate, now=int(time.time())):
                recovered += 1
        return recovered

    async def _deliver(
        self, candidate: TractionCandidate
    ) -> tuple[int, int] | None:
        """Send with bounded exponential backoff.

        A rate limit is a delay, not a dropped alert, which is why this retries
        rather than returning on the first refusal. It gives up eventually so a
        permanently broken channel cannot block the pool forever.
        """

        if self._publisher is None:
            return (0, 0)  # no surface wired; treat as delivered for tests

        delay = self.config.send_backoff_seconds
        for attempt in range(1, self.config.max_send_attempts + 1):
            try:
                result = await self._publisher(candidate)
            except Exception as exc:
                self.last_error = str(exc)[:200]
                logger.debug(
                    "Traction send attempt %s/%s failed: %s",
                    attempt,
                    self.config.max_send_attempts,
                    self.last_error,
                )
                result = None
            if result is not None:
                return result
            if attempt < self.config.max_send_attempts:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
        return None

    async def _enrich(self, candidate: TractionCandidate) -> None:
        """Fetch safety, then EDIT the card that was already sent."""

        if self._enricher is None:
            return
        try:
            async with asyncio.timeout(self.config.enrich_timeout_seconds):
                safety = await self._enricher(candidate)
        except TimeoutError:
            self.enrichments_failed += 1
            safety = build_safety(
                candidate.mint,
                notes=("safety enrichment timed out; these values were not measured",),
                enriched_at=int(time.time()),
            )
        except Exception as exc:
            self.enrichments_failed += 1
            self.last_error = str(exc)[:200]
            safety = build_safety(
                candidate.mint,
                notes=(f"safety enrichment failed: {self.last_error}",),
                enriched_at=int(time.time()),
            )
        else:
            self.enrichments_ok += 1

        enriched = replace(candidate, safety=safety)
        await self.store.mark_enriched(
            candidate.mint, at=safety.enriched_at, payload=enriched.to_json()
        )
        if self._editor is not None:
            try:
                await self._editor(enriched)
            except Exception as exc:
                logger.debug("Traction card edit failed: %s", str(exc)[:200])

    # ------------------------------------------------------------------
    async def latency_report(self, *, since: int = 0) -> LatencyReport:
        return build_report(await self.store.latency_samples(since=since))

    async def status(self, *, now: int | None = None) -> dict[str, Any]:
        """The lane's honest state, including what it is not listening to."""

        moment = now if now is not None else int(time.time())
        report = await self.latency_report(since=moment - 86_400)
        return {
            "enabled": self.config.enabled,
            "profile": self.config.profile.to_json(),
            "launchpads": self.registry.status(),
            "pool_size": len(self._pool),
            "reads_spent": self.reads_spent,
            "reads_deferred_for_budget": self.reads_deferred_for_budget,
            "read_budget_remaining": self._read_budget(now=moment),
            "max_reads_per_minute": self.config.max_reads_per_minute,
            "recheck_seconds": self.config.recheck_seconds,
            "intake": self.intake_count,
            "dropped_unknown_launchpad": self.dropped_unknown_launchpad,
            "alerts_sent": self.alerts_sent,
            "alerts_failed": self.alerts_failed,
            "alerts_last_hour": self._alerts_last_hour(now=moment),
            "max_alerts_per_hour": self.config.max_alerts_per_hour,
            "rate_limited": self.rate_limited,
            "last_delivery_seconds": self.last_delivery_seconds,
            "enrichments_ok": self.enrichments_ok,
            "enrichments_failed": self.enrichments_failed,
            "forward_registered": self.forward_registered,
            "forward_failed": self.forward_failed,
            "forward_pending": len(await self.store.pending_forward_registration()),
            "last_pass_at": self.last_pass_at,
            "last_error": self.last_error,
            "latency": report.to_json(),
            "store": await self.store.stats(since=moment - 86_400),
            # Stated in the payload because it is the lane's defining property.
            "read_only": True,
        }


def _candidate_from_row(row: Mapping[str, Any]) -> TractionCandidate | None:
    """Rebuild just enough of a candidate to register it for forward tracking.

    Deliberately reads the flat columns rather than un-serialising
    ``payload_json``: forward tracking needs the entry baseline (mint, the market
    values at the alert, and the two timestamps the horizons are measured from)
    and nothing else, and a partial rebuild that pretended to be the original
    card would be a worse thing to hand anybody.
    """

    mint = str(row.get("mint") or "")
    if not mint:
        return None
    observation = TractionObservation(
        mint=mint,
        launchpad=str(row.get("launchpad") or ""),
        chain_created_at=_int_or_none(row.get("chain_created_at")),
        market_cap_usd=_decimal(row.get("market_cap_at_alert_usd")),
        volume_usd=_decimal(row.get("volume_at_alert_usd")),
        liquidity_usd=_decimal(row.get("liquidity_at_alert_usd")),
        migration_state=str(row.get("migration_state") or MIGRATION_UNKNOWN),
        x_link=str(row.get("x_link") or ""),
        name=str(row.get("name") or ""),
        symbol=str(row.get("symbol") or ""),
    )
    alerted_at = _int_or_none(row.get("alerted_at")) or 0
    detected_at = _int_or_none(row.get("detected_at")) or alerted_at
    return TractionCandidate(
        observation=observation,
        verdict=ProfileVerdict(mint=mint, qualifies=True),
        detected_at=detected_at,
        qualified_at=alerted_at,
        alert_sent_at=alerted_at,
    )


def _window_seconds(label: str) -> int:
    """Map a volume-window label to seconds.  Unknown labels fall back to 5m."""

    mapping = {"5m": 300, "1h": 3_600, "6h": 21_600, "24h": 86_400, "1m": 60}
    return mapping.get(label.strip().lower(), 300)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() else None


def _merge_observation(
    observation: TractionObservation, row: Mapping[str, Any]
) -> TractionObservation:
    """Fold a market reading into an observation without losing known facts.

    A field the reader omitted keeps its previous value rather than being reset
    to ``None``: a provider that drops ``volume`` on one poll should not make a
    token that already met the volume floor look like it never did.
    """

    if not row:
        return observation
    return replace(
        observation,
        market_cap_usd=_decimal(row.get("market_cap_usd")) or observation.market_cap_usd,
        volume_usd=_decimal(row.get("volume_usd")) or observation.volume_usd,
        liquidity_usd=_decimal(row.get("liquidity_usd")) or observation.liquidity_usd,
        price_usd=_decimal(row.get("price_usd")) or observation.price_usd,
        name=str(row.get("name") or "") or observation.name,
        symbol=str(row.get("symbol") or "") or observation.symbol,
        x_link=str(row.get("x_link") or "") or observation.x_link,
        dex_paid=(
            row.get("dex_paid") if row.get("dex_paid") is not None else observation.dex_paid
        ),
        migration_state=str(row.get("migration_state") or "")
        or observation.migration_state,
        chain_created_at=(
            observation.chain_created_at
            if observation.chain_created_at is not None
            else _int_or_none(row.get("chain_created_at"))
        ),
    )


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
