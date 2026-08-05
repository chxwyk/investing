from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import replace
from decimal import Decimal
from typing import Protocol

from .config import Settings
from .constants import PAPER_DEMO_ENTRY_PRICE_USD, PAPER_DEMO_MINT
from .database import Database
from .detector import SwapDetector
from .discovery import DiscoveryPolicy, SolanaTrackerClient
from .errors import DiscoveryError, JupiterError, RpcError
from .executor import ExecutionManager
from .market import JupiterClient
from .models import (
    DetectedSwap,
    DiscoveryRefresh,
    ExecutionMode,
    ExecutionResult,
    PaperSummary,
    RiskDecision,
    ScoredTrader,
    Side,
    Signal,
    TokenInfo,
    TrackedTrader,
    TraderMetrics,
)
from .risk import RiskEngine
from .rpc import SolanaRPC
from .scoring import rank_traders
from .strategy import ConsensusStrategy

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    async def on_discovery(self, refresh: DiscoveryRefresh) -> None: ...

    async def on_swap(self, swap: DetectedSwap, trader: TrackedTrader) -> None: ...

    async def on_signal(
        self, signal: Signal, token_info: TokenInfo | None, decision: RiskDecision
    ) -> None: ...

    async def on_execution(self, result: ExecutionResult) -> None: ...

    async def on_error(self, context: str, error: Exception) -> None: ...


class NullNotifier:
    async def on_discovery(self, refresh: DiscoveryRefresh) -> None:
        return None

    async def on_swap(self, swap: DetectedSwap, trader: TrackedTrader) -> None:
        return None

    async def on_signal(
        self, signal: Signal, token_info: TokenInfo | None, decision: RiskDecision
    ) -> None:
        return None

    async def on_execution(self, result: ExecutionResult) -> None:
        return None

    async def on_error(self, context: str, error: Exception) -> None:
        logger.error("%s: %s", context, error)


class SmartMoneyEngine:
    def __init__(self, settings: Settings, notifier: Notifier | None = None) -> None:
        self.settings = settings
        self.notifier: Notifier = notifier or NullNotifier()
        self.database = Database(settings.database_path, settings.paper_starting_usd)
        self.rpc = SolanaRPC(
            settings.solana_rpc_url,
            max_requests_per_second=settings.rpc_requests_per_second,
            max_retries=settings.rpc_max_retries,
        )
        self.market = JupiterClient(settings.jupiter_api_key)
        self.discovery = (
            SolanaTrackerClient(settings.solana_tracker_api_key)
            if settings.solana_tracker_api_key
            else None
        )
        self.discovery_policy = DiscoveryPolicy.from_settings(settings)
        self.detector = SwapDetector(self.market, settings.min_source_trade_usd)
        self.strategy = ConsensusStrategy(
            self.database,
            minimum_traders=settings.consensus_min_traders,
            window_seconds=settings.consensus_window_seconds,
            cooldown_seconds=settings.signal_cooldown_seconds,
            minimum_trader_score=settings.min_trader_score,
        )
        self.risk = RiskEngine(settings, self.database)
        self.executor = ExecutionManager(settings, self.database, self.market)
        self._task: asyncio.Task[None] | None = None
        self._scan_lock = asyncio.Lock()
        self._discovery_lock = asyncio.Lock()
        self._initialized = False
        self.last_scan_started_at: int | None = None
        self.last_scan_finished_at: int | None = None
        self.last_error: str | None = None
        self.last_discovery_refresh_at: int | None = None

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self.database.connect()
        if self.settings.discord_alert_channel_id:
            await self.database.set_setting(
                "alert_channel_id", str(self.settings.discord_alert_channel_id)
            )
        raw_refresh = await self.database.get_setting("discovery_last_refresh")
        self.last_discovery_refresh_at = int(raw_refresh) if raw_refresh else None
        self._initialized = True

    async def start(self) -> None:
        await self.initialize()
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop(), name="smart-money-monitor")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.rpc.close()
        await self.market.close()
        if self.discovery:
            await self.discovery.close()
        await self.database.close()

    async def _run_loop(self) -> None:
        while True:
            try:
                paused = (await self.database.get_setting("paused", "false")) == "true"
                if not paused:
                    try:
                        await self.refresh_discovery()
                    except DiscoveryError as exc:
                        self.last_error = f"Discovery: {exc}"
                        await self.notifier.on_error("Refreshing wallet discovery", exc)
                    await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("Monitor loop failed")
                await self.notifier.on_error("Monitor loop", exc)
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def refresh_discovery(self, *, force: bool = False) -> DiscoveryRefresh | None:
        if not self.settings.auto_discovery_enabled or self.discovery is None:
            return None
        if self._discovery_lock.locked():
            return None
        now = int(time.time())
        if (
            not force
            and self.last_discovery_refresh_at is not None
            and now - self.last_discovery_refresh_at < self.settings.discovery_refresh_seconds
        ):
            return None

        async with self._discovery_lock:
            candidates = await self.discovery.top_24h(self.discovery_policy)
            if not candidates:
                raise DiscoveryError(
                    "The 24-hour feed returned no wallets matching the configured filters; "
                    "the existing watchlist was preserved"
                )
            refresh = await self.database.apply_discovery(candidates)
            self.last_discovery_refresh_at = refresh.refreshed_at
            self.last_error = None
            if refresh.added_wallets or refresh.disabled_wallets:
                await self.notifier.on_discovery(refresh)
            return refresh

    async def scan_once(self) -> dict[str, int]:
        if self._scan_lock.locked():
            return {"wallets": 0, "transactions": 0, "swaps": 0}
        async with self._scan_lock:
            self.last_scan_started_at = int(time.time())
            totals = {"wallets": 0, "transactions": 0, "swaps": 0}
            traders = await self.database.list_traders(enabled_only=True)
            for trader in traders:
                try:
                    counts = await self._sync_trader(trader)
                    totals["wallets"] += 1
                    totals["transactions"] += counts["transactions"]
                    totals["swaps"] += counts["swaps"]
                except (RpcError, JupiterError, ValueError) as exc:
                    self.last_error = f"{trader.alias}: {exc}"
                    await self.notifier.on_error(f"Scanning {trader.alias}", exc)
            try:
                await self._check_position_exits()
            except (JupiterError, ValueError) as exc:
                self.last_error = f"Risk exits: {exc}"
                await self.notifier.on_error("Checking risk exits", exc)
            self.last_scan_finished_at = int(time.time())
            return totals

    async def _sync_trader(self, trader: TrackedTrader) -> dict[str, int]:
        candidates, newest, is_bootstrap = await self._signature_candidates(trader)
        counts = {"transactions": 0, "swaps": 0}
        if not candidates or newest is None:
            return counts

        had_retryable_failure = False
        for item in reversed(candidates):
            signature = item.get("signature")
            if not signature or item.get("err") is not None:
                continue
            if await self.database.is_processed(signature):
                continue
            try:
                transaction = await self.rpc.get_transaction(signature)
            except RpcError:
                had_retryable_failure = True
                continue
            if transaction is None:
                had_retryable_failure = True
                continue

            block_time = int(item.get("blockTime") or transaction.get("blockTime") or 0)
            counts["transactions"] += 1
            swap = await self.detector.detect(
                transaction,
                wallet=trader.address,
                signature=signature,
                block_time=block_time,
            )
            if swap is not None:
                inserted = await self.database.record_swap(swap)
                if inserted:
                    counts["swaps"] += 1
                    if not is_bootstrap:
                        await self.notifier.on_swap(swap, trader)
                        await self._consider_signal(swap)
            await self.database.mark_processed(signature, trader.address, block_time)

        if not had_retryable_failure:
            await self.database.update_last_signature(trader.address, newest)
        return counts

    async def _signature_candidates(
        self, trader: TrackedTrader
    ) -> tuple[list[dict[str, object]], str | None, bool]:
        is_bootstrap = trader.last_signature is None
        cutoff = int(time.time()) - (self.settings.bootstrap_hours * 3600)
        collected: list[dict[str, object]] = []
        before: str | None = None
        newest: str | None = None

        while len(collected) < self.settings.max_backfill_transactions:
            batch = await self.rpc.get_signatures_for_address(
                trader.address,
                limit=min(100, self.settings.max_backfill_transactions - len(collected)),
                before=before,
                until=trader.last_signature if not is_bootstrap else None,
            )
            if not batch:
                break
            if newest is None:
                newest = batch[0].get("signature")

            should_stop = False
            for item in batch:
                signature = item.get("signature")
                if trader.last_signature and signature == trader.last_signature:
                    should_stop = True
                    break
                block_time = int(item.get("blockTime") or 0)
                if is_bootstrap and block_time and block_time < cutoff:
                    should_stop = True
                    break
                collected.append(item)
                if len(collected) >= self.settings.max_backfill_transactions:
                    should_stop = True
                    break
            if should_stop or len(batch) < 100:
                break
            before = batch[-1].get("signature")
            if not before:
                break

        return collected, str(newest) if newest else None, is_bootstrap

    async def _consider_signal(self, swap: DetectedSwap) -> None:
        rankings = await self.rankings()
        signal = await self.strategy.ingest(swap, rankings)
        if signal is None:
            return
        await self._process_signal(signal)

    async def _process_signal(
        self, signal: Signal, *, known_price: Decimal | None = None
    ) -> None:
        signal_id = await self.database.record_signal(signal)
        token_info: TokenInfo | None
        try:
            token_info = await self.market.token_info(signal.token_mint)
        except JupiterError:
            token_info = None
        price = known_price
        if price is None:
            try:
                price = await self.market.price(signal.token_mint)
            except JupiterError:
                price = None
        price = price or signal.reference_price_usd
        mode = await self.execution_mode()

        if mode is ExecutionMode.ALERTS:
            decision = RiskDecision(
                allowed=True,
                size_usd=Decimal("0"),
                reasons=("Alert-only mode",),
            )
        else:
            decision = await self.risk.assess(
                signal=signal,
                mode=mode,
                token_info=token_info,
                market_price_usd=price,
            )
        await self.notifier.on_signal(signal, token_info, decision)
        if not decision.allowed or price is None:
            return

        result = await self.executor.execute(
            signal_id=signal_id,
            signal=signal,
            mode=mode,
            token_info=token_info,
            market_price_usd=price,
            size_usd=decision.size_usd,
        )
        await self.notifier.on_execution(result)

    async def _check_position_exits(self) -> None:
        mode = await self.execution_mode()
        if mode is ExecutionMode.ALERTS:
            return
        if mode is ExecutionMode.PAPER:
            positions = await self.database.paper_positions()
            positions = [
                item for item in positions if item["token_mint"] != PAPER_DEMO_MINT
            ]
        else:
            positions = await self.database.live_positions()
        if not positions:
            return

        now = int(time.time())
        prices = await self.market.prices([str(item["token_mint"]) for item in positions])
        for position in positions:
            mint = str(position["token_mint"])
            price = prices.get(mint)
            if price is None or price <= 0:
                continue
            if mode is ExecutionMode.PAPER:
                average_entry = Decimal(str(position["average_entry_usd"]))
            else:
                quantity = Decimal(str(position["quantity_raw"])) / (
                    Decimal(10) ** int(position["decimals"])
                )
                if quantity <= 0:
                    continue
                average_entry = Decimal(str(position["cost_basis_usd"])) / quantity
            if average_entry <= 0:
                continue

            change_percent = ((price / average_entry) - Decimal("1")) * Decimal("100")
            age_seconds = now - int(position["opened_at"])
            reason: str | None = None
            if change_percent <= -self.settings.stop_loss_percent:
                reason = f"stop loss ({change_percent:.2f}%)"
            elif change_percent >= self.settings.take_profit_percent:
                reason = f"take profit (+{change_percent:.2f}%)"
            elif age_seconds >= self.settings.max_hold_seconds:
                reason = f"maximum hold time ({age_seconds // 3600}h)"
            if reason is None:
                continue
            if await self.database.recent_signal_exists(mint, Side.SELL, now - 60):
                continue

            signal = Signal(
                token_mint=mint,
                side=Side.SELL,
                created_at=now,
                trader_addresses=("RISK_ENGINE",),
                trader_aliases=(f"Risk engine: {reason}",),
                source_signatures=(f"risk-{mint}-{now}",),
                combined_score=Decimal("100"),
                reference_price_usd=price,
            )
            await self._process_signal(signal, known_price=price)

    async def rankings(self) -> list[ScoredTrader]:
        metrics_24h, metrics_7d = await asyncio.gather(
            self.database.metrics(86_400), self.database.metrics(604_800)
        )
        local_rankings = rank_traders(metrics_24h, metrics_7d)
        discovered = await self.database.list_discovered(limit=50)
        merged = {item.metrics_24h.address: item for item in local_rankings}
        for candidate in discovered:
            local = merged.get(candidate.address)
            wins = int(
                Decimal(candidate.closed_tokens)
                * candidate.win_rate_percent
                / Decimal("100")
            )
            losses = max(0, candidate.closed_tokens - wins)
            external_metrics = TraderMetrics(
                address=candidate.address,
                alias=candidate.alias,
                window_seconds=86_400,
                trades=candidate.trades_24h,
                buys=candidate.buys_24h,
                sells=candidate.sells_24h,
                wins=wins,
                losses=losses,
                realized_pnl_usd=candidate.realized_pnl_24h,
                matched_cost_usd=candidate.invested_24h_usd,
                volume_usd=candidate.volume_24h_usd,
                max_drawdown_usd=Decimal("0"),
            )
            if local is None:
                zero_week = replace(
                    external_metrics,
                    window_seconds=604_800,
                    trades=0,
                    buys=0,
                    sells=0,
                    wins=0,
                    losses=0,
                    realized_pnl_usd=Decimal("0"),
                    matched_cost_usd=Decimal("0"),
                    volume_usd=Decimal("0"),
                )
                merged[candidate.address] = ScoredTrader(
                    metrics_24h=external_metrics,
                    metrics_7d=zero_week,
                    score=candidate.score,
                )
                continue

            closed_local = local.metrics_24h.wins + local.metrics_24h.losses
            if local.metrics_24h.trades >= 10 and closed_local >= 3:
                blended_score = (
                    local.score * Decimal("0.60")
                    + candidate.score * Decimal("0.40")
                ).quantize(Decimal("0.01"))
            else:
                blended_score = candidate.score
            merged[candidate.address] = ScoredTrader(
                metrics_24h=external_metrics,
                metrics_7d=local.metrics_7d,
                score=blended_score,
            )

        return sorted(
            merged.values(),
            key=lambda item: (
                item.score,
                item.metrics_24h.realized_pnl_usd,
                item.metrics_24h.trades,
            ),
            reverse=True,
        )

    async def execution_mode(self) -> ExecutionMode:
        raw = await self.database.get_setting("mode", ExecutionMode.PAPER.value)
        try:
            return ExecutionMode(raw or ExecutionMode.PAPER.value)
        except ValueError:
            return ExecutionMode.PAPER

    async def set_execution_mode(self, mode: ExecutionMode) -> None:
        if mode is ExecutionMode.LIVE and not self.settings.live_is_unlocked:
            raise ValueError("Live mode is not unlocked by environment configuration")
        await self.database.set_setting("mode", mode.value)

    async def set_paused(self, paused: bool) -> None:
        await self.database.set_setting("paused", "true" if paused else "false")

    async def is_paused(self) -> bool:
        return (await self.database.get_setting("paused", "false")) == "true"

    async def paper_summary(self) -> PaperSummary:
        positions = await self.database.paper_positions()
        mints = [
            item["token_mint"]
            for item in positions
            if item["token_mint"] != PAPER_DEMO_MINT
        ]
        try:
            prices = await self.market.prices(mints) if mints else {}
        except JupiterError:
            prices = {}
        if any(item["token_mint"] == PAPER_DEMO_MINT for item in positions):
            prices[PAPER_DEMO_MINT] = Decimal(PAPER_DEMO_ENTRY_PRICE_USD)
        return await self.database.paper_summary(prices)

    async def status(self) -> dict[str, object]:
        try:
            rpc_health = await self.rpc.health()
        except RpcError as exc:
            rpc_health = f"error: {exc}"
        return {
            "rpc": rpc_health,
            "mode": (await self.execution_mode()).value,
            "paused": await self.is_paused(),
            "wallets": len(await self.database.list_traders(enabled_only=True)),
            "last_scan": self.last_scan_finished_at,
            "last_error": self.last_error,
            "live_unlocked": self.settings.live_is_unlocked,
            "discovery_enabled": self.settings.auto_discovery_enabled,
            "discovery_configured": self.settings.discovery_is_configured,
            "discovery_last_refresh": self.last_discovery_refresh_at,
            "discovered_wallets": len(await self.database.list_discovered(limit=50)),
        }
