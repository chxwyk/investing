"""Regression suite for the v2.41 ultra-early alpha engine.

The product failure this release exists to fix is one sentence long: the bot
recorded Grok Pocket at ~$31K and the operator did not get useful visibility
until ~$61K.  So the tests that matter most here are about *timing* and
*honesty* — did the human get a chance while the edge existed, and when they
did not, does the card say so instead of printing "first seen $31.2K" beside a
doubled price.

The second half is restraint.  Being early is worthless if the channel fills
with noise, so a creator self-buy must not read as demand, a high legacy score
must not earn a ping on its own, and a token that merely copied a campaign URL
must never inherit the real story's credibility.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib
import re
import textwrap
import time
from dataclasses import replace as dataclass_replace
from decimal import Decimal

import pytest

from smart_money_bot.lab.early import (
    BUY_CONFIRMED,
    BUY_INSIDER,
    EDGE_AVAILABLE,
    EDGE_CONSUMED,
    OPERATOR_VISIBLE_STAGES,
    STAGE_BOT_FIRST_SEEN,
    STAGE_CHEAP_SIGNAL,
    STAGE_EARLY_RUNNER,
    STAGE_URGENT_PING,
    TIER_EARLY_HEADS_UP,
    TIER_EARLY_RUNNER,
    TIER_NONE,
    TIER_ORGANIC_RUNNER,
    WHY_INSIDER_ONLY,
    WHY_MOVE_CONSUMED,
    WHY_NOT_SERIOUS,
    AlertTiming,
    EarlyConfig,
    EarlySignals,
    MissedRunner,
    audit_missed_runners,
    classify_edge_state,
    detect_large_buy,
    evaluate_early_signal,
    summarize_alert_performance,
)
from smart_money_bot.lab.narrative import (
    DIR_STORY_TO_TOKEN,
    DIR_TOKEN_TO_STORY,
    REL_DIRECTLY_LINKED,
    REL_NAME_ONLY,
    REL_PLAUSIBLE,
    REL_UNRELATED,
    VIRALITY_STRONG,
    W_TOKEN_PREDATES_STORY,
    NarrativeEntity,
    StorySource,
    TokenIdentityClaim,
    assess_narrative_link,
    assess_virality,
    build_collision_group,
    extract_mints,
    mark_official,
    narrative_id_for,
    open_launch_watch,
    story_lead_seconds,
)
from smart_money_bot.models import DexSnapshot

D = Decimal
NOW = 1_800_000_000
MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
OTHER_MINT = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def signals(**overrides) -> EarlySignals:
    """The Grok Pocket first-seen state: fresh, thin, and accelerating."""

    payload = {
        "mint": MINT,
        "now": NOW,
        "first_seen_at": NOW - 8,
        "pair_age_seconds": 82,
        "market_cap_usd": D("33100"),
        "first_seen_market_cap_usd": D("31180"),
        "liquidity_usd": D("6900"),
        "volume_5m_usd": D("5200"),
        "price_change_5m_percent": D("14"),
        "buys_5m": 26,
        "sells_5m": 6,
        "route_available": True,
    }
    payload.update(overrides)
    return EarlySignals(**payload)


def snapshot(**overrides) -> DexSnapshot:
    payload = {
        "available": True,
        "market_cap_usd": D("31180"),
        "liquidity_usd": D("6900"),
        "pair_age_minutes": 1,
        "buys_5m": 26,
        "sells_5m": 6,
        "volume_5m_usd": D("5200"),
        "price_change_5m_percent": D("14"),
    }
    payload.update(overrides)
    return DexSnapshot(**payload)


class _Notifier:
    """Collects published cards; every other hook is a no-op."""

    def __init__(self) -> None:
        self.cards: list = []

    async def on_fast_alert(self, alert) -> bool:
        self.cards.append(alert)
        return True

    async def on_error(self, *args, **kwargs) -> None:
        return None

    def __getattr__(self, name):
        async def _noop(*args, **kwargs):
            return True

        return _noop


async def engine_for(settings, tmp_path, name: str = "early.db"):
    from smart_money_bot.engine import SmartMoneyEngine

    engine = SmartMoneyEngine(
        dataclass_replace(settings, database_path=str(tmp_path / name))
    )
    engine.notifier = _Notifier()
    await engine.database.connect()
    return engine


# ===========================================================================
# 1, 4, 80. FIRST VISIBILITY MUST NOT WAIT ON DEEP ENRICHMENT
# ===========================================================================


async def test_the_operator_is_alerted_before_deep_enrichment_finishes(
    settings, tmp_path
) -> None:
    """Section 80: the whole point of the release.

    Deep enrichment is deliberately slow here.  If the alert waits for it, the
    operator learns about a $31K token at $61K, which is the exact production
    failure being fixed.
    """

    engine = await engine_for(settings, tmp_path)
    try:

        async def _snapshot(mint, refresh=False):
            return snapshot()

        async def _slow_analysis(*args, **kwargs):
            await asyncio.sleep(20)
            return None

        engine.dex_screener.snapshot = _snapshot
        engine.analyze_runner = _slow_analysis

        started = time.monotonic()
        published = await engine._run_early_lane(MINT, now=NOW)
        elapsed = time.monotonic() - started

        assert published is True
        assert elapsed < 2, "first visibility must not wait on a 20s enrichment pass"
        assert engine.notifier.cards, "the operator got no card at all"
    finally:
        await engine.database.close()


async def test_the_alert_fires_at_the_first_seen_market_cap(settings, tmp_path) -> None:
    engine = await engine_for(settings, tmp_path)
    try:

        async def _snapshot(mint, refresh=False):
            return snapshot()

        engine.dex_screener.snapshot = _snapshot
        await engine._run_early_lane(MINT, now=NOW)

        timeline = {
            str(row["stage"]): row for row in await engine.database.alert_timeline(MINT)
        }
        assert timeline[STAGE_BOT_FIRST_SEEN]["market_cap_usd"] == 31180.0
        assert timeline[STAGE_EARLY_RUNNER]["market_cap_usd"] == 31180.0
        assert STAGE_URGENT_PING in timeline
    finally:
        await engine.database.close()


async def test_the_early_lane_runs_before_the_deep_gather_in_the_radar_loop() -> None:
    """Structural: ordering is the fix, so ordering is asserted."""

    from smart_money_bot import engine as engine_module

    source = inspect.getsource(engine_module.SmartMoneyEngine._run_fomo_radar)
    early = source.index("_run_early_lane")
    deep = source.index("evaluate(mint) for mint in selected")

    assert early < deep, "the cheap operator lane must run before deep enrichment"


async def test_a_missing_snapshot_records_a_reason_rather_than_failing_silently(
    settings, tmp_path
) -> None:
    engine = await engine_for(settings, tmp_path)
    try:

        async def _snapshot(mint, refresh=False):
            return DexSnapshot(available=False)

        engine.dex_screener.snapshot = _snapshot
        assert await engine._run_early_lane(MINT, now=NOW) is False

        rows = await engine.database.alert_suppression_rows(mint=MINT)
        assert [row["reason_code"] for row in rows] == ["NO_CHEAP_MARKET_DATA"]
    finally:
        await engine.database.close()


async def test_an_early_lane_failure_never_breaks_the_radar(settings, tmp_path) -> None:
    engine = await engine_for(settings, tmp_path)
    try:

        async def _explode(mint, refresh=False):
            raise RuntimeError("provider melted")

        engine.dex_screener.snapshot = _explode

        assert await engine._run_early_lane(MINT, now=NOW) is False
    finally:
        await engine.database.close()


# ===========================================================================
# 3, 10, 11, 47, 52, 81. LATE IS LABELLED LATE, AND CAPS ARE IMMUTABLE
# ===========================================================================


def test_the_grok_pocket_shape_is_classified_late_not_early() -> None:
    """Section 47: $31K seen, $61K alerted is not an early alert."""

    timing = AlertTiming(
        mint=MINT,
        first_seen_at=NOW,
        alert_at=NOW + 300,
        first_seen_market_cap_usd=D("31180"),
        alert_market_cap_usd=D("61490"),
        current_market_cap_usd=D("61490"),
    )

    assert timing.move_before_alert_percent == D("97.21")
    assert timing.was_early is False
    assert timing.edge_state() == EDGE_CONSUMED


def test_a_genuinely_early_alert_is_classified_early() -> None:
    timing = AlertTiming(
        mint=MINT,
        first_seen_at=NOW,
        alert_at=NOW + 8,
        first_seen_market_cap_usd=D("31180"),
        alert_market_cap_usd=D("33100"),
        current_market_cap_usd=D("61490"),
    )

    assert timing.was_early is True
    assert timing.edge_state() == EDGE_AVAILABLE
    assert timing.move_before_alert_percent < D("10")
    assert timing.move_after_alert_percent > D("80")
    assert timing.first_seen_to_alert_seconds == 8


async def test_a_late_card_says_it_is_late_and_never_pings(settings, tmp_path) -> None:
    """Section 81: it must not be labelled EARLY_RUNNER."""

    engine = await engine_for(settings, tmp_path)
    try:
        quiet = snapshot(
            buys_5m=2, sells_5m=2, volume_5m_usd=D("120"), price_change_5m_percent=D("1")
        )
        hot = snapshot(
            market_cap_usd=D("61490"),
            liquidity_usd=D("14000"),
            pair_age_minutes=6,
            buys_5m=40,
            sells_5m=8,
            volume_5m_usd=D("22000"),
            price_change_5m_percent=D("30"),
        )
        current = {"snap": quiet}

        async def _snapshot(mint, refresh=False):
            return current["snap"]

        engine.dex_screener.snapshot = _snapshot

        assert await engine._run_early_lane(MINT, now=NOW) is False
        current["snap"] = hot
        assert await engine._run_early_lane(MINT, now=NOW + 300) is True

        card = engine.notifier.cards[-1]
        body = "\n".join(item.value for item in card.spec.fields)

        assert "EDGE CONSUMED" in card.spec.title
        assert card.may_ping is False
        assert card.lane == "RADAR"
        assert "+97.21%" in body
        assert "WHY THIS IS NOT AN EARLY ALERT" in {
            item.name.replace("⚠ ", "") for item in card.spec.fields
        } | {item.name for item in card.spec.fields}
    finally:
        await engine.database.close()


async def test_the_first_seen_market_cap_can_never_be_rewritten(
    settings, tmp_path
) -> None:
    """Section 52: enrichment must not be able to make a late alert look early."""

    engine = await engine_for(settings, tmp_path)
    try:
        await engine.database.record_alert_stage(
            mint=MINT,
            stage=STAGE_BOT_FIRST_SEEN,
            occurred_at=NOW,
            market_cap_usd=D("31180"),
        )
        rewritten = await engine.database.record_alert_stage(
            mint=MINT,
            stage=STAGE_BOT_FIRST_SEEN,
            occurred_at=NOW + 600,
            market_cap_usd=D("61490"),
        )

        assert rewritten is False, "a stage must be write-once"
        row = (await engine.database.alert_timeline(MINT))[0]
        assert row["market_cap_usd"] == 31180.0
        assert row["occurred_at"] == NOW
    finally:
        await engine.database.close()


def test_edge_state_escalates_with_the_move_already_spent() -> None:
    assert classify_edge_state(None) == EDGE_AVAILABLE
    assert classify_edge_state(D("5")) == EDGE_AVAILABLE
    assert classify_edge_state(D("40")) == "EDGE_NARROWING"
    assert classify_edge_state(D("90")) == EDGE_CONSUMED
    assert classify_edge_state(D("400")) == "MOVE_ALREADY_EXTENDED"


def test_a_late_runner_records_that_the_move_was_already_gone() -> None:
    """Sections 10, 12: the Grok Pocket case, told honestly on the card."""

    verdict = evaluate_early_signal(
        signals(first_seen_market_cap_usd=D("31180"), market_cap_usd=D("61490"))
    )

    assert verdict.edge_state == EDGE_CONSUMED
    assert WHY_MOVE_CONSUMED in verdict.why_not_pinged
    assert verdict.late is True
    assert "EDGE CONSUMED" in verdict.label


# ===========================================================================
# 7, 8, 82, 83. LARGE BUYS, AND WHOSE MONEY THEY ARE
# ===========================================================================


def test_a_large_buy_is_measured_against_the_market_not_a_dollar_threshold() -> None:
    """Section 7: $900 is trivial against deep liquidity and huge against $6.9K."""

    thin = detect_large_buy(signals(largest_buy_usd=D("900"), liquidity_usd=D("6900")))
    deep = detect_large_buy(
        signals(largest_buy_usd=D("900"), liquidity_usd=D("900000"), volume_5m_usd=D("400000"))
    )

    assert thin.detected is True
    assert thin.liquidity_share_percent == D("13.04")
    assert deep.detected is False


def test_a_large_buy_with_independent_follow_on_demand_is_real_demand() -> None:
    impulse = detect_large_buy(
        signals(largest_buy_usd=D("900"), independent_buyers_after_largest_buy=15)
    )

    assert impulse.quality == BUY_CONFIRMED
    assert impulse.is_demand is True


def test_a_creator_self_buy_with_nobody_following_is_not_demand(settings) -> None:
    """Section 83: the same chart shape, none of the meaning."""

    verdict = evaluate_early_signal(
        signals(
            buys_5m=2,
            sells_5m=0,
            volume_5m_usd=D("950"),
            price_change_5m_percent=D("30"),
            largest_buy_usd=D("900"),
            largest_buy_is_creator_linked=True,
            independent_buyers_after_largest_buy=0,
        )
    )

    assert verdict.impulse.quality == BUY_INSIDER
    assert verdict.impulse.is_demand is False
    assert verdict.tier == TIER_NONE
    assert WHY_INSIDER_ONLY in verdict.why_not_pinged


def test_an_impulse_can_be_inferred_from_the_move_when_no_trade_feed_exists() -> None:
    impulse = detect_large_buy(
        signals(largest_buy_usd=None, price_change_5m_percent=D("14"))
    )

    assert impulse.detected is True
    assert any("inferred" in reason for reason in impulse.reasons)


# ===========================================================================
# 5, 15, 16, 17, 43, 88, 89. TIERS AND SERIOUS EVIDENCE
# ===========================================================================


def test_an_organic_runner_needs_no_story_at_all() -> None:
    """Section 88: pure market alpha is a legitimate reason to look."""

    verdict = evaluate_early_signal(signals())

    assert verdict.tier == TIER_ORGANIC_RUNNER
    assert verdict.may_ping is True
    assert "ORGANIC_MARKET_EVIDENCE" in verdict.evidence_categories
    assert "ORGANIC RUNNER" in verdict.label


def test_a_story_or_wallet_alongside_market_evidence_makes_it_an_early_runner() -> None:
    story = evaluate_early_signal(
        signals(story_state="VIRAL", story_relationship="STRONG")
    )
    wallet = evaluate_early_signal(signals(proven_early_wallet_count=2))

    for verdict in (story, wallet):
        assert verdict.tier == TIER_EARLY_RUNNER
        assert "MULTI_SOURCE_CONFLUENCE" in verdict.evidence_categories


def test_organic_flow_and_a_market_impulse_are_one_evidence_family() -> None:
    """Section 41: confluence means *independent* families, not one seen twice."""

    verdict = evaluate_early_signal(signals())

    assert "MULTI_SOURCE_CONFLUENCE" not in verdict.evidence_categories
    assert verdict.tier == TIER_ORGANIC_RUNNER


def test_a_high_score_with_no_serious_evidence_cannot_ping(settings) -> None:
    """Section 89: a legacy 87/100 is not a reason to interrupt anyone."""

    # Enough activity to score, not enough to name a serious category.
    verdict = evaluate_early_signal(
        signals(buys_5m=14, sells_5m=8, volume_5m_usd=D("1500"), largest_buy_usd=None)
    )

    assert verdict.tier == TIER_EARLY_HEADS_UP
    assert verdict.may_ping is False
    assert verdict.evidence_categories == ()


def test_a_pingable_tier_without_a_category_is_demoted_to_a_heads_up() -> None:
    config = EarlyConfig(runner_min_score=Decimal("1"))
    verdict = evaluate_early_signal(
        signals(buys_5m=13, sells_5m=9, volume_5m_usd=D("1400"), largest_buy_usd=None),
        config=config,
    )

    assert verdict.tier == TIER_EARLY_HEADS_UP
    assert WHY_NOT_SERIOUS in verdict.why_not_pinged


def test_a_quiet_token_is_not_surfaced_at_all() -> None:
    verdict = evaluate_early_signal(
        signals(
            buys_5m=3,
            sells_5m=4,
            volume_5m_usd=D("120"),
            price_change_5m_percent=D("1"),
            pair_age_seconds=2_000,
        )
    )

    assert verdict.tier == TIER_NONE
    assert verdict.visible is False


def test_blockers_stop_a_surface_however_good_the_flow_looks() -> None:
    for override in (
        {"rugged": True},
        {"route_available": False},
        {"liquidity_usd": D("100")},
        {"pair_age_seconds": 99_999},
        {"market_cap_usd": D("500")},
    ):
        verdict = evaluate_early_signal(signals(**override))
        assert verdict.tier == TIER_NONE, override
        assert verdict.blockers, override


def test_the_early_lane_can_never_authorise_an_entry() -> None:
    assert evaluate_early_signal(signals()).entry_eligible is False


# ===========================================================================
# 12, 13, 14. WHY WASN'T I PINGED, AND HOW OFTEN WAS I EARLY
# ===========================================================================


async def test_every_suppression_reason_is_persisted(settings, tmp_path) -> None:
    engine = await engine_for(settings, tmp_path)
    try:

        async def _snapshot(mint, refresh=False):
            return snapshot(
                buys_5m=2, sells_5m=2, volume_5m_usd=D("120"), price_change_5m_percent=D("1")
            )

        engine.dex_screener.snapshot = _snapshot
        await engine._run_early_lane(MINT, now=NOW)

        counts = await engine.database.suppression_counts()
        assert counts.get("NO_EARLY_SIGNAL") == 1
        rows = await engine.database.alert_suppression_rows(mint=MINT)
        assert rows[0]["detail"], "a reason code must carry a human explanation"
    finally:
        await engine.database.close()


def test_alert_performance_answers_how_often_we_beat_the_move() -> None:
    early = AlertTiming(
        mint=MINT,
        first_seen_at=NOW,
        alert_at=NOW + 5,
        first_seen_market_cap_usd=D("31180"),
        alert_market_cap_usd=D("32000"),
    )
    late = AlertTiming(
        mint=OTHER_MINT,
        first_seen_at=NOW,
        alert_at=NOW + 300,
        first_seen_market_cap_usd=D("31180"),
        alert_market_cap_usd=D("61490"),
    )

    report = summarize_alert_performance([early, late])

    assert report.alerts == 2
    assert report.early_alerts == 1
    assert report.late_alerts == 1
    assert report.early_rate_percent == D("50.00")
    assert report.alerted_before_10_percent == D("50.00")
    assert report.alerted_before_100_percent == D("100.00")


def test_a_runner_the_operator_never_saw_in_time_counts_as_missed() -> None:
    """Section 13: evaluation only, and only when the bot really was early."""

    missed = MissedRunner(
        mint=MINT,
        first_seen_at=NOW,
        first_seen_market_cap_usd=D("31180"),
        peak_market_cap_usd=D("120000"),
        alert_at=NOW + 300,
        alert_market_cap_usd=D("61490"),
    )
    caught = MissedRunner(
        mint=OTHER_MINT,
        first_seen_at=NOW,
        first_seen_market_cap_usd=D("31180"),
        peak_market_cap_usd=D("120000"),
        alert_at=NOW + 5,
        alert_market_cap_usd=D("32000"),
    )
    quiet = MissedRunner(
        mint=MINT,
        first_seen_at=NOW,
        first_seen_market_cap_usd=D("31180"),
        peak_market_cap_usd=D("32000"),
    )

    assert missed.missed is True
    assert caught.missed is False
    assert quiet.missed is False, "a token that never ran is not a missed runner"
    assert audit_missed_runners([caught, missed, quiet]) == (missed,)


# ===========================================================================
# 20-27, 84-87. MINT IS IDENTITY
# ===========================================================================


def story(*, links_mint: str = "") -> NarrativeEntity:
    return NarrativeEntity(
        narrative_id=narrative_id_for("Justice for HeeHaw"),
        title="Justice for HeeHaw",
        aliases=("heehaw",),
        keywords=("justice for heehaw",),
        first_seen_at=NOW,
        last_seen_at=NOW + 3_600,
        sources=(
            StorySource(
                name="campaign",
                url="https://justiceforheehaw.org",
                observed_at=NOW,
                is_primary=True,
                links_exact_mint=links_mint,
            ),
            StorySource(name="outlet-a", url="https://a.example/heehaw", observed_at=NOW + 600),
            StorySource(name="outlet-b", url="https://b.example/heehaw", observed_at=NOW + 900),
        ),
    )


def test_two_tokens_with_the_same_name_stay_completely_separate() -> None:
    """Section 84: same name is not the same token."""

    narrative = story(links_mint=MINT)
    real = TokenIdentityClaim(
        mint=MINT,
        name="Justice for HeeHaw",
        symbol="HEEHAW",
        website_url="https://justiceforheehaw.org",
        created_at=NOW + 2_820,
    )
    impostor = TokenIdentityClaim(
        mint=OTHER_MINT,
        name="Justice for HeeHaw",
        symbol="HEEHAW",
        created_at=NOW + 3_600,
    )

    real_link = assess_narrative_link(narrative, real, now=NOW + 4_000)
    fake_link = assess_narrative_link(narrative, impostor, now=NOW + 4_000)

    assert real_link.mint != fake_link.mint
    assert real_link.relationship == REL_DIRECTLY_LINKED
    assert fake_link.relationship == REL_NAME_ONLY
    assert real_link.inherits_story is True
    assert fake_link.inherits_story is False


def test_a_story_source_naming_an_exact_mint_is_the_strong_direction() -> None:
    """Section 85."""

    narrative = story(links_mint=MINT)
    token = TokenIdentityClaim(mint=MINT, name="Justice for HeeHaw", created_at=NOW + 60)

    link = assess_narrative_link(narrative, token, now=NOW + 100)

    assert link.relationship == REL_DIRECTLY_LINKED
    assert link.direction == DIR_STORY_TO_TOKEN
    assert link.confidence >= D("85")


def test_a_token_that_merely_copies_a_story_url_never_inherits_the_story() -> None:
    """Sections 23, 26, 86: metadata is copyable, so it is capped."""

    narrative = story(links_mint=MINT)
    copycat = TokenIdentityClaim(
        mint=OTHER_MINT,
        name="Justice for HeeHaw",
        website_url="https://justiceforheehaw.org",
        created_at=NOW + 3_000,
    )

    link = assess_narrative_link(narrative, copycat, now=NOW + 4_000)

    assert link.direction == DIR_TOKEN_TO_STORY
    assert link.relationship == REL_PLAUSIBLE
    assert link.inherits_story is False
    with pytest.raises(ValueError, match="never establish OFFICIAL"):
        mark_official(link, authority="operator")


def test_official_requires_a_named_authority_and_the_story_side() -> None:
    narrative = story(links_mint=MINT)
    token = TokenIdentityClaim(mint=MINT, name="Justice for HeeHaw", created_at=NOW + 60)
    link = assess_narrative_link(narrative, token, now=NOW + 100)

    with pytest.raises(ValueError, match="named authority"):
        mark_official(link, authority="   ")

    official = mark_official(link, authority="verified campaign page")
    assert official.relationship == "OFFICIAL"
    assert any("verified campaign page" in item for item in official.reasons)


def test_a_token_that_predates_the_story_is_flagged_not_condemned() -> None:
    """Section 27."""

    narrative = story()
    old = TokenIdentityClaim(
        mint=OTHER_MINT, name="HeeHaw", created_at=NOW - 900_000
    )

    link = assess_narrative_link(narrative, old, now=NOW + 100)

    assert W_TOKEN_PREDATES_STORY in link.warnings
    assert link.relationship == REL_NAME_ONLY
    assert link.relationship != REL_UNRELATED, "flagged, not dismissed"


def test_an_unrelated_token_links_to_nothing() -> None:
    link = assess_narrative_link(
        story(), TokenIdentityClaim(mint=OTHER_MINT, name="Dog Coin"), now=NOW
    )

    assert link.relationship == REL_UNRELATED
    assert link.inherits_story is False


def test_the_collision_group_ranks_by_what_each_mint_proved() -> None:
    """Sections 25, 77."""

    narrative = story(links_mint=MINT)
    tokens = (
        TokenIdentityClaim(
            mint=MINT,
            name="Justice for HeeHaw",
            website_url="https://justiceforheehaw.org",
            created_at=NOW + 2_820,
        ),
        TokenIdentityClaim(
            mint=OTHER_MINT,
            name="Justice for HeeHaw",
            website_url="https://justiceforheehaw.org",
            created_at=NOW + 3_000,
        ),
        TokenIdentityClaim(mint="9" * 43, name="HeeHaw", created_at=NOW + 4_000),
    )
    links = [assess_narrative_link(narrative, item, now=NOW + 5_000) for item in tokens]

    group = build_collision_group(narrative, links)

    assert group.has_collision is True
    assert group.candidates == 3
    assert group.strongest.mint == MINT
    assert group.contested is False


def test_a_story_first_launch_watch_matches_the_token_when_it_appears() -> None:
    """Section 87: the terms are loaded before the token exists."""

    narrative = story()
    virality = assess_virality(narrative, now=NOW + 1_200)
    watch = open_launch_watch(narrative, now=NOW + 1_200, virality=virality)

    assert virality == VIRALITY_STRONG
    assert watch is not None
    assert watch.active(now=NOW + 2_000) is True

    later = TokenIdentityClaim(mint=MINT, name="Justice for HeeHaw", created_at=NOW + 2_820)
    assert watch.matches(later) is True
    assert story_lead_seconds(narrative, later) == 2_820


def test_exact_mints_are_resolved_from_urls_before_free_text() -> None:
    """Section 24: never navigate by name when an exact mint exists."""

    found = extract_mints(f"chat {OTHER_MINT} but real is https://pump.fun/coin/{MINT}")

    assert found[0] == MINT
    assert extract_mints("no mint here") == ()


# ===========================================================================
# 90-96. STREAM, SLOW PROVIDERS, EXITS, SAFETY, NO LOOKAHEAD
# ===========================================================================


def test_the_wallet_stream_reconnects_with_bounded_backoff() -> None:
    """Section 90: the reconnect loop and its counters must exist."""

    from smart_money_bot import stream as stream_module

    source = inspect.getsource(stream_module)
    tree = ast.parse(source)
    names = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    assert "reconnects" in names
    assert "while True" in source
    assert "sleep" in source, "a reconnect loop must back off rather than spin"


async def test_a_slow_provider_cannot_delay_first_visibility(settings, tmp_path) -> None:
    """Section 91."""

    engine = await engine_for(settings, tmp_path)
    try:

        async def _snapshot(mint, refresh=False):
            return snapshot()

        async def _slow(*args, **kwargs):
            await asyncio.sleep(30)
            raise AssertionError("the early lane must not await this")

        engine.dex_screener.snapshot = _snapshot
        engine.tracker_token_risk.snapshot = _slow
        engine.analyze_runner = _slow

        started = time.monotonic()
        assert await engine._run_early_lane(MINT, now=NOW) is True
        assert time.monotonic() - started < 2
    finally:
        await engine.database.close()


def test_a_momentum_pause_no_longer_dumps_a_healthy_runner() -> None:
    """Section 92."""

    from smart_money_bot.lab.exits import ExitContext, open_position
    from smart_money_bot.lab.shadow import DEFAULT_SHADOW_CONFIG
    from smart_money_bot.lab.shadow_exits import (
        SHADOW_SOFT_PAUSE_HOLD,
        RunnerEvidence,
        plan_shadow_exit,
    )

    position = open_position(
        position_id="p",
        mint=MINT,
        now=NOW,
        decision_price_usd=D("0.001"),
        size_usd=D("10"),
        config=DEFAULT_SHADOW_CONFIG.exit_config(),
    )
    context = ExitContext(
        now=NOW + 600,
        price_usd=D("0.00105"),
        liquidity_usd=D("42000"),
        entry_liquidity_usd=D("40000"),
        momentum_score=D("10"),
        organic_score=D("65"),
        buys=140,
        sells=40,
        safety_status="PASS",
        route_available=True,
    )

    assessment = plan_shadow_exit(position, context, RunnerEvidence())

    assert assessment.plan.reason_code == SHADOW_SOFT_PAUSE_HOLD
    assert assessment.plan.acts is False


def test_no_lookahead_reaches_an_earlier_alert_decision() -> None:
    """Section 96: a later 20x cannot change what the alert said."""

    before = evaluate_early_signal(signals())
    after = evaluate_early_signal(signals())

    assert before == after

    tree = ast.parse(textwrap.dedent(inspect.getsource(evaluate_early_signal)))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for forbidden in ("peak", "peak_market_cap_usd", "later_market_cap_usd", "outcome"):
        assert forbidden not in names


def test_missed_runner_analysis_is_evaluation_only() -> None:
    """The audit reads the future by construction, so nothing live may use it."""

    from smart_money_bot.lab import early as early_module

    tree = ast.parse(inspect.getsource(early_module))
    live = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "evaluate_early_signal"
    )
    names = {node.id for node in ast.walk(live) if isinstance(node, ast.Name)}

    assert "MissedRunner" not in names
    assert "audit_missed_runners" not in names


# ===========================================================================
# 62, 79, 97, 98. HISTORY, COMMANDS, RESTART, REAL MONEY
# ===========================================================================


def test_the_shadow_experiment_terms_are_untouched() -> None:
    """Section 62: this release must not reset or rewrite forward history."""

    from smart_money_bot.lab.shadow import DEFAULT_SHADOW_CONFIG

    assert DEFAULT_SHADOW_CONFIG.bankroll_usd == D("100")
    assert DEFAULT_SHADOW_CONFIG.position_usd == D("10")
    assert DEFAULT_SHADOW_CONFIG.max_concurrent_positions == 5
    assert DEFAULT_SHADOW_CONFIG.max_total_exposure_usd == D("50")


def test_every_documented_command_and_view_actually_exists() -> None:
    """Section 79: the README must not document a command that is not registered."""

    from smart_money_bot import bot as bot_module

    source = inspect.getsource(bot_module)
    registered = set(re.findall(r'@app_commands\.command\(\s*name="([a-z-]+)"', source))
    readme = pathlib.Path("README.md").read_text(encoding="utf-8")

    documented = set(re.findall(r"`/fomo ([a-z-]+)", readme))
    missing = documented - registered
    assert not missing, f"README documents commands that do not exist: {sorted(missing)}"

    for name in ("runners", "runner", "collisions", "profit", "shadow", "realtime"):
        assert name in registered, f"/fomo {name} is not registered"

    # Every documented view of a parameterised command must be a real literal.
    for command, views in (
        ("profit", {"summary", "signals", "exits", "providers", "alerts"}),
        ("shadow", {"account", "trades", "results", "venues", "policies"}),
    ):
        block = source[source.index(f'name="{command}"') :][:4000]
        for view in views:
            assert f'"{view}"' in block, f"/fomo {command} view:{view} is not registered"


async def test_the_schema_is_additive_and_restart_safe(settings, tmp_path) -> None:
    """Section 97: a redeploy must not lose or duplicate anything."""

    engine = await engine_for(settings, tmp_path, name="restart.db")
    try:

        async def _snapshot(mint, refresh=False):
            return snapshot()

        engine.dex_screener.snapshot = _snapshot
        await engine._run_early_lane(MINT, now=NOW)
        await engine.database._init_schema()

        cursor = await engine.database.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND (name LIKE 'alert_%' OR name LIKE 'narrative%') ORDER BY name"
        )
        tables = [row["name"] for row in await cursor.fetchall()]
        assert tables == [
            "alert_suppression",
            "alert_timeline",
            "narrative_links",
            "narratives",
        ]

        # Replaying the same pass must not duplicate a stage or re-publish.
        before = len(await engine.database.alert_timeline(MINT))
        published_again = await engine._run_early_lane(MINT, now=NOW + 1)
        after = len(await engine.database.alert_timeline(MINT))

        assert published_again is False, "the cooldown must stop a duplicate card"
        assert before == after
    finally:
        await engine.database.close()


def test_the_early_lane_cannot_spend_real_money() -> None:
    """Section 98."""

    import importlib

    for module_name in ("smart_money_bot.lab.early", "smart_money_bot.lab.narrative"):
        tree = ast.parse(inspect.getsource(importlib.import_module(module_name)))
        names = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Name | ast.Attribute)
        }
        for forbidden in ("Keypair", "sign_message", "execute_order", "send_transaction"):
            assert forbidden not in names, f"{module_name} must not reference {forbidden}"

        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        assert not imports & {"aiohttp", "solders", "aiosqlite", "requests"}


def test_operator_visible_stages_are_exactly_the_ones_a_human_sees() -> None:
    assert set(OPERATOR_VISIBLE_STAGES) == {
        "OPERATOR_HEADS_UP_SENT",
        "EARLY_RUNNER_TRIGGER",
        "URGENT_PING_SENT",
    }
    assert STAGE_CHEAP_SIGNAL not in OPERATOR_VISIBLE_STAGES
    assert STAGE_BOT_FIRST_SEEN not in OPERATOR_VISIBLE_STAGES


def test_the_social_lane_reports_a_real_state_not_a_generic_healthy(
    settings, tmp_path
) -> None:
    """Section 31: zero activity is not HEALTHY."""

    from smart_money_bot.engine import SmartMoneyEngine

    engine = SmartMoneyEngine(
        dataclass_replace(settings, database_path=str(tmp_path / "social.db"))
    )
    status = engine.social_status()

    assert status["state"] in {
        "ACTIVE",
        "ACTIVE_NO_EVENTS",
        "DISABLED_BY_CONFIG",
        "NO_SOURCE_CONFIGURED",
        "AUTH_MISSING",
        "PROVIDER_DEGRADED",
        "RATE_LIMITED",
    }
    assert status["state"] != "HEALTHY"
