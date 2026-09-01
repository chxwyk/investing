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
            family="NEW_PAIR",
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
            family="TRENDING",
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

    async def _early_lane_task(mint, *, now):
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
    guarded = engine._withhold_ping_from_copies(alert)
    assert guarded.ping is False
    assert guarded.lane == fa.LANE_RADAR
    assert guarded.symbol_collision is True


def test_the_backstop_leaves_an_original_alone() -> None:
    engine = _partial_engine()
    engine._clone_verdicts[REAL_MINT] = classify_clone(_real(), [_copy()])
    alert = _card()
    assert engine._withhold_ping_from_copies(alert).ping is True


def test_the_backstop_never_suppresses_the_card_itself() -> None:
    engine = _partial_engine()
    engine._clone_verdicts[REAL_MINT] = classify_clone(_copy(mint=REAL_MINT), [_real(mint="other")])
    guarded = engine._withhold_ping_from_copies(_card())
    assert guarded.spec.title
    assert guarded.mint == REAL_MINT


def test_the_backstop_is_reached_before_the_alert_is_reserved() -> None:
    # Reserving first would record a ping the operator never got, and the
    # dedupe row would then stop the corrected card from ever publishing.
    import smart_money_bot.engine as engine_module

    source = inspect.getsource(engine_module.SmartMoneyEngine._publish_fast_alert)
    assert source.index("_withhold_ping_from_copies") < source.index("reserve_fast_alert")


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
