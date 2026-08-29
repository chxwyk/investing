"""Regression suite for the card confidence / buyer-consistency defects.

Three concrete display bugs are locked down here:

1. `/fomo opportunities` rendered `100%` confidence while economic authenticity
   was UNKNOWN, the bounded SOL activity sample was missing, and safety was not
   PASS.
2. The same card could show `14 independent buyer clusters` beside `0 raw
   buyers` — an internally impossible pair, because the two numbers came from
   different populations measured by different mechanisms.
3. A 100/100 organic-demand score was rendered without any signal that it was
   uncorroborated visible demand rather than confirmed authentic demand.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from smart_money_bot.lab.authenticity import (
    BAND_AUTHENTIC,
    AuthenticityAssessment,
    SolActivityProfile,
)
from smart_money_bot.lab.bankroll import BankrollState
from smart_money_bot.lab.decision import EvidenceQuality, SafetyStatus
from smart_money_bot.lab.entry import EntryContext, evaluate_entry
from smart_money_bot.lab.evidence import (
    CAP_ACTIVITY_UNAVAILABLE,
    CAP_AUTHENTICITY_PARTIAL,
    CAP_AUTHENTICITY_UNKNOWN,
    CAP_DEMAND_UNKNOWN,
    CAP_EVIDENCE_PARTIAL,
    CAP_EVIDENCE_UNKNOWN,
    CAP_SAFETY_FAIL,
    CAP_SAFETY_UNKNOWN,
    ORGANIC_CONFIRMED,
    ORGANIC_RAW,
    ORGANIC_UNVERIFIED,
    BuyerEvidence,
    buyer_evidence,
    confidence_cap,
    organic_demand_state,
    organic_demand_text,
)
from smart_money_bot.lab.lifecycle import new_lifecycle
from smart_money_bot.lab_runtime import _edge_confidence
from smart_money_bot.models import RunnerFundingObservation, RunnerMarketSnapshot
from smart_money_bot.quality import build_demand_profile
from smart_money_bot.runner import summarize_forensics

MINT = "So11111111111111111111111111111111111111112"
D = Decimal


# ---------------------------------------------------------------------------
# 1. confidence must reflect evidence completeness
# ---------------------------------------------------------------------------


def test_confidence_is_uncapped_only_on_complete_evidence() -> None:
    cap = confidence_cap(
        evidence_quality=EvidenceQuality.COMPLETE,
        authenticity_quality=EvidenceQuality.COMPLETE,
        activity_available=True,
        safety_status="PASS",
        demand_confidence="HIGH",
    )
    assert cap.ceiling == D("100")
    assert not cap.limited
    assert cap.apply(D("100")) == D("100.00")


@pytest.mark.parametrize(
    ("kwargs", "ceiling"),
    [
        ({"evidence_quality": EvidenceQuality.PARTIAL}, CAP_EVIDENCE_PARTIAL),
        ({"evidence_quality": EvidenceQuality.UNKNOWN}, CAP_EVIDENCE_UNKNOWN),
        ({"authenticity_quality": EvidenceQuality.PARTIAL}, CAP_AUTHENTICITY_PARTIAL),
        ({"authenticity_quality": EvidenceQuality.UNKNOWN}, CAP_AUTHENTICITY_UNKNOWN),
        ({"activity_available": False}, CAP_ACTIVITY_UNAVAILABLE),
        ({"safety_status": "UNKNOWN"}, CAP_SAFETY_UNKNOWN),
        ({"safety_status": "FAIL"}, CAP_SAFETY_FAIL),
        ({"demand_confidence": "UNKNOWN"}, CAP_DEMAND_UNKNOWN),
    ],
)
def test_each_missing_evidence_kind_caps_confidence(kwargs, ceiling) -> None:
    cap = confidence_cap(**kwargs)
    assert cap.ceiling == ceiling
    assert cap.limited
    assert cap.apply(D("100")) == ceiling.quantize(D("0.01"))
    assert cap.reasons


def test_the_strictest_ceiling_wins() -> None:
    cap = confidence_cap(
        evidence_quality=EvidenceQuality.PARTIAL,
        authenticity_quality=EvidenceQuality.UNKNOWN,
        activity_available=False,
        safety_status="FAIL",
        demand_confidence="HIGH",
    )
    assert cap.ceiling == CAP_SAFETY_FAIL
    assert len(cap.reasons) == 4


def test_capping_never_raises_a_low_confidence() -> None:
    cap = confidence_cap(authenticity_quality=EvidenceQuality.UNKNOWN)
    assert cap.apply(D("12")) == D("12.00")
    assert cap.apply(None) is None


def test_perfect_organic_score_no_longer_reports_full_confidence() -> None:
    """The exact reported defect: organic 100 with no authenticity evidence."""

    quality = SimpleNamespace(
        organic_score=D("100"), demand=SimpleNamespace(confidence="HIGH")
    )
    unknown_authenticity = AuthenticityAssessment()

    capped = _edge_confidence(quality, unknown_authenticity, safety_status="PASS")
    assert capped == CAP_AUTHENTICITY_UNKNOWN.quantize(D("0.01"))
    assert capped < D("100")

    failing = _edge_confidence(quality, unknown_authenticity, safety_status="FAIL")
    assert failing == CAP_SAFETY_FAIL.quantize(D("0.01"))

    degraded = _edge_confidence(
        quality, unknown_authenticity, safety_status="PASS", data_degraded=True
    )
    assert degraded <= CAP_AUTHENTICITY_UNKNOWN


@pytest.mark.parametrize("organic", ["100", "60", "40", "20"])
@pytest.mark.parametrize("demand_confidence", ["UNKNOWN", "HIGH"])
def test_capping_is_never_less_conservative_than_the_previous_behaviour(
    organic, demand_confidence
) -> None:
    """The cap may only lower a confidence, never raise one.

    Before the cap, an untraced candidate simply halved its confidence.  A
    ceiling alone would have *raised* that for a low organic score, so the
    halving is kept and the ceiling applied on top.
    """

    quality = SimpleNamespace(
        organic_score=D(organic), demand=SimpleNamespace(confidence=demand_confidence)
    )
    previous = D(organic) / 2 if demand_confidence == "UNKNOWN" else D(organic)
    current = _edge_confidence(
        quality, AuthenticityAssessment(), safety_status="PASS"
    )
    assert current <= previous


def test_complete_evidence_still_reaches_full_confidence() -> None:
    quality = SimpleNamespace(
        organic_score=D("100"), demand=SimpleNamespace(confidence="HIGH")
    )
    complete = AuthenticityAssessment(
        score=D("100"),
        band=BAND_AUTHENTIC,
        quality=EvidenceQuality.COMPLETE,
        activity=SolActivityProfile(quality=EvidenceQuality.COMPLETE, sampled_wallets=20),
    )
    assert _edge_confidence(quality, complete, safety_status="PASS") == D("100.00")


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
        "organic_score": D("100"),
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
        "regime": MarketRegime(state=NORMAL, samples=20),
        "expected_upside_percent": D("60"),
        "expected_downside_percent": D("30"),
        "edge_confidence": D("100"),
        "move_since_first_surface_percent": D("20"),
        "signal_age_seconds": 60,
    }
    base.update(overrides)
    return EntryContext(**base)


def test_entry_engine_refuses_to_persist_an_unjustified_confidence() -> None:
    """A caller handing in 100 cannot push it into the persisted decision."""

    result = evaluate_entry(
        _entry_context(authenticity=AuthenticityAssessment()),
        lifecycle=new_lifecycle(MINT, now=0),
        bankroll=BankrollState(),
    )
    assert result.decision.edge_confidence is not None
    assert result.decision.edge_confidence <= CAP_AUTHENTICITY_UNKNOWN
    assert result.decision.evidence["confidence_limited_by"]
    assert result.decision.evidence["confidence_ceiling"] == str(CAP_AUTHENTICITY_UNKNOWN)


def test_safety_fail_and_unknown_stay_fail_closed_with_the_cap_applied() -> None:
    """The cap must not soften the fail-closed entry gates."""

    for status, eligible in (("FAIL", False), ("UNKNOWN", False)):
        result = evaluate_entry(
            _entry_context(safety_status=status, safety_entry_eligible=eligible),
            lifecycle=new_lifecycle(MINT, now=0),
            bankroll=BankrollState(),
        )
        assert not result.entry_eligible
        assert result.decision.size_usd == D("0")
        assert result.decision.safety is not SafetyStatus.PASS
        assert result.decision.edge_confidence <= CAP_SAFETY_UNKNOWN


def test_capping_does_not_block_an_otherwise_valid_entry() -> None:
    """Preserved behaviour: an unsampled activity profile still permits entry."""

    result = evaluate_entry(
        _entry_context(authenticity=AuthenticityAssessment()),
        lifecycle=new_lifecycle(MINT, now=0),
        bankroll=BankrollState(),
    )
    assert result.entry_eligible
    assert result.decision.size_usd > D("0")


# ---------------------------------------------------------------------------
# 2. buyer / cluster population consistency
# ---------------------------------------------------------------------------


def _traced_demand(traced: int = 14, *, raw_buyers: int = 0):
    rows = tuple(
        RunnerFundingObservation(wallet=f"w{index}", funder=f"f{index}", trace_complete=True)
        for index in range(traced)
    )
    forensics = summarize_forensics(rows, raw_unique_buyers=raw_buyers, checked_at=1)
    snapshot = RunnerMarketSnapshot(
        mint=MINT, captured_at=1, verified_unique_buyers=raw_buyers
    )
    return build_demand_profile(forensics=forensics, current=snapshot), forensics


def test_zero_verified_buyers_beside_a_real_trace_is_never_rendered_as_zero() -> None:
    """The exact reported defect: 14 independent clusters beside 0 raw buyers."""

    demand, forensics = _traced_demand(14, raw_buyers=0)
    assert demand.raw_buyers == 0
    assert demand.estimated_independent_buyers == 14

    buyers = buyer_evidence(demand, forensics)
    assert buyers.verified_buyers is None
    assert buyers.verified_buyers_text == "not observed"
    assert buyers.independence_text == "14 of 14 traced"
    assert "0" not in buyers.verified_buyers_text


def test_independence_is_always_reported_against_its_own_population() -> None:
    demand, forensics = _traced_demand(9, raw_buyers=0)
    buyers = buyer_evidence(demand, forensics)
    assert buyers.traced
    assert buyers.independence_text.endswith("traced")
    assert str(buyers.traced_wallets) in buyers.independence_text


def test_an_impossible_buyer_combination_can_never_be_constructed() -> None:
    with pytest.raises(ValueError):
        BuyerEvidence(verified_buyers=0, traced_wallets=5, independent_clusters=14)


def test_mixed_populations_are_clamped_rather_than_rendered() -> None:
    """A caller mixing fields gets a coherent view, not an impossible one."""

    mixed = SimpleNamespace(
        raw_buyers=0,
        traced_wallets=5,
        estimated_independent_buyers=14,
        largest_cluster_wallets=1,
        independence_ratio=None,
        confidence="HIGH",
    )
    buyers = buyer_evidence(mixed)
    assert buyers.independent_clusters == 5
    assert buyers.independent_clusters <= buyers.traced_wallets


def test_untraced_candidate_reports_not_traced_not_zero() -> None:
    snapshot = RunnerMarketSnapshot(mint=MINT, captured_at=1, verified_unique_buyers=0)
    demand = build_demand_profile(forensics=None, current=snapshot)
    buyers = buyer_evidence(demand, None)
    assert not buyers.traced
    assert buyers.independence_text == "not traced"
    assert buyers.verified_buyers_text == "none observed"


def test_real_verified_buyers_are_reported_as_a_count() -> None:
    demand, forensics = _traced_demand(10, raw_buyers=7)
    buyers = buyer_evidence(demand, forensics)
    assert buyers.verified_buyers == 7
    assert buyers.verified_buyers_text == "7"


# ---------------------------------------------------------------------------
# 3. organic demand must be labelled when unverified
# ---------------------------------------------------------------------------


def test_organic_demand_is_labelled_raw_without_authenticity_evidence() -> None:
    assert organic_demand_state(authenticity_quality=EvidenceQuality.UNKNOWN) == ORGANIC_RAW
    text = organic_demand_text(D("100"), authenticity_quality=EvidenceQuality.UNKNOWN)
    assert "100" in text
    assert "RAW" in text
    assert "unverified" in text.lower()


def test_organic_demand_is_labelled_unverified_on_partial_authenticity() -> None:
    assert (
        organic_demand_state(authenticity_quality=EvidenceQuality.PARTIAL)
        == ORGANIC_UNVERIFIED
    )
    text = organic_demand_text(D("100"), authenticity_quality=EvidenceQuality.PARTIAL)
    assert "UNVERIFIED" in text


def test_a_weak_demand_trace_also_downgrades_the_organic_label() -> None:
    assert (
        organic_demand_state(
            authenticity_quality=EvidenceQuality.COMPLETE, demand_confidence="LOW"
        )
        == ORGANIC_UNVERIFIED
    )


def test_confirmed_organic_demand_carries_no_qualifier() -> None:
    assert (
        organic_demand_state(
            authenticity_quality=EvidenceQuality.COMPLETE, demand_confidence="HIGH"
        )
        == ORGANIC_CONFIRMED
    )
    text = organic_demand_text(
        D("100"), authenticity_quality=EvidenceQuality.COMPLETE, demand_confidence="HIGH"
    )
    assert text == "100"


def test_entry_decision_records_the_organic_demand_state() -> None:
    result = evaluate_entry(
        _entry_context(authenticity=AuthenticityAssessment()),
        lifecycle=new_lifecycle(MINT, now=0),
        bankroll=BankrollState(),
    )
    assert result.decision.evidence["organic_demand_state"] == ORGANIC_RAW


# ---------------------------------------------------------------------------
# rendered-card regressions
# ---------------------------------------------------------------------------


def _lab_result(*, authenticity: AuthenticityAssessment, safety_status: str = "PASS"):
    """Build the exact objects `/fomo opportunities` renders."""

    from smart_money_bot.lab.smartmoney import SmartMoneyAssessment
    from smart_money_bot.lab_runtime import LabEvaluation
    from smart_money_bot.models import (
        RunnerQualityAssessment,
        RunnerSafetyAssessment,
        RunnerScoreBreakdown,
    )

    demand, forensics = _traced_demand(14, raw_buyers=0)
    quality = RunnerQualityAssessment(
        momentum_score=D("70"),
        opportunity_score=D("72"),
        organic_score=D("100"),
        stage="ENTRY_CANDIDATE",
        qualified=True,
        demand=demand,
        evaluated_at=1_000,
    )
    snapshot = RunnerMarketSnapshot(
        mint=MINT,
        captured_at=1_000,
        price_usd=D("0.001"),
        market_cap_usd=D("40000"),
        liquidity_usd=D("60000"),
        buys_5m=120,
        sells_5m=40,
        route_available=True,
        buy_route_status="PASS",
        sell_route_status="PASS",
    )
    candidate = SimpleNamespace(
        mint=MINT,
        symbol="REAL",
        name="Real Token",
        first=snapshot,
        current=snapshot,
        quality=quality,
        safety=RunnerSafetyAssessment(status=safety_status, entry_eligible=False),
        forensics=forensics,
        breakdown=RunnerScoreBreakdown(),
        smart_wallets=(),
        stage="ENTRY_CANDIDATE",
        pair_url="",
        generated_at=1_000,
        pair_created_at=None,
        chain_created_at=None,
        graduated_at=None,
        graduation_source="TEST",
        score=D("70"),
        tier="TEST",
        overextended=False,
        earliest_smart_entry_age_seconds=None,
        x_evidence=SimpleNamespace(available=False, error=None),
        why_surfaced=(),
        detection_forensics=forensics,
    )
    evaluation = evaluate_entry(
        _entry_context(authenticity=authenticity, safety_status=safety_status),
        lifecycle=new_lifecycle(MINT, now=0),
        bankroll=BankrollState(),
    )
    result = LabEvaluation(
        mint=MINT,
        identity=__import__(
            "smart_money_bot.lab.identity", fromlist=["build_token_identity"]
        ).build_token_identity(MINT, name="Real Token", symbol="REAL"),
        lifecycle=new_lifecycle(MINT, now=0),
        evaluation=evaluation,
        authenticity=authenticity,
        smart_money=SmartMoneyAssessment(),
        reentry=None,
        position=None,
    )
    return candidate, result


def test_opportunity_card_never_prints_full_confidence_on_thin_evidence() -> None:
    from smart_money_bot.bot import _lab_opportunity_embed

    candidate, result = _lab_result(
        authenticity=AuthenticityAssessment(), safety_status="FAIL"
    )
    embed = _lab_opportunity_embed(
        candidate, result, index=0, total=1, referral_code=None
    )
    rendered = "\n".join(field.value for field in embed.fields)
    assert "confidence `100" not in rendered
    assert "capped:" in rendered


def test_opportunity_card_never_prints_an_impossible_buyer_pair() -> None:
    from smart_money_bot.bot import _lab_opportunity_embed

    candidate, result = _lab_result(authenticity=AuthenticityAssessment())
    embed = _lab_opportunity_embed(
        candidate, result, index=0, total=1, referral_code=None
    )
    rendered = "\n".join(field.value for field in embed.fields)
    assert "14 of 14 traced" in rendered
    assert "verified buyers `not observed`" in rendered
    assert "verified buyers `0`" not in rendered


def test_opportunity_card_labels_uncorroborated_organic_demand() -> None:
    from smart_money_bot.bot import _lab_opportunity_embed

    candidate, result = _lab_result(authenticity=AuthenticityAssessment())
    embed = _lab_opportunity_embed(
        candidate, result, index=0, total=1, referral_code=None
    )
    quality_field = next(field for field in embed.fields if field.name == "QUALITY")
    assert "RAW" in quality_field.value
    assert "unverified authenticity" in quality_field.value


def test_forensic_card_pairs_only_comparable_populations() -> None:
    """`/fomo forensic` used to print "Raw buyers 0 • independent clusters 14"."""

    from smart_money_bot.bot import _runner_forensic_embed
    from smart_money_bot.models import (
        RunnerCandidate,
        RunnerQualityAssessment,
        RunnerSafetyAssessment,
        RunnerScoreBreakdown,
    )

    demand, forensics = _traced_demand(14, raw_buyers=0)
    snapshot = RunnerMarketSnapshot(
        mint=MINT,
        captured_at=1_000,
        price_usd=D("0.001"),
        market_cap_usd=D("40000"),
        liquidity_usd=D("60000"),
    )
    candidate = RunnerCandidate(
        mint=MINT,
        symbol="REAL",
        name="Real Token",
        first_seen_at=900,
        graduated_at=None,
        graduation_source="TEST",
        first=snapshot,
        current=snapshot,
        score=D("70"),
        tier="TEST",
        breakdown=RunnerScoreBreakdown(),
        quality=RunnerQualityAssessment(demand=demand, evaluated_at=1_000),
        safety=RunnerSafetyAssessment(status="PASS"),
        forensics=forensics,
        generated_at=1_000,
    )
    embed = _runner_forensic_embed(candidate, None)
    rendered = "\n".join(field.value for field in embed.fields)
    assert "Raw buyers `0`" not in rendered
    assert "14 of 14 traced" in rendered
    assert "Verified buyers `not observed`" in rendered
