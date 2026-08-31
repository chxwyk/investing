"""Tests for the Terminal-style public/on-chain intelligence engine (v2.43).

Written against product invariants rather than implementation:

* five timeframes are computed **independently**, with no leakage (section 85);
* acceleration on the short windows raises priority (section 86);
* raw transaction count is not demand — clustered wallets collapse to one actor
  (sections 87, 88), and genuinely independent ones do not (section 89);
* dev selling, rising concentration and distributing bundles raise risk
  (sections 90-92);
* a healthy almost-bonded token can surface *before* graduation (section 93) and
  keeps its history across the transition (section 94);
* paid DEX placement cannot carry our public model (section 95);
* independent evidence families count once each, duplicate feeds do not (§96);
* neither Terminal nor Fomo can be claimed without an authorised feed (§97, 98);
* a later pump cannot change an earlier decision (section 99);
* and nothing anywhere can spend a real dollar (section 100).
"""

from __future__ import annotations

import struct
from decimal import Decimal

import pytest

from smart_money_bot.database import Database
from smart_money_bot.pump_chain import bonding_curve_address, decode_bonding_curve
from smart_money_bot.trenches import (
    BUNDLE_RISK_HIGH,
    BUNDLE_RISK_NONE,
    CADENCE_HOT,
    CADENCE_NORMAL,
    CONCENTRATION_IMPROVING,
    CONCENTRATION_WORSENING,
    DEV_HISTORY_HIGH_FAILURE,
    DEV_HOLDING_SELLING,
    DEV_HOLDING_STABLE,
    FAIL,
    FORBIDDEN_RANKING_CLAIMS,
    MODEL_NAME,
    MOMENTUM_INCREASING,
    PASS,
    PUBLIC_TRENDING_MODEL,
    RISK_BUNDLE,
    RISK_CONCENTRATION,
    RISK_DEV,
    RISK_LIQUIDITY,
    SHAPE_FADING,
    SHAPE_SUSTAINED_TREND,
    SHAPE_VERY_EARLY_ACCELERATION,
    STAGE_ALMOST_BONDED,
    STAGE_GRADUATING,
    STAGE_MID_CURVE,
    STAGE_NEW,
    STAGE_PUMPSWAP,
    STAGE_UNKNOWN,
    SUPPRESS_CLUSTERED_DEMAND,
    SUPPRESS_DEV_RISK,
    SUPPRESS_NO_NAMED_REASON,
    TF_1H,
    TF_1M,
    TF_5M,
    TIMEFRAMES,
    TRENCH_RUNNER,
    UNKNOWN,
    BondingCurveState,
    BuyerRecord,
    DevProfile,
    HolderAccount,
    MarketObservation,
    Nomination,
    PriorToken,
    SlotTrade,
    SourceRef,
    TokenMetadata,
    assert_honest_ranking_name,
    assess_bundles,
    assess_concentration_trend,
    assess_depth,
    assess_dev_history,
    assess_dev_holding,
    assess_large_buy,
    assess_participants,
    bonding_milestones,
    build_consensus,
    build_holder_snapshot,
    build_risk_profile,
    build_timeframe_profile,
    cadence_tier,
    classify_lifecycle,
    classify_wallet_age,
    count_independent,
    decide_trench_tier,
    detect_reuse,
    rank_public_trend,
    score_public_trend,
    score_pump_trench,
    window_metrics,
)
from smart_money_bot.trenches.provenance import (
    DEXSCREENER_PUBLIC,
    FOMO_AUTHORIZED,
    J7_AUTHORIZED,
    PUMP_ONCHAIN,
    SOLANA_RPC,
)
from smart_money_bot.trenches_runtime import SOURCE_CREATION_STREAM, TrenchesRuntime
from smart_money_bot.trenches_store import TrenchesStore

D = Decimal
NOW = 1_700_000_000
MINT = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def curve_bytes(
    *,
    real_token_remaining: int = 400_000_000_000_000,
    complete: bool = False,
    real_sol: int = 5_000_000_000,
) -> bytes:
    return (
        b"\x00" * 8
        + struct.pack(
            "<QQQQQ",
            1_073_000_000_000_000,
            30_000_000_000,
            real_token_remaining,
            real_sol,
            1_000_000_000_000_000,
        )
        + (b"\x01" if complete else b"\x00")
    )


def obs(at: int, **kwargs) -> MarketObservation:
    payload = {"at": at, "market_cap_usd": D("10000"), "price_usd": D("0.001")}
    payload.update(kwargs)
    return MarketObservation(**payload)


@pytest.fixture
async def store():
    database = Database(":memory:", D("1000"))
    await database.connect()
    try:
        yield TrenchesStore(database)
    finally:
        await database.close()


class _Chain:
    """A scripted on-chain reader.  No network, no cost."""

    def __init__(self, curves: dict[str, BondingCurveState] | None = None):
        self.curves = curves or {}
        self.calls = 0

    def usage_snapshot(self):
        return {"curve_reads": self.calls}

    async def bonding_curves(self, mints):
        self.calls += 1
        return {
            mint: self.curves.get(mint, decode_bonding_curve(mint, curve_bytes()))
            for mint in mints
        }


# ---------------------------------------------------------------------------
# sections 7, 8: lifecycle and bonding progress from real curve state
# ---------------------------------------------------------------------------
def test_bonding_progress_is_computed_from_reserves_not_guessed() -> None:
    """Section 8: progress comes from the account's own numbers."""

    empty = decode_bonding_curve(MINT, None)
    assert empty.available is False
    assert empty.progress_percent() is None, "an unreadable curve is unknown, not 0%"

    half = decode_bonding_curve(MINT, curve_bytes(real_token_remaining=396_550_000_000_000))
    assert half.available is True
    assert half.progress_percent() == D("50.00")
    assert half.sol_in_curve() == D("5.000")

    done = decode_bonding_curve(MINT, curve_bytes(real_token_remaining=0, complete=True))
    assert done.progress_percent() == D("100")
    assert done.complete is True


def test_graduation_is_never_inferred_from_age(store) -> None:
    """Section 7: a six-hour-old token can be at 4%; age proves nothing."""

    young_and_full = decode_bonding_curve(
        MINT, curve_bytes(real_token_remaining=39_655_000_000_000)
    )
    state = classify_lifecycle(young_and_full, now=NOW, created_at=NOW - 120)
    assert state.stage == STAGE_ALMOST_BONDED
    assert state.progress_percent == D("95.00")

    old_and_empty = decode_bonding_curve(
        MINT, curve_bytes(real_token_remaining=790_000_000_000_000)
    )
    aged = classify_lifecycle(old_and_empty, now=NOW, created_at=NOW - 21_600)
    assert aged.stage != STAGE_ALMOST_BONDED
    assert aged.pre_graduation is True


def test_a_new_token_is_named_new_and_an_unreadable_curve_is_unknown() -> None:
    fresh = classify_lifecycle(
        decode_bonding_curve(MINT, curve_bytes(real_token_remaining=780_000_000_000_000)),
        now=NOW,
        created_at=NOW - 30,
    )
    assert fresh.stage == STAGE_NEW

    blind = classify_lifecycle(decode_bonding_curve(MINT, None), now=NOW)
    assert blind.stage == STAGE_UNKNOWN
    assert any("could not be read" in reason for reason in blind.reasons)


def test_an_unreadable_curve_plus_pumpswap_activity_is_a_graduated_token() -> None:
    """A closed curve account is not an unknown token when it trades on PumpSwap."""

    state = classify_lifecycle(
        decode_bonding_curve(MINT, None), now=NOW, on_pumpswap=True
    )
    assert state.stage == STAGE_PUMPSWAP


def test_bonding_milestones_fire_once_on_crossing() -> None:
    """Section 43: a milestone is an event, not a level."""

    assert bonding_milestones(D("48"), D("62")) == (D("50"),)
    assert bonding_milestones(D("62"), D("62")) == ()
    assert bonding_milestones(D("10"), D("96")) == (D("25"), D("50"), D("75"), D("90"), D("95"))
    assert bonding_milestones(None, D("80")) == ()


def test_the_bonding_curve_pda_is_derived_deterministically() -> None:
    assert bonding_curve_address(MINT) == bonding_curve_address(MINT)
    other = "So11111111111111111111111111111111111111112"
    assert bonding_curve_address(MINT) != bonding_curve_address(other)


# ---------------------------------------------------------------------------
# section 85: five independent windows
# ---------------------------------------------------------------------------
def test_every_timeframe_is_computed_from_only_its_own_samples() -> None:
    """Section 85: no leakage across windows."""

    observations = [
        obs(NOW - 3000, market_cap_usd=D("10000")),
        obs(NOW - 1500, market_cap_usd=D("12000")),
        obs(NOW - 700, market_cap_usd=D("15000")),
        obs(NOW - 200, market_cap_usd=D("20000")),
        obs(NOW - 30, market_cap_usd=D("30000")),
    ]
    one_minute = window_metrics(observations, timeframe=TF_1M, now=NOW)
    five = window_metrics(observations, timeframe=TF_5M, now=NOW)
    hour = window_metrics(observations, timeframe=TF_1H, now=NOW)

    # The 1m window has one sample inside it, so it reports nothing at all
    # rather than borrowing the 5m reading.
    assert one_minute.usable is False
    assert one_minute.market_cap_change_percent is None

    assert five.usable is True
    assert five.samples == 2
    assert five.market_cap_change_percent == D("50.00")

    assert hour.samples == 5
    assert hour.market_cap_change_percent == D("200.00")
    # Each window's span is its own, never the full history.
    assert five.span_seconds < hour.span_seconds


def test_an_empty_window_reports_nothing_rather_than_zero() -> None:
    assert window_metrics([], timeframe=TF_5M, now=NOW).usable is False
    single = window_metrics([obs(NOW - 10)], timeframe=TF_1M, now=NOW)
    assert single.usable is False
    assert single.buy_sell_ratio is None


def test_all_five_timeframes_are_produced() -> None:
    observations = [obs(NOW - offset) for offset in (3500, 1700, 800, 250, 40, 10)]
    profile = build_timeframe_profile(MINT, observations, now=NOW)
    assert set(profile.windows) == set(TIMEFRAMES)


# ---------------------------------------------------------------------------
# sections 10, 11, 86: shape and the momentum curve
# ---------------------------------------------------------------------------
def test_a_hot_short_window_against_a_flat_long_one_is_very_early_acceleration() -> None:
    observations = [
        obs(NOW - 880, market_cap_usd=D("10000")),
        obs(NOW - 400, market_cap_usd=D("10050")),
        obs(NOW - 250, market_cap_usd=D("10100")),
        obs(NOW - 50, market_cap_usd=D("13000")),
        obs(NOW - 5, market_cap_usd=D("16000")),
    ]
    profile = build_timeframe_profile(MINT, observations, now=NOW)
    assert profile.shape == SHAPE_VERY_EARLY_ACCELERATION
    assert profile.actionable is True


def test_all_windows_strong_together_is_a_sustained_trend() -> None:
    observations = [
        obs(NOW - 880, market_cap_usd=D("10000")),
        obs(NOW - 500, market_cap_usd=D("16000")),
        obs(NOW - 200, market_cap_usd=D("26000")),
        obs(NOW - 40, market_cap_usd=D("40000")),
        obs(NOW - 5, market_cap_usd=D("48000")),
    ]
    profile = build_timeframe_profile(MINT, observations, now=NOW)
    assert profile.shape == SHAPE_SUSTAINED_TREND
    assert profile.momentum_curve in {MOMENTUM_INCREASING, "STEADY"}


def test_a_rolling_over_token_is_named_fading_not_green() -> None:
    """Section 11: 'currently green' is not the same as still accelerating."""

    observations = [
        obs(NOW - 1700, market_cap_usd=D("10000")),
        obs(NOW - 900, market_cap_usd=D("30000")),
        obs(NOW - 400, market_cap_usd=D("34000")),
        obs(NOW - 120, market_cap_usd=D("28000")),
        obs(NOW - 20, market_cap_usd=D("24000")),
    ]
    profile = build_timeframe_profile(MINT, observations, now=NOW)
    assert profile.shape in {SHAPE_FADING, "COOLING"}
    assert profile.actionable is False


def test_a_missing_short_window_does_not_report_a_moving_token_as_flat() -> None:
    """A gap in observations is absence of data, not evidence of calm."""

    observations = [
        obs(NOW - 900, market_cap_usd=D("10000")),
        obs(NOW - 600, market_cap_usd=D("20000")),
        obs(NOW - 300, market_cap_usd=D("40000")),
    ]
    profile = build_timeframe_profile(MINT, observations, now=NOW)
    assert profile.window(TF_1M) is None
    assert profile.shape == SHAPE_SUSTAINED_TREND


def test_acceleration_raises_the_recheck_cadence(store) -> None:
    """Section 86: accelerating candidates get looked at sooner."""

    near = cadence_tier(score=D("58"), alpha_threshold=D("62"))
    assert near == CADENCE_HOT
    bonded = cadence_tier(score=D("20"), alpha_threshold=D("62"), almost_bonded=True)
    assert bonded == CADENCE_HOT
    quiet = cadence_tier(score=D("12"), alpha_threshold=D("62"))
    assert quiet == CADENCE_NORMAL


# ---------------------------------------------------------------------------
# section 12: market cap versus liquidity
# ---------------------------------------------------------------------------
def test_the_same_market_cap_on_different_liquidity_is_not_the_same_token() -> None:
    thin = assess_depth(market_cap_usd=D("50000"), liquidity_usd=D("1000"))
    healthy = assess_depth(market_cap_usd=D("50000"), liquidity_usd=D("15000"))
    assert thin.thin is True
    assert healthy.thin is False
    assert thin.estimated_impact_percent > healthy.estimated_impact_percent


def test_extreme_turnover_is_flagged_as_churn() -> None:
    churn = assess_depth(
        market_cap_usd=D("50000"), liquidity_usd=D("5000"), volume_usd=D("120000")
    )
    assert churn.churning is True


# ---------------------------------------------------------------------------
# sections 13, 87, 88, 89: participants
# ---------------------------------------------------------------------------
def test_many_transactions_from_few_wallets_is_not_organic_demand() -> None:
    """Section 87: 1000 buys from 4 bots must not read like broad demand."""

    buyers = [
        BuyerRecord(
            wallet=f"BOT{index % 4}",
            at=NOW,
            amount_usd=D("50"),
            first_activity_at=NOW - 900_000,
        )
        for index in range(1000)
    ]
    profile = assess_participants(MINT, buyers, buys=1000, sells=40)
    assert profile.unique_buyers == 4
    assert profile.organic is False
    assert profile.buys_per_independent_buyer >= D("100")


def test_fresh_wallets_from_one_funder_collapse_to_one_actor() -> None:
    """Section 88: twenty sybils funded by one source are one unit of demand."""

    buyers = [
        BuyerRecord(
            wallet=f"F{index}",
            at=NOW,
            amount_usd=D("25"),
            first_activity_at=NOW - 600,
            funded_by="UPSTREAM",
            funded_at=NOW - 700,
        )
        for index in range(20)
    ]
    profile = assess_participants(MINT, buyers, buys=20, sells=0)
    assert profile.unique_buyers == 20
    assert profile.independent_buyers == 1
    assert profile.independent_fresh_buyers == 0
    assert profile.clustered_percent == D("100.0")
    assert profile.organic is False
    assert any("clustered" in reason for reason in profile.reasons)


def test_independently_funded_fresh_wallets_remain_independent() -> None:
    """Section 89: real new traders are real demand."""

    buyers = [
        BuyerRecord(
            wallet=f"N{index}",
            at=NOW,
            amount_usd=D("30"),
            first_activity_at=NOW - 3600,
            funded_by=f"SOURCE{index}",
            funded_at=NOW - 4000,
        )
        for index in range(20)
    ]
    profile = assess_participants(MINT, buyers, buys=20, sells=2)
    assert profile.independent_buyers == 20
    assert profile.independent_fresh_buyers == 20
    assert profile.organic is True


def test_wallet_age_is_unknown_when_history_is_unreadable() -> None:
    """A wallet we could not read is UNKNOWN, never quietly ESTABLISHED."""

    assert classify_wallet_age(first_activity_at=None, at=NOW) == "UNKNOWN"
    assert classify_wallet_age(first_activity_at=NOW - 600, at=NOW) == "VERY_NEW"
    assert classify_wallet_age(first_activity_at=NOW - 900_000, at=NOW) == "ESTABLISHED"


def test_a_large_buy_is_measured_against_the_pool_it_landed_in() -> None:
    """Section 44: size is relative, and demand needs follow-through."""

    small_pool = assess_large_buy(
        wallet="W",
        amount_usd=D("500"),
        liquidity_usd=D("5000"),
        market_cap_usd=D("40000"),
        average_trade_usd=D("30"),
    )
    assert small_pool.significant is True
    assert small_pool.confirmed_demand is False, "a big buy alone is not demand"

    confirmed = assess_large_buy(
        wallet="W",
        amount_usd=D("500"),
        liquidity_usd=D("5000"),
        market_cap_usd=D("40000"),
        average_trade_usd=D("30"),
        followed_by_independent_demand=True,
    )
    assert confirmed.confirmed_demand is True


# ---------------------------------------------------------------------------
# sections 16-19, 90: dev
# ---------------------------------------------------------------------------
def test_a_creator_reducing_their_position_raises_risk() -> None:
    """Section 90."""

    stable = assess_dev_holding(wallet="D", initial_percent=D("5"), current_percent=D("5"))
    assert stable.posture == DEV_HOLDING_STABLE
    assert stable.selling is False

    selling = assess_dev_holding(wallet="D", initial_percent=D("8"), current_percent=D("2"))
    assert selling.posture == DEV_HOLDING_SELLING
    assert selling.selling is True

    profile = build_risk_profile(MINT, dev_selling=True)
    assert profile.dimension(RISK_DEV).verdict == FAIL


def test_a_poor_creator_record_gets_a_neutral_label_not_an_accusation() -> None:
    """Section 19: bad outcomes are not a criminal finding about a person."""

    history = assess_dev_history(
        "D",
        [
            PriorToken(mint=f"M{index}", collapsed=True)
            for index in range(4)
        ],
    )
    assert history.label == DEV_HISTORY_HIGH_FAILURE
    assert history.failure_rate == D("1.00")
    assert "scam" not in history.operator_line().casefold()
    assert "fraud" not in history.operator_line().casefold()


def test_funding_just_before_launch_is_context_not_a_verdict() -> None:
    """Section 17."""

    from smart_money_bot.trenches import DevFunding

    funding = DevFunding(
        wallet="D", source_type="CEX", seconds_before_launch=180, amount_sol=D("2")
    )
    assert funding.funded_just_before_launch is True
    profile = DevProfile(mint=MINT, wallet="D", funding=funding)
    concerns = " ".join(profile.concerns).casefold()
    assert "context" in concerns
    assert "scam" not in concerns


def test_dev_history_is_unknown_without_evidence() -> None:
    assert assess_dev_history("D", []).label == "DEV_HISTORY_UNKNOWN"


# ---------------------------------------------------------------------------
# sections 20, 21, 91: holders
# ---------------------------------------------------------------------------
def test_infrastructure_accounts_are_not_counted_as_holders() -> None:
    """The bonding curve is not a whale (section 20)."""

    accounts = [
        HolderAccount(address="CURVE", amount=D("700"), infrastructure=True),
        *[HolderAccount(address=f"H{index}", amount=D("10")) for index in range(30)],
    ]
    snapshot = build_holder_snapshot(
        MINT, accounts, total_supply=D("1000"), at=NOW
    )
    # Concentration is measured against the 300 circulating, not the 1000 total.
    assert snapshot.top10_percent == D("33.33")
    assert snapshot.infrastructure_percent == D("70.00")


def test_concentration_is_reported_as_a_trend_not_a_snapshot() -> None:
    """Section 91: 43→37→31 and 18→35 mean opposite things."""

    def snapshot(at: int, top10: str):
        """Ten large holders plus a long tail, so the top-10 share can move."""

        whales = D(top10) / 10
        tail = (D("100") - D(top10)) / 40
        return build_holder_snapshot(
            MINT,
            [HolderAccount(address=f"W{index}", amount=whales) for index in range(10)]
            + [HolderAccount(address=f"T{index}", amount=tail) for index in range(40)],
            total_supply=D("100"),
            at=at,
        )

    broadening = assess_concentration_trend(
        MINT, [snapshot(NOW - 600, "43"), snapshot(NOW - 300, "37"), snapshot(NOW, "31")]
    )
    assert broadening.state == CONCENTRATION_IMPROVING
    assert broadening.change_points == D("-12.00")

    concentrating = assess_concentration_trend(
        MINT, [snapshot(NOW - 600, "18"), snapshot(NOW, "35")]
    )
    assert concentrating.state == CONCENTRATION_WORSENING
    assert concentrating.worsening is True

    risk = build_risk_profile(MINT, top10_percent=D("35"), concentration_worsening=True)
    assert risk.dimension(RISK_CONCENTRATION).verdict == FAIL


def test_a_single_snapshot_cannot_claim_a_trend() -> None:
    only_one = assess_concentration_trend(
        MINT,
        [
            build_holder_snapshot(
                MINT, [HolderAccount(address="A", amount=D("10"))], total_supply=D("100"), at=NOW
            )
        ],
    )
    assert only_one.state == "UNKNOWN"


# ---------------------------------------------------------------------------
# sections 23, 92: bundles
# ---------------------------------------------------------------------------
def test_launch_bundles_are_detected_and_ordinary_co_trading_is_not() -> None:
    """Section 23: same-slot activity on a mature pool is block production."""

    launch = [
        SlotTrade(wallet=f"B{index}", slot=100, at=NOW + 10, token_amount=D("60000000000000"))
        for index in range(5)
    ]
    bundled = assess_bundles(
        MINT, launch, created_at=NOW, total_supply=D("1000000000000000")
    )
    assert bundled.bundle_count == 1
    assert bundled.risk == BUNDLE_RISK_HIGH

    later = [
        SlotTrade(wallet=f"C{index}", slot=900, at=NOW + 7200, token_amount=D("1000"))
        for index in range(5)
    ]
    ordinary = assess_bundles(
        MINT,
        later,
        created_at=NOW,
        total_supply=D("1000000000000000"),
        pre_graduation=False,
    )
    assert ordinary.risk == BUNDLE_RISK_NONE
    assert any("ordinary co-trading" in reason for reason in ordinary.reasons)


def test_bundle_wallets_selling_raises_risk(store) -> None:
    """Section 92."""

    launch = [
        SlotTrade(wallet=f"B{index}", slot=100, at=NOW + 5, token_amount=D("30000000000000"))
        for index in range(4)
    ]
    distributing = assess_bundles(
        MINT,
        launch,
        created_at=NOW,
        total_supply=D("1000000000000000"),
        current_bundle_holdings=D("1"),
    )
    assert distributing.distributing is True
    # A 12% bundle that is selling escalates to HIGH on behaviour, not size.
    assert distributing.bundle_supply_percent == D("12.00")
    assert distributing.risk == BUNDLE_RISK_HIGH
    assert any("selling" in reason for reason in distributing.reasons)

    risk = build_risk_profile(MINT, bundle_risk="HIGH", bundle_distributing=True)
    assert risk.dimension(RISK_BUNDLE).verdict == FAIL


def test_bundles_are_unknown_without_trade_detail() -> None:
    assert assess_bundles(MINT, []).risk == "UNKNOWN"


# ---------------------------------------------------------------------------
# sections 26, 27, 95: placement and reuse
# ---------------------------------------------------------------------------
def test_paid_dex_placement_cannot_carry_the_public_model() -> None:
    """Section 95: a purchase is not a trend."""

    flat = build_timeframe_profile(
        MINT,
        [obs(NOW - 900, market_cap_usd=D("10000")), obs(NOW - 10, market_cap_usd=D("10000"))],
        now=NOW,
    )
    boosted = score_public_trend(MINT, timeframes=flat, dex_paid=True, dex_boosts=50)
    assert boosted.score <= D("5"), "boost alone must not lift a token that is not moving"

    moving = build_timeframe_profile(
        MINT,
        [
            obs(NOW - 880, market_cap_usd=D("10000")),
            obs(NOW - 400, market_cap_usd=D("18000")),
            obs(NOW - 100, market_cap_usd=D("30000")),
            obs(NOW - 20, market_cap_usd=D("42000")),
        ],
        now=NOW,
    )
    organic = score_public_trend(MINT, timeframes=moving, independent_buyers=90)
    assert organic.score > boosted.score * 5


def test_metadata_reuse_across_mints_is_evidence_not_a_verdict() -> None:
    """Section 27."""

    subject = TokenMetadata(
        mint=MINT, image_url="https://cdn.example/a.png", website="https://proj.example"
    )
    other_prints = TokenMetadata(
        mint="OTHER", image_url="https://cdn.example/a.png", website="https://proj.example"
    ).fingerprints()
    reuse = detect_reuse(subject, [("OTHER", other_prints, NOW - 100)])
    assert reuse.state == "HEAVY_REUSE"
    assert "OTHER" in reuse.other_mints
    assert "malice" in reuse.warning_line().casefold()

    unique = detect_reuse(subject, [("OTHER", {"image": "different"}, NOW)])
    assert unique.state == "NONE"


def test_a_token_never_collides_with_itself() -> None:
    subject = TokenMetadata(mint=MINT, image_url="https://cdn.example/a.png")
    same = detect_reuse(subject, [(MINT, subject.fingerprints(), NOW)])
    assert same.state == "NONE"


# ---------------------------------------------------------------------------
# sections 34, 96: consensus
# ---------------------------------------------------------------------------
def test_duplicate_market_feeds_are_one_evidence_family() -> None:
    """Section 96: two vendors relaying the same chain is one observation."""

    duplicates = build_consensus(
        [
            Nomination(mint=MINT, lane="A", source=SourceRef(kind=SOLANA_RPC)),
            Nomination(mint=MINT, lane="B", source=SourceRef(kind=DEXSCREENER_PUBLIC)),
            Nomination(mint=MINT, lane="C", source=SourceRef(kind=PUMP_ONCHAIN)),
        ]
    )[MINT]
    assert duplicates.lane_count == 3
    assert duplicates.independent_count == 1
    assert duplicates.strong is False

    genuine = build_consensus(
        [
            Nomination(mint=MINT, lane="A", source=SourceRef(kind=PUMP_ONCHAIN)),
            Nomination(mint=MINT, lane="B", source=SourceRef(kind=J7_AUTHORIZED)),
            Nomination(mint=MINT, lane="C", source=SourceRef(kind=FOMO_AUTHORIZED)),
        ]
    )[MINT]
    assert genuine.independent_count == 3
    assert genuine.strong is True


def test_independent_counting_never_double_counts() -> None:
    sources = (SourceRef(kind=SOLANA_RPC), SourceRef(kind=SOLANA_RPC))
    assert count_independent(sources) == 1


# ---------------------------------------------------------------------------
# sections 3, 97, 98: source honesty
# ---------------------------------------------------------------------------
def test_our_ranking_can_never_claim_to_be_terminals() -> None:
    """Section 97: without an authorised feed, no Terminal claim, ever."""

    assert MODEL_NAME == PUBLIC_TRENDING_MODEL
    for claim in FORBIDDEN_RANKING_CLAIMS:
        with pytest.raises(ValueError):
            assert_honest_ranking_name(claim)
    # The honest names pass.
    assert_honest_ranking_name(PUBLIC_TRENDING_MODEL)
    assert_honest_ranking_name("TERMINAL_STYLE_PUBLIC_MODEL")


def test_every_public_model_score_carries_its_caveat() -> None:
    profile = build_timeframe_profile(
        MINT, [obs(NOW - 300), obs(NOW - 10, market_cap_usd=D("20000"))], now=NOW
    )
    score = score_public_trend(MINT, timeframes=profile)
    payload = score.to_json()
    assert payload["model"] == PUBLIC_TRENDING_MODEL
    assert "not Terminal" in payload["caveat"]
    assert "not Fomo" in payload["caveat"]


def test_the_public_board_ranks_by_our_score_alone() -> None:
    def scored(mint: str, caps: list[str]):
        return score_public_trend(
            mint,
            timeframes=build_timeframe_profile(
                mint,
                [
                    obs(NOW - 880, market_cap_usd=D(caps[0])),
                    obs(NOW - 300, market_cap_usd=D(caps[1])),
                    obs(NOW - 20, market_cap_usd=D(caps[2])),
                ],
                now=NOW,
            ),
        )

    board = rank_public_trend(
        [scored("SLOW", ["10000", "10100", "10200"]), scored("FAST", ["10000", "25000", "60000"])],
        min_score=D("0"),
    )
    assert board[0].mint == "FAST"
    assert board[0].rank == 1


# ---------------------------------------------------------------------------
# sections 59-61: explicit risk
# ---------------------------------------------------------------------------
def test_risk_is_reported_per_dimension_not_as_one_number() -> None:
    """Section 59: one blended score hides which thing is wrong."""

    profile = build_risk_profile(MINT, liquidity_usd=D("20000"), top10_percent=D("25"))
    names = {item.name for item in profile.dimensions}
    assert RISK_LIQUIDITY in names
    assert RISK_DEV in names
    assert RISK_BUNDLE in names
    assert len(profile.dimensions) >= 10


def test_a_missing_provider_produces_unknown_not_pass_and_not_fail() -> None:
    """Section 61."""

    blind = build_risk_profile(MINT)
    assert blind.dimension(RISK_LIQUIDITY).verdict == UNKNOWN
    assert blind.dimension(RISK_DEV).verdict == UNKNOWN
    assert PASS not in {item.verdict for item in blind.dimensions}
    assert blind.blocked is False, "not knowing is not a hard failure"
    assert any("UNKNOWN" in note for note in blind.notes)


def test_hard_failures_block_regardless_of_everything_else() -> None:
    """Section 60."""

    for kwargs in (
        {"sell_failed": True},
        {"liquidity_collapsed": True},
        {"malicious_evidence": True},
        {"route_available": False},
    ):
        profile = build_risk_profile(MINT, liquidity_usd=D("500000"), **kwargs)
        assert profile.blocked is True, kwargs


# ---------------------------------------------------------------------------
# sections 29, 37, 93: the trench score and tiering
# ---------------------------------------------------------------------------
def _healthy_candidate():
    curve = decode_bonding_curve(MINT, curve_bytes(real_token_remaining=79_310_000_000_000))
    lifecycle = classify_lifecycle(curve, now=NOW, created_at=NOW - 200)
    buyers = [
        BuyerRecord(
            wallet=f"W{index}",
            at=NOW,
            amount_usd=D("40"),
            first_activity_at=NOW - 900_000,
            funded_by=f"S{index}",
        )
        for index in range(140)
    ]
    participants = assess_participants(MINT, buyers, buys=212, sells=78)
    timeframes = build_timeframe_profile(
        MINT,
        [
            obs(NOW - 880, market_cap_usd=D("14000"), independent_buyers=10),
            obs(NOW - 300, market_cap_usd=D("18000"), independent_buyers=60),
            obs(NOW - 40, market_cap_usd=D("22000"), independent_buyers=136),
        ],
        now=NOW,
    )
    holders = build_holder_snapshot(
        MINT,
        [HolderAccount(address=f"H{index}", amount=D("10")) for index in range(50)],
        total_supply=D("500"),
        at=NOW,
    )
    return lifecycle, participants, timeframes, holders


def test_a_healthy_almost_bonded_token_can_surface_before_graduation() -> None:
    """Section 93."""

    lifecycle, participants, timeframes, holders = _healthy_candidate()
    assert lifecycle.stage in {STAGE_ALMOST_BONDED, STAGE_GRADUATING}

    score = score_pump_trench(
        MINT,
        lifecycle=lifecycle,
        participants=participants,
        timeframes=timeframes,
        depth=assess_depth(market_cap_usd=D("22000"), liquidity_usd=D("18000")),
        holders=holders,
    )
    assert score.score >= D("50")
    assert score.has_named_reason is True

    decision = decide_trench_tier(
        MINT,
        score=score.score,
        reasons=score.reasons,
        almost_bonded=True,
        runner_threshold=D("50"),
    )
    assert decision.ping is True
    assert decision.tier in {TRENCH_RUNNER, "HIGH_CONFLUENCE"}


def test_a_score_without_a_named_reason_never_pings() -> None:
    """A number is not a reason to interrupt anyone."""

    decision = decide_trench_tier(MINT, score=D("95"), reasons=(), runner_threshold=D("62"))
    assert decision.ping is False
    assert decision.suppression == SUPPRESS_NO_NAMED_REASON


def test_clustered_demand_blocks_a_ping_however_good_the_score_looks() -> None:
    """Section 87 at the alert boundary."""

    decision = decide_trench_tier(
        MINT,
        score=D("90"),
        reasons=("INDEPENDENT_DEMAND",),
        runner_threshold=D("62"),
        clustered_demand=True,
    )
    assert decision.ping is False
    assert decision.suppression == SUPPRESS_CLUSTERED_DEMAND


def test_a_selling_creator_blocks_a_ping() -> None:
    decision = decide_trench_tier(
        MINT,
        score=D("90"),
        reasons=("MARKET_ACCELERATION",),
        runner_threshold=D("62"),
        dev_selling=True,
    )
    assert decision.ping is False
    assert decision.suppression == SUPPRESS_DEV_RISK


def test_a_special_mode_token_is_capped_rather_than_scored_normally() -> None:
    """Section 28: a documented special state changes how the token behaves."""

    curve = BondingCurveState(
        mint=MINT,
        available=True,
        real_token_reserves=D("79310000000000"),
        virtual_sol_reserves=D("30000000000"),
        virtual_token_reserves=D("1073000000000000"),
        token_total_supply=D("1000000000000000"),
        is_mayhem_mode=True,
    )
    lifecycle = classify_lifecycle(curve, now=NOW, created_at=NOW - 200)
    assert lifecycle.special_mode == "MAYHEM"
    _, participants, timeframes, holders = _healthy_candidate()
    score = score_pump_trench(
        MINT,
        lifecycle=lifecycle,
        participants=participants,
        timeframes=timeframes,
        depth=assess_depth(market_cap_usd=D("22000"), liquidity_usd=D("18000")),
        holders=holders,
    )
    assert score.score <= D("40")


def test_a_hard_failure_zeroes_the_trench_score_and_its_reasons() -> None:
    lifecycle, participants, timeframes, holders = _healthy_candidate()
    blocked = build_risk_profile(MINT, sell_failed=True)
    score = score_pump_trench(
        MINT,
        lifecycle=lifecycle,
        participants=participants,
        timeframes=timeframes,
        holders=holders,
        risk=blocked,
    )
    assert score.score == D("0.0")
    assert score.reasons == ()
    assert score.actionable is False


# ---------------------------------------------------------------------------
# runtime: persistence, graduation, no lookahead, real money
# ---------------------------------------------------------------------------
async def test_a_new_mint_is_stamped_before_any_enrichment(store) -> None:
    """Section 74: the first-observation stamp is what latency is measured against."""

    runtime = TrenchesRuntime(store, _Chain())
    assert await runtime.observe_creation(
        MINT, at=NOW, created_at=NOW - 3, source=SOURCE_CREATION_STREAM
    )
    row = await store.token(MINT)
    assert row["first_observed_at"] == NOW
    assert row["first_observed_source"] == SOURCE_CREATION_STREAM
    latency = await store.discovery_latencies()
    assert latency[0]["latency_seconds"] == 3

    # A second sighting does not re-stamp it.
    assert await runtime.observe_creation(MINT, at=NOW + 500) is False
    assert (await store.token(MINT))["first_observed_at"] == NOW


async def test_graduation_preserves_the_tokens_earlier_history(store) -> None:
    """Section 94: the same mint, not a new unrelated token."""

    runtime = TrenchesRuntime(store, _Chain())
    await runtime.observe_creation(MINT, at=NOW, created_at=NOW)
    await store.record_token(
        MINT, now=NOW + 10, stage=STAGE_MID_CURVE, bonding_percent=D("40"),
        market_cap_usd=D("18000"),
    )
    await store.mark_graduated(MINT, at=NOW + 600, market_cap_usd=D("69000"))
    await store.record_token(
        MINT, now=NOW + 700, stage=STAGE_PUMPSWAP, market_cap_usd=D("80000")
    )

    row = await store.token(MINT)
    assert row["first_observed_at"] == NOW, "history survives the transition"
    assert row["graduated_at"] == NOW + 600
    assert row["graduation_market_cap_usd"] == 69000.0
    assert row["stage"] == STAGE_PUMPSWAP

    # A later pass never rewrites the graduation moment.
    await store.mark_graduated(MINT, at=NOW + 9000, market_cap_usd=D("1"))
    assert (await store.token(MINT))["graduated_at"] == NOW + 600


async def test_a_later_pump_cannot_change_an_earlier_decision(store) -> None:
    """Section 99: every verdict is causal."""

    caps = iter([D("10000"), D("10100"), D("900000")])

    async def enrich(mint):
        return {"market_cap_usd": next(caps), "liquidity_usd": D("9000")}

    runtime = TrenchesRuntime(store, _Chain(), enrich=enrich, max_enrichment_per_scan=5)
    await runtime.observe_creation(MINT, at=NOW, created_at=NOW)
    first = await runtime.scan_once(now=NOW + 30)
    second = await runtime.scan_once(now=NOW + 200)
    await runtime.scan_once(now=NOW + 400)

    assert first.candidates[0].decision.ping is False
    assert second.candidates[0].decision.ping is False
    history = await store.observations(MINT)
    assert history[0].market_cap_usd == D("10000"), "the earliest reading is immutable"


async def test_the_lane_never_reports_live_execution(store) -> None:
    """Section 100."""

    runtime = TrenchesRuntime(store, _Chain())
    status = await runtime.status(now=NOW)
    assert status["live_execution"] is False

    import pathlib

    import smart_money_bot.trenches as package

    for path in pathlib.Path(package.__file__).parent.glob("*.py"):
        text = path.read_text()
        for forbidden in ("Keypair", "send_transaction", "sign_transaction", "aiohttp"):
            assert forbidden not in text, f"{path.name} must stay provider- and signer-free"


async def test_cadence_tiers_stay_bounded_in_population(store) -> None:
    """Section 42: the fast tier is capped in size, not just in interval."""

    from smart_money_bot.trenches import CadenceConfig

    runtime = TrenchesRuntime(
        store, _Chain(), cadence_config=CadenceConfig(max_hot=2, max_warm=3)
    )
    for index in range(8):
        await runtime.observe_creation(f"MINT{index}", at=NOW, created_at=NOW)
    for tracked in runtime._tracked.values():
        tracked.cadence = CADENCE_HOT
    runtime._enforce_cadence_caps()
    hot = sum(1 for item in runtime._tracked.values() if item.cadence == CADENCE_HOT)
    assert hot <= 2


async def test_the_public_board_is_persisted_with_our_model_name(store) -> None:
    async def enrich(mint):
        return {"market_cap_usd": D("50000"), "liquidity_usd": D("30000")}

    runtime = TrenchesRuntime(store, _Chain(), enrich=enrich)
    await runtime.observe_creation(MINT, at=NOW, created_at=NOW)
    await runtime.scan_once(now=NOW + 30)
    await runtime.scan_once(now=NOW + 200)
    board = await runtime.public_board()
    if board:
        assert board[0]["model"] == PUBLIC_TRENDING_MODEL


async def test_discovery_latency_is_attributable_per_source(store) -> None:
    """Section 73: a slow stream and a slow poll need different fixes."""

    runtime = TrenchesRuntime(store, _Chain())
    await runtime.observe_creation(
        "A" * 43, at=NOW, created_at=NOW - 2, source="PUMP_CREATION_STREAM"
    )
    await runtime.observe_creation("B" * 43, at=NOW, created_at=NOW - 45, source="PUMP_POLL")
    summary = await store.discovery_latency_by_source()
    assert summary["PUMP_CREATION_STREAM"]["p50"] == 2
    assert summary["PUMP_POLL"]["p50"] == 45


# ---------------------------------------------------------------------------
# sections 22, 24, 4: related exposure, bot activity, lane health
# ---------------------------------------------------------------------------
def test_related_wallet_exposure_totals_only_graph_linked_wallets() -> None:
    """Section 22: 'related' is a transaction-graph claim, never an identity one."""

    from smart_money_bot.trenches import assess_related_exposure

    exposure = assess_related_exposure(
        MINT,
        related_wallets=["A", "B", "C"],
        holdings={"A": D("10"), "B": D("15"), "C": D("5"), "UNRELATED": D("70")},
        circulating_supply=D("100"),
        evidence=("SHARED_FUNDER (3 wallets)",),
    )
    assert exposure.related_wallets == 3
    assert exposure.related_percent == D("30.00")
    assert exposure.significant is True
    assert "SHARED_FUNDER" in exposure.evidence[0]

    unknown = assess_related_exposure(
        MINT, related_wallets=[], holdings={}, circulating_supply=D("100")
    )
    assert unknown.related_percent is None, "no relationship is unknown, not zero"


def test_bot_activity_is_context_not_smart_money() -> None:
    """Section 24: a trading app was used. That is attention, not skill."""

    from smart_money_bot.trenches import assess_bot_activity

    activity = assess_bot_activity(
        MINT,
        program_ids_per_trade=[
            ["JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"],
            ["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"],
            ["SomeUnknownProgram111111111111111111111111"],
            [],
        ],
    )
    assert activity.total_trades == 4
    assert activity.router_trades == 2
    assert activity.router_share == D("0.50")
    assert "not smart money" in activity.operator_line()

    assert assess_bot_activity(MINT, program_ids_per_trade=[]).router_share is None


async def test_lane_health_reports_whether_we_are_vendor_independent(store) -> None:
    """Section 4: the bot must keep working when every vendor is down."""

    from smart_money_bot.trenches import LaneHealth, UniverseHealth
    from smart_money_bot.trenches.consensus import LANE_DEX_ACTIVE, LANE_PUMP_NEW

    vendor_only = UniverseHealth(
        lanes=(
            LaneHealth(lane=LANE_DEX_ACTIVE, enabled=True, configured=True, nominations=5),
            LaneHealth(lane=LANE_PUMP_NEW, enabled=True, configured=True, nominations=0),
        )
    )
    assert vendor_only.self_sufficient is False, (
        "a live vendor lane with a dead on-chain lane is not self-sufficient"
    )

    onchain = UniverseHealth(
        lanes=(
            LaneHealth(lane=LANE_DEX_ACTIVE, enabled=True, configured=True, nominations=0),
            LaneHealth(lane=LANE_PUMP_NEW, enabled=True, configured=True, nominations=9),
        )
    )
    assert onchain.self_sufficient is True

    unconfigured = LaneHealth(lane=LANE_DEX_ACTIVE)
    assert unconfigured.state == "NO_SOURCE_CONFIGURED"
    assert (
        LaneHealth(lane=LANE_PUMP_NEW, configured=True, enabled=False).state
        == "DISABLED_BY_CONFIG"
    )
    assert (
        LaneHealth(lane=LANE_PUMP_NEW, configured=True, enabled=True, nominations=0).state
        == "ACTIVE_NO_CANDIDATES"
    )


async def test_status_reports_universe_health(store) -> None:
    runtime = TrenchesRuntime(store, _Chain())
    status = await runtime.status(now=NOW)
    assert "universe_health" in status
    assert status["universe_health"]["lanes"]
