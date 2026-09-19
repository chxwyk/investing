"""The canonical ground truth: when an exact mint FIRST entered FOMO Trending.

Everything this engine claims is measured against one event, so that event has
to be defined precisely enough that it cannot drift:

    ``FOMO_TREND_ENTER`` — an exact mint was ABSENT from the previous **valid**
    Trending snapshot and PRESENT in the next **valid** snapshot.

Three words in that sentence do the work.

**Exact mint.**  Membership is keyed by mint.  A board row with a ticker and no
resolvable mint is not a membership fact and is dropped from the snapshot rather
than guessed at.

**Valid.**  A snapshot is valid when the provider actually answered with a
board.  An empty response, a timeout, an HTTP error or a board shorter than the
configured floor is an *unknown*, not "Trending is now empty".  This distinction
is the difference between a quiet minute and a fabricated stampede: treating one
failed fetch as an empty board would emit a LEFT event for every mint on the
board and then an ENTER event for every one of them on the next success —
hundreds of fake ground-truth events from a single network blip, permanently
poisoning the training labels.  So an invalid snapshot advances nothing.

**First.**  ``first_trending_at`` is written once and is never updated, by any
code path, ever.  A mint that leaves and returns produces ``FOMO_TREND_REENTER``
with its own timestamp while the first entry stays frozen.  Without this rule
"was the alert early?" becomes unanswerable: a re-entry two hours later would
silently redefine the target and make every late alert look prescient.

Membership states are explicit (``ABSENT`` → ``ENTERED`` → ``ACTIVE`` → ``LEFT``
→ ``REENTERED``) so a consumer never has to infer a transition from two
snapshots it may not both have seen.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from .identity import extract_mint

# --- membership states -------------------------------------------------------
#: Never observed on the board.
MEMBERSHIP_ABSENT = "ABSENT"
#: Appeared on this snapshot for the first time ever.
MEMBERSHIP_ENTERED = "ENTERED"
#: Present on this snapshot and the previous valid one.
MEMBERSHIP_ACTIVE = "ACTIVE"
#: Present on the previous valid snapshot, absent on this one.
MEMBERSHIP_LEFT = "LEFT"
#: Absent on the previous valid snapshot, present now, and present before that.
MEMBERSHIP_REENTERED = "REENTERED"

MEMBERSHIP_STATES: tuple[str, ...] = (
    MEMBERSHIP_ABSENT,
    MEMBERSHIP_ENTERED,
    MEMBERSHIP_ACTIVE,
    MEMBERSHIP_LEFT,
    MEMBERSHIP_REENTERED,
)

# --- ground-truth event kinds ------------------------------------------------
#: The canonical label event.  Emitted at most once per mint, for all time.
FOMO_TREND_ENTER = "FOMO_TREND_ENTER"
#: A later stint on the board.  Never overwrites the first entry.
FOMO_TREND_REENTER = "FOMO_TREND_REENTER"
#: The mint dropped off the board.
FOMO_TREND_LEAVE = "FOMO_TREND_LEAVE"

TREND_EVENT_KINDS: tuple[str, ...] = (
    FOMO_TREND_ENTER,
    FOMO_TREND_REENTER,
    FOMO_TREND_LEAVE,
)

# --- why a snapshot was rejected ---------------------------------------------
SNAPSHOT_VALID = "VALID"
SNAPSHOT_PROVIDER_ERROR = "PROVIDER_ERROR"
SNAPSHOT_EMPTY = "EMPTY"
SNAPSHOT_TOO_SHORT = "TOO_SHORT"
SNAPSHOT_STALE = "STALE"
SNAPSHOT_DUPLICATE = "DUPLICATE"
SNAPSHOT_NO_EXACT_MINTS = "NO_EXACT_MINTS"

INVALID_REASONS: tuple[str, ...] = (
    SNAPSHOT_PROVIDER_ERROR,
    SNAPSHOT_EMPTY,
    SNAPSHOT_TOO_SHORT,
    SNAPSHOT_STALE,
    SNAPSHOT_DUPLICATE,
    SNAPSHOT_NO_EXACT_MINTS,
)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() else None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class BoardRow:
    """One exact mint as the board displayed it, with its raw evidence kept."""

    mint: str
    rank: int | None = None
    symbol: str = ""
    name: str = ""
    #: Whatever tier/indicator the board displays ($ / $$ / $$$), stored raw and
    #: uninterpreted.  We do not know what it means and will not pretend to.
    tier: str = ""
    market_cap_usd: Decimal | None = None
    price_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_usd: Decimal | None = None
    holders: int | None = None
    token_age_seconds: int | None = None
    pair_age_seconds: int | None = None
    #: The provider's own timestamp for this reading, when it supplies one.
    source_at: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.mint:
            raise ValueError("a board row requires the exact mint")

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, rank: int | None = None
    ) -> BoardRow | None:
        """Build a row from a provider payload, or ``None`` if the mint is unresolvable.

        A row we cannot key by mint is not a weaker row — it is not a membership
        fact at all, because we cannot say *which* token is on the board.
        """

        mint = None
        for key in ("mint", "address", "tokenAddress", "token_address", "ca"):
            mint = extract_mint(payload.get(key))
            if mint:
                break
        if not mint:
            return None
        return cls(
            mint=mint,
            rank=_int_or_none(payload.get("rank")) if payload.get("rank") is not None else rank,
            symbol=str(payload.get("symbol") or payload.get("ticker") or "")[:32],
            name=str(payload.get("name") or "")[:120],
            tier=str(payload.get("tier") or payload.get("indicator") or "")[:16],
            market_cap_usd=_decimal(payload.get("marketCap") or payload.get("market_cap")),
            price_usd=_decimal(payload.get("price") or payload.get("priceUsd")),
            liquidity_usd=_decimal(payload.get("liquidity")),
            volume_usd=_decimal(payload.get("volume") or payload.get("volumeUsd")),
            holders=_int_or_none(payload.get("holders") or payload.get("holderCount")),
            token_age_seconds=_int_or_none(payload.get("tokenAgeSeconds")),
            pair_age_seconds=_int_or_none(payload.get("pairAgeSeconds")),
            source_at=_int_or_none(payload.get("sourceAt") or payload.get("timestamp")),
            raw=dict(payload),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "rank": self.rank,
            "symbol": self.symbol,
            "name": self.name,
            "tier": self.tier,
            "market_cap_usd": None if self.market_cap_usd is None else str(self.market_cap_usd),
            "price_usd": None if self.price_usd is None else str(self.price_usd),
            "liquidity_usd": None if self.liquidity_usd is None else str(self.liquidity_usd),
            "volume_usd": None if self.volume_usd is None else str(self.volume_usd),
            "holders": self.holders,
            "token_age_seconds": self.token_age_seconds,
            "pair_age_seconds": self.pair_age_seconds,
            "source_at": self.source_at,
        }


@dataclass(frozen=True, slots=True)
class TrendingSnapshot:
    """One attempt to read the board, valid or not, with full provenance."""

    #: When our collector took the reading.
    observed_at: int
    rows: tuple[BoardRow, ...] = ()
    provider: str = ""
    #: Provenance kind from :mod:`smart_money_bot.trending.source`.
    source_kind: str = ""
    #: Non-empty when the provider failed.  A failure is never an empty board.
    error: str = ""
    #: The provider's own timestamp for the whole board, if it publishes one.
    source_at: int | None = None
    collector_version: str = ""

    @property
    def mints(self) -> frozenset[str]:
        return frozenset(row.mint for row in self.rows)

    def by_mint(self) -> dict[str, BoardRow]:
        return {row.mint: row for row in self.rows}

    def to_json(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at,
            "provider": self.provider,
            "source_kind": self.source_kind,
            "error": self.error,
            "source_at": self.source_at,
            "collector_version": self.collector_version,
            "rows": [row.to_json() for row in self.rows],
        }


@dataclass(frozen=True, slots=True)
class SnapshotValidity:
    """Whether a snapshot may advance membership state, and why."""

    valid: bool
    reason: str = SNAPSHOT_VALID
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"valid": self.valid, "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class TrendEntryEvent:
    """A ground-truth board transition for one exact mint."""

    kind: str
    mint: str
    occurred_at: int
    symbol: str = ""
    name: str = ""
    initial_rank: int | None = None
    tier: str = ""
    market_cap_usd: Decimal | None = None
    price_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_usd: Decimal | None = None
    holders: int | None = None
    token_age_seconds: int | None = None
    pair_age_seconds: int | None = None
    provider: str = ""
    source_kind: str = ""
    source_at: int | None = None
    collector_at: int = 0
    collector_version: str = ""
    #: The board row exactly as the provider sent it.
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "mint": self.mint,
            "occurred_at": self.occurred_at,
            "symbol": self.symbol,
            "name": self.name,
            "initial_rank": self.initial_rank,
            "tier": self.tier,
            "market_cap_usd": None if self.market_cap_usd is None else str(self.market_cap_usd),
            "price_usd": None if self.price_usd is None else str(self.price_usd),
            "liquidity_usd": None if self.liquidity_usd is None else str(self.liquidity_usd),
            "volume_usd": None if self.volume_usd is None else str(self.volume_usd),
            "holders": self.holders,
            "token_age_seconds": self.token_age_seconds,
            "pair_age_seconds": self.pair_age_seconds,
            "provider": self.provider,
            "source_kind": self.source_kind,
            "source_at": self.source_at,
            "collector_at": self.collector_at,
            "collector_version": self.collector_version,
        }


@dataclass(frozen=True, slots=True)
class MembershipRecord:
    """Per-mint board history.  ``first_trending_at`` is write-once."""

    mint: str
    first_trending_at: int
    first_rank: int | None = None
    first_market_cap_usd: Decimal | None = None
    state: str = MEMBERSHIP_ENTERED
    last_seen_on_board_at: int = 0
    left_at: int | None = None
    entries: int = 1
    #: Every stint, oldest first, as ``(entered_at, left_at_or_None)``.
    stints: tuple[tuple[int, int | None], ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "first_trending_at": self.first_trending_at,
            "first_rank": self.first_rank,
            "first_market_cap_usd": (
                None if self.first_market_cap_usd is None else str(self.first_market_cap_usd)
            ),
            "state": self.state,
            "last_seen_on_board_at": self.last_seen_on_board_at,
            "left_at": self.left_at,
            "entries": self.entries,
            "stints": [list(stint) for stint in self.stints],
        }


@dataclass(frozen=True, slots=True)
class SnapshotOutcome:
    """What one ingested snapshot did."""

    validity: SnapshotValidity
    events: tuple[TrendEntryEvent, ...] = ()
    entered: tuple[str, ...] = ()
    reentered: tuple[str, ...] = ()
    left: tuple[str, ...] = ()
    active: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.validity.valid


@dataclass(frozen=True, slots=True)
class GroundTruthConfig:
    """Tunables for snapshot validation.  Every default is deliberately strict."""

    #: A valid board must have at least this many exact-mint rows.  A board that
    #: suddenly shrinks to two rows is far more likely to be a broken response
    #: than a real market event, and treating it as real would emit dozens of
    #: false LEFT events.
    min_rows: int = 5
    #: A snapshot whose provider timestamp is older than this is refused rather
    #: than replayed as current.
    max_source_age_seconds: int = 300
    #: Two snapshots with the same observed_at are the same reading.
    reject_duplicates: bool = True
    #: If the board shrinks by more than this fraction in one step, the snapshot
    #: is refused as a suspected partial response.
    max_shrink_ratio: Decimal = Decimal("0.6")


DEFAULT_GROUND_TRUTH_CONFIG = GroundTruthConfig()


def assess_snapshot(
    snapshot: TrendingSnapshot,
    *,
    previous_size: int | None,
    previous_observed_at: int | None,
    config: GroundTruthConfig = DEFAULT_GROUND_TRUTH_CONFIG,
) -> SnapshotValidity:
    """Decide whether this reading may advance membership state.

    Ordering matters: a provider error is reported as an error even when the
    payload also happens to be empty, because the operator fix is different.
    """

    if snapshot.error:
        return SnapshotValidity(False, SNAPSHOT_PROVIDER_ERROR, snapshot.error[:200])
    if not snapshot.rows:
        return SnapshotValidity(
            False, SNAPSHOT_EMPTY, "provider returned no rows; this is unknown, not empty"
        )
    if len(snapshot.rows) < config.min_rows:
        return SnapshotValidity(
            False,
            SNAPSHOT_TOO_SHORT,
            f"{len(snapshot.rows)} rows is below the {config.min_rows}-row floor",
        )
    if (
        config.reject_duplicates
        and previous_observed_at is not None
        and snapshot.observed_at <= previous_observed_at
    ):
        return SnapshotValidity(
            False,
            SNAPSHOT_DUPLICATE,
            f"observed_at {snapshot.observed_at} does not advance past {previous_observed_at}",
        )
    if snapshot.source_at is not None:
        age = snapshot.observed_at - snapshot.source_at
        if age > config.max_source_age_seconds:
            return SnapshotValidity(
                False, SNAPSHOT_STALE, f"provider timestamp is {age}s old"
            )
    if previous_size:
        shrink = Decimal(previous_size - len(snapshot.rows)) / Decimal(previous_size)
        if shrink > config.max_shrink_ratio:
            return SnapshotValidity(
                False,
                SNAPSHOT_TOO_SHORT,
                f"board shrank {shrink:.0%} in one step; treated as a partial response",
            )
    return SnapshotValidity(True)


class TrendingGroundTruth:
    """Turns a stream of board readings into immutable first-entry events.

    The class holds only membership bookkeeping.  It does no I/O, so it can be
    driven identically by the live collector and by a historical replay — which
    is what makes a replayed label provably the same label the live system saw.
    """

    def __init__(
        self,
        *,
        config: GroundTruthConfig = DEFAULT_GROUND_TRUTH_CONFIG,
        records: Iterable[MembershipRecord] = (),
        collector_version: str = "",
    ) -> None:
        self.config = config
        self.collector_version = collector_version
        self._records: dict[str, MembershipRecord] = {
            record.mint: record for record in records
        }
        self._on_board: set[str] = {
            mint
            for mint, record in self._records.items()
            if record.state in {MEMBERSHIP_ENTERED, MEMBERSHIP_ACTIVE, MEMBERSHIP_REENTERED}
        }
        self._last_observed_at: int | None = None
        self._last_size: int | None = None
        self.accepted_snapshots = 0
        self.rejected_snapshots = 0
        self.rejections: dict[str, int] = {}

    # ------------------------------------------------------------------
    @property
    def records(self) -> dict[str, MembershipRecord]:
        return dict(self._records)

    def record(self, mint: str) -> MembershipRecord | None:
        return self._records.get(mint)

    def first_trending_at(self, mint: str) -> int | None:
        record = self._records.get(mint)
        return None if record is None else record.first_trending_at

    def on_board(self) -> frozenset[str]:
        return frozenset(self._on_board)

    # ------------------------------------------------------------------
    def ingest(self, snapshot: TrendingSnapshot) -> SnapshotOutcome:
        """Apply one reading.  An invalid reading changes nothing at all."""

        validity = assess_snapshot(
            snapshot,
            previous_size=self._last_size,
            previous_observed_at=self._last_observed_at,
            config=self.config,
        )
        if not validity.valid:
            self.rejected_snapshots += 1
            self.rejections[validity.reason] = self.rejections.get(validity.reason, 0) + 1
            return SnapshotOutcome(validity)

        self.accepted_snapshots += 1
        rows = snapshot.by_mint()
        current = frozenset(rows)
        previous = frozenset(self._on_board)

        entered: list[str] = []
        reentered: list[str] = []
        events: list[TrendEntryEvent] = []

        for mint in sorted(current - previous):
            row = rows[mint]
            existing = self._records.get(mint)
            if existing is None:
                # FIRST entry, ever.  This timestamp is now frozen for all time.
                self._records[mint] = MembershipRecord(
                    mint=mint,
                    first_trending_at=snapshot.observed_at,
                    first_rank=row.rank,
                    first_market_cap_usd=row.market_cap_usd,
                    state=MEMBERSHIP_ENTERED,
                    last_seen_on_board_at=snapshot.observed_at,
                    entries=1,
                    stints=((snapshot.observed_at, None),),
                )
                entered.append(mint)
                events.append(self._event(FOMO_TREND_ENTER, row, snapshot))
            else:
                # A later stint.  first_trending_at is deliberately re-used from
                # the existing record and never recomputed.
                self._records[mint] = replace(
                    existing,
                    state=MEMBERSHIP_REENTERED,
                    last_seen_on_board_at=snapshot.observed_at,
                    left_at=None,
                    entries=existing.entries + 1,
                    stints=existing.stints + ((snapshot.observed_at, None),),
                )
                reentered.append(mint)
                events.append(self._event(FOMO_TREND_REENTER, row, snapshot))

        left: list[str] = []
        for mint in sorted(previous - current):
            existing = self._records.get(mint)
            if existing is None:
                continue
            stints = existing.stints
            if stints and stints[-1][1] is None:
                stints = stints[:-1] + ((stints[-1][0], snapshot.observed_at),)
            self._records[mint] = replace(
                existing,
                state=MEMBERSHIP_LEFT,
                left_at=snapshot.observed_at,
                stints=stints,
            )
            left.append(mint)
            events.append(
                TrendEntryEvent(
                    kind=FOMO_TREND_LEAVE,
                    mint=mint,
                    occurred_at=snapshot.observed_at,
                    provider=snapshot.provider,
                    source_kind=snapshot.source_kind,
                    source_at=snapshot.source_at,
                    collector_at=snapshot.observed_at,
                    collector_version=snapshot.collector_version or self.collector_version,
                )
            )

        active: list[str] = []
        for mint in sorted(current & previous):
            existing = self._records.get(mint)
            if existing is not None:
                self._records[mint] = replace(
                    existing,
                    state=MEMBERSHIP_ACTIVE,
                    last_seen_on_board_at=snapshot.observed_at,
                )
            active.append(mint)

        self._on_board = set(current)
        self._last_observed_at = snapshot.observed_at
        self._last_size = len(snapshot.rows)

        return SnapshotOutcome(
            validity=validity,
            events=tuple(events),
            entered=tuple(entered),
            reentered=tuple(reentered),
            left=tuple(left),
            active=tuple(active),
        )

    # ------------------------------------------------------------------
    def _event(
        self, kind: str, row: BoardRow, snapshot: TrendingSnapshot
    ) -> TrendEntryEvent:
        return TrendEntryEvent(
            kind=kind,
            mint=row.mint,
            occurred_at=snapshot.observed_at,
            symbol=row.symbol,
            name=row.name,
            initial_rank=row.rank,
            tier=row.tier,
            market_cap_usd=row.market_cap_usd,
            price_usd=row.price_usd,
            liquidity_usd=row.liquidity_usd,
            volume_usd=row.volume_usd,
            holders=row.holders,
            token_age_seconds=row.token_age_seconds,
            pair_age_seconds=row.pair_age_seconds,
            provider=snapshot.provider,
            source_kind=snapshot.source_kind,
            source_at=row.source_at if row.source_at is not None else snapshot.source_at,
            collector_at=snapshot.observed_at,
            collector_version=snapshot.collector_version or self.collector_version,
            raw=row.raw,
        )

    # ------------------------------------------------------------------
    def health(self, *, now: int | None = None) -> dict[str, Any]:
        moment = now if now is not None else int(time.time())
        total = self.accepted_snapshots + self.rejected_snapshots
        return {
            "accepted_snapshots": self.accepted_snapshots,
            "rejected_snapshots": self.rejected_snapshots,
            "acceptance_rate": (
                None if total == 0 else round(self.accepted_snapshots / total, 4)
            ),
            "rejections": dict(sorted(self.rejections.items())),
            "tracked_mints": len(self._records),
            "on_board": len(self._on_board),
            "last_accepted_at": self._last_observed_at,
            "seconds_since_accepted": (
                None if self._last_observed_at is None else moment - self._last_observed_at
            ),
        }


def rank_progression(
    snapshots: Sequence[TrendingSnapshot], mint: str, *, entry_at: int
) -> dict[str, Any]:
    """Rank at entry and at fixed offsets after it (section 59).

    Offsets read the *latest snapshot at or before* the target time rather than
    the nearest one, so a missing snapshot yields ``None`` instead of a value
    borrowed from the future.
    """

    ordered = sorted(
        (snap for snap in snapshots if not snap.error and snap.rows),
        key=lambda snap: snap.observed_at,
    )
    ranks: list[tuple[int, int | None]] = []
    for snap in ordered:
        row = snap.by_mint().get(mint)
        if row is not None:
            ranks.append((snap.observed_at, row.rank))

    def rank_at(offset: int) -> int | None:
        target = entry_at + offset
        best: int | None = None
        for at, rank in ranks:
            if at <= target:
                best = rank
            else:
                break
        return best

    known = [rank for _, rank in ranks if rank is not None]
    best_rank = min(known) if known else None

    def time_to(threshold: int) -> int | None:
        for at, rank in ranks:
            if rank is not None and rank <= threshold:
                return at - entry_at
        return None

    return {
        "initial_rank": rank_at(0),
        "rank_30s": rank_at(30),
        "rank_1m": rank_at(60),
        "rank_2m": rank_at(120),
        "rank_5m": rank_at(300),
        "best_rank": best_rank,
        "seconds_to_top10": time_to(10),
        "seconds_to_top3": time_to(3),
        "seconds_to_rank1": time_to(1),
        "observations": len(ranks),
    }
