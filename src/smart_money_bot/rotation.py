from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Protocol

from .config import Settings
from .detector import SwapDetector
from .errors import RpcError
from .models import DiscoveryCandidate

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


class RotationRPC(Protocol):
    async def get_signatures_for_address(
        self,
        address: str,
        *,
        limit: int = 100,
        before: str | None = None,
        until: str | None = None,
    ) -> list[dict]: ...

    async def get_transaction(self, signature: str) -> dict | None: ...


@dataclass(frozen=True, slots=True)
class RotationResult:
    selected: tuple[DiscoveryCandidate, ...]
    evaluated: tuple[DiscoveryCandidate, ...]
    rejection_reasons: dict[str, str]
    pool_size: int
    verified_pump_wallets: int


class CandidateRotator:
    """Verifies recent Pump activity before a profitable wallet enters the hot set."""

    def __init__(
        self,
        settings: Settings,
        rpc: RotationRPC,
        detector: SwapDetector,
    ) -> None:
        self.settings = settings
        self.rpc = rpc
        self.detector = detector
        self._semaphore = asyncio.Semaphore(max(1, min(settings.rpc_requests_per_second, 12)))

    async def evaluate(
        self,
        candidates: list[DiscoveryCandidate],
        *,
        now: int | None = None,
    ) -> RotationResult:
        current_time = int(time.time()) if now is None else now
        probes = await asyncio.gather(
            *(self._probe(candidate, current_time) for candidate in candidates)
        )
        evaluated = [candidate for candidate, _ in probes]
        rejection_reasons = {
            candidate.address: reason for candidate, reason in probes if reason is not None
        }
        qualified = [candidate for candidate, reason in probes if reason is None]
        qualified.sort(key=_hot_sort_key, reverse=True)
        selected = qualified[: self.settings.discovery_max_wallets]
        selected_addresses = {candidate.address for candidate in selected}
        for candidate in qualified:
            if candidate.address not in selected_addresses:
                rejection_reasons[candidate.address] = (
                    "still profitable and Pump-verified, but outranked by the current hot set"
                )

        ranked = [
            replace(candidate, rank=index) for index, candidate in enumerate(selected, start=1)
        ]
        return RotationResult(
            selected=tuple(ranked),
            evaluated=tuple(evaluated),
            rejection_reasons=rejection_reasons,
            pool_size=len(candidates),
            verified_pump_wallets=len(qualified),
        )

    async def _probe(
        self, candidate: DiscoveryCandidate, now: int
    ) -> tuple[DiscoveryCandidate, str | None]:
        try:
            async with self._semaphore:
                signatures = await self.rpc.get_signatures_for_address(
                    candidate.address,
                    limit=self.settings.rotation_probe_transactions,
                )
        except RpcError as exc:
            return candidate, f"activity verification failed: {exc}"

        cutoff = now - self.settings.rotation_max_idle_seconds
        recent = [
            item
            for item in signatures
            if item.get("err") is None and int(item.get("blockTime") or 0) >= cutoff
        ]
        swaps = 0
        pump_swaps = 0
        last_activity_at: int | None = None
        for item in recent:
            signature = str(item.get("signature") or "")
            if not signature:
                continue
            try:
                async with self._semaphore:
                    transaction = await self.rpc.get_transaction(signature)
            except RpcError:
                continue
            if transaction is None:
                continue
            block_time = int(item.get("blockTime") or transaction.get("blockTime") or 0)
            swap = await self.detector.detect(
                transaction,
                wallet=candidate.address,
                signature=signature,
                block_time=block_time,
            )
            if swap is None:
                continue
            swaps += 1
            last_activity_at = max(last_activity_at or 0, block_time)
            if is_pump_trade(transaction, swap.token_mint):
                pump_swaps += 1

        activity_bonus = min(Decimal(swaps) * Decimal("0.50"), Decimal("4"))
        pump_bonus = min(Decimal(pump_swaps), Decimal("3"))
        evidence = candidate.selection_reason or "strict 24H/7D profit verified"
        updated = replace(
            candidate,
            score=min(Decimal("100"), candidate.score + activity_bonus + pump_bonus),
            recent_swaps=swaps,
            pump_swaps=pump_swaps,
            last_activity_at=last_activity_at,
            selection_reason=(f"{evidence}; {swaps} recent swaps ({pump_swaps} Pump)"),
        )
        if swaps < self.settings.rotation_min_recent_swaps:
            return updated, (
                f"inactive: only {swaps} qualifying swaps inside the "
                f"last {self.settings.rotation_max_idle_seconds // 60}m"
            )
        if (
            self.settings.rotation_require_pump_activity
            and pump_swaps < self.settings.rotation_min_pump_swaps
        ):
            return updated, (
                f"not Pump-verified: {pump_swaps} recent Pump swaps; "
                f"minimum is {self.settings.rotation_min_pump_swaps}"
            )
        return updated, None


def is_pump_mint(mint: str) -> bool:
    """Pump-created mint addresses retain their conventional `pump` suffix."""

    return mint.lower().endswith("pump")


def is_pump_trade(transaction: dict, mint: str) -> bool:
    """Recognize native Pump instructions or the persistent mint of a graduated coin."""

    if is_pump_mint(mint):
        return True
    message = (transaction.get("transaction") or {}).get("message") or {}
    instructions = list(message.get("instructions") or [])
    meta = transaction.get("meta") or {}
    for group in meta.get("innerInstructions") or []:
        if isinstance(group, dict):
            instructions.extend(group.get("instructions") or [])
    for instruction in instructions:
        if isinstance(instruction, dict) and instruction.get("programId") == PUMP_PROGRAM_ID:
            return True
    return False


def _hot_sort_key(
    candidate: DiscoveryCandidate,
) -> tuple[Decimal, int, int, int, Decimal, Decimal]:
    return (
        candidate.score,
        candidate.pump_swaps,
        candidate.recent_swaps,
        candidate.last_activity_at or 0,
        candidate.realized_pnl_24h,
        candidate.realized_pnl_7d,
    )
