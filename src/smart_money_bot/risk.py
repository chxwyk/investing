from __future__ import annotations

import time
from decimal import Decimal

from .config import Settings
from .database import Database
from .models import ExecutionMode, RiskDecision, Side, Signal, TokenInfo


class RiskEngine:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database

    async def assess(
        self,
        *,
        signal: Signal,
        mode: ExecutionMode,
        token_info: TokenInfo | None,
        market_price_usd: Decimal | None,
    ) -> RiskDecision:
        blockers: list[str] = []
        warnings: list[str] = []
        age = int(time.time()) - signal.created_at

        if market_price_usd is None or market_price_usd <= 0:
            blockers.append("No reliable USD price")
        if age > self.settings.max_signal_age_seconds:
            blockers.append(f"Signal is stale ({age}s old)")
        if (
            signal.side is Side.BUY
            and len(set(signal.trader_addresses)) < self.settings.consensus_min_traders
        ):
            blockers.append("Not enough independent traders")

        if signal.side is Side.BUY and token_info is None:
            if mode is ExecutionMode.LIVE:
                blockers.append("Token safety metadata unavailable")
            else:
                warnings.append("Token metadata unavailable; paper mode only")
        elif signal.side is Side.BUY and token_info is not None:
            if token_info.suspicious:
                blockers.append("Jupiter flags token as suspicious")
            if token_info.liquidity_usd is None:
                if mode is ExecutionMode.LIVE:
                    blockers.append("Liquidity is unknown")
            elif token_info.liquidity_usd < self.settings.min_token_liquidity_usd:
                blockers.append(
                    f"Liquidity ${token_info.liquidity_usd:,.0f} is below minimum"
                )
            if (
                token_info.holder_count is not None
                and token_info.holder_count < self.settings.min_token_holders
            ):
                blockers.append(f"Only {token_info.holder_count:,} holders")
            if (
                token_info.organic_score is not None
                and token_info.organic_score < self.settings.min_organic_score
            ):
                blockers.append(f"Organic score is only {token_info.organic_score}")
            if token_info.freeze_authority_disabled is False:
                blockers.append("Freeze authority is enabled")
            if token_info.mint_authority_disabled is False:
                blockers.append("Mint authority is enabled")
            if (
                token_info.top_holders_percent is not None
                and token_info.top_holders_percent > self.settings.max_top_holders_percent
            ):
                blockers.append(
                    f"Top-holder concentration is {token_info.top_holders_percent}%"
                )

        if mode is ExecutionMode.PAPER:
            daily_pnl = await self.database.paper_daily_realized_pnl()
            if daily_pnl <= -self.settings.max_daily_loss_usd:
                blockers.append("Daily loss limit reached")
            if signal.side is Side.BUY:
                positions = await self.database.paper_position_count()
                if positions >= self.settings.max_open_positions:
                    blockers.append("Maximum open positions reached")
        elif mode is ExecutionMode.LIVE:
            if not self.settings.live_is_unlocked:
                blockers.append("Live trading is not unlocked in environment settings")
            if signal.side is Side.BUY:
                positions = await self.database.live_position_count()
                if positions >= self.settings.max_open_positions:
                    blockers.append("Maximum live positions reached")

        size = min(self.settings.default_copy_usd, self.settings.max_copy_usd)
        return RiskDecision(
            allowed=not blockers,
            size_usd=size,
            reasons=tuple(blockers + warnings),
        )
