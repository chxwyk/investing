from __future__ import annotations

import sqlite3
import time
from decimal import Decimal

import pytest

from smart_money_bot.database import Database
from smart_money_bot.models import Side, Signal


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
    connection.commit()
    connection.close()

    database = Database(str(path), Decimal("1000"))
    await database.connect()
    try:
        cursor = await database.db.execute("PRAGMA table_info(paper_trades)")
        columns = {row["name"] for row in await cursor.fetchall()}
        assert {"source_trader", "source_signature", "execution_kind"} <= columns
        assert await database.paper_mirror_positions() == []
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
