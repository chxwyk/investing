"""Reconstructing what was knowable before a token entered the board.

This is the module that answers the operator's real question — *who was early,
how early, at what market cap, and could we have seen it?* — and it is the
module most at risk of quietly cheating, because it runs after the outcome is
known.  Two structural choices prevent that.

**It reconstructs states, not conclusions.**  Every offset snapshot is built by
calling :func:`~smart_money_bot.pretrend.features.build_features` on a state
truncated at ``entry_at - offset``.  It is the same function live inference
calls.  There is no forensics-only feature, so there is nothing that could look
good here and be unavailable in production.

**Nothing may read past its own offset.**  The truncation is applied to the
inputs, not to the output, so a feature physically cannot see the entry it is
being measured against.  The ``T-5m`` row is exactly what the live system would
have computed five minutes before the entry, given the same collected data.

What the reconstruction *cannot* do is invent history.  If the collector was not
running, or the FOMO tape was unavailable, the offset rows will be sparse and
say so via ``completeness``.  A forensic timeline built from three data points is
reported as a forensic timeline built from three data points.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from .activity import ActivityEvent, ActivityTape
from .affinity import AffinityRecord
from .cascade import CascadeProfile
from .features import FeatureVector, MarketSeries, PretrendState, build_features
from .groundtruth import TrendEntryEvent
from .windows import Series

ZERO = Decimal("0")

#: The offsets before the board entry that the forensic timeline reports.
FORENSIC_OFFSETS_SECONDS: tuple[int, ...] = (1_200, 600, 300, 180, 120, 60, 30, 0)

OFFSET_LABELS: dict[int, str] = {
    1_200: "T-20m",
    600: "T-10m",
    300: "T-5m",
    180: "T-3m",
    120: "T-2m",
    60: "T-1m",
    30: "T-30s",
    0: "T0",
}


@dataclass(frozen=True, slots=True)
class ForensicSnapshot:
    """The reconstructed state at one offset before the board entry."""

    offset_seconds: int
    at: int
    vector: FeatureVector

    @property
    def label(self) -> str:
        return OFFSET_LABELS.get(self.offset_seconds, f"T-{self.offset_seconds}s")

    def value(self, name: str) -> Decimal | None:
        return self.vector.get(name)

    def to_json(self) -> dict[str, Any]:
        def s(name: str) -> str | None:
            value = self.vector.get(name)
            return None if value is None else str(value)

        return {
            "offset_seconds": self.offset_seconds,
            "label": self.label,
            "at": self.at,
            "completeness": str(self.vector.completeness),
            "fomo_buyers_1m": s("unique_fomo_buyers_1m"),
            "quality_fomo_buyers_1m": s("quality_fomo_buyers_1m"),
            "independent_quality_buyers": s("independent_quality_buyers"),
            "fomo_buy_usd_1m": s("fomo_buy_usd_1m"),
            "fomo_net_buy_usd_3m": s("fomo_net_buy_usd_3m"),
            "theses_3m": s("fomo_thesis_count_3m"),
            "onchain_unique_buyers": s("unique_buyers_level_1m"),
            "holders": s("holders_level_1m"),
            "volume_usd_5m": s("volume_usd_level_5m"),
            "market_cap_usd": s("market_cap_usd"),
            "liquidity_usd": s("liquidity_usd"),
            "social_engagement": s("social_engagement_level_5m"),
        }


@dataclass(frozen=True, slots=True)
class EarlyActor:
    """One account that acted on the mint before it entered the board."""

    actor_id: str
    handle: str
    acted_at: int
    event_type: str
    market_cap_at_action_usd: Decimal | None
    price_at_action_usd: Decimal | None
    lead_seconds: int
    #: The actor's affinity record as measured on data BEFORE this token's
    #: entry.  Using their full lifetime record would include this very token.
    affinity: AffinityRecord | None = None

    @property
    def affinity_observations(self) -> int:
        return 0 if self.affinity is None else self.affinity.observations

    def to_json(self) -> dict[str, Any]:
        primary = None if self.affinity is None else self.affinity.primary(300)
        return {
            "actor_id": self.actor_id,
            "handle": self.handle,
            "acted_at": self.acted_at,
            "event_type": self.event_type,
            "market_cap_at_action_usd": (
                None
                if self.market_cap_at_action_usd is None
                else str(self.market_cap_at_action_usd)
            ),
            "lead_seconds": self.lead_seconds,
            "affinity_observations": self.affinity_observations,
            "affinity_adjusted_rate_5m": (
                None
                if primary is None or primary.adjusted_rate is None
                else str(primary.adjusted_rate)
            ),
            "affinity_lift_5m": (
                None if primary is None or primary.lift is None else str(primary.lift)
            ),
            "statistically_meaningful": (
                False if self.affinity is None else self.affinity.statistically_meaningful
            ),
        }


@dataclass(frozen=True, slots=True)
class ForensicChange:
    """One metric's movement across the pre-entry window."""

    metric: str
    early_value: Decimal | None
    late_value: Decimal | None

    @property
    def ratio(self) -> Decimal | None:
        if (
            self.early_value is None
            or self.late_value is None
            or self.early_value <= ZERO
        ):
            return None
        return (self.late_value / self.early_value).quantize(Decimal("0.01"))

    @property
    def delta(self) -> Decimal | None:
        if self.early_value is None or self.late_value is None:
            return None
        return self.late_value - self.early_value

    def to_json(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "early_value": None if self.early_value is None else str(self.early_value),
            "late_value": None if self.late_value is None else str(self.late_value),
            "ratio": None if self.ratio is None else str(self.ratio),
            "delta": None if self.delta is None else str(self.delta),
        }


@dataclass(frozen=True, slots=True)
class ForensicReport:
    """The full ``/trendforensics`` answer for one exact mint."""

    mint: str
    entry: TrendEntryEvent | None
    snapshots: tuple[ForensicSnapshot, ...] = ()
    early_actors: tuple[EarlyActor, ...] = ()
    changes: tuple[ForensicChange, ...] = ()
    cascade: CascadeProfile | None = None
    #: Our own prediction record, if we had one.
    predicted: bool | None = None
    first_alert_at: int | None = None
    first_alert_probability: Decimal | None = None
    first_alert_market_cap_usd: Decimal | None = None
    lead_seconds: int | None = None
    #: Why the reconstruction is incomplete, when it is.
    limitations: tuple[str, ...] = ()

    @property
    def data_available(self) -> bool:
        return bool(self.snapshots)

    def to_json(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "entry": None if self.entry is None else self.entry.to_json(),
            "snapshots": [snapshot.to_json() for snapshot in self.snapshots],
            "early_actors": [actor.to_json() for actor in self.early_actors],
            "changes": [change.to_json() for change in self.changes],
            "cascade": None if self.cascade is None else self.cascade.to_json(),
            "predicted": self.predicted,
            "first_alert_at": self.first_alert_at,
            "first_alert_probability": (
                None
                if self.first_alert_probability is None
                else str(self.first_alert_probability)
            ),
            "first_alert_market_cap_usd": (
                None
                if self.first_alert_market_cap_usd is None
                else str(self.first_alert_market_cap_usd)
            ),
            "lead_seconds": self.lead_seconds,
            "limitations": list(self.limitations),
            "data_available": self.data_available,
        }


def _truncate_series(series: Series, *, at: int) -> Series:
    """A copy containing only samples at or before ``at``."""

    return Series(series.name, series.before(at))


def truncate_state(state: PretrendState, *, at: int) -> PretrendState:
    """A state containing only what was known at ``at``.

    Every container is rebuilt rather than filtered in place, so the caller's
    state is not mutated and a later offset cannot be contaminated by an earlier
    truncation.
    """

    market = MarketSeries(
        price_usd=_truncate_series(state.market.price_usd, at=at),
        market_cap_usd=_truncate_series(state.market.market_cap_usd, at=at),
        liquidity_usd=_truncate_series(state.market.liquidity_usd, at=at),
        volume_usd=_truncate_series(state.market.volume_usd, at=at),
        buys=_truncate_series(state.market.buys, at=at),
        sells=_truncate_series(state.market.sells, at=at),
        unique_buyers=_truncate_series(state.market.unique_buyers, at=at),
        holders=_truncate_series(state.market.holders, at=at),
        net_flow_usd=_truncate_series(state.market.net_flow_usd, at=at),
        social_engagement=_truncate_series(state.market.social_engagement, at=at),
    )

    tape: ActivityTape | None = None
    if state.tape is not None:
        tape = ActivityTape(state.tape.mint)
        tape.extend(state.tape.before(at))

    return replace(
        state,
        at=at,
        market=market,
        tape=tape,
        arrivals=tuple(arrival for arrival in state.arrivals if arrival.at <= at),
        populations=tuple(
            snapshot for snapshot in state.populations if snapshot.at <= at
        ),
        first_seen_by_source={
            source: seen
            for source, seen in state.first_seen_by_source.items()
            if seen <= at
        },
        source_timestamps={
            name: value
            for name, value in state.source_timestamps.items()
            if value is None or value <= at
        },
    )


def reconstruct_timeline(
    state: PretrendState,
    *,
    entry_at: int,
    offsets: Sequence[int] = FORENSIC_OFFSETS_SECONDS,
) -> tuple[ForensicSnapshot, ...]:
    """Build the offset ladder by truncating and running the live feature code."""

    snapshots: list[ForensicSnapshot] = []
    for offset in offsets:
        moment = entry_at - offset
        truncated = truncate_state(state, at=moment)
        snapshots.append(
            ForensicSnapshot(
                offset_seconds=offset,
                at=moment,
                vector=build_features(truncated),
            )
        )
    return tuple(snapshots)


def earliest_actors(
    tape: ActivityTape | None,
    *,
    entry_at: int,
    affinities: Mapping[str, AffinityRecord] | None = None,
    lookback_seconds: int = 1_200,
    limit: int = 14,
) -> tuple[EarlyActor, ...]:
    """Who acted first, and what their measured record was at the time.

    Only the *first* action per actor is reported: a single enthusiastic account
    buying eight times is one early actor, and listing it eight times would make
    a crowd out of a person.
    """

    if tape is None:
        return ()
    window_start = entry_at - lookback_seconds
    seen: dict[str, ActivityEvent] = {}
    for event in tape.before(entry_at):
        if event.occurred_at < window_start or not event.trader_id:
            continue
        existing = seen.get(event.trader_id)
        if existing is None or event.occurred_at < existing.occurred_at:
            seen[event.trader_id] = event

    actors = [
        EarlyActor(
            actor_id=event.trader_id,
            handle=event.handle,
            acted_at=event.occurred_at,
            event_type=event.event_type,
            market_cap_at_action_usd=event.market_cap_usd,
            price_at_action_usd=event.price_usd,
            lead_seconds=entry_at - event.occurred_at,
            affinity=(affinities or {}).get(event.trader_id),
        )
        for event in seen.values()
    ]
    actors.sort(key=lambda actor: (actor.acted_at, actor.actor_id))
    return tuple(actors[:limit])


#: The metrics the "what changed before Trending?" section reports on.
CHANGE_METRICS: tuple[str, ...] = (
    "unique_fomo_buyers_1m",
    "quality_fomo_buyers_1m",
    "independent_quality_buyers",
    "fomo_buy_usd_1m",
    "fomo_thesis_count_3m",
    "unique_buyers_level_1m",
    "holders_level_1m",
    "volume_usd_level_5m",
    "market_cap_usd",
    "liquidity_usd",
    "social_engagement_level_5m",
)


def summarise_changes(
    snapshots: Sequence[ForensicSnapshot],
    *,
    early_offset: int = 600,
    late_offset: int = 0,
    metrics: Sequence[str] = CHANGE_METRICS,
) -> tuple[ForensicChange, ...]:
    """What moved between two offsets, strongest relative move first."""

    early = next(
        (snap for snap in snapshots if snap.offset_seconds == early_offset), None
    )
    late = next((snap for snap in snapshots if snap.offset_seconds == late_offset), None)
    if early is None or late is None:
        return ()
    changes = [
        ForensicChange(
            metric=metric, early_value=early.value(metric), late_value=late.value(metric)
        )
        for metric in metrics
    ]
    changes.sort(
        key=lambda change: (change.ratio if change.ratio is not None else Decimal("-1")),
        reverse=True,
    )
    return tuple(changes)


def build_forensics(
    state: PretrendState,
    *,
    entry: TrendEntryEvent | None,
    affinities: Mapping[str, AffinityRecord] | None = None,
    cascade: CascadeProfile | None = None,
    predicted: bool | None = None,
    first_alert_at: int | None = None,
    first_alert_probability: Decimal | None = None,
    first_alert_market_cap_usd: Decimal | None = None,
    offsets: Sequence[int] = FORENSIC_OFFSETS_SECONDS,
) -> ForensicReport:
    """Assemble the whole report for one mint."""

    limitations: list[str] = []
    if entry is None:
        return ForensicReport(
            mint=state.mint,
            entry=None,
            limitations=("no FOMO_TREND_ENTER event is recorded for this mint",),
        )

    entry_at = entry.occurred_at
    snapshots = reconstruct_timeline(state, entry_at=entry_at, offsets=offsets)
    if state.tape is None:
        limitations.append(
            "no FOMO-native activity was collected for this mint; the FOMO "
            "columns are UNKNOWN rather than zero"
        )
    earliest_sample = state.market.market_cap_usd.earliest()
    if earliest_sample is not None and earliest_sample.at > entry_at - max(offsets):
        limitations.append(
            f"market collection began {earliest_sample.at - (entry_at - max(offsets))}s "
            "into the reconstruction window; earlier offsets are sparse"
        )
    if not any(snapshot.vector.completeness > Decimal("0.2") for snapshot in snapshots):
        limitations.append(
            "fewer than 20% of features were computable at every offset; treat "
            "this timeline as indicative only"
        )

    lead = (
        None
        if first_alert_at is None
        else entry_at - first_alert_at
    )

    return ForensicReport(
        mint=state.mint,
        entry=entry,
        snapshots=snapshots,
        early_actors=earliest_actors(
            state.tape, entry_at=entry_at, affinities=affinities
        ),
        changes=summarise_changes(snapshots),
        cascade=cascade,
        predicted=predicted,
        first_alert_at=first_alert_at,
        first_alert_probability=first_alert_probability,
        first_alert_market_cap_usd=first_alert_market_cap_usd,
        lead_seconds=lead,
        limitations=tuple(limitations),
    )
