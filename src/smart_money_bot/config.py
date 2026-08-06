from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

from .constants import LIVE_ACK_TEXT, USDC_MINT


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
    database_path: str

    auto_discovery_enabled: bool
    discovery_refresh_seconds: int
    discovery_7d_refresh_seconds: int
    discovery_fetch_limit: int
    discovery_max_wallets: int
    discovery_min_24h_pnl_usd: Decimal
    discovery_min_win_rate_percent: Decimal
    discovery_min_roi_percent: Decimal
    discovery_min_trades: int
    discovery_max_trades: int
    discovery_min_closed_tokens: int
    discovery_max_single_token_percent: Decimal
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
    realtime_wallet_stream_enabled: bool
    solana_ws_url: str | None

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
    paper_raw_entry_filter_enabled: bool
    paper_daily_target_usd: Decimal
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
            fomo_referral_code=os.getenv(
                "FOMO_REFERRAL_CODE", "WetOuterLemur"
            ).strip()
            or None,
            solana_rpc_url=os.getenv(
                "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
            ).strip(),
            rpc_requests_per_second=_int("RPC_REQUESTS_PER_SECOND", 8),
            rpc_max_retries=_int("RPC_MAX_RETRIES", 4),
            jupiter_api_key=os.getenv("JUPITER_API_KEY", "").strip() or None,
            solana_tracker_api_key=os.getenv("SOLANA_TRACKER_API_KEY", "").strip() or None,
            database_path=os.getenv("DATABASE_PATH", "./data/smart_money.db").strip(),
            auto_discovery_enabled=_bool("AUTO_DISCOVERY_ENABLED", True),
            discovery_refresh_seconds=_int("DISCOVERY_REFRESH_SECONDS", 1200),
            discovery_7d_refresh_seconds=_int("DISCOVERY_7D_REFRESH_SECONDS", 21600),
            discovery_fetch_limit=_int("DISCOVERY_FETCH_LIMIT", 100),
            discovery_max_wallets=_int("DISCOVERY_MAX_WALLETS", 25),
            discovery_min_24h_pnl_usd=_decimal("DISCOVERY_MIN_24H_PNL_USD", "100"),
            discovery_min_win_rate_percent=_decimal(
                "DISCOVERY_MIN_WIN_RATE_PERCENT", "55"
            ),
            discovery_min_roi_percent=_decimal("DISCOVERY_MIN_ROI_PERCENT", "3"),
            discovery_min_trades=_int("DISCOVERY_MIN_TRADES", 5),
            discovery_max_trades=_int("DISCOVERY_MAX_TRADES", 250),
            discovery_min_closed_tokens=_int("DISCOVERY_MIN_CLOSED_TOKENS", 2),
            discovery_max_single_token_percent=_decimal(
                "DISCOVERY_MAX_SINGLE_TOKEN_PERCENT", "70"
            ),
            discovery_min_7d_pnl_usd=_decimal("DISCOVERY_MIN_7D_PNL_USD", "300"),
            discovery_min_7d_win_rate_percent=_decimal(
                "DISCOVERY_MIN_7D_WIN_RATE_PERCENT", "55"
            ),
            discovery_min_7d_roi_percent=_decimal("DISCOVERY_MIN_7D_ROI_PERCENT", "5"),
            discovery_min_7d_trades=_int("DISCOVERY_MIN_7D_TRADES", 10),
            discovery_max_7d_trades=_int("DISCOVERY_MAX_7D_TRADES", 1000),
            rotation_refresh_seconds=_int("ROTATION_REFRESH_SECONDS", 300),
            rotation_max_idle_seconds=_int("ROTATION_MAX_IDLE_SECONDS", 3600),
            rotation_probe_transactions=_int("ROTATION_PROBE_TRANSACTIONS", 6),
            rotation_min_recent_swaps=_int("ROTATION_MIN_RECENT_SWAPS", 1),
            rotation_min_pump_swaps=_int("ROTATION_MIN_PUMP_SWAPS", 1),
            rotation_require_pump_activity=_bool(
                "ROTATION_REQUIRE_PUMP_ACTIVITY", True
            ),
            realtime_wallet_stream_enabled=_bool(
                "REALTIME_WALLET_STREAM_ENABLED", True
            ),
            solana_ws_url=os.getenv("SOLANA_WS_URL", "").strip() or None,
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
            paper_raw_entry_filter_enabled=_bool(
                "PAPER_RAW_ENTRY_FILTER_ENABLED", True
            ),
            paper_daily_target_usd=_decimal("PAPER_DAILY_TARGET_USD", "100"),
            paper_use_executable_quotes=_bool(
                "PAPER_USE_EXECUTABLE_QUOTES", True
            ),
            paper_quote_output_buffer_bps=_int(
                "PAPER_QUOTE_OUTPUT_BUFFER_BPS", 50
            ),
            max_adverse_entry_drift_percent=_decimal(
                "MAX_ADVERSE_ENTRY_DRIFT_PERCENT", "8"
            ),
            max_quote_price_impact_percent=_decimal(
                "MAX_QUOTE_PRICE_IMPACT_PERCENT", "2"
            ),
            max_quote_latency_ms=_int("MAX_QUOTE_LATENCY_MS", 5000),
            max_consecutive_quote_failures=_int(
                "MAX_CONSECUTIVE_QUOTE_FAILURES", 5
            ),
            readiness_min_active_days=_int("READINESS_MIN_ACTIVE_DAYS", 14),
            readiness_min_closed_trades=_int(
                "READINESS_MIN_CLOSED_TRADES", 100
            ),
            readiness_min_profit_factor=_decimal(
                "READINESS_MIN_PROFIT_FACTOR", "1.25"
            ),
            readiness_max_drawdown_percent=_decimal(
                "READINESS_MAX_DRAWDOWN_PERCENT", "10"
            ),
            readiness_min_quote_success_percent=_decimal(
                "READINESS_MIN_QUOTE_SUCCESS_PERCENT", "95"
            ),
            max_copy_usd=_decimal("MAX_COPY_USD", "25"),
            max_daily_loss_usd=_decimal("MAX_DAILY_LOSS_USD", "30"),
            max_open_positions=_int("MAX_OPEN_POSITIONS", 6),
            min_token_liquidity_usd=_decimal("MIN_TOKEN_LIQUIDITY_USD", "50000"),
            min_token_holders=_int("MIN_TOKEN_HOLDERS", 100),
            min_organic_score=_decimal("MIN_ORGANIC_SCORE", "20"),
            max_top_holders_percent=_decimal("MAX_TOP_HOLDERS_PERCENT", "70"),
            max_signal_age_seconds=_int("MAX_SIGNAL_AGE_SECONDS", 90),
            stop_loss_percent=_decimal("STOP_LOSS_PERCENT", "12"),
            take_profit_percent=_decimal("TAKE_PROFIT_PERCENT", "30"),
            max_hold_seconds=_int("MAX_HOLD_SECONDS", 21_600),
            raw_mirror_stop_loss_percent=_decimal(
                "RAW_MIRROR_STOP_LOSS_PERCENT", "8"
            ),
            raw_mirror_take_profit_percent=_decimal(
                "RAW_MIRROR_TAKE_PROFIT_PERCENT", "20"
            ),
            raw_mirror_trailing_activation_percent=_decimal(
                "RAW_MIRROR_TRAILING_ACTIVATION_PERCENT", "8"
            ),
            raw_mirror_trailing_stop_percent=_decimal(
                "RAW_MIRROR_TRAILING_STOP_PERCENT", "4"
            ),
            raw_mirror_max_hold_seconds=_int(
                "RAW_MIRROR_MAX_HOLD_SECONDS", 7_200
            ),
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
    def discovery_is_configured(self) -> bool:
        return self.auto_discovery_enabled and bool(self.solana_tracker_api_key)

    def validate(self) -> None:
        if self.consensus_min_traders < 1:
            raise ValueError("CONSENSUS_MIN_TRADERS must be at least 1")
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
        if self.discovery_min_7d_pnl_usd < 0:
            raise ValueError("DISCOVERY_MIN_7D_PNL_USD cannot be negative")
        if not 0 <= self.discovery_min_7d_win_rate_percent <= 100:
            raise ValueError(
                "DISCOVERY_MIN_7D_WIN_RATE_PERCENT must be between 0 and 100"
            )
        if self.discovery_min_7d_roi_percent < 0:
            raise ValueError("DISCOVERY_MIN_7D_ROI_PERCENT cannot be negative")
        if self.discovery_min_7d_trades < 1:
            raise ValueError("DISCOVERY_MIN_7D_TRADES must be at least 1")
        if self.discovery_max_7d_trades < self.discovery_min_7d_trades:
            raise ValueError(
                "DISCOVERY_MAX_7D_TRADES cannot be below DISCOVERY_MIN_7D_TRADES"
            )
        if self.rotation_refresh_seconds < 300:
            raise ValueError("ROTATION_REFRESH_SECONDS must be at least 300")
        if self.rotation_max_idle_seconds < self.rotation_refresh_seconds:
            raise ValueError(
                "ROTATION_MAX_IDLE_SECONDS cannot be below ROTATION_REFRESH_SECONDS"
            )
        if not 1 <= self.rotation_probe_transactions <= 25:
            raise ValueError("ROTATION_PROBE_TRANSACTIONS must be between 1 and 25")
        if self.rotation_min_recent_swaps < 1:
            raise ValueError("ROTATION_MIN_RECENT_SWAPS must be at least 1")
        if self.rotation_min_pump_swaps < 1:
            raise ValueError("ROTATION_MIN_PUMP_SWAPS must be at least 1")
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
        if self.stop_loss_percent <= 0 or self.take_profit_percent <= 0:
            raise ValueError("Stop-loss and take-profit percentages must be positive")
        if self.max_hold_seconds < 60:
            raise ValueError("MAX_HOLD_SECONDS must be at least 60")
        if self.paper_daily_target_usd <= 0:
            raise ValueError("PAPER_DAILY_TARGET_USD must be positive")
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
            raise ValueError(
                "READINESS_MIN_QUOTE_SUCCESS_PERCENT must be between 0 and 100"
            )
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
