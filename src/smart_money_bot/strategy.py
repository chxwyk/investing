from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal

from .database import Database
from .models import DetectedSwap, ScoredTrader, Side, Signal


@dataclass(frozen=True, slots=True)
class _Event:
    swap: DetectedSwap
    alias: str
    score: Decimal


class ConsensusStrategy:
    def __init__(
        self,
        database: Database,
        *,
        minimum_traders: int,
        window_seconds: int,
        cooldown_seconds: int,
        minimum_trader_score: Decimal,
    ) -> None:
        self.database = database
        self.minimum_traders = minimum_traders
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self.minimum_trader_score = minimum_trader_score
        self._events: dict[tuple[str, str], deque[_Event]] = defaultdict(deque)

    async def ingest(self, swap: DetectedSwap, rankings: list[ScoredTrader]) -> Signal | None:
        ranking = {item.metrics_24h.address: item for item in rankings}
        scored = ranking.get(swap.trader_address)
        trader = await self.database.resolve_trader(swap.trader_address)
        if not scored or not trader:
            return None
        adjusted_score = min(Decimal("100"), scored.score * trader.weight)
        if adjusted_score < self.minimum_trader_score:
            return None

        now = int(time.time())
        key = (swap.token_mint, swap.side.value)
        events = self._events[key]
        events.append(_Event(swap=swap, alias=scored.metrics_24h.alias, score=adjusted_score))
        cutoff = now - self.window_seconds
        while events and events[0].swap.block_time < cutoff:
            events.popleft()

        # One most-recent event per wallet prevents a single wallet from manufacturing consensus.
        unique: dict[str, _Event] = {}
        for event in events:
            unique[event.swap.trader_address] = event
        selected = sorted(unique.values(), key=lambda item: item.swap.block_time, reverse=True)
        required_traders = self.minimum_traders if swap.side is Side.BUY else 1
        if len(selected) < required_traders:
            return None

        if await self.database.recent_signal_exists(
            swap.token_mint, swap.side, now - self.cooldown_seconds
        ):
            return None

        chosen = selected[: max(required_traders, 5)]
        combined_score = sum((item.score for item in chosen), Decimal("0")) / Decimal(len(chosen))
        prices = [
            item.swap.token_price_usd for item in chosen if item.swap.token_price_usd is not None
        ]
        reference_price = prices[0] if prices else None
        return Signal(
            token_mint=swap.token_mint,
            side=swap.side,
            created_at=now,
            trader_addresses=tuple(item.swap.trader_address for item in chosen),
            trader_aliases=tuple(item.alias for item in chosen),
            source_signatures=tuple(item.swap.signature for item in chosen),
            combined_score=combined_score.quantize(Decimal("0.01")),
            reference_price_usd=reference_price,
        )
