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
    'shutdown OR "supreme court")) OR (("pump.fun" OR "contract address" OR '
    '"CA:") (solana OR memecoin OR token))) lang:en -is:retweet -is:reply'
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


def _address_tuple(name: str, default: str = "") -> tuple[str, ...]:
    """Accept either comma- or pipe-separated public addresses."""

    raw = os.getenv(name, default).replace("|", ",")
    return tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))


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
    x_budget_guard_enabled: bool
    x_estimated_total_budget_usd: Decimal
    x_estimated_daily_budget_usd: Decimal
    x_max_targeted_verifications_per_day: int
    x_verify_max_posts: int
    x_estimated_post_read_usd: Decimal
    x_estimated_user_read_usd: Decimal
    x_budget_period_id: str
    x_user_cache_seconds: int
    x_radar_enabled: bool
    x_radar_query: str
    x_radar_poll_seconds: int
    x_radar_max_contracts_per_scan: int
    fomo_radar_enabled: bool
    fomo_radar_poll_seconds: int
    fomo_radar_max_candidates_per_scan: int
    fomo_radar_recheck_seconds: int
    fomo_runner_enabled: bool
    fomo_runner_fast_watch_seconds: int
    fomo_runner_fast_watch_minutes: int
    fomo_runner_fast_watch_min_score: Decimal
    fomo_runner_public_alert_min_score: Decimal
    fomo_runner_max_fast_watch: int
    fomo_runner_lab_candidates: int
    fomo_runner_max_graduation_age_minutes: int
    fomo_runner_outcome_poll_seconds: int
    fomo_runner_digest_enabled: bool
    fomo_runner_digest_seconds: int
    fomo_runner_digest_min_score: Decimal
    fomo_runner_digest_max_candidates: int
    fomo_runner_fresh_alert_enabled: bool
    fomo_runner_fresh_max_age_seconds: int
    fomo_runner_fresh_watch_enabled: bool
    fomo_runner_fresh_watch_seconds: int
    fomo_runner_fresh_watch_max: int
    fomo_runner_forensics_min_score: Decimal
    # --- v2.35 candidate-qualification group -------------------------------
    # Every one of these has a working default; none needs to be set in
    # Railway for the upgrade to run.  They exist so `/fomo calibration` can
    # drive them from real forward outcomes instead of code edits.
    fomo_runner_min_evidence_families: int
    fomo_runner_min_opportunity_score: Decimal
    fomo_runner_heating_min_opportunity: Decimal
    fomo_runner_heating_min_momentum: Decimal
    fomo_runner_entry_min_opportunity: Decimal
    fomo_runner_entry_min_momentum: Decimal
    fomo_runner_min_independence_ratio: Decimal
    fomo_runner_max_cluster_supply_percent: Decimal
    fomo_runner_fresh_requires_qualification: bool
    fomo_runner_forensics_max_wallets: int
    fomo_runner_funding_max_depth: int
    fomo_runner_wallet_history_limit: int
    fomo_runner_excluded_funders: tuple[str, ...]
    # -----------------------------------------------------------------------
    fomo_runner_invalidation_drawdown_percent: Decimal
    fomo_runner_invalidation_liquidity_decline_percent: Decimal
    fomo_runner_invalidation_liquidity_floor_usd: Decimal

    # --- PAPER research laboratory (v2.36) -------------------------------
    # Safe code defaults cover every value below, so a deployment does not
    # need to define any of these Railway variables to run the lab.
    fomo_lab_engine_enabled: bool
    fomo_lab_auto_paper_enabled: bool
    fomo_lab_bankroll_usd: Decimal
    fomo_lab_position_usd: Decimal
    fomo_lab_max_position_usd: Decimal
    fomo_lab_max_concurrent_positions: int
    fomo_lab_max_total_exposure_usd: Decimal
    fomo_lab_daily_loss_cap_usd: Decimal
    fomo_lab_min_liquidity_usd: Decimal
    fomo_lab_max_price_impact_percent: Decimal
    fomo_lab_max_slippage_percent: Decimal
    fomo_lab_min_net_edge_percent: Decimal
    fomo_lab_platform_fee_bps: int
    fomo_lab_slippage_bps: int
    fomo_lab_priority_fee_usd: Decimal
    fomo_lab_network_fee_usd: Decimal
    fomo_lab_cooldown_seconds: int
    fomo_lab_min_forward_sample: int
    fomo_social_radar_enabled: bool
    fomo_social_posts_per_account: int
    fomo_social_daily_request_budget: int

    # --- discovery speed / current actionability (v2.37) -----------------
    fomo_discovery_source_name: str
    fomo_fast_watch_enabled: bool
    fomo_fast_watch_min_actionability: Decimal
    fomo_current_radar_suppress_stale: bool

    # --- realtime alpha engine (v2.38) -----------------------------------
    fomo_fast_watch_publish_enabled: bool
    fomo_fast_watch_min_score: Decimal
    fomo_fast_watch_max_queue_age_seconds: int
    fomo_fast_watch_cooldown_seconds: int
    fomo_fast_watch_max_per_hour: int
    fomo_notable_alerts_enabled: bool
    fomo_notable_min_trade_usd: Decimal
    fomo_notable_ping_enabled: bool
    fomo_notable_max_signal_age_seconds: int
    fomo_catalyst_alerts_enabled: bool
    fomo_catalyst_max_event_age_seconds: int
    fomo_catalyst_ping_enabled: bool
    fomo_confluence_alerts_enabled: bool
    fomo_alert_enrichment_enabled: bool
    fomo_alert_enrichment_delay_seconds: int

    # --- SHADOW auto-trader (v2.39) --------------------------------------
    # Safe code defaults cover every value below, so a deployment does not need
    # to define any of these Railway variables to run the shadow experiment.
    fomo_runner_analysis_budget_seconds: int
    fomo_early_lane_enabled: bool
    fomo_early_heads_up_ping: bool
    fomo_early_min_liquidity_usd: Decimal
    fomo_early_max_age_seconds: int
    fomo_early_runner_min_score: Decimal
    fomo_early_max_runners_per_hour: int
    fomo_early_cooldown_seconds: int
    fomo_forward_ping_gate_enabled: bool
    fomo_shadow_auto_enabled: bool
    fomo_shadow_publish_cards: bool
    fomo_shadow_bankroll_usd: Decimal
    fomo_shadow_position_usd: Decimal
    fomo_shadow_max_position_usd: Decimal
    fomo_shadow_max_positions: int
    fomo_shadow_max_exposure_usd: Decimal
    fomo_shadow_net_profit_objective_usd: Decimal
    fomo_shadow_daily_loss_cap_usd: Decimal
    fomo_shadow_max_price_impact_percent: Decimal
    fomo_shadow_max_signal_age_seconds: int
    fomo_shadow_max_fill_latency_ms: int
    fomo_shadow_allow_fallback_fill: bool
    fomo_shadow_min_forward_sample: int
    fomo_live_radar_channel_id: int | None
    fomo_urgent_channel_id: int | None

    # --- Trending-first alpha engine (v2.42) ------------------------------
    # Every value has a safe code default.  A deployment that sets none of these
    # runs the Trending lane against the public proxy source, with the legacy
    # graduated lane still active as the secondary universe.
    fomo_trending_primary_enabled: bool
    fomo_graduated_secondary_enabled: bool
    #: An administrator-supplied, authorised Fomo Trending feed.  Empty means the
    #: bot has no exact Trending access and must label its data TRENDING_PROXY.
    fomo_trending_api_url: str | None
    fomo_trending_api_key: str | None
    #: What window the authorised feed's displayed percentage covers.  Left blank
    #: the bot records CHANGE_WINDOW_UNKNOWN rather than guessing.
    fomo_trending_change_window: str
    fomo_trending_proxy_enabled: bool
    fomo_trending_poll_seconds: int
    fomo_trending_max_tracked: int
    fomo_trending_alpha_min_score: Decimal
    fomo_trending_watch_min_score: Decimal
    fomo_trending_max_alerts_per_hour: int
    fomo_trending_cooldown_seconds: int
    fomo_trending_hot_watch_enabled: bool
    fomo_trending_hot_watch_seconds: int
    fomo_trending_hot_watch_recheck_seconds: int
    fomo_trending_hot_watch_max: int
    fomo_trending_hot_watch_band: Decimal
    fomo_trending_social_enrich_enabled: bool
    fomo_trending_shadow_enabled: bool
    fomo_trending_off_board_exception_enabled: bool
    fomo_trending_stale_snapshot_seconds: int

    # --- Terminal-style trenches intelligence (v2.43) ----------------------
    # Every value has a safe code default.  The whole engine runs on public
    # Solana RPC and Pump.fun program state, so no paid provider is required.
    fomo_trenches_enabled: bool
    fomo_trenches_poll_seconds: int
    fomo_trenches_max_tracked: int
    fomo_trenches_runner_min_score: Decimal
    fomo_trenches_heads_up_min_score: Decimal
    fomo_trenches_max_alerts_per_hour: int
    fomo_trenches_cooldown_seconds: int
    #: Realtime Pump.fun token-creation detection from public program logs.
    fomo_pump_creation_stream_enabled: bool
    #: Per-candidate on-chain enrichment budget, so a busy board cannot become a
    #: thousand RPC calls.
    fomo_trenches_max_enrichment_per_scan: int
    fomo_trenches_wallet_lookups_per_token: int
    fomo_trenches_holder_reads_per_scan: int
    #: Our own public Trending model (never labelled as anyone else's rank).
    fomo_public_trending_enabled: bool
    fomo_public_trending_min_score: Decimal
    #: Cadence tiers for rapid rechecks.
    fomo_trenches_hot_recheck_seconds: int
    fomo_trenches_warm_recheck_seconds: int
    fomo_trenches_normal_recheck_seconds: int
    fomo_trenches_max_hot: int
    fomo_trenches_max_warm: int
    #: Attribution-only Trench cohort inside the Trending shadow book, or its own
    #: bankroll when clean separation is explicitly wanted.
    fomo_trench_shadow_separate_bankroll: bool

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
            coin_x_prefilter_min_score=_decimal("COIN_X_PREFILTER_MIN_SCORE", "60"),
            coin_watch_alerts_enabled=_bool("COIN_WATCH_ALERTS_ENABLED", True),
            coin_watch_min_score=_decimal("COIN_WATCH_MIN_SCORE", "55"),
            fomo_watch_min_score=_decimal("FOMO_WATCH_MIN_SCORE", "50"),
            trade_activity_alerts_enabled=_bool("TRADE_ACTIVITY_ALERTS_ENABLED", False),
            x_search_max_results=_int("X_SEARCH_MAX_RESULTS", 10),
            x_daily_search_limit=_int("X_DAILY_SEARCH_LIMIT", 10),
            x_daily_search_timezone=os.getenv(
                "X_DAILY_SEARCH_TIMEZONE", "America/Los_Angeles"
            ).strip(),
            x_paid_search_enabled=_bool("X_PAID_SEARCH_ENABLED", False),
            x_budget_guard_enabled=_bool("X_BUDGET_GUARD_ENABLED", True),
            x_estimated_total_budget_usd=_decimal("X_ESTIMATED_TOTAL_BUDGET_USD", "10"),
            x_estimated_daily_budget_usd=_decimal("X_ESTIMATED_DAILY_BUDGET_USD", "0.50"),
            x_max_targeted_verifications_per_day=_int("X_MAX_TARGETED_VERIFICATIONS_PER_DAY", 10),
            x_verify_max_posts=_int("X_VERIFY_MAX_POSTS", 10),
            x_estimated_post_read_usd=_decimal("X_ESTIMATED_POST_READ_USD", "0.005"),
            x_estimated_user_read_usd=_decimal("X_ESTIMATED_USER_READ_USD", "0.010"),
            x_budget_period_id=os.getenv("X_BUDGET_PERIOD_ID", "experiment-1").strip(),
            x_user_cache_seconds=_int("X_USER_CACHE_SECONDS", 86400),
            x_radar_enabled=_bool("X_RADAR_ENABLED", False),
            x_radar_query=os.getenv("X_RADAR_QUERY", DEFAULT_X_RADAR_QUERY).strip(),
            x_radar_poll_seconds=_int("X_RADAR_POLL_SECONDS", 1800),
            x_radar_max_contracts_per_scan=_int("X_RADAR_MAX_CONTRACTS_PER_SCAN", 3),
            fomo_radar_enabled=_bool("FOMO_RADAR_ENABLED", True),
            fomo_radar_poll_seconds=_int("FOMO_RADAR_POLL_SECONDS", 60),
            fomo_radar_max_candidates_per_scan=_int("FOMO_RADAR_MAX_CANDIDATES_PER_SCAN", 12),
            fomo_radar_recheck_seconds=_int("FOMO_RADAR_RECHECK_SECONDS", 1800),
            fomo_runner_enabled=_bool("FOMO_RUNNER_ENABLED", True),
            fomo_runner_fast_watch_seconds=_int("FOMO_RUNNER_FAST_WATCH_SECONDS", 15),
            fomo_runner_fast_watch_minutes=_int("FOMO_RUNNER_FAST_WATCH_MINUTES", 15),
            fomo_runner_fast_watch_min_score=_decimal("FOMO_RUNNER_FAST_WATCH_MIN_SCORE", "20"),
            fomo_runner_public_alert_min_score=_decimal("FOMO_RUNNER_PUBLIC_ALERT_MIN_SCORE", "70"),
            fomo_runner_max_fast_watch=_int("FOMO_RUNNER_MAX_FAST_WATCH", 12),
            fomo_runner_lab_candidates=_int("FOMO_RUNNER_LAB_CANDIDATES", 6),
            fomo_runner_max_graduation_age_minutes=_int(
                "FOMO_RUNNER_MAX_GRADUATION_AGE_MINUTES", 60
            ),
            fomo_runner_outcome_poll_seconds=_int("FOMO_RUNNER_OUTCOME_POLL_SECONDS", 60),
            fomo_runner_digest_enabled=_bool("FOMO_RUNNER_DIGEST_ENABLED", True),
            fomo_runner_digest_seconds=_int("FOMO_RUNNER_DIGEST_SECONDS", 180),
            fomo_runner_digest_min_score=_decimal("FOMO_RUNNER_DIGEST_MIN_SCORE", "15"),
            fomo_runner_digest_max_candidates=_int(
                "FOMO_RUNNER_DIGEST_MAX_CANDIDATES", 10
            ),
            fomo_runner_fresh_alert_enabled=_bool("FOMO_RUNNER_FRESH_ALERT_ENABLED", True),
            fomo_runner_fresh_max_age_seconds=_int(
                "FOMO_RUNNER_FRESH_MAX_AGE_SECONDS", 300
            ),
            fomo_runner_fresh_watch_enabled=_bool("FOMO_RUNNER_FRESH_WATCH_ENABLED", True),
            fomo_runner_fresh_watch_seconds=_int("FOMO_RUNNER_FRESH_WATCH_SECONDS", 15),
            fomo_runner_fresh_watch_max=_int("FOMO_RUNNER_FRESH_WATCH_MAX", 15),
            fomo_runner_min_evidence_families=_int(
                "FOMO_RUNNER_MIN_EVIDENCE_FAMILIES", 2
            ),
            fomo_runner_min_opportunity_score=_decimal(
                "FOMO_RUNNER_MIN_OPPORTUNITY_SCORE", "45"
            ),
            fomo_runner_heating_min_opportunity=_decimal(
                "FOMO_RUNNER_HEATING_MIN_OPPORTUNITY", "55"
            ),
            fomo_runner_heating_min_momentum=_decimal(
                "FOMO_RUNNER_HEATING_MIN_MOMENTUM", "60"
            ),
            fomo_runner_entry_min_opportunity=_decimal(
                "FOMO_RUNNER_ENTRY_MIN_OPPORTUNITY", "65"
            ),
            fomo_runner_entry_min_momentum=_decimal(
                "FOMO_RUNNER_ENTRY_MIN_MOMENTUM", "50"
            ),
            fomo_runner_min_independence_ratio=_decimal(
                "FOMO_RUNNER_MIN_INDEPENDENCE_RATIO", "0.45"
            ),
            fomo_runner_max_cluster_supply_percent=_decimal(
                "FOMO_RUNNER_MAX_CLUSTER_SUPPLY_PERCENT", "25"
            ),
            fomo_runner_fresh_requires_qualification=_bool(
                "FOMO_RUNNER_FRESH_REQUIRES_QUALIFICATION", True
            ),
            fomo_runner_forensics_max_wallets=_int(
                "FOMO_RUNNER_FORENSICS_MAX_WALLETS", 14
            ),
            fomo_runner_funding_max_depth=_int("FOMO_RUNNER_FUNDING_MAX_DEPTH", 2),
            fomo_runner_wallet_history_limit=_int(
                "FOMO_RUNNER_WALLET_HISTORY_LIMIT", 60
            ),
            fomo_runner_excluded_funders=_address_tuple("FOMO_RUNNER_EXCLUDED_FUNDERS"),
            fomo_runner_forensics_min_score=_decimal(
                "FOMO_RUNNER_FORENSICS_MIN_SCORE", "50"
            ),
            fomo_runner_invalidation_drawdown_percent=_decimal(
                "FOMO_RUNNER_INVALIDATION_DRAWDOWN_PERCENT", "50"
            ),
            fomo_runner_invalidation_liquidity_decline_percent=_decimal(
                "FOMO_RUNNER_INVALIDATION_LIQUIDITY_DECLINE_PERCENT", "35"
            ),
            fomo_runner_invalidation_liquidity_floor_usd=_decimal(
                "FOMO_RUNNER_INVALIDATION_LIQUIDITY_FLOOR_USD", "500"
            ),
            fomo_lab_engine_enabled=_bool("FOMO_LAB_ENGINE_ENABLED", True),
            fomo_lab_auto_paper_enabled=_bool("FOMO_LAB_AUTO_PAPER_ENABLED", True),
            fomo_lab_bankroll_usd=_decimal("FOMO_LAB_BANKROLL_USD", "100"),
            fomo_lab_position_usd=_decimal("FOMO_LAB_POSITION_USD", "5"),
            fomo_lab_max_position_usd=_decimal("FOMO_LAB_MAX_POSITION_USD", "10"),
            fomo_lab_max_concurrent_positions=_int("FOMO_LAB_MAX_CONCURRENT_POSITIONS", 5),
            fomo_lab_max_total_exposure_usd=_decimal("FOMO_LAB_MAX_TOTAL_EXPOSURE_USD", "30"),
            fomo_lab_daily_loss_cap_usd=_decimal("FOMO_LAB_DAILY_LOSS_CAP_USD", "15"),
            fomo_lab_min_liquidity_usd=_decimal("FOMO_LAB_MIN_LIQUIDITY_USD", "15000"),
            fomo_lab_max_price_impact_percent=_decimal(
                "FOMO_LAB_MAX_PRICE_IMPACT_PERCENT", "2.5"
            ),
            fomo_lab_max_slippage_percent=_decimal("FOMO_LAB_MAX_SLIPPAGE_PERCENT", "2.5"),
            fomo_lab_min_net_edge_percent=_decimal("FOMO_LAB_MIN_NET_EDGE_PERCENT", "12"),
            fomo_lab_platform_fee_bps=_int("FOMO_LAB_PLATFORM_FEE_BPS", 100),
            fomo_lab_slippage_bps=_int("FOMO_LAB_SLIPPAGE_BPS", 80),
            fomo_lab_priority_fee_usd=_decimal("FOMO_LAB_PRIORITY_FEE_USD", "0.02"),
            fomo_lab_network_fee_usd=_decimal("FOMO_LAB_NETWORK_FEE_USD", "0.0008"),
            fomo_lab_cooldown_seconds=_int("FOMO_LAB_COOLDOWN_SECONDS", 3600),
            fomo_lab_min_forward_sample=_int("FOMO_LAB_MIN_FORWARD_SAMPLE", 30),
            fomo_social_radar_enabled=_bool("FOMO_SOCIAL_RADAR_ENABLED", False),
            fomo_social_posts_per_account=_int("FOMO_SOCIAL_POSTS_PER_ACCOUNT", 10),
            fomo_social_daily_request_budget=_int("FOMO_SOCIAL_DAILY_REQUEST_BUDGET", 40),
            fomo_discovery_source_name=(
                os.getenv("FOMO_DISCOVERY_SOURCE_NAME", "dexscreener_trending").strip()
                or "dexscreener_trending"
            ),
            fomo_fast_watch_enabled=_bool("FOMO_FAST_WATCH_ENABLED", True),
            fomo_fast_watch_min_actionability=_decimal(
                "FOMO_FAST_WATCH_MIN_ACTIONABILITY", "55"
            ),
            fomo_current_radar_suppress_stale=_bool(
                "FOMO_CURRENT_RADAR_SUPPRESS_STALE", True
            ),
            fomo_fast_watch_publish_enabled=_bool("FOMO_FAST_WATCH_PUBLISH_ENABLED", True),
            fomo_fast_watch_min_score=_decimal("FOMO_FAST_WATCH_MIN_SCORE", "55"),
            fomo_fast_watch_max_queue_age_seconds=_int(
                "FOMO_FAST_WATCH_MAX_QUEUE_AGE_SECONDS", 300
            ),
            fomo_fast_watch_cooldown_seconds=_int("FOMO_FAST_WATCH_COOLDOWN_SECONDS", 1800),
            fomo_fast_watch_max_per_hour=_int("FOMO_FAST_WATCH_MAX_PER_HOUR", 12),
            fomo_notable_alerts_enabled=_bool("FOMO_NOTABLE_ALERTS_ENABLED", True),
            fomo_notable_min_trade_usd=_decimal("FOMO_NOTABLE_MIN_TRADE_USD", "250"),
            fomo_notable_ping_enabled=_bool("FOMO_NOTABLE_PING_ENABLED", False),
            fomo_notable_max_signal_age_seconds=_int(
                "FOMO_NOTABLE_MAX_SIGNAL_AGE_SECONDS", 900
            ),
            fomo_catalyst_alerts_enabled=_bool("FOMO_CATALYST_ALERTS_ENABLED", True),
            fomo_catalyst_max_event_age_seconds=_int(
                "FOMO_CATALYST_MAX_EVENT_AGE_SECONDS", 3600
            ),
            fomo_catalyst_ping_enabled=_bool("FOMO_CATALYST_PING_ENABLED", False),
            fomo_confluence_alerts_enabled=_bool("FOMO_CONFLUENCE_ALERTS_ENABLED", True),
            fomo_alert_enrichment_enabled=_bool("FOMO_ALERT_ENRICHMENT_ENABLED", True),
            fomo_alert_enrichment_delay_seconds=_int(
                "FOMO_ALERT_ENRICHMENT_DELAY_SECONDS", 45
            ),
            fomo_runner_analysis_budget_seconds=_int(
                "FOMO_RUNNER_ANALYSIS_BUDGET_SECONDS", 30
            ),
            fomo_forward_ping_gate_enabled=_bool("FOMO_FORWARD_PING_GATE_ENABLED", True),
            fomo_early_lane_enabled=_bool("FOMO_EARLY_LANE_ENABLED", True),
            fomo_early_heads_up_ping=_bool("FOMO_EARLY_HEADS_UP_PING", False),
            fomo_early_min_liquidity_usd=_decimal("FOMO_EARLY_MIN_LIQUIDITY_USD", "4000"),
            fomo_early_max_age_seconds=_int("FOMO_EARLY_MAX_AGE_SECONDS", 3600),
            fomo_early_runner_min_score=_decimal("FOMO_EARLY_RUNNER_MIN_SCORE", "55"),
            fomo_early_max_runners_per_hour=_int("FOMO_EARLY_MAX_RUNNERS_PER_HOUR", 12),
            fomo_early_cooldown_seconds=_int("FOMO_EARLY_COOLDOWN_SECONDS", 1800),
            fomo_shadow_auto_enabled=_bool("FOMO_SHADOW_AUTO_ENABLED", True),
            fomo_shadow_publish_cards=_bool("FOMO_SHADOW_PUBLISH_CARDS", True),
            fomo_shadow_bankroll_usd=_decimal("FOMO_SHADOW_BANKROLL_USD", "100"),
            fomo_shadow_position_usd=_decimal("FOMO_SHADOW_POSITION_USD", "10"),
            fomo_shadow_max_position_usd=_decimal("FOMO_SHADOW_MAX_POSITION_USD", "10"),
            fomo_shadow_max_positions=_int("FOMO_SHADOW_MAX_POSITIONS", 5),
            fomo_shadow_max_exposure_usd=_decimal("FOMO_SHADOW_MAX_EXPOSURE_USD", "50"),
            fomo_shadow_net_profit_objective_usd=_decimal(
                "FOMO_SHADOW_NET_PROFIT_OBJECTIVE_USD", "2"
            ),
            fomo_shadow_daily_loss_cap_usd=_decimal("FOMO_SHADOW_DAILY_LOSS_CAP_USD", "15"),
            fomo_shadow_max_price_impact_percent=_decimal(
                "FOMO_SHADOW_MAX_PRICE_IMPACT_PERCENT", "12"
            ),
            fomo_shadow_max_signal_age_seconds=_int("FOMO_SHADOW_MAX_SIGNAL_AGE_SECONDS", 900),
            fomo_shadow_max_fill_latency_ms=_int("FOMO_SHADOW_MAX_FILL_LATENCY_MS", 30_000),
            fomo_shadow_allow_fallback_fill=_bool("FOMO_SHADOW_ALLOW_FALLBACK_FILL", True),
            fomo_shadow_min_forward_sample=_int("FOMO_SHADOW_MIN_FORWARD_SAMPLE", 30),
            fomo_trending_primary_enabled=_bool("FOMO_TRENDING_PRIMARY_ENABLED", True),
            fomo_graduated_secondary_enabled=_bool("FOMO_GRADUATED_SECONDARY_ENABLED", True),
            fomo_trending_api_url=(os.getenv("FOMO_TRENDING_API_URL", "").strip() or None),
            fomo_trending_api_key=(os.getenv("FOMO_TRENDING_API_KEY", "").strip() or None),
            fomo_trending_change_window=os.getenv("FOMO_TRENDING_CHANGE_WINDOW", "").strip(),
            fomo_trending_proxy_enabled=_bool("FOMO_TRENDING_PROXY_ENABLED", True),
            # 45s is the fastest cadence the public proxy's documented endpoints
            # tolerate comfortably alongside the existing radar; an authorised
            # feed may permit faster, which is why it is configurable rather
            # than hard-coded.
            fomo_trending_poll_seconds=_int("FOMO_TRENDING_POLL_SECONDS", 45),
            fomo_trending_max_tracked=_int("FOMO_TRENDING_MAX_TRACKED", 60),
            fomo_trending_alpha_min_score=_decimal("FOMO_TRENDING_ALPHA_MIN_SCORE", "62"),
            fomo_trending_watch_min_score=_decimal("FOMO_TRENDING_WATCH_MIN_SCORE", "40"),
            fomo_trending_max_alerts_per_hour=_int("FOMO_TRENDING_MAX_ALERTS_PER_HOUR", 10),
            fomo_trending_cooldown_seconds=_int("FOMO_TRENDING_COOLDOWN_SECONDS", 1800),
            fomo_trending_hot_watch_enabled=_bool("FOMO_TRENDING_HOT_WATCH_ENABLED", True),
            fomo_trending_hot_watch_seconds=_int("FOMO_TRENDING_HOT_WATCH_SECONDS", 900),
            fomo_trending_hot_watch_recheck_seconds=_int(
                "FOMO_TRENDING_HOT_WATCH_RECHECK_SECONDS", 45
            ),
            fomo_trending_hot_watch_max=_int("FOMO_TRENDING_HOT_WATCH_MAX", 12),
            fomo_trending_hot_watch_band=_decimal("FOMO_TRENDING_HOT_WATCH_BAND", "12"),
            fomo_trending_social_enrich_enabled=_bool(
                "FOMO_TRENDING_SOCIAL_ENRICH_ENABLED", True
            ),
            fomo_trending_shadow_enabled=_bool("FOMO_TRENDING_SHADOW_ENABLED", True),
            fomo_trending_off_board_exception_enabled=_bool(
                "FOMO_TRENDING_OFF_BOARD_EXCEPTION_ENABLED", True
            ),
            fomo_trending_stale_snapshot_seconds=_int(
                "FOMO_TRENDING_STALE_SNAPSHOT_SECONDS", 600
            ),
            fomo_trenches_enabled=_bool("FOMO_TRENCHES_ENABLED", True),
            # 30s is comfortable against a public RPC once curve reads are
            # batched 100 per request; the creation stream is what actually
            # provides speed, so this poll is the safety net rather than the
            # discovery path.
            fomo_trenches_poll_seconds=_int("FOMO_TRENCHES_POLL_SECONDS", 30),
            fomo_trenches_max_tracked=_int("FOMO_TRENCHES_MAX_TRACKED", 80),
            fomo_trenches_runner_min_score=_decimal("FOMO_TRENCHES_RUNNER_MIN_SCORE", "62"),
            fomo_trenches_heads_up_min_score=_decimal(
                "FOMO_TRENCHES_HEADS_UP_MIN_SCORE", "38"
            ),
            fomo_trenches_max_alerts_per_hour=_int("FOMO_TRENCHES_MAX_ALERTS_PER_HOUR", 8),
            fomo_trenches_cooldown_seconds=_int("FOMO_TRENCHES_COOLDOWN_SECONDS", 1800),
            fomo_pump_creation_stream_enabled=_bool(
                "FOMO_PUMP_CREATION_STREAM_ENABLED", True
            ),
            fomo_trenches_max_enrichment_per_scan=_int(
                "FOMO_TRENCHES_MAX_ENRICHMENT_PER_SCAN", 12
            ),
            fomo_trenches_wallet_lookups_per_token=_int(
                "FOMO_TRENCHES_WALLET_LOOKUPS_PER_TOKEN", 25
            ),
            fomo_trenches_holder_reads_per_scan=_int(
                "FOMO_TRENCHES_HOLDER_READS_PER_SCAN", 10
            ),
            fomo_public_trending_enabled=_bool("FOMO_PUBLIC_TRENDING_ENABLED", True),
            fomo_public_trending_min_score=_decimal("FOMO_PUBLIC_TRENDING_MIN_SCORE", "10"),
            fomo_trenches_hot_recheck_seconds=_int("FOMO_TRENCHES_HOT_RECHECK_SECONDS", 15),
            fomo_trenches_warm_recheck_seconds=_int("FOMO_TRENCHES_WARM_RECHECK_SECONDS", 45),
            fomo_trenches_normal_recheck_seconds=_int(
                "FOMO_TRENCHES_NORMAL_RECHECK_SECONDS", 120
            ),
            fomo_trenches_max_hot=_int("FOMO_TRENCHES_MAX_HOT", 6),
            fomo_trenches_max_warm=_int("FOMO_TRENCHES_MAX_WARM", 16),
            fomo_trench_shadow_separate_bankroll=_bool(
                "FOMO_TRENCH_SHADOW_SEPARATE_BANKROLL", False
            ),
            fomo_live_radar_channel_id=_optional_int("FOMO_LIVE_RADAR_CHANNEL_ID"),
            fomo_urgent_channel_id=_optional_int("FOMO_URGENT_CHANNEL_ID"),
            news_radar_enabled=_bool("NEWS_RADAR_ENABLED", True),
            x_news_stream_enabled=_bool("X_NEWS_STREAM_ENABLED", False),
            x_news_stream_rule=os.getenv("X_NEWS_STREAM_RULE", DEFAULT_X_NEWS_RULE).strip(),
            news_rss_feeds=_str_tuple("NEWS_RSS_FEEDS", DEFAULT_NEWS_RSS_FEEDS),
            j7_authorized_feed_url=(os.getenv("J7_AUTHORIZED_FEED_URL", "").strip() or None),
            news_poll_seconds=_int("NEWS_POLL_SECONDS", 30),
            news_min_score=_int("NEWS_MIN_SCORE", 45),
            news_launch_ready_score=_int("NEWS_LAUNCH_READY_SCORE", 72),
            no_x_launch_candidates_enabled=_bool("NO_X_LAUNCH_CANDIDATES_ENABLED", True),
            no_x_launch_min_score=_int("NO_X_LAUNCH_MIN_SCORE", 78),
            news_x_verify_min_score=_int("NEWS_X_VERIFY_MIN_SCORE", 70),
            news_x_trend_cache_seconds=_int("NEWS_X_TREND_CACHE_SECONDS", 3600),
            news_max_alerts_per_hour=_int("NEWS_MAX_ALERTS_PER_HOUR", 30),
            news_source_image_enabled=_bool("NEWS_SOURCE_IMAGE_ENABLED", True),
            news_dex_match_enabled=_bool("NEWS_DEX_MATCH_ENABLED", True),
            news_dex_match_min_liquidity_usd=_decimal("NEWS_DEX_MATCH_MIN_LIQUIDITY_USD", "2000"),
            news_dex_match_max_age_minutes=_int("NEWS_DEX_MATCH_MAX_AGE_MINUTES", 60),
            news_pair_recheck_seconds=_int_tuple("NEWS_PAIR_RECHECK_SECONDS", "0,30,90,180"),
            pump_one_click_launch_enabled=_bool("PUMP_ONE_CLICK_LAUNCH_ENABLED", False),
            pump_launch_ack=os.getenv("PUMP_LAUNCH_ACK", "").strip(),
            pump_launch_private_key=(os.getenv("PUMP_LAUNCH_PRIVATE_KEY", "").strip() or None),
            pinata_jwt=os.getenv("PINATA_JWT", "").strip() or None,
            pump_launch_initial_buy_sol=_decimal("PUMP_LAUNCH_INITIAL_BUY_SOL", "0.01"),
            pump_launch_min_score=_int("PUMP_LAUNCH_MIN_SCORE", 72),
            pump_launch_max_per_day=_int("PUMP_LAUNCH_MAX_PER_DAY", 3),
            pump_launch_max_sol_per_day=_decimal("PUMP_LAUNCH_MAX_SOL_PER_DAY", "0.05"),
            pump_launch_timezone=os.getenv("PUMP_LAUNCH_TIMEZONE", "America/Los_Angeles").strip(),
            pump_launch_cashback=_bool("PUMP_LAUNCH_CASHBACK", False),
            pump_launch_mayhem_mode=_bool("PUMP_LAUNCH_MAYHEM_MODE", False),
            pump_launch_tokenized_agent=_bool("PUMP_LAUNCH_TOKENIZED_AGENT", False),
            pump_launch_buyback_bps=_int("PUMP_LAUNCH_BUYBACK_BPS", 5000),
            j7_launch_enabled=_bool("J7_LAUNCH_ENABLED", False),
            j7_launch_session_token=(os.getenv("J7_LAUNCH_SESSION_TOKEN", "").strip() or None),
            j7_launch_api_key=os.getenv("J7_LAUNCH_API_KEY", "").strip() or None,
            j7_launch_region=os.getenv("J7_LAUNCH_REGION", "na-east").strip().lower(),
            j7_launch_wallet_address=(os.getenv("J7_LAUNCH_WALLET_ADDRESS", "").strip() or None),
            j7_launch_min_balance_buffer_sol=_decimal("J7_LAUNCH_MIN_BALANCE_BUFFER_SOL", "0.002"),
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
        if self.x_estimated_total_budget_usd <= 0:
            raise ValueError("X_ESTIMATED_TOTAL_BUDGET_USD must be greater than zero")
        if not Decimal("0.01") <= self.x_estimated_daily_budget_usd:
            raise ValueError("X_ESTIMATED_DAILY_BUDGET_USD must be at least 0.01")
        if self.x_estimated_daily_budget_usd > self.x_estimated_total_budget_usd:
            raise ValueError("X_ESTIMATED_DAILY_BUDGET_USD cannot exceed the total X budget")
        if not 1 <= self.x_max_targeted_verifications_per_day <= 100:
            raise ValueError("X_MAX_TARGETED_VERIFICATIONS_PER_DAY must be between 1 and 100")
        if not 10 <= self.x_verify_max_posts <= 100:
            raise ValueError("X_VERIFY_MAX_POSTS must be between 10 and 100")
        if self.x_estimated_post_read_usd <= 0 or self.x_estimated_user_read_usd <= 0:
            raise ValueError("configured X resource prices must be greater than zero")
        if not self.x_budget_period_id or len(self.x_budget_period_id) > 80:
            raise ValueError("X_BUDGET_PERIOD_ID must contain 1 to 80 characters")
        if not 300 <= self.x_user_cache_seconds <= 604800:
            raise ValueError("X_USER_CACHE_SECONDS must be between 300 and 604800")
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
        if not 15 <= self.fomo_runner_fast_watch_seconds <= 300:
            raise ValueError("FOMO_RUNNER_FAST_WATCH_SECONDS must be between 15 and 300")
        if not 5 <= self.fomo_runner_fast_watch_minutes <= 60:
            raise ValueError("FOMO_RUNNER_FAST_WATCH_MINUTES must be between 5 and 60")
        if not 0 <= self.fomo_runner_fast_watch_min_score <= 100:
            raise ValueError("FOMO_RUNNER_FAST_WATCH_MIN_SCORE must be between 0 and 100")
        if not 0 <= self.fomo_runner_public_alert_min_score <= 100:
            raise ValueError("FOMO_RUNNER_PUBLIC_ALERT_MIN_SCORE must be between 0 and 100")
        if not 1 <= self.fomo_runner_max_fast_watch <= 20:
            raise ValueError("FOMO_RUNNER_MAX_FAST_WATCH must be between 1 and 20")
        if not 1 <= self.fomo_runner_lab_candidates <= 10:
            raise ValueError("FOMO_RUNNER_LAB_CANDIDATES must be between 1 and 10")
        if not 5 <= self.fomo_runner_max_graduation_age_minutes <= 1440:
            raise ValueError("FOMO_RUNNER_MAX_GRADUATION_AGE_MINUTES must be between 5 and 1440")
        if not 30 <= self.fomo_runner_outcome_poll_seconds <= 3600:
            raise ValueError("FOMO_RUNNER_OUTCOME_POLL_SECONDS must be between 30 and 3600")
        if not 60 <= self.fomo_runner_digest_seconds <= 86400:
            raise ValueError("FOMO_RUNNER_DIGEST_SECONDS must be between 60 and 86400")
        if not 0 <= self.fomo_runner_digest_min_score <= 100:
            raise ValueError("FOMO_RUNNER_DIGEST_MIN_SCORE must be between 0 and 100")
        if not 1 <= self.fomo_runner_digest_max_candidates <= 10:
            raise ValueError("FOMO_RUNNER_DIGEST_MAX_CANDIDATES must be between 1 and 10")
        if not 30 <= self.fomo_runner_fresh_max_age_seconds <= 900:
            raise ValueError("FOMO_RUNNER_FRESH_MAX_AGE_SECONDS must be between 30 and 900")
        if not 15 <= self.fomo_runner_fresh_watch_seconds <= 300:
            raise ValueError("FOMO_RUNNER_FRESH_WATCH_SECONDS must be between 15 and 300")
        if not 1 <= self.fomo_runner_fresh_watch_max <= 20:
            raise ValueError("FOMO_RUNNER_FRESH_WATCH_MAX must be between 1 and 20")
        if not 0 <= self.fomo_runner_forensics_min_score <= 100:
            raise ValueError("FOMO_RUNNER_FORENSICS_MIN_SCORE must be between 0 and 100")
        if not 1 <= self.fomo_runner_min_evidence_families <= 6:
            raise ValueError("FOMO_RUNNER_MIN_EVIDENCE_FAMILIES must be between 1 and 6")
        for name, value in (
            ("FOMO_RUNNER_MIN_OPPORTUNITY_SCORE", self.fomo_runner_min_opportunity_score),
            ("FOMO_RUNNER_HEATING_MIN_OPPORTUNITY", self.fomo_runner_heating_min_opportunity),
            ("FOMO_RUNNER_HEATING_MIN_MOMENTUM", self.fomo_runner_heating_min_momentum),
            ("FOMO_RUNNER_ENTRY_MIN_OPPORTUNITY", self.fomo_runner_entry_min_opportunity),
            ("FOMO_RUNNER_ENTRY_MIN_MOMENTUM", self.fomo_runner_entry_min_momentum),
            (
                "FOMO_RUNNER_MAX_CLUSTER_SUPPLY_PERCENT",
                self.fomo_runner_max_cluster_supply_percent,
            ),
        ):
            if not 0 <= value <= 100:
                raise ValueError(f"{name} must be between 0 and 100")
        if not 0 <= self.fomo_runner_min_independence_ratio <= 1:
            raise ValueError("FOMO_RUNNER_MIN_INDEPENDENCE_RATIO must be between 0 and 1")
        if not 4 <= self.fomo_runner_forensics_max_wallets <= 40:
            raise ValueError("FOMO_RUNNER_FORENSICS_MAX_WALLETS must be between 4 and 40")
        if not 1 <= self.fomo_runner_funding_max_depth <= 3:
            raise ValueError("FOMO_RUNNER_FUNDING_MAX_DEPTH must be between 1 and 3")
        if not 10 <= self.fomo_runner_wallet_history_limit <= 1000:
            raise ValueError("FOMO_RUNNER_WALLET_HISTORY_LIMIT must be between 10 and 1000")
        if not 1 <= self.fomo_runner_invalidation_drawdown_percent <= 100:
            raise ValueError(
                "FOMO_RUNNER_INVALIDATION_DRAWDOWN_PERCENT must be between 1 and 100"
            )
        if not 1 <= self.fomo_runner_invalidation_liquidity_decline_percent <= 100:
            raise ValueError(
                "FOMO_RUNNER_INVALIDATION_LIQUIDITY_DECLINE_PERCENT must be between 1 and 100"
            )
        if self.fomo_runner_invalidation_liquidity_floor_usd < 0:
            raise ValueError("FOMO_RUNNER_INVALIDATION_LIQUIDITY_FLOOR_USD cannot be negative")
        if self.fomo_lab_bankroll_usd <= 0:
            raise ValueError("FOMO_LAB_BANKROLL_USD must be positive")
        if not 0 < self.fomo_lab_position_usd <= self.fomo_lab_max_position_usd:
            raise ValueError("FOMO_LAB_POSITION_USD must be positive and at most the maximum")
        if self.fomo_lab_max_position_usd > self.fomo_lab_bankroll_usd:
            raise ValueError("FOMO_LAB_MAX_POSITION_USD cannot exceed the simulated bankroll")
        if not 1 <= self.fomo_lab_max_concurrent_positions <= 25:
            raise ValueError("FOMO_LAB_MAX_CONCURRENT_POSITIONS must be between 1 and 25")
        if self.fomo_lab_max_total_exposure_usd <= 0:
            raise ValueError("FOMO_LAB_MAX_TOTAL_EXPOSURE_USD must be positive")
        if self.fomo_lab_daily_loss_cap_usd <= 0:
            raise ValueError("FOMO_LAB_DAILY_LOSS_CAP_USD must be positive")
        if self.fomo_lab_min_liquidity_usd < 0:
            raise ValueError("FOMO_LAB_MIN_LIQUIDITY_USD cannot be negative")
        if not 0 < self.fomo_lab_max_price_impact_percent <= 100:
            raise ValueError("FOMO_LAB_MAX_PRICE_IMPACT_PERCENT must be between 0 and 100")
        if not 0 < self.fomo_lab_max_slippage_percent <= 100:
            raise ValueError("FOMO_LAB_MAX_SLIPPAGE_PERCENT must be between 0 and 100")
        if self.fomo_lab_min_net_edge_percent < 0:
            raise ValueError("FOMO_LAB_MIN_NET_EDGE_PERCENT cannot be negative")
        if not 0 <= self.fomo_lab_platform_fee_bps <= 5000:
            raise ValueError("FOMO_LAB_PLATFORM_FEE_BPS must be between 0 and 5000")
        if not 0 <= self.fomo_lab_slippage_bps <= 5000:
            raise ValueError("FOMO_LAB_SLIPPAGE_BPS must be between 0 and 5000")
        if self.fomo_lab_priority_fee_usd < 0 or self.fomo_lab_network_fee_usd < 0:
            raise ValueError("Simulated network and priority fees cannot be negative")
        if not 60 <= self.fomo_lab_cooldown_seconds <= 604_800:
            raise ValueError("FOMO_LAB_COOLDOWN_SECONDS must be between 60 and 604800")
        if not 1 <= self.fomo_lab_min_forward_sample <= 10_000:
            raise ValueError("FOMO_LAB_MIN_FORWARD_SAMPLE must be between 1 and 10000")
        if not 1 <= self.fomo_social_posts_per_account <= 100:
            raise ValueError("FOMO_SOCIAL_POSTS_PER_ACCOUNT must be between 1 and 100")
        if not 0 <= self.fomo_social_daily_request_budget <= 10_000:
            raise ValueError("FOMO_SOCIAL_DAILY_REQUEST_BUDGET must be between 0 and 10000")
        if not 0 <= self.fomo_fast_watch_min_actionability <= 100:
            raise ValueError("FOMO_FAST_WATCH_MIN_ACTIONABILITY must be between 0 and 100")
        if not self.fomo_discovery_source_name or len(self.fomo_discovery_source_name) > 64:
            raise ValueError("FOMO_DISCOVERY_SOURCE_NAME must be 1-64 characters")
        if not 0 <= self.fomo_fast_watch_min_score <= 100:
            raise ValueError("FOMO_FAST_WATCH_MIN_SCORE must be between 0 and 100")
        if not 30 <= self.fomo_fast_watch_max_queue_age_seconds <= 3600:
            raise ValueError(
                "FOMO_FAST_WATCH_MAX_QUEUE_AGE_SECONDS must be between 30 and 3600"
            )
        if not 0 <= self.fomo_fast_watch_cooldown_seconds <= 86_400:
            raise ValueError("FOMO_FAST_WATCH_COOLDOWN_SECONDS must be between 0 and 86400")
        if not 0 <= self.fomo_fast_watch_max_per_hour <= 500:
            raise ValueError("FOMO_FAST_WATCH_MAX_PER_HOUR must be between 0 and 500")
        # The SHADOW experiment only produces comparable per-family expectancy
        # if every entry is the same size, so a misconfigured stake fails loudly
        # here instead of quietly producing an uninterpretable sample.
        if not 5 <= self.fomo_runner_analysis_budget_seconds <= 300:
            raise ValueError(
                "FOMO_RUNNER_ANALYSIS_BUDGET_SECONDS must be between 5 and 300"
            )
        if self.fomo_early_min_liquidity_usd < 0:
            raise ValueError("FOMO_EARLY_MIN_LIQUIDITY_USD cannot be negative")
        if not 60 <= self.fomo_early_max_age_seconds <= 86_400:
            raise ValueError(
                "FOMO_EARLY_MAX_AGE_SECONDS must be between 60 and 86400"
            )
        if not 0 <= self.fomo_early_runner_min_score <= 100:
            raise ValueError("FOMO_EARLY_RUNNER_MIN_SCORE must be between 0 and 100")
        if not 0 <= self.fomo_early_max_runners_per_hour <= 500:
            raise ValueError(
                "FOMO_EARLY_MAX_RUNNERS_PER_HOUR must be between 0 and 500"
            )
        if not 0 <= self.fomo_early_cooldown_seconds <= 86_400:
            raise ValueError("FOMO_EARLY_COOLDOWN_SECONDS must be between 0 and 86400")
        if self.fomo_shadow_position_usd <= 0:
            raise ValueError("FOMO_SHADOW_POSITION_USD must be positive")
        if self.fomo_shadow_max_position_usd != self.fomo_shadow_position_usd:
            raise ValueError(
                "FOMO_SHADOW_MAX_POSITION_USD must equal FOMO_SHADOW_POSITION_USD — "
                "every shadow entry is the same size by design"
            )
        if self.fomo_shadow_bankroll_usd < self.fomo_shadow_position_usd:
            raise ValueError("FOMO_SHADOW_BANKROLL_USD cannot be smaller than one entry")
        if not 1 <= self.fomo_shadow_max_positions <= 25:
            raise ValueError("FOMO_SHADOW_MAX_POSITIONS must be between 1 and 25")
        if self.fomo_shadow_max_exposure_usd < self.fomo_shadow_position_usd:
            raise ValueError("FOMO_SHADOW_MAX_EXPOSURE_USD cannot be below one entry")
        if self.fomo_shadow_max_exposure_usd > self.fomo_shadow_bankroll_usd:
            raise ValueError("FOMO_SHADOW_MAX_EXPOSURE_USD cannot exceed the shadow bankroll")
        if self.fomo_shadow_net_profit_objective_usd <= 0:
            raise ValueError("FOMO_SHADOW_NET_PROFIT_OBJECTIVE_USD must be positive")
        if self.fomo_shadow_daily_loss_cap_usd <= 0:
            raise ValueError("FOMO_SHADOW_DAILY_LOSS_CAP_USD must be positive")
        if not 0 < self.fomo_shadow_max_price_impact_percent <= 100:
            raise ValueError("FOMO_SHADOW_MAX_PRICE_IMPACT_PERCENT must be between 0 and 100")
        if not 30 <= self.fomo_shadow_max_signal_age_seconds <= 86_400:
            raise ValueError(
                "FOMO_SHADOW_MAX_SIGNAL_AGE_SECONDS must be between 30 and 86400"
            )
        if not 100 <= self.fomo_shadow_max_fill_latency_ms <= 600_000:
            raise ValueError(
                "FOMO_SHADOW_MAX_FILL_LATENCY_MS must be between 100 and 600000"
            )
        if not 1 <= self.fomo_shadow_min_forward_sample <= 10_000:
            raise ValueError("FOMO_SHADOW_MIN_FORWARD_SAMPLE must be between 1 and 10000")
        # Trending cadence is bounded on both sides: fast enough to be worth
        # having a separate lane for, never fast enough to hammer a source.
        if not 15 <= self.fomo_trending_poll_seconds <= 3_600:
            raise ValueError("FOMO_TRENDING_POLL_SECONDS must be between 15 and 3600")
        if not 5 <= self.fomo_trending_max_tracked <= 500:
            raise ValueError("FOMO_TRENDING_MAX_TRACKED must be between 5 and 500")
        if not 0 <= self.fomo_trending_alpha_min_score <= 100:
            raise ValueError("FOMO_TRENDING_ALPHA_MIN_SCORE must be between 0 and 100")
        if not 0 <= self.fomo_trending_watch_min_score <= 100:
            raise ValueError("FOMO_TRENDING_WATCH_MIN_SCORE must be between 0 and 100")
        if self.fomo_trending_watch_min_score > self.fomo_trending_alpha_min_score:
            raise ValueError(
                "FOMO_TRENDING_WATCH_MIN_SCORE cannot exceed FOMO_TRENDING_ALPHA_MIN_SCORE"
            )
        if not 0 <= self.fomo_trending_max_alerts_per_hour <= 200:
            raise ValueError("FOMO_TRENDING_MAX_ALERTS_PER_HOUR must be between 0 and 200")
        if not 0 <= self.fomo_trending_cooldown_seconds <= 86_400:
            raise ValueError("FOMO_TRENDING_COOLDOWN_SECONDS must be between 0 and 86400")
        # A hot watch that reevaluates as slowly as the legacy recheck is not a
        # hot watch; that slowness is the bug it exists to fix.
        if not 15 <= self.fomo_trending_hot_watch_recheck_seconds <= 600:
            raise ValueError(
                "FOMO_TRENDING_HOT_WATCH_RECHECK_SECONDS must be between 15 and 600"
            )
        if not 60 <= self.fomo_trending_hot_watch_seconds <= 7_200:
            raise ValueError("FOMO_TRENDING_HOT_WATCH_SECONDS must be between 60 and 7200")
        if self.fomo_trending_hot_watch_recheck_seconds >= self.fomo_trending_hot_watch_seconds:
            raise ValueError(
                "FOMO_TRENDING_HOT_WATCH_RECHECK_SECONDS must be shorter than the hot-watch window"
            )
        if not 1 <= self.fomo_trending_hot_watch_max <= 100:
            raise ValueError("FOMO_TRENDING_HOT_WATCH_MAX must be between 1 and 100")
        if not 0 <= self.fomo_trending_hot_watch_band <= 50:
            raise ValueError("FOMO_TRENDING_HOT_WATCH_BAND must be between 0 and 50")
        if not 60 <= self.fomo_trending_stale_snapshot_seconds <= 86_400:
            raise ValueError(
                "FOMO_TRENDING_STALE_SNAPSHOT_SECONDS must be between 60 and 86400"
            )
        # The Trenches loop reads public RPC, so its floor is set by politeness
        # to the node rather than by a vendor's plan.
        if not 10 <= self.fomo_trenches_poll_seconds <= 3_600:
            raise ValueError("FOMO_TRENCHES_POLL_SECONDS must be between 10 and 3600")
        if not 5 <= self.fomo_trenches_max_tracked <= 500:
            raise ValueError("FOMO_TRENCHES_MAX_TRACKED must be between 5 and 500")
        if not 0 <= self.fomo_trenches_runner_min_score <= 100:
            raise ValueError("FOMO_TRENCHES_RUNNER_MIN_SCORE must be between 0 and 100")
        if not 0 <= self.fomo_trenches_heads_up_min_score <= 100:
            raise ValueError("FOMO_TRENCHES_HEADS_UP_MIN_SCORE must be between 0 and 100")
        if self.fomo_trenches_heads_up_min_score > self.fomo_trenches_runner_min_score:
            raise ValueError(
                "FOMO_TRENCHES_HEADS_UP_MIN_SCORE cannot exceed FOMO_TRENCHES_RUNNER_MIN_SCORE"
            )
        if not 0 <= self.fomo_trenches_max_alerts_per_hour <= 200:
            raise ValueError("FOMO_TRENCHES_MAX_ALERTS_PER_HOUR must be between 0 and 200")
        if not 0 <= self.fomo_trenches_cooldown_seconds <= 86_400:
            raise ValueError("FOMO_TRENCHES_COOLDOWN_SECONDS must be between 0 and 86400")
        # Enrichment budgets are what keep a busy board affordable (section 71).
        if not 1 <= self.fomo_trenches_max_enrichment_per_scan <= 100:
            raise ValueError(
                "FOMO_TRENCHES_MAX_ENRICHMENT_PER_SCAN must be between 1 and 100"
            )
        if not 0 <= self.fomo_trenches_wallet_lookups_per_token <= 200:
            raise ValueError(
                "FOMO_TRENCHES_WALLET_LOOKUPS_PER_TOKEN must be between 0 and 200"
            )
        if not 0 <= self.fomo_trenches_holder_reads_per_scan <= 100:
            raise ValueError(
                "FOMO_TRENCHES_HOLDER_READS_PER_SCAN must be between 0 and 100"
            )
        if not 0 <= self.fomo_public_trending_min_score <= 100:
            raise ValueError("FOMO_PUBLIC_TRENDING_MIN_SCORE must be between 0 and 100")
        # The cadence tiers must actually be tiers.
        if not 5 <= self.fomo_trenches_hot_recheck_seconds <= 600:
            raise ValueError("FOMO_TRENCHES_HOT_RECHECK_SECONDS must be between 5 and 600")
        if not (
            self.fomo_trenches_hot_recheck_seconds
            <= self.fomo_trenches_warm_recheck_seconds
            <= self.fomo_trenches_normal_recheck_seconds
        ):
            raise ValueError(
                "Trenches recheck cadences must satisfy hot <= warm <= normal"
            )
        if not 1 <= self.fomo_trenches_max_hot <= 50:
            raise ValueError("FOMO_TRENCHES_MAX_HOT must be between 1 and 50")
        if self.fomo_trenches_max_warm < self.fomo_trenches_max_hot:
            raise ValueError(
                "FOMO_TRENCHES_MAX_WARM cannot be smaller than FOMO_TRENCHES_MAX_HOT"
            )
        if self.fomo_notable_min_trade_usd < 0:
            raise ValueError("FOMO_NOTABLE_MIN_TRADE_USD cannot be negative")
        if not 60 <= self.fomo_notable_max_signal_age_seconds <= 86_400:
            raise ValueError(
                "FOMO_NOTABLE_MAX_SIGNAL_AGE_SECONDS must be between 60 and 86400"
            )
        if not 60 <= self.fomo_catalyst_max_event_age_seconds <= 604_800:
            raise ValueError(
                "FOMO_CATALYST_MAX_EVENT_AGE_SECONDS must be between 60 and 604800"
            )
        if not 0 <= self.fomo_alert_enrichment_delay_seconds <= 900:
            raise ValueError(
                "FOMO_ALERT_ENRICHMENT_DELAY_SECONDS must be between 0 and 900"
            )
        if len(self.x_news_stream_rule) > 1024:
            raise ValueError("X_NEWS_STREAM_RULE cannot exceed 1024 characters")
        if not 15 <= self.news_poll_seconds <= 3600:
            raise ValueError("NEWS_POLL_SECONDS must be between 15 and 3600")
        if not 0 <= self.news_min_score <= 100:
            raise ValueError("NEWS_MIN_SCORE must be between 0 and 100")
        if not self.news_min_score <= self.news_launch_ready_score <= 100:
            raise ValueError("NEWS_LAUNCH_READY_SCORE must be between NEWS_MIN_SCORE and 100")
        if not self.news_min_score <= self.no_x_launch_min_score <= 100:
            raise ValueError("NO_X_LAUNCH_MIN_SCORE must be between NEWS_MIN_SCORE and 100")
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
