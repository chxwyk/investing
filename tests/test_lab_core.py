"""Deterministic regression suite for the v2.36 PAPER research laboratory.

Every case answers one product question: does the bot still refuse to buy this,
does it still remember that this token already pumped, and does the money maths
still count every real cost?  Nothing here touches a network provider.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from smart_money_bot.lab.authenticity import (
    BAND_AUTHENTIC,
    BAND_MANUFACTURED,
    BAND_UNKNOWN,
    MARK_CLUSTER_DOMINATES,
    MARK_FRESH_WALLET_SWARM,
    MARK_REPETITIVE_SIZING,
    MARK_SHARED_FUNDING,
    REWARD_INDEPENDENT_FUNDING,
    SolActivityProfile,
    WalletActivity,
    aggregate_sol_activity,
    assess_economic_authenticity,
    build_manipulation_graph,
)
from smart_money_bot.lab.bankroll import (
    AllocationCandidate,
    BankrollState,
    BreakerInputs,
    SizingInputs,
    allocate_capital,
    apply_entry,
    evaluate_circuit_breakers,
    size_position,
)
from smart_money_bot.lab.bankroll import (
    apply_exit as apply_bankroll_exit,
)
from smart_money_bot.lab.config import DEFAULT_LAB_CONFIG, LabConfig
from smart_money_bot.lab.costs import (
    estimate_expected_edge,
    estimate_round_trip_cost,
    leg_costs,
)
from smart_money_bot.lab.decision import (
    Decision,
    EvidenceQuality,
    Reason,
    SafetyStatus,
    TradeDecision,
    decision_from_json,
    decision_to_json,
)
from smart_money_bot.lab.entry import EntryContext, evaluate_entry
from smart_money_bot.lab.exits import (
    EXIT_HARD_STOP,
    EXIT_LIQUIDITY_EMERGENCY,
    EXIT_MILESTONE,
    EXIT_MOMENTUM_DECAY,
    EXIT_SAFETY_EMERGENCY,
    EXIT_TIME_STOP,
    EXIT_TRAILING,
    HOLD_HEALTHY,
    ExitContext,
    apply_exit,
    observe,
    open_position,
    plan_exit,
    position_from_json,
    position_to_json,
)
from smart_money_bot.lab.identity import (
    NO_DESCRIPTION,
    build_token_identity,
    identity_from_payload,
    safe_image_url,
    sanitize_description,
)
from smart_money_bot.lab.lifecycle import (
    COOLDOWN,
    FIRST_DISCOVERY,
    FIRST_QUALIFIED,
    REENTRY_WATCH,
    RETRACED,
    TRIGGER_LIFECYCLE,
    TRIGGER_SAFETY,
    WINNER,
    LifecycleObservation,
    PublicationState,
    advance_lifecycle,
    assess_reentry,
    lifecycle_from_json,
    lifecycle_to_json,
    new_lifecycle,
    should_republish,
)
from smart_money_bot.lab.regime import RISK_OFF, RegimeSample, classify_regime
from smart_money_bot.lab.registry import (
    EDGE_CONSUMED,
    EDGE_FRESH,
    HIGH_VALUE_EARLY,
    IDEA_ONLY_ACCOUNTS,
    INSUFFICIENT_DATA,
    MENTIONED,
    PROMOTED,
    TIER_A,
    TIER_A_ACCOUNTS,
    TIER_B,
    TIER_B_ACCOUNTS,
    TIER_C,
    TIER_C_ACCOUNTS,
    TIER_IDEA,
    AccountObservation,
    account_tier,
    build_signal,
    dedupe_signals,
    is_muted,
    lookup_account,
    measure_account,
    plan_social_fetch,
    registry_snapshot,
    signal_edge_state,
)
from smart_money_bot.lab.replay import (
    EXIT_STAGED_TP,
    POLICY_IMMEDIATE,
    POLICY_NO_TRADE,
    SAMPLE_TOO_SMALL,
    MissedWinner,
    PerformanceReport,
    ReplayObservation,
    TradeRecord,
    analyze_missed_winners,
    attribute_loss,
    compare_policies,
    evaluate_promotion,
    replay_policy,
    split_walk_forward,
    summarize_trades,
)
from smart_money_bot.lab.smartmoney import (
    ACCUMULATING,
    DISTRIBUTING,
    LATE_CHASER,
    POOR_HISTORY,
    PROVEN_EARLY,
    WalletOutcome,
    assess_smart_money,
    build_reputation,
    decay_reputation,
    exit_pressure,
    hold_support,
)
from smart_money_bot.lab.smartmoney import (
    UNKNOWN as WALLET_UNKNOWN,
)
from smart_money_bot.lab.timeline import (
    PRICE_OBSERVED,
    Provenance,
    TokenEvent,
    TokenTimeline,
    event_from_json,
    event_to_json,
    observation_events,
)

MINT = "So11111111111111111111111111111111111111112"
D = Decimal


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


def test_about_is_never_invented() -> None:
    identity = build_token_identity(MINT, metadata={"source": "dexscreener"})
    assert identity.description == NO_DESCRIPTION
    assert not identity.has_description
    assert sanitize_description("   ") == NO_DESCRIPTION
    assert sanitize_description(None) == NO_DESCRIPTION


def test_token_image_falls_back_gracefully() -> None:
    for hostile in (
        "http://cdn.example.com/a.png",
        "https://127.0.0.1/a.png",
        "https://localhost/a.png",
        "https://user:pass@cdn.example.com/a.png",
        "https://cdn.example.com/" + "a" * 600 + ".png",
        "https://example.com/not-an-image",
    ):
        url, reason = safe_image_url(hostile)
        assert url == "", hostile
        assert reason

    url, reason = safe_image_url("https://cdn.example.com/logo.png")
    assert url == "https://cdn.example.com/logo.png"
    assert reason == ""


def test_malicious_metadata_cannot_break_a_card() -> None:
    identity = build_token_identity(
        MINT,
        metadata={
            "source": "dexscreener",
            "name": "**@everyone** [click](https://evil)",
            "symbol": "X" * 200,
            "description": "Y" * 5_000,
            "image": "javascript:alert(1)",
        },
    )
    assert "[" not in identity.name and "*" not in identity.name
    assert len(identity.symbol) <= 16
    assert len(identity.description) <= 281
    assert identity.image_url == ""
    assert any("image" in warning.lower() for warning in identity.warnings)


def test_unverified_social_is_never_marked_official() -> None:
    guessed = build_token_identity(
        MINT,
        metadata={"source": "unverified_name_match", "twitter": "https://x.com/someproject"},
    )
    x_link = guessed.link("X")
    assert x_link is not None
    assert x_link.official is False
    assert "(unverified)" in x_link.label

    authoritative = build_token_identity(
        MINT, metadata={"source": "dexscreener", "twitter": "https://x.com/someproject"}
    )
    assert authoritative.link("X").official is True


def test_identity_round_trips_through_storage_payload() -> None:
    identity = build_token_identity(
        MINT,
        metadata={"source": "dexscreener", "image": "https://cdn.example.com/a.png"},
        name="Real Token",
        symbol="REAL",
        resolved_at=100,
    )
    payload = {
        "mint": identity.mint,
        "name": identity.name,
        "symbol": identity.symbol,
        "description": identity.description,
        "image_url": identity.image_url,
        "links": [
            {
                "platform": link.platform,
                "url": link.url,
                "source": link.source,
                "official": link.official,
            }
            for link in identity.links
        ],
        "resolved_at": identity.resolved_at,
    }
    rebuilt = identity_from_payload(payload)
    assert rebuilt.name == "Real Token"
    assert rebuilt.image_url == "https://cdn.example.com/a.png"
    assert len(rebuilt.links) == len(identity.links)


# ---------------------------------------------------------------------------
# lifecycle, dedupe and re-entry
# ---------------------------------------------------------------------------


def _observe(record, at, price, market_cap, **kwargs):
    return advance_lifecycle(
        record,
        LifecycleObservation(
            observed_at=at,
            price_usd=D(price),
            market_cap_usd=D(market_cap),
            **kwargs,
        ),
    )


def test_first_discovery_then_qualification() -> None:
    record = new_lifecycle(MINT, now=0)
    assert record.state == FIRST_DISCOVERY
    assert record.is_fresh_setup
    record = _observe(record, 10, "0.001", "32000", surfaced=True, qualified=True)
    assert record.state == FIRST_QUALIFIED
    assert record.first_surface_market_cap_usd == D("32000")


def test_old_pump_is_never_a_fresh_setup_again() -> None:
    """The exact $32k → $150k → $38k scenario from the product contract."""

    record = new_lifecycle(MINT, now=0)
    record = _observe(record, 10, "0.000032", "32000", surfaced=True, qualified=True)
    record = _observe(record, 60, "0.00015", "150000")
    assert record.state == WINNER
    assert record.max_return_from_surface_percent == D("368.75")

    record = _observe(record, 120, "0.000038", "38000")
    assert record.state == RETRACED
    assert record.current_drawdown_percent > D("70")
    assert record.historical_high_market_cap_usd == D("150000")
    assert record.first_surface_market_cap_usd == D("32000")
    assert not record.is_fresh_setup
    assert record.is_reentry
    assert record.cycle_count == 1


def test_lifecycle_survives_a_restart_round_trip() -> None:
    record = new_lifecycle(MINT, now=0)
    record = _observe(record, 10, "0.000032", "32000", surfaced=True, qualified=True)
    record = _observe(record, 60, "0.00015", "150000")
    record = _observe(record, 120, "0.000038", "38000")

    rehydrated = lifecycle_from_json(lifecycle_to_json(record))
    assert rehydrated == record
    assert rehydrated.state != FIRST_DISCOVERY
    assert rehydrated.first_discovered_at == record.first_discovered_at


def test_cooldown_then_reentry_watch() -> None:
    record = new_lifecycle(MINT, now=0)
    record = _observe(record, 10, "0.001", "32000", surfaced=True, qualified=True)
    record = _observe(record, 60, "0.01", "320000")
    record = _observe(record, 120, "0.002", "64000")
    assert record.state == RETRACED
    record = _observe(record, 180, "0.002", "64000")
    assert record.state == COOLDOWN
    assert record.cooldown_until == 180 + DEFAULT_LAB_CONFIG.cooldown_seconds
    record = _observe(record, 180 + DEFAULT_LAB_CONFIG.cooldown_seconds + 1, "0.002", "64000")
    assert record.state == REENTRY_WATCH


def test_valid_reentry_requires_all_new_evidence() -> None:
    record = new_lifecycle(MINT, now=0)
    record = _observe(record, 10, "0.001", "32000", surfaced=True, qualified=True)
    record = _observe(record, 60, "0.01", "320000")
    record = _observe(record, 120, "0.002", "64000")
    record = dataclasses.replace(
        record,
        stable_observations=4,
        lower_lows=0,
        trough_price_usd=D("0.002"),
        volume_at_trough_usd=D("1000"),
        buyers_at_trough=10,
        last_liquidity_usd=D("40000"),
    )
    good = LifecycleObservation(
        observed_at=300,
        price_usd=D("0.0022"),
        market_cap_usd=D("70000"),
        liquidity_usd=D("45000"),
        volume_usd=D("2000"),
        independent_buyers=25,
        momentum_score=D("65"),
        organic_score=D("70"),
        safety_status="PASS",
    )
    assessment = assess_reentry(
        record,
        good,
        smart_money_accumulating=True,
        distribution_fading=True,
        route_healthy=True,
        expected_net_edge_percent=D("30"),
    )
    assert assessment.qualified, assessment.missing
    assert not assessment.dead_cat


def test_cheap_again_is_not_good_again() -> None:
    """A lower price with no new evidence stays REENTRY_WATCH."""

    record = new_lifecycle(MINT, now=0)
    record = _observe(record, 10, "0.001", "32000", surfaced=True, qualified=True)
    record = _observe(record, 60, "0.01", "320000")
    record = _observe(record, 120, "0.002", "64000")
    weak = LifecycleObservation(
        observed_at=300,
        price_usd=D("0.002"),
        market_cap_usd=D("64000"),
        liquidity_usd=D("10000"),
        volume_usd=D("100"),
        independent_buyers=3,
        momentum_score=D("15"),
        organic_score=D("20"),
        safety_status="UNKNOWN",
    )
    assessment = assess_reentry(record, weak)
    assert not assessment.qualified
    assert "SAFETY_PASS" in assessment.missing
    assert assessment.state == REENTRY_WATCH


def test_dead_cat_bounce_is_detected() -> None:
    record = new_lifecycle(MINT, now=0)
    record = dataclasses.replace(
        record,
        trough_price_usd=D("1"),
        stable_observations=0,
        lower_lows=5,
        last_price_usd=D("1.05"),
    )
    observation = LifecycleObservation(observed_at=100, price_usd=D("1.1"))
    assessment = assess_reentry(
        record,
        observation,
        recent_prices=(D("3"), D("2"), D("1.5"), D("1.1")),
    )
    assert assessment.dead_cat
    assert not assessment.qualified


def test_identical_state_does_not_republish() -> None:
    previous = PublicationState(
        mint=MINT,
        published_at=1_000,
        lifecycle_state=FIRST_QUALIFIED,
        opportunity_score=D("70"),
        momentum_score=D("70"),
        organic_score=D("70"),
        safety_status="PASS",
        independent_buyers=20,
        liquidity_usd=D("50000"),
        smart_wallets=1,
    )
    verdict = should_republish(
        previous,
        now=1_100,
        lifecycle_state=FIRST_QUALIFIED,
        opportunity_score=D("70"),
        momentum_score=D("70"),
        organic_score=D("70"),
        safety_status="PASS",
        independent_buyers=20,
        liquidity_usd=D("50000"),
        smart_wallets=1,
        decision="WAIT",
    )
    assert not verdict.should_publish
    assert "no material change" in verdict.reason


def test_meaningful_transition_may_republish() -> None:
    previous = PublicationState(
        mint=MINT,
        published_at=1_000,
        lifecycle_state=FIRST_QUALIFIED,
        safety_status="UNKNOWN",
    )
    verdict = should_republish(
        previous,
        now=1_010,
        lifecycle_state=WINNER,
        opportunity_score=D("70"),
        momentum_score=D("70"),
        organic_score=D("70"),
        safety_status="PASS",
        independent_buyers=20,
        liquidity_usd=D("50000"),
        smart_wallets=1,
        decision="ENTRY",
    )
    assert verdict.should_publish
    assert TRIGGER_LIFECYCLE in verdict.triggers
    assert TRIGGER_SAFETY in verdict.triggers


def test_first_publication_is_always_allowed() -> None:
    verdict = should_republish(
        None,
        now=1,
        lifecycle_state=FIRST_QUALIFIED,
        opportunity_score=D("50"),
        momentum_score=D("50"),
        organic_score=D("50"),
        safety_status="PASS",
        independent_buyers=10,
        liquidity_usd=D("50000"),
        smart_wallets=0,
        decision="WAIT",
    )
    assert verdict.should_publish


# ---------------------------------------------------------------------------
# event timeline / no look-ahead
# ---------------------------------------------------------------------------


def test_timeline_is_ordered_and_deduplicated() -> None:
    timeline = TokenTimeline(MINT)
    first = observation_events(MINT, occurred_at=20, price_usd=D("2"))
    second = observation_events(MINT, occurred_at=10, price_usd=D("1"))
    timeline.extend(first)
    timeline.extend(second)
    assert [event.occurred_at for event in timeline] == [10, 20]
    assert timeline.extend(first) == 0


def test_timeline_before_cannot_see_the_future() -> None:
    timeline = TokenTimeline(MINT)
    for at, price in ((10, "1"), (20, "2"), (30, "5")):
        timeline.extend(observation_events(MINT, occurred_at=at, price_usd=D(price)))
    assert timeline.peak_price(until=20) == D("2")
    assert timeline.peak_price() == D("5")
    assert all(event.occurred_at <= 20 for event in timeline.before(20))


def test_event_round_trip_and_stable_id() -> None:
    event = TokenEvent(
        mint=MINT,
        event_type=PRICE_OBSERVED,
        occurred_at=100,
        payload={"price_usd": "1"},
        provenance=Provenance(source="dexscreener", observed_at=100),
        price_usd=D("1"),
    )
    assert event_from_json(event_to_json(event)) == event
    assert event.event_id == dataclasses.replace(event).event_id


def test_unknown_event_type_is_rejected() -> None:
    with pytest.raises(ValueError):
        TokenEvent(mint=MINT, event_type="NOT_A_REAL_EVENT", occurred_at=1)


def test_provenance_reports_staleness_honestly() -> None:
    provenance = Provenance(source="tracker", source_timestamp=100, observed_at=100)
    assert provenance.age_seconds(400) == 300
    assert provenance.is_stale(400, max_age_seconds=120)
    assert not provenance.is_stale(150, max_age_seconds=120)
    assert Provenance().is_stale(100, max_age_seconds=10)


# ---------------------------------------------------------------------------
# economic authenticity
# ---------------------------------------------------------------------------


def _independent_wallets(count: int = 20) -> tuple[WalletActivity, ...]:
    return tuple(
        WalletActivity(
            wallet=f"w{index}",
            transactions=2,
            buys=2,
            volume_usd=D("100"),
            base_fee_sol=D("0.001"),
            priority_fee_sol=D("0.001"),
            first_seen_at=0,
            last_seen_at=600,
            trade_sizes_usd=(D(str(10 + index)),),
        )
        for index in range(count)
    )


def _clustered_wallets(count: int = 5) -> tuple[WalletActivity, ...]:
    return tuple(
        WalletActivity(
            wallet=f"b{index}",
            transactions=20,
            buys=10,
            sells=10,
            volume_usd=D("1000"),
            base_fee_sol=D("0.05"),
            priority_fee_sol=D("0.05"),
            first_seen_at=0,
            last_seen_at=30,
            cluster_id="cluster-1",
            trade_sizes_usd=(D("50"), D("50")),
        )
        for index in range(count)
    )


def test_activity_aggregation_reports_concentration() -> None:
    profile = aggregate_sol_activity(_clustered_wallets(), independent_buyers=2)
    assert profile.transactions == 100
    assert profile.fee_concentration_top_cluster_percent == D("100.00")
    assert profile.round_trip_wallets == 5
    assert profile.total_fees_sol == D("0.50")


def test_missing_activity_stays_unknown_not_pass() -> None:
    assessment = assess_economic_authenticity(SolActivityProfile())
    assert assessment.band == BAND_UNKNOWN
    assert assessment.quality is EvidenceQuality.UNKNOWN
    assert assessment.score == D("0")


def test_partial_sample_is_reported_as_partial() -> None:
    profile = aggregate_sol_activity(_independent_wallets(5), expected_wallets=40)
    assert profile.quality is EvidenceQuality.PARTIAL
    assessment = assess_economic_authenticity(profile)
    assert assessment.quality is EvidenceQuality.PARTIAL
    assert any("partial" in warning.lower() for warning in assessment.warnings)


def test_clustered_activity_is_penalised() -> None:
    profile = aggregate_sol_activity(_clustered_wallets(), independent_buyers=2)
    assessment = assess_economic_authenticity(
        profile,
        demand=dataclasses.make_dataclass(
            "D", [("independence_ratio", Decimal), ("fresh_wallet_percent", Decimal)]
        )(D("0.2"), D("80")),
        forensics=dataclasses.make_dataclass(
            "F", [("shared_funder_groups", tuple), ("time_linked_groups", tuple)]
        )((1,), (1,)),
    )
    assert assessment.band == BAND_MANUFACTURED
    assert assessment.looks_manufactured
    assert MARK_CLUSTER_DOMINATES in assessment.manufactured_markers
    assert MARK_REPETITIVE_SIZING in assessment.manufactured_markers
    assert MARK_SHARED_FUNDING in assessment.manufactured_markers
    assert MARK_FRESH_WALLET_SWARM in assessment.manufactured_markers


def test_independent_activity_is_favoured() -> None:
    profile = aggregate_sol_activity(_independent_wallets(), independent_buyers=18)
    assessment = assess_economic_authenticity(
        profile,
        demand=dataclasses.make_dataclass(
            "D", [("independence_ratio", Decimal), ("fresh_wallet_percent", Decimal)]
        )(D("0.85"), D("10")),
        forensics=dataclasses.make_dataclass(
            "F", [("shared_funder_groups", tuple), ("time_linked_groups", tuple)]
        )((), ()),
        independent_buyer_growth=8,
    )
    assert assessment.band == BAND_AUTHENTIC
    assert REWARD_INDEPENDENT_FUNDING in assessment.authentic_markers
    assert not assessment.looks_manufactured


def test_high_sol_spend_alone_does_not_prove_legitimacy() -> None:
    """Five wallets burning real SOL still score as manufactured."""

    heavy = _clustered_wallets()
    profile = aggregate_sol_activity(heavy)
    assert profile.total_fees_sol > D("0.4")
    assessment = assess_economic_authenticity(profile)
    assert assessment.score < DEFAULT_LAB_CONFIG.min_authenticity_score


def test_manipulation_graph_is_bounded_and_never_claims_ownership() -> None:
    observations = tuple(
        dataclasses.make_dataclass(
            "O", [("wallet", str), ("funder", str), ("upstream_funder", str)]
        )(f"w{index}", "funder-1", "upstream-1")
        for index in range(200)
    )
    graph = build_manipulation_graph(
        mint=MINT, creator="creator", observations=observations, max_nodes=20
    )
    assert len(graph.nodes) <= 20
    assert graph.truncated
    kinds = {"DEPLOYED", "TRADED", "FUNDED", "UPSTREAM_FUNDED"}
    assert all(edge.kind in kinds for edge in graph.edges)


# ---------------------------------------------------------------------------
# smart-wallet reputation
# ---------------------------------------------------------------------------


def _outcomes(count: int, **kwargs) -> list[WalletOutcome]:
    defaults = {
        "forward_return_percent": D("40"),
        "max_favourable_percent": D("80"),
        "max_adverse_percent": D("10"),
        "entered_after_move_percent": D("10"),
    }
    defaults.update(kwargs)
    return [
        WalletOutcome(wallet="w", mint=str(index), entered_at=index * 100, **defaults)
        for index in range(count)
    ]


def test_reputation_requires_a_real_sample() -> None:
    small = build_reputation("w", _outcomes(3), now=1_000)
    assert small.state == WALLET_UNKNOWN
    assert not small.has_material_sample

    proven = build_reputation("w", _outcomes(12), now=1_000)
    assert proven.state == PROVEN_EARLY
    assert proven.has_material_sample


def test_late_chaser_and_poor_history_are_classified() -> None:
    chaser = build_reputation("w", _outcomes(12, entered_after_move_percent=D("300")), now=1)
    assert chaser.state == LATE_CHASER

    poor = build_reputation(
        "w",
        _outcomes(12, forward_return_percent=D("-40"), max_favourable_percent=D("2")),
        now=1,
    )
    assert poor.state == POOR_HISTORY


def test_reputation_decays_without_refresh() -> None:
    reputation = build_reputation("w", _outcomes(12), now=1_000)
    decayed = decay_reputation(reputation, now=1_000 + 1_209_600 * 3)
    assert decayed.score < reputation.score
    assert decayed.state == WALLET_UNKNOWN


def test_clustered_smart_money_is_not_consensus() -> None:
    reputation = build_reputation("w", _outcomes(12), now=1)
    clustered = assess_smart_money(
        [reputation, reputation], independent_clusters=1, shared_funding=True, buy_events=4
    )
    assert not clustered.is_supporting_evidence
    assert any("share a funder" in warning for warning in clustered.warnings)

    independent = assess_smart_money(
        [reputation, reputation], independent_clusters=3, buy_events=4
    )
    assert independent.is_supporting_evidence
    assert independent.posture == ACCUMULATING


def test_stale_smart_wallet_buy_is_discounted() -> None:
    reputation = build_reputation("w", _outcomes(12), now=1)
    stale = assess_smart_money(
        [reputation],
        independent_clusters=1,
        entry_ages_seconds=(99_999,),
        max_signal_age_seconds=3_600,
        buy_events=2,
    )
    assert stale.stale_signals == 1
    assert any("stale" in warning for warning in stale.warnings)


def test_hold_and_exit_support_need_more_than_one_wallet() -> None:
    reputation = build_reputation("w", _outcomes(12), now=1)
    accumulating = assess_smart_money(
        [reputation, reputation], independent_clusters=3, buy_events=4
    )
    assert hold_support(
        accumulating, organic_healthy=True, momentum_healthy=True, liquidity_healthy=True
    )
    assert not hold_support(
        accumulating, organic_healthy=False, momentum_healthy=True, liquidity_healthy=True
    )

    distributing = assess_smart_money([reputation], sell_events=4, buy_events=0)
    assert distributing.posture == DISTRIBUTING
    assert not exit_pressure(distributing, flow_weakening=False, liquidity_worsening=False)
    assert exit_pressure(distributing, flow_weakening=True, liquidity_worsening=False)


# ---------------------------------------------------------------------------
# curated social registry
# ---------------------------------------------------------------------------


def test_registry_tiers_match_the_curated_review() -> None:
    snapshot = registry_snapshot()
    assert "solana" in snapshot[TIER_A] and "pumpfun" in snapshot[TIER_A]
    assert "lookonchain" in snapshot[TIER_B] and "arkham" in snapshot[TIER_B]
    assert "toly" in snapshot[TIER_C] and "ansem" in snapshot[TIER_C]
    assert "elonmusk" in snapshot[TIER_IDEA] and "mrbeast" in snapshot[TIER_IDEA]
    assert len(TIER_A_ACCOUNTS) == 9
    assert len(TIER_B_ACCOUNTS) == 10
    assert len(TIER_C_ACCOUNTS) == 8
    assert len(IDEA_ONLY_ACCOUNTS) == 16


def test_no_account_in_any_tier_can_enter_or_launch() -> None:
    for account in (*TIER_A_ACCOUNTS, *TIER_B_ACCOUNTS, *TIER_C_ACCOUNTS, *IDEA_ONLY_ACCOUNTS):
        assert account.can_enter is False
        assert account.can_launch is False


def test_idea_only_accounts_cannot_qualify_a_token() -> None:
    for account in IDEA_ONLY_ACCOUNTS:
        assert account.idea_only
        assert account.can_qualify_token is False
    for account in (*TIER_A_ACCOUNTS, *TIER_B_ACCOUNTS, *TIER_C_ACCOUNTS):
        assert account.can_qualify_token is True


def test_idea_only_signal_cannot_qualify_or_enter() -> None:
    signal = build_signal(
        platform="x",
        account="elonmusk",
        url="https://x.com/elonmusk/status/1",
        observed_at=100,
        source_timestamp=100,
        mint=MINT,
        exact_mint_confidence=D("100"),
    )
    assert signal.tier == TIER_IDEA
    assert signal.can_qualify_token is False
    assert signal.can_enter is False
    assert signal.can_launch is False


def test_everything_outside_the_registry_is_muted() -> None:
    assert is_muted("binance")
    assert is_muted("random_influencer_9000")
    assert not is_muted("@Lookonchain")
    assert account_tier("nobody") == "MUTED"
    assert lookup_account("nobody") is None


def test_broad_radar_is_off_by_default_and_muted_accounts_are_not_polled() -> None:
    assert DEFAULT_LAB_CONFIG.broad_social_radar_enabled is False
    plan = plan_social_fetch([], now=1_000)
    assert plan.accounts == ()
    assert "disabled" in plan.reason

    plan = plan_social_fetch(["binance", "lookonchain"], now=1_000)
    assert plan.accounts == ("lookonchain",)
    assert plan.skipped_muted == ("binance",)


def test_social_window_and_budget_are_bounded() -> None:
    plan = plan_social_fetch(
        [account.handle for account in TIER_B_ACCOUNTS], now=1_000, requests_used_today=0
    )
    assert len(plan.accounts) <= DEFAULT_LAB_CONFIG.social_max_accounts_per_check
    assert plan.posts_per_account == DEFAULT_LAB_CONFIG.social_posts_per_account
    assert plan.estimated_posts <= 60

    exhausted = plan_social_fetch(
        ["lookonchain"],
        now=1_000,
        requests_used_today=DEFAULT_LAB_CONFIG.social_daily_request_budget,
    )
    assert exhausted.accounts == ()
    assert "budget" in exhausted.reason


def test_account_metadata_cache_prevents_a_refetch() -> None:
    plan = plan_social_fetch(["toly"], now=1_000, cache={"toly": 999})
    assert plan.accounts == ()
    assert plan.skipped_cached == ("toly",)

    stale = plan_social_fetch(
        ["toly"],
        now=1_000 + DEFAULT_LAB_CONFIG.social_account_cache_seconds + 1,
        cache={"toly": 999},
    )
    assert stale.accounts == ("toly",)


def test_duplicate_posts_are_deduplicated() -> None:
    signal = build_signal(
        platform="x",
        account="ansem",
        url="https://x.com/ansem/status/1",
        observed_at=1,
        source_timestamp=1,
        text_hash="abc",
    )
    assert len(dedupe_signals([signal, signal, signal])) == 1


def test_mention_is_not_a_buy() -> None:
    mention = build_signal(
        platform="x",
        account="ansem",
        url="https://x.com/a/1",
        observed_at=1,
        source_timestamp=1,
        classification=MENTIONED,
    )
    promoted = build_signal(
        platform="x",
        account="ansem",
        url="https://x.com/a/2",
        observed_at=1,
        source_timestamp=1,
        classification=PROMOTED,
    )
    assert not mention.is_disclosed_position
    assert not promoted.is_disclosed_position


def test_stale_signal_becomes_edge_consumed() -> None:
    signal = build_signal(
        platform="x",
        account="ansem",
        url="https://x.com/a/1",
        observed_at=100,
        source_timestamp=100,
        price_at_signal=D("1"),
    )
    state, move = signal_edge_state(signal, current_price=D("1.02"), now=120)
    assert state == EDGE_FRESH
    assert move == D("2.00")

    state, _ = signal_edge_state(signal, current_price=D("5"), now=120)
    assert state == EDGE_CONSUMED

    state, _ = signal_edge_state(signal, current_price=D("1.01"), now=100_000)
    assert state == EDGE_CONSUMED


def test_account_weighting_requires_a_sample() -> None:
    few = [
        AccountObservation(
            account="lookonchain",
            mint=MINT,
            signalled_at=index,
            move_before_signal_percent=D("5"),
            forward_returns={3_600: D("40")},
            max_favourable_percent=D("60"),
        )
        for index in range(5)
    ]
    performance = measure_account("lookonchain", few)
    assert performance.classification == INSUFFICIENT_DATA
    assert performance.strategy_weight == D("0")

    many = [
        AccountObservation(
            account="lookonchain",
            mint=str(index),
            signalled_at=index,
            move_before_signal_percent=D("5"),
            forward_returns={3_600: D("40")},
            max_favourable_percent=D("60"),
        )
        for index in range(30)
    ]
    strong = measure_account("lookonchain", many)
    assert strong.classification == HIGH_VALUE_EARLY
    assert strong.strategy_weight > D("0")


def test_idea_only_account_never_earns_strategy_weight() -> None:
    observations = [
        AccountObservation(
            account="elonmusk",
            mint=str(index),
            signalled_at=index,
            move_before_signal_percent=D("2"),
            forward_returns={3_600: D("90")},
            max_favourable_percent=D("150"),
        )
        for index in range(50)
    ]
    performance = measure_account("elonmusk", observations)
    assert performance.tier == TIER_IDEA
    assert performance.strategy_weight == D("0")


# ---------------------------------------------------------------------------
# costs
# ---------------------------------------------------------------------------


def test_round_trip_cost_counts_both_legs_and_every_fee() -> None:
    cost = estimate_round_trip_cost(
        D("5"), buy_price_impact_percent=D("1"), sell_price_impact_percent=D("1.5")
    )
    assert cost.platform_fees_usd == D("0.100000")
    assert cost.network_fees_usd > 0
    assert cost.priority_fees_usd > 0
    assert cost.price_impact_usd == D("0.125000")
    assert cost.slippage_usd == D("0.080000")
    assert cost.total_cost_usd == (
        cost.platform_fees_usd
        + cost.network_fees_usd
        + cost.priority_fees_usd
        + cost.price_impact_usd
        + cost.slippage_usd
    )


def test_net_pnl_is_gross_minus_every_cost() -> None:
    costs = leg_costs(D("10"), price_impact_percent=D("1"))
    realized = dataclasses.replace(costs, gross_pnl_usd=D("2"))
    assert realized.net_pnl_usd == D("2") - realized.total_cost_usd
    payload = realized.as_dict()
    assert set(payload) >= {
        "GROSS_PNL",
        "PLATFORM_FEES",
        "NETWORK_FEES",
        "PRIORITY_FEES",
        "PRICE_IMPACT",
        "SLIPPAGE",
        "TOTAL_COST",
        "NET_PNL",
    }


def test_expected_edge_requires_a_cushion_over_costs() -> None:
    strong = estimate_expected_edge(
        notional_usd=D("5"),
        gross_upside_percent=D("60"),
        downside_percent=D("30"),
        buy_price_impact_percent=D("1"),
        confidence=D("70"),
        quality=EvidenceQuality.COMPLETE,
    )
    assert strong.meets(DEFAULT_LAB_CONFIG)

    thin = estimate_expected_edge(
        notional_usd=D("5"),
        gross_upside_percent=D("12"),
        downside_percent=D("30"),
        buy_price_impact_percent=D("1"),
        confidence=D("70"),
        quality=EvidenceQuality.COMPLETE,
    )
    assert not thin.meets(DEFAULT_LAB_CONFIG)


def test_unknown_upside_is_never_optimistic() -> None:
    unknown = estimate_expected_edge(
        notional_usd=D("5"), gross_upside_percent=None, downside_percent=None
    )
    assert unknown.quality is EvidenceQuality.UNKNOWN
    assert unknown.net_edge_percent == D("0")
    assert not unknown.meets(DEFAULT_LAB_CONFIG)


# ---------------------------------------------------------------------------
# entry engine
# ---------------------------------------------------------------------------


def _authentic():
    from smart_money_bot.lab.authenticity import AuthenticityAssessment

    return AuthenticityAssessment(
        score=D("75"),
        band=BAND_AUTHENTIC,
        quality=EvidenceQuality.COMPLETE,
        activity=SolActivityProfile(quality=EvidenceQuality.COMPLETE, sampled_wallets=20),
    )


def _entry_context(**overrides) -> EntryContext:
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
        "decision_latency_ms": 1_000,
        "authenticity": _authentic(),
        "regime": MarketRegime(state=NORMAL, samples=20),
        "expected_upside_percent": D("60"),
        "expected_downside_percent": D("30"),
        "edge_confidence": D("70"),
        "move_since_first_surface_percent": D("20"),
        "signal_age_seconds": 60,
    }
    base.update(overrides)
    return EntryContext(**base)


def _decide(context: EntryContext, *, lifecycle=None, bankroll=None, **kwargs):
    return evaluate_entry(
        context,
        lifecycle=lifecycle or new_lifecycle(MINT, now=0),
        bankroll=bankroll or BankrollState(),
        **kwargs,
    )


def test_entry_is_allowed_for_a_complete_clean_setup() -> None:
    result = _decide(_entry_context())
    assert result.decision.decision is Decision.ENTRY
    assert result.entry_eligible
    assert result.decision.size_usd == D("5.00")
    assert result.decision.safety is SafetyStatus.PASS
    assert result.decision.evidence_quality is EvidenceQuality.COMPLETE
    assert Reason.SAFETY_PASS in result.decision.reason_codes


def test_safety_unknown_blocks_automatic_entry() -> None:
    result = _decide(_entry_context(safety_status="UNKNOWN", safety_entry_eligible=False))
    assert not result.entry_eligible
    assert Reason.SAFETY_UNKNOWN in result.decision.reason_codes
    assert result.decision.safety is SafetyStatus.UNKNOWN


def test_safety_fail_blocks_automatic_entry() -> None:
    result = _decide(_entry_context(safety_status="FAIL"))
    assert result.decision.decision is Decision.REJECT
    assert Reason.SAFETY_FAIL in result.decision.reason_codes


def test_overextension_blocks_entry() -> None:
    result = _decide(_entry_context(move_since_first_surface_percent=D("500")))
    assert result.decision.decision is Decision.REJECT
    assert Reason.ALREADY_EXTENDED in result.decision.reason_codes


def test_price_acceleration_without_buyers_is_exit_liquidity() -> None:
    result = _decide(
        _entry_context(price_acceleration_ratio=D("6"), buyer_acceleration_ratio=D("0.2"))
    )
    assert Reason.ALREADY_EXTENDED in result.decision.reason_codes


def test_edge_consumed_blocks_entry() -> None:
    result = _decide(_entry_context(move_since_signal_percent=D("400")))
    assert Reason.EDGE_CONSUMED in result.decision.reason_codes
    assert result.decision.decision is Decision.REJECT


def test_low_liquidity_blocks_entry() -> None:
    result = _decide(_entry_context(liquidity_usd=D("5000")))
    assert Reason.LIQUIDITY_TOO_WEAK in result.decision.reason_codes


def test_excessive_impact_and_slippage_block_entry() -> None:
    impact = _decide(_entry_context(buy_price_impact_percent=D("9")))
    assert Reason.PRICE_IMPACT_TOO_HIGH in impact.decision.reason_codes

    slippage = _decide(_entry_context(slippage_percent=D("9")))
    assert Reason.SLIPPAGE_TOO_HIGH in slippage.decision.reason_codes


def test_insufficient_net_edge_blocks_entry() -> None:
    result = _decide(_entry_context(expected_upside_percent=D("8")))
    assert Reason.EXPECTED_NET_EDGE_TOO_LOW in result.decision.reason_codes
    assert result.decision.decision is Decision.WAIT


def test_manufactured_activity_blocks_entry() -> None:
    from smart_money_bot.lab.authenticity import AuthenticityAssessment

    fake = AuthenticityAssessment(
        score=D("10"),
        band=BAND_MANUFACTURED,
        quality=EvidenceQuality.COMPLETE,
        activity=SolActivityProfile(quality=EvidenceQuality.COMPLETE, sampled_wallets=5),
    )
    result = _decide(_entry_context(authenticity=fake))
    assert Reason.MANUFACTURED_ACTIVITY in result.decision.reason_codes
    assert result.decision.decision is Decision.REJECT


def test_unknown_authenticity_sizes_down_but_does_not_block() -> None:
    """Missing activity evidence is never read as authentic, and never as a pass."""

    from smart_money_bot.lab.authenticity import AuthenticityAssessment

    unknown = _decide(_entry_context(authenticity=AuthenticityAssessment()))
    assert unknown.decision.decision is Decision.ENTRY
    assert unknown.decision.evidence_quality is EvidenceQuality.PARTIAL
    assert Reason.AUTHENTIC_ECONOMIC_ACTIVITY not in unknown.decision.reason_codes
    known = _decide(_entry_context())
    assert unknown.decision.size_usd < known.decision.size_usd


def test_too_few_independent_buyers_blocks_entry() -> None:
    result = _decide(_entry_context(independent_buyers=2))
    assert Reason.INDEPENDENT_BUYERS_TOO_FEW in result.decision.reason_codes


def test_clustered_smart_money_cannot_rescue_an_entry() -> None:
    reputation = build_reputation("w", _outcomes(12), now=1)
    clustered = assess_smart_money([reputation], independent_clusters=1, shared_funding=True)
    result = _decide(_entry_context(smart_money=clustered))
    assert Reason.CLUSTERED_SMART_MONEY in result.decision.reason_codes


def test_smart_money_cannot_override_safety_or_overextension() -> None:
    reputation = build_reputation("w", _outcomes(12), now=1)
    strong = assess_smart_money([reputation, reputation], independent_clusters=3, buy_events=4)
    unsafe = _decide(_entry_context(smart_money=strong, safety_status="UNKNOWN"))
    assert not unsafe.entry_eligible

    extended = _decide(
        _entry_context(smart_money=strong, move_since_first_surface_percent=D("900"))
    )
    assert not extended.entry_eligible


def test_late_social_signal_blocks_entry() -> None:
    result = _decide(_entry_context(social_edge_state="EDGE_CONSUMED"))
    assert Reason.SOCIAL_SIGNAL_LATE in result.decision.reason_codes


def test_hostile_regime_blocks_entry() -> None:
    from smart_money_bot.lab.regime import LIQUIDITY_STRESS, MarketRegime

    result = _decide(
        _entry_context(regime=MarketRegime(state=LIQUIDITY_STRESS, samples=30))
    )
    assert Reason.REGIME_UNFAVOURABLE in result.decision.reason_codes


def test_degraded_data_blocks_entry() -> None:
    assert Reason.DATA_DEGRADED in _decide(_entry_context(data_degraded=True)).decision.reason_codes
    assert Reason.DATA_UNKNOWN in _decide(_entry_context(data_unknown=True)).decision.reason_codes


def test_cooldown_yields_a_cooldown_decision() -> None:
    lifecycle = dataclasses.replace(
        new_lifecycle(MINT, now=0), cooldown_until=2_000, cycle_count=1
    )
    result = _decide(_entry_context(), lifecycle=lifecycle)
    assert result.decision.decision is Decision.COOLDOWN
    assert Reason.COOLDOWN_ACTIVE in result.decision.reason_codes


def test_unstabilized_reentry_is_watched_not_entered() -> None:
    lifecycle = dataclasses.replace(
        new_lifecycle(MINT, now=0), state=REENTRY_WATCH, cycle_count=1
    )
    from smart_money_bot.lab.lifecycle import ReentryAssessment

    result = _decide(
        _entry_context(),
        lifecycle=lifecycle,
        reentry=ReentryAssessment(qualified=False, missing=("SAFETY_PASS",)),
    )
    assert result.decision.decision is Decision.REENTRY_WATCH
    assert Reason.REENTRY_NOT_STABILIZED in result.decision.reason_codes
    assert not result.entry_eligible


def test_qualified_reentry_can_enter_at_a_reduced_size() -> None:
    from smart_money_bot.lab.lifecycle import ReentryAssessment

    lifecycle = dataclasses.replace(
        new_lifecycle(MINT, now=0), state=REENTRY_WATCH, cycle_count=1
    )
    result = _decide(
        _entry_context(),
        lifecycle=lifecycle,
        reentry=ReentryAssessment(qualified=True, satisfied=("SAFETY_PASS",)),
    )
    assert result.decision.decision is Decision.REENTRY_QUALIFIED
    assert result.entry_eligible
    assert result.decision.size_usd < D("5")


def test_never_averages_down_into_an_open_position() -> None:
    result = _decide(_entry_context(), exposure_in_token_usd=D("5"))
    assert Reason.NO_AVERAGE_DOWN in result.decision.reason_codes
    assert not result.entry_eligible


def test_bankroll_limits_block_entry() -> None:
    full = dataclasses.replace(BankrollState(), open_positions=5)
    assert Reason.MAX_POSITIONS_REACHED in _decide(
        _entry_context(), bankroll=full
    ).decision.reason_codes

    broke = dataclasses.replace(BankrollState(), cash_usd=D("0.5"))
    assert Reason.BANKROLL_EXHAUSTED in _decide(
        _entry_context(), bankroll=broke
    ).decision.reason_codes

    capped = dataclasses.replace(BankrollState(), day_realized_net_pnl_usd=D("-20"))
    assert Reason.DAILY_LOSS_CAP in _decide(
        _entry_context(), bankroll=capped
    ).decision.reason_codes


def test_decision_round_trips_and_records_its_rules() -> None:
    result = _decide(_entry_context())
    decision = result.decision
    assert decision.strategy_version
    assert decision.config_hash == DEFAULT_LAB_CONFIG.config_hash()
    assert decision.bot_version
    assert decision_from_json(decision_to_json(decision)) == decision


def test_a_decision_with_no_size_is_never_entry_eligible() -> None:
    decision = TradeDecision(mint=MINT, decision=Decision.ENTRY, size_usd=D("0"))
    assert not decision.entry_eligible


# ---------------------------------------------------------------------------
# sizing, allocation and breakers
# ---------------------------------------------------------------------------


def test_sizing_only_ever_reduces() -> None:
    state = BankrollState()
    clean = size_position(
        state,
        SizingInputs(liquidity_usd=D("60000"), regime="NORMAL", authenticity_score=D("80")),
    )
    assert clean.size_usd == DEFAULT_LAB_CONFIG.normal_position_usd

    weak = size_position(
        state,
        SizingInputs(liquidity_usd=D("16000"), regime=RISK_OFF, is_reentry=True),
    )
    assert weak.size_usd < clean.size_usd
    assert weak.size_usd >= DEFAULT_LAB_CONFIG.min_position_usd


def test_losing_streak_reduces_never_increases_size() -> None:
    inputs = SizingInputs(liquidity_usd=D("60000"), regime="NORMAL", authenticity_score=D("80"))
    calm = size_position(BankrollState(), inputs)
    losing = size_position(
        dataclasses.replace(BankrollState(), consecutive_losses=3),
        inputs,
    )
    assert losing.size_usd < calm.size_usd
    assert "recent losing streak" in losing.reductions


def test_capital_is_allocated_by_risk_adjusted_edge_not_arrival() -> None:
    weak = AllocationCandidate(
        mint="weak",
        expected_net_edge_percent=D("15"),
        edge_confidence=D("40"),
        requested_usd=D("5"),
        downside_percent=D("40"),
    )
    strong = AllocationCandidate(
        mint="strong",
        expected_net_edge_percent=D("60"),
        edge_confidence=D("80"),
        requested_usd=D("5"),
        downside_percent=D("20"),
    )
    allocation = allocate_capital(
        [weak, strong],
        dataclasses.replace(BankrollState(), cash_usd=D("5"), open_positions=4),
    )
    assert allocation.funded[0][0] == "strong"
    assert "weak" in allocation.deferred


def test_circuit_breakers_pause_simulated_trading() -> None:
    for state, inputs in (
        (dataclasses.replace(BankrollState(), consecutive_losses=4), BreakerInputs()),
        (BankrollState(), BreakerInputs(provider_outage=True)),
        (BankrollState(), BreakerInputs(persistence_failure=True)),
        (BankrollState(), BreakerInputs(safety_provider_disagreement=True)),
        (BankrollState(), BreakerInputs(stale_critical_data_seconds=10_000)),
    ):
        assert evaluate_circuit_breakers(state, inputs).paused

    assert not evaluate_circuit_breakers(BankrollState(), BreakerInputs()).paused


def test_paused_bankroll_blocks_sizing() -> None:
    paused = dataclasses.replace(BankrollState(), paused_reason="PROVIDER_OUTAGE")
    result = size_position(paused, SizingInputs())
    assert result.blocked_reason == Reason.TRADING_PAUSED_DATA_CONTROL_RISK


def test_bankroll_bookkeeping_tracks_streaks_and_drawdown() -> None:
    state = apply_entry(BankrollState(), size_usd=D("5"))
    assert state.open_positions == 1
    assert state.cash_usd == D("95.000000")

    state = apply_bankroll_exit(
        state, cost_basis_usd=D("5"), net_proceeds_usd=D("4"), closed=True, day_key="d1"
    )
    assert state.consecutive_losses == 1
    assert state.realized_net_pnl_usd == D("-1.000000")
    assert state.drawdown_percent > 0

    state = apply_entry(state, size_usd=D("5"))
    state = apply_bankroll_exit(
        state, cost_basis_usd=D("5"), net_proceeds_usd=D("9"), closed=True, day_key="d1"
    )
    assert state.consecutive_losses == 0


# ---------------------------------------------------------------------------
# exit engine
# ---------------------------------------------------------------------------


def _position(**kwargs):
    defaults = {
        "position_id": "p1",
        "mint": MINT,
        "now": 0,
        "decision_price_usd": D("1"),
        "size_usd": D("5"),
    }
    defaults.update(kwargs)
    return open_position(**defaults)


def _exit_context(at: int, price: str, **overrides) -> ExitContext:
    base = {
        "momentum_score": D("70"),
        "organic_score": D("70"),
        "buys": 50,
        "sells": 20,
        "entry_liquidity_usd": D("50000"),
        "liquidity_usd": D("50000"),
        "safety_status": "PASS",
    }
    base.update(overrides)
    return ExitContext(now=at, price_usd=D(price), **base)


def test_exit_ladder_takes_staged_profit_not_everything_at_plus_ten() -> None:
    position = _position()
    context = _exit_context(60, "1.12")
    position = observe(position, context)
    plan = plan_exit(position, context)
    assert plan.reason_code == EXIT_MILESTONE
    position, journal = apply_exit(position, plan, context)
    assert journal is not None
    assert position.is_open
    assert position.tokens_remaining > 0
    assert "10" in position.milestones_taken


def test_each_milestone_is_taken_once() -> None:
    position = _position()
    taken = []
    for at, price in ((60, "1.12"), (120, "1.30"), (180, "1.60"), (240, "2.10")):
        context = _exit_context(at, price)
        position = observe(position, context)
        plan = plan_exit(position, context)
        position, _ = apply_exit(position, plan, context)
        taken.append(plan.reason_code)
    assert taken == [EXIT_MILESTONE] * 4
    assert position.milestones_taken == ("10", "25", "50", "100")
    assert position.tokens_remaining > 0
    assert position.realized_net_pnl_usd > 0


def test_a_healthy_runner_may_keep_more_upside() -> None:
    position = _position()
    for at, price in ((60, "1.12"), (120, "1.30"), (180, "1.60"), (240, "2.10")):
        context = _exit_context(at, price)
        position = observe(position, context)
        position, _ = apply_exit(position, plan_exit(position, context), context)
    healthy = _exit_context(300, "2.20")
    position = observe(position, healthy)
    assert plan_exit(position, healthy).reason_code == HOLD_HEALTHY


def test_trailing_protection_closes_a_faded_winner() -> None:
    position = _position()
    for at, price in ((60, "1.5"), (120, "2.0")):
        context = _exit_context(at, price)
        position = observe(position, context)
        position, _ = apply_exit(position, plan_exit(position, context), context)
    assert position.trailing_armed
    faded = _exit_context(200, "1.1", momentum_score=D("60"), buys=30, sells=25)
    position = observe(position, faded)
    plan = plan_exit(position, faded)
    assert plan.reason_code == EXIT_TRAILING
    assert plan.final


def test_momentum_decay_de_risks() -> None:
    position = _position()
    context = _exit_context(200, "1.05", momentum_score=D("10"))
    position = observe(position, context)
    plan = plan_exit(position, context)
    assert plan.reason_code == EXIT_MOMENTUM_DECAY
    assert not plan.final


def test_liquidity_emergency_and_safety_emergency_close_the_position() -> None:
    position = _position()
    emergency = _exit_context(100, "1.1", liquidity_usd=D("10000"))
    assert plan_exit(position, emergency).reason_code == EXIT_LIQUIDITY_EMERGENCY

    unsafe = _exit_context(100, "1.1", safety_status="FAIL")
    plan = plan_exit(position, unsafe)
    assert plan.reason_code == EXIT_SAFETY_EMERGENCY
    assert plan.final


def test_hard_stop_and_time_stop() -> None:
    position = _position()
    assert plan_exit(position, _exit_context(100, "0.6")).reason_code == EXIT_HARD_STOP

    stale = _exit_context(DEFAULT_LAB_CONFIG.time_stop_seconds + 10, "1.01")
    assert plan_exit(position, stale).reason_code == EXIT_TIME_STOP


def test_peak_and_drawdown_are_tracked_separately_from_realized() -> None:
    """"Was +110%, now +20%" must never look like "never went green"."""

    position = _position()
    for at, price in ((60, "2.1"), (120, "1.2")):
        context = _exit_context(at, price)
        position = observe(position, context)
    assert position.max_favourable_percent >= D("110")
    assert position.peak_price_usd == D("2.1")
    assert position.drawdown_from_peak_percent(D("1.2")) > D("40")
    assert position.realized_net_pnl_usd == D("0")


def test_partial_exit_journal_records_every_cost() -> None:
    position = _position()
    context = _exit_context(60, "1.12")
    position = observe(position, context)
    position, journal = apply_exit(position, plan_exit(position, context), context)
    assert journal is not None
    assert journal.gross_proceeds_usd > 0
    assert journal.costs.total_cost_usd > 0
    assert journal.net_proceeds_usd == journal.gross_proceeds_usd - journal.costs.total_cost_usd
    assert journal.realized_net_pnl_usd < journal.realized_gross_pnl_usd
    assert journal.tokens_remaining == position.tokens_remaining


def test_moon_bag_survives_staged_exits() -> None:
    position = _position()
    for at, price in ((60, "1.12"), (120, "1.30"), (180, "1.60"), (240, "2.10")):
        context = _exit_context(at, price)
        position = observe(position, context)
        position, _ = apply_exit(position, plan_exit(position, context), context)
    moon_bag = position.tokens * DEFAULT_LAB_CONFIG.moon_bag_percent / D("100")
    assert position.tokens_remaining >= moon_bag


def test_position_round_trips_through_json() -> None:
    position = _position()
    context = _exit_context(60, "1.12")
    position = observe(position, context)
    position, _ = apply_exit(position, plan_exit(position, context), context)
    assert position_from_json(position_to_json(position)) == position


# ---------------------------------------------------------------------------
# market regime
# ---------------------------------------------------------------------------


def test_regime_stays_unknown_on_a_small_sample() -> None:
    regime = classify_regime([RegimeSample(mint="a", observed_at=0)])
    assert regime.state == "UNKNOWN"
    assert regime.size_multiplier < D("1")


def test_hostile_regime_is_detected() -> None:
    samples = [
        RegimeSample(
            mint=str(index),
            observed_at=index,
            liquidity_usd=D("30000"),
            forward_return_percent=D("-40"),
            max_favourable_percent=D("5"),
            max_adverse_percent=D("70"),
        )
        for index in range(20)
    ]
    regime = classify_regime(samples)
    assert regime.is_hostile
    assert regime.size_multiplier < D("1")


# ---------------------------------------------------------------------------
# replay, counterfactuals, validation
# ---------------------------------------------------------------------------


def _replay_series() -> tuple[TokenTimeline, list[ReplayObservation]]:
    timeline = TokenTimeline(MINT)
    observations: list[ReplayObservation] = []
    prices = ["1", "1.05", "1.2", "1.5", "1.9", "2.4", "1.8", "1.2", "0.9"]
    for index, price in enumerate(prices):
        at = index * 60
        observations.append(
            ReplayObservation(
                at=at,
                price_usd=D(price),
                qualified=index >= 1,
                momentum_score=D("70") if index < 5 else D("15"),
                buys=50 if index < 5 else 5,
                sells=10 if index < 5 else 60,
                safety_status="PASS",
            )
        )
        timeline.extend(observation_events(MINT, occurred_at=at, price_usd=D(price)))
    return timeline, observations


def test_counterfactual_policies_are_compared_on_identical_evidence() -> None:
    timeline, observations = _replay_series()
    results = compare_policies(timeline, observations)
    assert len(results) == 8 * 9
    traded = {(item.policy, item.exit_policy) for item in results if item.traded}
    assert (POLICY_IMMEDIATE, EXIT_STAGED_TP) in traded
    assert all(not item.traded for item in results if item.policy == POLICY_NO_TRADE)


def test_replay_never_uses_future_data() -> None:
    timeline, observations = _replay_series()
    truncated = TokenTimeline(MINT)
    truncated.extend(timeline.before(180))
    early = replay_policy(
        truncated,
        observations,
        entry_policy=POLICY_IMMEDIATE,
        exit_policy=EXIT_STAGED_TP,
    )
    assert early.exited_at is not None
    assert early.exited_at <= 180
    # The full series contains a higher peak that the truncated replay cannot see.
    assert early.max_favourable_percent < D("140")


def test_net_return_is_always_below_gross() -> None:
    timeline, observations = _replay_series()
    trade = replay_policy(
        timeline, observations, entry_policy=POLICY_IMMEDIATE, exit_policy=EXIT_STAGED_TP
    )
    assert trade.net_return_percent < trade.gross_return_percent
    assert trade.cost_percent > 0


def _trades(count: int, *, net: str = "1", **kwargs) -> list[TradeRecord]:
    defaults = {
        "strategy_version": "lab-v1",
        "gross_pnl_usd": D(net) + D("0.2"),
        "cost_usd": D("0.2"),
        "size_usd": D("5"),
        "max_favourable_percent": D("40"),
    }
    defaults.update(kwargs)
    return [
        TradeRecord(
            mint=str(index),
            opened_at=index * 100,
            closed_at=index * 100 + 50,
            net_pnl_usd=D(net),
            **defaults,
        )
        for index in range(count)
    ]


def test_small_forward_sample_says_so() -> None:
    report = summarize_trades(_trades(4), strategy_version="lab-v1")
    assert report.sample_too_small
    assert report.note == SAMPLE_TOO_SMALL

    big = summarize_trades(_trades(40), strategy_version="lab-v1")
    assert not big.sample_too_small


def test_losers_are_never_hidden_from_the_metrics() -> None:
    trades = _trades(20) + _trades(20, net="-1")
    report = summarize_trades(trades, strategy_version="lab-v1")
    assert report.sample == 40
    assert report.losses == 20
    assert report.worst_trade_usd == D("-1")
    assert report.profit_factor is not None


def test_challenger_cannot_be_promoted_from_in_sample_replay_alone() -> None:
    champion = summarize_trades(_trades(40), strategy_version="champion")
    challenger = summarize_trades(_trades(40, net="3"), strategy_version="challenger")
    blocked = evaluate_promotion(champion, challenger, out_of_sample_trades=2)
    assert not blocked.promote
    assert any("out-of-sample" in reason for reason in blocked.reasons)


def test_challenger_promotion_requires_broad_improvement() -> None:
    champion = summarize_trades(_trades(40), strategy_version="champion")
    worse_drawdown = summarize_trades(
        _trades(20, net="6") + _trades(20, net="-4"), strategy_version="challenger"
    )
    verdict = evaluate_promotion(champion, worse_drawdown, out_of_sample_trades=60)
    assert not verdict.promote


def test_walk_forward_split_separates_calibration_from_forward() -> None:
    trades = _trades(10)
    calibration, forward = split_walk_forward(trades, calibration_cutoff_at=450)
    assert len(calibration) + len(forward) == 10
    assert all(item.closed_at <= 450 for item in calibration)
    assert all(item.closed_at > 450 for item in forward)


def test_loss_attribution_classifies_the_cause() -> None:
    losing = _trades(1, net="-1")[0]
    assert (
        attribute_loss(dataclasses.replace(losing, close_reason="SAFETY_DETERIORATION"))
        == "RUG_SAFETY"
    )
    assert attribute_loss(losing, entry_move_since_signal_percent=D("400")) == "EDGE_CONSUMED"
    assert attribute_loss(losing, signal_age_seconds=99_999) == "LATE_ENTRY"
    assert attribute_loss(losing, slippage_percent=D("9")) == "SLIPPAGE"
    assert attribute_loss(losing, data_degraded=True) == "DATA_QUALITY"
    assert attribute_loss(losing, decision_latency_ms=99_999) == "LATENCY"


def test_missed_winner_analysis_states_the_cost_of_relaxing_a_gate() -> None:
    analysed = analyze_missed_winners(
        [
            MissedWinner(
                mint=MINT,
                rejecting_gate="SAFETY",
                rejection_reason=Reason.SAFETY_UNKNOWN,
                later_max_favourable_percent=D("300"),
            )
        ],
        additional_losers_if_relaxed=14,
        additional_rugs_if_relaxed=3,
    )
    assert "14 more losers" in analysed[0].relaxation_cost
    assert "3 more rugs" in analysed[0].relaxation_cost


def test_performance_report_defaults_are_honest() -> None:
    empty = PerformanceReport()
    assert empty.sample == 0
    assert empty.sample_too_small
    assert empty.note == SAMPLE_TOO_SMALL


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def test_config_hash_changes_with_the_rules() -> None:
    base = LabConfig()
    assert base.config_hash() == LabConfig().config_hash()
    changed = base.with_overrides(min_liquidity_usd=D("99999"))
    assert changed.config_hash() != base.config_hash()


def test_default_bankroll_matches_the_product_contract() -> None:
    assert DEFAULT_LAB_CONFIG.bankroll_usd == D("100")
    assert DEFAULT_LAB_CONFIG.normal_position_usd == D("5")
    assert DEFAULT_LAB_CONFIG.max_position_usd == D("10")


def test_lab_package_has_no_live_execution() -> None:
    import smart_money_bot.lab as lab

    assert lab.LIVE_EXECUTION_ENABLED is False


def test_moon_bag_is_released_only_when_the_runner_stops_looking_healthy() -> None:
    from smart_money_bot.lab.exits import EXIT_MOON_BAG

    position = _position()
    for at, price in ((60, "1.12"), (120, "1.30"), (180, "1.60"), (240, "2.10")):
        context = _exit_context(at, price)
        position = observe(position, context)
        position, _ = apply_exit(position, plan_exit(position, context), context)

    # A healthy runner keeps the remainder.
    healthy = _exit_context(300, "2.20")
    position = observe(position, healthy)
    assert plan_exit(position, healthy).reason_code != EXIT_MOON_BAG

    # Only once the setup deteriorates is the remainder released.
    fading = _exit_context(360, "2.15", organic_score=D("30"), buys=10, sells=12)
    position = observe(position, fading)
    plan = plan_exit(position, fading)
    assert plan.reason_code == EXIT_MOON_BAG
    assert plan.final
    position, journal = apply_exit(position, plan, fading)
    assert journal is not None
    assert position.tokens_remaining == D("0.000000")
    assert not position.is_open


def test_entry_records_the_buy_leg_impact_for_the_fill() -> None:
    result = _decide(_entry_context(buy_price_impact_percent=D("1.25")))
    assert result.decision.evidence["buy_price_impact_percent"] == "1.25"
    assert result.decision.evidence["cost_percent"] != "1.25"
