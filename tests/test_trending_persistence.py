"""Schema, persistence and restart-safety for the Trending lane (sections 110, 111).

Three properties are load-bearing here:

* the schema is **additive and idempotent** — re-running it on a populated
  database changes nothing and loses nothing (section 110);
* first observations are **write-once at the SQL level**, not merely by
  convention in the caller (sections 5, 93);
* and everything the loops need comes back after a restart, with the original
  timing evidence intact (section 111).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from smart_money_bot.database import Database
from smart_money_bot.trending import (
    ORIGIN_TRENDING_NEAR_MISS,
    AuthorReputation,
    HotWatchConfig,
    ThesisRecord,
    TrendingLedgerEntry,
    TrendingObservation,
    build_thesis_panel,
    open_hot_watch,
    recheck_hot_watch,
    source_from_settings,
)
from smart_money_bot.trending_store import TrendingStore

D = Decimal
NOW = 1_700_000_000
PROXY = source_from_settings(api_url=None, api_key=None, proxy_enabled=True)

#: Every table this release adds.  A missing one is a silent feature outage.
TRENDING_TABLES = (
    "trending_tokens",
    "trending_snapshots",
    "trending_events",
    "trending_hot_watch",
    "trending_theses",
    "trending_thesis_authors",
    "trending_about",
    "trending_latency",
    "trending_suppression",
    "trending_missed",
)

#: Tables that existed before this release and must be untouched by it.
PRE_EXISTING_TABLES = (
    "shadow_experiment",
    "shadow_positions",
    "shadow_bankroll",
    "shadow_exits",
    "lab_positions",
    "paper_trades",
    "runner_candidates",
    "alert_timeline",
    "narratives",
)


@pytest.fixture
async def database():
    db = Database(":memory:", D("1000"))
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


async def _tables(database) -> set[str]:
    cursor = await database.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {row["name"] for row in await cursor.fetchall()}


def _obs(mint="MintAAA", *, at=NOW, rank=40, cap="200000", **kwargs):
    return TrendingObservation(
        mint=mint,
        observed_at=at,
        rank=rank,
        market_cap_usd=D(cap),
        source=PROXY,
        **kwargs,
    )


async def test_the_new_schema_is_additive(database) -> None:
    """Section 110: nothing pre-existing is dropped, renamed or altered."""

    tables = await _tables(database)
    for name in TRENDING_TABLES:
        assert name in tables, name
    for name in PRE_EXISTING_TABLES:
        assert name in tables, f"{name} must survive the Trending upgrade"


async def test_the_schema_upgrade_is_idempotent_and_loses_nothing(database) -> None:
    """Section 110: a restart re-runs the whole block harmlessly."""

    store = TrendingStore(database)
    entry = TrendingLedgerEntry.from_first_observation(_obs())
    await store.record_observation(entry, _obs())

    before = await _tables(database)
    # Exactly what happens on every process start.
    await database._init_schema()
    await database._init_schema()
    after = await _tables(database)

    assert before == after
    survivor = await store.load_entry("MintAAA")
    assert survivor is not None
    assert survivor.first_market_cap_usd == D("200000")


async def test_the_ledger_upsert_cannot_rewrite_a_first_observation(database) -> None:
    """Section 93: immutability is enforced by the SQL, not by good manners.

    Even a caller that hands the store a deliberately corrupted entry — one whose
    ``first_*`` fields have been reassigned — must not be able to move the
    persisted entry numbers, because ``DO UPDATE SET`` never lists those columns.
    """

    store = TrendingStore(database)
    original = TrendingLedgerEntry.from_first_observation(
        _obs(rank=44, cap="120000", holder_count=90)
    )
    await store.record_observation(original, _obs(rank=44, cap="120000", holder_count=90))

    from dataclasses import replace

    tampered = replace(
        original,
        first_rank=1,
        first_market_cap_usd=D("999999"),
        first_seen_at=NOW - 100_000,
        first_holder_count=1,
        current_rank=5,
        last_observed_at=NOW + 60,
    )
    await store.record_observation(tampered)

    cursor = await database.db.execute(
        "SELECT first_rank, first_market_cap_usd, first_seen_at, first_holder_count,"
        " current_rank FROM trending_tokens WHERE mint = ?",
        ("MintAAA",),
    )
    row = await cursor.fetchone()
    assert row["first_rank"] == 44
    assert row["first_market_cap_usd"] == 120000.0
    assert row["first_seen_at"] == NOW
    assert row["first_holder_count"] == 90
    # The mutable half did move, which is the point of the upsert.
    assert row["current_rank"] == 5

    # And the *read path* must agree.  The upsert replaces payload_json
    # wholesale, so a loader that trusted the JSON would round-trip the
    # corruption straight back out and the guarantee would be cosmetic.
    reloaded = await store.load_entry("MintAAA")
    assert reloaded is not None
    assert reloaded.first_rank == 44
    assert reloaded.first_market_cap_usd == D("120000")
    assert reloaded.first_seen_at == NOW
    assert reloaded.first_holder_count == 90
    assert reloaded.current_rank == 5

    board = await store.load_board()
    assert board and board[0].first_rank == 44


async def test_rank_history_and_velocity_survive_a_restart(database) -> None:
    """Section 111: velocity is only measurable if the samples persist."""

    store = TrendingStore(database)
    entry = TrendingLedgerEntry.from_first_observation(_obs(rank=40))
    await store.record_observation(entry, _obs(rank=40))
    for step, rank in enumerate([31, 18, 9], start=1):
        observation = _obs(at=NOW + step * 60, rank=rank)
        entry = entry.observe(observation)
        await store.record_observation(entry, observation)

    history = await store.rank_history("MintAAA")
    assert [point.rank for point in history] == [40, 31, 18, 9]
    assert [point.at for point in history] == [NOW, NOW + 60, NOW + 120, NOW + 180]


async def test_a_board_exit_is_recorded_and_never_deletes_history(database) -> None:
    store = TrendingStore(database)
    entry = TrendingLedgerEntry.from_first_observation(_obs())
    await store.record_observation(entry, _obs())
    await store.mark_left_board(("MintAAA",), at=NOW + 600)

    survivor = await store.load_entry("MintAAA")
    assert survivor is not None
    assert survivor.first_market_cap_usd == D("200000")
    assert await store.tracked_count() == 0
    assert await store.rank_history("MintAAA")


async def test_an_active_hot_watch_restores_with_its_timing_evidence(database) -> None:
    """Section 111: a redeploy must not reset the promotion clock."""

    store = TrendingStore(database)
    config = HotWatchConfig(ttl_seconds=900, recheck_seconds=30)
    watch = open_hot_watch(
        "MintAAA",
        origin=ORIGIN_TRENDING_NEAR_MISS,
        now=NOW,
        score=D("52"),
        market_cap_usd=D("500000"),
        heads_up_market_cap_usd=D("500000"),
        first_seen_market_cap_usd=D("310000"),
        config=config,
    )
    await store.save_hot_watch(watch)

    restored = await store.active_hot_watches()
    assert len(restored) == 1
    assert restored[0].entered_at == NOW
    assert restored[0].heads_up_market_cap_usd == D("500000")
    assert restored[0].first_seen_market_cap_usd == D("310000")
    assert restored[0].entry_score == D("52")


async def test_a_promotion_is_persisted_with_its_lateness(database) -> None:
    """Section 49: heads-up $500K → promotion $1M means the promotion was late."""

    store = TrendingStore(database)
    watch = open_hot_watch(
        "MintAAA",
        origin=ORIGIN_TRENDING_NEAR_MISS,
        now=NOW,
        score=D("52"),
        market_cap_usd=D("500000"),
        heads_up_market_cap_usd=D("500000"),
    )
    await store.save_hot_watch(watch)
    outcome = recheck_hot_watch(
        watch,
        now=NOW + 120,
        score=D("70"),
        reasons=("TRENDING_ACCELERATION",),
        market_cap_usd=D("1000000"),
        alpha_threshold=D("62"),
    )
    await store.save_hot_watch(outcome.entry)

    history = await store.hot_watch_history()
    assert len(history) == 1
    assert history[0].promotion_move_percent() == D("100.0")
    assert history[0].promotion_delay_seconds() == 120
    # And it is no longer restored as active work.
    assert await store.active_hot_watches() == []


async def test_latency_stamps_are_write_once(database) -> None:
    """Section 79: a stamp that could move measures nothing."""

    store = TrendingStore(database)
    await store.stamp_latency("MintAAA", "BOT_OBSERVATION", at=NOW, market_cap_usd=D("100"))
    await store.stamp_latency("MintAAA", "BOT_OBSERVATION", at=NOW + 500, market_cap_usd=D("900"))

    rows = await store.latency_rows()
    stamps = [row for row in rows if row["stage"] == "BOT_OBSERVATION"]
    assert len(stamps) == 1
    assert stamps[0]["occurred_at"] == NOW
    assert stamps[0]["market_cap_usd"] == 100.0


async def test_suppressions_and_misses_are_queryable(database) -> None:
    """Section 91: silence is always explainable."""

    store = TrendingStore(database)
    await store.record_suppression(
        "MintAAA", reason_code="HOT_WATCH", at=NOW, score=D("52"), detail="near miss"
    )
    await store.record_suppression(
        "MintBBB", reason_code="EDGE_CONSUMED", at=NOW + 1, score=D("20")
    )
    counts = await store.suppression_counts()
    assert counts["HOT_WATCH"] == 1
    assert counts["EDGE_CONSUMED"] == 1

    await store.record_missed(
        "MintAAA",
        miss_class="MISSED_TRENDING_RUNNER",
        observed_at=NOW,
        market_cap_at_observation_usd=D("100000"),
        peak_market_cap_usd=D("400000"),
        move_percent=D("300"),
        suppression_reason="NOT_STRONG_ENOUGH",
    )
    rows = await store.missed_rows()
    assert rows[0]["miss_class"] == "MISSED_TRENDING_RUNNER"
    assert rows[0]["move_percent"] == 300.0


async def test_theses_persist_against_the_exact_mint(database) -> None:
    """Section 13: a thesis row belongs to one mint and only that mint."""

    store = TrendingStore(database)
    panel = build_thesis_panel(
        "MintAAA",
        [
            ThesisRecord(
                mint="MintAAA",
                author="alice",
                posted_at=NOW,
                text="The bridge shipped on Mar 14, repo at https://x.example/r",
                market_cap_at_thesis_usd=D("100000"),
            ),
            # A same-story thesis about a different mint must not be adopted.
            ThesisRecord(
                mint="MintBBB",
                author="bob",
                posted_at=NOW,
                text="The bridge shipped on Mar 14, repo at https://x.example/r",
            ),
        ],
    )
    assert panel.total == 1
    for assessment in panel.assessments:
        await store.save_thesis(assessment)

    rows = await store.theses_for("MintAAA")
    assert len(rows) == 1
    assert rows[0]["author"] == "alice"
    assert await store.theses_for("MintBBB") == []


async def test_author_reputation_round_trips(database) -> None:
    store = TrendingStore(database)
    await store.save_author_reputation(
        AuthorReputation(
            author="alice",
            sample=12,
            avg_forward_move_percent=D("40"),
            severe_failures=1,
        )
    )
    reputations = await store.author_reputations()
    assert reputations["alice"].sample == 12
    assert reputations["alice"].credible is True


async def test_about_records_keep_claim_and_corroboration_apart(database) -> None:
    """Section 17: the two must be separate columns, not one blended field."""

    store = TrendingStore(database)
    await store.save_about(
        "MintAAA",
        summary="Claims a quantum AI platform.",
        claims=("AI",),
        website="https://example.invalid",
        has_official_claim=True,
        external_state="UNVERIFIED",
        token_link="UNVERIFIED",
        mentions_exact_mint=False,
    )
    row = await store.about_for("MintAAA")
    assert row["external_state"] == "UNVERIFIED"
    assert row["mentions_exact_mint"] == 0
    assert row["has_official_claim"] == 1


async def test_snapshot_pruning_never_touches_the_ledger(database) -> None:
    """Section 112: bound snapshot growth without losing the entry numbers."""

    store = TrendingStore(database)
    entry = TrendingLedgerEntry.from_first_observation(_obs())
    await store.record_observation(entry, _obs())
    later = _obs(at=NOW + 100_000, rank=5)
    await store.record_observation(entry.observe(later), later)

    removed = await store.prune_snapshots(older_than=NOW + 50_000)
    assert removed == 1
    survivor = await store.load_entry("MintAAA")
    assert survivor is not None
    assert survivor.first_market_cap_usd == D("200000")
