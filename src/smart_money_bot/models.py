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
