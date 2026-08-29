"""Regression suite for the v2.37 speed / current-edge / reliability update.

Four production problems are locked down here:

1. Source→first-seen latency was measured against *pair creation* time, so an
   old pair appearing on a trending feed reported an ~19-hour "ingestion
   latency" that no loop could produce.
2. `/fomo opportunities` and `/fomo lab mode:test` hit Discord HTTP 400 / 50035
   because the 6000-character budget is per *message*, not per embed.
3. A pre-v2.36 mint re-initialised as `FIRST_DISCOVERY • FRESH`, defeating
   old-pump memory.
4. A stale, materially negative candidate stayed ranked beside fresh setups.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from smart_money_bot.database import Database
from smart_money_bot.discord_render import (
    MESSAGE_EMBED_LIMIT,
    OPTIONAL_PRIORITY_FLOOR,
    P_ABOUT,
    P_DECISION,
    P_DIAGNOSTICS,
    P_IDENTITY,
    P_SAFETY,
    P_SMART_MONEY,
    SAFE_MESSAGE_BUDGET,
    CardField,
    CardSpec,
    build_embed,
    describe_render,
    is_embed_too_large,
    render_message,
    resolve_with_cards,
)
from smart_money_bot.lab.actionability import (
    ACTIONABLE,
    DETERIORATED,
    EDGE_CONSUMED,
    R_FLOW_WEAKENING,
    R_NEGATIVE_SINCE_FIRST_SEEN,
    Actionability,
    ActionabilityInputs,
    RankedCandidate,
    assess_actionability,
    rank_by_current_edge,
    split_current_radar,
)
from smart_money_bot.lab.backfill import (
    COMPLETE,
    LegacyEvidence,
    LegacyObservation,
    merge_backfill,
    reconstruct_lifecycle,
)
from smart_money_bot.lab.fastwatch import (
    ALL_PENDING,
    B_LIQUIDITY_TOO_THIN,
    FastWatchSignals,
    FastWatchVerdict,
    evaluate_fast_watch,
    still_current,
)
from smart_money_bot.lab.latency import (
    HISTORICAL,
    REALTIME,
    STAGE_SOURCE_TO_FIRST_SEEN,
    UNKNOWN,
    LatencySample,
    outcome_by_latency_band,
    pipeline_breakdown,
    slowest_stage,
    summarize_sources,
    summarize_stage,
)
from smart_money_bot.lab.lifecycle import FIRST_DISCOVERY, new_lifecycle
from smart_money_bot.lab_store import LabStore

MINT = "So11111111111111111111111111111111111111112"
D = Decimal


@pytest.fixture
async def database(tmp_path):
    db = Database(str(tmp_path / "speed.db"), D("100"))
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# discovery pipeline (section 53)
# ---------------------------------------------------------------------------


async def test_first_seen_persists_immediately_on_cheap_discovery(database) -> None:
    first = await database.record_discovery(
        mint=MINT, source_name="dexscreener_trending", source_event_at=900, now=1_000
    )
    assert first is True
    rows = await database.discovery_latency_rows()
    assert rows[0]["first_seen_at"] == 1_000
    assert rows[0]["source_name"] == "dexscreener_trending"


async def test_a_slow_provider_cannot_delay_first_seen(database) -> None:
    """Enrichment happens after the ledger write, so it cannot move first-seen."""

    await database.record_discovery(
        mint=MINT, source_name="dexscreener_trending", source_event_at=900, now=1_000
    )
    # Simulate the enrichment finishing 400 seconds later and re-reporting.
    await database.record_discovery(
        mint=MINT, source_name="dexscreener_trending", source_event_at=900, now=1_400
    )
    rows = await database.discovery_latency_rows()
    assert rows[0]["first_seen_at"] == 1_000


async def test_duplicate_discovery_is_idempotent(database) -> None:
    assert await database.record_discovery(
        mint=MINT, source_name="s", source_event_at=None, now=1_000
    )
    for _ in range(3):
        await database.record_discovery(
            mint=MINT, source_name="s", source_event_at=None, now=1_100
        )
    assert await database.discovery_count() == 1
    rows = await database.discovery_latency_rows()
    assert rows[0]["first_seen_at"] == 1_000


async def test_stage_times_are_write_once_and_never_move_earlier(database) -> None:
    await database.record_discovery(mint=MINT, source_name="s", source_event_at=None, now=1_000)
    await database.mark_discovery_stage(mint=MINT, stage="watch", at=1_010)
    await database.mark_discovery_stage(mint=MINT, stage="watch", at=1_500)
    await database.mark_discovery_stage(mint=MINT, stage="qualified", at=1_090)
    rows = await database.discovery_latency_rows()
    assert rows[0]["first_watch_at"] == 1_010
    assert rows[0]["first_qualified_at"] == 1_090


async def test_source_timestamp_is_never_rewritten(database) -> None:
    await database.record_discovery(mint=MINT, source_name="s", source_event_at=900, now=1_000)
    await database.record_discovery(mint=MINT, source_name="s", source_event_at=500, now=1_100)
    rows = await database.discovery_latency_rows()
    assert rows[0]["source_event_at"] == 900


# ---------------------------------------------------------------------------
# latency forensics (sections 12, 13, 47)
# ---------------------------------------------------------------------------


def _fast_samples() -> list[LatencySample]:
    return [
        LatencySample(
            mint=f"f{index}",
            source_name="dexscreener_trending",
            source_event_at=1_000,
            first_seen_at=1_000 + delay,
            first_watch_at=1_000 + delay + 5,
            first_qualified_at=1_000 + delay + 90,
        )
        for index, delay in enumerate([20, 45, 100, 250, 400])
    ]


def test_a_historical_pair_never_pollutes_realtime_latency() -> None:
    """The ~19-hour p90 was an old pair, not a slow loop."""

    samples = [
        *_fast_samples(),
        LatencySample(
            mint="old",
            source_name="dexscreener_trending",
            source_event_at=1_000,
            first_seen_at=1_000 + 67_620,
        ),
    ]
    stale = samples[-1]
    assert stale.timing_quality == HISTORICAL
    assert not stale.counts_as_realtime

    stats = summarize_stage(samples, STAGE_SOURCE_TO_FIRST_SEEN)
    assert stats.count == 5
    assert stats.p90 is not None
    assert stats.p90 < D("1000")


def test_historical_samples_are_still_counted_and_labelled() -> None:
    samples = [
        *_fast_samples(),
        LatencySample(
            mint="old", source_name="dexscreener_trending",
            source_event_at=1_000, first_seen_at=99_999,
        ),
    ]
    sources = summarize_sources(samples)
    assert len(sources) == 1
    assert sources[0].historical_count == 1
    assert sources[0].realtime.count == 5
    assert sources[0].total == 6


def test_a_missing_source_timestamp_is_unknown_not_zero() -> None:
    sample = LatencySample(mint="x", source_name="s", source_event_at=None, first_seen_at=10)
    assert sample.timing_quality == UNKNOWN
    assert not sample.counts_as_realtime
    assert summarize_stage([sample], STAGE_SOURCE_TO_FIRST_SEEN).count == 0


def test_a_genuinely_fast_sample_grades_realtime() -> None:
    sample = LatencySample(mint="x", source_name="s", source_event_at=1_000, first_seen_at=1_020)
    assert sample.timing_quality == REALTIME


def test_pipeline_breakdown_makes_the_slow_stage_obvious() -> None:
    breakdown = pipeline_breakdown(_fast_samples())
    assert breakdown[STAGE_SOURCE_TO_FIRST_SEEN].count == 5
    assert slowest_stage(breakdown) == STAGE_SOURCE_TO_FIRST_SEEN


def test_outcome_is_reported_by_latency_band() -> None:
    bands = outcome_by_latency_band(
        [(20, D("30"), False), (25, D("40"), False), (700, D("-40"), True)]
    )
    quick = next(item for item in bands if item.label == "<=30s")
    slow = next(item for item in bands if item.label == ">10m")
    assert quick.count == 2
    assert quick.severe_failure_percent == D("0.00")
    assert slow.severe_failure_percent == D("100.00")


# ---------------------------------------------------------------------------
# fast watch (sections 6, 7, 41, 55)
# ---------------------------------------------------------------------------


def _hot_signals(**overrides) -> FastWatchSignals:
    base = {
        "now": 1_000,
        "pair_age_seconds": 300,
        "market_cap_usd": D("45000"),
        "first_seen_market_cap_usd": D("40000"),
        "market_cap_acceleration_ratio": D("1.4"),
        "price_change_percent": D("18"),
        "volume_acceleration_ratio": D("2.2"),
        "buys": 90,
        "sells": 25,
        "holder_growth": 25,
        "liquidity_usd": D("30000"),
        "liquidity_change_percent": D("22"),
        "route_available": True,
    }
    base.update(overrides)
    return FastWatchSignals(**base)


def test_accelerating_fresh_candidate_becomes_a_watch() -> None:
    verdict = evaluate_fast_watch(_hot_signals())
    assert verdict.watch
    assert verdict.reasons
    assert "HEATING UP" in verdict.label


def test_fast_watch_is_never_entry_eligible() -> None:
    """The structural guarantee: no FAST WATCH can authorise a PAPER entry."""

    assert evaluate_fast_watch(_hot_signals()).entry_eligible is False
    assert FastWatchVerdict(watch=True, score=D("100")).entry_eligible is False


def test_fast_watch_lists_the_evidence_it_did_not_wait_for() -> None:
    verdict = evaluate_fast_watch(_hot_signals())
    assert set(verdict.pending_evidence) == set(ALL_PENDING)
    assert "safety" in verdict.pending_evidence
    assert "economic authenticity" in verdict.pending_evidence


def test_fast_watch_needs_no_expensive_evidence_to_decide() -> None:
    """No forensics, no tracker risk, no social — it still decides."""

    verdict = evaluate_fast_watch(_hot_signals())
    assert verdict.watch is True


def test_obvious_blockers_stop_a_watch() -> None:
    thin = evaluate_fast_watch(_hot_signals(liquidity_usd=D("100")))
    assert not thin.watch
    assert B_LIQUIDITY_TOO_THIN in thin.blockers
    assert not evaluate_fast_watch(_hot_signals(rugged=True)).watch
    assert not evaluate_fast_watch(_hot_signals(route_available=False)).watch


def test_a_queued_candidate_cannot_publish_as_early() -> None:
    """Section 41: ten minutes in a queue is not an early watch."""

    ok, _ = still_current(_hot_signals(), first_seen_at=980)
    assert ok
    stale, reason = still_current(_hot_signals(), first_seen_at=400)
    assert not stale
    assert "queued" in reason


def test_publication_time_freshness_check_catches_a_collapse() -> None:
    dropped = _hot_signals(market_cap_usd=D("30000"), first_seen_market_cap_usd=D("40000"))
    ok, reason = still_current(dropped, first_seen_at=990)
    assert not ok
    assert "below first seen" in reason


# ---------------------------------------------------------------------------
# current actionability (section 56)
# ---------------------------------------------------------------------------


def _jelly() -> Actionability:
    return assess_actionability(
        ActionabilityInputs(
            now=10_000,
            first_seen_at=4_000,
            return_since_first_seen_percent=D("-21"),
            momentum_score=D("20"),
            buys=5,
            sells=30,
            drawdown_from_peak_percent=D("35"),
        )
    )


def _fresh() -> Actionability:
    return assess_actionability(
        ActionabilityInputs(
            now=10_000,
            first_seen_at=9_800,
            return_since_first_seen_percent=D("18"),
            momentum_score=D("75"),
            momentum_change=D("15"),
            buys=90,
            sells=25,
            independent_buyer_change=6,
            liquidity_change_percent=D("20"),
        )
    )


def test_the_jelly_case_is_suppressed_from_the_current_radar() -> None:
    jelly = _jelly()
    assert jelly.state == DETERIORATED
    assert jelly.suppressed
    assert R_NEGATIVE_SINCE_FIRST_SEEN in jelly.reasons
    assert R_FLOW_WEAKENING in jelly.reasons


def test_a_strong_current_candidate_is_actionable() -> None:
    fresh = _fresh()
    assert fresh.state == ACTIONABLE
    assert not fresh.suppressed
    assert fresh.score > D("60")


def test_edge_consumed_is_distinguished_from_mere_weakness() -> None:
    consumed = assess_actionability(
        ActionabilityInputs(
            now=10_000,
            first_seen_at=9_000,
            return_since_first_surface_percent=D("600"),
            momentum_score=D("70"),
            buys=80,
            sells=20,
        )
    )
    assert consumed.state == EDGE_CONSUMED
    assert consumed.suppressed


def test_a_historically_high_score_cannot_outrank_a_fresh_setup() -> None:
    """Section 17: an 86 that collapsed ranks below a genuine new accelerator."""

    ranked = rank_by_current_edge(
        [
            RankedCandidate(
                mint="old", actionability=_jelly(), historical_opportunity_score=D("86")
            ),
            RankedCandidate(
                mint="new", actionability=_fresh(), historical_opportunity_score=D("55")
            ),
        ]
    )
    assert [item.mint for item in ranked] == ["new", "old"]


def test_suppressed_candidates_are_returned_not_deleted() -> None:
    """Section 16: suppressed from the radar is not removed from the record."""

    current, suppressed = split_current_radar(
        [
            RankedCandidate(mint="old", actionability=_jelly()),
            RankedCandidate(mint="new", actionability=_fresh()),
        ]
    )
    assert [item.mint for item in current] == ["new"]
    assert [item.mint for item in suppressed] == ["old"]


def test_lifecycle_cooldown_suppresses_without_extra_rules() -> None:
    cooling = assess_actionability(
        ActionabilityInputs(now=10_000, first_seen_at=9_900, lifecycle_state="COOLDOWN",
                            momentum_score=D("70"), buys=50, sells=10)
    )
    assert cooling.suppressed


def test_no_current_evidence_ranks_below_anything_measured() -> None:
    blank = assess_actionability(ActionabilityInputs(now=1))
    assert not blank.evidence_present
    assert blank.score == D("0")


# ---------------------------------------------------------------------------
# legacy lifecycle backfill (section 57)
# ---------------------------------------------------------------------------


async def _seed_legacy(database, mint: str = "LEGACY") -> None:
    await database.db.execute(
        """
        INSERT INTO runner_candidates(
            mint, payload_json, first_seen_at, graduation_source, first_score,
            latest_score, tier, first_market_cap_usd, first_visible_market_cap_usd,
            peak_market_cap_usd, radar_first_seen_at, first_discord_visible_at, last_seen_at
        ) VALUES(?, '{}', 1000, 'TEST', 10, 20, 'T', 32000, 32000, 150000, 950, 1100, 9000)
        """,
        (mint,),
    )
    for at, price, cap in ((1_000, "0.000032", "32000"), (3_000, "0.00015", "150000"),
                           (9_000, "0.000038", "38000")):
        await database.db.execute(
            "INSERT INTO runner_snapshots(mint, captured_at, snapshot_json, score) VALUES(?,?,?,?)",
            (mint, at, json.dumps({"price_usd": price, "market_cap_usd": cap}), 10),
        )
    await database.db.execute(
        """
        INSERT INTO runner_alert_events(mint, event_type, fingerprint, first_sent_at,
                                        last_sent_at, send_count)
        VALUES(?, 'ALERT', 'f', 1100, 5000, 3)
        """,
        (mint,),
    )
    await database.db.execute(
        "INSERT INTO runner_stage_events(mint, stage, decided_at, safety_status)"
        " VALUES(?, 'QUALIFIED_RESEARCH', 1050, 'PASS')",
        (mint,),
    )
    await database.db.commit()


async def test_a_pre_v236_mint_no_longer_looks_brand_new(database) -> None:
    """The confirmed bug: legacy history made a token FIRST_DISCOVERY • FRESH."""

    await _seed_legacy(database)
    record = await LabStore(database).load_lifecycle("LEGACY", now=99_999)
    assert record.state != FIRST_DISCOVERY
    assert not record.is_fresh_setup
    assert record.first_discovered_at == 950
    assert record.first_surfaced_at == 1_100


async def test_backfill_recovers_the_real_history(database) -> None:
    await _seed_legacy(database)
    record = await LabStore(database).load_lifecycle("LEGACY", now=99_999)
    assert record.first_surface_market_cap_usd == D("32000")
    assert record.historical_high_market_cap_usd == D("150000")
    assert record.publications == 3
    assert record.qualification_count == 1
    assert record.max_return_from_surface_percent is not None


async def test_repeated_backfill_is_harmless(database) -> None:
    await _seed_legacy(database)
    store = LabStore(database)
    first = await store.load_lifecycle("LEGACY", now=99_999)
    for moment in (100_000, 200_000, 300_000):
        again = await store.load_lifecycle("LEGACY", now=moment)
        assert again.first_discovered_at == first.first_discovered_at
        assert again.state == first.state


async def test_backfill_survives_a_restart(tmp_path) -> None:
    path = str(tmp_path / "legacy.db")
    database = Database(path, D("100"))
    await database.connect()
    await _seed_legacy(database)
    original = await LabStore(database).load_lifecycle("LEGACY", now=99_999)
    await database.close()

    reopened = Database(path, D("100"))
    await reopened.connect()
    try:
        record = await LabStore(reopened).load_lifecycle("LEGACY", now=500_000)
        assert record.first_discovered_at == original.first_discovered_at
        assert record.state == original.state
        assert not record.is_fresh_setup
    finally:
        await reopened.close()


async def test_a_truly_unseen_mint_is_still_first_discovery(database) -> None:
    record = await LabStore(database).load_lifecycle("NEVER_SEEN_MINT", now=500)
    assert record.state == FIRST_DISCOVERY
    assert record.is_fresh_setup
    assert record.first_discovered_at == 500


def test_missing_legacy_data_stays_unknown_never_fabricated() -> None:
    sparse = LegacyEvidence(mint="M", first_seen_at=1_000, last_seen_at=2_000)
    result = reconstruct_lifecycle(sparse, now=9_999)
    assert result.reconstructed
    assert result.completeness != COMPLETE
    assert result.lifecycle.historical_high_market_cap_usd is None
    assert result.lifecycle.first_surfaced_at is None
    assert "first_surfaced_at" in result.missing


def test_no_history_at_all_reconstructs_nothing() -> None:
    assert not reconstruct_lifecycle(LegacyEvidence(mint="M"), now=1).reconstructed


def test_merging_backfill_only_moves_timestamps_earlier() -> None:
    evidence = LegacyEvidence(
        mint="M",
        first_seen_at=1_000,
        radar_first_seen_at=950,
        last_seen_at=9_000,
        observations=(LegacyObservation(observed_at=1_000, price_usd=D("1")),),
    )
    recovered = reconstruct_lifecycle(evidence, now=9_999).lifecycle
    live = new_lifecycle("M", now=50_000)
    merged = merge_backfill(live, recovered)
    assert merged.first_discovered_at == 950
    assert merge_backfill(merged, recovered).first_discovered_at == 950


# ---------------------------------------------------------------------------
# shared Discord renderer (sections 24-29, 58)
# ---------------------------------------------------------------------------


def _card(size: int = 900, *, title: str = "Card") -> CardSpec:
    return CardSpec(
        title=title,
        description="D" * 400,
        compact_description="Token $TKN\n`MINT` • REJECT • safety UNKNOWN",
        fields=(
            CardField("IDENTITY", "i" * 120, P_IDENTITY),
            CardField("DECISION", "d" * 120, P_DECISION),
            CardField("SAFETY", "s" * 120, P_SAFETY),
            CardField("SMART", "m" * size, P_SMART_MONEY),
            CardField("ABOUT", "a" * size, P_ABOUT),
            CardField("DIAG", "x" * size, P_DIAGNOSTICS),
        ),
    )


def test_a_normal_card_set_renders_untrimmed() -> None:
    report = describe_render([_card(60)] * 2)
    assert report.embeds == 2
    assert report.notes == ()
    assert report.within_hard_limit


def test_five_rich_cards_no_longer_exceed_the_message_budget() -> None:
    """The exact 50035 cause: per-embed clamping under a per-message limit."""

    embeds, notes = render_message([_card()] * 5)
    total = sum(len(item) for item in embeds)
    assert total <= SAFE_MESSAGE_BUDGET
    assert total <= MESSAGE_EMBED_LIMIT
    assert notes


def test_optional_content_is_trimmed_before_critical_content() -> None:
    embeds, _ = render_message([_card()] * 4)
    rendered = "\n".join(
        f"{field.name}{field.value}" for embed in embeds for field in embed.fields
    )
    assert "DIAG" not in rendered
    for embed in embeds:
        names = {field.name for field in embed.fields}
        assert "IDENTITY" in names or not embed.fields


def test_a_single_enormous_card_degrades_to_compact_then_minimal() -> None:
    monster = CardSpec(
        title="T" * 300,
        description="D" * 6_000,
        compact_description="Token `MINT` • REJECT",
        fields=tuple(CardField(f"F{i}", "v" * 1_000, P_SAFETY) for i in range(20)),
    )
    embeds, notes = render_message([monster])
    assert len(embeds) == 1
    assert len(embeds[0]) <= MESSAGE_EMBED_LIMIT
    assert notes


def test_long_names_and_about_cannot_break_the_render() -> None:
    hostile = CardSpec(
        title="Z" * 5_000,
        description="Q" * 9_000,
        compact_description="Z `MINT`",
        fields=(CardField("ABOUT", "A" * 9_000, P_ABOUT),),
    )
    embed = build_embed(hostile)
    assert len(embed.title) <= 256
    assert len(embed.description) <= 4_096
    assert len(embed.fields[0].value) <= 1_024


def test_discord_oversized_error_is_recognised() -> None:
    class Coded(Exception):
        code = 50035

    assert is_embed_too_large(Coded())
    assert is_embed_too_large(Exception("Embed size exceeds maximum size of 6000"))
    assert not is_embed_too_large(Exception("some other failure"))


class _Interaction:
    """Records every edit so a test can assert the interaction resolved."""

    def __init__(self, *, failures: int = 0, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._failures = failures
        self._error = error or Exception("Embed size exceeds maximum size of 6000")

    async def edit_original_response(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self._failures:
            raise self._error
        return None


async def test_cards_resolve_on_the_first_attempt_when_they_fit() -> None:
    interaction = _Interaction()
    assert await resolve_with_cards(interaction, [_card(60)], fallback_text="fallback")
    assert len(interaction.calls) == 1
    assert interaction.calls[0]["embeds"]


async def test_exactly_one_emergency_retry_after_a_50035() -> None:
    interaction = _Interaction(failures=1)
    assert await resolve_with_cards(interaction, [_card()], fallback_text="fallback")
    assert len(interaction.calls) == 2
    assert "minimum" in interaction.calls[1]["content"]


async def test_a_persistently_rejected_card_still_resolves_as_text() -> None:
    interaction = _Interaction(failures=2)
    assert await resolve_with_cards(interaction, [_card()], fallback_text="visible fallback")
    assert len(interaction.calls) == 3
    assert interaction.calls[2]["content"] == "visible fallback"
    assert interaction.calls[2]["embeds"] == []


async def test_the_interaction_never_spins_forever() -> None:
    """v2.35.1 contract: every path ends in a visible response, and stops."""

    interaction = _Interaction(failures=99)
    assert await resolve_with_cards(interaction, [_card()], fallback_text="x") is False
    # Three attempts, then it stops.  No infinite retry loop.
    assert len(interaction.calls) == 3


async def test_render_failure_never_fabricates_or_trades() -> None:
    interaction = _Interaction(failures=1)
    await resolve_with_cards(interaction, [_card()], fallback_text="fallback")
    body = json.dumps(interaction.calls[-1], default=str)
    assert "ENTRY" not in body
    assert "fabricat" not in body.casefold() or "Nothing was fabricated" in body


def test_priority_floor_separates_essential_from_optional() -> None:
    card = _card(60)
    essential = {field.name for field in card.essential_fields}
    assert essential == {"IDENTITY", "DECISION", "SAFETY"}
    assert all(field.priority < OPTIONAL_PRIORITY_FLOOR for field in card.essential_fields)


# ---------------------------------------------------------------------------
# the boundary that must not move (sections 7, 43)
# ---------------------------------------------------------------------------


def _entry_context(**overrides):
    from smart_money_bot.lab.authenticity import (
        BAND_AUTHENTIC,
        AuthenticityAssessment,
        SolActivityProfile,
    )
    from smart_money_bot.lab.decision import EvidenceQuality
    from smart_money_bot.lab.entry import EntryContext
    from smart_money_bot.lab.regime import NORMAL, MarketRegime

    base = {
        "mint": MINT,
        "now": 1_000,
        "price_usd": D("0.001"),
        "market_cap_usd": D("40000"),
        "liquidity_usd": D("60000"),
        "qualified": True,
        "stage": "ENTRY_CANDIDATE",
        "opportunity_score": D("70"),
        "momentum_score": D("70"),
        "organic_score": D("70"),
        "independent_buyers": 30,
        "independence_ratio": D("0.8"),
        "cluster_supply_percent": D("10"),
        "buys": 100,
        "sells": 40,
        "safety_status": "PASS",
        "safety_entry_eligible": True,
        "route_available": True,
        "sell_route_available": True,
        "buy_price_impact_percent": D("0.5"),
        "sell_price_impact_percent": D("0.6"),
        "slippage_percent": D("0.8"),
        "authenticity": AuthenticityAssessment(
            score=D("75"),
            band=BAND_AUTHENTIC,
            quality=EvidenceQuality.COMPLETE,
            activity=SolActivityProfile(
                quality=EvidenceQuality.COMPLETE, sampled_wallets=20
            ),
        ),
        "regime": MarketRegime(state=NORMAL, samples=20),
        "expected_upside_percent": D("60"),
        "expected_downside_percent": D("30"),
        "edge_confidence": D("70"),
        "move_since_first_surface_percent": D("20"),
        "signal_age_seconds": 60,
    }
    base.update(overrides)
    return EntryContext(**base)


def _decide(context):
    from smart_money_bot.lab.bankroll import BankrollState
    from smart_money_bot.lab.entry import evaluate_entry

    return evaluate_entry(
        context, lifecycle=new_lifecycle(MINT, now=0), bankroll=BankrollState()
    )


def test_a_hot_fast_watch_does_not_make_an_unsafe_candidate_enterable() -> None:
    """FAST WATCH buys speed of information, never speed of commitment."""

    watch = evaluate_fast_watch(_hot_signals())
    assert watch.watch and watch.entry_eligible is False

    for status in ("UNKNOWN", "FAIL"):
        result = _decide(_entry_context(safety_status=status, safety_entry_eligible=False))
        assert not result.entry_eligible
        assert result.decision.size_usd == D("0")


def test_missing_route_or_liquidity_still_blocks_paper_entry() -> None:
    assert not _decide(_entry_context(route_available=False)).entry_eligible
    assert not _decide(_entry_context(sell_route_available=False)).entry_eligible
    assert not _decide(_entry_context(liquidity_usd=D("100"))).entry_eligible


def test_suppression_does_not_touch_the_entry_gate() -> None:
    """Current-radar suppression is a display concern, not a trading one."""

    clean = _decide(_entry_context())
    assert clean.entry_eligible
    assert clean.decision.size_usd > D("0")
