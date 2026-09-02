"""Not every verified launch is a buy.

A stock-anchored launch is interesting the moment it is verified — genuinely
early, which is what the operator asked for. But a token that exists and has
never traded has no price, no liquidity and no buyers, and calling that an entry
would be the same failure this project spent four releases fixing, wearing a new
badge.

Covers specification tests 8 and 9.
"""

from __future__ import annotations

from decimal import Decimal

from smart_money_bot.lab.hardgates import (
    FAIL,
    GATES,
    PASS,
    UNKNOWN,
    GateResult,
    build_report,
)
from smart_money_bot.stocks.anchors import ANCHOR_ONCHAIN, AnchoredCoin, StockAnchor
from smart_money_bot.stocks.registry import StockToken
from smart_money_bot.stocks.signal import evaluate_anchor
from smart_money_bot.stocks.stages import (
    STAGE_DIAGNOSTIC,
    STAGE_ENTRY_CANDIDATE,
    STAGE_TRACTION_WATCH,
    STAGE_UNSAFE_MOMENTUM,
    STAGE_VERIFIED_LAUNCH,
    W_ANCHOR_QUIET,
    W_GATES,
    W_NO_ANCHOR,
    W_NO_MARKET,
    W_UNSAFE,
    MarketState,
    classify_stage,
)
from smart_money_bot.stocks.verification import (
    NOT_STOCK_LINKED,
    PROOF_LAUNCH_RECORD,
    AnchorProof,
)

NOW = 1_700_000_000
MEME = "0x2e8c31162b855a2ffa90f6f8634643ad6f111e18"
NVDA = "0xnvda000000000000000000000000000000000001"


def _proof(verified: bool = True) -> AnchorProof:
    if not verified:
        return AnchorProof(meme_address=MEME, proof=NOT_STOCK_LINKED)
    return AnchorProof(
        meme_address=MEME,
        proof=PROOF_LAUNCH_RECORD,
        anchors=(StockToken(address=NVDA, symbol="NVDA", name="NVIDIA", status="active"),),
        launchpad="Pons",
    )


def _gates(answer: str = PASS, **overrides):
    return build_report(
        MEME,
        [
            GateResult(gate=gate, answer=overrides.get(gate, answer),
                       source="fixture", observed_at=NOW, reason="fixture")
            for gate in GATES
        ],
        now=NOW,
    )


def _hot_anchor():
    return evaluate_anchor(
        AnchoredCoin(
            mint=MEME, anchor_key=NVDA, anchor_ticker="NVDA", anchor_claim=ANCHOR_ONCHAIN,
            liquidity_usd=Decimal("48000"), holder_count=310, buys=400, sells=280,
        ),
        StockAnchor(
            ticker="NVDA", token_address=NVDA, change_percent=Decimal("9.4"),
            relative_volume=Decimal("4.2"), news_sources=5,
        ),
    )


def _priced() -> MarketState:
    return MarketState(priced=True, liquidity_usd=Decimal("48000"), observed_at=NOW)


# ===========================================================================
# 8 — an unpriced verified launch is stage A and never an entry
# ===========================================================================


def test_8_a_verified_but_unpriced_launch_is_stage_a_only() -> None:
    decision = classify_stage(_proof(), market=None)

    assert decision.stage == STAGE_VERIFIED_LAUNCH
    assert decision.may_ping is False
    assert W_NO_MARKET in decision.wait_reasons
    assert decision.anchor_ticker == "NVDA"


def test_8b_stage_a_says_too_early_in_its_own_title_not_in_small_print() -> None:
    """A card that looks like an opportunity will be read as one.

    So the caveat is in the title, not the footer.
    """

    title = classify_stage(_proof(), market=None).title()
    assert "TOO EARLY FOR ENTRY" in title
    assert "UNPRICED" in title


def test_8c_an_unpriced_launch_cannot_reach_entry_even_with_perfect_gates() -> None:
    decision = classify_stage(
        _proof(), market=MarketState(priced=False), gates=_gates(PASS), anchor=_hot_anchor()
    )
    assert decision.stage == STAGE_VERIFIED_LAUNCH
    assert decision.stage != STAGE_ENTRY_CANDIDATE


def test_an_unresolved_anchor_never_reaches_the_operators_channel() -> None:
    # "We think this might be about NVIDIA" is not something to interrupt
    # anyone with. It stays in diagnostics until it resolves or expires.
    decision = classify_stage(_proof(verified=False), market=_priced(), gates=_gates(PASS))
    assert decision.stage == STAGE_DIAGNOSTIC
    assert decision.publishable is False
    assert decision.may_ping is False
    assert W_NO_ANCHOR in decision.wait_reasons


# ===========================================================================
# 9 — traction plus a safety failure is UNSAFE MOMENTUM, never an entry
# ===========================================================================


def test_9_a_resolved_anchor_with_traction_but_failed_safety_is_unsafe_momentum() -> None:
    decision = classify_stage(
        _proof(),
        market=_priced(),
        gates=_gates(PASS, SELL_ROUTE_OK=FAIL),
        anchor=_hot_anchor(),
    )
    assert decision.stage == STAGE_UNSAFE_MOMENTUM
    assert decision.may_ping is False
    assert W_UNSAFE in decision.wait_reasons
    assert "NOT AN ENTRY" in decision.title()


def test_9b_unknown_safety_is_treated_the_same_as_failed_safety() -> None:
    decision = classify_stage(
        _proof(), market=_priced(), gates=_gates(PASS, SELL_EVIDENCE_OK=UNKNOWN),
        anchor=_hot_anchor(),
    )
    assert decision.stage == STAGE_UNSAFE_MOMENTUM


def test_9c_unsafe_outranks_every_other_shortfall() -> None:
    # Not moving AND unsafe still reads as unsafe: the more dangerous fact wins
    # the headline rather than being averaged with the milder one.
    decision = classify_stage(
        _proof(), market=_priced(), gates=_gates(PASS, CONTRACT_SAFETY_OK=FAIL),
        anchor=_hot_anchor(), is_anchor_leader=False,
    )
    assert decision.stage == STAGE_UNSAFE_MOMENTUM


# ===========================================================================
# Stage C requires everything
# ===========================================================================


def test_stage_c_requires_every_condition_at_once() -> None:
    full = classify_stage(
        _proof(), market=_priced(), gates=_gates(PASS), anchor=_hot_anchor(),
        is_anchor_leader=True,
    )
    assert full.stage == STAGE_ENTRY_CANDIDATE
    assert full.may_ping is True
    assert full.why_now, "an entry card must be able to say why now"
    assert any("NVDA" in item for item in full.why_now)


def test_removing_any_single_requirement_drops_it_out_of_entry() -> None:
    for label, kwargs in (
        ("no gate report", {"gates": None, "anchor": _hot_anchor(), "is_anchor_leader": True}),
        ("gates not all passing", {"gates": _gates(PASS, MOMENTUM_OK=FAIL),
                                   "anchor": _hot_anchor(), "is_anchor_leader": True}),
        ("no anchor verdict", {"gates": _gates(PASS), "anchor": None,
                               "is_anchor_leader": True}),
        ("not the anchor leader", {"gates": _gates(PASS), "anchor": _hot_anchor(),
                                   "is_anchor_leader": False}),
    ):
        decision = classify_stage(_proof(), market=_priced(), **kwargs)
        assert decision.stage != STAGE_ENTRY_CANDIDATE, label
        assert decision.may_ping is False, label


def test_a_quiet_stock_leaves_it_at_traction_watch_with_the_reason_named() -> None:
    quiet_anchor = evaluate_anchor(
        AnchoredCoin(
            mint=MEME, anchor_key=NVDA, anchor_ticker="NVDA", anchor_claim=ANCHOR_ONCHAIN,
            liquidity_usd=Decimal("48000"), holder_count=310, buys=400, sells=280,
        ),
        StockAnchor(ticker="NVDA", token_address=NVDA, change_percent=Decimal("0.2"),
                    relative_volume=Decimal("0.9"), news_sources=0),
    )
    decision = classify_stage(
        _proof(), market=_priced(), gates=_gates(PASS), anchor=quiet_anchor
    )
    assert decision.stage == STAGE_TRACTION_WATCH
    assert W_ANCHOR_QUIET in decision.wait_reasons
    assert "not moving" in " ".join(decision.waits())


def test_a_traction_watch_card_states_what_it_is_waiting_for() -> None:
    decision = classify_stage(_proof(), market=_priced(), gates=_gates(PASS, MOMENTUM_OK=FAIL))
    assert decision.stage == STAGE_TRACTION_WATCH
    assert W_GATES in decision.wait_reasons
    assert decision.waits()


def test_only_an_entry_candidate_can_ever_ping() -> None:
    for stage_decision in (
        classify_stage(_proof(verified=False), market=_priced()),
        classify_stage(_proof(), market=None),
        classify_stage(_proof(), market=_priced(), gates=_gates(PASS, VERIFIED_POOL=FAIL)),
        classify_stage(_proof(), market=_priced(), gates=_gates(PASS)),
    ):
        assert stage_decision.may_ping is (stage_decision.stage == STAGE_ENTRY_CANDIDATE)


def test_every_decision_is_marked_research_only() -> None:
    assert classify_stage(
        _proof(), market=_priced(), gates=_gates(PASS), anchor=_hot_anchor(),
        is_anchor_leader=True,
    ).to_json()["research_only"] is True
