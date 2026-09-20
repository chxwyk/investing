"""When providers disagree about the same number, do not pick the nicest one.

FOMO says the market cap is $61K, DEX Screener says $74K, GMGN says $68K.  All
three are reading the same chain, so at most one is right and probably none are
current.  The tempting resolutions are all wrong in the same direction:

* taking the highest makes every token look stronger than it is,
* taking the newest quietly prefers whichever provider polls fastest,
* averaging invents a number no source ever reported.

So this module does three things instead.  It **keeps every reading** with its
provider and timestamp, so the disagreement itself is auditable.  It applies one
**deterministic** normalisation — the median, which is the most robust choice
when one of three sources is badly wrong and we cannot tell which.  And it
**surfaces** the spread, because a 20% disagreement between providers is itself
information: it usually means the token is moving fast, the pools are
fragmented, or one provider is stale.

A wide spread never silently becomes a confident number.  It is reported, and
downstream code can treat a high-disagreement reading as lower-confidence
evidence rather than as fact.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

ZERO = Decimal("0")

#: Relative spread above which a reading is flagged as materially disputed.
DEFAULT_MATERIAL_SPREAD = Decimal("0.15")


@dataclass(frozen=True, slots=True)
class ProviderReading:
    """One provider's answer for one quantity, with when it was true."""

    provider: str
    value: Decimal
    #: The provider's own timestamp, when it supplies one.
    source_at: int | None = None
    received_at: int | None = None

    @property
    def staleness(self) -> int | None:
        if self.source_at is None or self.received_at is None:
            return None
        return self.received_at - self.source_at


@dataclass(frozen=True, slots=True)
class Reconciled:
    """The normalised value, the spread around it, and every input kept."""

    metric: str
    value: Decimal | None
    readings: tuple[ProviderReading, ...] = ()
    method: str = "median"

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(reading.provider for reading in self.readings)

    @property
    def low(self) -> Decimal | None:
        return min((r.value for r in self.readings), default=None)

    @property
    def high(self) -> Decimal | None:
        return max((r.value for r in self.readings), default=None)

    @property
    def spread(self) -> Decimal | None:
        """Relative spread: (high - low) / median.  ``None`` below two readings."""

        if len(self.readings) < 2 or self.value is None or self.value <= ZERO:
            return None
        low, high = self.low, self.high
        if low is None or high is None:
            return None
        return ((high - low) / self.value).quantize(Decimal("0.0001"))

    def disputed(
        self, *, threshold: Decimal = DEFAULT_MATERIAL_SPREAD
    ) -> bool:
        spread = self.spread
        return spread is not None and spread > threshold

    @property
    def confidence(self) -> str:
        """``SINGLE_SOURCE`` / ``AGREED`` / ``DISPUTED`` / ``UNKNOWN``.

        ``SINGLE_SOURCE`` is deliberately distinct from ``AGREED``: one provider
        agreeing with itself is not corroboration, and rendering it as agreement
        is how an unchecked number acquires a second opinion it never had.
        """

        if self.value is None:
            return "UNKNOWN"
        if len(self.readings) < 2:
            return "SINGLE_SOURCE"
        return "DISPUTED" if self.disputed() else "AGREED"

    def to_json(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "value": None if self.value is None else str(self.value),
            "method": self.method,
            "confidence": self.confidence,
            "spread": None if self.spread is None else str(self.spread),
            "low": None if self.low is None else str(self.low),
            "high": None if self.high is None else str(self.high),
            "readings": [
                {
                    "provider": reading.provider,
                    "value": str(reading.value),
                    "source_at": reading.source_at,
                    "staleness": reading.staleness,
                }
                for reading in self.readings
            ],
        }


def reconcile(
    metric: str,
    readings: Sequence[ProviderReading],
    *,
    max_staleness_seconds: int | None = None,
) -> Reconciled:
    """Normalise deterministically, keeping every input.

    Stale readings are dropped *before* the median when a bound is given, since
    a five-minute-old market cap is not a dissenting opinion about the present,
    it is a correct opinion about the past.  If dropping leaves nothing, the
    result is UNKNOWN rather than a silent fallback to the stale value.
    """

    usable = [reading for reading in readings if reading.value.is_finite()]
    if max_staleness_seconds is not None:
        usable = [
            reading
            for reading in usable
            if reading.staleness is None or reading.staleness <= max_staleness_seconds
        ]
    if not usable:
        return Reconciled(metric=metric, value=None, readings=tuple(readings))

    ordered = sorted(usable, key=lambda reading: reading.value)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        value = ordered[middle].value
    else:
        value = ((ordered[middle - 1].value + ordered[middle].value) / 2).quantize(
            Decimal("0.00000001")
        )
    return Reconciled(
        metric=metric, value=value, readings=tuple(usable), method="median"
    )


def disagreement_note(reconciled: Reconciled) -> str:
    """One line a card can print when providers materially disagree."""

    if reconciled.confidence != "DISPUTED":
        return ""
    parts = ", ".join(
        f"{reading.provider} {reading.value}" for reading in reconciled.readings
    )
    return (
        f"{reconciled.metric}: providers disagree by {reconciled.spread} "
        f"({parts}); using the median {reconciled.value}"
    )
