"""Regression suite for the v2.35 candidate-qualification funnel.

Every case here answers one question: does the bot still show me this?  The
fixtures are the synthetic scenarios that motivated the upgrade — a graduated
token with a pulse, an organically accelerating one, engineered activity,
a smart-wallet illusion, an overextended chart, missing safety evidence, and a
setup that qualified honestly and then rugged.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from smart_money_bot.bot import SmartMoneyBot, _runner_digest_embed, _runner_embed
from smart_money_bot.callouts import SolanaTrackerTokenRiskClient
from smart_money_bot.engine import SmartMoneyEngine
from smart_money_bot.models import (
    CoinCallout,
    DexSnapshot,
    RunnerForensics,
    RunnerFundingObservation,
    RunnerMarketSnapshot,
    SwapQuote,
    TokenInfo,
    TokenRiskSnapshot,
    XSocialSnapshot,
)
from smart_money_bot.quality import (
    STAGE_ENTRY,
    STAGE_HEATING,
    STAGE_QUALIFIED,
    STAGE_SILENT_WATCH,
    STAGE_STRONG,
    STAGE_UNSAFE,
    USER_FACING_STAGES,
    assess_runner_quality,
    attention_rank_key,
    merge_best_stage,
    rank_for_attention,
    why_surfaced,
)
from smart_money_bot.runner import (
    assess_runner_safety,
    build_funding_clusters,
    funding_observation_from_transaction,
    is_fresh_research_worthy,
    runner_candidate_from_json,
    runner_candidate_to_json,
    runner_snapshot_from_callout,
    runner_snapshot_to_json,
    score_runner_candidate,
    summarize_forensics,
)

MINT = "So11111111111111111111111111111111111111112"
MINT_TWO = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BASE_AT = 1_800_000_000


def _quote(impact: str = "1") -> SwapQuote:
    return SwapQuote(
        input_mint=MINT_TWO,
        output_mint=MINT,
        input_amount_raw=5_000_000,
        output_amount_raw=5_000_000,
        other_amount_threshold_raw=4_900_000,
        input_amount=Decimal("5"),
        output_amount=Decimal("5000000"),
        input_usd_value=Decimal("5"),
        output_usd_value=Decimal("5"),
        price_impact_percent=Decimal(impact),
        router="Jupiter",
        fee_bps=0,
        api_time_ms=10,
        observed_latency_ms=20,
        quoted_at=BASE_AT,
    )


def snapshot(
    at: int,
    *,
    price: str = "0.0010",
    market_cap: str = "50000",
    liquidity: str = "15000",
    volume: str = "3000",
    buys: int = 30,
    sells: int = 10,
    holders: int | None = 120,
    top10: str | None = "25",
    risk: str | None = "3",
    sell_route: str = "PASS",
    buy_route: str = "PASS",
    rugged: bool = False,
    authorities: bool | None = True,
    unique_buyers: int = 0,
    largest_buyer: str | None = None,
    dex_5m: str | None = "8",
) -> RunnerMarketSnapshot:
    return RunnerMarketSnapshot(
        mint=MINT,
        captured_at=at,
        price_usd=Decimal(price),
        market_cap_usd=Decimal(market_cap),
        liquidity_usd=Decimal(liquidity),
        volume_5m_usd=Decimal(volume),
        dex_price_change_5m_percent=(Decimal(dex_5m) if dex_5m is not None else None),
        buys_5m=buys,
        sells_5m=sells,
        holder_count=holders,
        verified_unique_buyers=unique_buyers,
        largest_verified_buyer_percent=(
            Decimal(largest_buyer) if largest_buyer is not None else None
        ),
        top10_percent=(Decimal(top10) if top10 is not None else None),
        dev_percent=Decimal("2") if top10 is not None else None,
        bundlers_percent=Decimal("2") if top10 is not None else None,
        insiders_percent=Decimal("1") if top10 is not None else None,
        snipers_percent=Decimal("3") if top10 is not None else None,
        risk_score=(Decimal(risk) if risk is not None else None),
        rugged=rugged,
        route_available=buy_route == "PASS",
        route_price_impact_percent=Decimal("1"),
        buy_route_status=buy_route,
        sell_route_status=sell_route,
        sell_route_price_impact_percent=Decimal("1") if sell_route == "PASS" else None,
        mint_authority_disabled=authorities,
        freeze_authority_disabled=authorities,
    )


def quality(
    first: RunnerMarketSnapshot,
    current: RunnerMarketSnapshot,
    *,
    history: tuple[RunnerMarketSnapshot, ...] = (),
    forensics: RunnerForensics | None = None,
    score_history: tuple[Decimal, ...] = (),
    raw_smart_wallets: int = 0,
    independent_smart_clusters: int | None = None,
    age_seconds: int = 180,
    dex_5m: Decimal | None = Decimal("8"),
):
    safety = assess_runner_safety(current, forensics)
    return assess_runner_quality(
        first=first,
        current=current,
        history=history,
        forensics=forensics,
        safety=safety,
        dex_price_change_5m=dex_5m,
        score_history=score_history,
        raw_smart_wallets=raw_smart_wallets,
        independent_smart_clusters=independent_smart_clusters,
        age_seconds=age_seconds,
        now=current.captured_at,
    ), safety


def clean_forensics(count: int = 10, *, at: int = BASE_AT) -> RunnerForensics:
    """Distinct funders, aged wallets, spread-out amounts: no coordination."""

    observations = [
        RunnerFundingObservation(
            wallet=f"independent-wallet-{index}",
            funder=f"independent-funder-{index}",
            funded_at=at - 100_000 - index * 5_000,
            amount_sol=Decimal(str(0.4 + index * 0.35)),
            supply_percent=Decimal("1.5"),
            wallet_age_seconds=400_000 + index * 1_000,
            trace_complete=True,
        )
        for index in range(count)
    ]
    return summarize_forensics(
        observations,
        raw_unique_buyers=44,
        raw_top10_percent=Decimal("25"),
        checked_at=at,
    )


def bundled_forensics(count: int = 9, *, at: int = BASE_AT) -> RunnerForensics:
    """One funder, near-identical amounts, tight funding and buy windows."""

    observations = [
        RunnerFundingObservation(
            wallet=f"linked-wallet-{index}",
            funder="single-shared-funder",
            funded_at=at - 600 + index * 3,
            amount_sol=Decimal("0.045"),
            bought_at=at - 60 + index,
            supply_percent=Decimal("2.2"),
            wallet_age_seconds=3_000,
            trace_complete=True,
        )
        for index in range(count)
    ]
    return summarize_forensics(
        observations,
        raw_unique_buyers=70,
        raw_top10_percent=Decimal("30"),
        checked_at=at,
    )


def _callout(**overrides) -> CoinCallout:
    values = {
        "liquidity": "15000",
        "market_cap": "50000",
        "volume": "3000",
        "buys": 30,
        "sells": 10,
        "holders": 120,
        "pair_age": 3,
        "route": True,
    }
    values.update(overrides)
    token = TokenInfo(
        mint=MINT,
        symbol="RUN",
        name="Real Runner",
        decimals=6,
        usd_price=Decimal("0.001"),
        liquidity_usd=Decimal(values["liquidity"]),
        market_cap_usd=Decimal(values["market_cap"]),
        holder_count=values["holders"],
        top_holders_percent=Decimal("25"),
        dev_balance_percent=Decimal("2"),
    )
    return CoinCallout(
        mint=MINT,
        symbol="RUN",
        name="Real Runner",
        score=Decimal("50"),
        verdict="WATCH",
        confidence="MEDIUM",
        smart_wallets=(),
        token_info=token,
        dex=DexSnapshot(
            available=True,
            liquidity_usd=Decimal(values["liquidity"]),
            market_cap_usd=Decimal(values["market_cap"]),
            pair_age_minutes=values["pair_age"],
            buys_5m=values["buys"],
            sells_5m=values["sells"],
            volume_5m_usd=Decimal(values["volume"]),
            price_change_5m_percent=Decimal("8"),
            pair_url=f"https://dexscreener.com/solana/{MINT}",
        ),
        social=XSocialSnapshot(available=False),
        tracker_risk=TokenRiskSnapshot(
            available=True,
            score=Decimal("3"),
            bundlers_percent=Decimal("2"),
            insiders_percent=Decimal("1"),
            snipers_percent=Decimal("3"),
            dev_percent=Decimal("2"),
            top10_percent=Decimal("25"),
        ),
        positives=(),
        warnings=(),
        hard_blockers=(),
        generated_at=BASE_AT,
        executable_quote=_quote() if values["route"] else None,
        quote_error=None if values["route"] else "no route",
    )


def candidate_for(**overrides):
    """Build a candidate the way the engine does: snapshot derived from the callout."""

    callout = _callout(**overrides)
    current = runner_snapshot_from_callout(callout, captured_at=BASE_AT)
    return score_runner_candidate(
        callout,
        first=current,
        current=current,
        graduated_at=BASE_AT - 120,
        graduation_source="DEX_PAIR_CREATED_PROXY — not exact Pump graduation",
        pair_created_at=BASE_AT - 120,
        now=BASE_AT,
    )


# ---------------------------------------------------------------------------
# CASE A–G regression scenarios
# ---------------------------------------------------------------------------


def test_case_a_graduated_garbage_never_reaches_the_research_feed() -> None:
    thin = snapshot(
        BASE_AT,
        market_cap="30000",
        liquidity="2500",
        volume="400",
        buys=3,
        sells=1,
        holders=None,
        top10=None,
        risk=None,
        sell_route="UNKNOWN",
        authorities=None,
    )

    result, safety = quality(thin, thin, age_seconds=90)

    assert result.stage == STAGE_SILENT_WATCH
    assert result.qualified is False
    assert safety.status == "UNKNOWN"
    assert len(result.evidence_families) < 2
    assert any("transactions" in item for item in result.quality_warnings)


def test_case_b_organic_acceleration_ranks_as_a_real_setup() -> None:
    first = snapshot(
        BASE_AT,
        market_cap="40000",
        liquidity="12000",
        volume="1200",
        buys=10,
        sells=4,
        holders=60,
    )
    middle = snapshot(
        BASE_AT + 60, market_cap="52000", liquidity="14400", volume="2600", buys=26, sells=8,
        holders=95, price="0.0013",
    )
    current = snapshot(
        BASE_AT + 120, market_cap="62000", liquidity="16000", volume="4300", buys=42, sells=12,
        holders=132, price="0.0015",
    )

    result, safety = quality(
        first,
        current,
        history=(first, middle),
        forensics=clean_forensics(),
        score_history=(Decimal("21"), Decimal("29"), Decimal("41")),
        raw_smart_wallets=2,
        independent_smart_clusters=2,
    )

    assert result.stage in {STAGE_ENTRY, STAGE_STRONG}
    assert result.qualified is True
    assert safety.status == "PASS"
    assert result.organic_score >= Decimal("70")
    assert "holders" in result.evidence_families
    assert result.quality_warnings == ()


def test_case_c_engineered_activity_is_not_treated_as_organic_demand() -> None:
    first = snapshot(
        BASE_AT,
        market_cap="90000",
        liquidity="9000",
        volume="2000",
        buys=8,
        sells=3,
        holders=88,
    )
    current = snapshot(
        BASE_AT + 120, market_cap="98000", liquidity="9000", volume="38000", buys=48, sells=22,
        holders=90,
    )

    result, _safety = quality(first, current, history=(first,), forensics=bundled_forensics())

    assert result.qualified is False
    assert result.coordination_veto is True
    assert result.organic_score <= Decimal("20")
    assert result.momentum_score >= Decimal("40"), "raw acceleration is still measured"
    joined = " ".join(result.quality_warnings)
    assert "independent clusters" in joined
    assert "time-linked" in joined
    assert "volume grew" in joined


def test_case_d_five_smart_wallets_in_one_cluster_are_not_five_confirmations() -> None:
    first = snapshot(
        BASE_AT,
        market_cap="40000",
        liquidity="12000",
        volume="1200",
        buys=10,
        sells=4,
        holders=60,
    )
    current = snapshot(
        BASE_AT + 120, market_cap="62000", liquidity="16000", volume="4300", buys=42, sells=12,
        holders=132, price="0.0015",
    )

    illusion, _ = quality(
        first, current, history=(first,), forensics=bundled_forensics(),
        raw_smart_wallets=5, independent_smart_clusters=1,
    )
    genuine, _ = quality(
        first, current, history=(first,), forensics=clean_forensics(),
        raw_smart_wallets=5, independent_smart_clusters=5,
    )

    assert illusion.demand.raw_smart_wallets == 5
    assert illusion.demand.independent_smart_clusters == 1
    assert illusion.qualified is False
    assert genuine.qualified is True
    assert genuine.opportunity_score > illusion.opportunity_score
    assert any("independent cluster" in item for item in illusion.quality_warnings)


def test_case_e_overextended_chart_cannot_become_entry_quality() -> None:
    first = snapshot(
        BASE_AT,
        market_cap="40000",
        liquidity="15000",
        volume="1200",
        buys=10,
        sells=4,
        holders=120,
    )
    current = snapshot(
        BASE_AT + 120, price="0.0035", market_cap="175000", liquidity="15000", volume="9000",
        buys=35, sells=25, holders=120,
    )

    result, safety = quality(
        first, current, history=(first,), forensics=clean_forensics(),
        dex_5m=Decimal("140"), age_seconds=400,
    )

    assert result.overextended is True
    assert safety.status == "PASS"
    assert result.stage not in {STAGE_ENTRY, STAGE_STRONG, STAGE_HEATING}
    assert result.momentum_score >= Decimal("60"), "the move itself is still measured"
    assert any("late-chase" in item for item in result.quality_warnings)


def test_case_f_unknown_safety_may_heat_up_but_never_becomes_entry() -> None:
    first = snapshot(
        BASE_AT,
        market_cap="40000",
        liquidity="12000",
        volume="1200",
        buys=10,
        sells=4,
        holders=60,
    )
    middle = snapshot(
        BASE_AT + 60,
        market_cap="52000",
        liquidity="14400",
        volume="2600",
        buys=26,
        sells=8,
        holders=95,
    )
    current = snapshot(
        BASE_AT + 120, market_cap="62000", liquidity="16000", volume="4300", buys=42, sells=12,
        holders=132, price="0.0015", risk=None, top10=None, sell_route="UNKNOWN",
    )

    result, safety = quality(
        first, current, history=(first, middle),
        score_history=(Decimal("21"), Decimal("33"), Decimal("48")),
        raw_smart_wallets=2, independent_smart_clusters=2,
    )

    assert safety.status == "UNKNOWN"
    assert safety.entry_eligible is False
    assert result.stage not in {STAGE_ENTRY, STAGE_STRONG}
    assert result.stage in USER_FACING_STAGES


@pytest.mark.asyncio
async def test_case_g_good_then_rug_still_escalates_then_invalidates_once(
    settings,
    tmp_path,
) -> None:
    notifier = SimpleNamespace(
        on_runner_risk_escalation=AsyncMock(return_value=True),
        on_runner_invalidated=AsyncMock(return_value=True),
        on_error=AsyncMock(),
    )
    engine = SmartMoneyEngine(
        replace(settings, database_path=str(tmp_path / "rug.db")),
        notifier=notifier,
    )
    healthy = candidate_for()
    healthy = replace(healthy, first_discord_visible_at=BASE_AT, stage=STAGE_QUALIFIED)
    collapsed_snapshot = snapshot(
        BASE_AT + 300, price="0.0002", market_cap="9000", liquidity="300", volume="120",
        buys=1, sells=14, holders=40, sell_route="FAIL",
    )
    collapsed = replace(
        healthy,
        current=collapsed_snapshot,
        generated_at=BASE_AT + 300,
        safety=assess_runner_safety(collapsed_snapshot),
    )
    await engine.initialize()
    try:
        await engine.database.store_runner_candidate(
            healthy,
            payload_json=runner_candidate_to_json(healthy),
            snapshot_json=runner_snapshot_to_json(healthy.current),
        )
        await engine.database.store_runner_candidate(
            collapsed,
            payload_json=runner_candidate_to_json(collapsed),
            snapshot_json=runner_snapshot_to_json(collapsed.current),
        )

        await engine._evaluate_runner_transitions(healthy, collapsed)
        await engine._evaluate_runner_transitions(healthy, collapsed)
    finally:
        await engine.close()

    notifier.on_runner_risk_escalation.assert_awaited_once()
    notifier.on_runner_invalidated.assert_awaited_once()
    _candidate, metrics, reasons = notifier.on_runner_invalidated.await_args.args
    assert reasons
    assert "first_market_cap" in metrics and "peak_market_cap" in metrics


# ---------------------------------------------------------------------------
# Funnel behaviour
# ---------------------------------------------------------------------------


def test_permissive_fresh_gate_is_ingest_only_not_a_discord_gate() -> None:
    graduate = candidate_for(liquidity="2500", market_cap="30000", volume="400", buys=3, sells=1)

    # The permissive v2.34 gate still admits it: young, liquid enough, alive.
    # That is exactly the "this graduated, therefore show it" behaviour, so it
    # must now decide watching, not showing.
    assert is_fresh_research_worthy(graduate) is True
    assert graduate.stage == STAGE_SILENT_WATCH
    assert graduate.quality.qualified is False


@pytest.mark.asyncio
async def test_raw_graduate_is_watched_silently_and_never_alerts(settings, tmp_path) -> None:
    notifier = SimpleNamespace(on_runner_fresh=AsyncMock(return_value=True), on_error=AsyncMock())
    engine = SmartMoneyEngine(
        replace(settings, database_path=str(tmp_path / "silent.db")),
        notifier=notifier,
    )
    graduate = candidate_for(
        liquidity="2600",
        market_cap="30000",
        volume="600",
        buys=4,
        sells=2,
        pair_age=1,
    )
    await engine.initialize()
    try:
        await engine.database.store_runner_candidate(
            graduate,
            payload_json=runner_candidate_to_json(graduate),
            snapshot_json=runner_snapshot_to_json(graduate.current),
        )

        published = await engine._maybe_publish_fresh(graduate)
        await engine._maybe_publish_runner(graduate)
    finally:
        await engine.close()

    assert published is False
    notifier.on_runner_fresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_digest_only_carries_qualified_candidates(settings, tmp_path) -> None:
    notifier = SimpleNamespace(on_runner_digest=AsyncMock(), on_error=AsyncMock())
    engine = SmartMoneyEngine(
        replace(settings, database_path=str(tmp_path / "ranked.db")),
        notifier=notifier,
    )
    good = candidate_for()
    weak = replace(
        candidate_for(liquidity="2600", market_cap="30000", volume="600", buys=4, sells=2),
        mint=MINT_TWO,
        symbol="WEAK",
        name="Weak Token",
    )
    await engine.initialize()
    try:
        for item in (good, weak):
            await engine.database.store_runner_candidate(
                item,
                payload_json=runner_candidate_to_json(item),
                snapshot_json=runner_snapshot_to_json(item.current),
            )

        assert await engine._publish_runner_digest() is True
        sent, _floor = notifier.on_runner_digest.await_args.args
    finally:
        await engine.close()

    assert [item.mint for item in sent] == [MINT]
    assert all(item.stage in USER_FACING_STAGES for item in sent)


def test_accelerating_young_candidate_outranks_a_stale_static_one() -> None:
    first = snapshot(
        BASE_AT,
        market_cap="40000",
        liquidity="12000",
        volume="1200",
        buys=10,
        sells=4,
        holders=60,
    )
    middle = snapshot(
        BASE_AT + 60,
        market_cap="48000",
        liquidity="13000",
        volume="2400",
        buys=24,
        sells=8,
        holders=90,
    )
    accelerating = snapshot(
        BASE_AT + 90,
        market_cap="55000",
        liquidity="14000",
        volume="3800",
        buys=38,
        sells=11,
        holders=118,
        price="0.0012",
    )
    rising, rising_safety = quality(
        first,
        accelerating,
        history=(first, middle),
        forensics=clean_forensics(),
        score_history=(Decimal("12"), Decimal("19"), Decimal("27"), Decimal("34")),
        age_seconds=90,
    )
    stale, stale_safety = quality(
        first,
        snapshot(
            BASE_AT + 1_500,
            market_cap="41000",
            liquidity="12000",
            volume="1150",
            buys=9,
            sells=8,
            holders=61,
        ),
        history=(first,),
        forensics=clean_forensics(),
        score_history=(Decimal("45"), Decimal("45"), Decimal("44")),
        age_seconds=1_500,
    )

    assert rising.momentum_score > stale.momentum_score
    assert attention_rank_key(rising, safety=rising_safety, age_seconds=90) > attention_rank_key(
        stale, safety=stale_safety, age_seconds=1_500
    )


def test_top_n_selection_keeps_only_the_best_few() -> None:
    first = snapshot(BASE_AT)
    rows = []
    for index in range(6):
        current = snapshot(
            BASE_AT + 60,
            market_cap=str(50_000 + index * 1_000),
            volume=str(1_000 + index * 900),
            buys=10 + index * 6,
            holders=100 + index * 12,
        )
        item, safety = quality(first, current, history=(first,), forensics=clean_forensics())
        rows.append((item, safety, 120, f"candidate-{index}"))

    selected = rank_for_attention(rows, limit=3)

    assert len(selected) == 3
    assert selected[0] == "candidate-5"


# ---------------------------------------------------------------------------
# Organic demand, funding and wallet age
# ---------------------------------------------------------------------------


def test_shared_funder_and_time_linked_funding_reduce_independence() -> None:
    independent = clean_forensics()
    bundled = bundled_forensics()

    assert independent.estimated_independent_clusters == independent.traced_wallets
    assert bundled.estimated_independent_clusters == 1
    assert bundled.time_linked_groups
    assert bundled.time_linked_groups[0].wallet_count == 9
    assert bundled.time_linked_groups[0].confidence == "HIGH"
    assert bundled.fresh_wallet_count == 9


def test_multi_hop_funding_links_wallets_whose_direct_funders_differ() -> None:
    observations = [
        RunnerFundingObservation(
            wallet=f"leaf-{index}",
            funder=f"intermediary-{index}",
            funded_at=BASE_AT - 400 + index * 5,
            amount_sol=Decimal("0.05"),
            bought_at=BASE_AT - 30 + index,
            supply_percent=Decimal("2"),
            upstream_funder="one-common-source",
            funder_depth=2,
            trace_complete=True,
        )
        for index in range(4)
    ]

    clusters = build_funding_clusters(observations)

    assert len(clusters) == 1
    upstream = clusters[0]
    assert upstream.cluster_kind == "UPSTREAM_FUNDER"
    assert upstream.cluster_id == "one-common-source"
    assert upstream.wallet_count == 4
    assert upstream.time_linked is True


def test_infrastructure_and_configured_funders_never_form_a_cluster() -> None:
    observations = [
        RunnerFundingObservation(wallet=f"w{index}", funder="known-exchange-hot-wallet")
        for index in range(5)
    ]

    assert build_funding_clusters(observations)
    assert (
        build_funding_clusters(
            observations, excluded_funders=frozenset({"known-exchange-hot-wallet"})
        )
        == ()
    )


def test_truncated_signature_page_never_claims_a_funder_or_a_wallet_age() -> None:
    transaction = {
        "blockTime": BASE_AT - 1_000,
        "transaction": {
            "message": {
                "accountKeys": [
                    {"pubkey": "funder", "signer": True},
                    {"pubkey": "holder", "signer": False},
                ]
            }
        },
        "meta": {
            "preBalances": [10_000_000_000, 0],
            "postBalances": [9_000_000_000, 1_000_000_000],
        },
    }

    truncated = funding_observation_from_transaction(
        transaction, wallet="holder", trace_complete=False, now=BASE_AT
    )
    complete = funding_observation_from_transaction(
        transaction, wallet="holder", trace_complete=True, now=BASE_AT
    )

    assert truncated.funder is None
    assert truncated.wallet_age_seconds is None
    assert truncated.trace_complete is False
    assert complete.funder == "funder"
    assert complete.amount_sol == Decimal("1")
    assert complete.wallet_age_seconds == 1_000
    assert complete.trace_complete is True


def test_independence_is_measured_over_traced_wallets_not_every_raw_buyer() -> None:
    partial = summarize_forensics(
        [
            RunnerFundingObservation(wallet=f"w{index}", funder="one-funder", trace_complete=True)
            for index in range(6)
        ],
        raw_unique_buyers=87,
        checked_at=BASE_AT,
    )
    current = snapshot(BASE_AT)

    result, _safety = quality(current, current, forensics=partial)

    assert partial.traced_wallets == 6
    assert partial.estimated_independent_clusters == 1
    # 1/6, not 86/87: tracing six holders says nothing about the other buyers.
    assert result.demand.independence_ratio == Decimal("0.1667")


def test_holder_growth_with_independent_buyers_beats_raw_volume() -> None:
    first = snapshot(BASE_AT, volume="1000", buys=8, sells=3, holders=60)
    organic = snapshot(BASE_AT + 120, volume="2600", buys=26, sells=8, holders=132)
    volume_only = snapshot(BASE_AT + 120, volume="26000", buys=90, sells=40, holders=61)

    good, _ = quality(first, organic, history=(first,), forensics=clean_forensics())
    noisy, _ = quality(first, volume_only, history=(first,), forensics=bundled_forensics())

    assert good.opportunity_score > noisy.opportunity_score
    assert good.organic_score > noisy.organic_score
    assert good.qualified is True
    assert noisy.qualified is False


def test_fragile_liquidity_relative_to_valuation_is_warned() -> None:
    fragile = snapshot(BASE_AT, market_cap="200000", liquidity="4000")

    result, _safety = quality(fragile, fragile)

    assert result.liquidity_to_market_cap == Decimal("0.0200")
    assert any("% of market cap" in item for item in result.quality_warnings)
    assert result.liquidity_quality is not None and result.liquidity_quality <= Decimal("40")


def test_price_pump_without_holder_or_liquidity_growth_is_penalized() -> None:
    first = snapshot(BASE_AT, price="0.0010", market_cap="40000", liquidity="12000", holders=100)
    confirmed = snapshot(
        BASE_AT + 120, price="0.0016", market_cap="64000", liquidity="14000", holders=140
    )
    unconfirmed = snapshot(
        BASE_AT + 120, price="0.0019", market_cap="76000", liquidity="12000", holders=100
    )

    healthy, _ = quality(first, confirmed, history=(first,), forensics=clean_forensics())
    hollow, _ = quality(first, unconfirmed, history=(first,), forensics=clean_forensics())

    assert healthy.price_quality is not None and hollow.price_quality is not None
    assert healthy.price_quality > hollow.price_quality
    assert any("without holder or liquidity" in item for item in hollow.quality_warnings)


# ---------------------------------------------------------------------------
# Safety separation
# ---------------------------------------------------------------------------


def test_unknown_safety_never_becomes_pass_and_blocks_entry() -> None:
    incomplete = snapshot(BASE_AT, risk=None, top10=None, sell_route="UNKNOWN", authorities=None)

    safety = assess_runner_safety(incomplete)

    assert safety.status == "UNKNOWN"
    assert safety.entry_eligible is False
    assert safety.critical_unknowns


def test_sell_route_failure_blocks_entry_outright() -> None:
    unsellable = snapshot(BASE_AT, sell_route="FAIL")

    safety = assess_runner_safety(unsellable)

    assert safety.status == "FAIL"
    assert safety.entry_eligible is False
    assert any("sell route" in item for item in safety.failures)


def test_high_momentum_with_failing_safety_becomes_unsafe_momentum() -> None:
    first = snapshot(
        BASE_AT,
        market_cap="40000",
        liquidity="12000",
        volume="1200",
        buys=10,
        sells=4,
        holders=60,
    )
    dangerous = snapshot(
        BASE_AT + 120, market_cap="62000", liquidity="16000", volume="6000", buys=52, sells=12,
        holders=150, price="0.0015", top10="88",
    )

    result, safety = quality(first, dangerous, history=(first,), forensics=clean_forensics())

    assert safety.status == "FAIL"
    assert result.momentum_score >= Decimal("70")
    assert result.stage == STAGE_UNSAFE
    assert result.stage not in {STAGE_ENTRY, STAGE_STRONG}


# ---------------------------------------------------------------------------
# Persistence, analytics, provider behaviour
# ---------------------------------------------------------------------------


def test_decision_snapshot_survives_a_json_round_trip() -> None:
    item = candidate_for()

    restored = runner_candidate_from_json(runner_candidate_to_json(item))

    assert restored.stage == item.stage
    assert restored.quality.opportunity_score == item.quality.opportunity_score
    assert restored.quality.evidence == item.quality.evidence
    assert restored.quality.demand.confidence == item.quality.demand.confidence
    assert restored.why_surfaced == item.why_surfaced


def test_best_stage_is_a_high_water_mark() -> None:
    assert merge_best_stage(STAGE_QUALIFIED, STAGE_SILENT_WATCH) == STAGE_QUALIFIED
    assert merge_best_stage(STAGE_SILENT_WATCH, STAGE_HEATING) == STAGE_HEATING
    assert merge_best_stage("", STAGE_QUALIFIED) == STAGE_QUALIFIED


@pytest.mark.asyncio
async def test_silent_candidates_keep_forward_tracking_and_feed_missed_runner_analytics(
    settings,
    tmp_path,
) -> None:
    engine = SmartMoneyEngine(replace(settings, database_path=str(tmp_path / "missed.db")))
    silent = replace(
        candidate_for(liquidity="2600", market_cap="30000", volume="600", buys=4, sells=2),
        mint="silent-runner-mint",
    )
    await engine.initialize()
    try:
        await engine.database.store_runner_candidate(
            silent,
            payload_json=runner_candidate_to_json(silent),
            snapshot_json=runner_snapshot_to_json(silent.current),
        )
        # The same token later triples. A funnel that discards its rejects
        # could never see this.
        later = replace(
            silent,
            current=replace(
                silent.current,
                captured_at=BASE_AT + 600,
                price_usd=Decimal("0.0032"),
                market_cap_usd=Decimal("96000"),
            ),
            generated_at=BASE_AT + 600,
        )
        await engine.database.store_runner_candidate(
            later,
            payload_json=runner_candidate_to_json(later),
            snapshot_json=runner_snapshot_to_json(later.current),
        )

        report = await engine.runner_quality_report(since_days=3650)
        stage_events = await engine.database.runner_stage_event_rows(mint=silent.mint)
    finally:
        await engine.close()

    assert report["silent_watched"] == 1
    assert report["qualified"] == 0
    assert report["missed_runners"] == 1
    assert "silent-runner-mint" in report["missed_runner_examples"]
    assert report["no_look_ahead"] is True
    assert stage_events, "every stage decision is persisted immutably"
    assert stage_events[0]["decision_version"] == "quality-v1"


@pytest.mark.asyncio
async def test_quality_report_records_provider_cost(settings, tmp_path) -> None:
    engine = SmartMoneyEngine(replace(settings, database_path=str(tmp_path / "cost.db")))
    await engine.initialize()
    try:
        await engine._record_provider_call("solana_rpc", "runner_forensics", calls=7, cache_hits=3)
        report = await engine.runner_quality_report(since_days=1)
    finally:
        await engine.close()

    rows = {(row["provider"], row["feature"]): row for row in report["provider_calls"]}
    assert rows[("solana_rpc", "runner_forensics")]["calls"] == 7
    assert rows[("solana_rpc", "runner_forensics")]["cache_hits"] == 3


@pytest.mark.asyncio
async def test_tracker_degraded_mode_reports_unknown_and_does_not_raise() -> None:
    client = SolanaTrackerTokenRiskClient("key")
    client._credit_failures = 3
    client._degraded_until = float("inf")

    result = await client.snapshot(MINT)
    unknown = snapshot(BASE_AT, risk=None, top10=None, sell_route="UNKNOWN", authorities=None)
    safety = assess_runner_safety(unknown)

    assert client.degraded is True
    assert result.available is False
    assert result.score is None
    assert safety.status == "UNKNOWN"
    assert safety.entry_eligible is False


# ---------------------------------------------------------------------------
# Discord surface
# ---------------------------------------------------------------------------


def test_digest_row_shows_why_surfaced_and_exact_mint_links() -> None:
    item = candidate_for()

    embed = _runner_digest_embed((item,), Decimal("70"), "ref")
    body = "\n".join(field.value for field in embed.fields)

    assert "WHY SURFACED" in body
    assert f"address={MINT}" in body
    assert f"https://pump.fun/coin/{MINT}" in body
    assert f"https://dexscreener.com/solana/{MINT}" in body
    assert f"https://solscan.io/token/{MINT}" in body
    assert why_surfaced(item.quality)


def test_runner_embed_makes_missing_safety_evidence_obvious() -> None:
    unknown_snapshot = snapshot(
        BASE_AT,
        risk=None,
        top10=None,
        sell_route="UNKNOWN",
        authorities=None,
    )
    item = replace(
        candidate_for(),
        current=unknown_snapshot,
        safety=assess_runner_safety(unknown_snapshot),
    )

    embed = _runner_embed(item, fomo_referral_code="ref")
    names = [field.name for field in embed.fields]

    assert any("SAFETY: UNKNOWN" in name for name in names)
    assert "Opportunity" in (embed.description or "")


def test_no_runner_surface_exposes_a_trading_control() -> None:
    item = candidate_for()

    embed = _runner_embed(item, fomo_referral_code="ref")
    digest = _runner_digest_embed((item,), Decimal("70"), "ref")
    text = " ".join(
        [embed.description or "", digest.description or ""]
        + [field.value for field in (*embed.fields, *digest.fields)]
    ).upper()

    for forbidden in ("JUP.AG/SWAP", "AUTO-BUY", "J7 LAUNCH", "SIGN TRANSACTION"):
        assert forbidden not in text


@pytest.mark.asyncio
async def test_digest_delivery_never_pings(settings) -> None:
    item = candidate_for()
    fake_bot = SimpleNamespace(_send_alert=AsyncMock())

    await SmartMoneyBot.on_runner_digest(fake_bot, (item,), Decimal("70"))

    kwargs = fake_bot._send_alert.await_args.kwargs
    assert kwargs["ping_user"] is False


@pytest.mark.asyncio
async def test_v234_database_upgrades_in_place_without_losing_history(tmp_path) -> None:
    """A live v2.34 database must keep every runner row and forward observation."""

    import sqlite3

    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE runner_candidates (
            mint TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            first_seen_at INTEGER NOT NULL,
            graduated_at INTEGER,
            graduation_source TEXT NOT NULL,
            first_price_usd REAL,
            first_market_cap_usd REAL,
            first_liquidity_usd REAL,
            first_score REAL NOT NULL,
            latest_score REAL NOT NULL,
            tier TEXT NOT NULL,
            x_verified INTEGER NOT NULL DEFAULT 0,
            last_seen_at INTEGER NOT NULL
        );
        CREATE TABLE runner_outcomes (
            mint TEXT NOT NULL,
            horizon_seconds INTEGER NOT NULL,
            observed_at INTEGER NOT NULL,
            price_return_percent REAL,
            market_cap_return_percent REAL,
            liquidity_return_percent REAL,
            liquidity_disappeared INTEGER NOT NULL DEFAULT 0,
            rugged INTEGER NOT NULL DEFAULT 0,
            route_available INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (mint, horizon_seconds)
        );
        """
    )
    legacy.execute(
        "INSERT INTO runner_candidates VALUES "
        "('legacy-mint', '{}', 1, NULL, 'UNAVAILABLE', 1.0, 2.0, 3.0, 4.0, 5.0, 'WATCH', 0, 9)"
    )
    legacy.executemany(
        "INSERT INTO runner_outcomes(mint, horizon_seconds, observed_at, price_return_percent) "
        "VALUES (?, ?, ?, ?)",
        [("legacy-mint", 60, 100, 12.5), ("legacy-mint", 300, 400, 44.0)],
    )
    legacy.commit()
    legacy.close()

    from smart_money_bot.database import Database

    database = Database(str(path), Decimal("1000"))
    await database.connect()
    try:
        cursor = await database.db.execute(
            "SELECT mint, stage, best_stage, qualified_at FROM runner_candidates"
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        outcomes = await database.db.execute("SELECT COUNT(*) AS n FROM runner_outcomes")
        outcome_count = dict(await outcomes.fetchone())["n"]
    finally:
        await database.close()

    assert [row["mint"] for row in rows] == ["legacy-mint"]
    assert rows[0]["stage"] == "RAW_DISCOVERY"
    assert rows[0]["best_stage"] == "RAW_DISCOVERY"
    assert rows[0]["qualified_at"] is None
    assert outcome_count == 2


@pytest.mark.asyncio
async def test_digest_visibility_keeps_escalation_and_invalidation_alive(
    settings,
    tmp_path,
) -> None:
    """Digest-only candidates must still be eligible for the risk lanes.

    Both risk escalation and setup invalidation key off
    ``first_discord_visible_at``. With fewer standalone fresh alerts, the digest
    has to record visibility or those lanes would silently stop firing for the
    candidates the user actually sees.
    """

    notifier = SimpleNamespace(on_runner_digest=AsyncMock(), on_error=AsyncMock())
    engine = SmartMoneyEngine(
        replace(settings, database_path=str(tmp_path / "visible.db")),
        notifier=notifier,
    )
    item = candidate_for()
    await engine.initialize()
    try:
        await engine.database.store_runner_candidate(
            item,
            payload_json=runner_candidate_to_json(item),
            snapshot_json=runner_snapshot_to_json(item.current),
        )

        assert await engine._publish_runner_digest() is True
        rows = await engine.database.runner_latency_rows(limit=10)
    finally:
        await engine.close()

    assert rows[0]["first_discord_visible_at"] is not None
    assert rows[0]["first_visible_market_cap_usd"] == float(item.current.market_cap_usd)
