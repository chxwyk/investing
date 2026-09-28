"""SQL for the Early Traction lane.  The :mod:`smart_money_bot.traction` package stays pure.

Same split the rest of this repository uses: the profile, the blocks and the
latency maths are pure and testable, and every statement that touches a database
lives here.

Three persistence rules are enforced in this file rather than trusted to callers.

**One alert per mint, forever.**  ``traction_alerts.mint`` is the primary key and
:meth:`TractionStore.claim_alert` uses ``INSERT OR IGNORE``, returning whether the
row was actually created.  That makes the dedupe check and the dedupe claim the
same atomic operation — a check-then-insert would let two concurrent evaluations
of the same mint both pass the check.  It also survives restarts by construction,
because the claim is in the database rather than in a set on the runtime.

**Forward outcomes reuse the existing tables, and this file does not write
them.**  ``runner_candidates`` and ``runner_outcomes`` already hold the v2.34
forward-observation history with the exact horizons this lane wants, so Early
Traction contributes to it rather than starting a parallel one.  But
``runner_candidates.payload_json`` is a typed ``RunnerCandidate`` blob owned by
the runner lane, and an earlier version of this file wrote its own shape into it
with raw SQL.  That looked harmless and was not: ``runner_due_mints`` picks the
row up 45 seconds later, ``runner_candidate_from_json`` raises ``KeyError:
'first'`` on the foreign payload, and the runner outcome loop wedges on that mint
forever while emitting an error card every poll.  So registration now goes
through the runner lane's own writer, from the engine, and this file only records
*that* it happened -- see :meth:`mark_forward_registered`.

**X-link reuse is answered from our own history.**  ``traction_x_links`` is
append-only, keyed ``(handle, mint)``, so "this account is on six tokens" costs
one indexed query and no provider credit.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

from .database import Database
from .traction.candidate import TractionCandidate
from .traction.latency import LatencySample
from .traction.profile import ProfileVerdict
from .traction.xlink import ReuseReport, XLinkAssessment


def _f(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _dumps(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)


class TractionStore:
    """Persistence for Early Traction alerts, X-link history and latency."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @property
    def _db(self) -> Any:
        return self.database.db

    # ------------------------------------------------------------------
    # dedupe
    # ------------------------------------------------------------------
    async def claim_alert(
        self, candidate: TractionCandidate, *, now: int | None = None
    ) -> bool:
        """Atomically claim the right to alert this mint.

        Returns ``True`` exactly once per mint, for all time.  The claim and the
        check are one statement on purpose: a ``SELECT`` followed by an
        ``INSERT`` leaves a window in which two evaluations of the same mint both
        decide to send, which is precisely the duplicate the operator asked to be
        impossible.
        """

        moment = now if now is not None else int(time.time())
        observation = candidate.observation
        assessment = (
            candidate.x_link.assessment if candidate.x_link is not None else None
        )
        cursor = await self._db.execute(
            """
            INSERT OR IGNORE INTO traction_alerts (
                mint, alerted_at, launchpad, migration_state, name, symbol,
                chain_created_at, detected_at, market_cap_at_alert_usd,
                volume_at_alert_usd, liquidity_at_alert_usd, age_at_alert_seconds,
                x_link, x_handle, x_link_class, momentum_state, quality_state,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.mint,
                moment,
                candidate.launchpad,
                candidate.migration_state,
                observation.name,
                observation.symbol,
                observation.chain_created_at,
                candidate.detected_at,
                _f(observation.market_cap_usd),
                _f(observation.volume_usd),
                _f(observation.liquidity_usd),
                candidate.age_seconds(now=moment),
                observation.x_link,
                "" if assessment is None else assessment.handle,
                "" if assessment is None else assessment.link_class,
                candidate.momentum.state,
                candidate.quality.state,
                _dumps(candidate.to_json()),
            ),
        )
        await self._db.commit()
        return bool(cursor.rowcount)

    async def release_alert(self, mint: str) -> bool:
        """Give back a claim whose card was never delivered.

        Without this, a send that fails permanently would hold the dedupe row
        forever and the token could never be alerted again -- turning a transient
        Discord outage into silent, permanent data loss. The claim is released
        only when delivery definitively failed, never on a successful send.
        """

        cursor = await self._db.execute(
            "DELETE FROM traction_alerts WHERE mint = ? AND discord_message_id IS NULL",
            (mint,),
        )
        await self._db.commit()
        return bool(cursor.rowcount)

    async def alert_row(self, mint: str) -> dict[str, Any] | None:
        """One alert as stored, so a restart can rebuild what it needs from it."""

        cursor = await self._db.execute(
            "SELECT * FROM traction_alerts WHERE mint = ?", (mint,)
        )
        row = await cursor.fetchone()
        return None if row is None else dict(row)

    async def already_alerted(self, mint: str) -> bool:
        cursor = await self._db.execute(
            "SELECT 1 FROM traction_alerts WHERE mint = ?", (mint,)
        )
        return await cursor.fetchone() is not None

    async def alerted_mints(self, *, since: int = 0) -> frozenset[str]:
        """Warm the runtime's in-memory guard after a restart."""

        cursor = await self._db.execute(
            "SELECT mint FROM traction_alerts WHERE alerted_at >= ?", (since,)
        )
        return frozenset(row["mint"] for row in await cursor.fetchall())

    async def record_discord_message(
        self, mint: str, *, channel_id: int | None, message_id: int | None
    ) -> None:
        """Remember where the card landed so enrichment can edit it.

        Persisted rather than held only in memory: a redeploy between the send
        and the enrichment would otherwise lose the handle and the safety block
        would silently never arrive.
        """

        await self._db.execute(
            """
            UPDATE traction_alerts
            SET discord_channel_id = ?, discord_message_id = ?
            WHERE mint = ?
            """,
            (channel_id, message_id, mint),
        )
        await self._db.commit()

    async def mark_enriched(
        self, mint: str, *, at: int | None = None, payload: dict[str, Any] | None = None
    ) -> None:
        moment = at if at is not None else int(time.time())
        if payload is None:
            await self._db.execute(
                "UPDATE traction_alerts SET enriched_at = ? WHERE mint = ?",
                (moment, mint),
            )
        else:
            await self._db.execute(
                """
                UPDATE traction_alerts SET enriched_at = ?, payload_json = ?
                WHERE mint = ?
                """,
                (moment, _dumps(payload), mint),
            )
        await self._db.commit()

    async def pending_enrichment(
        self, *, older_than: int = 0, limit: int = 50
    ) -> tuple[dict[str, Any], ...]:
        """Alerts whose safety block never arrived, so a restart can retry them."""

        cursor = await self._db.execute(
            """
            SELECT * FROM traction_alerts
            WHERE enriched_at IS NULL AND alerted_at >= ?
            ORDER BY alerted_at DESC LIMIT ?
            """,
            (older_than, limit),
        )
        return tuple(dict(row) for row in await cursor.fetchall())

    # ------------------------------------------------------------------
    # X-link history and reuse
    # ------------------------------------------------------------------
    async def note_x_link(
        self, mint: str, assessment: XLinkAssessment, *, at: int | None = None
    ) -> None:
        """Append this mint's X identity.  Write-once per (handle, mint)."""

        if not assessment.handle and not assessment.tweet_id:
            return
        moment = at if at is not None else int(time.time())
        await self._db.execute(
            """
            INSERT OR IGNORE INTO traction_x_links (
                handle, mint, first_seen_at, link_class, tweet_id, raw_link
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                assessment.handle or f"tweet:{assessment.tweet_id}",
                mint,
                moment,
                assessment.link_class,
                assessment.tweet_id,
                assessment.raw[:500],
            ),
        )
        await self._db.commit()

    async def x_link_reuse(
        self,
        mint: str,
        assessment: XLinkAssessment,
        *,
        window_seconds: int = 7 * 86_400,
        now: int | None = None,
    ) -> ReuseReport:
        """How many OTHER mints carry this same X identity.

        Excludes the subject mint, so a token is never reported as reusing its
        own link.  Bounded by a window because an account legitimately
        associated with a project two years ago is not the same signal as one
        attached to four tokens this afternoon.
        """

        moment = now if now is not None else int(time.time())
        since = moment - window_seconds
        others: list[str] = []
        tweet_others: list[str] = []

        if assessment.handle:
            cursor = await self._db.execute(
                """
                SELECT DISTINCT mint FROM traction_x_links
                WHERE handle = ? AND mint != ? AND first_seen_at >= ?
                ORDER BY first_seen_at DESC LIMIT 25
                """,
                (assessment.handle, mint, since),
            )
            others = [row["mint"] for row in await cursor.fetchall()]

        if assessment.tweet_id:
            cursor = await self._db.execute(
                """
                SELECT DISTINCT mint FROM traction_x_links
                WHERE tweet_id = ? AND tweet_id != '' AND mint != ?
                  AND first_seen_at >= ?
                ORDER BY first_seen_at DESC LIMIT 25
                """,
                (assessment.tweet_id, mint, since),
            )
            tweet_others = [row["mint"] for row in await cursor.fetchall()]

        return ReuseReport(
            handle=assessment.handle,
            other_mints=tuple(others),
            tweet_other_mints=tuple(tweet_others),
            window_seconds=window_seconds,
        )

    # ------------------------------------------------------------------
    # latency
    # ------------------------------------------------------------------
    async def record_detection(
        self,
        mint: str,
        *,
        chain_created_at: int | None,
        detected_at: int,
        source: str = "",
        launchpad: str = "",
    ) -> None:
        """Stamp first sighting immediately.

        Written before any enrichment, because this timestamp is the number every
        latency figure is measured against and delaying it would make the lane
        look faster than it is.
        """

        await self._db.execute(
            """
            INSERT OR IGNORE INTO traction_latency (
                mint, chain_created_at, detected_at, source, launchpad
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (mint, chain_created_at, detected_at, source, launchpad),
        )
        await self._db.commit()

    async def record_alert_sent(self, mint: str, *, at: int) -> None:
        await self._db.execute(
            """
            UPDATE traction_latency SET alert_sent_at = ?
            WHERE mint = ? AND alert_sent_at IS NULL
            """,
            (at, mint),
        )
        await self._db.commit()

    async def latency_samples(
        self, *, since: int = 0, limit: int = 5_000
    ) -> tuple[LatencySample, ...]:
        cursor = await self._db.execute(
            """
            SELECT * FROM traction_latency WHERE detected_at >= ?
            ORDER BY detected_at DESC LIMIT ?
            """,
            (since, limit),
        )
        return tuple(
            LatencySample(
                mint=row["mint"],
                chain_created_at=row["chain_created_at"],
                detected_at=int(row["detected_at"]),
                alert_sent_at=row["alert_sent_at"],
                source=row["source"] or "",
                launchpad=row["launchpad"] or "",
            )
            for row in await cursor.fetchall()
        )

    # ------------------------------------------------------------------
    # rejections, for tuning against real near-misses
    # ------------------------------------------------------------------
    async def record_rejection(
        self, verdict: ProfileVerdict, *, at: int | None = None
    ) -> None:
        moment = at if at is not None else int(time.time())
        await self._db.execute(
            """
            INSERT OR IGNORE INTO traction_rejections (
                mint, decided_at, reasons_json, measured_json, terminal
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                verdict.mint,
                moment,
                _dumps(list(verdict.reasons)),
                _dumps(verdict.measured),
                1 if verdict.terminal else 0,
            ),
        )
        await self._db.commit()

    async def rejection_summary(self, *, since: int = 0) -> dict[str, int]:
        """Which threshold rejects most, so the operator can tune from evidence."""

        cursor = await self._db.execute(
            "SELECT reasons_json FROM traction_rejections WHERE decided_at >= ?",
            (since,),
        )
        counts: dict[str, int] = {}
        for row in await cursor.fetchall():
            try:
                reasons = json.loads(row["reasons_json"])
            except (ValueError, TypeError):
                continue
            for reason in reasons if isinstance(reasons, list) else []:
                counts[str(reason)] = counts.get(str(reason), 0) + 1
        return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True))

    async def sweep_rejections(self, *, before: int) -> int:
        """Bound the rejection log.  It is diagnostics, not history."""

        cursor = await self._db.execute(
            "DELETE FROM traction_rejections WHERE decided_at < ?", (before,)
        )
        await self._db.commit()
        return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------
    # forward tracking, into the EXISTING v2.34 tables
    # ------------------------------------------------------------------
    async def mark_forward_registered(self, mint: str, *, at: int | None = None) -> bool:
        """Record that this mint reached the shared forward-observation history.

        Only the flag lives here.  The row itself is written by the runner lane's
        own ``store_runner_candidate``, from the engine, because
        ``runner_candidates.payload_json`` is that lane's typed blob and writing a
        foreign shape into it breaks its reader (see the module docstring).

        Guarded on ``IS NULL`` so it is idempotent: a restart that replays an
        alert cannot double-register, and the returned flag tells the caller
        whether this call was the one that did it.
        """

        moment = at if at is not None else int(time.time())
        cursor = await self._db.execute(
            """
            UPDATE traction_alerts SET forward_registered_at = ?
            WHERE mint = ? AND forward_registered_at IS NULL
            """,
            (moment, mint),
        )
        await self._db.commit()
        return bool(cursor.rowcount)

    async def forward_registered(self, mint: str) -> bool:
        cursor = await self._db.execute(
            "SELECT forward_registered_at FROM traction_alerts WHERE mint = ?",
            (mint,),
        )
        row = await cursor.fetchone()
        return bool(row is not None and row["forward_registered_at"] is not None)

    async def pending_forward_registration(
        self, *, limit: int = 50
    ) -> tuple[str, ...]:
        """Alerted mints that never reached the shared history, so a restart retries.

        Registration happens after the send, so a redeploy in between would
        otherwise leave an alerted token with no forward record at all -- the one
        thing this lane is supposed to accumulate.
        """

        cursor = await self._db.execute(
            """
            SELECT mint FROM traction_alerts
            WHERE forward_registered_at IS NULL
            ORDER BY alerted_at DESC LIMIT ?
            """,
            (limit,),
        )
        return tuple(row["mint"] for row in await cursor.fetchall())

    async def forward_outcomes(
        self, mint: str
    ) -> tuple[dict[str, Any], ...]:
        """This mint's forward record, read back out of the shared tables."""

        cursor = await self._db.execute(
            """
            SELECT horizon_seconds, observed_at, price_return_percent,
                   market_cap_return_percent, liquidity_return_percent,
                   liquidity_disappeared, rugged
            FROM runner_outcomes WHERE mint = ? ORDER BY horizon_seconds
            """,
            (mint,),
        )
        return tuple(dict(row) for row in await cursor.fetchall())

    async def tracked_alert_rows(
        self, *, limit: int = 25
    ) -> tuple[dict[str, Any], ...]:
        """Alerted mints joined to whatever forward outcome exists so far."""

        cursor = await self._db.execute(
            """
            SELECT a.mint, a.alerted_at, a.symbol, a.launchpad,
                   a.market_cap_at_alert_usd, a.age_at_alert_seconds,
                   a.momentum_state, a.quality_state, a.x_link_class,
                   COUNT(o.horizon_seconds) AS horizons_observed,
                   MAX(o.rugged) AS ever_rugged
            FROM traction_alerts a
            LEFT JOIN runner_outcomes o ON o.mint = a.mint
            GROUP BY a.mint
            ORDER BY a.alerted_at DESC LIMIT ?
            """,
            (limit,),
        )
        return tuple(dict(row) for row in await cursor.fetchall())

    async def stats(self, *, since: int = 0) -> dict[str, Any]:
        cursor = await self._db.execute(
            """
            SELECT COUNT(*) AS alerts,
                   SUM(CASE WHEN enriched_at IS NULL THEN 1 ELSE 0 END) AS pending,
                   MIN(alerted_at) AS first_at, MAX(alerted_at) AS last_at
            FROM traction_alerts WHERE alerted_at >= ?
            """,
            (since,),
        )
        row = await cursor.fetchone()
        alerts = int(row["alerts"] or 0)
        span = int(row["last_at"] or 0) - int(row["first_at"] or 0)
        return {
            "alerts": alerts,
            "pending_enrichment": int(row["pending"] or 0),
            "span_seconds": span,
            "alerts_per_hour": (
                None if span <= 0 else round(alerts * 3600 / span, 2)
            ),
            "rejections": await self.rejection_summary(since=since),
        }
