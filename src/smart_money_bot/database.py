from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import replace
from decimal import Decimal
from typing import Any

try:
    import aiosqlite
except ImportError:  # pragma: no cover - exercised by the minimal-runtime self-check
    from . import sqlite_compat as aiosqlite  # type: ignore[no-redef]

from .models import (
    DetectedSwap,
    DiscoveryCandidate,
    DiscoveryRefresh,
    ExecutionMode,
    PaperSummary,
    Side,
    Signal,
    TrackedTrader,
    TraderMetrics,
)


def _d(value: Any) -> Decimal:
    return Decimal(str(value or 0))


class Database:
    def __init__(self, path: str, paper_starting_usd: Decimal) -> None:
        self.path = path
        self.paper_starting_usd = paper_starting_usd
        self.connection: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA journal_mode=WAL")
        await self.connection.execute("PRAGMA foreign_keys=ON")
        await self.connection.execute("PRAGMA busy_timeout=5000")
        await self._init_schema()

    @property
    def db(self) -> aiosqlite.Connection:
        if self.connection is None:
            raise RuntimeError("Database is not connected")
        return self.connection

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    async def _init_schema(self) -> None:
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tracked_traders (
                address TEXT PRIMARY KEY,
                alias TEXT NOT NULL UNIQUE COLLATE NOCASE,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_signature TEXT,
                weight REAL NOT NULL DEFAULT 1,
                source TEXT NOT NULL DEFAULT 'manual',
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS discovery_wallets (
                address TEXT PRIMARY KEY,
                alias TEXT NOT NULL,
                realized_pnl_24h REAL NOT NULL,
                previous_pnl_24h REAL,
                roi_24h_percent REAL NOT NULL,
                win_rate_percent REAL NOT NULL,
                trades_24h INTEGER NOT NULL,
                buys_24h INTEGER NOT NULL,
                sells_24h INTEGER NOT NULL,
                closed_tokens INTEGER NOT NULL,
                invested_24h_usd REAL NOT NULL,
                volume_24h_usd REAL NOT NULL,
                last_trade_ms INTEGER,
                score REAL NOT NULL,
                rank INTEGER NOT NULL,
                qualified INTEGER NOT NULL DEFAULT 1,
                first_seen_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_discovery_qualified_rank
                ON discovery_wallets(qualified, rank);

            CREATE TABLE IF NOT EXISTS processed_signatures (
                signature TEXT PRIMARY KEY,
                trader_address TEXT NOT NULL,
                block_time INTEGER,
                processed_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS swaps (
                signature TEXT PRIMARY KEY,
                trader_address TEXT NOT NULL,
                block_time INTEGER NOT NULL,
                side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
                token_mint TEXT NOT NULL,
                token_amount REAL NOT NULL,
                quote_mint TEXT NOT NULL,
                quote_amount REAL NOT NULL,
                usd_value REAL,
                token_price_usd REAL,
                realized_pnl_usd REAL NOT NULL DEFAULT 0,
                matched_cost_usd REAL NOT NULL DEFAULT 0,
                recorded_at INTEGER NOT NULL,
                FOREIGN KEY (trader_address) REFERENCES tracked_traders(address)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_swaps_trader_time
                ON swaps(trader_address, block_time);
            CREATE INDEX IF NOT EXISTS idx_swaps_token_time
                ON swaps(token_mint, block_time);

            CREATE TABLE IF NOT EXISTS trader_inventory (
                trader_address TEXT NOT NULL,
                token_mint TEXT NOT NULL,
                quantity REAL NOT NULL,
                cost_basis_usd REAL NOT NULL,
                PRIMARY KEY (trader_address, token_mint),
                FOREIGN KEY (trader_address) REFERENCES tracked_traders(address)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_mint TEXT NOT NULL,
                side TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                traders_json TEXT NOT NULL,
                signatures_json TEXT NOT NULL,
                combined_score REAL NOT NULL,
                reference_price_usd REAL
            );
            CREATE INDEX IF NOT EXISTS idx_signals_token_side_time
                ON signals(token_mint, side, created_at);

            CREATE TABLE IF NOT EXISTS paper_account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                starting_cash_usd REAL NOT NULL,
                cash_usd REAL NOT NULL,
                realized_pnl_usd REAL NOT NULL DEFAULT 0,
                high_watermark_usd REAL NOT NULL,
                max_drawdown_usd REAL NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_positions (
                token_mint TEXT PRIMARY KEY,
                quantity REAL NOT NULL,
                cost_basis_usd REAL NOT NULL,
                average_entry_usd REAL NOT NULL,
                opened_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_mirror_positions (
                trader_address TEXT NOT NULL,
                token_mint TEXT NOT NULL,
                source_quantity REAL NOT NULL,
                paper_quantity REAL NOT NULL,
                cost_basis_usd REAL NOT NULL,
                average_entry_usd REAL NOT NULL,
                opened_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (trader_address, token_mint)
            );
            CREATE INDEX IF NOT EXISTS idx_paper_mirror_token
                ON paper_mirror_positions(token_mint);

            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                token_mint TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity REAL NOT NULL,
                execution_price_usd REAL NOT NULL,
                gross_value_usd REAL NOT NULL,
                fee_usd REAL NOT NULL,
                realized_pnl_usd REAL NOT NULL DEFAULT 0,
                source_trader TEXT,
                source_signature TEXT,
                execution_kind TEXT NOT NULL DEFAULT 'CONSENSUS',
                created_at INTEGER NOT NULL,
                FOREIGN KEY (signal_id) REFERENCES signals(id)
            );

            CREATE TABLE IF NOT EXISTS live_positions (
                token_mint TEXT PRIMARY KEY,
                quantity_raw TEXT NOT NULL,
                decimals INTEGER NOT NULL,
                cost_basis_usd REAL NOT NULL,
                opened_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS execution_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                mode TEXT NOT NULL,
                token_mint TEXT NOT NULL,
                side TEXT NOT NULL,
                size_usd REAL NOT NULL,
                success INTEGER NOT NULL,
                signature TEXT,
                message TEXT,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (signal_id) REFERENCES signals(id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        await self._ensure_column(
            "tracked_traders", "source", "TEXT NOT NULL DEFAULT 'manual'"
        )
        await self._ensure_column("paper_trades", "source_trader", "TEXT")
        await self._ensure_column("paper_trades", "source_signature", "TEXT")
        await self._ensure_column(
            "paper_trades", "execution_kind", "TEXT NOT NULL DEFAULT 'CONSENSUS'"
        )
        await self.db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_trades_source_signature
            ON paper_trades(source_signature) WHERE source_signature IS NOT NULL
            """
        )
        now = int(time.time())
        await self.db.execute(
            """
            INSERT OR IGNORE INTO paper_account(
                id, starting_cash_usd, cash_usd, high_watermark_usd, updated_at
            ) VALUES (1, ?, ?, ?, ?)
            """,
            (
                float(self.paper_starting_usd),
                float(self.paper_starting_usd),
                float(self.paper_starting_usd),
                now,
            ),
        )
        await self.db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES ('mode', ?)",
            (ExecutionMode.PAPER.value,),
        )
        await self.db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES ('paused', 'false')"
        )
        await self.db.commit()

    async def _ensure_column(self, table: str, column: str, definition: str) -> None:
        cursor = await self.db.execute(f"PRAGMA table_info({table})")
        columns = {row["name"] for row in await cursor.fetchall()}
        if column not in columns:
            await self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def add_trader(
        self,
        address: str,
        alias: str,
        weight: Decimal = Decimal("1"),
        *,
        source: str = "manual",
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO tracked_traders(address, alias, enabled, weight, source, created_at)
            VALUES (?, ?, 1, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
                alias = excluded.alias,
                enabled = 1,
                weight = excluded.weight,
                source = excluded.source
            """,
            (address, alias, float(weight), source, int(time.time())),
        )
        await self.db.commit()

    async def remove_trader(self, address_or_alias: str) -> bool:
        cursor = await self.db.execute(
            "DELETE FROM tracked_traders WHERE address = ? OR alias = ? COLLATE NOCASE",
            (address_or_alias, address_or_alias),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def list_traders(self, *, enabled_only: bool = False) -> list[TrackedTrader]:
        query = "SELECT * FROM tracked_traders"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY alias COLLATE NOCASE"
        cursor = await self.db.execute(query)
        rows = await cursor.fetchall()
        return [
            TrackedTrader(
                address=row["address"],
                alias=row["alias"],
                enabled=bool(row["enabled"]),
                last_signature=row["last_signature"],
                weight=_d(row["weight"]),
                source=row["source"],
            )
            for row in rows
        ]

    async def resolve_trader(self, address_or_alias: str) -> TrackedTrader | None:
        cursor = await self.db.execute(
            "SELECT * FROM tracked_traders WHERE address = ? OR alias = ? COLLATE NOCASE",
            (address_or_alias, address_or_alias),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return TrackedTrader(
            address=row["address"],
            alias=row["alias"],
            enabled=bool(row["enabled"]),
            last_signature=row["last_signature"],
            weight=_d(row["weight"]),
            source=row["source"],
        )

    async def update_last_signature(self, address: str, signature: str) -> None:
        await self.db.execute(
            "UPDATE tracked_traders SET last_signature = ? WHERE address = ?",
            (signature, address),
        )
        await self.db.commit()

    async def apply_discovery(
        self, candidates: list[DiscoveryCandidate]
    ) -> DiscoveryRefresh:
        """Persist a fresh leaderboard and rotate only API-managed wallets."""

        refreshed_at = int(time.time())
        if not candidates:
            return DiscoveryRefresh((), (), (), refreshed_at)

        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                enabled_cursor = await self.db.execute(
                    "SELECT address FROM tracked_traders WHERE enabled = 1"
                )
                previously_enabled = {
                    row["address"] for row in await enabled_cursor.fetchall()
                }
                auto_cursor = await self.db.execute(
                    "SELECT address FROM tracked_traders WHERE source = 'auto' AND enabled = 1"
                )
                previously_auto = {row["address"] for row in await auto_cursor.fetchall()}

                await self.db.execute("UPDATE discovery_wallets SET qualified = 0")
                hydrated: list[DiscoveryCandidate] = []
                selected_addresses: list[str] = []
                for candidate in candidates:
                    previous_cursor = await self.db.execute(
                        "SELECT realized_pnl_24h FROM discovery_wallets WHERE address = ?",
                        (candidate.address,),
                    )
                    previous_row = await previous_cursor.fetchone()
                    previous_pnl = (
                        _d(previous_row["realized_pnl_24h"]) if previous_row else None
                    )
                    hydrated_candidate = replace(
                        candidate, previous_pnl_24h=previous_pnl
                    )
                    hydrated.append(hydrated_candidate)
                    selected_addresses.append(candidate.address)

                    await self.db.execute(
                        """
                        INSERT INTO discovery_wallets(
                            address, alias, realized_pnl_24h, previous_pnl_24h,
                            roi_24h_percent, win_rate_percent, trades_24h,
                            buys_24h, sells_24h, closed_tokens, invested_24h_usd,
                            volume_24h_usd, last_trade_ms, score, rank, qualified,
                            first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                        ON CONFLICT(address) DO UPDATE SET
                            alias = excluded.alias,
                            previous_pnl_24h = discovery_wallets.realized_pnl_24h,
                            realized_pnl_24h = excluded.realized_pnl_24h,
                            roi_24h_percent = excluded.roi_24h_percent,
                            win_rate_percent = excluded.win_rate_percent,
                            trades_24h = excluded.trades_24h,
                            buys_24h = excluded.buys_24h,
                            sells_24h = excluded.sells_24h,
                            closed_tokens = excluded.closed_tokens,
                            invested_24h_usd = excluded.invested_24h_usd,
                            volume_24h_usd = excluded.volume_24h_usd,
                            last_trade_ms = excluded.last_trade_ms,
                            score = excluded.score,
                            rank = excluded.rank,
                            qualified = 1,
                            last_seen_at = excluded.last_seen_at
                        """,
                        (
                            candidate.address,
                            candidate.alias,
                            float(candidate.realized_pnl_24h),
                            float(previous_pnl) if previous_pnl is not None else None,
                            float(candidate.roi_24h_percent),
                            float(candidate.win_rate_percent),
                            candidate.trades_24h,
                            candidate.buys_24h,
                            candidate.sells_24h,
                            candidate.closed_tokens,
                            float(candidate.invested_24h_usd),
                            float(candidate.volume_24h_usd),
                            candidate.last_trade_ms,
                            float(candidate.score),
                            candidate.rank,
                            refreshed_at,
                            refreshed_at,
                        ),
                    )

                    auto_alias = f"Auto {candidate.address}"
                    await self.db.execute(
                        """
                        INSERT INTO tracked_traders(
                            address, alias, enabled, weight, source, created_at
                        ) VALUES (?, ?, 1, 1, 'auto', ?)
                        ON CONFLICT(address) DO UPDATE SET
                            alias = CASE
                                WHEN tracked_traders.source = 'manual'
                                THEN tracked_traders.alias ELSE excluded.alias END,
                            enabled = 1,
                            weight = CASE
                                WHEN tracked_traders.source = 'manual'
                                THEN tracked_traders.weight ELSE 1 END,
                            source = CASE
                                WHEN tracked_traders.source = 'manual'
                                THEN 'manual' ELSE 'auto' END
                        """,
                        (candidate.address, auto_alias, refreshed_at),
                    )

                placeholders = ",".join("?" for _ in selected_addresses)
                await self.db.execute(
                    f"""
                    UPDATE tracked_traders SET enabled = 0
                    WHERE source = 'auto' AND address NOT IN ({placeholders})
                    """,
                    tuple(selected_addresses),
                )
                await self.db.execute(
                    """
                    INSERT INTO settings(key, value) VALUES ('discovery_last_refresh', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(refreshed_at),),
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise

        selected = set(selected_addresses)
        added = tuple(sorted(selected - previously_enabled))
        disabled = tuple(sorted(previously_auto - selected))
        return DiscoveryRefresh(tuple(hydrated), added, disabled, refreshed_at)

    async def list_discovered(self, *, limit: int = 25) -> list[DiscoveryCandidate]:
        cursor = await self.db.execute(
            """
            SELECT * FROM discovery_wallets
            WHERE qualified = 1 ORDER BY rank LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            DiscoveryCandidate(
                address=row["address"],
                alias=row["alias"],
                realized_pnl_24h=_d(row["realized_pnl_24h"]),
                previous_pnl_24h=(
                    _d(row["previous_pnl_24h"])
                    if row["previous_pnl_24h"] is not None
                    else None
                ),
                roi_24h_percent=_d(row["roi_24h_percent"]),
                win_rate_percent=_d(row["win_rate_percent"]),
                trades_24h=int(row["trades_24h"]),
                buys_24h=int(row["buys_24h"]),
                sells_24h=int(row["sells_24h"]),
                closed_tokens=int(row["closed_tokens"]),
                invested_24h_usd=_d(row["invested_24h_usd"]),
                volume_24h_usd=_d(row["volume_24h_usd"]),
                last_trade_ms=(
                    int(row["last_trade_ms"])
                    if row["last_trade_ms"] is not None
                    else None
                ),
                score=_d(row["score"]),
                rank=int(row["rank"]),
            )
            for row in rows
        ]

    async def is_processed(self, signature: str) -> bool:
        cursor = await self.db.execute(
            "SELECT 1 FROM processed_signatures WHERE signature = ?", (signature,)
        )
        return await cursor.fetchone() is not None

    async def mark_processed(
        self, signature: str, trader_address: str, block_time: int | None
    ) -> None:
        await self.db.execute(
            """
            INSERT OR IGNORE INTO processed_signatures(
                signature, trader_address, block_time, processed_at
            ) VALUES (?, ?, ?, ?)
            """,
            (signature, trader_address, block_time, int(time.time())),
        )
        await self.db.commit()

    async def record_swap(self, swap: DetectedSwap) -> bool:
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    "SELECT 1 FROM swaps WHERE signature = ?", (swap.signature,)
                )
                if await cursor.fetchone():
                    await self.db.rollback()
                    return False

                realized_pnl = Decimal("0")
                matched_cost = Decimal("0")
                if swap.usd_value is not None:
                    if swap.side is Side.BUY:
                        await self._inventory_buy(swap)
                    else:
                        realized_pnl, matched_cost = await self._inventory_sell(swap)

                await self.db.execute(
                    """
                    INSERT INTO swaps(
                        signature, trader_address, block_time, side, token_mint,
                        token_amount, quote_mint, quote_amount, usd_value,
                        token_price_usd, realized_pnl_usd, matched_cost_usd, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        swap.signature,
                        swap.trader_address,
                        swap.block_time,
                        swap.side.value,
                        swap.token_mint,
                        float(swap.token_amount),
                        swap.quote_mint,
                        float(swap.quote_amount),
                        float(swap.usd_value) if swap.usd_value is not None else None,
                        float(swap.token_price_usd)
                        if swap.token_price_usd is not None
                        else None,
                        float(realized_pnl),
                        float(matched_cost),
                        int(time.time()),
                    ),
                )
                await self.db.commit()
                return True
            except Exception:
                await self.db.rollback()
                raise

    async def _inventory_buy(self, swap: DetectedSwap) -> None:
        assert swap.usd_value is not None
        cursor = await self.db.execute(
            """
            SELECT quantity, cost_basis_usd FROM trader_inventory
            WHERE trader_address = ? AND token_mint = ?
            """,
            (swap.trader_address, swap.token_mint),
        )
        row = await cursor.fetchone()
        quantity = _d(row["quantity"]) if row else Decimal("0")
        cost = _d(row["cost_basis_usd"]) if row else Decimal("0")
        await self.db.execute(
            """
            INSERT INTO trader_inventory(trader_address, token_mint, quantity, cost_basis_usd)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(trader_address, token_mint) DO UPDATE SET
                quantity = excluded.quantity,
                cost_basis_usd = excluded.cost_basis_usd
            """,
            (
                swap.trader_address,
                swap.token_mint,
                float(quantity + swap.token_amount),
                float(cost + swap.usd_value),
            ),
        )

    async def _inventory_sell(self, swap: DetectedSwap) -> tuple[Decimal, Decimal]:
        assert swap.usd_value is not None
        cursor = await self.db.execute(
            """
            SELECT quantity, cost_basis_usd FROM trader_inventory
            WHERE trader_address = ? AND token_mint = ?
            """,
            (swap.trader_address, swap.token_mint),
        )
        row = await cursor.fetchone()
        if not row:
            return Decimal("0"), Decimal("0")

        quantity = _d(row["quantity"])
        cost_basis = _d(row["cost_basis_usd"])
        if quantity <= 0:
            return Decimal("0"), Decimal("0")

        matched_quantity = min(quantity, swap.token_amount)
        ratio = matched_quantity / quantity
        matched_cost = cost_basis * ratio
        proceeds_ratio = matched_quantity / swap.token_amount
        matched_proceeds = swap.usd_value * proceeds_ratio
        realized_pnl = matched_proceeds - matched_cost
        remaining_quantity = quantity - matched_quantity
        remaining_cost = cost_basis - matched_cost

        if remaining_quantity <= Decimal("0.000000001"):
            await self.db.execute(
                "DELETE FROM trader_inventory WHERE trader_address = ? AND token_mint = ?",
                (swap.trader_address, swap.token_mint),
            )
        else:
            await self.db.execute(
                """
                UPDATE trader_inventory SET quantity = ?, cost_basis_usd = ?
                WHERE trader_address = ? AND token_mint = ?
                """,
                (
                    float(remaining_quantity),
                    float(remaining_cost),
                    swap.trader_address,
                    swap.token_mint,
                ),
            )
        return realized_pnl, matched_cost

    async def metrics(self, window_seconds: int) -> list[TraderMetrics]:
        cutoff = int(time.time()) - window_seconds
        traders = await self.list_traders(enabled_only=True)
        result: list[TraderMetrics] = []
        for trader in traders:
            cursor = await self.db.execute(
                """
                SELECT
                    COUNT(*) AS trades,
                    SUM(CASE WHEN side = 'BUY' THEN 1 ELSE 0 END) AS buys,
                    SUM(CASE WHEN side = 'SELL' THEN 1 ELSE 0 END) AS sells,
                    SUM(CASE WHEN side = 'SELL' AND matched_cost_usd > 0
                             AND realized_pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN side = 'SELL' AND matched_cost_usd > 0
                             AND realized_pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses,
                    COALESCE(SUM(realized_pnl_usd), 0) AS pnl,
                    COALESCE(SUM(matched_cost_usd), 0) AS matched_cost,
                    COALESCE(SUM(usd_value), 0) AS volume
                FROM swaps
                WHERE trader_address = ? AND block_time >= ?
                """,
                (trader.address, cutoff),
            )
            row = await cursor.fetchone()
            pnl_cursor = await self.db.execute(
                """
                SELECT realized_pnl_usd FROM swaps
                WHERE trader_address = ? AND block_time >= ?
                  AND side = 'SELL' AND matched_cost_usd > 0
                ORDER BY block_time, rowid
                """,
                (trader.address, cutoff),
            )
            pnl_rows = await pnl_cursor.fetchall()
            equity = Decimal("0")
            peak = Decimal("0")
            max_drawdown = Decimal("0")
            for pnl_row in pnl_rows:
                equity += _d(pnl_row["realized_pnl_usd"])
                peak = max(peak, equity)
                max_drawdown = max(max_drawdown, peak - equity)

            result.append(
                TraderMetrics(
                    address=trader.address,
                    alias=trader.alias,
                    window_seconds=window_seconds,
                    trades=int(row["trades"] or 0),
                    buys=int(row["buys"] or 0),
                    sells=int(row["sells"] or 0),
                    wins=int(row["wins"] or 0),
                    losses=int(row["losses"] or 0),
                    realized_pnl_usd=_d(row["pnl"]),
                    matched_cost_usd=_d(row["matched_cost"]),
                    volume_usd=_d(row["volume"]),
                    max_drawdown_usd=max_drawdown,
                )
            )
        return result

    async def record_signal(self, signal: Signal) -> int:
        cursor = await self.db.execute(
            """
            INSERT INTO signals(
                token_mint, side, created_at, traders_json, signatures_json,
                combined_score, reference_price_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.token_mint,
                signal.side.value,
                signal.created_at,
                json.dumps(signal.trader_addresses),
                json.dumps(signal.source_signatures),
                float(signal.combined_score),
                float(signal.reference_price_usd)
                if signal.reference_price_usd is not None
                else None,
            ),
        )
        await self.db.commit()
        return int(cursor.lastrowid)

    async def recent_signal_exists(
        self, token_mint: str, side: Side, cutoff: int
    ) -> bool:
        cursor = await self.db.execute(
            """
            SELECT 1 FROM signals
            WHERE token_mint = ? AND side = ? AND created_at >= ? LIMIT 1
            """,
            (token_mint, side.value, cutoff),
        )
        return await cursor.fetchone() is not None

    async def paper_execute(
        self,
        *,
        signal_id: int,
        token_mint: str,
        side: Side,
        market_price_usd: Decimal,
        size_usd: Decimal,
        fee_bps: int,
        slippage_bps: int,
    ) -> dict[str, Decimal] | None:
        fee_rate = Decimal(fee_bps) / Decimal(10_000)
        slip_rate = Decimal(slippage_bps) / Decimal(10_000)
        now = int(time.time())
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                account_cursor = await self.db.execute(
                    "SELECT * FROM paper_account WHERE id = 1"
                )
                account = await account_cursor.fetchone()
                cash = _d(account["cash_usd"])
                realized_total = _d(account["realized_pnl_usd"])
                pos_cursor = await self.db.execute(
                    "SELECT * FROM paper_positions WHERE token_mint = ?", (token_mint,)
                )
                position = await pos_cursor.fetchone()

                if side is Side.BUY:
                    notional = min(size_usd, cash)
                    if notional <= Decimal("0.01"):
                        await self.db.rollback()
                        return None
                    fee = notional * fee_rate
                    effective_price = market_price_usd * (Decimal("1") + slip_rate)
                    quantity = (notional - fee) / effective_price
                    old_quantity = _d(position["quantity"]) if position else Decimal("0")
                    old_cost = _d(position["cost_basis_usd"]) if position else Decimal("0")
                    new_quantity = old_quantity + quantity
                    new_cost = old_cost + notional
                    avg_entry = new_cost / new_quantity
                    await self.db.execute(
                        """
                        INSERT INTO paper_positions(
                            token_mint, quantity, cost_basis_usd, average_entry_usd,
                            opened_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(token_mint) DO UPDATE SET
                            quantity = excluded.quantity,
                            cost_basis_usd = excluded.cost_basis_usd,
                            average_entry_usd = excluded.average_entry_usd,
                            updated_at = excluded.updated_at
                        """,
                        (
                            token_mint,
                            float(new_quantity),
                            float(new_cost),
                            float(avg_entry),
                            now,
                            now,
                        ),
                    )
                    cash -= notional
                    gross = notional
                    realized = Decimal("0")
                else:
                    if not position:
                        await self.db.rollback()
                        return None
                    quantity = _d(position["quantity"])
                    cost_basis = _d(position["cost_basis_usd"])
                    effective_price = market_price_usd * (Decimal("1") - slip_rate)
                    gross = quantity * effective_price
                    fee = gross * fee_rate
                    net = gross - fee
                    realized = net - cost_basis
                    cash += net
                    realized_total += realized
                    await self.db.execute(
                        "DELETE FROM paper_positions WHERE token_mint = ?", (token_mint,)
                    )

                await self.db.execute(
                    """
                    UPDATE paper_account
                    SET cash_usd = ?, realized_pnl_usd = ?, updated_at = ?
                    WHERE id = 1
                    """,
                    (float(cash), float(realized_total), now),
                )
                await self.db.execute(
                    """
                    INSERT INTO paper_trades(
                        signal_id, token_mint, side, quantity, execution_price_usd,
                        gross_value_usd, fee_usd, realized_pnl_usd, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signal_id,
                        token_mint,
                        side.value,
                        float(quantity),
                        float(effective_price),
                        float(gross),
                        float(fee),
                        float(realized),
                        now,
                    ),
                )
                await self.db.commit()
                return {
                    "quantity": quantity,
                    "price": effective_price,
                    "gross": gross,
                    "fee": fee,
                    "realized_pnl": realized,
                }
            except Exception:
                await self.db.rollback()
                raise

    async def paper_mirror_execute(
        self,
        *,
        trader_address: str,
        source_signature: str,
        token_mint: str,
        side: Side,
        source_token_amount: Decimal,
        market_price_usd: Decimal,
        size_usd: Decimal,
        fee_bps: int,
        slippage_bps: int,
    ) -> dict[str, Decimal] | None:
        """Mirror one source wallet while keeping each wallet's paper lot separate."""

        if source_token_amount <= 0 or market_price_usd <= 0:
            return None
        fee_rate = Decimal(fee_bps) / Decimal(10_000)
        slip_rate = Decimal(slippage_bps) / Decimal(10_000)
        now = int(time.time())
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                duplicate_cursor = await self.db.execute(
                    "SELECT 1 FROM paper_trades WHERE source_signature = ?",
                    (source_signature,),
                )
                if await duplicate_cursor.fetchone():
                    await self.db.rollback()
                    return None
                account_cursor = await self.db.execute(
                    "SELECT * FROM paper_account WHERE id = 1"
                )
                account = await account_cursor.fetchone()
                cash = _d(account["cash_usd"])
                realized_total = _d(account["realized_pnl_usd"])
                position_cursor = await self.db.execute(
                    """
                    SELECT * FROM paper_mirror_positions
                    WHERE trader_address = ? AND token_mint = ?
                    """,
                    (trader_address, token_mint),
                )
                position = await position_cursor.fetchone()

                if side is Side.BUY:
                    notional = min(size_usd, cash)
                    if notional <= Decimal("0.01"):
                        await self.db.rollback()
                        return None
                    fee = notional * fee_rate
                    effective_price = market_price_usd * (Decimal("1") + slip_rate)
                    paper_quantity = (notional - fee) / effective_price
                    old_source_quantity = (
                        _d(position["source_quantity"]) if position else Decimal("0")
                    )
                    old_paper_quantity = (
                        _d(position["paper_quantity"]) if position else Decimal("0")
                    )
                    old_cost = _d(position["cost_basis_usd"]) if position else Decimal("0")
                    new_source_quantity = old_source_quantity + source_token_amount
                    new_paper_quantity = old_paper_quantity + paper_quantity
                    new_cost = old_cost + notional
                    average_entry = new_cost / new_paper_quantity
                    await self.db.execute(
                        """
                        INSERT INTO paper_mirror_positions(
                            trader_address, token_mint, source_quantity, paper_quantity,
                            cost_basis_usd, average_entry_usd, opened_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(trader_address, token_mint) DO UPDATE SET
                            source_quantity = excluded.source_quantity,
                            paper_quantity = excluded.paper_quantity,
                            cost_basis_usd = excluded.cost_basis_usd,
                            average_entry_usd = excluded.average_entry_usd,
                            updated_at = excluded.updated_at
                        """,
                        (
                            trader_address,
                            token_mint,
                            float(new_source_quantity),
                            float(new_paper_quantity),
                            float(new_cost),
                            float(average_entry),
                            now,
                            now,
                        ),
                    )
                    cash -= notional
                    gross = notional
                    realized = Decimal("0")
                    source_fraction = Decimal("1")
                    remaining_quantity = new_paper_quantity
                    remaining_cost = new_cost
                else:
                    if not position:
                        await self.db.rollback()
                        return None
                    observed_source_quantity = _d(position["source_quantity"])
                    held_paper_quantity = _d(position["paper_quantity"])
                    held_cost = _d(position["cost_basis_usd"])
                    if observed_source_quantity <= 0 or held_paper_quantity <= 0:
                        await self.db.rollback()
                        return None
                    source_fraction = min(
                        Decimal("1"), source_token_amount / observed_source_quantity
                    )
                    paper_quantity = held_paper_quantity * source_fraction
                    matched_cost = held_cost * source_fraction
                    effective_price = market_price_usd * (Decimal("1") - slip_rate)
                    gross = paper_quantity * effective_price
                    fee = gross * fee_rate
                    net = gross - fee
                    realized = net - matched_cost
                    cash += net
                    realized_total += realized
                    remaining_source_quantity = (
                        observed_source_quantity
                        - min(source_token_amount, observed_source_quantity)
                    )
                    remaining_quantity = held_paper_quantity - paper_quantity
                    remaining_cost = held_cost - matched_cost
                    if (
                        remaining_source_quantity <= Decimal("0.000000001")
                        or remaining_quantity <= Decimal("0.000000001")
                    ):
                        await self.db.execute(
                            """
                            DELETE FROM paper_mirror_positions
                            WHERE trader_address = ? AND token_mint = ?
                            """,
                            (trader_address, token_mint),
                        )
                        remaining_quantity = Decimal("0")
                        remaining_cost = Decimal("0")
                    else:
                        average_entry = remaining_cost / remaining_quantity
                        await self.db.execute(
                            """
                            UPDATE paper_mirror_positions SET
                                source_quantity = ?, paper_quantity = ?,
                                cost_basis_usd = ?, average_entry_usd = ?, updated_at = ?
                            WHERE trader_address = ? AND token_mint = ?
                            """,
                            (
                                float(remaining_source_quantity),
                                float(remaining_quantity),
                                float(remaining_cost),
                                float(average_entry),
                                now,
                                trader_address,
                                token_mint,
                            ),
                        )

                await self.db.execute(
                    """
                    UPDATE paper_account
                    SET cash_usd = ?, realized_pnl_usd = ?, updated_at = ?
                    WHERE id = 1
                    """,
                    (float(cash), float(realized_total), now),
                )
                await self.db.execute(
                    """
                    INSERT INTO paper_trades(
                        signal_id, token_mint, side, quantity, execution_price_usd,
                        gross_value_usd, fee_usd, realized_pnl_usd, source_trader,
                        source_signature, execution_kind, created_at
                    ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RAW_MIRROR', ?)
                    """,
                    (
                        token_mint,
                        side.value,
                        float(paper_quantity),
                        float(effective_price),
                        float(gross),
                        float(fee),
                        float(realized),
                        trader_address,
                        source_signature,
                        now,
                    ),
                )
                await self.db.commit()
                return {
                    "quantity": paper_quantity,
                    "price": effective_price,
                    "gross": gross,
                    "fee": fee,
                    "realized_pnl": realized,
                    "source_fraction": source_fraction,
                    "remaining_quantity": remaining_quantity,
                    "remaining_cost_basis": remaining_cost,
                }
            except Exception:
                await self.db.rollback()
                raise

    async def has_paper_mirror_execution(self, source_signature: str) -> bool:
        cursor = await self.db.execute(
            "SELECT 1 FROM paper_trades WHERE source_signature = ?",
            (source_signature,),
        )
        return await cursor.fetchone() is not None

    async def paper_positions(self) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM paper_positions ORDER BY opened_at"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def paper_mirror_positions(self) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM paper_mirror_positions ORDER BY opened_at"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def paper_all_positions(self) -> list[dict[str, Any]]:
        standard = await self.paper_positions()
        mirror = await self.paper_mirror_positions()
        combined = [
            {
                **item,
                "position_kind": "STRATEGY",
                "source_trader": None,
            }
            for item in standard
        ]
        combined.extend(
            {
                "token_mint": item["token_mint"],
                "quantity": item["paper_quantity"],
                "cost_basis_usd": item["cost_basis_usd"],
                "average_entry_usd": item["average_entry_usd"],
                "opened_at": item["opened_at"],
                "updated_at": item["updated_at"],
                "position_kind": "RAW_MIRROR",
                "source_trader": item["trader_address"],
                "source_quantity": item["source_quantity"],
            }
            for item in mirror
        )
        return sorted(combined, key=lambda item: int(item["opened_at"]))

    async def paper_position_count(self) -> int:
        standard_cursor = await self.db.execute(
            "SELECT COUNT(*) AS count FROM paper_positions"
        )
        mirror_cursor = await self.db.execute(
            "SELECT COUNT(*) AS count FROM paper_mirror_positions"
        )
        standard = await standard_cursor.fetchone()
        mirror = await mirror_cursor.fetchone()
        return int(standard["count"]) + int(mirror["count"])

    async def paper_daily_realized_pnl(self) -> Decimal:
        cursor = await self.db.execute(
            """
            SELECT COALESCE(SUM(realized_pnl_usd), 0) AS pnl
            FROM paper_trades WHERE created_at >= ?
            """,
            (int(time.time()) - 86_400,),
        )
        row = await cursor.fetchone()
        return _d(row["pnl"])

    async def paper_summary(self, prices: dict[str, Decimal]) -> PaperSummary:
        account_cursor = await self.db.execute("SELECT * FROM paper_account WHERE id = 1")
        account = await account_cursor.fetchone()
        positions = await self.paper_all_positions()
        positions_value = Decimal("0")
        cost_basis = Decimal("0")
        for position in positions:
            price = prices.get(position["token_mint"], _d(position["average_entry_usd"]))
            positions_value += _d(position["quantity"]) * price
            cost_basis += _d(position["cost_basis_usd"])
        cash = _d(account["cash_usd"])
        equity = cash + positions_value
        await self._update_paper_drawdown(equity)

        trades_cursor = await self.db.execute(
            """
            SELECT
                SUM(CASE WHEN side = 'SELL' THEN 1 ELSE 0 END) AS trades,
                SUM(CASE WHEN side = 'SELL' AND realized_pnl_usd > 0 THEN 1 ELSE 0 END) wins,
                SUM(CASE WHEN side = 'SELL' AND realized_pnl_usd <= 0 THEN 1 ELSE 0 END) losses
            FROM paper_trades
            """
        )
        trade_row = await trades_cursor.fetchone()
        refreshed = await self.db.execute("SELECT * FROM paper_account WHERE id = 1")
        account = await refreshed.fetchone()
        return PaperSummary(
            starting_cash_usd=_d(account["starting_cash_usd"]),
            cash_usd=cash,
            positions_value_usd=positions_value,
            equity_usd=equity,
            realized_pnl_usd=_d(account["realized_pnl_usd"]),
            unrealized_pnl_usd=positions_value - cost_basis,
            trades=int(trade_row["trades"] or 0),
            wins=int(trade_row["wins"] or 0),
            losses=int(trade_row["losses"] or 0),
            max_drawdown_usd=_d(account["max_drawdown_usd"]),
        )

    async def _update_paper_drawdown(self, equity: Decimal) -> None:
        cursor = await self.db.execute(
            "SELECT high_watermark_usd, max_drawdown_usd FROM paper_account WHERE id = 1"
        )
        row = await cursor.fetchone()
        high = max(_d(row["high_watermark_usd"]), equity)
        drawdown = max(_d(row["max_drawdown_usd"]), high - equity)
        await self.db.execute(
            """
            UPDATE paper_account SET high_watermark_usd = ?, max_drawdown_usd = ?, updated_at = ?
            WHERE id = 1
            """,
            (float(high), float(drawdown), int(time.time())),
        )
        await self.db.commit()

    async def reset_paper(self) -> None:
        now = int(time.time())
        async with self._write_lock:
            await self.db.execute("DELETE FROM paper_positions")
            await self.db.execute("DELETE FROM paper_mirror_positions")
            await self.db.execute("DELETE FROM paper_trades")
            await self.db.execute(
                """
                UPDATE paper_account SET
                    cash_usd = starting_cash_usd,
                    realized_pnl_usd = 0,
                    high_watermark_usd = starting_cash_usd,
                    max_drawdown_usd = 0,
                    updated_at = ?
                WHERE id = 1
                """,
                (now,),
            )
            await self.db.commit()

    async def get_live_position(self, token_mint: str) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM live_positions WHERE token_mint = ?", (token_mint,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def live_positions(self) -> list[dict[str, Any]]:
        cursor = await self.db.execute("SELECT * FROM live_positions ORDER BY opened_at")
        return [dict(row) for row in await cursor.fetchall()]

    async def set_live_position(
        self,
        token_mint: str,
        *,
        quantity_raw: int,
        decimals: int,
        cost_basis_usd: Decimal,
    ) -> None:
        now = int(time.time())
        async with self._write_lock:
            cursor = await self.db.execute(
                "SELECT quantity_raw, cost_basis_usd FROM live_positions WHERE token_mint = ?",
                (token_mint,),
            )
            existing = await cursor.fetchone()
            total_quantity = quantity_raw + (int(existing["quantity_raw"]) if existing else 0)
            total_cost = cost_basis_usd + (
                _d(existing["cost_basis_usd"]) if existing else Decimal("0")
            )
            await self.db.execute(
                """
                INSERT INTO live_positions(
                    token_mint, quantity_raw, decimals, cost_basis_usd, opened_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(token_mint) DO UPDATE SET
                    quantity_raw = excluded.quantity_raw,
                    cost_basis_usd = excluded.cost_basis_usd,
                    decimals = excluded.decimals,
                    updated_at = excluded.updated_at
                """,
                (token_mint, str(total_quantity), decimals, float(total_cost), now, now),
            )
            await self.db.commit()

    async def clear_live_position(self, token_mint: str) -> None:
        await self.db.execute("DELETE FROM live_positions WHERE token_mint = ?", (token_mint,))
        await self.db.commit()

    async def live_position_count(self) -> int:
        cursor = await self.db.execute("SELECT COUNT(*) AS count FROM live_positions")
        row = await cursor.fetchone()
        return int(row["count"])

    async def log_execution(
        self,
        *,
        signal_id: int | None,
        mode: ExecutionMode,
        token_mint: str,
        side: Side,
        size_usd: Decimal,
        success: bool,
        signature: str | None,
        message: str,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO execution_log(
                signal_id, mode, token_mint, side, size_usd, success,
                signature, message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_id,
                mode.value,
                token_mint,
                side.value,
                float(size_usd),
                int(success),
                signature,
                message[:1000],
                int(time.time()),
            ),
        )
        await self.db.commit()

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        cursor = await self.db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.db.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await self.db.commit()
