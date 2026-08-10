from __future__ import annotations

import time
from decimal import ROUND_DOWN, Decimal

from solders.keypair import Keypair

from .config import Settings
from .constants import USDC_MINT
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
from .rotation import is_pump_mint


class ExecutionManager:
    def __init__(
        self, settings: Settings, database: Database, market: JupiterClient
    ) -> None:
        self.settings = settings
        self.database = database
        self.market = market
        self.consecutive_quote_failures = 0
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
        token_info: TokenInfo | None = None,
        pump_source_fallback: bool = False,
        observation_mode: bool = False,
        sniper_mode: bool = False,
        baseline_mode: bool = False,
    ) -> ExecutionResult:
        if (
            self.settings.paper_use_executable_quotes
            and not pump_source_fallback
            and not observation_mode
            and not baseline_mode
        ):
            result = await self._execute_quoted_paper_mirror(
                swap=swap,
                trader=trader,
                market_price_usd=market_price_usd,
                size_usd=size_usd,
                token_info=token_info,
                execution_kind="SNIPER_QUOTE" if sniper_mode else "RAW_MIRROR",
                readiness_tracking=not sniper_mode,
                max_entry_drift_percent=(
                    self.settings.paper_sniper_max_entry_drift_percent
                    if sniper_mode
                    else None
                ),
                max_price_impact_percent=(
                    self.settings.paper_sniper_max_quote_price_impact_percent
                    if sniper_mode
                    else None
                ),
            )
            if (
                sniper_mode
                and not result.success
                and result.message.startswith("Skipped: quote unavailable")
                and is_pump_mint(swap.token_mint)
                and swap.token_price_usd is not None
                and swap.token_price_usd > 0
            ):
                penalty = Decimal(
                    self.settings.paper_sniper_source_penalty_bps
                ) / Decimal(10_000)
                market_price_usd = (
                    swap.token_price_usd * (Decimal("1") + penalty)
                    if swap.side is Side.BUY
                    else swap.token_price_usd * (Decimal("1") - penalty)
                )
                pump_source_fallback = True
            else:
                await self._log(None, result)
                return result

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
            max_position_usd=(
                None if observation_mode else self.settings.max_copy_usd
            ),
            execution_kind=(
                "TRACKING_BASELINE"
                if baseline_mode
                else (
                    "FORCED_OBSERVATION"
                    if observation_mode
                    else (
                        (
                            "SNIPER_SOURCE_FALLBACK"
                            if pump_source_fallback
                            else "SNIPER_PAPER"
                        )
                        if sniper_mode
                        else (
                            "PUMP_SOURCE_FALLBACK"
                            if pump_source_fallback
                            else "RAW_MIRROR"
                        )
                    )
                )
            ),
            source_price_usd=swap.token_price_usd,
        )
        if fill is None:
            if swap.side is Side.BUY:
                message = "Skipped: no paper cash or raw-lot capacity remains for this buy"
            else:
                message = await self._unmatched_sell_message(
                    trader.address, swap.token_mint
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
            if baseline_mode:
                result = ExecutionResult(
                    success=True,
                    mode=ExecutionMode.PAPER,
                    token_mint=swap.token_mint,
                    side=swap.side,
                    size_usd=size_usd,
                    message=(
                        f"Tracking baseline for {trader.alias}: opened a "
                        f"{size_usd:.2f} PAPER lot at ${fill['price']:.8f} because "
                        "the source wallet already held this token when monitoring "
                        "started. Only movement after this baseline counts; earlier "
                        "unobserved profit or loss is not invented."
                    ),
                )
                await self._log(None, result)
                return result
            observation_note = (
                " Forced PAPER observation used the source transaction price plus "
                f"{self.settings.paper_observation_penalty_bps}bps and bypassed "
                "liquidity, holder, route, drift, and quote gates. It records strategy "
                "behavior, not a price a live order could necessarily obtain."
                if observation_mode
                else ""
            )
            fallback_note = (
                (
                    (
                        f" Sniper PAPER used the detected on-chain price with a "
                        f"{self.settings.paper_sniper_source_penalty_bps}bps adverse "
                        "penalty; "
                    )
                    if pump_source_fallback
                    else " Sniper PAPER used the launch-stage test lane; "
                )
                + "this simulation is excluded from live-readiness evidence."
                if sniper_mode
                else (
                    f" Pump PAPER fallback used the detected on-chain price with a "
                    f"{self.settings.paper_pump_source_fallback_bps}bps adverse penalty; "
                    "this simulated fill is not proof that a live Jupiter order was executable."
                )
                if pump_source_fallback
                else ""
            )
            result = ExecutionResult(
                success=True,
                mode=ExecutionMode.PAPER,
                token_mint=swap.token_mint,
                side=swap.side,
                size_usd=size_usd,
                message=(
                    f"{'Sniper PAPER' if sniper_mode else 'Raw mirror'} of "
                    f"{trader.alias}: bought {fill['quantity']:.6f} paper "
                    f"tokens at ${fill['price']:.8f}; fee ${fill['fee']:.4f}. "
                    f"This fake lot is linked to that source wallet.{observation_note}"
                    f"{fallback_note}"
                ),
            )
        else:
            sold_percent = fill["source_fraction"] * Decimal("100")
            observation_note = (
                " Forced PAPER observation used the source transaction price minus "
                f"{self.settings.paper_observation_penalty_bps}bps; it is excluded "
                "from live-executable readiness evidence."
                if observation_mode
                else ""
            )
            fallback_note = (
                (
                    (
                        f" Sniper PAPER used the detected on-chain price with a "
                        f"{self.settings.paper_sniper_source_penalty_bps}bps adverse "
                        "penalty; "
                    )
                    if pump_source_fallback
                    else " Sniper PAPER used the launch-stage test lane; "
                )
                + "this exit is excluded from live-readiness evidence."
                if sniper_mode
                else (
                    f" Pump PAPER fallback used the detected on-chain price with a "
                    f"{self.settings.paper_pump_source_fallback_bps}bps adverse penalty; "
                    "this exit does not count as live-executable quote evidence."
                )
                if pump_source_fallback
                else ""
            )
            result = ExecutionResult(
                success=True,
                mode=ExecutionMode.PAPER,
                token_mint=swap.token_mint,
                side=swap.side,
                size_usd=size_usd,
                message=(
                    f"{'Sniper PAPER' if sniper_mode else 'Raw mirror'} of "
                    f"{trader.alias}: sold {sold_percent:.1f}% of that "
                    f"wallet's linked paper lot at ${fill['price']:.8f}; fee "
                    f"${fill['fee']:.4f}; realized P&L ${fill['realized_pnl']:.2f}."
                    f"{observation_note}{fallback_note}"
                ),
            )
        await self._log(None, result)
        return result

    async def _execute_quoted_paper_mirror(
        self,
        *,
        swap: DetectedSwap,
        trader: TrackedTrader,
        market_price_usd: Decimal,
        size_usd: Decimal,
        token_info: TokenInfo | None,
        execution_kind: str = "RAW_MIRROR",
        exit_reason: str | None = None,
        readiness_tracking: bool = True,
        max_entry_drift_percent: Decimal | None = None,
        max_price_impact_percent: Decimal | None = None,
    ) -> ExecutionResult:
        """Shadow a raw swap using a quote-only Jupiter Swap V2 order."""

        if swap.side is Side.BUY:
            capacity = await self.database.paper_mirror_buy_capacity(
                trader.address,
                swap.token_mint,
                size_usd,
                self.settings.max_copy_usd,
            )
            if capacity <= Decimal("0.01"):
                return self._paper_skip(
                    swap,
                    size_usd,
                    "no paper cash or raw-lot capacity remains for this buy",
                )
            if swap.token_price_usd is None or swap.token_price_usd <= 0:
                return await self._quote_failure(
                    swap=swap,
                    size_usd=capacity,
                    reason="source transaction price is unavailable; entry drift cannot be checked",
                    record_attempt=readiness_tracking,
                )
            if token_info is None or token_info.decimals is None:
                return await self._quote_failure(
                    swap=swap,
                    size_usd=capacity,
                    reason="token decimals are unavailable for an executable quote",
                    record_attempt=readiness_tracking,
                )
            amount_usd = capacity
            token_decimals = token_info.decimals
            try:
                input_amount_raw = await self._paper_base_amount_raw(amount_usd)
            except JupiterError as exc:
                return await self._quote_failure(
                    swap=swap,
                    size_usd=capacity,
                    reason=str(exc),
                    record_attempt=readiness_tracking,
                )
            input_decimals = self.settings.live_base_decimals
            output_decimals = token_decimals
            input_mint = self.settings.live_base_mint
            output_mint = swap.token_mint
        else:
            preview = await self.database.paper_mirror_sell_preview(
                trader.address,
                swap.token_mint,
                swap.token_amount,
            )
            if preview is None:
                return self._paper_skip(
                    swap,
                    size_usd,
                    (
                        await self._unmatched_sell_message(
                            trader.address, swap.token_mint, include_prefix=False
                        )
                    ),
                )
            raw_decimals = preview["token_decimals"]
            if raw_decimals is None:
                if token_info is None:
                    try:
                        token_info = await self.market.token_info(swap.token_mint)
                    except JupiterError:
                        token_info = None
                raw_decimals = token_info.decimals if token_info else None
            if raw_decimals is None:
                return await self._quote_failure(
                    swap=swap,
                    size_usd=Decimal(str(preview["matched_cost_usd"])),
                    reason="token decimals are unavailable for the exit quote",
                    record_attempt=readiness_tracking,
                )
            token_decimals = int(raw_decimals)
            input_amount = Decimal(str(preview["paper_quantity"]))
            input_amount_raw = int(
                (input_amount * (Decimal(10) ** token_decimals)).to_integral_value(
                    rounding=ROUND_DOWN
                )
            )
            if input_amount_raw <= 0:
                return self._paper_skip(swap, size_usd, "paper position is only token dust")
            amount_usd = Decimal(str(preview["matched_cost_usd"]))
            input_decimals = token_decimals
            output_decimals = self.settings.live_base_decimals
            input_mint = swap.token_mint
            output_mint = self.settings.live_base_mint

        try:
            quote = await self.market.quote_order(
                input_mint=input_mint,
                output_mint=output_mint,
                amount_raw=input_amount_raw,
                input_decimals=input_decimals,
                output_decimals=output_decimals,
            )
        except (JupiterError, ValueError) as exc:
            return await self._quote_failure(
                swap=swap,
                size_usd=amount_usd,
                reason=str(exc),
                record_attempt=readiness_tracking,
            )

        self.consecutive_quote_failures = 0
        buffer_multiplier = Decimal("1") - (
            Decimal(self.settings.paper_quote_output_buffer_bps) / Decimal(10_000)
        )
        price_impact = quote.price_impact_percent
        entry_drift_limit = (
            max_entry_drift_percent
            if max_entry_drift_percent is not None
            else self.settings.max_adverse_entry_drift_percent
        )
        price_impact_limit = (
            max_price_impact_percent
            if max_price_impact_percent is not None
            else self.settings.max_quote_price_impact_percent
        )

        if swap.side is Side.BUY:
            quote_price = amount_usd / quote.output_amount
            drift = (
                (quote_price / swap.token_price_usd) - Decimal("1")
            ) * Decimal("100")
            blocker: str | None = None
            if drift > entry_drift_limit:
                blocker = (
                    f"entry drift +{drift:.2f}% exceeds the "
                    f"{entry_drift_limit:.2f}% chase limit"
                )
            elif price_impact > price_impact_limit:
                blocker = (
                    f"Jupiter price impact {price_impact:.2f}% exceeds "
                    f"{price_impact_limit:.2f}%"
                )
            elif quote.observed_latency_ms > self.settings.max_quote_latency_ms:
                blocker = (
                    f"quote latency {quote.observed_latency_ms}ms exceeds "
                    f"{self.settings.max_quote_latency_ms}ms"
                )
            if blocker:
                if readiness_tracking:
                    await self.database.record_paper_quote_attempt(
                        source_signature=swap.signature,
                        token_mint=swap.token_mint,
                        side=swap.side,
                        quote_success=True,
                        accepted=False,
                        reason=blocker,
                        latency_ms=quote.observed_latency_ms,
                        price_impact_percent=price_impact,
                        price_drift_percent=drift,
                    )
                return self._paper_skip(swap, amount_usd, blocker)
            quoted_input_amount = quote.input_amount
            quoted_output_amount = quote.output_amount * buffer_multiplier
            source_price = swap.token_price_usd
        else:
            base_price = await self._paper_base_price()
            unbuffered_output_usd = (
                quote.output_usd_value
                if quote.output_usd_value is not None and quote.output_usd_value > 0
                else quote.output_amount * base_price
            )
            quote_price = unbuffered_output_usd / quote.input_amount
            drift = None
            quoted_input_amount = quote.input_amount
            quoted_output_amount = unbuffered_output_usd * buffer_multiplier
            source_price = swap.token_price_usd

        if readiness_tracking:
            await self.database.record_paper_quote_attempt(
                source_signature=swap.signature,
                token_mint=swap.token_mint,
                side=swap.side,
                quote_success=True,
                accepted=True,
                reason=None,
                latency_ms=quote.observed_latency_ms,
                price_impact_percent=price_impact,
                price_drift_percent=drift,
            )
        fill = await self.database.paper_mirror_execute(
            trader_address=trader.address,
            source_signature=swap.signature,
            token_mint=swap.token_mint,
            side=swap.side,
            source_token_amount=swap.token_amount,
            market_price_usd=quote_price,
            size_usd=amount_usd,
            fee_bps=0,
            slippage_bps=0,
            max_position_usd=self.settings.max_copy_usd,
            execution_kind=execution_kind,
            exit_reason=exit_reason,
            quoted_input_amount=quoted_input_amount,
            quoted_output_amount=quoted_output_amount,
            token_decimals=token_decimals,
            source_price_usd=source_price,
            quote_price_usd=quote_price,
            price_drift_percent=drift,
            price_impact_percent=price_impact,
            quote_router=quote.router,
            quote_latency_ms=quote.observed_latency_ms,
            quote_fee_bps=quote.fee_bps,
        )
        if fill is None:
            return self._paper_skip(
                swap,
                amount_usd,
                "paper lot changed before the quoted fill could be recorded",
            )
        if swap.side is Side.BUY:
            label = (
                "Sniper quote-shadow BUY"
                if execution_kind.startswith("SNIPER_")
                else "Quote-shadow BUY"
            )
            return ExecutionResult(
                success=True,
                mode=ExecutionMode.PAPER,
                token_mint=swap.token_mint,
                side=swap.side,
                size_usd=amount_usd,
                message=(
                    f"{label} of {trader.alias}: {fill['quantity']:.6f} paper "
                    f"tokens at ${fill['price']:.8f}. Jupiter {quote.router}; drift "
                    f"{drift:+.2f}%; impact {price_impact:.2f}%; "
                    f"{quote.observed_latency_ms}ms. The "
                    f"{self.settings.paper_quote_output_buffer_bps}bps "
                    "output buffer is already included."
                ),
            )

        sold_percent = fill["source_fraction"] * Decimal("100")
        if execution_kind.endswith("RISK_EXIT"):
            prefix = "Automatic quote-shadow risk exit"
        elif execution_kind.startswith("MANUAL"):
            prefix = "Manual PAPER quote exit"
        elif execution_kind.startswith("SNIPER_"):
            prefix = f"Sniper quote-shadow SELL of {trader.alias}"
        else:
            prefix = f"Quote-shadow SELL of {trader.alias}"
        reason_text = f": {exit_reason}" if exit_reason else ""
        return ExecutionResult(
            success=True,
            mode=ExecutionMode.PAPER,
            token_mint=swap.token_mint,
            side=swap.side,
            size_usd=amount_usd,
            message=(
                f"{prefix}{reason_text}. Sold {sold_percent:.1f}% of the linked paper lot "
                f"at ${fill['price']:.8f}; realized P&L ${fill['realized_pnl']:.2f}. "
                f"Jupiter {quote.router}; impact {price_impact:.2f}%; "
                f"{quote.observed_latency_ms}ms."
            ),
        )

    async def _paper_base_price(self) -> Decimal:
        if self.settings.live_base_mint == USDC_MINT:
            return Decimal("1")
        price = await self.market.price(self.settings.live_base_mint)
        if price is None or price <= 0:
            raise JupiterError("paper quote base-token price is unavailable")
        return price

    async def _paper_base_amount_raw(self, size_usd: Decimal) -> int:
        base_price = await self._paper_base_price()
        amount_raw = int(
            (
                size_usd
                / base_price
                * (Decimal(10) ** self.settings.live_base_decimals)
            ).to_integral_value(rounding=ROUND_DOWN)
        )
        if amount_raw <= 0:
            raise JupiterError("configured paper size is below one base-token unit")
        return amount_raw

    def _paper_skip(
        self, swap: DetectedSwap, size_usd: Decimal, reason: str
    ) -> ExecutionResult:
        return ExecutionResult(
            success=False,
            mode=ExecutionMode.PAPER,
            token_mint=swap.token_mint,
            side=swap.side,
            size_usd=size_usd,
            message=f"Skipped: {reason}",
        )

    async def _unmatched_sell_message(
        self,
        trader_address: str,
        token_mint: str,
        *,
        include_prefix: bool = True,
    ) -> str:
        latest = await self.database.paper_mirror_latest_event(
            trader_address, token_mint
        )
        if latest is None:
            reason = (
                "no open paper lot exists because no earlier BUY for this wallet/token "
                "was filled; the entry was likely skipped"
            )
        elif str(latest["side"]) == Side.SELL.value:
            exit_reason = str(latest.get("exit_reason") or "a prior source SELL")
            reason = f"the paper lot was already closed by {exit_reason}"
        else:
            reason = "no open paper lot remains; it was closed after the latest recorded BUY"
        return f"Skipped: {reason}" if include_prefix else reason

    async def _quote_failure(
        self,
        *,
        swap: DetectedSwap,
        size_usd: Decimal,
        reason: str,
        record_attempt: bool = True,
    ) -> ExecutionResult:
        if not record_attempt:
            return self._paper_skip(swap, size_usd, f"quote unavailable — {reason}")

        self.consecutive_quote_failures += 1
        await self.database.record_paper_quote_attempt(
            source_signature=swap.signature,
            token_mint=swap.token_mint,
            side=swap.side,
            quote_success=False,
            accepted=False,
            reason=reason,
        )
        suffix = ""
        if self.consecutive_quote_failures >= self.settings.max_consecutive_quote_failures:
            await self.database.set_setting("paused", "true")
            suffix = (
                f" Monitoring auto-paused after {self.consecutive_quote_failures} "
                "consecutive quote failures; use /smartmoney pause action:resume after fixing it."
            )
        return self._paper_skip(swap, size_usd, f"quote unavailable — {reason}.{suffix}")

    async def execute_paper_mirror_manual_exit(
        self,
        *,
        position: dict[str, object],
        market_price_usd: Decimal,
        requested_by: str,
        execution_kind: str = "MANUAL_EXIT",
        exit_reason: str | None = None,
        message_label: str = "Manual PAPER SELL",
    ) -> ExecutionResult:
        """Close one source-linked fake lot without moving real funds."""

        trader_address = str(position["trader_address"])
        token_mint = str(position["token_mint"])
        cost_basis = Decimal(str(position["cost_basis_usd"]))
        reason = exit_reason or f"manual PAPER sell requested by {requested_by}"
        signature_kind = execution_kind.lower().replace("_", "-")
        source_signature = f"paper-{signature_kind}-{time.time_ns()}"

        if self.settings.paper_force_observation_mode:
            penalty = Decimal(self.settings.paper_observation_penalty_bps) / Decimal(10_000)
            exit_price = market_price_usd * (Decimal("1") - penalty)
            fill = await self.database.paper_mirror_execute(
                trader_address=trader_address,
                source_signature=source_signature,
                token_mint=token_mint,
                side=Side.SELL,
                source_token_amount=Decimal(str(position["source_quantity"])),
                market_price_usd=exit_price,
                size_usd=cost_basis,
                fee_bps=self.settings.simulated_fee_bps,
                slippage_bps=self.settings.simulated_slippage_bps,
                execution_kind=(
                    "MANUAL_OBSERVATION_EXIT"
                    if execution_kind == "MANUAL_EXIT"
                    else execution_kind
                ),
                exit_reason=reason,
                source_price_usd=market_price_usd,
            )
            if fill is None:
                result = ExecutionResult(
                    success=False,
                    mode=ExecutionMode.PAPER,
                    token_mint=token_mint,
                    side=Side.SELL,
                    size_usd=cost_basis,
                    message="Skipped: that paper lot was already closed",
                )
            else:
                result = ExecutionResult(
                    success=True,
                    mode=ExecutionMode.PAPER,
                    token_mint=token_mint,
                    side=Side.SELL,
                    size_usd=cost_basis,
                    message=(
                        f"{message_label} filled in forced-observation mode. "
                        f"Sold the full linked fake lot at ${fill['price']:.8f}; fee "
                        f"${fill['fee']:.4f}; realized P&L ${fill['realized_pnl']:.2f}. "
                        "This is excluded from quote-readiness evidence."
                    ),
                )
            await self._log(None, result)
            return result

        if self.settings.paper_use_executable_quotes:
            swap = DetectedSwap(
                signature=source_signature,
                trader_address=trader_address,
                block_time=int(time.time()),
                side=Side.SELL,
                token_mint=token_mint,
                token_amount=Decimal(str(position["source_quantity"])),
                quote_mint=self.settings.live_base_mint,
                quote_amount=Decimal("0"),
                usd_value=cost_basis,
                token_price_usd=market_price_usd,
            )
            result = await self._execute_quoted_paper_mirror(
                swap=swap,
                trader=TrackedTrader(address=trader_address, alias=requested_by),
                market_price_usd=market_price_usd,
                size_usd=cost_basis,
                token_info=None,
                execution_kind=execution_kind,
                exit_reason=reason,
                readiness_tracking=False,
            )
            await self._log(None, result)
            return result

        fill = await self.database.paper_mirror_execute(
            trader_address=trader_address,
            source_signature=source_signature,
            token_mint=token_mint,
            side=Side.SELL,
            source_token_amount=Decimal(str(position["source_quantity"])),
            market_price_usd=market_price_usd,
            size_usd=cost_basis,
            fee_bps=self.settings.simulated_fee_bps,
            slippage_bps=self.settings.simulated_slippage_bps,
            execution_kind=execution_kind,
            exit_reason=reason,
            source_price_usd=market_price_usd,
        )
        if fill is None:
            result = ExecutionResult(
                success=False,
                mode=ExecutionMode.PAPER,
                token_mint=token_mint,
                side=Side.SELL,
                size_usd=cost_basis,
                message="Skipped: that paper lot was already closed",
            )
        else:
            result = ExecutionResult(
                success=True,
                mode=ExecutionMode.PAPER,
                token_mint=token_mint,
                side=Side.SELL,
                size_usd=cost_basis,
                message=(
                    f"{message_label} filled at ${fill['price']:.8f}; fee "
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
        sniper_lot = await self.database.paper_mirror_open_lot_is_sniper(
            trader_address, token_mint
        )
        execution_kind = "SNIPER_RISK_EXIT" if sniper_lot else "RISK_EXIT"
        if self.settings.paper_use_executable_quotes:
            swap = DetectedSwap(
                signature=f"paper-risk-{time.time_ns()}",
                trader_address=trader_address,
                block_time=int(time.time()),
                side=Side.SELL,
                token_mint=token_mint,
                token_amount=Decimal(str(position["source_quantity"])),
                quote_mint=self.settings.live_base_mint,
                quote_amount=Decimal("0"),
                usd_value=cost_basis,
                token_price_usd=market_price_usd,
            )
            result = await self._execute_quoted_paper_mirror(
                swap=swap,
                trader=TrackedTrader(
                    address=trader_address,
                    alias="risk engine",
                ),
                market_price_usd=market_price_usd,
                size_usd=cost_basis,
                token_info=None,
                execution_kind=execution_kind,
                exit_reason=reason,
                readiness_tracking=not sniper_lot,
            )
            await self._log(None, result)
            return result

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
            execution_kind=execution_kind,
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
