from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from smart_money_bot.bot import _news_lead_view, _split_discord_text, _token_view
from smart_money_bot.models import NewsAlert


@pytest.mark.asyncio
async def test_token_view_builds_exact_solana_coin_links() -> None:
    mint = "CBRRJc94xJpVnNWiMX2JemVQh7CxY2E27UnYL5tqpump"
    view = _token_view(mint, "WetOuterLemur")
    buttons = {item.label: item for item in view.children}

    fomo_url = urlparse(buttons["Open in Fomo"].url)
    assert fomo_url.scheme == "https"
    assert fomo_url.netloc == "fomo.family"
    assert fomo_url.path == "/coin"
    assert parse_qs(fomo_url.query) == {
        "address": [mint],
        "chainId": ["1399811149"],
        "r": ["WetOuterLemur"],
        "source": ["share_link"],
    }

    assert buttons["Open in Pump.fun"].url == f"https://pump.fun/coin/{mint}"
    assert buttons["Buy on Jupiter"].url == f"https://jup.ag/swap/SOL-{mint}"
    assert buttons["Sell on Jupiter"].url == f"https://jup.ag/swap/{mint}-SOL"
    assert buttons["Chart"].url == f"https://dexscreener.com/solana/{mint}"
    assert buttons["Solscan"].url == f"https://solscan.io/token/{mint}"
    assert buttons["Open in Fomo"].row == 0
    assert buttons["Open in Pump.fun"].row == 0
    assert buttons["Buy on Jupiter"].row == 0
    assert buttons["Sell on Jupiter"].row == 0
    assert buttons["Chart"].row == 1
    assert buttons["Solscan"].row == 1


def test_news_without_contract_has_search_and_creation_links() -> None:
    alert = NewsAlert(
        source="X filtered stream",
        headline="BREAKING: Trump announces Project Kitchen",
        summary="",
        score=60,
        urgency="HIGH",
        narrative_terms=("Trump", "Kitchen"),
        url="https://x.com/example/status/1",
    )
    buttons = {item.label: item for item in _news_lead_view(alert).children}

    assert buttons["Create on Pump.fun"].url == "https://pump.fun/create"
    assert buttons["Explore Pump.fun"].url == "https://pump.fun/coins"
    assert parse_qs(urlparse(buttons["Search Matching Coins"].url).query) == {
        "q": ["Trump Kitchen"]
    }
    assert buttons["Original News"].url == alert.url


@pytest.mark.asyncio
async def test_fomo_link_can_omit_referral() -> None:
    mint = "So11111111111111111111111111111111111111112"
    view = _token_view(mint)
    fomo_button = next(item for item in view.children if item.label == "Open in Fomo")

    assert "r=" not in fomo_button.url


def test_long_status_is_split_below_discord_content_limit() -> None:
    text = "\n".join(f"status line {index}: " + "x" * 90 for index in range(80))

    chunks = _split_discord_text(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= 1900 for chunk in chunks)
    assert "status line 0" in chunks[0]
    assert "status line 79" in chunks[-1]
