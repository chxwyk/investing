from __future__ import annotations

import pytest
from smart_money_bot.config import Settings


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Settings:
    values = {
        "DATABASE_PATH": str(tmp_path / "test.db"),
        "POLL_INTERVAL_SECONDS": "5",
        "PAPER_STARTING_USD": "1000",
        "DEFAULT_COPY_USD": "10",
        "MAX_COPY_USD": "25",
        "CONSENSUS_MIN_TRADERS": "2",
        "MAX_SIGNAL_AGE_SECONDS": "90",
        "MIN_TOKEN_LIQUIDITY_USD": "50000",
        "MIN_TOKEN_HOLDERS": "100",
        "MIN_ORGANIC_SCORE": "20",
        "MAX_TOP_HOLDERS_PERCENT": "70",
        "PAPER_USE_EXECUTABLE_QUOTES": "false",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return Settings.from_env(require_discord_token=False)
