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

Two further rules exist because the first version of this module got them wrong.

**Only an authorised FOMO source may establish a FOMO label.**  This deployment's
default Trending source is not FOMO at all — with no ``FOMO_TRENDING_API_URL``
configured it is a DexScreener paid-boost ordering stamped ``TRENDING_PROXY``.
That is a legitimate *observation* and it is still collected, but labelling it
``FOMO_TREND_ENTER`` would mean every downstream claim — lead time, affinity,
precision, base rate — described a different event from the one named.  Proxy
membership is therefore tracked in a completely separate namespace, emits its own
``PROXY_BOARD_*`` event kinds, and can never reach a FOMO label, a confirmation
card or a training target.  :meth:`TrendingGroundTruth.first_trending_at` returns
``None`` for a mint known only to the proxy.

**Presence is not entry.**  A mint that is already on the board when collection
starts did not just enter it; we simply began watching.  Recording the collector's
start time as its ``first_trending_at`` would invent an entry that never happened
and, worse, would date it to the moment we were least able to have predicted it —
manufacturing a cohort of "entries" no model could ever have called.  The same
applies after a coverage gap: if the collector was down for ten minutes, a mint
that appears on the next snapshot may have entered at any point in between.  Such
mints are recorded as ``PRESENT_UNPROVEN`` with a ``first_observed_on_board_at``
and **no** ``first_trending_at``, and they are excluded from labels in both
directions — they cannot be positives, and they must not be used as controls
either, because they *were* trending.

Membership states are explicit (``ABSENT`` → ``ENTERED`` → ``ACTIVE`` → ``LEFT``
→ ``REENTERED``, plus ``PRESENT_UNPROVEN``) so a consumer never has to infer a
transition from two snapshots it may not both have seen.
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
#: On the board the first time we looked, or the first time we looked after a
#: coverage gap.  We know it is there; we do not know when it arrived.
MEMBERSHIP_PRESENT_UNPROVEN = "PRESENT_UNPROVEN"

MEMBERSHIP_STATES: tuple[str, ...] = (
    MEMBERSHIP_ABSENT,
    MEMBERSHIP_ENTERED,
    MEMBERSHIP_ACTIVE,
    MEMBERSHIP_LEFT,
    MEMBERSHIP_REENTERED,
    MEMBERSHIP_PRESENT_UNPROVEN,
)

#: States that mean "currently on the board", whatever we know about arrival.
ON_BOARD_STATES: frozenset[str] = frozenset(
    {
        MEMBERSHIP_ENTERED,
        MEMBERSHIP_ACTIVE,
        MEMBERSHIP_REENTERED,
        MEMBERSHIP_PRESENT_UNPROVEN,
    }
)

# --- ground-truth event kinds ------------------------------------------------
#: The canonical label event.  Emitted at most once per mint, for all time.
FOMO_TREND_ENTER = "FOMO_TREND_ENTER"
#: A later stint on the board.  Never overwrites the first entry.
FOMO_TREND_REENTER = "FOMO_TREND_REENTER"
#: The mint dropped off the board.
FOMO_TREND_LEAVE = "FOMO_TREND_LEAVE"
#: Seen on the board without witnessing the arrival.  Evidence, never a label.
FOMO_BOARD_PRESENT_UNPROVEN = "FOMO_BOARD_PRESENT_UNPROVEN"

#: Proxy-source equivalents.  Deliberately different strings so that no filter,
#: query or card written for the FOMO kinds can ever match a proxy row by
#: accident -- the two describe different events on different boards.
PROXY_BOARD_ENTER = "PROXY_BOARD_ENTER"
PROXY_BOARD_REENTER = "PROXY_BOARD_REENTER"
PROXY_BOARD_LEAVE = "PROXY_BOARD_LEAVE"
PROXY_BOARD_PRESENT_UNPROVEN = "PROXY_BOARD_PRESENT_UNPROVEN"

TREND_EVENT_KINDS: tuple[str, ...] = (
    FOMO_TREND_ENTER,
    FOMO_TREND_REENTER,
    FOMO_TREND_LEAVE,
    FOMO_BOARD_PRESENT_UNPROVEN,
    PROXY_BOARD_ENTER,
    PROXY_BOARD_REENTER,
    PROXY_BOARD_LEAVE,
    PROXY_BOARD_PRESENT_UNPROVEN,
)

#: The only event kind that may establish a FOMO training label.
LABEL_EVENT_KINDS: frozenset[str] = frozenset({FOMO_TREND_ENTER})

# --- label grade -------------------------------------------------------------
#: An authorised FOMO Trending feed.  May establish labels.
GRADE_FOMO = "FOMO"
#: A public approximation.  Observed and stored; never a label.
GRADE_PROXY = "PROXY"

#: Provenance kinds (from :mod:`smart_money_bot.trending.source`) that are
#: authorised to establish FOMO labels.  Membership of this set is the single
#: gate; there is no heuristic, hostname check or response-shape check that can
#: promote a source into it.
AUTHORISED_SOURCE_KINDS: frozenset[str] = frozenset({"FOMO_TRENDING"})


def grade_for_source(source_kind: str) -> str:
    """Whether a source may establish FOMO labels.  Unknown means proxy."""

    return GRADE_FOMO if source_kind in AUTHORISED_SOURCE_KINDS else GRADE_PROXY


#: ``grade -> (enter, reenter, leave, unproven)`` event kinds.
_EVENT_KINDS_BY_GRADE: dict[str, tuple[str, str, str, str]] = {
    GRADE_FOMO: (
        FOMO_TREND_ENTER,
        FOMO_TREND_REENTER,
        FOMO_TREND_LEAVE,
        FOMO_BOARD_PRESENT_UNPROVEN,
    ),
    GRADE_PROXY: (
        PROXY_BOARD_ENTER,
        PROXY_BOARD_REENTER,
        PROXY_BOARD_LEAVE,
        PROXY_BOARD_PRESENT_UNPROVEN,
    ),
}

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
    #: ``FOMO`` or ``PROXY``.  Only ``FOMO`` may establish a training label.
    grade: str = GRADE_PROXY
    source_at: int | None = None
    collector_at: int = 0
    collector_version: str = ""
    #: The board row exactly as the provider sent it.
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def establishes_label(self) -> bool:
        """Whether this event may be used as a supervised target."""

        return self.grade == GRADE_FOMO and self.kind in LABEL_EVENT_KINDS

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
            "grade": self.grade,
            "establishes_label": self.establishes_label,
            "source_at": self.source_at,
            "collector_at": self.collector_at,
            "collector_version": self.collector_version,
        }


@dataclass(frozen=True, slots=True)
class MembershipRecord:
    """Per-mint board history.  Observation and proven entry are separate facts.

    ``first_observed_on_board_at`` is when we first *saw* it there and is always
    known.  ``first_trending_at`` is when we *witnessed it arrive* and is
    ``None`` unless we actually observed the absent-to-present transition.
    Conflating the two is what turns "we started watching" into "it just
    entered", and dates a fabricated entry to the one moment no model could
    have predicted it.
    """

    mint: str
    #: Always set: when this mint was first seen on the board.
    first_observed_on_board_at: int
    #: Set only when the ABSENT -> PRESENT transition was actually witnessed.
    first_trending_at: int | None = None
    first_rank: int | None = None
    first_market_cap_usd: Decimal | None = None
    state: str = MEMBERSHIP_ENTERED
    #: ``FOMO`` or ``PROXY``.  Proxy records live in their own namespace.
    grade: str = GRADE_FOMO
    last_seen_on_board_at: int = 0
    left_at: int | None = None
    entries: int = 1
    #: Every stint, oldest first, as ``(entered_at, left_at_or_None)``.
    stints: tuple[tuple[int, int | None], ...] = ()
    #: Why the entry is unproven, when it is.
    unproven_reason: str = ""

    @property
    def entry_proven(self) -> bool:
        """Whether we witnessed this mint arrive, as opposed to finding it there."""

        return self.first_trending_at is not None

    @property
    def usable_as_label(self) -> bool:
        """Whether this record may supply a supervised target."""

        return self.entry_proven and self.grade == GRADE_FOMO

    def to_json(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "first_observed_on_board_at": self.first_observed_on_board_at,
            "first_trending_at": self.first_trending_at,
            "entry_proven": self.entry_proven,
            "usable_as_label": self.usable_as_label,
            "grade": self.grade,
            "unproven_reason": self.unproven_reason,
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
    #: Mints found on the board without witnessing their arrival.
    present_unproven: tuple[str, ...] = ()
    #: ``FOMO`` or ``PROXY`` -- which namespace this snapshot advanced.
    grade: str = GRADE_PROXY
    #: True when a coverage gap made this snapshot's arrivals unprovable.
    after_coverage_gap: bool = False

    @property
    def accepted(self) -> bool:
        return self.validity.valid

    @property
    def establishes_labels(self) -> bool:
        """Whether anything here may become a supervised target."""

        return self.grade == GRADE_FOMO

    @property
    def label_events(self) -> tuple[TrendEntryEvent, ...]:
        """Only the events that may be used as training targets."""

        return tuple(event for event in self.events if event.establishes_label)


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
    #: A gap longer than this between accepted snapshots means we cannot claim to
    #: have witnessed anything that appeared during it.  Set a little above the
    #: poll interval so an ordinary missed beat does not void a real entry, but
    #: far below the shortest label horizon so a voided entry is never one the
    #: model could have been scored on.
    max_coverage_gap_seconds: int = 180


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
    driven identically by the live collector and by a historical replay -- which
    is what makes a replayed label provably the same label the live system saw.

    FOMO and proxy membership are tracked in two entirely separate namespaces.
    A proxy snapshot can never advance FOMO membership, and switching a
    deployment from proxy to an authorised feed starts the FOMO namespace clean
    rather than inheriting whatever the proxy happened to be showing.
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
        self._records: dict[str, dict[str, MembershipRecord]] = {
            GRADE_FOMO: {},
            GRADE_PROXY: {},
        }
        self._on_board: dict[str, set[str]] = {GRADE_FOMO: set(), GRADE_PROXY: set()}
        self._last_observed_at: dict[str, int | None] = {
            GRADE_FOMO: None,
            GRADE_PROXY: None,
        }
        self._last_size: dict[str, int | None] = {GRADE_FOMO: None, GRADE_PROXY: None}

        for record in records:
            grade = record.grade if record.grade in self._records else GRADE_FOMO
            self._records[grade][record.mint] = record
            if record.state in ON_BOARD_STATES:
                self._on_board[grade].add(record.mint)
                # Restoring membership also restores coverage: a restart is not
                # a coverage gap if the board has not moved on without us.  The
                # runtime supplies the real last-accepted time via restore().
                last = self._last_observed_at[grade]
                seen = record.last_seen_on_board_at
                if seen and (last is None or seen > last):
                    self._last_observed_at[grade] = seen

        self.accepted_snapshots = 0
        self.rejected_snapshots = 0
        self.rejections: dict[str, int] = {}
        self.coverage_gaps = 0

    # ------------------------------------------------------------------
    @property
    def records(self) -> dict[str, MembershipRecord]:
        """FOMO-grade records.  Proxy records are reached through :meth:`proxy_records`."""

        return dict(self._records[GRADE_FOMO])

    def proxy_records(self) -> dict[str, MembershipRecord]:
        return dict(self._records[GRADE_PROXY])

    def all_records(self) -> tuple[MembershipRecord, ...]:
        return tuple(self._records[GRADE_FOMO].values()) + tuple(
            self._records[GRADE_PROXY].values()
        )

    def record(self, mint: str, *, grade: str = GRADE_FOMO) -> MembershipRecord | None:
        return self._records.get(grade, {}).get(mint)

    def first_trending_at(self, mint: str) -> int | None:
        """The witnessed FOMO entry time, or ``None``.

        ``None`` covers three genuinely different situations -- never on the
        board, on the board but we never saw it arrive, and on a proxy board
        only -- and none of them may become a label, which is why they share a
        return value.  :meth:`describe` distinguishes them for a human.
        """

        record = self._records[GRADE_FOMO].get(mint)
        if record is None or not record.usable_as_label:
            return None
        return record.first_trending_at

    def label_map(self) -> dict[str, int]:
        """Every mint whose FOMO entry we actually witnessed."""

        return {
            mint: record.first_trending_at
            for mint, record in self._records[GRADE_FOMO].items()
            if record.usable_as_label and record.first_trending_at is not None
        }

    def entry_unproven_mints(self) -> frozenset[str]:
        """Mints seen on a board whose arrival we never witnessed.

        These are excluded from labels in **both** directions.  They cannot be
        positives because we do not know when they entered, and they must not be
        used as controls either, because they were demonstrably on the board --
        using them as "tokens that did not trend" would be exactly backwards.
        """

        return frozenset(
            mint
            for grade in (GRADE_FOMO, GRADE_PROXY)
            for mint, record in self._records[grade].items()
            if not record.entry_proven
        )

    def proxy_only_mints(self) -> frozenset[str]:
        """Mints known to the proxy board but never witnessed entering FOMO."""

        fomo = self._records[GRADE_FOMO]
        return frozenset(
            mint
            for mint in self._records[GRADE_PROXY]
            if mint not in fomo or not fomo[mint].usable_as_label
        )

    def on_board(self, *, grade: str = GRADE_FOMO) -> frozenset[str]:
        return frozenset(self._on_board.get(grade, set()))

    def describe(self, mint: str) -> str:
        """A human-readable membership verdict for one mint."""

        fomo = self._records[GRADE_FOMO].get(mint)
        if fomo is not None and fomo.usable_as_label:
            return f"FOMO entry witnessed at {fomo.first_trending_at}"
        if fomo is not None:
            return f"on the FOMO board but arrival not witnessed ({fomo.unproven_reason})"
        proxy = self._records[GRADE_PROXY].get(mint)
        if proxy is not None:
            return "seen on the PROXY board only; not a FOMO fact"
        return "never observed on any board"

    # ------------------------------------------------------------------
    def note_coverage(self, *, grade: str, at: int) -> None:
        """Tell the tracker when coverage was last known good, after a restart."""

        if grade in self._last_observed_at:
            current = self._last_observed_at[grade]
            if current is None or at > current:
                self._last_observed_at[grade] = at

    # ------------------------------------------------------------------
    def ingest(self, snapshot: TrendingSnapshot) -> SnapshotOutcome:
        """Apply one reading.  An invalid reading changes nothing at all."""

        grade = grade_for_source(snapshot.source_kind)
        validity = assess_snapshot(
            snapshot,
            previous_size=self._last_size[grade],
            previous_observed_at=self._last_observed_at[grade],
            config=self.config,
        )
        if not validity.valid:
            self.rejected_snapshots += 1
            self.rejections[validity.reason] = self.rejections.get(validity.reason, 0) + 1
            return SnapshotOutcome(validity, grade=grade)

        self.accepted_snapshots += 1
        enter_kind, reenter_kind, leave_kind, unproven_kind = _EVENT_KINDS_BY_GRADE[grade]
        records = self._records[grade]
        rows = snapshot.by_mint()
        current = frozenset(rows)
        previous = frozenset(self._on_board[grade])

        # Can we claim to have witnessed an arrival on this snapshot at all?
        last_at = self._last_observed_at[grade]
        if last_at is None:
            witnessed = False
            gap_reason = "first snapshot of this collection run"
        else:
            gap = snapshot.observed_at - last_at
            witnessed = gap <= self.config.max_coverage_gap_seconds
            gap_reason = "" if witnessed else f"{gap}s coverage gap before this snapshot"
        if not witnessed:
            self.coverage_gaps += 1

        entered: list[str] = []
        reentered: list[str] = []
        unproven: list[str] = []
        events: list[TrendEntryEvent] = []

        for mint in sorted(current - previous):
            row = rows[mint]
            existing = records.get(mint)

            if not witnessed:
                # We found it there.  We did not see it arrive, and saying
                # otherwise would date a fabricated entry to this instant.
                if existing is None:
                    records[mint] = MembershipRecord(
                        mint=mint,
                        first_observed_on_board_at=snapshot.observed_at,
                        first_trending_at=None,
                        first_rank=row.rank,
                        first_market_cap_usd=row.market_cap_usd,
                        state=MEMBERSHIP_PRESENT_UNPROVEN,
                        grade=grade,
                        last_seen_on_board_at=snapshot.observed_at,
                        entries=1,
                        stints=((snapshot.observed_at, None),),
                        unproven_reason=gap_reason,
                    )
                    unproven.append(mint)
                    events.append(
                        self._event(unproven_kind, row, snapshot, grade=grade)
                    )
                else:
                    # A known mint reappearing across a gap: the stint is real,
                    # the first entry (proven or not) is untouched.
                    records[mint] = replace(
                        existing,
                        state=(
                            MEMBERSHIP_REENTERED
                            if existing.entry_proven
                            else MEMBERSHIP_PRESENT_UNPROVEN
                        ),
                        last_seen_on_board_at=snapshot.observed_at,
                        left_at=None,
                        entries=existing.entries + 1,
                        stints=existing.stints + ((snapshot.observed_at, None),),
                    )
                    if existing.entry_proven:
                        reentered.append(mint)
                        events.append(
                            self._event(reenter_kind, row, snapshot, grade=grade)
                        )
                    else:
                        unproven.append(mint)
                        events.append(
                            self._event(unproven_kind, row, snapshot, grade=grade)
                        )
                continue

            if existing is None:
                # A genuine, witnessed first arrival.  This timestamp is now
                # frozen for all time.
                records[mint] = MembershipRecord(
                    mint=mint,
                    first_observed_on_board_at=snapshot.observed_at,
                    first_trending_at=snapshot.observed_at,
                    first_rank=row.rank,
                    first_market_cap_usd=row.market_cap_usd,
                    state=MEMBERSHIP_ENTERED,
                    grade=grade,
                    last_seen_on_board_at=snapshot.observed_at,
                    entries=1,
                    stints=((snapshot.observed_at, None),),
                )
                entered.append(mint)
                events.append(self._event(enter_kind, row, snapshot, grade=grade))
            elif not existing.entry_proven:
                # We had found it on the board without seeing it arrive; it has
                # since left and come back, and THIS arrival we did witness.
                #
                # It is tempting to promote that to a proven first entry.  It is
                # wrong: this mint's FIRST entry happened before we started
                # watching, so recording the return as the first entry would
                # understate how long it had been on the board and flatter every
                # lead time measured against it.  A missed first entry stays
                # missed, permanently.  The re-entry is real and is recorded as
                # exactly that.
                records[mint] = replace(
                    existing,
                    state=MEMBERSHIP_REENTERED,
                    last_seen_on_board_at=snapshot.observed_at,
                    left_at=None,
                    entries=existing.entries + 1,
                    stints=existing.stints + ((snapshot.observed_at, None),),
                )
                reentered.append(mint)
                events.append(self._event(reenter_kind, row, snapshot, grade=grade))
            else:
                # A later stint.  first_trending_at is re-used from the existing
                # record and never recomputed.
                records[mint] = replace(
                    existing,
                    state=MEMBERSHIP_REENTERED,
                    last_seen_on_board_at=snapshot.observed_at,
                    left_at=None,
                    entries=existing.entries + 1,
                    stints=existing.stints + ((snapshot.observed_at, None),),
                )
                reentered.append(mint)
                events.append(self._event(reenter_kind, row, snapshot, grade=grade))

        left: list[str] = []
        for mint in sorted(previous - current):
            existing = records.get(mint)
            if existing is None:
                continue
            stints = existing.stints
            if stints and stints[-1][1] is None:
                stints = stints[:-1] + ((stints[-1][0], snapshot.observed_at),)
            records[mint] = replace(
                existing,
                state=MEMBERSHIP_LEFT,
                left_at=snapshot.observed_at,
                stints=stints,
            )
            left.append(mint)
            events.append(
                TrendEntryEvent(
                    kind=leave_kind,
                    mint=mint,
                    occurred_at=snapshot.observed_at,
                    provider=snapshot.provider,
                    source_kind=snapshot.source_kind,
                    grade=grade,
                    source_at=snapshot.source_at,
                    collector_at=snapshot.observed_at,
                    collector_version=snapshot.collector_version or self.collector_version,
                )
            )

        active: list[str] = []
        for mint in sorted(current & previous):
            existing = records.get(mint)
            if existing is not None:
                records[mint] = replace(
                    existing,
                    state=(
                        MEMBERSHIP_ACTIVE
                        if existing.entry_proven
                        else MEMBERSHIP_PRESENT_UNPROVEN
                    ),
                    last_seen_on_board_at=snapshot.observed_at,
                )
            active.append(mint)

        self._on_board[grade] = set(current)
        self._last_observed_at[grade] = snapshot.observed_at
        self._last_size[grade] = len(snapshot.rows)

        return SnapshotOutcome(
            validity=validity,
            events=tuple(events),
            entered=tuple(entered),
            reentered=tuple(reentered),
            left=tuple(left),
            active=tuple(active),
            present_unproven=tuple(unproven),
            grade=grade,
            after_coverage_gap=not witnessed,
        )

    # ------------------------------------------------------------------
    def _event(
        self, kind: str, row: BoardRow, snapshot: TrendingSnapshot, *, grade: str
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
            grade=grade,
            source_at=row.source_at if row.source_at is not None else snapshot.source_at,
            collector_at=snapshot.observed_at,
            collector_version=snapshot.collector_version or self.collector_version,
            raw=row.raw,
        )

    # ------------------------------------------------------------------
    def health(self, *, now: int | None = None) -> dict[str, Any]:
        moment = now if now is not None else int(time.time())
        total = self.accepted_snapshots + self.rejected_snapshots
        last_fomo = self._last_observed_at[GRADE_FOMO]
        return {
            "accepted_snapshots": self.accepted_snapshots,
            "rejected_snapshots": self.rejected_snapshots,
            "acceptance_rate": (
                None if total == 0 else round(self.accepted_snapshots / total, 4)
            ),
            "rejections": dict(sorted(self.rejections.items())),
            "coverage_gaps": self.coverage_gaps,
            "fomo_tracked_mints": len(self._records[GRADE_FOMO]),
            "proxy_tracked_mints": len(self._records[GRADE_PROXY]),
            "fomo_on_board": len(self._on_board[GRADE_FOMO]),
            "proxy_on_board": len(self._on_board[GRADE_PROXY]),
            # The number that actually matters: how many supervised targets
            # exist.  A deployment running on the proxy will show zero here
            # forever, which is the honest answer.
            "usable_labels": len(self.label_map()),
            "entry_unproven": len(self.entry_unproven_mints()),
            "last_fomo_snapshot_at": last_fomo,
            "last_proxy_snapshot_at": self._last_observed_at[GRADE_PROXY],
            "seconds_since_fomo_snapshot": (
                None if last_fomo is None else moment - last_fomo
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
