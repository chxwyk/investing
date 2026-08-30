"""Restart-safe persistence for the SHADOW auto-trader (sections 42, 55).

Kept apart from :mod:`smart_money_bot.lab_store` on purpose: the two strategy
families must never share a bankroll row, a position row or an exit journal, and
a separate store makes that structural rather than a convention.

Every write here is idempotent:

* a partial unique index allows exactly one open shadow position per
  ``(mint, family, strategy_version)`` — the duplicate-entry lock,
* exit rows are keyed by ``(position_id, sequence)``, so a retried partial exit
  cannot double-sell,
* observation rows are keyed by ``(position_id, observed_at)``, so a replayed
  observation is a no-op rather than a second data point,
* the experiment checkpoint is written once and never rewritten.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

try:
    import aiosqlite
except ImportError:  # pragma: no cover - exercised by the minimal-runtime self-check
    from . import sqlite_compat as aiosqlite  # type: ignore[no-redef]

from .constants import BOT_VERSION
from .database import Database
from .lab.exits import ExitJournalEntry
from .lab.shadow import (
    SHADOW_EXPERIMENT_VERSION,
    SHADOW_STRATEGY_VERSION,
    ShadowConfig,
    ShadowEntryDecision,
    ShadowPosition,
)
from .lab.shadow_metrics import ShadowObservation, VenueFill
from .lab.venues import RouteQuote, RouteSelection

ZERO = Decimal("0")


class ShadowStore:
    """SQL for the shadow experiment.  The strategy package stays storage-free."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @property
    def _db(self) -> Any:
        return self.database.db

    # ------------------------------------------------------------------
    # experiment checkpoint (section 42)
    # ------------------------------------------------------------------
    async def ensure_experiment(
        self,
        config: ShadowConfig,
        *,
        experiment_version: str = SHADOW_EXPERIMENT_VERSION,
        now: int | None = None,
    ) -> dict[str, Any]:
        """Write the checkpoint once, then always return the stored one.

        The stored ``started_at`` is what makes section 41 enforceable: a trade
        decided before it is not part of the forward experiment, and the value
        must therefore never move on a restart or a redeploy.
        """

        moment = now if now is not None else int(time.time())
        payload = {
            "starting_bankroll_usd": str(config.bankroll_usd),
            "position_usd": str(config.position_usd),
            "max_positions": config.max_concurrent_positions,
            "max_exposure_usd": str(config.max_total_exposure_usd),
            "net_objective_usd": str(config.net_profit_objective_usd),
            "strategy_version": config.strategy_version,
        }
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT OR IGNORE INTO shadow_experiment (
                    experiment_version, started_at, starting_bankroll_usd, position_usd,
                    max_positions, max_exposure_usd, net_objective_usd, config_hash,
                    bot_version, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_version,
                    moment,
                    float(config.bankroll_usd),
                    float(config.position_usd),
                    config.max_concurrent_positions,
                    float(config.max_total_exposure_usd),
                    float(config.net_profit_objective_usd),
                    config.config_hash(),
                    BOT_VERSION,
                    json.dumps(payload, separators=(",", ":"), sort_keys=True),
                    moment,
                ),
            )
            await self._db.commit()
        stored = await self.experiment(experiment_version=experiment_version)
        return stored or {}

    async def experiment(
        self,
        *,
        experiment_version: str = SHADOW_EXPERIMENT_VERSION,
    ) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            "SELECT * FROM shadow_experiment WHERE experiment_version = ?",
            (experiment_version,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # bankroll
    # ------------------------------------------------------------------
    async def load_bankroll_payload(
        self,
        *,
        strategy_version: str = SHADOW_STRATEGY_VERSION,
    ) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            "SELECT payload_json FROM shadow_bankroll WHERE strategy_version = ?",
            (strategy_version,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    async def save_bankroll_payload(
        self,
        payload: dict[str, Any],
        *,
        strategy_version: str = SHADOW_STRATEGY_VERSION,
        now: int | None = None,
    ) -> None:
        moment = now if now is not None else int(time.time())
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO shadow_bankroll (strategy_version, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(strategy_version) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    strategy_version,
                    json.dumps(payload, separators=(",", ":"), sort_keys=True),
                    moment,
                ),
            )
            await self._db.commit()

    # ------------------------------------------------------------------
    # positions
    # ------------------------------------------------------------------
    async def open_position_for(
        self,
        mint: str,
        *,
        family: str,
        strategy_version: str = SHADOW_STRATEGY_VERSION,
    ) -> ShadowPosition | None:
        cursor = await self._db.execute(
            """
            SELECT payload_json FROM shadow_positions
            WHERE mint = ? AND family = ? AND strategy_version = ? AND closed_at IS NULL
            """,
            (mint, family, strategy_version),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return _position_from_row(row)

    async def open_positions_for_mint(
        self,
        mint: str,
        *,
        strategy_version: str = SHADOW_STRATEGY_VERSION,
    ) -> list[ShadowPosition]:
        cursor = await self._db.execute(
            """
            SELECT payload_json FROM shadow_positions
            WHERE mint = ? AND strategy_version = ? AND closed_at IS NULL
            ORDER BY opened_at ASC
            """,
            (mint, strategy_version),
        )
        return [_position_from_row(row) for row in await cursor.fetchall()]

    async def save_position(
        self,
        shadow: ShadowPosition,
        *,
        now: int | None = None,
    ) -> bool:
        """Insert or update.  A duplicate open position is refused by the index."""

        moment = now if now is not None else int(time.time())
        position = shadow.position
        statement = """
            INSERT INTO shadow_positions (
                position_id, mint, family, strategy_version, experiment_version,
                opened_at, closed_at, size_usd, entry_price_usd, entry_market_cap_usd,
                realized_net_pnl_usd, peak_net_pnl_usd, close_reason, venue,
                fill_source, graduation_state, config_hash, signal_json,
                payload_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(position_id) DO UPDATE SET
                closed_at = excluded.closed_at,
                realized_net_pnl_usd = excluded.realized_net_pnl_usd,
                peak_net_pnl_usd = excluded.peak_net_pnl_usd,
                close_reason = excluded.close_reason,
                venue = excluded.venue,
                fill_source = excluded.fill_source,
                graduation_state = excluded.graduation_state,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
        """
        values = (
            position.position_id,
            position.mint,
            shadow.family,
            position.strategy_version or SHADOW_STRATEGY_VERSION,
            shadow.experiment_version,
            position.opened_at,
            position.closed_at,
            float(position.size_usd),
            float(position.entry_price_usd),
            _float(position.entry_market_cap_usd),
            float(position.realized_net_pnl_usd),
            float(shadow.peak_net_pnl_usd),
            position.close_reason,
            shadow.venue,
            shadow.fill_source,
            shadow.graduation_state,
            position.config_hash,
            json.dumps(shadow.signal_evidence, separators=(",", ":"), sort_keys=True),
            json.dumps(shadow.to_payload(), separators=(",", ":"), sort_keys=True),
            moment,
        )
        async with self.database._write_lock:
            try:
                cursor = await self._db.execute(statement, values)
            except aiosqlite.IntegrityError:
                # The partial unique index refused a second open position for
                # this mint and family.  That is the duplicate-entry lock
                # working, not an error worth propagating.
                await self._db.rollback()
                return False
            await self._db.commit()
        return bool(cursor.rowcount)

    async def open_positions(
        self,
        *,
        strategy_version: str = SHADOW_STRATEGY_VERSION,
    ) -> list[ShadowPosition]:
        cursor = await self._db.execute(
            """
            SELECT payload_json FROM shadow_positions
            WHERE strategy_version = ? AND closed_at IS NULL
            ORDER BY opened_at ASC
            """,
            (strategy_version,),
        )
        return [_position_from_row(row) for row in await cursor.fetchall()]

    async def closed_positions(
        self,
        *,
        strategy_version: str = SHADOW_STRATEGY_VERSION,
        limit: int = 500,
    ) -> list[ShadowPosition]:
        cursor = await self._db.execute(
            """
            SELECT payload_json FROM shadow_positions
            WHERE strategy_version = ? AND closed_at IS NOT NULL
            ORDER BY closed_at DESC
            LIMIT ?
            """,
            (strategy_version, limit),
        )
        return [_position_from_row(row) for row in await cursor.fetchall()]

    async def position_rows(
        self,
        *,
        strategy_version: str = SHADOW_STRATEGY_VERSION,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            """
            SELECT position_id, mint, family, opened_at, closed_at, size_usd,
                   realized_net_pnl_usd, peak_net_pnl_usd, close_reason, venue,
                   fill_source, graduation_state
            FROM shadow_positions
            WHERE strategy_version = ?
            ORDER BY opened_at DESC
            LIMIT ?
            """,
            (strategy_version, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]

    # ------------------------------------------------------------------
    # exits
    # ------------------------------------------------------------------
    async def record_exit(
        self,
        entry: ExitJournalEntry,
        *,
        family: str = "",
        venue: str = "UNKNOWN",
    ) -> bool:
        """Append one partial/full shadow exit.  A retry is silently ignored."""

        async with self.database._write_lock:
            cursor = await self._db.execute(
                """
                INSERT OR IGNORE INTO shadow_exits (
                    position_id, sequence, mint, family, occurred_at, reason_code,
                    fraction_sold, gross_proceeds_usd, total_cost_usd, net_pnl_usd,
                    venue, final, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.position_id,
                    entry.sequence,
                    entry.mint,
                    family,
                    entry.occurred_at,
                    entry.reason_code,
                    float(entry.fraction_sold),
                    float(entry.gross_proceeds_usd),
                    float(entry.costs.total_cost_usd),
                    float(entry.realized_net_pnl_usd),
                    venue,
                    1 if entry.final else 0,
                    json.dumps(
                        {
                            "position_id": entry.position_id,
                            "sequence": entry.sequence,
                            "mint": entry.mint,
                            "family": family,
                            "venue": venue,
                            "occurred_at": entry.occurred_at,
                            "reason_code": entry.reason_code,
                            "quote_price_usd": str(entry.quote_price_usd),
                            "tokens_sold": str(entry.tokens_sold),
                            "tokens_remaining": str(entry.tokens_remaining),
                            "cost_basis_usd": str(entry.cost_basis_usd),
                            "costs": entry.costs.as_dict(),
                            "net_proceeds_usd": str(entry.net_proceeds_usd),
                            "net_pnl_usd": str(entry.realized_net_pnl_usd),
                            "gross_pnl_usd": str(entry.realized_gross_pnl_usd),
                            "final": entry.final,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
            await self._db.commit()
        return bool(cursor.rowcount)

    async def exit_rows(self, *, limit: int = 50) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            """
            SELECT position_id, sequence, mint, family, occurred_at, reason_code,
                   fraction_sold, gross_proceeds_usd, total_cost_usd, net_pnl_usd,
                   venue, final
            FROM shadow_exits
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    # ------------------------------------------------------------------
    # observations — the one stream every counterfactual reads (section 54)
    # ------------------------------------------------------------------
    async def record_observation(
        self,
        position_id: str,
        observation: ShadowObservation,
    ) -> bool:
        async with self.database._write_lock:
            cursor = await self._db.execute(
                """
                INSERT OR IGNORE INTO shadow_observations (
                    position_id, observed_at, price_usd, market_cap_usd, liquidity_usd,
                    volume_usd, momentum_score, organic_score, buys, sells,
                    independent_buyers, safety_status, route_available,
                    smart_money_distributing, smart_money_accumulating
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position_id,
                    observation.at,
                    float(observation.price_usd),
                    _float(observation.market_cap_usd),
                    _float(observation.liquidity_usd),
                    _float(observation.volume_usd),
                    _float(observation.momentum_score),
                    _float(observation.organic_score),
                    observation.buys,
                    observation.sells,
                    observation.independent_buyers,
                    observation.safety_status,
                    1 if observation.route_available else 0,
                    1 if observation.smart_money_distributing else 0,
                    1 if observation.smart_money_accumulating else 0,
                ),
            )
            await self._db.commit()
        return bool(cursor.rowcount)

    async def observations(
        self,
        position_id: str,
        *,
        limit: int = 500,
    ) -> list[ShadowObservation]:
        cursor = await self._db.execute(
            """
            SELECT * FROM shadow_observations
            WHERE position_id = ?
            ORDER BY observed_at ASC
            LIMIT ?
            """,
            (position_id, limit),
        )
        return [
            ShadowObservation(
                at=int(row["observed_at"]),
                price_usd=_decimal(row["price_usd"]),
                market_cap_usd=_decimal_or_none(row["market_cap_usd"]),
                liquidity_usd=_decimal_or_none(row["liquidity_usd"]),
                volume_usd=_decimal_or_none(row["volume_usd"]),
                momentum_score=_decimal_or_none(row["momentum_score"]),
                organic_score=_decimal_or_none(row["organic_score"]),
                buys=int(row["buys"] or 0),
                sells=int(row["sells"] or 0),
                independent_buyers=(
                    int(row["independent_buyers"])
                    if row["independent_buyers"] is not None
                    else None
                ),
                safety_status=str(row["safety_status"] or "UNKNOWN"),
                route_available=bool(row["route_available"]),
                smart_money_distributing=bool(row["smart_money_distributing"]),
                smart_money_accumulating=bool(row["smart_money_accumulating"]),
            )
            for row in await cursor.fetchall()
        ]

    # ------------------------------------------------------------------
    # venue fills (sections 23, 24, 37)
    # ------------------------------------------------------------------
    async def record_venue_fill(
        self,
        *,
        position_id: str,
        sequence: int,
        selection: RouteSelection,
        filled_at: int,
        cost_usd: Decimal = ZERO,
        net_pnl_usd: Decimal = ZERO,
    ) -> bool:
        quote = selection.chosen
        if quote is None:
            return False
        async with self.database._write_lock:
            cursor = await self._db.execute(
                """
                INSERT OR IGNORE INTO shadow_venue_fills (
                    position_id, sequence, venue, side, filled_at, notional_usd,
                    fill_price_usd, reference_price_usd, price_impact_percent,
                    slippage_bps, fee_bps, quote_latency_ms, cost_usd, net_pnl_usd,
                    deterioration_percent, fill_source, graduation_state, considered_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position_id,
                    sequence,
                    quote.venue,
                    quote.side,
                    filled_at,
                    float(quote.notional_usd),
                    _float(quote.fill_price_usd),
                    _float(quote.reference_price_usd),
                    float(quote.price_impact_percent),
                    quote.slippage_bps,
                    quote.fee_bps,
                    quote.quote_latency_ms,
                    float(cost_usd),
                    float(net_pnl_usd),
                    _float(quote.deterioration_percent),
                    quote.source,
                    quote.graduation_state,
                    json.dumps(
                        [item.as_dict() for item in selection.considered],
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
            await self._db.commit()
        return bool(cursor.rowcount)

    async def venue_fills(self, *, limit: int = 500) -> list[VenueFill]:
        cursor = await self._db.execute(
            """
            SELECT venue, side, slippage_bps, price_impact_percent, quote_latency_ms,
                   cost_usd, net_pnl_usd, deterioration_percent, fill_source
            FROM shadow_venue_fills
            ORDER BY filled_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            VenueFill(
                venue=str(row["venue"]),
                side=str(row["side"]),
                slippage_bps=int(row["slippage_bps"] or 0),
                price_impact_percent=_decimal(row["price_impact_percent"]),
                quote_latency_ms=int(row["quote_latency_ms"] or 0),
                cost_usd=_decimal(row["cost_usd"]),
                net_pnl_usd=_decimal(row["net_pnl_usd"]),
                deterioration_percent=_decimal_or_none(row["deterioration_percent"]),
                fill_source=str(row["fill_source"] or ""),
            )
            for row in await cursor.fetchall()
        ]

    async def venue_comparison_rows(
        self,
        *,
        position_id: str,
        sequence: int = 1,
        side: str = "BUY",
    ) -> list[dict[str, str]]:
        """Every venue that quoted this exact trade, chosen or not (section 24)."""

        cursor = await self._db.execute(
            """
            SELECT considered_json FROM shadow_venue_fills
            WHERE position_id = ? AND sequence = ? AND side = ?
            """,
            (position_id, sequence, side),
        )
        row = await cursor.fetchone()
        if not row:
            return []
        try:
            payload = json.loads(row["considered_json"])
        except json.JSONDecodeError:
            return []
        return [item for item in payload if isinstance(item, dict)]

    # ------------------------------------------------------------------
    # signal log
    # ------------------------------------------------------------------
    async def record_signal(
        self,
        decision: ShadowEntryDecision,
        *,
        evidence: dict[str, str] | None = None,
        route: RouteQuote | None = None,
    ) -> bool:
        """Persist every shadow verdict, accepted or refused (section 43)."""

        key = f"{decision.strategy_version}:{decision.family}:{decision.mint}:{decision.decided_at}"
        payload = {
            "reason_codes": list(decision.reason_codes),
            "notes": list(decision.notes),
            "config_hash": decision.config_hash,
            "evidence": evidence or {},
            "route": route.as_dict() if route is not None else {},
        }
        async with self.database._write_lock:
            cursor = await self._db.execute(
                """
                INSERT OR IGNORE INTO shadow_signal_log (
                    signal_key, mint, family, decided_at, accepted, reason_code,
                    size_usd, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    decision.mint,
                    decision.family,
                    decision.decided_at,
                    1 if decision.accepted else 0,
                    decision.primary_reason,
                    float(decision.size_usd),
                    json.dumps(payload, separators=(",", ":"), sort_keys=True),
                ),
            )
            await self._db.commit()
        return bool(cursor.rowcount)

    async def signal_rows(self, *, limit: int = 50) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            """
            SELECT signal_key, mint, family, decided_at, accepted, reason_code, size_usd
            FROM shadow_signal_log
            ORDER BY decided_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def refusal_counts(self, *, since: int = 0) -> dict[str, int]:
        cursor = await self._db.execute(
            """
            SELECT reason_code, COUNT(*) AS total FROM shadow_signal_log
            WHERE accepted = 0 AND decided_at >= ?
            GROUP BY reason_code
            ORDER BY total DESC
            """,
            (since,),
        )
        return {str(row["reason_code"]): int(row["total"]) for row in await cursor.fetchall()}


def _position_from_row(row: Any) -> ShadowPosition:
    try:
        payload = json.loads(row["payload_json"])
    except json.JSONDecodeError:
        payload = {}
    return ShadowPosition.from_payload(payload if isinstance(payload, dict) else {})


def positions_by_family(positions: Sequence[ShadowPosition]) -> dict[str, list[ShadowPosition]]:
    grouped: dict[str, list[ShadowPosition]] = {}
    for item in positions:
        grouped.setdefault(item.family, []).append(item)
    return grouped


def _float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _decimal(value: Any, *, default: Decimal = ZERO) -> Decimal:
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
