from __future__ import annotations

from decimal import Decimal

from smart_money_bot.config import Settings
from smart_money_bot.constants import LIVE_ACK_TEXT, PUMP_LAUNCH_ACK_TEXT


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


def test_pump_launch_requires_separate_ack_wallet_and_pinata(monkeypatch) -> None:
    monkeypatch.setenv("PUMP_ONE_CLICK_LAUNCH_ENABLED", "true")
    monkeypatch.setenv("PUMP_LAUNCH_ACK", PUMP_LAUNCH_ACK_TEXT)
    monkeypatch.setenv("PUMP_LAUNCH_PRIVATE_KEY", "dedicated-secret-present")
    monkeypatch.setenv("PINATA_JWT", "pinata-secret-present")

    settings = Settings.from_env(require_discord_token=False)

    assert settings.pump_launch_is_unlocked is True
    assert settings.enable_live_trading is False


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
    monkeypatch.delenv("DISCOVERY_7D_REFRESH_SECONDS", raising=False)
    monkeypatch.delenv("DISCOVERY_CANDIDATE_PAGES", raising=False)
    settings = Settings.from_env(require_discord_token=False)
    assert settings.discovery_refresh_seconds == 1200
    assert settings.discovery_candidate_pages == 5
    assert settings.effective_discovery_refresh_seconds == 10800
    assert settings.effective_discovery_7d_refresh_seconds == 43200


def test_public_kol_and_forward_evidence_defaults_are_safe(monkeypatch) -> None:
    names = (
        "DISCOVERY_INCLUDE_KOLS",
        "DISCOVERY_KOL_LIMIT",
        "FORWARD_EVIDENCE_MIN_CLOSED_SELLS",
        "FORWARD_EVIDENCE_MIN_PROFIT_FACTOR",
        "FORWARD_EVIDENCE_MAX_LOSS_USD",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env(require_discord_token=False)

    assert settings.discovery_include_kols is True
    assert settings.discovery_kol_limit == 100
    assert settings.forward_evidence_min_closed_sells == 5
    assert settings.forward_evidence_min_profit_factor == Decimal("1.0")
    assert settings.forward_evidence_max_loss_usd == Decimal("10")


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
        "PAPER_ALLOW_PUMP_SOURCE_FALLBACK",
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
    assert settings.paper_allow_pump_source_fallback is False
    assert settings.paper_raw_entry_filter_enabled is True
    assert settings.raw_mirror_stop_loss_percent == 6
    assert settings.raw_mirror_take_profit_percent == 15
    assert settings.raw_mirror_trailing_activation_percent == 5
    assert settings.raw_mirror_trailing_stop_percent == 3
    assert settings.raw_mirror_max_hold_seconds == 3600


def test_daily_paper_profit_lock_defaults_to_100_pacific(monkeypatch) -> None:
    monkeypatch.delenv("PAPER_DAILY_TARGET_USD", raising=False)
    monkeypatch.delenv("PAPER_DAILY_PROFIT_LOCK_ENABLED", raising=False)
    monkeypatch.delenv("PAPER_DAILY_LOSS_LIMIT_USD", raising=False)
    monkeypatch.delenv("PAPER_DAILY_LOSS_LOCK_ENABLED", raising=False)
    monkeypatch.delenv("PAPER_DAILY_LOCK_TIMEZONE", raising=False)
    monkeypatch.delenv("PAPER_DAILY_PROFIT_CHECK_SECONDS", raising=False)

    settings = Settings.from_env(require_discord_token=False)

    assert settings.paper_daily_target_usd == Decimal("100")
    assert settings.paper_daily_profit_lock_enabled is True
    assert settings.paper_daily_loss_limit_usd == Decimal("20")
    assert settings.paper_daily_loss_lock_enabled is True
    assert settings.max_daily_loss_usd == Decimal("20")
    assert settings.paper_daily_lock_timezone == "America/Los_Angeles"
    assert settings.paper_daily_profit_check_seconds == 15


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
    assert settings.max_adverse_entry_drift_percent == 5
    assert settings.max_quote_price_impact_percent == Decimal("1.5")
    assert settings.max_quote_latency_ms == 5000
    assert settings.max_consecutive_quote_failures == 5
    assert settings.readiness_min_active_days == 14
    assert settings.readiness_min_closed_trades == 100


def test_selective_observation_and_tracking_baselines_default_off(monkeypatch) -> None:
    monkeypatch.delenv("PAPER_FORCE_OBSERVATION_MODE", raising=False)
    monkeypatch.delenv("PAPER_OBSERVATION_PENALTY_BPS", raising=False)
    monkeypatch.delenv("PAPER_SEED_TRACKING_BASELINES", raising=False)
    monkeypatch.delenv("PAPER_BASELINE_MAX_POSITIONS_PER_WALLET", raising=False)
    monkeypatch.delenv("REALTIME_STREAM_COMMITMENT", raising=False)
    settings = Settings.from_env(require_discord_token=False)
    assert settings.paper_force_observation_mode is False
    assert settings.paper_observation_penalty_bps == 300
    assert settings.paper_seed_tracking_baselines is False
    assert settings.paper_baseline_max_positions_per_wallet == 10
    assert settings.realtime_stream_commitment == "processed"


def test_v216_public_profile_nominations_default_to_slow_fail_closed_mode(
    monkeypatch,
) -> None:
    names = (
        "PUMP_PROFILE_DISCOVERY_ENABLED",
        "PUMP_PROFILE_PAGES",
        "PUMP_PROFILE_MIN_FOLLOWERS",
        "PUMP_PROFILE_LIMIT",
        "PUMP_PROFILE_MAX_PAGE_FETCHES",
        "PUMP_PROFILE_REFRESH_SECONDS",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env(require_discord_token=False)

    assert settings.pump_profile_discovery_enabled is True
    assert settings.pump_profile_pages == 1
    assert settings.pump_profile_min_followers == 1000
    assert settings.pump_profile_limit == 50
    assert settings.pump_profile_max_page_fetches == 25
    assert settings.pump_profile_refresh_seconds == 21600


def test_pump_source_price_fallback_defaults_off_for_executable_trial(
    monkeypatch,
) -> None:
    monkeypatch.delenv("PAPER_ALLOW_PUMP_SOURCE_FALLBACK", raising=False)
    monkeypatch.delenv("PAPER_PUMP_SOURCE_FALLBACK_BPS", raising=False)

    settings = Settings.from_env(require_discord_token=False)

    assert settings.paper_allow_pump_source_fallback is False
    assert settings.paper_pump_source_fallback_bps == 300


def test_news_radar_defaults_to_fast_but_cost_bounded_sources(monkeypatch) -> None:
    names = (
        "NEWS_RADAR_ENABLED",
        "X_NEWS_STREAM_ENABLED",
        "X_NEWS_STREAM_RULE",
        "X_CRYPTO_TRUSTED_ACCOUNTS",
        "NEWS_RSS_FEEDS",
        "J7_AUTHORIZED_FEED_URL",
        "NEWS_POLL_SECONDS",
        "NEWS_MIN_SCORE",
        "NEWS_LAUNCH_READY_SCORE",
        "NO_X_LAUNCH_CANDIDATES_ENABLED",
        "NO_X_LAUNCH_MIN_SCORE",
        "NEWS_X_VERIFY_MIN_SCORE",
        "NEWS_X_TREND_CACHE_SECONDS",
        "NEWS_MAX_ALERTS_PER_HOUR",
        "NEWS_SOURCE_IMAGE_ENABLED",
        "NEWS_DEX_MATCH_ENABLED",
        "NEWS_PAIR_RECHECK_SECONDS",
        "X_SEARCH_MAX_RESULTS",
        "X_DAILY_SEARCH_LIMIT",
        "X_DAILY_SEARCH_TIMEZONE",
        "X_PAID_SEARCH_ENABLED",
        "X_BUDGET_GUARD_ENABLED",
        "X_ESTIMATED_TOTAL_BUDGET_USD",
        "X_ESTIMATED_DAILY_BUDGET_USD",
        "X_MAX_TARGETED_VERIFICATIONS_PER_DAY",
        "X_VERIFY_MAX_POSTS",
        "X_ESTIMATED_POST_READ_USD",
        "X_ESTIMATED_USER_READ_USD",
        "X_BUDGET_PERIOD_ID",
        "X_USER_CACHE_SECONDS",
        "X_RADAR_ENABLED",
        "X_RADAR_QUERY",
        "X_RADAR_POLL_SECONDS",
        "X_RADAR_MAX_CONTRACTS_PER_SCAN",
        "FOMO_RADAR_ENABLED",
        "FOMO_RADAR_POLL_SECONDS",
        "FOMO_RADAR_MAX_CANDIDATES_PER_SCAN",
        "FOMO_RADAR_RECHECK_SECONDS",
        "FOMO_RUNNER_FRESH_ALERT_ENABLED",
        "FOMO_RUNNER_FRESH_MAX_AGE_SECONDS",
        "FOMO_RUNNER_FRESH_WATCH_ENABLED",
        "FOMO_RUNNER_FRESH_WATCH_SECONDS",
        "FOMO_RUNNER_FRESH_WATCH_MAX",
        "FOMO_WATCH_MIN_SCORE",
        "TRADE_ACTIVITY_ALERTS_ENABLED",
        "COIN_X_PREFILTER_MIN_SCORE",
        "COIN_WATCH_ALERTS_ENABLED",
        "COIN_WATCH_MIN_SCORE",
        "PUMP_ONE_CLICK_LAUNCH_ENABLED",
        "PUMP_LAUNCH_ACK",
        "PUMP_LAUNCH_PRIVATE_KEY",
        "PINATA_JWT",
        "J7_LAUNCH_ENABLED",
        "J7_LAUNCH_SESSION_TOKEN",
        "J7_LAUNCH_API_KEY",
        "J7_LAUNCH_REGION",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env(require_discord_token=False)

    assert settings.news_radar_enabled is True
    assert settings.x_news_stream_enabled is False
    assert "from:elonmusk" in settings.x_news_stream_rule
    assert "from:WatcherGuru" in settings.x_news_stream_rule
    assert "from:espn" not in settings.x_news_stream_rule
    assert "WatcherGuru" in settings.x_crypto_trusted_accounts
    assert settings.news_rss_feeds
    assert settings.j7_authorized_feed_url is None
    assert settings.news_poll_seconds == 30
    assert settings.news_min_score == 45
    assert settings.news_launch_ready_score == 72
    assert settings.no_x_launch_candidates_enabled is True
    assert settings.no_x_launch_min_score == 78
    assert settings.news_x_verify_min_score == 70
    assert settings.news_max_alerts_per_hour == 30
    assert settings.news_source_image_enabled is True
    assert settings.news_dex_match_enabled is True
    assert settings.pump_one_click_launch_enabled is False
    assert settings.pump_launch_is_unlocked is False
    assert settings.j7_launch_enabled is False
    assert settings.j7_launch_is_unlocked is False
    assert settings.j7_launch_region == "na-east"
    assert settings.news_pair_recheck_seconds == (0, 30, 90, 180)
    assert settings.x_search_max_results == 10
    assert settings.x_daily_search_limit == 10
    assert settings.x_budget_guard_enabled is True
    assert settings.x_estimated_total_budget_usd == Decimal("10")
    assert settings.x_estimated_daily_budget_usd == Decimal("0.50")
    assert settings.x_max_targeted_verifications_per_day == 10
    assert settings.x_verify_max_posts == 10
    assert settings.x_estimated_post_read_usd == Decimal("0.005")
    assert settings.x_estimated_user_read_usd == Decimal("0.010")
    assert settings.x_daily_search_timezone == "America/Los_Angeles"
    assert settings.x_paid_search_enabled is False
    assert settings.x_radar_enabled is False
    assert "pump.fun" in settings.x_radar_query
    assert "contract address" in settings.x_radar_query
    assert settings.x_radar_poll_seconds == 1800
    assert settings.x_radar_max_contracts_per_scan == 3
    assert settings.fomo_radar_enabled is True
    assert settings.fomo_radar_poll_seconds == 60
    assert settings.fomo_radar_max_candidates_per_scan == 12
    assert settings.fomo_radar_recheck_seconds == 1800
    assert settings.fomo_runner_fresh_alert_enabled is True
    assert settings.fomo_runner_fresh_max_age_seconds == 300
    assert settings.fomo_runner_fresh_watch_enabled is True
    assert settings.fomo_runner_fresh_watch_seconds == 15
    assert settings.fomo_runner_fresh_watch_max == 15
    assert settings.fomo_watch_min_score == Decimal("50")
    assert settings.trade_activity_alerts_enabled is False
    assert settings.coin_x_prefilter_min_score == Decimal("60")
    assert settings.coin_watch_alerts_enabled is True
    assert settings.coin_watch_min_score == Decimal("55")


def test_j7_launch_requires_encrypted_key_session_ack_and_pinata(monkeypatch) -> None:
    monkeypatch.setenv("J7_LAUNCH_ENABLED", "true")
    monkeypatch.setenv("J7_LAUNCH_SESSION_TOKEN", "session-jwt")
    monkeypatch.setenv("J7_LAUNCH_API_KEY", "encrypted-wallet-key")
    monkeypatch.setenv("J7_LAUNCH_REGION", "na-west")
    monkeypatch.setenv("PINATA_JWT", "pinata-jwt")
    monkeypatch.setenv(
        "PUMP_LAUNCH_ACK",
        "I_UNDERSTAND_PUMP_LAUNCHES_SPEND_REAL_SOL",
    )

    settings = Settings.from_env(require_discord_token=False)

    assert settings.j7_launch_is_unlocked is True
    assert settings.j7_launch_region == "na-west"
    assert settings.pump_launch_is_unlocked is False
