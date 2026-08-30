"""Dependency-light verification for restricted build environments."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from smart_money_bot.config import Settings
from smart_money_bot.constants import WRAPPED_SOL_MINT
from smart_money_bot.database import Database
from smart_money_bot.detector import SwapDetector
from smart_money_bot.models import (
    DetectedSwap,
    DiscoveryCandidate,
    ExecutionMode,
    Side,
    Signal,
    TokenInfo,
    TraderMetrics,
)
from smart_money_bot.risk import RiskEngine
from smart_money_bot.scoring import score_trader

WALLET = "wallet"
TOKEN = "token"


class FakeMarket:
    async def price(self, mint: str) -> Decimal | None:
        return Decimal("100") if mint == WRAPPED_SOL_MINT else Decimal("1")


def token_balance(amount: int) -> dict:
    return {
        "mint": TOKEN,
        "owner": WALLET,
        "uiTokenAmount": {"amount": str(amount), "decimals": 6},
    }


def transaction(pre_sol: int, post_sol: int, pre_token: int, post_token: int) -> dict:
    return {
        "transaction": {"message": {"accountKeys": [{"pubkey": WALLET}]}},
        "meta": {
            "err": None,
            "fee": 5000,
            "preBalances": [pre_sol],
            "postBalances": [post_sol],
            "preTokenBalances": [token_balance(pre_token)],
            "postTokenBalances": [token_balance(post_token)],
        },
    }


def metrics(pnl: str, cost: str, wins: int, losses: int, trades: int, dd: str) -> TraderMetrics:
    return TraderMetrics(
        address=WALLET,
        alias="Trader",
        window_seconds=86_400,
        trades=trades,
        buys=trades // 2,
        sells=trades - trades // 2,
        wins=wins,
        losses=losses,
        realized_pnl_usd=Decimal(pnl),
        matched_cost_usd=Decimal(cost),
        volume_usd=Decimal("10000"),
        max_drawdown_usd=Decimal(dd),
    )


def make_settings(database_path: str) -> Settings:
    env = {
        "DATABASE_PATH": database_path,
        "POLL_INTERVAL_SECONDS": "5",
        "PAPER_STARTING_USD": "1000",
        "DEFAULT_COPY_USD": "10",
        "MAX_COPY_USD": "25",
        "CONSENSUS_MIN_TRADERS": "2",
    }
    with patch.dict(os.environ, env, clear=True):
        return Settings.from_env(require_discord_token=False)


async def main() -> None:
    detector = SwapDetector(FakeMarket(), Decimal("10"))
    buy = await detector.detect(
        transaction(10_000_000_000, 8_999_995_000, 0, 100_000_000),
        wallet=WALLET,
        signature="buy",
        block_time=int(time.time()),
    )
    assert buy and buy.side is Side.BUY and buy.usd_value == Decimal("100")
    sell = await detector.detect(
        transaction(9_000_000_000, 9_499_995_000, 100_000_000, 50_000_000),
        wallet=WALLET,
        signature="sell",
        block_time=int(time.time()),
    )
    assert sell and sell.side is Side.SELL and sell.usd_value == Decimal("50.0")

    consistent = metrics("300", "1500", 8, 2, 20, "40")
    lucky = metrics("1000", "100", 1, 0, 2, "0")
    assert score_trader(consistent, consistent) > score_trader(lucky, lucky)

    with tempfile.TemporaryDirectory() as directory:
        database_path = str(Path(directory) / "selfcheck.db")
        settings = make_settings(database_path)
        database = Database(database_path, Decimal("1000"))
        await database.connect()
        try:
            await database.add_trader(WALLET, "Trader")
            now = int(time.time())
            await database.record_swap(
                DetectedSwap(
                    signature="wallet-buy",
                    trader_address=WALLET,
                    block_time=now - 10,
                    side=Side.BUY,
                    token_mint=TOKEN,
                    token_amount=Decimal("100"),
                    quote_mint=WRAPPED_SOL_MINT,
                    quote_amount=Decimal("1"),
                    usd_value=Decimal("100"),
                    token_price_usd=Decimal("1"),
                )
            )
            await database.record_swap(
                DetectedSwap(
                    signature="wallet-sell",
                    trader_address=WALLET,
                    block_time=now,
                    side=Side.SELL,
                    token_mint=TOKEN,
                    token_amount=Decimal("100"),
                    quote_mint=WRAPPED_SOL_MINT,
                    quote_amount=Decimal("1.2"),
                    usd_value=Decimal("120"),
                    token_price_usd=Decimal("1.2"),
                )
            )
            trader_metrics = (await database.metrics(86_400))[0]
            assert trader_metrics.realized_pnl_usd == Decimal("20.0")

            signal = Signal(
                token_mint=TOKEN,
                side=Side.BUY,
                created_at=now,
                trader_addresses=("a", "b"),
                trader_aliases=("A", "B"),
                source_signatures=("a1", "b1"),
                combined_score=Decimal("75"),
                reference_price_usd=Decimal("1"),
            )
            signal_id = await database.record_signal(signal)
            paper_buy = await database.paper_execute(
                signal_id=signal_id,
                token_mint=TOKEN,
                side=Side.BUY,
                market_price_usd=Decimal("1"),
                size_usd=Decimal("100"),
                fee_bps=50,
                slippage_bps=100,
            )
            assert paper_buy is not None
            paper_sell = await database.paper_execute(
                signal_id=signal_id,
                token_mint=TOKEN,
                side=Side.SELL,
                market_price_usd=Decimal("1"),
                size_usd=Decimal("100"),
                fee_bps=50,
                slippage_bps=100,
            )
            assert paper_sell and paper_sell["realized_pnl"] < 0

            risk = RiskEngine(settings, database)
            healthy = TokenInfo(
                mint=TOKEN,
                decimals=6,
                liquidity_usd=Decimal("500000"),
                holder_count=5000,
                organic_score=Decimal("80"),
                mint_authority_disabled=True,
                freeze_authority_disabled=True,
                top_holders_percent=Decimal("20"),
            )
            decision = await risk.assess(
                signal=signal,
                mode=ExecutionMode.PAPER,
                token_info=healthy,
                market_price_usd=Decimal("1"),
            )
            assert decision.allowed

            discovery_candidate = DiscoveryCandidate(
                address="auto-wallet-one",
                alias="Auto One",
                realized_pnl_24h=Decimal("250"),
                previous_pnl_24h=None,
                roi_24h_percent=Decimal("18"),
                win_rate_percent=Decimal("70"),
                trades_24h=20,
                buys_24h=10,
                sells_24h=10,
                closed_tokens=8,
                invested_24h_usd=Decimal("1000"),
                volume_24h_usd=Decimal("2400"),
                last_trade_ms=None,
                score=Decimal("72"),
                rank=1,
            )
            refresh = await database.apply_discovery([discovery_candidate])
            assert refresh.added_wallets == ("auto-wallet-one",)
            discovered = await database.list_discovered()
            assert discovered[0].realized_pnl_24h == Decimal("250.0")
            tracked = await database.resolve_trader("auto-wallet-one")
            assert tracked and tracked.enabled and tracked.source == "auto"
        finally:
            await database.close()

    await check_paper_laboratory()
    await check_shadow_auto_trader()

    print(
        "SELF-CHECK PASSED: detector, scoring, database, discovery rotation, "
        "paper P&L, risk gate, PAPER laboratory, discovery-speed, realtime-alpha "
        "and SHADOW auto-trader invariants"
    )


async def check_paper_laboratory() -> None:
    """The invariants that must hold before this release may ever be trusted.

    These are deliberately the non-negotiables from the product contract, not a
    happy path: no live execution, safety never becomes PASS by omission, an old
    pump never returns as a fresh setup, no public account can enter or launch,
    and the broad social radar stays off.
    """

    import smart_money_bot.lab as lab
    from smart_money_bot.lab.decision import Decision, Reason
    from smart_money_bot.lab.entry import EntryContext, evaluate_entry
    from smart_money_bot.lab.lifecycle import (
        FIRST_DISCOVERY,
        LifecycleObservation,
        advance_lifecycle,
        new_lifecycle,
    )
    from smart_money_bot.lab.registry import (
        IDEA_ONLY_ACCOUNTS,
        TIER_A_ACCOUNTS,
        TIER_B_ACCOUNTS,
        TIER_C_ACCOUNTS,
    )

    assert lab.LIVE_EXECUTION_ENABLED is False, "live execution must stay disabled"
    assert lab.DEFAULT_LAB_CONFIG.broad_social_radar_enabled is False
    assert lab.DEFAULT_LAB_CONFIG.bankroll_usd == Decimal("100")
    assert lab.DEFAULT_LAB_CONFIG.normal_position_usd == Decimal("5")
    assert lab.DEFAULT_LAB_CONFIG.max_position_usd == Decimal("10")

    for account in (*TIER_A_ACCOUNTS, *TIER_B_ACCOUNTS, *TIER_C_ACCOUNTS, *IDEA_ONLY_ACCOUNTS):
        assert account.can_enter is False
        assert account.can_launch is False
    for account in IDEA_ONLY_ACCOUNTS:
        assert account.can_qualify_token is False

    # Missing evidence must never become PASS.
    blank = evaluate_entry(
        EntryContext(mint=TOKEN, now=1_000),
        lifecycle=new_lifecycle(TOKEN, now=0),
        bankroll=lab.BankrollState(),
    )
    assert not blank.entry_eligible
    assert Reason.SAFETY_UNKNOWN in blank.decision.reason_codes
    assert blank.decision.decision is not Decision.ENTRY

    # An old pump is never a fresh setup again.
    record = new_lifecycle(TOKEN, now=0)
    for at, price, market_cap, extra in (
        (10, "0.000032", "32000", {"surfaced": True, "qualified": True}),
        (60, "0.00015", "150000", {}),
        (120, "0.000038", "38000", {}),
    ):
        record = advance_lifecycle(
            record,
            LifecycleObservation(
                observed_at=at,
                price_usd=Decimal(price),
                market_cap_usd=Decimal(market_cap),
                **extra,
            ),
        )
    assert record.state != FIRST_DISCOVERY
    assert record.first_surface_market_cap_usd == Decimal("32000")
    assert record.historical_high_market_cap_usd == Decimal("150000")
    assert not record.is_fresh_setup

    rehydrated = lab.lifecycle_from_json(lab.lifecycle_to_json(record))
    assert rehydrated == record

    # Only NET PnL counts.
    cost = lab.estimate_round_trip_cost(Decimal("5"), buy_price_impact_percent=Decimal("1"))
    assert cost.total_cost_usd > 0
    assert cost.platform_fees_usd > 0 and cost.network_fees_usd > 0

    # --- v2.37 invariants -------------------------------------------------
    from smart_money_bot.discord_render import (
        MESSAGE_EMBED_LIMIT,
        SAFE_MESSAGE_BUDGET,
        CardField,
        CardSpec,
        render_message,
    )
    from smart_money_bot.lab.actionability import ActionabilityInputs, assess_actionability
    from smart_money_bot.lab.fastwatch import FastWatchSignals, evaluate_fast_watch
    from smart_money_bot.lab.latency import HISTORICAL, LatencySample

    # FAST WATCH is research visibility only and can never authorise an entry.
    hot = FastWatchSignals(
        now=1_000,
        pair_age_seconds=300,
        price_change_percent=Decimal("25"),
        volume_acceleration_ratio=Decimal("2"),
        buys=90,
        sells=20,
        liquidity_usd=Decimal("30000"),
        route_available=True,
    )
    watch = evaluate_fast_watch(hot)
    assert watch.entry_eligible is False, "FAST WATCH must never be entry eligible"
    assert watch.pending_evidence, "FAST WATCH must declare the evidence it skipped"

    # A pair created long before we saw it is historical, not ingestion latency.
    stale_timing = LatencySample(
        mint=TOKEN, source_name="feed", source_event_at=1_000, first_seen_at=1_000 + 67_620
    )
    assert stale_timing.timing_quality == HISTORICAL
    assert not stale_timing.counts_as_realtime

    # A materially negative, fading candidate is kept out of the current radar.
    jelly = assess_actionability(
        ActionabilityInputs(
            now=10_000,
            first_seen_at=4_000,
            return_since_first_seen_percent=Decimal("-21"),
            momentum_score=Decimal("20"),
            buys=5,
            sells=30,
        )
    )
    assert jelly.suppressed, "a deteriorated candidate must not rank beside fresh ones"

    # Several rich cards must fit one Discord message.
    card = CardSpec(
        title="Card",
        description="D" * 400,
        compact_description="Token `MINT`",
        fields=tuple(CardField(f"F{index}", "v" * 900) for index in range(6)),
    )
    embeds, _ = render_message([card] * 5)
    total = sum(len(item) for item in embeds)
    assert total <= SAFE_MESSAGE_BUDGET <= MESSAGE_EMBED_LIMIT

    # --- v2.38 invariants -------------------------------------------------
    from smart_money_bot import fast_alerts as fast
    from smart_money_bot.lab.catalyst import (
        CONNECTION_OFFICIAL,
        M_CIRCULAR_SOURCING,
        CatalystEvent,
        ConfluenceInputs,
        EventSource,
        assess_event,
        assess_token_link,
        classify_catalyst_alert,
    )
    from smart_money_bot.lab.fastwatch import still_current
    from smart_money_bot.lab.notable import (
        EDGE_CONSUMED,
        ONCHAIN_ONLY,
        NotableSignal,
        NotableTrade,
        NotableWallet,
        build_consensus,
        decide_ping,
    )

    # FAST WATCH now has a publication path, and it is still not entry eligible.
    watch_alert = fast.build_fast_watch_alert(
        mint=TOKEN,
        name="Token",
        symbol="TKN",
        fomo_url="https://fomo.biz/token/x",
        verdict=watch,
        age_seconds=300,
        market_cap_usd=Decimal("90000"),
        first_seen_market_cap_usd=Decimal("60000"),
        liquidity_usd=Decimal("30000"),
        move_since_first_seen_percent=Decimal("50"),
        momentum_score=Decimal("70"),
        organic_score=None,
        buys=90,
        sells=20,
    )
    assert watch_alert.entry_eligible is False, "a published FAST WATCH cannot be an entry"
    assert watch_alert.may_ping is False, "FAST WATCH must never interrupt the user"
    assert any(item.name == "SAFETY" for item in watch_alert.spec.fields)

    # A queued candidate cannot publish as "early" after the move happened.
    queued_ok, queued_reason = still_current(hot, first_seen_at=hot.now - 3_600)
    assert queued_ok is False and "queued" in queued_reason

    # An unmapped wallet is never given an identity.
    anon = NotableWallet(wallet=WALLET, provenance=ONCHAIN_ONLY, anonymous_index=17)
    assert anon.identified is False and anon.display_name() == "Wallet #17"
    try:
        NotableWallet(wallet=WALLET, label="Someone", provenance=ONCHAIN_ONLY)
    except ValueError:
        pass
    else:  # pragma: no cover - the guard must hold
        raise AssertionError("an anonymous wallet must never carry a public label")

    # Lateness is quantified and published, and a late signal is never chased.
    late_trade = NotableTrade(
        wallet=WALLET,
        mint=TOKEN,
        signature="sig",
        chain_time=1_000,
        observed_at=1_004,
        entry_market_cap_usd=Decimal("48000"),
    )
    late_signal = NotableSignal(
        trade=late_trade,
        wallet_profile=anon,
        detection_market_cap_usd=Decimal("50000"),
        current_market_cap_usd=Decimal("500000"),
        now=1_030,
    )
    assert late_signal.freshness() == EDGE_CONSUMED
    assert late_signal.may_chase() is False
    assert decide_ping(late_signal).ping is False
    late_card = fast.build_notable_trader_alert(
        signal=late_signal, fomo_url="u", name="Token", symbol="TKN"
    )
    assert late_card.kind == fast.NOTABLE_TRADER_LATE
    assert late_card.may_ping is False and late_card.entry_eligible is False

    # A funded swarm is one actor, never several confirmations.
    swarm = [
        NotableSignal(
            trade=NotableTrade(
                wallet=f"w{index}",
                mint=TOKEN,
                signature=f"s{index}",
                chain_time=1_000,
                observed_at=1_002,
                entry_market_cap_usd=Decimal("48000"),
            ),
            wallet_profile=anon,
            current_market_cap_usd=Decimal("54000"),
            now=1_030,
        )
        for index in range(4)
    ]
    clustered = build_consensus(swarm, cluster_of={f"w{i}": "funder" for i in range(4)})
    assert clustered.raw_wallets == 4 and clustered.independent_wallets == 1
    assert clustered.is_independent_consensus is False

    # A quoted repost is not an independent confirmation.
    primary = EventSource(
        name="Official",
        published_at=900,
        is_primary=True,
        account_verified=True,
        tier="TIER_A_OFFICIAL",
        content_hash="p",
    )
    quoting = EventSource(
        name="Repost", published_at=940, quotes_source="Official", content_hash="q"
    )
    circular = assess_event(
        CatalystEvent(
            event_id="evt",
            headline="Exchange lists a Solana memecoin",
            detected_at=1_000,
            occurred_at=900,
            sources=(primary, quoting),
            crypto_relevance=Decimal("90"),
        ),
        now=1_000,
    )
    assert circular.independent_confirmations == 0
    assert M_CIRCULAR_SOURCING in circular.markers

    # A verified event is never evidence that a token is real.
    verified = assess_event(
        CatalystEvent(
            event_id="evt2",
            headline="Exchange lists a Solana memecoin",
            detected_at=1_000,
            occurred_at=900,
            sources=(
                primary,
                EventSource(name="A", published_at=930, account_verified=True, content_hash="a"),
                EventSource(name="B", published_at=935, account_verified=True, content_hash="b"),
            ),
            discussion_velocity=Decimal("80"),
            novelty=Decimal("90"),
            crypto_relevance=Decimal("95"),
        ),
        now=1_000,
    )
    link = assess_token_link(
        mint=TOKEN,
        event=verified,
        name_similarity=Decimal("100"),
        minted_after_event=True,
        seconds_after_event=120,
    )
    assert link.connection != CONNECTION_OFFICIAL
    assert link.official is False, "only the event's own source can make a link OFFICIAL"

    # Confluence raises priority, never eligibility.
    convergence = classify_catalyst_alert(
        ConfluenceInputs(
            event=verified,
            link=assess_token_link(
                mint=TOKEN,
                event=verified,
                name_similarity=Decimal("100"),
                minted_after_event=True,
                seconds_after_event=90,
            ),
            token_age_seconds=180,
            independent_notable_wallets=3,
            proven_early_wallets=2,
            earliest_notable_entry_market_cap_usd=Decimal("40000"),
            current_market_cap_usd=Decimal("52000"),
            independent_buyers_accelerating=True,
            liquidity_growing=True,
            current_actionability=Decimal("75"),
            safety_status="PASS",
        ),
        now=1_000,
    )
    assert convergence.entry_eligible is False, "confluence must never authorise an entry"
    assert any("EVENT VERIFIED" in item for item in convergence.warnings)

    # A degraded provider becomes UNKNOWN, never PASS by omission.
    degraded = fast.enrichment_from_evidence(
        alert_key="k", safety_status="PASS", provider_degraded="Solana Tracker"
    )
    safety_field = next(item for item in degraded.fields if item.name == "SAFETY")
    assert "UNKNOWN" in safety_field.value and "**PASS**" not in safety_field.value

    # Only the three urgent classes may interrupt the user.
    assert fast.FAST_WATCH not in fast.PINGABLE
    assert fast.NOTABLE_TRADER_LATE not in fast.PINGABLE
    assert fast.CATALYST_WATCH not in fast.PINGABLE


async def check_shadow_auto_trader() -> None:
    """The non-negotiables of the $100 / $10 forward experiment.

    These are the claims the whole experiment rests on: every entry is exactly
    $10, the book cannot exceed 5 positions or $50, the two strategy families
    never share state, and nothing in the shadow path can spend real money.
    """

    import ast
    import importlib
    import inspect
    from contextlib import suppress

    from smart_money_bot.database import Database
    from smart_money_bot.lab.bankroll import BankrollState
    from smart_money_bot.lab.config import DEFAULT_LAB_CONFIG
    from smart_money_bot.lab.shadow import (
        DEFAULT_SHADOW_CONFIG,
        SHADOW_REAL_MONEY_SPEND,
        SIGNAL_FAMILIES,
        ShadowConfig,
        ShadowExposure,
        ShadowSignal,
        ShadowTimestamps,
        evaluate_shadow_entry,
    )
    from smart_money_bot.lab.venues import (
        FILL_FALLBACK_PENALISED,
        BondingCurveState,
        bonding_curve_quote,
    )
    from smart_money_bot.shadow_runtime import ShadowRuntime
    from smart_money_bot.shadow_store import ShadowStore

    config = DEFAULT_SHADOW_CONFIG
    assert config.position_usd == Decimal("10"), "every shadow entry must be exactly $10"
    assert config.min_position_usd == Decimal("10"), "there is no $5 shadow entry"
    assert config.max_position_usd == Decimal("10"), "no signal may buy more than $10"
    assert config.bankroll_usd == Decimal("100")
    assert config.max_concurrent_positions == 5
    assert config.max_total_exposure_usd == Decimal("50")
    assert config.max_token_exposure_usd == Decimal("10")
    assert config.net_profit_objective_usd == Decimal("2")

    # A misconfigured stake must fail loudly, never silently skew the cohorts.
    try:
        ShadowConfig(position_usd=Decimal("5"))
    except ValueError:
        pass
    else:  # pragma: no cover - the guard above must raise
        raise AssertionError("a $5 shadow configuration must be refused")

    assert not SHADOW_REAL_MONEY_SPEND, "SHADOW_REAL_MONEY_SPEND must be zero"

    # No shadow module may reach a signer, a wallet or a swap submission.
    forbidden = {
        "Keypair",
        "sign_message",
        "sign_versioned_transaction",
        "VersionedTransaction",
        "execute_order",
        "load_keypair",
        "JupiterClient",
    }
    for module_name in (
        "smart_money_bot.lab.shadow",
        "smart_money_bot.lab.shadow_exits",
        "smart_money_bot.lab.shadow_metrics",
        "smart_money_bot.lab.venues",
        "smart_money_bot.shadow_runtime",
        "smart_money_bot.shadow_store",
    ):
        module = importlib.import_module(module_name)
        tree = ast.parse(inspect.getsource(module))
        names = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Name | ast.Attribute)
        }
        leaked = names & forbidden
        assert not leaked, f"{module_name} must not reference {leaked}"

    # STRICT PAPER and SHADOW are different strategy families.
    assert DEFAULT_SHADOW_CONFIG.strategy_version != DEFAULT_LAB_CONFIG.strategy_version

    def signal(mint: str, family: str) -> ShadowSignal:
        return ShadowSignal(
            mint=mint,
            family=family,
            timestamps=ShadowTimestamps(signal_at=1_000, decision_at=1_000),
            price_usd=Decimal("0.001"),
            market_cap_usd=Decimal("60000"),
            liquidity_usd=Decimal("40000"),
            buys=80,
            sells=20,
            route_available=True,
        )

    state = BankrollState(
        starting_usd=Decimal("100"),
        cash_usd=Decimal("100"),
        peak_equity_usd=Decimal("100"),
    )
    for family in SIGNAL_FAMILIES:
        decision = evaluate_shadow_entry(signal("mint-a", family), state)
        assert decision.accepted, f"{family} must be able to open a shadow trade"
        assert decision.size_usd == Decimal("10"), f"{family} must deploy exactly $10"

    # Only $7 left is refused honestly, never rounded into a fake $10 trade.
    thin = BankrollState(
        starting_usd=Decimal("100"),
        cash_usd=Decimal("7"),
        open_exposure_usd=Decimal("40"),
        peak_equity_usd=Decimal("47"),
    )
    refused = evaluate_shadow_entry(
        signal("mint-b", SIGNAL_FAMILIES[0]),
        thin,
        ShadowExposure(open_positions=4, open_exposure_usd=Decimal("40")),
    )
    assert not refused.accepted and refused.size_usd == Decimal("0")

    # A completed bonding curve refuses to invent a price for a $10 buy.
    completed = bonding_curve_quote(
        BondingCurveState(
            virtual_sol_reserves=Decimal("32"),
            virtual_token_reserves=Decimal("1073000000"),
            complete=True,
            sol_price_usd=Decimal("150"),
        ),
        side="BUY",
        notional_usd=Decimal("10"),
    )
    assert not completed.usable, "a graduated curve must not price a bonding-curve buy"

    # A fallback price is always labelled, never presented as an executable fill.
    from smart_money_bot.lab.venues import fallback_quote

    fallback = fallback_quote(
        side="BUY", notional_usd=Decimal("10"), observed_price_usd=Decimal("0.001")
    )
    assert fallback.source == FILL_FALLBACK_PENALISED
    assert fallback.fill_price_usd > Decimal("0.001")

    # End to end: the book stops at 5 positions and $50, and the strict PAPER
    # tables stay empty throughout.
    path = tempfile.mktemp(suffix=".db")
    database = Database(path, Decimal("1000"))
    await database.connect()
    try:
        runtime = ShadowRuntime(ShadowStore(database))
        await runtime.start_experiment(now=900)
        for index in range(7):
            await runtime.consider_signal(
                signal(f"mint-{index}", SIGNAL_FAMILIES[0]), now=1_000 + index
            )
        book = await runtime.bankroll()
        assert book.open_positions == 5, "the shadow book must stop at five positions"
        assert book.open_exposure_usd == Decimal("50"), "exposure must stop at $50"
        assert book.cash_usd == Decimal("50")

        cursor = await database.db.execute("SELECT COUNT(*) AS total FROM lab_positions")
        assert (await cursor.fetchone())["total"] == 0, "SHADOW must not touch STRICT PAPER"

        status = await runtime.status()
        assert status["live_execution_enabled"] is False
        assert not status["real_money_spend_usd"]
    finally:
        await database.close()
        with suppress(FileNotFoundError):
            os.unlink(path)


if __name__ == "__main__":
    asyncio.run(main())
