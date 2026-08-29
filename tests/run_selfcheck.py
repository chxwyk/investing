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

    print(
        "SELF-CHECK PASSED: detector, scoring, database, discovery rotation, "
        "paper P&L, risk gate, and PAPER laboratory invariants"
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


if __name__ == "__main__":
    asyncio.run(main())
