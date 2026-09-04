"""Regression suite for early-candidate promotion and top-trader intelligence.

The production failure this release exists to fix, exactly (section 1): a
three-minute-old token at $71.93K with $21.09K liquidity, 78 buys against 48
sells, five-minute volume at 1.21x liquidity and price up 46.48% scored
**76/100** — twenty-one points clear of the runner bar — and never interrupted
anyone.

Reconstructing it is the first test below, because the fix is only worth
anything if the diagnosis is right.  The verdict was correct at the instant it
was made: a runner needs a *serious evidence category*, the buy/sell ratio was
1.625 against an organic bar of 2.0, no large buy had been observed, and no
story, wallet or catalyst evidence existed yet.  What was wrong is that nobody
ever looked again.

So the tests here are about the second look: which near-misses earn one, what
counts as *new* information, what must never count (a sybil cluster, a
concentrating holder base, known money on its way out), and the guarantee that
a candidate interrupts a human exactly once and only while the edge is live.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

import smart_money_bot.bot as bot_module
import smart_money_bot.engine as engine_module
import smart_money_bot.fast_alerts as fa
from smart_money_bot.bot import _terminal_token_url
from smart_money_bot.lab.early import (
    TIER_EARLY_HEADS_UP,
    EarlySignals,
    evaluate_early_signal,
)
from smart_money_bot.lab.forward import (
    COHORT_MULTI_KNOWN_TRADER,
    COHORT_NO_KNOWN_TRADER,
    COHORT_ONE_KNOWN_TRADER,
    COHORT_STORY_AND_TRADER,
    EVIDENCE_COHORTS,
    assign_cohorts,
)
from smart_money_bot.lab.promotion import (
    FAMILY_CONFLUENCE,
    FAMILY_HOLDER,
    FAMILY_KNOWN_TRADER,
    FAMILY_MARKET,
    WHY_ALREADY_PROMOTED,
    WHY_CONCENTRATION_WORSENING,
    WHY_EDGE_CONSUMED,
    WHY_EXPIRED,
    WHY_KNOWN_MONEY_LEAVING,
    WHY_NO_NEW_EVIDENCE,
    EarlyWatchConfig,
    PromotionEvidence,
    entry_from_json,
    evaluate_promotion,
    open_early_watch,
    prune,
    should_open_watch,
    summarise,
)
from smart_money_bot.lab.toptraders import (
    POS_ADDING,
    POS_BUYING,
    POS_DISTRIBUTING,
    POS_EXITED,
    POS_PARTIAL_SELLING,
    TraderFill,
    build_positions,
    independent_confirmations,
    join_known_traders,
    known_money_flow,
    rank_top_traders,
)
from smart_money_bot.trenches.bundles import SlotTrade, assess_bundles, detect_bundles
from smart_money_bot.trenches.dev import (
    DEV_HOLDING_EXITED,
    DEV_HOLDING_UNKNOWN,
    PriorToken,
    assess_dev_history,
    assess_dev_holding,
)
from smart_money_bot.trenches.holders import HolderSnapshot, assess_concentration_trend
from smart_money_bot.trenches.participants import BuyerRecord, assess_participants
from smart_money_bot.trending.holders import HolderSample, HolderSeries

D = Decimal

MINT = "GPR7Ax4kQ2mVn8hLdT6yWc3JbRfE9uZsXqM1oP5tH4dK"
OTHER = "7TqH1d4Vf9QG578vB99Q7ewFQPoxSYqBDxSAzBpBpump"
NOW = 1_700_000_000


# ===========================================================================
# 1. The production case, reconstructed
# ===========================================================================


def _screenshot_signals(**overrides) -> EarlySignals:
    """The exact candidate from section 1, to the decimal."""

    payload = {
        "mint": MINT,
        "now": NOW,
        "first_seen_at": NOW - 5,
        "pair_age_seconds": 180,
        "market_cap_usd": D("71930"),
        "first_seen_market_cap_usd": D("71930"),
        "liquidity_usd": D("21090"),
        # 1.214x liquidity, as printed on the card.
        "volume_5m_usd": D("21090") * D("1.214"),
        "price_change_5m_percent": D("46.48"),
        "buys_5m": 78,
        "sells_5m": 48,
        "route_available": True,
    }
    payload.update(overrides)
    return EarlySignals(**payload)


def test_the_screenshot_candidate_was_suppressed_for_a_nameable_reason() -> None:
    """Not "the score was too low" — the score was 76 against a bar of 55."""

    verdict = evaluate_early_signal(_screenshot_signals())

    assert verdict.tier == TIER_EARLY_HEADS_UP
    assert verdict.score == D("76.00")
    assert verdict.may_ping is False
    # The whole cause: no serious evidence category, so the runner branch was
    # never reachable however high the score went.
    assert verdict.evidence_categories == ()
    assert "NO_SERIOUS_EVIDENCE_CATEGORY" in verdict.why_not_pinged
    # And it was not late — the edge was still there when it was suppressed.
    assert verdict.edge_state == "EDGE_AVAILABLE"


def test_that_exact_candidate_now_earns_a_hot_watch() -> None:
    """Section 2: a strong near-miss gets a second look instead of one card."""

    verdict = evaluate_early_signal(_screenshot_signals())

    assert should_open_watch(verdict) is True


def test_a_candidate_that_already_pinged_does_not_need_a_watch() -> None:
    strong = evaluate_early_signal(
        _screenshot_signals(buys_5m=200, sells_5m=20, independent_buyers_5m=60)
    )

    assert strong.may_ping is True
    assert should_open_watch(strong) is False


def test_a_weak_or_late_candidate_is_not_watched() -> None:
    weak = evaluate_early_signal(
        _screenshot_signals(buys_5m=6, sells_5m=9, price_change_5m_percent=D("1"))
    )
    late = evaluate_early_signal(
        _screenshot_signals(first_seen_market_cap_usd=D("20000"), market_cap_usd=D("71930"))
    )

    assert should_open_watch(weak) is False
    assert late.late is True
    assert should_open_watch(late) is False


def test_the_watch_can_be_switched_off_entirely() -> None:
    verdict = evaluate_early_signal(_screenshot_signals())

    assert should_open_watch(verdict, config=EarlyWatchConfig(enabled=False)) is False


# ===========================================================================
# 2-3. Promotion needs *new* information
# ===========================================================================


def _watch(**overrides):
    verdict = evaluate_early_signal(_screenshot_signals())
    payload = {
        "verdict": verdict,
        "now": NOW,
        "market_cap_usd": D("71930"),
        "first_seen_market_cap_usd": D("71930"),
        "liquidity_usd": D("21090"),
        "buys": 78,
        "holder_count": 26,
    }
    payload.update(overrides)
    return open_early_watch(MINT, **payload)


def test_looking_at_the_same_evidence_again_is_not_a_reason_to_ping() -> None:
    """The heart of section 3: promotion is a *difference*, not a retry."""

    outcome = evaluate_promotion(
        _watch(),
        PromotionEvidence(now=NOW + 30, score=D("77"), buys=80, holder_count=27),
    )

    assert outcome.decision.promote is False
    assert outcome.entry.suppression_reason == WHY_NO_NEW_EVIDENCE
    assert outcome.entry.rechecks == 1


def test_a_known_trader_entering_promotes_the_candidate() -> None:
    outcome = evaluate_promotion(
        _watch(),
        PromotionEvidence(
            now=NOW + 40,
            score=D("77"),
            buys=82,
            proven_independent_traders=2,
            known_money_flow="KNOWN_MONEY_ACCUMULATING",
            trigger="known_trader_buy",
        ),
    )

    assert outcome.decision.promote is True
    assert outcome.decision.family == FAMILY_KNOWN_TRADER
    assert outcome.should_ping is True
    assert outcome.entry.promoted_at == NOW + 40


def test_holder_acceleration_promotes_the_candidate() -> None:
    outcome = evaluate_promotion(
        _watch(),
        PromotionEvidence(
            now=NOW + 120,
            score=D("78"),
            buys=88,
            holder_count=94,
            holders_per_minute=D("34"),
            concentration_trend="IMPROVING",
        ),
    )

    assert outcome.decision.promote is True
    assert FAMILY_HOLDER in outcome.decision.families


def test_market_acceleration_alone_still_counts_as_new_information() -> None:
    outcome = evaluate_promotion(
        _watch(),
        PromotionEvidence(now=NOW + 90, score=D("88"), buys=140, sells=52),
    )

    assert outcome.decision.promote is True
    assert outcome.decision.family == FAMILY_MARKET


def test_two_independent_families_are_named_as_confluence() -> None:
    outcome = evaluate_promotion(
        _watch(),
        PromotionEvidence(
            now=NOW + 120,
            score=D("90"),
            buys=180,
            holder_count=94,
            holders_per_minute=D("34"),
            concentration_trend="IMPROVING",
        ),
    )

    assert outcome.decision.family == FAMILY_CONFLUENCE
    assert {FAMILY_MARKET, FAMILY_HOLDER} <= set(outcome.decision.families)


def test_a_board_move_and_a_price_move_are_one_family_not_two() -> None:
    """Confluence means independent families; both of these are the market."""

    outcome = evaluate_promotion(
        _watch(),
        PromotionEvidence(
            now=NOW + 90,
            score=D("88"),
            buys=140,
            trending_event="TRENDING_ACCELERATION",
        ),
    )

    assert outcome.decision.promote is True
    assert FAMILY_CONFLUENCE not in outcome.decision.families


# ===========================================================================
# What must never promote
# ===========================================================================


def test_holders_growing_into_fewer_hands_is_not_expansion() -> None:
    """Section 12: growth plus concentration is accumulation, not distribution."""

    outcome = evaluate_promotion(
        _watch(),
        PromotionEvidence(
            now=NOW + 120,
            score=D("77"),
            buys=85,
            holder_count=94,
            holders_per_minute=D("34"),
            concentration_trend="WORSENING",
        ),
    )

    assert outcome.decision.promote is False
    assert outcome.entry.suppression_reason == WHY_CONCENTRATION_WORSENING


def test_known_money_selling_into_new_buyers_never_promotes() -> None:
    """Section 9: they are here, and they are leaving.  That is not a signal to buy."""

    outcome = evaluate_promotion(
        _watch(),
        PromotionEvidence(
            now=NOW + 120,
            score=D("77"),
            buys=85,
            proven_independent_traders=3,
            known_money_flow="KNOWN_MONEY_DISTRIBUTING",
        ),
    )

    assert outcome.decision.promote is False
    assert outcome.entry.suppression_reason == WHY_KNOWN_MONEY_LEAVING


def test_a_candidate_is_promoted_at_most_once() -> None:
    """Section 3: exactly one operator ping, at the moment of promotion."""

    first = evaluate_promotion(
        _watch(),
        PromotionEvidence(
            now=NOW + 40, score=D("77"), proven_independent_traders=2,
            known_money_flow="KNOWN_MONEY_ACCUMULATING",
        ),
    )
    second = evaluate_promotion(
        first.entry,
        PromotionEvidence(
            now=NOW + 200, score=D("99"), buys=900, proven_independent_traders=9,
            known_money_flow="KNOWN_MONEY_ACCUMULATING",
        ),
    )

    assert first.decision.promote is True
    assert second.decision.promote is False
    assert second.should_ping is False
    assert second.entry.suppression_reason == WHY_ALREADY_PROMOTED


def test_a_promoted_or_expired_watch_is_pruned() -> None:
    live = _watch()
    promoted = evaluate_promotion(
        live,
        PromotionEvidence(
            now=NOW + 40, score=D("77"), proven_independent_traders=2,
            known_money_flow="KNOWN_MONEY_ACCUMULATING",
        ),
    ).entry

    assert prune([live, promoted], now=NOW + 60) == (live,)
    assert prune([live], now=NOW + 100_000) == ()


def test_evidence_arriving_after_the_move_does_not_ping() -> None:
    """Section 3: promote *while edge remains*, not once it is spent."""

    outcome = evaluate_promotion(
        _watch(),
        PromotionEvidence(
            now=NOW + 300,
            score=D("95"),
            buys=400,
            edge_available=False,
            proven_independent_traders=4,
            known_money_flow="KNOWN_MONEY_ACCUMULATING",
        ),
    )

    assert outcome.decision.promote is False
    assert outcome.entry.suppression_reason == WHY_EDGE_CONSUMED


def test_a_watch_that_runs_out_of_window_says_so() -> None:
    outcome = evaluate_promotion(
        _watch(),
        PromotionEvidence(now=NOW + 100_000, score=D("99"), proven_independent_traders=5),
    )

    assert outcome.expired is True
    assert outcome.decision.promote is False
    assert outcome.entry.suppression_reason == WHY_EXPIRED


# ===========================================================================
# 29. Event-driven promotion
# ===========================================================================


def test_an_event_recheck_does_not_wait_for_the_timer() -> None:
    """Section 29: a known trader buying at second 12 is news at second 12."""

    entry = _watch()
    assert entry.due(now=NOW + 5) is False  # the timer would still be waiting

    outcome = evaluate_promotion(
        entry,
        PromotionEvidence(
            now=NOW + 5,
            score=D("77"),
            proven_independent_traders=2,
            known_money_flow="KNOWN_MONEY_ACCUMULATING",
            trigger="known_trader_buy",
        ),
    )

    assert outcome.decision.promote is True
    assert outcome.entry.event_rechecks == 1


def test_the_engine_reevaluates_on_a_known_trader_buy_rather_than_on_a_timer() -> None:
    source = inspect.getsource(engine_module.SmartMoneyEngine._maybe_publish_notable)

    assert "note_early_watch_event" in source
    assert 'trigger="known_trader_buy"' in source


def test_the_engine_opens_a_watch_from_the_early_lane() -> None:
    source = inspect.getsource(engine_module.SmartMoneyEngine._early_lane_task)

    assert "_open_early_watch" in source


# ===========================================================================
# 5-9. Top traders
# ===========================================================================


def _fills() -> list[TraderFill]:
    return [
        TraderFill("walletA", MINT, "BUY", NOW, D("4200"), D("1000"), D("69000")),
        TraderFill("walletA", MINT, "BUY", NOW + 60, D("1800"), D("400"), D("74000")),
        TraderFill("walletB", MINT, "BUY", NOW + 10, D("3000"), D("800"), D("70000")),
        TraderFill("walletB", MINT, "SELL", NOW + 200, D("9000"), D("760"), D("210000")),
        TraderFill("walletC", MINT, "BUY", NOW + 20, D("900"), D("250"), D("71000")),
        TraderFill("walletD", MINT, "BUY", NOW + 30, D("500"), D("120"), D("72000")),
    ]


def test_a_position_is_a_story_over_time_not_an_entry() -> None:
    """Section 9: bought-and-adding and bought-and-dumping are different trades."""

    positions = {item.wallet: item for item in build_positions(_fills(), mint=MINT)}

    assert positions["walletA"].state == POS_ADDING
    assert positions["walletB"].state == POS_EXITED
    assert positions["walletC"].state == POS_BUYING


def test_selling_half_into_a_tripling_price_is_not_distribution() -> None:
    """Tokens decide the posture, not dollars — the dollar rule gets this wrong."""

    fills = [
        TraderFill("w", MINT, "BUY", NOW, D("1000"), D("1000"), D("70000")),
        # Half the tokens, three times the money back.
        TraderFill("w", MINT, "SELL", NOW + 100, D("3000"), D("400"), D("210000")),
    ]
    position = build_positions(fills, mint=MINT)[0]

    assert position.sold_usd > position.bought_usd
    assert position.state == POS_PARTIAL_SELLING
    assert position.supportive is False and position.exiting is False


def test_selling_most_of_the_position_is_distribution() -> None:
    fills = [
        TraderFill("w", MINT, "BUY", NOW, D("1000"), D("1000"), D("70000")),
        TraderFill("w", MINT, "SELL", NOW + 100, D("2000"), D("600"), D("140000")),
    ]

    assert build_positions(fills, mint=MINT)[0].state == POS_DISTRIBUTING


def test_the_board_ranks_by_observed_size_and_by_who_arrived_first() -> None:
    positions = build_positions(_fills(), mint=MINT)
    board = rank_top_traders(positions, mint=MINT)

    assert board.top_buyers[0].wallet == "walletA"
    assert board.top_sellers[0].wallet == "walletB"
    assert board.early_entrants[0].wallet == "walletA"
    assert [item.wallet for item in board.distributing] == ["walletB"]


def test_five_wallets_with_one_funder_are_one_confirmation() -> None:
    """Section 8: a sybil group must not be able to confirm itself."""

    positions = build_positions(_fills(), mint=MINT)
    known = join_known_traders(
        positions,
        mint=MINT,
        registry={f"wallet{k}": k for k in "ABCD"},
        reputations={f"wallet{k}": ("PROVEN_EARLY", 20) for k in "ABCD"},
        clusters={"walletA": "funder:X", "walletC": "funder:X", "walletD": "funder:X"},
    )
    confirmation = independent_confirmations(known, mint=MINT)

    assert confirmation.wallet_count == 4
    assert confirmation.independent_count == 2
    assert confirmation.cluster_adjusted is True
    assert "collapse to 2 independent actor(s)" in " ".join(confirmation.notes)


def test_unclustered_wallets_each_count_once() -> None:
    """Absence of a detected link is not evidence of a link."""

    positions = build_positions(_fills(), mint=MINT)
    known = join_known_traders(
        positions,
        mint=MINT,
        registry={f"wallet{k}": k for k in "ABCD"},
        reputations={f"wallet{k}": ("PROVEN_EARLY", 20) for k in "ABCD"},
    )
    confirmation = independent_confirmations(known, mint=MINT)

    assert confirmation.independent_count == 4
    assert confirmation.cluster_adjusted is False


def test_a_reputation_label_without_samples_carries_no_weight() -> None:
    """Section 7: PROVEN_EARLY on three observations is a coincidence with a label."""

    positions = build_positions(_fills(), mint=MINT)
    known = join_known_traders(
        positions,
        mint=MINT,
        registry={"walletA": "Alpha"},
        reputations={"walletA": ("PROVEN_EARLY", 3)},
    )

    assert known[0].proven is False
    assert independent_confirmations(known, mint=MINT).proven_independent_count == 0


def test_known_money_flow_names_which_side_the_known_wallets_are_on() -> None:
    positions = build_positions(_fills(), mint=MINT)
    registry = {f"wallet{k}": k for k in "ABCD"}
    reputations = {f"wallet{k}": ("PROVEN_EARLY", 20) for k in "ABCD"}
    mixed = independent_confirmations(
        join_known_traders(positions, mint=MINT, registry=registry, reputations=reputations),
        mint=MINT,
    )
    accumulating = independent_confirmations(
        join_known_traders(
            positions, mint=MINT, registry={"walletA": "Alpha"}, reputations=reputations
        ),
        mint=MINT,
    )
    leaving = independent_confirmations(
        join_known_traders(
            positions, mint=MINT, registry={"walletB": "Bravo"}, reputations=reputations
        ),
        mint=MINT,
    )

    assert known_money_flow(mixed) == "KNOWN_MONEY_MIXED"
    assert known_money_flow(accumulating) == "KNOWN_MONEY_ACCUMULATING"
    assert known_money_flow(leaving) == "KNOWN_MONEY_DISTRIBUTING"


# ===========================================================================
# 27. Identity: evidence never crosses mints
# ===========================================================================


def test_trader_evidence_for_another_mint_is_never_folded_in() -> None:
    """Section 27: a known wallet's history on $SAMETICKER is a different token."""

    fills = [
        TraderFill("walletA", MINT, "BUY", NOW, D("100"), D("10"), D("70000")),
        # A huge position in the same-ticker clone must not appear here at all.
        TraderFill("walletA", OTHER, "BUY", NOW, D("90000"), D("50000"), D("1000")),
    ]
    positions = build_positions(fills, mint=MINT)

    assert len(positions) == 1
    assert positions[0].bought_usd == D("100")
    assert all(item.mint == MINT for item in positions)


def test_a_position_in_another_mint_cannot_confirm_this_one() -> None:
    other = build_positions(
        [TraderFill("walletA", OTHER, "BUY", NOW, D("9000"), D("500"), D("1000"))],
        mint=OTHER,
    )
    known = join_known_traders(
        other,
        mint=MINT,
        registry={"walletA": "Alpha"},
        reputations={"walletA": ("PROVEN_EARLY", 40)},
    )

    assert known == ()
    assert independent_confirmations(known, mint=MINT).proven_independent_count == 0


def test_the_database_reads_traders_by_exact_mint() -> None:
    from smart_money_bot.database import Database

    source = inspect.getsource(Database.token_swap_rows)
    assert "WHERE token_mint = ?" in source


def test_story_evidence_stays_bound_to_the_exact_mint() -> None:
    """Section 27, the story half: the thesis grader rejects cross-mint records."""

    from smart_money_bot.trending.thesis import ThesisRecord, reject_cross_mint

    record = ThesisRecord(mint=OTHER, author="a", text="x", posted_at=NOW)

    assert reject_cross_mint(record, MINT) is True
    assert reject_cross_mint(
        ThesisRecord(mint=MINT, author="a", text="x", posted_at=NOW), MINT
    ) is False


# ===========================================================================
# 10-14. Holders, concentration, fresh wallets
# ===========================================================================


def test_the_holder_series_is_a_shape_not_a_number() -> None:
    """Section 11: 26 → 51 → 94, and how long each step took."""

    series = HolderSeries(mint=MINT)
    for at, count in ((NOW, 26), (NOW + 60, 51), (NOW + 120, 94)):
        series = series.record(HolderSample(at=at, holder_count=count))

    assert series.render() == "26 → 51 → 94"
    assert series.added == 68
    assert series.per_minute == D("34.00")
    assert series.accelerating is True


def test_two_samples_cannot_say_anything_about_acceleration() -> None:
    """It takes two rates to compare rates; unknown must not read as flat."""

    series = HolderSeries(mint=MINT)
    for at, count in ((NOW, 26), (NOW + 60, 51)):
        series = series.record(HolderSample(at=at, holder_count=count))

    assert series.accelerating is None


def test_a_stale_holder_read_cannot_invent_a_dip() -> None:
    series = HolderSeries(mint=MINT)
    for at, count in ((NOW, 26), (NOW + 60, 51)):
        series = series.record(HolderSample(at=at, holder_count=count))
    raced = series.record(HolderSample(at=NOW + 30, holder_count=4))

    assert raced.render() == "26 → 51"


def test_concentration_is_reported_as_a_trend_not_a_level() -> None:
    """Section 12: 48% → 31% and 20% → 42% are opposite stories."""

    def snapshots(first: str, second: str):
        return [
            HolderSnapshot(mint=MINT, at=NOW, top10_percent=D(first)),
            HolderSnapshot(mint=MINT, at=NOW + 600, top10_percent=D(second)),
        ]

    improving = assess_concentration_trend(MINT, snapshots("48", "31"))
    worsening = assess_concentration_trend(MINT, snapshots("20", "42"))

    assert improving.state == "IMPROVING"
    assert worsening.state == "WORSENING"
    # One sample is not a trend, and must not be reported as a stable one.
    assert assess_concentration_trend(
        MINT, [HolderSnapshot(mint=MINT, at=NOW, top10_percent=D("31"))]
    ).state == "UNKNOWN"


def test_twenty_wallets_from_one_funder_are_not_twenty_participants() -> None:
    """Section 14, and the reason a raw holder count is not evidence."""

    buyers = [
        BuyerRecord(
            wallet=f"fresh{index}",
            at=NOW + index,
            amount_usd=D("100"),
            first_activity_at=NOW - 60,
            signature_count=1,
            funded_by="oneFunder",
            funded_at=NOW - 120,
        )
        for index in range(20)
    ]
    profile = assess_participants(MINT, buyers, buys=20, sells=0)

    assert profile.unique_buyers == 20
    assert profile.independent_buyers < 20
    assert profile.clusters and profile.clusters[0].size >= 3


def test_independently_funded_fresh_wallets_are_not_a_cluster() -> None:
    """A fresh wallet is not inherently bullish, and not inherently a sybil."""

    buyers = [
        BuyerRecord(
            wallet=f"fresh{index}",
            at=NOW + index * 120,
            amount_usd=D("100"),
            first_activity_at=NOW - 60,
            signature_count=1,
            funded_by=f"funder{index}",
            funded_at=NOW - 120 - index * 3_600,
        )
        for index in range(8)
    ]
    profile = assess_participants(MINT, buyers, buys=8, sells=0)

    assert profile.independent_buyers == 8
    assert profile.clusters == ()


# ===========================================================================
# 15-19. Dev and bundles
# ===========================================================================


def test_a_creator_holding_steady_and_one_exiting_are_named_differently() -> None:
    holding = assess_dev_holding(wallet="dev", initial_percent=D("5"), current_percent=D("5"))
    selling = assess_dev_holding(wallet="dev", initial_percent=D("5"), current_percent=D("1"))
    gone = assess_dev_holding(wallet="dev", initial_percent=D("5"), current_percent=D("0.1"))

    assert holding.selling is False
    assert selling.selling is True
    assert gone.posture == DEV_HOLDING_EXITED


def test_an_unknown_creator_position_is_unknown_not_clean() -> None:
    unknown = assess_dev_holding(wallet="dev", initial_percent=None, current_percent=None)

    assert unknown.posture == DEV_HOLDING_UNKNOWN
    assert unknown.selling is False


def test_creator_history_is_stated_neutrally() -> None:
    """Section 15: poor outcomes are evidence, never an accusation about a person."""

    history = assess_dev_history(
        "dev",
        [
            PriorToken(mint=f"m{index}", created_at=NOW - index * 86_400, collapsed=True)
            for index in range(4)
        ],
    )

    assert history.tokens_created == 4
    assert "SCAM" not in history.label.upper()
    assert "FAILURE" in history.label.upper()


def test_same_slot_activity_on_a_mature_token_is_not_a_launch_bundle() -> None:
    """Section 18: unrelated people share slots constantly on a busy pool."""

    trades = [SlotTrade(wallet=f"w{i}", slot=900, at=NOW + 7_200) for i in range(6)]
    launch = assess_bundles(MINT, trades, created_at=NOW, pre_graduation=True)
    mature = assess_bundles(MINT, trades, created_at=NOW, pre_graduation=False)

    assert detect_bundles(trades)
    # The same slot group is a launch bundle before graduation and ordinary
    # co-trading afterwards: on a busy pool, sharing a slot is just block
    # production.
    assert launch.bundle_count >= mature.bundle_count
    assert mature.risk != "BUNDLE_RISK_HIGH"


# ===========================================================================
# 20. Terminal navigation — link only, never a data dependency
# ===========================================================================


def test_a_template_without_the_exact_mint_produces_no_link() -> None:
    """Section 20: a link that identifies a token by anything but its address is
    the wrong link, and an unset template means no button rather than a guess."""

    assert _terminal_token_url("", MINT) == ""
    assert _terminal_token_url("https://example.org/token", MINT) == ""


def test_one_terminal_link_definition_serves_every_card() -> None:
    """Section 33: extend the existing surface rather than build a second one.

    v2.43 already links trench cards to the public per-token page; this release
    reuses that definition for the promotion card and the Discord button instead
    of inventing a parallel, differently-shaped one.
    """

    from smart_money_bot.constants import TERMINAL_TOKEN_URL_TEMPLATE

    assert "{mint}" in TERMINAL_TOKEN_URL_TEMPLATE
    assert fa._terminal_url(MINT) == _terminal_token_url(TERMINAL_TOKEN_URL_TEMPLATE, MINT)


def test_an_operator_can_remove_the_terminal_button_entirely() -> None:
    """An empty template is a deliberate off switch, not a broken config."""

    assert _terminal_token_url("", MINT) == ""
    assert "Open in Terminal" not in {
        item.label for item in bot_module._token_view(MINT, terminal_url="").children
    }


def test_a_configured_terminal_link_always_carries_the_exact_mint() -> None:
    url = _terminal_token_url("https://example.org/solana/{mint}", MINT)

    assert url == f"https://example.org/solana/{MINT}"
    assert MINT in url


def test_the_terminal_button_only_appears_when_a_link_exists() -> None:
    without = {item.label for item in bot_module._token_view(MINT).children}
    with_link = {
        item.label
        for item in bot_module._token_view(
            MINT, terminal_url=f"https://example.org/t/{MINT}"
        ).children
    }

    assert "Open in Terminal" not in without
    assert "Open in Terminal" in with_link


def test_nothing_in_this_release_scrapes_or_authenticates_against_terminal() -> None:
    """Section 20 and the release constraints: navigation only, no private access."""

    import pathlib

    import smart_money_bot

    root = pathlib.Path(smart_money_bot.__file__).parent
    # A navigation deep link built from the exact mint is what section 20 asks
    # for.  What is forbidden is everything that would make Terminal a *data
    # dependency*: a session, a credential, or a read of an authenticated page.
    # These are code shapes, not prose: several modules *state* in their
    # docstrings that they do not read cookies, and a bare substring search
    # would flag exactly the modules being careful.
    forbidden = (
        "cookies=",
        '"cookie"',
        "'cookie'",
        "set_cookie",
        "cookiejar",
        "aiohttp.cookiejar",
    )
    hits: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text().lower()
        hits.extend(f"{path.name}:{needle}" for needle in forbidden if needle in text)
    assert not hits, f"private-session access to a third-party terminal: {hits}"

    # And the only Terminal reference anywhere is a link derived from the mint.
    references = sorted(
        path.name for path in root.rglob("*.py") if "padre.gg" in path.read_text()
    )
    assert references == ["constants.py"], (
        "one URL template, in one place, so there is one thing to audit"
    )
    assert MINT in fa._terminal_url(MINT)


# ===========================================================================
# 32, 38. Safety labelling and real money
# ===========================================================================


def test_a_promotion_card_states_the_safety_it_does_not_know() -> None:
    """Section 32: an actionable research alert is allowed — if it says so."""

    alert = _promotion_alert()
    state = {field.name: field.value for field in alert.spec.fields}["STATE"]

    assert "Safety: **UNKNOWN**" in state
    assert "this is not a safety pass" in state
    assert "Entry eligible: **NO**" in state


def test_a_promotion_never_hands_out_a_buy_control() -> None:
    """Section 38: no automatic real money, and no one-click path to it either."""

    assert _promotion_alert().trade_eligible is False


def test_a_promotion_on_an_unverified_mint_loses_its_actionable_language() -> None:
    """Identity outranks evidence: no market case makes an unknown token a buy.

    v2.54 tightened both sides of this.  A verified identity used to buy the
    headline ``🚨 EARLY RUNNER — LOOK NOW`` — one answered question out of
    thirteen, printed as an instruction above a body that went on to admit
    unknown safety.  It is the exact headline in the operator's screenshot, so
    the verified card no longer instructs either; it names what it found.
    """

    verified = _promotion_alert()
    unverified = _promotion_alert(identity_verified=False)

    assert verified.spec.title == "🚨 EARLY MOVER — RESEARCH ONLY"
    assert "LOOK NOW" not in verified.spec.title
    assert "EARLY RUNNER" not in verified.spec.title
    assert "IDENTITY UNVERIFIED" in unverified.spec.title
    assert "LOOK NOW" not in unverified.spec.title
    assert unverified.ping is False
    assert unverified.lane == fa.LANE_RADAR


def test_the_promotion_card_carries_the_exact_mint() -> None:
    alert = _promotion_alert()

    assert alert.token_mint == MINT
    assert MINT in alert.spec.description
    assert OTHER not in alert.spec.description


def test_one_promotion_key_per_mint_backs_up_the_latch() -> None:
    assert _promotion_alert().alert_key == f"{fa.EARLY_PROMOTION}:{MINT}"


def _promotion_alert(**overrides):
    entry = _watch()
    decision = evaluate_promotion(
        entry,
        PromotionEvidence(
            now=NOW + 40,
            score=D("84"),
            buys=140,
            proven_independent_traders=2,
            known_money_flow="KNOWN_MONEY_ACCUMULATING",
        ),
    ).decision
    payload = {
        "mint": MINT,
        "name": "Grok Pocket",
        "symbol": "GPRO",
        "fomo_url": f"https://fomo.family/coin?address={MINT}",
        "decision": decision,
        "entry": entry,
        "current_market_cap_usd": D("78200"),
        "liquidity_usd": D("21090"),
        "buys": 140,
        "sells": 52,
        "safety_status": "UNKNOWN",
    }
    payload.update(overrides)
    return fa.build_promotion_alert(**payload)


def test_no_module_in_the_promotion_path_can_move_money() -> None:
    """Section 38, structurally: the strategy modules cannot reach a signer."""

    import pathlib

    for module in ("lab/promotion.py", "lab/toptraders.py"):
        text = (
            pathlib.Path(engine_module.__file__).parent / module
        ).read_text()
        for forbidden in ("Keypair", "send_transaction", "sign_transaction", "aiohttp"):
            assert forbidden not in text, f"{module} must stay signer- and provider-free"


# ===========================================================================
# 30, 36. The record, and whether any of it helps
# ===========================================================================


def test_the_baseline_survives_every_recheck() -> None:
    """Section 30: promotion is measured against the heads-up, so it must persist."""

    entry = _watch()
    for index in range(5):
        entry = evaluate_promotion(
            entry,
            PromotionEvidence(now=NOW + 30 * (index + 1), score=D("77"), buys=80 + index),
        ).entry

    assert entry.entry_score == D("76.00")
    assert entry.entry_buys == 78
    assert entry.entry_holder_count == 26
    assert entry.entry_why_not_pinged == ("NO_SERIOUS_EVIDENCE_CATEGORY",)
    assert entry.rechecks == 5


def test_a_watch_round_trips_through_its_persisted_form() -> None:
    entry = _watch()
    restored = entry_from_json(entry.to_json())

    assert restored == entry


def test_the_summary_counts_every_suppression_reason() -> None:
    stalled = evaluate_promotion(
        _watch(), PromotionEvidence(now=NOW + 30, score=D("77"))
    ).entry
    promoted = evaluate_promotion(
        _watch(),
        PromotionEvidence(
            now=NOW + 40, score=D("77"), proven_independent_traders=2,
            known_money_flow="KNOWN_MONEY_ACCUMULATING",
        ),
    ).entry
    status = summarise([stalled, promoted], now=NOW + 60)

    assert status.promoted == 1
    assert status.live == 1
    assert dict(status.suppression_counts)[WHY_NO_NEW_EVIDENCE] == 1


def test_every_suppression_reason_reads_as_a_sentence() -> None:
    """`view:whynotpinged` has to be answerable in English, not in constants."""

    entry = evaluate_promotion(
        _watch(), PromotionEvidence(now=NOW + 30, score=D("77"))
    ).entry

    assert entry.human_reason().startswith("Nothing new arrived")


def test_the_concentration_vocabulary_is_shared_across_packages() -> None:
    """A string compared across a package boundary is a bug waiting to happen.

    ``lab.promotion`` restates the holder modules' values rather than importing
    them, which keeps the package independent — and makes this test the thing
    that stops the two drifting apart and silently disabling the guard.
    """

    from smart_money_bot.lab.promotion import CONCENTRATION_WORSENING as PROMOTION
    from smart_money_bot.trenches import holders as trenches_holders
    from smart_money_bot.trending import holders as trending_holders

    assert PROMOTION == trenches_holders.CONCENTRATION_WORSENING
    assert PROMOTION == trending_holders.CONCENTRATION_WORSENING
    # And the guard actually fires on the value the engine really passes.
    outcome = evaluate_promotion(
        _watch(),
        PromotionEvidence(
            now=NOW + 120,
            score=D("77"),
            holder_count=94,
            holders_per_minute=D("34"),
            concentration_trend=trenches_holders.CONCENTRATION_WORSENING,
        ),
    )
    assert outcome.entry.suppression_reason == WHY_CONCENTRATION_WORSENING


def test_evidence_cohorts_are_assigned_from_what_was_known_at_entry() -> None:
    """Section 36: measure whether these additions actually improve results."""

    assert COHORT_NO_KNOWN_TRADER in assign_cohorts(proven_independent_traders=0)
    assert COHORT_ONE_KNOWN_TRADER in assign_cohorts(proven_independent_traders=1)
    assert COHORT_MULTI_KNOWN_TRADER in assign_cohorts(proven_independent_traders=4)
    assert COHORT_STORY_AND_TRADER in assign_cohorts(
        proven_independent_traders=2, story_confirmed=True
    )
    from smart_money_bot.lab.forward import COHORT_CONCENTRATION_IMPROVING

    assert COHORT_CONCENTRATION_IMPROVING in assign_cohorts(
        proven_independent_traders=1, concentration_trend="IMPROVING"
    )
    # A story with no known trader is already measured by the STORY family.
    assert COHORT_STORY_AND_TRADER not in assign_cohorts(
        proven_independent_traders=0, story_confirmed=True
    )
    # v2.45 adds the provider-label cohorts alongside the evidence ones.
    assert len(EVIDENCE_COHORTS) >= 10
    assert len(set(EVIDENCE_COHORTS)) == len(EVIDENCE_COHORTS)


def test_the_new_views_are_registered_without_taking_a_command_slot() -> None:
    """Discord allows 25 children per group and this product is at the ceiling."""

    from smart_money_bot.bot import SmartMoneyCommands

    assert len(SmartMoneyCommands.__cog_app_commands__) <= 25
    source = inspect.getsource(bot_module)
    assert '"whynotpinged"' in source
    assert '"traders"' in source


# ===========================================================================
# Persistence: the record has to survive to be an answer (sections 30, 37)
# ===========================================================================


@pytest.fixture
async def database(tmp_path):
    from smart_money_bot.database import Database

    db = Database(str(tmp_path / "promotion.db"), D("100"))
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


async def test_the_schema_is_idempotent(database) -> None:
    """A redeploy re-runs it; that must be a no-op, not a migration failure."""

    await database._init_schema()
    await database._init_schema()

    assert await database.early_watch_rows() == []


async def test_the_watch_baseline_cannot_be_overwritten_by_a_later_write(database) -> None:
    """Section 30: the baseline *is* the comparison, so an update must not touch it.

    This is the same write-once discipline the Trending ledger uses for
    first-observation fields, and it is enforced in the same two places: the
    UPDATE clause omits the protected columns, and the read path takes them from
    the columns rather than from the convenience payload the upsert rewrites.
    """

    entry = _watch()
    await database.save_early_watch(entry.to_json(), now=NOW)
    moved = evaluate_promotion(
        entry, PromotionEvidence(now=NOW + 60, score=D("81"), buys=90, holder_count=40)
    ).entry
    await database.save_early_watch(moved.to_json(), now=NOW + 60)

    # Now try to rewrite history through the same door enrichment uses.
    tampered = moved.to_json() | {
        "entry_score": "1",
        "entry_buys": 0,
        "entry_holder_count": 999,
        "entry_tier": "SOMETHING_ELSE",
    }
    await database.save_early_watch(tampered, now=NOW + 90)

    restored = entry_from_json(await database.early_watch_row(MINT))
    assert restored.entry_score == D("76.0")
    assert restored.entry_buys == 78
    assert restored.entry_holder_count == 26
    assert restored.entry_tier == TIER_EARLY_HEADS_UP
    # The mutable half really did move.
    assert restored.rechecks == 1
    assert restored.best_score == D("81.0")


async def test_only_open_watches_are_restored_after_a_redeploy(database) -> None:
    live = _watch()
    promoted = evaluate_promotion(
        live,
        PromotionEvidence(
            now=NOW + 40, score=D("77"), proven_independent_traders=2,
            known_money_flow="KNOWN_MONEY_ACCUMULATING",
        ),
    ).entry
    await database.save_early_watch(live.to_json(), now=NOW)
    await database.save_early_watch(
        {**promoted.to_json(), "mint": OTHER}, now=NOW + 40
    )

    open_rows = await database.early_watch_rows(open_only=True, now=NOW + 60)

    assert [row["mint"] for row in open_rows] == [MINT]
    # Both are still readable for the "why wasn't I pinged" record.
    assert len(await database.early_watch_rows()) == 2


async def test_trader_positions_are_stored_per_exact_mint(database) -> None:
    for mint in (MINT, OTHER):
        await database.record_token_trader(
            mint=mint,
            wallet="walletA",
            position={
                "buys": 2, "sells": 0, "bought_usd": "4200", "tokens_bought": "1000",
                "first_buy_at": NOW, "first_buy_market_cap_usd": "69000",
                "state": POS_ADDING,
            },
            cluster_id="funder:X",
            now=NOW,
        )

    rows = await database.token_trader_rows(MINT)

    assert [row["mint"] for row in rows] == [MINT]
    assert rows[0]["cluster_id"] == "funder:X"
    assert rows[0]["state"] == POS_ADDING


async def test_the_holder_series_reads_back_in_the_order_it_happened(database) -> None:
    for at, count in ((NOW, 26), (NOW + 60, 51), (NOW + 120, 94)):
        await database.record_holder_sample(mint=MINT, observed_at=at, holder_count=count)
    # A duplicate observation of the same second must not create a second point.
    await database.record_holder_sample(mint=MINT, observed_at=NOW, holder_count=999)

    samples = await database.holder_samples(MINT)

    assert [row["holder_count"] for row in samples] == [26, 51, 94]


async def test_the_bots_own_fills_are_read_by_mint_not_by_wallet(database) -> None:
    """A wallet's fills on a same-ticker token must not reach this token's board."""

    rows = await database.token_swap_rows(MINT)

    assert rows == []
    assert "token_mint = ?" in inspect.getsource(type(database).token_swap_rows)
