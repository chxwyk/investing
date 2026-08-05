from __future__ import annotations

from smart_money_bot.config import Settings
from smart_money_bot.constants import LIVE_ACK_TEXT


def test_live_mode_requires_every_lock(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    monkeypatch.setenv("LIVE_TRADING_ACK", LIVE_ACK_TEXT)
    monkeypatch.setenv("TRADING_PRIVATE_KEY", "present-but-not-loaded-by-settings")
    monkeypatch.setenv("JUPITER_API_KEY", "jup_test")
    settings = Settings.from_env(require_discord_token=False)
    assert settings.live_is_unlocked is True


def test_live_mode_stays_locked_without_ack(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    monkeypatch.setenv("TRADING_PRIVATE_KEY", "present")
    monkeypatch.setenv("JUPITER_API_KEY", "jup_test")
    settings = Settings.from_env(require_discord_token=False)
    assert settings.live_is_unlocked is False

