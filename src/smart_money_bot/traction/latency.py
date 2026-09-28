"""Measure first: creation -> detection -> alert, at p50 and p95.

The operator's instruction was to measure before optimising, which is the right
order and the one most easily skipped.  This module defines the three stages that
matter and refuses to blur them:

``creation -> detection``
    On-chain creation timestamp to the moment our collector first saw the mint.
    This is the stage a websocket fixes and a poll interval dominates.

``detection -> alert``
    First sighting to the Discord message being accepted.  This is the stage that
    enrichment work would ruin, which is why enrichment happens after the send.

``creation -> alert``
    The number the operator actually experiences.  Not the sum of the other two
    medians -- percentiles do not add -- so it is measured directly.

Two measurement rules, both learned from `lab/latency.py` in this repository:

**A sample without an on-chain creation time is not a fast sample, it is an
unknown one.**  Averaging it in as zero would flatter every figure. Such samples
are counted separately and excluded from the percentiles.

**p95, not just p50.**  The median hides the tail, and the tail is where an
opportunity is actually lost. This module reports p50, p95 and max, with the
sample count beside them so a figure from four observations looks like one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

STAGE_CREATION_TO_DETECTION = "creation_to_detection"
STAGE_DETECTION_TO_ALERT = "detection_to_alert"
STAGE_CREATION_TO_ALERT = "creation_to_alert"

STAGES: tuple[str, ...] = (
    STAGE_CREATION_TO_DETECTION,
    STAGE_DETECTION_TO_ALERT,
    STAGE_CREATION_TO_ALERT,
)

#: Below this many samples a percentile is reported but flagged as thin.
MIN_SAMPLES = 5


@dataclass(frozen=True, slots=True)
class LatencySample:
    """One token's journey, in absolute timestamps."""

    mint: str
    #: On-chain creation.  ``None`` makes this an unknown-grade sample.
    chain_created_at: int | None
    detected_at: int
    alert_sent_at: int | None = None
    source: str = ""
    launchpad: str = ""

    @property
    def graded_realtime(self) -> bool:
        """Whether this sample may contribute to a latency percentile."""

        return self.chain_created_at is not None

    def stage_seconds(self, stage: str) -> int | None:
        if stage == STAGE_CREATION_TO_DETECTION:
            if self.chain_created_at is None:
                return None
            return max(0, self.detected_at - self.chain_created_at)
        if stage == STAGE_DETECTION_TO_ALERT:
            if self.alert_sent_at is None:
                return None
            return max(0, self.alert_sent_at - self.detected_at)
        if stage == STAGE_CREATION_TO_ALERT:
            if self.chain_created_at is None or self.alert_sent_at is None:
                return None
            return max(0, self.alert_sent_at - self.chain_created_at)
        return None


def _percentile(values: Sequence[int], fraction: Decimal) -> Decimal | None:
    """Nearest-rank percentile.  ``None`` on an empty population."""

    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return Decimal(ordered[0])
    index = int(
        (fraction * Decimal(len(ordered) - 1)).to_integral_value(rounding="ROUND_HALF_UP")
    )
    return Decimal(ordered[max(0, min(index, len(ordered) - 1))])


@dataclass(frozen=True, slots=True)
class StageLatency:
    """One stage's distribution, with the sample count attached."""

    stage: str
    samples: int = 0
    p50_seconds: Decimal | None = None
    p95_seconds: Decimal | None = None
    max_seconds: Decimal | None = None
    #: Samples excluded for lacking an on-chain creation time.
    unknown_grade: int = 0

    @property
    def sufficient(self) -> bool:
        return self.samples >= MIN_SAMPLES

    def render(self) -> str:
        if self.samples == 0:
            return f"{self.stage}: no samples yet"
        caveat = "" if self.sufficient else f" (thin: n={self.samples})"
        return (
            f"{self.stage}: p50 {self.p50_seconds}s · p95 {self.p95_seconds}s "
            f"· max {self.max_seconds}s · n={self.samples}{caveat}"
        )

    def to_json(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "stage": self.stage,
            "samples": self.samples,
            "p50_seconds": s(self.p50_seconds),
            "p95_seconds": s(self.p95_seconds),
            "max_seconds": s(self.max_seconds),
            "unknown_grade": self.unknown_grade,
            "sufficient": self.sufficient,
        }


def summarise_stage(samples: Sequence[LatencySample], stage: str) -> StageLatency:
    """Percentiles for one stage, excluding samples that cannot be graded."""

    usable: list[int] = []
    unknown = 0
    for sample in samples:
        if stage != STAGE_DETECTION_TO_ALERT and not sample.graded_realtime:
            unknown += 1
            continue
        value = sample.stage_seconds(stage)
        if value is None:
            unknown += 1
            continue
        usable.append(value)

    return StageLatency(
        stage=stage,
        samples=len(usable),
        p50_seconds=_percentile(usable, Decimal("0.5")),
        p95_seconds=_percentile(usable, Decimal("0.95")),
        max_seconds=Decimal(max(usable)) if usable else None,
        unknown_grade=unknown,
    )


@dataclass(frozen=True, slots=True)
class LatencyReport:
    """The whole picture, per stage and per source."""

    stages: dict[str, StageLatency] = field(default_factory=dict)
    by_source: dict[str, StageLatency] = field(default_factory=dict)
    total_samples: int = 0

    @property
    def headline(self) -> str:
        end_to_end = self.stages.get(STAGE_CREATION_TO_ALERT)
        if end_to_end is None or end_to_end.samples == 0:
            return "no end-to-end latency samples yet"
        return end_to_end.render()

    def render_lines(self) -> tuple[str, ...]:
        lines = [self.stages[stage].render() for stage in STAGES if stage in self.stages]
        if self.by_source:
            lines.append("by source (creation → detection):")
            lines.extend(
                f"  {source}: {stat.render().split(': ', 1)[1]}"
                for source, stat in sorted(self.by_source.items())
            )
        return tuple(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "total_samples": self.total_samples,
            "headline": self.headline,
            "stages": {name: stat.to_json() for name, stat in self.stages.items()},
            "by_source": {
                name: stat.to_json() for name, stat in self.by_source.items()
            },
        }


def build_report(samples: Sequence[LatencySample]) -> LatencyReport:
    """Summarise every stage, plus creation→detection split by source."""

    by_source: dict[str, list[LatencySample]] = {}
    for sample in samples:
        by_source.setdefault(sample.source or "unknown", []).append(sample)

    return LatencyReport(
        stages={stage: summarise_stage(samples, stage) for stage in STAGES},
        by_source={
            source: summarise_stage(group, STAGE_CREATION_TO_DETECTION)
            for source, group in by_source.items()
        },
        total_samples=len(samples),
    )
