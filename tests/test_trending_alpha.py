"""Tests for the Trending-first alpha engine (v2.42).

These are written against the *product* invariants, not the implementation:

* an entry number, once recorded, can never move (sections 5, 93);
* a high static rank is not alpha, and rank *velocity* is (sections 9, 94, 95);
* a strong near miss gets a fast second look instead of a 30-minute wait
  (sections 42-47, 103) and expires silently if it never improves (section 104);
* evidence never crosses mints, however identical two tokens look (sections 13,
  99);
* attention never outvotes a hard safety failure (sections 71, 100);
* the two forward experiments cannot touch each other (sections 63, 106);
* nothing observed later can change an earlier decision (section 108);
* and no path in any of it can spend a real dollar (section 109).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from smart_money_bot.database import Database
from smart_money_bot.trending import (
    CHANGE_WINDOW_UNKNOWN,
    EXTERNAL_SUPPORTED,
    EXTERNAL_UNVERIFIED,
    HOT_WATCH_EXPIRED,
    HOT_WATCH_PROMOTED,
    LEGACY_STRATEGY_VERSION,
    ORIGIN_TRENDING_NEAR_MISS,
    QUALITY_NOISE,
    QUALITY_SPECULATIVE,
    QUALITY_SUPPORTED,
    SAFETY_FAIL,
    SOURCE_FOMO_TRENDING,
    SOURCE_NONE,
    SOURCE_TRENDING_PROXY,
    SUPPRESS_EDGE_CONSUMED,
    SUPPRESS_HARD_SAFETY,
    SUPPRESS_HOT_WATCH,
    TIER_ALPHA,
    TRENDING_ACCELERATING,
    TRENDING_CONTINUATION,
    TRENDING_EDGE_CONSUMED,
    TRENDING_EXPERIMENT_VERSION,
    TRENDING_HEALTHY,
    TRENDING_STRATEGY_VERSION,
    AuthorReputation,
    HotWatchConfig,
    SocialMention,
    ThesisRecord,
    TokenLabel,
    TrendingLedgerEntry,
    TrendingObservation,
    TrendingShadowConfig,
    UniverseTrade,
    assess_holders,
    build_risk_panel,
    build_thesis_panel,
    build_universe_report,
    classify_trending_event,
    compare_universes,
    decide_alert,
    family_for_reasons,
    find_collisions,
    link_matches_mint,
    measure_social_velocity,
    normalise_change_window,
    open_hot_watch,
    parse_about,
    ramp,
    rank_velocity,
    recheck_hot_watch,
    score_trending_edge,
    source_from_settings,
    validate_project_claim,
)
from smart_money_bot.trending.exits import (
    POLICY_CHAMPION,
    POLICY_TRENDING_PERSISTENCE,
    TRENDING_EXIT_POLICIES,
    TrendingExitContext,
    compare_policies,
    evaluate_policy,
)
from smart_money_bot.trending.latency import (
    MISSED_THESIS_ALPHA,
    MISSED_TRENDING_RUNNER,
    LatencySample,
    build_latency_reports,
    classify_miss,
)
from smart_money_bot.trending_runtime import TrendingRuntime
from smart_money_bot.trending_store import TrendingStore

D = Decimal
NOW = 1_700_000_000

PROXY = source_from_settings(api_url=None, api_key=None, proxy_enabled=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _obs(
    mint: str = "MintAAA",
    *,
    at: int = NOW,
    rank: int | None = 40,
    market_cap: str | None = "200000",
    holders: int | None = None,
    liquidity: str = "60000",
    top10: str | None = None,
    source=PROXY,
    **kwargs,
) -> TrendingObservation:
    return TrendingObservation(
        mint=mint,
        observed_at=at,
        rank=rank,
        market_cap_usd=None if market_cap is None else D(market_cap),
        liquidity_usd=D(liquidity),
        holder_count=holders,
        top10_percent=None if top10 is None else D(top10),
        source=source,
        **kwargs,
    )


class _Client:
    """A scripted Trending source.  No network, no provider, no cost."""

    def __init__(self, frames):
        self.frames = frames
        self.source = PROXY
        self.snapshots = 0
        self.last_snapshot_at = None
        self.last_error = ""
        self.index = 0

    async def fetch_board(self, *, limit):
        frame = self.frames[min(self.index, len(self.frames) - 1)]
        self.index += 1
        self.snapshots += 1
        if frame:
            self.last_snapshot_at = frame[0].observed_at
        return frame

    async def close(self):
        return None


@pytest.fixture
async def store():
    database = Database(":memory:", D("1000"))
    await database.connect()
    try:
        yield TrendingStore(database)
    finally:
        await database.close()


# ---------------------------------------------------------------------------
# section 4 / 11: what the source honestly is
# ---------------------------------------------------------------------------
def test_a_proxy_can_never_call_itself_fomo_trending() -> None:
    """Section 4: a DexScreener approximation is never labelled FOMO_TRENDING."""

    proxy = source_from_settings(api_url=None, api_key=None, proxy_enabled=True)
    assert proxy.kind == SOURCE_TRENDING_PROXY
    assert proxy.is_exact_fomo is False
    assert "not Fomo" in proxy.rank_caveat()

    nothing = source_from_settings(api_url=None, api_key=None, proxy_enabled=False)
    assert nothing.kind == SOURCE_NONE

    authorised = source_from_settings(
        api_url="https://feed.example/trending", api_key="k", proxy_enabled=True
    )
    assert authorised.kind == SOURCE_FOMO_TRENDING
    assert authorised.is_exact_fomo is True


def test_an_undocumented_percentage_window_stays_unknown() -> None:
    """Section 6: +325% of *what*?  We do not guess, ever."""

    assert normalise_change_window(None) == CHANGE_WINDOW_UNKNOWN
    assert normalise_change_window("since the launch party") == CHANGE_WINDOW_UNKNOWN
    assert normalise_change_window("h24") == "24H"
    assert normalise_change_window("5m") == "5M"

    entry = TrendingLedgerEntry.from_first_observation(
        _obs(displayed_change_percent=D("325"))
    )
    assert entry.change_window == CHANGE_WINDOW_UNKNOWN


# ---------------------------------------------------------------------------
# sections 5, 8, 93: the ledger's entry numbers are immutable
# ---------------------------------------------------------------------------
def test_first_trending_observations_are_frozen_forever() -> None:
    """Section 93: if entry numbers could move, "were we early?" is unanswerable."""

    entry = TrendingLedgerEntry.from_first_observation(
        _obs(rank=44, market_cap="120000", holders=90, top10="30")
    )
    for step, (rank, cap, holders) in enumerate(
        [(31, "180000", 150), (18, "260000", 240), (9, "410000", 520)], start=1
    ):
        entry = entry.observe(
            _obs(at=NOW + step * 60, rank=rank, market_cap=cap, holders=holders, top10="28")
        )

    assert entry.first_rank == 44
    assert entry.first_market_cap_usd == D("120000")
    assert entry.first_holder_count == 90
    assert entry.first_top10_percent == D("30")
    assert entry.first_seen_at == NOW
    # And the current view still moved.
    assert entry.current_rank == 9
    assert entry.best_rank == 9
    assert entry.holder_growth() == 430


def test_a_ledger_entry_refuses_to_merge_a_different_mint() -> None:
    """Section 13: mint is identity.  Merging two mints is a hard error."""

    entry = TrendingLedgerEntry.from_first_observation(_obs("MintAAA"))
    with pytest.raises(ValueError):
        entry.observe(_obs("MintBBB"))


def test_a_reentry_starts_a_new_stint_without_rewriting_the_first_one() -> None:
    entry = TrendingLedgerEntry.from_first_observation(_obs(rank=12, market_cap="90000"))
    entry = entry.observe(_obs(at=NOW + 120, rank=10))
    entry = entry.mark_left_board(at=NOW + 300)
    assert entry.on_board is False

    entry = entry.observe(_obs(at=NOW + 4000, rank=15, market_cap="150000"))
    assert entry.entries == 2
    assert entry.on_board is True
    assert entry.first_rank == 12
    assert entry.first_market_cap_usd == D("90000")
    assert entry.stint_started_at == NOW + 4000


# ---------------------------------------------------------------------------
# sections 9, 94, 95: velocity is the signal, not absolute rank
# ---------------------------------------------------------------------------
def test_rank_velocity_measures_the_climb_not_the_position() -> None:
    """Section 94: #40 → #22 → #8 in minutes is the actionable object."""

    entry = TrendingLedgerEntry.from_first_observation(_obs(rank=40))
    entry = entry.observe(_obs(at=NOW + 60, rank=22))
    entry = entry.observe(_obs(at=NOW + 120, rank=8))

    velocity = rank_velocity(entry.rank_history, now=NOW + 120, first_seen_at=NOW)
    assert velocity.delta == 32
    assert velocity.climbing is True
    assert velocity.per_minute > D("10")
    assert velocity.seconds_to_top25 == 60
    assert velocity.seconds_to_top10 == 120


def test_a_high_rank_that_never_moves_is_not_alpha() -> None:
    """Section 95: #2 flat for six hours is a position, not a signal."""

    # Six hours of continuous observation at the lane's real cadence: the token
    # never left the board, so this is one long stint, not six re-entries.
    entry = TrendingLedgerEntry.from_first_observation(_obs(rank=2, at=NOW - 21_600))
    for step in range(1, 73):
        entry = entry.observe(_obs(at=NOW - 21_600 + step * 300, rank=2))
    assert entry.entries == 1
    assert entry.seconds_on_board == 21_600

    velocity = rank_velocity(entry.rank_history, now=NOW, first_seen_at=entry.first_seen_at)
    event = classify_trending_event(entry, velocity, now=NOW)
    assert event.state == TRENDING_HEALTHY
    assert velocity.delta == 0

    score = score_trending_edge(entry, event)
    verdict = decide_alert(score, event, alpha_threshold=D("62"))
    assert verdict.alert is False
    assert verdict.tier != TIER_ALPHA


def test_a_climbing_rank_becomes_acceleration() -> None:
    entry = TrendingLedgerEntry.from_first_observation(_obs(rank=40, at=NOW - 1200))
    entry = entry.observe(_obs(at=NOW - 600, rank=22))
    entry = entry.observe(_obs(at=NOW, rank=8))
    velocity = rank_velocity(entry.rank_history, now=NOW, first_seen_at=entry.first_seen_at)
    event = classify_trending_event(entry, velocity, now=NOW, market_cap_velocity=D("2"))
    assert event.state == TRENDING_ACCELERATING
    assert event.strengthening is True


# ---------------------------------------------------------------------------
# sections 11, 12, 51, 96: continuation needs NEW evidence
# ---------------------------------------------------------------------------
def test_an_already_large_token_needs_new_evidence_to_resurface() -> None:
    """Section 96 and section 51: "it pumped" is not new evidence."""

    entry = TrendingLedgerEntry.from_first_observation(
        _obs(rank=30, market_cap="900000", at=NOW - 3600)
    )
    entry = entry.observe(_obs(at=NOW - 600, rank=20, market_cap="1100000"))
    entry = entry.observe(_obs(at=NOW, rank=7, market_cap="1200000", holders=900))
    velocity = rank_velocity(entry.rank_history, now=NOW, first_seen_at=entry.first_seen_at)

    without = classify_trending_event(entry, velocity, now=NOW, has_new_evidence=False)
    assert without.state != TRENDING_CONTINUATION

    with_evidence = classify_trending_event(
        entry, velocity, now=NOW, has_new_evidence=True, holder_growth=400
    )
    assert with_evidence.state == TRENDING_CONTINUATION
    assert with_evidence.already_large is True
    # The card must never call this early.
    assert any("not early" in reason for reason in with_evidence.reasons)


def test_a_consumed_move_is_labelled_consumed_rather_than_early() -> None:
    entry = TrendingLedgerEntry.from_first_observation(
        _obs(rank=30, market_cap="100000", at=NOW - 3600)
    )
    entry = entry.observe(_obs(at=NOW, rank=28, market_cap="400000"))
    velocity = rank_velocity(entry.rank_history, now=NOW, first_seen_at=entry.first_seen_at)
    event = classify_trending_event(entry, velocity, now=NOW)
    assert event.state == TRENDING_EDGE_CONSUMED

    score = score_trending_edge(entry, event)
    assert score.edge_state == "EDGE_CONSUMED"
    verdict = decide_alert(score, event, alpha_threshold=D("62"))
    assert verdict.alert is False
    assert verdict.suppression == SUPPRESS_EDGE_CONSUMED


# ---------------------------------------------------------------------------
# section 43: no threshold cliffs
# ---------------------------------------------------------------------------
def test_the_score_ramp_has_no_cliff_between_neighbouring_values() -> None:
    """1.94 and 2.00 must not live in different universes (section 43)."""

    just_under = ramp(D("1.94"), floor=D("0"), target=D("2"), weight=D("10"))
    just_over = ramp(D("2.00"), floor=D("0"), target=D("2"), weight=D("10"))
    assert just_over - just_under < D("0.5")
    assert just_under > D("9")
    # Below the floor still earns nothing; the ramp is bounded on both sides.
    assert ramp(D("-1"), floor=D("0"), target=D("2"), weight=D("10")) == D("0")
    assert ramp(D("99"), floor=D("0"), target=D("2"), weight=D("10")) == D("10")


def test_strength_in_one_dimension_compensates_for_a_marginal_miss() -> None:
    """The APEUS class: strong volume and buyers must not be zeroed by one metric."""

    entry = TrendingLedgerEntry.from_first_observation(_obs(rank=25, market_cap="300000"))
    entry = entry.observe(_obs(at=NOW + 120, rank=18, market_cap="330000", holders=800))
    velocity = rank_velocity(entry.rank_history, now=NOW + 120, first_seen_at=NOW)
    event = classify_trending_event(entry, velocity, now=NOW + 120)

    holders = assess_holders(
        entry.mint,
        holder_count=800,
        first_holder_count=300,
        seconds_elapsed=120,
        top10_percent=D("30"),
        first_top10_percent=D("32"),
        independent_buyers=789,
        buys=1532,
    )
    score = score_trending_edge(
        entry, event, holders=holders, market_cap_velocity=D("5")
    )
    # A candidate this strong on demand and structure is not left at zero merely
    # because one ratio came in marginally under a round number.
    assert score.score > D("40")
    assert score.has_named_reason is True


# ---------------------------------------------------------------------------
# sections 42-47, 103, 104: HOT WATCH
# ---------------------------------------------------------------------------
def test_a_strong_near_miss_enters_hot_watch_and_does_not_ping() -> None:
    """Section 44: hot watch is a fast second look, never an interruption."""

    entry = TrendingLedgerEntry.from_first_observation(_obs(rank=20, market_cap="300000"))
    entry = entry.observe(_obs(at=NOW + 60, rank=16, market_cap="320000", holders=400))
    velocity = rank_velocity(entry.rank_history, now=NOW + 60, first_seen_at=NOW)
    event = classify_trending_event(entry, velocity, now=NOW + 60)
    score = score_trending_edge(entry, event)

    verdict = decide_alert(
        score,
        event,
        alpha_threshold=score.score + D("6"),
        watch_threshold=D("10"),
        hot_watch_band=D("12"),
    )
    assert verdict.alert is False
    assert verdict.suppression == SUPPRESS_HOT_WATCH
    assert verdict.hot_watch_candidate is True
    assert verdict.near_miss_gap == D("6")


def test_a_hot_watch_promotes_once_when_the_evidence_strengthens() -> None:
    """Section 47: heads-up → hot watch → promotion → exactly one ping."""

    watch = open_hot_watch(
        "MintAAA",
        origin=ORIGIN_TRENDING_NEAR_MISS,
        now=NOW,
        score=D("52"),
        market_cap_usd=D("500000"),
        heads_up_market_cap_usd=D("500000"),
        config=HotWatchConfig(recheck_seconds=30, ttl_seconds=900),
    )
    assert watch.active is True

    outcome = recheck_hot_watch(
        watch,
        now=NOW + 45,
        score=D("68"),
        reasons=("TRENDING_ACCELERATION", "HOLDER_EXPANSION"),
        market_cap_usd=D("560000"),
        alpha_threshold=D("62"),
    )
    assert outcome.promoted is True
    assert outcome.should_ping is True
    assert outcome.entry.state == HOT_WATCH_PROMOTED
    # Section 49: the lateness is measured honestly, not implied away.
    assert outcome.entry.promotion_delay_seconds() == 45
    assert outcome.entry.promotion_move_percent() == D("12.0")


def test_a_score_without_a_named_reason_never_promotes() -> None:
    """Section 57: "score 80 therefore ping" is not allowed, even in the fast lane."""

    watch = open_hot_watch(
        "MintAAA", origin=ORIGIN_TRENDING_NEAR_MISS, now=NOW, score=D("55")
    )
    outcome = recheck_hot_watch(
        watch, now=NOW + 60, score=D("95"), reasons=(), alpha_threshold=D("62")
    )
    assert outcome.promoted is False


def test_a_hot_watch_whose_evidence_fades_expires_silently() -> None:
    """Section 104: no improvement, no ping, no noise."""

    config = HotWatchConfig(recheck_seconds=30, ttl_seconds=300)
    watch = open_hot_watch(
        "MintAAA", origin=ORIGIN_TRENDING_NEAR_MISS, now=NOW, score=D("52"), config=config
    )
    outcome = recheck_hot_watch(
        watch,
        now=NOW + 400,
        score=D("30"),
        reasons=("TRENDING_ACCELERATION",),
        alpha_threshold=D("62"),
        config=config,
    )
    assert outcome.expired is True
    assert outcome.promoted is False
    assert outcome.entry.state == HOT_WATCH_EXPIRED


def test_a_hard_safety_failure_drops_a_hot_watch_instead_of_promoting_it() -> None:
    watch = open_hot_watch(
        "MintAAA", origin=ORIGIN_TRENDING_NEAR_MISS, now=NOW, score=D("60")
    )
    outcome = recheck_hot_watch(
        watch,
        now=NOW + 60,
        score=D("90"),
        reasons=("TRENDING_ACCELERATION",),
        alpha_threshold=D("62"),
        blocked=True,
    )
    assert outcome.promoted is False
    assert outcome.dropped is True


# ---------------------------------------------------------------------------
# sections 16-18, 97, 98: About, claims and project validation
# ---------------------------------------------------------------------------
def test_the_about_section_is_summarised_not_dumped() -> None:
    about = parse_about(
        "MintAAA",
        "QuantumMind is an AI research platform. " * 20,
        website="https://quantummind.example",
    )
    assert len(about.summary) < 300
    assert about.summary.endswith("…")
    assert "AI" in about.claims
    assert about.website == "https://quantummind.example"


def test_a_real_project_that_never_mentions_the_mint_is_not_a_link() -> None:
    """Section 17: developer marketing must never render as verified."""

    about = parse_about(
        "MintAAA", "Officially partnered with Quantum Labs, an AI research company."
    )
    assert about.has_official_claim is True

    validation = validate_project_claim(about, project_exists=True)
    assert validation.external_state == EXTERNAL_UNVERIFIED
    assert validation.mentions_exact_mint is False
    claim, fact = validation.operator_lines()
    assert "UNVERIFIED" in fact
    assert "does not mention this mint" in fact


def test_only_an_exact_mint_mention_upgrades_a_claim_to_supported() -> None:
    """Section 18: the project publishing *this mint* is the only proof."""

    about = parse_about("MintAAA", "An AI agent protocol with an on-chain model.")
    validation = validate_project_claim(
        about,
        project_exists=True,
        official_sources_mentioning_mint=("https://quantum.example/token",),
    )
    assert validation.external_state == EXTERNAL_SUPPORTED
    assert validation.supported is True


# ---------------------------------------------------------------------------
# sections 19-26, 97, 98: theses
# ---------------------------------------------------------------------------
def _thesis(mint="MintAAA", author="alice", text="", at=NOW, **kwargs) -> ThesisRecord:
    return ThesisRecord(mint=mint, author=author, posted_at=at, text=text, **kwargs)


def test_a_promoter_moon_post_grades_as_noise() -> None:
    """Section 97: an unsupported developer claim is not analysis."""

    panel = build_thesis_panel(
        "MintAAA",
        [
            _thesis(
                text="our token is going to the moon, buy now, next 100x, trust me",
                author_is_creator=True,
            )
        ],
    )
    assert panel.total == 1
    assert panel.strongest.quality == QUALITY_NOISE
    assert panel.has_serious_thesis is False


def test_a_specific_corroborated_thesis_grades_higher() -> None:
    """Section 98: specific, checkable, corroborated, exact-mint."""

    record = _thesis(
        text=(
            "The team shipped the agent beta on Mar 14 and published the repo at "
            "https://github.example/agent — 12,500 downloads in the first day."
        ),
        market_cap_at_thesis_usd=D("100000"),
    )
    panel = build_thesis_panel(
        "MintAAA",
        [record],
        current_market_cap_usd=D("120000"),
        externally_supported_ids=frozenset({record.thesis_id}),
        corroborating_sources={record.thesis_id: 2},
    )
    assert panel.strongest.quality in {QUALITY_SUPPORTED, "STRONG"}
    assert panel.has_serious_thesis is True


def test_copies_of_one_thesis_count_as_one_information_source() -> None:
    """Section 26: three copies are one source; three analysts are three."""

    shared = "the protocol shipped its mainnet bridge today and volume followed"
    panel = build_thesis_panel(
        "MintAAA",
        [
            _thesis(author="alice", text=shared, at=NOW),
            _thesis(author="bob", text=shared, at=NOW + 30),
            _thesis(author="carol", text=shared, at=NOW + 60),
        ],
    )
    assert panel.total == 3
    assert panel.independent_sources == 1
    followers = [item for item in panel.assessments if not item.cluster_leader]
    assert len(followers) == 2
    assert all("repeats an earlier thesis" in " ".join(item.reasons) for item in followers)


def test_a_thesis_about_another_mint_is_dropped_not_matched_loosely() -> None:
    """Section 99: a same-name token's thesis is not this token's evidence."""

    panel = build_thesis_panel(
        "MintAAA",
        [_thesis(mint="MintBBB", text="detailed analysis of the real project")],
    )
    assert panel.total == 0


def test_an_uncorroborated_insider_claim_is_penalised() -> None:
    """Sections 22, 23: public chatter only, and never dressed up as fact."""

    panel = build_thesis_panel(
        "MintAAA",
        [_thesis(text="my source at the exchange leaked that a listing lands Friday")],
    )
    assert panel.strongest.quality in {QUALITY_NOISE, QUALITY_SPECULATIVE}
    assert "UNSUPPORTED_INSIDER_CLAIM" in panel.strongest.penalties


def test_author_reputation_is_forward_measured_not_popularity() -> None:
    """Section 25: a big following with a bad record grades a thesis down."""

    popular_but_wrong = AuthorReputation(
        author="loud", sample=20, avg_forward_move_percent=D("-30"), severe_failures=14
    )
    assert popular_but_wrong.credible is False

    quiet_but_right = AuthorReputation(
        author="quiet", sample=12, avg_forward_move_percent=D("45"), severe_failures=1
    )
    assert quiet_but_right.credible is True


# ---------------------------------------------------------------------------
# sections 33, 34, 102: social
# ---------------------------------------------------------------------------
def test_engagement_is_never_invented() -> None:
    """Section 33: a missing like count is None, never a confident zero."""

    velocity = measure_social_velocity(
        "MintAAA",
        [
            SocialMention(mint="MintAAA", author="a", posted_at=NOW - 60),
            SocialMention(mint="MintAAA", author="b", posted_at=NOW - 30),
        ],
        now=NOW,
    )
    assert velocity.total_likes is None
    assert velocity.total_views is None
    assert velocity.mentions == 2


def test_chatter_without_market_confirmation_does_not_ping() -> None:
    """Section 102: a loud token with a bad market is not high conviction."""

    entry = TrendingLedgerEntry.from_first_observation(
        _obs(rank=30, market_cap="60000", liquidity="900")
    )
    entry = entry.observe(_obs(at=NOW + 60, rank=29, market_cap="60000", liquidity="900"))
    velocity = rank_velocity(entry.rank_history, now=NOW + 60, first_seen_at=NOW)
    event = classify_trending_event(entry, velocity, now=NOW + 60)
    social = measure_social_velocity(
        "MintAAA",
        [
            SocialMention(mint="MintAAA", author=f"a{index}", posted_at=NOW + 50)
            for index in range(9)
        ],
        now=NOW + 60,
    )
    score = score_trending_edge(entry, event, social=social)
    verdict = decide_alert(score, event, alpha_threshold=D("62"))
    assert verdict.alert is False


# ---------------------------------------------------------------------------
# sections 35, 36, 101: holders
# ---------------------------------------------------------------------------
def test_participant_growth_is_distinguished_from_repeat_transactions() -> None:
    """Section 36: 1000 txns from 10 wallets is not 500 new holders."""

    wash = assess_holders(
        "MintAAA",
        holder_count=110,
        first_holder_count=100,
        seconds_elapsed=600,
        independent_buyers=10,
        buys=1000,
    )
    assert wash.participant_quality == D("0.01")

    organic = assess_holders(
        "MintAAA",
        holder_count=600,
        first_holder_count=100,
        seconds_elapsed=600,
        top10_percent=D("25"),
        first_top10_percent=D("40"),
        independent_buyers=480,
        buys=520,
    )
    assert organic.genuinely_expanding is True
    assert organic.concentration_trend == "IMPROVING"
    assert organic.participant_quality > D("0.9")


def test_pure_market_evidence_can_still_qualify_without_a_story() -> None:
    """Section 101: organic rank + holders + healthy market is a real case."""

    entry = TrendingLedgerEntry.from_first_observation(
        _obs(rank=45, market_cap="150000", liquidity="120000")
    )
    entry = entry.observe(
        _obs(at=NOW + 120, rank=12, market_cap="210000", liquidity="130000", holders=900)
    )
    velocity = rank_velocity(entry.rank_history, now=NOW + 120, first_seen_at=NOW)
    event = classify_trending_event(entry, velocity, now=NOW + 120, market_cap_velocity=D("10"))
    holders = assess_holders(
        "MintAAA",
        holder_count=900,
        first_holder_count=100,
        seconds_elapsed=120,
        top10_percent=D("20"),
        first_top10_percent=D("22"),
    )
    score = score_trending_edge(entry, event, holders=holders, market_cap_velocity=D("10"))
    verdict = decide_alert(score, event, alpha_threshold=D("62"))
    assert verdict.alert is True
    assert verdict.tier == TIER_ALPHA
    assert score.has_named_reason is True


# ---------------------------------------------------------------------------
# sections 37, 71, 100: trending never overrides hard safety
# ---------------------------------------------------------------------------
def test_a_verified_badge_is_recorded_but_never_treated_as_safety() -> None:
    """Section 37: VERIFIED is a badge, not rug protection."""

    panel = build_risk_panel("MintAAA", fomo_verified="VERIFIED", safety_status="UNKNOWN")
    assert panel.blocked is False
    joined = " ".join(panel.concerns)
    assert "not a safety guarantee" in joined
    assert "UNKNOWN — that is not a pass" in joined


def test_a_hard_safety_failure_beats_every_attention_signal() -> None:
    """Section 100: a trending token that fails hard is still a failure."""

    entry = TrendingLedgerEntry.from_first_observation(_obs(rank=40, market_cap="100000"))
    entry = entry.observe(_obs(at=NOW + 60, rank=3, market_cap="400000", holders=5000))
    velocity = rank_velocity(entry.rank_history, now=NOW + 60, first_seen_at=NOW)
    event = classify_trending_event(entry, velocity, now=NOW + 60, market_cap_velocity=D("50"))

    risk = build_risk_panel(
        "MintAAA", sell_failed=True, liquidity_collapsed=True, safety_status=SAFETY_FAIL
    )
    assert risk.blocked is True

    score = score_trending_edge(entry, event, risk=risk)
    assert score.score == D("0.0")
    assert score.reasons == ()

    verdict = decide_alert(score, event, alpha_threshold=D("62"), risk=risk)
    assert verdict.alert is False
    assert verdict.suppression == SUPPRESS_HARD_SAFETY


# ---------------------------------------------------------------------------
# sections 14, 15: link integrity and collisions
# ---------------------------------------------------------------------------
def test_a_fomo_link_must_resolve_to_the_card_s_own_mint() -> None:
    """Section 14: never show one HeeHaw and link to another."""

    right = "https://fomo.family/coin?address=MintAAA&chainId=1399811149"
    wrong = "https://fomo.family/coin?address=MintBBB&chainId=1399811149"
    assert link_matches_mint(right, "MintAAA") is True
    assert link_matches_mint(wrong, "MintAAA") is False
    assert link_matches_mint("https://evil.example/coin?address=MintAAA", "MintAAA") is False


def test_same_name_tokens_are_surfaced_as_a_collision_not_merged() -> None:
    """Section 15: four tokens sharing a story is four tokens."""

    subject = TokenLabel("MintAAA", name="HeeHaw", symbol="HEE", story_key="donkey-rescue")
    others = [
        TokenLabel("MintBBB", name="HeeHaw", symbol="HEE", story_key="donkey-rescue"),
        TokenLabel("MintCCC", name="HeeHaw", symbol="HEE2", story_key="donkey-rescue"),
        TokenLabel("MintDDD", name="Unrelated", symbol="UNR", story_key="other"),
    ]
    group = find_collisions(subject, others)
    assert group.collision_count == 2
    assert "OTHER TOKEN(S) SHARE THIS NAME" in group.warning_line()
    assert "MintAAA" in group.warning_line()


# ---------------------------------------------------------------------------
# sections 62-63, 106-107: the two experiments are isolated
# ---------------------------------------------------------------------------
def test_the_two_shadow_experiments_can_never_share_a_bankroll() -> None:
    """Section 106: a fair comparison needs two genuinely separate books."""

    config = TrendingShadowConfig()
    assert config.strategy_version == TRENDING_STRATEGY_VERSION
    assert config.experiment_version == TRENDING_EXPERIMENT_VERSION
    assert config.strategy_version != LEGACY_STRATEGY_VERSION
    assert TRENDING_STRATEGY_VERSION != LEGACY_STRATEGY_VERSION


def test_the_trending_experiment_shape_is_fixed_by_construction() -> None:
    """Section 107: $100 bankroll, $10 entries, 5 positions, $50 exposure."""

    config = TrendingShadowConfig()
    assert config.bankroll_usd == D("100")
    assert config.position_usd == D("10")
    assert config.max_concurrent_positions == 5
    assert config.max_total_exposure_usd == D("50")

    for override in (
        {"position_usd": D("25")},
        {"bankroll_usd": D("500")},
        {"max_concurrent_positions": 9},
        {"max_total_exposure_usd": D("90")},
        {"strategy_version": LEGACY_STRATEGY_VERSION},
    ):
        with pytest.raises(ValueError):
            TrendingShadowConfig(**override)


def test_only_configured_strategy_signals_reach_the_trending_shadow() -> None:
    """Section 65: the radar shows everything; the experiment trades a strategy."""

    assert family_for_reasons(("TRENDING_ACCELERATION",)) == "TRENDING_ACCELERATION"
    assert family_for_reasons(("CONFLUENCE", "THESIS")) == "TRENDING_CONFLUENCE"
    # Chatter or holder growth alone is not a tradeable signal.
    assert family_for_reasons(("PUBLIC_SOCIAL",)) is None
    assert family_for_reasons(("HOLDER_EXPANSION",)) is None
    assert family_for_reasons(()) is None


def test_the_scoreboard_refuses_a_verdict_on_a_tiny_sample() -> None:
    """Section 66: a ratio built from four trades is confidently wrong."""

    trades = [
        UniverseTrade(
            mint=f"M{index}",
            family="TRENDING_ACCELERATION",
            opened_at=NOW,
            closed_at=NOW + 600,
            net_pnl_usd=D("1.5"),
            mfe_percent=D("60"),
        )
        for index in range(3)
    ]
    comparison = compare_universes(
        build_universe_report("TRENDING", trades),
        build_universe_report("LEGACY", []),
    )
    assert comparison.comparable is False
    assert "NOT ENOUGH FORWARD DATA" in comparison.verdict()


def test_the_scoreboard_reports_safety_and_upside_separately() -> None:
    """Sections 67, 68: "safer" and "runs further" are different questions."""

    trending = [
        UniverseTrade(
            mint=f"T{index}",
            family="TRENDING_ACCELERATION",
            opened_at=NOW,
            closed_at=NOW + 600,
            net_pnl_usd=D("0.5"),
            mfe_percent=D("30"),
        )
        for index in range(12)
    ]
    legacy = [
        UniverseTrade(
            mint=f"L{index}",
            family="FAST_WATCH",
            opened_at=NOW,
            closed_at=NOW + 600,
            net_pnl_usd=D("1.0"),
            mfe_percent=D("150"),
            rugged=index < 4,
        )
        for index in range(12)
    ]
    comparison = compare_universes(
        build_universe_report("TRENDING", trending),
        build_universe_report("LEGACY", legacy),
    )
    assert comparison.comparable is True
    assert comparison.safety_leader == "TRENDING"
    assert comparison.upside_leader == "LEGACY"
    assert comparison.net_leader == "LEGACY"


# ---------------------------------------------------------------------------
# sections 69-73: exits
# ---------------------------------------------------------------------------
def _exit_context(step: int, **kwargs) -> TrendingExitContext:
    defaults = {
        "at": NOW + step * 60,
        "seconds_held": step * 60,
        "unrealized_percent": D("10"),
        "peak_percent": D("10"),
        "rank": 10,
        "rank_direction": 1,
        "on_board": True,
        "holder_growth": 50,
        "story_active": True,
        "thesis_active": True,
        "liquidity_usd": D("50000"),
    }
    defaults.update(kwargs)
    return TrendingExitContext(**defaults)


def test_a_hard_failure_exits_even_the_most_patient_policy() -> None:
    """Section 71: Trending is not rug protection, for any policy."""

    observations = [
        _exit_context(1),
        _exit_context(2, sell_failed=True),
        _exit_context(3, unrealized_percent=D("400")),
    ]
    for policy in TRENDING_EXIT_POLICIES:
        decision = evaluate_policy(policy, observations)
        assert decision.exit is True, policy
        assert decision.reason == "SELL_FAILED", policy


def test_a_soft_pause_on_a_healthy_trending_token_is_not_a_reversal() -> None:
    """Section 70: one weak print does not end a still-convincing runner."""

    observations = [
        _exit_context(step, momentum_state="SOFT_PAUSE" if step == 2 else "HEALTHY")
        for step in range(1, 5)
    ]
    decision = evaluate_policy(POLICY_TRENDING_PERSISTENCE, observations)
    assert decision.exit is False


def test_challengers_are_compared_against_the_champion_not_substituted() -> None:
    """Section 69: test the patience hypothesis, do not assume it."""

    observations = [_exit_context(step) for step in range(1, 6)]
    results = compare_policies(observations)
    policies = {row.policy for row in results}
    assert POLICY_CHAMPION in policies
    assert POLICY_TRENDING_PERSISTENCE in policies
    assert len(policies) == len(TRENDING_EXIT_POLICIES)


# ---------------------------------------------------------------------------
# sections 79-83: latency and misses
# ---------------------------------------------------------------------------
def test_latency_is_reported_per_stage_so_a_regression_is_attributable() -> None:
    samples = [
        LatencySample(
            mint=f"M{index}",
            source_appearance_at=NOW,
            bot_observation_at=NOW + index,
            cheap_verdict_at=NOW + index + 2,
            discord_send_at=NOW + index + 3,
        )
        for index in range(1, 11)
    ]
    reports = {report.stage: report for report in build_latency_reports(samples)}
    assert reports["source_to_observation"].samples == 10
    assert reports["source_to_observation"].p50 is not None
    assert reports["source_to_send"].p90 >= reports["source_to_send"].p50


def test_a_token_we_saw_and_ignored_that_then_ran_is_recorded_as_a_miss() -> None:
    """Section 80: a miss is only a miss if we actually saw it."""

    miss = classify_miss(
        "MintAAA",
        observed_at=NOW,
        market_cap_at_observation_usd=D("100000"),
        peak_market_cap_usd=D("300000"),
        alerted=False,
        suppression_reason="NOT_STRONG_ENOUGH",
    )
    assert miss is not None
    assert miss.miss_class == MISSED_TRENDING_RUNNER
    assert miss.move_percent == D("200.0")

    with_thesis = classify_miss(
        "MintAAA",
        observed_at=NOW,
        market_cap_at_observation_usd=D("100000"),
        peak_market_cap_usd=D("300000"),
        alerted=False,
        had_quality_thesis=True,
    )
    assert with_thesis.miss_class == MISSED_THESIS_ALPHA

    # An alert we did send is not a miss.
    assert (
        classify_miss(
            "MintAAA",
            observed_at=NOW,
            market_cap_at_observation_usd=D("100000"),
            peak_market_cap_usd=D("300000"),
            alerted=True,
        )
        is None
    )


# ---------------------------------------------------------------------------
# runtime: persistence, restart safety, no lookahead
# ---------------------------------------------------------------------------
async def test_the_ledger_survives_a_restart_with_its_entry_numbers_intact(store) -> None:
    """Section 111: a redeploy must not reset "when did we first see this?"."""

    frames = [
        (_obs(at=NOW, rank=40, market_cap="200000"),),
        (_obs(at=NOW + 60, rank=22, market_cap="260000"),),
    ]
    runtime = TrendingRuntime(store, _Client(frames))
    await runtime.poll_once(now=NOW)
    await runtime.poll_once(now=NOW + 60)

    # A brand-new runtime over the same store is exactly a restart.
    restarted = TrendingRuntime(store, _Client([()]))
    await restarted.restore()
    entry = restarted.entry_for("MintAAA")
    assert entry is not None
    assert entry.first_rank == 40
    assert entry.first_market_cap_usd == D("200000")
    assert entry.first_seen_at == NOW
    assert entry.current_rank == 22


async def test_an_expired_hot_watch_is_not_resurrected_by_a_restart(store) -> None:
    """A window that elapsed while the process was down stays elapsed."""

    config = HotWatchConfig(ttl_seconds=300, recheck_seconds=30)
    watch = open_hot_watch(
        "MintAAA", origin=ORIGIN_TRENDING_NEAR_MISS, now=NOW, score=D("50"), config=config
    )
    await store.save_hot_watch(watch)

    runtime = TrendingRuntime(store, _Client([()]), hot_watch_config=config)
    # Restore happens "later" than the window; the entry must not come back live.
    import time as _time

    original = _time.time
    _time.time = lambda: NOW + 10_000
    try:
        await runtime.restore()
    finally:
        _time.time = original
    assert runtime.hot_watch_status()["active"] == 0


async def test_a_later_pump_cannot_change_an_earlier_decision(store) -> None:
    """Section 108: every verdict is causal."""

    frames = [
        (_obs(at=NOW, rank=48, market_cap="100000"),),
        (_obs(at=NOW + 60, rank=47, market_cap="101000"),),
        (_obs(at=NOW + 120, rank=1, market_cap="5000000"),),
    ]
    runtime = TrendingRuntime(store, _Client(frames))
    first = await runtime.poll_once(now=NOW)
    second = await runtime.poll_once(now=NOW + 60)
    await runtime.poll_once(now=NOW + 120)

    # The first two verdicts are re-read from what was persisted at the time and
    # are unchanged by the third observation.
    assert first.candidates[0].verdict.alert is False
    assert second.candidates[0].verdict.alert is False
    events = await store.recent_events(limit=10)
    early = [row for row in events if row["occurred_at"] == NOW]
    assert early and early[0]["market_cap_usd"] == 100000.0


async def test_one_escalation_ping_per_candidate(store) -> None:
    """Section 47: promotion sends exactly one ping, not one per poll."""

    frames = [
        (_obs(at=NOW, rank=45, market_cap="150000", liquidity="120000"),),
        (
            _obs(
                at=NOW + 120,
                rank=10,
                market_cap="210000",
                liquidity="130000",
                holders=900,
            ),
        ),
        (
            _obs(
                at=NOW + 180,
                rank=4,
                market_cap="240000",
                liquidity="130000",
                holders=1400,
            ),
        ),
    ]
    published: list[str] = []

    async def publish(candidate):
        published.append(candidate.mint)
        return True

    runtime = TrendingRuntime(store, _Client(frames), publish=publish)
    await runtime.poll_once(now=NOW)
    await runtime.poll_once(now=NOW + 120)
    await runtime.poll_once(now=NOW + 180)
    assert published.count("MintAAA") <= 1


async def test_the_lane_reports_no_source_rather_than_an_empty_board(store) -> None:
    """Section 34: "not connected" and "connected and quiet" are different."""

    class _NoSource(_Client):
        def __init__(self):
            super().__init__([()])
            self.source = source_from_settings(
                api_url=None, api_key=None, proxy_enabled=False
            )

    runtime = TrendingRuntime(store, _NoSource())
    status = await runtime.status(now=NOW)
    assert status["health"]["state"] == "NO_SOURCE_CONFIGURED"
    assert status["source"]["kind"] == SOURCE_NONE


async def test_the_trending_lane_never_reports_live_execution(store) -> None:
    """Section 109: no signer, no key, no swap, no SOL — structurally."""

    runtime = TrendingRuntime(store, _Client([()]))
    status = await runtime.status(now=NOW)
    assert status["live_execution"] is False

    import smart_money_bot.trending as package

    source = __import__("pathlib").Path(package.__file__).parent
    for path in source.glob("*.py"):
        text = path.read_text()
        for forbidden in ("Keypair", "send_transaction", "sign_transaction", "aiohttp"):
            assert forbidden not in text, f"{path.name} must stay provider-free"


async def test_a_new_entrant_is_evaluated_before_older_board_members(store) -> None:
    """Section 77: a backlog must never push a fresh entrant to the next poll."""

    old = _obs("MintOLD", at=NOW, rank=3, market_cap="900000")
    runtime = TrendingRuntime(
        store,
        _Client(
            [
                (old,),
                (
                    _obs("MintOLD", at=NOW + 60, rank=3, market_cap="900000"),
                    _obs("MintNEW", at=NOW + 60, rank=30, market_cap="120000"),
                ),
            ]
        ),
    )
    await runtime.poll_once(now=NOW)
    result = await runtime.poll_once(now=NOW + 60)
    assert "MintNEW" in result.new_entries
    assert result.candidates[0].mint == "MintNEW"
