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


def test_raw_paper_mirroring_defaults_on_and_can_be_disabled(monkeypatch) -> None:
    monkeypatch.delenv("PAPER_MIRROR_RAW_SWAPS", raising=False)
    settings = Settings.from_env(require_discord_token=False)
    assert settings.paper_mirror_raw_swaps is True

    monkeypatch.setenv("PAPER_MIRROR_RAW_SWAPS", "false")
    disabled = Settings.from_env(require_discord_token=False)
    assert disabled.paper_mirror_raw_swaps is False


def test_v27_paper_risk_guards_default_on(monkeypatch) -> None:
    names = (
        "PAPER_REQUIRE_CURRENT_PRICE",
        "PAPER_RAW_ENTRY_FILTER_ENABLED",
        "RAW_MIRROR_STOP_LOSS_PERCENT",
        "RAW_MIRROR_TAKE_PROFIT_PERCENT",
        "RAW_MIRROR_TRAILING_ACTIVATION_PERCENT",
        "RAW_MIRROR_TRAILING_STOP_PERCENT",
        "RAW_MIRROR_MAX_HOLD_SECONDS",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env(require_discord_token=False)
    assert settings.paper_require_current_price is True
    assert settings.paper_raw_entry_filter_enabled is True
    assert settings.raw_mirror_stop_loss_percent == 8
    assert settings.raw_mirror_take_profit_percent == 20
    assert settings.raw_mirror_trailing_activation_percent == 8
    assert settings.raw_mirror_trailing_stop_percent == 4
    assert settings.raw_mirror_max_hold_seconds == 7200


def test_v28_quote_shadow_and_readiness_defaults(monkeypatch) -> None:
    names = (
        "PAPER_USE_EXECUTABLE_QUOTES",
        "PAPER_QUOTE_OUTPUT_BUFFER_BPS",
        "MAX_ADVERSE_ENTRY_DRIFT_PERCENT",
        "MAX_QUOTE_PRICE_IMPACT_PERCENT",
        "MAX_QUOTE_LATENCY_MS",
        "MAX_CONSECUTIVE_QUOTE_FAILURES",
        "READINESS_MIN_ACTIVE_DAYS",
        "READINESS_MIN_CLOSED_TRADES",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env(require_discord_token=False)
    assert settings.paper_use_executable_quotes is True
    assert settings.paper_quote_output_buffer_bps == 50
    assert settings.max_adverse_entry_drift_percent == 8
    assert settings.max_quote_price_impact_percent == 2
    assert settings.max_quote_latency_ms == 5000
    assert settings.max_consecutive_quote_failures == 5
    assert settings.readiness_min_active_days == 14
    assert settings.readiness_min_closed_trades == 100
