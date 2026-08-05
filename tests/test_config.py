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


def test_discovery_requires_api_key(monkeypatch) -> None:
    monkeypatch.setenv("AUTO_DISCOVERY_ENABLED", "true")
    monkeypatch.delenv("SOLANA_TRACKER_API_KEY", raising=False)
    settings = Settings.from_env(require_discord_token=False)
    assert settings.discovery_is_configured is False

    monkeypatch.setenv("SOLANA_TRACKER_API_KEY", "st_test")
    configured = Settings.from_env(require_discord_token=False)
    assert configured.discovery_is_configured is True


def test_discovery_default_refresh_fits_free_monthly_quota(monkeypatch) -> None:
    monkeypatch.delenv("DISCOVERY_REFRESH_SECONDS", raising=False)
    settings = Settings.from_env(require_discord_token=False)
    assert settings.discovery_refresh_seconds == 1200


def test_rpc_defaults_are_free_tier_friendly(monkeypatch) -> None:
    monkeypatch.delenv("RPC_REQUESTS_PER_SECOND", raising=False)
    monkeypatch.delenv("RPC_MAX_RETRIES", raising=False)
    monkeypatch.delenv("POLL_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("MAX_BACKFILL_TRANSACTIONS", raising=False)
    settings = Settings.from_env(require_discord_token=False)
    assert settings.rpc_requests_per_second == 8
    assert settings.rpc_max_retries == 4
    assert settings.poll_interval_seconds == 60
    assert settings.max_backfill_transactions == 100


def test_fomo_referral_defaults_to_shared_code(monkeypatch) -> None:
    monkeypatch.delenv("FOMO_REFERRAL_CODE", raising=False)
    settings = Settings.from_env(require_discord_token=False)
    assert settings.fomo_referral_code == "WetOuterLemur"

    monkeypatch.setenv("FOMO_REFERRAL_CODE", "")
    without_referral = Settings.from_env(require_discord_token=False)
    assert without_referral.fomo_referral_code is None
