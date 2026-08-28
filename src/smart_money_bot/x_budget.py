from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from .config import Settings
from .database import Database
from .models import XSocialSnapshot


@dataclass(frozen=True, slots=True)
class XBudgetReservation:
    id: int
    fingerprint: str
    context: str
    query: str
    max_posts: int
    free_score: int | None = None


@dataclass(frozen=True, slots=True)
class XBudgetDecision:
    allowed: bool
    reservation: XBudgetReservation | None = None
    reason: str | None = None


def x_query_fingerprint(query: str) -> str:
    normalized = " ".join(query.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class XBudgetManager:
    """One persistent spend guard shared by every official paid-X caller."""

    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    def usage_day(self) -> str:
        return datetime.now(ZoneInfo(self.settings.x_daily_search_timezone)).date().isoformat()

    async def reserve(
        self,
        *,
        query: str,
        context: str,
        free_score: int | None = None,
    ) -> XBudgetDecision:
        fingerprint = x_query_fingerprint(query)
        reservation_id, reason = await self.database.reserve_x_verification(
            usage_day=self.usage_day(),
            period_id=self.settings.x_budget_period_id,
            fingerprint=fingerprint,
            context=context,
            query=query,
            max_posts=self.settings.x_verify_max_posts,
            request_limit=self.settings.x_daily_search_limit,
            verification_limit=self.settings.x_max_targeted_verifications_per_day,
            daily_budget_usd=self.settings.x_estimated_daily_budget_usd,
            total_budget_usd=self.settings.x_estimated_total_budget_usd,
            post_unit_cost_usd=self.settings.x_estimated_post_read_usd,
            guard_enabled=self.settings.x_budget_guard_enabled,
        )
        if reservation_id is None:
            return XBudgetDecision(allowed=False, reason=reason)
        return XBudgetDecision(
            allowed=True,
            reservation=XBudgetReservation(
                id=reservation_id,
                fingerprint=fingerprint,
                context=context,
                query=query,
                max_posts=self.settings.x_verify_max_posts,
                free_score=free_score,
            ),
        )

    async def record_posts(
        self, reservation: XBudgetReservation, post_ids: tuple[str, ...]
    ) -> int:
        return await self.database.record_x_resources(
            verification_id=reservation.id,
            resource_type="post",
            resource_ids=post_ids,
            unit_cost_usd=self.settings.x_estimated_post_read_usd,
        )

    async def cached_users(self, user_ids: tuple[str, ...]) -> dict[str, dict[str, object]]:
        return await self.database.cached_x_users(
            user_ids=user_ids,
            minimum_fetched_at=int(time.time()) - self.settings.x_user_cache_seconds,
        )

    async def reserve_users(
        self,
        reservation: XBudgetReservation,
        user_ids: tuple[str, ...],
    ) -> tuple[tuple[str, ...], str | None]:
        return await self.database.reserve_x_user_resources(
            verification_id=reservation.id,
            user_ids=user_ids,
            daily_budget_usd=self.settings.x_estimated_daily_budget_usd,
            total_budget_usd=self.settings.x_estimated_total_budget_usd,
            user_unit_cost_usd=self.settings.x_estimated_user_read_usd,
            guard_enabled=self.settings.x_budget_guard_enabled,
        )

    async def record_users(
        self,
        reservation: XBudgetReservation,
        users: tuple[dict[str, object], ...],
    ) -> int:
        now = int(time.time())
        await self.database.cache_x_users(users, fetched_at=now)
        return await self.database.record_x_resources(
            verification_id=reservation.id,
            resource_type="user",
            resource_ids=tuple(str(user.get("id") or "") for user in users),
            unit_cost_usd=self.settings.x_estimated_user_read_usd,
        )

    async def finish(
        self,
        reservation: XBudgetReservation,
        *,
        status_code: int = 200,
        http_requests: int = 1,
    ) -> None:
        await self.database.finish_x_verification(
            verification_id=reservation.id,
            status_code=status_code,
            free_score=reservation.free_score,
            http_requests=http_requests,
        )

    async def fail(
        self,
        reservation: XBudgetReservation,
        *,
        status_code: int | None,
        error_category: str,
        http_requests: int = 1,
    ) -> None:
        await self.database.fail_x_verification(
            verification_id=reservation.id,
            status_code=status_code,
            error_category=error_category,
            http_requests=http_requests,
        )

    async def record_outcome(
        self,
        verification_id: int | None,
        *,
        free_score: int,
        final_score: int,
        outcome: str,
    ) -> None:
        if verification_id is None:
            return
        await self.database.update_x_verification_outcome(
            verification_id=verification_id,
            free_score=free_score,
            final_score=final_score,
            outcome=outcome,
        )

    async def cached_snapshot(self, query: str) -> XSocialSnapshot | None:
        raw = await self.database.cached_x_snapshot(
            fingerprint=x_query_fingerprint(query),
            now=int(time.time()),
        )
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            for key in ("duplicate_percent", "posts_per_minute"):
                payload[key] = Decimal(str(payload.get(key) or 0))
            for key in ("notable_accounts", "notable_posts"):
                payload[key] = tuple(payload.get(key) or ())
            return XSocialSnapshot(**payload)
        except (TypeError, ValueError, KeyError):
            return None

    async def cache_snapshot(self, query: str, snapshot: XSocialSnapshot) -> None:
        now = int(time.time())
        await self.database.cache_x_snapshot(
            fingerprint=x_query_fingerprint(query),
            query=query,
            snapshot_json=json.dumps(asdict(snapshot), default=str, separators=(",", ":")),
            fetched_at=now,
            expires_at=now + self.settings.news_x_trend_cache_seconds,
        )

    async def status(self) -> dict[str, object]:
        status = await self.database.x_budget_status(
            usage_day=self.usage_day(),
            period_id=self.settings.x_budget_period_id,
        )
        status.update(
            {
                "guard_enabled": self.settings.x_budget_guard_enabled,
                "daily_budget": self.settings.x_estimated_daily_budget_usd,
                "total_budget": self.settings.x_estimated_total_budget_usd,
                "verification_limit": self.settings.x_max_targeted_verifications_per_day,
                "request_limit": self.settings.x_daily_search_limit,
                "max_posts": self.settings.x_verify_max_posts,
                "post_unit_cost": self.settings.x_estimated_post_read_usd,
                "user_unit_cost": self.settings.x_estimated_user_read_usd,
                "period_id": self.settings.x_budget_period_id,
                "actual_usage_available": False,
            }
        )
        return status
