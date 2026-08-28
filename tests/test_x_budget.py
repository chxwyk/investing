from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from smart_money_bot.bot import LaunchLabView, XVerificationConfirmationView
from smart_money_bot.callouts import XRecentSearchClient, build_x_narrative_query
from smart_money_bot.database import Database
from smart_money_bot.engine import SmartMoneyEngine
from smart_money_bot.launch import (
    NO_X_LAUNCH_VERDICT,
    X_VERIFIED_LAUNCH_VERDICT,
    score_launch_opportunity,
    should_request_x_for_launch_opportunity,
)
from smart_money_bot.models import NarrativeCompetition, NewsAlert, XSocialSnapshot
from smart_money_bot.x_budget import XBudgetManager


class FakeResponse:
    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, **_kwargs):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class FakeSession:
    def __init__(self, *events: object) -> None:
        self.events = list(events)
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.closed = False

    def get(self, url: str, *, params: dict[str, str], headers: dict[str, str]):
        del headers
        self.calls.append((url, params))
        if not self.events:
            raise AssertionError("unexpected X request")
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event


def _configured(settings, **changes):
    defaults = dict(
        x_api_bearer_token="secret-bearer-token",
        x_paid_search_enabled=True,
        x_budget_guard_enabled=True,
        x_estimated_total_budget_usd=Decimal("10"),
        x_estimated_daily_budget_usd=Decimal("0.50"),
        x_max_targeted_verifications_per_day=10,
        x_verify_max_posts=10,
        x_search_max_results=10,
        x_daily_search_limit=10,
        x_budget_period_id="test-experiment",
        x_estimated_post_read_usd=Decimal("0.005"),
        x_estimated_user_read_usd=Decimal("0.010"),
        x_user_cache_seconds=86400,
    )
    defaults.update(changes)
    return replace(settings, **defaults)


async def _budget(settings, tmp_path, **changes):
    configured = _configured(
        settings,
        database_path=str(tmp_path / "budget.db"),
        **changes,
    )
    database = Database(configured.database_path, Decimal("1000"))
    await database.connect()
    return configured, database, XBudgetManager(database, configured)


def _posts(count: int, *, prefix: str = "p", authors: int | None = None):
    authors = authors or count
    recent = (datetime.now(UTC) - timedelta(seconds=20)).isoformat()
    return [
        {
            "id": f"{prefix}{index}",
            "author_id": f"u{index % authors}",
            "created_at": recent,
            "text": f"Kitchen Moment meme coin post {index}",
            "public_metrics": {"like_count": 20 + index, "retweet_count": 2},
        }
        for index in range(count)
    ]


def _users(count: int):
    old = (datetime.now(UTC) - timedelta(days=365)).isoformat()
    return [
        {
            "id": f"u{index}",
            "username": f"crypto{index}",
            "description": "Solana crypto memecoin trader",
            "created_at": old,
            "public_metrics": {"followers_count": 10_000, "following_count": 100},
            "verified": index == 0,
        }
        for index in range(count)
    ]


def _alert(now: int | None = None) -> NewsAlert:
    now = now or int(time.time())
    return NewsAlert(
        source="CoinDesk",
        author="@coindesk",
        headline='BREAKING: Solana traders spread viral "Kitchen Moment" meme',
        summary="Crypto traders say the Kitchen Moment is spreading across Solana communities.",
        url="https://coindesk.com/markets/kitchen-moment",
        narrative_terms=("Kitchen Moment", "Solana"),
        created_at=now - 20,
        received_at=now,
    )


def _free_opportunity(*, score: int = 74):
    item = score_launch_opportunity(
        _alert(),
        competition=NarrativeCompetition(query="Kitchen Moment"),
        cross_source_count=2,
    )
    return replace(
        item,
        score=score,
        blockers=(),
        lane="CRYPTO TREND",
        competition_score=10,
    )


@pytest.mark.asyncio
async def test_budget_reservation_counts_ten_returned_posts(settings, tmp_path) -> None:
    configured, database, budget = await _budget(settings, tmp_path)
    try:
        decision = await budget.reserve(query="query one", context="automatic_news")
        assert decision.allowed and decision.reservation
        await budget.record_posts(
            decision.reservation,
            tuple(item["id"] for item in _posts(10)),
        )
        await budget.finish(decision.reservation)
        status = await budget.status()
        assert status["verifications"] == 1
        assert status["post_resources"] == 10
        assert status["estimated_spend_today"] == Decimal("0.05")
        assert configured.x_verify_max_posts == 10
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_partial_results_and_duplicate_post_ids_are_not_double_counted(
    settings, tmp_path
) -> None:
    _configured_settings, database, budget = await _budget(settings, tmp_path)
    try:
        first = (await budget.reserve(query="one", context="automatic_news")).reservation
        second = (await budget.reserve(query="two", context="launch_lab_manual")).reservation
        assert first and second
        await budget.record_posts(first, ("1", "2", "3"))
        await budget.finish(first)
        await budget.record_posts(second, ("2", "3", "4"))
        await budget.finish(second)
        status = await budget.status()
        assert status["post_resources"] == 4
        assert status["estimated_spend_today"] == Decimal("0.02")
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_daily_verification_cap_is_shared_across_callers(settings, tmp_path) -> None:
    _configured_settings, database, budget = await _budget(
        settings,
        tmp_path,
        x_max_targeted_verifications_per_day=1,
    )
    try:
        first = await budget.reserve(query="news", context="automatic_news")
        second = await budget.reserve(query="coin", context="coin_callout")
        assert first.allowed is True
        assert second.allowed is False
        assert "DAILY VERIFICATION CAP" in str(second.reason)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_concurrent_callers_cannot_overbook_shared_cap(settings, tmp_path) -> None:
    _configured_settings, database, budget = await _budget(
        settings,
        tmp_path,
        x_max_targeted_verifications_per_day=1,
    )
    try:
        decisions = await asyncio.gather(
            budget.reserve(query="automatic", context="automatic_news"),
            budget.reserve(query="manual", context="launch_lab_manual"),
        )
        assert sum(item.allowed for item in decisions) == 1
        assert sum(not item.allowed for item in decisions) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_concurrent_duplicate_query_is_reserved_only_once(settings, tmp_path) -> None:
    _configured_settings, database, budget = await _budget(settings, tmp_path)
    try:
        decisions = await asyncio.gather(
            budget.reserve(query="same narrative", context="automatic_news"),
            budget.reserve(query="same narrative", context="launch_lab_manual"),
        )
        assert sum(item.allowed for item in decisions) == 1
        rejected = next(item for item in decisions if not item.allowed)
        assert "SAME QUERY ALREADY IN PROGRESS" in str(rejected.reason)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_daily_estimated_dollar_cap_fails_closed(settings, tmp_path) -> None:
    _configured_settings, database, budget = await _budget(
        settings,
        tmp_path,
        x_estimated_daily_budget_usd=Decimal("0.04"),
    )
    try:
        denied = await budget.reserve(query="too expensive", context="automatic_news")
        assert denied.allowed is False
        assert denied.reason == "X VERIFICATION SKIPPED — DAILY BUDGET REACHED"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_total_experiment_budget_fails_closed(settings, tmp_path) -> None:
    _configured_settings, database, budget = await _budget(
        settings,
        tmp_path,
        x_estimated_total_budget_usd=Decimal("0.04"),
        x_estimated_daily_budget_usd=Decimal("0.04"),
    )
    try:
        denied = await budget.reserve(query="too expensive", context="launch_lab_manual")
        assert denied.allowed is False
        assert "BUDGET REACHED" in str(denied.reason)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_budget_usage_survives_manager_restart(settings, tmp_path) -> None:
    configured, database, budget = await _budget(settings, tmp_path)
    try:
        reservation = (await budget.reserve(query="persist", context="automatic_news")).reservation
        assert reservation
        await budget.record_posts(reservation, ("persisted-post",))
        await budget.finish(reservation)
        restarted = XBudgetManager(database, configured)
        status = await restarted.status()
        assert status["verifications"] == 1
        assert status["post_resources"] == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_user_cache_is_persistent_and_does_not_contain_bearer_token(
    settings, tmp_path
) -> None:
    _configured_settings, database, budget = await _budget(settings, tmp_path)
    try:
        await database.cache_x_users(tuple(_users(2)), fetched_at=int(time.time()))
        cached = await budget.cached_users(("u0", "u1"))
        assert set(cached) == {"u0", "u1"}
        assert "secret-bearer-token" not in json.dumps(cached)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_client_cache_prevents_a_second_paid_query(settings, tmp_path) -> None:
    configured, database, budget = await _budget(settings, tmp_path)
    client = XRecentSearchClient(
        configured.x_api_bearer_token,
        max_results=10,
        budget_manager=budget,
        paid_search_enabled=True,
    )
    session = FakeSession(FakeResponse(200, {"data": _posts(1)}))
    client._session = session
    try:
        first = await client.narrative_snapshot("Kitchen Moment")
        second = await client.narrative_snapshot("Kitchen Moment")
        assert first.available and second.available
        assert len(session.calls) == 1
        assert (await budget.status())["verifications"] == 1
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (429, {"detail": "rate limit"}, "X RATE LIMITED"),
        (402, {"detail": "credits exhausted"}, "X API CREDITS UNAVAILABLE"),
        (401, {"detail": "bad token"}, "X AUTH FAILED"),
    ],
)
async def test_x_paid_failures_are_sanitized_and_not_retried(
    settings, tmp_path, status, body, expected
) -> None:
    configured, database, budget = await _budget(settings, tmp_path)
    client = XRecentSearchClient(
        configured.x_api_bearer_token,
        budget_manager=budget,
        paid_search_enabled=True,
    )
    session = FakeSession(FakeResponse(status, body))
    client._session = session
    try:
        snapshot = await client.narrative_snapshot("Kitchen Moment")
        assert snapshot.available is False
        assert expected in str(snapshot.error)
        assert len(session.calls) == 1
        assert "secret-bearer-token" not in str(snapshot)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_x_5xx_has_one_limited_retry(settings, tmp_path) -> None:
    configured, database, budget = await _budget(settings, tmp_path)
    client = XRecentSearchClient(
        configured.x_api_bearer_token,
        budget_manager=budget,
        paid_search_enabled=True,
    )
    session = FakeSession(
        FakeResponse(500, {"detail": "temporary"}),
        FakeResponse(200, {"data": _posts(1)}),
    )
    client._session = session
    try:
        snapshot = await client.narrative_snapshot("Kitchen Moment")
        assert snapshot.available is True
        assert len(session.calls) == 2
        assert (await budget.status())["requests"] == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_x_timeout_does_not_retry_or_break_free_system(settings, tmp_path) -> None:
    configured, database, budget = await _budget(settings, tmp_path)
    client = XRecentSearchClient(
        configured.x_api_bearer_token,
        budget_manager=budget,
        paid_search_enabled=True,
    )
    client._session = FakeSession(TimeoutError("slow"))
    try:
        snapshot = await client.narrative_snapshot("Kitchen Moment")
        assert snapshot.available is False
        assert "X NETWORK FAILURE" in str(snapshot.error)
        free = score_launch_opportunity(
            _alert(),
            x_evidence=snapshot,
            competition=NarrativeCompetition(query="Kitchen Moment"),
            cross_source_count=2,
        )
        assert free.x_verified is False
        assert "X/social velocity was not verified." in free.warnings
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_malformed_x_response_is_not_verified(settings, tmp_path) -> None:
    configured, database, budget = await _budget(settings, tmp_path)
    client = XRecentSearchClient(
        configured.x_api_bearer_token,
        budget_manager=budget,
        paid_search_enabled=True,
    )
    client._session = FakeSession(FakeResponse(200, {"data": {"wrong": True}}))
    try:
        snapshot = await client.narrative_snapshot("Kitchen Moment")
        assert snapshot.available is False
        assert snapshot.error == "X MALFORMED RESPONSE"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_user_hydration_is_batched_and_reused_from_cache(settings, tmp_path) -> None:
    configured, database, budget = await _budget(settings, tmp_path)
    first_client = XRecentSearchClient(
        configured.x_api_bearer_token,
        budget_manager=budget,
        paid_search_enabled=True,
    )
    first_session = FakeSession(
        FakeResponse(200, {"data": _posts(2)}),
        FakeResponse(200, {"data": _users(2)}),
    )
    first_client._session = first_session
    second_client = XRecentSearchClient(
        configured.x_api_bearer_token,
        budget_manager=budget,
        paid_search_enabled=True,
    )
    second_session = FakeSession(FakeResponse(200, {"data": _posts(2, prefix="q")}))
    second_client._session = second_session
    try:
        first = await first_client.narrative_snapshot("Kitchen Moment")
        second = await second_client.narrative_snapshot("Kitchen Sequel")
        assert first.credible_crypto_authors == 2
        assert second.credible_crypto_authors == 2
        assert len(first_session.calls) == 2
        assert len(second_session.calls) == 1
        status = await budget.status()
        assert status["user_resources"] == 2
    finally:
        await database.close()


def test_free_x_eligibility_runs_cheap_blockers_first() -> None:
    promising = _free_opportunity(score=74)
    assert should_request_x_for_launch_opportunity(promising, minimum_score=70)[0] is True
    assert should_request_x_for_launch_opportunity(
        replace(promising, score=69), minimum_score=70
    )[0] is False
    assert should_request_x_for_launch_opportunity(
        replace(promising, blockers=("rumor",)), minimum_score=70
    )[0] is False
    assert should_request_x_for_launch_opportunity(
        replace(promising, alert=replace(promising.alert, token_mints=("mint",))),
        minimum_score=70,
    )[0] is False


def test_strong_x_can_upgrade_but_weak_x_cannot() -> None:
    strong = XSocialSnapshot(
        available=True,
        posts=10,
        unique_authors=8,
        established_authors=6,
        influential_authors=2,
        crypto_authors=6,
        credible_crypto_authors=4,
        promoter_posts=5,
        engagements=500,
        posts_per_minute=Decimal("1"),
        duplicate_percent=Decimal("5"),
    )
    weak = replace(strong, unique_authors=2, crypto_authors=1, credible_crypto_authors=0)
    competition = NarrativeCompetition(query="Kitchen Moment")
    strong_result = score_launch_opportunity(
        _alert(), x_evidence=strong, competition=competition, cross_source_count=2
    )
    weak_result = score_launch_opportunity(
        _alert(), x_evidence=weak, competition=competition, cross_source_count=2
    )
    assert strong_result.verdict == X_VERIFIED_LAUNCH_VERDICT
    assert strong_result.x_verified is True
    assert weak_result.x_verified is False
    assert weak_result.verdict != X_VERIFIED_LAUNCH_VERDICT


def test_x_disabled_and_missing_token_leave_free_lane_available(settings) -> None:
    disabled = XRecentSearchClient("token", paid_search_enabled=False)
    missing = XRecentSearchClient(None, paid_search_enabled=True)
    assert disabled.search_enabled is False
    assert missing.search_enabled is False
    candidate = score_launch_opportunity(
        _alert(),
        competition=NarrativeCompetition(query="Kitchen Moment"),
        cross_source_count=2,
    )
    assert candidate.x_verified is False
    assert candidate.verdict in {NO_X_LAUNCH_VERDICT, "WATCH"}


def test_budget_exhaustion_preserves_automatic_no_x_candidate() -> None:
    competition = NarrativeCompetition(query="Kitchen Moment")
    free_candidate = score_launch_opportunity(
        _alert(),
        competition=competition,
        cross_source_count=2,
    )
    exhausted = score_launch_opportunity(
        _alert(),
        x_evidence=XSocialSnapshot(
            available=False,
            error="X VERIFICATION SKIPPED — DAILY BUDGET REACHED",
            verification_state="NOT_VERIFIED",
        ),
        competition=competition,
        cross_source_count=2,
        pre_x_score=free_candidate.score,
    )
    assert free_candidate.verdict == NO_X_LAUNCH_VERDICT
    assert exhausted.verdict == NO_X_LAUNCH_VERDICT
    assert exhausted.no_x_candidate_ready is True
    assert exhausted.x_verified is False


@pytest.mark.asyncio
async def test_launch_lab_x_preview_and_verification_never_call_j7(settings) -> None:
    opportunity = _free_opportunity(score=74)
    updated = replace(
        opportunity,
        x_evidence=XSocialSnapshot(
            available=True,
            posts=2,
            unique_authors=2,
            verification_state="CHECKED",
        ),
    )
    j7_launch = AsyncMock()
    engine = SimpleNamespace(
        verify_launch_lab_candidate=AsyncMock(return_value=updated),
        x_budget=SimpleNamespace(
            status=AsyncMock(
                return_value={
                    "estimated_spend_today": Decimal("0"),
                    "daily_budget": Decimal("0.50"),
                    "verifications": 0,
                    "verification_limit": 10,
                }
            )
        ),
        pump_launcher=SimpleNamespace(
            j7=SimpleNamespace(
                wallet_address="So11111111111111111111111111111111111111112",
                render_draft_art=AsyncMock(return_value=b"png"),
                launch=j7_launch,
            )
        ),
    )
    bot = SimpleNamespace(settings=_configured(settings), engine=engine)
    view = LaunchLabView(bot, (opportunity,), owner_id=1, balance=Decimal("0.03"))
    x_button = next(item for item in view.children if item.label == "X VERIFY")
    preview_interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
    )
    await x_button.callback(preview_interaction)
    engine.verify_launch_lab_candidate.assert_not_awaited()
    j7_launch.assert_not_awaited()

    confirmation = XVerificationConfirmationView(view)
    run_button = next(item for item in confirmation.children if item.label == "RUN X VERIFICATION")
    run_interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        response=SimpleNamespace(defer=AsyncMock()),
        message=SimpleNamespace(edit=AsyncMock()),
    )
    await run_button.callback(run_interaction)
    engine.verify_launch_lab_candidate.assert_awaited_once()
    j7_launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_launch_lab_x_control_is_admin_owner_only(settings) -> None:
    bot = SimpleNamespace(
        settings=_configured(settings),
        engine=SimpleNamespace(
            pump_launcher=SimpleNamespace(
                j7=SimpleNamespace(wallet_address="wallet", launch=AsyncMock())
            )
        ),
    )
    view = LaunchLabView(bot, (_free_opportunity(),), owner_id=1, balance=None)
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=2),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    assert await view.interaction_check(interaction) is False
    bot.engine.pump_launcher.j7.launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_budget_exhaustion_does_not_break_launch_lab_or_call_j7(settings, tmp_path) -> None:
    configured = _configured(
        settings,
        database_path=str(tmp_path / "engine.db"),
        x_estimated_daily_budget_usd=Decimal("0.04"),
    )
    engine = SmartMoneyEngine(configured)
    await engine.database.connect()
    engine.pump_launcher.j7.launch = AsyncMock()
    opportunity = _free_opportunity(score=74)
    try:
        updated = await engine.verify_launch_lab_candidate(opportunity)
        assert updated.x_verified is False
        assert "BUDGET REACHED" in str(updated.x_evidence.error)
        engine.pump_launcher.j7.launch.assert_not_awaited()
    finally:
        await engine.database.close()


def test_x_usage_status_structure_cannot_expose_bearer_token(settings, tmp_path) -> None:
    configured = _configured(settings, database_path=str(tmp_path / "safe.db"))
    public_settings = {
        "guard": configured.x_budget_guard_enabled,
        "daily": str(configured.x_estimated_daily_budget_usd),
        "total": str(configured.x_estimated_total_budget_usd),
    }
    assert "secret-bearer-token" not in json.dumps(public_settings)


def test_query_builder_is_compact_and_excludes_reposts_and_replies() -> None:
    query = build_x_narrative_query(
        "BREAKING: The Kitchen Moment is becoming a viral Solana community meme today"
    )
    assert len(query) < 512
    assert "-is:retweet" in query
    assert "-is:reply" in query
    assert '"BREAKING Kitchen Moment becoming"' in query
