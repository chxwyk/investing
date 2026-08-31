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
    RunnerCandidate,
    Side,
    Signal,
    TrackedTrader,
    TraderMetrics,
    WalletRotationEvent,
)
from .quality import STAGE_RAW, USER_FACING_STAGES, merge_best_stage


def _d(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def _float_or_none(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


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
                    status IN (
                        'RESERVED', 'SUBMITTED', 'CONFIRMED', 'FAILED', 'UNKNOWN_RESULT'
                    )
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

            CREATE TABLE IF NOT EXISTS launch_candidates (
                cluster_key TEXT PRIMARY KEY,
                alert_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                headline TEXT NOT NULL,
                source_url TEXT NOT NULL,
                category TEXT NOT NULL,
                score INTEGER NOT NULL,
                verdict TEXT NOT NULL,
                evaluated_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_launch_candidates_rank
                ON launch_candidates(score DESC, evaluated_at DESC);

            CREATE TABLE IF NOT EXISTS runner_candidates (
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
                chain_created_at INTEGER,
                pair_created_at INTEGER,
                radar_first_seen_at INTEGER,
                first_market_data_at INTEGER,
                first_research_eligible_at INTEGER,
                first_discord_visible_at INTEGER,
                entry_eligible_at INTEGER,
                strong_alert_at INTEGER,
                first_visible_market_cap_usd REAL,
                entry_market_cap_usd REAL,
                peak_market_cap_usd REAL,
                last_seen_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_runner_candidates_rank
                ON runner_candidates(latest_score DESC, last_seen_at DESC);

            CREATE TABLE IF NOT EXISTS runner_snapshots (
                mint TEXT NOT NULL,
                captured_at INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                score REAL NOT NULL,
                PRIMARY KEY (mint, captured_at),
                FOREIGN KEY (mint) REFERENCES runner_candidates(mint) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_runner_snapshots_time
                ON runner_snapshots(captured_at DESC);

            CREATE TABLE IF NOT EXISTS runner_outcomes (
                mint TEXT NOT NULL,
                horizon_seconds INTEGER NOT NULL,
                observed_at INTEGER NOT NULL,
                price_return_percent REAL,
                market_cap_return_percent REAL,
                liquidity_return_percent REAL,
                liquidity_disappeared INTEGER NOT NULL DEFAULT 0,
                rugged INTEGER NOT NULL DEFAULT 0,
                route_available INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (mint, horizon_seconds),
                FOREIGN KEY (mint) REFERENCES runner_candidates(mint) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_runner_outcomes_horizon
                ON runner_outcomes(horizon_seconds, observed_at DESC);

            CREATE TABLE IF NOT EXISTS runner_alert_events (
                mint TEXT NOT NULL,
                event_type TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                first_sent_at INTEGER NOT NULL,
                last_sent_at INTEGER NOT NULL,
                send_count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (mint, event_type),
                FOREIGN KEY (mint) REFERENCES runner_candidates(mint) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS runner_forensics (
                mint TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                funding_checked_at INTEGER NOT NULL,
                dynamic_checked_at INTEGER NOT NULL,
                FOREIGN KEY (mint) REFERENCES runner_candidates(mint) ON DELETE CASCADE
            );

            -- Immutable funnel decisions. One row per (mint, stage, second) so a
            -- later re-evaluation can never rewrite what was known at the time.
            CREATE TABLE IF NOT EXISTS runner_stage_events (
                mint TEXT NOT NULL,
                stage TEXT NOT NULL,
                decided_at INTEGER NOT NULL,
                momentum_score REAL,
                opportunity_score REAL,
                organic_score REAL,
                safety_status TEXT,
                market_cap_usd REAL,
                liquidity_usd REAL,
                evidence_json TEXT,
                warnings_json TEXT,
                decision_version TEXT NOT NULL DEFAULT 'quality-v1',
                PRIMARY KEY (mint, stage, decided_at),
                FOREIGN KEY (mint) REFERENCES runner_candidates(mint) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_runner_stage_events_stage
                ON runner_stage_events(stage, decided_at DESC);

            -- Provider request accounting that is not tied to a daily cap, so
            -- cost can be attributed per feature without gating anything.
            CREATE TABLE IF NOT EXISTS provider_call_usage (
                provider TEXT NOT NULL,
                feature TEXT NOT NULL,
                usage_day TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0,
                cache_hits INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (provider, feature, usage_day)
            );

            -- Funding relationships are immutable once observed, so caching them
            -- removes the dominant repeat RPC cost of the forensic trace.
            CREATE TABLE IF NOT EXISTS wallet_funding_edges (
                wallet TEXT PRIMARY KEY,
                funder TEXT,
                funded_at INTEGER,
                amount_sol REAL,
                first_activity_at INTEGER,
                trace_complete INTEGER NOT NULL DEFAULT 0,
                resolved_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS api_usage_daily (
                provider TEXT NOT NULL,
                operation TEXT NOT NULL,
                usage_day TEXT NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (provider, operation, usage_day)
            );

            CREATE TABLE IF NOT EXISTS x_budget_verifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usage_day TEXT NOT NULL,
                period_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                context TEXT NOT NULL,
                query TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('RESERVED', 'COMPLETED', 'FAILED')
                ),
                max_posts INTEGER NOT NULL,
                reserved_estimate_usd REAL NOT NULL,
                estimated_spend_usd REAL NOT NULL DEFAULT 0,
                post_resources INTEGER NOT NULL DEFAULT 0,
                user_resources INTEGER NOT NULL DEFAULT 0,
                http_requests INTEGER NOT NULL DEFAULT 0,
                free_score INTEGER,
                final_score INTEGER,
                outcome TEXT,
                status_code INTEGER,
                error_category TEXT,
                started_at INTEGER NOT NULL,
                completed_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_x_budget_day
                ON x_budget_verifications(usage_day, started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_x_budget_period
                ON x_budget_verifications(period_id, started_at DESC);

            CREATE TABLE IF NOT EXISTS x_budget_resources (
                usage_day TEXT NOT NULL,
                period_id TEXT NOT NULL,
                resource_type TEXT NOT NULL CHECK (resource_type IN ('post', 'user')),
                resource_id TEXT NOT NULL,
                estimated_cost_usd REAL NOT NULL,
                first_seen_at INTEGER NOT NULL,
                PRIMARY KEY (usage_day, resource_type, resource_id)
            );
            CREATE INDEX IF NOT EXISTS idx_x_resources_period
                ON x_budget_resources(period_id, first_seen_at DESC);

            CREATE TABLE IF NOT EXISTS x_user_cache (
                user_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                fetched_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS x_verification_cache (
                fingerprint TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                fetched_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- ============================================================
            -- PAPER research laboratory (v2.36).  Every table below is
            -- additive: nothing existing is dropped, renamed or rewritten,
            -- and every write is keyed so a Railway restart or a retried
            -- coroutine cannot duplicate a lifecycle, alert, entry or exit.
            -- ============================================================

            -- Durable per-mint memory.  This is what stops an old pump from
            -- ever being re-discovered as a brand new setup.
            CREATE TABLE IF NOT EXISTS lab_token_lifecycle (
                mint TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                first_discovered_at INTEGER NOT NULL,
                first_surfaced_at INTEGER,
                cycle_count INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_lab_lifecycle_state
                ON lab_token_lifecycle(state, updated_at DESC);

            -- The authoritative chronological event stream.  ``event_id`` is a
            -- content hash, so replaying a write is a no-op instead of a
            -- duplicate fact.
            CREATE TABLE IF NOT EXISTS lab_token_events (
                event_id TEXT PRIMARY KEY,
                mint TEXT NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_lab_events_mint_time
                ON lab_token_events(mint, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_lab_events_type
                ON lab_token_events(event_type, occurred_at DESC);

            -- Immutable decisions.  Old trades stay attributable to the exact
            -- rules and thresholds that produced them.
            CREATE TABLE IF NOT EXISTS lab_decisions (
                mint TEXT NOT NULL,
                decided_at INTEGER NOT NULL,
                strategy_version TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason_codes_json TEXT NOT NULL,
                evidence_quality TEXT NOT NULL,
                safety_status TEXT NOT NULL,
                lifecycle_state TEXT NOT NULL,
                expected_net_edge_percent REAL,
                size_usd REAL NOT NULL DEFAULT 0,
                config_hash TEXT NOT NULL,
                bot_version TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (mint, decided_at, strategy_version)
            );
            CREATE INDEX IF NOT EXISTS idx_lab_decisions_recent
                ON lab_decisions(decided_at DESC);
            CREATE INDEX IF NOT EXISTS idx_lab_decisions_decision
                ON lab_decisions(decision, decided_at DESC);

            -- Simulated positions.  The partial unique index is the duplicate
            -- entry lock: one open simulated position per mint per strategy.
            CREATE TABLE IF NOT EXISTS lab_positions (
                position_id TEXT PRIMARY KEY,
                mint TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                opened_at INTEGER NOT NULL,
                closed_at INTEGER,
                size_usd REAL NOT NULL,
                entry_price_usd REAL NOT NULL,
                realized_net_pnl_usd REAL NOT NULL DEFAULT 0,
                close_reason TEXT NOT NULL DEFAULT '',
                is_reentry INTEGER NOT NULL DEFAULT 0,
                lifecycle_state TEXT NOT NULL DEFAULT '',
                config_hash TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_lab_positions_open_unique
                ON lab_positions(mint, strategy_version)
                WHERE closed_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_lab_positions_recent
                ON lab_positions(opened_at DESC);

            -- The partial-exit journal.  (position_id, sequence) makes a retried
            -- exit idempotent instead of double-selling a simulated position.
            CREATE TABLE IF NOT EXISTS lab_exits (
                position_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                mint TEXT NOT NULL,
                occurred_at INTEGER NOT NULL,
                reason_code TEXT NOT NULL,
                fraction_sold REAL NOT NULL,
                gross_proceeds_usd REAL NOT NULL,
                total_cost_usd REAL NOT NULL,
                net_pnl_usd REAL NOT NULL,
                final INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (position_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS idx_lab_exits_recent
                ON lab_exits(occurred_at DESC);

            -- One simulated bankroll per strategy, so champion and challenger
            -- never share capital state.
            CREATE TABLE IF NOT EXISTS lab_bankroll (
                strategy_version TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );

            -- What was last published for a mint, so identical cards are
            -- suppressed across restarts.
            CREATE TABLE IF NOT EXISTS lab_publications (
                mint TEXT PRIMARY KEY,
                published_at INTEGER NOT NULL,
                lifecycle_state TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lab_wallet_reputation (
                wallet TEXT PRIMARY KEY,
                samples INTEGER NOT NULL DEFAULT 0,
                score REAL NOT NULL DEFAULT 50,
                state TEXT NOT NULL DEFAULT 'UNKNOWN',
                payload_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );

            -- Public social signals.  ``dedupe_key`` stops the same post from
            -- being counted twice, which is what makes the account lead/lag
            -- statistics honest.
            CREATE TABLE IF NOT EXISTS lab_social_signals (
                dedupe_key TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                account TEXT NOT NULL,
                tier TEXT NOT NULL,
                mint TEXT,
                classification TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                source_timestamp INTEGER NOT NULL,
                exact_mint_confidence REAL NOT NULL DEFAULT 0,
                price_at_signal REAL,
                market_cap_at_signal REAL,
                url TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_lab_social_account
                ON lab_social_signals(account, source_timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_lab_social_mint
                ON lab_social_signals(mint, source_timestamp DESC);

            CREATE TABLE IF NOT EXISTS lab_account_performance (
                account TEXT PRIMARY KEY,
                tier TEXT NOT NULL,
                samples INTEGER NOT NULL DEFAULT 0,
                classification TEXT NOT NULL DEFAULT 'INSUFFICIENT_DATA',
                lead_lag TEXT NOT NULL DEFAULT 'INSUFFICIENT_DATA',
                strategy_weight REAL NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );

            -- Static account metadata is cached aggressively so the curated
            -- radar spends as few X requests as possible.
            CREATE TABLE IF NOT EXISTS lab_account_cache (
                account TEXT PRIMARY KEY,
                fetched_at INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lab_social_budget (
                usage_day TEXT NOT NULL,
                provider TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0,
                posts_processed INTEGER NOT NULL DEFAULT 0,
                cache_hits INTEGER NOT NULL DEFAULT 0,
                cache_misses INTEGER NOT NULL DEFAULT 0,
                useful_signals INTEGER NOT NULL DEFAULT 0,
                useless_signals INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd REAL NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (usage_day, provider)
            );

            CREATE TABLE IF NOT EXISTS lab_strategy_registry (
                strategy_version TEXT PRIMARY KEY,
                role TEXT NOT NULL DEFAULT 'CHAMPION',
                config_hash TEXT NOT NULL DEFAULT '',
                activated_at INTEGER NOT NULL DEFAULT 0,
                calibration_cutoff_at INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL DEFAULT 0
            );

            -- Cheap-discovery ledger (v2.37).  A candidate's first-seen time is
            -- written here the instant cheap discovery detects it, BEFORE any
            -- expensive enrichment, so ingestion latency measures ingestion and
            -- not how long a provider took.  Stage times are filled forward
            -- only; first_seen_at can never move later.
            CREATE TABLE IF NOT EXISTS runner_discovery (
                mint TEXT PRIMARY KEY,
                source_name TEXT NOT NULL DEFAULT 'unknown',
                source_event_at INTEGER,
                source_is_realtime INTEGER NOT NULL DEFAULT 1,
                ingested_at INTEGER NOT NULL,
                first_seen_at INTEGER NOT NULL,
                first_watch_at INTEGER,
                first_qualified_at INTEGER,
                first_paper_decision_at INTEGER,
                simulated_fill_at INTEGER,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_runner_discovery_seen
                ON runner_discovery(first_seen_at DESC);
            CREATE INDEX IF NOT EXISTS idx_runner_discovery_source
                ON runner_discovery(source_name, first_seen_at DESC);

            -- Real-time alpha engine (v2.38).  All additive.

            -- Verified public wallet mappings.  A wallet with ONCHAIN_ONLY
            -- provenance is deliberately anonymous: it earns standing from
            -- forward outcomes, never from a guessed identity.
            CREATE TABLE IF NOT EXISTS notable_wallets (
                wallet TEXT PRIMARY KEY,
                label TEXT NOT NULL DEFAULT '',
                provenance TEXT NOT NULL DEFAULT 'ONCHAIN_ONLY',
                verification_source TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0,
                category TEXT NOT NULL DEFAULT 'trader',
                enabled INTEGER NOT NULL DEFAULT 1,
                anonymous_index INTEGER,
                last_verified_at INTEGER,
                updated_at INTEGER NOT NULL
            );

            -- One row per observed public trade by a monitored wallet.  Keyed by
            -- signature so a retry or a restart cannot duplicate an alert.
            CREATE TABLE IF NOT EXISTS notable_wallet_events (
                signature TEXT NOT NULL,
                wallet TEXT NOT NULL,
                mint TEXT NOT NULL,
                side TEXT NOT NULL DEFAULT 'BUY',
                chain_time INTEGER NOT NULL DEFAULT 0,
                observed_at INTEGER NOT NULL,
                amount_usd REAL,
                entry_price_usd REAL,
                entry_market_cap_usd REAL,
                detection_market_cap_usd REAL,
                freshness TEXT NOT NULL DEFAULT 'FRESH',
                alerted_at INTEGER,
                message_id INTEGER,
                payload_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (signature, wallet, mint)
            );
            CREATE INDEX IF NOT EXISTS idx_notable_events_recent
                ON notable_wallet_events(observed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_notable_events_mint
                ON notable_wallet_events(mint, chain_time DESC);

            -- Graded catalyst events, kept separate from any token claim.
            CREATE TABLE IF NOT EXISTS catalyst_events (
                event_id TEXT PRIMARY KEY,
                headline TEXT NOT NULL,
                detected_at INTEGER NOT NULL,
                occurred_at INTEGER,
                confidence TEXT NOT NULL DEFAULT 'UNVERIFIED',
                priority TEXT NOT NULL DEFAULT 'NORMAL',
                markers_json TEXT NOT NULL DEFAULT '[]',
                payload_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_catalyst_recent
                ON catalyst_events(detected_at DESC);

            -- Token <-> event connection is a DIFFERENT question from event
            -- confidence and is stored separately so the two can never merge.
            CREATE TABLE IF NOT EXISTS catalyst_token_links (
                event_id TEXT NOT NULL,
                mint TEXT NOT NULL,
                connection TEXT NOT NULL DEFAULT 'NO_EVIDENCE',
                name_similarity REAL,
                seconds_after_event INTEGER,
                official INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                PRIMARY KEY (event_id, mint)
            );
            CREATE INDEX IF NOT EXISTS idx_catalyst_links_mint
                ON catalyst_token_links(mint, created_at DESC);

            -- Published fast alerts, so a restart cannot re-ping and so a later
            -- enrichment pass can edit the original message.
            CREATE TABLE IF NOT EXISTS fast_alerts (
                alert_key TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                mint TEXT NOT NULL,
                published_at INTEGER NOT NULL,
                message_id INTEGER,
                channel_id INTEGER,
                pinged INTEGER NOT NULL DEFAULT 0,
                fingerprint TEXT NOT NULL DEFAULT '',
                enriched_at INTEGER,
                payload_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_fast_alerts_recent
                ON fast_alerts(published_at DESC);
            CREATE INDEX IF NOT EXISTS idx_fast_alerts_mint
                ON fast_alerts(mint, published_at DESC);

            CREATE TABLE IF NOT EXISTS lab_token_identity (
                mint TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                resolved_at INTEGER NOT NULL
            );

            -- SHADOW auto-trader (v2.39).  All additive; the strict PAPER lab
            -- tables above are untouched, and the two strategy families never
            -- share a row, a bankroll or a position.

            -- The forward-experiment checkpoint (section 42).  Every live
            -- result is attributable to the exact experiment that produced it.
            CREATE TABLE IF NOT EXISTS shadow_experiment (
                experiment_version TEXT PRIMARY KEY,
                started_at INTEGER NOT NULL,
                starting_bankroll_usd REAL NOT NULL,
                position_usd REAL NOT NULL,
                max_positions INTEGER NOT NULL,
                max_exposure_usd REAL NOT NULL,
                net_objective_usd REAL NOT NULL,
                config_hash TEXT NOT NULL DEFAULT '',
                bot_version TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL
            );

            -- Simulated $10 positions.  The partial unique index is the
            -- duplicate-entry lock: one open shadow position per mint per
            -- signal family, which survives a restart and a replayed signal.
            CREATE TABLE IF NOT EXISTS shadow_positions (
                position_id TEXT PRIMARY KEY,
                mint TEXT NOT NULL,
                family TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                experiment_version TEXT NOT NULL DEFAULT '',
                opened_at INTEGER NOT NULL,
                closed_at INTEGER,
                size_usd REAL NOT NULL,
                entry_price_usd REAL NOT NULL,
                entry_market_cap_usd REAL,
                realized_net_pnl_usd REAL NOT NULL DEFAULT 0,
                peak_net_pnl_usd REAL NOT NULL DEFAULT 0,
                close_reason TEXT NOT NULL DEFAULT '',
                venue TEXT NOT NULL DEFAULT 'UNKNOWN',
                fill_source TEXT NOT NULL DEFAULT '',
                graduation_state TEXT NOT NULL DEFAULT 'UNKNOWN',
                config_hash TEXT NOT NULL DEFAULT '',
                signal_json TEXT NOT NULL DEFAULT '{}',
                payload_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_shadow_positions_open_unique
                ON shadow_positions(mint, family, strategy_version)
                WHERE closed_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_shadow_positions_recent
                ON shadow_positions(opened_at DESC);
            CREATE INDEX IF NOT EXISTS idx_shadow_positions_family
                ON shadow_positions(family, opened_at DESC);

            -- The shadow partial-exit journal.  (position_id, sequence) makes a
            -- retried exit idempotent instead of double-selling.
            CREATE TABLE IF NOT EXISTS shadow_exits (
                position_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                mint TEXT NOT NULL,
                family TEXT NOT NULL DEFAULT '',
                occurred_at INTEGER NOT NULL,
                reason_code TEXT NOT NULL,
                fraction_sold REAL NOT NULL,
                gross_proceeds_usd REAL NOT NULL,
                total_cost_usd REAL NOT NULL,
                net_pnl_usd REAL NOT NULL,
                venue TEXT NOT NULL DEFAULT 'UNKNOWN',
                final INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (position_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS idx_shadow_exits_recent
                ON shadow_exits(occurred_at DESC);

            -- One simulated shadow bankroll, kept apart from the strict lab's.
            CREATE TABLE IF NOT EXISTS shadow_bankroll (
                strategy_version TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );

            -- The single post-entry observation stream every counterfactual
            -- reads (section 54).  One row per position per observation, so
            -- twelve policies cost zero extra provider requests.
            CREATE TABLE IF NOT EXISTS shadow_observations (
                position_id TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                price_usd REAL NOT NULL,
                market_cap_usd REAL,
                liquidity_usd REAL,
                volume_usd REAL,
                momentum_score REAL,
                organic_score REAL,
                buys INTEGER NOT NULL DEFAULT 0,
                sells INTEGER NOT NULL DEFAULT 0,
                independent_buyers INTEGER,
                safety_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                route_available INTEGER NOT NULL DEFAULT 1,
                smart_money_distributing INTEGER NOT NULL DEFAULT 0,
                smart_money_accumulating INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (position_id, observed_at)
            );
            CREATE INDEX IF NOT EXISTS idx_shadow_observations_time
                ON shadow_observations(position_id, observed_at);

            -- Simulated fills per venue, for the venue comparison (section 24).
            CREATE TABLE IF NOT EXISTS shadow_venue_fills (
                position_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                venue TEXT NOT NULL,
                side TEXT NOT NULL,
                filled_at INTEGER NOT NULL,
                notional_usd REAL NOT NULL DEFAULT 0,
                fill_price_usd REAL,
                reference_price_usd REAL,
                price_impact_percent REAL NOT NULL DEFAULT 0,
                slippage_bps INTEGER NOT NULL DEFAULT 0,
                fee_bps INTEGER NOT NULL DEFAULT 0,
                quote_latency_ms INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                net_pnl_usd REAL NOT NULL DEFAULT 0,
                deterioration_percent REAL,
                fill_source TEXT NOT NULL DEFAULT '',
                graduation_state TEXT NOT NULL DEFAULT 'UNKNOWN',
                considered_json TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (position_id, sequence, side)
            );
            CREATE INDEX IF NOT EXISTS idx_shadow_venue_fills_venue
                ON shadow_venue_fills(venue, filled_at DESC);

            -- Refused shadow signals, so "why did nothing happen?" is always
            -- answerable and a rejected signal is never silently lost.
            CREATE TABLE IF NOT EXISTS shadow_signal_log (
                signal_key TEXT PRIMARY KEY,
                mint TEXT NOT NULL,
                family TEXT NOT NULL,
                decided_at INTEGER NOT NULL,
                accepted INTEGER NOT NULL DEFAULT 0,
                reason_code TEXT NOT NULL DEFAULT '',
                size_usd REAL NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_shadow_signal_log_recent
                ON shadow_signal_log(decided_at DESC);
            CREATE INDEX IF NOT EXISTS idx_shadow_signal_log_family
                ON shadow_signal_log(family, decided_at DESC);

            -- Ultra-early discovery (v2.41).  All additive.

            -- The operator-visibility timeline (sections 2, 3, 52).  One row per
            -- (mint, stage), written with INSERT OR IGNORE, so every stage is
            -- write-once: the market cap an alert was actually sent at can never
            -- be rewritten during enrichment.  That immutability is the whole
            -- point -- it is what stops a late alert looking early in hindsight.
            CREATE TABLE IF NOT EXISTS alert_timeline (
                mint TEXT NOT NULL,
                stage TEXT NOT NULL,
                occurred_at INTEGER NOT NULL,
                market_cap_usd REAL,
                price_usd REAL,
                liquidity_usd REAL,
                tier TEXT NOT NULL DEFAULT '',
                edge_state TEXT NOT NULL DEFAULT '',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (mint, stage)
            );
            CREATE INDEX IF NOT EXISTS idx_alert_timeline_recent
                ON alert_timeline(occurred_at DESC);
            CREATE INDEX IF NOT EXISTS idx_alert_timeline_stage
                ON alert_timeline(stage, occurred_at DESC);

            -- Why the operator was not pinged (section 12).  Append-only, keyed
            -- so a repeated pass does not spam the same explanation.
            CREATE TABLE IF NOT EXISTS alert_suppression (
                mint TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                occurred_at INTEGER NOT NULL,
                market_cap_usd REAL,
                tier TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (mint, reason_code)
            );
            CREATE INDEX IF NOT EXISTS idx_alert_suppression_recent
                ON alert_suppression(occurred_at DESC);

            -- Durable real-world narratives, independent of any token (s21).
            CREATE TABLE IF NOT EXISTS narratives (
                narrative_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                virality TEXT NOT NULL DEFAULT 'NONE',
                first_seen_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_narratives_recent
                ON narratives(last_seen_at DESC);

            -- Graded, directional narrative -> exact-mint claims (sections 22-26).
            -- Keyed by (narrative_id, mint): a story and a mint are different
            -- things, and the link between them belongs to neither alone.
            CREATE TABLE IF NOT EXISTS narrative_links (
                narrative_id TEXT NOT NULL,
                mint TEXT NOT NULL,
                relationship TEXT NOT NULL DEFAULT 'UNRELATED',
                direction TEXT NOT NULL DEFAULT 'TOKEN_TO_STORY',
                confidence REAL NOT NULL DEFAULT 0,
                seconds_after_story INTEGER,
                payload_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (narrative_id, mint)
            );
            CREATE INDEX IF NOT EXISTS idx_narrative_links_mint
                ON narrative_links(mint);
            CREATE INDEX IF NOT EXISTS idx_narrative_links_rank
                ON narrative_links(narrative_id, confidence DESC);

            -- Trending-first alpha engine (v2.42).  Every statement here is
            -- additive and IF NOT EXISTS: no existing table is altered, no
            -- forward history is rewritten, and a restart re-runs the whole
            -- block harmlessly.  The two shadow experiments share these tables
            -- only in the sense that they share the file -- they are partitioned
            -- by strategy_version, which is a different bankroll.

            -- The primary Trending ledger (section 5).  One row per exact mint.
            -- The first_* columns are written once by INSERT OR IGNORE and then
            -- never updated, which is what makes "was the alert early?"
            -- answerable rather than reconstructable.
            CREATE TABLE IF NOT EXISTS trending_tokens (
                mint TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                symbol TEXT NOT NULL DEFAULT '',
                fomo_token_id TEXT NOT NULL DEFAULT '',
                fomo_url TEXT NOT NULL DEFAULT '',
                source_kind TEXT NOT NULL DEFAULT 'TRENDING_PROXY',
                first_seen_at INTEGER NOT NULL,
                first_rank INTEGER,
                first_market_cap_usd REAL,
                first_holder_count INTEGER,
                first_top10_percent REAL,
                current_rank INTEGER,
                best_rank INTEGER,
                current_market_cap_usd REAL,
                peak_market_cap_usd REAL,
                change_window TEXT NOT NULL DEFAULT 'CHANGE_WINDOW_UNKNOWN',
                verification TEXT NOT NULL DEFAULT 'UNKNOWN',
                entries INTEGER NOT NULL DEFAULT 1,
                seconds_on_board INTEGER NOT NULL DEFAULT 0,
                on_board INTEGER NOT NULL DEFAULT 1,
                exited_at INTEGER,
                last_observed_at INTEGER NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trending_tokens_rank
                ON trending_tokens(on_board DESC, current_rank ASC);
            CREATE INDEX IF NOT EXISTS idx_trending_tokens_recent
                ON trending_tokens(last_observed_at DESC);

            -- Raw board snapshots, so rank velocity survives a restart.
            CREATE TABLE IF NOT EXISTS trending_snapshots (
                mint TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                rank INTEGER,
                market_cap_usd REAL,
                price_usd REAL,
                liquidity_usd REAL,
                holder_count INTEGER,
                top10_percent REAL,
                displayed_change_percent REAL,
                change_window TEXT NOT NULL DEFAULT 'CHANGE_WINDOW_UNKNOWN',
                source_kind TEXT NOT NULL DEFAULT 'TRENDING_PROXY',
                PRIMARY KEY (mint, observed_at)
            );
            CREATE INDEX IF NOT EXISTS idx_trending_snapshots_time
                ON trending_snapshots(observed_at DESC);

            -- Classified Trending state changes (section 7).
            CREATE TABLE IF NOT EXISTS trending_events (
                mint TEXT NOT NULL,
                state TEXT NOT NULL,
                occurred_at INTEGER NOT NULL,
                rank INTEGER,
                rank_delta INTEGER NOT NULL DEFAULT 0,
                market_cap_usd REAL,
                move_percent REAL,
                score REAL,
                reasons_json TEXT NOT NULL DEFAULT '[]',
                payload_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (mint, state, occurred_at)
            );
            CREATE INDEX IF NOT EXISTS idx_trending_events_recent
                ON trending_events(occurred_at DESC);
            CREATE INDEX IF NOT EXISTS idx_trending_events_state
                ON trending_events(state, occurred_at DESC);

            -- HOT WATCH (sections 41-50).  One row per mint per entry window;
            -- the promotion market caps are write-once for the same reason the
            -- ledger's first_* columns are.
            CREATE TABLE IF NOT EXISTS trending_hot_watch (
                mint TEXT NOT NULL,
                entered_at INTEGER NOT NULL,
                origin TEXT NOT NULL DEFAULT 'TRENDING_NEAR_MISS',
                state TEXT NOT NULL DEFAULT 'ACTIVE',
                expires_at INTEGER NOT NULL,
                entry_score REAL NOT NULL DEFAULT 0,
                best_score REAL NOT NULL DEFAULT 0,
                last_score REAL NOT NULL DEFAULT 0,
                rechecks INTEGER NOT NULL DEFAULT 0,
                last_recheck_at INTEGER NOT NULL DEFAULT 0,
                promoted_at INTEGER,
                resolved_at INTEGER,
                hot_watch_market_cap_usd REAL,
                promotion_market_cap_usd REAL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (mint, entered_at)
            );
            CREATE INDEX IF NOT EXISTS idx_trending_hot_watch_state
                ON trending_hot_watch(state, expires_at);
            CREATE INDEX IF NOT EXISTS idx_trending_hot_watch_recent
                ON trending_hot_watch(entered_at DESC);

            -- Ingested public theses, keyed to the EXACT mint (sections 19-26).
            CREATE TABLE IF NOT EXISTS trending_theses (
                thesis_id TEXT PRIMARY KEY,
                mint TEXT NOT NULL,
                author TEXT NOT NULL,
                posted_at INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'OTHER',
                quality TEXT NOT NULL DEFAULT 'NOISE',
                timing TEXT NOT NULL DEFAULT 'TIMELY',
                specificity INTEGER NOT NULL DEFAULT 0,
                cluster_id TEXT NOT NULL DEFAULT '',
                cluster_leader INTEGER NOT NULL DEFAULT 1,
                market_cap_at_thesis_usd REAL,
                penalties_json TEXT NOT NULL DEFAULT '[]',
                text TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trending_theses_mint
                ON trending_theses(mint, posted_at DESC);
            CREATE INDEX IF NOT EXISTS idx_trending_theses_author
                ON trending_theses(author, posted_at DESC);

            -- Forward-measured thesis-author reputation (section 25).  This is
            -- an outcome record, not a popularity score.
            CREATE TABLE IF NOT EXISTS trending_thesis_authors (
                author TEXT PRIMARY KEY,
                sample INTEGER NOT NULL DEFAULT 0,
                avg_forward_move_percent REAL,
                avg_mfe_percent REAL,
                avg_mae_percent REAL,
                severe_failures INTEGER NOT NULL DEFAULT 0,
                rug_exposures INTEGER NOT NULL DEFAULT 0,
                late_theses INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            );

            -- The token's own About section and how far it could be checked
            -- (sections 16-18).  Claims and corroboration are separate columns
            -- on purpose: they must never be rendered as one thing.
            CREATE TABLE IF NOT EXISTS trending_about (
                mint TEXT PRIMARY KEY,
                summary TEXT NOT NULL DEFAULT '',
                claims_json TEXT NOT NULL DEFAULT '[]',
                website TEXT NOT NULL DEFAULT '',
                has_official_claim INTEGER NOT NULL DEFAULT 0,
                external_state TEXT NOT NULL DEFAULT 'NOT_APPLICABLE',
                token_link TEXT NOT NULL DEFAULT 'NO_CLAIM',
                mentions_exact_mint INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL
            );

            -- Trending latency stamps (sections 78-79).  Write-once per stage.
            CREATE TABLE IF NOT EXISTS trending_latency (
                mint TEXT NOT NULL,
                stage TEXT NOT NULL,
                occurred_at INTEGER NOT NULL,
                market_cap_usd REAL,
                PRIMARY KEY (mint, stage)
            );
            CREATE INDEX IF NOT EXISTS idx_trending_latency_recent
                ON trending_latency(occurred_at DESC);

            -- Why a Trending candidate did not ping (section 91).
            CREATE TABLE IF NOT EXISTS trending_suppression (
                mint TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                occurred_at INTEGER NOT NULL,
                score REAL,
                market_cap_usd REAL,
                detail TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (mint, reason_code, occurred_at)
            );
            CREATE INDEX IF NOT EXISTS idx_trending_suppression_recent
                ON trending_suppression(occurred_at DESC);

            -- Missed Trending opportunities, graded after the fact (s80-82).
            CREATE TABLE IF NOT EXISTS trending_missed (
                mint TEXT NOT NULL,
                miss_class TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                market_cap_at_observation_usd REAL,
                peak_market_cap_usd REAL,
                move_percent REAL,
                suppression_reason TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (mint, miss_class)
            );
            CREATE INDEX IF NOT EXISTS idx_trending_missed_recent
                ON trending_missed(observed_at DESC);

            -- Terminal-style trenches intelligence (v2.43).  Additive and
            -- IF NOT EXISTS throughout: no existing table is altered, no forward
            -- history is touched, and a restart re-runs the block harmlessly.

            -- One row per Pump.fun mint the engine has ever observed.  The
            -- first_* columns are write-once by INSERT OR IGNORE and never
            -- appear in an UPDATE SET clause, exactly as the Trending ledger
            -- does, so "when did we first see it and at what price" survives
            -- every later enrichment pass.
            CREATE TABLE IF NOT EXISTS pump_tokens (
                mint TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                symbol TEXT NOT NULL DEFAULT '',
                creator TEXT NOT NULL DEFAULT '',
                created_at INTEGER,
                first_observed_at INTEGER NOT NULL,
                first_observed_source TEXT NOT NULL DEFAULT '',
                first_market_cap_usd REAL,
                first_bonding_percent REAL,
                stage TEXT NOT NULL DEFAULT 'UNKNOWN',
                bonding_percent REAL,
                market_cap_usd REAL,
                liquidity_usd REAL,
                holders INTEGER,
                top10_percent REAL,
                graduated_at INTEGER,
                graduation_market_cap_usd REAL,
                special_mode TEXT NOT NULL DEFAULT '',
                last_observed_at INTEGER NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pump_tokens_stage
                ON pump_tokens(stage, bonding_percent DESC);
            CREATE INDEX IF NOT EXISTS idx_pump_tokens_recent
                ON pump_tokens(first_observed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_pump_tokens_creator
                ON pump_tokens(creator);

            -- The multi-timeframe observation stream.  Every window in
            -- sections 9-11 is computed from these rows, so they are the single
            -- source the 1m/5m/15m/30m/1h numbers all derive from.
            CREATE TABLE IF NOT EXISTS pump_observations (
                mint TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                price_usd REAL,
                market_cap_usd REAL,
                liquidity_usd REAL,
                bonding_percent REAL,
                buys INTEGER NOT NULL DEFAULT 0,
                sells INTEGER NOT NULL DEFAULT 0,
                volume_usd REAL NOT NULL DEFAULT 0,
                unique_buyers INTEGER,
                unique_sellers INTEGER,
                independent_buyers INTEGER,
                holders INTEGER,
                PRIMARY KEY (mint, observed_at)
            );
            CREATE INDEX IF NOT EXISTS idx_pump_observations_time
                ON pump_observations(mint, observed_at DESC);

            -- Holder concentration over time (section 21).  A single snapshot
            -- cannot say whether ownership is broadening or concentrating.
            CREATE TABLE IF NOT EXISTS pump_holder_snapshots (
                mint TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                top10_percent REAL,
                top20_percent REAL,
                largest_holder_percent REAL,
                infrastructure_percent REAL,
                holder_count INTEGER,
                PRIMARY KEY (mint, observed_at)
            );
            CREATE INDEX IF NOT EXISTS idx_pump_holder_snapshots_time
                ON pump_holder_snapshots(mint, observed_at DESC);

            -- Creator intelligence, kept per creator rather than per token so a
            -- dev's observable record accumulates across their launches.
            CREATE TABLE IF NOT EXISTS pump_dev_profiles (
                wallet TEXT PRIMARY KEY,
                tokens_created INTEGER NOT NULL DEFAULT 0,
                graduated INTEGER NOT NULL DEFAULT 0,
                collapsed INTEGER NOT NULL DEFAULT 0,
                retained_liquidity INTEGER NOT NULL DEFAULT 0,
                history_label TEXT NOT NULL DEFAULT 'DEV_HISTORY_UNKNOWN',
                funding_source_type TEXT NOT NULL DEFAULT 'UNKNOWN',
                funding_source_wallet TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL
            );

            -- Per-token dev holding, bundle exposure and participant quality.
            CREATE TABLE IF NOT EXISTS pump_intel (
                mint TEXT PRIMARY KEY,
                dev_wallet TEXT NOT NULL DEFAULT '',
                dev_initial_percent REAL,
                dev_current_percent REAL,
                dev_posture TEXT NOT NULL DEFAULT 'UNKNOWN',
                bundle_risk TEXT NOT NULL DEFAULT 'UNKNOWN',
                bundle_count INTEGER NOT NULL DEFAULT 0,
                bundle_supply_percent REAL,
                bundle_distributing INTEGER NOT NULL DEFAULT 0,
                independent_buyers INTEGER,
                unique_buyers INTEGER,
                clustered_percent REAL,
                fresh_wallet_percent REAL,
                related_percent REAL,
                metadata_reuse TEXT NOT NULL DEFAULT 'NONE',
                payload_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL
            );

            -- Metadata fingerprints, so reuse across mints is detectable
            -- (section 27) without retaining third-party text.
            CREATE TABLE IF NOT EXISTS pump_metadata_prints (
                mint TEXT NOT NULL,
                field TEXT NOT NULL,
                digest TEXT NOT NULL,
                created_at INTEGER,
                PRIMARY KEY (mint, field)
            );
            CREATE INDEX IF NOT EXISTS idx_pump_metadata_prints_digest
                ON pump_metadata_prints(field, digest);

            -- Our own public Trending ranking over time.
            CREATE TABLE IF NOT EXISTS public_trend_ranks (
                mint TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                rank INTEGER NOT NULL,
                score REAL NOT NULL,
                shape TEXT NOT NULL DEFAULT '',
                momentum_curve TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT 'PUBLIC_TRENDING_MODEL',
                PRIMARY KEY (mint, observed_at)
            );
            CREATE INDEX IF NOT EXISTS idx_public_trend_ranks_time
                ON public_trend_ranks(observed_at DESC, rank ASC);

            -- Which lanes nominated a mint, for the consensus count (s33-34).
            CREATE TABLE IF NOT EXISTS discovery_nominations (
                mint TEXT NOT NULL,
                lane TEXT NOT NULL,
                source_kind TEXT NOT NULL DEFAULT '',
                first_at INTEGER NOT NULL,
                last_at INTEGER NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (mint, lane)
            );
            CREATE INDEX IF NOT EXISTS idx_discovery_nominations_recent
                ON discovery_nominations(last_at DESC);

            -- Trench alerts and why they were or were not sent (s35, 82).
            CREATE TABLE IF NOT EXISTS trench_alerts (
                mint TEXT NOT NULL,
                tier TEXT NOT NULL,
                occurred_at INTEGER NOT NULL,
                score REAL,
                stage TEXT NOT NULL DEFAULT '',
                bonding_percent REAL,
                market_cap_usd REAL,
                reasons_json TEXT NOT NULL DEFAULT '[]',
                payload_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (mint, tier, occurred_at)
            );
            CREATE INDEX IF NOT EXISTS idx_trench_alerts_recent
                ON trench_alerts(occurred_at DESC);

            CREATE TABLE IF NOT EXISTS trench_suppression (
                mint TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                occurred_at INTEGER NOT NULL,
                score REAL,
                stage TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (mint, reason_code, occurred_at)
            );
            CREATE INDEX IF NOT EXISTS idx_trench_suppression_recent
                ON trench_suppression(occurred_at DESC);

            -- Time-to-first-observation, the v2.43 latency question (s73, 79).
            CREATE TABLE IF NOT EXISTS pump_discovery_latency (
                mint TEXT PRIMARY KEY,
                created_at INTEGER,
                observed_at INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                latency_seconds INTEGER,
                market_cap_at_observation_usd REAL
            );
            CREATE INDEX IF NOT EXISTS idx_pump_discovery_latency_recent
                ON pump_discovery_latency(observed_at DESC);

            -- Administrator-supplied benchmark observations (section 83).
            -- Manually captured only; nothing in this codebase fetches them.
            CREATE TABLE IF NOT EXISTS benchmark_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                board_name TEXT NOT NULL DEFAULT '',
                captured_at INTEGER NOT NULL,
                captured_by TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'ADMIN_MANUAL_OBSERVATION',
                entries_json TEXT NOT NULL DEFAULT '[]',
                comparison_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_benchmark_snapshots_recent
                ON benchmark_snapshots(captured_at DESC);
            """
        )
        await self._migrate_pump_launch_status_constraint()
        await self._ensure_column(
            "provider_call_usage", "calls_skipped", "INTEGER NOT NULL DEFAULT 0"
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
        for column, definition in (
            ("chain_created_at", "INTEGER"),
            ("pair_created_at", "INTEGER"),
            ("radar_first_seen_at", "INTEGER"),
            ("first_market_data_at", "INTEGER"),
            ("first_research_eligible_at", "INTEGER"),
            ("first_discord_visible_at", "INTEGER"),
            ("entry_eligible_at", "INTEGER"),
            ("strong_alert_at", "INTEGER"),
            ("first_visible_market_cap_usd", "REAL"),
            ("entry_market_cap_usd", "REAL"),
            ("peak_market_cap_usd", "REAL"),
            # v2.35 funnel columns. Added, never replacing anything, so every
            # existing runner row and forward observation survives the upgrade.
            ("stage", "TEXT NOT NULL DEFAULT 'RAW_DISCOVERY'"),
            ("best_stage", "TEXT NOT NULL DEFAULT 'RAW_DISCOVERY'"),
            ("qualified_at", "INTEGER"),
            ("qualified_market_cap_usd", "REAL"),
            ("heating_at", "INTEGER"),
            ("momentum_score", "REAL"),
            ("opportunity_score", "REAL"),
            ("organic_score", "REAL"),
        ):
            await self._ensure_column("runner_candidates", column, definition)
        await self.db.execute(
            """
            UPDATE runner_candidates
            SET pair_created_at = COALESCE(pair_created_at, graduated_at),
                graduated_at = NULL
            WHERE graduation_source LIKE 'DEX_PAIR_CREATED_PROXY%'
            """
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
        await self.db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES ('paper_trial_started_at', ?)",
            (str(now),),
        )
        await self.db.commit()

    async def _migrate_pump_launch_status_constraint(self) -> None:
        """Expand the old CHECK constraint without deleting any launch history."""

        cursor = await self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'pump_launches'"
        )
        row = await cursor.fetchone()
        sql = str(row["sql"] or "") if row else ""
        if not sql or "UNKNOWN_RESULT" in sql:
            return
        await self.db.executescript(
            """
            ALTER TABLE pump_launches RENAME TO pump_launches_v230;
            CREATE TABLE pump_launches (
                alert_key TEXT PRIMARY KEY,
                source_url TEXT NOT NULL,
                headline TEXT NOT NULL,
                name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                score INTEGER NOT NULL,
                initial_buy_sol REAL NOT NULL,
                requested_by TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'RESERVED', 'SUBMITTED', 'CONFIRMED', 'FAILED', 'UNKNOWN_RESULT'
                    )
                ),
                mint TEXT,
                signature TEXT,
                metadata_uri TEXT,
                error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            INSERT INTO pump_launches SELECT * FROM pump_launches_v230;
            DROP TABLE pump_launches_v230;
            CREATE INDEX IF NOT EXISTS idx_pump_launches_time
                ON pump_launches(created_at DESC);
            """
        )

    async def reserve_daily_api_request(
        self,
        *,
        provider: str,
        operation: str,
        usage_day: str,
        request_limit: int,
    ) -> tuple[bool, int]:
        """Persistently reserve one paid API request without exceeding its daily cap."""

        now = int(time.time())
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self.db.execute(
                    """
                    INSERT OR IGNORE INTO api_usage_daily(
                        provider, operation, usage_day, request_count, updated_at
                    ) VALUES (?, ?, ?, 0, ?)
                    """,
                    (provider, operation, usage_day, now),
                )
                cursor = await self.db.execute(
                    """
                    UPDATE api_usage_daily
                    SET request_count = request_count + 1, updated_at = ?
                    WHERE provider = ? AND operation = ? AND usage_day = ?
                      AND request_count < ?
                    """,
                    (now, provider, operation, usage_day, request_limit),
                )
                count_cursor = await self.db.execute(
                    """
                    SELECT request_count FROM api_usage_daily
                    WHERE provider = ? AND operation = ? AND usage_day = ?
                    """,
                    (provider, operation, usage_day),
                )
                row = await count_cursor.fetchone()
                count = int(row["request_count"] if row else 0)
                await self.db.commit()
                return cursor.rowcount > 0, count
            except Exception:
                await self.db.rollback()
                raise

    async def daily_api_request_count(
        self,
        *,
        provider: str,
        operation: str,
        usage_day: str,
    ) -> int:
        cursor = await self.db.execute(
            """
            SELECT request_count FROM api_usage_daily
            WHERE provider = ? AND operation = ? AND usage_day = ?
            """,
            (provider, operation, usage_day),
        )
        row = await cursor.fetchone()
        return int(row["request_count"] if row else 0)

    async def reserve_x_verification(
        self,
        *,
        usage_day: str,
        period_id: str,
        fingerprint: str,
        context: str,
        query: str,
        max_posts: int,
        request_limit: int,
        verification_limit: int,
        daily_budget_usd: Decimal,
        total_budget_usd: Decimal,
        post_unit_cost_usd: Decimal,
        guard_enabled: bool,
    ) -> tuple[int | None, str | None]:
        """Atomically reserve one targeted X search and its worst-case Post reads."""

        now = int(time.time())
        ceiling = Decimal(max_posts) * post_unit_cost_usd
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self.db.execute(
                    """
                    UPDATE x_budget_verifications
                    SET state = 'FAILED', reserved_estimate_usd = 0,
                        error_category = 'STALE LOCAL RESERVATION RECOVERED',
                        completed_at = ?
                    WHERE state = 'RESERVED' AND started_at < ?
                    """,
                    (now, now - 300),
                )
                duplicate_cursor = await self.db.execute(
                    """
                    SELECT 1 FROM x_budget_verifications
                    WHERE usage_day = ? AND fingerprint = ? AND state = 'RESERVED'
                    LIMIT 1
                    """,
                    (usage_day, fingerprint),
                )
                if await duplicate_cursor.fetchone() is not None:
                    await self.db.rollback()
                    return None, "X VERIFICATION SKIPPED — SAME QUERY ALREADY IN PROGRESS"
                day_cursor = await self.db.execute(
                    """
                    SELECT COUNT(*) AS verifications,
                           COALESCE(SUM(CASE WHEN http_requests > 0
                                             THEN http_requests ELSE 1 END), 0) AS attempts,
                           COALESCE(SUM(
                               CASE WHEN state = 'RESERVED'
                                    THEN reserved_estimate_usd
                                    ELSE estimated_spend_usd END
                           ), 0) AS guarded_spend
                    FROM x_budget_verifications
                    WHERE usage_day = ?
                    """,
                    (usage_day,),
                )
                day = await day_cursor.fetchone()
                period_cursor = await self.db.execute(
                    """
                    SELECT COALESCE(SUM(
                               CASE WHEN state = 'RESERVED'
                                    THEN reserved_estimate_usd
                                    ELSE estimated_spend_usd END
                           ), 0) AS guarded_spend
                    FROM x_budget_verifications
                    WHERE period_id = ?
                    """,
                    (period_id,),
                )
                period = await period_cursor.fetchone()
                attempts = int(day["attempts"] if day else 0)
                verifications = int(day["verifications"] if day else 0)
                day_spend = _d(day["guarded_spend"] if day else 0)
                period_spend = _d(period["guarded_spend"] if period else 0)
                reason: str | None = None
                if attempts >= request_limit:
                    reason = "X VERIFICATION SKIPPED — REQUEST BACKSTOP REACHED"
                elif verifications >= verification_limit:
                    reason = "X VERIFICATION SKIPPED — DAILY VERIFICATION CAP REACHED"
                elif guard_enabled and day_spend + ceiling > daily_budget_usd:
                    reason = "X VERIFICATION SKIPPED — DAILY BUDGET REACHED"
                elif guard_enabled and period_spend + ceiling > total_budget_usd:
                    reason = "X VERIFICATION SKIPPED — EXPERIMENT BUDGET REACHED"
                if reason:
                    await self.db.rollback()
                    return None, reason
                cursor = await self.db.execute(
                    """
                    INSERT INTO x_budget_verifications(
                        usage_day, period_id, fingerprint, context, query, state,
                        max_posts, reserved_estimate_usd, started_at
                    ) VALUES (?, ?, ?, ?, ?, 'RESERVED', ?, ?, ?)
                    """,
                    (
                        usage_day,
                        period_id,
                        fingerprint,
                        context,
                        query,
                        max_posts,
                        float(ceiling),
                        now,
                    ),
                )
                await self.db.commit()
                return int(cursor.lastrowid), None
            except Exception:
                await self.db.rollback()
                raise

    async def reserve_x_user_resources(
        self,
        *,
        verification_id: int,
        user_ids: tuple[str, ...],
        daily_budget_usd: Decimal,
        total_budget_usd: Decimal,
        user_unit_cost_usd: Decimal,
        guard_enabled: bool,
    ) -> tuple[tuple[str, ...], str | None]:
        """Reserve only User resources not already counted locally for this UTC/local day."""

        if not user_ids:
            return (), None
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                row_cursor = await self.db.execute(
                    """
                    SELECT usage_day, period_id, state FROM x_budget_verifications
                    WHERE id = ?
                    """,
                    (verification_id,),
                )
                row = await row_cursor.fetchone()
                if row is None or row["state"] != "RESERVED":
                    await self.db.rollback()
                    return (), "X VERIFICATION RESERVATION IS NOT ACTIVE"
                placeholders = ",".join("?" for _ in user_ids)
                existing_cursor = await self.db.execute(
                    f"""
                    SELECT resource_id FROM x_budget_resources
                    WHERE usage_day = ? AND resource_type = 'user'
                      AND resource_id IN ({placeholders})
                    """,
                    (row["usage_day"], *user_ids),
                )
                existing = {str(item["resource_id"]) for item in await existing_cursor.fetchall()}
                billable = tuple(item for item in user_ids if item not in existing)
                ceiling = Decimal(len(billable)) * user_unit_cost_usd
                day_cursor = await self.db.execute(
                    """
                    SELECT COALESCE(SUM(
                               CASE WHEN state = 'RESERVED'
                                    THEN reserved_estimate_usd
                                    ELSE estimated_spend_usd END
                           ), 0) AS guarded_spend
                    FROM x_budget_verifications WHERE usage_day = ?
                    """,
                    (row["usage_day"],),
                )
                period_cursor = await self.db.execute(
                    """
                    SELECT COALESCE(SUM(
                               CASE WHEN state = 'RESERVED'
                                    THEN reserved_estimate_usd
                                    ELSE estimated_spend_usd END
                           ), 0) AS guarded_spend
                    FROM x_budget_verifications WHERE period_id = ?
                    """,
                    (row["period_id"],),
                )
                day = await day_cursor.fetchone()
                period = await period_cursor.fetchone()
                day_spend = _d(day["guarded_spend"] if day else 0)
                period_spend = _d(period["guarded_spend"] if period else 0)
                if guard_enabled and day_spend + ceiling > daily_budget_usd:
                    await self.db.rollback()
                    return (), "X USER HYDRATION SKIPPED — DAILY BUDGET REACHED"
                if guard_enabled and period_spend + ceiling > total_budget_usd:
                    await self.db.rollback()
                    return (), "X USER HYDRATION SKIPPED — EXPERIMENT BUDGET REACHED"
                await self.db.execute(
                    """
                    UPDATE x_budget_verifications
                    SET reserved_estimate_usd = reserved_estimate_usd + ?
                    WHERE id = ?
                    """,
                    (float(ceiling), verification_id),
                )
                await self.db.commit()
                return billable, None
            except Exception:
                await self.db.rollback()
                raise

    async def record_x_resources(
        self,
        *,
        verification_id: int,
        resource_type: str,
        resource_ids: tuple[str, ...],
        unit_cost_usd: Decimal,
    ) -> int:
        """Record unique daily resources and return the newly counted quantity."""

        ids = tuple(dict.fromkeys(item for item in resource_ids if item))
        now = int(time.time())
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                row_cursor = await self.db.execute(
                    "SELECT usage_day, period_id FROM x_budget_verifications WHERE id = ?",
                    (verification_id,),
                )
                row = await row_cursor.fetchone()
                if row is None:
                    raise ValueError("unknown X verification reservation")
                added = 0
                for resource_id in ids:
                    cursor = await self.db.execute(
                        """
                        INSERT OR IGNORE INTO x_budget_resources(
                            usage_day, period_id, resource_type, resource_id,
                            estimated_cost_usd, first_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row["usage_day"],
                            row["period_id"],
                            resource_type,
                            resource_id,
                            float(unit_cost_usd),
                            now,
                        ),
                    )
                    added += max(0, cursor.rowcount)
                column = "post_resources" if resource_type == "post" else "user_resources"
                await self.db.execute(
                    f"""
                    UPDATE x_budget_verifications
                    SET {column} = {column} + ?,
                        estimated_spend_usd = estimated_spend_usd + ?
                    WHERE id = ?
                    """,
                    (added, float(Decimal(added) * unit_cost_usd), verification_id),
                )
                await self.db.commit()
                return added
            except Exception:
                await self.db.rollback()
                raise

    async def finish_x_verification(
        self,
        *,
        verification_id: int,
        status_code: int | None,
        free_score: int | None = None,
        final_score: int | None = None,
        outcome: str | None = None,
        http_requests: int = 1,
    ) -> None:
        async with self._write_lock:
            await self.db.execute(
                """
                UPDATE x_budget_verifications
                SET state = 'COMPLETED', reserved_estimate_usd = estimated_spend_usd,
                    status_code = ?, free_score = COALESCE(?, free_score),
                    final_score = COALESCE(?, final_score), outcome = COALESCE(?, outcome),
                    http_requests = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status_code,
                    free_score,
                    final_score,
                    outcome,
                    http_requests,
                    int(time.time()),
                    verification_id,
                ),
            )
            await self.db.commit()

    async def fail_x_verification(
        self,
        *,
        verification_id: int,
        status_code: int | None,
        error_category: str,
        http_requests: int = 1,
    ) -> None:
        async with self._write_lock:
            await self.db.execute(
                """
                UPDATE x_budget_verifications
                SET state = 'FAILED', reserved_estimate_usd = 0,
                    estimated_spend_usd = 0, status_code = ?, error_category = ?,
                    http_requests = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status_code,
                    error_category[:160],
                    http_requests,
                    int(time.time()),
                    verification_id,
                ),
            )
            await self.db.commit()

    async def update_x_verification_outcome(
        self,
        *,
        verification_id: int,
        free_score: int,
        final_score: int,
        outcome: str,
    ) -> None:
        async with self._write_lock:
            await self.db.execute(
                """
                UPDATE x_budget_verifications
                SET free_score = ?, final_score = ?, outcome = ? WHERE id = ?
                """,
                (free_score, final_score, outcome[:80], verification_id),
            )
            await self.db.commit()

    async def x_budget_status(self, *, usage_day: str, period_id: str) -> dict[str, Any]:
        day_cursor = await self.db.execute(
            """
            SELECT COUNT(*) AS verifications,
                   COALESCE(SUM(http_requests), 0) AS requests,
                   MAX(CASE WHEN state = 'COMPLETED' THEN completed_at END) AS last_success,
                   MAX(CASE WHEN state = 'FAILED' THEN completed_at END) AS last_failure,
                   SUM(CASE WHEN outcome = 'UPGRADED' THEN 1 ELSE 0 END) AS upgraded,
                   SUM(CASE WHEN outcome = 'WEAK' THEN 1 ELSE 0 END) AS weak,
                   AVG(CASE WHEN free_score IS NOT NULL THEN free_score END) AS avg_free,
                   AVG(CASE WHEN final_score IS NOT NULL THEN final_score END) AS avg_final
            FROM x_budget_verifications WHERE usage_day = ?
            """,
            (usage_day,),
        )
        day = await day_cursor.fetchone()
        resource_cursor = await self.db.execute(
            """
            SELECT resource_type, COUNT(*) AS resources,
                   COALESCE(SUM(estimated_cost_usd), 0) AS spend
            FROM x_budget_resources WHERE usage_day = ? GROUP BY resource_type
            """,
            (usage_day,),
        )
        resource_rows = await resource_cursor.fetchall()
        period_cursor = await self.db.execute(
            """
            SELECT COALESCE(SUM(estimated_cost_usd), 0) AS spend
            FROM x_budget_resources WHERE period_id = ?
            """,
            (period_id,),
        )
        period = await period_cursor.fetchone()
        error_cursor = await self.db.execute(
            """
            SELECT error_category FROM x_budget_verifications
            WHERE usage_day = ? AND error_category IS NOT NULL
            ORDER BY completed_at DESC LIMIT 1
            """,
            (usage_day,),
        )
        error = await error_cursor.fetchone()
        resources = {str(row["resource_type"]): row for row in resource_rows}
        post_row = resources.get("post")
        user_row = resources.get("user")
        return {
            "verifications": int(day["verifications"] or 0),
            "requests": int(day["requests"] or 0),
            "post_resources": int(post_row["resources"] if post_row else 0),
            "user_resources": int(user_row["resources"] if user_row else 0),
            "estimated_spend_today": sum(
                (_d(row["spend"] or 0) for row in resource_rows),
                start=Decimal("0"),
            ),
            "estimated_spend_period": _d(period["spend"] if period else 0),
            "last_success": int(day["last_success"]) if day["last_success"] else None,
            "last_failure": int(day["last_failure"]) if day["last_failure"] else None,
            "last_error": str(error["error_category"]) if error else None,
            "upgraded": int(day["upgraded"] or 0),
            "weak": int(day["weak"] or 0),
            "average_free_score": (
                Decimal(str(day["avg_free"])).quantize(Decimal("0.01"))
                if day["avg_free"] is not None
                else None
            ),
            "average_final_score": (
                Decimal(str(day["avg_final"])).quantize(Decimal("0.01"))
                if day["avg_final"] is not None
                else None
            ),
        }

    async def cached_x_users(
        self, *, user_ids: tuple[str, ...], minimum_fetched_at: int
    ) -> dict[str, dict[str, Any]]:
        if not user_ids:
            return {}
        placeholders = ",".join("?" for _ in user_ids)
        cursor = await self.db.execute(
            f"""
            SELECT user_id, payload_json FROM x_user_cache
            WHERE fetched_at >= ? AND user_id IN ({placeholders})
            """,
            (minimum_fetched_at, *user_ids),
        )
        result: dict[str, dict[str, Any]] = {}
        for row in await cursor.fetchall():
            try:
                payload = json.loads(str(row["payload_json"]))
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                result[str(row["user_id"])] = payload
        return result

    async def cache_x_users(self, users: tuple[dict[str, Any], ...], *, fetched_at: int) -> None:
        async with self._write_lock:
            for user in users:
                user_id = str(user.get("id") or "")
                if not user_id:
                    continue
                await self.db.execute(
                    """
                    INSERT INTO x_user_cache(user_id, payload_json, fetched_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        fetched_at = excluded.fetched_at
                    """,
                    (user_id, json.dumps(user, separators=(",", ":")), fetched_at),
                )
            await self.db.commit()

    async def cached_x_snapshot(self, *, fingerprint: str, now: int) -> str | None:
        cursor = await self.db.execute(
            """
            SELECT snapshot_json FROM x_verification_cache
            WHERE fingerprint = ? AND expires_at >= ?
            """,
            (fingerprint, now),
        )
        row = await cursor.fetchone()
        return str(row["snapshot_json"]) if row else None

    async def cache_x_snapshot(
        self,
        *,
        fingerprint: str,
        query: str,
        snapshot_json: str,
        fetched_at: int,
        expires_at: int,
    ) -> None:
        async with self._write_lock:
            await self.db.execute(
                """
                INSERT INTO x_verification_cache(
                    fingerprint, query, snapshot_json, fetched_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    query = excluded.query,
                    snapshot_json = excluded.snapshot_json,
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at
                """,
                (fingerprint, query, snapshot_json, fetched_at, expires_at),
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

    async def recent_verified_token_buy_evidence(
        self,
        token_mint: str,
        cutoff: int,
    ) -> dict[str, Any]:
        """Summarize only the bot's financially verified tracked-wallet buys."""

        cursor = await self.db.execute(
            """
            SELECT
                s.trader_address,
                t.alias,
                MIN(s.block_time) AS earliest_buy,
                SUM(COALESCE(s.usd_value, 0)) AS buy_value
            FROM swaps AS s
            JOIN tracked_traders AS t ON t.address = s.trader_address
            JOIN discovery_wallets AS d ON d.address = s.trader_address
            WHERE s.token_mint = ? AND s.side = 'BUY' AND s.block_time >= ?
              AND t.enabled = 1 AND d.qualified = 1
            GROUP BY s.trader_address, t.alias
            ORDER BY earliest_buy ASC
            """,
            (token_mint, cutoff),
        )
        rows = await cursor.fetchall()
        values = [_d(row["buy_value"]) for row in rows]
        total = sum(values, Decimal("0"))
        largest_percent = max(values) / total * Decimal("100") if values and total > 0 else None
        return {
            "wallets": tuple(str(row["alias"]) for row in rows),
            "wallet_addresses": tuple(str(row["trader_address"]) for row in rows),
            "buyer_first_seen_at": {
                str(row["trader_address"]): int(row["earliest_buy"]) for row in rows
            },
            "unique_buyers": len(rows),
            "earliest_buy_at": (min(int(row["earliest_buy"]) for row in rows) if rows else None),
            "largest_buyer_percent": largest_percent,
            "scope": "financially verified tracked wallets only",
        }

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

    async def mark_pump_launch_unknown(self, alert_key: str, error: str) -> None:
        """Keep the reservation locked when the external submission result is unknown."""

        await self.db.execute(
            """
            UPDATE pump_launches
            SET status = 'UNKNOWN_RESULT', error = ?, updated_at = ?
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
              AND status IN ('RESERVED', 'SUBMITTED', 'CONFIRMED', 'UNKNOWN_RESULT')
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

    async def launch_reservation_health(self) -> tuple[bool, int, int]:
        cursor = await self.db.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'RESERVED' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status = 'UNKNOWN_RESULT' THEN 1 ELSE 0 END) AS unknown
            FROM pump_launches
            """
        )
        row = await cursor.fetchone()
        return True, int(row["pending"] or 0), int(row["unknown"] or 0)

    async def pump_launch_identity_exists(self, *, name: str, symbol: str) -> bool:
        cursor = await self.db.execute(
            """
            SELECT 1 FROM pump_launches
            WHERE name = ? COLLATE NOCASE OR symbol = ? COLLATE NOCASE
            LIMIT 1
            """,
            (name, symbol),
        )
        return await cursor.fetchone() is not None

    async def cache_launch_candidate(
        self,
        *,
        cluster_key: str,
        alert_key: str,
        payload_json: str,
        headline: str,
        source_url: str,
        category: str,
        score: int,
        verdict: str,
        evaluated_at: int,
        expires_at: int,
    ) -> None:
        """Persist the strongest recent version of one narrative cluster."""

        await self.db.execute(
            """
            INSERT INTO launch_candidates(
                cluster_key, alert_key, payload_json, headline, source_url,
                category, score, verdict, evaluated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cluster_key) DO UPDATE SET
                alert_key = CASE
                    WHEN excluded.score >= launch_candidates.score
                    THEN excluded.alert_key ELSE launch_candidates.alert_key END,
                payload_json = CASE
                    WHEN excluded.score >= launch_candidates.score
                    THEN excluded.payload_json ELSE launch_candidates.payload_json END,
                headline = CASE
                    WHEN excluded.score >= launch_candidates.score
                    THEN excluded.headline ELSE launch_candidates.headline END,
                source_url = CASE
                    WHEN excluded.score >= launch_candidates.score
                    THEN excluded.source_url ELSE launch_candidates.source_url END,
                category = CASE
                    WHEN excluded.score >= launch_candidates.score
                    THEN excluded.category ELSE launch_candidates.category END,
                score = MAX(launch_candidates.score, excluded.score),
                verdict = CASE
                    WHEN excluded.score >= launch_candidates.score
                    THEN excluded.verdict ELSE launch_candidates.verdict END,
                evaluated_at = MAX(launch_candidates.evaluated_at, excluded.evaluated_at),
                expires_at = MAX(launch_candidates.expires_at, excluded.expires_at)
            """,
            (
                cluster_key,
                alert_key,
                payload_json,
                headline[:1000],
                source_url[:1000],
                category[:80],
                score,
                verdict[:80],
                evaluated_at,
                expires_at,
            ),
        )
        await self.db.execute("DELETE FROM launch_candidates WHERE expires_at < ?", (evaluated_at,))
        await self.db.commit()

    async def recent_launch_candidate_payloads(
        self,
        *,
        now: int,
        limit: int,
    ) -> list[str]:
        cursor = await self.db.execute(
            """
            SELECT payload_json FROM launch_candidates
            WHERE expires_at >= ?
            ORDER BY score DESC, evaluated_at DESC
            LIMIT ?
            """,
            (now, max(1, min(50, limit))),
        )
        return [str(row["payload_json"]) for row in await cursor.fetchall()]

    async def launch_candidate_stats(self, *, start_at: int, end_at: int) -> dict[str, Any]:
        cursor = await self.db.execute(
            """
            SELECT COUNT(*) AS evaluated, MAX(score) AS highest,
                   MAX(evaluated_at) AS last_evaluated
            FROM launch_candidates
            WHERE evaluated_at >= ? AND evaluated_at < ?
            """,
            (start_at, end_at),
        )
        row = await cursor.fetchone()
        return {
            "evaluated": int(row["evaluated"] or 0),
            "highest": int(row["highest"] or 0),
            "last_evaluated": int(row["last_evaluated"] or 0) or None,
        }

    async def store_runner_candidate(
        self,
        candidate: RunnerCandidate,
        *,
        payload_json: str,
        snapshot_json: str,
    ) -> bool:
        """Persist an immutable first-seen row plus one time-T evidence snapshot."""

        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    "SELECT payload_json FROM runner_candidates WHERE mint = ?",
                    (candidate.mint,),
                )
                existing = await cursor.fetchone()
                is_new = existing is None
                if existing is not None:
                    # The immutable time-T baseline is database-owned. Even if a
                    # caller accidentally supplies a newly constructed ``first``
                    # snapshot during refresh, never let future evidence rewrite
                    # the original detection inputs used for outcome measurement.
                    existing_payload = json.loads(str(existing["payload_json"]))
                    updated_payload = json.loads(payload_json)
                    if str(existing_payload.get("graduation_source") or "").startswith(
                        "DEX_PAIR_CREATED_PROXY"
                    ):
                        existing_payload["pair_created_at"] = (
                            existing_payload.get("pair_created_at")
                            or existing_payload.get("graduated_at")
                        )
                        existing_payload["graduated_at"] = None
                    for key in (
                        "first_seen_at",
                        "graduated_at",
                        "graduation_source",
                        "first",
                        "detection_safety",
                        "detection_forensics",
                        "detection_score",
                        "detection_quality",
                    ):
                        updated_payload[key] = existing_payload.get(key)
                    for key in (
                        "chain_created_at",
                        "pair_created_at",
                        "radar_first_seen_at",
                        "first_market_data_at",
                    ):
                        updated_payload[key] = existing_payload.get(key) or updated_payload.get(key)
                    for key in (
                        "first_research_eligible_at",
                        "first_discord_visible_at",
                        "entry_eligible_at",
                        "strong_alert_at",
                        "qualified_at",
                        "qualified_market_cap_usd",
                        "heating_at",
                    ):
                        updated_payload[key] = existing_payload.get(key) or updated_payload.get(key)
                    # The funnel high-water mark never regresses, so
                    # missed-runner analysis can ask "did this token ever
                    # qualify?" long after it cooled off.
                    updated_payload["best_stage"] = merge_best_stage(
                        str(existing_payload.get("best_stage") or ""),
                        str(updated_payload.get("stage") or STAGE_RAW),
                    )
                    payload_json = json.dumps(updated_payload, separators=(",", ":"))
                await self.db.execute(
                    """
                    INSERT INTO runner_candidates(
                        mint, payload_json, first_seen_at, graduated_at,
                        graduation_source, first_price_usd, first_market_cap_usd,
                        first_liquidity_usd, first_score, latest_score, tier,
                        x_verified, chain_created_at, pair_created_at,
                        radar_first_seen_at, first_market_data_at,
                        first_research_eligible_at, first_discord_visible_at,
                        entry_eligible_at, strong_alert_at,
                        first_visible_market_cap_usd, entry_market_cap_usd,
                        peak_market_cap_usd, last_seen_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    ON CONFLICT(mint) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        latest_score = excluded.latest_score,
                        tier = excluded.tier,
                        x_verified = MAX(runner_candidates.x_verified, excluded.x_verified),
                        graduated_at = COALESCE(
                            runner_candidates.graduated_at,
                            excluded.graduated_at
                        ),
                        chain_created_at = COALESCE(
                            runner_candidates.chain_created_at,
                            excluded.chain_created_at
                        ),
                        pair_created_at = COALESCE(
                            runner_candidates.pair_created_at,
                            excluded.pair_created_at
                        ),
                        radar_first_seen_at = COALESCE(
                            runner_candidates.radar_first_seen_at,
                            excluded.radar_first_seen_at
                        ),
                        first_market_data_at = COALESCE(
                            runner_candidates.first_market_data_at,
                            excluded.first_market_data_at
                        ),
                        first_research_eligible_at = COALESCE(
                            runner_candidates.first_research_eligible_at,
                            excluded.first_research_eligible_at
                        ),
                        entry_eligible_at = COALESCE(
                            runner_candidates.entry_eligible_at,
                            excluded.entry_eligible_at
                        ),
                        entry_market_cap_usd = COALESCE(
                            runner_candidates.entry_market_cap_usd,
                            excluded.entry_market_cap_usd
                        ),
                        peak_market_cap_usd = CASE
                            WHEN runner_candidates.peak_market_cap_usd IS NULL
                                THEN excluded.peak_market_cap_usd
                            WHEN excluded.peak_market_cap_usd IS NULL
                                THEN runner_candidates.peak_market_cap_usd
                            ELSE MAX(
                                runner_candidates.peak_market_cap_usd,
                                excluded.peak_market_cap_usd
                            )
                        END,
                        last_seen_at = MAX(runner_candidates.last_seen_at, excluded.last_seen_at)
                    """,
                    (
                        candidate.mint,
                        payload_json,
                        candidate.first_seen_at,
                        candidate.graduated_at,
                        candidate.graduation_source[:80],
                        (
                            float(candidate.first.price_usd)
                            if candidate.first.price_usd is not None
                            else None
                        ),
                        (
                            float(candidate.first.market_cap_usd)
                            if candidate.first.market_cap_usd is not None
                            else None
                        ),
                        (
                            float(candidate.first.liquidity_usd)
                            if candidate.first.liquidity_usd is not None
                            else None
                        ),
                        float(candidate.score),
                        float(candidate.score),
                        candidate.tier[:80],
                        int(candidate.x_evidence.available),
                        candidate.chain_created_at,
                        candidate.pair_created_at,
                        candidate.radar_first_seen_at or candidate.first_seen_at,
                        candidate.first_market_data_at,
                        candidate.first_research_eligible_at,
                        candidate.first_discord_visible_at,
                        candidate.entry_eligible_at,
                        candidate.strong_alert_at,
                        None,
                        (
                            float(candidate.current.market_cap_usd)
                            if candidate.entry_eligible_at is not None
                            and candidate.current.market_cap_usd is not None
                            else None
                        ),
                        (
                            float(candidate.current.market_cap_usd)
                            if candidate.current.market_cap_usd is not None
                            else None
                        ),
                        candidate.generated_at,
                    ),
                )
                merged = json.loads(payload_json)
                quality = candidate.quality
                await self.db.execute(
                    """
                    UPDATE runner_candidates SET
                        stage = ?,
                        best_stage = ?,
                        qualified_at = COALESCE(qualified_at, ?),
                        qualified_market_cap_usd = COALESCE(qualified_market_cap_usd, ?),
                        heating_at = COALESCE(heating_at, ?),
                        momentum_score = ?,
                        opportunity_score = ?,
                        organic_score = ?
                    WHERE mint = ?
                    """,
                    (
                        candidate.stage,
                        str(merged.get("best_stage") or candidate.best_stage),
                        merged.get("qualified_at"),
                        (
                            float(merged["qualified_market_cap_usd"])
                            if merged.get("qualified_market_cap_usd") is not None
                            else None
                        ),
                        merged.get("heating_at"),
                        float(quality.momentum_score),
                        float(quality.opportunity_score),
                        float(quality.organic_score),
                        candidate.mint,
                    ),
                )
                await self.db.execute(
                    """
                    INSERT OR IGNORE INTO runner_snapshots(
                        mint, captured_at, snapshot_json, score
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        candidate.mint,
                        candidate.current.captured_at,
                        snapshot_json,
                        float(candidate.score),
                    ),
                )
                # One immutable decision row per stage transition. Calibration
                # replays these, so nothing here may ever be rewritten with
                # information that arrived later.
                await self.db.execute(
                    """
                    INSERT OR IGNORE INTO runner_stage_events(
                        mint, stage, decided_at, momentum_score, opportunity_score,
                        organic_score, safety_status, market_cap_usd, liquidity_usd,
                        evidence_json, warnings_json, decision_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate.mint,
                        candidate.stage,
                        candidate.generated_at,
                        float(quality.momentum_score),
                        float(quality.opportunity_score),
                        float(quality.organic_score),
                        candidate.safety.status,
                        (
                            float(candidate.current.market_cap_usd)
                            if candidate.current.market_cap_usd is not None
                            else None
                        ),
                        (
                            float(candidate.current.liquidity_usd)
                            if candidate.current.liquidity_usd is not None
                            else None
                        ),
                        json.dumps(list(quality.evidence), separators=(",", ":")),
                        json.dumps(list(quality.quality_warnings), separators=(",", ":")),
                        quality.decision_version,
                    ),
                )
                await self.db.commit()
                return is_new
            except Exception:
                await self.db.rollback()
                raise

    async def reserve_fast_alert(
        self,
        *,
        alert_key: str,
        kind: str,
        mint: str,
        now: int,
        fingerprint: str = "",
        pinged: bool = False,
    ) -> bool:
        """Claim the right to publish one fast alert exactly once.

        Returns ``False`` when this alert was already published, which is what
        makes a restart or a retried coroutine unable to re-ping.
        """

        async with self._write_lock:
            cursor = await self.db.execute(
                """
                INSERT OR IGNORE INTO fast_alerts (
                    alert_key, kind, mint, published_at, pinged, fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (alert_key, kind, mint, now, 1 if pinged else 0, fingerprint),
            )
            await self.db.commit()
            return bool(cursor.rowcount)

    async def attach_fast_alert_message(
        self,
        *,
        alert_key: str,
        message_id: int | None,
        channel_id: int | None,
    ) -> None:
        """Remember where a fast alert landed so enrichment can edit it."""

        async with self._write_lock:
            await self.db.execute(
                "UPDATE fast_alerts SET message_id = ?, channel_id = ? WHERE alert_key = ?",
                (message_id, channel_id, alert_key),
            )
            await self.db.commit()

    async def release_fast_alert(self, alert_key: str) -> None:
        """Give back an unpublished reservation.

        A card Discord refused was never seen, so holding its lock would
        silence that alert permanently.  Releasing lets the next cycle retry;
        a card that *was* delivered keeps its lock and can never re-ping.
        """

        async with self._write_lock:
            await self.db.execute(
                "DELETE FROM fast_alerts WHERE alert_key = ? AND message_id IS NULL",
                (alert_key,),
            )
            await self.db.commit()

    async def fast_alert_row(self, alert_key: str) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM fast_alerts WHERE alert_key = ?", (alert_key,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def mark_fast_alert_enriched(self, *, alert_key: str, now: int) -> None:
        async with self._write_lock:
            await self.db.execute(
                "UPDATE fast_alerts SET enriched_at = ? WHERE alert_key = ?", (now, alert_key)
            )
            await self.db.commit()

    async def recent_fast_alerts(self, *, limit: int = 20) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM fast_alerts ORDER BY published_at DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def record_notable_event(
        self,
        *,
        signature: str,
        wallet: str,
        mint: str,
        side: str,
        chain_time: int,
        observed_at: int,
        amount_usd: float | None = None,
        entry_price_usd: float | None = None,
        entry_market_cap_usd: float | None = None,
        detection_market_cap_usd: float | None = None,
        freshness: str = "FRESH",
    ) -> bool:
        """Persist a public wallet trade the instant it is observed.

        This is the first thing that happens on the fast path, before any
        enrichment, so a slow provider can never delay the record or the alert.
        """

        async with self._write_lock:
            cursor = await self.db.execute(
                """
                INSERT OR IGNORE INTO notable_wallet_events (
                    signature, wallet, mint, side, chain_time, observed_at,
                    amount_usd, entry_price_usd, entry_market_cap_usd,
                    detection_market_cap_usd, freshness
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signature,
                    wallet,
                    mint,
                    side,
                    chain_time,
                    observed_at,
                    amount_usd,
                    entry_price_usd,
                    entry_market_cap_usd,
                    detection_market_cap_usd,
                    freshness,
                ),
            )
            await self.db.commit()
            return bool(cursor.rowcount)

    async def notable_events_for(self, mint: str, *, limit: int = 25) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT * FROM notable_wallet_events
            WHERE mint = ? ORDER BY chain_time DESC LIMIT ?
            """,
            (mint, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def recent_notable_events(self, *, limit: int = 25) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM notable_wallet_events ORDER BY observed_at DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def upsert_notable_wallet(
        self,
        *,
        wallet: str,
        label: str,
        provenance: str,
        verification_source: str,
        confidence: float,
        category: str,
        enabled: bool,
        anonymous_index: int | None,
        last_verified_at: int | None,
        now: int,
    ) -> None:
        async with self._write_lock:
            await self.db.execute(
                """
                INSERT INTO notable_wallets (
                    wallet, label, provenance, verification_source, confidence,
                    category, enabled, anonymous_index, last_verified_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet) DO UPDATE SET
                    label = excluded.label,
                    provenance = excluded.provenance,
                    verification_source = excluded.verification_source,
                    confidence = excluded.confidence,
                    category = excluded.category,
                    enabled = excluded.enabled,
                    anonymous_index = COALESCE(
                        notable_wallets.anonymous_index, excluded.anonymous_index
                    ),
                    last_verified_at = excluded.last_verified_at,
                    updated_at = excluded.updated_at
                """,
                (
                    wallet,
                    label,
                    provenance,
                    verification_source,
                    confidence,
                    category,
                    1 if enabled else 0,
                    anonymous_index,
                    last_verified_at,
                    now,
                ),
            )
            await self.db.commit()

    async def notable_wallet_rows(self, *, enabled_only: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM notable_wallets"
        if enabled_only:
            query += " WHERE enabled = 1"
        cursor = await self.db.execute(query + " ORDER BY wallet")
        return [dict(row) for row in await cursor.fetchall()]

    async def store_catalyst_event(
        self,
        *,
        event_id: str,
        headline: str,
        detected_at: int,
        occurred_at: int | None,
        confidence: str,
        priority: str,
        markers_json: str,
        payload_json: str,
        now: int,
    ) -> None:
        async with self._write_lock:
            await self.db.execute(
                """
                INSERT INTO catalyst_events (
                    event_id, headline, detected_at, occurred_at, confidence,
                    priority, markers_json, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    confidence = excluded.confidence,
                    priority = excluded.priority,
                    markers_json = excluded.markers_json,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    event_id,
                    headline,
                    detected_at,
                    occurred_at,
                    confidence,
                    priority,
                    markers_json,
                    payload_json,
                    now,
                ),
            )
            await self.db.commit()

    async def store_catalyst_link(
        self,
        *,
        event_id: str,
        mint: str,
        connection: str,
        name_similarity: float | None,
        seconds_after_event: int | None,
        official: bool,
        payload_json: str,
        now: int,
    ) -> None:
        async with self._write_lock:
            await self.db.execute(
                """
                INSERT INTO catalyst_token_links (
                    event_id, mint, connection, name_similarity,
                    seconds_after_event, official, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id, mint) DO UPDATE SET
                    connection = excluded.connection,
                    name_similarity = excluded.name_similarity,
                    seconds_after_event = excluded.seconds_after_event,
                    official = excluded.official,
                    payload_json = excluded.payload_json
                """,
                (
                    event_id,
                    mint,
                    connection,
                    name_similarity,
                    seconds_after_event,
                    1 if official else 0,
                    payload_json,
                    now,
                ),
            )
            await self.db.commit()

    async def recent_catalyst_events(self, *, limit: int = 10) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM catalyst_events ORDER BY detected_at DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def catalyst_links_for(self, mint: str, *, limit: int = 10) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT l.*, e.headline, e.confidence, e.priority, e.detected_at
            FROM catalyst_token_links AS l
            JOIN catalyst_events AS e ON e.event_id = l.event_id
            WHERE l.mint = ? ORDER BY l.created_at DESC LIMIT ?
            """,
            (mint, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def record_discovery(
        self,
        *,
        mint: str,
        source_name: str,
        source_event_at: int | None,
        now: int,
        source_is_realtime: bool = True,
    ) -> bool:
        """Persist first-seen the moment cheap discovery detects a mint.

        Called before any enrichment, so a slow provider can never inflate the
        measured ingestion latency.  ``first_seen_at`` is written once and only
        ever moves earlier, never later, so a rediscovery cannot rewrite history.
        Returns whether this was the first time the mint was seen.
        """

        async with self._write_lock:
            cursor = await self.db.execute(
                """
                INSERT INTO runner_discovery (
                    mint, source_name, source_event_at, source_is_realtime,
                    ingested_at, first_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mint) DO UPDATE SET
                    first_seen_at = MIN(runner_discovery.first_seen_at, excluded.first_seen_at),
                    source_event_at = COALESCE(
                        runner_discovery.source_event_at, excluded.source_event_at
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    mint,
                    source_name,
                    source_event_at,
                    1 if source_is_realtime else 0,
                    now,
                    now,
                    now,
                ),
            )
            await self.db.commit()
            return bool(cursor.rowcount) and cursor.lastrowid is not None

    async def mark_discovery_stage(
        self,
        *,
        mint: str,
        stage: str,
        at: int,
    ) -> None:
        """Record the first time a candidate reached one pipeline stage.

        Write-once per stage: ``COALESCE`` keeps the earliest real time, so a
        re-evaluation cannot make the pipeline look faster than it was.
        """

        column = {
            "watch": "first_watch_at",
            "qualified": "first_qualified_at",
            "decision": "first_paper_decision_at",
            "fill": "simulated_fill_at",
        }.get(stage)
        if column is None:
            return
        async with self._write_lock:
            await self.db.execute(
                f"""
                UPDATE runner_discovery
                SET {column} = COALESCE({column}, ?), updated_at = ?
                WHERE mint = ?
                """,
                (at, at, mint),
            )
            await self.db.commit()

    async def discovery_latency_rows(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Timing rows joined with the market-cap path, newest first."""

        cursor = await self.db.execute(
            """
            SELECT d.mint, d.source_name, d.source_event_at, d.source_is_realtime,
                   d.ingested_at, d.first_seen_at, d.first_watch_at,
                   d.first_qualified_at, d.first_paper_decision_at, d.simulated_fill_at,
                   c.first_discord_visible_at, c.pair_created_at, c.chain_created_at,
                   c.first_market_cap_usd, c.first_visible_market_cap_usd,
                   c.entry_market_cap_usd, c.peak_market_cap_usd
            FROM runner_discovery AS d
            LEFT JOIN runner_candidates AS c ON c.mint = d.mint
            ORDER BY d.first_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def discovery_count(self) -> int:
        cursor = await self.db.execute("SELECT COUNT(*) AS total FROM runner_discovery")
        row = await cursor.fetchone()
        return int(row["total"] or 0) if row else 0

    async def recent_runner_candidate_payloads(
        self,
        *,
        now: int,
        max_age_seconds: int,
        limit: int,
    ) -> list[str]:
        cursor = await self.db.execute(
            """
            SELECT payload_json FROM runner_candidates
            WHERE last_seen_at >= ?
            ORDER BY latest_score DESC, last_seen_at DESC
            LIMIT ?
            """,
            (now - max_age_seconds, max(1, min(50, limit))),
        )
        return [str(row["payload_json"]) for row in await cursor.fetchall()]

    async def runner_candidate_payload(self, mint: str) -> str | None:
        cursor = await self.db.execute(
            "SELECT payload_json FROM runner_candidates WHERE mint = ?",
            (mint,),
        )
        row = await cursor.fetchone()
        return str(row["payload_json"]) if row else None

    async def runner_score_history(self, mint: str, *, limit: int = 8) -> tuple[Decimal, ...]:
        cursor = await self.db.execute(
            """
            SELECT score FROM runner_snapshots
            WHERE mint = ? ORDER BY captured_at DESC LIMIT ?
            """,
            (mint, max(1, min(50, limit))),
        )
        rows = await cursor.fetchall()
        return tuple(Decimal(str(row["score"])) for row in reversed(rows))

    async def reserve_runner_alert(
        self,
        *,
        mint: str,
        event_type: str,
        fingerprint: str,
        now: int,
        allow_changed_fingerprint: bool = False,
    ) -> bool:
        """Atomically deduplicate fresh, escalation, strong, and invalidation alerts."""

        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    """
                    SELECT fingerprint FROM runner_alert_events
                    WHERE mint = ? AND event_type = ?
                    """,
                    (mint, event_type),
                )
                row = await cursor.fetchone()
                if row is not None and (
                    not allow_changed_fingerprint or str(row["fingerprint"]) == fingerprint
                ):
                    await self.db.rollback()
                    return False
                if row is None:
                    await self.db.execute(
                        """
                        INSERT INTO runner_alert_events(
                            mint, event_type, fingerprint, first_sent_at, last_sent_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (mint, event_type, fingerprint, now, now),
                    )
                else:
                    await self.db.execute(
                        """
                        UPDATE runner_alert_events
                        SET fingerprint = ?, last_sent_at = ?, send_count = send_count + 1
                        WHERE mint = ? AND event_type = ?
                        """,
                        (fingerprint, now, mint, event_type),
                    )
                await self.db.commit()
                return True
            except Exception:
                await self.db.rollback()
                raise

    async def mark_runner_visible(
        self,
        *,
        mint: str,
        visible_at: int,
        market_cap_usd: Decimal | None,
        strong: bool = False,
    ) -> None:
        """Persist actual post-notifier visibility separately from digest time."""

        field = "strong_alert_at" if strong else "first_discord_visible_at"
        market_field = "entry_market_cap_usd" if strong else "first_visible_market_cap_usd"
        cursor = await self.db.execute(
            "SELECT payload_json FROM runner_candidates WHERE mint = ?",
            (mint,),
        )
        row = await cursor.fetchone()
        if row is None:
            return
        payload = json.loads(str(row["payload_json"]))
        payload[field] = payload.get(field) or visible_at
        if not strong:
            payload["first_discord_visible_at"] = (
                payload.get("first_discord_visible_at") or visible_at
            )
        await self.db.execute(
            f"""
            UPDATE runner_candidates
            SET {field} = COALESCE({field}, ?),
                {market_field} = COALESCE({market_field}, ?),
                payload_json = ?
            WHERE mint = ?
            """,
            (
                visible_at,
                float(market_cap_usd) if market_cap_usd is not None else None,
                json.dumps(payload, separators=(",", ":")),
                mint,
            ),
        )
        await self.db.commit()

    async def release_runner_alert(self, *, mint: str, event_type: str) -> None:
        await self.db.execute(
            "DELETE FROM runner_alert_events WHERE mint = ? AND event_type = ?",
            (mint, event_type),
        )
        await self.db.commit()

    async def runner_latency_rows(self, *, limit: int = 100) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT mint, chain_created_at, pair_created_at, graduated_at,
                   radar_first_seen_at, first_market_data_at,
                   first_research_eligible_at, first_discord_visible_at,
                   entry_eligible_at, strong_alert_at,
                   first_market_cap_usd, first_visible_market_cap_usd,
                   entry_market_cap_usd, peak_market_cap_usd
            FROM runner_candidates
            ORDER BY first_seen_at DESC LIMIT ?
            """,
            (max(1, min(100, limit)),),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def store_runner_forensics(
        self,
        *,
        mint: str,
        payload_json: str,
        funding_checked_at: int,
        dynamic_checked_at: int,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO runner_forensics(
                mint, payload_json, funding_checked_at, dynamic_checked_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(mint) DO UPDATE SET
                payload_json = excluded.payload_json,
                funding_checked_at = MAX(
                    runner_forensics.funding_checked_at,
                    excluded.funding_checked_at
                ),
                dynamic_checked_at = MAX(
                    runner_forensics.dynamic_checked_at,
                    excluded.dynamic_checked_at
                )
            """,
            (mint, payload_json, funding_checked_at, dynamic_checked_at),
        )
        await self.db.commit()

    async def runner_forensics_payload(self, mint: str) -> str | None:
        cursor = await self.db.execute(
            "SELECT payload_json FROM runner_forensics WHERE mint = ?",
            (mint,),
        )
        row = await cursor.fetchone()
        return str(row["payload_json"]) if row else None

    async def runner_snapshot_payloads(
        self,
        mint: str,
        *,
        before_at: int | None = None,
        limit: int = 30,
    ) -> list[str]:
        if before_at is None:
            cursor = await self.db.execute(
                """
                SELECT snapshot_json FROM runner_snapshots
                WHERE mint = ? ORDER BY captured_at DESC LIMIT ?
                """,
                (mint, max(1, min(200, limit))),
            )
        else:
            cursor = await self.db.execute(
                """
                SELECT snapshot_json FROM runner_snapshots
                WHERE mint = ? AND captured_at < ?
                ORDER BY captured_at DESC LIMIT ?
                """,
                (mint, before_at, max(1, min(200, limit))),
            )
        rows = await cursor.fetchall()
        return [str(row["snapshot_json"]) for row in reversed(rows)]

    async def runner_due_mints(
        self,
        *,
        now: int,
        maximum_age_seconds: int = 86_400,
        limit: int = 20,
    ) -> list[str]:
        cursor = await self.db.execute(
            """
            SELECT mint FROM runner_candidates
            WHERE first_seen_at >= ? AND last_seen_at < ?
            ORDER BY last_seen_at ASC LIMIT ?
            """,
            (now - maximum_age_seconds, now - 45, max(1, min(100, limit))),
        )
        return [str(row["mint"]) for row in await cursor.fetchall()]

    async def record_runner_outcome(
        self,
        *,
        mint: str,
        horizon_seconds: int,
        observed_at: int,
        price_return_percent: Decimal | None,
        market_cap_return_percent: Decimal | None,
        liquidity_return_percent: Decimal | None,
        liquidity_disappeared: bool,
        rugged: bool,
        route_available: bool,
    ) -> bool:
        cursor = await self.db.execute(
            """
            INSERT OR IGNORE INTO runner_outcomes(
                mint, horizon_seconds, observed_at, price_return_percent,
                market_cap_return_percent, liquidity_return_percent,
                liquidity_disappeared, rugged, route_available
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mint,
                horizon_seconds,
                observed_at,
                float(price_return_percent) if price_return_percent is not None else None,
                (
                    float(market_cap_return_percent)
                    if market_cap_return_percent is not None
                    else None
                ),
                (float(liquidity_return_percent) if liquidity_return_percent is not None else None),
                int(liquidity_disappeared),
                int(rugged),
                int(route_available),
            ),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def runner_results_rows(self) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT
                c.mint, c.first_seen_at, c.graduated_at, c.first_market_cap_usd,
                c.first_score, c.latest_score, c.x_verified,
                o.horizon_seconds, o.observed_at, o.price_return_percent,
                o.market_cap_return_percent, o.liquidity_disappeared,
                o.rugged, o.route_available
            FROM runner_candidates AS c
            LEFT JOIN runner_outcomes AS o ON o.mint = c.mint
            ORDER BY c.first_seen_at DESC, o.horizon_seconds ASC
            """
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def runner_all_snapshot_rows(self) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT mint, captured_at, snapshot_json, score
            FROM runner_snapshots ORDER BY mint, captured_at
            """
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def record_provider_call(
        self,
        *,
        provider: str,
        feature: str,
        usage_day: str,
        calls: int = 1,
        cache_hits: int = 0,
        errors: int = 0,
        calls_skipped: int = 0,
    ) -> None:
        """Attribute provider cost to the feature that spent it.

        Uncapped on purpose: this is accounting, not a budget gate.  The paid
        daily caps stay in ``api_usage_daily``.

        ``calls_skipped`` is counted separately from ``cache_hits`` on purpose:
        a call a breaker refused during an outage is a saving, but it is not a
        cache hit, and folding the two together would inflate the cache-hit rate
        exactly when a provider is failing.
        """

        now = int(time.time())
        async with self._write_lock:
            await self.db.execute(
                """
                INSERT INTO provider_call_usage(
                    provider, feature, usage_day, calls, cache_hits, errors,
                    calls_skipped, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, feature, usage_day) DO UPDATE SET
                    calls = provider_call_usage.calls + excluded.calls,
                    cache_hits = provider_call_usage.cache_hits + excluded.cache_hits,
                    errors = provider_call_usage.errors + excluded.errors,
                    calls_skipped =
                        provider_call_usage.calls_skipped + excluded.calls_skipped,
                    updated_at = excluded.updated_at
                """,
                (
                    provider,
                    feature,
                    usage_day,
                    calls,
                    cache_hits,
                    errors,
                    calls_skipped,
                    now,
                ),
            )
            await self.db.commit()

    async def provider_call_rows(self, *, usage_day: str | None = None) -> list[dict[str, Any]]:
        if usage_day is None:
            cursor = await self.db.execute(
                """
                SELECT provider, feature, usage_day, calls, cache_hits, errors,
                       calls_skipped
                FROM provider_call_usage
                ORDER BY usage_day DESC, calls DESC
                LIMIT 200
                """
            )
        else:
            cursor = await self.db.execute(
                """
                SELECT provider, feature, usage_day, calls, cache_hits, errors,
                       calls_skipped
                FROM provider_call_usage
                WHERE usage_day = ?
                ORDER BY calls DESC
                """,
                (usage_day,),
            )
        return [dict(row) for row in await cursor.fetchall()]

    # ------------------------------------------------------------------
    # operator-visibility timeline (sections 2, 3, 12, 52)
    # ------------------------------------------------------------------

    async def known_symbols(self, *, limit: int = 500) -> dict[str, str]:
        """Every mint we have a symbol for, as ``mint -> symbol``.

        Used **only** for symbol-collision detection.  It is deliberately keyed
        by mint rather than by symbol: a symbol is not a key, and building a
        symbol-keyed index is how a lookup that substitutes one token for another
        gets written by accident.
        """

        known: dict[str, str] = {}
        for table, mint_column, symbol_column in (
            ("pump_tokens", "mint", "symbol"),
            ("runner_candidates", "mint", "symbol"),
        ):
            try:
                cursor = await self.db.execute(
                    f"SELECT {mint_column} AS mint, {symbol_column} AS symbol "
                    f"FROM {table} WHERE {symbol_column} IS NOT NULL "
                    f"AND {symbol_column} != '' LIMIT ?",
                    (limit,),
                )
            except Exception:
                continue
            for row in await cursor.fetchall():
                mint = str(row["mint"] or "")
                symbol = str(row["symbol"] or "")
                if mint and symbol:
                    known.setdefault(mint, symbol)
        return known

    async def record_alert_stage(
        self,
        *,
        mint: str,
        stage: str,
        occurred_at: int,
        market_cap_usd: Decimal | None = None,
        price_usd: Decimal | None = None,
        liquidity_usd: Decimal | None = None,
        tier: str = "",
        edge_state: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        """Write one stage of the visibility timeline, once and only once.

        ``INSERT OR IGNORE`` is the immutability (section 52): the market cap an
        alert was sent at is a historical fact, and enrichment arriving later
        must never be able to overwrite it.  Returns whether this call was the
        one that recorded the stage.
        """

        async with self._write_lock:
            cursor = await self.db.execute(
                """
                INSERT OR IGNORE INTO alert_timeline (
                    mint, stage, occurred_at, market_cap_usd, price_usd,
                    liquidity_usd, tier, edge_state, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mint,
                    stage,
                    occurred_at,
                    _float_or_none(market_cap_usd),
                    _float_or_none(price_usd),
                    _float_or_none(liquidity_usd),
                    tier,
                    edge_state,
                    json.dumps(evidence or {}, separators=(",", ":"), sort_keys=True),
                ),
            )
            await self.db.commit()
        return bool(cursor.rowcount)

    async def alert_timeline(self, mint: str) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT stage, occurred_at, market_cap_usd, price_usd, liquidity_usd,
                   tier, edge_state, evidence_json
            FROM alert_timeline WHERE mint = ? ORDER BY occurred_at ASC
            """,
            (mint,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def alert_stage_rows(
        self,
        *,
        stage: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if stage is None:
            cursor = await self.db.execute(
                """
                SELECT mint, stage, occurred_at, market_cap_usd, price_usd,
                       liquidity_usd, tier, edge_state
                FROM alert_timeline ORDER BY occurred_at DESC LIMIT ?
                """,
                (limit,),
            )
        else:
            cursor = await self.db.execute(
                """
                SELECT mint, stage, occurred_at, market_cap_usd, price_usd,
                       liquidity_usd, tier, edge_state
                FROM alert_timeline WHERE stage = ?
                ORDER BY occurred_at DESC LIMIT ?
                """,
                (stage, limit),
            )
        return [dict(row) for row in await cursor.fetchall()]

    async def record_alert_suppression(
        self,
        *,
        mint: str,
        reason_code: str,
        occurred_at: int,
        market_cap_usd: Decimal | None = None,
        tier: str = "",
        detail: str = "",
    ) -> bool:
        """Persist why the operator did not get pinged (section 12)."""

        async with self._write_lock:
            cursor = await self.db.execute(
                """
                INSERT OR IGNORE INTO alert_suppression (
                    mint, reason_code, occurred_at, market_cap_usd, tier, detail
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    mint,
                    reason_code,
                    occurred_at,
                    _float_or_none(market_cap_usd),
                    tier,
                    detail[:400],
                ),
            )
            await self.db.commit()
        return bool(cursor.rowcount)

    async def alert_suppression_rows(
        self,
        *,
        mint: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if mint is None:
            cursor = await self.db.execute(
                """
                SELECT mint, reason_code, occurred_at, market_cap_usd, tier, detail
                FROM alert_suppression ORDER BY occurred_at DESC LIMIT ?
                """,
                (limit,),
            )
        else:
            cursor = await self.db.execute(
                """
                SELECT mint, reason_code, occurred_at, market_cap_usd, tier, detail
                FROM alert_suppression WHERE mint = ?
                ORDER BY occurred_at DESC LIMIT ?
                """,
                (mint, limit),
            )
        return [dict(row) for row in await cursor.fetchall()]

    async def suppression_counts(self, *, since: int = 0) -> dict[str, int]:
        cursor = await self.db.execute(
            """
            SELECT reason_code, COUNT(*) AS total FROM alert_suppression
            WHERE occurred_at >= ? GROUP BY reason_code ORDER BY total DESC
            """,
            (since,),
        )
        return {str(row["reason_code"]): int(row["total"]) for row in await cursor.fetchall()}

    # ------------------------------------------------------------------
    # narratives and exact-mint links (sections 21-26)
    # ------------------------------------------------------------------

    async def save_narrative(
        self,
        *,
        narrative_id: str,
        title: str,
        virality: str,
        first_seen_at: int,
        last_seen_at: int,
        payload: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> None:
        moment = now if now is not None else int(time.time())
        async with self._write_lock:
            await self.db.execute(
                """
                INSERT INTO narratives (
                    narrative_id, title, virality, first_seen_at, last_seen_at,
                    payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(narrative_id) DO UPDATE SET
                    title = excluded.title,
                    virality = excluded.virality,
                    last_seen_at = excluded.last_seen_at,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    narrative_id,
                    title,
                    virality,
                    first_seen_at,
                    last_seen_at,
                    json.dumps(payload or {}, separators=(",", ":"), sort_keys=True),
                    moment,
                ),
            )
            await self.db.commit()

    async def narrative_rows(self, *, limit: int = 50) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT narrative_id, title, virality, first_seen_at, last_seen_at, payload_json
            FROM narratives ORDER BY last_seen_at DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def save_narrative_link(
        self,
        *,
        narrative_id: str,
        mint: str,
        relationship: str,
        direction: str,
        confidence: Decimal,
        seconds_after_story: int | None = None,
        payload: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> None:
        moment = now if now is not None else int(time.time())
        async with self._write_lock:
            await self.db.execute(
                """
                INSERT INTO narrative_links (
                    narrative_id, mint, relationship, direction, confidence,
                    seconds_after_story, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(narrative_id, mint) DO UPDATE SET
                    relationship = excluded.relationship,
                    direction = excluded.direction,
                    confidence = excluded.confidence,
                    seconds_after_story = excluded.seconds_after_story,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    narrative_id,
                    mint,
                    relationship,
                    direction,
                    float(confidence),
                    seconds_after_story,
                    json.dumps(payload or {}, separators=(",", ":"), sort_keys=True),
                    moment,
                ),
            )
            await self.db.commit()

    async def narrative_link_rows(
        self,
        *,
        narrative_id: str | None = None,
        mint: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if narrative_id is not None:
            cursor = await self.db.execute(
                """
                SELECT narrative_id, mint, relationship, direction, confidence,
                       seconds_after_story, payload_json
                FROM narrative_links WHERE narrative_id = ?
                ORDER BY confidence DESC LIMIT ?
                """,
                (narrative_id, limit),
            )
        elif mint is not None:
            cursor = await self.db.execute(
                """
                SELECT narrative_id, mint, relationship, direction, confidence,
                       seconds_after_story, payload_json
                FROM narrative_links WHERE mint = ?
                ORDER BY confidence DESC LIMIT ?
                """,
                (mint, limit),
            )
        else:
            cursor = await self.db.execute(
                """
                SELECT narrative_id, mint, relationship, direction, confidence,
                       seconds_after_story, payload_json
                FROM narrative_links ORDER BY confidence DESC LIMIT ?
                """,
                (limit,),
            )
        return [dict(row) for row in await cursor.fetchall()]

    async def cached_funding_edges(self, wallets: list[str]) -> dict[str, dict[str, Any]]:
        """Read previously resolved funding relationships for these wallets."""

        if not wallets:
            return {}
        placeholders = ",".join("?" for _ in wallets[:100])
        cursor = await self.db.execute(
            f"""
            SELECT wallet, funder, funded_at, amount_sol, first_activity_at, trace_complete
            FROM wallet_funding_edges WHERE wallet IN ({placeholders})
            """,
            tuple(wallets[:100]),
        )
        return {str(row["wallet"]): dict(row) for row in await cursor.fetchall()}

    async def store_funding_edges(self, edges: list[dict[str, Any]]) -> None:
        """Cache immutable funding relationships; a resolved edge never changes."""

        if not edges:
            return
        now = int(time.time())
        async with self._write_lock:
            await self.db.executemany(
                """
                INSERT INTO wallet_funding_edges(
                    wallet, funder, funded_at, amount_sol, first_activity_at,
                    trace_complete, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet) DO UPDATE SET
                    funder = COALESCE(excluded.funder, wallet_funding_edges.funder),
                    funded_at = COALESCE(excluded.funded_at, wallet_funding_edges.funded_at),
                    amount_sol = COALESCE(excluded.amount_sol, wallet_funding_edges.amount_sol),
                    first_activity_at = COALESCE(
                        excluded.first_activity_at, wallet_funding_edges.first_activity_at
                    ),
                    trace_complete = MAX(
                        wallet_funding_edges.trace_complete, excluded.trace_complete
                    ),
                    resolved_at = excluded.resolved_at
                """,
                [
                    (
                        edge["wallet"],
                        edge.get("funder"),
                        edge.get("funded_at"),
                        edge.get("amount_sol"),
                        edge.get("first_activity_at"),
                        int(bool(edge.get("trace_complete"))),
                        now,
                    )
                    for edge in edges
                    if edge.get("wallet")
                ],
            )
            await self.db.commit()

    async def runner_stage_counts(self, *, since: int = 0) -> dict[str, int]:
        """Count how many distinct mints ever reached each funnel stage."""

        cursor = await self.db.execute(
            """
            SELECT stage, COUNT(DISTINCT mint) AS mints
            FROM runner_stage_events
            WHERE decided_at >= ?
            GROUP BY stage
            """,
            (since,),
        )
        return {str(row["stage"]): int(row["mints"]) for row in await cursor.fetchall()}

    async def runner_funnel_rows(self, *, since: int = 0) -> list[dict[str, Any]]:
        """One row per observed token with its funnel outcome and forward path.

        Silent and rejected candidates are deliberately included: a system that
        only measures what it alerted on cannot detect a missed runner.
        """

        cursor = await self.db.execute(
            """
            SELECT
                c.mint,
                c.stage,
                c.best_stage,
                c.qualified_at,
                c.qualified_market_cap_usd,
                c.heating_at,
                c.first_discord_visible_at,
                c.first_seen_at,
                c.radar_first_seen_at,
                c.pair_created_at,
                c.chain_created_at,
                c.graduated_at,
                c.first_market_cap_usd,
                c.first_visible_market_cap_usd,
                c.entry_market_cap_usd,
                c.peak_market_cap_usd,
                c.first_score,
                c.latest_score,
                c.momentum_score,
                c.opportunity_score,
                c.organic_score,
                MAX(COALESCE(
                    o.price_return_percent, o.market_cap_return_percent
                )) AS best_return_percent,
                MIN(COALESCE(
                    o.price_return_percent, o.market_cap_return_percent
                )) AS worst_return_percent,
                MAX(COALESCE(o.liquidity_disappeared, 0)) AS liquidity_disappeared,
                MAX(COALESCE(o.rugged, 0)) AS rugged,
                COUNT(o.horizon_seconds) AS outcome_rows
            FROM runner_candidates AS c
            LEFT JOIN runner_outcomes AS o ON o.mint = c.mint
            WHERE c.first_seen_at >= ?
            GROUP BY c.mint
            ORDER BY c.first_seen_at DESC
            LIMIT 2000
            """,
            (since,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def runner_stage_event_rows(
        self,
        *,
        mint: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if mint:
            cursor = await self.db.execute(
                """
                SELECT * FROM runner_stage_events WHERE mint = ?
                ORDER BY decided_at DESC LIMIT ?
                """,
                (mint, limit),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM runner_stage_events ORDER BY decided_at DESC LIMIT ?",
                (limit,),
            )
        return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    def user_facing_stage(stage: str | None) -> bool:
        return str(stage or STAGE_RAW) in USER_FACING_STAGES

    async def runner_observation_count(self) -> int:
        cursor = await self.db.execute("SELECT COUNT(*) AS count FROM runner_candidates")
        row = await cursor.fetchone()
        return int(row["count"] or 0)

    async def recent_observed_token_mints(self, *, limit: int = 20) -> list[str]:
        """Return real token mints seen in public tracked-wallet swaps, newest first."""

        cursor = await self.db.execute(
            """
            SELECT token_mint, MAX(block_time) AS latest
            FROM swaps
            GROUP BY token_mint
            ORDER BY latest DESC
            LIMIT ?
            """,
            (max(1, min(100, limit)),),
        )
        return [str(row["token_mint"]) for row in await cursor.fetchall()]

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
