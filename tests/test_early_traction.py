"""Early Traction (v2.56): the operator's Axiom screen, and the speed lane.

This lane is unusual in this repository in that its correctness is mostly about
what it *refuses* to do, so most of what follows is a negative test.

Three properties are load-bearing and each has its own group below.

**No safety gate, ever.**  The operator's Axiom filters leave top-10, dev
holding, insider, bundler and holder count blank.  Replicating the screen means
computing those numbers, showing them loudly, and letting a token through
anyway.  A well-meaning future change that "just adds a top-10 ceiling" would
silently produce a different screen from the one being replicated, so the
absence of that gate is asserted structurally as well as behaviourally.

**One alert per mint, surviving restarts.**  The claim is an atomic
``INSERT OR IGNORE``, so two concurrent passes cannot both send; and a delivery
that fails permanently gives the claim back, so a Discord outage costs a delay
rather than the token.

**Latency is measured, not asserted.**  Every figure is anchored to the on-chain
creation timestamp, samples without one are counted separately rather than
averaged in as zero, and p95 is reported beside p50 because the tail is where
the opportunity is actually lost.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import time
from decimal import Decimal
from pathlib import Path

import pytest

import smart_money_bot.fast_alerts as fa
import smart_money_bot.traction as traction_package
import smart_money_bot.traction_cards as cards
import smart_money_bot.traction_runtime as runtime_module
import smart_money_bot.traction_store as store_module
from smart_money_bot.database import Database
from smart_money_bot.discord_render import MESSAGE_EMBED_LIMIT, build_embed
from smart_money_bot.traction import profile as profile_module
from smart_money_bot.traction.candidate import (
    ACCELERATING,
    BALANCED,
    FADING,
    RICH,
    THIN,
    UNKNOWN,
    MomentumBlock,
    QualityBlock,
    TractionCandidate,
    build_momentum,
    build_quality,
)
from smart_money_bot.traction.latency import (
    STAGE_CREATION_TO_ALERT,
    STAGE_CREATION_TO_DETECTION,
    STAGE_DETECTION_TO_ALERT,
    LatencySample,
    build_report,
    summarise_stage,
)
from smart_money_bot.traction.launchpads import (
    BAGS,
    DISABLED,
    ENABLED,
    HEAVEN,
    INVALID,
    MIGRATION_UNKNOWN,
    NOT_CONFIGURED,
    POST_MIGRATION,
    PRE_MIGRATION,
    PUMP,
    build_registry,
)
from smart_money_bot.traction.profile import (
    REASON_AGE,
    REASON_AGE_UNKNOWN,
    REASON_CHAIN,
    REASON_LAUNCHPAD,
    REASON_MARKET_CAP,
    REASON_MARKET_CAP_UNKNOWN,
    REASON_MIGRATION_EXCLUDED,
    REASON_NO_X_LINK,
    REASON_VOLUME,
    REASON_VOLUME_UNKNOWN,
    TractionObservation,
    TractionProfile,
    evaluate,
)
from smart_money_bot.traction.safety import DeveloperHistory, SafetyReport, build_safety
from smart_money_bot.traction.xlink import (
    ABSENT,
    ACCOUNT_MISSING,
    ACCOUNT_UNVERIFIED,
    ACCOUNT_VERIFIED,
    COMMUNITY,
    MALFORMED,
    NOT_AN_ACCOUNT,
    SINGLE_TWEET,
    ReuseReport,
    XLinkAssessment,
    XLinkVerdict,
    assess_link,
)
from smart_money_bot.traction_cards import build_traction_card
from smart_money_bot.traction_runtime import (
    TractionConfig,
    TractionRuntime,
    _merge_observation,
)
from smart_money_bot.traction_store import TractionStore

NOW = 1_700_000_000
#: A real Pump program address, already used in production by this repository.
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def mint(seed: str) -> str:
    """A base58-shaped mint, deterministic per seed."""

    return (seed * 44)[:44]


ALPHA = mint("A")
BRAVO = mint("B")
CHARLIE = mint("C")


def registry(**kwargs):
    kwargs.setdefault("pump_program_id", PUMP_PROGRAM)
    return build_registry(**kwargs)


PUMP_ONLY = registry()


def observation(**overrides) -> TractionObservation:
    """A token that matches the screen, so each test can break one thing."""

    values = dict(
        mint=ALPHA,
        launchpad=PUMP,
        chain_created_at=NOW - 120,
        market_cap_usd=Decimal("12000"),
        volume_usd=Decimal("9000"),
        liquidity_usd=Decimal("6000"),
        price_usd=Decimal("0.000012"),
        migration_state=PRE_MIGRATION,
        x_link="https://x.com/realproject",
        name="Test Token",
        symbol="TEST",
        first_seen_at=NOW - 118,
    )
    values.update(overrides)
    return TractionObservation(**values)


def verdict_for(obs: TractionObservation, *, profile=None, now: int = NOW):
    return evaluate(
        obs,
        registry=PUMP_ONLY,
        profile=profile or TractionProfile(),
        now=now,
    )


# ======================================================================
# THE PROFILE: the operator's screen, and the gates it deliberately lacks
# ======================================================================
def test_the_default_profile_is_the_operators_axiom_screen() -> None:
    """Every default here is a number the operator dictated, not a guess."""

    profile = TractionProfile()
    assert profile.max_age_seconds == 1_500  # 25 minutes
    assert profile.min_market_cap_usd == Decimal("8000")
    assert profile.min_volume_usd == Decimal("5000")
    assert profile.require_x_link is True
    assert profile.require_dex_paid is False
    assert profile.include_pre_migration is True
    assert profile.include_post_migration is True
    assert profile.chains == frozenset({"solana"})


def test_a_token_matching_the_screen_qualifies() -> None:
    result = verdict_for(observation())
    assert result.qualifies, result.reasons
    assert result.reasons == ()
    assert result.measured["age_seconds"] == 120
    assert result.measured["volume_window"] == "5m"


def test_no_safety_number_however_bad_can_reject_a_candidate() -> None:
    """The headline invariant, asserted behaviourally.

    The operator's filters leave every safety field blank, so this token — 98%
    of supply in ten wallets, the deployer holding most of it, a thousand
    flagged bundler buys and four holders — still matches the screen.  Seeing it
    and refusing it is the operator's decision to make; not seeing it is not.
    """

    horrific = build_safety(
        ALPHA,
        top10_percent=Decimal("98"),
        dev_holding_percent=Decimal("74"),
        insider_percent=Decimal("61"),
        bundler_percent=Decimal("88"),
        holder_count=4,
        enriched_at=NOW,
    )
    # The safety report exists, is fully populated, and is not an input to the
    # decision at all -- evaluate() has no parameter that could accept it.
    assert horrific.known_count == 5
    assert "safety" not in inspect.signature(evaluate).parameters
    assert verdict_for(observation()).qualifies


def test_the_profile_module_cannot_even_see_the_safety_module() -> None:
    """Structural version of the same rule.

    Behaviour can be changed by one line; an import cannot be added by accident.
    If a future change wants a safety gate it has to add this import first, and
    this test is where it has to argue for it.
    """

    source = Path(inspect.getfile(profile_module)).read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "safety" not in node.module, (
                "profile.py imported the safety module: the screen has no safety "
                "gates and must not be able to grow one"
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert "safety" not in alias.name


def test_the_profile_json_states_that_the_absence_of_safety_gates_is_deliberate() -> None:
    """An operator reading /status must not mistake this for an oversight."""

    payload = TractionProfile().to_json()
    assert "NONE" in payload["safety_gates"]
    assert "never filtered" in payload["safety_gates"]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"market_cap_usd": Decimal("7999")}, REASON_MARKET_CAP),
        ({"market_cap_usd": None}, REASON_MARKET_CAP_UNKNOWN),
        ({"volume_usd": Decimal("4999")}, REASON_VOLUME),
        ({"volume_usd": None}, REASON_VOLUME_UNKNOWN),
        ({"x_link": "   "}, REASON_NO_X_LINK),
        ({"chain_created_at": None}, REASON_AGE_UNKNOWN),
    ],
)
def test_a_not_yet_failure_is_retryable(overrides, reason) -> None:
    """A token below the floor at two minutes may clear it at four.

    This is the difference between a screen and a snapshot of the instant of
    creation: nothing meets a $5,000 volume floor in its first second, so a
    lane that evaluated once at mint time would alert on nothing at all.
    """

    result = verdict_for(observation(**overrides))
    assert not result.qualifies
    assert reason in result.reasons
    assert result.retryable
    assert not result.terminal


@pytest.mark.parametrize(
    ("overrides", "reason", "profile"),
    [
        ({"launchpad": "SOMETHING_ELSE"}, REASON_LAUNCHPAD, None),
        ({"chain": "ink"}, REASON_CHAIN, None),
        ({"chain_created_at": NOW - 1_501}, REASON_AGE, None),
        (
            {"migration_state": POST_MIGRATION},
            REASON_MIGRATION_EXCLUDED,
            TractionProfile(include_post_migration=False),
        ),
    ],
)
def test_a_no_is_terminal_and_the_mint_is_evicted(overrides, reason, profile) -> None:
    """No amount of traction makes a token younger or moves its launchpad."""

    result = verdict_for(observation(**overrides), profile=profile)
    assert not result.qualifies
    assert reason in result.reasons
    assert result.terminal
    assert not result.retryable


def test_one_terminal_reason_makes_the_whole_verdict_terminal() -> None:
    """A mixed verdict must not be kept alive by its retryable half."""

    result = verdict_for(
        observation(chain_created_at=NOW - 2_000, market_cap_usd=Decimal("10"))
    )
    assert REASON_AGE in result.reasons
    assert REASON_MARKET_CAP in result.reasons
    assert result.terminal


def test_both_sides_of_migration_are_admitted_by_default() -> None:
    """The operator's screen covers bonding curve and migrated alike."""

    for state in (PRE_MIGRATION, POST_MIGRATION, MIGRATION_UNKNOWN):
        assert verdict_for(observation(migration_state=state)).qualifies, state


def test_dex_paid_is_recorded_and_never_required() -> None:
    """'Dex paid: not required' was explicit, so an unpaid token must pass."""

    result = verdict_for(observation(dex_paid=False))
    assert result.qualifies
    assert result.measured["dex_paid"] is False


def test_an_unknown_value_fails_its_own_check_rather_than_passing_it() -> None:
    """Not knowing is not the same as meeting the threshold.

    Admitting an unreadable market cap 'because we are not sure' would quietly
    widen the screen; the failure is retryable, so an unknown that resolves
    seconds later still qualifies.
    """

    result = verdict_for(observation(market_cap_usd=None, volume_usd=None))
    assert not result.qualifies
    assert result.retryable
    assert result.measured["market_cap_usd"] is None


# ======================================================================
# LAUNCHPADS: the refusal to invent a program address
# ======================================================================
def test_only_pump_ships_enabled_and_every_other_launchpad_says_why() -> None:
    """A guessed program address is indistinguishable from a dead lane.

    It subscribes successfully and then either goes quiet or decodes unrelated
    instructions into plausible launches, so an unconfigured adapter is off and
    reported rather than approximated.
    """

    status = PUMP_ONLY.status()
    assert status["enabled"] == [PUMP]
    assert set(status["unavailable"]) == {BAGS, "BONK", "LIQUIDAF", HEAVEN}
    for name, detail in status["unavailable"].items():
        assert "will not guess" in detail
        # The detail must name the exact variable to set, or it is not actionable.
        assert f"TRACTION_LAUNCHPAD_{name}_PROGRAM_ID" in detail
    assert status["listening_to_programs"] == [PUMP_PROGRAM]


def test_an_unconfigured_launchpad_is_refused_not_admitted() -> None:
    """Being unable to listen to a venue is not a reason to accept its tokens."""

    assert PUMP_ONLY.accepts(PUMP)
    for name in (BAGS, "BONK", "LIQUIDAF", HEAVEN, "UNKNOWN_VENUE", ""):
        assert not PUMP_ONLY.accepts(name), name


def test_an_operator_supplied_address_turns_a_launchpad_on() -> None:
    resolved = registry(program_ids={"BAGS": BRAVO})
    adapter = resolved.by_name(BAGS)
    assert adapter is not None
    assert adapter.state == ENABLED
    assert adapter.program_id == BRAVO
    assert "supplied by the operator" in adapter.detail
    assert resolved.accepts(BAGS)


def test_a_typo_in_a_railway_variable_is_visible_rather_than_mysterious() -> None:
    """An invalid address must not be silently dropped.

    Dropped, it looks exactly like a quiet launchpad; reported as INVALID, it
    looks like the typo it is.
    """

    adapter = registry(program_ids={"BONK": "not a base58 address!!"}).by_name("BONK")
    assert adapter is not None
    assert adapter.state == INVALID
    assert not adapter.enabled
    assert "typo" in adapter.detail


def test_a_configured_launchpad_can_still_be_switched_off() -> None:
    adapter = registry(
        program_ids={"HEAVEN": CHARLIE}, disabled=frozenset({HEAVEN})
    ).by_name(HEAVEN)
    assert adapter is not None
    assert adapter.state == DISABLED
    assert not adapter.enabled


def test_pump_falls_back_to_the_address_this_repository_already_uses() -> None:
    adapter = PUMP_ONLY.by_name(PUMP)
    assert adapter is not None
    assert adapter.program_id == PUMP_PROGRAM
    assert "already used in production" in adapter.detail


def test_with_no_builtin_address_even_pump_is_not_configured() -> None:
    """Nothing in the registry invents an address, including for Pump."""

    adapter = build_registry().by_name(PUMP)
    assert adapter is not None
    assert adapter.state == NOT_CONFIGURED


# ======================================================================
# X LINKS: validation without a network call, and reuse across mints
# ======================================================================
@pytest.mark.parametrize(
    ("raw", "expected", "handle"),
    [
        ("https://x.com/realproject", ACCOUNT_UNVERIFIED, "realproject"),
        ("https://twitter.com/RealProject", ACCOUNT_UNVERIFIED, "realproject"),
        ("@realproject", ACCOUNT_UNVERIFIED, "realproject"),
        ("x.com/realproject", ACCOUNT_UNVERIFIED, "realproject"),
        ("https://x.com/i/communities/1789", COMMUNITY, ""),
        ("https://x.com/someone/status/1234567890", SINGLE_TWEET, "someone"),
        ("https://x.com/search?q=dogwifhat", NOT_AN_ACCOUNT, ""),
        ("https://x.com/", NOT_AN_ACCOUNT, ""),
        ("https://example.com/project", MALFORMED, ""),
        ("https://x.com/way_too_long_a_handle_for_x", MALFORMED, ""),
        ("", ABSENT, ""),
        ("   ", ABSENT, ""),
    ],
)
def test_an_x_link_is_classified_from_its_structure_alone(raw, expected, handle) -> None:
    """'Has a Twitter link' is a much weaker fact than it looks.

    The metadata field accepts any string, so this grades what is actually in
    it -- for free, with no network call, which is what lets it run on the fast
    path.
    """

    result = assess_link(raw)
    assert result.link_class == expected, raw
    assert result.handle == handle


def test_a_community_page_and_a_borrowed_tweet_are_present_but_not_accounts() -> None:
    """The specific abuse the operator asked to be flagged.

    A reused tweet borrows somebody else's engagement, and a community page has
    no owner, follower count or history to judge.  Both are *present*, so a
    naive presence check counts them; neither is an account.
    """

    community = assess_link("https://x.com/i/communities/1789")
    tweet = assess_link("https://x.com/someone/status/1234567890")
    for result in (community, tweet):
        assert not result.is_account
        assert not result.usable
    assert tweet.tweet_id == "1234567890"
    assert community.community_id == "1789"


def test_a_structurally_valid_handle_is_never_reported_as_verified() -> None:
    """Existence costs an X API call, so the fast path must not claim it."""

    result = assess_link("https://x.com/realproject")
    assert result.link_class == ACCOUNT_UNVERIFIED
    assert "existence not yet checked" in result.detail
    summary = XLinkVerdict(assessment=result).summary
    assert "existence unchecked" in summary
    assert "exists" not in summary.replace("existence unchecked", "")


def test_the_summary_says_what_a_live_lookup_found_only_once_it_ran() -> None:
    verified = XLinkVerdict(
        assessment=XLinkAssessment(
            raw="https://x.com/realproject",
            link_class=ACCOUNT_VERIFIED,
            handle="realproject",
        ),
        followers=4_120,
        account_age_days=900,
        live_checked=True,
    )
    assert "exists" in verified.summary
    assert "4120 followers" in verified.summary

    missing = XLinkVerdict(
        assessment=XLinkAssessment(
            raw="https://x.com/ghost", link_class=ACCOUNT_MISSING, handle="ghost"
        ),
        live_checked=True,
    )
    assert "not found or suspended" in missing.summary


@pytest.mark.parametrize(
    ("count", "severity"),
    [
        (0, "NONE"),
        (1, "SEEN_ON_ONE_OTHER_MINT"),
        (3, "SEEN_ON_SEVERAL_MINTS"),
        (9, "SEEN_ON_MANY_MINTS"),
    ],
)
def test_reuse_severity_counts_distinct_mints(count, severity) -> None:
    """One X identity on six tokens says more than any single token's metadata."""

    report = ReuseReport(
        handle="shared",
        other_mints=tuple(mint(chr(ord("d") + index)) for index in range(count)),
    )
    assert report.severity == severity
    assert report.distinct_mints == count
    assert report.reused is (count > 0)


def test_reuse_counts_a_mint_once_even_when_the_handle_and_tweet_both_match() -> None:
    shared = mint("d")
    report = ReuseReport(
        handle="shared", other_mints=(shared,), tweet_other_mints=(shared,)
    )
    assert report.distinct_mints == 1


def test_the_card_summary_flags_reuse_beside_the_link() -> None:
    result = XLinkVerdict(
        assessment=assess_link("https://x.com/shared"),
        reuse=ReuseReport(handle="shared", other_mints=(BRAVO, CHARLIE)),
    )
    assert "same X identity seen on 2" in result.summary


# ======================================================================
# SAFETY: loud and powerless
# ======================================================================
def test_the_safety_report_exposes_no_boolean_a_gate_could_read() -> None:
    """There is deliberately nothing here for a filter to consult.

    Adding a gate later would require adding a property here first, which is
    where somebody would have to argue for it.
    """

    forbidden = {"passed", "passes", "safe", "is_safe", "blocked", "ok", "rejected"}
    attributes = set(dir(SafetyReport))
    assert not (forbidden & attributes), forbidden & attributes


@pytest.mark.parametrize("metric", ["top10_percent", "dev_holding_percent", "holder_count"])
def test_an_unmeasured_safety_value_renders_as_unknown_never_as_zero(metric) -> None:
    """A comfortable default destroys the block's only purpose.

    The value of this field is that the operator can see which risks were
    actually measured; '0%' asserts a measurement nobody took.
    """

    report = build_safety(ALPHA, enriched_at=NOW)
    value = getattr(report, metric)
    assert not value.known
    assert value.render() == "unknown"
    assert "0" not in value.render()


def test_a_pending_safety_block_says_pending_rather_than_looking_clean() -> None:
    """An absent block reads as 'no risks found'.  A pending one does not."""

    pending = build_safety(ALPHA)
    assert pending.pending
    lines = "\n".join(pending.render_lines())
    assert "enrichment in flight" in lines
    assert "not yet measured" in lines


def test_a_returned_safety_block_reports_its_own_completeness() -> None:
    report = build_safety(
        ALPHA,
        top10_percent=Decimal("41.5"),
        holder_count=212,
        developer=DeveloperHistory(
            wallet=BRAVO, tokens_created=9, graduated=1, collapsed=7, source="test"
        ),
        enriched_at=NOW,
    )
    lines = "\n".join(report.render_lines())
    assert "top-10 holders: 41.5%" in lines
    assert "holders: 212" in lines
    assert "9 prior launch(es)" in lines
    assert "2/5 measured" in lines
    assert "never 0" in lines


def test_a_nonsense_provider_value_becomes_unknown_rather_than_a_number() -> None:
    report = build_safety(
        ALPHA, top10_percent="not a number", insider_percent=float("nan"), enriched_at=NOW
    )
    assert not report.top10_percent.known
    assert not report.insider_percent.known


def test_the_deployer_history_says_so_when_there_is_no_record() -> None:
    assert "no prior-launch record" in DeveloperHistory().render()


# ======================================================================
# LATENCY: measure first
# ======================================================================
def _sample(**overrides) -> LatencySample:
    values = dict(
        mint=ALPHA,
        chain_created_at=NOW,
        detected_at=NOW + 3,
        alert_sent_at=NOW + 11,
        source="creation_stream",
        launchpad=PUMP,
    )
    values.update(overrides)
    return LatencySample(**values)


def test_the_three_stages_are_measured_separately() -> None:
    """creation->detection is fixed by a websocket; detection->alert by us."""

    sample = _sample()
    assert sample.stage_seconds(STAGE_CREATION_TO_DETECTION) == 3
    assert sample.stage_seconds(STAGE_DETECTION_TO_ALERT) == 8
    assert sample.stage_seconds(STAGE_CREATION_TO_ALERT) == 11


def test_a_sample_without_an_on_chain_creation_time_is_unknown_not_zero() -> None:
    """Averaging it in as zero would flatter every figure in the report."""

    samples = [
        _sample(mint=ALPHA, chain_created_at=NOW, detected_at=NOW + 10),
        _sample(mint=BRAVO, chain_created_at=None, detected_at=NOW + 10),
    ]
    stage = summarise_stage(samples, STAGE_CREATION_TO_DETECTION)
    assert stage.samples == 1
    assert stage.unknown_grade == 1
    assert stage.p50_seconds == Decimal(10)
    assert samples[0].graded_realtime
    assert not samples[1].graded_realtime


def test_p95_and_max_both_exist_because_they_answer_different_questions() -> None:
    """A tail the median hides, and a single outlier the p95 also hides.

    With one slow detection in twenty-one, the p95 is correctly unmoved -- under
    5% of the population is above it -- and that is exactly why ``max`` is
    reported too.  Neither figure alone is honest about the tail.
    """

    fast = [
        _sample(mint=mint(chr(ord("d") + index)), detected_at=NOW + index)
        for index in range(20)
    ]
    one_outlier = summarise_stage(
        [*fast, _sample(mint=mint("z"), detected_at=NOW + 400)],
        STAGE_CREATION_TO_DETECTION,
    )
    assert one_outlier.samples == 21
    assert one_outlier.p50_seconds == Decimal(10)
    assert one_outlier.p95_seconds == Decimal(19)
    assert one_outlier.max_seconds == Decimal(400)

    # A real tail -- roughly a tenth of detections slow -- does move the p95,
    # while the median stays comfortable.  That is the case p95 exists for.
    real_tail = summarise_stage(
        [
            *fast,
            _sample(mint=mint("y"), detected_at=NOW + 400),
            _sample(mint=mint("z"), detected_at=NOW + 420),
        ],
        STAGE_CREATION_TO_DETECTION,
    )
    assert real_tail.p50_seconds == Decimal(11)
    assert real_tail.p95_seconds == Decimal(400)
    assert real_tail.max_seconds == Decimal(420)
    assert real_tail.sufficient


def test_a_figure_from_four_observations_looks_like_one() -> None:
    stage = summarise_stage([_sample(mint=mint("d"))], STAGE_CREATION_TO_DETECTION)
    assert not stage.sufficient
    assert "thin: n=1" in stage.render()


def test_no_samples_is_stated_rather_than_rendered_as_a_zero() -> None:
    report = build_report([])
    assert report.headline == "no end-to-end latency samples yet"
    assert "no samples yet" in report.stages[STAGE_CREATION_TO_ALERT].render()


def test_the_report_splits_creation_to_detection_by_source() -> None:
    """So a slow source is visible rather than averaged away by a fast one."""

    report = build_report(
        [
            _sample(mint=ALPHA, source="creation_stream", detected_at=NOW + 2),
            _sample(mint=BRAVO, source="trending_poll", detected_at=NOW + 240),
        ]
    )
    assert report.by_source["creation_stream"].p50_seconds == Decimal(2)
    assert report.by_source["trending_poll"].p50_seconds == Decimal(240)


# ======================================================================
# MOMENTUM AND QUALITY: separate, and never blended
# ======================================================================
def test_momentum_needs_two_readings_before_it_claims_a_rate() -> None:
    block = build_momentum([(NOW, Decimal("9000"), Decimal("6000"))], volume_window_seconds=300)
    assert block.readings == 1
    assert block.state == UNKNOWN
    assert "rates need two" in "\n".join(block.render_lines())


def test_momentum_divides_by_the_time_actually_elapsed() -> None:
    """On a sparse pool the real gap and the nominal window differ.

    Dividing by the nominal value would understate the rate exactly when
    polling was slowest, which is when the number matters most.
    """

    block = build_momentum(
        [
            (NOW, Decimal("10000"), Decimal("6000")),
            (NOW + 120, Decimal("14000"), Decimal("9000")),
        ],
        volume_window_seconds=300,
    )
    # $4,000 over 120 real seconds is $2,000/min, not $4,000 over a 300s window.
    assert block.market_cap_per_minute == Decimal("2000.00")
    assert block.window_seconds == 120
    assert block.market_cap_ratio == Decimal("1.4000")
    assert block.state == ACCELERATING


@pytest.mark.parametrize(
    ("first", "second", "state"),
    [
        (Decimal("10000"), Decimal("12000"), ACCELERATING),
        (Decimal("10000"), Decimal("10200"), "STEADY"),
        (Decimal("10000"), Decimal("9000"), FADING),
    ],
)
def test_momentum_state_describes_the_move_without_judging_it(first, second, state) -> None:
    block = build_momentum(
        [(NOW, first, Decimal("6000")), (NOW + 60, second, Decimal("6000"))],
        volume_window_seconds=300,
    )
    assert block.state == state


@pytest.mark.parametrize(
    ("volume", "state"),
    [
        (Decimal("12000"), RICH),
        (Decimal("5000"), BALANCED),
        (Decimal("1000"), THIN),
    ],
)
def test_quality_puts_the_move_in_proportion(volume, state) -> None:
    block = build_quality(observation(market_cap_usd=Decimal("10000"), volume_usd=volume))
    assert block.state == state


def test_quality_returns_unknown_rather_than_guessing_a_denominator() -> None:
    assert build_quality(observation(market_cap_usd=None)).state == UNKNOWN
    assert build_quality(observation(market_cap_usd=Decimal("0"))).state == UNKNOWN
    assert build_quality(observation(volume_usd=None)).volume_over_market_cap is None


def test_momentum_quality_and_safety_never_become_one_number() -> None:
    """The instruction was explicit and it is load-bearing.

    A blended score lets a strong momentum reading cancel a 70% top-ten
    holding, producing a confident figure that describes neither -- the failure
    this repository's own v2.54 notes describe.
    """

    candidate = TractionCandidate(
        observation=observation(),
        verdict=verdict_for(observation()),
        momentum=MomentumBlock(market_cap_ratio=Decimal("1.4"), readings=2),
        quality=QualityBlock(volume_over_market_cap=Decimal("0.9")),
        safety=build_safety(ALPHA, top10_percent=Decimal("70"), enriched_at=NOW),
    )
    payload = candidate.to_json()
    assert set(payload["momentum"]) & set(payload["quality"]) == {"state"}
    assert payload["momentum"]["state"] != payload["quality"]["state"]
    # No blended field anywhere in the candidate payload.
    flat = str(payload)
    for forbidden in ("overall_score", "combined_score", "total_score", "blended"):
        assert forbidden not in flat
    # And nothing in the candidate module does arithmetic across the blocks.
    source = Path(inspect.getfile(runtime_module)).read_text()
    assert "momentum.state ==" not in source
    assert "quality.state ==" not in source


# ======================================================================
# THE STORE AND THE RUNTIME: dedupe, backoff, enrichment, forward tracking
# ======================================================================
@pytest.fixture
async def database(tmp_path):
    db = Database(str(tmp_path / "traction.db"), Decimal("1000"))
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
async def store(database):
    return TractionStore(database)


class Recorder:
    """A publisher/editor pair that records instead of talking to Discord."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.sent: list[str] = []
        self.edited: list[str] = []
        self.sent_safety_pending: list[bool] = []
        self.edited_safety_pending: list[bool] = []

    async def publish(self, candidate: TractionCandidate) -> tuple[int, int] | None:
        if self.fail_times > 0:
            self.fail_times -= 1
            return None
        self.sent.append(candidate.mint)
        self.sent_safety_pending.append(
            candidate.safety is None or candidate.safety.pending
        )
        return (4242, 1000 + len(self.sent))

    async def edit(self, candidate: TractionCandidate) -> bool:
        self.edited.append(candidate.mint)
        self.edited_safety_pending.append(
            candidate.safety is None or candidate.safety.pending
        )
        return True


def build_runtime(
    store: TractionStore,
    *,
    recorder: Recorder | None = None,
    market: dict[str, dict[str, object]] | None = None,
    enricher=None,
    forward_tracker=None,
    **config_overrides,
) -> tuple[TractionRuntime, Recorder]:
    surface = recorder or Recorder()
    rows = market if market is not None else {}

    async def reader(mints):
        return {name: rows[name] for name in mints if name in rows}

    config_overrides.setdefault("recheck_seconds", 0)
    config_overrides.setdefault("max_reads_per_minute", 10_000)
    config = TractionConfig(
        poll_seconds=0,
        send_backoff_seconds=0.0,
        **config_overrides,
    )
    return (
        TractionRuntime(
            store,
            registry=PUMP_ONLY,
            config=config,
            market_reader=reader,
            enricher=enricher,
            publisher=surface.publish,
            editor=surface.edit,
            forward_tracker=forward_tracker,
        ),
        surface,
    )


def market_row(**overrides) -> dict[str, object]:
    row = dict(
        market_cap_usd=Decimal("12000"),
        volume_usd=Decimal("9000"),
        liquidity_usd=Decimal("6000"),
        price_usd=Decimal("0.000012"),
        x_link="https://x.com/realproject",
        symbol="TEST",
        name="Test Token",
    )
    row.update(overrides)
    return row


async def drain_enrichment(runtime: TractionRuntime) -> None:
    """Await the detached enrichment tasks.

    They are detached on purpose -- the card must not wait on them -- so a test
    has to wait for them explicitly rather than relying on ordering.
    """

    for _ in range(20):
        pending = [task for task in runtime._enrich_tasks if not task.done()]
        if not pending:
            await asyncio.sleep(0)
            if not any(not task.done() for task in runtime._enrich_tasks):
                return
            continue
        await asyncio.gather(*pending, return_exceptions=True)


async def test_a_mint_can_be_claimed_exactly_once_for_all_time(store) -> None:
    """The dedupe key is the primary key, so this is enforced by the schema."""

    candidate = TractionCandidate(
        observation=observation(), verdict=verdict_for(observation()), detected_at=NOW
    )
    assert await store.claim_alert(candidate, now=NOW) is True
    assert await store.claim_alert(candidate, now=NOW + 60) is False
    assert await store.already_alerted(ALPHA)


async def test_two_concurrent_evaluations_cannot_both_win_the_claim(store) -> None:
    """The check and the claim are one statement precisely for this case.

    A SELECT followed by an INSERT leaves a window in which both evaluations
    decide to send, which is the duplicate the operator asked to be impossible.
    """

    candidate = TractionCandidate(
        observation=observation(), verdict=verdict_for(observation()), detected_at=NOW
    )
    results = await asyncio.gather(
        *(store.claim_alert(candidate, now=NOW) for _ in range(6))
    )
    assert results.count(True) == 1


async def test_the_dedupe_survives_a_restart(database) -> None:
    """A fresh runtime with a cold cache must not re-alert an old mint."""

    store = TractionStore(database)
    first, surface = build_runtime(store, market={ALPHA: market_row()})
    await first.observe_creation(
        mint=ALPHA, launchpad=PUMP, chain_created_at=NOW - 60, now=NOW
    )
    assert (await first.run_pass(now=NOW + 1)).alerted == (ALPHA,)
    await drain_enrichment(first)

    # Simulated redeploy: new runtime, new in-memory state, same database.
    second, again = build_runtime(TractionStore(database), market={ALPHA: market_row()})
    await second.restore(now=NOW + 2)
    assert ALPHA in second._alerted
    entered = await second.observe_creation(
        mint=ALPHA, launchpad=PUMP, chain_created_at=NOW - 60, now=NOW + 2
    )
    assert entered is False
    assert again.sent == []
    assert surface.sent == [ALPHA]


async def test_a_permanently_failed_send_releases_the_claim(database) -> None:
    """A held claim on an undelivered card is permanent silent loss.

    Without the release, one Discord outage would make the token unalertable
    forever -- the alert would be marked sent and never have been seen.
    """

    store = TractionStore(database)
    runtime, surface = build_runtime(
        store,
        recorder=Recorder(fail_times=99),
        market={ALPHA: market_row()},
        max_send_attempts=2,
    )
    await runtime.observe_creation(
        mint=ALPHA, launchpad=PUMP, chain_created_at=NOW - 60, now=NOW
    )
    result = await runtime.run_pass(now=NOW + 1)
    assert result.qualified == (ALPHA,)
    assert result.alerted == ()
    assert runtime.alerts_failed == 1
    assert await store.already_alerted(ALPHA) is False
    assert ALPHA not in runtime._alerted

    # The mint is still in the pool, so the next pass can alert it.
    surface.fail_times = 0
    assert (await runtime.run_pass(now=NOW + 2)).alerted == (ALPHA,)
    await drain_enrichment(runtime)


async def test_a_rate_limit_costs_a_delay_rather_than_the_alert(database) -> None:
    """Two refusals then a success: the card still lands, once."""

    store = TractionStore(database)
    runtime, surface = build_runtime(
        store,
        recorder=Recorder(fail_times=2),
        market={ALPHA: market_row()},
        max_send_attempts=5,
    )
    await runtime.observe_creation(
        mint=ALPHA, launchpad=PUMP, chain_created_at=NOW - 60, now=NOW
    )
    assert (await runtime.run_pass(now=NOW + 1)).alerted == (ALPHA,)
    assert surface.sent == [ALPHA]
    assert runtime.alerts_failed == 0
    assert runtime.last_delivery_seconds is not None
    await drain_enrichment(runtime)


async def test_a_released_claim_cannot_delete_a_delivered_alert(store) -> None:
    """The release is guarded: only a row with no message id may be removed."""

    candidate = TractionCandidate(
        observation=observation(), verdict=verdict_for(observation()), detected_at=NOW
    )
    await store.claim_alert(candidate, now=NOW)
    await store.record_discord_message(ALPHA, channel_id=4242, message_id=7)
    assert await store.release_alert(ALPHA) is False
    assert await store.already_alerted(ALPHA) is True


async def test_the_card_is_sent_with_safety_pending_and_then_edited(database) -> None:
    """Send-then-edit: the alert never waits on enrichment.

    The pending block is what makes this honest -- omitting safety until it
    arrives would read as 'no risks found' on a two-minute-old token.
    """

    store = TractionStore(database)

    async def enricher(candidate):
        return build_safety(
            candidate.mint,
            top10_percent=Decimal("62"),
            holder_count=51,
            enriched_at=NOW + 5,
        )

    runtime, surface = build_runtime(
        store, market={ALPHA: market_row()}, enricher=enricher
    )
    await runtime.observe_creation(
        mint=ALPHA, launchpad=PUMP, chain_created_at=NOW - 60, now=NOW
    )
    await runtime.run_pass(now=NOW + 1)

    assert surface.sent == [ALPHA]
    assert surface.sent_safety_pending == [True]
    await drain_enrichment(runtime)
    # Exactly one edit, and NO second send.
    assert surface.edited == [ALPHA]
    assert surface.edited_safety_pending == [False]
    assert surface.sent == [ALPHA]
    assert runtime.enrichments_ok == 1

    rows = await store.pending_enrichment()
    assert rows == ()


async def test_an_enrichment_failure_leaves_the_card_in_place(database) -> None:
    """The alert is not held hostage to the safety provider."""

    store = TractionStore(database)

    async def broken(candidate):
        raise RuntimeError("provider 503")

    runtime, surface = build_runtime(
        store, market={ALPHA: market_row()}, enricher=broken
    )
    await runtime.observe_creation(
        mint=ALPHA, launchpad=PUMP, chain_created_at=NOW - 60, now=NOW
    )
    await runtime.run_pass(now=NOW + 1)
    await drain_enrichment(runtime)

    assert surface.sent == [ALPHA]
    assert runtime.enrichments_failed == 1
    assert surface.edited == [ALPHA]
    # The edit says the values were not measured, rather than showing zeros.
    assert surface.edited_safety_pending == [False]


async def test_a_slow_enricher_does_not_delay_the_alert(database) -> None:
    """The send must complete while enrichment is still in flight."""

    store = TractionStore(database)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(candidate):
        started.set()
        await release.wait()
        return build_safety(candidate.mint, enriched_at=NOW + 30)

    runtime, surface = build_runtime(store, market={ALPHA: market_row()}, enricher=slow)
    await runtime.observe_creation(
        mint=ALPHA, launchpad=PUMP, chain_created_at=NOW - 60, now=NOW
    )
    assert (await runtime.run_pass(now=NOW + 1)).alerted == (ALPHA,)
    assert surface.sent == [ALPHA]  # already delivered
    await asyncio.sleep(0)
    assert started.is_set()
    assert surface.edited == []  # and the edit has not happened yet
    release.set()
    await drain_enrichment(runtime)
    assert surface.edited == [ALPHA]


async def test_every_alerted_token_reaches_the_shared_forward_history(database) -> None:
    """Forward outcomes accrue in runner_candidates/runner_outcomes, not a copy.

    A parallel history would have to be reconciled with this one before either
    could be trusted, and the v2.34 horizons already cover +5m/+15m/+1h/+24h.
    """

    store = TractionStore(database)
    handed: list[str] = []

    async def tracker(candidate: TractionCandidate) -> bool:
        handed.append(candidate.mint)
        return True

    runtime, _ = build_runtime(
        store, market={ALPHA: market_row()}, forward_tracker=tracker
    )
    await runtime.observe_creation(
        mint=ALPHA, launchpad=PUMP, chain_created_at=NOW - 60, now=NOW
    )
    await runtime.run_pass(now=NOW + 1)
    await drain_enrichment(runtime)

    assert handed == [ALPHA]
    assert await store.forward_registered(ALPHA)
    assert runtime.forward_registered == 1
    assert await store.pending_forward_registration() == ()

    tracked = await store.tracked_alert_rows()
    assert [entry["mint"] for entry in tracked] == [ALPHA]
    assert tracked[0]["horizons_observed"] == 0  # no outcome observed yet, honestly


async def test_the_forward_row_this_lane_writes_is_readable_by_the_runner_lane(
    database,
) -> None:
    """Regression: this lane must not poison another lane's typed payload.

    ``runner_candidates.payload_json`` is a ``RunnerCandidate`` blob and
    ``runner_outcomes`` has a foreign key onto that table, so the row has to
    exist *and* be the right shape.  An earlier version of this lane wrote its
    own JSON there with raw SQL.  It inserted cleanly; then ``runner_due_mints``
    picked the mint up 45 seconds later, ``runner_candidate_from_json`` raised
    ``KeyError: 'first'``, and the runner outcome loop wedged on that mint and
    emitted an error card every poll -- a silent break in a lane this release was
    told not to touch.

    So the assertion is the whole round trip, through the runner lane's own
    reader, exactly as ``analyze_runner`` performs it.
    """

    from smart_money_bot.engine import SmartMoneyEngine
    from smart_money_bot.models import RunnerCandidate as _RC
    from smart_money_bot.runner import runner_candidate_from_json

    engine = SmartMoneyEngine.__new__(SmartMoneyEngine)
    engine.database = database

    # This one test uses the wall clock rather than the module's frozen NOW,
    # because the runner scheduler's own window ("first seen within 24h, not
    # refreshed in the last 45s") is only meaningful against real time, and
    # asserting it against a 2023 timestamp would pass or fail for the wrong
    # reason.
    now = int(time.time())
    detected_at = now - 118
    live = observation(chain_created_at=now - 120)
    candidate = TractionCandidate(
        observation=live,
        verdict=verdict_for(live, now=now),
        detected_at=detected_at,
        qualified_at=now,
        alert_sent_at=now,
    )
    assert await SmartMoneyEngine._traction_register_forward(engine, candidate) is True

    # 1. The row exists, so runner_outcomes can reference it at all.
    raw = await database.runner_candidate_payload(ALPHA)
    assert raw

    # 2. It parses through the runner lane's own reader -- the step that broke.
    parsed = runner_candidate_from_json(raw)
    assert isinstance(parsed, _RC)
    assert parsed.mint == ALPHA
    assert parsed.graduation_source == f"EARLY_TRACTION:{PUMP}"

    # 3. The horizons are measured from OUR detection, not from the alert.
    assert parsed.first_seen_at == detected_at
    assert parsed.first.market_cap_usd == Decimal("12000")
    assert parsed.first.liquidity_usd == Decimal("6000")

    # 4. The scheduler that drives the outcome loop can see it.
    assert await database.runner_due_mints(now=now + 60) == [ALPHA]

    # 5. And an outcome row can actually be written against it (the FK holds).
    assert await database.record_runner_outcome(
        mint=ALPHA,
        horizon_seconds=300,
        observed_at=now + 300,
        price_return_percent=None,
        market_cap_return_percent=Decimal("42"),
        liquidity_return_percent=None,
        liquidity_disappeared=False,
        rugged=False,
        route_available=True,
    )
    store = TractionStore(database)
    outcomes = await store.forward_outcomes(ALPHA)
    assert [row["horizon_seconds"] for row in outcomes] == [300]
    assert outcomes[0]["market_cap_return_percent"] == 42.0


async def test_forward_registration_never_overwrites_another_lanes_entry_facts(
    database,
) -> None:
    """This lane contributes observations; it does not get to rewrite history."""

    from smart_money_bot.engine import SmartMoneyEngine
    from smart_money_bot.models import (
        RunnerCandidate,
        RunnerMarketSnapshot,
        RunnerScoreBreakdown,
    )
    from smart_money_bot.runner import (
        runner_candidate_from_json,
        runner_candidate_to_json,
        runner_snapshot_to_json,
    )

    # The runner lane gets there first, through its own writer.
    incumbent_first = RunnerMarketSnapshot(
        mint=ALPHA, captured_at=NOW - 900, market_cap_usd=Decimal("999")
    )
    incumbent = RunnerCandidate(
        mint=ALPHA,
        symbol="TEST",
        name="Test Token",
        first_seen_at=NOW - 900,
        graduated_at=None,
        graduation_source="FOMO_RUNNER",
        first=incumbent_first,
        current=incumbent_first,
        score=Decimal("0"),
        tier="GRADUATED",
        breakdown=RunnerScoreBreakdown(),
    )
    await database.store_runner_candidate(
        incumbent,
        payload_json=runner_candidate_to_json(incumbent),
        snapshot_json=runner_snapshot_to_json(incumbent_first),
    )

    engine = SmartMoneyEngine.__new__(SmartMoneyEngine)
    engine.database = database
    candidate = TractionCandidate(
        observation=observation(),
        verdict=verdict_for(observation()),
        detected_at=NOW,
        qualified_at=NOW,
        alert_sent_at=NOW,
    )
    await SmartMoneyEngine._traction_register_forward(engine, candidate)

    # The runner lane's immutable baseline survives: its first_seen_at, its
    # graduation_source and its entry market cap are all untouched.
    parsed = runner_candidate_from_json(await database.runner_candidate_payload(ALPHA))
    assert parsed.first_seen_at == NOW - 900
    assert parsed.graduation_source == "FOMO_RUNNER"
    cursor = await database.db.execute(
        "SELECT first_market_cap_usd FROM runner_candidates WHERE mint = ?", (ALPHA,)
    )
    assert (await cursor.fetchone())["first_market_cap_usd"] == 999.0


async def test_a_forward_registration_failure_does_not_unsend_the_card(
    database,
) -> None:
    """The card is already on screen; a missing forward row is retried instead."""

    store = TractionStore(database)

    async def broken(candidate: TractionCandidate) -> bool:
        raise RuntimeError("runner table locked")

    runtime, surface = build_runtime(
        store, market={ALPHA: market_row()}, forward_tracker=broken
    )
    await runtime.observe_creation(
        mint=ALPHA, launchpad=PUMP, chain_created_at=NOW - 60, now=NOW
    )
    assert (await runtime.run_pass(now=NOW + 1)).alerted == (ALPHA,)
    await drain_enrichment(runtime)

    assert surface.sent == [ALPHA]  # the alert stands
    assert runtime.alerts_failed == 0
    assert runtime.forward_failed == 1
    assert not await store.forward_registered(ALPHA)
    assert await store.pending_forward_registration() == (ALPHA,)


async def test_a_restart_between_the_send_and_the_registration_is_recovered(
    database,
) -> None:
    """Otherwise the forward record would be missing exactly the alerts near a
    redeploy -- and those are indistinguishable from alerts that never happened."""

    store = TractionStore(database)
    first, _ = build_runtime(
        store,
        market={ALPHA: market_row()},
        forward_tracker=_raising_tracker(),
    )
    await first.observe_creation(
        mint=ALPHA, launchpad=PUMP, chain_created_at=NOW - 60, now=NOW
    )
    await first.run_pass(now=NOW + 1)
    await drain_enrichment(first)
    assert await store.pending_forward_registration() == (ALPHA,)

    handed: list[str] = []

    async def tracker(candidate: TractionCandidate) -> bool:
        handed.append(candidate.mint)
        # The rebuilt candidate must carry the entry baseline, or the horizons
        # would be measured against nothing.
        assert candidate.observation.market_cap_usd == Decimal("12000")
        assert candidate.detected_at == NOW
        return True

    second, _ = build_runtime(store, forward_tracker=tracker)
    assert await second.retry_forward_registration() == 1
    assert handed == [ALPHA]
    assert await store.pending_forward_registration() == ()


def _raising_tracker():
    async def tracker(candidate: TractionCandidate) -> bool:
        raise RuntimeError("redeployed mid-registration")

    return tracker


async def test_detection_is_stamped_before_anything_else_happens(database) -> None:
    """This timestamp anchors every latency figure in the lane.

    Anything that ran before it -- a filter, a query, an enrichment hop -- would
    make the lane look faster than it is.
    """

    store = TractionStore(database)
    runtime, _ = build_runtime(store, market={})
    assert await runtime.observe_creation(
        mint=ALPHA, launchpad=PUMP, chain_created_at=NOW - 30, now=NOW
    )
    samples = await store.latency_samples()
    assert len(samples) == 1
    assert samples[0].chain_created_at == NOW - 30
    assert samples[0].detected_at == NOW
    assert samples[0].alert_sent_at is None
    assert samples[0].source == "creation_stream"


async def test_a_launchpad_we_do_not_listen_to_is_dropped_before_any_storage(
    database,
) -> None:
    """It can never qualify however it develops, so it costs nothing at all."""

    store = TractionStore(database)
    runtime, _ = build_runtime(store)
    assert await runtime.observe_creation(
        mint=BRAVO, launchpad="BAGS", chain_created_at=NOW, now=NOW
    ) is False
    assert runtime.dropped_unknown_launchpad == 1
    assert await store.latency_samples() == ()
    assert runtime._pool == {}


async def test_the_young_pool_is_bounded_against_a_launch_storm(database) -> None:
    """An unbounded pool is how a fast lane becomes a slow one."""

    store = TractionStore(database)
    runtime, _ = build_runtime(store, max_pool=5)
    for index in range(12):
        await runtime.observe_creation(
            mint=mint(chr(ord("d") + index)),
            launchpad=PUMP,
            chain_created_at=NOW + index,
            now=NOW + index,
        )
    assert len(runtime._pool) == 5
    # The oldest is evicted first: it is closest to ageing out anyway.
    assert mint("d") not in runtime._pool
    assert mint("o") in runtime._pool


async def test_a_token_that_ages_out_is_evicted_rather_than_re_read(database) -> None:
    store = TractionStore(database)
    runtime, _ = build_runtime(store, market={ALPHA: market_row()})
    await runtime.observe_creation(
        mint=ALPHA, launchpad=PUMP, chain_created_at=NOW, now=NOW
    )
    result = await runtime.run_pass(now=NOW + 1_501)
    assert result.evicted == (ALPHA,)
    assert result.checked == 0
    assert runtime._pool == {}


async def test_a_token_below_the_floor_stays_in_the_pool_and_alerts_later(
    database,
) -> None:
    """The point of the pool: $5,000 of volume does not exist at second one."""

    store = TractionStore(database)
    market = {ALPHA: market_row(market_cap_usd=Decimal("900"), volume_usd=Decimal("10"))}
    runtime, surface = build_runtime(store, market=market)
    await runtime.observe_creation(
        mint=ALPHA, launchpad=PUMP, chain_created_at=NOW, now=NOW
    )
    first = await runtime.run_pass(now=NOW + 5)
    assert first.qualified == ()
    assert first.pool_size == 1
    assert surface.sent == []

    market[ALPHA] = market_row()
    second = await runtime.run_pass(now=NOW + 10)
    assert second.alerted == (ALPHA,)
    await drain_enrichment(runtime)


async def test_the_hourly_cap_bounds_the_lane(database) -> None:
    """The lane is pingable, so its volume is bounded by a hard budget."""

    store = TractionStore(database)
    mints = [mint(chr(ord("d") + index)) for index in range(4)]
    runtime, surface = build_runtime(
        store,
        market={name: market_row() for name in mints},
        max_alerts_per_hour=2,
    )
    for name in mints:
        await runtime.observe_creation(
            mint=name, launchpad=PUMP, chain_created_at=NOW, now=NOW
        )
    result = await runtime.run_pass(now=NOW + 5)
    assert len(result.qualified) == 4
    assert len(result.alerted) == 2
    assert runtime.rate_limited == 2
    await drain_enrichment(runtime)


async def test_the_sweep_never_exceeds_its_provider_read_budget(database) -> None:
    """The operator's instruction was explicit: no unattended credit burn.

    A launch storm filling the pool would otherwise issue one request per mint
    per recheck interval -- 1,800 a minute at a 600-mint pool -- against a public
    endpoint documented at 300. The overflow waits for the next sweep rather
    than being dropped, and the counter says how much waited.
    """

    store = TractionStore(database)
    mints = [mint(chr(ord("d") + index)) for index in range(20)]
    reads: list[int] = []

    async def counting_reader(batch):
        reads.append(len(batch))
        return {}

    runtime, _ = build_runtime(store, max_reads_per_minute=6)
    runtime._market_reader = counting_reader
    for name in mints:
        await runtime.observe_creation(
            mint=name, launchpad=PUMP, chain_created_at=NOW, now=NOW
        )

    first = await runtime.run_pass(now=NOW + 1)
    assert first.checked == 6
    assert sum(reads) == 6
    assert runtime.reads_spent == 6
    assert runtime.reads_deferred_for_budget == 14
    assert first.pool_size == 20  # nothing was dropped

    # Still inside the same minute: the budget is spent, so nothing is read.
    second = await runtime.run_pass(now=NOW + 2)
    assert second.checked == 0
    assert sum(reads) == 6

    # The window rolls and the deferred mints get their turn.
    third = await runtime.run_pass(now=NOW + 61)
    assert third.checked == 6
    assert sum(reads) == 12


async def test_a_mint_is_not_re_read_faster_than_the_providers_own_cache(
    database,
) -> None:
    """Below the cache TTL the provider returns the identical bytes.

    So a faster sweep buys no freshness, spends budget, and fills the momentum
    block with duplicate readings that make a flat token look repeatedly
    measured.
    """

    store = TractionStore(database)
    reads: list[str] = []

    async def counting_reader(batch):
        reads.extend(batch)
        return {}

    runtime, _ = build_runtime(store, recheck_seconds=20)
    runtime._market_reader = counting_reader
    await runtime.observe_creation(
        mint=ALPHA, launchpad=PUMP, chain_created_at=NOW, now=NOW
    )

    assert (await runtime.run_pass(now=NOW + 1)).checked == 1
    assert reads == [ALPHA]
    # Five seconds later the sweep wakes, finds nothing due, and spends nothing.
    assert (await runtime.run_pass(now=NOW + 6)).checked == 0
    assert reads == [ALPHA]
    # Past the cache TTL it is read again.
    assert (await runtime.run_pass(now=NOW + 22)).checked == 1
    assert reads == [ALPHA, ALPHA]
    entry = runtime._pool[ALPHA]
    assert len(entry.readings) == 2


async def test_end_to_end_latency_is_anchored_to_the_on_chain_timestamp(
    database,
) -> None:
    """The whole lane exists to make this number small, so it must be real.

    Both timestamps come from the same injected clock; mixing an injected clock
    with ``time.time()`` here produced a p50 of 1.7 billion seconds before it
    was fixed, which is how the bug was found.
    """

    store = TractionStore(database)
    runtime, _ = build_runtime(store, market={ALPHA: market_row()})
    await runtime.observe_creation(
        mint=ALPHA, launchpad=PUMP, chain_created_at=NOW - 90, now=NOW - 88
    )
    await runtime.run_pass(now=NOW - 80)
    await drain_enrichment(runtime)

    report = await runtime.latency_report()
    stages = report.to_json()["stages"]
    assert stages[STAGE_CREATION_TO_DETECTION]["p50_seconds"] == "2"
    assert stages[STAGE_DETECTION_TO_ALERT]["p50_seconds"] == "8"
    assert stages[STAGE_CREATION_TO_ALERT]["p50_seconds"] == "10"
    # Sanity: a wall-clock leak would make this astronomically large.
    assert int(stages[STAGE_CREATION_TO_ALERT]["max_seconds"]) < 3_600


async def test_a_provider_dropping_a_field_does_not_lose_a_known_fact() -> None:
    """A token that already met the volume floor must not look like it never did."""

    known = observation()
    merged = _merge_observation(known, {"market_cap_usd": Decimal("15000")})
    assert merged.market_cap_usd == Decimal("15000")
    assert merged.volume_usd == known.volume_usd
    assert merged.x_link == known.x_link
    assert merged.symbol == known.symbol


async def test_the_x_link_history_answers_reuse_without_a_provider_call(store) -> None:
    """Our own table, one indexed query, no credit spent."""

    shared = assess_link("https://x.com/shared")
    for name in (ALPHA, BRAVO, CHARLIE):
        await store.note_x_link(name, shared, at=NOW)

    report = await store.x_link_reuse(ALPHA, shared, now=NOW + 10)
    assert set(report.other_mints) == {BRAVO, CHARLIE}
    assert ALPHA not in report.other_mints  # never reported as reusing its own link
    assert report.severity == "SEEN_ON_SEVERAL_MINTS"


async def test_reuse_is_windowed_so_an_old_association_is_not_todays_signal(
    store,
) -> None:
    shared = assess_link("https://x.com/shared")
    await store.note_x_link(BRAVO, shared, at=NOW - 30 * 86_400)
    await store.note_x_link(ALPHA, shared, at=NOW)
    recent = await store.x_link_reuse(
        ALPHA, shared, window_seconds=7 * 86_400, now=NOW
    )
    assert recent.other_mints == ()
    wide = await store.x_link_reuse(ALPHA, shared, window_seconds=90 * 86_400, now=NOW)
    assert wide.other_mints == (BRAVO,)


async def test_the_same_tweet_on_two_mints_is_reported(store) -> None:
    """Reusing one tweet for its engagement is the flagged case."""

    tweet = assess_link("https://x.com/someone/status/999")
    await store.note_x_link(BRAVO, tweet, at=NOW)
    await store.note_x_link(ALPHA, tweet, at=NOW)
    report = await store.x_link_reuse(ALPHA, tweet, now=NOW)
    assert report.tweet_other_mints == (BRAVO,)
    assert report.reused


async def test_rejections_are_recorded_so_thresholds_can_be_tuned_from_evidence(
    database,
) -> None:
    store = TractionStore(database)
    runtime, _ = build_runtime(store)
    await runtime.observe_creation(
        mint=ALPHA, launchpad=PUMP, chain_created_at=NOW - 2_000, now=NOW
    )
    summary = await store.rejection_summary()
    assert summary.get(REASON_AGE) == 1


async def test_the_status_payload_states_what_the_lane_is_not_listening_to(
    database,
) -> None:
    """A silently missing venue looks exactly like a quiet one."""

    store = TractionStore(database)
    runtime, _ = build_runtime(store)
    status = await runtime.status(now=NOW)
    assert status["read_only"] is True
    assert status["launchpads"]["enabled"] == [PUMP]
    assert set(status["launchpads"]["unavailable"]) == {BAGS, "BONK", "LIQUIDAF", HEAVEN}
    assert status["profile"]["safety_gates"].startswith("NONE")
    assert status["max_alerts_per_hour"] == runtime.config.max_alerts_per_hour


async def test_a_disabled_lane_does_nothing_at_all(database) -> None:
    store = TractionStore(database)
    runtime, surface = build_runtime(store, market={ALPHA: market_row()}, enabled=False)
    assert await runtime.observe_creation(
        mint=ALPHA, launchpad=PUMP, chain_created_at=NOW, now=NOW
    ) is False
    result = await runtime.run_pass(now=NOW)
    assert result.error
    assert surface.sent == []
    assert await store.latency_samples() == ()


# ======================================================================
# THE CARD
# ======================================================================
def candidate_for_card(**overrides) -> TractionCandidate:
    obs = observation()
    values = dict(
        observation=obs,
        verdict=verdict_for(obs),
        momentum=build_momentum(
            [
                (NOW - 60, Decimal("9000"), Decimal("5000")),
                (NOW, Decimal("12000"), Decimal("9000")),
            ],
            volume_window_seconds=300,
        ),
        quality=build_quality(obs),
        safety=build_safety(ALPHA),
        x_link=XLinkVerdict(assessment=assess_link(obs.x_link)),
        detected_at=NOW - 118,
        qualified_at=NOW,
        alert_sent_at=NOW,
    )
    values.update(overrides)
    return TractionCandidate(**values)


def test_the_card_keeps_the_three_blocks_as_separate_labelled_fields() -> None:
    spec = build_traction_card(candidate_for_card())
    names = [field.name for field in spec.fields]
    assert any(name.startswith("MOMENTUM") for name in names)
    assert any(name.startswith("QUALITY") for name in names)
    assert any(name.startswith("SAFETY") for name in names)
    # And no combined field claiming to summarise them.
    assert not any("SCORE" in name.upper() for name in names)


def test_the_card_says_the_profile_does_not_filter_on_safety() -> None:
    """The reader must not infer that a shown risk was screened for."""

    spec = build_traction_card(candidate_for_card())
    safety = next(field for field in spec.fields if field.name.startswith("SAFETY"))
    assert "does NOT filter on these" in safety.name


def test_the_card_shows_safety_as_pending_before_enrichment_returns() -> None:
    spec = build_traction_card(candidate_for_card())
    safety = next(field for field in spec.fields if field.name.startswith("SAFETY"))
    assert "pending" in safety.name
    assert "enrichment in flight" in safety.value


def test_the_card_shows_measured_safety_once_enrichment_returned() -> None:
    spec = build_traction_card(
        candidate_for_card(
            safety=build_safety(
                ALPHA, top10_percent=Decimal("62.5"), holder_count=90, enriched_at=NOW
            )
        )
    )
    safety = next(field for field in spec.fields if field.name.startswith("SAFETY"))
    assert "pending" not in safety.name
    assert "62.5%" in safety.value
    assert "unknown" in safety.value  # the metrics that were not measured


def test_the_card_makes_no_profit_claim() -> None:
    """The lane has no forward record for these thresholds yet.

    A confident phrasing would be asserting something nobody has measured, so
    the wording is deliberately observational.
    """

    spec = build_traction_card(candidate_for_card())
    text = " ".join(
        [spec.title, spec.description, spec.footer or ""]
        + [f"{field.name} {field.value}" for field in spec.fields]
    ).lower()
    for phrase in ("guaranteed", "will pump", "buy now", "easy", "safe bet", "moon"):
        assert phrase not in text, phrase
    assert "research candidate" in text
    assert "no outcome is predicted" in text
    assert "read-only" in text


def test_the_card_states_the_migration_side_it_observed() -> None:
    pre = build_traction_card(candidate_for_card())
    assert "PRE-migration" in str([field.value for field in pre.fields])

    post_obs = observation(migration_state=POST_MIGRATION)
    post = build_traction_card(
        candidate_for_card(observation=post_obs, verdict=verdict_for(post_obs))
    )
    assert "POST-migration" in str([field.value for field in post.fields])

    unknown_obs = observation(migration_state=MIGRATION_UNKNOWN)
    unknown = build_traction_card(
        candidate_for_card(observation=unknown_obs, verdict=verdict_for(unknown_obs))
    )
    rendered = str([field.value for field in unknown.fields])
    assert "migration state unknown" in rendered
    # Never rendered as one side or the other when we do not know.
    assert "PRE-migration" not in rendered
    assert "POST-migration" not in rendered


def test_the_card_shows_the_copyable_mint_and_the_navigation_links() -> None:
    spec = build_traction_card(candidate_for_card())
    values = "\n".join(field.value for field in spec.fields)
    assert f"`{ALPHA}`" in values  # copyable
    for label in ("AXIOM", "PADRE", "DEXSCREENER", "SOLSCAN"):
        assert label in values


def test_the_card_reports_its_own_detection_speed() -> None:
    spec = build_traction_card(candidate_for_card())
    speed = next(field for field in spec.fields if field.name == "DETECTION SPEED")
    assert "creation → detection" in speed.value
    assert "creation → this alert" in speed.value


def test_the_card_flags_a_reused_x_link() -> None:
    spec = build_traction_card(
        candidate_for_card(
            x_link=XLinkVerdict(
                assessment=assess_link("https://x.com/shared"),
                reuse=ReuseReport(handle="shared", other_mints=(BRAVO, CHARLIE)),
            )
        )
    )
    x_field = next(field for field in spec.fields if field.name == "X / TWITTER")
    assert "SEEN_ON_SEVERAL_MINTS" in x_field.value


def test_the_card_fits_inside_one_discord_message() -> None:
    spec = build_traction_card(
        candidate_for_card(
            observation=observation(name="T" * 200, symbol="LONGSYMBOL"),
            safety=build_safety(
                ALPHA,
                top10_percent=Decimal("62.5"),
                dev_holding_percent=Decimal("12.1"),
                insider_percent=Decimal("8.4"),
                bundler_percent=Decimal("30.2"),
                holder_count=412,
                developer=DeveloperHistory(
                    wallet=BRAVO, tokens_created=40, graduated=2, collapsed=38
                ),
                notes=("mint authority still live", "freeze authority still live"),
                enriched_at=NOW,
            ),
            x_link=XLinkVerdict(
                assessment=assess_link("https://x.com/shared"),
                reuse=ReuseReport(
                    handle="shared",
                    other_mints=tuple(mint(chr(ord("d") + i)) for i in range(10)),
                ),
            ),
        )
    )
    assert len(build_embed(spec)) <= MESSAGE_EMBED_LIMIT


# ======================================================================
# ARCHITECTURE: properties that must stay impossible
# ======================================================================
TRACTION_PACKAGE_ROOT = Path(inspect.getfile(traction_package)).parent
LANE_MODULES = (runtime_module, store_module, cards)

#: Anything that could place, price, sign or send an order.
EXECUTION_TOKENS = (
    "executor",
    "Executor",
    "Keypair",
    "send_transaction",
    "sendTransaction",
    "place_order",
    "submit_swap",
    "jupiter_swap",
    "sign_transaction",
    "live_execution",
)


def _traction_sources() -> list[tuple[str, str]]:
    sources = [
        (str(path), path.read_text())
        for path in sorted(TRACTION_PACKAGE_ROOT.glob("*.py"))
    ]
    for module in LANE_MODULES:
        path = Path(inspect.getfile(module))
        sources.append((str(path), path.read_text()))
    return sources


def _code_only(source: str) -> str:
    """Executable code with comments and docstrings stripped.

    Prose *about* execution is how the constraint is documented -- this lane's
    own docstrings say it must never trade.  What the invariant forbids is a
    real reference, so the docstrings are removed rather than the check being
    weakened to tolerate them.
    """

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_the_traction_lane_has_no_way_to_spend_sol() -> None:
    """'Read-only' is a structural property here, not a promise in a docstring."""

    offenders: list[str] = []
    for path, source in _traction_sources():
        for token in EXECUTION_TOKENS:
            if token in _code_only(source):
                offenders.append(f"{path}: {token}")
    assert not offenders, f"the Early Traction lane referenced execution: {offenders}"


def test_the_lane_never_imports_a_trading_module() -> None:
    forbidden = {"executor", "execution", "shadow_runtime", "lab_runtime", "wallet"}
    offenders: list[str] = []
    for path, source in _traction_sources():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[-1] in forbidden:
                    offenders.append(f"{path}: from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[-1] in forbidden:
                        offenders.append(f"{path}: import {alias.name}")
    assert not offenders, offenders


def test_the_fast_path_makes_no_network_call() -> None:
    """Everything in the pure package must run in memory.

    The cheap checks are evaluated for every new mint, so an HTTP call hidden in
    one of them would put a round trip on the hot path and, worse, spend a
    provider credit per launch.
    """

    forbidden = ("aiohttp", "requests", "httpx", "urlopen", "websockets")
    offenders: list[str] = []
    for path in sorted(TRACTION_PACKAGE_ROOT.glob("*.py")):
        code = _code_only(path.read_text())
        for token in forbidden:
            if token in code:
                offenders.append(f"{path}: {token}")
    assert not offenders, offenders


def test_the_pure_package_touches_no_database() -> None:
    """Same split as the rest of the repository: SQL lives in the store."""

    offenders: list[str] = []
    for path in sorted(TRACTION_PACKAGE_ROOT.glob("*.py")):
        code = _code_only(path.read_text())
        for token in ("SELECT ", "INSERT ", "UPDATE ", "aiosqlite", "Database"):
            if token in code:
                offenders.append(f"{path}: {token}")
    assert not offenders, offenders


def test_the_traction_card_publishes_through_the_single_choke_point() -> None:
    """One door to Discord, so one place enforces the publication guard."""

    engine_source = (TRACTION_PACKAGE_ROOT.parent / "engine.py").read_text()
    assert engine_source.count("notifier.on_fast_alert(") == 1

    start = engine_source.index("async def _publish_traction(")
    body = engine_source[start : engine_source.index("async def _edit_traction(")]
    assert "_dispatch_card(" in body


def test_the_enrichment_edit_can_never_send_a_second_message() -> None:
    """A second card for one token is exactly the noise this lane avoids."""

    engine_source = (TRACTION_PACKAGE_ROOT.parent / "engine.py").read_text()
    start = engine_source.index("async def _edit_traction(")
    body = engine_source[start : start + 1_200]
    assert "on_fast_alert_enrichment(" in body
    assert "on_fast_alert(" not in body
    assert "_dispatch_card(" not in body


def test_the_schema_additions_alter_no_existing_table() -> None:
    """A new lane must not migrate another lane's columns to suit itself."""

    source = (TRACTION_PACKAGE_ROOT.parent / "database.py").read_text()
    start = source.index("EARLY TRACTION (v2.56)")
    end = source.index("_migrate_pump_launch_status_constraint", start)
    block = source[start:end]
    assert "ALTER TABLE" not in block
    assert "DROP TABLE" not in block
    assert "DROP COLUMN" not in block
    for table in (
        "traction_alerts",
        "traction_x_links",
        "traction_latency",
        "traction_rejections",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in block


def test_the_lane_starts_no_parallel_forward_observation_history() -> None:
    """It writes into runner_candidates, which already has the horizons."""

    source = Path(inspect.getfile(store_module)).read_text()
    assert "traction_outcomes" not in source
    assert "CREATE TABLE" not in source
    # And this file no longer writes another lane's typed table by hand: the row
    # goes through that lane's own writer, from the engine.
    assert "INSERT OR IGNORE INTO runner_candidates" not in source
    assert "INSERT INTO runner_candidates" not in source

    engine_source = (TRACTION_PACKAGE_ROOT.parent / "engine.py").read_text()
    start = engine_source.index("async def _traction_register_forward(")
    body = engine_source[start : engine_source.index("async def _publish_traction(")]
    assert "store_runner_candidate(" in body
    assert "runner_candidate_to_json(" in body
    # Raw SQL against that table from this lane is the bug this replaced.
    assert "INSERT" not in body


def test_the_alert_class_is_pingable_and_urgent_together() -> None:
    """A pingable class that is not urgent pings from the quiet lane."""

    assert fa.EARLY_TRACTION_ALERT in fa.ALERT_CLASSES
    assert fa.EARLY_TRACTION_ALERT in fa.URGENT_CLASSES
    assert fa.EARLY_TRACTION_ALERT in fa.PINGABLE
    assert set(fa.PINGABLE) <= set(fa.URGENT_CLASSES)


def test_every_threshold_the_operator_named_is_an_environment_variable() -> None:
    """'Every threshold an env var' was explicit, so none may be hardcoded."""

    source = (TRACTION_PACKAGE_ROOT.parent / "config.py").read_text()
    for variable in (
        "TRACTION_ENABLED",
        "TRACTION_LAUNCHPADS",
        "TRACTION_MAX_AGE_SECONDS",
        "TRACTION_MIN_MARKET_CAP_USD",
        "TRACTION_MIN_VOLUME_USD",
        "TRACTION_VOLUME_WINDOW",
        "TRACTION_REQUIRE_X_LINK",
        "TRACTION_REQUIRE_DEX_PAID",
        "TRACTION_INCLUDE_PRE_MIGRATION",
        "TRACTION_INCLUDE_POST_MIGRATION",
        "TRACTION_INCLUDE_INK",
        "TRACTION_POLL_SECONDS",
        "TRACTION_MAX_ALERTS_PER_HOUR",
        "TRACTION_MAX_SEND_ATTEMPTS",
        "TRACTION_X_REUSE_WINDOW_SECONDS",
        "TRACTION_ENRICH_TIMEOUT_SECONDS",
    ):
        assert variable in source, variable


# ======================================================================
# THE COMMANDS
# ======================================================================
def test_the_traction_commands_live_in_their_own_group() -> None:
    """``/fomo`` is at Discord's 25-child ceiling, so this lane gets its own."""

    from smart_money_bot.bot import FomoCommands, TractionCommands

    fomo = {command.name for command in FomoCommands.__cog_app_commands__}
    traction = {command.name for command in TractionCommands.__cog_app_commands__}
    assert len(fomo) <= 24, "leave a child slot free for the next release"
    assert len(traction) <= 24
    assert traction == {"status", "latency", "tracked"}
    # Separate groups, so a shared child name is not a collision: /fomo status
    # and /traction status are different commands.
    assert TractionCommands.__cog_group_name__ == "traction"


async def test_the_status_command_names_the_launchpads_it_cannot_listen_to(
    database,
) -> None:
    """The operator must be able to see why a quiet lane is quiet."""

    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from smart_money_bot.bot import TractionCommands

    store = TractionStore(database)
    runtime, _ = build_runtime(store)
    status = await runtime.status(now=NOW)
    cog = TractionCommands.__new__(TractionCommands)
    cog.bot = SimpleNamespace(
        engine=SimpleNamespace(traction_status=AsyncMock(return_value=status)),
        settings=SimpleNamespace(),
    )
    cog._require_admin = AsyncMock(return_value=True)
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
        user=SimpleNamespace(id=1),
    )
    await TractionCommands.status.callback(cog, interaction)
    embed = interaction.edit_original_response.await_args.kwargs["embed"]
    rendered = "\n".join(
        f"{field.name} {field.value}" for field in embed.fields
    )
    assert "LAUNCHPADS LISTENING" in rendered
    assert PUMP in rendered
    assert "LAUNCHPADS NOT LISTENING" in rendered
    assert "safety gates" in rendered
    assert "NONE" in rendered
    assert "no buy, sell, signing or sol spend" in embed.description.lower()


async def test_the_latency_command_reports_the_samples_it_excluded(database) -> None:
    """A latency figure that hid its exclusions would be the old p90 bug again."""

    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from smart_money_bot.bot import TractionCommands

    store = TractionStore(database)
    await store.record_detection(
        mint=ALPHA, chain_created_at=NOW - 5, detected_at=NOW, source="creation_stream"
    )
    await store.record_alert_sent(ALPHA, at=NOW + 4)
    await store.record_detection(
        mint=BRAVO, chain_created_at=None, detected_at=NOW, source="creation_stream"
    )
    runtime, _ = build_runtime(store)

    cog = TractionCommands.__new__(TractionCommands)
    cog.bot = SimpleNamespace(
        engine=SimpleNamespace(
            traction_latency=AsyncMock(
                return_value=(await runtime.latency_report()).to_json()
            )
        ),
        settings=SimpleNamespace(),
    )
    cog._require_admin = AsyncMock(return_value=True)
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
        user=SimpleNamespace(id=1),
    )
    await TractionCommands.latency.callback(cog, interaction, days=1)
    embed = interaction.edit_original_response.await_args.kwargs["embed"]
    rendered = "\n".join(f"{field.name} {field.value}" for field in embed.fields)
    assert STAGE_CREATION_TO_ALERT in rendered
    assert "excluded (no creation time): 1" in rendered
    assert "on-chain creation timestamp" in embed.description


async def test_the_tracked_command_says_zero_horizons_rather_than_implying_a_result(
    database,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from smart_money_bot.bot import TractionCommands

    store = TractionStore(database)
    runtime, _ = build_runtime(store, market={ALPHA: market_row()})
    await runtime.observe_creation(
        mint=ALPHA, launchpad=PUMP, chain_created_at=NOW - 60, now=NOW
    )
    await runtime.run_pass(now=NOW + 1)
    await drain_enrichment(runtime)

    cog = TractionCommands.__new__(TractionCommands)
    cog.bot = SimpleNamespace(
        engine=SimpleNamespace(
            traction_tracked=AsyncMock(return_value=await store.tracked_alert_rows())
        ),
        settings=SimpleNamespace(),
    )
    cog._require_admin = AsyncMock(return_value=True)
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
        user=SimpleNamespace(id=1),
    )
    await TractionCommands.tracked.callback(cog, interaction, limit=5)
    embed = interaction.edit_original_response.await_args.kwargs["embed"]
    rendered = "\n".join(f"{field.name} {field.value}" for field in embed.fields)
    assert ALPHA in rendered
    assert "horizons observed: 0" in rendered
    assert "honest state" in embed.description
