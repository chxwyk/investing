from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class ExecutionMode(StrEnum):
    ALERTS = "ALERTS"
    PAPER = "PAPER"
    LIVE = "LIVE"


@dataclass(frozen=True, slots=True)
class TrackedTrader:
    address: str
    alias: str
    enabled: bool = True
    last_signature: str | None = None
    weight: Decimal = Decimal("1")
    source: str = "manual"


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    address: str
    alias: str
    realized_pnl_24h: Decimal
    previous_pnl_24h: Decimal | None
    roi_24h_percent: Decimal
    win_rate_percent: Decimal
    trades_24h: int
    buys_24h: int
    sells_24h: int
    closed_tokens: int
    invested_24h_usd: Decimal
    volume_24h_usd: Decimal
    last_trade_ms: int | None
    score: Decimal
    rank: int
    realized_pnl_7d: Decimal = Decimal("0")
    roi_7d_percent: Decimal = Decimal("0")
    win_rate_7d_percent: Decimal = Decimal("0")
    trades_7d: int = 0
    recent_swaps: int = 0
    pump_swaps: int = 0
    last_activity_at: int | None = None
    selection_reason: str = ""
    metrics_limited_24h: bool = False
    metrics_limited_7d: bool = False

    @property
    def pnl_momentum_usd(self) -> Decimal | None:
        if self.previous_pnl_24h is None:
            return None
        return self.realized_pnl_24h - self.previous_pnl_24h


@dataclass(frozen=True, slots=True)
class DiscoveryRefresh:
    candidates: tuple[DiscoveryCandidate, ...]
    added_wallets: tuple[str, ...]
    disabled_wallets: tuple[str, ...]
    refreshed_at: int
    candidate_pool_size: int = 0
    verified_pump_wallets: int = 0
    removal_events: tuple[WalletRotationEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class WalletRotationEvent:
    address: str
    alias: str
    action: str
    reason: str
    score: Decimal
    pnl_24h_usd: Decimal
    pnl_7d_usd: Decimal
    baseline_pnl_24h_usd: Decimal
    baseline_pnl_7d_usd: Decimal
    observed_source_pnl_usd: Decimal
    paper_pnl_usd: Decimal
    recorded_at: int


@dataclass(frozen=True, slots=True)
class DetectedSwap:
    signature: str
    trader_address: str
    block_time: int
    side: Side
    token_mint: str
    token_amount: Decimal
    quote_mint: str
    quote_amount: Decimal
    usd_value: Decimal | None
    token_price_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class TokenInfo:
    mint: str
    symbol: str | None = None
    name: str | None = None
    decimals: int | None = None
    usd_price: Decimal | None = None
    liquidity_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    holder_count: int | None = None
    organic_score: Decimal | None = None
    verified: bool | None = None
    suspicious: bool = False
    mint_authority_disabled: bool | None = None
    freeze_authority_disabled: bool | None = None
    top_holders_percent: Decimal | None = None
    dev_balance_percent: Decimal | None = None


@dataclass(frozen=True, slots=True)
class DexSnapshot:
    available: bool
    liquidity_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    pair_age_minutes: int | None = None
    buys_5m: int = 0
    sells_5m: int = 0
    buys_1h: int = 0
    sells_1h: int = 0
    volume_5m_usd: Decimal = Decimal("0")
    volume_1h_usd: Decimal = Decimal("0")
    price_change_5m_percent: Decimal | None = None
    price_change_1h_percent: Decimal | None = None
    active_boosts: int = 0
    has_website: bool = False
    has_x_profile: bool = False
    website_url: str = ""
    x_handle: str = ""
    pair_url: str = ""


@dataclass(frozen=True, slots=True)
class XSocialSnapshot:
    available: bool
    posts: int = 0
    contract_posts: int = 0
    identity_posts: int = 0
    unique_authors: int = 0
    established_authors: int = 0
    influential_authors: int = 0
    suspicious_authors: int = 0
    engagements: int = 0
    duplicate_percent: Decimal = Decimal("0")
    posts_per_minute: Decimal = Decimal("0")
    query: str = ""
    error: str | None = None


@dataclass(frozen=True, slots=True)
class NewsAlert:
    source: str
    headline: str
    summary: str
    url: str
    author: str = ""
    author_followers: int = 0
    author_verified: bool = False
    score: int = 0
    urgency: str = "LOW"
    matched_rule: str = ""
    narrative_terms: tuple[str, ...] = ()
    token_mints: tuple[str, ...] = ()
    created_at: int = 0
    received_at: int = 0


@dataclass(frozen=True, slots=True)
class NarrativePairMatch:
    narrative: str
    mint: str
    symbol: str
    name: str
    liquidity_usd: Decimal | None
    market_cap_usd: Decimal | None
    pair_age_minutes: int | None
    buys_5m: int
    sells_5m: int
    volume_5m_usd: Decimal
    pair_url: str


@dataclass(frozen=True, slots=True)
class TokenRiskSnapshot:
    available: bool
    score: Decimal | None = None
    rugged: bool = False
    snipers_percent: Decimal | None = None
    insiders_percent: Decimal | None = None
    bundlers_percent: Decimal | None = None
    top10_percent: Decimal | None = None
    dev_percent: Decimal | None = None
    danger_flags: tuple[str, ...] = ()
    jupiter_verified: bool | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CoinCallout:
    mint: str
    symbol: str | None
    name: str | None
    score: Decimal
    verdict: str
    confidence: str
    smart_wallets: tuple[str, ...]
    token_info: TokenInfo | None
    dex: DexSnapshot
    social: XSocialSnapshot
    tracker_risk: TokenRiskSnapshot
    positives: tuple[str, ...]
    warnings: tuple[str, ...]
    hard_blockers: tuple[str, ...]
    generated_at: int


@dataclass(frozen=True, slots=True)
class SwapQuote:
    """A quote-only Jupiter Swap V2 order normalized for paper execution."""

    input_mint: str
    output_mint: str
    input_amount_raw: int
    output_amount_raw: int
    other_amount_threshold_raw: int | None
    input_amount: Decimal
    output_amount: Decimal
    input_usd_value: Decimal | None
    output_usd_value: Decimal | None
    price_impact_percent: Decimal
    router: str
    fee_bps: int
    api_time_ms: int | None
    observed_latency_ms: int
    quoted_at: int


@dataclass(frozen=True, slots=True)
class TraderMetrics:
    address: str
    alias: str
    window_seconds: int
    trades: int
    buys: int
    sells: int
    wins: int
    losses: int
    realized_pnl_usd: Decimal
    matched_cost_usd: Decimal
    volume_usd: Decimal
    max_drawdown_usd: Decimal

    @property
    def win_rate(self) -> Decimal:
        closed = self.wins + self.losses
        return Decimal(self.wins) / Decimal(closed) if closed else Decimal("0")

    @property
    def realized_roi(self) -> Decimal:
        if self.matched_cost_usd <= 0:
            return Decimal("0")
        return self.realized_pnl_usd / self.matched_cost_usd


@dataclass(frozen=True, slots=True)
class ScoredTrader:
    metrics_24h: TraderMetrics
    metrics_7d: TraderMetrics
    score: Decimal


@dataclass(frozen=True, slots=True)
class Signal:
    token_mint: str
    side: Side
    created_at: int
    trader_addresses: tuple[str, ...]
    trader_aliases: tuple[str, ...]
    source_signatures: tuple[str, ...]
    combined_score: Decimal
    reference_price_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    size_usd: Decimal
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    success: bool
    mode: ExecutionMode
    token_mint: str
    side: Side
    size_usd: Decimal
    signature: str | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class PaperSummary:
    starting_cash_usd: Decimal
    cash_usd: Decimal
    positions_value_usd: Decimal
    equity_usd: Decimal
    realized_pnl_usd: Decimal
    unrealized_pnl_usd: Decimal
    trades: int
    wins: int
    losses: int
    max_drawdown_usd: Decimal
    current_drawdown_usd: Decimal
    realized_pnl_24h_usd: Decimal
    gross_profit_usd: Decimal
    gross_loss_usd: Decimal
    average_win_usd: Decimal
    average_loss_usd: Decimal
    expectancy_usd: Decimal
    profit_factor: Decimal | None


@dataclass(frozen=True, slots=True)
class PaperDailyLockStatus:
    enabled: bool
    day: str
    target_usd: Decimal
    loss_limit_usd: Decimal
    baseline_equity_usd: Decimal
    current_equity_usd: Decimal
    marked_pnl_usd: Decimal
    locked: bool
    triggered_at: int | None
    open_positions: int
    lock_reason: str | None


@dataclass(frozen=True, slots=True)
class PaperReadiness:
    trial_started_at: int
    active_days: int
    quote_attempts: int
    quote_successes: int
    quote_success_percent: Decimal
    accepted_entries: int
    closed_trades: int
    gross_profit_usd: Decimal
    gross_loss_usd: Decimal
    expectancy_usd: Decimal
    profit_factor: Decimal | None
    max_drawdown_usd: Decimal
    max_drawdown_percent: Decimal
    ready: bool
    blockers: tuple[str, ...]
