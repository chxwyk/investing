"""SQL for the pre-trend lane.  The :mod:`smart_money_bot.pretrend` package stays storage-free.

Same split the Trending lane uses: the strategy and statistics are pure and
testable, and every statement that touches a database lives here.

Three persistence rules are enforced in this file rather than trusted to callers.

**First observations are write-once.**  ``pretrend_membership.first_trending_at``,
``pretrend_source_first_seen.first_seen_at`` and
``pretrend_actor_observations.observed_at`` are written by ``INSERT OR IGNORE``
and appear in no ``UPDATE SET`` clause in this module.  A later reading cannot
move them, so the answer to "when did we first see this, and how early were we?"
cannot drift.

**Observations are append-only.**  ``pretrend_observations`` is keyed by
``(mint, observed_at)`` and written with ``INSERT OR IGNORE``.  Re-writing a past
observation with a present value is the quiet way a "historical" dataset becomes
a snapshot of now.

**Predictions are recorded whether or not they alerted.**  A scoreboard built
only from published predictions measures the publishing rule, not the model.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from .database import Database
from .pretrend.activity import ActivityEvent, ActivityTape
from .pretrend.affinity import AffinityObservation, AffinityRecord
from .pretrend.features import FeatureVector, MarketSeries, PretrendState
from .pretrend.groundtruth import (
    BoardRow,
    MembershipRecord,
    SnapshotValidity,
    TrendEntryEvent,
    TrendingSnapshot,
)
from .pretrend.states import TokenState

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


def _loads(raw: Any, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(str(raw))
    except ValueError:
        return fallback


class PretrendStore:
    """Persistence for board snapshots, ground truth, observations and predictions."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @property
    def _db(self) -> Any:
        return self.database.db

    # ------------------------------------------------------------------
    # board snapshots and ground truth
    # ------------------------------------------------------------------
    async def record_snapshot(
        self, snapshot: TrendingSnapshot, validity: SnapshotValidity
    ) -> None:
        """Store the reading and, when valid, its rows.

        An invalid snapshot is still stored — with ``valid = 0`` and its reason —
        because a gap in the ground truth is only interpretable if the failures
        that caused it are on the record.
        """

        await self._db.execute(
            """
            INSERT OR REPLACE INTO pretrend_board_snapshots (
                observed_at, provider, source_kind, valid, invalid_reason,
                invalid_detail, row_count, error, source_at, collector_version,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.observed_at,
                snapshot.provider,
                snapshot.source_kind,
                1 if validity.valid else 0,
                validity.reason,
                validity.detail[:500],
                len(snapshot.rows),
                snapshot.error[:500],
                snapshot.source_at,
                snapshot.collector_version,
                _dumps({"validity": validity.to_json()}),
            ),
        )
        if validity.valid:
            await self._db.executemany(
                """
                INSERT OR IGNORE INTO pretrend_board_rows (
                    observed_at, mint, rank, symbol, name, tier, market_cap_usd,
                    price_usd, liquidity_usd, volume_usd, holders,
                    token_age_seconds, pair_age_seconds, source_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot.observed_at,
                        row.mint,
                        row.rank,
                        row.symbol,
                        row.name,
                        row.tier,
                        _f(row.market_cap_usd),
                        _f(row.price_usd),
                        _f(row.liquidity_usd),
                        _f(row.volume_usd),
                        row.holders,
                        row.token_age_seconds,
                        row.pair_age_seconds,
                        row.source_at,
                    )
                    for row in snapshot.rows
                ],
            )
        await self._db.commit()

    async def record_trend_events(self, events: Sequence[TrendEntryEvent]) -> None:
        """Append ground-truth events.  ``INSERT OR IGNORE`` makes replay safe."""

        if not events:
            return
        await self._db.executemany(
            """
            INSERT OR IGNORE INTO pretrend_trend_events (
                mint, kind, occurred_at, symbol, name, initial_rank, tier,
                market_cap_usd, price_usd, liquidity_usd, volume_usd, holders,
                token_age_seconds, pair_age_seconds, provider, source_kind,
                source_at, collector_at, collector_version, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    event.mint,
                    event.kind,
                    event.occurred_at,
                    event.symbol,
                    event.name,
                    event.initial_rank,
                    event.tier,
                    _f(event.market_cap_usd),
                    _f(event.price_usd),
                    _f(event.liquidity_usd),
                    _f(event.volume_usd),
                    event.holders,
                    event.token_age_seconds,
                    event.pair_age_seconds,
                    event.provider,
                    event.source_kind,
                    event.source_at,
                    event.collector_at,
                    event.collector_version,
                    _dumps(dict(event.raw)),
                )
                for event in events
            ],
        )
        await self._db.commit()

    async def upsert_membership(self, records: Sequence[MembershipRecord]) -> None:
        """Write membership state.

        ``first_trending_at``, ``first_rank`` and ``first_market_cap_usd`` are
        set by the INSERT and are deliberately absent from the UPDATE clause, so
        a re-entry — or a bug — cannot rewrite what the entry numbers were.
        """

        if not records:
            return
        now = int(time.time())
        for record in records:
            await self._db.execute(
                """
                INSERT INTO pretrend_membership (
                    mint, first_trending_at, first_rank, first_market_cap_usd,
                    state, last_seen_on_board_at, left_at, entries, stints_json,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mint) DO UPDATE SET
                    state = excluded.state,
                    last_seen_on_board_at = excluded.last_seen_on_board_at,
                    left_at = excluded.left_at,
                    entries = excluded.entries,
                    stints_json = excluded.stints_json,
                    updated_at = excluded.updated_at
                """,
                (
                    record.mint,
                    record.first_trending_at,
                    record.first_rank,
                    _f(record.first_market_cap_usd),
                    record.state,
                    record.last_seen_on_board_at,
                    record.left_at,
                    record.entries,
                    _dumps([list(stint) for stint in record.stints]),
                    now,
                ),
            )
        await self._db.commit()

    async def load_membership(self) -> tuple[MembershipRecord, ...]:
        cursor = await self._db.execute(
            "SELECT * FROM pretrend_membership ORDER BY first_trending_at"
        )
        rows = await cursor.fetchall()
        return tuple(
            MembershipRecord(
                mint=row["mint"],
                first_trending_at=int(row["first_trending_at"]),
                first_rank=row["first_rank"],
                first_market_cap_usd=_d(row["first_market_cap_usd"]),
                state=row["state"],
                last_seen_on_board_at=int(row["last_seen_on_board_at"] or 0),
                left_at=row["left_at"],
                entries=int(row["entries"] or 1),
                stints=tuple(
                    (int(item[0]), None if item[1] is None else int(item[1]))
                    for item in _loads(row["stints_json"], [])
                    if isinstance(item, list) and len(item) == 2
                ),
            )
            for row in rows
        )

    async def first_trending_map(self) -> dict[str, int]:
        """``mint -> first_trending_at`` for labelling.  The ground-truth index."""

        cursor = await self._db.execute(
            "SELECT mint, first_trending_at FROM pretrend_membership"
        )
        rows = await cursor.fetchall()
        return {row["mint"]: int(row["first_trending_at"]) for row in rows}

    async def trend_entries(
        self, *, since: int = 0, limit: int = 200
    ) -> tuple[dict[str, Any], ...]:
        cursor = await self._db.execute(
            """
            SELECT * FROM pretrend_trend_events
            WHERE kind = 'FOMO_TREND_ENTER' AND occurred_at >= ?
            ORDER BY occurred_at DESC LIMIT ?
            """,
            (since, limit),
        )
        rows = await cursor.fetchall()
        return tuple(dict(row) for row in rows)

    async def trend_entry(self, mint: str) -> TrendEntryEvent | None:
        cursor = await self._db.execute(
            """
            SELECT * FROM pretrend_trend_events
            WHERE mint = ? AND kind = 'FOMO_TREND_ENTER'
            ORDER BY occurred_at LIMIT 1
            """,
            (mint,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return TrendEntryEvent(
            kind=row["kind"],
            mint=row["mint"],
            occurred_at=int(row["occurred_at"]),
            symbol=row["symbol"],
            name=row["name"],
            initial_rank=row["initial_rank"],
            tier=row["tier"],
            market_cap_usd=_d(row["market_cap_usd"]),
            price_usd=_d(row["price_usd"]),
            liquidity_usd=_d(row["liquidity_usd"]),
            volume_usd=_d(row["volume_usd"]),
            holders=row["holders"],
            token_age_seconds=row["token_age_seconds"],
            pair_age_seconds=row["pair_age_seconds"],
            provider=row["provider"],
            source_kind=row["source_kind"],
            source_at=row["source_at"],
            collector_at=int(row["collector_at"] or 0),
            collector_version=row["collector_version"],
            raw=_loads(row["raw_json"], {}),
        )

    async def board_rows_for(
        self, mint: str, *, limit: int = 500
    ) -> tuple[BoardRow, ...]:
        cursor = await self._db.execute(
            """
            SELECT * FROM pretrend_board_rows WHERE mint = ?
            ORDER BY observed_at LIMIT ?
            """,
            (mint, limit),
        )
        rows = await cursor.fetchall()
        return tuple(
            BoardRow(
                mint=row["mint"],
                rank=row["rank"],
                symbol=row["symbol"],
                name=row["name"],
                tier=row["tier"],
                market_cap_usd=_d(row["market_cap_usd"]),
                price_usd=_d(row["price_usd"]),
                liquidity_usd=_d(row["liquidity_usd"]),
                volume_usd=_d(row["volume_usd"]),
                holders=row["holders"],
                source_at=row["source_at"],
            )
            for row in rows
        )

    async def snapshot_health(self, *, since: int = 0) -> dict[str, Any]:
        cursor = await self._db.execute(
            """
            SELECT valid, invalid_reason, COUNT(*) AS n
            FROM pretrend_board_snapshots WHERE observed_at >= ?
            GROUP BY valid, invalid_reason
            """,
            (since,),
        )
        rows = await cursor.fetchall()
        accepted = sum(int(row["n"]) for row in rows if row["valid"])
        rejected = sum(int(row["n"]) for row in rows if not row["valid"])
        total = accepted + rejected
        return {
            "accepted": accepted,
            "rejected": rejected,
            "acceptance_rate": None if total == 0 else round(accepted / total, 4),
            "rejections": {
                row["invalid_reason"]: int(row["n"]) for row in rows if not row["valid"]
            },
        }

    # ------------------------------------------------------------------
    # append-only candidate observations
    # ------------------------------------------------------------------
    async def record_observation(
        self,
        *,
        mint: str,
        observed_at: int,
        provider: str = "",
        collector_version: str = "",
        source_at: int | None = None,
        received_at: int | None = None,
        price_usd: Decimal | None = None,
        market_cap_usd: Decimal | None = None,
        liquidity_usd: Decimal | None = None,
        volume_usd: Decimal | None = None,
        buys: int | None = None,
        sells: int | None = None,
        unique_buyers: int | None = None,
        holders: int | None = None,
        net_flow_usd: Decimal | None = None,
        social_engagement: Decimal | None = None,
        token_age_seconds: int | None = None,
        pair_age_seconds: int | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        """Append one point-in-time reading.  Never updates an existing row."""

        await self._db.execute(
            """
            INSERT OR IGNORE INTO pretrend_observations (
                mint, observed_at, source_at, received_at, provider,
                collector_version, price_usd, market_cap_usd, liquidity_usd,
                volume_usd, buys, sells, unique_buyers, holders, net_flow_usd,
                social_engagement, token_age_seconds, pair_age_seconds, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mint,
                observed_at,
                source_at,
                received_at if received_at is not None else observed_at,
                provider,
                collector_version,
                _f(price_usd),
                _f(market_cap_usd),
                _f(liquidity_usd),
                _f(volume_usd),
                buys,
                sells,
                unique_buyers,
                holders,
                _f(net_flow_usd),
                _f(social_engagement),
                token_age_seconds,
                pair_age_seconds,
                _dumps(dict(payload or {})),
            ),
        )
        await self._db.commit()

    async def observations_for(
        self,
        mint: str,
        *,
        until: int | None = None,
        since: int | None = None,
        limit: int = 5_000,
    ) -> tuple[dict[str, Any], ...]:
        """Rows for one mint, bounded in SQL rather than by the caller.

        Both bounds are applied in the query so a replay cannot forget one.

        The ``LIMIT`` selects the **most recent** rows and the result is then
        re-sorted ascending.  Taking the oldest rows instead — the obvious
        spelling — would silently drop the recent history on any long-lived
        mint, which is precisely the history every window feature reads, and
        the features would quietly degrade rather than fail.

        ``since`` exists because the feature ladder never looks back further
        than twice its longest window.  Reading a token's entire life on every
        scoring cycle is pure waste at 60 candidates every 30 seconds.
        """

        clauses = ["mint = ?"]
        params: list[Any] = [mint]
        if until is not None:
            clauses.append("observed_at <= ?")
            params.append(until)
        if since is not None:
            clauses.append("observed_at >= ?")
            params.append(since)
        params.append(limit)
        cursor = await self._db.execute(
            f"""
            SELECT * FROM pretrend_observations
            WHERE {" AND ".join(clauses)}
            ORDER BY observed_at DESC LIMIT ?
            """,
            tuple(params),
        )
        rows = await cursor.fetchall()
        return tuple(dict(row) for row in reversed(rows))

    async def market_series_for(
        self, mint: str, *, until: int | None = None, since: int | None = None
    ) -> MarketSeries:
        """Rebuild the append-only market series for one mint."""

        series = MarketSeries()
        for row in await self.observations_for(mint, until=until, since=since):
            at = int(row["observed_at"])
            source_at = row["source_at"]
            provider = row["provider"] or ""
            for column, target in (
                ("price_usd", series.price_usd),
                ("market_cap_usd", series.market_cap_usd),
                ("liquidity_usd", series.liquidity_usd),
                ("volume_usd", series.volume_usd),
                ("buys", series.buys),
                ("sells", series.sells),
                ("unique_buyers", series.unique_buyers),
                ("holders", series.holders),
                ("net_flow_usd", series.net_flow_usd),
                ("social_engagement", series.social_engagement),
            ):
                target.add(at, row[column], source_at=source_at, provider=provider)
        return series

    async def observation_mints(
        self, *, since: int, limit: int = 500
    ) -> tuple[str, ...]:
        cursor = await self._db.execute(
            """
            SELECT mint, MAX(observed_at) AS last_at FROM pretrend_observations
            WHERE observed_at >= ? GROUP BY mint ORDER BY last_at DESC LIMIT ?
            """,
            (since, limit),
        )
        rows = await cursor.fetchall()
        return tuple(row["mint"] for row in rows)

    # ------------------------------------------------------------------
    # FOMO-native activity
    # ------------------------------------------------------------------
    async def record_activity(self, events: Sequence[ActivityEvent]) -> int:
        """Append tape events.  ``INSERT OR IGNORE`` on ``event_id`` de-duplicates."""

        if not events:
            return 0
        await self._db.executemany(
            """
            INSERT OR IGNORE INTO pretrend_activity_events (
                event_id, mint, occurred_at, received_at, event_type, trader_id,
                handle, profile_url, amount_usd, token_amount, market_cap_usd,
                price_usd, thesis_text, token_name, token_symbol, chain,
                provider, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    event.event_id,
                    event.mint,
                    event.occurred_at,
                    event.received_at,
                    event.event_type,
                    event.trader_id,
                    event.handle,
                    event.profile_url,
                    _f(event.amount_usd),
                    _f(event.token_amount),
                    _f(event.market_cap_usd),
                    _f(event.price_usd),
                    event.thesis_text,
                    event.token_name,
                    event.token_symbol,
                    event.chain,
                    event.provider,
                    _dumps(dict(event.raw)),
                )
                for event in events
            ],
        )
        # The actor/mint first-action index.  INSERT OR IGNORE makes it
        # write-once: a later action never moves the first-action timestamp.
        await self._db.executemany(
            """
            INSERT OR IGNORE INTO pretrend_actor_observations (
                actor_id, mint, surface, observed_at,
                market_cap_at_observation_usd, token_age_seconds, handle
            ) VALUES (?, ?, 'fomo_activity', ?, ?, NULL, ?)
            """,
            [
                (
                    event.trader_id,
                    event.mint,
                    event.occurred_at,
                    _f(event.market_cap_usd),
                    event.handle,
                )
                for event in events
                if event.trader_id
            ],
        )
        await self._db.commit()
        return len(events)

    async def activity_tape(
        self,
        mint: str,
        *,
        until: int | None = None,
        since: int | None = None,
        limit: int = 5_000,
    ) -> ActivityTape:
        clauses = ["mint = ?"]
        params: list[Any] = [mint]
        if until is not None:
            clauses.append("occurred_at <= ?")
            params.append(until)
        if since is not None:
            clauses.append("occurred_at >= ?")
            params.append(since)
        params.append(limit)
        cursor = await self._db.execute(
            f"""
            SELECT * FROM pretrend_activity_events
            WHERE {" AND ".join(clauses)}
            ORDER BY occurred_at DESC LIMIT ?
            """,
            tuple(params),
        )
        rows = reversed(await cursor.fetchall())
        tape = ActivityTape(mint)
        for row in rows:
            tape.add(
                ActivityEvent(
                    event_id=row["event_id"],
                    mint=row["mint"],
                    occurred_at=int(row["occurred_at"]),
                    event_type=row["event_type"],
                    trader_id=row["trader_id"],
                    handle=row["handle"],
                    profile_url=row["profile_url"],
                    amount_usd=_d(row["amount_usd"]),
                    token_amount=_d(row["token_amount"]),
                    market_cap_usd=_d(row["market_cap_usd"]),
                    price_usd=_d(row["price_usd"]),
                    thesis_text=row["thesis_text"],
                    token_name=row["token_name"],
                    token_symbol=row["token_symbol"],
                    chain=row["chain"],
                    provider=row["provider"],
                    received_at=row["received_at"],
                )
            )
        return tape

    async def actor_observations(
        self, *, surface: str = "fomo_activity", limit: int = 100_000
    ) -> tuple[AffinityObservation, ...]:
        """Every (actor, mint) first action, joined to ground truth.

        The join is a LEFT JOIN on purpose: a mint with no membership row never
        entered the board, which is a *negative* observation and must be in the
        denominator.  An inner join here would compute every actor's precision
        against only their winners — the single most flattering bug available.
        """

        cursor = await self._db.execute(
            """
            SELECT a.actor_id, a.mint, a.observed_at, a.surface, a.handle,
                   a.market_cap_at_observation_usd, a.token_age_seconds,
                   m.first_trending_at, m.first_market_cap_usd
            FROM pretrend_actor_observations a
            LEFT JOIN pretrend_membership m ON m.mint = a.mint
            WHERE a.surface = ?
            ORDER BY a.observed_at LIMIT ?
            """,
            (surface, limit),
        )
        rows = await cursor.fetchall()
        return tuple(
            AffinityObservation(
                actor_id=row["actor_id"],
                mint=row["mint"],
                observed_at=int(row["observed_at"]),
                trend_entered_at=row["first_trending_at"],
                market_cap_at_observation_usd=_d(row["market_cap_at_observation_usd"]),
                market_cap_at_entry_usd=_d(row["first_market_cap_usd"]),
                token_age_seconds=row["token_age_seconds"],
                surface=row["surface"],
            )
            for row in rows
        )

    async def actor_handles(self, *, surface: str = "fomo_activity") -> dict[str, str]:
        cursor = await self._db.execute(
            """
            SELECT actor_id, MAX(handle) AS handle FROM pretrend_actor_observations
            WHERE surface = ? AND handle != '' GROUP BY actor_id
            """,
            (surface,),
        )
        rows = await cursor.fetchall()
        return {row["actor_id"]: row["handle"] for row in rows}

    async def save_affinity(
        self, records: Iterable[AffinityRecord], *, computed_at: int | None = None
    ) -> int:
        moment = computed_at if computed_at is not None else int(time.time())
        payload = [
            (
                record.actor_id,
                record.surface or "fomo_activity",
                record.handle,
                record.observations,
                record.recent_observations,
                record.median_lead_seconds,
                _f(record.median_entry_market_cap_usd),
                1 if record.statistically_meaningful else 0,
                _dumps(
                    {
                        str(key): value.to_json()
                        for key, value in record.horizons.items()
                    }
                ),
                moment,
            )
            for record in records
        ]
        if not payload:
            return 0
        await self._db.executemany(
            """
            INSERT OR REPLACE INTO pretrend_affinity (
                actor_id, surface, handle, observations, recent_observations,
                median_lead_seconds, median_entry_market_cap_usd,
                statistically_meaningful, horizons_json, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        await self._db.commit()
        return len(payload)

    async def top_affinity(
        self, *, limit: int = 20, surface: str = "fomo_activity"
    ) -> tuple[dict[str, Any], ...]:
        cursor = await self._db.execute(
            """
            SELECT * FROM pretrend_affinity
            WHERE surface = ? AND statistically_meaningful = 1
            ORDER BY observations DESC LIMIT ?
            """,
            (surface, limit),
        )
        rows = await cursor.fetchall()
        return tuple(dict(row) for row in rows)

    # ------------------------------------------------------------------
    # cross-source first-seen
    # ------------------------------------------------------------------
    async def note_first_seen(
        self,
        mint: str,
        source: str,
        *,
        at: int,
        market_cap_usd: Decimal | None = None,
    ) -> None:
        """Write-once per (mint, source).  A later sighting never overwrites."""

        await self._db.execute(
            """
            INSERT OR IGNORE INTO pretrend_source_first_seen (
                mint, source, first_seen_at, market_cap_at_first_seen_usd
            ) VALUES (?, ?, ?, ?)
            """,
            (mint, source, at, _f(market_cap_usd)),
        )
        await self._db.commit()

    async def first_seen_for(self, mint: str) -> dict[str, int]:
        cursor = await self._db.execute(
            "SELECT source, first_seen_at FROM pretrend_source_first_seen WHERE mint = ?",
            (mint,),
        )
        rows = await cursor.fetchall()
        return {row["source"]: int(row["first_seen_at"]) for row in rows}

    # ------------------------------------------------------------------
    # predictions
    # ------------------------------------------------------------------
    async def record_prediction(
        self,
        *,
        prediction_id: str,
        mint: str,
        predicted_at: int,
        model_version: str,
        feature_version: str,
        lane: str,
        horizon_seconds: int,
        probability: Decimal,
        calibration_bucket: str,
        sample_support: int,
        missing_features: int,
        market_cap_usd: Decimal | None,
        state: str,
        alerted: bool,
        reason_codes: Sequence[tuple[str, Decimal]] = (),
        vector: FeatureVector | None = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT OR REPLACE INTO pretrend_predictions (
                prediction_id, mint, predicted_at, model_version, feature_version,
                lane, horizon_seconds, probability, calibration_bucket,
                sample_support, missing_features, market_cap_usd, state, alerted,
                reason_codes_json, outcome
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                COALESCE(
                    (SELECT outcome FROM pretrend_predictions
                     WHERE prediction_id = ?),
                    'PENDING'
                ))
            """,
            (
                prediction_id,
                mint,
                predicted_at,
                model_version,
                feature_version,
                lane,
                horizon_seconds,
                float(probability),
                calibration_bucket,
                sample_support,
                missing_features,
                _f(market_cap_usd),
                state,
                1 if alerted else 0,
                _dumps([[name, str(value)] for name, value in reason_codes]),
                prediction_id,
            ),
        )
        if vector is not None:
            await self._db.execute(
                """
                INSERT OR REPLACE INTO pretrend_prediction_features (
                    prediction_id, mint, observed_at, feature_version, mc_cohort,
                    age_cohort, completeness, missing_json, values_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prediction_id,
                    vector.mint,
                    vector.observed_at,
                    vector.feature_version,
                    vector.mc_cohort,
                    vector.age_cohort,
                    float(vector.completeness),
                    _dumps(sorted(vector.missing)),
                    _dumps(
                        {
                            name: (None if value is None else str(value))
                            for name, value in vector.values.items()
                        }
                    ),
                ),
            )
        await self._db.commit()

    async def resolve_predictions(self, *, now: int, max_horizon: int = 1_200) -> int:
        """Settle pending predictions against ground truth.

        A prediction is only resolved once its horizon has fully elapsed.
        Resolving early would score a 20-minute call after four minutes and
        record most of them as failures.
        """

        cursor = await self._db.execute(
            """
            SELECT p.prediction_id, p.mint, p.predicted_at, p.horizon_seconds,
                   m.first_trending_at
            FROM pretrend_predictions p
            LEFT JOIN pretrend_membership m ON m.mint = p.mint
            WHERE p.outcome = 'PENDING' AND p.predicted_at + p.horizon_seconds <= ?
            LIMIT 5000
            """,
            (now,),
        )
        rows = await cursor.fetchall()
        resolved = 0
        for row in rows:
            entered = row["first_trending_at"]
            predicted_at = int(row["predicted_at"])
            horizon = int(row["horizon_seconds"])
            hit = (
                entered is not None
                and predicted_at < int(entered) <= predicted_at + horizon
            )
            await self._db.execute(
                """
                UPDATE pretrend_predictions
                SET outcome = ?, resolved_at = ?, trend_entered_at = ?,
                    lead_seconds = ?
                WHERE prediction_id = ?
                """,
                (
                    "HIT" if hit else "MISS",
                    now,
                    entered,
                    None if entered is None else int(entered) - predicted_at,
                    row["prediction_id"],
                ),
            )
            resolved += 1
        await self._db.commit()
        return resolved

    async def prediction_metrics(
        self, *, since: int = 0, lane: str = "production", threshold: float = 0.2
    ) -> dict[str, Any]:
        """Out-of-sample precision, base rate and lift over resolved predictions."""

        cursor = await self._db.execute(
            """
            SELECT probability, outcome, alerted, lead_seconds
            FROM pretrend_predictions
            WHERE lane = ? AND predicted_at >= ? AND outcome IN ('HIT', 'MISS')
            """,
            (lane, since),
        )
        rows = await cursor.fetchall()
        total = len(rows)
        hits = sum(1 for row in rows if row["outcome"] == "HIT")
        alerts = [row for row in rows if row["probability"] >= threshold]
        alert_hits = sum(1 for row in alerts if row["outcome"] == "HIT")
        leads = sorted(
            int(row["lead_seconds"])
            for row in rows
            if row["outcome"] == "HIT" and row["lead_seconds"] is not None
        )
        base_rate = None if total == 0 else hits / total
        precision = None if not alerts else alert_hits / len(alerts)
        return {
            "lane": lane,
            "resolved": total,
            "positives": hits,
            "base_rate": None if base_rate is None else round(base_rate, 6),
            "alerts": len(alerts),
            "alert_hits": alert_hits,
            "precision": None if precision is None else round(precision, 6),
            "lift": (
                None
                if not precision or not base_rate
                else round(precision / base_rate, 2)
            ),
            "median_lead_seconds": (
                None if not leads else leads[len(leads) // 2]
            ),
            "threshold": threshold,
            "sufficient": hits >= 30 and total >= 500,
        }

    async def recent_predictions(
        self, *, mint: str | None = None, limit: int = 25, lane: str = "production"
    ) -> tuple[dict[str, Any], ...]:
        if mint:
            cursor = await self._db.execute(
                """
                SELECT * FROM pretrend_predictions WHERE mint = ? AND lane = ?
                ORDER BY predicted_at DESC LIMIT ?
                """,
                (mint, lane, limit),
            )
        else:
            cursor = await self._db.execute(
                """
                SELECT * FROM pretrend_predictions WHERE lane = ?
                ORDER BY predicted_at DESC LIMIT ?
                """,
                (lane, limit),
            )
        rows = await cursor.fetchall()
        return tuple(dict(row) for row in rows)

    async def false_positives(
        self, *, limit: int = 15, threshold: float = 0.2
    ) -> tuple[dict[str, Any], ...]:
        cursor = await self._db.execute(
            """
            SELECT * FROM pretrend_predictions
            WHERE outcome = 'MISS' AND alerted = 1 AND probability >= ?
            ORDER BY probability DESC, predicted_at DESC LIMIT ?
            """,
            (threshold, limit),
        )
        rows = await cursor.fetchall()
        return tuple(dict(row) for row in rows)

    async def missed_trends(self, *, limit: int = 15) -> tuple[dict[str, Any], ...]:
        """Board entries we never alerted on — the false negatives.

        The subquery bound is ``predicted_at < first_trending_at``: a prediction
        made *after* the entry is not a catch, and counting it as one would turn
        every reaction into a prediction.
        """

        cursor = await self._db.execute(
            """
            SELECT e.mint, e.occurred_at, e.symbol, e.name, e.initial_rank,
                   e.market_cap_usd, m.first_trending_at
            FROM pretrend_trend_events e
            JOIN pretrend_membership m ON m.mint = e.mint
            WHERE e.kind = 'FOMO_TREND_ENTER'
              AND NOT EXISTS (
                SELECT 1 FROM pretrend_alert_events a
                WHERE a.mint = e.mint AND a.sent_at < m.first_trending_at
                  AND a.kind = 'PRE_TREND'
              )
            ORDER BY e.occurred_at DESC LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return tuple(dict(row) for row in rows)

    # ------------------------------------------------------------------
    # token state and alerts
    # ------------------------------------------------------------------
    async def save_state(self, state: TokenState) -> None:
        await self._db.execute(
            """
            INSERT OR REPLACE INTO pretrend_token_state (
                mint, state, entered_state_at, first_seen_at, last_evaluated_at,
                best_probability, last_probability, last_band, last_alert_at,
                alerts_sent, suppressed, alerted_quality_traders, cooldown_until,
                trend_confirmed_at, first_pretrend_alert_at,
                first_pretrend_probability, first_pretrend_market_cap_usd,
                history_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.mint,
                state.state,
                state.entered_state_at,
                state.first_seen_at,
                state.last_evaluated_at,
                float(state.best_probability),
                float(state.last_probability),
                state.last_band,
                state.last_alert_at,
                state.alerts_sent,
                state.suppressed,
                state.alerted_quality_traders,
                state.cooldown_until,
                state.trend_confirmed_at,
                state.first_pretrend_alert_at,
                _f(state.first_pretrend_probability),
                _f(state.first_pretrend_market_cap_usd),
                _dumps([list(entry) for entry in state.history[-50:]]),
                int(time.time()),
            ),
        )
        await self._db.commit()

    async def load_states(self, *, limit: int = 2_000) -> tuple[TokenState, ...]:
        cursor = await self._db.execute(
            "SELECT * FROM pretrend_token_state ORDER BY last_evaluated_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return tuple(
            TokenState(
                mint=row["mint"],
                state=row["state"],
                entered_state_at=int(row["entered_state_at"] or 0),
                first_seen_at=int(row["first_seen_at"] or 0),
                last_evaluated_at=int(row["last_evaluated_at"] or 0),
                best_probability=_d(row["best_probability"]) or ZERO,
                last_probability=_d(row["last_probability"]) or ZERO,
                last_band=int(row["last_band"] or 0),
                last_alert_at=row["last_alert_at"],
                alerts_sent=int(row["alerts_sent"] or 0),
                suppressed=int(row["suppressed"] or 0),
                alerted_quality_traders=int(row["alerted_quality_traders"] or 0),
                cooldown_until=int(row["cooldown_until"] or 0),
                trend_confirmed_at=row["trend_confirmed_at"],
                first_pretrend_alert_at=row["first_pretrend_alert_at"],
                first_pretrend_probability=_d(row["first_pretrend_probability"]),
                first_pretrend_market_cap_usd=_d(row["first_pretrend_market_cap_usd"]),
                history=tuple(
                    (int(item[0]), str(item[1]), str(item[2]))
                    for item in _loads(row["history_json"], [])
                    if isinstance(item, list) and len(item) == 3
                ),
            )
            for row in rows
        )

    async def record_alert(
        self,
        *,
        alert_id: str,
        mint: str,
        sent_at: int,
        kind: str,
        reason: str,
        probability: Decimal | None,
        market_cap_usd: Decimal | None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT OR IGNORE INTO pretrend_alert_events (
                alert_id, mint, sent_at, kind, reason, probability,
                market_cap_usd, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert_id,
                mint,
                sent_at,
                kind,
                reason,
                _f(probability),
                _f(market_cap_usd),
                _dumps(dict(payload or {})),
            ),
        )
        await self._db.commit()

    async def recent_alert_times(self, *, since: int) -> tuple[int, ...]:
        """Alert timestamps for restoring the rate budget after a restart."""

        cursor = await self._db.execute(
            "SELECT sent_at FROM pretrend_alert_events WHERE sent_at >= ? AND kind = 'PRE_TREND'",
            (since,),
        )
        rows = await cursor.fetchall()
        return tuple(int(row["sent_at"]) for row in rows)

    async def alert_rate(self, *, since: int) -> dict[str, Any]:
        cursor = await self._db.execute(
            """
            SELECT kind, COUNT(*) AS n, MIN(sent_at) AS first_at, MAX(sent_at) AS last_at
            FROM pretrend_alert_events WHERE sent_at >= ? GROUP BY kind
            """,
            (since,),
        )
        rows = await cursor.fetchall()
        result: dict[str, Any] = {"by_kind": {}}
        for row in rows:
            span = int(row["last_at"] or 0) - int(row["first_at"] or 0)
            result["by_kind"][row["kind"]] = {
                "count": int(row["n"]),
                "per_hour": (
                    None if span <= 0 else round(int(row["n"]) * 3600 / span, 2)
                ),
                "span_seconds": span,
            }
        return result

    # ------------------------------------------------------------------
    # models
    # ------------------------------------------------------------------
    async def save_model(
        self,
        *,
        model_id: str,
        name: str,
        lane: str,
        horizon_seconds: int,
        feature_version: str,
        trained_at: int,
        training_cutoff_at: int,
        trained_rows: int,
        trained_positives: int,
        threshold: Decimal,
        active: bool,
        metrics: Mapping[str, Any],
        payload: str,
    ) -> None:
        if active:
            # Exactly one active model per lane.  Two would make "which model
            # produced this alert?" unanswerable.
            await self._db.execute(
                "UPDATE pretrend_models SET active = 0 WHERE lane = ?", (lane,)
            )
        await self._db.execute(
            """
            INSERT OR REPLACE INTO pretrend_models (
                model_id, name, lane, horizon_seconds, feature_version,
                trained_at, training_cutoff_at, trained_rows, trained_positives,
                threshold, active, metrics_json, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model_id,
                name,
                lane,
                horizon_seconds,
                feature_version,
                trained_at,
                training_cutoff_at,
                trained_rows,
                trained_positives,
                float(threshold),
                1 if active else 0,
                _dumps(dict(metrics)),
                payload,
            ),
        )
        await self._db.commit()

    async def active_model(self, *, lane: str = "production") -> dict[str, Any] | None:
        cursor = await self._db.execute(
            """
            SELECT * FROM pretrend_models WHERE lane = ? AND active = 1
            ORDER BY trained_at DESC LIMIT 1
            """,
            (lane,),
        )
        row = await cursor.fetchone()
        return None if row is None else dict(row)

    async def model_health(self) -> tuple[dict[str, Any], ...]:
        cursor = await self._db.execute(
            """
            SELECT model_id, name, lane, horizon_seconds, feature_version,
                   trained_at, training_cutoff_at, trained_rows,
                   trained_positives, threshold, active, metrics_json
            FROM pretrend_models ORDER BY trained_at DESC LIMIT 20
            """
        )
        rows = await cursor.fetchall()
        return tuple(dict(row) for row in rows)

    # ------------------------------------------------------------------
    # paper / shadow
    # ------------------------------------------------------------------
    async def open_paper_observation(
        self,
        *,
        observation_id: str,
        mint: str,
        signalled_at: int,
        entry_price_usd: Decimal | None,
        entry_market_cap_usd: Decimal | None,
        entry_liquidity_usd: Decimal | None,
        probability: Decimal | None,
    ) -> None:
        await self._db.execute(
            """
            INSERT OR IGNORE INTO pretrend_paper_observations (
                observation_id, mint, signalled_at, entry_price_usd,
                entry_market_cap_usd, entry_liquidity_usd, probability,
                outcome, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
            """,
            (
                observation_id,
                mint,
                signalled_at,
                _f(entry_price_usd),
                _f(entry_market_cap_usd),
                _f(entry_liquidity_usd),
                _f(probability),
                int(time.time()),
            ),
        )
        await self._db.commit()

    async def update_paper_observation(
        self,
        observation_id: str,
        *,
        market_cap_usd: Decimal | None = None,
        trend_entered_at: int | None = None,
        market_cap_at_trend_usd: Decimal | None = None,
        outcome: str | None = None,
        now: int | None = None,
    ) -> None:
        """Track excursions.  Only the extremes move, and only in one direction."""

        moment = now if now is not None else int(time.time())
        cursor = await self._db.execute(
            "SELECT * FROM pretrend_paper_observations WHERE observation_id = ?",
            (observation_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return
        favourable = _d(row["max_favourable_market_cap_usd"])
        adverse = _d(row["max_adverse_market_cap_usd"])
        if market_cap_usd is not None:
            favourable = (
                market_cap_usd if favourable is None else max(favourable, market_cap_usd)
            )
            adverse = (
                market_cap_usd if adverse is None else min(adverse, market_cap_usd)
            )
        seconds_to_trend = (
            None
            if trend_entered_at is None
            else trend_entered_at - int(row["signalled_at"])
        )
        await self._db.execute(
            """
            UPDATE pretrend_paper_observations
            SET max_favourable_market_cap_usd = ?,
                max_adverse_market_cap_usd = ?,
                trend_entered_at = COALESCE(?, trend_entered_at),
                seconds_to_trend = COALESCE(?, seconds_to_trend),
                market_cap_at_trend_usd = COALESCE(?, market_cap_at_trend_usd),
                outcome = COALESCE(?, outcome),
                resolved_at = CASE WHEN ? IS NULL THEN resolved_at ELSE ? END,
                updated_at = ?
            WHERE observation_id = ?
            """,
            (
                _f(favourable),
                _f(adverse),
                trend_entered_at,
                seconds_to_trend,
                _f(market_cap_at_trend_usd),
                outcome,
                outcome,
                moment,
                moment,
                observation_id,
            ),
        )
        await self._db.commit()

    async def paper_summary(self, *, since: int = 0) -> dict[str, Any]:
        cursor = await self._db.execute(
            """
            SELECT outcome, COUNT(*) AS n, AVG(seconds_to_trend) AS avg_lead
            FROM pretrend_paper_observations WHERE signalled_at >= ?
            GROUP BY outcome
            """,
            (since,),
        )
        rows = await cursor.fetchall()
        return {
            row["outcome"]: {
                "count": int(row["n"]),
                "avg_seconds_to_trend": (
                    None if row["avg_lead"] is None else round(float(row["avg_lead"]), 1)
                ),
            }
            for row in rows
        }

    async def open_paper_observations(
        self, *, limit: int = 200
    ) -> tuple[dict[str, Any], ...]:
        cursor = await self._db.execute(
            """
            SELECT * FROM pretrend_paper_observations WHERE outcome = 'OPEN'
            ORDER BY signalled_at DESC LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return tuple(dict(row) for row in rows)

    # ------------------------------------------------------------------
    async def rebuild_state(
        self, mint: str, *, until: int | None = None, since: int | None = None
    ) -> PretrendState:
        """Reconstruct a mint's full point-in-time state from storage.

        This is what replay and forensics consume.  The ``until`` bound is
        pushed all the way down into SQL, so the reconstruction cannot contain a
        row the caller meant to exclude.
        """

        market = await self.market_series_for(mint, until=until, since=since)
        tape = await self.activity_tape(mint, until=until, since=since)
        first_seen = await self.first_seen_for(mint)
        moment = until if until is not None else int(time.time())

        arrivals = []
        seen: set[str] = set()
        for event in tape.before(moment):
            if not event.trader_id or event.trader_id in seen:
                continue
            seen.add(event.trader_id)
            from .pretrend.independence import Arrival

            arrivals.append(
                Arrival(
                    actor_id=event.trader_id,
                    at=event.occurred_at,
                    amount_usd=event.amount_usd,
                )
            )

        return PretrendState(
            mint=mint,
            at=moment,
            market=market,
            tape=tape if len(tape) else None,
            arrivals=tuple(arrivals),
            first_seen_by_source={
                source: at for source, at in first_seen.items() if at <= moment
            },
        )
