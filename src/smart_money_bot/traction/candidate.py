"""The Early Traction candidate, and three blocks that are never blended.

The operator was explicit: momentum, opportunity quality and safety stay separate
fields, not one score.  That instruction is load-bearing rather than cosmetic.  A
single blended number lets a strong momentum reading cancel a 70%-top-ten
holding, and the result is a confident figure that describes neither — the exact
failure this repository's own v2.54 release notes describe ("a card reading WATCH
— HEATING UP above its own body saying Safety: UNKNOWN was telling the operator
two opposite things and letting the louder one win").

So there are three blocks, computed independently, rendered independently, and
with no arithmetic connecting them:

**Momentum** — is this moving *now*?  Rates of change of market cap and volume,
from readings this lane took itself.  Cheap: derived from the in-memory pool, no
extra provider call.

**Quality** — is the move worth anything?  Ratios that put the move in
proportion: volume against market cap, liquidity against market cap.  A $9K token
doing $40K of volume is a different object from one doing $5.1K, and neither is
"better" in a way a single number captures.

**Safety** — what is wrong with it?  Lives in
:mod:`smart_money_bot.traction.safety`, is display-only, and cannot gate.

Each block reports ``UNKNOWN`` when its inputs are missing rather than defaulting
to a neutral-looking number, because on a two-minute-old token most inputs
genuinely are missing and pretending otherwise is how a card ends up asserting
things nobody measured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .launchpads import MIGRATION_UNKNOWN
from .profile import ProfileVerdict, TractionObservation
from .safety import SafetyReport
from .xlink import XLinkVerdict

ZERO = Decimal("0")

# --- qualitative labels ------------------------------------------------------
UNKNOWN = "UNKNOWN"
ACCELERATING = "ACCELERATING"
STEADY = "STEADY"
FADING = "FADING"

THIN = "THIN"
BALANCED = "BALANCED"
RICH = "RICH"


@dataclass(frozen=True, slots=True)
class MomentumBlock:
    """Is it moving?  Rates only — no judgement about whether that is good."""

    #: USD of market cap added per minute, from our own readings.
    market_cap_per_minute: Decimal | None = None
    #: USD of volume per minute over the profile's window.
    volume_per_minute: Decimal | None = None
    #: Latest reading divided by the one before it.
    market_cap_ratio: Decimal | None = None
    readings: int = 0
    window_seconds: int = 0

    @property
    def state(self) -> str:
        if self.market_cap_ratio is None or self.readings < 2:
            return UNKNOWN
        if self.market_cap_ratio >= Decimal("1.15"):
            return ACCELERATING
        if self.market_cap_ratio <= Decimal("0.95"):
            return FADING
        return STEADY

    def render_lines(self) -> tuple[str, ...]:
        if self.readings < 2:
            return (
                f"only {self.readings} reading(s) so far — rates need two",
                "state: UNKNOWN",
            )
        def money(value: Decimal | None) -> str:
            return "unknown" if value is None else f"${value:,.0f}"

        return (
            f"state: {self.state}",
            f"mcap rate: {money(self.market_cap_per_minute)}/min",
            f"volume rate: {money(self.volume_per_minute)}/min",
            f"mcap ratio vs previous reading: "
            f"{'unknown' if self.market_cap_ratio is None else f'{self.market_cap_ratio:.2f}x'}",
            f"({self.readings} readings over {self.window_seconds}s)",
        )

    def to_json(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "state": self.state,
            "market_cap_per_minute": s(self.market_cap_per_minute),
            "volume_per_minute": s(self.volume_per_minute),
            "market_cap_ratio": s(self.market_cap_ratio),
            "readings": self.readings,
            "window_seconds": self.window_seconds,
        }


@dataclass(frozen=True, slots=True)
class QualityBlock:
    """Is the move in proportion?  Ratios, with no safety content."""

    volume_over_market_cap: Decimal | None = None
    liquidity_over_market_cap: Decimal | None = None

    @property
    def state(self) -> str:
        ratio = self.volume_over_market_cap
        if ratio is None:
            return UNKNOWN
        if ratio >= Decimal("1.0"):
            return RICH
        if ratio <= Decimal("0.2"):
            return THIN
        return BALANCED

    def render_lines(self) -> tuple[str, ...]:
        def ratio(value: Decimal | None) -> str:
            return "unknown" if value is None else f"{value:.2f}x"

        return (
            f"state: {self.state}",
            f"volume / mcap: {ratio(self.volume_over_market_cap)}",
            f"liquidity / mcap: {ratio(self.liquidity_over_market_cap)}",
        )

    def to_json(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "state": self.state,
            "volume_over_market_cap": s(self.volume_over_market_cap),
            "liquidity_over_market_cap": s(self.liquidity_over_market_cap),
        }


def build_momentum(
    readings: list[tuple[int, Decimal | None, Decimal | None]],
    *,
    volume_window_seconds: int,
) -> MomentumBlock:
    """Rates from ``(at, market_cap, volume)`` readings this lane took itself.

    Divides by the time actually elapsed between the two readings used, not by a
    nominal window: on a sparse pool those differ, and dividing by the nominal
    value would understate the rate exactly when polling was slowest.
    """

    usable = [
        (at, market_cap, volume)
        for at, market_cap, volume in sorted(readings, key=lambda row: row[0])
        if market_cap is not None
    ]
    if len(usable) < 2:
        latest_volume = next(
            (volume for _, _, volume in reversed(usable) if volume is not None), None
        )
        return MomentumBlock(
            volume_per_minute=(
                None
                if latest_volume is None or volume_window_seconds <= 0
                else (
                    latest_volume * Decimal(60) / Decimal(volume_window_seconds)
                ).quantize(Decimal("0.01"))
            ),
            readings=len(usable),
        )

    first_at, first_cap, _ = usable[0]
    last_at, last_cap, last_volume = usable[-1]
    elapsed = max(1, last_at - first_at)

    market_cap_per_minute = (
        (last_cap - first_cap) * Decimal(60) / Decimal(elapsed)
    ).quantize(Decimal("0.01"))

    previous_cap = usable[-2][1]
    ratio = (
        None
        if previous_cap is None or previous_cap <= ZERO
        else (last_cap / previous_cap).quantize(Decimal("0.0001"))
    )

    return MomentumBlock(
        market_cap_per_minute=market_cap_per_minute,
        volume_per_minute=(
            None
            if last_volume is None or volume_window_seconds <= 0
            else (
                last_volume * Decimal(60) / Decimal(volume_window_seconds)
            ).quantize(Decimal("0.01"))
        ),
        market_cap_ratio=ratio,
        readings=len(usable),
        window_seconds=elapsed,
    )


def build_quality(observation: TractionObservation) -> QualityBlock:
    """Proportion ratios.  Returns unknowns rather than guessing a denominator."""

    market_cap = observation.market_cap_usd
    if market_cap is None or market_cap <= ZERO:
        return QualityBlock()
    return QualityBlock(
        volume_over_market_cap=(
            None
            if observation.volume_usd is None
            else (observation.volume_usd / market_cap).quantize(Decimal("0.0001"))
        ),
        liquidity_over_market_cap=(
            None
            if observation.liquidity_usd is None
            else (observation.liquidity_usd / market_cap).quantize(Decimal("0.0001"))
        ),
    )


@dataclass(frozen=True, slots=True)
class TractionCandidate:
    """One qualifying mint, everything the card needs, nothing blended."""

    observation: TractionObservation
    verdict: ProfileVerdict
    momentum: MomentumBlock = field(default_factory=MomentumBlock)
    quality: QualityBlock = field(default_factory=QualityBlock)
    safety: SafetyReport | None = None
    x_link: XLinkVerdict | None = None
    #: On-chain creation, first sighting, alert send -- the latency anchors.
    detected_at: int = 0
    qualified_at: int = 0
    alert_sent_at: int | None = None

    @property
    def mint(self) -> str:
        return self.observation.mint

    @property
    def launchpad(self) -> str:
        return self.observation.launchpad or "unknown"

    @property
    def migration_state(self) -> str:
        return self.observation.migration_state or MIGRATION_UNKNOWN

    def age_seconds(self, *, now: int | None = None) -> int | None:
        moment = now if now is not None else self.qualified_at
        return self.observation.age_seconds(now=moment)

    @property
    def detection_latency_seconds(self) -> int | None:
        if self.observation.chain_created_at is None:
            return None
        return max(0, self.detected_at - self.observation.chain_created_at)

    @property
    def alert_latency_seconds(self) -> int | None:
        if self.observation.chain_created_at is None or self.alert_sent_at is None:
            return None
        return max(0, self.alert_sent_at - self.observation.chain_created_at)

    def to_json(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "launchpad": self.launchpad,
            "migration_state": self.migration_state,
            "name": self.observation.name,
            "symbol": self.observation.symbol,
            "age_seconds": self.age_seconds(),
            "market_cap_usd": (
                None
                if self.observation.market_cap_usd is None
                else str(self.observation.market_cap_usd)
            ),
            "volume_usd": (
                None
                if self.observation.volume_usd is None
                else str(self.observation.volume_usd)
            ),
            "detected_at": self.detected_at,
            "qualified_at": self.qualified_at,
            "alert_sent_at": self.alert_sent_at,
            "detection_latency_seconds": self.detection_latency_seconds,
            "alert_latency_seconds": self.alert_latency_seconds,
            "momentum": self.momentum.to_json(),
            "quality": self.quality.to_json(),
            "safety": None if self.safety is None else self.safety.to_json(),
            "x_link": None if self.x_link is None else self.x_link.to_json(),
            "verdict": self.verdict.to_json(),
        }
