"""SQL for the Trending lane.  The :mod:`smart_money_bot.trending` package stays storage-free.

Same split as the shadow experiment: strategy logic is pure and testable, and
every statement that touches a database lives here.

Two persistence rules are enforced in this file rather than left to callers:

* **First observations are write-once.**  The ledger upsert deliberately does not
  update ``first_seen_at``, ``first_rank``, ``first_market_cap_usd``,
  ``first_holder_count`` or ``first_top10_percent``.  Those columns are set by the
  initial ``INSERT OR IGNORE`` and are never in an ``UPDATE SET`` clause, so no
  code path — including a bug — can rewrite what the entry numbers were.
* **Restart safety.**  Everything the loops need to resume is reconstructable
  from these tables: the ledger, recent snapshots (for rank velocity), and any
  still-active hot watches with their original timing evidence intact.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

from .database import Database
from .trending.hotwatch import (
    HOT_WATCH_ACTIVE,
    HotWatchEntry,
)
from .trending.hotwatch import (
    entry_from_json as hot_watch_from_json,
)
from .trending.ledger import (
    RankPoint,
    TrendingLedgerEntry,
    TrendingObservation,
    entry_from_json,
)
from .trending.thesis import AuthorReputation, ThesisAssessment

ZERO = Decimal("0")


def _f(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _d(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _dumps(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)


class TrendingStore:
    """Persistence for the Trending ledger, hot watches, theses and diagnostics."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @property
    def _db(self) -> Any:
        return self.database.db

    # ------------------------------------------------------------------
    # the ledger (section 5)
    # ------------------------------------------------------------------
    async def record_observation(
        self,
        entry: TrendingLedgerEntry,
        observation: TrendingObservation | None = None,
    ) -> None:
        """Upsert the ledger row and append the raw snapshot.

        The ``INSERT`` carries the first-observation columns; the ``DO UPDATE``
        clause deliberately omits every one of them.
        """

        payload = entry.to_json()
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO trending_tokens (
                    mint, name, symbol, fomo_token_id, fomo_url, source_kind,
                    first_seen_at, first_rank, first_market_cap_usd,
                    first_holder_count, first_top10_percent,
                    current_rank, best_rank, current_market_cap_usd, peak_market_cap_usd,
                    change_window, verification, entries, seconds_on_board, on_board,
                    exited_at, last_observed_at, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mint) DO UPDATE SET
                    name = excluded.name,
                    symbol = excluded.symbol,
                    fomo_token_id = excluded.fomo_token_id,
                    fomo_url = excluded.fomo_url,
                    source_kind = excluded.source_kind,
                    current_rank = excluded.current_rank,
                    best_rank = excluded.best_rank,
                    current_market_cap_usd = excluded.current_market_cap_usd,
                    peak_market_cap_usd = excluded.peak_market_cap_usd,
                    change_window = excluded.change_window,
                    verification = excluded.verification,
                    entries = excluded.entries,
                    seconds_on_board = excluded.seconds_on_board,
                    on_board = excluded.on_board,
                    exited_at = excluded.exited_at,
                    last_observed_at = excluded.last_observed_at,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    entry.mint,
                    entry.name,
                    entry.symbol,
                    entry.fomo_token_id,
                    entry.fomo_url,
                    entry.source.kind,
                    entry.first_seen_at,
                    entry.first_rank,
                    _f(entry.first_market_cap_usd),
                    entry.first_holder_count,
                    _f(entry.first_top10_percent),
                    entry.current_rank,
                    entry.best_rank,
                    _f(entry.current_market_cap_usd),
                    _f(entry.peak_market_cap_usd),
                    entry.change_window,
                    entry.verification,
                    entry.entries,
                    entry.seconds_on_board,
                    1 if entry.on_board else 0,
                    entry.exited_at,
                    entry.last_observed_at,
                    _dumps(payload),
                    entry.last_observed_at,
                ),
            )
            if observation is not None:
                await self._db.execute(
                    """
                    INSERT OR IGNORE INTO trending_snapshots (
                        mint, observed_at, rank, market_cap_usd, price_usd, liquidity_usd,
                        holder_count, top10_percent, displayed_change_percent,
                        change_window, source_kind
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation.mint,
                        observation.observed_at,
                        observation.rank,
                        _f(observation.market_cap_usd),
                        _f(observation.price_usd),
                        _f(observation.liquidity_usd),
                        observation.holder_count,
                        _f(observation.top10_percent),
                        _f(observation.displayed_change_percent),
                        observation.change_window,
                        observation.source.kind,
                    ),
                )
            await self._db.commit()

    #: The columns the upsert never updates.  They are the authority on what the
    #: first observation was; the JSON payload is only a convenient carrier for
    #: the mutable rest of the record.
    _IMMUTABLE_COLUMNS: tuple[str, ...] = (
        "first_seen_at",
        "first_rank",
        "first_market_cap_usd",
        "first_holder_count",
        "first_top10_percent",
    )

    async def load_entry(self, mint: str) -> TrendingLedgerEntry | None:
        cursor = await self._db.execute(
            """
            SELECT payload_json, first_seen_at, first_rank, first_market_cap_usd,
                   first_holder_count, first_top10_percent
            FROM trending_tokens WHERE mint = ?
            """,
            (mint,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._entry_from_row(row)

    @classmethod
    def _entry_from_row(cls, row: Any) -> TrendingLedgerEntry | None:
        """Rebuild an entry, with the protected columns overriding the payload.

        The upsert replaces ``payload_json`` wholesale, so the JSON alone is not
        a safe source for the first-observation fields — a caller that handed us
        a corrupted entry would round-trip its corruption.  The columns are the
        ones the upsert never touches, so they win on read.  That is what makes
        "the entry numbers can never move" true against *any* caller rather than
        only against well-behaved ones.
        """

        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict) or not payload.get("mint"):
            return None
        for column in cls._IMMUTABLE_COLUMNS:
            payload[column] = row[column]
        return entry_from_json(payload)

    async def load_board(self, *, limit: int = 100) -> list[TrendingLedgerEntry]:
        """Everything currently on the board, best rank first (section 111)."""

        cursor = await self._db.execute(
            """
            SELECT payload_json, first_seen_at, first_rank, first_market_cap_usd,
                   first_holder_count, first_top10_percent
            FROM trending_tokens
            WHERE on_board = 1
            ORDER BY CASE WHEN current_rank IS NULL THEN 1 ELSE 0 END, current_rank ASC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        entries: list[TrendingLedgerEntry] = []
        for row in rows:
            entry = self._entry_from_row(row)
            if entry is not None:
                entries.append(entry)
        return entries

    async def mark_left_board(self, mints: tuple[str, ...], *, at: int) -> None:
        """Record an exit.  Nothing is deleted — the history is the product."""

        if not mints:
            return
        placeholders = ",".join("?" for _ in mints)
        async with self.database._write_lock:
            await self._db.execute(
                f"""
                UPDATE trending_tokens
                SET on_board = 0, exited_at = ?, current_rank = NULL, updated_at = ?
                WHERE mint IN ({placeholders}) AND on_board = 1
                """,
                (at, at, *mints),
            )
            await self._db.commit()

    async def rank_history(
        self,
        mint: str,
        *,
        since: int = 0,
        limit: int = 64,
    ) -> tuple[RankPoint, ...]:
        """Recent rank readings, oldest first, for the velocity calculation."""

        cursor = await self._db.execute(
            """
            SELECT observed_at, rank FROM trending_snapshots
            WHERE mint = ? AND rank IS NOT NULL AND observed_at >= ?
            ORDER BY observed_at DESC LIMIT ?
            """,
            (mint, since, limit),
        )
        rows = await cursor.fetchall()
        return tuple(
            reversed(
                [RankPoint(at=int(row["observed_at"]), rank=int(row["rank"])) for row in rows]
            )
        )

    async def previous_market_cap(
        self,
        mint: str,
        *,
        before: int,
    ) -> tuple[Decimal | None, int]:
        """The last market cap recorded before ``before``, and its age in seconds."""

        cursor = await self._db.execute(
            """
            SELECT observed_at, market_cap_usd FROM trending_snapshots
            WHERE mint = ? AND observed_at < ? AND market_cap_usd IS NOT NULL
            ORDER BY observed_at DESC LIMIT 1
            """,
            (mint, before),
        )
        row = await cursor.fetchone()
        if not row:
            return None, 0
        return _d(row["market_cap_usd"]), max(0, before - int(row["observed_at"]))

    async def prune_snapshots(self, *, older_than: int) -> int:
        """Bound snapshot growth without touching the ledger's own history."""

        async with self.database._write_lock:
            cursor = await self._db.execute(
                "DELETE FROM trending_snapshots WHERE observed_at < ?", (older_than,)
            )
            await self._db.commit()
        return cursor.rowcount or 0

    # ------------------------------------------------------------------
    # events (section 7)
    # ------------------------------------------------------------------
    async def record_event(
        self,
        mint: str,
        *,
        state: str,
        occurred_at: int,
        rank: int | None = None,
        rank_delta: int = 0,
        market_cap_usd: Decimal | None = None,
        move_percent: Decimal | None = None,
        score: Decimal | None = None,
        reasons: tuple[str, ...] = (),
        payload: dict[str, Any] | None = None,
    ) -> None:
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT OR IGNORE INTO trending_events (
                    mint, state, occurred_at, rank, rank_delta, market_cap_usd,
                    move_percent, score, reasons_json, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mint,
                    state,
                    occurred_at,
                    rank,
                    rank_delta,
                    _f(market_cap_usd),
                    _f(move_percent),
                    _f(score),
                    _dumps(list(reasons)),
                    _dumps(payload or {}),
                ),
            )
            await self._db.commit()

    async def recent_events(self, *, limit: int = 20) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            """
            SELECT mint, state, occurred_at, rank, rank_delta, market_cap_usd,
                   move_percent, score, reasons_json
            FROM trending_events ORDER BY occurred_at DESC LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def event_counts(self, *, since: int = 0) -> dict[str, int]:
        cursor = await self._db.execute(
            """
            SELECT state, COUNT(*) AS total FROM trending_events
            WHERE occurred_at >= ? GROUP BY state
            """,
            (since,),
        )
        rows = await cursor.fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    # ------------------------------------------------------------------
    # hot watch (sections 41-50, 90)
    # ------------------------------------------------------------------
    async def save_hot_watch(self, entry: HotWatchEntry) -> None:
        moment = int(time.time())
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO trending_hot_watch (
                    mint, entered_at, origin, state, expires_at, entry_score,
                    best_score, last_score, rechecks, last_recheck_at, promoted_at,
                    resolved_at, hot_watch_market_cap_usd, promotion_market_cap_usd,
                    payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mint, entered_at) DO UPDATE SET
                    state = excluded.state,
                    best_score = excluded.best_score,
                    last_score = excluded.last_score,
                    rechecks = excluded.rechecks,
                    last_recheck_at = excluded.last_recheck_at,
                    promoted_at = excluded.promoted_at,
                    resolved_at = excluded.resolved_at,
                    promotion_market_cap_usd = excluded.promotion_market_cap_usd,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    entry.mint,
                    entry.entered_at,
                    entry.origin,
                    entry.state,
                    entry.expires_at,
                    float(entry.entry_score),
                    float(entry.best_score),
                    float(entry.last_score),
                    entry.rechecks,
                    entry.last_recheck_at,
                    entry.promoted_at,
                    entry.resolved_at,
                    _f(entry.hot_watch_market_cap_usd),
                    _f(entry.promotion_market_cap_usd),
                    _dumps(entry.to_json()),
                    moment,
                ),
            )
            await self._db.commit()

    async def active_hot_watches(self) -> list[HotWatchEntry]:
        """Restore live hot watches after a restart, timing evidence intact."""

        cursor = await self._db.execute(
            """
            SELECT payload_json FROM trending_hot_watch
            WHERE state = ? ORDER BY entered_at ASC
            """,
            (HOT_WATCH_ACTIVE,),
        )
        rows = await cursor.fetchall()
        entries: list[HotWatchEntry] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("mint"):
                entries.append(hot_watch_from_json(payload))
        return entries

    async def hot_watch_history(self, *, limit: int = 200) -> list[HotWatchEntry]:
        cursor = await self._db.execute(
            "SELECT payload_json FROM trending_hot_watch ORDER BY entered_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        entries: list[HotWatchEntry] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("mint"):
                entries.append(hot_watch_from_json(payload))
        return entries

    # ------------------------------------------------------------------
    # theses (sections 19-26)
    # ------------------------------------------------------------------
    async def save_thesis(self, assessment: ThesisAssessment) -> None:
        record = assessment.record
        moment = int(time.time())
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO trending_theses (
                    thesis_id, mint, author, posted_at, source, category, quality,
                    timing, specificity, cluster_id, cluster_leader,
                    market_cap_at_thesis_usd, penalties_json, text, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thesis_id) DO UPDATE SET
                    category = excluded.category,
                    quality = excluded.quality,
                    timing = excluded.timing,
                    specificity = excluded.specificity,
                    cluster_id = excluded.cluster_id,
                    cluster_leader = excluded.cluster_leader,
                    penalties_json = excluded.penalties_json,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    record.thesis_id,
                    record.mint,
                    record.author,
                    record.posted_at,
                    record.source,
                    assessment.category,
                    assessment.quality,
                    assessment.timing,
                    assessment.specificity,
                    assessment.cluster_id,
                    1 if assessment.cluster_leader else 0,
                    _f(record.market_cap_at_thesis_usd),
                    _dumps(list(assessment.penalties)),
                    record.text[:2000],
                    _dumps(assessment.to_json()),
                    moment,
                ),
            )
            await self._db.commit()

    async def theses_for(self, mint: str, *, limit: int = 25) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            """
            SELECT * FROM trending_theses WHERE mint = ?
            ORDER BY posted_at DESC LIMIT ?
            """,
            (mint, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def save_author_reputation(self, reputation: AuthorReputation) -> None:
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO trending_thesis_authors (
                    author, sample, avg_forward_move_percent, avg_mfe_percent,
                    avg_mae_percent, severe_failures, rug_exposures, late_theses, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(author) DO UPDATE SET
                    sample = excluded.sample,
                    avg_forward_move_percent = excluded.avg_forward_move_percent,
                    avg_mfe_percent = excluded.avg_mfe_percent,
                    avg_mae_percent = excluded.avg_mae_percent,
                    severe_failures = excluded.severe_failures,
                    rug_exposures = excluded.rug_exposures,
                    late_theses = excluded.late_theses,
                    updated_at = excluded.updated_at
                """,
                (
                    reputation.author,
                    reputation.sample,
                    _f(reputation.avg_forward_move_percent),
                    _f(reputation.avg_mfe_percent),
                    _f(reputation.avg_mae_percent),
                    reputation.severe_failures,
                    reputation.rug_exposures,
                    reputation.late_theses,
                    int(time.time()),
                ),
            )
            await self._db.commit()

    async def author_reputations(self) -> dict[str, AuthorReputation]:
        cursor = await self._db.execute("SELECT * FROM trending_thesis_authors")
        rows = await cursor.fetchall()
        return {
            str(row["author"]): AuthorReputation(
                author=str(row["author"]),
                sample=int(row["sample"] or 0),
                avg_forward_move_percent=_d(row["avg_forward_move_percent"]),
                avg_mfe_percent=_d(row["avg_mfe_percent"]),
                avg_mae_percent=_d(row["avg_mae_percent"]),
                severe_failures=int(row["severe_failures"] or 0),
                rug_exposures=int(row["rug_exposures"] or 0),
                late_theses=int(row["late_theses"] or 0),
            )
            for row in rows
        }

    # ------------------------------------------------------------------
    # About / project validation (sections 16-18)
    # ------------------------------------------------------------------
    async def save_about(
        self,
        mint: str,
        *,
        summary: str,
        claims: tuple[str, ...],
        website: str,
        has_official_claim: bool,
        external_state: str,
        token_link: str,
        mentions_exact_mint: bool,
        payload: dict[str, Any] | None = None,
    ) -> None:
        moment = int(time.time())
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO trending_about (
                    mint, summary, claims_json, website, has_official_claim,
                    external_state, token_link, mentions_exact_mint, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mint) DO UPDATE SET
                    summary = excluded.summary,
                    claims_json = excluded.claims_json,
                    website = excluded.website,
                    has_official_claim = excluded.has_official_claim,
                    external_state = excluded.external_state,
                    token_link = excluded.token_link,
                    mentions_exact_mint = excluded.mentions_exact_mint,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    mint,
                    summary[:1000],
                    _dumps(list(claims)),
                    website[:500],
                    1 if has_official_claim else 0,
                    external_state,
                    token_link,
                    1 if mentions_exact_mint else 0,
                    _dumps(payload or {}),
                    moment,
                ),
            )
            await self._db.commit()

    async def about_for(self, mint: str) -> dict[str, Any] | None:
        cursor = await self._db.execute("SELECT * FROM trending_about WHERE mint = ?", (mint,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # latency, suppression and misses (sections 79-82, 91)
    # ------------------------------------------------------------------
    async def stamp_latency(
        self,
        mint: str,
        stage: str,
        *,
        at: int,
        market_cap_usd: Decimal | None = None,
    ) -> None:
        """Write-once per stage: a stamp that could move measures nothing."""

        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT OR IGNORE INTO trending_latency (mint, stage, occurred_at, market_cap_usd)
                VALUES (?, ?, ?, ?)
                """,
                (mint, stage, at, _f(market_cap_usd)),
            )
            await self._db.commit()

    async def latency_rows(self, *, limit: int = 500) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            """
            SELECT mint, stage, occurred_at, market_cap_usd FROM trending_latency
            ORDER BY occurred_at DESC LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def record_suppression(
        self,
        mint: str,
        *,
        reason_code: str,
        at: int,
        score: Decimal | None = None,
        market_cap_usd: Decimal | None = None,
        detail: str = "",
    ) -> None:
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT OR IGNORE INTO trending_suppression (
                    mint, reason_code, occurred_at, score, market_cap_usd, detail
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (mint, reason_code, at, _f(score), _f(market_cap_usd), detail[:300]),
            )
            await self._db.commit()

    async def suppression_counts(self, *, since: int = 0) -> dict[str, int]:
        cursor = await self._db.execute(
            """
            SELECT reason_code, COUNT(*) AS total FROM trending_suppression
            WHERE occurred_at >= ? GROUP BY reason_code ORDER BY total DESC
            """,
            (since,),
        )
        rows = await cursor.fetchall()
        return {str(row["reason_code"]): int(row["total"]) for row in rows}

    async def record_missed(
        self,
        mint: str,
        *,
        miss_class: str,
        observed_at: int,
        market_cap_at_observation_usd: Decimal | None,
        peak_market_cap_usd: Decimal | None,
        move_percent: Decimal | None,
        suppression_reason: str = "",
        detail: str = "",
    ) -> None:
        async with self.database._write_lock:
            await self._db.execute(
                """
                INSERT INTO trending_missed (
                    mint, miss_class, observed_at, market_cap_at_observation_usd,
                    peak_market_cap_usd, move_percent, suppression_reason, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mint, miss_class) DO UPDATE SET
                    peak_market_cap_usd = excluded.peak_market_cap_usd,
                    move_percent = excluded.move_percent,
                    suppression_reason = excluded.suppression_reason,
                    detail = excluded.detail
                """,
                (
                    mint,
                    miss_class,
                    observed_at,
                    _f(market_cap_at_observation_usd),
                    _f(peak_market_cap_usd),
                    _f(move_percent),
                    suppression_reason,
                    detail[:300],
                ),
            )
            await self._db.commit()

    async def missed_rows(self, *, limit: int = 50) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            "SELECT * FROM trending_missed ORDER BY observed_at DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def tracked_count(self) -> int:
        cursor = await self._db.execute(
            "SELECT COUNT(*) AS total FROM trending_tokens WHERE on_board = 1"
        )
        row = await cursor.fetchone()
        return int(row["total"]) if row else 0
