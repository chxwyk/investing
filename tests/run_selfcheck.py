"""Dependency-light verification for restricted build environments."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import tempfile
import time
from dataclasses import replace
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
    await check_profit_optimization()
    await check_early_alpha()
    await check_trending_alpha()

    print(
        "SELF-CHECK PASSED: detector, scoring, database, discovery rotation, "
        "paper P&L, risk gate, PAPER laboratory, discovery-speed, realtime-alpha, "
        "SHADOW auto-trader, profit-optimization, early-alpha and "
        "Trending-first invariants"
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


async def check_profit_optimization() -> None:
    """The invariants that keep the bot from wasting money on itself.

    Every one of these traces to something observed in production: a paid
    provider hammered every minute after its plan ran out, a failure logged with
    no message at all, and a shared exit rule that would half-sell healthy
    positions for the duration of that outage.
    """

    import ast
    import inspect

    from smart_money_bot.errors import describe_exception
    from smart_money_bot.lab.config import DEFAULT_LAB_CONFIG
    from smart_money_bot.lab.exits import EXIT_SAFETY_EMERGENCY, ExitContext, open_position
    from smart_money_bot.lab.forward import (
        MIN_SAMPLE,
        VERDICT_DISABLED,
        VERDICT_INSUFFICIENT,
        WEIGHT_CEILING,
        WEIGHT_FLOOR,
        calibrate_families,
    )
    from smart_money_bot.lab.providers import (
        BACKOFF_SECONDS,
        PROVIDER_FEATURES,
        ProviderState,
        record_failure,
    )
    from smart_money_bot.lab.shadow import DEFAULT_SHADOW_CONFIG, FAMILY_CATALYST_WATCH
    from smart_money_bot.lab.shadow_exits import (
        SHADOW_SAFETY_MONITOR,
        RunnerEvidence,
        plan_shadow_exit,
    )
    from smart_money_bot.lab.shadow_metrics import ShadowTradeRecord

    # A provider that is failing must be called less, not more.
    state = ProviderState(name="solana_tracker")
    state = record_failure(state, now=0.0, status=403, message="Insufficient credits")
    assert state.is_degraded(now=0.0), "a credit failure must open a backoff window"
    assert BACKOFF_SECONDS[-1] <= 3_600, "backoff must stay bounded"
    unknown_record = record_failure(ProviderState(name="p"), now=0.0, status=404)
    assert not unknown_record.is_degraded(now=0.0), "a 404 is not a credit failure"

    # Core detection must survive without Solana Tracker.
    tracker_features = [
        item for item in PROVIDER_FEATURES if item.provider == "solana_tracker"
    ]
    assert tracker_features, "the provider map must describe Solana Tracker"
    assert all(
        not item.essential and item.on_chain_fallback for item in tracker_features
    ), "Solana Tracker must be optional enrichment with an on-chain fallback"

    # The refresh throttle must not disengage when the pool is empty.
    from smart_money_bot import engine as engine_module

    tree = ast.parse(inspect.getsource(engine_module))
    refresh = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "refresh_discovery"
    )
    conditions = " ".join(
        ast.unparse(node) for node in ast.walk(refresh) if isinstance(node, ast.BoolOp)
    )
    assert "self._candidate_pool" not in conditions, (
        "the discovery throttle must not depend on a non-empty candidate pool"
    )

    # A failure with no message must still say what failed.
    assert describe_exception(TimeoutError()).startswith("TimeoutError"), (
        "a timeout must never log as an empty string"
    )

    # A provider outage is not a token failure.
    position = open_position(
        position_id="p",
        mint="mint",
        now=1_000,
        decision_price_usd=Decimal("0.001"),
        size_usd=Decimal("10"),
        config=DEFAULT_SHADOW_CONFIG.exit_config(),
    )
    healthy = ExitContext(
        now=1_600,
        price_usd=Decimal("0.00105"),
        liquidity_usd=Decimal("42000"),
        entry_liquidity_usd=Decimal("40000"),
        momentum_score=Decimal("70"),
        organic_score=Decimal("65"),
        buys=140,
        sells=40,
        safety_status="UNKNOWN",
        route_available=True,
    )
    unguarded = plan_shadow_exit(position, healthy, RunnerEvidence())
    guarded = plan_shadow_exit(
        position, healthy, RunnerEvidence(safety_provider_degraded=True)
    )
    assert unguarded.plan.reason_code == EXIT_SAFETY_EMERGENCY
    assert guarded.plan.reason_code == SHADOW_SAFETY_MONITOR, (
        "a provider outage must not be read as a token failure"
    )
    confirmed = plan_shadow_exit(
        position,
        dataclasses.replace(healthy, safety_status="FAIL"),
        RunnerEvidence(safety_provider_degraded=True, safety_confirmed_fail=True),
    )
    assert confirmed.plan.final and confirmed.plan.fraction == Decimal("1"), (
        "a confirmed hard safety failure must still exit immediately and in full"
    )

    # Forward weights must be bounded, and a tiny sample must do nothing.
    def _trade(family: str, net: str, index: int, reason: str = "") -> ShadowTradeRecord:
        return ShadowTradeRecord(
            position_id=f"{family}{index}",
            mint="mint",
            family=family,
            opened_at=1_000,
            closed_at=1_000 + index,
            size_usd=Decimal("10"),
            realized_net_pnl_usd=Decimal(net),
            close_reason=reason,
            open=False,
        )

    lucky = [_trade(FAMILY_CATALYST_WATCH, "90", 1)]
    weights = calibrate_families(lucky, as_of=999_999)
    assert weights[FAMILY_CATALYST_WATCH].verdict == VERDICT_INSUFFICIENT
    assert weights[FAMILY_CATALYST_WATCH].weight == Decimal("1"), (
        "one lucky coin must not move the ranking"
    )
    assert MIN_SAMPLE >= 10

    rugging = [
        _trade(FAMILY_CATALYST_WATCH, "-6", index, "SAFETY_DETERIORATION")
        for index in range(1, 31)
    ]
    disabled = calibrate_families(rugging, as_of=999_999)[FAMILY_CATALYST_WATCH]
    assert disabled.verdict == VERDICT_DISABLED and not disabled.enabled, (
        "a family that loses money and rugs must be retired"
    )
    for entry in calibrate_families(rugging, as_of=999_999).values():
        assert WEIGHT_FLOOR <= entry.weight <= WEIGHT_CEILING

    # The experiment and the strict floor are untouched.
    assert DEFAULT_SHADOW_CONFIG.bankroll_usd == Decimal("100")
    assert DEFAULT_SHADOW_CONFIG.position_usd == Decimal("10")
    assert DEFAULT_SHADOW_CONFIG.max_concurrent_positions == 5
    assert DEFAULT_SHADOW_CONFIG.max_total_exposure_usd == Decimal("50")
    assert DEFAULT_LAB_CONFIG.normal_position_usd == Decimal("5")
    assert DEFAULT_LAB_CONFIG.min_independent_buyers == 12


async def check_early_alpha() -> None:
    """The invariants behind "the bot knew at $31K and I found out at $61K".

    Being early is the whole point of the release, so the first three checks are
    about *ordering*: the cheap lane must run before deep enrichment, its verdict
    must be reachable without any provider, and the market cap it recorded must
    not be rewritten once the price has moved.  The rest is restraint — a score
    alone must not ping, a creator self-buy must not read as demand, and a token
    that merely copied a campaign link must not inherit the real story.
    """

    import ast
    import inspect

    from smart_money_bot import engine as engine_module
    from smart_money_bot.fast_alerts import PINGABLE, URGENT_CLASSES
    from smart_money_bot.lab.early import (
        BUY_INSIDER,
        EDGE_CONSUMED,
        PINGABLE_TIERS,
        TIER_EARLY_HEADS_UP,
        TIER_NONE,
        TIER_ORGANIC_RUNNER,
        WHY_INSIDER_ONLY,
        WHY_MOVE_CONSUMED,
        WHY_NOT_SERIOUS,
        EarlyConfig,
        EarlySignals,
        detect_large_buy,
        evaluate_early_signal,
    )
    from smart_money_bot.lab.exits import ExitContext, open_position
    from smart_money_bot.lab.narrative import (
        DIR_STORY_TO_TOKEN,
        DIR_TOKEN_TO_STORY,
        INHERITS_STORY,
        REL_NAME_ONLY,
        REL_PLAUSIBLE,
        NarrativeEntity,
        StorySource,
        TokenIdentityClaim,
        assess_narrative_link,
        mark_official,
    )
    from smart_money_bot.lab.shadow import DEFAULT_SHADOW_CONFIG
    from smart_money_bot.lab.shadow_exits import (
        SHADOW_SOFT_PAUSE_HOLD,
        RunnerEvidence,
        plan_shadow_exit,
    )

    now = 1_800_000_000
    mint = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"

    def signals(**overrides) -> EarlySignals:
        payload = {
            "mint": mint,
            "now": now,
            "first_seen_at": now - 8,
            "pair_age_seconds": 82,
            "market_cap_usd": Decimal("33100"),
            "first_seen_market_cap_usd": Decimal("31180"),
            "liquidity_usd": Decimal("6900"),
            "volume_5m_usd": Decimal("5200"),
            "price_change_5m_percent": Decimal("14"),
            "buys_5m": 26,
            "sells_5m": 6,
            "route_available": True,
        }
        payload.update(overrides)
        return EarlySignals(**payload)

    # 1. The cheap lane runs before the deep gather.  This ordering *is* the fix.
    radar = inspect.getsource(engine_module.SmartMoneyEngine._run_fomo_radar)
    assert radar.index("_run_early_lane") < radar.index("evaluate(mint) for mint in selected"), (
        "first operator visibility must not wait on deep enrichment"
    )

    # 2. The verdict must be reachable from a DEX snapshot alone: no provider
    #    call, no wallet forensics, no social lookup anywhere in the module.
    from smart_money_bot.lab import early as early_module

    early_tree = ast.parse(inspect.getsource(early_module))
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(early_tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(early_tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not (imported & {"aiohttp", "httpx", "requests", "solana", "solders"}), (
        "the early lane must not be able to call a provider"
    )

    # 3. A grade the operator can act on, right at first sight.
    verdict = evaluate_early_signal(signals())
    assert verdict.tier == TIER_ORGANIC_RUNNER and verdict.may_ping
    assert verdict.entry_eligible is False, "the cheap lane must never authorise an entry"

    # 4. A late alert says so instead of dressing itself up as early.
    late = evaluate_early_signal(
        signals(first_seen_market_cap_usd=Decimal("31180"), market_cap_usd=Decimal("61490"))
    )
    assert late.edge_state == EDGE_CONSUMED and late.late
    assert WHY_MOVE_CONSUMED in late.why_not_pinged
    assert "EDGE CONSUMED" in late.label

    # 5. A score alone is never a reason to interrupt anyone.
    scored = evaluate_early_signal(
        signals(buys_5m=13, sells_5m=9, volume_5m_usd=Decimal("1400")),
        config=EarlyConfig(runner_min_score=Decimal("1")),
    )
    assert scored.tier == TIER_EARLY_HEADS_UP and not scored.may_ping
    assert WHY_NOT_SERIOUS in scored.why_not_pinged

    # 6. A creator self-buy is not demand.
    insider = detect_large_buy(
        signals(largest_buy_usd=Decimal("900"), largest_buy_is_creator_linked=True)
    )
    assert insider.quality == BUY_INSIDER and not insider.is_demand
    blocked = evaluate_early_signal(
        signals(largest_buy_usd=Decimal("900"), largest_buy_is_creator_linked=True, buys_5m=6)
    )
    assert blocked.tier == TIER_NONE and WHY_INSIDER_ONLY in blocked.why_not_pinged

    # 7. Only tiers that earned it may ping, and anything that may interrupt a
    #    person lands in the urgent lane.  A heads-up is radar only, always.
    assert set(PINGABLE_TIERS) == {"EARLY_RUNNER", "ORGANIC_RUNNER"}
    assert set(PINGABLE) <= set(URGENT_CLASSES), "a pingable class must ride the urgent lane"
    assert "EARLY_RUNNER" in PINGABLE
    assert "EARLY_HEADS_UP" not in PINGABLE and "EARLY_HEADS_UP" not in URGENT_CLASSES

    # 8. MINT IS IDENTITY: the same name is never the same token, and a token
    #    that only claims a link can never inherit the story's credibility.
    def story(links_mint: str = "") -> NarrativeEntity:
        return NarrativeEntity(
            narrative_id="grok-pocket",
            title="Grok Pocket",
            keywords=("grok pocket",),
            first_seen_at=now - 900,
            last_seen_at=now,
            sources=(
                StorySource(
                    name="campaign",
                    url="https://grokpocket.example",
                    observed_at=now - 900,
                    is_primary=True,
                    links_exact_mint=links_mint,
                ),
            ),
        )

    # A token that merely copied the campaign URL claims the link by itself, and
    # metadata can be copied, so it must never inherit the story's credibility.
    copycat = assess_narrative_link(
        story(),
        TokenIdentityClaim(
            mint=mint,
            name="Grok Pocket",
            website_url="https://grokpocket.example",
            created_at=now - 60,
        ),
        now=now,
    )
    assert copycat.direction == DIR_TOKEN_TO_STORY
    assert copycat.relationship not in INHERITS_STORY, (
        "metadata can be copied, so a token's own claim can never inherit a story"
    )
    assert copycat.relationship == REL_PLAUSIBLE and copycat.confidence <= Decimal("70")
    assert copycat.inherits_story is False

    other = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"
    name_only = assess_narrative_link(
        story(links_mint=mint),
        TokenIdentityClaim(mint=other, name="Grok Pocket", created_at=now - 60),
        now=now,
    )
    assert name_only.relationship == REL_NAME_ONLY, "same name is not the same token"
    assert name_only.mint == other and name_only.mint != mint

    try:
        mark_official(copycat, authority="operator")
    except ValueError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("token-to-story evidence must never establish OFFICIAL")

    # Only the story side, naming the exact mint, can establish OFFICIAL.
    from_story = assess_narrative_link(
        story(links_mint=mint),
        TokenIdentityClaim(mint=mint, name="Grok Pocket", created_at=now - 60),
        now=now,
    )
    assert from_story.direction == DIR_STORY_TO_TOKEN
    official = mark_official(from_story, authority="verified campaign page")
    assert official.relationship == "OFFICIAL" and official.mint == mint

    try:
        mark_official(from_story, authority="   ")
    except ValueError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("OFFICIAL requires a named authority")

    # 9. A pause is not a reversal: one weak print must not dump a healthy runner.
    position = open_position(
        position_id="p",
        mint=mint,
        now=1_000,
        decision_price_usd=Decimal("0.001"),
        size_usd=Decimal("10"),
        market_cap_usd=Decimal("60000"),
        config=DEFAULT_SHADOW_CONFIG.exit_config(),
    )
    # Momentum prints weak while buyers still lead and liquidity is growing.
    cooling = ExitContext(
        now=1_600,
        price_usd=Decimal("0.00105"),
        market_cap_usd=Decimal("90000"),
        liquidity_usd=Decimal("42000"),
        entry_liquidity_usd=Decimal("40000"),
        momentum_score=Decimal("10"),
        organic_score=Decimal("70"),
        buys=140,
        sells=40,
        volume_usd=Decimal("18000"),
        entry_volume_usd=Decimal("12000"),
        safety_status="PASS",
        route_available=True,
    )
    paused = plan_shadow_exit(position, cooling, RunnerEvidence())
    assert paused.plan.fraction == Decimal("0"), (
        "a single weak momentum print must not sell a healthy runner"
    )
    assert paused.plan.reason_code == SHADOW_SOFT_PAUSE_HOLD and not paused.plan.final

    decaying = plan_shadow_exit(
        position, cooling, RunnerEvidence(consecutive_weak_observations=3)
    )
    assert decaying.plan.fraction > Decimal("0"), "confirmed decay must still de-risk"

    # 10. Nothing here spends a cent, and the experiment is untouched.
    from smart_money_bot.lab.shadow import SHADOW_REAL_MONEY_SPEND

    assert SHADOW_REAL_MONEY_SPEND == 0
    assert DEFAULT_SHADOW_CONFIG.bankroll_usd == Decimal("100")
    assert DEFAULT_SHADOW_CONFIG.position_usd == Decimal("10")



async def check_trending_alpha() -> None:
    """The Trending-first invariants that must hold before a deploy is trusted.

    These are the product's non-negotiables, not a sample of the test suite: a
    deployment that violates any of them is worse than one that never shipped
    the feature, because it would present guesses as facts.
    """

    import tempfile
    from pathlib import Path as _Path

    from smart_money_bot.lab.shadow import (
        DEFAULT_SHADOW_CONFIG,
        SHADOW_STRATEGY_VERSION,
        SIGNAL_FAMILIES,
    )
    from smart_money_bot.stream import (
        CONFIGURATION_STREAM_STATES,
        STREAM_CONNECTED,
        STREAM_DISABLED,
        STREAM_NO_WALLETS,
        STREAM_STATES,
        RealtimeWalletStream,
    )
    from smart_money_bot.trending import (
        CHANGE_WINDOW_UNKNOWN,
        LEGACY_STRATEGY_VERSION,
        SOURCE_FOMO_TRENDING,
        SOURCE_NONE,
        SOURCE_TRENDING_PROXY,
        TRENDING_EXIT_POLICIES,
        TRENDING_FAMILIES,
        TRENDING_STRATEGY_VERSION,
        HotWatchConfig,
        TrendingLedgerEntry,
        TrendingObservation,
        TrendingShadowConfig,
        build_risk_panel,
        classify_trending_event,
        decide_alert,
        normalise_change_window,
        open_hot_watch,
        ramp,
        rank_velocity,
        recheck_hot_watch,
        score_trending_edge,
        source_from_settings,
    )
    from smart_money_bot.trending.exits import TrendingExitContext, evaluate_policy
    from smart_money_bot.trending.hotwatch import ORIGIN_TRENDING_NEAR_MISS
    from smart_money_bot.trending_store import TrendingStore

    now = 1_700_000_000

    # 1. A proxy can never present itself as Fomo Trending (section 4).
    proxy = source_from_settings(api_url=None, api_key=None, proxy_enabled=True)
    assert proxy.kind == SOURCE_TRENDING_PROXY and not proxy.is_exact_fomo
    assert "not Fomo" in proxy.rank_caveat()
    assert source_from_settings(
        api_url=None, api_key=None, proxy_enabled=False
    ).kind == SOURCE_NONE
    authorised = source_from_settings(
        api_url="https://feed.example/t", api_key=None, proxy_enabled=True
    )
    assert authorised.kind == SOURCE_FOMO_TRENDING and authorised.is_exact_fomo

    # 2. An undocumented percentage window is never guessed (section 6).
    assert normalise_change_window("who knows") == CHANGE_WINDOW_UNKNOWN
    assert normalise_change_window(None) == CHANGE_WINDOW_UNKNOWN

    def observation(**kwargs):
        payload = {
            "mint": "MintSelfCheck",
            "observed_at": now,
            "rank": 40,
            "market_cap_usd": Decimal("200000"),
            "liquidity_usd": Decimal("80000"),
            "source": proxy,
        }
        payload.update(kwargs)
        return TrendingObservation(**payload)

    # 3. First observations are immutable (sections 5, 93).
    entry = TrendingLedgerEntry.from_first_observation(observation(rank=44))
    entry = entry.observe(
        observation(observed_at=now + 60, rank=22, market_cap_usd=Decimal("260000"))
    )
    entry = entry.observe(
        observation(observed_at=now + 120, rank=8, market_cap_usd=Decimal("330000"))
    )
    assert entry.first_rank == 44, "the entry rank must never be rewritten"
    assert entry.first_market_cap_usd == Decimal("200000")
    assert entry.first_seen_at == now

    # 4. Mint is identity (section 13).
    try:
        entry.observe(observation(mint="OtherMint"))
    except ValueError:
        pass
    else:  # pragma: no cover - a merged mint is a product failure
        raise AssertionError("a ledger entry must refuse to merge a different mint")

    # 5. Rank velocity, not absolute rank, is the signal (sections 9, 95).
    velocity = rank_velocity(entry.rank_history, now=now + 120, first_seen_at=entry.first_seen_at)
    assert velocity.delta == 36 and velocity.climbing
    flat = TrendingLedgerEntry.from_first_observation(observation(rank=2))
    for step in range(1, 13):
        flat = flat.observe(observation(observed_at=now + step * 300, rank=2))
    flat_velocity = rank_velocity(
        flat.rank_history, now=now + 3600, first_seen_at=flat.first_seen_at
    )
    flat_event = classify_trending_event(flat, flat_velocity, now=now + 3600)
    flat_score = score_trending_edge(flat, flat_event)
    flat_verdict = decide_alert(flat_score, flat_event, alpha_threshold=Decimal("62"))
    assert not flat_verdict.alert, "a high static rank is not alpha"

    # 6. No threshold cliffs (section 43).
    near = ramp(Decimal("1.94"), floor=Decimal("0"), target=Decimal("2"), weight=Decimal("10"))
    exact = ramp(Decimal("2.00"), floor=Decimal("0"), target=Decimal("2"), weight=Decimal("10"))
    assert exact - near < Decimal("0.5"), "1.94 and 2.00 must not be different universes"

    # 7. Hard safety beats every attention signal (sections 71, 100).
    hot_entry = TrendingLedgerEntry.from_first_observation(observation(rank=40))
    hot_entry = hot_entry.observe(
        observation(observed_at=now + 60, rank=3, market_cap_usd=Decimal("400000"))
    )
    hot_velocity = rank_velocity(
        hot_entry.rank_history, now=now + 60, first_seen_at=hot_entry.first_seen_at
    )
    hot_event = classify_trending_event(hot_entry, hot_velocity, now=now + 60)
    blocked = build_risk_panel("MintSelfCheck", sell_failed=True, liquidity_collapsed=True)
    assert blocked.blocked
    blocked_score = score_trending_edge(hot_entry, hot_event, risk=blocked)
    assert blocked_score.score == Decimal("0.0") and not blocked_score.reasons
    blocked_verdict = decide_alert(
        blocked_score, hot_event, alpha_threshold=Decimal("62"), risk=blocked
    )
    assert not blocked_verdict.alert, "trending must never override a hard failure"

    # 8. A verified badge is a badge (section 37).
    badged = build_risk_panel("MintSelfCheck", fomo_verified="VERIFIED", safety_status="UNKNOWN")
    assert not badged.blocked
    assert any("not a safety guarantee" in concern for concern in badged.concerns)

    # 9. HOT WATCH promotes once, on named evidence, and expires quietly.
    config = HotWatchConfig(ttl_seconds=300, recheck_seconds=30)
    watch = open_hot_watch(
        "MintSelfCheck",
        origin=ORIGIN_TRENDING_NEAR_MISS,
        now=now,
        score=Decimal("52"),
        market_cap_usd=Decimal("500000"),
        heads_up_market_cap_usd=Decimal("500000"),
        config=config,
    )
    unnamed = recheck_hot_watch(
        watch, now=now + 40, score=Decimal("99"), reasons=(), alpha_threshold=Decimal("62")
    )
    assert not unnamed.promoted, "a score without a named reason must never ping"
    promoted = recheck_hot_watch(
        watch,
        now=now + 40,
        score=Decimal("70"),
        reasons=("TRENDING_ACCELERATION",),
        market_cap_usd=Decimal("600000"),
        alpha_threshold=Decimal("62"),
    )
    assert promoted.promoted and promoted.should_ping
    assert promoted.entry.promotion_move_percent() == Decimal("20.0")
    faded = recheck_hot_watch(
        watch,
        now=now + 400,
        score=Decimal("20"),
        reasons=("TRENDING_ACCELERATION",),
        alpha_threshold=Decimal("62"),
        config=config,
    )
    assert faded.expired and not faded.promoted

    # 10. A hot watch recheck is genuinely fast (section 46).
    assert HotWatchConfig().recheck_seconds <= 120, (
        "a hot watch that rechecks as slowly as the legacy radar is the bug it fixes"
    )

    # 11. The two experiments are isolated and identically shaped (sections 62-63).
    trending_config = TrendingShadowConfig()
    assert trending_config.strategy_version == TRENDING_STRATEGY_VERSION
    assert TRENDING_STRATEGY_VERSION != LEGACY_STRATEGY_VERSION == SHADOW_STRATEGY_VERSION
    assert trending_config.bankroll_usd == DEFAULT_SHADOW_CONFIG.bankroll_usd == Decimal("100")
    assert trending_config.position_usd == DEFAULT_SHADOW_CONFIG.position_usd == Decimal("10")
    assert trending_config.max_concurrent_positions == 5
    assert trending_config.max_total_exposure_usd == Decimal("50")
    for family in TRENDING_FAMILIES:
        assert family in SIGNAL_FAMILIES, f"{family} must be a registered shadow family"

    # 12. Every exit policy still obeys a hard failure (section 71).
    failure = [
        TrendingExitContext(
            at=now, seconds_held=60, unrealized_percent=Decimal("50"),
            peak_percent=Decimal("50"), sell_failed=True,
        )
    ]
    for policy in TRENDING_EXIT_POLICIES:
        decision = evaluate_policy(policy, failure)
        assert decision.exit and decision.reason == "SELL_FAILED", policy

    # 13. The wallet lane names its state instead of a bare boolean (section 52).
    assert len(set(STREAM_STATES)) == len(STREAM_STATES)
    assert STREAM_DISABLED in CONFIGURATION_STREAM_STATES

    with tempfile.TemporaryDirectory() as directory:
        database = Database(str(_Path(directory) / "trending.db"), Decimal("1000"))
        await database.connect()
        try:
            # 14. The schema is additive and idempotent (section 110).
            await database._init_schema()
            store = TrendingStore(database)
            fresh = TrendingLedgerEntry.from_first_observation(observation(rank=44))
            await store.record_observation(fresh, observation(rank=44))
            # A tampered write must not move the persisted entry numbers.
            await store.record_observation(
                replace(fresh, first_rank=1, first_market_cap_usd=Decimal("1"), current_rank=2)
            )
            reloaded = await store.load_entry("MintSelfCheck")
            assert reloaded is not None and reloaded.first_rank == 44, (
                "the SQL upsert must never rewrite a first observation"
            )
            assert reloaded.first_market_cap_usd == Decimal("200000")

            offline = RealtimeWalletStream(
                database, rpc_url="https://rpc.example/", explicit_ws_url=None, enabled=False
            )
            assert offline.health().state == STREAM_DISABLED
            await offline._run_connection()
            live = RealtimeWalletStream(
                database, rpc_url="https://rpc.example/", explicit_ws_url=None, enabled=True
            )
            await live._run_connection()
            assert live.health().state == STREAM_NO_WALLETS, (
                "no wallets is its own state, not a bare DISCONNECTED"
            )
            live._set_state(STREAM_CONNECTED)
            assert live.health().healthy and not live.health().fallback_active
        finally:
            await database.close()

    # 15. Nothing in the Trending package can move real funds (section 109).
    import pathlib as _pathlib

    import smart_money_bot.trending as _trending

    for path in _pathlib.Path(_trending.__file__).parent.glob("*.py"):
        source = path.read_text()
        for forbidden in ("Keypair", "send_transaction", "sign_transaction", "aiohttp"):
            assert forbidden not in source, f"{path.name} must stay provider- and signer-free"


if __name__ == "__main__":
    asyncio.run(main())
