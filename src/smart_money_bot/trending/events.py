"""First-class Trending signal states (section 7).

The point of naming these is that "it is on the board" is not a signal.  A token
sitting at #2 unchanged for six hours and a token that went ``#40 → #22 → #8`` in
four minutes are both "trending", and only one of them is information.  Every
state below is derived from *movement* — rank velocity, market-cap acceleration,
holder growth, time on the board — rather than from absolute rank, so a high
static rank can never masquerade as alpha (section 95).

The states are deliberately descriptive, not prescriptive: they say what the
board is doing, and the scoring and alerting layers decide separately whether
that is worth interrupting a human for.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .ledger import RankVelocity, TrendingLedgerEntry

ZERO = Decimal("0")

# --- the states --------------------------------------------------------------
#: The mint just appeared on the board for the first time.
TRENDING_NEW_ENTRY = "TRENDING_NEW_ENTRY"
#: Rank is improving, but not fast enough to call it acceleration.
TRENDING_RANK_RISING = "TRENDING_RANK_RISING"
#: Rank *and* market cap are moving together, quickly.
TRENDING_ACCELERATING = "TRENDING_ACCELERATING"
#: Established on the board, holding its place, nothing breaking.
TRENDING_HEALTHY = "TRENDING_HEALTHY"
#: Already large / already moved, but a fresh leg is developing (sections 11-12).
TRENDING_CONTINUATION = "TRENDING_CONTINUATION"
#: Left the board and came back.
TRENDING_REENTRY = "TRENDING_REENTRY"
#: Losing ground slowly.
TRENDING_COOLING = "TRENDING_COOLING"
#: Losing ground decisively.
TRENDING_FADING = "TRENDING_FADING"
#: Gone from the board.
TRENDING_EXITED = "TRENDING_EXITED"
#: Still on the board, but the move an early entry would have wanted is over.
TRENDING_EDGE_CONSUMED = "TRENDING_EDGE_CONSUMED"

TRENDING_STATES: tuple[str, ...] = (
    TRENDING_NEW_ENTRY,
    TRENDING_RANK_RISING,
    TRENDING_ACCELERATING,
    TRENDING_HEALTHY,
    TRENDING_CONTINUATION,
    TRENDING_REENTRY,
    TRENDING_COOLING,
    TRENDING_FADING,
    TRENDING_EXITED,
    TRENDING_EDGE_CONSUMED,
)

#: States where attention is still building.  Not "safe", not "buy" — building.
STRENGTHENING_STATES: frozenset[str] = frozenset(
    {
        TRENDING_NEW_ENTRY,
        TRENDING_RANK_RISING,
        TRENDING_ACCELERATING,
        TRENDING_CONTINUATION,
        TRENDING_REENTRY,
    }
)

#: States where attention is leaving.
WEAKENING_STATES: frozenset[str] = frozenset(
    {TRENDING_COOLING, TRENDING_FADING, TRENDING_EXITED}
)

STATE_LABELS: dict[str, str] = {
    TRENDING_NEW_ENTRY: "NEW TRENDING ENTRY",
    TRENDING_RANK_RISING: "RANK RISING",
    TRENDING_ACCELERATING: "ACCELERATING",
    TRENDING_HEALTHY: "HEALTHY",
    TRENDING_CONTINUATION: "CONTINUATION / SECOND LEG",
    TRENDING_REENTRY: "RE-ENTERED TRENDING",
    TRENDING_COOLING: "COOLING",
    TRENDING_FADING: "FADING",
    TRENDING_EXITED: "LEFT TRENDING",
    TRENDING_EDGE_CONSUMED: "EDGE CONSUMED",
}


@dataclass(frozen=True, slots=True)
class TrendingEventConfig:
    """Bounded, auditable thresholds.  No magic numbers live in the logic."""

    #: How long after first appearing a mint still counts as a new entrant.
    new_entry_seconds: int = 600
    #: Rank places gained per minute that counts as acceleration.
    accelerating_rank_per_minute: Decimal = Decimal("1.5")
    #: Rank places gained (any speed) that counts as rising.
    rising_rank_delta: int = 2
    #: Market-cap percent per minute that supports acceleration.
    accelerating_mc_per_minute: Decimal = Decimal("1.0")
    #: Rank places lost before we call it cooling / fading.
    cooling_rank_delta: int = 3
    fading_rank_delta: int = 10
    #: Move since Trending entry beyond which an *early* entry is no longer early.
    edge_consumed_move_percent: Decimal = Decimal("80")
    #: Move beyond which a continuation must carry genuinely new evidence.
    continuation_move_percent: Decimal = Decimal("35")
    #: A market cap above which "early" is simply not an honest word (section 11).
    already_large_market_cap_usd: Decimal = Decimal("500000")


DEFAULT_EVENT_CONFIG = TrendingEventConfig()


@dataclass(frozen=True, slots=True)
class TrendingEvent:
    """A classified state plus the evidence that produced it."""

    mint: str
    state: str
    reasons: tuple[str, ...] = ()
    rank_velocity: RankVelocity | None = None
    market_cap_velocity: Decimal | None = None
    move_since_entry_percent: Decimal | None = None
    already_large: bool = False
    at: int = 0

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def strengthening(self) -> bool:
        return self.state in STRENGTHENING_STATES

    @property
    def weakening(self) -> bool:
        return self.state in WEAKENING_STATES

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "state": self.state,
            "reasons": list(self.reasons),
            "rank_velocity": self.rank_velocity.to_json() if self.rank_velocity else None,
            "market_cap_velocity": (
                None if self.market_cap_velocity is None else str(self.market_cap_velocity)
            ),
            "move_since_entry_percent": (
                None
                if self.move_since_entry_percent is None
                else str(self.move_since_entry_percent)
            ),
            "already_large": self.already_large,
            "at": self.at,
        }


def classify_trending_event(
    entry: TrendingLedgerEntry,
    velocity: RankVelocity,
    *,
    now: int,
    market_cap_velocity: Decimal | None = None,
    holder_growth: int | None = None,
    has_new_evidence: bool = False,
    config: TrendingEventConfig = DEFAULT_EVENT_CONFIG,
) -> TrendingEvent:
    """Turn a ledger entry plus its rank velocity into one named state.

    ``has_new_evidence`` is the continuation gate (section 51).  A token that has
    already run is only re-surfaced when something genuinely *new* arrived — a
    fresh supported thesis, a new catalyst, renewed accumulation, holder
    acceleration.  "It pumped, therefore buy" is not new evidence and cannot
    reach ``TRENDING_CONTINUATION`` through this function.
    """

    reasons: list[str] = []
    move = entry.market_cap_move_percent()
    already_large = (
        entry.current_market_cap_usd is not None
        and entry.current_market_cap_usd >= config.already_large_market_cap_usd
    )
    seconds = entry.seconds_trending(now=now)

    def build(state: str) -> TrendingEvent:
        return TrendingEvent(
            mint=entry.mint,
            state=state,
            reasons=tuple(reasons),
            rank_velocity=velocity,
            market_cap_velocity=market_cap_velocity,
            move_since_entry_percent=move,
            already_large=already_large,
            at=now,
        )

    if not entry.on_board:
        reasons.append("no longer on the Trending board")
        return build(TRENDING_EXITED)

    accelerating = (
        velocity.per_minute >= config.accelerating_rank_per_minute
        or (
            velocity.climbing
            and market_cap_velocity is not None
            and market_cap_velocity >= config.accelerating_mc_per_minute
        )
    )
    rising = velocity.delta >= config.rising_rank_delta

    # A token that has already made its move cannot be sold to the operator as
    # early.  It can still be a *continuation*, but only on new evidence.
    edge_consumed = move is not None and move >= config.edge_consumed_move_percent
    continuation_zone = already_large or (
        move is not None and move >= config.continuation_move_percent
    )

    if continuation_zone and has_new_evidence and (accelerating or rising):
        if already_large:
            reasons.append("already large — this is not early")
        if move is not None:
            reasons.append(f"already moved {move:+.1f}% since entering Trending")
        reasons.append("new evidence arrived after the first leg")
        if accelerating:
            reasons.append(f"rank accelerating again ({velocity.delta:+d} places)")
        elif rising:
            reasons.append(f"rank climbing again ({velocity.delta:+d} places)")
        if holder_growth is not None and holder_growth > 0:
            reasons.append(f"holders +{holder_growth}")
        return build(TRENDING_CONTINUATION)

    if edge_consumed:
        reasons.append(
            f"moved {move:+.1f}% since entering Trending — an early entry is gone"
            if move is not None
            else "the early move is gone"
        )
        if not has_new_evidence:
            reasons.append("no new evidence to justify a second leg")
        return build(TRENDING_EDGE_CONSUMED)

    if entry.entries > 1 and seconds <= config.new_entry_seconds:
        reasons.append(f"re-entered Trending (stint {entry.entries})")
        if velocity.climbing:
            reasons.append(f"rank climbing ({velocity.delta:+d} places)")
        return build(TRENDING_REENTRY)

    if entry.entries == 1 and seconds <= config.new_entry_seconds:
        reasons.append(f"new Trending entrant {seconds}s ago")
        if entry.first_rank is not None:
            reasons.append(f"entered at #{entry.first_rank}")
        if accelerating:
            reasons.append(f"rank accelerating ({velocity.per_minute}/min)")
        return build(TRENDING_NEW_ENTRY)

    if accelerating:
        reasons.append(f"rank accelerating ({velocity.delta:+d} places, {velocity.per_minute}/min)")
        if market_cap_velocity is not None:
            reasons.append(f"market cap {market_cap_velocity:+}%/min")
        if holder_growth is not None and holder_growth > 0:
            reasons.append(f"holders +{holder_growth}")
        return build(TRENDING_ACCELERATING)

    if rising:
        reasons.append(f"rank climbing ({velocity.delta:+d} places)")
        return build(TRENDING_RANK_RISING)

    if velocity.delta <= -config.fading_rank_delta:
        reasons.append(f"rank falling hard ({velocity.delta:+d} places)")
        return build(TRENDING_FADING)

    if velocity.delta <= -config.cooling_rank_delta:
        reasons.append(f"rank slipping ({velocity.delta:+d} places)")
        return build(TRENDING_COOLING)

    reasons.append(
        f"holding #{entry.current_rank}" if entry.current_rank else "on the board, rank unchanged"
    )
    reasons.append("no rank acceleration — a high rank alone is not a signal")
    return build(TRENDING_HEALTHY)
