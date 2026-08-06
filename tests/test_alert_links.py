from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from smart_money_bot.bot import _token_view


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
    assert buttons["Chart"].url == f"https://dexscreener.com/solana/{mint}"
    assert buttons["Solscan"].url == f"https://solscan.io/token/{mint}"
    assert buttons["Open in Fomo"].row == 0
    assert buttons["Open in Pump.fun"].row == 0
    assert buttons["Buy on Jupiter"].row == 0
    assert buttons["Chart"].row == 1
    assert buttons["Solscan"].row == 1


@pytest.mark.asyncio
async def test_fomo_link_can_omit_referral() -> None:
    mint = "So11111111111111111111111111111111111111112"
    view = _token_view(mint)
    fomo_button = next(item for item in view.children if item.label == "Open in Fomo")

    assert "r=" not in fomo_button.url
