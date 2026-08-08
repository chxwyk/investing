from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from smart_money_bot.discovery import (
    DiscoveryPolicy,
    SolanaTrackerClient,
    merge_verified_windows,
    parse_candidates,
    parse_window_candidates,
)

WALLET_ONE = "HkFGQsW8mr8DTC2AE2WcC7MzwSnynfEryGMQSht271nf"
WALLET_TWO = "ApAKzJEqfnP7F74Za5xdTQxZMK4nD8dFTVBQ9bksTtGM"


def policy() -> DiscoveryPolicy:
    return DiscoveryPolicy(
        fetch_limit=100,
        max_wallets=12,
        minimum_pnl_usd=Decimal("100"),
        minimum_win_rate_percent=Decimal("55"),
        minimum_roi_percent=Decimal("3"),
        minimum_trades=5,
        maximum_trades=250,
        minimum_closed_tokens=2,
        maximum_single_token_percent=Decimal("70"),
    )


def row(
    wallet: str,
    *,
    pnl: int = 500,
    roi: int = 20,
    win_rate: int = 70,
    trades: int = 20,
    closed: int = 10,
    tags: list[str] | None = None,
) -> dict:
    return {
        "wallet": wallet,
        "period": {"realized": pnl, "roi": roi, "volume": 5000},
        "invested": 2000,
        "counts": {"trades": trades, "buys": 10, "sells": 10},
        "tokens": {"closed": closed},
        "winRate": win_rate,
        "timing": {"lastTrade": 1_700_000_000_000},
        "identity": {"name": "Disciplined Trader", "tags": tags or []},
    }


def test_parse_candidates_filters_low_profit_and_non_human_wallets() -> None:
    payload = {
        "traders": [
            row(WALLET_ONE),
            row(WALLET_TWO, pnl=50),
            row(WALLET_TWO, pnl=1000, tags=["arbitrage"]),
        ]
    }

    candidates = parse_candidates(payload, policy())

    assert len(candidates) == 1
    assert candidates[0].address == WALLET_ONE
    assert candidates[0].rank == 1
    assert candidates[0].score >= Decimal("25")


def test_parse_candidates_rejects_hyperactive_bot_like_wallet() -> None:
    payload = {"traders": [row(WALLET_ONE, trades=5000)]}

    assert parse_candidates(payload, policy()) == []


def test_merge_verified_windows_requires_profit_in_both_periods() -> None:
    daily = parse_window_candidates(
        {"traders": [row(WALLET_ONE), row(WALLET_TWO)]}, policy(), days=1
    )
    weekly = parse_window_candidates(
        {
            "traders": [
                row(WALLET_ONE, pnl=2000, roi=35, win_rate=72, trades=80),
            ]
        },
        policy(),
        days=7,
    )

    merged = merge_verified_windows(daily, weekly, policy())

    assert [candidate.address for candidate in merged] == [WALLET_ONE]
    assert merged[0].realized_pnl_7d == Decimal("2000")
    assert merged[0].roi_7d_percent == Decimal("35")
    assert "24H + 7D" in merged[0].selection_reason


@pytest.mark.asyncio
async def test_leaderboard_paginates_and_passes_cursor() -> None:
    client = SolanaTrackerClient("test-key")
    client._request = AsyncMock(
        side_effect=(
            {
                "traders": [row(WALLET_ONE)],
                "pagination": {"hasMore": True, "nextCursor": "page-2"},
            },
            {
                "traders": [row(WALLET_TWO)],
                "pagination": {"hasMore": False, "nextCursor": None},
            },
        )
    )

    result = await client.daily_pool(
        replace(policy(), fetch_limit=1, candidate_pages=5)
    )

    assert [candidate.address for candidate in result] == [WALLET_ONE, WALLET_TWO]
    first_params = client._request.await_args_list[0].kwargs["params"]
    second_params = client._request.await_args_list[1].kwargs["params"]
    assert "cursor" not in first_params
    assert second_params["cursor"] == "page-2"
    assert first_params["limit"] == "1"
    assert client._request.await_count == 2
