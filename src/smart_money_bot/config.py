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
    discovery_fetch_limit: int
    discovery_max_wallets: int
    discovery_min_24h_pnl_usd: Decimal
    discovery_min_win_rate_percent: Decimal
    discovery_min_roi_percent: Decimal
    discovery_min_trades: int
    discovery_max_trades: int
    discovery_min_closed_tokens: int
    discovery_max_single_token_percent: Decimal

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
            discovery_fetch_limit=_int("DISCOVERY_FETCH_LIMIT", 100),
            discovery_max_wallets=_int("DISCOVERY_MAX_WALLETS", 12),
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
