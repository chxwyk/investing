"""SQL for the trenches engine.  The :mod:`smart_money_bot.trenches` package stays storage-free.

Same split as the lab, the shadow experiment and the Trending ledger: strategy is
pure and testable, and every statement that touches a database lives here.

The write-once rule is enforced in this file rather than trusted to callers.  The
``first_*`` columns are set by the initial insert and never appear in an
``UPDATE SET`` clause, and the read path re-derives them from those protected
columns rather than from the JSON payload — which is the bug the v2.42 self-check
caught and which would otherwise make the guarantee cosmetic.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

from .database import Database
from .trenches.holders import HolderSnapshot
from .trenches.timeframes import MarketObservation

ZERO = Decimal("0")


def _f(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _d(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() else None


def _dumps(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)


class TrenchesStore:
    """Persistence for Pump tokens, observations, intelligence and diagnostics."""

    #: Columns the upsert never updates — the authority on first observation.
    _IMMUTABLE_COLUMNS: tuple[str, ...] = (
        "first_observed_at",
        "first_observed_source",
        "first_market_cap_usd",
        "first_bonding_percent",
        "created_at",
    )

    def __init__(self, database: Database) -> None:
        self.database = database

    @property
    def _db(self) -> Any:
        return self.database.db

    # ------------------------------------------------------------------
    # the token ledger
    # ------------------------------------------------------------------
    async def record_token(
        self,
        mint: str,
        *,
        now: int,
        name: str = "",
        symbol: str = "",
        creator: str = "",
        created_at: int | None = None,
        source: str = "",
        stage: str = "UNKNOWN",
        bonding_percent: Decimal | None = None,
        market_cap_usd: Decimal | None = None,
        liquidity_usd: Decimal | None = None,
        holders: int | None = None,
        top10_percent: Decimal | None = None,
        graduated_at: int | None = None,
        graduation_market_cap_usd: Decimal | None = None,
        special_mode: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Upsert a token.  First-observation columns are absent from the update."""

        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO pump_tokens (
                    mint, name, symbol, creator, created_at, first_observed_at,
                    first_observed_source, first_market_cap_usd, first_bonding_percent,
                    stage, bonding_percent, market_cap_usd, liquidity_usd, holders,
                    top10_percent, graduated_at, graduation_market_cap_usd,
                    special_mode, last_observed_at, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mint) DO UPDATE SET
                    name = CASE
                        WHEN excluded.name != '' THEN excluded.name
                        ELSE pump_tokens.name END,
                    symbol = CASE
                        WHEN excluded.symbol != '' THEN excluded.symbol
                        ELSE pump_tokens.symbol END,
                    creator = CASE
                        WHEN excluded.creator != '' THEN excluded.creator
                        ELSE pump_tokens.creator END,
                    stage = excluded.stage,
                    bonding_percent = excluded.bonding_percent,
                    market_cap_usd = excluded.market_cap_usd,
                    liquidity_usd = excluded.liquidity_usd,
                    holders = excluded.holders,
                    top10_percent = excluded.top10_percent,
                    graduated_at = COALESCE(pump_tokens.graduated_at, excluded.graduated_at),
                    graduation_market_cap_usd = COALESCE(
                        pump_tokens.graduation_market_cap_usd, excluded.graduation_market_cap_usd
                    ),
                    special_mode = excluded.special_mode,
                    last_observed_at = excluded.last_observed_at,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    mint,
                    name,
                    symbol,
                    creator,
                    created_at,
                    now,
                    source,
                    _f(market_cap_usd),
                    _f(bonding_percent),
                    stage,
                    _f(bonding_percent),
                    _f(market_cap_usd),
                    _f(liquidity_usd),
                    holders,
                    _f(top10_percent),
                    graduated_at,
                    _f(graduation_market_cap_usd),
                    special_mode,
                    now,
                    _dumps(payload or {}),
                    now,
                ),
            )
            await self._db.commit()

    async def token(self, mint: str) -> dict[str, Any] | None:
        cursor = await self._db.execute("SELECT * FROM pump_tokens WHERE mint = ?", (mint,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def tokens_by_stage(
        self,
        stages: tuple[str, ...],
        *,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """The Trenches sections an operator browses (section 78)."""

        if not stages:
            return []
        placeholders = ",".join("?" for _ in stages)
        cursor = await self._db.execute(
            f"""
            SELECT * FROM pump_tokens WHERE stage IN ({placeholders})
            ORDER BY
                CASE WHEN bonding_percent IS NULL THEN 1 ELSE 0 END,
                bonding_percent DESC,
                first_observed_at DESC
            LIMIT ?
            """,
            (*stages, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def recent_tokens(self, *, limit: int = 50, since: int = 0) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            """
            SELECT * FROM pump_tokens WHERE first_observed_at >= ?
            ORDER BY first_observed_at DESC LIMIT ?
            """,
            (since, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def tracked_count(self, *, since: int = 0) -> int:
        cursor = await self._db.execute(
            "SELECT COUNT(*) AS total FROM pump_tokens WHERE last_observed_at >= ?", (since,)
        )
        row = await cursor.fetchone()
        return int(row["total"]) if row else 0

    async def mark_graduated(
        self,
        mint: str,
        *,
        at: int,
        market_cap_usd: Decimal | None,
    ) -> None:
        """Record graduation once.  A later pass never rewrites the moment."""

        async with self.database._write_lock:
            await self._db.execute(
                """
                UPDATE pump_tokens
                SET graduated_at = COALESCE(graduated_at, ?),
                    graduation_market_cap_usd = COALESCE(graduation_market_cap_usd, ?),
                    updated_at = ?
                WHERE mint = ?
                """,
                (at, _f(market_cap_usd), at, mint),
            )
            await self._db.commit()

    # ------------------------------------------------------------------
    # the observation stream (sections 9-11)
    # ------------------------------------------------------------------
    async def record_observation(
        self,
        mint: str,
        observation: MarketObservation,
        *,
        bonding_percent: Decimal | None = None,
    ) -> None:
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT OR IGNORE INTO pump_observations (
                    mint, observed_at, price_usd, market_cap_usd, liquidity_usd,
                    bonding_percent, buys, sells, volume_usd, unique_buyers,
                    unique_sellers, independent_buyers, holders
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mint,
                    observation.at,
                    _f(observation.price_usd),
                    _f(observation.market_cap_usd),
                    _f(observation.liquidity_usd),
                    _f(bonding_percent),
                    observation.buys,
                    observation.sells,
                    float(observation.volume_usd),
                    observation.unique_buyers,
                    observation.unique_sellers,
                    observation.independent_buyers,
                    observation.holders,
                ),
            )
            await self._db.commit()

    async def observations(
        self,
        mint: str,
        *,
        since: int = 0,
        limit: int = 240,
    ) -> list[MarketObservation]:
        """The rows every timeframe window is computed from, oldest first."""

        cursor = await self._db.execute(
            """
            SELECT * FROM pump_observations WHERE mint = ? AND observed_at >= ?
            ORDER BY observed_at DESC LIMIT ?
            """,
            (mint, since, limit),
        )
        rows = list(reversed(await cursor.fetchall()))
        return [
            MarketObservation(
                at=int(row["observed_at"]),
                price_usd=_d(row["price_usd"]),
                market_cap_usd=_d(row["market_cap_usd"]),
                liquidity_usd=_d(row["liquidity_usd"]),
                buys=int(row["buys"] or 0),
                sells=int(row["sells"] or 0),
                volume_usd=_d(row["volume_usd"]) or ZERO,
                unique_buyers=row["unique_buyers"],
                unique_sellers=row["unique_sellers"],
                independent_buyers=row["independent_buyers"],
                holders=row["holders"],
            )
            for row in rows
        ]

    async def prune_observations(self, *, older_than: int) -> int:
        async with self.database._write_lock:
            cursor = await self._db.execute(
                "DELETE FROM pump_observations WHERE observed_at < ?", (older_than,)
            )
            await self._db.commit()
        return cursor.rowcount or 0

    # ------------------------------------------------------------------
    # holders (sections 20, 21)
    # ------------------------------------------------------------------
    async def record_holder_snapshot(self, snapshot: HolderSnapshot) -> None:
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT OR IGNORE INTO pump_holder_snapshots (
                    mint, observed_at, top10_percent, top20_percent,
                    largest_holder_percent, infrastructure_percent, holder_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.mint,
                    snapshot.at,
                    _f(snapshot.top10_percent),
                    _f(snapshot.top20_percent),
                    _f(snapshot.largest_holder_percent),
                    _f(snapshot.infrastructure_percent),
                    snapshot.holder_count,
                ),
            )
            await self._db.commit()

    async def holder_snapshots(self, mint: str, *, limit: int = 24) -> list[HolderSnapshot]:
        cursor = await self._db.execute(
            """
            SELECT * FROM pump_holder_snapshots WHERE mint = ?
            ORDER BY observed_at DESC LIMIT ?
            """,
            (mint, limit),
        )
        rows = list(reversed(await cursor.fetchall()))
        return [
            HolderSnapshot(
                mint=mint,
                at=int(row["observed_at"]),
                top10_percent=_d(row["top10_percent"]),
                top20_percent=_d(row["top20_percent"]),
                largest_holder_percent=_d(row["largest_holder_percent"]),
                infrastructure_percent=_d(row["infrastructure_percent"]),
                holder_count=row["holder_count"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # dev and per-token intelligence (sections 16-23)
    # ------------------------------------------------------------------
    async def save_dev_profile(
        self,
        wallet: str,
        *,
        tokens_created: int,
        graduated: int,
        collapsed: int,
        retained_liquidity: int,
        history_label: str,
        funding_source_type: str = "UNKNOWN",
        funding_source_wallet: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        if not wallet:
            return
        moment = int(time.time())
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO pump_dev_profiles (
                    wallet, tokens_created, graduated, collapsed, retained_liquidity,
                    history_label, funding_source_type, funding_source_wallet,
                    payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet) DO UPDATE SET
                    tokens_created = excluded.tokens_created,
                    graduated = excluded.graduated,
                    collapsed = excluded.collapsed,
                    retained_liquidity = excluded.retained_liquidity,
                    history_label = excluded.history_label,
                    funding_source_type = CASE
                        WHEN excluded.funding_source_type != 'UNKNOWN'
                        THEN excluded.funding_source_type
                        ELSE pump_dev_profiles.funding_source_type END,
                    funding_source_wallet = CASE
                        WHEN excluded.funding_source_wallet != ''
                        THEN excluded.funding_source_wallet
                        ELSE pump_dev_profiles.funding_source_wallet END,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    wallet,
                    tokens_created,
                    graduated,
                    collapsed,
                    retained_liquidity,
                    history_label,
                    funding_source_type,
                    funding_source_wallet,
                    _dumps(payload or {}),
                    moment,
                ),
            )
            await self._db.commit()

    async def dev_profile(self, wallet: str) -> dict[str, Any] | None:
        if not wallet:
            return None
        cursor = await self._db.execute(
            "SELECT * FROM pump_dev_profiles WHERE wallet = ?", (wallet,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def creator_tokens(self, wallet: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Prior tokens by the same creator, for the neutral history label (§19)."""

        if not wallet:
            return []
        cursor = await self._db.execute(
            """
            SELECT mint, created_at, graduated_at, liquidity_usd, market_cap_usd
            FROM pump_tokens WHERE creator = ? ORDER BY first_observed_at DESC LIMIT ?
            """,
            (wallet, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def save_intel(
        self,
        mint: str,
        *,
        dev_wallet: str = "",
        dev_initial_percent: Decimal | None = None,
        dev_current_percent: Decimal | None = None,
        dev_posture: str = "UNKNOWN",
        bundle_risk: str = "UNKNOWN",
        bundle_count: int = 0,
        bundle_supply_percent: Decimal | None = None,
        bundle_distributing: bool = False,
        independent_buyers: int | None = None,
        unique_buyers: int | None = None,
        clustered_percent: Decimal | None = None,
        fresh_wallet_percent: Decimal | None = None,
        related_percent: Decimal | None = None,
        metadata_reuse: str = "NONE",
        payload: dict[str, Any] | None = None,
    ) -> None:
        moment = int(time.time())
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO pump_intel (
                    mint, dev_wallet, dev_initial_percent, dev_current_percent,
                    dev_posture, bundle_risk, bundle_count, bundle_supply_percent,
                    bundle_distributing, independent_buyers, unique_buyers,
                    clustered_percent, fresh_wallet_percent, related_percent,
                    metadata_reuse, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mint) DO UPDATE SET
                    dev_wallet = CASE
                        WHEN excluded.dev_wallet != '' THEN excluded.dev_wallet
                        ELSE pump_intel.dev_wallet END,
                    dev_initial_percent = COALESCE(
                        pump_intel.dev_initial_percent, excluded.dev_initial_percent
                    ),
                    dev_current_percent = excluded.dev_current_percent,
                    dev_posture = excluded.dev_posture,
                    bundle_risk = excluded.bundle_risk,
                    bundle_count = excluded.bundle_count,
                    bundle_supply_percent = excluded.bundle_supply_percent,
                    bundle_distributing = excluded.bundle_distributing,
                    independent_buyers = excluded.independent_buyers,
                    unique_buyers = excluded.unique_buyers,
                    clustered_percent = excluded.clustered_percent,
                    fresh_wallet_percent = excluded.fresh_wallet_percent,
                    related_percent = excluded.related_percent,
                    metadata_reuse = excluded.metadata_reuse,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    mint,
                    dev_wallet,
                    _f(dev_initial_percent),
                    _f(dev_current_percent),
                    dev_posture,
                    bundle_risk,
                    bundle_count,
                    _f(bundle_supply_percent),
                    1 if bundle_distributing else 0,
                    independent_buyers,
                    unique_buyers,
                    _f(clustered_percent),
                    _f(fresh_wallet_percent),
                    _f(related_percent),
                    metadata_reuse,
                    _dumps(payload or {}),
                    moment,
                ),
            )
            await self._db.commit()

    async def intel(self, mint: str) -> dict[str, Any] | None:
        cursor = await self._db.execute("SELECT * FROM pump_intel WHERE mint = ?", (mint,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # metadata fingerprints (section 27)
    # ------------------------------------------------------------------
    async def save_metadata_prints(
        self,
        mint: str,
        prints: dict[str, str],
        *,
        created_at: int | None = None,
    ) -> None:
        if not prints:
            return
        async with self.database._write_lock:
            for field, digest in prints.items():
                await self._db.execute(
                    """
                    INSERT OR IGNORE INTO pump_metadata_prints (mint, field, digest, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (mint, field, digest, created_at),
                )
            await self._db.commit()

    async def mints_sharing_prints(
        self,
        prints: dict[str, str],
        *,
        exclude_mint: str = "",
        limit: int = 20,
    ) -> list[tuple[str, dict[str, str], int]]:
        """Other mints whose fingerprints collide with these."""

        if not prints:
            return []
        found: dict[str, dict[str, str]] = {}
        created: dict[str, int] = {}
        for field, digest in prints.items():
            cursor = await self._db.execute(
                """
                SELECT mint, field, digest, created_at FROM pump_metadata_prints
                WHERE field = ? AND digest = ? AND mint != ? LIMIT ?
                """,
                (field, digest, exclude_mint, limit),
            )
            for row in await cursor.fetchall():
                found.setdefault(row["mint"], {})[row["field"]] = row["digest"]
                created[row["mint"]] = int(row["created_at"] or 0)
        return [(mint, fields, created.get(mint, 0)) for mint, fields in found.items()]

    # ------------------------------------------------------------------
    # our public ranking, nominations, alerts and diagnostics
    # ------------------------------------------------------------------
    async def record_public_rank(
        self,
        mint: str,
        *,
        at: int,
        rank: int,
        score: Decimal,
        shape: str = "",
        momentum_curve: str = "",
        model: str = "PUBLIC_TRENDING_MODEL",
    ) -> None:
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT OR IGNORE INTO public_trend_ranks (
                    mint, observed_at, rank, score, shape, momentum_curve, model
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (mint, at, rank, float(score), shape, momentum_curve, model),
            )
            await self._db.commit()

    async def previous_public_ranks(self, *, limit: int = 100) -> dict[str, int]:
        """The most recent rank per mint, for computing rank movement."""

        cursor = await self._db.execute(
            """
            SELECT mint, rank FROM public_trend_ranks
            WHERE observed_at = (SELECT MAX(observed_at) FROM public_trend_ranks)
            LIMIT ?
            """,
            (limit,),
        )
        return {row["mint"]: int(row["rank"]) for row in await cursor.fetchall()}

    async def record_nomination(
        self,
        mint: str,
        *,
        lane: str,
        source_kind: str,
        at: int,
        detail: str = "",
    ) -> None:
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO discovery_nominations (
                    mint, lane, source_kind, first_at, last_at, detail
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(mint, lane) DO UPDATE SET
                    last_at = excluded.last_at,
                    detail = excluded.detail
                """,
                (mint, lane, source_kind, at, at, detail[:200]),
            )
            await self._db.commit()

    async def nominations_for(self, mint: str) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            "SELECT * FROM discovery_nominations WHERE mint = ? ORDER BY first_at", (mint,)
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def lane_counts(self, *, since: int = 0) -> dict[str, int]:
        cursor = await self._db.execute(
            """
            SELECT lane, COUNT(*) AS total FROM discovery_nominations
            WHERE last_at >= ? GROUP BY lane ORDER BY total DESC
            """,
            (since,),
        )
        return {row["lane"]: int(row["total"]) for row in await cursor.fetchall()}

    async def record_alert(
        self,
        mint: str,
        *,
        tier: str,
        at: int,
        score: Decimal | None = None,
        stage: str = "",
        bonding_percent: Decimal | None = None,
        market_cap_usd: Decimal | None = None,
        reasons: tuple[str, ...] = (),
        payload: dict[str, Any] | None = None,
    ) -> None:
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT OR IGNORE INTO trench_alerts (
                    mint, tier, occurred_at, score, stage, bonding_percent,
                    market_cap_usd, reasons_json, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mint,
                    tier,
                    at,
                    _f(score),
                    stage,
                    _f(bonding_percent),
                    _f(market_cap_usd),
                    _dumps(list(reasons)),
                    _dumps(payload or {}),
                ),
            )
            await self._db.commit()

    async def recent_alerts(self, *, limit: int = 20) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            "SELECT * FROM trench_alerts ORDER BY occurred_at DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def record_suppression(
        self,
        mint: str,
        *,
        reason_code: str,
        at: int,
        score: Decimal | None = None,
        stage: str = "",
        detail: str = "",
    ) -> None:
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT OR IGNORE INTO trench_suppression (
                    mint, reason_code, occurred_at, score, stage, detail
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (mint, reason_code, at, _f(score), stage, detail[:300]),
            )
            await self._db.commit()

    async def suppression_counts(self, *, since: int = 0) -> dict[str, int]:
        cursor = await self._db.execute(
            """
            SELECT reason_code, COUNT(*) AS total FROM trench_suppression
            WHERE occurred_at >= ? GROUP BY reason_code ORDER BY total DESC
            """,
            (since,),
        )
        return {row["reason_code"]: int(row["total"]) for row in await cursor.fetchall()}

    # ------------------------------------------------------------------
    # discovery latency (section 73)
    # ------------------------------------------------------------------
    async def record_discovery_latency(
        self,
        mint: str,
        *,
        observed_at: int,
        created_at: int | None,
        source: str,
        market_cap_usd: Decimal | None = None,
    ) -> None:
        """Write-once: the first observation is the number being measured."""

        latency = None
        if created_at is not None and created_at <= observed_at:
            latency = observed_at - created_at
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT OR IGNORE INTO pump_discovery_latency (
                    mint, created_at, observed_at, source, latency_seconds,
                    market_cap_at_observation_usd
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (mint, created_at, observed_at, source, latency, _f(market_cap_usd)),
            )
            await self._db.commit()

    async def discovery_latencies(self, *, limit: int = 500) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            """
            SELECT * FROM pump_discovery_latency WHERE latency_seconds IS NOT NULL
            ORDER BY observed_at DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def discovery_latency_by_source(self, *, since: int = 0) -> dict[str, dict[str, Any]]:
        """Latency percentiles per discovery source, so a regression is attributable."""

        cursor = await self._db.execute(
            """
            SELECT source, latency_seconds FROM pump_discovery_latency
            WHERE latency_seconds IS NOT NULL AND observed_at >= ?
            """,
            (since,),
        )
        buckets: dict[str, list[int]] = {}
        for row in await cursor.fetchall():
            buckets.setdefault(row["source"] or "unknown", []).append(
                int(row["latency_seconds"])
            )
        summary: dict[str, dict[str, Any]] = {}
        for source, values in buckets.items():
            ordered = sorted(values)
            summary[source] = {
                "samples": len(ordered),
                "p50": ordered[len(ordered) // 2],
                "p90": ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))],
                "best": ordered[0],
                "worst": ordered[-1],
            }
        return summary

    # ------------------------------------------------------------------
    # benchmark (section 83)
    # ------------------------------------------------------------------
    async def save_benchmark(
        self,
        snapshot_id: str,
        *,
        board_name: str,
        captured_at: int,
        captured_by: str,
        source: str,
        entries: list[dict[str, Any]],
        comparison: dict[str, Any],
    ) -> None:
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO benchmark_snapshots (
                    snapshot_id, board_name, captured_at, captured_by, source,
                    entries_json, comparison_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id) DO UPDATE SET
                    comparison_json = excluded.comparison_json
                """,
                (
                    snapshot_id,
                    board_name,
                    captured_at,
                    captured_by,
                    source,
                    _dumps(entries),
                    _dumps(comparison),
                ),
            )
            await self._db.commit()

    async def benchmarks(self, *, limit: int = 10) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            "SELECT * FROM benchmark_snapshots ORDER BY captured_at DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in await cursor.fetchall()]
