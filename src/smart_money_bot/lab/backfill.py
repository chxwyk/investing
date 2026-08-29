"""Reconstruct pre-v2.36 lifecycle memory from existing runner history.

Confirmed production bug (sections 20-23): a mint that the Fomo runner observed
*before* the v2.36 lifecycle tables existed had no ``lab_token_lifecycle`` row,
so ``/fomo lifecycle <mint>`` initialised it as ``FIRST_DISCOVERY • FRESH``,
discovered "a few seconds ago", with zero alerts, zero qualifications and an
unknown historical peak.  That silently defeated the whole old-pump / re-entry
memory for every token observed before the upgrade.

The runner has been persisting the underlying evidence all along — candidate
rows, snapshots, alert events and stage events.  This module reconstructs the
lifecycle from that real history.

Two rules govern everything here:

* **Never fabricate.**  A field with no stored evidence stays ``None`` and the
  record is marked ``PARTIAL``; it is never filled with a plausible guess.
* **Never invent an earlier time.**  Reconstruction can only recover timestamps
  that were actually persisted, and repeated lookups can only ever move
  ``first_discovered_at`` earlier, never later.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal

from .config import DEFAULT_LAB_CONFIG, LabConfig
from .lifecycle import (
    ACTIVE_SETUP,
    COOLDOWN,
    FIRST_DISCOVERY,
    FIRST_QUALIFIED,
    RETRACED,
    SILENT_WATCH,
    WINNER,
    TokenLifecycle,
)

ZERO = Decimal("0")
HUNDRED = Decimal("100")

#: Completeness of a reconstruction.
COMPLETE = "COMPLETE"
PARTIAL = "PARTIAL"
UNKNOWN = "UNKNOWN"

#: Marker written into ``notes`` so a reconstructed record is never mistaken
#: for one the lab observed live.
BACKFILL_NOTE = "legacy_backfill"


@dataclass(frozen=True, slots=True)
class LegacyObservation:
    """One persisted historical observation of a mint."""

    observed_at: int
    price_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    safety_status: str | None = None
    qualified: bool = False
    stage: str | None = None


@dataclass(frozen=True, slots=True)
class LegacyEvidence:
    """Everything the pre-v2.36 tables know about one mint."""

    mint: str
    first_seen_at: int | None = None
    radar_first_seen_at: int | None = None
    first_discord_visible_at: int | None = None
    entry_eligible_at: int | None = None
    strong_alert_at: int | None = None
    last_seen_at: int | None = None
    first_price_usd: Decimal | None = None
    first_market_cap_usd: Decimal | None = None
    first_visible_market_cap_usd: Decimal | None = None
    entry_market_cap_usd: Decimal | None = None
    peak_market_cap_usd: Decimal | None = None
    alert_count: int = 0
    first_alert_at: int | None = None
    last_alert_at: int | None = None
    qualified_at: int | None = None
    qualification_count: int = 0
    observations: tuple[LegacyObservation, ...] = ()
    safety_history: tuple[str, ...] = ()
    paper_entries: int = 0
    paper_exits: int = 0

    @property
    def has_any_history(self) -> bool:
        """Whether anything at all was persisted for this mint."""

        return any(
            (
                self.first_seen_at,
                self.radar_first_seen_at,
                self.first_discord_visible_at,
                self.last_seen_at,
                self.alert_count,
                self.qualification_count,
                self.observations,
            )
        )


@dataclass(frozen=True, slots=True)
class BackfillResult:
    """A reconstructed lifecycle plus an honest account of its completeness."""

    lifecycle: TokenLifecycle | None = None
    completeness: str = UNKNOWN
    recovered: tuple[str, ...] = ()
    missing: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reconstructed(self) -> bool:
        return self.lifecycle is not None


def reconstruct_lifecycle(
    evidence: LegacyEvidence,
    *,
    now: int,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> BackfillResult:
    """Rebuild a lifecycle record from persisted history.

    Returns an empty result when the mint has no stored history at all — a
    genuinely unseen token must still become ``FIRST_DISCOVERY • FRESH``.
    """

    if not evidence.has_any_history:
        return BackfillResult(completeness=UNKNOWN, missing=("no persisted history",))

    recovered: list[str] = []
    missing: list[str] = []

    # --- earliest known times.  Only real persisted timestamps qualify. ----
    discovery_times = [
        value
        for value in (
            evidence.radar_first_seen_at,
            evidence.first_seen_at,
            evidence.observations[0].observed_at if evidence.observations else None,
        )
        if value
    ]
    first_discovered = min(discovery_times) if discovery_times else None
    if first_discovered is None:
        return BackfillResult(completeness=UNKNOWN, missing=("no usable discovery time",))
    recovered.append("first_discovered_at")

    first_seen = min(value for value in (evidence.first_seen_at, first_discovered) if value)

    # --- first surface: the first time this actually reached Discord ------
    surfaced_at = evidence.first_discord_visible_at or evidence.first_alert_at
    surface_market_cap = evidence.first_visible_market_cap_usd
    surface_price = None
    if surfaced_at:
        recovered.append("first_surfaced_at")
        surface_price = _price_at(evidence.observations, surfaced_at)
        if surface_market_cap is None:
            surface_market_cap = _market_cap_at(evidence.observations, surfaced_at)
        if surface_market_cap is not None:
            recovered.append("first_surface_market_cap_usd")
        else:
            missing.append("first_surface_market_cap_usd")
    else:
        missing.append("first_surfaced_at")

    # --- historical peak ---------------------------------------------------
    observed_caps = [
        item.market_cap_usd for item in evidence.observations if item.market_cap_usd is not None
    ]
    peak_market_cap = _max(evidence.peak_market_cap_usd, max(observed_caps, default=None))
    if peak_market_cap is not None:
        recovered.append("historical_high_market_cap_usd")
    else:
        missing.append("historical_high_market_cap_usd")

    observed_prices = [
        item.price_usd for item in evidence.observations if item.price_usd is not None
    ]
    peak_price = max(observed_prices, default=None)
    peak_at = _peak_time(evidence.observations, peak_price)
    if peak_price is not None:
        recovered.append("historical_high_price_usd")
    else:
        missing.append("historical_high_price_usd")

    last_price = observed_prices[-1] if observed_prices else None
    last_market_cap = observed_caps[-1] if observed_caps else None

    # --- derived return and drawdown, from real numbers only --------------
    surface_base = surface_market_cap or evidence.first_market_cap_usd
    max_return = _percent_change(peak_market_cap, surface_base)
    current_drawdown = _drawdown(last_price, peak_price)
    if current_drawdown is None:
        current_drawdown = _drawdown(last_market_cap, peak_market_cap)
    max_drawdown = current_drawdown
    for item in evidence.observations:
        candidate = _drawdown(item.price_usd, peak_price) or _drawdown(
            item.market_cap_usd, peak_market_cap
        )
        max_drawdown = _max(max_drawdown, candidate)

    if evidence.alert_count:
        recovered.append("publications")
    if evidence.qualification_count:
        recovered.append("qualification_count")
    if evidence.safety_history:
        recovered.append("safety_history")
    else:
        missing.append("safety_history")

    state = _reconstruct_state(
        surfaced_at=surfaced_at,
        qualified_at=evidence.qualified_at,
        max_return_percent=max_return,
        drawdown_percent=current_drawdown,
        config=config,
    )
    cycle_count = 1 if state in {RETRACED, COOLDOWN} else 0

    lifecycle = TokenLifecycle(
        mint=evidence.mint,
        state=state,
        first_discovered_at=first_discovered,
        first_seen_at=first_seen,
        first_surfaced_at=surfaced_at,
        first_surface_price_usd=surface_price,
        first_surface_market_cap_usd=surface_market_cap,
        historical_high_price_usd=peak_price,
        historical_high_market_cap_usd=peak_market_cap,
        historical_high_at=peak_at,
        max_return_from_surface_percent=max_return,
        max_drawdown_percent=max_drawdown,
        current_drawdown_percent=current_drawdown,
        last_price_usd=last_price,
        last_market_cap_usd=last_market_cap,
        last_observed_at=evidence.last_seen_at or first_discovered,
        last_qualified_at=evidence.qualified_at,
        last_alert_at=evidence.last_alert_at,
        publications=evidence.alert_count,
        qualification_count=evidence.qualification_count,
        cycle_count=cycle_count,
        paper_entries=evidence.paper_entries,
        paper_exits=evidence.paper_exits,
        safety_history=evidence.safety_history[-20:],
        state_history=((first_discovered, FIRST_DISCOVERY), (now, state))
        if state != FIRST_DISCOVERY
        else ((first_discovered, FIRST_DISCOVERY),),
        notes={
            "source": BACKFILL_NOTE,
            "backfilled_at": now,
            "recovered": sorted(set(recovered)),
            "missing": sorted(set(missing)),
        },
    )
    completeness = COMPLETE if not missing else PARTIAL
    return BackfillResult(
        lifecycle=lifecycle,
        completeness=completeness,
        recovered=tuple(dict.fromkeys(recovered)),
        missing=tuple(dict.fromkeys(missing)),
    )


def merge_backfill(
    existing: TokenLifecycle,
    reconstructed: TokenLifecycle,
) -> TokenLifecycle:
    """Fold recovered history into a record that was already created live.

    Only ever moves the earliest timestamps *earlier* and the high-water marks
    *higher*, so running the backfill twice — or after the lab already saw the
    token — cannot rewrite history in the wrong direction.
    """

    return replace(
        existing,
        first_discovered_at=min(
            value
            for value in (existing.first_discovered_at, reconstructed.first_discovered_at)
            if value
        ),
        first_seen_at=min(
            value for value in (existing.first_seen_at, reconstructed.first_seen_at) if value
        ),
        first_surfaced_at=_earliest(
            existing.first_surfaced_at, reconstructed.first_surfaced_at
        ),
        first_surface_price_usd=(
            existing.first_surface_price_usd or reconstructed.first_surface_price_usd
        ),
        first_surface_market_cap_usd=(
            existing.first_surface_market_cap_usd
            or reconstructed.first_surface_market_cap_usd
        ),
        historical_high_price_usd=_max(
            existing.historical_high_price_usd, reconstructed.historical_high_price_usd
        ),
        historical_high_market_cap_usd=_max(
            existing.historical_high_market_cap_usd,
            reconstructed.historical_high_market_cap_usd,
        ),
        max_return_from_surface_percent=_max(
            existing.max_return_from_surface_percent,
            reconstructed.max_return_from_surface_percent,
        ),
        max_drawdown_percent=_max(
            existing.max_drawdown_percent, reconstructed.max_drawdown_percent
        ),
        publications=max(existing.publications, reconstructed.publications),
        qualification_count=max(
            existing.qualification_count, reconstructed.qualification_count
        ),
        cycle_count=max(existing.cycle_count, reconstructed.cycle_count),
        paper_entries=max(existing.paper_entries, reconstructed.paper_entries),
        paper_exits=max(existing.paper_exits, reconstructed.paper_exits),
        last_alert_at=_latest(existing.last_alert_at, reconstructed.last_alert_at),
        last_qualified_at=_latest(
            existing.last_qualified_at, reconstructed.last_qualified_at
        ),
        notes={**reconstructed.notes, **existing.notes},
    )


def _reconstruct_state(
    *,
    surfaced_at: int | None,
    qualified_at: int | None,
    max_return_percent: Decimal | None,
    drawdown_percent: Decimal | None,
    config: LabConfig,
) -> str:
    """Derive the lifecycle state the recovered history implies.

    Uses the same thresholds as the live state machine, so a backfilled token
    lands where a live one with the same history would have.
    """

    if drawdown_percent is not None:
        if drawdown_percent >= config.retraced_drawdown_percent:
            return RETRACED
        if drawdown_percent >= config.exhaustion_drawdown_percent:
            return RETRACED if qualified_at else SILENT_WATCH
    if max_return_percent is not None and max_return_percent >= config.winner_return_percent:
        return WINNER
    if qualified_at:
        return ACTIVE_SETUP if surfaced_at else FIRST_QUALIFIED
    if surfaced_at:
        return SILENT_WATCH
    return FIRST_DISCOVERY


def _price_at(observations: Sequence[LegacyObservation], at: int) -> Decimal | None:
    best: Decimal | None = None
    for item in observations:
        if item.observed_at <= at and item.price_usd is not None:
            best = item.price_usd
    return best


def _market_cap_at(observations: Sequence[LegacyObservation], at: int) -> Decimal | None:
    best: Decimal | None = None
    for item in observations:
        if item.observed_at <= at and item.market_cap_usd is not None:
            best = item.market_cap_usd
    return best


def _peak_time(observations: Sequence[LegacyObservation], peak: Decimal | None) -> int | None:
    if peak is None:
        return None
    for item in observations:
        if item.price_usd == peak:
            return item.observed_at
    return None


def _percent_change(current: Decimal | None, base: Decimal | None) -> Decimal | None:
    if current is None or base is None or base <= 0:
        return None
    return ((current - base) / base * HUNDRED).quantize(Decimal("0.01"))


def _drawdown(current: Decimal | None, peak: Decimal | None) -> Decimal | None:
    if current is None or peak is None or peak <= 0:
        return None
    return max(ZERO, ((peak - current) / peak * HUNDRED)).quantize(Decimal("0.01"))


def _max(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _earliest(left: int | None, right: int | None) -> int | None:
    values = [value for value in (left, right) if value]
    return min(values) if values else None


def _latest(left: int | None, right: int | None) -> int | None:
    values = [value for value in (left, right) if value]
    return max(values) if values else None
