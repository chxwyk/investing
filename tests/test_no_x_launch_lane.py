from __future__ import annotations

import asyncio
import time
from collections import deque
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from smart_money_bot.engine import SmartMoneyEngine
from smart_money_bot.launch import NO_X_LAUNCH_VERDICT, score_launch_opportunity
from smart_money_bot.models import NarrativeCompetition, NewsAlert


def _candidate(now: int):
    alert = NewsAlert(
        source="CoinDesk",
        headline=(
            'BREAKING: Solana traders launch viral "Kitchen Moment" meme after record rally'
        ),
        summary="Crypto traders say the meme is spreading across Solana communities.",
        url="https://coindesk.com/markets/kitchen-moment",
        narrative_terms=("Kitchen Moment", "Solana"),
        created_at=now - 20,
        received_at=now,
    )
    return score_launch_opportunity(
        alert,
        competition=NarrativeCompetition(query="Kitchen Moment"),
        cross_source_count=2,
        now=now,
        no_x_candidates_enabled=True,
        no_x_launch_min_score=78,
    )


@pytest.mark.asyncio
async def test_no_x_candidate_alert_never_auto_launches() -> None:
    now = int(time.time())
    alert = _candidate(now).alert
    engine = object.__new__(SmartMoneyEngine)
    # v2.46: the presentation cache is consulted on every publish, so a
    # partial engine still needs it — empty, not mocked away.
    engine._presentations = {}
    engine._presentation_tasks = set()
    engine.presentation_edits = 0
    engine.presentation_unresolved = 0
    # v2.47: the publish path asks whether this mint is a copy of something
    # already live, so a partial engine still needs the verdict cache — empty,
    # not mocked away.
    engine._token_facts = {}
    engine._clone_verdicts = {}
    engine._quality_scores = {}
    engine.clone_suppressed = 0
    engine.collision_suppressed = 0
    engine.thin_quality_suppressed = 0
    engine.not_an_entry_suppressed = 0
    engine.refused_publications = 0
    engine.early_lane_evaluated = 0
    engine.settings = SimpleNamespace(
        news_min_score=45,
        news_launch_ready_score=72,
        no_x_launch_candidates_enabled=True,
        no_x_launch_min_score=78,
        news_x_verify_min_score=70,
        news_dex_match_enabled=True,
        news_max_alerts_per_hour=30,
        # This lane is about launch candidates, not catalysts; the v2.38
        # catalyst path is exercised by its own tests.
        fomo_catalyst_alerts_enabled=False,
    )
    engine.x_social = SimpleNamespace(search_enabled=False)
    engine.news_matcher = SimpleNamespace(
        competition=AsyncMock(
            return_value=NarrativeCompetition(query="Kitchen Moment")
        )
    )
    engine.notifier = SimpleNamespace(on_news_alert=AsyncMock())
    engine.pump_launcher = SimpleNamespace(launch=AsyncMock())
    engine._run_narrative_match = AsyncMock()
    engine._news_alert_times = deque()
    engine._news_match_tasks = set()
    terms = frozenset(item.casefold() for item in alert.narrative_terms)
    engine._recent_news_events = deque(
        [
            (now - 8, "reuters", terms),
            (now - 4, "associated press", terms),
        ]
    )

    await engine._handle_news_alert(alert)
    await asyncio.sleep(0)

    published = engine.notifier.on_news_alert.await_args.args[1]
    assert published.verdict == NO_X_LAUNCH_VERDICT
    engine.pump_launcher.launch.assert_not_awaited()


def _launch_engine(*, daily_usage: tuple[int, Decimal], reserved: bool = True):
    engine = object.__new__(SmartMoneyEngine)
    engine.settings = SimpleNamespace(
        pump_launch_timezone="UTC",
        pump_launch_initial_buy_sol=Decimal("0.01"),
        pump_launch_max_per_day=3,
        pump_launch_max_sol_per_day=Decimal("0.05"),
    )
    engine.database = SimpleNamespace(
        pump_launch_daily_usage=AsyncMock(return_value=daily_usage),
        reserve_pump_launch=AsyncMock(return_value=reserved),
        complete_pump_launch=AsyncMock(),
        fail_pump_launch=AsyncMock(),
    )
    engine.pump_launcher = SimpleNamespace(configured=True, launch=AsyncMock())
    return engine


@pytest.mark.asyncio
async def test_existing_duplicate_launch_reservation_still_blocks_candidate() -> None:
    engine = _launch_engine(daily_usage=(0, Decimal("0")), reserved=False)

    result = await engine.launch_news_opportunity(
        _candidate(int(time.time())),
        requested_by="admin-123",
    )

    assert result.status == "DUPLICATE"
    engine.pump_launcher.launch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("daily_usage", "expected_message"),
    [
        ((3, Decimal("0.03")), "launch-count"),
        ((2, Decimal("0.05")), "initial-buy SOL"),
    ],
)
async def test_existing_daily_count_and_sol_caps_still_block_candidate(
    daily_usage: tuple[int, Decimal],
    expected_message: str,
) -> None:
    engine = _launch_engine(daily_usage=daily_usage)

    result = await engine.launch_news_opportunity(
        _candidate(int(time.time())),
        requested_by="admin-123",
    )

    assert result.status == "DAILY_LIMIT"
    assert expected_message in result.message
    engine.database.reserve_pump_launch.assert_not_awaited()
    engine.pump_launcher.launch.assert_not_awaited()
