from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any, Protocol

from .constants import QUOTE_MINTS, STABLE_MINTS, WRAPPED_SOL_MINT
from .errors import JupiterError
from .models import DetectedSwap, Side

LAMPORTS_PER_SOL = Decimal("1000000000")
TOKEN_DUST = Decimal("0.000000001")


class PriceProvider(Protocol):
    async def price(self, mint: str) -> Decimal | None: ...


class SwapDetector:
    def __init__(self, market: PriceProvider, min_trade_usd: Decimal) -> None:
        self.market = market
        self.min_trade_usd = min_trade_usd

    async def detect(
        self,
        transaction: dict[str, Any],
        *,
        wallet: str,
        signature: str,
        block_time: int,
    ) -> DetectedSwap | None:
        meta = transaction.get("meta") or {}
        if meta.get("err") is not None:
            return None

        pre = _owned_token_balances(meta.get("preTokenBalances") or [], wallet)
        post = _owned_token_balances(meta.get("postTokenBalances") or [], wallet)
        deltas: dict[str, Decimal] = {}
        for mint in pre.keys() | post.keys():
            delta = post.get(mint, Decimal("0")) - pre.get(mint, Decimal("0"))
            if abs(delta) > TOKEN_DUST:
                deltas[mint] = delta

        native_delta = _native_sol_delta(transaction, wallet)
        if abs(native_delta) > Decimal("0.000001"):
            deltas[WRAPPED_SOL_MINT] = deltas.get(WRAPPED_SOL_MINT, Decimal("0")) + native_delta

        quote_deltas = {mint: delta for mint, delta in deltas.items() if mint in QUOTE_MINTS}
        asset_deltas = {mint: delta for mint, delta in deltas.items() if mint not in QUOTE_MINTS}
        positive_assets = [(mint, delta) for mint, delta in asset_deltas.items() if delta > 0]
        negative_assets = [(mint, delta) for mint, delta in asset_deltas.items() if delta < 0]
        negative_quotes = [(mint, delta) for mint, delta in quote_deltas.items() if delta < 0]
        positive_quotes = [(mint, delta) for mint, delta in quote_deltas.items() if delta > 0]

        if len(positive_assets) == 1 and negative_quotes:
            side = Side.BUY
            token_mint, token_delta = positive_assets[0]
            quote_mint, quote_delta = await self._largest_quote(negative_quotes)
        elif len(negative_assets) == 1 and positive_quotes:
            side = Side.SELL
            token_mint, token_delta = negative_assets[0]
            quote_mint, quote_delta = await self._largest_quote(positive_quotes)
        else:
            return None

        token_amount = abs(token_delta)
        quote_amount = abs(quote_delta)
        quote_price = (
            Decimal("1") if quote_mint in STABLE_MINTS else await self._safe_price(quote_mint)
        )
        usd_value = quote_amount * quote_price if quote_price is not None else None
        if usd_value is not None and usd_value < self.min_trade_usd:
            return None
        token_price = usd_value / token_amount if usd_value is not None and token_amount else None

        return DetectedSwap(
            signature=signature,
            trader_address=wallet,
            block_time=block_time,
            side=side,
            token_mint=token_mint,
            token_amount=token_amount,
            quote_mint=quote_mint,
            quote_amount=quote_amount,
            usd_value=usd_value,
            token_price_usd=token_price,
        )

    async def _largest_quote(self, candidates: list[tuple[str, Decimal]]) -> tuple[str, Decimal]:
        valued: list[tuple[Decimal, str, Decimal]] = []
        for mint, amount in candidates:
            price = Decimal("1") if mint in STABLE_MINTS else await self._safe_price(mint)
            value = abs(amount) * (price or Decimal("0"))
            valued.append((value, mint, amount))
        _, mint, amount = max(valued, key=lambda item: item[0])
        return mint, amount

    async def _safe_price(self, mint: str) -> Decimal | None:
        try:
            return await self.market.price(mint)
        except JupiterError:
            return None


def _owned_token_balances(entries: list[dict[str, Any]], wallet: str) -> dict[str, Decimal]:
    balances: dict[str, Decimal] = defaultdict(Decimal)
    for entry in entries:
        if entry.get("owner") != wallet:
            continue
        mint = entry.get("mint")
        amount = (entry.get("uiTokenAmount") or {}).get("amount")
        decimals = (entry.get("uiTokenAmount") or {}).get("decimals")
        if not mint or amount is None or decimals is None:
            continue
        balances[mint] += Decimal(str(amount)) / (Decimal(10) ** int(decimals))
    return dict(balances)


def _native_sol_delta(transaction: dict[str, Any], wallet: str) -> Decimal:
    meta = transaction.get("meta") or {}
    message = (transaction.get("transaction") or {}).get("message") or {}
    account_keys = message.get("accountKeys") or []
    normalized: list[str] = []
    for key in account_keys:
        normalized.append(str(key.get("pubkey")) if isinstance(key, dict) else str(key))
    try:
        index = normalized.index(wallet)
    except ValueError:
        return Decimal("0")

    pre_balances = meta.get("preBalances") or []
    post_balances = meta.get("postBalances") or []
    if index >= len(pre_balances) or index >= len(post_balances):
        return Decimal("0")
    lamports = Decimal(post_balances[index]) - Decimal(pre_balances[index])
    if index == 0:
        lamports += Decimal(meta.get("fee") or 0)
    return lamports / LAMPORTS_PER_SOL
