"""Ground truth: the label event, and every way it could be corrupted.

These tests are not about features or models.  They are about the one fact the
whole research programme is measured against — *when did this exact mint FIRST
appear on FOMO Trending* — and the specific failures that would silently
poison it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from smart_money_bot.pretrend.groundtruth import (
    FOMO_TREND_ENTER,
    FOMO_TREND_LEAVE,
    FOMO_TREND_REENTER,
    MEMBERSHIP_ACTIVE,
    MEMBERSHIP_LEFT,
    MEMBERSHIP_REENTERED,
    SNAPSHOT_DUPLICATE,
    SNAPSHOT_EMPTY,
    SNAPSHOT_PROVIDER_ERROR,
    SNAPSHOT_STALE,
    SNAPSHOT_TOO_SHORT,
    BoardRow,
    GroundTruthConfig,
    TrendingGroundTruth,
    TrendingSnapshot,
    rank_progression,
)
from smart_money_bot.pretrend.identity import (
    AMBIGUOUS,
    EXACT_CA_MATCH,
    NO_MATCH,
    STRONG_CA_MATCH,
    extract_mint,
    match_records,
)


def mint(seed: str) -> str:
    """A syntactically valid, distinct 44-character base58 mint."""

    return (seed * 44)[:44]


ALPHA = mint("A")
BRAVO = mint("B")
CHARLIE = mint("C")


def board(mints: list[str], *, at: int, **kwargs) -> TrendingSnapshot:
    return TrendingSnapshot(
        observed_at=at,
        rows=tuple(
            BoardRow(mint=item, rank=index, market_cap_usd=Decimal("50000"))
            for index, item in enumerate(mints, start=1)
        ),
        provider="test",
        source_kind="FOMO_TRENDING",
        **kwargs,
    )


def filler(count: int) -> list[str]:
    return [mint(chr(ord("d") + index)) for index in range(count)]


# --- identity ----------------------------------------------------------------
def test_the_exact_mint_is_the_only_identity() -> None:
    left = {"mint": ALPHA, "symbol": "CAT"}
    right = {"mint": ALPHA, "symbol": "DOG"}
    result = match_records(left, right)
    assert result.state == EXACT_CA_MATCH
    assert result.mint == ALPHA
    # Different symbols do not weaken an exact-mint match: the mint is identity
    # and a display name is not.
    assert result.confirmed


def test_the_same_ticker_on_two_mints_is_never_a_match() -> None:
    """The single most common way one token's evidence contaminates another."""

    result = match_records({"mint": ALPHA, "symbol": "CAT"}, {"mint": BRAVO, "symbol": "CAT"})
    assert result.state == NO_MATCH
    assert not result.confirmed


def test_the_same_name_on_two_mints_is_never_a_match() -> None:
    result = match_records({"mint": ALPHA, "name": "Good Boy"}, {"mint": BRAVO, "name": "Good Boy"})
    assert result.state == NO_MATCH


def test_a_shared_ticker_without_a_mint_is_ambiguous_not_confirmed() -> None:
    result = match_records({"symbol": "CAT"}, {"symbol": "cat"})
    assert result.state == AMBIGUOUS
    assert not result.confirmed, "AMBIGUOUS must never be usable as confirmation"


def test_a_published_reference_is_a_strong_match_but_recorded_separately() -> None:
    result = match_records(
        {"mint": ALPHA, "token_id": "fomo-91821"}, {"token_id": "fomo-91821"}
    )
    assert result.state == STRONG_CA_MATCH
    assert result.confirmed
    assert not result.exact, "a derived match must not be recorded as an exact one"


def test_two_mints_in_one_blob_of_text_resolve_to_nothing() -> None:
    """Picking the first of two candidates would be a guess wearing a result."""

    assert extract_mint(f"see {ALPHA} or maybe {BRAVO}") is None
    assert extract_mint(f"the contract is {ALPHA}") == ALPHA


# --- first entry -------------------------------------------------------------
def test_first_entry_emits_exactly_one_enter_event() -> None:
    truth = TrendingGroundTruth()
    outcome = truth.ingest(board([ALPHA, *filler(6)], at=1_000))

    assert outcome.accepted
    enters = [event for event in outcome.events if event.kind == FOMO_TREND_ENTER]
    assert len(enters) == 7
    assert truth.first_trending_at(ALPHA) == 1_000


def test_first_trending_at_is_never_overwritten_by_a_reentry() -> None:
    """Without this, every late alert looks early in hindsight."""

    truth = TrendingGroundTruth()
    others = filler(6)
    truth.ingest(board([ALPHA, *others], at=1_000))
    truth.ingest(board(others + [mint("z")], at=1_060))  # ALPHA leaves
    outcome = truth.ingest(board([ALPHA, *others], at=1_120))  # and returns

    assert truth.first_trending_at(ALPHA) == 1_000, "the first entry moved"
    kinds = {event.kind for event in outcome.events if event.mint == ALPHA}
    assert kinds == {FOMO_TREND_REENTER}
    record = truth.record(ALPHA)
    assert record is not None
    assert record.state == MEMBERSHIP_REENTERED
    assert record.entries == 2
    assert len(record.stints) == 2


def test_leaving_the_board_records_a_leave_and_keeps_the_first_entry() -> None:
    truth = TrendingGroundTruth()
    others = filler(6)
    truth.ingest(board([ALPHA, *others], at=1_000))
    outcome = truth.ingest(board(others + [mint("z")], at=1_060))

    assert ALPHA in outcome.left
    assert any(
        event.kind == FOMO_TREND_LEAVE and event.mint == ALPHA for event in outcome.events
    )
    record = truth.record(ALPHA)
    assert record is not None
    assert record.state == MEMBERSHIP_LEFT
    assert record.first_trending_at == 1_000


def test_a_mint_present_across_snapshots_becomes_active_without_new_events() -> None:
    truth = TrendingGroundTruth()
    mints = [ALPHA, *filler(6)]
    truth.ingest(board(mints, at=1_000))
    outcome = truth.ingest(board(mints, at=1_060))

    assert outcome.events == ()
    assert truth.record(ALPHA).state == MEMBERSHIP_ACTIVE


# --- snapshot validity -------------------------------------------------------
def test_a_failed_snapshot_never_empties_the_board() -> None:
    """The failure this whole validity layer exists to prevent.

    One network blip treated as an empty board would emit a LEFT event for
    every mint, then an ENTER for all of them on the next success — hundreds of
    fabricated ground-truth events, permanently in the training labels.
    """

    truth = TrendingGroundTruth()
    mints = [ALPHA, BRAVO, *filler(6)]
    truth.ingest(board(mints, at=1_000))

    failed = TrendingSnapshot(observed_at=1_060, rows=(), provider="test", error="timeout")
    outcome = truth.ingest(failed)

    assert not outcome.accepted
    assert outcome.validity.reason == SNAPSHOT_PROVIDER_ERROR
    assert outcome.left == ()
    assert outcome.events == ()
    assert truth.on_board() == frozenset(mints), "membership must be untouched"

    # And the next good snapshot produces no spurious re-entries.
    recovered = truth.ingest(board(mints, at=1_120))
    assert recovered.entered == ()
    assert recovered.reentered == ()


def test_an_empty_payload_without_an_error_is_still_refused() -> None:
    truth = TrendingGroundTruth()
    truth.ingest(board([ALPHA, *filler(6)], at=1_000))
    outcome = truth.ingest(TrendingSnapshot(observed_at=1_060, rows=(), provider="test"))
    assert outcome.validity.reason == SNAPSHOT_EMPTY
    assert outcome.left == ()


def test_a_partial_board_is_refused_rather_than_read_as_mass_departure() -> None:
    truth = TrendingGroundTruth()
    mints = [ALPHA, BRAVO, CHARLIE, *filler(9)]
    truth.ingest(board(mints, at=1_000))
    # Twelve rows collapse to five: a shrink of ~58% is below the 60% default,
    # so push it further to prove the guard fires.
    outcome = truth.ingest(board(mints[:3], at=1_060))
    assert not outcome.accepted
    assert outcome.validity.reason == SNAPSHOT_TOO_SHORT
    assert outcome.left == ()


def test_a_short_board_below_the_row_floor_is_refused() -> None:
    truth = TrendingGroundTruth(config=GroundTruthConfig(min_rows=5))
    outcome = truth.ingest(board([ALPHA, BRAVO], at=1_000))
    assert not outcome.accepted
    assert outcome.validity.reason == SNAPSHOT_TOO_SHORT


def test_a_duplicate_snapshot_changes_nothing() -> None:
    truth = TrendingGroundTruth()
    mints = [ALPHA, *filler(6)]
    first = truth.ingest(board(mints, at=1_000))
    assert first.accepted

    repeat = truth.ingest(board(mints, at=1_000))
    assert not repeat.accepted
    assert repeat.validity.reason == SNAPSHOT_DUPLICATE
    assert repeat.events == ()


def test_a_stale_provider_timestamp_is_refused() -> None:
    truth = TrendingGroundTruth()
    snapshot = TrendingSnapshot(
        observed_at=10_000,
        rows=tuple(BoardRow(mint=item) for item in [ALPHA, *filler(6)]),
        provider="test",
        source_at=9_000,
    )
    outcome = truth.ingest(snapshot)
    assert not outcome.accepted
    assert outcome.validity.reason == SNAPSHOT_STALE


def test_a_partial_read_with_rows_and_an_error_is_refused() -> None:
    """Rows plus an error means an incomplete board, which fabricates departures."""

    truth = TrendingGroundTruth()
    mints = [ALPHA, BRAVO, *filler(6)]
    truth.ingest(board(mints, at=1_000))
    partial = board(mints[:6], at=1_060, error="upstream 502 on page 2")
    outcome = truth.ingest(partial)
    assert not outcome.accepted
    assert outcome.left == ()


def test_a_row_without_a_resolvable_mint_is_not_a_membership_fact() -> None:
    assert BoardRow.from_payload({"symbol": "CAT", "name": "Cat"}) is None
    row = BoardRow.from_payload({"mint": ALPHA, "symbol": "CAT"}, rank=3)
    assert row is not None and row.mint == ALPHA and row.rank == 3


def test_a_board_row_requires_a_mint() -> None:
    with pytest.raises(ValueError):
        BoardRow(mint="")


# --- health and rank ---------------------------------------------------------
def test_rejections_are_counted_so_a_gap_is_explainable() -> None:
    truth = TrendingGroundTruth()
    truth.ingest(board([ALPHA, *filler(6)], at=1_000))
    truth.ingest(TrendingSnapshot(observed_at=1_060, rows=(), provider="t", error="boom"))
    truth.ingest(TrendingSnapshot(observed_at=1_120, rows=(), provider="t"))

    health = truth.health(now=1_200)
    assert health["accepted_snapshots"] == 1
    assert health["rejected_snapshots"] == 2
    assert health["rejections"][SNAPSHOT_PROVIDER_ERROR] == 1
    assert health["rejections"][SNAPSHOT_EMPTY] == 1


def test_rank_progression_reads_backwards_never_forwards() -> None:
    snapshots = [
        TrendingSnapshot(
            observed_at=1_000 + step * 30,
            rows=(BoardRow(mint=ALPHA, rank=12 - step),),
            provider="t",
        )
        for step in range(6)
    ]
    progression = rank_progression(snapshots, ALPHA, entry_at=1_000)
    assert progression["initial_rank"] == 12
    assert progression["rank_1m"] == 10
    assert progression["best_rank"] == 7
    # No snapshot exists an hour out, so the answer is None rather than the
    # most recent value carried forward.
    assert rank_progression(snapshots[:1], ALPHA, entry_at=1_000)["rank_5m"] == 12
