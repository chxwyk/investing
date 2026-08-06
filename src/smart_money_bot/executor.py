from __future__ import annotations

import time
from decimal import ROUND_DOWN, Decimal

from solders.keypair import Keypair

from .config import Settings
from .database import Database
from .errors import JupiterError
from .market import JupiterClient, load_keypair
from .models import (
    DetectedSwap,
    ExecutionMode,
    ExecutionResult,
    Side,
    Signal,
    TokenInfo,
    TrackedTrader,
)


class ExecutionManager:
    def __init__(
        self, settings: Settings, database: Database, market: JupiterClient
    ) -> None:
        self.settings = settings
        self.database = database
        self.market = market
        self.keypair: Keypair | None = None
        if settings.trading_private_key:
            self.keypair = load_keypair(settings.trading_private_key)

    async def execute(
        self,
        *,
        signal_id: int,
        signal: Signal,
        mode: ExecutionMode,
        token_info: TokenInfo | None,
        market_price_usd: Decimal,
        size_usd: Decimal,
    ) -> ExecutionResult:
        if mode is ExecutionMode.ALERTS:
            return ExecutionResult(
                success=True,
                mode=mode,
                token_mint=signal.token_mint,
                side=signal.side,
                size_usd=Decimal("0"),
                message="Alert generated; execution disabled",
            )

        if mode is ExecutionMode.PAPER:
            fill = await self.database.paper_execute(
                signal_id=signal_id,
                token_mint=signal.token_mint,
                side=signal.side,
                market_price_usd=market_price_usd,
                size_usd=size_usd,
                fee_bps=self.settings.simulated_fee_bps,
                slippage_bps=self.settings.simulated_slippage_bps,
            )
            if fill is None:
                result = ExecutionResult(
                    success=False,
                    mode=mode,
                    token_mint=signal.token_mint,
                    side=signal.side,
                    size_usd=size_usd,
                    message=(
                        "Skipped: the paper account does not own this token"
                        if signal.side is Side.SELL
                        else "No paper cash available for this signal"
                    ),
                )
            else:
                result = ExecutionResult(
                    success=True,
                    mode=mode,
                    token_mint=signal.token_mint,
                    side=signal.side,
                    size_usd=size_usd,
                    message=(
                        f"Paper fill: {fill['quantity']:.6f} tokens at "
                        f"${fill['price']:.8f}; fee ${fill['fee']:.4f}; "
                        f"realized P&L ${fill['realized_pnl']:.2f}"
                    ),
                )
            await self._log(signal_id, result)
            return result

        result = await self._execute_live(
            signal_id=signal_id,
            signal=signal,
            token_info=token_info,
            market_price_usd=market_price_usd,
            size_usd=size_usd,
        )
        await self._log(signal_id, result)
        return result

    async def execute_paper_mirror(
        self,
        *,
        swap: DetectedSwap,
        trader: TrackedTrader,
        market_price_usd: Decimal,
        size_usd: Decimal,
    ) -> ExecutionResult:
        fill = await self.database.paper_mirror_execute(
            trader_address=trader.address,
            source_signature=swap.signature,
            token_mint=swap.token_mint,
            side=swap.side,
            source_token_amount=swap.token_amount,
            market_price_usd=market_price_usd,
            size_usd=size_usd,
            fee_bps=self.settings.simulated_fee_bps,
            slippage_bps=self.settings.simulated_slippage_bps,
            max_position_usd=self.settings.max_copy_usd,
        )
        if fill is None:
            message = (
                "Skipped: no paper cash or raw-lot capacity remains for this buy"
                if swap.side is Side.BUY
                else (
                    "Skipped: no open paper lot remains for this tracked wallet. "
                    "It may already have been closed by an automatic paper risk guard."
                )
            )
            result = ExecutionResult(
                success=False,
                mode=ExecutionMode.PAPER,
                token_mint=swap.token_mint,
                side=swap.side,
                size_usd=size_usd,
                message=message,
            )
        elif swap.side is Side.BUY:
            result = ExecutionResult(
                success=True,
                mode=ExecutionMode.PAPER,
                token_mint=swap.token_mint,
                side=swap.side,
                size_usd=size_usd,
                message=(
                    f"Raw mirror of {trader.alias}: bought {fill['quantity']:.6f} paper "
                    f"tokens at ${fill['price']:.8f}; fee ${fill['fee']:.4f}. "
                    "This fake lot is linked to that source wallet."
                ),
            )
        else:
            sold_percent = fill["source_fraction"] * Decimal("100")
            result = ExecutionResult(
                success=True,
                mode=ExecutionMode.PAPER,
                token_mint=swap.token_mint,
                side=swap.side,
                size_usd=size_usd,
                message=(
                    f"Raw mirror of {trader.alias}: sold {sold_percent:.1f}% of that "
                    f"wallet's linked paper lot at ${fill['price']:.8f}; fee "
                    f"${fill['fee']:.4f}; realized P&L ${fill['realized_pnl']:.2f}."
                ),
            )
        await self._log(None, result)
        return result

    async def execute_paper_mirror_risk_exit(
        self,
        *,
        position: dict[str, object],
        market_price_usd: Decimal,
        reason: str,
    ) -> ExecutionResult:
        """Close one raw-mirror paper lot independently of the source wallet."""

        trader_address = str(position["trader_address"])
        token_mint = str(position["token_mint"])
        cost_basis = Decimal(str(position["cost_basis_usd"]))
        fill = await self.database.paper_mirror_execute(
            trader_address=trader_address,
            source_signature=f"paper-risk-{time.time_ns()}",
            token_mint=token_mint,
            side=Side.SELL,
            source_token_amount=Decimal(str(position["source_quantity"])),
            market_price_usd=market_price_usd,
            size_usd=cost_basis,
            fee_bps=self.settings.simulated_fee_bps,
            slippage_bps=self.settings.simulated_slippage_bps,
            execution_kind="RISK_EXIT",
            exit_reason=reason,
        )
        if fill is None:
            result = ExecutionResult(
                success=False,
                mode=ExecutionMode.PAPER,
                token_mint=token_mint,
                side=Side.SELL,
                size_usd=cost_basis,
                message="Skipped: the raw paper lot was already closed",
            )
        else:
            result = ExecutionResult(
                success=True,
                mode=ExecutionMode.PAPER,
                token_mint=token_mint,
                side=Side.SELL,
                size_usd=cost_basis,
                message=(
                    f"Automatic raw-mirror risk exit: {reason}. Sold the full linked "
                    f"paper lot at ${fill['price']:.8f}; fee ${fill['fee']:.4f}; "
                    f"realized P&L ${fill['realized_pnl']:.2f}."
                ),
            )
        await self._log(None, result)
        return result

    async def _execute_live(
        self,
        *,
        signal_id: int,
        signal: Signal,
        token_info: TokenInfo | None,
        market_price_usd: Decimal,
        size_usd: Decimal,
    ) -> ExecutionResult:
        del signal_id, market_price_usd
        if not self.settings.live_is_unlocked or self.keypair is None:
            return ExecutionResult(
                success=False,
                mode=ExecutionMode.LIVE,
                token_mint=signal.token_mint,
                side=signal.side,
                size_usd=size_usd,
                message="Live trading lock is not fully configured",
            )

        try:
            if signal.side is Side.BUY:
                if token_info is None or token_info.decimals is None:
                    raise JupiterError("Token decimals unavailable")
                base_price = await self.market.price(self.settings.live_base_mint)
                if base_price is None:
                    raise JupiterError("Live base-token price unavailable")
                base_units = size_usd / base_price
                amount_raw = int(
                    (
                        base_units * (Decimal(10) ** self.settings.live_base_decimals)
                    ).to_integral_value(rounding=ROUND_DOWN)
                )
                response = await self.market.swap(
                    input_mint=self.settings.live_base_mint,
                    output_mint=signal.token_mint,
                    amount_raw=amount_raw,
                    keypair=self.keypair,
                )
                output_raw = int(response["totalOutputAmount"])
                await self.database.set_live_position(
                    signal.token_mint,
                    quantity_raw=output_raw,
                    decimals=token_info.decimals,
                    cost_basis_usd=size_usd,
                )
            else:
                position = await self.database.get_live_position(signal.token_mint)
                if not position:
                    raise JupiterError("No tracked live position to sell")
                amount_raw = int(position["quantity_raw"])
                response = await self.market.swap(
                    input_mint=signal.token_mint,
                    output_mint=self.settings.live_base_mint,
                    amount_raw=amount_raw,
                    keypair=self.keypair,
                )
                await self.database.clear_live_position(signal.token_mint)

            return ExecutionResult(
                success=True,
                mode=ExecutionMode.LIVE,
                token_mint=signal.token_mint,
                side=signal.side,
                size_usd=size_usd,
                signature=response.get("signature"),
                message="Live Jupiter spot swap confirmed",
            )
        except (JupiterError, ValueError, KeyError) as exc:
            return ExecutionResult(
                success=False,
                mode=ExecutionMode.LIVE,
                token_mint=signal.token_mint,
                side=signal.side,
                size_usd=size_usd,
                message=str(exc),
            )

    async def _log(self, signal_id: int | None, result: ExecutionResult) -> None:
        await self.database.log_execution(
            signal_id=signal_id,
            mode=result.mode,
            token_mint=result.token_mint,
            side=result.side,
            size_usd=result.size_usd,
            success=result.success,
            signature=result.signature,
            message=result.message,
        )
