"""Regression suite for the wrong-token hotfix.

The production failure this file exists to prevent, in one sentence: the bot
alerted on a brand-new token that merely shared a ticker with the one the
operator was watching, called it an ORGANIC RUNNER, said the safety status was
UNKNOWN in the same card, and offered a buy button.

Four independent defects had to line up for that, and each one has its own
tests below:

1. a text search resolved an identity, and broke the tie by picking the
   *youngest* pair — which is always the clone;
2. "organic" was inferred from a raw buy/sell count, which four wallets in a
   loop can manufacture;
3. a card whose validation had not finished still led with actionable
   language; and
4. the buy CTA rendered regardless of what was known about the token.

The rule the whole file enforces: a token's identity is its chain plus its
exact mint.  Name and ticker are display metadata.  Failure is preferable to
substitution.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from decimal import Decimal

import pytest

import smart_money_bot.engine as engine_module
from smart_money_bot.bot import _token_view
from smart_money_bot.fast_alerts import build_early_alert
from smart_money_bot.lab.early import (
    TIER_EARLY_HEADS_UP,
    TIER_EARLY_RUNNER,
    TIER_ORGANIC_RUNNER,
    WHY_INDEPENDENCE_UNCONFIRMED,
    EarlySignals,
    evaluate_early_signal,
)
from smart_money_bot.news import DexNarrativeMatcher
from smart_money_bot.token_identity import (
    METHOD_EXACT_MINT,
    METHOD_SYMBOL_SEARCH,
    METHOD_UNRESOLVED,
    SOURCE_RESOLVED_MISMATCH,
    UNRESOLVED_EXACT_MINT,
    ResolutionProvenance,
    TokenIdentityError,
    assert_exact_propagation,
    detect_symbol_collision,
    exact,
    from_symbol_search,
    unresolved,
)

D = Decimal

# The mint the operator was actually watching, and the same-ticker newcomer the
# bot alerted on instead.
WATCHED = "GPR7Ax4kQ2mVn8hLdT6yWc3JbRfE9uZsXqM1oP5tH4dK"
CLONE = "7TqH1d4Vf9QG578vB99Q7ewFQPoxSYqBDxSAzBpBpump"
SYMBOL = "GPRO"


# ===========================================================================
# 1. The same-symbol clone
# ===========================================================================


def _pair(mint: str, *, age_minutes: int, liquidity: str) -> dict:
    import time

    return {
        "chainId": "solana",
        "baseToken": {"address": mint, "symbol": SYMBOL, "name": SYMBOL},
        "liquidity": {"usd": liquidity},
        "pairCreatedAt": int(time.time() * 1000) - age_minutes * 60_000,
        "txns": {"m5": {"buys": 40, "sells": 5}},
        "volume": {"m5": "9000"},
        "marketCap": "40000",
    }


def test_a_same_symbol_clone_can_never_be_chosen_over_the_watched_token() -> None:
    """Hotfix section 2: two tokens, one ticker, no basis to pick — so pick neither.

    The pre-hotfix tie-break sorted youngest-first, which reliably selected the
    freshest clone.  Refusing to answer is the only honest outcome.
    """

    matcher = DexNarrativeMatcher(min_liquidity_usd=D("1000"), max_age_minutes=600)
    pairs = [
        _pair(WATCHED, age_minutes=300, liquidity="90000"),
        _pair(CLONE, age_minutes=2, liquidity="6000"),
    ]

    async def _fake_search(query: str):
        return pairs

    matcher._search_pairs = _fake_search  # type: ignore[method-assign]

    assert asyncio.run(matcher.search(SYMBOL)) is None


def test_the_youngest_pair_is_not_a_tie_break_when_one_token_matches() -> None:
    """The refusal must not cost us the unambiguous case."""

    matcher = DexNarrativeMatcher(min_liquidity_usd=D("1000"), max_age_minutes=600)

    async def _fake_search(query: str):
        return [_pair(WATCHED, age_minutes=300, liquidity="90000")]

    matcher._search_pairs = _fake_search  # type: ignore[method-assign]

    match = asyncio.run(matcher.search(SYMBOL))
    assert match is not None
    assert match.mint == WATCHED


def test_a_text_resolved_lead_is_never_a_verified_identity() -> None:
    """Section 2: a symbol search produces a lead, and the card must say so."""

    provenance = from_symbol_search(
        CLONE, source="dex_search", query=SYMBOL, collision_mints=(WATCHED, CLONE)
    )

    assert provenance.resolution_method == METHOD_SYMBOL_SEARCH
    assert provenance.identity_verified is False
    assert provenance.symbol_collision is True
    # It started from text, so there is no source address it could have kept.
    assert provenance.source_mint == ""


# ===========================================================================
# 2. Exact enrichment that fails
# ===========================================================================


def test_failed_exact_enrichment_reports_unresolved_instead_of_substituting() -> None:
    """Section 2: "failure is preferable to substitution"."""

    provenance = unresolved(WATCHED, source="fomo_trending", note="provider 404")

    assert provenance.resolution_method == METHOD_UNRESOLVED
    assert provenance.unresolved is True
    assert provenance.identity_verified is False
    assert provenance.failure_reason() == UNRESOLVED_EXACT_MINT
    # Crucially: it did not quietly acquire some other token's address.
    assert provenance.resolved_mint == ""
    assert provenance.source_mint == WATCHED


def test_an_unresolved_candidate_is_not_promotable_and_gets_no_buy_control() -> None:
    provenance = unresolved(WATCHED, source="fomo_trending")

    assert provenance.identity_verified is False
    buttons = {item.label: item for item in _token_view(WATCHED).children}
    assert "Buy on Jupiter" not in buttons
    # Research links survive: the operator still has to be able to go look.
    assert "Open in Fomo" in buttons
    assert "Solscan" in buttons


# ===========================================================================
# 3. Symbol collision is informational, never a selector
# ===========================================================================


def test_a_symbol_collision_groups_tokens_and_refuses_to_rank_them() -> None:
    """Section 3: the flag raises the guard; it never picks a winner."""

    collision = detect_symbol_collision(
        SYMBOL,
        {WATCHED: "GPRO", CLONE: "gpro", "So11111111111111111111111111111111111111112": "SOL"},
        subject_mint=WATCHED,
    )

    assert collision.detected is True
    assert collision.count == 2
    assert set(collision.mints) == {WATCHED, CLONE}
    # Both survive as separate entities; neither is merged into the other.
    assert WATCHED in collision.warning_line(WATCHED)
    assert "no other" in collision.warning_line(WATCHED)
    # There is no "winner", "best" or "preferred" accessor to misuse.
    assert not [name for name in dir(collision) if name in {"best", "winner", "preferred"}]


def test_a_collision_does_not_change_which_mint_a_card_is_about() -> None:
    alert = build_early_alert(
        mint=WATCHED,
        name="Grok Pocket",
        symbol=SYMBOL,
        fomo_url="https://fomo.family/coin?address=" + WATCHED,
        verdict=_verdict(),
        age_seconds=82,
        first_seen_seconds_ago=8,
        first_seen_market_cap_usd=D("31180"),
        alert_market_cap_usd=D("33100"),
        current_market_cap_usd=D("33100"),
        liquidity_usd=D("6900"),
        buys=26,
        sells=6,
        symbol_collision=True,
    )

    assert alert.mint == WATCHED
    assert alert.token_mint == WATCHED
    assert alert.symbol_collision is True
    rendered = alert.spec.description + " ".join(
        field.name + field.value for field in alert.spec.fields
    )
    # Section 9: the exact mint is on the card, and the clone's is not.
    assert WATCHED in rendered
    assert CLONE not in rendered
    assert "SYMBOL COLLISION" in rendered


# ===========================================================================
# 4. Source mint and resolved mint must agree
# ===========================================================================


def test_a_source_resolved_mismatch_is_a_hard_failure() -> None:
    """Section 4: when the source supplied an address, it must be the one used."""

    swapped = ResolutionProvenance(
        source="fomo_trending",
        source_mint=WATCHED,
        resolved_mint=CLONE,
        resolution_method=METHOD_EXACT_MINT,
    )

    assert swapped.substituted is True
    assert swapped.identity_verified is False
    assert swapped.failure_reason() == SOURCE_RESOLVED_MISMATCH
    with pytest.raises(TokenIdentityError):
        swapped.verify()


def test_every_pipeline_stage_asserts_the_mint_survived_it() -> None:
    # The good case is silent.
    assert_exact_propagation(WATCHED, WATCHED, stage="enrichment")
    for stage in ("enrichment", "scoring", "persistence", "discord render"):
        with pytest.raises(TokenIdentityError) as excinfo:
            assert_exact_propagation(WATCHED, CLONE, stage=stage)
        assert stage in str(excinfo.value)


def test_an_exact_address_carried_end_to_end_is_the_only_verified_identity() -> None:
    provenance = exact(WATCHED, source="fomo_trending")

    assert provenance.identity_verified is True
    assert provenance.source_mint == provenance.resolved_mint == WATCHED
    assert provenance.source_chain == provenance.resolved_chain == "solana"
    assert provenance.failure_reason() == ""
    payload = provenance.to_json()
    assert payload["identity_verified"] is True
    assert set(payload) >= {
        "source",
        "source_chain",
        "source_mint",
        "resolved_chain",
        "resolved_mint",
        "resolution_method",
        "symbol_collision",
        "identity_verified",
    }


def test_the_narrative_lane_asserts_propagation_and_flags_unverified_leads() -> None:
    """The lane that produced the wrong card now checks its own hand-off."""

    source = inspect.getsource(engine_module.SmartMoneyEngine._run_narrative_match)
    assert "assert_exact_propagation" in source
    assert "from_symbol_search" in source
    assert "identity_verified" in source


# ===========================================================================
# 5-6. A card that does not know is not allowed to sound like it does
# ===========================================================================


def _signals(**overrides) -> EarlySignals:
    payload = {
        "mint": WATCHED,
        "now": 1_700_000_000,
        "first_seen_at": 1_700_000_000 - 8,
        "pair_age_seconds": 82,
        "market_cap_usd": D("33100"),
        "first_seen_market_cap_usd": D("31180"),
        "liquidity_usd": D("6900"),
        "volume_5m_usd": D("5200"),
        "price_change_5m_percent": D("14"),
        "buys_5m": 26,
        "sells_5m": 6,
        "independent_buyers_5m": 19,
        "route_available": True,
    }
    payload.update(overrides)
    return EarlySignals(**payload)


def _verdict(**overrides):
    return evaluate_early_signal(_signals(**overrides))


def _early_alert(**overrides):
    payload = {
        "mint": WATCHED,
        "name": "Grok Pocket",
        "symbol": SYMBOL,
        "fomo_url": "https://fomo.family/coin?address=" + WATCHED,
        "verdict": _verdict(),
        "age_seconds": 82,
        "first_seen_seconds_ago": 8,
        "first_seen_market_cap_usd": D("31180"),
        "alert_market_cap_usd": D("33100"),
        "current_market_cap_usd": D("33100"),
        "liquidity_usd": D("6900"),
        "buys": 26,
        "sells": 6,
    }
    payload.update(overrides)
    return build_early_alert(**payload)


def test_safety_unknown_can_never_render_as_organic_runner_look_now() -> None:
    """Section 5: the reported card said both things at once.  Now it cannot."""

    assert _verdict().tier == TIER_ORGANIC_RUNNER  # the tier itself is unchanged
    alert = _early_alert(safety_status="UNKNOWN")

    assert "LOOK NOW" not in alert.spec.title
    assert "RESEARCH CANDIDATE" in alert.spec.title
    assert "VALIDATION PENDING" in alert.spec.title
    # The tier is still reported — it is real information, it just does not lead.
    assert "ORGANIC RUNNER" in alert.spec.title
    assert "LOOK NOW" not in alert.spec.compact_description


@pytest.mark.parametrize("status", ["UNKNOWN", "PENDING", "VALIDATION RUNNING", "FAILED"])
def test_no_unvalidated_status_produces_actionable_language(status: str) -> None:
    alert = _early_alert(safety_status=status)

    for phrase in ("LOOK NOW", "BUY NOW", "APE", "SEND IT"):
        assert phrase not in alert.spec.title
        assert phrase not in alert.spec.compact_description


def test_a_card_published_before_validation_is_never_entry_eligible() -> None:
    """Section 6: validation still running means no entry and no buy control."""

    alert = _early_alert(safety_status="VALIDATION RUNNING")
    state = {field.name: field.value for field in alert.spec.fields}["STATE"]

    assert "Entry eligible: **NO**" in state
    assert "Trade CTA: **DISABLED**" in state
    assert "VALIDATION RUNNING" in state
    assert alert.trade_eligible is False
    assert _verdict().entry_eligible is False


def test_the_card_states_what_it_does_not_know_about_identity_and_safety() -> None:
    alert = _early_alert(safety_status="UNKNOWN", identity_verified=False, symbol_collision=True)
    fields = {field.name: field.value for field in alert.spec.fields}

    assert "Identity: **UNVERIFIED**" in fields["STATE"]
    assert "Symbol collision: **YES**" in fields["STATE"]
    assert "Safety: **UNKNOWN**" in fields["STATE"]
    assert "⚠ IDENTITY UNVERIFIED" in fields
    assert "⚠ SYMBOL COLLISION" in fields


def test_there_is_no_buy_control_left_to_gate() -> None:
    """Section 6, hardened in v2.54: research links always, transactions never.

    The old version of this test asserted that ``Buy on Jupiter`` appeared
    once a caller passed ``trade_eligible=True``.  No caller in the repository
    ever did, and the sibling ``Sell on Jupiter`` button was not gated at all
    — it rendered on every card, including cards for tokens whose safety was
    UNKNOWN, and it opened a live swap screen preloaded with the mint.  This
    is a research and paper-observation system; the honest state is that no
    such control exists, and no flag re-enables one.
    """

    blocked = {item.label: item for item in _token_view(WATCHED).children}

    for label in ("Open in Fomo", "Open in Pump.fun", "Open in GMGN", "Chart", "Solscan"):
        assert label in blocked
    assert "Buy on Jupiter" not in blocked
    assert "Sell on Jupiter" not in blocked
    assert not any("jup.ag" in item.url for item in blocked.values())
    assert "trade_eligible" not in inspect.signature(_token_view).parameters


def test_the_early_lane_still_carries_its_gate_state(settings) -> None:
    """The eligibility flag survives on the alert even with no control to gate.

    ``trade_eligible`` remains on :class:`FastAlert` because other things read
    it — the card prints ``Trade CTA: DISABLED`` from it, and the publication
    guard clears it on refusal.  What it no longer does is decide whether a
    button that spends money appears, because that button is gone.
    """

    from smart_money_bot.fast_alerts import FastAlert

    assert "trade_eligible" in {f.name for f in dataclasses.fields(FastAlert)}
    assert _early_alert().trade_eligible is False


# ===========================================================================
# 7. "Organic" is a claim about who is buying
# ===========================================================================


def test_a_raw_buy_count_does_not_make_a_token_organic() -> None:
    """Section 7: 542 buys against 144 sells is activity, not proven demand.

    Four wallets trading in a loop produce exactly these numbers.  Without
    independent-buyer evidence the token still surfaces — it just is not called
    organic.
    """

    verdict = evaluate_early_signal(
        _signals(
            buys_5m=542,
            sells_5m=144,
            volume_5m_usd=D("180000"),
            independent_buyers_5m=None,
        )
    )

    assert "ORGANIC_MARKET_EVIDENCE" not in verdict.evidence_categories
    assert verdict.tier != TIER_ORGANIC_RUNNER
    assert WHY_INDEPENDENCE_UNCONFIRMED in verdict.why_not_pinged


def test_confirmed_independent_buyers_are_what_earn_the_organic_label() -> None:
    confirmed = evaluate_early_signal(
        _signals(buys_5m=542, sells_5m=144, volume_5m_usd=D("180000"), independent_buyers_5m=120)
    )

    assert "ORGANIC_MARKET_EVIDENCE" in confirmed.evidence_categories
    assert confirmed.tier == TIER_ORGANIC_RUNNER
    assert WHY_INDEPENDENCE_UNCONFIRMED not in confirmed.why_not_pinged


def test_unconfirmed_independence_still_leaves_the_token_visible() -> None:
    """Restraint, not blindness: the operator keeps seeing it."""

    verdict = evaluate_early_signal(
        _signals(buys_5m=542, sells_5m=144, volume_5m_usd=D("180000"), independent_buyers_5m=None)
    )

    assert verdict.visible is True
    assert verdict.tier in {TIER_EARLY_HEADS_UP, TIER_EARLY_RUNNER}


def test_unknown_independence_is_not_the_same_as_zero_independence() -> None:
    """The engine returns ``None`` when it cannot know, and the gate respects it."""

    source = inspect.getsource(engine_module.SmartMoneyEngine._independent_buyers_5m)
    assert "-> int | None" in source
    assert "return None" in source
    assert EarlySignals(mint=WATCHED, now=0).independent_buyers_5m is None
