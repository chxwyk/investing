"""Source-specific latency forensics and pipeline breakdown (sections 12, 13).

The production `/fomo latency` sample that motivated this module reported a
median source→first-seen of ~532 seconds and a p90 of ~67,620 seconds — nearly
19 hours.  No ingestion loop is 19 hours slow.  The p90 was a measurement
artifact.

``source_at`` was taken as ``chain_created_at or pair_created_at or
graduated_at`` — the moment the *token or pair was created*, not the moment a
realtime source told us it existed.  So the metric was really measuring "how old
was this pair when the trending feed first surfaced it", which is a property of
the upstream source's own ranking, not of our pipeline.  An old pair appearing
on a trending list is a *historical* reference point, and mixing it into a
realtime latency figure is exactly what section 12 forbids.

This module keeps the honest measurement and throws none of the data away:

* every timing gets a quality grade, and only ``REALTIME``-grade samples feed
  the realtime percentiles,
* ``HISTORICAL`` samples are still reported, separately and labelled,
* and the pipeline is broken into its real stages so the slow one is visible.

No timestamp is ever rewritten to make a number look better.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal("0")

# --- timing quality (section 12) ---------------------------------------------
REALTIME = "REALTIME"
APPROXIMATE = "APPROXIMATE"
HISTORICAL = "HISTORICAL"
UNKNOWN = "UNKNOWN"

#: A source timestamp further back than this cannot be read as a realtime
#: discovery event.  A pair created two hours before we saw it tells us about
#: the upstream feed's ranking delay, not about our ingestion speed.
REALTIME_HORIZON_SECONDS = 3_600

#: Within this window the source timestamp is a good realtime proxy.
EXACT_HORIZON_SECONDS = 900

#: Capture buckets reported for every realtime source.
CAPTURE_BUCKETS: tuple[tuple[str, int], ...] = (
    ("30s", 30),
    ("60s", 60),
    ("2m", 120),
    ("5m", 300),
    ("10m", 600),
)

# --- pipeline stages (section 13) --------------------------------------------
STAGE_SOURCE_TO_FIRST_SEEN = "SOURCE_TO_FIRST_SEEN"
STAGE_FIRST_SEEN_TO_WATCH = "FIRST_SEEN_TO_FAST_WATCH"
STAGE_WATCH_TO_QUALIFIED = "FAST_WATCH_TO_QUALIFIED"
STAGE_QUALIFIED_TO_DECISION = "QUALIFIED_TO_PAPER_DECISION"
STAGE_DECISION_TO_FILL = "PAPER_DECISION_TO_SIMULATED_FILL"

PIPELINE_STAGES: tuple[str, ...] = (
    STAGE_SOURCE_TO_FIRST_SEEN,
    STAGE_FIRST_SEEN_TO_WATCH,
    STAGE_WATCH_TO_QUALIFIED,
    STAGE_QUALIFIED_TO_DECISION,
    STAGE_DECISION_TO_FILL,
)


@dataclass(frozen=True, slots=True)
class LatencySample:
    """One candidate's timing across the whole pipeline."""

    mint: str
    source_name: str = "unknown"
    source_event_at: int | None = None
    ingested_at: int | None = None
    first_seen_at: int | None = None
    first_watch_at: int | None = None
    first_qualified_at: int | None = None
    first_discord_at: int | None = None
    first_paper_decision_at: int | None = None
    simulated_fill_at: int | None = None
    source_is_realtime: bool = True

    @property
    def timing_quality(self) -> str:
        """How much the source timestamp can honestly say about our speed."""

        if self.source_event_at is None or self.first_seen_at is None:
            return UNKNOWN
        delta = self.first_seen_at - self.source_event_at
        if delta < 0:
            return UNKNOWN
        if not self.source_is_realtime:
            return HISTORICAL
        if delta <= EXACT_HORIZON_SECONDS:
            return REALTIME
        if delta <= REALTIME_HORIZON_SECONDS:
            return APPROXIMATE
        return HISTORICAL

    @property
    def counts_as_realtime(self) -> bool:
        return self.timing_quality in {REALTIME, APPROXIMATE}

    def stage_seconds(self, stage: str) -> int | None:
        pairs = {
            STAGE_SOURCE_TO_FIRST_SEEN: (self.source_event_at, self.first_seen_at),
            STAGE_FIRST_SEEN_TO_WATCH: (self.first_seen_at, self.first_watch_at),
            STAGE_WATCH_TO_QUALIFIED: (
                self.first_watch_at or self.first_seen_at,
                self.first_qualified_at,
            ),
            STAGE_QUALIFIED_TO_DECISION: (
                self.first_qualified_at,
                self.first_paper_decision_at,
            ),
            STAGE_DECISION_TO_FILL: (self.first_paper_decision_at, self.simulated_fill_at),
        }
        start, end = pairs.get(stage, (None, None))
        if start is None or end is None or end < start:
            return None
        return end - start


@dataclass(frozen=True, slots=True)
class LatencyStats:
    """Percentiles and capture rates for one population of samples."""

    count: int = 0
    p50: Decimal | None = None
    p90: Decimal | None = None
    captures: Mapping[str, Decimal] = field(default_factory=dict)

    @property
    def sufficient(self) -> bool:
        return self.count >= 5


@dataclass(frozen=True, slots=True)
class SourceLatency:
    """Latency for one named discovery source, graded honestly."""

    source_name: str
    realtime: LatencyStats = field(default_factory=LatencyStats)
    historical_count: int = 0
    unknown_count: int = 0
    quality: str = UNKNOWN

    @property
    def total(self) -> int:
        return self.realtime.count + self.historical_count + self.unknown_count


def summarize_stage(samples: Sequence[LatencySample], stage: str) -> LatencyStats:
    """Percentiles for one pipeline stage across every usable sample."""

    values = [
        Decimal(value)
        for value in (sample.stage_seconds(stage) for sample in samples)
        if value is not None
    ]
    if stage == STAGE_SOURCE_TO_FIRST_SEEN:
        # Only realtime-grade timings may describe our ingestion speed.
        values = [
            Decimal(sample.stage_seconds(stage) or 0)
            for sample in samples
            if sample.counts_as_realtime and sample.stage_seconds(stage) is not None
        ]
    return _stats(values)


def summarize_sources(samples: Sequence[LatencySample]) -> tuple[SourceLatency, ...]:
    """Per-source latency, with historical timings kept out of the percentiles."""

    grouped: dict[str, list[LatencySample]] = {}
    for sample in samples:
        grouped.setdefault(sample.source_name or "unknown", []).append(sample)

    results: list[SourceLatency] = []
    for name, rows in sorted(grouped.items()):
        realtime_values = [
            Decimal(row.stage_seconds(STAGE_SOURCE_TO_FIRST_SEEN) or 0)
            for row in rows
            if row.counts_as_realtime
            and row.stage_seconds(STAGE_SOURCE_TO_FIRST_SEEN) is not None
        ]
        historical = sum(1 for row in rows if row.timing_quality == HISTORICAL)
        unknown = sum(1 for row in rows if row.timing_quality == UNKNOWN)
        stats = _stats(realtime_values)
        if stats.count:
            quality = REALTIME if stats.count >= historical else APPROXIMATE
        elif historical:
            quality = HISTORICAL
        else:
            quality = UNKNOWN
        results.append(
            SourceLatency(
                source_name=name,
                realtime=stats,
                historical_count=historical,
                unknown_count=unknown,
                quality=quality,
            )
        )
    return tuple(results)


def pipeline_breakdown(samples: Sequence[LatencySample]) -> dict[str, LatencyStats]:
    """Every stage, so the slow one is obvious rather than inferred."""

    return {stage: summarize_stage(samples, stage) for stage in PIPELINE_STAGES}


def slowest_stage(breakdown: Mapping[str, LatencyStats]) -> str | None:
    """The stage with the worst measured median, ignoring unmeasured ones."""

    best: tuple[str, Decimal] | None = None
    for stage, stats in breakdown.items():
        if stats.p50 is None or not stats.count:
            continue
        if best is None or stats.p50 > best[1]:
            best = (stage, stats.p50)
    return best[0] if best else None


@dataclass(frozen=True, slots=True)
class LatencyOutcomeBand:
    """Forward outcome grouped by how quickly we saw the token (section 47)."""

    label: str
    count: int = 0
    median_forward_return_percent: Decimal | None = None
    reached_25_percent: Decimal | None = None
    severe_failure_percent: Decimal | None = None

    @property
    def sufficient(self) -> bool:
        return self.count >= 5


def outcome_by_latency_band(
    rows: Sequence[tuple[int | None, Decimal | None, bool]],
) -> tuple[LatencyOutcomeBand, ...]:
    """Answer "were the earlier things actually useful?".

    ``rows`` are ``(first_seen_latency_seconds, forward_return_percent,
    severe_failure)``.  Speed only counts as an improvement if the earlier
    cohort actually performs better, which is what this makes measurable.
    """

    buckets: list[tuple[str, int | None]] = [
        ("<=30s", 30),
        ("<=60s", 60),
        ("<=2m", 120),
        ("<=5m", 300),
        ("<=10m", 600),
        (">10m", None),
    ]
    assigned: dict[str, list[tuple[Decimal | None, bool]]] = {
        label: [] for label, _ in buckets
    }
    for latency, forward, severe in rows:
        if latency is None:
            continue
        label = ">10m"
        for candidate_label, limit in buckets:
            if limit is not None and latency <= limit:
                label = candidate_label
                break
        assigned[label].append((forward, severe))

    bands: list[LatencyOutcomeBand] = []
    for label, _ in buckets:
        entries = assigned[label]
        returns = sorted(value for value, _ in entries if value is not None)
        severe = sum(1 for _, flag in entries if flag)
        reached = sum(1 for value, _ in entries if value is not None and value >= 25)
        bands.append(
            LatencyOutcomeBand(
                label=label,
                count=len(entries),
                median_forward_return_percent=_median(returns),
                reached_25_percent=_rate(reached, len(entries)),
                severe_failure_percent=_rate(severe, len(entries)),
            )
        )
    return tuple(bands)


def _stats(values: Sequence[Decimal]) -> LatencyStats:
    if not values:
        return LatencyStats()
    ordered = sorted(values)
    captures = {
        label: (
            Decimal(sum(1 for value in ordered if value <= seconds))
            / Decimal(len(ordered))
            * Decimal("100")
        ).quantize(Decimal("0.01"))
        for label, seconds in CAPTURE_BUCKETS
    }
    return LatencyStats(
        count=len(ordered),
        p50=_percentile(ordered, Decimal("0.50")),
        p90=_percentile(ordered, Decimal("0.90")),
        captures=captures,
    )


def _percentile(ordered: Sequence[Decimal], quantile: Decimal) -> Decimal | None:
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = Decimal(len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - Decimal(lower)
    return (ordered[lower] + (ordered[upper] - ordered[lower]) * fraction).quantize(
        Decimal("0.01")
    )


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return ((ordered[middle - 1] + ordered[middle]) / 2).quantize(Decimal("0.01"))


def _rate(count: int, total: int) -> Decimal | None:
    if total <= 0:
        return None
    return (Decimal(count) / Decimal(total) * Decimal("100")).quantize(Decimal("0.01"))
