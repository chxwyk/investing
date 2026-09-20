"""Rolling windows, velocity and acceleration — computed only from the past.

Every feature in this engine is one of three things about some metric: its
**level** now, its **velocity** (how fast it is changing) and its
**acceleration** (whether that change is itself speeding up).  For predicting a
board entry that has not happened yet, the second and third usually matter more
than the first: a token with 400 buyers that has had 400 buyers for an hour is a
different object from a token with 80 buyers that had 10 a minute ago.

The one rule this module enforces structurally is that a window ending at ``t``
reads only samples with timestamp ``<= t``.  :func:`window_slice` is the single
door to sample data and it cannot return a later sample, so no caller — live,
training or replay — can accidentally read forward.  That is not a stylistic
preference: with a label defined as "entered Trending within the next 5
minutes", a single feature that peeks one second past ``t`` would leak the
answer and produce a backtest that cannot be reproduced live.

Acceleration is measured as the difference between the most recent velocity and
the velocity of the immediately preceding window of equal length, so it has the
units of "per second, per window" and is comparable across metrics.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")

#: The window ladder the whole engine speaks in.  Short windows dominate
#: because the event we are predicting resolves in minutes, not hours.
STANDARD_WINDOWS_SECONDS: tuple[int, ...] = (15, 30, 60, 120, 180, 300, 600, 900)

#: Human labels for the ladder, used in feature names and on cards.
WINDOW_LABELS: dict[int, str] = {
    15: "15s",
    30: "30s",
    60: "1m",
    120: "2m",
    180: "3m",
    300: "5m",
    600: "10m",
    900: "15m",
}


def window_label(seconds: int) -> str:
    """The canonical short label for a window length."""

    if seconds in WINDOW_LABELS:
        return WINDOW_LABELS[seconds]
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def to_decimal(value: Any) -> Decimal | None:
    """Coerce to a finite Decimal, or admit the value is unusable."""

    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


@dataclass(frozen=True, slots=True)
class Sample:
    """One timestamped reading of one metric, with its provenance."""

    at: int
    value: Decimal
    #: When the provider observed it, if that differs from when we received it.
    source_at: int | None = None
    provider: str = ""

    @property
    def lag_seconds(self) -> int | None:
        """How stale the reading already was when we stored it."""

        return None if self.source_at is None else self.at - self.source_at


class Series:
    """An append-only, time-ordered series of samples for one metric.

    Append-only is the point.  Overwriting a past sample with a present value is
    the most common way a "historical" dataset silently becomes a snapshot of
    now, and every level, velocity and acceleration derived from it then
    describes a moment that never existed.
    """

    __slots__ = ("name", "_times", "_samples")

    def __init__(self, name: str, samples: Sequence[Sample] = ()) -> None:
        self.name = name
        ordered = sorted(samples, key=lambda sample: sample.at)
        self._samples: list[Sample] = list(ordered)
        self._times: list[int] = [sample.at for sample in ordered]

    def __len__(self) -> int:
        return len(self._samples)

    def __iter__(self):
        return iter(self._samples)

    @property
    def samples(self) -> tuple[Sample, ...]:
        return tuple(self._samples)

    def append(self, sample: Sample) -> None:
        """Add a sample.  Out-of-order arrivals are inserted, never dropped."""

        index = bisect_right(self._times, sample.at)
        self._times.insert(index, sample.at)
        self._samples.insert(index, sample)

    def add(
        self,
        at: int,
        value: Any,
        *,
        source_at: int | None = None,
        provider: str = "",
    ) -> bool:
        """Convenience append.  Returns False when the value was unusable."""

        decimal_value = to_decimal(value)
        if decimal_value is None:
            return False
        self.append(Sample(at=at, value=decimal_value, source_at=source_at, provider=provider))
        return True

    # ------------------------------------------------------------------
    def before(self, at: int, *, inclusive: bool = True) -> tuple[Sample, ...]:
        """Every sample at or before ``at``.  The only door to the data."""

        index = bisect_right(self._times, at) if inclusive else _bisect_left(self._times, at)
        return tuple(self._samples[:index])

    def slice(self, start: int, end: int) -> tuple[Sample, ...]:
        """Samples in ``(start, end]`` — half-open at the start, closed at the end."""

        left = bisect_right(self._times, start)
        right = bisect_right(self._times, end)
        return tuple(self._samples[left:right])

    def latest(self, at: int) -> Sample | None:
        """The most recent sample at or before ``at``."""

        index = bisect_right(self._times, at)
        return self._samples[index - 1] if index else None

    def value_at(self, at: int) -> Decimal | None:
        sample = self.latest(at)
        return None if sample is None else sample.value

    def earliest(self) -> Sample | None:
        return self._samples[0] if self._samples else None


def _bisect_left(times: list[int], at: int) -> int:
    low, high = 0, len(times)
    while low < high:
        mid = (low + high) // 2
        if times[mid] < at:
            low = mid + 1
        else:
            high = mid
    return low


def window_slice(series: Series, *, end: int, seconds: int) -> tuple[Sample, ...]:
    """Samples inside the window ending at ``end``.  Never reads past ``end``."""

    return series.slice(end - seconds, end)


@dataclass(frozen=True, slots=True)
class Dynamics:
    """Level, velocity and acceleration for one metric at one instant."""

    metric: str
    window_seconds: int
    level: Decimal | None = None
    #: Change per second across the window.
    velocity: Decimal | None = None
    #: This window's velocity minus the previous equal window's velocity.
    acceleration: Decimal | None = None
    #: ``level`` divided by the equal-length window immediately before it.
    ratio_to_prior: Decimal | None = None
    samples: int = 0
    prior_samples: int = 0

    @property
    def known(self) -> bool:
        return self.level is not None

    def classify(
        self,
        *,
        accelerating_at: Decimal = Decimal("1.5"),
        decelerating_at: Decimal = Decimal("0.7"),
    ) -> str:
        """``ACCELERATING`` / ``STABLE`` / ``DECELERATING`` / ``UNKNOWN``.

        ``UNKNOWN`` is returned whenever the comparison could not be made.  It
        is a real value, not a synonym for stable (section 67).
        """

        if self.ratio_to_prior is None:
            return "UNKNOWN"
        if self.ratio_to_prior >= accelerating_at:
            return "ACCELERATING"
        if self.ratio_to_prior <= decelerating_at:
            return "DECELERATING"
        return "STABLE"

    def to_json(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "window_seconds": self.window_seconds,
            "window": window_label(self.window_seconds),
            "level": None if self.level is None else str(self.level),
            "velocity": None if self.velocity is None else str(self.velocity),
            "acceleration": None if self.acceleration is None else str(self.acceleration),
            "ratio_to_prior": (
                None if self.ratio_to_prior is None else str(self.ratio_to_prior)
            ),
            "samples": self.samples,
            "prior_samples": self.prior_samples,
            "trend": self.classify(),
        }


def _ratio(current: Decimal | None, prior: Decimal | None) -> Decimal | None:
    """Growth ratio that stays honest when the prior window was empty.

    A jump from 0 to 12 is unbounded growth, not "12x".  Returning ``None``
    there is deliberate: a synthetic large ratio would dominate any percentile
    ranking and manufacture signal out of a token's first observation.
    """

    if current is None or prior is None or prior <= ZERO:
        return None
    return (current / prior).quantize(Decimal("0.0001"))


def counter_dynamics(
    series: Series,
    *,
    end: int,
    seconds: int,
    metric: str = "",
) -> Dynamics:
    """Dynamics for an **event counter** (buys, buyers, theses).

    The level is the number of events inside the window — this is the right
    reading for a stream of discrete events, where a "current value" does not
    exist between events.
    """

    current = window_slice(series, end=end, seconds=seconds)
    prior = series.slice(end - 2 * seconds, end - seconds)
    level = Decimal(len(current))
    prior_level = Decimal(len(prior))
    velocity = (level / Decimal(seconds)).quantize(Decimal("0.000001"))
    prior_velocity = (prior_level / Decimal(seconds)).quantize(Decimal("0.000001"))
    return Dynamics(
        metric=metric or series.name,
        window_seconds=seconds,
        level=level,
        velocity=velocity,
        acceleration=velocity - prior_velocity,
        ratio_to_prior=_ratio(level, prior_level),
        samples=len(current),
        prior_samples=len(prior),
    )


def sum_dynamics(
    series: Series,
    *,
    end: int,
    seconds: int,
    metric: str = "",
) -> Dynamics:
    """Dynamics for an **additive quantity** (notional USD, volume)."""

    current = window_slice(series, end=end, seconds=seconds)
    prior = series.slice(end - 2 * seconds, end - seconds)
    level = sum((sample.value for sample in current), ZERO)
    prior_level = sum((sample.value for sample in prior), ZERO)
    velocity = (level / Decimal(seconds)).quantize(Decimal("0.000001"))
    prior_velocity = (prior_level / Decimal(seconds)).quantize(Decimal("0.000001"))
    return Dynamics(
        metric=metric or series.name,
        window_seconds=seconds,
        level=level,
        velocity=velocity,
        acceleration=velocity - prior_velocity,
        ratio_to_prior=_ratio(level, prior_level),
        samples=len(current),
        prior_samples=len(prior),
    )


def level_dynamics(
    series: Series,
    *,
    end: int,
    seconds: int,
    metric: str = "",
) -> Dynamics:
    """Dynamics for a **stock variable** (price, market cap, holders, liquidity).

    Velocity is the change between the reading at the window's start and the
    reading at its end, divided by the elapsed time between those two readings —
    not by the nominal window length.  Dividing by the nominal length would
    understate the rate whenever polling was sparse, which is exactly when the
    engine is least certain and should not also be biased.
    """

    end_sample = series.latest(end)
    if end_sample is None:
        return Dynamics(metric=metric or series.name, window_seconds=seconds)

    def baseline(at: int, window_end: int) -> Sample | None:
        """The best available reading for the state of the metric at ``at``.

        Prefer a reading from at or before ``at``.  When collection began
        *inside* the window there is none, so fall back to the earliest reading
        within the window: velocity is divided by real elapsed time, so a
        shorter baseline yields a correct rate over a shorter period rather
        than a wrong rate or a spurious ``None``.
        """

        found = series.latest(at)
        if found is not None:
            return found
        inside = series.slice(at, window_end)
        return inside[0] if inside else None

    start_sample = baseline(end - seconds, end)
    prior_sample = baseline(end - 2 * seconds, end - seconds)

    velocity: Decimal | None = None
    if start_sample is not None and start_sample.at < end_sample.at:
        elapsed = Decimal(end_sample.at - start_sample.at)
        velocity = ((end_sample.value - start_sample.value) / elapsed).quantize(
            Decimal("0.000001")
        )

    prior_velocity: Decimal | None = None
    if (
        prior_sample is not None
        and start_sample is not None
        and prior_sample.at < start_sample.at
    ):
        elapsed = Decimal(start_sample.at - prior_sample.at)
        prior_velocity = ((start_sample.value - prior_sample.value) / elapsed).quantize(
            Decimal("0.000001")
        )

    acceleration = (
        None if velocity is None or prior_velocity is None else velocity - prior_velocity
    )
    return Dynamics(
        metric=metric or series.name,
        window_seconds=seconds,
        level=end_sample.value,
        velocity=velocity,
        acceleration=acceleration,
        ratio_to_prior=(
            None
            if start_sample is None
            else _ratio(end_sample.value, start_sample.value)
        ),
        samples=len(window_slice(series, end=end, seconds=seconds)),
        prior_samples=len(series.slice(end - 2 * seconds, end - seconds)),
    )


def first_acceleration_at(
    series: Series,
    *,
    until: int,
    window_seconds: int = 60,
    step_seconds: int = 15,
    ratio_threshold: Decimal = Decimal("2"),
    min_level: Decimal = Decimal("3"),
    kind: str = "counter",
    min_prior_samples: int = 2,
    since: int | None = None,
) -> int | None:
    """The first instant this metric measurably took off, scanning forward.

    Used to answer "which signal moved first?" (section 22).  The scan is
    bounded by ``until``, so a reconstruction performed after a board entry
    still cannot see past the moment it claims to describe.

    Two guards keep this from firing on noise.  ``min_level`` stops a jump from
    1 event to 2 counting as a 2x acceleration; on a low-traffic token that
    happens constantly.  ``min_prior_samples`` stops the *series' own birth*
    from registering as acceleration — the first populated window always looks
    explosive next to the empty one before it, so without this guard every
    metric would report that it moved first, at the moment it started being
    collected, and the "which signal led?" comparison would be meaningless.
    """

    earliest = series.earliest()
    if earliest is None:
        return None
    dynamics_fn = counter_dynamics if kind == "counter" else (
        sum_dynamics if kind == "sum" else level_dynamics
    )
    # Start once a full prior window can exist, so the comparison is against an
    # established baseline rather than against the absence of data.
    floor = earliest.at + 2 * window_seconds
    start = max(floor, since) if since is not None else floor
    moment = start
    while moment <= until:
        dynamics = dynamics_fn(series, end=moment, seconds=window_seconds)
        if (
            dynamics.level is not None
            and dynamics.level >= min_level
            and dynamics.prior_samples >= min_prior_samples
            and dynamics.ratio_to_prior is not None
            and dynamics.ratio_to_prior >= ratio_threshold
        ):
            return moment
        moment += step_seconds
    return None
