from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, replace
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
    PaperReadiness,
    PaperSummary,
    Side,
    Signal,
    TrackedTrader,
    TraderMetrics,
    WalletRotationEvent,
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
                realized_pnl_7d REAL NOT NULL DEFAULT 0,
                roi_7d_percent REAL NOT NULL DEFAULT 0,
                win_rate_7d_percent REAL NOT NULL DEFAULT 0,
                trades_7d INTEGER NOT NULL DEFAULT 0,
                recent_swaps INTEGER NOT NULL DEFAULT 0,
                pump_swaps INTEGER NOT NULL DEFAULT 0,
                last_activity_at INTEGER,
                selection_reason TEXT NOT NULL DEFAULT '',
                removal_reason TEXT,
                baseline_pnl_24h REAL,
                baseline_pnl_7d REAL,
                tracking_started_at INTEGER,
                qualified INTEGER NOT NULL DEFAULT 1,
                first_seen_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_discovery_qualified_rank
                ON discovery_wallets(qualified, rank);

            CREATE TABLE IF NOT EXISTS wallet_rotation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL,
                alias TEXT NOT NULL,
                action TEXT NOT NULL CHECK (action IN ('ADDED', 'REMOVED')),
                reason TEXT NOT NULL,
                score REAL NOT NULL,
                pnl_24h_usd REAL NOT NULL,
                pnl_7d_usd REAL NOT NULL,
                baseline_pnl_24h_usd REAL NOT NULL,
                baseline_pnl_7d_usd REAL NOT NULL,
                observed_source_pnl_usd REAL NOT NULL DEFAULT 0,
                paper_pnl_usd REAL NOT NULL DEFAULT 0,
                recorded_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_wallet_rotation_events_time
                ON wallet_rotation_events(recorded_at DESC);

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
                peak_price_usd REAL NOT NULL DEFAULT 0,
                token_decimals INTEGER,
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
                exit_reason TEXT,
                source_price_usd REAL,
                quote_price_usd REAL,
                price_drift_percent REAL,
                price_impact_percent REAL,
                quote_router TEXT,
                quote_latency_ms INTEGER,
                quote_fee_bps INTEGER,
                quote_based INTEGER NOT NULL DEFAULT 0,
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

            CREATE TABLE IF NOT EXISTS paper_quote_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_signature TEXT,
                token_mint TEXT NOT NULL,
                side TEXT NOT NULL,
                quote_success INTEGER NOT NULL,
                accepted INTEGER NOT NULL,
                reason TEXT,
                latency_ms INTEGER,
                price_impact_percent REAL,
                price_drift_percent REAL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_paper_quote_attempts_time
                ON paper_quote_attempts(created_at);

            CREATE TABLE IF NOT EXISTS paper_equity_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                equity_usd REAL NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_paper_equity_samples_time
                ON paper_equity_samples(created_at);

            CREATE TABLE IF NOT EXISTS pump_launches (
                alert_key TEXT PRIMARY KEY,
                source_url TEXT NOT NULL,
                headline TEXT NOT NULL,
                name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                score INTEGER NOT NULL,
                initial_buy_sol REAL NOT NULL,
                requested_by TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('RESERVED', 'SUBMITTED', 'CONFIRMED', 'FAILED')
                ),
                mint TEXT,
                signature TEXT,
                metadata_uri TEXT,
                error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pump_launches_time
                ON pump_launches(created_at DESC);

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        await self._ensure_column("tracked_traders", "source", "TEXT NOT NULL DEFAULT 'manual'")
        await self._ensure_column("discovery_wallets", "realized_pnl_7d", "REAL NOT NULL DEFAULT 0")
        await self._ensure_column("discovery_wallets", "roi_7d_percent", "REAL NOT NULL DEFAULT 0")
        await self._ensure_column(
            "discovery_wallets", "win_rate_7d_percent", "REAL NOT NULL DEFAULT 0"
        )
        await self._ensure_column("discovery_wallets", "trades_7d", "INTEGER NOT NULL DEFAULT 0")
        await self._ensure_column("discovery_wallets", "recent_swaps", "INTEGER NOT NULL DEFAULT 0")
        await self._ensure_column("discovery_wallets", "pump_swaps", "INTEGER NOT NULL DEFAULT 0")
        await self._ensure_column("discovery_wallets", "last_activity_at", "INTEGER")
        await self._ensure_column(
            "discovery_wallets", "selection_reason", "TEXT NOT NULL DEFAULT ''"
        )
        await self._ensure_column("discovery_wallets", "removal_reason", "TEXT")
        await self._ensure_column("discovery_wallets", "baseline_pnl_24h", "REAL")
        await self._ensure_column("discovery_wallets", "baseline_pnl_7d", "REAL")
        await self._ensure_column("discovery_wallets", "tracking_started_at", "INTEGER")
        await self._ensure_column("paper_trades", "source_trader", "TEXT")
        await self._ensure_column("paper_trades", "source_signature", "TEXT")
        await self._ensure_column(
            "paper_trades", "execution_kind", "TEXT NOT NULL DEFAULT 'CONSENSUS'"
        )
        await self._ensure_column("paper_trades", "exit_reason", "TEXT")
        await self._ensure_column(
            "paper_mirror_positions", "peak_price_usd", "REAL NOT NULL DEFAULT 0"
        )
        await self._ensure_column("paper_mirror_positions", "token_decimals", "INTEGER")
        await self._ensure_column("paper_trades", "source_price_usd", "REAL")
        await self._ensure_column("paper_trades", "quote_price_usd", "REAL")
        await self._ensure_column("paper_trades", "price_drift_percent", "REAL")
        await self._ensure_column("paper_trades", "price_impact_percent", "REAL")
        await self._ensure_column("paper_trades", "quote_router", "TEXT")
        await self._ensure_column("paper_trades", "quote_latency_ms", "INTEGER")
        await self._ensure_column("paper_trades", "quote_fee_bps", "INTEGER")
        await self._ensure_column("paper_trades", "quote_based", "INTEGER NOT NULL DEFAULT 0")
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
        await self.db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES ('paper_trial_started_at', ?)",
            (str(now),),
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

    async def trader_is_exit_only(self, address: str) -> bool:
        """Return whether an automatic wallet is retained only to close a paper lot."""

        cursor = await self.db.execute(
            """
            SELECT tracked_traders.source, discovery_wallets.qualified
            FROM tracked_traders
            LEFT JOIN discovery_wallets
                ON discovery_wallets.address = tracked_traders.address
            WHERE tracked_traders.address = ?
            """,
            (address,),
        )
        row = await cursor.fetchone()
        if row is None or str(row["source"]) != "auto":
            return False
        return row["qualified"] is None or not bool(row["qualified"])

    async def exit_only_trader_count(self) -> int:
        cursor = await self.db.execute(
            """
            SELECT COUNT(*) AS count
            FROM tracked_traders
            LEFT JOIN discovery_wallets
                ON discovery_wallets.address = tracked_traders.address
            WHERE tracked_traders.enabled = 1
              AND tracked_traders.source = 'auto'
              AND COALESCE(discovery_wallets.qualified, 0) = 0
              AND EXISTS (
                  SELECT 1 FROM paper_mirror_positions
                  WHERE paper_mirror_positions.trader_address = tracked_traders.address
              )
            """
        )
        row = await cursor.fetchone()
        return int(row["count"] or 0)

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
        self,
        candidates: list[DiscoveryCandidate],
        *,
        evaluated_candidates: list[DiscoveryCandidate] | None = None,
        removal_reasons: dict[str, str] | None = None,
        candidate_pool_size: int = 0,
        verified_pump_wallets: int = 0,
    ) -> DiscoveryRefresh:
        """Persist a hot set and audit every automatic admission/removal."""

        refreshed_at = int(time.time())
        if not candidates:
            return DiscoveryRefresh((), (), (), refreshed_at)

        evaluated = {item.address: item for item in (evaluated_candidates or candidates)}
        reasons = removal_reasons or {}
        removal_events: list[WalletRotationEvent] = []
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                enabled_cursor = await self.db.execute(
                    "SELECT address FROM tracked_traders WHERE enabled = 1"
                )
                previously_enabled = {row["address"] for row in await enabled_cursor.fetchall()}
                auto_cursor = await self.db.execute(
                    "SELECT address FROM tracked_traders WHERE source = 'auto' AND enabled = 1"
                )
                previously_auto = {row["address"] for row in await auto_cursor.fetchall()}

                hydrated: list[DiscoveryCandidate] = []
                selected_addresses: list[str] = []
                for candidate in candidates:
                    previous_cursor = await self.db.execute(
                        "SELECT * FROM discovery_wallets WHERE address = ?",
                        (candidate.address,),
                    )
                    previous_row = await previous_cursor.fetchone()
                    previous_pnl = _d(previous_row["realized_pnl_24h"]) if previous_row else None
                    hydrated_candidate = replace(candidate, previous_pnl_24h=previous_pnl)
                    hydrated.append(hydrated_candidate)
                    selected_addresses.append(candidate.address)
                    continuing = candidate.address in previously_auto
                    baseline_24h = (
                        _d(previous_row["baseline_pnl_24h"])
                        if continuing
                        and previous_row
                        and previous_row["baseline_pnl_24h"] is not None
                        else candidate.realized_pnl_24h
                    )
                    baseline_7d = (
                        _d(previous_row["baseline_pnl_7d"])
                        if continuing
                        and previous_row
                        and previous_row["baseline_pnl_7d"] is not None
                        else candidate.realized_pnl_7d
                    )
                    tracking_started_at = (
                        int(previous_row["tracking_started_at"])
                        if continuing
                        and previous_row
                        and previous_row["tracking_started_at"] is not None
                        else refreshed_at
                    )

                    await self.db.execute(
                        """
                        INSERT INTO discovery_wallets(
                            address, alias, realized_pnl_24h, previous_pnl_24h,
                            roi_24h_percent, win_rate_percent, trades_24h,
                            buys_24h, sells_24h, closed_tokens, invested_24h_usd,
                            volume_24h_usd, last_trade_ms, score, rank,
                            realized_pnl_7d, roi_7d_percent, win_rate_7d_percent,
                            trades_7d, recent_swaps, pump_swaps, last_activity_at,
                            selection_reason, removal_reason, baseline_pnl_24h,
                            baseline_pnl_7d, tracking_started_at, qualified,
                            first_seen_at, last_seen_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 1, ?, ?
                        )
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
                            realized_pnl_7d = excluded.realized_pnl_7d,
                            roi_7d_percent = excluded.roi_7d_percent,
                            win_rate_7d_percent = excluded.win_rate_7d_percent,
                            trades_7d = excluded.trades_7d,
                            recent_swaps = excluded.recent_swaps,
                            pump_swaps = excluded.pump_swaps,
                            last_activity_at = excluded.last_activity_at,
                            selection_reason = excluded.selection_reason,
                            removal_reason = NULL,
                            baseline_pnl_24h = excluded.baseline_pnl_24h,
                            baseline_pnl_7d = excluded.baseline_pnl_7d,
                            tracking_started_at = excluded.tracking_started_at,
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
                            float(candidate.realized_pnl_7d),
                            float(candidate.roi_7d_percent),
                            float(candidate.win_rate_7d_percent),
                            candidate.trades_7d,
                            candidate.recent_swaps,
                            candidate.pump_swaps,
                            candidate.last_activity_at,
                            candidate.selection_reason,
                            float(baseline_24h),
                            float(baseline_7d),
                            tracking_started_at,
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

                    if candidate.address not in previously_enabled:
                        await self._insert_rotation_event(
                            address=candidate.address,
                            alias=candidate.alias,
                            action="ADDED",
                            reason=candidate.selection_reason or "qualified for the hot set",
                            score=candidate.score,
                            pnl_24h=candidate.realized_pnl_24h,
                            pnl_7d=candidate.realized_pnl_7d,
                            baseline_24h=baseline_24h,
                            baseline_7d=baseline_7d,
                            tracking_started_at=tracking_started_at,
                            recorded_at=refreshed_at,
                        )

                placeholders = ",".join("?" for _ in selected_addresses)
                disabled_addresses = sorted(previously_auto - set(selected_addresses))
                for address in disabled_addresses:
                    row_cursor = await self.db.execute(
                        "SELECT * FROM discovery_wallets WHERE address = ?", (address,)
                    )
                    row = await row_cursor.fetchone()
                    if row is None:
                        continue
                    current = evaluated.get(address)
                    pnl_24h = (
                        current.realized_pnl_24h
                        if current is not None
                        else _d(row["realized_pnl_24h"])
                    )
                    pnl_7d = (
                        current.realized_pnl_7d
                        if current is not None
                        else _d(row["realized_pnl_7d"])
                    )
                    score = current.score if current is not None else _d(row["score"])
                    baseline_24h = _d(row["baseline_pnl_24h"])
                    baseline_7d = _d(row["baseline_pnl_7d"])
                    tracking_started_at = int(row["tracking_started_at"] or row["first_seen_at"])
                    reason = reasons.get(
                        address, "rotated out by a higher-ranked active Pump wallet"
                    )
                    await self.db.execute(
                        """
                        UPDATE discovery_wallets SET
                            realized_pnl_24h = ?, realized_pnl_7d = ?, score = ?,
                            recent_swaps = ?, pump_swaps = ?, last_activity_at = ?,
                            qualified = 0, removal_reason = ?, last_seen_at = ?
                        WHERE address = ?
                        """,
                        (
                            float(pnl_24h),
                            float(pnl_7d),
                            float(score),
                            current.recent_swaps if current else int(row["recent_swaps"]),
                            current.pump_swaps if current else int(row["pump_swaps"]),
                            current.last_activity_at if current else row["last_activity_at"],
                            reason,
                            refreshed_at,
                            address,
                        ),
                    )
                    event = await self._insert_rotation_event(
                        address=address,
                        alias=str(row["alias"]),
                        action="REMOVED",
                        reason=reason,
                        score=score,
                        pnl_24h=pnl_24h,
                        pnl_7d=pnl_7d,
                        baseline_24h=baseline_24h,
                        baseline_7d=baseline_7d,
                        tracking_started_at=tracking_started_at,
                        recorded_at=refreshed_at,
                    )
                    removal_events.append(event)

                # A rotated-out wallet with an open source-linked PAPER lot remains
                # subscribed in exit-only mode. New buys from it are ignored by the
                # engine, but its later sell can still close the linked fake position.
                await self.db.execute(
                    f"""
                    UPDATE tracked_traders SET enabled = 0
                    WHERE source = 'auto'
                      AND address NOT IN ({placeholders})
                      AND address NOT IN (
                          SELECT DISTINCT trader_address
                          FROM paper_mirror_positions
                      )
                    """,
                    tuple(selected_addresses),
                )
                await self.db.execute(
                    """
                    INSERT INTO settings(key, value) VALUES ('rotation_last_refresh', ?)
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
        return DiscoveryRefresh(
            tuple(hydrated),
            added,
            disabled,
            refreshed_at,
            candidate_pool_size=candidate_pool_size or len(evaluated),
            verified_pump_wallets=verified_pump_wallets or len(candidates),
            removal_events=tuple(removal_events),
        )

    async def _insert_rotation_event(
        self,
        *,
        address: str,
        alias: str,
        action: str,
        reason: str,
        score: Decimal,
        pnl_24h: Decimal,
        pnl_7d: Decimal,
        baseline_24h: Decimal,
        baseline_7d: Decimal,
        tracking_started_at: int,
        recorded_at: int,
    ) -> WalletRotationEvent:
        source_cursor = await self.db.execute(
            """
            SELECT COALESCE(SUM(realized_pnl_usd), 0) AS pnl
            FROM swaps WHERE trader_address = ? AND block_time >= ?
            """,
            (address, tracking_started_at),
        )
        source_row = await source_cursor.fetchone()
        paper_cursor = await self.db.execute(
            """
            SELECT COALESCE(SUM(realized_pnl_usd), 0) AS pnl
            FROM paper_trades WHERE source_trader = ? AND created_at >= ?
            """,
            (address, tracking_started_at),
        )
        paper_row = await paper_cursor.fetchone()
        observed_source_pnl = _d(source_row["pnl"] if source_row else 0)
        paper_pnl = _d(paper_row["pnl"] if paper_row else 0)
        await self.db.execute(
            """
            INSERT INTO wallet_rotation_events(
                address, alias, action, reason, score, pnl_24h_usd, pnl_7d_usd,
                baseline_pnl_24h_usd, baseline_pnl_7d_usd,
                observed_source_pnl_usd, paper_pnl_usd, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                address,
                alias,
                action,
                reason,
                float(score),
                float(pnl_24h),
                float(pnl_7d),
                float(baseline_24h),
                float(baseline_7d),
                float(observed_source_pnl),
                float(paper_pnl),
                recorded_at,
            ),
        )
        return WalletRotationEvent(
            address=address,
            alias=alias,
            action=action,
            reason=reason,
            score=score,
            pnl_24h_usd=pnl_24h,
            pnl_7d_usd=pnl_7d,
            baseline_pnl_24h_usd=baseline_24h,
            baseline_pnl_7d_usd=baseline_7d,
            observed_source_pnl_usd=observed_source_pnl,
            paper_pnl_usd=paper_pnl,
            recorded_at=recorded_at,
        )

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
                    _d(row["previous_pnl_24h"]) if row["previous_pnl_24h"] is not None else None
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
                    int(row["last_trade_ms"]) if row["last_trade_ms"] is not None else None
                ),
                score=_d(row["score"]),
                rank=int(row["rank"]),
                realized_pnl_7d=_d(row["realized_pnl_7d"]),
                roi_7d_percent=_d(row["roi_7d_percent"]),
                win_rate_7d_percent=_d(row["win_rate_7d_percent"]),
                trades_7d=int(row["trades_7d"]),
                recent_swaps=int(row["recent_swaps"]),
                pump_swaps=int(row["pump_swaps"]),
                last_activity_at=(
                    int(row["last_activity_at"]) if row["last_activity_at"] is not None else None
                ),
                selection_reason=str(row["selection_reason"] or ""),
            )
            for row in rows
        ]

    async def rotation_events(self, *, limit: int = 10) -> list[WalletRotationEvent]:
        cursor = await self.db.execute(
            "SELECT * FROM wallet_rotation_events ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [
            WalletRotationEvent(
                address=row["address"],
                alias=row["alias"],
                action=row["action"],
                reason=row["reason"],
                score=_d(row["score"]),
                pnl_24h_usd=_d(row["pnl_24h_usd"]),
                pnl_7d_usd=_d(row["pnl_7d_usd"]),
                baseline_pnl_24h_usd=_d(row["baseline_pnl_24h_usd"]),
                baseline_pnl_7d_usd=_d(row["baseline_pnl_7d_usd"]),
                observed_source_pnl_usd=_d(row["observed_source_pnl_usd"]),
                paper_pnl_usd=_d(row["paper_pnl_usd"]),
                recorded_at=int(row["recorded_at"]),
            )
            for row in rows
        ]

    async def hot_wallet_reports(self, *, limit: int = 25) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT * FROM discovery_wallets
            WHERE qualified = 1 ORDER BY rank LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        reports: list[dict[str, Any]] = []
        for row in rows:
            started_at = int(row["tracking_started_at"] or row["first_seen_at"])
            source_cursor = await self.db.execute(
                """
                SELECT COUNT(*) AS swaps, COALESCE(SUM(realized_pnl_usd), 0) AS pnl
                FROM swaps WHERE trader_address = ? AND block_time >= ?
                """,
                (row["address"], started_at),
            )
            source = await source_cursor.fetchone()
            paper_cursor = await self.db.execute(
                """
                SELECT COUNT(*) AS fills,
                       COALESCE(SUM(realized_pnl_usd), 0) AS pnl,
                       COALESCE(SUM(CASE WHEN side = 'SELL' THEN 1 ELSE 0 END), 0)
                            AS closed_sells,
                       COALESCE(SUM(CASE WHEN side = 'SELL' AND realized_pnl_usd > 0
                            THEN realized_pnl_usd ELSE 0 END), 0) AS gross_profit,
                       COALESCE(SUM(CASE WHEN side = 'SELL' AND realized_pnl_usd < 0
                            THEN -realized_pnl_usd ELSE 0 END), 0) AS gross_loss
                FROM paper_trades WHERE source_trader = ? AND created_at >= ?
                """,
                (row["address"], started_at),
            )
            paper = await paper_cursor.fetchone()
            gross_profit = _d(paper["gross_profit"] if paper else 0)
            gross_loss = _d(paper["gross_loss"] if paper else 0)
            profit_factor = (
                gross_profit / gross_loss
                if gross_loss > 0
                else Decimal("999")
                if gross_profit > 0
                else Decimal("0")
            )
            reports.append(
                {
                    "address": row["address"],
                    "alias": row["alias"],
                    "rank": int(row["rank"]),
                    "score": _d(row["score"]),
                    "pnl_24h": _d(row["realized_pnl_24h"]),
                    "pnl_7d": _d(row["realized_pnl_7d"]),
                    "roi_24h": _d(row["roi_24h_percent"]),
                    "roi_7d": _d(row["roi_7d_percent"]),
                    "win_24h": _d(row["win_rate_percent"]),
                    "win_7d": _d(row["win_rate_7d_percent"]),
                    "recent_swaps": int(row["recent_swaps"]),
                    "pump_swaps": int(row["pump_swaps"]),
                    "started_at": started_at,
                    "baseline_24h": _d(row["baseline_pnl_24h"]),
                    "baseline_7d": _d(row["baseline_pnl_7d"]),
                    "observed_swaps": int(source["swaps"] if source else 0),
                    "observed_source_pnl": _d(source["pnl"] if source else 0),
                    "paper_fills": int(paper["fills"] if paper else 0),
                    "paper_closed_sells": int((paper["closed_sells"] if paper else 0) or 0),
                    "paper_pnl": _d(paper["pnl"] if paper else 0),
                    "paper_profit_factor": profit_factor,
                    "selection_reason": str(row["selection_reason"] or ""),
                }
            )
        return reports

    async def paper_wallet_performance(
        self, addresses: list[str]
    ) -> dict[str, dict[str, Decimal | int]]:
        """Forward PAPER evidence for candidate admission and removal decisions."""

        unique = list(dict.fromkeys(addresses))
        if not unique:
            return {}
        placeholders = ",".join("?" for _ in unique)
        cursor = await self.db.execute(
            f"""
            SELECT source_trader,
                   COUNT(*) AS closed_sells,
                   COALESCE(SUM(realized_pnl_usd), 0) AS pnl,
                   COALESCE(SUM(CASE WHEN realized_pnl_usd > 0
                        THEN realized_pnl_usd ELSE 0 END), 0) AS gross_profit,
                   COALESCE(SUM(CASE WHEN realized_pnl_usd < 0
                        THEN -realized_pnl_usd ELSE 0 END), 0) AS gross_loss
            FROM paper_trades
            WHERE side = 'SELL' AND source_trader IN ({placeholders})
            GROUP BY source_trader
            """,
            tuple(unique),
        )
        rows = await cursor.fetchall()
        performance: dict[str, dict[str, Decimal | int]] = {}
        for row in rows:
            gross_profit = _d(row["gross_profit"])
            gross_loss = _d(row["gross_loss"])
            profit_factor = (
                gross_profit / gross_loss
                if gross_loss > 0
                else Decimal("999")
                if gross_profit > 0
                else Decimal("0")
            )
            performance[str(row["source_trader"])] = {
                "closed_sells": int(row["closed_sells"]),
                "pnl": _d(row["pnl"]),
                "gross_profit": gross_profit,
                "gross_loss": gross_loss,
                "profit_factor": profit_factor,
            }
        return performance

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
                        float(swap.token_price_usd) if swap.token_price_usd is not None else None,
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

    async def recent_signal_exists(self, token_mint: str, side: Side, cutoff: int) -> bool:
        cursor = await self.db.execute(
            """
            SELECT 1 FROM signals
            WHERE token_mint = ? AND side = ? AND created_at >= ? LIMIT 1
            """,
            (token_mint, side.value, cutoff),
        )
        return await cursor.fetchone() is not None

    async def recent_verified_token_buyers(
        self, token_mint: str, cutoff: int
    ) -> list[tuple[str, str]]:
        """Return distinct financially verified wallets buying a mint since cutoff."""

        cursor = await self.db.execute(
            """
            SELECT s.trader_address, t.alias, MAX(s.block_time) AS latest_buy
            FROM swaps AS s
            JOIN tracked_traders AS t ON t.address = s.trader_address
            JOIN discovery_wallets AS d ON d.address = s.trader_address
            WHERE s.token_mint = ? AND s.side = 'BUY' AND s.block_time >= ?
              AND t.enabled = 1 AND d.qualified = 1
            GROUP BY s.trader_address, t.alias
            ORDER BY latest_buy DESC
            """,
            (token_mint, cutoff),
        )
        rows = await cursor.fetchall()
        return [(str(row["trader_address"]), str(row["alias"])) for row in rows]

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
        execution_kind: str = "CONSENSUS",
        exit_reason: str | None = None,
    ) -> dict[str, Decimal] | None:
        fee_rate = Decimal(fee_bps) / Decimal(10_000)
        slip_rate = Decimal(slippage_bps) / Decimal(10_000)
        now = int(time.time())
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                account_cursor = await self.db.execute("SELECT * FROM paper_account WHERE id = 1")
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
                        gross_value_usd, fee_usd, realized_pnl_usd,
                        execution_kind, exit_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        execution_kind,
                        exit_reason,
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
        max_position_usd: Decimal | None = None,
        execution_kind: str = "RAW_MIRROR",
        exit_reason: str | None = None,
        quoted_input_amount: Decimal | None = None,
        quoted_output_amount: Decimal | None = None,
        token_decimals: int | None = None,
        source_price_usd: Decimal | None = None,
        quote_price_usd: Decimal | None = None,
        price_drift_percent: Decimal | None = None,
        price_impact_percent: Decimal | None = None,
        quote_router: str | None = None,
        quote_latency_ms: int | None = None,
        quote_fee_bps: int | None = None,
    ) -> dict[str, Decimal] | None:
        """Mirror one source wallet while keeping each wallet's paper lot separate."""

        if source_token_amount <= 0 or market_price_usd <= 0:
            return None
        effective_fee_bps = quote_fee_bps if quote_fee_bps is not None else fee_bps
        fee_rate = Decimal(effective_fee_bps) / Decimal(10_000)
        slip_rate = Decimal(slippage_bps) / Decimal(10_000)
        quote_based = quoted_output_amount is not None
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
                account_cursor = await self.db.execute("SELECT * FROM paper_account WHERE id = 1")
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
                    old_cost = _d(position["cost_basis_usd"]) if position else Decimal("0")
                    remaining_capacity = (
                        max(Decimal("0"), max_position_usd - old_cost)
                        if max_position_usd is not None
                        else size_usd
                    )
                    notional = min(size_usd, cash, remaining_capacity)
                    if notional <= Decimal("0.01"):
                        await self.db.rollback()
                        return None
                    fee = notional * fee_rate
                    if quote_based:
                        if quoted_output_amount is None or quoted_output_amount <= 0:
                            await self.db.rollback()
                            return None
                        paper_quantity = quoted_output_amount
                        effective_price = notional / paper_quantity
                    else:
                        effective_price = market_price_usd * (Decimal("1") + slip_rate)
                        paper_quantity = (notional - fee) / effective_price
                    old_source_quantity = (
                        _d(position["source_quantity"]) if position else Decimal("0")
                    )
                    old_paper_quantity = (
                        _d(position["paper_quantity"]) if position else Decimal("0")
                    )
                    old_peak = _d(position["peak_price_usd"]) if position else Decimal("0")
                    new_source_quantity = old_source_quantity + source_token_amount
                    new_paper_quantity = old_paper_quantity + paper_quantity
                    new_cost = old_cost + notional
                    average_entry = new_cost / new_paper_quantity
                    peak_price = max(old_peak, market_price_usd)
                    await self.db.execute(
                        """
                        INSERT INTO paper_mirror_positions(
                            trader_address, token_mint, source_quantity, paper_quantity,
                            cost_basis_usd, average_entry_usd, peak_price_usd,
                            token_decimals, opened_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(trader_address, token_mint) DO UPDATE SET
                            source_quantity = excluded.source_quantity,
                            paper_quantity = excluded.paper_quantity,
                            cost_basis_usd = excluded.cost_basis_usd,
                            average_entry_usd = excluded.average_entry_usd,
                            peak_price_usd = excluded.peak_price_usd,
                            token_decimals = COALESCE(
                                excluded.token_decimals,
                                paper_mirror_positions.token_decimals
                            ),
                            updated_at = excluded.updated_at
                        """,
                        (
                            trader_address,
                            token_mint,
                            float(new_source_quantity),
                            float(new_paper_quantity),
                            float(new_cost),
                            float(average_entry),
                            float(peak_price),
                            token_decimals,
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
                    if quote_based:
                        if (
                            quoted_input_amount is None
                            or quoted_input_amount <= 0
                            or quoted_output_amount is None
                            or quoted_output_amount <= 0
                        ):
                            await self.db.rollback()
                            return None
                        paper_quantity = min(paper_quantity, quoted_input_amount)
                        net = quoted_output_amount
                        fee = (
                            net * fee_rate / (Decimal("1") - fee_rate)
                            if fee_rate < 1
                            else Decimal("0")
                        )
                        gross = net + fee
                        effective_price = net / paper_quantity
                    else:
                        effective_price = market_price_usd * (Decimal("1") - slip_rate)
                        gross = paper_quantity * effective_price
                        fee = gross * fee_rate
                        net = gross - fee
                    realized = net - matched_cost
                    cash += net
                    realized_total += realized
                    remaining_source_quantity = observed_source_quantity - min(
                        source_token_amount, observed_source_quantity
                    )
                    remaining_quantity = held_paper_quantity - paper_quantity
                    remaining_cost = held_cost - matched_cost
                    if remaining_source_quantity <= Decimal(
                        "0.000000001"
                    ) or remaining_quantity <= Decimal("0.000000001"):
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
                        source_signature, execution_kind, exit_reason, source_price_usd,
                        quote_price_usd, price_drift_percent, price_impact_percent,
                        quote_router, quote_latency_ms, quote_fee_bps, quote_based,
                        created_at
                    ) VALUES (
                        NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
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
                        execution_kind,
                        exit_reason,
                        float(source_price_usd) if source_price_usd is not None else None,
                        float(quote_price_usd) if quote_price_usd is not None else None,
                        (float(price_drift_percent) if price_drift_percent is not None else None),
                        (float(price_impact_percent) if price_impact_percent is not None else None),
                        quote_router,
                        quote_latency_ms,
                        effective_fee_bps if quote_based else None,
                        1 if quote_based else 0,
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
                    "quote_based": Decimal("1") if quote_based else Decimal("0"),
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

    async def has_paper_mirror_position(self, trader_address: str, token_mint: str) -> bool:
        cursor = await self.db.execute(
            """
            SELECT 1 FROM paper_mirror_positions
            WHERE trader_address = ? AND token_mint = ?
            """,
            (trader_address, token_mint),
        )
        return await cursor.fetchone() is not None

    async def paper_tracking_baseline_candidates(
        self, trader_address: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return source holdings that predate PAPER tracking for this wallet.

        Bootstrap swaps build ``trader_inventory`` without firing old copy signals.
        A current-price baseline lets future source sells measure only the movement
        observed after tracking started.  Any token that already received a PAPER buy
        is excluded so a risk/manual exit can never be silently reopened.
        """

        cursor = await self.db.execute(
            """
            SELECT
                inventory.token_mint,
                inventory.quantity AS source_quantity,
                inventory.cost_basis_usd AS source_cost_basis_usd,
                MAX(swaps.block_time) AS last_source_activity_at,
                (
                    SELECT priced.token_price_usd
                    FROM swaps AS priced
                    WHERE priced.trader_address = inventory.trader_address
                      AND priced.token_mint = inventory.token_mint
                      AND priced.token_price_usd IS NOT NULL
                      AND priced.token_price_usd > 0
                    ORDER BY priced.block_time DESC, priced.rowid DESC
                    LIMIT 1
                ) AS last_source_price_usd
            FROM trader_inventory AS inventory
            JOIN swaps
              ON swaps.trader_address = inventory.trader_address
             AND swaps.token_mint = inventory.token_mint
            WHERE inventory.trader_address = ?
              AND inventory.quantity > 0.000000001
              AND NOT EXISTS (
                  SELECT 1 FROM paper_mirror_positions AS position
                  WHERE position.trader_address = inventory.trader_address
                    AND position.token_mint = inventory.token_mint
              )
              AND NOT EXISTS (
                  SELECT 1 FROM paper_trades AS trade
                  WHERE trade.source_trader = inventory.trader_address
                    AND trade.token_mint = inventory.token_mint
                    AND trade.side = 'BUY'
              )
            GROUP BY
                inventory.trader_address,
                inventory.token_mint,
                inventory.quantity,
                inventory.cost_basis_usd
            ORDER BY last_source_activity_at DESC
            LIMIT ?
            """,
            (trader_address, max(1, min(limit, 50))),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def paper_mirror_open_lot_is_sniper(self, trader_address: str, token_mint: str) -> bool:
        """Return whether the newest buy contributing to an open lot used sniper PAPER."""

        if not await self.has_paper_mirror_position(trader_address, token_mint):
            return False
        cursor = await self.db.execute(
            """
            SELECT execution_kind FROM paper_trades
            WHERE source_trader = ? AND token_mint = ? AND side = 'BUY'
            ORDER BY id DESC LIMIT 1
            """,
            (trader_address, token_mint),
        )
        row = await cursor.fetchone()
        return bool(row and str(row["execution_kind"]).startswith("SNIPER_"))

    async def paper_mirror_latest_event(
        self, trader_address: str, token_mint: str
    ) -> dict[str, Any] | None:
        """Return the latest filled paper event for clearer unmatched-sell messages."""

        cursor = await self.db.execute(
            """
            SELECT side, execution_kind, exit_reason, realized_pnl_usd, created_at
            FROM paper_trades
            WHERE source_trader = ? AND token_mint = ?
            ORDER BY id DESC LIMIT 1
            """,
            (trader_address, token_mint),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def paper_mirror_buy_capacity(
        self,
        trader_address: str,
        token_mint: str,
        requested_usd: Decimal,
        max_position_usd: Decimal,
    ) -> Decimal:
        account_cursor = await self.db.execute("SELECT cash_usd FROM paper_account WHERE id = 1")
        account = await account_cursor.fetchone()
        position_cursor = await self.db.execute(
            """
            SELECT cost_basis_usd FROM paper_mirror_positions
            WHERE trader_address = ? AND token_mint = ?
            """,
            (trader_address, token_mint),
        )
        position = await position_cursor.fetchone()
        current_cost = _d(position["cost_basis_usd"]) if position else Decimal("0")
        remaining = max(Decimal("0"), max_position_usd - current_cost)
        return max(
            Decimal("0"),
            min(requested_usd, _d(account["cash_usd"]), remaining),
        )

    async def paper_mirror_sell_preview(
        self,
        trader_address: str,
        token_mint: str,
        source_token_amount: Decimal,
    ) -> dict[str, Decimal | int | None] | None:
        cursor = await self.db.execute(
            """
            SELECT * FROM paper_mirror_positions
            WHERE trader_address = ? AND token_mint = ?
            """,
            (trader_address, token_mint),
        )
        position = await cursor.fetchone()
        if position is None:
            return None
        source_quantity = _d(position["source_quantity"])
        paper_quantity = _d(position["paper_quantity"])
        cost_basis = _d(position["cost_basis_usd"])
        if source_quantity <= 0 or paper_quantity <= 0:
            return None
        source_fraction = min(Decimal("1"), source_token_amount / source_quantity)
        return {
            "source_fraction": source_fraction,
            "paper_quantity": paper_quantity * source_fraction,
            "matched_cost_usd": cost_basis * source_fraction,
            "token_decimals": position["token_decimals"],
        }

    async def record_paper_quote_attempt(
        self,
        *,
        source_signature: str | None,
        token_mint: str,
        side: Side,
        quote_success: bool,
        accepted: bool,
        reason: str | None,
        latency_ms: int | None = None,
        price_impact_percent: Decimal | None = None,
        price_drift_percent: Decimal | None = None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO paper_quote_attempts(
                source_signature, token_mint, side, quote_success, accepted,
                reason, latency_ms, price_impact_percent, price_drift_percent,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_signature,
                token_mint,
                side.value,
                1 if quote_success else 0,
                1 if accepted else 0,
                reason,
                latency_ms,
                (float(price_impact_percent) if price_impact_percent is not None else None),
                float(price_drift_percent) if price_drift_percent is not None else None,
                int(time.time()),
            ),
        )
        await self.db.commit()

    async def update_paper_mirror_peak(
        self,
        trader_address: str,
        token_mint: str,
        market_price_usd: Decimal,
    ) -> Decimal:
        """Persist and return the highest observed market price for one mirror lot."""

        async with self._write_lock:
            cursor = await self.db.execute(
                """
                SELECT peak_price_usd FROM paper_mirror_positions
                WHERE trader_address = ? AND token_mint = ?
                """,
                (trader_address, token_mint),
            )
            row = await cursor.fetchone()
            if row is None:
                return market_price_usd
            previous_peak = _d(row["peak_price_usd"])
            peak = max(previous_peak, market_price_usd)
            if peak == previous_peak:
                return peak
            await self.db.execute(
                """
                UPDATE paper_mirror_positions
                SET peak_price_usd = ?
                WHERE trader_address = ? AND token_mint = ?
                """,
                (float(peak), trader_address, token_mint),
            )
            await self.db.commit()
            return peak

    async def paper_positions(self) -> list[dict[str, Any]]:
        cursor = await self.db.execute("SELECT * FROM paper_positions ORDER BY opened_at")
        return [dict(row) for row in await cursor.fetchall()]

    async def paper_mirror_positions(self) -> list[dict[str, Any]]:
        cursor = await self.db.execute("SELECT * FROM paper_mirror_positions ORDER BY opened_at")
        return [dict(row) for row in await cursor.fetchall()]

    async def paper_recent_trades(self, limit: int = 15) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT * FROM paper_trades
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 50)),),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def paper_trade_count(self) -> int:
        cursor = await self.db.execute("SELECT COUNT(*) AS count FROM paper_trades")
        row = await cursor.fetchone()
        return int(row["count"] or 0)

    async def paper_trades_page(self, *, limit: int = 5, offset: int = 0) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT * FROM paper_trades
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (max(1, min(limit, 10)), max(0, offset)),
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
                "peak_price_usd": item["peak_price_usd"],
            }
            for item in mirror
        )
        return sorted(combined, key=lambda item: int(item["opened_at"]))

    async def paper_position_count(self) -> int:
        standard_cursor = await self.db.execute("SELECT COUNT(*) AS count FROM paper_positions")
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

    async def first_paper_equity_between(
        self, start_timestamp: int, end_timestamp: int
    ) -> Decimal | None:
        """Return the first recorded account mark inside one local trading day."""

        cursor = await self.db.execute(
            """
            SELECT equity_usd FROM paper_equity_samples
            WHERE created_at >= ? AND created_at < ?
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (start_timestamp, end_timestamp),
        )
        row = await cursor.fetchone()
        return _d(row["equity_usd"]) if row is not None else None

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
                SUM(CASE WHEN side = 'SELL' AND realized_pnl_usd <= 0 THEN 1 ELSE 0 END) losses,
                SUM(
                    CASE WHEN side = 'SELL' AND realized_pnl_usd > 0
                    THEN realized_pnl_usd ELSE 0 END
                ) AS gross_profit,
                ABS(SUM(
                    CASE WHEN side = 'SELL' AND realized_pnl_usd < 0
                    THEN realized_pnl_usd ELSE 0 END
                )) AS gross_loss,
                AVG(
                    CASE WHEN side = 'SELL' AND realized_pnl_usd > 0
                    THEN realized_pnl_usd END
                ) AS average_win,
                ABS(AVG(
                    CASE WHEN side = 'SELL' AND realized_pnl_usd < 0
                    THEN realized_pnl_usd END
                )) AS average_loss
            FROM paper_trades
            """
        )
        trade_row = await trades_cursor.fetchone()
        trade_count = int(trade_row["trades"] or 0)
        gross_profit = _d(trade_row["gross_profit"])
        gross_loss = _d(trade_row["gross_loss"])
        net_closed = gross_profit - gross_loss
        refreshed = await self.db.execute("SELECT * FROM paper_account WHERE id = 1")
        account = await refreshed.fetchone()
        return PaperSummary(
            starting_cash_usd=_d(account["starting_cash_usd"]),
            cash_usd=cash,
            positions_value_usd=positions_value,
            equity_usd=equity,
            realized_pnl_usd=_d(account["realized_pnl_usd"]),
            unrealized_pnl_usd=positions_value - cost_basis,
            trades=trade_count,
            wins=int(trade_row["wins"] or 0),
            losses=int(trade_row["losses"] or 0),
            max_drawdown_usd=_d(account["max_drawdown_usd"]),
            current_drawdown_usd=max(Decimal("0"), _d(account["high_watermark_usd"]) - equity),
            realized_pnl_24h_usd=await self.paper_daily_realized_pnl(),
            gross_profit_usd=gross_profit,
            gross_loss_usd=gross_loss,
            average_win_usd=_d(trade_row["average_win"]),
            average_loss_usd=_d(trade_row["average_loss"]),
            expectancy_usd=(net_closed / Decimal(trade_count) if trade_count else Decimal("0")),
            profit_factor=(gross_profit / gross_loss if gross_loss > 0 else None),
        )

    async def paper_readiness(
        self,
        *,
        min_active_days: int,
        min_closed_trades: int,
        min_profit_factor: Decimal,
        max_drawdown_percent: Decimal,
        min_quote_success_percent: Decimal,
    ) -> PaperReadiness:
        now = int(time.time())
        raw_start = await self.get_setting("paper_trial_started_at", str(now))
        trial_started_at = int(raw_start or now)

        active_cursor = await self.db.execute(
            """
            SELECT COUNT(DISTINCT day) AS active_days FROM (
                SELECT date(created_at, 'unixepoch') AS day
                FROM paper_quote_attempts WHERE created_at >= ?
                UNION ALL
                SELECT date(created_at, 'unixepoch') AS day
                FROM paper_trades WHERE quote_based = 1 AND created_at >= ?
            )
            """,
            (trial_started_at, trial_started_at),
        )
        active_row = await active_cursor.fetchone()
        active_days = int(active_row["active_days"] or 0)

        quote_cursor = await self.db.execute(
            """
            SELECT
                COUNT(*) AS attempts,
                SUM(quote_success) AS successes,
                SUM(CASE WHEN side = 'BUY' AND accepted = 1 THEN 1 ELSE 0 END)
                    AS accepted_entries
            FROM paper_quote_attempts WHERE created_at >= ?
            """,
            (trial_started_at,),
        )
        quote_row = await quote_cursor.fetchone()
        quote_attempts = int(quote_row["attempts"] or 0)
        quote_successes = int(quote_row["successes"] or 0)
        quote_success_percent = (
            Decimal(quote_successes) / Decimal(quote_attempts) * Decimal("100")
            if quote_attempts
            else Decimal("0")
        )

        trade_cursor = await self.db.execute(
            """
            SELECT
                COUNT(*) AS closed_trades,
                SUM(CASE WHEN realized_pnl_usd > 0 THEN realized_pnl_usd ELSE 0 END)
                    AS gross_profit,
                ABS(SUM(CASE WHEN realized_pnl_usd < 0 THEN realized_pnl_usd ELSE 0 END))
                    AS gross_loss,
                SUM(realized_pnl_usd) AS net_pnl
            FROM paper_trades
            WHERE side = 'SELL'
              AND quote_based = 1
              AND execution_kind NOT LIKE 'MANUAL%'
              AND created_at >= ?
            """,
            (trial_started_at,),
        )
        trade_row = await trade_cursor.fetchone()
        closed_trades = int(trade_row["closed_trades"] or 0)
        gross_profit = _d(trade_row["gross_profit"])
        gross_loss = _d(trade_row["gross_loss"])
        net_pnl = _d(trade_row["net_pnl"])
        expectancy = net_pnl / Decimal(closed_trades) if closed_trades else Decimal("0")
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else None

        samples_cursor = await self.db.execute(
            """
            SELECT equity_usd FROM paper_equity_samples
            WHERE created_at >= ? ORDER BY created_at, id
            """,
            (trial_started_at,),
        )
        samples = [_d(row["equity_usd"]) for row in await samples_cursor.fetchall()]
        peak = samples[0] if samples else Decimal("0")
        max_drawdown = Decimal("0")
        max_drawdown_pct = Decimal("0")
        for equity in samples:
            peak = max(peak, equity)
            drawdown = max(Decimal("0"), peak - equity)
            drawdown_pct = drawdown / peak * Decimal("100") if peak > 0 else Decimal("0")
            max_drawdown = max(max_drawdown, drawdown)
            max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)

        blockers: list[str] = []
        if active_days < min_active_days:
            blockers.append(f"{min_active_days - active_days} more active test day(s)")
        if closed_trades < min_closed_trades:
            blockers.append(f"{min_closed_trades - closed_trades} more quoted exits")
        profit_factor_passes = (
            profit_factor >= min_profit_factor
            if profit_factor is not None
            else gross_profit > 0 and gross_loss == 0
        )
        if not profit_factor_passes:
            blockers.append(f"profit factor below {min_profit_factor:.2f}")
        if expectancy <= 0:
            blockers.append("expectancy is not positive")
        if max_drawdown_pct > max_drawdown_percent:
            blockers.append(f"drawdown {max_drawdown_pct:.2f}% exceeds {max_drawdown_percent:.2f}%")
        if quote_success_percent < min_quote_success_percent:
            blockers.append(
                f"quote success {quote_success_percent:.1f}% is below "
                f"{min_quote_success_percent:.1f}%"
            )

        return PaperReadiness(
            trial_started_at=trial_started_at,
            active_days=active_days,
            quote_attempts=quote_attempts,
            quote_successes=quote_successes,
            quote_success_percent=quote_success_percent,
            accepted_entries=int(quote_row["accepted_entries"] or 0),
            closed_trades=closed_trades,
            gross_profit_usd=gross_profit,
            gross_loss_usd=gross_loss,
            expectancy_usd=expectancy,
            profit_factor=profit_factor,
            max_drawdown_usd=max_drawdown,
            max_drawdown_percent=max_drawdown_pct,
            ready=not blockers,
            blockers=tuple(blockers),
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
        await self.db.execute(
            "INSERT INTO paper_equity_samples(equity_usd, created_at) VALUES (?, ?)",
            (float(equity), int(time.time())),
        )
        await self.db.commit()

    async def reset_paper(self) -> None:
        now = int(time.time())
        async with self._write_lock:
            await self.db.execute("DELETE FROM paper_positions")
            await self.db.execute("DELETE FROM paper_mirror_positions")
            await self.db.execute("DELETE FROM paper_trades")
            await self.db.execute("DELETE FROM paper_quote_attempts")
            await self.db.execute("DELETE FROM paper_equity_samples")
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
            await self.db.execute(
                """
                INSERT INTO settings(key, value) VALUES ('paper_trial_started_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(now),),
            )
            await self.db.execute(
                """
                DELETE FROM settings WHERE key IN (
                    'paper_daily_lock_day',
                    'paper_daily_lock_baseline_equity_usd',
                    'paper_daily_lock_triggered',
                    'paper_daily_lock_triggered_at'
                )
                """
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

    async def reserve_pump_launch(
        self,
        *,
        alert_key: str,
        source_url: str,
        headline: str,
        name: str,
        symbol: str,
        score: int,
        initial_buy_sol: Decimal,
        requested_by: str,
    ) -> bool:
        """Atomically reserve one news item so double clicks cannot launch twice."""

        now = int(time.time())
        async with self._write_lock:
            cursor = await self.db.execute(
                """
                INSERT OR IGNORE INTO pump_launches(
                    alert_key, source_url, headline, name, symbol, score,
                    initial_buy_sol, requested_by, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', ?, ?)
                """,
                (
                    alert_key,
                    source_url[:1000],
                    headline[:1000],
                    name[:64],
                    symbol[:16],
                    score,
                    float(initial_buy_sol),
                    requested_by[:64],
                    now,
                    now,
                ),
            )
            await self.db.commit()
            return cursor.rowcount > 0

    async def complete_pump_launch(
        self,
        *,
        alert_key: str,
        status: str,
        mint: str,
        signature: str,
        metadata_uri: str,
    ) -> None:
        await self.db.execute(
            """
            UPDATE pump_launches SET
                status = ?, mint = ?, signature = ?, metadata_uri = ?,
                error = NULL, updated_at = ?
            WHERE alert_key = ?
            """,
            (
                status,
                mint,
                signature,
                metadata_uri,
                int(time.time()),
                alert_key,
            ),
        )
        await self.db.commit()

    async def fail_pump_launch(self, alert_key: str, error: str) -> None:
        await self.db.execute(
            """
            UPDATE pump_launches SET status = 'FAILED', error = ?, updated_at = ?
            WHERE alert_key = ?
            """,
            (error[:1000], int(time.time()), alert_key),
        )
        await self.db.commit()

    async def pump_launch_daily_usage(
        self,
        *,
        start_at: int,
        end_at: int,
    ) -> tuple[int, Decimal]:
        cursor = await self.db.execute(
            """
            SELECT COUNT(*) AS launches, COALESCE(SUM(initial_buy_sol), 0) AS sol
            FROM pump_launches
            WHERE created_at >= ? AND created_at < ?
              AND status IN ('RESERVED', 'SUBMITTED', 'CONFIRMED')
            """,
            (start_at, end_at),
        )
        row = await cursor.fetchone()
        return int(row["launches"] or 0), _d(row["sol"])

    async def recent_pump_launches(self, *, limit: int = 10) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM pump_launches ORDER BY created_at DESC LIMIT ?",
            (max(1, min(50, limit)),),
        )
        return [dict(row) for row in await cursor.fetchall()]

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

    async def cache_discovery_candidates(self, candidates: list[DiscoveryCandidate]) -> None:
        """Persist the verified pre-rotation pool across Railway redeploys."""

        payload = [asdict(candidate) for candidate in candidates]
        await self.set_setting(
            "discovery_candidate_pool_v1",
            json.dumps(payload, default=str, separators=(",", ":")),
        )

    async def load_discovery_candidates(self) -> list[DiscoveryCandidate]:
        raw = await self.get_setting("discovery_candidate_pool_v1")
        if not raw:
            return []
        decimal_fields = {
            "realized_pnl_24h",
            "previous_pnl_24h",
            "roi_24h_percent",
            "win_rate_percent",
            "invested_24h_usd",
            "volume_24h_usd",
            "score",
            "realized_pnl_7d",
            "roi_7d_percent",
            "win_rate_7d_percent",
        }
        try:
            decoded = json.loads(raw)
            if not isinstance(decoded, list):
                return []
            candidates = []
            for item in decoded:
                if not isinstance(item, dict):
                    continue
                values = dict(item)
                for field_name in decimal_fields:
                    value = values.get(field_name)
                    if value is not None:
                        values[field_name] = Decimal(str(value))
                candidates.append(DiscoveryCandidate(**values))
            return candidates
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
