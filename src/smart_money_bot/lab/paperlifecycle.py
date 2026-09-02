"""What happens after the alert, which is where the money is actually lost.

An entry card is the start of an obligation, not the end of one.  The operator
has been told about a token; the conditions that justified telling them can stop
holding thirty seconds later, and saying nothing at that point is worse than
never having sent the card — it leaves someone holding a position the bot has
quietly stopped believing in.

So every entry candidate becomes a paper observation that keeps being asked the
same question: *would this still be an entry now?*  Five states, and the
transitions between them are driven by evidence rather than by price alone:

    ENTRY CANDIDATE      every hard gate passed
    HOLD                 they still do, and the move is intact
    PROFIT PROTECTION    it ran; the risk is now giving it back
    EXIT RISK            somebody is leaving and it is not the crowd
    INVALIDATED          the reason to be here is gone

Two design choices worth stating.

**Sellability is re-asked, not remembered.**  The most dangerous change after an
entry is not the price falling — it is the exit closing while the price looks
fine.  A liquidity withdrawal, a failing sell quote or a collapsing executable
depth is an ``INVALIDATED`` regardless of what the chart is doing.

**First-seen and entry evidence are immutable.**  A later observation may not
rewrite what we knew at entry, because "was this a good call?" is only
answerable if the snapshot that produced it survives being wrong.  Every
transition records its own evidence and leaves the entry alone.

Research and paper only.  Nothing here can buy, sell, sign or broadcast, and
there is deliberately no code path that could.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal

ZERO = Decimal("0")
HUNDRED = Decimal("100")
CENT = Decimal("0.01")

# --- states -------------------------------------------------------------------
ENTRY = "ENTRY_CANDIDATE_RESEARCH"
HOLD = "HOLD_MOMENTUM_INTACT_PAPER"
PROFIT_PROTECTION = "PROFIT_PROTECTION_WATCH_PAPER"
EXIT_RISK = "EXIT_RISK_DISTRIBUTION_PAPER"
INVALIDATED = "INVALIDATED_PAPER"

STATES: tuple[str, ...] = (ENTRY, HOLD, PROFIT_PROTECTION, EXIT_RISK, INVALIDATED)
#: Once here, the observation is closed and never reopens.
TERMINAL: frozenset[str] = frozenset({INVALIDATED})

LABEL: dict[str, str] = {
    ENTRY: "🟢 ENTRY CANDIDATE — RESEARCH",
    HOLD: "🟡 HOLD / MOMENTUM INTACT — PAPER",
    PROFIT_PROTECTION: "🔵 PROFIT-PROTECTION WATCH — PAPER",
    EXIT_RISK: "🟠 EXIT RISK / DISTRIBUTION — PAPER",
    INVALIDATED: "🔴 INVALIDATED / SELL ROUTE OR LIQUIDITY FAILURE — PAPER",
}

# --- reasons ------------------------------------------------------------------
R_SELL_ROUTE_LOST = "SELL_ROUTE_LOST"
R_LIQUIDITY_REMOVED = "LIQUIDITY_REMOVED"
R_DEPTH_COLLAPSED = "EXECUTABLE_DEPTH_COLLAPSED"
R_GATES_FAILED = "REQUIRED_GATES_NO_LONGER_PASS"
R_DISTRIBUTION = "INSIDER_OR_LARGE_HOLDER_DISTRIBUTION"
R_SELL_ACCELERATION = "INDEPENDENT_SELLING_ACCELERATING"
R_MOMENTUM_BREAKDOWN = "MOMENTUM_BROKE_DOWN"
R_TRAILING_STOP = "TRAILING_INVALIDATION_HIT"
R_EDGE_EXHAUSTED = "EDGE_EXHAUSTED"
R_STALE_EVIDENCE = "EVIDENCE_TOO_STALE_TO_JUDGE"
R_RUNNING = "STILL_RUNNING"
R_INTACT = "CONDITIONS_STILL_HOLD"

HUMAN_REASON: dict[str, str] = {
    R_SELL_ROUTE_LOST: "the sell route stopped working — the exit closed",
    R_LIQUIDITY_REMOVED: "liquidity was pulled out of the pool",
    R_DEPTH_COLLAPSED: "executable depth collapsed; getting out now costs too much",
    R_GATES_FAILED: "the evidence that justified this entry no longer passes",
    R_DISTRIBUTION: "creator, insider or large holders are selling into the move",
    R_SELL_ACCELERATION: "independent selling is accelerating against the buying",
    R_MOMENTUM_BREAKDOWN: "the move broke down",
    R_TRAILING_STOP: "gave back more of the move than the trailing invalidation allows",
    R_EDGE_EXHAUSTED: "the move is spent; there is no edge left to take",
    R_STALE_EVIDENCE: "the data went stale — this can no longer be judged safely",
    R_RUNNING: "still running",
    R_INTACT: "conditions still hold",
}


@dataclass(frozen=True, slots=True)
class LifecycleConfig:
    """When a paper observation changes state."""

    #: Above this gain the risk becomes giving it back rather than being wrong.
    profit_protection_gain: Decimal = Decimal("0.5")
    #: Give back this much of the best gain and the move is over.
    trailing_giveback: Decimal = Decimal("0.4")
    #: Sell volume over buy volume in the latest window.
    distribution_ratio: Decimal = Decimal("1.6")
    #: Loss below the entry that ends the observation.
    max_drawdown: Decimal = Decimal("0.35")
    #: Evidence older than this cannot support a HOLD.
    max_evidence_age_seconds: int = 600


DEFAULT_LIFECYCLE_CONFIG = LifecycleConfig()


@dataclass(frozen=True, slots=True)
class Observation:
    """One reading of a live paper position.  No orders, ever."""

    at: int
    price_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    #: Whether a fresh reverse quote still returns a route.
    sell_route_ok: bool | None = None
    #: Independently computed liquidity, from reserves.
    liquidity_usd: Decimal | None = None
    #: Executable impact at the paper size, as a rate.
    sell_impact: Decimal | None = None
    sell_to_buy_ratio: Decimal | None = None
    insider_selling: bool = False
    #: Whether the hard gates still pass on current evidence.
    gates_pass: bool | None = None
    evidence_age_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class PaperPosition:
    """An entry candidate being followed.  Entry evidence is immutable.

    ``entry_price_usd``, ``entry_at`` and ``entry_liquidity_usd`` are written
    once at entry and never recomputed.  A later observation that would improve
    them is a later observation, not a correction — and the whole point of
    keeping them is to be able to see, afterwards, what was actually known at
    the time.
    """

    mint: str
    entry_at: int
    entry_price_usd: Decimal | None = None
    entry_market_cap_usd: Decimal | None = None
    entry_liquidity_usd: Decimal | None = None
    state: str = ENTRY
    #: Best price seen since entry, for maximum favourable excursion.
    peak_price_usd: Decimal | None = None
    trough_price_usd: Decimal | None = None
    last_at: int | None = None
    last_price_usd: Decimal | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)
    #: Every state actually published, so a restart cannot repeat one.
    published_states: tuple[str, ...] = ()

    # ---- research-only arithmetic -------------------------------------
    @property
    def paper_return(self) -> Decimal | None:
        if not self.entry_price_usd or self.last_price_usd is None:
            return None
        if self.entry_price_usd <= ZERO:
            return None
        return ((self.last_price_usd / self.entry_price_usd) - 1).quantize(Decimal("0.0001"))

    @property
    def mfe(self) -> Decimal | None:
        """Maximum favourable excursion: the best it ever looked."""

        if not self.entry_price_usd or self.peak_price_usd is None:
            return None
        if self.entry_price_usd <= ZERO:
            return None
        return ((self.peak_price_usd / self.entry_price_usd) - 1).quantize(Decimal("0.0001"))

    @property
    def max_drawdown(self) -> Decimal | None:
        if not self.entry_price_usd or self.trough_price_usd is None:
            return None
        if self.entry_price_usd <= ZERO:
            return None
        return ((self.trough_price_usd / self.entry_price_usd) - 1).quantize(Decimal("0.0001"))

    @property
    def giveback(self) -> Decimal | None:
        """How much of the best gain has been handed back."""

        if self.peak_price_usd is None or self.last_price_usd is None:
            return None
        if self.peak_price_usd <= ZERO:
            return None
        return ((self.peak_price_usd - self.last_price_usd) / self.peak_price_usd).quantize(
            Decimal("0.0001")
        )

    @property
    def closed(self) -> bool:
        return self.state in TERMINAL

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "state": self.state,
            "label": LABEL.get(self.state, self.state),
            "entry_at": self.entry_at,
            "entry_price_usd": _s(self.entry_price_usd),
            "entry_market_cap_usd": _s(self.entry_market_cap_usd),
            "entry_liquidity_usd": _s(self.entry_liquidity_usd),
            "last_price_usd": _s(self.last_price_usd),
            "paper_return": _s(self.paper_return),
            "mfe": _s(self.mfe),
            "max_drawdown": _s(self.max_drawdown),
            "giveback": _s(self.giveback),
            "reasons": [HUMAN_REASON.get(item, item) for item in self.reasons],
            "reason_codes": list(self.reasons),
            "published_states": list(self.published_states),
            "closed": self.closed,
            "research_only": True,
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def open_position(
    mint: str,
    *,
    at: int,
    price_usd: Decimal | None,
    market_cap_usd: Decimal | None = None,
    liquidity_usd: Decimal | None = None,
) -> PaperPosition:
    """Record the entry.  These values are never rewritten afterwards."""

    return PaperPosition(
        mint=mint,
        entry_at=at,
        entry_price_usd=price_usd,
        entry_market_cap_usd=market_cap_usd,
        entry_liquidity_usd=liquidity_usd,
        state=ENTRY,
        peak_price_usd=price_usd,
        trough_price_usd=price_usd,
        last_at=at,
        last_price_usd=price_usd,
        published_states=(ENTRY,),
    )


def observe(
    position: PaperPosition,
    reading: Observation,
    *,
    config: LifecycleConfig = DEFAULT_LIFECYCLE_CONFIG,
) -> PaperPosition:
    """Advance the observation.  Returns the position, changed or not.

    The order of tests is the order of severity, and it is deliberate: an exit
    that has closed matters more than a price that has fallen, because a
    position you cannot leave is a different problem from one that is down.
    """

    if position.closed:
        return position

    price = reading.price_usd if reading.price_usd is not None else position.last_price_usd
    peak = _max(position.peak_price_usd, price)
    trough = _min(position.trough_price_usd, price)
    moved = replace(
        position,
        last_at=reading.at,
        last_price_usd=price,
        peak_price_usd=peak,
        trough_price_usd=trough,
    )

    # --- the exit closing outranks everything the chart is doing ---------
    if reading.sell_route_ok is False:
        return _to(moved, INVALIDATED, R_SELL_ROUTE_LOST)
    if (
        position.entry_liquidity_usd
        and reading.liquidity_usd is not None
        and position.entry_liquidity_usd > ZERO
        and (reading.liquidity_usd / position.entry_liquidity_usd) < Decimal("0.5")
    ):
        return _to(moved, INVALIDATED, R_LIQUIDITY_REMOVED)
    if reading.sell_impact is not None and reading.sell_impact > Decimal("0.35"):
        return _to(moved, INVALIDATED, R_DEPTH_COLLAPSED)
    if reading.gates_pass is False:
        return _to(moved, INVALIDATED, R_GATES_FAILED)

    drawdown = moved.paper_return
    if drawdown is not None and drawdown <= -config.max_drawdown:
        return _to(moved, INVALIDATED, R_MOMENTUM_BREAKDOWN)

    if (
        reading.evidence_age_seconds is not None
        and reading.evidence_age_seconds > config.max_evidence_age_seconds
    ):
        # Not invalidated — we simply cannot see well enough to keep saying
        # hold, and saying so is more honest than a stale reassurance.
        return _to(moved, EXIT_RISK, R_STALE_EVIDENCE)

    # --- somebody leaving ------------------------------------------------
    if reading.insider_selling:
        return _to(moved, EXIT_RISK, R_DISTRIBUTION)
    if (
        reading.sell_to_buy_ratio is not None
        and reading.sell_to_buy_ratio > config.distribution_ratio
    ):
        return _to(moved, EXIT_RISK, R_SELL_ACCELERATION)

    # --- giving back a win ------------------------------------------------
    giveback = moved.giveback
    mfe = moved.mfe
    if (
        mfe is not None
        and mfe >= config.profit_protection_gain
        and giveback is not None
        and giveback >= config.trailing_giveback
    ):
        return _to(moved, EXIT_RISK, R_TRAILING_STOP)
    if mfe is not None and mfe >= config.profit_protection_gain:
        return _to(moved, PROFIT_PROTECTION, R_RUNNING)

    return _to(moved, HOLD, R_INTACT)


def _to(position: PaperPosition, state: str, reason: str) -> PaperPosition:
    """Move to a state, recording it once.  Repeats never re-publish."""

    already = state in position.published_states
    return replace(
        position,
        state=state,
        reasons=(reason,),
        published_states=position.published_states if already
        else (*position.published_states, state),
    )


def should_publish(before: PaperPosition, after: PaperPosition) -> bool:
    """Whether this transition earns a card.

    Deduplicated by state: a position that sits in HOLD for an hour produces
    one card, not sixty.  A *return* to a state already published is also
    silent — the operator has been told, and telling them again is noise.
    """

    if before.state == after.state:
        return False
    return len(after.published_states) > len(before.published_states)


def summarise(position: PaperPosition) -> str:
    """One line for the card, in the operator's units."""

    parts = [LABEL.get(position.state, position.state)]
    if position.paper_return is not None:
        parts.append(f"paper {(position.paper_return * HUNDRED).quantize(CENT)}%")
    if position.mfe is not None:
        parts.append(f"best {(position.mfe * HUNDRED).quantize(CENT)}%")
    if position.max_drawdown is not None:
        parts.append(f"worst {(position.max_drawdown * HUNDRED).quantize(CENT)}%")
    for code in position.reasons:
        parts.append(HUMAN_REASON.get(code, code))
    return " • ".join(parts)


def replay(
    position: PaperPosition,
    readings: Sequence[Observation],
    *,
    config: LifecycleConfig = DEFAULT_LIFECYCLE_CONFIG,
) -> tuple[PaperPosition, tuple[str, ...]]:
    """Apply readings in order, returning the position and the cards published.

    Used by the tests and by restart recovery.  Replaying the same readings
    twice must produce the same cards once, which is what makes a restart safe.
    """

    published: list[str] = []
    current = position
    for reading in sorted(readings, key=lambda item: item.at):
        nxt = observe(current, reading, config=config)
        if should_publish(current, nxt):
            published.append(nxt.state)
        current = nxt
    return current, tuple(published)


def _max(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    values = [item for item in (left, right) if item is not None]
    return max(values) if values else None


def _min(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    values = [item for item in (left, right) if item is not None]
    return min(values) if values else None
