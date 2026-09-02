"""Regression suite for v2.47 — the two same-name $SNP500 cards.

The production failure, in the operator's own words: *"I'm getting recommended
fake coins with this bot."*  Two alerts arrived minutes apart, both titled
**Sock and Pussy 500 · $SNP500**, both discovered via GMGN Trending, and both
printing ``Symbol collision: NO`` on the card.  They were two different mints.
One went to $789K; the other was riding its name.

Three separate defects produced that, and each has its own section below.

**The collision check was blind.**  ``known_symbols()`` read ``pump_tokens`` and
``runner_candidates``.  A GMGN-discovered token is written to neither, so the
card answered "NO" to a question it could not see.  That is not a missing
feature; it is a lie the card told the operator.

**Nothing ranked the two against each other.**  Even with the collision known,
the bot had no way to say which mint came first or which had the money, so both
were presented identically and the operator was left to guess.

**The lane was too slow to matter.**  ``gmgn_enrichment_per_scan`` defaulted to
6 and gated *evaluation*, not just GMGN's expensive per-token calls — six looks
at roughly 255 candidates on a 45-second poll.  Production showed a mint first
seen at $9.87K that was not evaluated until $40.71K.  The operator asked to be
able to buy "right there" at first sight; instead the alert arrived after a
four-times move.

Nothing in this suite touches a network, a wallet or a signer.
"""

from __future__ import annotations

import asyncio
import inspect
from decimal import Decimal
from types import SimpleNamespace

import pytest

import smart_money_bot.fast_alerts as fa
from smart_money_bot.database import Database
from smart_money_bot.lab.clone import (
    AMBIGUOUS,
    ORIGINAL,
    SUSPECTED_CLONE,
    UNIQUE,
    CloneConfig,
    CloneVerdict,
    TokenFacts,
    classify_clone,
    group_by_identity,
    normalise,
)
from smart_money_bot.lab.early import (
    HUMAN_WHY,
    WHY_NAME_COLLISION,
    WHY_SUSPECTED_CLONE,
    WHY_THIN_QUALITY,
)
from smart_money_bot.lab.shadow import FAMILY_GMGN_TRENDING
from smart_money_bot.lab.tokenquality import (
    DEFAULT_QUALITY_CONFIG,
    QualityScore,
    inverse_ramp,
    ramp,
    rank_candidates,
    score_quality,
)

# The two mints exactly as production reported them.
COPY_MINT = "J8GLnJ7Qk2m5t9WcQeF3bXn4Zr8vH1sYp6uJdLxAKpump"
REAL_MINT = "3DV5zV8sQhRtYwXnLp2CkAaB7mNfE9uJqZrGdTxfXUjp"


def _real(**overrides) -> TokenFacts:
    """The mint that went on to $789K, as it looked at alert time."""

    values = dict(
        mint=REAL_MINT,
        name="Sock And Pussy 500",
        symbol="$SNP-500",
        created_at=1_000_000,
        first_seen_at=1_000_000,
        age_seconds=420,
        liquidity_usd=Decimal("15180"),
        volume_usd=Decimal("64000"),
        market_cap_usd=Decimal("40710"),
        holder_count=520,
        buys=450,
        sells=438,
        total_fee_sol=Decimal("9.6"),
        top10_holder_rate=Decimal("0.19"),
        dev_hold_rate=Decimal("0.02"),
        bundler_rate=Decimal("0.08"),
        sniper_hold_rate=Decimal("0.11"),
        insider_rate=Decimal("0.07"),
    )
    values.update(overrides)
    return TokenFacts(**values)


def _copy(**overrides) -> TokenFacts:
    """The one wearing its name, two minutes later."""

    values = dict(
        mint=COPY_MINT,
        name="Sock and Pussy 500",
        symbol="SNP500",
        created_at=1_000_120,
        first_seen_at=1_000_120,
        age_seconds=300,
        liquidity_usd=Decimal("12080"),
        volume_usd=Decimal("21000"),
        market_cap_usd=Decimal("27150"),
        holder_count=180,
        buys=399,
        sells=334,
        total_fee_sol=Decimal("0.9"),
        top10_holder_rate=Decimal("0.42"),
        dev_hold_rate=Decimal("0.05"),
        bundler_rate=Decimal("0.22"),
        sniper_hold_rate=Decimal("0.28"),
        insider_rate=Decimal("0.18"),
    )
    values.update(overrides)
    return TokenFacts(**values)


# ---------------------------------------------------------------------------
# 1. Grouping.  A copy is never a byte-for-byte copy.
# ---------------------------------------------------------------------------


def test_case_spacing_and_punctuation_do_not_hide_a_collision() -> None:
    # "Sock and Pussy 500 / $SNP500" against "Sock And Pussy 500 / $SNP-500".
    # If the fold missed either difference the two would never be compared and
    # the operator would get both cards with no warning on either.
    assert _real().identity_key == _copy().identity_key


def test_the_fold_is_never_used_to_resolve_a_token() -> None:
    # The folded key exists to *group* mints for comparison.  A resolver keyed
    # by it would be the v2.43.1 substitution bug rebuilt, so the module must
    # not contain a lookup from name to mint.
    import smart_money_bot.lab.clone as clone_module

    source = inspect.getsource(clone_module)
    assert "def resolve" not in source
    assert "by_symbol" not in source
    # Every entry point is handed the facts of one exact mint and answers about
    # that mint.  None of them accepts a bare name to look up.
    assert list(inspect.signature(classify_clone).parameters) == ["subject", "peers", "config"]
    assert classify_clone(_real(), [_copy()]).mint == REAL_MINT


def test_normalise_folds_only_for_comparison() -> None:
    assert normalise("Sock and Pussy 500") == normalise("SOCK AND PUSSY-500!")
    assert normalise("$SNP500") == normalise("snp 500")
    assert normalise(None) == ""
    # Different tokens still fold apart.  A fold that collapsed everything
    # would report a collision on every card, which is the same as reporting
    # none.
    assert normalise("SNP500") != normalise("SNP5000")


def test_group_by_identity_returns_only_actual_collisions() -> None:
    lonely = TokenFacts(mint="Solo", name="Only One", symbol="ONE")
    grouped = group_by_identity([_real(), _copy(), lonely])
    assert len(grouped) == 1
    (members,) = grouped.values()
    # Oldest first, so the panel reads in the order the tokens actually
    # happened rather than in feed order.
    assert [item.mint for item in members] == [REAL_MINT, COPY_MINT]


# ---------------------------------------------------------------------------
# 2. The verdict.  Which of the two is the operator looking at?
# ---------------------------------------------------------------------------


def test_the_original_is_named_and_may_still_ping() -> None:
    verdict = classify_clone(_real(), [_copy()])
    assert verdict.verdict == ORIGINAL
    assert verdict.may_ping is True
    assert verdict.collision is True
    assert verdict.peers == (COPY_MINT,)
    assert verdict.leader_mint == REAL_MINT


def test_the_copy_is_named_and_never_pings() -> None:
    verdict = classify_clone(_copy(), [_real()])
    assert verdict.verdict == SUSPECTED_CLONE
    assert verdict.may_ping is False
    assert verdict.suspected_clone is True
    assert verdict.leader_mint == REAL_MINT
    assert "was using this name first" in verdict.reasons[0]


def test_the_copy_is_still_shown_with_a_warning_rather_than_hidden() -> None:
    # Hiding it would leave the operator exactly as blind as the two
    # "Symbol collision: NO" cards did.  They must be able to see that two
    # tokens are wearing one name.
    warning = classify_clone(_copy(), [_real()]).warning_line()
    assert "SUSPECTED COPY" in warning
    assert REAL_MINT[:8] in warning


def test_a_token_nobody_is_imitating_is_unaffected() -> None:
    verdict = classify_clone(_real(), [TokenFacts(mint="Other", name="Other", symbol="OTH")])
    assert verdict.verdict == UNIQUE
    assert verdict.may_ping is True
    assert verdict.collision is False
    assert verdict.warning_line() == ""


def test_two_launches_too_close_together_are_called_unresolved_not_guessed() -> None:
    # At sixty seconds old two same-name launches really can be
    # indistinguishable.  Saying so is a real answer; a confident coin flip is
    # not, and neither of them may interrupt anybody.
    left = _real(created_at=1_000_000, first_seen_at=1_000_000)
    right = _copy(
        created_at=1_000_010,
        first_seen_at=1_000_010,
        liquidity_usd=Decimal("15000"),
        volume_usd=Decimal("62000"),
        total_fee_sol=Decimal("9.4"),
        age_seconds=410,
    )
    verdict = classify_clone(left, [right])
    assert verdict.verdict == AMBIGUOUS
    assert verdict.may_ping is False
    assert "NAME COLLISION" in verdict.warning_line()


def test_being_ten_seconds_earlier_is_not_evidence_of_anything() -> None:
    config = CloneConfig()
    right = _copy(created_at=1_000_000 + config.older_by_seconds - 5)
    verdict = classify_clone(right, [_real()])
    # Ten seconds apart is two launches happening at once, not a copy.  The
    # later one is not condemned for it.
    assert verdict.verdict != SUSPECTED_CLONE


def test_order_alone_condemns_a_later_token_even_when_it_looks_stronger() -> None:
    # The dangerous case: the copy pumps harder than the original for a few
    # minutes.  Coming second with someone else's name is the definition of the
    # thing the operator asked us to stop recommending, so depth cannot buy it
    # a ping.
    loud_copy = _copy(
        liquidity_usd=Decimal("90000"),
        volume_usd=Decimal("400000"),
        total_fee_sol=Decimal("40"),
    )
    verdict = classify_clone(loud_copy, [_real()])
    assert verdict.verdict == SUSPECTED_CLONE
    assert verdict.may_ping is False


def test_a_missing_measurement_never_counts_as_evidence() -> None:
    # A provider that did not answer must not read as a token with no
    # liquidity.  Comparing against None would hand the subject a free win.
    blind_peer = TokenFacts(
        mint=COPY_MINT,
        name="Sock and Pussy 500",
        symbol="SNP500",
        created_at=1_000_120,
    )
    verdict = classify_clone(
        _real(liquidity_usd=None, volume_usd=None, total_fee_sol=None), [blind_peer]
    )
    # Order is the only thing known, and one line of evidence is not enough to
    # name an original.
    assert verdict.verdict == AMBIGUOUS
    assert verdict.evidence_count == 1


def test_a_nameless_token_collides_with_nothing() -> None:
    verdict = classify_clone(TokenFacts(mint="Blank"), [TokenFacts(mint="AlsoBlank")])
    assert verdict.verdict == UNIQUE


def test_the_subjects_own_mint_is_never_its_own_peer() -> None:
    verdict = classify_clone(_real(), [_real(), _copy()])
    assert verdict.peers == (COPY_MINT,)


def test_birth_prefers_chain_creation_over_when_we_happened_to_look() -> None:
    # We noticed the original late and the copy immediately.  Ranking on
    # observation time would name the copy the original, which is the exact
    # inversion this module exists to prevent.
    original = _real(created_at=1_000_000, first_seen_at=1_000_900)
    copy = _copy(created_at=1_000_120, first_seen_at=1_000_130)
    assert original.birth == 1_000_000
    assert classify_clone(copy, [original]).verdict == SUSPECTED_CLONE
    assert classify_clone(original, [copy]).verdict == ORIGINAL


def test_fee_velocity_is_a_rate_and_not_a_total() -> None:
    # 0.5 SOL in two minutes and 0.5 SOL in four hours are different tokens
    # wearing the same number.
    fast = TokenFacts(mint="a", total_fee_sol=Decimal("0.5"), age_seconds=120)
    slow = TokenFacts(mint="b", total_fee_sol=Decimal("0.5"), age_seconds=14_400)
    assert fast.fee_velocity_sol_per_minute > slow.fee_velocity_sol_per_minute
    assert TokenFacts(mint="c", total_fee_sol=Decimal("1")).fee_velocity_sol_per_minute is None
    assert TokenFacts(mint="d", age_seconds=60).fee_velocity_sol_per_minute is None


def test_a_verdict_serialises_everything_the_card_needs() -> None:
    payload = classify_clone(_copy(), [_real()]).to_json()
    assert payload["verdict"] == SUSPECTED_CLONE
    assert payload["may_ping"] is False
    assert payload["peers"] == [REAL_MINT]
    assert payload["warning"]


# ---------------------------------------------------------------------------
# 3. Real money.  "You can tell when there's a fake coin."
# ---------------------------------------------------------------------------


def test_the_original_scores_materially_higher_than_the_copy() -> None:
    good = score_quality(_real())
    bad = score_quality(_copy())
    assert good.score > bad.score
    # Fee velocity is the separation the operator described — 9.6 SOL against
    # 0.9 on tokens of similar age — so it must be the dominant component.
    components = dict(good.components)
    assert Decimal(components["fee velocity"]) > Decimal(dict(bad.components)["fee velocity"])


def test_every_bar_is_a_ramp_and_not_a_cliff() -> None:
    # A token with 47 holders instead of 50 is not categorically different from
    # one that clears.  Scoring must move continuously, or the bot inherits the
    # threshold cliff the operator's manual filter has.
    floor, target = Decimal("20"), Decimal("120")
    assert ramp(Decimal("19"), floor, target) == 0
    assert ramp(Decimal("120"), floor, target) == 1
    middle = ramp(Decimal("70"), floor, target)
    assert 0 < middle < 1
    assert ramp(Decimal("71"), floor, target) > middle


def test_a_risk_rate_ramps_the_other_way() -> None:
    assert inverse_ramp(Decimal("0.10"), Decimal("0.25"), Decimal("0.55")) == 1
    assert inverse_ramp(Decimal("0.90"), Decimal("0.25"), Decimal("0.55")) == 0
    assert 0 < inverse_ramp(Decimal("0.40"), Decimal("0.25"), Decimal("0.55")) < 1


def test_an_unmeasured_field_scores_zero_but_says_so() -> None:
    thin = TokenFacts(mint="x", name="X", symbol="X", liquidity_usd=Decimal("20000"))
    score = score_quality(thin)
    assert score.score > 0
    # Most of the picture was never observed, so the score does not get to
    # claim confidence.  A token must not look strong by being unknown.
    assert score.measured_fraction < DEFAULT_QUALITY_CONFIG.min_measured_fraction
    assert score.confident() is False
    assert score.strong() is False


def test_ownership_risk_is_one_family_so_it_cannot_outvote_real_demand() -> None:
    # Five concentration rates counted separately would swamp fees, liquidity,
    # volume and holders combined, and a real runner with a chunky top-10 would
    # be scored as a rug.
    concentrated = _real(
        top10_holder_rate=Decimal("0.60"),
        dev_hold_rate=Decimal("0.20"),
        bundler_rate=Decimal("0.50"),
        sniper_hold_rate=Decimal("0.50"),
        insider_rate=Decimal("0.40"),
    )
    score = score_quality(concentrated)
    lost = Decimal(dict(score_quality(_real()).components)["ownership"]) - Decimal(
        dict(score.components)["ownership"]
    )
    assert lost <= DEFAULT_QUALITY_CONFIG.weight_ownership
    # It still hurts, and the card still says why.
    assert score.score < score_quality(_real()).score
    assert any("top 10" in item for item in score.concerns)


def test_only_a_token_we_could_see_clearly_is_ever_called_weak() -> None:
    # A DEX-only snapshot cannot see fees, holders or ownership.  Treating "we
    # could not look" as "there is nothing there" would silently switch off the
    # entire Pump lane.
    dex_only = TokenFacts(
        mint="x",
        name="X",
        symbol="X",
        liquidity_usd=Decimal("100"),
        volume_usd=Decimal("10"),
        buys=1,
        sells=0,
    )
    assert dex_only is not None
    assert score_quality(dex_only).weak() is False

    measured_and_dead = TokenFacts(
        mint="y",
        name="Y",
        symbol="Y",
        age_seconds=3_000,
        liquidity_usd=Decimal("900"),
        volume_usd=Decimal("120"),
        market_cap_usd=Decimal("9000"),
        holder_count=6,
        buys=3,
        sells=2,
        total_fee_sol=Decimal("0.01"),
        top10_holder_rate=Decimal("0.90"),
        dev_hold_rate=Decimal("0.40"),
        bundler_rate=Decimal("0.70"),
        sniper_hold_rate=Decimal("0.60"),
        insider_rate=Decimal("0.55"),
    )
    dead = score_quality(measured_and_dead)
    assert dead.confident() is True
    assert dead.weak() is True


def test_ranking_puts_the_strongest_candidate_first() -> None:
    # The lateness, in one line: feed order put a real runner behind hundreds of
    # dead launches, so it was not evaluated until the move was over.
    dead = [
        TokenFacts(
            mint=f"dead{index}",
            name=f"Dead {index}",
            symbol=f"D{index}",
            age_seconds=1_200,
            liquidity_usd=Decimal("500"),
            volume_usd=Decimal("50"),
            holder_count=3,
            buys=1,
            sells=0,
            total_fee_sol=Decimal("0.001"),
        )
        for index in range(200)
    ]
    ranked = rank_candidates([*dead, _real()])
    assert ranked[0][0].mint == REAL_MINT


def test_ranking_is_stable_for_equal_scores() -> None:
    left = TokenFacts(mint="aaa", name="N", symbol="N")
    right = TokenFacts(mint="bbb", name="N", symbol="N")
    assert [item.mint for item, _ in rank_candidates([right, left])] == ["aaa", "bbb"]


def test_a_quality_score_serialises_for_the_card() -> None:
    payload = score_quality(_real()).to_json()
    assert payload["mint"] == REAL_MINT
    assert payload["confident"] is True
    assert payload["weak"] is False


# ---------------------------------------------------------------------------
# 4. The card.  What the operator actually sees.
# ---------------------------------------------------------------------------


def _verdict(tier: str = "EARLY_RUNNER") -> SimpleNamespace:
    return SimpleNamespace(
        tier=tier,
        label="🚨 EARLY RUNNER",
        score=Decimal("80"),
        late=False,
        visible=True,
        edge_state="AVAILABLE",
        evidence_categories=("ORGANIC_FLOW",),
        reasons=("flow accelerating",),
        why_not_pinged=(),
        impulse=None,
    )


def _card(**overrides):
    values = dict(
        mint=REAL_MINT,
        name="Sock And Pussy 500",
        symbol="SNP500",
        fomo_url="https://fomo.example/x",
        verdict=_verdict(),
        age_seconds=420,
        first_seen_seconds_ago=30,
        first_seen_market_cap_usd=Decimal("9870"),
        alert_market_cap_usd=Decimal("11000"),
        current_market_cap_usd=Decimal("11000"),
        liquidity_usd=Decimal("15180"),
        buys=450,
        sells=438,
    )
    values.update(overrides)
    return fa.build_early_alert(**values)


def test_without_a_verdict_the_card_behaves_exactly_as_before() -> None:
    # The new arguments are optional, and every existing caller must keep the
    # behaviour it had.
    alert = _card()
    assert alert.ping is True
    assert alert.lane == fa.LANE_URGENT


def test_a_suspected_copy_publishes_but_never_pings() -> None:
    alert = _card(clone_verdict=classify_clone(_copy(), [_real()]), quality=score_quality(_copy()))
    assert alert.ping is False
    assert alert.lane == fa.LANE_RADAR
    # And it says why, on the card, where the operator is looking.
    body = "\n".join(field.value for field in alert.spec.fields)
    assert "ANOTHER TOKEN USES THIS NAME" in "\n".join(
        field.name for field in alert.spec.fields
    )
    assert "SUSPECTED COPY" in body
    assert REAL_MINT[:8] in body


def test_an_unresolved_collision_also_withholds_the_ping() -> None:
    left = _real()
    right = _copy(
        created_at=1_000_010,
        liquidity_usd=Decimal("15000"),
        volume_usd=Decimal("62000"),
        total_fee_sol=Decimal("9.4"),
    )
    alert = _card(clone_verdict=classify_clone(left, [right]))
    assert alert.ping is False


def test_the_original_still_pings_even_though_a_copy_exists() -> None:
    # Being imitated is not a reason to lose the alert.  The operator asked to
    # stop being shown copies, not to stop being shown the real thing.
    alert = _card(clone_verdict=classify_clone(_real(), [_copy()]), quality=score_quality(_real()))
    assert alert.ping is True
    assert alert.lane == fa.LANE_URGENT
    names = "\n".join(field.name for field in alert.spec.fields)
    assert "ANOTHER TOKEN USES THIS NAME" in names


def test_a_measurably_thin_token_does_not_ping() -> None:
    thin = TokenFacts(
        mint=REAL_MINT,
        name="X",
        symbol="X",
        age_seconds=3_000,
        liquidity_usd=Decimal("900"),
        volume_usd=Decimal("120"),
        holder_count=6,
        buys=3,
        sells=2,
        total_fee_sol=Decimal("0.01"),
        top10_holder_rate=Decimal("0.90"),
        dev_hold_rate=Decimal("0.40"),
        bundler_rate=Decimal("0.70"),
        sniper_hold_rate=Decimal("0.60"),
        insider_rate=Decimal("0.55"),
    )
    alert = _card(quality=score_quality(thin))
    assert alert.ping is False


def test_the_card_prints_the_numbers_the_operator_asked_for() -> None:
    alert = _card(quality=score_quality(_real()))
    panel = next(field for field in alert.spec.fields if field.name == "REAL MONEY")
    # Fees, liquidity and holders, printed unconditionally.  These are the
    # three the operator named, and a number withheld until it clears a
    # threshold cannot be used to form a judgement.
    assert "SOL/min" in panel.value
    assert "liquidity" in panel.value
    assert "holders" in panel.value
    assert "1.3714" in panel.value

    # And a token whose fees are *bad* still shows the number rather than
    # quietly omitting the line.
    quiet = _card(quality=score_quality(_copy()))
    quiet_panel = next(field for field in quiet.spec.fields if field.name == "REAL MONEY")
    assert "0.1800" in quiet_panel.value


def test_no_early_card_is_ever_entry_eligible_whatever_the_verdict() -> None:
    # v2.43.1's rule survives everything added since: this lane publishes before
    # safety finishes, so it can never hand out a buy control.
    for kwargs in (
        {},
        {"clone_verdict": classify_clone(_real(), [_copy()])},
        {"quality": score_quality(_real())},
    ):
        alert = _card(**kwargs)
        assert alert.trade_eligible is False
        assert alert.entry_eligible is False


# ---------------------------------------------------------------------------
# 5. The blind collision check — the defect that printed "Symbol collision: NO".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_gmgn_discovered_token_is_visible_to_the_collision_check(tmp_path) -> None:
    database = Database(str(tmp_path / "collision.db"), Decimal("1000"))
    await database.connect()
    try:
        # Exactly the production shape: both mints arrived through GMGN, so
        # neither was ever written to pump_tokens or runner_candidates.
        for mint, symbol in ((REAL_MINT, "SNP500"), (COPY_MINT, "SNP500")):
            await database.save_token_presentation(
                {
                    "mint": mint,
                    "chain": "solana",
                    "name": "Sock and Pussy 500",
                    "symbol": symbol,
                    "image_url": "",
                    "description": "",
                    "website": "",
                    "twitter": "",
                    "telegram": "",
                    "source": "GMGN_BOARD",
                    "source_at": 1_000_000,
                    "resolved_at": 1_000_000,
                    "identity_verified": 1,
                    "resolution_failed": 0,
                    "sources": ["GMGN_BOARD"],
                }
            )
        known = await database.known_symbols(limit=500)
        # Keyed by mint, never by symbol — a symbol-keyed index is how a
        # lookup that substitutes one token for another gets written.
        assert known.get(REAL_MINT) == "SNP500"
        assert known.get(COPY_MINT) == "SNP500"
        from smart_money_bot.token_identity import detect_symbol_collision

        collision = detect_symbol_collision("SNP500", known, subject_mint=REAL_MINT)
        assert collision.detected is True
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_the_collision_check_reads_every_table_a_mint_can_land_in(tmp_path) -> None:
    # The bug was not the query; it was the list of tables.  A new discovery
    # lane that writes somewhere unlisted would reintroduce it silently, so the
    # source is asserted directly.
    import smart_money_bot.database as database_module

    source = inspect.getsource(database_module.Database.known_symbols)
    for table in ("token_presentations", "gmgn_observations", "pump_tokens", "runner_candidates"):
        assert table in source, f"known_symbols() cannot see {table}"


@pytest.mark.asyncio
async def test_known_token_names_supplies_what_the_clone_check_needs(tmp_path) -> None:
    database = Database(str(tmp_path / "names.db"), Decimal("1000"))
    await database.connect()
    try:
        await database.save_token_presentation(
            {
                "mint": REAL_MINT,
                "chain": "solana",
                "name": "Sock and Pussy 500",
                "symbol": "SNP500",
                "image_url": "",
                "description": "",
                "website": "",
                "twitter": "",
                "telegram": "",
                "source": "GMGN_BOARD",
                "source_at": 1_000_000,
                "resolved_at": 1_000_000,
                "identity_verified": 1,
                "resolution_failed": 0,
                "sources": ["GMGN_BOARD"],
            }
        )
        rows = await database.known_token_names(limit=50)
        row = next(item for item in rows if item["mint"] == REAL_MINT)
        assert row["symbol"] == "SNP500"
        assert row["name"] == "Sock and Pussy 500"
        # Keyed by mint, like every other identity lookup here.  A name-keyed
        # index would be the substitution bug waiting to be written.
        assert len({item["mint"] for item in rows}) == len(rows)
    finally:
        await database.close()


# ---------------------------------------------------------------------------
# 6. Throughput — the 424-second and 748-second alerts.
# ---------------------------------------------------------------------------


def test_evaluation_has_its_own_budget_far_larger_than_the_gmgn_call_budget(
    settings,
) -> None:
    # Sharing one number between "how many expensive GMGN calls" and "how many
    # candidates get looked at" was the whole of the lateness.
    assert settings.gmgn_early_lane_per_scan > settings.gmgn_enrichment_per_scan
    assert settings.gmgn_early_lane_per_scan >= 50
    assert settings.gmgn_early_lane_concurrency > 1


def test_the_gmgn_cycle_ranks_before_it_truncates() -> None:
    # If the truncation happened first, ranking would be decoration: the
    # strongest candidate would already have been dropped.
    import smart_money_bot.engine as engine_module

    source = inspect.getsource(engine_module.SmartMoneyEngine._gmgn_cycle)
    rank_at = source.index("rank_candidates(")
    truncate_at = source.index("ranked[:budget]")
    assert rank_at < truncate_at


def test_a_wide_scan_evaluates_the_strongest_first_and_evaluates_far_more_than_six() -> None:
    """The 424-second alert, reproduced and fixed.

    Two hundred and one candidates on one scan, the real runner sitting at
    index 200 in feed order — exactly the shape production was in when a mint
    first seen at $9.87K was not evaluated until $40.71K.  Under the old code
    the loop took ``result.candidates[:6]`` in feed order and never reached it.
    """

    from smart_money_bot.engine import SmartMoneyEngine

    engine = _partial_engine()
    engine.gmgn_candidates_published = 0
    engine.early_lane_evaluated = 0
    engine.settings = SimpleNamespace(
        gmgn_enrichment_per_scan=6,
        gmgn_early_lane_per_scan=60,
        gmgn_early_lane_concurrency=8,
        gmgn_early_lane_max_cards_per_scan=4,
    )

    def _token(mint, *, name, symbol, fee, liquidity, volume, holders):
        return SimpleNamespace(
            mint=mint,
            name=name,
            symbol=symbol,
            image_url="",
            created_at=1_000_000,
            open_at=None,
            liquidity_usd=liquidity,
            volume_usd=volume,
            market_cap_usd=Decimal("40000"),
            holder_count=holders,
            buys=100,
            sells=90,
            total_fee=fee,
            top10_holder_rate=Decimal("0.2"),
            dev_team_hold_rate=Decimal("0.02"),
            bundler_rate=Decimal("0.05"),
            sniper_hold_rate=Decimal("0.05"),
            insider_rate=Decimal("0.05"),
        )

    candidates = [
        SimpleNamespace(
            mint=f"dead{index}",
            family=FAMILY_GMGN_TRENDING,
            token=_token(
                f"dead{index}",
                name=f"Dead {index}",
                symbol=f"D{index}",
                fee=Decimal("0.002"),
                liquidity=Decimal("600"),
                volume=Decimal("40"),
                holders=4,
            ),
        )
        for index in range(200)
    ]
    candidates.append(
        SimpleNamespace(
            mint=REAL_MINT,
            family=FAMILY_GMGN_TRENDING,
            token=_token(
                REAL_MINT,
                name="Sock and Pussy 500",
                symbol="SNP500",
                fee=Decimal("9.6"),
                liquidity=Decimal("15180"),
                volume=Decimal("64000"),
                holders=520,
            ),
        )
    )

    evaluated: list[str] = []

    async def _early_lane_task(mint, *, now, may_publish=True):
        evaluated.append(mint)
        return False

    async def _note_presentation(*_args, **_kwargs):
        return None

    async def _note_early_watch_event(*_args, **_kwargs):
        return None

    async def _scan(*, now):
        return SimpleNamespace(candidates=tuple(candidates), errors=())

    engine._early_lane_task = _early_lane_task
    engine.note_presentation = _note_presentation
    engine.note_early_watch_event = _note_early_watch_event
    engine.gmgn_runtime = SimpleNamespace(scan=_scan)

    asyncio.run(SmartMoneyEngine._gmgn_cycle(engine))

    # The runner is looked at first, not two hundred scans later.
    assert evaluated[0] == REAL_MINT
    # And the lane is no longer rationed by the GMGN *call* budget.
    assert len(evaluated) == 60
    assert len(evaluated) > engine.settings.gmgn_enrichment_per_scan
    # Every candidate was remembered for same-name comparison, evaluated or not.
    assert len(engine._token_facts) == 201


def test_every_candidate_is_remembered_even_when_only_some_are_evaluated() -> None:
    # The copy has to be recognisable on the scan it appears in.  Only caching
    # the evaluated few would leave the comparison blind to exactly the token
    # the operator needs warning about.
    import smart_money_bot.engine as engine_module

    source = inspect.getsource(engine_module.SmartMoneyEngine._gmgn_cycle)
    note_at = source.index("_note_token_facts")
    budget_at = source.index("budget = max(")
    assert note_at < budget_at


# ---------------------------------------------------------------------------
# 7. Engine wiring, on a partial engine — no network, no database, no wallet.
# ---------------------------------------------------------------------------


def _partial_engine():
    from smart_money_bot.engine import SmartMoneyEngine

    engine = object.__new__(SmartMoneyEngine)
    engine._token_facts = {}
    engine._clone_verdicts = {}
    engine._quality_scores = {}
    engine.refused_publications = 0
    return engine


def test_observations_merge_rather_than_overwrite_each_other() -> None:
    # A GMGN board row knows fees, holders and ownership; a DEX snapshot knows
    # the live pair.  Keeping only the newest would throw half the picture away
    # every time, and a token with half a picture loses comparisons it should
    # win.
    engine = _partial_engine()
    engine._note_token_facts(
        TokenFacts(
            mint=REAL_MINT,
            name="Sock",
            symbol="SNP500",
            total_fee_sol=Decimal("9.6"),
            holder_count=520,
            age_seconds=420,
        )
    )
    merged = engine._note_token_facts(
        TokenFacts(mint=REAL_MINT, name="Sock", symbol="SNP500", liquidity_usd=Decimal("15180"))
    )
    assert merged.total_fee_sol == Decimal("9.6")
    assert merged.holder_count == 520
    assert merged.liquidity_usd == Decimal("15180")


def test_a_birth_only_ever_moves_earlier() -> None:
    # Birth is a historical fact.  A later observation reporting a later start
    # is a worse measurement of it, never a correction — and accepting one
    # would let a copy claim to have come first.
    engine = _partial_engine()
    engine._note_token_facts(TokenFacts(mint=REAL_MINT, name="A", symbol="A", created_at=1_000_000))
    merged = engine._note_token_facts(
        TokenFacts(mint=REAL_MINT, name="A", symbol="A", created_at=1_009_999)
    )
    assert merged.created_at == 1_000_000


def test_the_facts_cache_is_bounded() -> None:
    engine = _partial_engine()
    limit = engine.MAX_TOKEN_FACTS
    for index in range(limit + 25):
        engine._note_token_facts(TokenFacts(mint=f"m{index}", name="n", symbol="s"))
    assert len(engine._token_facts) == limit


def test_a_gmgn_row_becomes_facts_without_inventing_anything() -> None:
    engine = _partial_engine()
    token = SimpleNamespace(
        mint=REAL_MINT,
        name="Sock and Pussy 500",
        symbol="SNP500",
        created_at=1_000_000,
        open_at=None,
        liquidity_usd=Decimal("15180"),
        volume_usd=Decimal("64000"),
        market_cap_usd=Decimal("40710"),
        holder_count=520,
        buys=450,
        sells=438,
        total_fee=Decimal("9.6"),
        top10_holder_rate=Decimal("0.19"),
        dev_team_hold_rate=Decimal("0.02"),
        bundler_rate=Decimal("0.08"),
        sniper_hold_rate=Decimal("0.11"),
        insider_rate=Decimal("0.07"),
    )
    facts = engine._facts_from_gmgn(token, now=1_000_420)
    assert facts.mint == REAL_MINT
    assert facts.age_seconds == 420
    assert facts.total_fee_sol == Decimal("9.6")
    assert facts.dev_hold_rate == Decimal("0.02")


def test_a_dex_snapshot_leaves_what_it_cannot_see_unknown() -> None:
    # Not zero.  A DEX pair says nothing about fees, holders or ownership, and
    # conflating "no answer" with "nothing there" is how a degraded feed starts
    # looking like a rug.
    engine = _partial_engine()
    snapshot = SimpleNamespace(
        pair_age_minutes=7,
        liquidity_usd=Decimal("15180"),
        volume_1h_usd=Decimal("64000"),
        volume_5m_usd=Decimal("9000"),
        market_cap_usd=Decimal("40710"),
        buys_5m=450,
        sells_5m=438,
    )
    facts = engine._facts_from_snapshot(
        REAL_MINT, snapshot, name="Sock", symbol="SNP500", first_seen_at=1_000_000, now=1_000_420
    )
    assert facts.total_fee_sol is None
    assert facts.holder_count is None
    assert facts.top10_holder_rate is None
    assert facts.age_seconds == 420
    assert facts.liquidity_usd == Decimal("15180")


def test_the_publish_backstop_withholds_a_ping_from_a_copy() -> None:
    # Every lane funnels through one publish path, so the rule lives in one
    # place rather than being re-implemented in each card builder.
    engine = _partial_engine()
    engine._clone_verdicts[REAL_MINT] = classify_clone(_copy(mint=REAL_MINT), [_real(mint="other")])
    alert = _card()
    assert alert.ping is True
    guarded = engine._guard_publication(alert)
    assert guarded.ping is False
    assert guarded.lane == fa.LANE_RADAR
    assert guarded.symbol_collision is True


def test_the_backstop_leaves_an_original_alone() -> None:
    engine = _partial_engine()
    engine._clone_verdicts[REAL_MINT] = classify_clone(_real(), [_copy()])
    alert = _card()
    assert engine._guard_publication(alert).ping is True


def test_the_backstop_never_suppresses_the_card_itself() -> None:
    engine = _partial_engine()
    engine._clone_verdicts[REAL_MINT] = classify_clone(_copy(mint=REAL_MINT), [_real(mint="other")])
    guarded = engine._guard_publication(_card())
    assert guarded.spec.title
    assert guarded.mint == REAL_MINT


def test_the_backstop_is_reached_before_the_alert_is_reserved() -> None:
    # Reserving first would record a ping the operator never got, and the
    # dedupe row would then stop the corrected card from ever publishing.
    import smart_money_bot.engine as engine_module

    source = inspect.getsource(engine_module.SmartMoneyEngine._publish_fast_alert)
    assert source.index("_guard_publication") < source.index("reserve_fast_alert")


def test_a_clone_check_failure_can_never_break_a_scan() -> None:
    engine = _partial_engine()

    class Exploding:
        async def known_token_names(self, **_kwargs):
            raise RuntimeError("provider down")

    engine.database = Exploding()
    verdict = asyncio.run(engine._clone_check(_real()))
    assert isinstance(verdict, CloneVerdict)


def test_a_quality_failure_can_never_break_a_scan() -> None:
    engine = _partial_engine()
    broken = SimpleNamespace(mint=REAL_MINT)
    assert isinstance(engine._quality_check(broken), QualityScore)


# ---------------------------------------------------------------------------
# 8. Answerability.  "Why wasn't I pinged?" must still have an answer.
# ---------------------------------------------------------------------------


def test_every_new_suppression_reason_has_a_human_sentence() -> None:
    for code in (WHY_SUSPECTED_CLONE, WHY_NAME_COLLISION, WHY_THIN_QUALITY):
        assert HUMAN_WHY[code]
        assert not HUMAN_WHY[code].isupper()


def test_the_early_lane_records_why_it_withheld_a_ping() -> None:
    import smart_money_bot.engine as engine_module

    source = inspect.getsource(engine_module.SmartMoneyEngine._early_lane_task)
    assert "EARLY_WHY_SUSPECTED_CLONE" in source
    assert "EARLY_WHY_NAME_COLLISION" in source
    assert "EARLY_WHY_THIN_QUALITY" in source


# ---------------------------------------------------------------------------
# 9. The standing rules, still standing.
# ---------------------------------------------------------------------------


def test_the_new_strategy_modules_hold_no_network_database_or_signer() -> None:
    import smart_money_bot.lab.clone as clone_module
    import smart_money_bot.lab.tokenquality as quality_module

    for module in (clone_module, quality_module):
        source = inspect.getsource(module)
        for forbidden in (
            "import aiohttp",
            "import requests",
            "aiosqlite",
            "from solders",
            "private_key",
            "cookies=",
        ):
            assert forbidden not in source, f"{module.__name__} must stay pure logic"


# ===========================================================================
# 10. v2.48 — direction, not level.  Built from the operator's screenshots.
# ===========================================================================
"""v2.47 made the lane ten times faster and left the selection bar alone, so
the operator got ten times the cards at the same quality. Their words: *"the
coins you're giving me are all fucking terrible research coins"*, *"it's the
fakest chart I've seen in my life"*.

Two of the cards they screenshotted, scored by v2.47:

    POKEMON   MC $1.7K at -99.8%, 252 buys / 3,400 sells   ->  42.00, passed
    ISABELLA  3 holders, -47.1% on every timeframe         ->  56.88, STRONG

Both earned marks for *volume* and *transactions*, because a token dying
produces the largest volume and transaction counts of its life. The scorer
measured levels and could not tell a run from a rug. These tests are those two
rows, plus the one the operator said they wanted and missed.
"""


def _pokemon() -> TokenFacts:
    """Straight off the Terminal Trending screenshot."""

    return TokenFacts(
        mint="POKEMONmint",
        name="Pokemon",
        symbol="POKEMON",
        age_seconds=7_200,
        liquidity_usd=Decimal("3500"),
        volume_usd=Decimal("40000"),
        market_cap_usd=Decimal("1700"),
        ath_market_cap_usd=Decimal("850000"),
        holder_count=252,
        buys=252,
        sells=3_400,
        total_fee_sol=Decimal("0.021"),
        price_change_5m_percent=Decimal("-30"),
    )


def _isabella() -> TokenFacts:
    """The token-detail screenshot: 3 holders, -47.1% on every timeframe."""

    return TokenFacts(
        mint="5EBcssFtURviaiVQpump",
        name="Isabella Cognita",
        symbol="ISABELLA",
        age_seconds=34,
        liquidity_usd=Decimal("6260"),
        volume_usd=Decimal("12000"),
        market_cap_usd=Decimal("3080"),
        holder_count=3,
        buys=2,
        sells=1,
        total_fee_sol=Decimal("0.575"),
        top10_holder_rate=Decimal("0.05"),
        dev_hold_rate=Decimal("0"),
        bundler_rate=Decimal("0.02"),
        sniper_hold_rate=Decimal("0"),
        insider_rate=Decimal("0"),
        price_change_1m_percent=Decimal("-20.3"),
    )


def test_a_token_down_ninety_nine_percent_is_not_an_entry_however_big_its_volume() -> None:
    score = score_quality(_pokemon())
    assert score.disqualified is True
    assert score.score == 0
    assert score.weak() is True
    joined = " ".join(score.disqualifiers)
    assert "own high" in joined
    assert "sells for every buy" in joined


def test_a_dump_can_no_longer_earn_marks_for_volume_or_transactions() -> None:
    # The specific v2.47 defect: $40K of volume on $3.5K of liquidity and 3.6K
    # transactions scored 14/14, 10/10 and 6/6 — full marks, three times over,
    # on the three figures a rug maximises.
    components = dict(score_quality(_pokemon()).components)
    for family in ("volume", "volume vs liquidity", "transactions"):
        assert Decimal(components[family]) == 0, f"{family} still rewards the dump"


def test_three_holders_is_not_a_market_at_any_age_or_score() -> None:
    score = score_quality(_isabella())
    assert score.disqualified is True
    assert score.strong() is False
    assert score.weak() is True
    assert any("holders" in item for item in score.disqualifiers)


def test_the_isabella_card_can_never_ping_and_says_why() -> None:
    alert = _card(quality=score_quality(_isabella()))
    assert alert.ping is False
    assert alert.lane == fa.LANE_RADAR
    names = [field.name for field in alert.spec.fields]
    assert "⛔ NOT AN ENTRY" in names
    panel = next(field for field in alert.spec.fields if field.name == "⛔ NOT AN ENTRY")
    assert "3 holders" in panel.value
    # Shown, not hidden. The operator asked to stop being *recommended* dead
    # charts, not to lose the ability to see that one exists.
    assert alert.spec.title


def test_the_same_token_running_is_still_pinged() -> None:
    # RETA, at the moment the operator described watching it: $131K and moving,
    # near its own high, buyers outnumbering sellers. This is the alert they
    # said they wanted and did not get.
    running = TokenFacts(
        mint="RETAmint",
        name="peptidezz",
        symbol="RETA",
        age_seconds=120,
        liquidity_usd=Decimal("22600"),
        volume_usd=Decimal("27500"),
        market_cap_usd=Decimal("131000"),
        ath_market_cap_usd=Decimal("133000"),
        holder_count=191,
        buys=140,
        sells=91,
        total_fee_sol=Decimal("0.42"),
        price_change_1m_percent=Decimal("45.0"),
        top10_holder_rate=Decimal("0.34"),
        dev_hold_rate=Decimal("0.02"),
        bundler_rate=Decimal("0.005"),
        sniper_hold_rate=Decimal("0.09"),
        insider_rate=Decimal("0.21"),
    )
    score = score_quality(running)
    assert score.disqualified is False
    assert score.strong() is True
    assert _card(quality=score).ping is True


def test_the_same_token_after_the_move_is_refused() -> None:
    # RETA as the screenshot actually caught it: $61.6K against a $222.2K high.
    # Same token, same name, same holders — a different question entirely, and
    # the ATH column is what separates them.
    late = TokenFacts(
        mint="RETAmint",
        name="peptidezz",
        symbol="RETA",
        age_seconds=120,
        liquidity_usd=Decimal("22600"),
        volume_usd=Decimal("27500"),
        market_cap_usd=Decimal("61600"),
        ath_market_cap_usd=Decimal("222200"),
        holder_count=191,
        buys=114,
        sells=117,
        total_fee_sol=Decimal("0.42"),
        price_change_1m_percent=Decimal("-60.4"),
    )
    assert score_quality(late).disqualified is True
    assert _card(quality=score_quality(late)).ping is False


def test_an_early_thin_token_is_still_allowed_to_ping() -> None:
    # The other half of the trade, and the one easy to break while fixing the
    # first. A sixty-second-old token cannot have 120 holders, 9 SOL of fees or
    # a meaningful high — it is thin because it is EARLY, which is exactly when
    # the operator asked to hear about it. Only the structural refusals apply.
    grok_pocket = TokenFacts(
        mint="GROKmint",
        name="Grok Pocket",
        symbol="GROK",
        age_seconds=60,
        liquidity_usd=Decimal("6900"),
        volume_usd=Decimal("5200"),
        market_cap_usd=Decimal("31180"),
        buys=26,
        sells=6,
        price_change_5m_percent=Decimal("14"),
    )
    score = score_quality(grok_pocket)
    assert score.disqualified is False
    # Not confident — four of the decisive families are simply unmeasurable at
    # this age — so the score bar withholds nothing.
    assert score.confident() is False
    assert score.weak() is False
    assert _card(quality=score).ping is True


def test_sell_pressure_scales_volume_rather_than_being_averaged_away() -> None:
    # Two tokens identical except for which way the flow runs. If sell pressure
    # were merely one more component, the loser would keep most of its volume
    # marks; scaling is what stops that.
    base = dict(
        mint="X",
        name="X",
        symbol="X",
        age_seconds=600,
        liquidity_usd=Decimal("20000"),
        volume_usd=Decimal("60000"),
        market_cap_usd=Decimal("50000"),
        ath_market_cap_usd=Decimal("52000"),
        holder_count=200,
        total_fee_sol=Decimal("2"),
    )
    buying = score_quality(TokenFacts(**base, buys=300, sells=200))
    selling = score_quality(TokenFacts(**base, buys=100, sells=280))
    assert buying.score > selling.score
    assert Decimal(dict(buying.components)["volume"]) > Decimal(
        dict(selling.components)["volume"]
    )


def test_an_unknown_measurement_never_disqualifies() -> None:
    # No ATH, no buy/sell split, no holder count. That is a token we could not
    # look at, which is not the same as a token with nothing in it — the same
    # rule v2.47 established, extended to the new refusals.
    unknown = TokenFacts(
        mint="U", name="U", symbol="U", age_seconds=300, liquidity_usd=Decimal("12000")
    )
    assert score_quality(unknown).disqualified is False


def test_ranking_puts_a_running_token_above_a_dumping_one() -> None:
    running = TokenFacts(
        mint="run",
        name="R",
        symbol="R",
        age_seconds=180,
        liquidity_usd=Decimal("18000"),
        volume_usd=Decimal("30000"),
        market_cap_usd=Decimal("90000"),
        ath_market_cap_usd=Decimal("92000"),
        holder_count=220,
        buys=200,
        sells=120,
        total_fee_sol=Decimal("3"),
        price_change_1m_percent=Decimal("30"),
    )
    ranked = rank_candidates([_pokemon(), _isabella(), running])
    assert ranked[0][0].mint == "run"
    # And both corpses sit at the bottom on a score of zero.
    assert all(item[1].score == 0 for item in ranked[1:])


def test_the_gmgn_row_supplies_momentum_and_the_all_time_high() -> None:
    # All three were already arriving on every board row and were dropped.
    engine = _partial_engine()
    token = SimpleNamespace(
        mint="M",
        name="M",
        symbol="M",
        created_at=1_000_000,
        open_at=None,
        liquidity_usd=Decimal("10000"),
        volume_usd=Decimal("20000"),
        market_cap_usd=Decimal("61600"),
        holder_count=191,
        buys=114,
        sells=117,
        total_fee=Decimal("0.42"),
        price_change_1m_percent=Decimal("-60.4"),
        price_change_5m_percent=Decimal("-30"),
        history_highest_market_cap_usd=Decimal("222200"),
        top10_holder_rate=Decimal("0.2"),
        dev_team_hold_rate=Decimal("0.02"),
        bundler_rate=Decimal("0.05"),
        sniper_hold_rate=Decimal("0.05"),
        insider_rate=Decimal("0.05"),
    )
    facts = engine._facts_from_gmgn(token, now=1_000_120)
    assert facts.price_change_1m_percent == Decimal("-60.4")
    assert facts.ath_market_cap_usd == Decimal("222200")
    assert facts.drawdown_from_ath > Decimal("0.7")
    assert facts.sell_pressure > 1


def test_a_wide_scan_does_not_become_a_wide_alert() -> None:
    """The whole complaint, reproduced.

    v2.47 raised evaluation from 6 to 60 per scan and left publishing uncapped,
    so a scan that *looked at* sixty candidates could tell the operator about
    sixty of them. Every one of these fifty candidates would publish; only the
    card budget may reach Discord.
    """

    from smart_money_bot.engine import SmartMoneyEngine

    engine = _partial_engine()
    engine.gmgn_candidates_published = 0
    engine.early_lane_evaluated = 0
    engine.settings = SimpleNamespace(
        gmgn_enrichment_per_scan=6,
        gmgn_early_lane_per_scan=60,
        gmgn_early_lane_concurrency=8,
        gmgn_early_lane_max_cards_per_scan=4,
    )

    candidates = [
        SimpleNamespace(
            mint=f"cand{index:03d}",
            family=FAMILY_GMGN_TRENDING,
            token=SimpleNamespace(
                mint=f"cand{index:03d}",
                name=f"Cand {index}",
                symbol=f"C{index}",
                image_url="",
                created_at=1_000_000,
                open_at=None,
                liquidity_usd=Decimal("15000") + index,
                volume_usd=Decimal("40000"),
                market_cap_usd=Decimal("50000"),
                holder_count=300,
                buys=200,
                sells=120,
                total_fee=Decimal("5"),
                price_change_1m_percent=Decimal("20"),
                price_change_5m_percent=Decimal("30"),
                history_highest_market_cap_usd=Decimal("51000"),
                top10_holder_rate=Decimal("0.2"),
                dev_team_hold_rate=Decimal("0.02"),
                bundler_rate=Decimal("0.05"),
                sniper_hold_rate=Decimal("0.05"),
                insider_rate=Decimal("0.05"),
            ),
        )
        for index in range(50)
    ]

    evaluated: list[str] = []

    async def _early_lane_task(mint, *, now, may_publish=True):
        evaluated.append(mint)
        return may_publish  # every single one would publish, if allowed

    async def _noop(*_args, **_kwargs):
        return None

    async def _scan(*, now):
        return SimpleNamespace(candidates=tuple(candidates), errors=())

    engine._early_lane_task = _early_lane_task
    engine.note_presentation = _noop
    engine.note_early_watch_event = _noop
    engine.gmgn_runtime = SimpleNamespace(scan=_scan)

    asyncio.run(SmartMoneyEngine._gmgn_cycle(engine))

    # Every one was still looked at — the first-seen market cap is a
    # historical fact whether or not a card came of it, and a near-miss
    # belongs on the watch list either way.
    assert len(evaluated) == 50
    # ...and the operator was told about four of them.
    assert engine.gmgn_candidates_published == 4


def test_the_card_cap_is_spent_best_first() -> None:
    # A cap that took feed order would just be the old lateness with a smaller
    # number: the ranking has to run before the budget is spent.
    import smart_money_bot.engine as engine_module

    source = inspect.getsource(engine_module.SmartMoneyEngine._gmgn_cycle)
    assert source.index("rank_candidates(") < source.index("card_budget")


def test_the_card_cap_is_smaller_than_the_evaluation_budget(settings) -> None:
    assert settings.gmgn_early_lane_max_cards_per_scan < settings.gmgn_early_lane_per_scan
    assert settings.gmgn_early_lane_max_cards_per_scan <= 6


def test_not_an_entry_has_its_own_answerable_reason() -> None:
    from smart_money_bot.lab.early import WHY_NOT_AN_ENTRY

    assert HUMAN_WHY[WHY_NOT_AN_ENTRY]
    import smart_money_bot.engine as engine_module

    source = inspect.getsource(engine_module.SmartMoneyEngine._early_lane_task)
    assert "EARLY_WHY_NOT_AN_ENTRY" in source


def test_each_refusal_stands_on_its_own() -> None:
    """POKEMON trips two refusals at once, so it proves neither individually.

    A single failing case that happens to satisfy several rules is how a rule
    quietly stops working: disable it and the case still fails, for the other
    reason. Each of the four is exercised alone, with everything else healthy.
    """

    healthy = dict(
        mint="X",
        name="X",
        symbol="X",
        age_seconds=600,
        liquidity_usd=Decimal("20000"),
        volume_usd=Decimal("80000"),
        market_cap_usd=Decimal("50000"),
        ath_market_cap_usd=Decimal("51000"),
        holder_count=400,
        buys=300,
        sells=250,
        total_fee_sol=Decimal("4"),
        price_change_1m_percent=Decimal("5"),
    )
    assert score_quality(TokenFacts(**healthy)).disqualified is False

    for label, broken in (
        ("sell pressure", {"buys": 100, "sells": 420}),
        ("drawdown", {"market_cap_usd": Decimal("15000"),
                      "ath_market_cap_usd": Decimal("120000")}),
        ("holder floor", {"holder_count": 4}),
        ("collapse", {"price_change_1m_percent": Decimal("-70")}),
    ):
        facts = TokenFacts(**{**healthy, **broken})
        score = score_quality(facts)
        assert score.disqualified is True, f"{label} refusal is not firing"
        assert score.score == 0
        assert len(score.disqualifiers) == 1, f"{label} case trips more than one rule"


# ===========================================================================
# 11. v2.49 — Trending only.  "Just start focusing on trending."
# ===========================================================================


def test_only_trending_candidates_can_produce_a_card() -> None:
    """The trenches board was burying Trending in the same candidate list.

    Three sections of up to sixty rows each — New, Almost bonded, Migrated —
    of tokens that are by definition minutes old. They ranked alongside
    Trending and, being numerous, took the cards.
    """

    from smart_money_bot.engine import SmartMoneyEngine
    from smart_money_bot.lab.shadow import (
        FAMILY_GMGN_HOT_SEARCH,
        FAMILY_GMGN_TRENCH_NEW,
    )

    engine = _partial_engine()
    engine.gmgn_candidates_published = 0
    engine.early_lane_evaluated = 0
    engine.settings = SimpleNamespace(
        gmgn_enrichment_per_scan=6,
        gmgn_early_lane_per_scan=60,
        gmgn_early_lane_concurrency=8,
        gmgn_early_lane_max_cards_per_scan=4,
        gmgn_trending_only=True,
    )

    def _candidate(mint, family, *, fee):
        return SimpleNamespace(
            mint=mint,
            family=family,
            token=SimpleNamespace(
                mint=mint,
                name=f"Token {mint}",
                symbol=mint.upper(),
                image_url="",
                created_at=1_000_000,
                open_at=None,
                liquidity_usd=Decimal("20000"),
                volume_usd=Decimal("50000"),
                market_cap_usd=Decimal("60000"),
                holder_count=300,
                buys=250,
                sells=150,
                total_fee=fee,
                price_change_1m_percent=Decimal("30"),
                price_change_5m_percent=Decimal("40"),
                history_highest_market_cap_usd=Decimal("61000"),
                top10_holder_rate=Decimal("0.2"),
                dev_team_hold_rate=Decimal("0.02"),
                bundler_rate=Decimal("0.05"),
                sniper_hold_rate=Decimal("0.05"),
                insider_rate=Decimal("0.05"),
            ),
        )

    # The trench rows are deliberately given the *stronger* numbers. Ranking
    # alone would hand them every card; only the family filter stops it.
    candidates = [
        _candidate(f"trench{i}", FAMILY_GMGN_TRENCH_NEW, fee=Decimal("50"))
        for i in range(40)
    ]
    candidates += [
        _candidate(f"hot{i}", FAMILY_GMGN_HOT_SEARCH, fee=Decimal("50")) for i in range(10)
    ]
    candidates += [
        _candidate(f"trend{i}", FAMILY_GMGN_TRENDING, fee=Decimal("6")) for i in range(5)
    ]

    evaluated: list[str] = []

    async def _early_lane_task(mint, *, now, may_publish=True):
        evaluated.append(mint)
        return may_publish

    async def _noop(*_args, **_kwargs):
        return None

    async def _scan(*, now):
        return SimpleNamespace(candidates=tuple(candidates), errors=())

    engine._early_lane_task = _early_lane_task
    engine.note_presentation = _noop
    engine.note_early_watch_event = _noop
    engine.gmgn_runtime = SimpleNamespace(scan=_scan)

    asyncio.run(SmartMoneyEngine._gmgn_cycle(engine))

    assert evaluated, "nothing was evaluated at all"
    assert all(mint.startswith("trend") for mint in evaluated), (
        f"a non-trending family reached the early lane: {sorted(set(evaluated))[:5]}"
    )
    # Every candidate still entered the same-name cache. The copy detection
    # needs the wide view — a trench launch is exactly what clones a trending
    # token — so the other feeds keep being observed, they just cannot alert.
    assert len(engine._token_facts) == 55


def test_the_other_feeds_come_back_when_the_switch_is_off() -> None:
    from smart_money_bot.engine import SmartMoneyEngine
    from smart_money_bot.lab.shadow import FAMILY_GMGN_TRENCH_NEW

    engine = _partial_engine()
    engine.gmgn_candidates_published = 0
    engine.early_lane_evaluated = 0
    engine.settings = SimpleNamespace(
        gmgn_enrichment_per_scan=6,
        gmgn_early_lane_per_scan=60,
        gmgn_early_lane_concurrency=8,
        gmgn_early_lane_max_cards_per_scan=4,
        gmgn_trending_only=False,
    )
    candidate = SimpleNamespace(
        mint="trench1",
        family=FAMILY_GMGN_TRENCH_NEW,
        token=SimpleNamespace(
            mint="trench1", name="T", symbol="T", image_url="", created_at=1_000_000,
            open_at=None, liquidity_usd=Decimal("20000"), volume_usd=Decimal("50000"),
            market_cap_usd=Decimal("60000"), holder_count=300, buys=250, sells=150,
            total_fee=Decimal("6"), price_change_1m_percent=Decimal("30"),
            price_change_5m_percent=None, history_highest_market_cap_usd=Decimal("61000"),
            top10_holder_rate=Decimal("0.2"), dev_team_hold_rate=Decimal("0.02"),
            bundler_rate=Decimal("0.05"), sniper_hold_rate=Decimal("0.05"),
            insider_rate=Decimal("0.05"),
        ),
    )
    evaluated: list[str] = []

    async def _early_lane_task(mint, *, now, may_publish=True):
        evaluated.append(mint)
        return may_publish

    async def _noop(*_args, **_kwargs):
        return None

    async def _scan(*, now):
        return SimpleNamespace(candidates=(candidate,), errors=())

    engine._early_lane_task = _early_lane_task
    engine.note_presentation = _noop
    engine.note_early_watch_event = _noop
    engine.gmgn_runtime = SimpleNamespace(scan=_scan)

    asyncio.run(SmartMoneyEngine._gmgn_cycle(engine))
    assert evaluated == ["trench1"]


def test_trending_only_is_the_default(settings) -> None:
    assert settings.gmgn_trending_only is True


def test_the_family_filter_runs_after_the_same_name_cache_is_filled() -> None:
    # Filtering before the cache would blind the copy detection to the trench
    # launches, which are exactly what clones a trending token.
    import smart_money_bot.engine as engine_module

    source = inspect.getsource(engine_module.SmartMoneyEngine._gmgn_cycle)
    assert source.index("_note_token_facts") < source.index("gmgn_trending_only")


# ===========================================================================
# 12. v2.50 — the promotion card bypassed every gate v2.47-2.49 added.
# ===========================================================================


def test_a_disqualified_token_cannot_ping_from_any_card_builder() -> None:
    """The NORMIE card, and why three releases of fixes did not reach it.

    Every gate v2.47-2.49 added lived in ``build_early_alert`` and the GMGN
    scan loop. The cards the operator was actually complaining about came from
    ``build_promotion_alert``, published off the hot-watch timer, which touches
    neither — so a promotion could be titled **🚨 EARLY RUNNER — LOOK NOW**
    while its own body reported -63.70% over five minutes and 819 sells against
    765 buys.

    v2.48 made it worse: capping cards per scan put every budget-skipped
    candidate on the watch list, which feeds exactly this path. The front door
    was capped and the back door widened.

    So the guard moved to the one place every card must pass through.
    """

    normie = TokenFacts(
        mint="7XRemzYVgQZ4auVMpxisWVZwyhivrowCF4Hq8TCepump",
        name="normie",
        symbol="NORMIE",
        age_seconds=240,
        liquidity_usd=Decimal("8410"),
        market_cap_usd=Decimal("14670"),
        buys=765,
        sells=819,
        price_change_1m_percent=None,  # the card said: 1m unknown
        price_change_5m_percent=Decimal("-63.70"),
    )
    score = score_quality(normie)
    assert score.disqualified is True

    engine = _partial_engine()
    engine._quality_scores[normie.mint] = score

    # A card from a builder that knows nothing about any of this — the exact
    # situation build_promotion_alert was in.
    unguarded = fa.FastAlert(
        kind=fa.EARLY_RUNNER,
        mint=normie.mint,
        alert_key=f"promotion:{normie.mint}",
        spec=fa.CardSpec(
            title="🚨 EARLY RUNNER — LOOK NOW",
            description="normie $NORMIE",
            fields=(fa.CardField("MOMENTUM", "5m -63.70%", 10),),
        ),
        ping=True,
        lane=fa.LANE_URGENT,
        token_mint=normie.mint,
    )
    assert unguarded.ping is True, "fixture must start actionable or it proves nothing"

    guarded = engine._guard_publication(unguarded)
    assert guarded.ping is False
    assert guarded.lane == fa.LANE_RADAR
    assert guarded.trade_eligible is False
    # And it no longer *reads* as an instruction. A title saying LOOK NOW above
    # a body saying -63.70% is the card telling the operator two opposite
    # things and letting the louder one win.
    assert "LOOK NOW" not in guarded.spec.title
    names = [field.name for field in guarded.spec.fields]
    assert names[0] == "⛔ NOT AN ENTRY"
    assert "falling over" in guarded.spec.fields[0].value
    assert engine.refused_publications == 1


def test_the_guard_leaves_a_healthy_token_completely_alone() -> None:
    engine = _partial_engine()
    engine._quality_scores[REAL_MINT] = score_quality(_real())
    alert = _card()
    assert engine._guard_publication(alert) is alert or (
        engine._guard_publication(alert).ping is True
    )


def test_the_promotion_path_scores_at_publish_time() -> None:
    # A hot watch is opened minutes before it is promoted, and the point of a
    # promotion is that something changed. Scoring at watch time would decide
    # on numbers that are no longer on the screen.
    import smart_money_bot.engine as engine_module

    source = inspect.getsource(engine_module.SmartMoneyEngine._publish_promotion)
    assert "_quality_check" in source, "the promotion path still never asks about quality"
    assert "if quality.disqualified:" in source, "it asks, then ignores the answer"
    assert "EARLY_WHY_NOT_AN_ENTRY" in source
    # It must refuse before it builds the card, not after.
    assert source.index("_quality_check") < source.index("build_promotion_alert")


def test_every_card_builder_is_covered_by_one_choke_point() -> None:
    # The structural claim. If a future builder is added and forgets the gates,
    # it still cannot ping about a token that is falling over.
    import smart_money_bot.engine as engine_module

    publish = inspect.getsource(engine_module.SmartMoneyEngine._publish_fast_alert)
    guard = inspect.getsource(engine_module.SmartMoneyEngine._guard_publication)
    assert "_guard_publication" in publish
    assert publish.index("_guard_publication") < publish.index("reserve_fast_alert")
    # The guard consults quality, not only the clone verdict — that omission is
    # what let the promotion cards through.
    assert "_quality_scores" in guard
    assert "disqualified" in guard
