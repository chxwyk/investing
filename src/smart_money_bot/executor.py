from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

from solders.keypair import Keypair

from .config import Settings
from .database import Database
from .errors import JupiterError
from .market import JupiterClient, load_keypair
from .models import (
    ExecutionMode,
    ExecutionResult,
    Side,
    Signal,
    TokenInfo,
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
                    message="No paper cash/position available for this signal",
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
                    (base_units * (Decimal(10) ** self.settings.live_base_decimals)).to_integral_value(
                        rounding=ROUND_DOWN
                    )
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

    async def _log(self, signal_id: int, result: ExecutionResult) -> None:
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
