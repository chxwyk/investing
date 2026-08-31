"""The Trending ledger: what we saw, when we first saw it, and how fast it is moving.

Two invariants carry the whole module.

**Mint is identity (section 13).**  Every row is keyed by the exact mint.  A
name, a ticker, a story, an About blurb and an image are all attributes that
several unrelated tokens can share, so none of them may ever merge two rows or
carry evidence from one mint to another.

**First observations are immutable (sections 5, 8, 48).**  ``first_rank``,
``first_market_cap_usd`` and ``first_seen_at`` are written once and then frozen.
This is what makes "was the alert early?" answerable at all: if the entry
numbers could be rewritten during enrichment, every late alert would look early
in hindsight.  :meth:`TrendingLedgerEntry.observe` therefore returns a *new*
entry and refuses to move those fields, including on a Trending re-entry.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal

from .source import (
    CHANGE_WINDOW_UNKNOWN,
    TrendingSourceInfo,
    normalise_change_window,
)

ZERO = Decimal("0")
HUNDRED = Decimal("100")

#: Verification is recorded, never trusted (section 37).
VERIFIED_YES = "VERIFIED"
VERIFIED_NO = "NOT_VERIFIED"
VERIFIED_UNKNOWN = "UNKNOWN"


def _percent_change(before: Decimal | None, after: Decimal | None) -> Decimal | None:
    if before is None or after is None or before <= ZERO:
        return None
    return (after - before) / before * HUNDRED


@dataclass(frozen=True, slots=True)
class TrendingObservation:
    """One reading of one exact mint on the Trending board."""

    mint: str
    observed_at: int
    rank: int | None = None
    name: str = ""
    symbol: str = ""
    fomo_token_id: str = ""
    fomo_url: str = ""
    market_cap_usd: Decimal | None = None
    price_usd: Decimal | None = None
    #: The percentage the source displayed, exactly as displayed.
    displayed_change_percent: Decimal | None = None
    #: What window that percentage covers — ``CHANGE_WINDOW_UNKNOWN`` unless the
    #: source documents it.  We never guess (section 6).
    change_window: str = CHANGE_WINDOW_UNKNOWN
    liquidity_usd: Decimal | None = None
    holder_count: int | None = None
    top10_percent: Decimal | None = None
    verification: str = VERIFIED_UNKNOWN
    source: TrendingSourceInfo = field(default_factory=TrendingSourceInfo)

    def __post_init__(self) -> None:
        if not self.mint:
            raise ValueError("a Trending observation requires the exact mint")
        object.__setattr__(self, "change_window", normalise_change_window(self.change_window))
        if self.verification not in {VERIFIED_YES, VERIFIED_NO, VERIFIED_UNKNOWN}:
            object.__setattr__(self, "verification", VERIFIED_UNKNOWN)


@dataclass(frozen=True, slots=True)
class RankPoint:
    at: int
    rank: int


@dataclass(frozen=True, slots=True)
class TrendingLedgerEntry:
    """The persisted per-mint Trending record.  Entry numbers never move."""

    mint: str
    name: str = ""
    symbol: str = ""
    fomo_token_id: str = ""
    fomo_url: str = ""

    # ---- immutable first observation (sections 5, 8) ---------------------
    first_seen_at: int = 0
    first_rank: int | None = None
    first_market_cap_usd: Decimal | None = None

    # ---- current state ---------------------------------------------------
    current_rank: int | None = None
    best_rank: int | None = None
    current_market_cap_usd: Decimal | None = None
    peak_market_cap_usd: Decimal | None = None
    price_usd: Decimal | None = None
    displayed_change_percent: Decimal | None = None
    change_window: str = CHANGE_WINDOW_UNKNOWN
    liquidity_usd: Decimal | None = None
    holder_count: int | None = None
    first_holder_count: int | None = None
    top10_percent: Decimal | None = None
    first_top10_percent: Decimal | None = None
    verification: str = VERIFIED_UNKNOWN

    last_observed_at: int = 0
    #: Seconds actually spent on the board across all stints.
    seconds_on_board: int = 0
    #: How many separate times the mint entered the board.
    entries: int = 1
    #: True while the mint is on the current snapshot.
    on_board: bool = True
    exited_at: int | None = None
    #: Start of the current stint, used for "Trending since".
    stint_started_at: int = 0

    rank_history: tuple[RankPoint, ...] = ()
    source: TrendingSourceInfo = field(default_factory=TrendingSourceInfo)

    #: Bounded so a long-lived board entry cannot grow without limit.
    history_limit: int = 64

    # ------------------------------------------------------------------
    @classmethod
    def from_first_observation(cls, observation: TrendingObservation) -> TrendingLedgerEntry:
        return cls(
            mint=observation.mint,
            name=observation.name,
            symbol=observation.symbol,
            fomo_token_id=observation.fomo_token_id,
            fomo_url=observation.fomo_url,
            first_seen_at=observation.observed_at,
            first_rank=observation.rank,
            first_market_cap_usd=observation.market_cap_usd,
            current_rank=observation.rank,
            best_rank=observation.rank,
            current_market_cap_usd=observation.market_cap_usd,
            peak_market_cap_usd=observation.market_cap_usd,
            price_usd=observation.price_usd,
            displayed_change_percent=observation.displayed_change_percent,
            change_window=observation.change_window,
            liquidity_usd=observation.liquidity_usd,
            holder_count=observation.holder_count,
            first_holder_count=observation.holder_count,
            top10_percent=observation.top10_percent,
            first_top10_percent=observation.top10_percent,
            verification=observation.verification,
            last_observed_at=observation.observed_at,
            stint_started_at=observation.observed_at,
            rank_history=(
                (RankPoint(at=observation.observed_at, rank=observation.rank),)
                if observation.rank is not None
                else ()
            ),
            source=observation.source,
        )

    # ------------------------------------------------------------------
    def observe(
        self,
        observation: TrendingObservation,
        *,
        outage_gap_seconds: int = 3600,
        max_credited_gap_seconds: int = 300,
    ) -> TrendingLedgerEntry:
        """Fold a new reading in.  First-observation fields are never rewritten.

        A mint that left the board and came back starts a new *stint* — the
        re-entry counter and ``stint_started_at`` move — but ``first_seen_at``,
        ``first_rank`` and ``first_market_cap_usd`` stay exactly as first
        recorded, because those are the numbers the "were we early?" question is
        measured against.

        Re-entry is decided by ``on_board``, which the board diff sets from the
        source itself.  It is deliberately *not* inferred from the gap between
        two observations: a slow poll, a restart or a busy loop all produce
        large gaps while the token never left the board, and treating those as
        re-entries would inflate the stint counter and corrupt "Trending since".
        ``outage_gap_seconds`` is only a backstop for a gap so long that we
        certainly missed an exit we never got to record.

        Time on the board is credited from observations we actually made, capped
        at ``max_credited_gap_seconds`` per gap.  Crediting a three-hour outage
        as three hours "on board" would be inventing observation we never did.
        """

        if observation.mint != self.mint:
            raise ValueError("a Trending ledger entry may never merge a different mint")

        gap = max(0, observation.observed_at - self.last_observed_at)
        reentered = (not self.on_board) or gap >= outage_gap_seconds
        elapsed = 0 if reentered else min(gap, max_credited_gap_seconds)

        history = self.rank_history
        if observation.rank is not None:
            history = (*history, RankPoint(at=observation.observed_at, rank=observation.rank))
            if len(history) > self.history_limit:
                history = history[-self.history_limit :]

        best = self.best_rank
        if observation.rank is not None:
            best = observation.rank if best is None else min(best, observation.rank)

        peak = self.peak_market_cap_usd
        if observation.market_cap_usd is not None:
            peak = (
                observation.market_cap_usd
                if peak is None
                else max(peak, observation.market_cap_usd)
            )

        return replace(
            self,
            # Identity/display fields may be filled in as they become known, but
            # a later blank never erases what we already had.
            name=observation.name or self.name,
            symbol=observation.symbol or self.symbol,
            fomo_token_id=observation.fomo_token_id or self.fomo_token_id,
            fomo_url=observation.fomo_url or self.fomo_url,
            current_rank=observation.rank,
            best_rank=best,
            current_market_cap_usd=(
                observation.market_cap_usd
                if observation.market_cap_usd is not None
                else self.current_market_cap_usd
            ),
            peak_market_cap_usd=peak,
            price_usd=(
                observation.price_usd if observation.price_usd is not None else self.price_usd
            ),
            displayed_change_percent=(
                observation.displayed_change_percent
                if observation.displayed_change_percent is not None
                else self.displayed_change_percent
            ),
            change_window=observation.change_window,
            liquidity_usd=(
                observation.liquidity_usd
                if observation.liquidity_usd is not None
                else self.liquidity_usd
            ),
            holder_count=(
                observation.holder_count
                if observation.holder_count is not None
                else self.holder_count
            ),
            # A first holder count we never had can be backfilled once; one we
            # already recorded is frozen like every other entry number.
            first_holder_count=(
                self.first_holder_count
                if self.first_holder_count is not None
                else observation.holder_count
            ),
            top10_percent=(
                observation.top10_percent
                if observation.top10_percent is not None
                else self.top10_percent
            ),
            first_top10_percent=(
                self.first_top10_percent
                if self.first_top10_percent is not None
                else observation.top10_percent
            ),
            verification=(
                observation.verification
                if observation.verification != VERIFIED_UNKNOWN
                else self.verification
            ),
            last_observed_at=observation.observed_at,
            seconds_on_board=self.seconds_on_board + elapsed,
            entries=self.entries + (1 if reentered else 0),
            on_board=True,
            exited_at=None,
            stint_started_at=(
                observation.observed_at
                if reentered
                else (self.stint_started_at or self.first_seen_at)
            ),
            rank_history=history,
            source=observation.source if observation.source.configured else self.source,
        )

    def mark_left_board(self, *, at: int) -> TrendingLedgerEntry:
        """Record that the mint is no longer on the board.  Nothing is deleted."""

        if not self.on_board:
            return self
        return replace(self, on_board=False, exited_at=at, current_rank=None)

    # ------------------------------------------------------------------
    # derived views
    # ------------------------------------------------------------------
    @property
    def is_new_entry_window(self) -> bool:
        """Whether this is still the mint's first minutes on the board."""

        return self.entries == 1 and self.seconds_on_board <= 600

    def seconds_trending(self, *, now: int | None = None) -> int:
        moment = now if now is not None else self.last_observed_at
        anchor = self.stint_started_at or self.first_seen_at
        if not anchor:
            return 0
        return max(0, moment - anchor)

    def market_cap_move_percent(self) -> Decimal | None:
        """Move since the mint *entered Trending* — not since launch."""

        return _percent_change(self.first_market_cap_usd, self.current_market_cap_usd)

    def holder_growth(self) -> int | None:
        if self.holder_count is None or self.first_holder_count is None:
            return None
        return self.holder_count - self.first_holder_count

    def concentration_trend(self) -> Decimal | None:
        """Positive means the top 10 are taking a *larger* share — worse."""

        if self.top10_percent is None or self.first_top10_percent is None:
            return None
        return self.top10_percent - self.first_top10_percent

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "name": self.name,
            "symbol": self.symbol,
            "fomo_token_id": self.fomo_token_id,
            "fomo_url": self.fomo_url,
            "first_seen_at": self.first_seen_at,
            "first_rank": self.first_rank,
            "first_market_cap_usd": _str_or_none(self.first_market_cap_usd),
            "current_rank": self.current_rank,
            "best_rank": self.best_rank,
            "current_market_cap_usd": _str_or_none(self.current_market_cap_usd),
            "peak_market_cap_usd": _str_or_none(self.peak_market_cap_usd),
            "price_usd": _str_or_none(self.price_usd),
            "displayed_change_percent": _str_or_none(self.displayed_change_percent),
            "change_window": self.change_window,
            "liquidity_usd": _str_or_none(self.liquidity_usd),
            "holder_count": self.holder_count,
            "first_holder_count": self.first_holder_count,
            "top10_percent": _str_or_none(self.top10_percent),
            "first_top10_percent": _str_or_none(self.first_top10_percent),
            "verification": self.verification,
            "last_observed_at": self.last_observed_at,
            "seconds_on_board": self.seconds_on_board,
            "entries": self.entries,
            "on_board": self.on_board,
            "exited_at": self.exited_at,
            "stint_started_at": self.stint_started_at,
            "rank_history": [{"at": point.at, "rank": point.rank} for point in self.rank_history],
            "source": self.source.to_json(),
        }


def _str_or_none(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def entry_from_json(payload: dict[str, object]) -> TrendingLedgerEntry:
    """Rebuild an entry after a restart (section 111)."""

    source_payload = payload.get("source")
    source = (
        TrendingSourceInfo(**source_payload)
        if isinstance(source_payload, dict)
        else TrendingSourceInfo()
    )
    history_payload = payload.get("rank_history")
    history: tuple[RankPoint, ...] = ()
    if isinstance(history_payload, list):
        history = tuple(
            RankPoint(at=int(item["at"]), rank=int(item["rank"]))
            for item in history_payload
            if isinstance(item, dict)
            and item.get("at") is not None
            and item.get("rank") is not None
        )
    return TrendingLedgerEntry(
        mint=str(payload["mint"]),
        name=str(payload.get("name") or ""),
        symbol=str(payload.get("symbol") or ""),
        fomo_token_id=str(payload.get("fomo_token_id") or ""),
        fomo_url=str(payload.get("fomo_url") or ""),
        first_seen_at=int(payload.get("first_seen_at") or 0),
        first_rank=_int_or_none(payload.get("first_rank")),
        first_market_cap_usd=_dec_or_none(payload.get("first_market_cap_usd")),
        current_rank=_int_or_none(payload.get("current_rank")),
        best_rank=_int_or_none(payload.get("best_rank")),
        current_market_cap_usd=_dec_or_none(payload.get("current_market_cap_usd")),
        peak_market_cap_usd=_dec_or_none(payload.get("peak_market_cap_usd")),
        price_usd=_dec_or_none(payload.get("price_usd")),
        displayed_change_percent=_dec_or_none(payload.get("displayed_change_percent")),
        change_window=normalise_change_window(payload.get("change_window")),
        liquidity_usd=_dec_or_none(payload.get("liquidity_usd")),
        holder_count=_int_or_none(payload.get("holder_count")),
        first_holder_count=_int_or_none(payload.get("first_holder_count")),
        top10_percent=_dec_or_none(payload.get("top10_percent")),
        first_top10_percent=_dec_or_none(payload.get("first_top10_percent")),
        verification=str(payload.get("verification") or VERIFIED_UNKNOWN),
        last_observed_at=int(payload.get("last_observed_at") or 0),
        seconds_on_board=int(payload.get("seconds_on_board") or 0),
        entries=int(payload.get("entries") or 1),
        on_board=bool(payload.get("on_board", True)),
        exited_at=_int_or_none(payload.get("exited_at")),
        stint_started_at=int(payload.get("stint_started_at") or 0),
        rank_history=history,
        source=source,
    )


def _int_or_none(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _dec_or_none(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


# --- rank velocity (section 9) -----------------------------------------------
@dataclass(frozen=True, slots=True)
class RankVelocity:
    """How fast the rank is improving, not merely how good it is.

    ``#44 → #31 → #18 → #9`` in minutes is a different, more actionable object
    than ``#2`` flat for six hours, and the scoring layer must be able to tell
    them apart.  ``delta`` is positive when the rank *improves* (the number goes
    down), because "climbing" reading as a positive number is what every surface
    prints.
    """

    delta: int = 0
    per_minute: Decimal = ZERO
    window_seconds: int = 0
    from_rank: int | None = None
    to_rank: int | None = None
    samples: int = 0
    seconds_to_top25: int | None = None
    seconds_to_top10: int | None = None
    seconds_to_top5: int | None = None

    @property
    def climbing(self) -> bool:
        return self.delta > 0

    @property
    def falling(self) -> bool:
        return self.delta < 0

    def to_json(self) -> dict[str, object]:
        return {
            "delta": self.delta,
            "per_minute": str(self.per_minute),
            "window_seconds": self.window_seconds,
            "from_rank": self.from_rank,
            "to_rank": self.to_rank,
            "samples": self.samples,
            "seconds_to_top25": self.seconds_to_top25,
            "seconds_to_top10": self.seconds_to_top10,
            "seconds_to_top5": self.seconds_to_top5,
        }


def rank_velocity(
    history: Sequence[RankPoint],
    *,
    window_seconds: int = 300,
    now: int | None = None,
    first_seen_at: int | None = None,
) -> RankVelocity:
    """Measure rank movement across a bounded recent window."""

    points = tuple(sorted(history, key=lambda point: point.at))
    if not points:
        return RankVelocity()
    moment = now if now is not None else points[-1].at
    recent = [point for point in points if moment - point.at <= window_seconds]
    if len(recent) < 2:
        # Fall back to the last two readings we have, whatever their spacing, so
        # a slow poll still produces a measurement instead of a silent zero.
        recent = list(points[-2:])
    if len(recent) < 2:
        latest = points[-1]
        return RankVelocity(
            from_rank=latest.rank,
            to_rank=latest.rank,
            samples=1,
            **_time_to_tiers(points, first_seen_at),
        )

    start, end = recent[0], recent[-1]
    span = max(0, end.at - start.at)
    delta = start.rank - end.rank
    per_minute = (
        (Decimal(delta) * Decimal(60) / Decimal(span)).quantize(Decimal("0.01"))
        if span > 0
        else ZERO
    )
    return RankVelocity(
        delta=delta,
        per_minute=per_minute,
        window_seconds=span,
        from_rank=start.rank,
        to_rank=end.rank,
        samples=len(recent),
        **_time_to_tiers(points, first_seen_at),
    )


def _time_to_tiers(
    points: Sequence[RankPoint],
    first_seen_at: int | None,
) -> dict[str, int | None]:
    """How long it took to first reach the top 25/10/5, from first observation."""

    anchor = first_seen_at if first_seen_at is not None else (points[0].at if points else None)
    result: dict[str, int | None] = {
        "seconds_to_top25": None,
        "seconds_to_top10": None,
        "seconds_to_top5": None,
    }
    if anchor is None:
        return result
    for point in points:
        if result["seconds_to_top25"] is None and point.rank <= 25:
            result["seconds_to_top25"] = max(0, point.at - anchor)
        if result["seconds_to_top10"] is None and point.rank <= 10:
            result["seconds_to_top10"] = max(0, point.at - anchor)
        if result["seconds_to_top5"] is None and point.rank <= 5:
            result["seconds_to_top5"] = max(0, point.at - anchor)
    return result


def market_cap_velocity(
    entry: TrendingLedgerEntry,
    *,
    previous_market_cap_usd: Decimal | None,
    seconds: int,
) -> Decimal | None:
    """Percent market-cap change per minute across the supplied gap."""

    change = _percent_change(previous_market_cap_usd, entry.current_market_cap_usd)
    if change is None or seconds <= 0:
        return None
    return (change * Decimal(60) / Decimal(seconds)).quantize(Decimal("0.01"))


def board_diff(
    previous: Iterable[str],
    current: Iterable[str],
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """(entered, still_present, left) between two board snapshots, by exact mint."""

    before = frozenset(previous)
    after = frozenset(current)
    return after - before, after & before, before - after
