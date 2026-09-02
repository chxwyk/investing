"""Real evidence behind the hard gates: routes, reserves and who is trading.

The operator's standing complaint, in their words: weak, fake, wash-traded,
untradable, fake-liquidity coins. Four releases answered it with better scoring
and it stayed true, because scoring cannot answer the questions that matter:

    is this the exact token?          -> canonical identity
    can a normal person get out?      -> a reverse quote, a simulation, and
                                         somebody unrelated who already did
    is the liquidity real?            -> reserves and a base price, not a card
    is the volume real?               -> economic actors, not wallets

This suite covers specification tests 21-44. Every fixture is deterministic and
nothing here touches a network, a database or a wallet.
"""

from __future__ import annotations

import inspect
import random
from decimal import Decimal

from smart_money_bot.lab.hardgates import (
    ENTRY_CANDIDATE,
    FAIL,
    GATES,
    PASS,
    REQUIRED_FOR_ENTRY,
    UNKNOWN,
    GateResult,
    build_report,
)
from smart_money_bot.lab.liquidityproof import (
    WSOL_MINT,
    LiquidityConfig,
    PoolAccount,
    ReserveObservation,
    price_impact,
    prove_liquidity,
    verify_pool,
)
from smart_money_bot.lab.organicflow import (
    BUY,
    SELL,
    FlowConfig,
    Trade,
    analyse_flow,
    prove_concentration,
    prove_organic_flow,
)
from smart_money_bot.lab.routeproof import BUY as Q_BUY
from smart_money_bot.lab.routeproof import SELL as Q_SELL
from smart_money_bot.lab.routeproof import (
    Quote,
    RouteConfig,
    SellEvent,
    SellSimulation,
    TokenHazards,
    prove_buy_route,
    prove_contract_safety,
    prove_sell_evidence,
    prove_sell_route,
)

NOW = 1_700_000_000
MINT = "GPR7Ax4kQ2mVn8hLdT6yWc3JbRfE9uZsXqM1oP5tH4dK"
POOL = "PooLAddress1111111111111111111111111111111"
RAYDIUM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
SOL_PRICE = Decimal("200")

LIQ = LiquidityConfig(allowed_programs=frozenset({RAYDIUM}))
ROUTE = RouteConfig()
FLOW = FlowConfig()


def _pool(**overrides) -> PoolAccount:
    values = dict(
        address=POOL,
        program_id=RAYDIUM,
        mint_a=MINT,
        mint_b=WSOL_MINT,
        vault_a="VaultA",
        vault_b="VaultB",
        vault_a_owner=POOL,
        vault_b_owner=POOL,
        reserve_a=Decimal("50000000"),
        reserve_b=Decimal("150"),
        slot=250_000_000,
        observed_at=NOW,
    )
    values.update(overrides)
    return PoolAccount(**values)


def _quote(direction: str, **overrides) -> Quote:
    values = dict(
        direction=direction,
        input_mint=MINT if direction == Q_SELL else WSOL_MINT,
        output_mint=MINT if direction == Q_BUY else WSOL_MINT,
        amount_in=Decimal("50"),
        amount_out=Decimal("980"),
        price_impact=Decimal("0.021"),
        route_pools=(POOL,),
        provider="jupiter",
        observed_at=NOW,
    )
    values.update(overrides)
    return Quote(**values)


def _stable_history() -> list[ReserveObservation]:
    return [
        ReserveObservation(at=NOW - offset, base_reserve=Decimal("150"))
        for offset in (240, 180, 120, 60, 5)
    ]


# ===========================================================================
# 22, 23, 24, 25 — liquidity that is actually there
# ===========================================================================


def test_22_provider_claims_liquidity_that_the_vaults_do_not_hold() -> None:
    gate, proof = prove_liquidity(
        MINT, _pool(reserve_b=Decimal("3")), base_price_usd=SOL_PRICE,
        provider_liquidity_usd=Decimal("61800"), config=LIQ, now=NOW,
    )
    assert gate.answer == FAIL
    assert proof.computed_liquidity_usd == Decimal("1200.00")
    assert "dust" in gate.reason.lower() or "floor" in gate.reason


def test_23_provider_materially_exceeding_on_chain_is_a_data_conflict() -> None:
    gate, proof = prove_liquidity(
        MINT, _pool(), base_price_usd=SOL_PRICE,
        provider_liquidity_usd=Decimal("240000"), config=LIQ, now=NOW,
    )
    assert gate.answer == FAIL
    assert "DATA CONFLICT" in gate.reason
    assert proof.provider_overstatement == Decimal("4.00")


def test_23b_a_provider_within_tolerance_does_not_raise_the_computed_figure() -> None:
    gate, proof = prove_liquidity(
        MINT, _pool(), base_price_usd=SOL_PRICE,
        provider_liquidity_usd=Decimal("61800"), config=LIQ, now=NOW,
    )
    assert gate.answer == PASS
    # The computed figure stands. The provider's number is only ever used to
    # catch a lie, never to lift the answer.
    assert proof.computed_liquidity_usd == Decimal("60000.00")


def test_24_a_fake_base_mint_is_rejected_however_it_is_labelled() -> None:
    fake_wsol = "So11111111111111111111111111111111111111119"
    gate = verify_pool(MINT, _pool(mint_b=fake_wsol), config=LIQ, now=NOW)
    assert gate.answer == FAIL
    assert "not WSOL, USDC or USDT" in gate.reason


def test_24b_an_unallowlisted_amm_program_is_rejected() -> None:
    assert verify_pool(MINT, _pool(program_id="NotAnAmm"), config=LIQ, now=NOW).answer == FAIL


def test_24c_vaults_must_belong_to_the_pool_that_claims_them() -> None:
    gate = verify_pool(MINT, _pool(vault_b_owner="SomeoneElse"), config=LIQ, now=NOW)
    assert gate.answer == FAIL
    assert "not this pool" in gate.reason


def test_25_flash_liquidity_added_then_removed_never_passes() -> None:
    history = [
        ReserveObservation(at=NOW - 110, base_reserve=Decimal("150")),
        ReserveObservation(at=NOW - 60, base_reserve=Decimal("150")),
        ReserveObservation(at=NOW - 5, base_reserve=Decimal("40")),
    ]
    gate, _ = prove_liquidity(
        MINT, _pool(reserve_b=Decimal("40")), base_price_usd=SOL_PRICE,
        history=history, config=LIQ, now=NOW,
    )
    assert gate.answer == FAIL
    assert "removed" in gate.reason


def test_25b_a_brand_new_pool_is_a_watch_rather_than_proven_depth() -> None:
    gate, _ = prove_liquidity(
        MINT, _pool(), base_price_usd=SOL_PRICE,
        history=[ReserveObservation(at=NOW - 5, base_reserve=Decimal("150"))],
        config=LIQ, now=NOW,
    )
    assert gate.answer == UNKNOWN
    assert "watch" in gate.reason


def test_provider_liquidity_alone_can_never_produce_a_pass() -> None:
    # There is no code path from a provider number to VERIFIED_LIQUIDITY=PASS:
    # without readable reserves the answer is UNKNOWN no matter what is claimed.
    gate, _ = prove_liquidity(
        MINT, _pool(reserve_a=None, reserve_b=None), base_price_usd=SOL_PRICE,
        provider_liquidity_usd=Decimal("999999"), config=LIQ, now=NOW,
    )
    assert gate.answer == UNKNOWN


def test_depth_is_not_the_same_as_size() -> None:
    # Constant-product arithmetic: the same headline liquidity can cost very
    # different amounts to exit depending on how lopsided the reserves are.
    shallow = price_impact(Decimal("10"), Decimal("50000000"), Decimal("1"))
    deep = price_impact(Decimal("1000"), Decimal("50000000"), Decimal("1"))
    assert shallow is not None and deep is not None
    assert shallow > deep
    gate, _ = prove_liquidity(
        MINT, _pool(reserve_b=Decimal("30"), reserve_a=Decimal("50000000")),
        base_price_usd=SOL_PRICE,
        config=LiquidityConfig(
            allowed_programs=frozenset({RAYDIUM}),
            probe_sizes_usd=(Decimal("500"),),
            max_impact_rate=Decimal("0.05"),
        ),
        now=NOW,
    )
    assert gate.answer == FAIL
    assert "depth is not size" in gate.reason


def test_liquidity_removable_by_one_party_is_a_standing_risk() -> None:
    gate, _ = prove_liquidity(
        MINT, _pool(withdrawable_rate=Decimal("0.9")), base_price_usd=SOL_PRICE,
        config=LIQ, now=NOW,
    )
    assert gate.answer == FAIL


def test_stale_reserves_cannot_back_a_decision() -> None:
    gate, _ = prove_liquidity(
        MINT, _pool(observed_at=NOW - 600), base_price_usd=SOL_PRICE, config=LIQ, now=NOW
    )
    assert gate.answer == UNKNOWN


def test_the_decision_stores_the_exact_arithmetic_it_used() -> None:
    gate, proof = prove_liquidity(
        MINT, _pool(), base_price_usd=SOL_PRICE, provider_liquidity_usd=Decimal("59000"),
        config=LIQ, now=NOW,
    )
    payload = proof.to_json()
    for field in ("base_reserve", "base_price_usd", "computed_liquidity_usd", "slot"):
        assert payload[field] is not None, field
    assert dict(gate.evidence)["slot"] == "250000000"


# ===========================================================================
# 26, 27, 28, 29 — sellability
# ===========================================================================


def test_26_a_buy_route_without_a_sell_route_fails_closed() -> None:
    """The honeypot's shape: the way in always works."""

    assert prove_buy_route(MINT, _quote(Q_BUY), verified_pools={POOL}, now=NOW).answer == PASS
    for label, sell in (
        ("no quote at all", None),
        ("router refused", _quote(Q_SELL, error="no route found")),
        ("stale", _quote(Q_SELL, observed_at=NOW - 600)),
        ("wrong mint", _quote(Q_SELL, input_mint="SomeOtherMint")),
        ("unverified pool", _quote(Q_SELL, route_pools=("UnknownPool",))),
        ("returns nothing", _quote(Q_SELL, amount_out=Decimal("0"))),
    ):
        gate = prove_sell_route(MINT, sell, verified_pools={POOL}, now=NOW)
        assert gate.answer in (FAIL, UNKNOWN), label
        assert gate.answer != PASS, label


def test_26b_a_sell_route_nobody_would_take_is_not_a_route() -> None:
    gate = prove_sell_route(
        MINT, _quote(Q_SELL, price_impact=Decimal("0.42")), verified_pools={POOL}, now=NOW
    )
    assert gate.answer == FAIL
    assert "not an exit" in gate.reason


def test_27_a_sell_quote_that_reverts_on_simulation_blocks() -> None:
    sells = [SellEvent(wallet=f"w{i}", at=NOW - 60, succeeded=True) for i in range(5)]
    gate = prove_sell_evidence(
        MINT, sells,
        simulation=SellSimulation(attempted=True, succeeded=False, error="0x1"),
        now=NOW,
    )
    assert gate.answer == FAIL
    assert "reverts when simulated" in gate.reason


def test_27b_an_unsimulatable_chain_is_unknown_rather_than_fine() -> None:
    sells = [SellEvent(wallet=f"w{i}", at=NOW - 60, succeeded=True) for i in range(5)]
    gate = prove_sell_evidence(
        MINT, sells, simulation=SellSimulation(unsupported=True), now=NOW
    )
    assert gate.answer == UNKNOWN


def test_28_displayed_sells_without_independent_sellers_prove_nothing() -> None:
    """NORMIE: the card said 819 sells and no independent seller existed."""

    gate = prove_sell_evidence(MINT, [], displayed_sell_count=819, now=NOW)
    assert gate.answer != PASS
    assert "819" in gate.reason
    assert "rather than evidence" in gate.reason


def test_28b_creator_sells_do_not_prove_anyone_else_can_exit() -> None:
    creator_only = [
        SellEvent(wallet=f"dev{i}", at=NOW - 60, succeeded=True, related_to_creator=True)
        for i in range(9)
    ]
    assert prove_sell_evidence(MINT, creator_only, now=NOW).answer != PASS


def test_28c_twenty_wallets_on_one_funder_are_one_seller() -> None:
    clustered = [
        SellEvent(wallet=f"w{i}", at=NOW - 60, succeeded=True, cluster_id="FUNDER-A")
        for i in range(20)
    ]
    gate = prove_sell_evidence(MINT, clustered, now=NOW)
    assert gate.answer == FAIL
    assert dict(gate.evidence)["independent_actors"] == "1"


def test_28d_failed_and_ancient_sells_are_not_evidence_about_now() -> None:
    stale = [SellEvent(wallet=f"w{i}", at=NOW - 99_999, succeeded=True) for i in range(9)]
    failed = [SellEvent(wallet=f"w{i}", at=NOW - 60, succeeded=False) for i in range(9)]
    assert prove_sell_evidence(MINT, stale, now=NOW).answer != PASS
    assert prove_sell_evidence(MINT, failed, now=NOW).answer != PASS


def test_29_risky_or_unreadable_contract_features_block() -> None:
    for label, hazards in (
        ("freeze authority", TokenHazards(freeze_authority_present=True)),
        ("blacklist", TokenHazards(freeze_authority_present=False, blacklist_present=True)),
        ("transfer hook", TokenHazards(freeze_authority_present=False, transfer_hook_present=True)),
        ("mint authority", TokenHazards(freeze_authority_present=False, transfer_hook_present=False,
                                        blacklist_present=False, mint_authority_present=True)),
        ("excessive fee", TokenHazards(freeze_authority_present=False, transfer_hook_present=False,
                                       blacklist_present=False, mint_authority_present=False,
                                       transfer_fee_rate=Decimal("0.2"))),
        ("nothing read", None),
        ("partially read", TokenHazards(source="mint")),
    ):
        gate = prove_contract_safety(MINT, hazards, now=NOW)
        assert gate.answer != PASS, label


def test_29b_asymmetric_buy_and_sell_taxes_are_the_honeypot_shape() -> None:
    gate = prove_contract_safety(
        MINT,
        TokenHazards(
            mint_authority_present=False, freeze_authority_present=False,
            transfer_hook_present=False, blacklist_present=False,
            # Both individually under the 5% absolute limit, so this proves the
            # asymmetry check bites on its own rather than riding the fee cap.
            buy_tax_rate=Decimal("0.005"), sell_tax_rate=Decimal("0.045"),
        ),
        now=NOW,
    )
    assert gate.answer == FAIL
    assert "asymmetric" in gate.reason


# ===========================================================================
# 30, 31 — wash trading and economic actors
# ===========================================================================


def test_30_wash_volume_from_a_funder_cluster_fails() -> None:
    trades = []
    for index in range(20):
        trades.append(Trade(wallet=f"w{index}", direction=BUY, amount_usd=Decimal("100"),
                            at=NOW - 30, cluster_id="FUNDER-A"))
        trades.append(Trade(wallet=f"w{index}", direction=SELL, amount_usd=Decimal("100"),
                            at=NOW - 20, cluster_id="FUNDER-A"))
    report = analyse_flow(MINT, trades)
    gate = prove_organic_flow(report, now=NOW)

    assert gate.answer == FAIL
    # Forty trades that collapse into one actor is a finding, not missing data.
    assert report.unique_wallets == 20
    assert report.unique_actors == 1
    assert report.wallets_per_actor == Decimal("20.00")


def test_31_related_wallets_count_as_one_economic_actor() -> None:
    split = [
        Trade(wallet=f"w{i}", direction=BUY, amount_usd=Decimal("500"), at=NOW - 30,
              cluster_id="ONE-BUYER")
        for i in range(15)
    ]
    report = analyse_flow(MINT, split)
    assert report.unique_wallets == 15
    assert report.unique_actors == 1
    assert prove_organic_flow(report, now=NOW).answer == FAIL


def test_30b_uniform_trade_sizes_read_as_a_script() -> None:
    trades = [
        Trade(wallet=f"w{i}", direction=BUY, amount_usd=Decimal("250"), at=NOW - 30)
        for i in range(20)
    ]
    assert "UNIFORM_TRADE_SIZES" in analyse_flow(MINT, trades).findings


def test_30c_insider_distribution_is_reported_as_distribution() -> None:
    trades = [Trade(wallet=f"b{i}", direction=BUY, amount_usd=Decimal(100 + i), at=NOW - 40)
              for i in range(12)]
    trades += [Trade(wallet=f"dev{i}", direction=SELL, amount_usd=Decimal(900 + i),
                     at=NOW - 20, is_creator=True) for i in range(6)]
    assert "DISTRIBUTION" in analyse_flow(MINT, trades).findings


def test_30d_medians_are_used_so_one_huge_print_cannot_set_the_picture() -> None:
    trades = [Trade(wallet=f"w{i}", direction=BUY, amount_usd=Decimal("50"), at=NOW - 30)
              for i in range(19)]
    trades.append(Trade(wallet="whale", direction=BUY, amount_usd=Decimal("500000"), at=NOW - 5))
    report = analyse_flow(MINT, trades)
    assert report.median_trade_usd == Decimal("50")
    assert report.trimmed_mean_trade_usd is not None
    assert report.trimmed_mean_trade_usd < Decimal("1000")


def test_thin_activity_is_unknown_rather_than_condemned() -> None:
    gate = prove_organic_flow(
        analyse_flow(MINT, [Trade(wallet="a", direction=BUY, amount_usd=Decimal("50"), at=NOW)]),
        now=NOW,
    )
    assert gate.answer == UNKNOWN


def test_concentration_refuses_a_market_a_few_wallets_can_end() -> None:
    assert prove_concentration(MINT, top10_rate=Decimal("0.82")).answer == FAIL
    assert prove_concentration(MINT, top10_rate=None).answer == UNKNOWN
    assert prove_concentration(MINT, top10_rate=Decimal("0.21")).answer == PASS


# ===========================================================================
# 32, 33, 34, 35 — the whole decision
# ===========================================================================


def _healthy_gates() -> list[GateResult]:
    """A complete, legitimate token, assembled from the real producers."""

    random.seed(11)
    gates: list[GateResult] = []

    gates.append(verify_pool(MINT, _pool(), config=LIQ, now=NOW))
    liquidity, _ = prove_liquidity(
        MINT, _pool(), base_price_usd=SOL_PRICE, provider_liquidity_usd=Decimal("58000"),
        history=_stable_history(), config=LIQ, now=NOW,
    )
    gates.append(liquidity)
    gates.append(prove_buy_route(MINT, _quote(Q_BUY), verified_pools={POOL}, now=NOW))
    gates.append(prove_sell_route(MINT, _quote(Q_SELL), verified_pools={POOL}, now=NOW))
    gates.append(
        prove_sell_evidence(
            MINT,
            [SellEvent(wallet=f"seller{i}", at=NOW - 120, amount_usd=Decimal("140"),
                       succeeded=True) for i in range(6)],
            simulation=SellSimulation(attempted=True, succeeded=True, observed_at=NOW),
            now=NOW,
        )
    )
    organic = [
        Trade(wallet=f"buyer{i}", direction=BUY,
              amount_usd=Decimal(str(round(random.uniform(23, 870), 2))), at=NOW - 60)
        for i in range(40)
    ] + [
        Trade(wallet=f"seller{i}", direction=SELL,
              amount_usd=Decimal(str(round(random.uniform(30, 500), 2))), at=NOW - 40)
        for i in range(14)
    ]
    gates.append(prove_organic_flow(analyse_flow(MINT, organic), now=NOW))
    gates.append(prove_concentration(MINT, top10_rate=Decimal("0.18"), observed_at=NOW))
    gates.append(
        prove_contract_safety(
            MINT,
            TokenHazards(
                mint_authority_present=False, freeze_authority_present=False,
                transfer_hook_present=False, blacklist_present=False,
                transfer_fee_rate=Decimal("0"), observed_at=NOW, source="mint account",
            ),
            now=NOW,
        )
    )
    for gate in (CANONICAL_GATES := ("CANONICAL_TOKEN", "VERIFIED_ORIGIN", "MOMENTUM_OK",
                                     "NOT_LATE_OR_EXHAUSTED", "FINAL_ALERT_GUARD_OK")):
        gates.append(
            GateResult(gate=gate, answer=PASS, source="fixture", observed_at=NOW,
                       reason="fixture")
        )
    assert len(CANONICAL_GATES) == 5
    return gates


def test_32_a_genuinely_healthy_token_can_pass_every_hard_gate() -> None:
    """The positive fixture the specification requires.

    Assembled from the real evidence producers rather than hand-written PASSes,
    so it proves the producers can agree — a suite that only ever refuses would
    be indistinguishable from one that refuses everything.
    """

    report = build_report(MINT, _healthy_gates(), now=NOW)
    assert report.blocking() == (), f"blocked on {report.blocking()}"
    assert report.classify() == ENTRY_CANDIDATE
    assert report.may_ping is True


def test_33_no_score_can_override_a_failed_required_gate() -> None:
    for gate in sorted(REQUIRED_FOR_ENTRY):
        results = [r for r in _healthy_gates() if r.gate != gate]
        results.append(GateResult(gate=gate, answer=FAIL, source="fixture", observed_at=NOW,
                                  reason="deliberately failed"))
        report = build_report(MINT, results, now=NOW)
        assert report.classify() != ENTRY_CANDIDATE, f"{gate}=FAIL still produced an entry"
        assert report.may_ping is False


def test_34_unknown_never_becomes_pass_through_any_route() -> None:
    for gate in sorted(REQUIRED_FOR_ENTRY):
        results = [r for r in _healthy_gates() if r.gate != gate]
        report = build_report(MINT, results, now=NOW)
        assert report.classify() != ENTRY_CANDIDATE, f"missing {gate} still produced an entry"


def test_35_trending_appearance_alone_authorises_nothing() -> None:
    # A discovery source is not evidence. There is no gate for "appeared on a
    # board", and adding one would be the whole bug back again.
    assert not any("TREND" in gate for gate in GATES)
    assert not any("FOMO" in gate for gate in GATES)
    trending_only = [
        GateResult(gate="MOMENTUM_OK", answer=PASS, source="gmgn trending",
                   observed_at=NOW, reason="ranked #3")
    ]
    assert build_report(MINT, trending_only, now=NOW).classify() != ENTRY_CANDIDATE


# ===========================================================================
# 44 — nothing here can spend anything
# ===========================================================================


def test_44_no_signing_or_broadcast_path_exists_in_any_evidence_module() -> None:
    import smart_money_bot.lab.liquidityproof as liquidity
    import smart_money_bot.lab.organicflow as flow
    import smart_money_bot.lab.routeproof as routes

    for module in (liquidity, flow, routes):
        source = inspect.getsource(module)
        for forbidden in (
            "send_transaction", "sign_transaction", "Keypair", "private_key",
            "sendTransaction", "signAndSend", "import aiohttp", "aiosqlite",
        ):
            assert forbidden not in source, f"{module.__name__} must not be able to spend"


def test_a_simulation_is_read_only_by_construction() -> None:
    # The type carries a result, never a signer or a serialized transaction.
    fields = set(SellSimulation.__slots__)
    assert not fields & {"signature", "signer", "keypair", "transaction", "payer"}
