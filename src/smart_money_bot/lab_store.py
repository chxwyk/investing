"""Persistence for the PAPER research laboratory (sections BC, BD).

The lab package itself is deliberately storage-free, so this module is the one
place that knows SQL.  Everything here is restart-safe by construction:

* Lifecycle rows are upserted per mint, so a restart rehydrates the same history
  instead of re-discovering the token.
* Events carry a content-hash id, so a retried write is a no-op.
* Decisions are keyed by ``(mint, decided_at, strategy_version)``.
* A partial unique index allows exactly one open simulated position per mint per
  strategy, which is the duplicate-entry lock.
* Exit journal rows are keyed by ``(position_id, sequence)``, so a retried
  partial exit cannot double-sell.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any

import aiosqlite

from .database import Database
from .lab.config import STRATEGY_VERSION
from .lab.decision import TradeDecision, decision_from_json, decision_to_json
from .lab.exits import (
    ExitJournalEntry,
    PaperPosition,
    position_from_json,
    position_to_json,
)
from .lab.identity import TokenIdentity
from .lab.lifecycle import (
    PublicationState,
    TokenLifecycle,
    lifecycle_from_json,
    lifecycle_to_json,
    new_lifecycle,
)
from .lab.registry import AccountPerformance, SocialSignal, account_tier
from .lab.smartmoney import WalletReputation
from .lab.timeline import TokenEvent, TokenTimeline, event_from_json, event_to_json

ZERO = Decimal("0")


class LabStore:
    """Restart-safe storage for lifecycle, events, decisions and positions."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @property
    def _db(self) -> Any:
        return self.database.db

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def load_lifecycle(self, mint: str, *, now: int | None = None) -> TokenLifecycle:
        """Return the persisted lifecycle, or a genuinely new one.

        A missing row is the *only* way a mint becomes FIRST_DISCOVERY, so a
        restart can never reset an existing token's history.
        """

        cursor = await self._db.execute(
            "SELECT payload_json FROM lab_token_lifecycle WHERE mint = ?", (mint,)
        )
        row = await cursor.fetchone()
        if row and row["payload_json"]:
            try:
                return lifecycle_from_json(row["payload_json"])
            except (ValueError, json.JSONDecodeError):
                pass
        return new_lifecycle(mint, now=now if now is not None else int(time.time()))

    async def save_lifecycle(self, record: TokenLifecycle, *, now: int | None = None) -> None:
        moment = now if now is not None else int(time.time())
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO lab_token_lifecycle (
                    mint, state, payload_json, first_discovered_at,
                    first_surfaced_at, cycle_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mint) DO UPDATE SET
                    state = excluded.state,
                    payload_json = excluded.payload_json,
                    first_surfaced_at = COALESCE(
                        lab_token_lifecycle.first_surfaced_at, excluded.first_surfaced_at
                    ),
                    cycle_count = excluded.cycle_count,
                    updated_at = excluded.updated_at
                """,
                (
                    record.mint,
                    record.state,
                    lifecycle_to_json(record),
                    record.first_discovered_at,
                    record.first_surfaced_at,
                    record.cycle_count,
                    moment,
                ),
            )
            await self._db.commit()

    async def lifecycle_rows(self, *, limit: int = 50) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            """
            SELECT mint, state, cycle_count, first_discovered_at, first_surfaced_at, updated_at
            FROM lab_token_lifecycle
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------
    async def append_events(self, events: Iterable[TokenEvent]) -> int:
        """Append events idempotently; returns how many were genuinely new."""

        payload = [
            (
                event.event_id,
                event.mint,
                event.event_type,
                event.occurred_at,
                event_to_json(event),
            )
            for event in events
        ]
        if not payload:
            return 0
        async with self.database._write_lock:
            cursor = await self._db.executemany(
                """
                INSERT OR IGNORE INTO lab_token_events (
                    event_id, mint, event_type, occurred_at, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                payload,
            )
            await self._db.commit()
        return int(cursor.rowcount or 0)

    async def timeline(
        self,
        mint: str,
        *,
        until: int | None = None,
        limit: int = 500,
    ) -> TokenTimeline:
        """Rehydrate a mint's timeline, optionally truncated at ``until``.

        Replay always passes ``until`` so a strategy cannot observe an event
        that had not happened at the decision time being replayed.
        """

        if until is None:
            cursor = await self._db.execute(
                """
                SELECT payload_json FROM lab_token_events
                WHERE mint = ?
                ORDER BY occurred_at ASC
                LIMIT ?
                """,
                (mint, limit),
            )
        else:
            cursor = await self._db.execute(
                """
                SELECT payload_json FROM lab_token_events
                WHERE mint = ? AND occurred_at <= ?
                ORDER BY occurred_at ASC
                LIMIT ?
                """,
                (mint, until, limit),
            )
        rows = await cursor.fetchall()
        timeline = TokenTimeline(mint)
        for row in rows:
            try:
                timeline.append(event_from_json(row["payload_json"]))
            except (ValueError, json.JSONDecodeError):
                continue
        return timeline

    async def event_count(self, mint: str | None = None) -> int:
        if mint is None:
            cursor = await self._db.execute("SELECT COUNT(*) AS total FROM lab_token_events")
        else:
            cursor = await self._db.execute(
                "SELECT COUNT(*) AS total FROM lab_token_events WHERE mint = ?", (mint,)
            )
        row = await cursor.fetchone()
        return int(row["total"] or 0) if row else 0

    # ------------------------------------------------------------------
    # decisions
    # ------------------------------------------------------------------
    async def record_decision(self, decision: TradeDecision) -> bool:
        """Persist one immutable decision.  Returns ``False`` if already stored."""

        async with self.database._write_lock:
            cursor = await self._db.execute(
                """
                INSERT OR IGNORE INTO lab_decisions (
                    mint, decided_at, strategy_version, decision, reason_codes_json,
                    evidence_quality, safety_status, lifecycle_state,
                    expected_net_edge_percent, size_usd, config_hash, bot_version,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.mint,
                    decision.timestamp,
                    decision.strategy_version,
                    str(decision.decision),
                    json.dumps(list(decision.reason_codes)),
                    str(decision.evidence_quality),
                    str(decision.safety),
                    decision.lifecycle_state,
                    _float(decision.expected_net_edge_percent),
                    float(decision.size_usd),
                    decision.config_hash,
                    decision.bot_version,
                    decision_to_json(decision),
                ),
            )
            await self._db.commit()
        return bool(cursor.rowcount)

    async def recent_decisions(
        self,
        *,
        limit: int = 25,
        decisions: Sequence[str] | None = None,
        strategy_version: str = STRATEGY_VERSION,
    ) -> list[TradeDecision]:
        if decisions:
            placeholders = ",".join("?" for _ in decisions)
            cursor = await self._db.execute(
                f"""
                SELECT payload_json FROM lab_decisions
                WHERE strategy_version = ? AND decision IN ({placeholders})
                ORDER BY decided_at DESC
                LIMIT ?
                """,
                (strategy_version, *decisions, limit),
            )
        else:
            cursor = await self._db.execute(
                """
                SELECT payload_json FROM lab_decisions
                WHERE strategy_version = ?
                ORDER BY decided_at DESC
                LIMIT ?
                """,
                (strategy_version, limit),
            )
        rows = await cursor.fetchall()
        parsed: list[TradeDecision] = []
        for row in rows:
            try:
                parsed.append(decision_from_json(row["payload_json"]))
            except (ValueError, json.JSONDecodeError):
                continue
        return parsed

    async def latest_decision(
        self,
        mint: str,
        *,
        strategy_version: str = STRATEGY_VERSION,
    ) -> TradeDecision | None:
        cursor = await self._db.execute(
            """
            SELECT payload_json FROM lab_decisions
            WHERE mint = ? AND strategy_version = ?
            ORDER BY decided_at DESC LIMIT 1
            """,
            (mint, strategy_version),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        try:
            return decision_from_json(row["payload_json"])
        except (ValueError, json.JSONDecodeError):
            return None

    async def decision_counts(self, *, since: int = 0) -> dict[str, int]:
        cursor = await self._db.execute(
            """
            SELECT decision, COUNT(*) AS total FROM lab_decisions
            WHERE decided_at >= ?
            GROUP BY decision
            """,
            (since,),
        )
        return {str(row["decision"]): int(row["total"]) for row in await cursor.fetchall()}

    # ------------------------------------------------------------------
    # positions
    # ------------------------------------------------------------------
    async def open_position_for(
        self,
        mint: str,
        *,
        strategy_version: str = STRATEGY_VERSION,
    ) -> PaperPosition | None:
        cursor = await self._db.execute(
            """
            SELECT payload_json FROM lab_positions
            WHERE mint = ? AND strategy_version = ? AND closed_at IS NULL
            """,
            (mint, strategy_version),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return position_from_json(row["payload_json"])

    async def save_position(self, position: PaperPosition, *, now: int | None = None) -> bool:
        """Insert or update one simulated position.

        The first insert relies on the partial unique index to reject a second
        open position for the same mint and strategy, which is what makes a
        duplicated entry impossible even under a restart race.
        """

        moment = now if now is not None else int(time.time())
        statement = """
            INSERT INTO lab_positions (
                position_id, mint, strategy_version, opened_at, closed_at,
                size_usd, entry_price_usd, realized_net_pnl_usd, close_reason,
                is_reentry, lifecycle_state, config_hash, payload_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(position_id) DO UPDATE SET
                closed_at = excluded.closed_at,
                realized_net_pnl_usd = excluded.realized_net_pnl_usd,
                close_reason = excluded.close_reason,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
        """
        values = (
            position.position_id,
            position.mint,
            position.strategy_version or STRATEGY_VERSION,
            position.opened_at,
            position.closed_at,
            float(position.size_usd),
            float(position.entry_price_usd),
            float(position.realized_net_pnl_usd),
            position.close_reason,
            1 if position.is_reentry else 0,
            position.lifecycle_state,
            position.config_hash,
            position_to_json(position),
            moment,
        )
        async with self.database._write_lock:
            try:
                cursor = await self._db.execute(statement, values)
            except aiosqlite.IntegrityError:
                # The partial unique index refused a second open position for
                # this mint and strategy.  That is the duplicate-entry lock
                # working, not an error worth propagating to the caller.
                await self._db.rollback()
                return False
            await self._db.commit()
        return bool(cursor.rowcount)

    async def record_exit(self, entry: ExitJournalEntry) -> bool:
        """Append one partial/full exit.  A retry is silently ignored."""

        async with self.database._write_lock:
            cursor = await self._db.execute(
                """
                INSERT OR IGNORE INTO lab_exits (
                    position_id, sequence, mint, occurred_at, reason_code,
                    fraction_sold, gross_proceeds_usd, total_cost_usd, net_pnl_usd,
                    final, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.position_id,
                    entry.sequence,
                    entry.mint,
                    entry.occurred_at,
                    entry.reason_code,
                    float(entry.fraction_sold),
                    float(entry.gross_proceeds_usd),
                    float(entry.costs.total_cost_usd),
                    float(entry.realized_net_pnl_usd),
                    1 if entry.final else 0,
                    json.dumps(
                        {
                            "position_id": entry.position_id,
                            "sequence": entry.sequence,
                            "mint": entry.mint,
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

    async def open_positions(
        self,
        *,
        strategy_version: str = STRATEGY_VERSION,
    ) -> list[PaperPosition]:
        cursor = await self._db.execute(
            """
            SELECT payload_json FROM lab_positions
            WHERE strategy_version = ? AND closed_at IS NULL
            ORDER BY opened_at ASC
            """,
            (strategy_version,),
        )
        return [position_from_json(row["payload_json"]) for row in await cursor.fetchall()]

    async def closed_positions(
        self,
        *,
        strategy_version: str = STRATEGY_VERSION,
        limit: int = 200,
    ) -> list[PaperPosition]:
        cursor = await self._db.execute(
            """
            SELECT payload_json FROM lab_positions
            WHERE strategy_version = ? AND closed_at IS NOT NULL
            ORDER BY closed_at DESC
            LIMIT ?
            """,
            (strategy_version, limit),
        )
        return [position_from_json(row["payload_json"]) for row in await cursor.fetchall()]

    async def exit_rows(self, *, limit: int = 50) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            """
            SELECT position_id, sequence, mint, occurred_at, reason_code,
                   fraction_sold, gross_proceeds_usd, total_cost_usd, net_pnl_usd, final
            FROM lab_exits
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    # ------------------------------------------------------------------
    # bankroll
    # ------------------------------------------------------------------
    async def load_bankroll_payload(
        self,
        *,
        strategy_version: str = STRATEGY_VERSION,
    ) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            "SELECT payload_json FROM lab_bankroll WHERE strategy_version = ?",
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
        strategy_version: str = STRATEGY_VERSION,
        now: int | None = None,
    ) -> None:
        moment = now if now is not None else int(time.time())
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO lab_bankroll (strategy_version, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(strategy_version) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (strategy_version, json.dumps(payload, sort_keys=True), moment),
            )
            await self._db.commit()

    # ------------------------------------------------------------------
    # publication state
    # ------------------------------------------------------------------
    async def load_publication(self, mint: str) -> PublicationState | None:
        cursor = await self._db.execute(
            "SELECT payload_json FROM lab_publications WHERE mint = ?", (mint,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None
        return PublicationState(
            mint=mint,
            published_at=int(payload.get("published_at") or 0),
            lifecycle_state=str(payload.get("lifecycle_state") or "FIRST_DISCOVERY"),
            opportunity_score=_decimal(payload.get("opportunity_score")),
            momentum_score=_decimal(payload.get("momentum_score")),
            organic_score=_decimal(payload.get("organic_score")),
            safety_status=str(payload.get("safety_status") or "UNKNOWN"),
            independent_buyers=int(payload.get("independent_buyers") or 0),
            liquidity_usd=(
                Decimal(str(payload["liquidity_usd"]))
                if payload.get("liquidity_usd") is not None
                else None
            ),
            smart_wallets=int(payload.get("smart_wallets") or 0),
            decision=str(payload.get("decision") or "WAIT"),
            fingerprint=str(payload.get("fingerprint") or ""),
        )

    async def save_publication(self, state: PublicationState) -> None:
        payload = {
            "published_at": state.published_at,
            "lifecycle_state": state.lifecycle_state,
            "opportunity_score": str(state.opportunity_score),
            "momentum_score": str(state.momentum_score),
            "organic_score": str(state.organic_score),
            "safety_status": state.safety_status,
            "independent_buyers": state.independent_buyers,
            "liquidity_usd": (
                str(state.liquidity_usd) if state.liquidity_usd is not None else None
            ),
            "smart_wallets": state.smart_wallets,
            "decision": state.decision,
            "fingerprint": state.fingerprint,
        }
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO lab_publications (
                    mint, published_at, lifecycle_state, fingerprint, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(mint) DO UPDATE SET
                    published_at = excluded.published_at,
                    lifecycle_state = excluded.lifecycle_state,
                    fingerprint = excluded.fingerprint,
                    payload_json = excluded.payload_json
                """,
                (
                    state.mint,
                    state.published_at,
                    state.lifecycle_state,
                    state.fingerprint,
                    json.dumps(payload, sort_keys=True),
                ),
            )
            await self._db.commit()

    # ------------------------------------------------------------------
    # wallet reputation
    # ------------------------------------------------------------------
    async def save_reputation(self, reputation: WalletReputation) -> None:
        payload = {
            "wallet": reputation.wallet,
            "samples": reputation.samples,
            "median_forward_return_percent": _text(reputation.median_forward_return_percent),
            "hit_10_percent": _text(reputation.hit_10_percent),
            "hit_25_percent": _text(reputation.hit_25_percent),
            "hit_50_percent": _text(reputation.hit_50_percent),
            "hit_100_percent": _text(reputation.hit_100_percent),
            "median_drawdown_percent": _text(reputation.median_drawdown_percent),
            "rugs_entered": reputation.rugs_entered,
            "chase_entries": reputation.chase_entries,
            "early_entries": reputation.early_entries,
            "distribution_events": reputation.distribution_events,
            "recent_return_percent": _text(reputation.recent_return_percent),
            "score": str(reputation.score),
            "state": reputation.state,
            "updated_at": reputation.updated_at,
        }
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO lab_wallet_reputation (
                    wallet, samples, score, state, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet) DO UPDATE SET
                    samples = excluded.samples,
                    score = excluded.score,
                    state = excluded.state,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    reputation.wallet,
                    reputation.samples,
                    float(reputation.score),
                    reputation.state,
                    json.dumps(payload, sort_keys=True),
                    reputation.updated_at,
                ),
            )
            await self._db.commit()

    async def load_reputations(self, wallets: Sequence[str]) -> dict[str, WalletReputation]:
        if not wallets:
            return {}
        placeholders = ",".join("?" for _ in wallets)
        cursor = await self._db.execute(
            f"SELECT payload_json FROM lab_wallet_reputation WHERE wallet IN ({placeholders})",
            tuple(wallets),
        )
        found: dict[str, WalletReputation] = {}
        for row in await cursor.fetchall():
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                continue
            reputation = WalletReputation(
                wallet=str(payload.get("wallet") or ""),
                samples=int(payload.get("samples") or 0),
                median_forward_return_percent=_decimal_or_none(
                    payload.get("median_forward_return_percent")
                ),
                hit_10_percent=_decimal_or_none(payload.get("hit_10_percent")),
                hit_25_percent=_decimal_or_none(payload.get("hit_25_percent")),
                hit_50_percent=_decimal_or_none(payload.get("hit_50_percent")),
                hit_100_percent=_decimal_or_none(payload.get("hit_100_percent")),
                median_drawdown_percent=_decimal_or_none(payload.get("median_drawdown_percent")),
                rugs_entered=int(payload.get("rugs_entered") or 0),
                chase_entries=int(payload.get("chase_entries") or 0),
                early_entries=int(payload.get("early_entries") or 0),
                distribution_events=int(payload.get("distribution_events") or 0),
                recent_return_percent=_decimal_or_none(payload.get("recent_return_percent")),
                score=_decimal(payload.get("score"), default=Decimal("50")),
                state=str(payload.get("state") or "UNKNOWN"),
                updated_at=int(payload.get("updated_at") or 0),
            )
            if reputation.wallet:
                found[reputation.wallet] = reputation
        return found

    # ------------------------------------------------------------------
    # social signals / account learning / budget
    # ------------------------------------------------------------------
    async def record_social_signal(self, signal: SocialSignal) -> bool:
        """Store one public post.  Duplicates are ignored, not double-counted."""

        async with self.database._write_lock:
            cursor = await self._db.execute(
                """
                INSERT OR IGNORE INTO lab_social_signals (
                    dedupe_key, platform, account, tier, mint, classification,
                    observed_at, source_timestamp, exact_mint_confidence,
                    price_at_signal, market_cap_at_signal, url, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.dedupe_key,
                    signal.platform,
                    signal.account,
                    signal.tier,
                    signal.mint,
                    signal.classification,
                    signal.observed_at,
                    signal.source_timestamp,
                    float(signal.exact_mint_confidence),
                    _float(signal.price_at_signal),
                    _float(signal.market_cap_at_signal),
                    signal.url,
                    json.dumps(
                        {
                            "platform": signal.platform,
                            "account": signal.account,
                            "tier": signal.tier,
                            "mint": signal.mint,
                            "classification": signal.classification,
                            "url": signal.url,
                            "observed_at": signal.observed_at,
                            "source_timestamp": signal.source_timestamp,
                        },
                        sort_keys=True,
                    ),
                ),
            )
            await self._db.commit()
        return bool(cursor.rowcount)

    async def social_signals_for(self, mint: str, *, limit: int = 20) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            """
            SELECT account, tier, classification, url, source_timestamp,
                   price_at_signal, market_cap_at_signal, exact_mint_confidence
            FROM lab_social_signals
            WHERE mint = ?
            ORDER BY source_timestamp DESC
            LIMIT ?
            """,
            (mint, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def save_account_performance(
        self,
        performance: AccountPerformance,
        *,
        now: int | None = None,
    ) -> None:
        moment = now if now is not None else int(time.time())
        payload = {
            "account": performance.account,
            "tier": performance.tier,
            "samples": performance.samples,
            "lead_lag": performance.lead_lag,
            "classification": performance.classification,
            "median_forward_return_percent": _text(performance.median_forward_return_percent),
            "hit_10_percent": _text(performance.hit_10_percent),
            "hit_25_percent": _text(performance.hit_25_percent),
            "hit_50_percent": _text(performance.hit_50_percent),
            "hit_100_percent": _text(performance.hit_100_percent),
            "failure_rate_percent": _text(performance.failure_rate_percent),
            "strategy_weight": str(performance.strategy_weight),
        }
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO lab_account_performance (
                    account, tier, samples, classification, lead_lag,
                    strategy_weight, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account) DO UPDATE SET
                    tier = excluded.tier,
                    samples = excluded.samples,
                    classification = excluded.classification,
                    lead_lag = excluded.lead_lag,
                    strategy_weight = excluded.strategy_weight,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    performance.account,
                    performance.tier or account_tier(performance.account),
                    performance.samples,
                    performance.classification,
                    performance.lead_lag,
                    float(performance.strategy_weight),
                    json.dumps(payload, sort_keys=True),
                    moment,
                ),
            )
            await self._db.commit()

    async def account_cache(self) -> dict[str, int]:
        cursor = await self._db.execute("SELECT account, fetched_at FROM lab_account_cache")
        return {str(row["account"]): int(row["fetched_at"]) for row in await cursor.fetchall()}

    async def cache_account(self, account: str, payload: dict[str, Any], *, now: int) -> None:
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO lab_account_cache (account, fetched_at, payload_json)
                VALUES (?, ?, ?)
                ON CONFLICT(account) DO UPDATE SET
                    fetched_at = excluded.fetched_at,
                    payload_json = excluded.payload_json
                """,
                (account, now, json.dumps(payload, sort_keys=True)),
            )
            await self._db.commit()

    async def record_social_usage(
        self,
        *,
        usage_day: str,
        provider: str = "x",
        calls: int = 0,
        posts_processed: int = 0,
        cache_hits: int = 0,
        cache_misses: int = 0,
        useful_signals: int = 0,
        useless_signals: int = 0,
        estimated_cost_usd: Decimal = ZERO,
        now: int | None = None,
    ) -> None:
        moment = now if now is not None else int(time.time())
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO lab_social_budget (
                    usage_day, provider, calls, posts_processed, cache_hits,
                    cache_misses, useful_signals, useless_signals,
                    estimated_cost_usd, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(usage_day, provider) DO UPDATE SET
                    calls = lab_social_budget.calls + excluded.calls,
                    posts_processed =
                        lab_social_budget.posts_processed + excluded.posts_processed,
                    cache_hits = lab_social_budget.cache_hits + excluded.cache_hits,
                    cache_misses = lab_social_budget.cache_misses + excluded.cache_misses,
                    useful_signals =
                        lab_social_budget.useful_signals + excluded.useful_signals,
                    useless_signals =
                        lab_social_budget.useless_signals + excluded.useless_signals,
                    estimated_cost_usd =
                        lab_social_budget.estimated_cost_usd + excluded.estimated_cost_usd,
                    updated_at = excluded.updated_at
                """,
                (
                    usage_day,
                    provider,
                    calls,
                    posts_processed,
                    cache_hits,
                    cache_misses,
                    useful_signals,
                    useless_signals,
                    float(estimated_cost_usd),
                    moment,
                ),
            )
            await self._db.commit()

    async def social_usage(self, *, usage_day: str) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            """
            SELECT provider, calls, posts_processed, cache_hits, cache_misses,
                   useful_signals, useless_signals, estimated_cost_usd
            FROM lab_social_budget WHERE usage_day = ?
            """,
            (usage_day,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    # ------------------------------------------------------------------
    # identity / strategy registry
    # ------------------------------------------------------------------
    async def save_identity(self, identity: TokenIdentity) -> None:
        payload = {
            "mint": identity.mint,
            "name": identity.name,
            "symbol": identity.symbol,
            "description": identity.description,
            "image_url": identity.image_url,
            "image_fallback_reason": identity.image_fallback_reason,
            "creator": identity.creator,
            "token_age_seconds": identity.token_age_seconds,
            "pair_age_seconds": identity.pair_age_seconds,
            "links": [
                {
                    "platform": link.platform,
                    "url": link.url,
                    "source": link.source,
                    "official": link.official,
                }
                for link in identity.links
            ],
            "warnings": list(identity.warnings),
            "sources": list(identity.sources),
            "resolved_at": identity.resolved_at,
        }
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO lab_token_identity (mint, payload_json, resolved_at)
                VALUES (?, ?, ?)
                ON CONFLICT(mint) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    resolved_at = excluded.resolved_at
                """,
                (identity.mint, json.dumps(payload, sort_keys=True), identity.resolved_at),
            )
            await self._db.commit()

    async def identity_payload(self, mint: str) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            "SELECT payload_json FROM lab_token_identity WHERE mint = ?", (mint,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    async def register_strategy(
        self,
        *,
        strategy_version: str,
        role: str,
        config_hash: str,
        calibration_cutoff_at: int = 0,
        now: int | None = None,
    ) -> None:
        moment = now if now is not None else int(time.time())
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO lab_strategy_registry (
                    strategy_version, role, config_hash, activated_at,
                    calibration_cutoff_at, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, '{}', ?)
                ON CONFLICT(strategy_version) DO UPDATE SET
                    role = excluded.role,
                    config_hash = excluded.config_hash,
                    calibration_cutoff_at = excluded.calibration_cutoff_at,
                    updated_at = excluded.updated_at
                """,
                (strategy_version, role, config_hash, moment, calibration_cutoff_at, moment),
            )
            await self._db.commit()

    async def strategy_rows(self) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            """
            SELECT strategy_version, role, config_hash, activated_at, calibration_cutoff_at
            FROM lab_strategy_registry ORDER BY activated_at ASC
            """
        )
        return [dict(row) for row in await cursor.fetchall()]


def position_with_strategy(position: PaperPosition, strategy_version: str) -> PaperPosition:
    if position.strategy_version:
        return position
    return replace(position, strategy_version=strategy_version)


def _float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal(value: Any, *, default: Decimal = ZERO) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
