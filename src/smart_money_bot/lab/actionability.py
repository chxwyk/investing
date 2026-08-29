"""Is this opportunity still worth surfacing *now*? (sections 14-18)

The v2.36 opportunity score answers "how interesting was this setup?".  It is a
historical measurement and it stays exactly as it is.  What production showed is
that a high historical score keeps a candidate ranked next to genuinely fresh
setups long after its edge is gone — the JELLY case, already ~21% below first
seen with weakening flow, sitting in the normal research radar as though it were
actionable.

This module adds the missing second question — *current* actionability — without
touching any existing score, threshold or gate.  Historical opportunity is
persisted and still drives research; current actionability decides what belongs
in the current radar and in what order.

Nothing here is a trading gate.  A candidate the actionability model likes still
has to clear every PAPER entry requirement unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .config import DEFAULT_LAB_CONFIG, LabConfig

ZERO = Decimal("0")
HUNDRED = Decimal("100")

# --- current state (section 15) ---------------------------------------------
ACTIONABLE = "ACTIONABLE"
WATCH = "WATCH"
DETERIORATED = "DETERIORATED"
EDGE_CONSUMED = "EDGE_CONSUMED"
EXPIRED = "EXPIRED"
INVALIDATED = "INVALIDATED"

#: States that must not appear beside fresh setups in the current radar.
SUPPRESSED_STATES = frozenset({DETERIORATED, EDGE_CONSUMED, EXPIRED, INVALIDATED})

#: Lifecycle states that carry their own suppression meaning already.
LIFECYCLE_SUPPRESSED = frozenset(
    {"RETRACED", "COOLDOWN", "REENTRY_WATCH", "INVALIDATED", "EXHAUSTED"}
)

# --- reason codes ------------------------------------------------------------
R_NEGATIVE_SINCE_FIRST_SEEN = "NEGATIVE_SINCE_FIRST_SEEN"
R_MOMENTUM_COLLAPSED = "MOMENTUM_COLLAPSED"
R_FLOW_WEAKENING = "FLOW_WEAKENING"
R_DEEP_PEAK_DRAWDOWN = "DEEP_PEAK_DRAWDOWN"
R_SIGNAL_STALE = "SIGNAL_STALE"
R_MOVE_ALREADY_MADE = "MOVE_ALREADY_MADE"
R_LIQUIDITY_FADING = "LIQUIDITY_FADING"
R_BUYERS_FADING = "BUYERS_FADING"
R_VOLUME_FADING = "VOLUME_FADING"
R_LIFECYCLE_SUPPRESSED = "LIFECYCLE_SUPPRESSED"
R_NO_CURRENT_EVIDENCE = "NO_CURRENT_EVIDENCE"
R_ACCELERATING = "ACCELERATING"
R_FRESH_SIGNAL = "FRESH_SIGNAL"
R_BUYERS_GROWING = "BUYERS_GROWING"
R_LIQUIDITY_GROWING = "LIQUIDITY_GROWING"
R_FLOW_STRONG = "FLOW_STRONG"


@dataclass(frozen=True, slots=True)
class ActionabilityInputs:
    """Current evidence only.  Historical scores are deliberately not inputs."""

    now: int = 0
    first_seen_at: int | None = None
    signal_at: int | None = None

    return_since_first_seen_percent: Decimal | None = None
    return_since_first_surface_percent: Decimal | None = None
    drawdown_from_peak_percent: Decimal | None = None

    momentum_score: Decimal | None = None
    momentum_change: Decimal | None = None
    price_acceleration_ratio: Decimal | None = None

    buys: int | None = None
    sells: int | None = None
    flow_change_ratio: Decimal | None = None

    independent_buyer_change: int | None = None
    volume_change_ratio: Decimal | None = None
    liquidity_change_percent: Decimal | None = None
    holder_growth: int | None = None

    lifecycle_state: str = "FIRST_DISCOVERY"
    safety_status: str = "UNKNOWN"
    route_available: bool | None = None
    expected_net_edge_percent: Decimal | None = None

    @property
    def signal_age_seconds(self) -> int | None:
        reference = self.signal_at or self.first_seen_at
        if reference is None or not self.now:
            return None
        return max(0, self.now - reference)

    @property
    def buy_sell_ratio(self) -> Decimal | None:
        if self.sells is None or self.buys is None:
            return None
        if self.sells <= 0:
            return Decimal("99") if self.buys > 0 else None
        return (Decimal(self.buys) / Decimal(self.sells)).quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class Actionability:
    """How worth surfacing this candidate is *right now*."""

    score: Decimal = ZERO
    state: str = WATCH
    reasons: tuple[str, ...] = ()
    positives: tuple[str, ...] = ()
    evidence_present: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def suppressed(self) -> bool:
        """Whether this must be kept out of the *current* radar.

        Suppressed never means deleted: the candidate stays in results,
        lifecycle, replay, calibration and every forward observation.
        """

        return self.state in SUPPRESSED_STATES

    @property
    def label(self) -> str:
        return self.state.replace("_", " ")


def assess_actionability(
    inputs: ActionabilityInputs,
    *,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> Actionability:
    """Score current actionability from current evidence, 0-100.

    Deliberately multi-signal: a single "anything below -20% is dead" rule would
    throw away a healthy setup that dipped, and would keep a flat one whose
    demand has quietly evaporated.
    """

    reasons: list[str] = []
    positives: list[str] = []
    score = Decimal("50")
    present = False

    # --- lifecycle already decided this ----------------------------------
    if inputs.lifecycle_state == "INVALIDATED":
        return Actionability(
            score=ZERO,
            state=INVALIDATED,
            reasons=(R_LIFECYCLE_SUPPRESSED,),
            evidence_present=True,
        )
    if inputs.lifecycle_state in LIFECYCLE_SUPPRESSED:
        reasons.append(R_LIFECYCLE_SUPPRESSED)
        score -= 30

    # --- move since we first saw it ---------------------------------------
    since_seen = inputs.return_since_first_seen_percent
    if since_seen is not None:
        present = True
        if since_seen <= Decimal("-15"):
            reasons.append(R_NEGATIVE_SINCE_FIRST_SEEN)
            score -= 22
        elif since_seen <= Decimal("-5"):
            score -= 8
        elif since_seen >= Decimal("15"):
            score += 6

    # --- edge decay: the move already happened ----------------------------
    since_surface = inputs.return_since_first_surface_percent
    if since_surface is not None:
        present = True
        if since_surface >= config.max_expansion_from_first_surface_percent:
            reasons.append(R_MOVE_ALREADY_MADE)
            score -= 30
        elif since_surface >= config.max_move_since_signal_percent:
            reasons.append(R_MOVE_ALREADY_MADE)
            score -= 15

    drawdown = inputs.drawdown_from_peak_percent
    if drawdown is not None:
        present = True
        if drawdown >= config.retraced_drawdown_percent:
            reasons.append(R_DEEP_PEAK_DRAWDOWN)
            score -= 25
        elif drawdown >= config.exhaustion_drawdown_percent:
            reasons.append(R_DEEP_PEAK_DRAWDOWN)
            score -= 12

    # --- momentum ---------------------------------------------------------
    momentum = inputs.momentum_score
    if momentum is not None:
        present = True
        if momentum <= config.momentum_decay_exit_score:
            reasons.append(R_MOMENTUM_COLLAPSED)
            score -= 25
        elif momentum >= 65:
            positives.append(R_ACCELERATING)
            score += 12
    if inputs.momentum_change is not None:
        present = True
        if inputs.momentum_change <= Decimal("-15"):
            reasons.append(R_MOMENTUM_COLLAPSED)
            score -= 10
        elif inputs.momentum_change >= Decimal("10"):
            positives.append(R_ACCELERATING)
            score += 8

    # --- flow -------------------------------------------------------------
    ratio = inputs.buy_sell_ratio
    if ratio is not None:
        present = True
        if ratio <= config.flow_reversal_ratio:
            reasons.append(R_FLOW_WEAKENING)
            score -= 18
        elif ratio < Decimal("1"):
            reasons.append(R_FLOW_WEAKENING)
            score -= 8
        elif ratio >= Decimal("1.6"):
            positives.append(R_FLOW_STRONG)
            score += 10
    if inputs.flow_change_ratio is not None and inputs.flow_change_ratio <= Decimal("0.6"):
        reasons.append(R_FLOW_WEAKENING)
        score -= 8

    # --- demand trend ------------------------------------------------------
    if inputs.independent_buyer_change is not None:
        present = True
        if inputs.independent_buyer_change <= 0:
            reasons.append(R_BUYERS_FADING)
            score -= 8
        elif inputs.independent_buyer_change >= 3:
            positives.append(R_BUYERS_GROWING)
            score += 10
    if inputs.volume_change_ratio is not None:
        present = True
        if inputs.volume_change_ratio <= Decimal("0.5"):
            reasons.append(R_VOLUME_FADING)
            score -= 10
        elif inputs.volume_change_ratio >= Decimal("1.5"):
            score += 6
    if inputs.liquidity_change_percent is not None:
        present = True
        if inputs.liquidity_change_percent <= Decimal("-20"):
            reasons.append(R_LIQUIDITY_FADING)
            score -= 15
        elif inputs.liquidity_change_percent >= Decimal("10"):
            positives.append(R_LIQUIDITY_GROWING)
            score += 6

    # --- freshness ---------------------------------------------------------
    age = inputs.signal_age_seconds
    if age is not None:
        present = True
        if age > config.max_signal_age_seconds * 4:
            reasons.append(R_SIGNAL_STALE)
            score -= 20
        elif age > config.max_signal_age_seconds:
            reasons.append(R_SIGNAL_STALE)
            score -= 10
        elif age <= config.max_signal_age_seconds // 3:
            positives.append(R_FRESH_SIGNAL)
            score += 8

    if inputs.route_available is False:
        reasons.append(R_LIQUIDITY_FADING)
        score -= 20

    if not present:
        return Actionability(
            score=ZERO,
            state=WATCH,
            reasons=(R_NO_CURRENT_EVIDENCE,),
            evidence_present=False,
            notes=("No current evidence yet; ranked below anything measured",),
        )

    bounded = max(ZERO, min(HUNDRED, score)).quantize(Decimal("0.01"))
    state = _classify(bounded, tuple(dict.fromkeys(reasons)), inputs, config=config)
    return Actionability(
        score=bounded,
        state=state,
        reasons=tuple(dict.fromkeys(reasons)),
        positives=tuple(dict.fromkeys(positives)),
        evidence_present=True,
    )


def _classify(
    score: Decimal,
    reasons: tuple[str, ...],
    inputs: ActionabilityInputs,
    *,
    config: LabConfig,
) -> str:
    """Map the score plus the specific reasons onto a current state."""

    if inputs.lifecycle_state in LIFECYCLE_SUPPRESSED:
        return DETERIORATED

    # The move is gone: a large completed expansion, or a stale signal that has
    # already run.  This is the EDGE_CONSUMED case, not mere weakness.
    if R_MOVE_ALREADY_MADE in reasons:
        return EDGE_CONSUMED
    since_surface = inputs.return_since_first_surface_percent
    if (
        R_SIGNAL_STALE in reasons
        and since_surface is not None
        and since_surface >= config.max_move_since_signal_percent
    ):
        return EDGE_CONSUMED

    # The JELLY shape: materially negative from first seen, weakening flow or
    # momentum, and nothing new supporting it.
    negative = R_NEGATIVE_SINCE_FIRST_SEEN in reasons
    fading = {R_FLOW_WEAKENING, R_MOMENTUM_COLLAPSED, R_BUYERS_FADING}.intersection(reasons)
    if negative and fading:
        return DETERIORATED

    if R_DEEP_PEAK_DRAWDOWN in reasons and fading:
        return DETERIORATED

    age = inputs.signal_age_seconds
    if age is not None and age > config.max_signal_age_seconds * 8 and score < 45:
        return EXPIRED

    if score >= 60:
        return ACTIONABLE
    if score < 30:
        return DETERIORATED
    return WATCH


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """One candidate ranked by *current* edge, not historical score."""

    mint: str
    actionability: Actionability
    expected_net_edge_percent: Decimal | None = None
    historical_opportunity_score: Decimal = ZERO
    decision: str = "WAIT"
    lifecycle_state: str = "FIRST_DISCOVERY"

    @property
    def rank_key(self) -> tuple[int, Decimal, Decimal, Decimal]:
        """Suppressed last, then current actionability, then net edge.

        The historical opportunity score is the final tiebreak only, so a token
        that once scored 86 but has collapsed cannot outrank a genuinely
        accelerating new setup.
        """

        return (
            0 if self.actionability.suppressed else 1,
            self.actionability.score,
            self.expected_net_edge_percent or ZERO,
            self.historical_opportunity_score,
        )


def rank_by_current_edge(candidates: Sequence[RankedCandidate]) -> tuple[RankedCandidate, ...]:
    return tuple(sorted(candidates, key=lambda item: item.rank_key, reverse=True))


def split_current_radar(
    candidates: Sequence[RankedCandidate],
) -> tuple[tuple[RankedCandidate, ...], tuple[RankedCandidate, ...]]:
    """Split into what belongs in the current radar and what is suppressed.

    Suppressed candidates are returned, not discarded — the caller still stores
    them and still measures their forward outcomes.
    """

    ranked = rank_by_current_edge(candidates)
    current = tuple(item for item in ranked if not item.actionability.suppressed)
    suppressed = tuple(item for item in ranked if item.actionability.suppressed)
    return current, suppressed
