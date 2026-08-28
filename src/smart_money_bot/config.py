from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .constants import LIVE_ACK_TEXT, PUMP_LAUNCH_ACK_TEXT, USDC_MINT

DEFAULT_X_CRYPTO_TRUSTED_ACCOUNTS = "|".join(
    (
        "WatcherGuru",
        "CoinDesk",
        "Cointelegraph",
        "solana",
        "pumpdotfun",
        "lookonchain",
        "ArkhamIntel",
        "Bubblemaps",
        "Rugcheckxyz",
        "SolanaFloor",
        "JupiterExchange",
        "phantom",
    )
)

DEFAULT_X_NEWS_RULE = (
    "((from:WatcherGuru OR from:CoinDesk OR from:Cointelegraph OR from:solana OR "
    "from:pumpdotfun OR from:lookonchain OR from:ArkhamIntel OR from:Bubblemaps OR "
    "from:Rugcheckxyz OR from:SolanaFloor OR from:JupiterExchange OR from:phantom) OR "
    "((from:realDonaldTrump OR from:elonmusk OR from:WhiteHouse OR from:AP OR "
    "from:Reuters) (breaking OR emergency OR arrested OR resigns OR dies OR attack OR "
    "shutdown OR \"supreme court\")) OR ((\"pump.fun\" OR \"contract address\" OR "
    "\"CA:\") (solana OR memecoin OR token))) lang:en -is:retweet -is:reply"
)

DEFAULT_X_RADAR_QUERY = (
    '((solana OR pumpfun OR "pump.fun" OR memecoin OR "meme coin") '
    '("CA:" OR "contract address" OR "just launched" OR "fair launch" OR '
    '"ape in" OR buying OR bullish)) -is:retweet -is:reply lang:en'
)

DEFAULT_NEWS_RSS_FEEDS = "|".join(
    (
        "https://www.whitehouse.gov/briefings-statements/feed/",
        "https://www.sec.gov/news/pressreleases.rss",
        "https://www.federalreserve.gov/feeds/press_all.xml",
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
    )
)


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _decimal(name: str, default: str) -> Decimal:
    return Decimal(os.getenv(name, default))


def _optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else None


def _int_set(name: str) -> frozenset[int]:
    raw = os.getenv(name, "")
    return frozenset(int(item.strip()) for item in raw.split(",") if item.strip())


def _str_tuple(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip() for item in raw.split("|") if item.strip())


def _int_tuple(name: str, default: str = "") -> tuple[int, ...]:
    raw = os.getenv(name, default)
    return tuple(int(item.strip()) for item in raw.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    discord_token: str
    discord_guild_id: int | None
    discord_alert_channel_id: int | None
    discord_alert_user_id: int | None
    discord_admin_role_ids: frozenset[int]
    fomo_referral_code: str | None

    solana_rpc_url: str
    rpc_requests_per_second: int
    rpc_max_retries: int
    jupiter_api_key: str | None
    solana_tracker_api_key: str | None
    x_api_bearer_token: str | None
    x_crypto_trusted_accounts: tuple[str, ...]
    database_path: str

    coin_callouts_enabled: bool
    coin_callout_window_seconds: int
    coin_callout_cooldown_seconds: int
    coin_callout_min_alert_score: Decimal
    coin_x_prefilter_min_score: Decimal
    coin_watch_alerts_enabled: bool
    coin_watch_min_score: Decimal
    fomo_watch_min_score: Decimal
    trade_activity_alerts_enabled: bool
    x_search_max_results: int
    x_daily_search_limit: int
    x_daily_search_timezone: str
    x_paid_search_enabled: bool
    x_radar_enabled: bool
    x_radar_query: str
    x_radar_poll_seconds: int
    x_radar_max_contracts_per_scan: int
    fomo_radar_enabled: bool
    fomo_radar_poll_seconds: int
    fomo_radar_max_candidates_per_scan: int
    fomo_radar_recheck_seconds: int

    news_radar_enabled: bool
    x_news_stream_enabled: bool
    x_news_stream_rule: str
    news_rss_feeds: tuple[str, ...]
    j7_authorized_feed_url: str | None
    news_poll_seconds: int
    news_min_score: int
    news_launch_ready_score: int
    no_x_launch_candidates_enabled: bool
    no_x_launch_min_score: int
    news_x_verify_min_score: int
    news_x_trend_cache_seconds: int
    news_max_alerts_per_hour: int
    news_source_image_enabled: bool
    news_dex_match_enabled: bool
    news_dex_match_min_liquidity_usd: Decimal
    news_dex_match_max_age_minutes: int
    news_pair_recheck_seconds: tuple[int, ...]

    pump_one_click_launch_enabled: bool
    pump_launch_ack: str
    pump_launch_private_key: str | None
    pinata_jwt: str | None
    pump_launch_initial_buy_sol: Decimal
    pump_launch_min_score: int
    pump_launch_max_per_day: int
    pump_launch_max_sol_per_day: Decimal
    pump_launch_timezone: str
    pump_launch_cashback: bool
    pump_launch_mayhem_mode: bool
    pump_launch_tokenized_agent: bool
    pump_launch_buyback_bps: int
    j7_launch_enabled: bool
    j7_launch_session_token: str | None
    j7_launch_api_key: str | None
    j7_launch_region: str
    j7_launch_wallet_address: str | None
    j7_launch_min_balance_buffer_sol: Decimal
    launch_lab_enabled: bool
    launch_lab_min_score: int
    launch_lab_max_age_seconds: int
    launch_lab_max_candidates: int

    auto_discovery_enabled: bool
    discovery_refresh_seconds: int
    discovery_7d_refresh_seconds: int
    discovery_candidate_pages: int
    discovery_fetch_limit: int
    discovery_max_wallets: int
    discovery_min_24h_pnl_usd: Decimal
    discovery_min_win_rate_percent: Decimal
    discovery_min_roi_percent: Decimal
    discovery_min_trades: int
    discovery_max_trades: int
    discovery_min_closed_tokens: int
    discovery_max_single_token_percent: Decimal
    discovery_include_kols: bool
    discovery_kol_limit: int
    pump_profile_discovery_enabled: bool
    pump_profile_pages: int
    pump_profile_min_followers: int
    pump_profile_limit: int
    pump_profile_max_page_fetches: int
    pump_profile_refresh_seconds: int
    discovery_min_7d_pnl_usd: Decimal
    discovery_min_7d_win_rate_percent: Decimal
    discovery_min_7d_roi_percent: Decimal
    discovery_min_7d_trades: int
    discovery_max_7d_trades: int

    rotation_refresh_seconds: int
    rotation_max_idle_seconds: int
    rotation_probe_transactions: int
    rotation_min_recent_swaps: int
    rotation_min_pump_swaps: int
    rotation_require_pump_activity: bool
    forward_evidence_min_closed_sells: int
    forward_evidence_min_profit_factor: Decimal
    forward_evidence_max_loss_usd: Decimal
    realtime_wallet_stream_enabled: bool
    solana_ws_url: str | None
    realtime_stream_commitment: str

    poll_interval_seconds: int
    bootstrap_hours: int
    max_backfill_transactions: int
    min_source_trade_usd: Decimal

    consensus_min_traders: int
    consensus_window_seconds: int
    signal_cooldown_seconds: int
    min_trader_score: Decimal

    paper_starting_usd: Decimal
    default_copy_usd: Decimal
    simulated_fee_bps: int
    simulated_slippage_bps: int
    paper_mirror_raw_swaps: bool
    paper_require_current_price: bool
    paper_allow_pump_source_fallback: bool
    paper_pump_source_fallback_bps: int
    paper_raw_entry_filter_enabled: bool
    paper_force_observation_mode: bool
    paper_observation_penalty_bps: int
    paper_seed_tracking_baselines: bool
    paper_baseline_max_positions_per_wallet: int
    paper_sniper_test_enabled: bool
    paper_sniper_copy_usd: Decimal
    paper_sniper_min_liquidity_usd: Decimal
    paper_sniper_min_holders: int
    paper_sniper_max_top_holders_percent: Decimal
    paper_sniper_source_penalty_bps: int
    paper_sniper_max_entry_drift_percent: Decimal
    paper_sniper_max_quote_price_impact_percent: Decimal
    paper_daily_target_usd: Decimal
    paper_daily_profit_lock_enabled: bool
    paper_daily_loss_limit_usd: Decimal
    paper_daily_loss_lock_enabled: bool
    paper_daily_lock_timezone: str
    paper_daily_profit_check_seconds: int
    paper_use_executable_quotes: bool
    paper_quote_output_buffer_bps: int
    max_adverse_entry_drift_percent: Decimal
    max_quote_price_impact_percent: Decimal
    max_quote_latency_ms: int
    max_consecutive_quote_failures: int

    readiness_min_active_days: int
    readiness_min_closed_trades: int
    readiness_min_profit_factor: Decimal
    readiness_max_drawdown_percent: Decimal
    readiness_min_quote_success_percent: Decimal

    max_copy_usd: Decimal
    max_daily_loss_usd: Decimal
    max_open_positions: int
    min_token_liquidity_usd: Decimal
    min_token_holders: int
    min_organic_score: Decimal
    max_top_holders_percent: Decimal
    max_signal_age_seconds: int
    stop_loss_percent: Decimal
    take_profit_percent: Decimal
    max_hold_seconds: int
    raw_mirror_stop_loss_percent: Decimal
    raw_mirror_take_profit_percent: Decimal
    raw_mirror_trailing_activation_percent: Decimal
    raw_mirror_trailing_stop_percent: Decimal
    raw_mirror_max_hold_seconds: int

    enable_live_trading: bool
    live_trading_ack: str
    trading_private_key: str | None
    live_base_mint: str
    live_base_decimals: int

    @classmethod
    def from_env(cls, *, require_discord_token: bool = True) -> Settings:
        discord_token = os.getenv("DISCORD_TOKEN", "").strip()
        if require_discord_token and not discord_token:
            raise ValueError("DISCORD_TOKEN is required")

        settings = cls(
            discord_token=discord_token,
            discord_guild_id=_optional_int("DISCORD_GUILD_ID"),
            discord_alert_channel_id=_optional_int("DISCORD_ALERT_CHANNEL_ID"),
            discord_alert_user_id=_optional_int("DISCORD_ALERT_USER_ID"),
            discord_admin_role_ids=_int_set("DISCORD_ADMIN_ROLE_IDS"),
            fomo_referral_code=os.getenv("FOMO_REFERRAL_CODE", "WetOuterLemur").strip() or None,
            solana_rpc_url=os.getenv(
                "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
            ).strip(),
            rpc_requests_per_second=_int("RPC_REQUESTS_PER_SECOND", 8),
            rpc_max_retries=_int("RPC_MAX_RETRIES", 4),
            jupiter_api_key=os.getenv("JUPITER_API_KEY", "").strip() or None,
            solana_tracker_api_key=os.getenv("SOLANA_TRACKER_API_KEY", "").strip() or None,
            x_api_bearer_token=os.getenv("X_API_BEARER_TOKEN", "").strip() or None,
            x_crypto_trusted_accounts=_str_tuple(
                "X_CRYPTO_TRUSTED_ACCOUNTS", DEFAULT_X_CRYPTO_TRUSTED_ACCOUNTS
            ),
            database_path=os.getenv("DATABASE_PATH", "./data/smart_money.db").strip(),
            coin_callouts_enabled=_bool("COIN_CALLOUTS_ENABLED", True),
            coin_callout_window_seconds=_int("COIN_CALLOUT_WINDOW_SECONDS", 300),
            coin_callout_cooldown_seconds=_int("COIN_CALLOUT_COOLDOWN_SECONDS", 3600),
            coin_callout_min_alert_score=_decimal("COIN_CALLOUT_MIN_ALERT_SCORE", "70"),
            coin_x_prefilter_min_score=_decimal("COIN_X_PREFILTER_MIN_SCORE", "35"),
            coin_watch_alerts_enabled=_bool("COIN_WATCH_ALERTS_ENABLED", True),
            coin_watch_min_score=_decimal("COIN_WATCH_MIN_SCORE", "55"),
            fomo_watch_min_score=_decimal("FOMO_WATCH_MIN_SCORE", "50"),
            trade_activity_alerts_enabled=_bool("TRADE_ACTIVITY_ALERTS_ENABLED", False),
            x_search_max_results=_int("X_SEARCH_MAX_RESULTS", 10),
            x_daily_search_limit=_int("X_DAILY_SEARCH_LIMIT", 25),
            x_daily_search_timezone=os.getenv(
                "X_DAILY_SEARCH_TIMEZONE", "America/Los_Angeles"
            ).strip(),
            x_paid_search_enabled=_bool("X_PAID_SEARCH_ENABLED", False),
            x_radar_enabled=_bool("X_RADAR_ENABLED", False),
            x_radar_query=os.getenv("X_RADAR_QUERY", DEFAULT_X_RADAR_QUERY).strip(),
            x_radar_poll_seconds=_int("X_RADAR_POLL_SECONDS", 1800),
            x_radar_max_contracts_per_scan=_int("X_RADAR_MAX_CONTRACTS_PER_SCAN", 3),
            fomo_radar_enabled=_bool("FOMO_RADAR_ENABLED", True),
            fomo_radar_poll_seconds=_int("FOMO_RADAR_POLL_SECONDS", 300),
            fomo_radar_max_candidates_per_scan=_int(
                "FOMO_RADAR_MAX_CANDIDATES_PER_SCAN", 5
            ),
            fomo_radar_recheck_seconds=_int("FOMO_RADAR_RECHECK_SECONDS", 1800),
            news_radar_enabled=_bool("NEWS_RADAR_ENABLED", True),
            x_news_stream_enabled=_bool("X_NEWS_STREAM_ENABLED", False),
            x_news_stream_rule=os.getenv("X_NEWS_STREAM_RULE", DEFAULT_X_NEWS_RULE).strip(),
            news_rss_feeds=_str_tuple("NEWS_RSS_FEEDS", DEFAULT_NEWS_RSS_FEEDS),
            j7_authorized_feed_url=(
                os.getenv("J7_AUTHORIZED_FEED_URL", "").strip() or None
            ),
            news_poll_seconds=_int("NEWS_POLL_SECONDS", 30),
            news_min_score=_int("NEWS_MIN_SCORE", 45),
            news_launch_ready_score=_int("NEWS_LAUNCH_READY_SCORE", 72),
            no_x_launch_candidates_enabled=_bool(
                "NO_X_LAUNCH_CANDIDATES_ENABLED", True
            ),
            no_x_launch_min_score=_int("NO_X_LAUNCH_MIN_SCORE", 78),
            news_x_verify_min_score=_int("NEWS_X_VERIFY_MIN_SCORE", 70),
            news_x_trend_cache_seconds=_int("NEWS_X_TREND_CACHE_SECONDS", 3600),
            news_max_alerts_per_hour=_int("NEWS_MAX_ALERTS_PER_HOUR", 30),
            news_source_image_enabled=_bool("NEWS_SOURCE_IMAGE_ENABLED", True),
            news_dex_match_enabled=_bool("NEWS_DEX_MATCH_ENABLED", True),
            news_dex_match_min_liquidity_usd=_decimal(
                "NEWS_DEX_MATCH_MIN_LIQUIDITY_USD", "2000"
            ),
            news_dex_match_max_age_minutes=_int("NEWS_DEX_MATCH_MAX_AGE_MINUTES", 60),
            news_pair_recheck_seconds=_int_tuple(
                "NEWS_PAIR_RECHECK_SECONDS", "0,30,90,180"
            ),
            pump_one_click_launch_enabled=_bool("PUMP_ONE_CLICK_LAUNCH_ENABLED", False),
            pump_launch_ack=os.getenv("PUMP_LAUNCH_ACK", "").strip(),
            pump_launch_private_key=(
                os.getenv("PUMP_LAUNCH_PRIVATE_KEY", "").strip() or None
            ),
            pinata_jwt=os.getenv("PINATA_JWT", "").strip() or None,
            pump_launch_initial_buy_sol=_decimal("PUMP_LAUNCH_INITIAL_BUY_SOL", "0.01"),
            pump_launch_min_score=_int("PUMP_LAUNCH_MIN_SCORE", 72),
            pump_launch_max_per_day=_int("PUMP_LAUNCH_MAX_PER_DAY", 3),
            pump_launch_max_sol_per_day=_decimal("PUMP_LAUNCH_MAX_SOL_PER_DAY", "0.05"),
            pump_launch_timezone=os.getenv(
                "PUMP_LAUNCH_TIMEZONE", "America/Los_Angeles"
            ).strip(),
            pump_launch_cashback=_bool("PUMP_LAUNCH_CASHBACK", False),
            pump_launch_mayhem_mode=_bool("PUMP_LAUNCH_MAYHEM_MODE", False),
            pump_launch_tokenized_agent=_bool("PUMP_LAUNCH_TOKENIZED_AGENT", False),
            pump_launch_buyback_bps=_int("PUMP_LAUNCH_BUYBACK_BPS", 5000),
            j7_launch_enabled=_bool("J7_LAUNCH_ENABLED", False),
            j7_launch_session_token=(
                os.getenv("J7_LAUNCH_SESSION_TOKEN", "").strip() or None
            ),
            j7_launch_api_key=os.getenv("J7_LAUNCH_API_KEY", "").strip() or None,
            j7_launch_region=os.getenv("J7_LAUNCH_REGION", "na-east").strip().lower(),
            j7_launch_wallet_address=(
                os.getenv("J7_LAUNCH_WALLET_ADDRESS", "").strip() or None
            ),
            j7_launch_min_balance_buffer_sol=_decimal(
                "J7_LAUNCH_MIN_BALANCE_BUFFER_SOL", "0.002"
            ),
            launch_lab_enabled=_bool("LAUNCH_LAB_ENABLED", True),
            launch_lab_min_score=_int("LAUNCH_LAB_MIN_SCORE", 60),
            launch_lab_max_age_seconds=_int("LAUNCH_LAB_MAX_AGE_SECONDS", 3600),
            launch_lab_max_candidates=_int("LAUNCH_LAB_MAX_CANDIDATES", 8),
            auto_discovery_enabled=_bool("AUTO_DISCOVERY_ENABLED", True),
            discovery_refresh_seconds=_int("DISCOVERY_REFRESH_SECONDS", 1200),
            discovery_7d_refresh_seconds=_int("DISCOVERY_7D_REFRESH_SECONDS", 21600),
            discovery_candidate_pages=_int("DISCOVERY_CANDIDATE_PAGES", 5),
            discovery_fetch_limit=_int("DISCOVERY_FETCH_LIMIT", 100),
            discovery_max_wallets=_int("DISCOVERY_MAX_WALLETS", 25),
            discovery_min_24h_pnl_usd=_decimal("DISCOVERY_MIN_24H_PNL_USD", "100"),
            discovery_min_win_rate_percent=_decimal("DISCOVERY_MIN_WIN_RATE_PERCENT", "55"),
            discovery_min_roi_percent=_decimal("DISCOVERY_MIN_ROI_PERCENT", "3"),
            discovery_min_trades=_int("DISCOVERY_MIN_TRADES", 5),
            discovery_max_trades=_int("DISCOVERY_MAX_TRADES", 250),
            discovery_min_closed_tokens=_int("DISCOVERY_MIN_CLOSED_TOKENS", 2),
            discovery_max_single_token_percent=_decimal("DISCOVERY_MAX_SINGLE_TOKEN_PERCENT", "70"),
            discovery_include_kols=_bool("DISCOVERY_INCLUDE_KOLS", True),
            discovery_kol_limit=_int("DISCOVERY_KOL_LIMIT", 100),
            pump_profile_discovery_enabled=_bool("PUMP_PROFILE_DISCOVERY_ENABLED", True),
            pump_profile_pages=_int("PUMP_PROFILE_PAGES", 1),
            pump_profile_min_followers=_int("PUMP_PROFILE_MIN_FOLLOWERS", 1000),
            pump_profile_limit=_int("PUMP_PROFILE_LIMIT", 50),
            pump_profile_max_page_fetches=_int("PUMP_PROFILE_MAX_PAGE_FETCHES", 25),
            pump_profile_refresh_seconds=_int("PUMP_PROFILE_REFRESH_SECONDS", 21600),
            discovery_min_7d_pnl_usd=_decimal("DISCOVERY_MIN_7D_PNL_USD", "300"),
            discovery_min_7d_win_rate_percent=_decimal("DISCOVERY_MIN_7D_WIN_RATE_PERCENT", "55"),
            discovery_min_7d_roi_percent=_decimal("DISCOVERY_MIN_7D_ROI_PERCENT", "5"),
            discovery_min_7d_trades=_int("DISCOVERY_MIN_7D_TRADES", 10),
            discovery_max_7d_trades=_int("DISCOVERY_MAX_7D_TRADES", 1000),
            rotation_refresh_seconds=_int("ROTATION_REFRESH_SECONDS", 300),
            rotation_max_idle_seconds=_int("ROTATION_MAX_IDLE_SECONDS", 3600),
            rotation_probe_transactions=_int("ROTATION_PROBE_TRANSACTIONS", 6),
            rotation_min_recent_swaps=_int("ROTATION_MIN_RECENT_SWAPS", 1),
            rotation_min_pump_swaps=_int("ROTATION_MIN_PUMP_SWAPS", 1),
            rotation_require_pump_activity=_bool("ROTATION_REQUIRE_PUMP_ACTIVITY", True),
            forward_evidence_min_closed_sells=_int("FORWARD_EVIDENCE_MIN_CLOSED_SELLS", 5),
            forward_evidence_min_profit_factor=_decimal(
                "FORWARD_EVIDENCE_MIN_PROFIT_FACTOR", "1.0"
            ),
            forward_evidence_max_loss_usd=_decimal("FORWARD_EVIDENCE_MAX_LOSS_USD", "10"),
            realtime_wallet_stream_enabled=_bool("REALTIME_WALLET_STREAM_ENABLED", True),
            solana_ws_url=os.getenv("SOLANA_WS_URL", "").strip() or None,
            realtime_stream_commitment=os.getenv("REALTIME_STREAM_COMMITMENT", "processed")
            .strip()
            .lower(),
            poll_interval_seconds=_int("POLL_INTERVAL_SECONDS", 60),
            bootstrap_hours=_int("BOOTSTRAP_HOURS", 24),
            max_backfill_transactions=_int("MAX_BACKFILL_TRANSACTIONS", 100),
            min_source_trade_usd=_decimal("MIN_SOURCE_TRADE_USD", "100"),
            consensus_min_traders=_int("CONSENSUS_MIN_TRADERS", 2),
            consensus_window_seconds=_int("CONSENSUS_WINDOW_SECONDS", 300),
            signal_cooldown_seconds=_int("SIGNAL_COOLDOWN_SECONDS", 900),
            min_trader_score=_decimal("MIN_TRADER_SCORE", "25"),
            paper_starting_usd=_decimal("PAPER_STARTING_USD", "1000"),
            default_copy_usd=_decimal("DEFAULT_COPY_USD", "10"),
            simulated_fee_bps=_int("SIMULATED_FEE_BPS", 60),
            simulated_slippage_bps=_int("SIMULATED_SLIPPAGE_BPS", 100),
            paper_mirror_raw_swaps=_bool("PAPER_MIRROR_RAW_SWAPS", True),
            paper_require_current_price=_bool("PAPER_REQUIRE_CURRENT_PRICE", True),
            paper_allow_pump_source_fallback=_bool("PAPER_ALLOW_PUMP_SOURCE_FALLBACK", False),
            paper_pump_source_fallback_bps=_int("PAPER_PUMP_SOURCE_FALLBACK_BPS", 300),
            paper_raw_entry_filter_enabled=_bool("PAPER_RAW_ENTRY_FILTER_ENABLED", True),
            paper_force_observation_mode=_bool("PAPER_FORCE_OBSERVATION_MODE", False),
            paper_observation_penalty_bps=_int("PAPER_OBSERVATION_PENALTY_BPS", 300),
            paper_seed_tracking_baselines=_bool("PAPER_SEED_TRACKING_BASELINES", False),
            paper_baseline_max_positions_per_wallet=_int(
                "PAPER_BASELINE_MAX_POSITIONS_PER_WALLET", 10
            ),
            paper_sniper_test_enabled=_bool("PAPER_SNIPER_TEST_ENABLED", False),
            paper_sniper_copy_usd=_decimal("PAPER_SNIPER_COPY_USD", "2"),
            paper_sniper_min_liquidity_usd=_decimal("PAPER_SNIPER_MIN_LIQUIDITY_USD", "2000"),
            paper_sniper_min_holders=_int("PAPER_SNIPER_MIN_HOLDERS", 20),
            paper_sniper_max_top_holders_percent=_decimal(
                "PAPER_SNIPER_MAX_TOP_HOLDERS_PERCENT", "85"
            ),
            paper_sniper_source_penalty_bps=_int("PAPER_SNIPER_SOURCE_PENALTY_BPS", 500),
            paper_sniper_max_entry_drift_percent=_decimal(
                "PAPER_SNIPER_MAX_ENTRY_DRIFT_PERCENT", "20"
            ),
            paper_sniper_max_quote_price_impact_percent=_decimal(
                "PAPER_SNIPER_MAX_QUOTE_PRICE_IMPACT_PERCENT", "5"
            ),
            paper_daily_target_usd=_decimal("PAPER_DAILY_TARGET_USD", "100"),
            paper_daily_profit_lock_enabled=_bool("PAPER_DAILY_PROFIT_LOCK_ENABLED", True),
            paper_daily_loss_limit_usd=_decimal("PAPER_DAILY_LOSS_LIMIT_USD", "20"),
            paper_daily_loss_lock_enabled=_bool("PAPER_DAILY_LOSS_LOCK_ENABLED", True),
            paper_daily_lock_timezone=os.getenv(
                "PAPER_DAILY_LOCK_TIMEZONE", "America/Los_Angeles"
            ).strip(),
            paper_daily_profit_check_seconds=_int("PAPER_DAILY_PROFIT_CHECK_SECONDS", 15),
            paper_use_executable_quotes=_bool("PAPER_USE_EXECUTABLE_QUOTES", True),
            paper_quote_output_buffer_bps=_int("PAPER_QUOTE_OUTPUT_BUFFER_BPS", 50),
            max_adverse_entry_drift_percent=_decimal("MAX_ADVERSE_ENTRY_DRIFT_PERCENT", "5"),
            max_quote_price_impact_percent=_decimal("MAX_QUOTE_PRICE_IMPACT_PERCENT", "1.5"),
            max_quote_latency_ms=_int("MAX_QUOTE_LATENCY_MS", 5000),
            max_consecutive_quote_failures=_int("MAX_CONSECUTIVE_QUOTE_FAILURES", 5),
            readiness_min_active_days=_int("READINESS_MIN_ACTIVE_DAYS", 14),
            readiness_min_closed_trades=_int("READINESS_MIN_CLOSED_TRADES", 100),
            readiness_min_profit_factor=_decimal("READINESS_MIN_PROFIT_FACTOR", "1.25"),
            readiness_max_drawdown_percent=_decimal("READINESS_MAX_DRAWDOWN_PERCENT", "10"),
            readiness_min_quote_success_percent=_decimal(
                "READINESS_MIN_QUOTE_SUCCESS_PERCENT", "95"
            ),
            max_copy_usd=_decimal("MAX_COPY_USD", "25"),
            max_daily_loss_usd=_decimal("MAX_DAILY_LOSS_USD", "20"),
            max_open_positions=_int("MAX_OPEN_POSITIONS", 4),
            min_token_liquidity_usd=_decimal("MIN_TOKEN_LIQUIDITY_USD", "50000"),
            min_token_holders=_int("MIN_TOKEN_HOLDERS", 100),
            min_organic_score=_decimal("MIN_ORGANIC_SCORE", "20"),
            max_top_holders_percent=_decimal("MAX_TOP_HOLDERS_PERCENT", "70"),
            max_signal_age_seconds=_int("MAX_SIGNAL_AGE_SECONDS", 90),
            stop_loss_percent=_decimal("STOP_LOSS_PERCENT", "12"),
            take_profit_percent=_decimal("TAKE_PROFIT_PERCENT", "30"),
            max_hold_seconds=_int("MAX_HOLD_SECONDS", 21_600),
            raw_mirror_stop_loss_percent=_decimal("RAW_MIRROR_STOP_LOSS_PERCENT", "6"),
            raw_mirror_take_profit_percent=_decimal("RAW_MIRROR_TAKE_PROFIT_PERCENT", "15"),
            raw_mirror_trailing_activation_percent=_decimal(
                "RAW_MIRROR_TRAILING_ACTIVATION_PERCENT", "5"
            ),
            raw_mirror_trailing_stop_percent=_decimal("RAW_MIRROR_TRAILING_STOP_PERCENT", "3"),
            raw_mirror_max_hold_seconds=_int("RAW_MIRROR_MAX_HOLD_SECONDS", 3_600),
            enable_live_trading=_bool("ENABLE_LIVE_TRADING", False),
            live_trading_ack=os.getenv("LIVE_TRADING_ACK", "").strip(),
            trading_private_key=os.getenv("TRADING_PRIVATE_KEY", "").strip() or None,
            live_base_mint=os.getenv("LIVE_BASE_MINT", USDC_MINT).strip(),
            live_base_decimals=_int("LIVE_BASE_DECIMALS", 6),
        )
        settings.validate()
        return settings

    @property
    def live_is_unlocked(self) -> bool:
        return (
            self.enable_live_trading
            and self.live_trading_ack == LIVE_ACK_TEXT
            and bool(self.trading_private_key)
            and bool(self.jupiter_api_key)
        )

    @property
    def pump_launch_is_unlocked(self) -> bool:
        return (
            self.pump_one_click_launch_enabled
            and self.pump_launch_ack == PUMP_LAUNCH_ACK_TEXT
            and bool(self.pump_launch_private_key)
            and bool(self.pinata_jwt)
        )

    @property
    def j7_launch_is_unlocked(self) -> bool:
        return (
            self.j7_launch_enabled
            and self.pump_launch_ack == PUMP_LAUNCH_ACK_TEXT
            and bool(self.j7_launch_session_token)
            and bool(self.j7_launch_api_key)
            and bool(self.pinata_jwt)
        )

    @property
    def discovery_is_configured(self) -> bool:
        return self.auto_discovery_enabled and bool(self.solana_tracker_api_key)

    @property
    def effective_discovery_refresh_seconds(self) -> int:
        """Clamp multi-page reads to a free-tier-friendly daily request budget."""

        if self.discovery_candidate_pages <= 1:
            return self.discovery_refresh_seconds
        return max(
            self.discovery_refresh_seconds,
            self.discovery_candidate_pages * 2_160,
        )

    @property
    def effective_discovery_7d_refresh_seconds(self) -> int:
        if self.discovery_candidate_pages <= 1:
            return self.discovery_7d_refresh_seconds
        return max(
            self.discovery_7d_refresh_seconds,
            self.discovery_candidate_pages * 8_640,
        )

    def validate(self) -> None:
        if self.consensus_min_traders < 1:
            raise ValueError("CONSENSUS_MIN_TRADERS must be at least 1")
        if not 60 <= self.coin_callout_window_seconds <= 3600:
            raise ValueError("COIN_CALLOUT_WINDOW_SECONDS must be between 60 and 3600")
        if not 30 <= self.coin_callout_cooldown_seconds <= 3600:
            raise ValueError("COIN_CALLOUT_COOLDOWN_SECONDS must be between 30 and 3600")
        if not 0 <= self.coin_callout_min_alert_score <= 100:
            raise ValueError("COIN_CALLOUT_MIN_ALERT_SCORE must be between 0 and 100")
        if not 0 <= self.coin_x_prefilter_min_score <= 100:
            raise ValueError("COIN_X_PREFILTER_MIN_SCORE must be between 0 and 100")
        if not 0 <= self.coin_watch_min_score <= 100:
            raise ValueError("COIN_WATCH_MIN_SCORE must be between 0 and 100")
        if not 0 <= self.fomo_watch_min_score <= 100:
            raise ValueError("FOMO_WATCH_MIN_SCORE must be between 0 and 100")
        if not 10 <= self.x_search_max_results <= 100:
            raise ValueError("X_SEARCH_MAX_RESULTS must be between 10 and 100")
        if not 1 <= self.x_daily_search_limit <= 500:
            raise ValueError("X_DAILY_SEARCH_LIMIT must be between 1 and 500")
        try:
            ZoneInfo(self.x_daily_search_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("X_DAILY_SEARCH_TIMEZONE must be a valid IANA timezone") from exc
        if not self.x_radar_query:
            raise ValueError("X_RADAR_QUERY cannot be empty")
        if len(self.x_radar_query) > 1024:
            raise ValueError("X_RADAR_QUERY cannot exceed 1024 characters")
        if not 300 <= self.x_radar_poll_seconds <= 86400:
            raise ValueError("X_RADAR_POLL_SECONDS must be between 300 and 86400")
        if not 1 <= self.x_radar_max_contracts_per_scan <= 10:
            raise ValueError("X_RADAR_MAX_CONTRACTS_PER_SCAN must be between 1 and 10")
        if not 60 <= self.fomo_radar_poll_seconds <= 86400:
            raise ValueError("FOMO_RADAR_POLL_SECONDS must be between 60 and 86400")
        if not 1 <= self.fomo_radar_max_candidates_per_scan <= 20:
            raise ValueError("FOMO_RADAR_MAX_CANDIDATES_PER_SCAN must be between 1 and 20")
        if not 300 <= self.fomo_radar_recheck_seconds <= 86400:
            raise ValueError("FOMO_RADAR_RECHECK_SECONDS must be between 300 and 86400")
        if len(self.x_news_stream_rule) > 1024:
            raise ValueError("X_NEWS_STREAM_RULE cannot exceed 1024 characters")
        if not 15 <= self.news_poll_seconds <= 3600:
            raise ValueError("NEWS_POLL_SECONDS must be between 15 and 3600")
        if not 0 <= self.news_min_score <= 100:
            raise ValueError("NEWS_MIN_SCORE must be between 0 and 100")
        if not self.news_min_score <= self.news_launch_ready_score <= 100:
            raise ValueError(
                "NEWS_LAUNCH_READY_SCORE must be between NEWS_MIN_SCORE and 100"
            )
        if not self.news_min_score <= self.no_x_launch_min_score <= 100:
            raise ValueError(
                "NO_X_LAUNCH_MIN_SCORE must be between NEWS_MIN_SCORE and 100"
            )
        if not 0 <= self.news_x_verify_min_score <= 100:
            raise ValueError("NEWS_X_VERIFY_MIN_SCORE must be between 0 and 100")
        if not 30 <= self.news_x_trend_cache_seconds <= 3600:
            raise ValueError("NEWS_X_TREND_CACHE_SECONDS must be between 30 and 3600")
        if not 1 <= self.news_max_alerts_per_hour <= 200:
            raise ValueError("NEWS_MAX_ALERTS_PER_HOUR must be between 1 and 200")
        if self.news_dex_match_min_liquidity_usd < 0:
            raise ValueError("NEWS_DEX_MATCH_MIN_LIQUIDITY_USD cannot be negative")
        if not 1 <= self.news_dex_match_max_age_minutes <= 1440:
            raise ValueError("NEWS_DEX_MATCH_MAX_AGE_MINUTES must be between 1 and 1440")
        if any(item < 0 or item > 900 for item in self.news_pair_recheck_seconds):
            raise ValueError("NEWS_PAIR_RECHECK_SECONDS values must be between 0 and 900")
        if self.pump_launch_initial_buy_sol <= 0:
            raise ValueError("PUMP_LAUNCH_INITIAL_BUY_SOL must be positive")
        if not self.news_launch_ready_score <= self.pump_launch_min_score <= 100:
            raise ValueError(
                "PUMP_LAUNCH_MIN_SCORE must be between NEWS_LAUNCH_READY_SCORE and 100"
            )
        if not 1 <= self.pump_launch_max_per_day <= 20:
            raise ValueError("PUMP_LAUNCH_MAX_PER_DAY must be between 1 and 20")
        if self.pump_launch_max_sol_per_day < self.pump_launch_initial_buy_sol:
            raise ValueError(
                "PUMP_LAUNCH_MAX_SOL_PER_DAY cannot be below PUMP_LAUNCH_INITIAL_BUY_SOL"
            )
        try:
            ZoneInfo(self.pump_launch_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("PUMP_LAUNCH_TIMEZONE must be a valid IANA timezone") from exc
        if not 0 <= self.pump_launch_buyback_bps <= 10_000:
            raise ValueError("PUMP_LAUNCH_BUYBACK_BPS must be between 0 and 10000")
        if self.j7_launch_region not in {
            "na-east",
            "na-west",
            "europe",
            "asia",
            "australia",
        }:
            raise ValueError(
                "J7_LAUNCH_REGION must be na-east, na-west, europe, asia, or australia"
            )
        if self.j7_launch_min_balance_buffer_sol < 0:
            raise ValueError("J7_LAUNCH_MIN_BALANCE_BUFFER_SOL cannot be negative")
        if not self.news_min_score <= self.launch_lab_min_score <= 100:
            raise ValueError("LAUNCH_LAB_MIN_SCORE must be between NEWS_MIN_SCORE and 100")
        if not 300 <= self.launch_lab_max_age_seconds <= 86_400:
            raise ValueError("LAUNCH_LAB_MAX_AGE_SECONDS must be between 300 and 86400")
        if not 1 <= self.launch_lab_max_candidates <= 20:
            raise ValueError("LAUNCH_LAB_MAX_CANDIDATES must be between 1 and 20")
        if not 1 <= self.rpc_requests_per_second <= 100:
            raise ValueError("RPC_REQUESTS_PER_SECOND must be between 1 and 100")
        if not 0 <= self.rpc_max_retries <= 10:
            raise ValueError("RPC_MAX_RETRIES must be between 0 and 10")
        if self.discovery_refresh_seconds < 300:
            raise ValueError("DISCOVERY_REFRESH_SECONDS must be at least 300")
        if self.discovery_7d_refresh_seconds < self.discovery_refresh_seconds:
            raise ValueError(
                "DISCOVERY_7D_REFRESH_SECONDS cannot be below DISCOVERY_REFRESH_SECONDS"
            )
        if not 1 <= self.discovery_candidate_pages <= 5:
            raise ValueError("DISCOVERY_CANDIDATE_PAGES must be between 1 and 5")
        if not 1 <= self.discovery_fetch_limit <= 500:
            raise ValueError("DISCOVERY_FETCH_LIMIT must be between 1 and 500")
        if not 1 <= self.discovery_max_wallets <= 50:
            raise ValueError("DISCOVERY_MAX_WALLETS must be between 1 and 50")
        if self.discovery_max_wallets > self.discovery_fetch_limit:
            raise ValueError("DISCOVERY_MAX_WALLETS cannot exceed DISCOVERY_FETCH_LIMIT")
        if self.discovery_min_24h_pnl_usd < 0:
            raise ValueError("DISCOVERY_MIN_24H_PNL_USD cannot be negative")
        if not 0 <= self.discovery_min_win_rate_percent <= 100:
            raise ValueError("DISCOVERY_MIN_WIN_RATE_PERCENT must be between 0 and 100")
        if self.discovery_min_trades < 1:
            raise ValueError("DISCOVERY_MIN_TRADES must be at least 1")
        if self.discovery_max_trades < self.discovery_min_trades:
            raise ValueError("DISCOVERY_MAX_TRADES cannot be below DISCOVERY_MIN_TRADES")
        if self.discovery_min_closed_tokens < 1:
            raise ValueError("DISCOVERY_MIN_CLOSED_TOKENS must be at least 1")
        if not 1 <= self.discovery_max_single_token_percent <= 100:
            raise ValueError("DISCOVERY_MAX_SINGLE_TOKEN_PERCENT must be between 1 and 100")
        if not 1 <= self.discovery_kol_limit <= 100:
            raise ValueError("DISCOVERY_KOL_LIMIT must be between 1 and 100")
        if not 1 <= self.pump_profile_pages <= 10:
            raise ValueError("PUMP_PROFILE_PAGES must be between 1 and 10")
        if self.pump_profile_min_followers < 0:
            raise ValueError("PUMP_PROFILE_MIN_FOLLOWERS cannot be negative")
        if not 1 <= self.pump_profile_limit <= 500:
            raise ValueError("PUMP_PROFILE_LIMIT must be between 1 and 500")
        if not 0 <= self.pump_profile_max_page_fetches <= 100:
            raise ValueError("PUMP_PROFILE_MAX_PAGE_FETCHES must be between 0 and 100")
        if self.pump_profile_refresh_seconds < 3600:
            raise ValueError("PUMP_PROFILE_REFRESH_SECONDS must be at least 3600")
        if self.discovery_min_7d_pnl_usd < 0:
            raise ValueError("DISCOVERY_MIN_7D_PNL_USD cannot be negative")
        if not 0 <= self.discovery_min_7d_win_rate_percent <= 100:
            raise ValueError("DISCOVERY_MIN_7D_WIN_RATE_PERCENT must be between 0 and 100")
        if self.discovery_min_7d_roi_percent < 0:
            raise ValueError("DISCOVERY_MIN_7D_ROI_PERCENT cannot be negative")
        if self.discovery_min_7d_trades < 1:
            raise ValueError("DISCOVERY_MIN_7D_TRADES must be at least 1")
        if self.discovery_max_7d_trades < self.discovery_min_7d_trades:
            raise ValueError("DISCOVERY_MAX_7D_TRADES cannot be below DISCOVERY_MIN_7D_TRADES")
        if self.rotation_refresh_seconds < 300:
            raise ValueError("ROTATION_REFRESH_SECONDS must be at least 300")
        if self.rotation_max_idle_seconds < self.rotation_refresh_seconds:
            raise ValueError("ROTATION_MAX_IDLE_SECONDS cannot be below ROTATION_REFRESH_SECONDS")
        if not 1 <= self.rotation_probe_transactions <= 25:
            raise ValueError("ROTATION_PROBE_TRANSACTIONS must be between 1 and 25")
        if self.rotation_min_recent_swaps < 1:
            raise ValueError("ROTATION_MIN_RECENT_SWAPS must be at least 1")
        if self.rotation_min_pump_swaps < 1:
            raise ValueError("ROTATION_MIN_PUMP_SWAPS must be at least 1")
        if self.forward_evidence_min_closed_sells < 1:
            raise ValueError("FORWARD_EVIDENCE_MIN_CLOSED_SELLS must be at least 1")
        if self.forward_evidence_min_profit_factor < 0:
            raise ValueError("FORWARD_EVIDENCE_MIN_PROFIT_FACTOR cannot be negative")
        if self.forward_evidence_max_loss_usd <= 0:
            raise ValueError("FORWARD_EVIDENCE_MAX_LOSS_USD must be positive")
        if self.realtime_stream_commitment not in {"processed", "confirmed"}:
            raise ValueError("REALTIME_STREAM_COMMITMENT must be processed or confirmed")
        if self.poll_interval_seconds < 5:
            raise ValueError("POLL_INTERVAL_SECONDS must be at least 5")
        if self.max_copy_usd <= 0 or self.default_copy_usd <= 0:
            raise ValueError("Copy sizes must be positive")
        if self.default_copy_usd > self.max_copy_usd:
            raise ValueError("DEFAULT_COPY_USD cannot exceed MAX_COPY_USD")
        if not 0 <= self.simulated_fee_bps <= 10_000:
            raise ValueError("SIMULATED_FEE_BPS must be between 0 and 10000")
        if not 0 <= self.simulated_slippage_bps <= 10_000:
            raise ValueError("SIMULATED_SLIPPAGE_BPS must be between 0 and 10000")
        if not 0 <= self.paper_pump_source_fallback_bps <= 10_000:
            raise ValueError("PAPER_PUMP_SOURCE_FALLBACK_BPS must be between 0 and 10000")
        if not 0 <= self.paper_observation_penalty_bps <= 10_000:
            raise ValueError("PAPER_OBSERVATION_PENALTY_BPS must be between 0 and 10000")
        if not 1 <= self.paper_baseline_max_positions_per_wallet <= 50:
            raise ValueError("PAPER_BASELINE_MAX_POSITIONS_PER_WALLET must be between 1 and 50")
        if self.paper_sniper_copy_usd <= 0:
            raise ValueError("PAPER_SNIPER_COPY_USD must be positive")
        if self.paper_sniper_copy_usd > self.max_copy_usd:
            raise ValueError("PAPER_SNIPER_COPY_USD cannot exceed MAX_COPY_USD")
        if self.paper_sniper_min_liquidity_usd < 0:
            raise ValueError("PAPER_SNIPER_MIN_LIQUIDITY_USD cannot be negative")
        if self.paper_sniper_min_holders < 1:
            raise ValueError("PAPER_SNIPER_MIN_HOLDERS must be at least 1")
        if not 1 <= self.paper_sniper_max_top_holders_percent <= 100:
            raise ValueError("PAPER_SNIPER_MAX_TOP_HOLDERS_PERCENT must be between 1 and 100")
        if not 0 <= self.paper_sniper_source_penalty_bps <= 10_000:
            raise ValueError("PAPER_SNIPER_SOURCE_PENALTY_BPS must be between 0 and 10000")
        if self.paper_sniper_max_entry_drift_percent < 0:
            raise ValueError("PAPER_SNIPER_MAX_ENTRY_DRIFT_PERCENT cannot be negative")
        if self.paper_sniper_max_quote_price_impact_percent <= 0:
            raise ValueError("PAPER_SNIPER_MAX_QUOTE_PRICE_IMPACT_PERCENT must be positive")
        if self.stop_loss_percent <= 0 or self.take_profit_percent <= 0:
            raise ValueError("Stop-loss and take-profit percentages must be positive")
        if self.max_hold_seconds < 60:
            raise ValueError("MAX_HOLD_SECONDS must be at least 60")
        if self.paper_daily_target_usd <= 0:
            raise ValueError("PAPER_DAILY_TARGET_USD must be positive")
        if self.paper_daily_loss_limit_usd <= 0:
            raise ValueError("PAPER_DAILY_LOSS_LIMIT_USD must be positive")
        try:
            ZoneInfo(self.paper_daily_lock_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("PAPER_DAILY_LOCK_TIMEZONE must be a valid IANA timezone") from exc
        if self.paper_daily_profit_check_seconds < 5:
            raise ValueError("PAPER_DAILY_PROFIT_CHECK_SECONDS must be at least 5")
        if not 0 <= self.paper_quote_output_buffer_bps < 10_000:
            raise ValueError("PAPER_QUOTE_OUTPUT_BUFFER_BPS must be between 0 and 9999")
        if self.max_adverse_entry_drift_percent < 0:
            raise ValueError("MAX_ADVERSE_ENTRY_DRIFT_PERCENT cannot be negative")
        if self.max_quote_price_impact_percent <= 0:
            raise ValueError("MAX_QUOTE_PRICE_IMPACT_PERCENT must be positive")
        if self.max_quote_latency_ms < 100:
            raise ValueError("MAX_QUOTE_LATENCY_MS must be at least 100")
        if self.max_consecutive_quote_failures < 1:
            raise ValueError("MAX_CONSECUTIVE_QUOTE_FAILURES must be at least 1")
        if self.readiness_min_active_days < 1:
            raise ValueError("READINESS_MIN_ACTIVE_DAYS must be at least 1")
        if self.readiness_min_closed_trades < 1:
            raise ValueError("READINESS_MIN_CLOSED_TRADES must be at least 1")
        if self.readiness_min_profit_factor <= 0:
            raise ValueError("READINESS_MIN_PROFIT_FACTOR must be positive")
        if not 0 < self.readiness_max_drawdown_percent <= 100:
            raise ValueError("READINESS_MAX_DRAWDOWN_PERCENT must be between 0 and 100")
        if not 0 <= self.readiness_min_quote_success_percent <= 100:
            raise ValueError("READINESS_MIN_QUOTE_SUCCESS_PERCENT must be between 0 and 100")
        raw_percentages = (
            self.raw_mirror_stop_loss_percent,
            self.raw_mirror_take_profit_percent,
            self.raw_mirror_trailing_activation_percent,
            self.raw_mirror_trailing_stop_percent,
        )
        if any(value <= 0 or value >= 100 for value in raw_percentages):
            raise ValueError("Raw-mirror risk percentages must be between 0 and 100")
        if self.raw_mirror_trailing_stop_percent >= self.raw_mirror_trailing_activation_percent:
            raise ValueError(
                "RAW_MIRROR_TRAILING_STOP_PERCENT must be below "
                "RAW_MIRROR_TRAILING_ACTIVATION_PERCENT"
            )
        if self.raw_mirror_max_hold_seconds < 60:
            raise ValueError("RAW_MIRROR_MAX_HOLD_SECONDS must be at least 60")
