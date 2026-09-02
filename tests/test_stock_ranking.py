"""Credible value, and knowing when the crown actually moved.

    credible_value = min(FDV, 150 x volume_24h, 500 x liquidity_usd)

The caps are the point. FDV alone is a supply number multiplied by whatever the
last trade was, and the last trade can be a dollar. The caps make the headline
figure earn itself: a token cannot be credibly worth more than a multiple of
what has actually traded through it, or of what is actually pooled behind it.

Covers specification tests 11 and 12.
"""

from __future__ import annotations

from decimal import Decimal

from smart_money_bot.stocks.ranking import (
    CAP_FDV,
    CAP_LIQUIDITY,
    CAP_VOLUME,
    CROWN_CHANGED,
    CROWN_CONTESTED,
    CROWN_ESTABLISHED,
    CROWN_UNCHANGED,
    Candidate,
    CrownState,
    credible_value,
    evaluate_crown,
    rank_anchor,
    summarise_anchor,
)

NOW = 1_700_000_000
ANCHOR = "0xnvda000000000000000000000000000000000001"


def _c(mint: str, fdv: str, volume: str, liquidity: str, **overrides) -> Candidate:
    values = dict(
        mint=mint,
        anchor_key=ANCHOR,
        symbol=mint,
        fdv_usd=Decimal(fdv),
        volume_24h_usd=Decimal(volume),
        liquidity_usd=Decimal(liquidity),
        launched_at=NOW - 3_600,
    )
    values.update(overrides)
    return Candidate(**values)


# ===========================================================================
# 11 — the biggest FDV does not win
# ===========================================================================


def test_11_a_huge_fdv_on_no_depth_loses_to_a_modest_real_token() -> None:
    inflated = _c("BIGFDV", "1000000000", "400", "200")
    real = _c("REAL", "300000", "40000", "55000")

    assert credible_value(inflated).value == Decimal("60000.00")
    assert credible_value(real).value == Decimal("300000.00")

    ranked = rank_anchor([inflated, real], anchor_key=ANCHOR)
    assert ranked[0].candidate.mint == "REAL"
    assert ranked[0].rank == 1


def test_11b_the_binding_cap_is_named_so_the_card_can_explain_the_rank() -> None:
    # Usually the most informative thing about a candidate: "capped by
    # liquidity" says more than any score.
    assert credible_value(_c("A", "1000000000", "400", "200")).binding_cap == CAP_VOLUME
    assert credible_value(_c("B", "1000000000", "9000000", "200")).binding_cap == CAP_LIQUIDITY
    assert credible_value(_c("C", "300000", "40000", "55000")).binding_cap == CAP_FDV
    assert "little has actually traded" in credible_value(_c("A", "1e9", "400", "200")).why()


def test_11c_the_caps_are_exactly_the_published_multipliers() -> None:
    value = credible_value(_c("X", "999999999", "1000", "999999999"))
    assert value.value == Decimal("150000.00")  # 150 x 1000
    value = credible_value(_c("Y", "999999999", "999999999", "1000"))
    assert value.value == Decimal("500000.00")  # 500 x 1000


def test_an_unmeasurable_candidate_is_unavailable_rather_than_zero() -> None:
    # A token we could not measure must not sort below one measured as
    # worthless: those are different findings.
    blind = Candidate(mint="BLIND", anchor_key=ANCHOR, fdv_usd=Decimal("500000"))
    value = credible_value(blind)
    assert value.value is None
    assert value.credible is False
    assert "not enough market data" in value.why()
    # And it is excluded from the crown rather than winning it by default.
    ranked = rank_anchor([blind], anchor_key=ANCHOR)
    assert evaluate_crown(ANCHOR, ranked, None, now=NOW).outcome == CROWN_CONTESTED


def test_ranking_is_deterministic_on_equal_inputs() -> None:
    # A ranking that reshuffles on ties would manufacture crown changes out of
    # nothing, so ties break by earlier launch and then by mint.
    early = _c("zzz", "300000", "40000", "55000", launched_at=NOW - 9_000)
    late = _c("aaa", "300000", "40000", "55000", launched_at=NOW - 100)
    assert [r.candidate.mint for r in rank_anchor([late, early], anchor_key=ANCHOR)] == [
        "zzz",
        "aaa",
    ]


def test_coins_on_another_anchor_are_not_in_this_ranking() -> None:
    other = _c("TSLACOIN", "9000000", "900000", "900000", anchor_key="0xtsla")
    ranked = rank_anchor([other, _c("REAL", "300000", "40000", "55000")], anchor_key=ANCHOR)
    assert [r.candidate.mint for r in ranked] == ["REAL"]


# ===========================================================================
# 12 — a true crown change alerts once; near-tie jitter does not flap
# ===========================================================================


def _incumbent() -> CrownState:
    return CrownState(
        anchor_key=ANCHOR,
        leader_mint="REAL",
        leader_value=Decimal("300000.00"),
        leader_since=NOW - 3_600,
    )


def test_12_a_decisive_change_alerts_once() -> None:
    ranked = rank_anchor(
        [_c("REAL", "300000", "40000", "55000"), _c("CHAL", "900000", "90000", "200000")],
        anchor_key=ANCHOR,
    )
    event = evaluate_crown(ANCHOR, ranked, _incumbent(), now=NOW)

    assert event.outcome == CROWN_CHANGED
    assert event.should_alert is True
    assert event.state.leader_mint == "CHAL"
    assert event.state.previous_leader_mint == "REAL"
    assert event.state.leader_since == NOW

    # And the very next poll, with nothing further changing, says nothing.
    again = evaluate_crown(ANCHOR, ranked, event.state, now=NOW + 60)
    assert again.outcome == CROWN_UNCHANGED
    assert again.should_alert is False


def test_12b_near_tie_jitter_does_not_flap_the_crown() -> None:
    """Two coins within a percent trade places on every refresh.

    Alerting on each swap produces a stream of notifications describing noise.
    """

    ranked = rank_anchor(
        [_c("REAL", "300000", "40000", "55000"), _c("CHAL", "310000", "41000", "56000")],
        anchor_key=ANCHOR,
    )
    event = evaluate_crown(ANCHOR, ranked, _incumbent(), now=NOW)

    assert event.outcome == CROWN_CONTESTED
    assert event.should_alert is False
    # The incumbent keeps the crown rather than being quietly replaced.
    assert event.state.leader_mint == "REAL"
    assert "below the" in event.reason


def test_12c_the_hysteresis_threshold_is_configurable() -> None:
    # A 1.10x lead: under the 1.15 default, over a 1.05 override. Picking a
    # margin outside that band would prove nothing about the threshold.
    ranked = rank_anchor(
        [_c("REAL", "300000", "40000", "55000"), _c("CHAL", "330000", "41000", "56000")],
        anchor_key=ANCHOR,
    )
    assert evaluate_crown(ANCHOR, ranked, _incumbent(), now=NOW).should_alert is False
    lenient = evaluate_crown(
        ANCHOR, ranked, _incumbent(), hysteresis=Decimal("1.05"), now=NOW
    )
    assert lenient.should_alert is True


def test_12d_holding_the_crown_preserves_how_long_it_has_been_held() -> None:
    # Resetting leader_since on every poll would erase the one piece of
    # information the field exists to carry.
    ranked = rank_anchor([_c("REAL", "300000", "40000", "55000")], anchor_key=ANCHOR)
    event = evaluate_crown(ANCHOR, ranked, _incumbent(), now=NOW)
    assert event.outcome == CROWN_UNCHANGED
    assert event.state.leader_since == NOW - 3_600


def test_the_first_leader_is_established_rather_than_a_change() -> None:
    ranked = rank_anchor([_c("REAL", "300000", "40000", "55000")], anchor_key=ANCHOR)
    event = evaluate_crown(ANCHOR, ranked, None, now=NOW)
    assert event.outcome == CROWN_ESTABLISHED
    # Not an alert: nobody was displaced.
    assert event.should_alert is False


def test_an_anchor_complex_sums_only_what_was_measured() -> None:
    ranked = rank_anchor(
        [
            _c("A", "300000", "40000", "55000"),
            Candidate(mint="B", anchor_key=ANCHOR, fdv_usd=Decimal("1")),
        ],
        anchor_key=ANCHOR,
    )
    complex_ = summarise_anchor(ANCHOR, ranked)
    assert complex_.coin_count == 2
    assert complex_.total_liquidity_usd == Decimal("55000")
    assert complex_.leader_mint == "A"
