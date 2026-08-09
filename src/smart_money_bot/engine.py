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
from .discovery import (
    DiscoveryPolicy,
    SolanaTrackerClient,
    WindowCandidate,
    merge_verified_windows,
)
from .errors import DiscoveryError, JupiterError, RpcError
from .executor import ExecutionManager
from .market import JupiterClient
from .models import (
    DetectedSwap,
    DiscoveryCandidate,
    DiscoveryRefresh,
    ExecutionMode,
    ExecutionResult,
    PaperReadiness,
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
from .rotation import CandidateRotator, RotationResult, is_pump_mint
from .rpc import SolanaRPC
from .scoring import rank_traders
from .strategy import ConsensusStrategy
from .stream import RealtimeWalletStream, StreamEvent

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
        self.rotator = CandidateRotator(settings, self.rpc, self.detector)
        self.stream = RealtimeWalletStream(
            self.database,
            rpc_url=settings.solana_rpc_url,
            explicit_ws_url=settings.solana_ws_url,
            enabled=settings.realtime_wallet_stream_enabled,
        )
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
        self._stream_task: asyncio.Task[None] | None = None
        self._stream_consumer_task: asyncio.Task[None] | None = None
        self._scan_lock = asyncio.Lock()
        self._discovery_lock = asyncio.Lock()
        self._processing_signatures: set[str] = set()
        self._initialized = False
        self.last_scan_started_at: int | None = None
        self.last_scan_finished_at: int | None = None
        self.last_error: str | None = None
        self.last_discovery_refresh_at: int | None = None
        self.last_weekly_refresh_at: int | None = None
        self.last_rotation_at: int | None = None
        self.last_rotation_result: RotationResult | None = None
        self._weekly_pool: list[WindowCandidate] = []
        self._candidate_pool = []

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
        raw_weekly = await self.database.get_setting("discovery_7d_last_refresh")
        self.last_weekly_refresh_at = int(raw_weekly) if raw_weekly else None
        raw_rotation = await self.database.get_setting("rotation_last_refresh")
        self.last_rotation_at = int(raw_rotation) if raw_rotation else None
        self._initialized = True

    async def start(self) -> None:
        await self.initialize()
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop(), name="smart-money-monitor")
        if self.stream.enabled:
            self._stream_task = asyncio.create_task(
                self.stream.run(), name="smart-money-wallet-stream"
            )
            self._stream_consumer_task = asyncio.create_task(
                self._consume_stream_events(), name="smart-money-stream-consumer"
            )

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for task in (self._stream_task, self._stream_consumer_task):
            if task:
                task.cancel()
        for task in (self._stream_task, self._stream_consumer_task):
            if task:
                with suppress(asyncio.CancelledError):
                    await task
        self._stream_task = None
        self._stream_consumer_task = None
        await self.stream.close()
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
                    try:
                        await self.rotate_wallets()
                    except DiscoveryError as exc:
                        self.last_error = f"Rotation: {exc}"
                        await self.notifier.on_error("Rotating hot wallets", exc)
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
            and self._candidate_pool
            and self.last_discovery_refresh_at is not None
            and now - self.last_discovery_refresh_at
            < self.settings.effective_discovery_refresh_seconds
        ):
            return None

        async with self._discovery_lock:
            refresh_weekly = (
                force
                or not self._weekly_pool
                or self.last_weekly_refresh_at is None
                or now - self.last_weekly_refresh_at
                >= self.settings.effective_discovery_7d_refresh_seconds
            )
            if refresh_weekly:
                self._weekly_pool = await self.discovery.weekly_pool(
                    self.discovery_policy
                )
                if self.discovery_policy.include_kols:
                    try:
                        self._weekly_pool.extend(
                            await self.discovery.kol_weekly_pool(self.discovery_policy)
                        )
                    except DiscoveryError as exc:
                        logger.warning(
                            "Public-KOL 7D discovery unavailable; using general feed: %s",
                            exc,
                        )
                if not self._weekly_pool:
                    raise DiscoveryError(
                        "The strict 7-day feed returned no qualifying wallets; "
                        "the existing hot set was preserved"
                    )
                self.last_weekly_refresh_at = int(time.time())
                await self.database.set_setting(
                    "discovery_7d_last_refresh", str(self.last_weekly_refresh_at)
                )

            daily_pool = await self.discovery.daily_pool(self.discovery_policy)
            if self.discovery_policy.include_kols:
                try:
                    daily_pool.extend(
                        await self.discovery.kol_daily_pool(self.discovery_policy)
                    )
                except DiscoveryError as exc:
                    logger.warning(
                        "Public-KOL 24H discovery unavailable; using general feed: %s",
                        exc,
                    )
            candidates = merge_verified_windows(
                daily_pool, self._weekly_pool, self.discovery_policy
            )
            if not candidates:
                raise DiscoveryError(
                    "No wallets were independently profitable in both strict 24-hour "
                    "and 7-day feeds; the existing hot set was preserved"
                )
            self._candidate_pool = candidates
            self.last_discovery_refresh_at = int(time.time())
            await self.database.set_setting(
                "discovery_last_refresh", str(self.last_discovery_refresh_at)
            )
            self.last_error = None
        return await self.rotate_wallets(force=True)

    async def rotate_wallets(self, *, force: bool = False) -> DiscoveryRefresh | None:
        if not self._candidate_pool:
            return None
        now = int(time.time())
        if (
            not force
            and self.last_rotation_at is not None
            and now - self.last_rotation_at < self.settings.rotation_refresh_seconds
        ):
            return None
        eligible, forward_rejections, forward_evaluated = (
            await self._apply_forward_paper_evidence(self._candidate_pool)
        )
        if not eligible:
            self.last_rotation_result = RotationResult(
                selected=(),
                evaluated=tuple(forward_evaluated),
                rejection_reasons=forward_rejections,
                pool_size=len(self._candidate_pool),
                verified_pump_wallets=0,
            )
            raise DiscoveryError(
                "Every candidate failed mature forward PAPER evidence; the existing hot set "
                "was preserved"
            )
        raw_result = await self.rotator.evaluate(eligible, now=now)
        current_hot_set = await self.database.list_discovered(limit=50)
        current_pool_addresses = {candidate.address for candidate in self._candidate_pool}
        feed_removed = tuple(
            candidate
            for candidate in current_hot_set
            if candidate.address not in current_pool_addresses
        )
        feed_rejections = {
            candidate.address: (
                "no longer present in the current dual-window qualifying pool; "
                "the 24H/7D feed filters or ranking changed"
            )
            for candidate in feed_removed
        }
        result = RotationResult(
            selected=raw_result.selected,
            evaluated=(
                raw_result.evaluated + tuple(forward_evaluated) + feed_removed
            ),
            rejection_reasons={
                **feed_rejections,
                **raw_result.rejection_reasons,
                **forward_rejections,
            },
            pool_size=len(self._candidate_pool),
            verified_pump_wallets=raw_result.verified_pump_wallets,
        )
        self.last_rotation_result = result
        if not result.selected:
            raise DiscoveryError(
                "No dual-window profitable wallets passed the recent Pump activity checks; "
                "the existing hot set was preserved"
            )
        refresh = await self.database.apply_discovery(
            list(result.selected),
            evaluated_candidates=list(result.evaluated),
            removal_reasons=result.rejection_reasons,
            candidate_pool_size=result.pool_size,
            verified_pump_wallets=result.verified_pump_wallets,
        )
        self.last_rotation_at = refresh.refreshed_at
        self.last_error = None
        if refresh.added_wallets or refresh.disabled_wallets:
            await self.notifier.on_discovery(refresh)
        return refresh

    async def _apply_forward_paper_evidence(
        self, candidates: list[DiscoveryCandidate]
    ) -> tuple[
        list[DiscoveryCandidate], dict[str, str], list[DiscoveryCandidate]
    ]:
        """Penalize proven forward losers without judging brand-new candidates early."""

        performance = await self.database.paper_wallet_performance(
            [candidate.address for candidate in candidates]
        )
        eligible = []
        rejected: dict[str, str] = {}
        evaluated = []
        for candidate in candidates:
            metrics = performance.get(candidate.address)
            if metrics is None or int(metrics["closed_sells"]) < (
                self.settings.forward_evidence_min_closed_sells
            ):
                eligible.append(candidate)
                continue
            closed_sells = int(metrics["closed_sells"])
            pnl = Decimal(metrics["pnl"])
            profit_factor = Decimal(metrics["profit_factor"])
            reason: str | None = None
            if pnl <= -self.settings.forward_evidence_max_loss_usd:
                reason = (
                    f"forward PAPER failed after {closed_sells} exits: PnL ${pnl:,.2f} "
                    f"breached -${self.settings.forward_evidence_max_loss_usd:,.2f}"
                )
            elif profit_factor < self.settings.forward_evidence_min_profit_factor:
                reason = (
                    f"forward PAPER failed after {closed_sells} exits: profit factor "
                    f"{profit_factor:.2f} is below "
                    f"{self.settings.forward_evidence_min_profit_factor:.2f}"
                )
            if reason is not None:
                rejected[candidate.address] = reason
                evaluated.append(candidate)
                continue

            forward_bonus = min(
                Decimal("5"),
                max(Decimal("0"), (profit_factor - Decimal("1")) * Decimal("2")),
            )
            eligible.append(
                replace(
                    candidate,
                    score=min(Decimal("100"), candidate.score + forward_bonus),
                    selection_reason=(
                        f"{candidate.selection_reason}; forward PAPER {closed_sells} exits, "
                        f"${pnl:,.2f}, PF {profit_factor:.2f}"
                    ),
                )
            )
        return eligible, rejected, evaluated

    async def _consume_stream_events(self) -> None:
        while True:
            event = await self.stream.events.get()
            try:
                if await self.is_paused():
                    continue
                await self._process_stream_event(event)
            except asyncio.CancelledError:
                raise
            except (RpcError, JupiterError, ValueError) as exc:
                self.last_error = f"Realtime stream: {exc}"
                await self.notifier.on_error("Processing realtime wallet event", exc)
            finally:
                self.stream.events.task_done()

    async def _process_stream_event(self, event: StreamEvent) -> None:
        trader = await self.database.resolve_trader(event.wallet)
        if trader is None or not trader.enabled or trader.last_signature is None:
            return
        if await self.database.is_processed(event.signature):
            return
        transaction = None
        for attempt in range(3):
            transaction = await self.rpc.get_transaction(event.signature)
            if transaction is not None:
                break
            await asyncio.sleep(attempt + 1)
        if transaction is None:
            raise RpcError("realtime transaction was unavailable after confirmation retries")
        block_time = int(transaction.get("blockTime") or time.time())
        await self._process_transaction(
            trader,
            signature=event.signature,
            transaction=transaction,
            block_time=block_time,
            is_bootstrap=False,
        )

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
            processed = await self._process_transaction(
                trader,
                signature=str(signature),
                transaction=transaction,
                block_time=block_time,
                is_bootstrap=is_bootstrap,
            )
            counts["transactions"] += processed["transactions"]
            counts["swaps"] += processed["swaps"]

        if not had_retryable_failure:
            await self.database.update_last_signature(trader.address, newest)
        return counts

    async def _process_transaction(
        self,
        trader: TrackedTrader,
        *,
        signature: str,
        transaction: dict,
        block_time: int,
        is_bootstrap: bool,
    ) -> dict[str, int]:
        # The websocket stream and polling fallback can observe the same confirmed
        # transaction concurrently. Keep one task responsible for it so alerts and
        # paper mirror attempts are never duplicated inside this process.
        if signature in self._processing_signatures:
            return {"transactions": 0, "swaps": 0}
        self._processing_signatures.add(signature)
        try:
            if await self.database.is_processed(signature):
                return {"transactions": 0, "swaps": 0}
            swap = await self.detector.detect(
                transaction,
                wallet=trader.address,
                signature=signature,
                block_time=block_time,
            )
            swap_count = 0
            if swap is not None:
                inserted = await self.database.record_swap(swap)
                swap_count = int(inserted)
                should_handle = inserted
                if (
                    not inserted
                    and not is_bootstrap
                    and self.settings.paper_mirror_raw_swaps
                    and await self.execution_mode() is ExecutionMode.PAPER
                ):
                    should_handle = not await self.database.has_paper_mirror_execution(
                        swap.signature
                    )
                if should_handle and not is_bootstrap:
                    await self._handle_new_swap(swap, trader)
            await self.database.mark_processed(signature, trader.address, block_time)
            return {"transactions": 1, "swaps": swap_count}
        finally:
            self._processing_signatures.discard(signature)

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

    async def _handle_new_swap(
        self, swap: DetectedSwap, trader: TrackedTrader
    ) -> None:
        await self.notifier.on_swap(swap, trader)
        mode = await self.execution_mode()
        if mode is ExecutionMode.PAPER and self.settings.paper_mirror_raw_swaps:
            await self._mirror_paper_swap(swap, trader)
        else:
            await self._consider_signal(swap)

    async def _mirror_paper_swap(
        self, swap: DetectedSwap, trader: TrackedTrader
    ) -> None:
        # A tracked wallet's transaction price is historical by the time this process
        # observes it. Price the shadow fill at detection time so PAPER results include
        # the latency that live copy execution would face.
        try:
            price = await self.market.price(swap.token_mint)
        except JupiterError:
            price = None
        pump_source_fallback = False
        if price is None or price <= 0:
            source_price = swap.token_price_usd
            if (
                self.settings.paper_allow_pump_source_fallback
                and is_pump_mint(swap.token_mint)
                and source_price is not None
                and source_price > 0
            ):
                penalty = Decimal(self.settings.paper_pump_source_fallback_bps) / Decimal(
                    10_000
                )
                price = (
                    source_price * (Decimal("1") + penalty)
                    if swap.side is Side.BUY
                    else source_price * (Decimal("1") - penalty)
                )
                pump_source_fallback = True
            elif not self.settings.paper_require_current_price:
                price = source_price
        size = min(self.settings.default_copy_usd, self.settings.max_copy_usd)
        if price is None or price <= 0:
            result = ExecutionResult(
                success=False,
                mode=ExecutionMode.PAPER,
                token_mint=swap.token_mint,
                side=swap.side,
                size_usd=size,
                message=(
                    "Skipped: no current Jupiter price was available for a realistic "
                    "paper fill"
                ),
            )
            await self.database.log_execution(
                signal_id=None,
                mode=result.mode,
                token_mint=result.token_mint,
                side=result.side,
                size_usd=result.size_usd,
                success=result.success,
                signature=None,
                message=result.message,
            )
        else:
            token_info: TokenInfo | None = None
            if swap.side is Side.BUY:
                try:
                    token_info = await self.market.token_info(swap.token_mint)
                except JupiterError:
                    token_info = None
            if swap.side is Side.BUY and self.settings.paper_raw_entry_filter_enabled:
                signal = Signal(
                    token_mint=swap.token_mint,
                    side=Side.BUY,
                    created_at=swap.block_time or int(time.time()),
                    trader_addresses=(trader.address,),
                    trader_aliases=(trader.alias,),
                    source_signatures=(swap.signature,),
                    combined_score=Decimal("100"),
                    reference_price_usd=price,
                )
                already_open = await self.database.has_paper_mirror_position(
                    trader.address, swap.token_mint
                )
                decision = await self.risk.assess(
                    signal=signal,
                    mode=ExecutionMode.PAPER,
                    token_info=token_info,
                    market_price_usd=price,
                    require_consensus=False,
                    enforce_position_limit=not already_open,
                )
                size = decision.size_usd
                if not decision.allowed:
                    reasons = "; ".join(decision.reasons) or "risk policy blocked entry"
                    result = ExecutionResult(
                        success=False,
                        mode=ExecutionMode.PAPER,
                        token_mint=swap.token_mint,
                        side=swap.side,
                        size_usd=size,
                        message=f"Skipped: paper entry guard — {reasons}",
                    )
                    await self.database.log_execution(
                        signal_id=None,
                        mode=result.mode,
                        token_mint=result.token_mint,
                        side=result.side,
                        size_usd=result.size_usd,
                        success=result.success,
                        signature=None,
                        message=result.message,
                    )
                    await self.notifier.on_execution(result)
                    return
            result = await self.executor.execute_paper_mirror(
                swap=swap,
                trader=trader,
                market_price_usd=price,
                size_usd=size,
                token_info=token_info,
                pump_source_fallback=pump_source_fallback,
            )
        await self.notifier.on_execution(result)

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
            strategy_positions = await self.database.paper_positions()
            strategy_positions = [
                item
                for item in strategy_positions
                if item["token_mint"] != PAPER_DEMO_MINT
            ]
            mirror_positions = await self.database.paper_mirror_positions()
            positions = strategy_positions + mirror_positions
        else:
            positions = await self.database.live_positions()
        if not positions:
            return

        now = int(time.time())
        prices = await self.market.prices(
            list(dict.fromkeys(str(item["token_mint"]) for item in positions))
        )
        if mode is ExecutionMode.PAPER:
            await self._check_strategy_paper_exits(strategy_positions, prices, now)
            await self._check_raw_mirror_exits(mirror_positions, prices, now)
            await self.database.paper_summary(prices)
            return

        for position in positions:
            mint = str(position["token_mint"])
            price = prices.get(mint)
            if price is None or price <= 0:
                continue
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

    async def _check_strategy_paper_exits(
        self,
        positions: list[dict[str, object]],
        prices: dict[str, Decimal],
        now: int,
    ) -> None:
        for position in positions:
            mint = str(position["token_mint"])
            price = prices.get(mint)
            average_entry = Decimal(str(position["average_entry_usd"]))
            if price is None or price <= 0 or average_entry <= 0:
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

    async def _check_raw_mirror_exits(
        self,
        positions: list[dict[str, object]],
        prices: dict[str, Decimal],
        now: int,
    ) -> None:
        for position in positions:
            mint = str(position["token_mint"])
            trader_address = str(position["trader_address"])
            price = prices.get(mint)
            average_entry = Decimal(str(position["average_entry_usd"]))
            if price is None or price <= 0 or average_entry <= 0:
                continue

            peak = await self.database.update_paper_mirror_peak(
                trader_address, mint, price
            )
            change_percent = ((price / average_entry) - Decimal("1")) * Decimal("100")
            peak_gain_percent = ((peak / average_entry) - Decimal("1")) * Decimal("100")
            pullback_percent = ((price / peak) - Decimal("1")) * Decimal("100")
            age_seconds = now - int(position["opened_at"])

            reason: str | None = None
            if change_percent <= -self.settings.raw_mirror_stop_loss_percent:
                reason = f"hard stop ({change_percent:.2f}%)"
            elif change_percent >= self.settings.raw_mirror_take_profit_percent:
                reason = f"take profit (+{change_percent:.2f}%)"
            elif (
                peak_gain_percent
                >= self.settings.raw_mirror_trailing_activation_percent
                and pullback_percent <= -self.settings.raw_mirror_trailing_stop_percent
            ):
                reason = (
                    f"trailing-profit lock (peak +{peak_gain_percent:.2f}%, "
                    f"pullback {pullback_percent:.2f}%)"
                )
            elif age_seconds >= self.settings.raw_mirror_max_hold_seconds:
                reason = f"maximum raw hold time ({age_seconds // 60}m)"
            if reason is None:
                continue

            result = await self.executor.execute_paper_mirror_risk_exit(
                position=position,
                market_price_usd=price,
                reason=reason,
            )
            await self.notifier.on_execution(result)

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
            weekly_wins = int(
                Decimal(max(candidate.trades_7d, 0))
                * candidate.win_rate_7d_percent
                / Decimal("100")
            )
            weekly_losses = max(0, candidate.trades_7d - weekly_wins)
            weekly_cost = (
                candidate.realized_pnl_7d
                / (candidate.roi_7d_percent / Decimal("100"))
                if candidate.roi_7d_percent > 0
                else Decimal("0")
            )
            external_week = TraderMetrics(
                address=candidate.address,
                alias=candidate.alias,
                window_seconds=604_800,
                trades=candidate.trades_7d,
                buys=0,
                sells=0,
                wins=weekly_wins,
                losses=weekly_losses,
                realized_pnl_usd=candidate.realized_pnl_7d,
                matched_cost_usd=weekly_cost,
                volume_usd=Decimal("0"),
                max_drawdown_usd=Decimal("0"),
            )
            if local is None:
                merged[candidate.address] = ScoredTrader(
                    metrics_24h=external_metrics,
                    metrics_7d=external_week,
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
                metrics_7d=external_week,
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
        positions = await self.database.paper_all_positions()
        mints = sorted(
            {
                item["token_mint"]
                for item in positions
                if item["token_mint"] != PAPER_DEMO_MINT
            }
        )
        try:
            prices = await self.market.prices(mints) if mints else {}
        except JupiterError:
            prices = {}
        if any(item["token_mint"] == PAPER_DEMO_MINT for item in positions):
            prices[PAPER_DEMO_MINT] = Decimal(PAPER_DEMO_ENTRY_PRICE_USD)
        return await self.database.paper_summary(prices)

    async def paper_readiness(self) -> PaperReadiness:
        return await self.database.paper_readiness(
            min_active_days=self.settings.readiness_min_active_days,
            min_closed_trades=self.settings.readiness_min_closed_trades,
            min_profit_factor=self.settings.readiness_min_profit_factor,
            max_drawdown_percent=self.settings.readiness_max_drawdown_percent,
            min_quote_success_percent=self.settings.readiness_min_quote_success_percent,
        )

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
            "discovery_7d_last_refresh": self.last_weekly_refresh_at,
            "rotation_last_refresh": self.last_rotation_at,
            "candidate_pool_size": len(self._candidate_pool),
            "kol_discovery_enabled": self.settings.discovery_include_kols,
            "rotation_verified_pump_wallets": (
                self.last_rotation_result.verified_pump_wallets
                if self.last_rotation_result
                else None
            ),
            "discovered_wallets": len(await self.database.list_discovered(limit=50)),
            "stream_enabled": self.stream.enabled,
            "stream_connected": self.stream.connected,
            "stream_subscriptions": self.stream.subscription_count,
            "stream_last_event": self.stream.last_event_at,
            "stream_last_error": self.stream.last_error,
            "stream_reconnects": self.stream.reconnects,
            "paper_mirror_raw_swaps": self.settings.paper_mirror_raw_swaps,
            "paper_use_executable_quotes": self.settings.paper_use_executable_quotes,
            "quote_ready": bool(self.settings.jupiter_api_key),
            "consecutive_quote_failures": self.executor.consecutive_quote_failures,
        }
