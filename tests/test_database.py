from __future__ import annotations

import sqlite3
import time
from dataclasses import replace
from decimal import Decimal

import pytest

from smart_money_bot.database import Database
from smart_money_bot.models import DiscoveryCandidate, Side, Signal


def _discovery_candidate(address: str, *, rank: int, pnl: str) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        address=address,
        alias=f"Wallet {address}",
        realized_pnl_24h=Decimal(pnl),
        previous_pnl_24h=None,
        roi_24h_percent=Decimal("20"),
        win_rate_percent=Decimal("70"),
        trades_24h=20,
        buys_24h=10,
        sells_24h=10,
        closed_tokens=10,
        invested_24h_usd=Decimal("2000"),
        volume_24h_usd=Decimal("5000"),
        last_trade_ms=int(time.time() * 1000),
        score=Decimal("80"),
        rank=rank,
        realized_pnl_7d=Decimal("2500"),
        roi_7d_percent=Decimal("35"),
        win_rate_7d_percent=Decimal("68"),
        trades_7d=80,
        recent_swaps=3,
        pump_swaps=2,
        last_activity_at=int(time.time()),
        selection_reason="strict 24H/7D + recent Pump activity",
    )


@pytest.mark.asyncio
async def test_existing_paper_database_migrates_raw_mirror_columns(tmp_path) -> None:
    path = tmp_path / "existing.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER,
            token_mint TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity REAL NOT NULL,
            execution_price_usd REAL NOT NULL,
            gross_value_usd REAL NOT NULL,
            fee_usd REAL NOT NULL,
            realized_pnl_usd REAL NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE paper_mirror_positions (
            trader_address TEXT NOT NULL,
            token_mint TEXT NOT NULL,
            source_quantity REAL NOT NULL,
            paper_quantity REAL NOT NULL,
            cost_basis_usd REAL NOT NULL,
            average_entry_usd REAL NOT NULL,
            opened_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (trader_address, token_mint)
        )
        """
    )
    connection.commit()
    connection.close()

    database = Database(str(path), Decimal("1000"))
    await database.connect()
    try:
        cursor = await database.db.execute("PRAGMA table_info(paper_trades)")
        columns = {row["name"] for row in await cursor.fetchall()}
        assert {
            "source_trader",
            "source_signature",
            "execution_kind",
            "exit_reason",
            "source_price_usd",
            "quote_price_usd",
            "price_drift_percent",
            "price_impact_percent",
            "quote_router",
            "quote_latency_ms",
            "quote_fee_bps",
            "quote_based",
        } <= columns
        mirror_cursor = await database.db.execute("PRAGMA table_info(paper_mirror_positions)")
        mirror_columns = {row["name"] for row in await mirror_cursor.fetchall()}
        assert {"peak_price_usd", "token_decimals"} <= mirror_columns
        assert await database.paper_mirror_positions() == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_hot_wallet_rotation_records_add_and_remove_audit(tmp_path) -> None:
    database = Database(str(tmp_path / "rotation.db"), Decimal("1000"))
    await database.connect()
    try:
        first = _discovery_candidate("wallet-a", rank=1, pnl="500")
        second = _discovery_candidate("wallet-b", rank=2, pnl="400")
        initial = await database.apply_discovery(
            [first, second], candidate_pool_size=100, verified_pump_wallets=2
        )
        assert set(initial.added_wallets) == {"wallet-a", "wallet-b"}

        weakened = replace(first, realized_pnl_24h=Decimal("25"), recent_swaps=0)
        refreshed = await database.apply_discovery(
            [second],
            evaluated_candidates=[weakened, second],
            removal_reasons={"wallet-a": "24H profit fell below the strict minimum"},
            candidate_pool_size=100,
            verified_pump_wallets=1,
        )

        assert refreshed.disabled_wallets == ("wallet-a",)
        assert refreshed.removal_events[0].reason.startswith("24H profit fell")
        events = await database.rotation_events(limit=10)
        assert events[0].action == "REMOVED"
        reports = await database.hot_wallet_reports(limit=25)
        assert [report["address"] for report in reports] == ["wallet-b"]
        assert reports[0]["pump_swaps"] == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_rotated_wallet_with_open_paper_lot_stays_enabled_for_exit(tmp_path) -> None:
    database = Database(str(tmp_path / "exit-only.db"), Decimal("1000"))
    await database.connect()
    try:
        first = _discovery_candidate("wallet-a", rank=1, pnl="500")
        second = _discovery_candidate("wallet-b", rank=2, pnl="400")
        await database.apply_discovery([first, second])
        await database.paper_mirror_execute(
            trader_address="wallet-a",
            source_signature="wallet-a-buy",
            token_mint="mint-a",
            side=Side.BUY,
            source_token_amount=Decimal("100"),
            market_price_usd=Decimal("1"),
            size_usd=Decimal("10"),
            fee_bps=0,
            slippage_bps=0,
        )

        refreshed = await database.apply_discovery(
            [second],
            evaluated_candidates=[first, second],
            removal_reasons={"wallet-a": "rotated out"},
        )

        retained = await database.resolve_trader("wallet-a")
        assert refreshed.disabled_wallets == ("wallet-a",)
        assert retained is not None and retained.enabled is True
        assert await database.trader_is_exit_only("wallet-a") is True
        assert await database.exit_only_trader_count() == 1

        await database.paper_mirror_execute(
            trader_address="wallet-a",
            source_signature="wallet-a-sell",
            token_mint="mint-a",
            side=Side.SELL,
            source_token_amount=Decimal("100"),
            market_price_usd=Decimal("1.2"),
            size_usd=Decimal("10"),
            fee_bps=0,
            slippage_bps=0,
        )
        await database.apply_discovery([second], evaluated_candidates=[second])
        disabled = await database.resolve_trader("wallet-a")
        assert disabled is not None and disabled.enabled is False
        assert await database.exit_only_trader_count() == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_paper_round_trip_includes_costs(tmp_path) -> None:
    database = Database(str(tmp_path / "paper.db"), Decimal("1000"))
    await database.connect()
    try:
        signal = Signal(
            token_mint="mint",
            side=Side.BUY,
            created_at=int(time.time()),
            trader_addresses=("a", "b"),
            trader_aliases=("A", "B"),
            source_signatures=("one", "two"),
            combined_score=Decimal("75"),
            reference_price_usd=Decimal("1"),
        )
        signal_id = await database.record_signal(signal)
        buy = await database.paper_execute(
            signal_id=signal_id,
            token_mint="mint",
            side=Side.BUY,
            market_price_usd=Decimal("1"),
            size_usd=Decimal("100"),
            fee_bps=50,
            slippage_bps=100,
        )
        assert buy is not None

        sell = await database.paper_execute(
            signal_id=signal_id,
            token_mint="mint",
            side=Side.SELL,
            market_price_usd=Decimal("1"),
            size_usd=Decimal("100"),
            fee_bps=50,
            slippage_bps=100,
        )
        assert sell is not None
        assert sell["realized_pnl"] < 0

        assert await database.paper_trade_count() == 2
        newest = await database.paper_trades_page(limit=1, offset=0)
        oldest = await database.paper_trades_page(limit=1, offset=1)
        assert newest[0]["side"] == Side.SELL.value
        assert oldest[0]["side"] == Side.BUY.value

        summary = await database.paper_summary({})
        assert summary.starting_cash_usd == Decimal("1000.0")
        assert summary.equity_usd < Decimal("1000")
        assert summary.realized_pnl_usd < 0
        assert summary.trades == 1
        assert summary.losses == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_raw_paper_mirror_tracks_partial_source_sells(tmp_path) -> None:
    database = Database(str(tmp_path / "mirror.db"), Decimal("1000"))
    await database.connect()
    try:
        buy = await database.paper_mirror_execute(
            trader_address="wallet-a",
            source_signature="buy-a",
            token_mint="mint",
            side=Side.BUY,
            source_token_amount=Decimal("100"),
            market_price_usd=Decimal("1"),
            size_usd=Decimal("100"),
            fee_bps=50,
            slippage_bps=100,
        )
        assert buy is not None
        duplicate = await database.paper_mirror_execute(
            trader_address="wallet-a",
            source_signature="buy-a",
            token_mint="mint",
            side=Side.BUY,
            source_token_amount=Decimal("100"),
            market_price_usd=Decimal("1"),
            size_usd=Decimal("100"),
            fee_bps=50,
            slippage_bps=100,
        )
        assert duplicate is None
        assert await database.has_paper_mirror_execution("buy-a") is True

        partial_sell = await database.paper_mirror_execute(
            trader_address="wallet-a",
            source_signature="sell-a-25",
            token_mint="mint",
            side=Side.SELL,
            source_token_amount=Decimal("25"),
            market_price_usd=Decimal("2"),
            size_usd=Decimal("100"),
            fee_bps=50,
            slippage_bps=100,
        )
        assert partial_sell is not None
        assert partial_sell["source_fraction"] == Decimal("0.25")
        positions = await database.paper_mirror_positions()
        assert len(positions) == 1
        assert Decimal(str(positions[0]["source_quantity"])) == Decimal("75.0")
        assert Decimal(str(positions[0]["cost_basis_usd"])) == Decimal("75.0")

        final_sell = await database.paper_mirror_execute(
            trader_address="wallet-a",
            source_signature="sell-a-rest",
            token_mint="mint",
            side=Side.SELL,
            source_token_amount=Decimal("75"),
            market_price_usd=Decimal("2"),
            size_usd=Decimal("100"),
            fee_bps=50,
            slippage_bps=100,
        )
        assert final_sell is not None
        assert await database.paper_mirror_positions() == []
        summary = await database.paper_summary({})
        assert summary.realized_pnl_usd > 0
        assert summary.trades == 2
        assert summary.wins == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_raw_paper_mirror_keeps_wallet_lots_separate(tmp_path) -> None:
    database = Database(str(tmp_path / "separate.db"), Decimal("1000"))
    await database.connect()
    try:
        for wallet in ("wallet-a", "wallet-b"):
            fill = await database.paper_mirror_execute(
                trader_address=wallet,
                source_signature=f"buy-{wallet}",
                token_mint="same-mint",
                side=Side.BUY,
                source_token_amount=Decimal("100"),
                market_price_usd=Decimal("1"),
                size_usd=Decimal("10"),
                fee_bps=0,
                slippage_bps=0,
            )
            assert fill is not None

        sold = await database.paper_mirror_execute(
            trader_address="wallet-a",
            source_signature="sell-wallet-a",
            token_mint="same-mint",
            side=Side.SELL,
            source_token_amount=Decimal("100"),
            market_price_usd=Decimal("1.2"),
            size_usd=Decimal("10"),
            fee_bps=0,
            slippage_bps=0,
        )
        assert sold is not None
        positions = await database.paper_mirror_positions()
        assert len(positions) == 1
        assert positions[0]["trader_address"] == "wallet-b"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_raw_paper_mirror_caps_one_wallet_token_lot(tmp_path) -> None:
    database = Database(str(tmp_path / "cap.db"), Decimal("1000"))
    await database.connect()
    try:
        fills = []
        for index in range(4):
            fills.append(
                await database.paper_mirror_execute(
                    trader_address="wallet-a",
                    source_signature=f"buy-{index}",
                    token_mint="mint",
                    side=Side.BUY,
                    source_token_amount=Decimal("100"),
                    market_price_usd=Decimal("1"),
                    size_usd=Decimal("10"),
                    fee_bps=0,
                    slippage_bps=0,
                    max_position_usd=Decimal("25"),
                )
            )

        assert fills[0] is not None
        assert fills[1] is not None
        assert fills[2] is not None
        assert fills[2]["gross"] == Decimal("5")
        assert fills[3] is None
        positions = await database.paper_mirror_positions()
        assert Decimal(str(positions[0]["cost_basis_usd"])) == Decimal("25.0")
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_paper_summary_reports_expectancy_and_profit_factor(tmp_path) -> None:
    database = Database(str(tmp_path / "metrics.db"), Decimal("1000"))
    await database.connect()
    try:
        for mint, exit_price in (("winner", "1.2"), ("loser", "0.9")):
            signal = Signal(
                token_mint=mint,
                side=Side.BUY,
                created_at=int(time.time()),
                trader_addresses=("a", "b"),
                trader_aliases=("A", "B"),
                source_signatures=(f"{mint}-a", f"{mint}-b"),
                combined_score=Decimal("75"),
                reference_price_usd=Decimal("1"),
            )
            signal_id = await database.record_signal(signal)
            await database.paper_execute(
                signal_id=signal_id,
                token_mint=mint,
                side=Side.BUY,
                market_price_usd=Decimal("1"),
                size_usd=Decimal("100"),
                fee_bps=0,
                slippage_bps=0,
            )
            await database.paper_execute(
                signal_id=signal_id,
                token_mint=mint,
                side=Side.SELL,
                market_price_usd=Decimal(exit_price),
                size_usd=Decimal("100"),
                fee_bps=0,
                slippage_bps=0,
            )

        summary = await database.paper_summary({})
        assert float(summary.gross_profit_usd) == pytest.approx(20)
        assert float(summary.gross_loss_usd) == pytest.approx(10)
        assert summary.profit_factor is not None
        assert float(summary.profit_factor) == pytest.approx(2)
        assert float(summary.average_win_usd) == pytest.approx(20)
        assert float(summary.average_loss_usd) == pytest.approx(10)
        assert float(summary.expectancy_usd) == pytest.approx(5)
        assert float(summary.realized_pnl_24h_usd) == pytest.approx(10)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_quote_based_round_trip_and_readiness(tmp_path) -> None:
    database = Database(str(tmp_path / "quoted.db"), Decimal("1000"))
    await database.connect()
    try:
        await database.record_paper_quote_attempt(
            source_signature="quote-buy",
            token_mint="mint",
            side=Side.BUY,
            quote_success=True,
            accepted=True,
            reason=None,
            latency_ms=100,
            price_impact_percent=Decimal("0.2"),
            price_drift_percent=Decimal("1"),
        )
        buy = await database.paper_mirror_execute(
            trader_address="wallet-a",
            source_signature="quote-buy",
            token_mint="mint",
            side=Side.BUY,
            source_token_amount=Decimal("100"),
            market_price_usd=Decimal("1"),
            size_usd=Decimal("10"),
            fee_bps=0,
            slippage_bps=0,
            quoted_input_amount=Decimal("10"),
            quoted_output_amount=Decimal("10"),
            token_decimals=6,
            source_price_usd=Decimal("1"),
            quote_price_usd=Decimal("1"),
            price_drift_percent=Decimal("0"),
            price_impact_percent=Decimal("0.2"),
            quote_router="metis",
            quote_latency_ms=100,
            quote_fee_bps=10,
        )
        assert buy is not None
        await database.record_paper_quote_attempt(
            source_signature="quote-sell",
            token_mint="mint",
            side=Side.SELL,
            quote_success=True,
            accepted=True,
            reason=None,
            latency_ms=100,
            price_impact_percent=Decimal("0.1"),
        )
        sell = await database.paper_mirror_execute(
            trader_address="wallet-a",
            source_signature="quote-sell",
            token_mint="mint",
            side=Side.SELL,
            source_token_amount=Decimal("100"),
            market_price_usd=Decimal("1.2"),
            size_usd=Decimal("10"),
            fee_bps=0,
            slippage_bps=0,
            quoted_input_amount=Decimal("10"),
            quoted_output_amount=Decimal("12"),
            token_decimals=6,
            quote_price_usd=Decimal("1.2"),
            price_impact_percent=Decimal("0.1"),
            quote_router="metis",
            quote_latency_ms=100,
            quote_fee_bps=10,
        )
        assert sell is not None
        assert sell["realized_pnl"] == Decimal("2")
        await database.paper_summary({})

        report = await database.paper_readiness(
            min_active_days=1,
            min_closed_trades=1,
            min_profit_factor=Decimal("1"),
            max_drawdown_percent=Decimal("10"),
            min_quote_success_percent=Decimal("95"),
        )
        assert report.ready is True
        assert report.closed_trades == 1
        assert report.quote_success_percent == Decimal("100")
        assert report.expectancy_usd == Decimal("2.0")
        trades = await database.paper_recent_trades()
        assert trades[0]["quote_based"] == 1
        assert trades[0]["quote_router"] == "metis"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_verified_candidate_pool_survives_reconnect(tmp_path) -> None:
    path = tmp_path / "candidate-cache.db"
    candidate = replace(
        _discovery_candidate("wallet-cache", rank=1, pnl="1234.56"),
        previous_pnl_24h=Decimal("1000.25"),
        metrics_limited_24h=True,
    )
    database = Database(str(path), Decimal("1000"))
    await database.connect()
    await database.cache_discovery_candidates([candidate])
    await database.close()

    reopened = Database(str(path), Decimal("1000"))
    await reopened.connect()
    try:
        loaded = await reopened.load_discovery_candidates()
        assert loaded == [candidate]
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_pump_launch_reservation_blocks_duplicate_and_counts_daily_limit(tmp_path) -> None:
    database = Database(str(tmp_path / "pump-launch.db"), Decimal("1000"))
    await database.connect()
    try:
        first = await database.reserve_pump_launch(
            alert_key="alert-1",
            source_url="https://example.test/news",
            headline="Unexpected Sprite event",
            name="Sprite Event",
            symbol="SE",
            score=88,
            initial_buy_sol=Decimal("0.01"),
            requested_by="123",
        )
        duplicate = await database.reserve_pump_launch(
            alert_key="alert-1",
            source_url="https://example.test/news",
            headline="Unexpected Sprite event",
            name="Sprite Event",
            symbol="SE",
            score=88,
            initial_buy_sol=Decimal("0.01"),
            requested_by="123",
        )
        now = int(time.time())
        count, sol = await database.pump_launch_daily_usage(
            start_at=now - 60,
            end_at=now + 60,
        )

        assert first is True
        assert duplicate is False
        assert count == 1
        assert sol == Decimal("0.01")
    finally:
        await database.close()
