"""Reading Pump.fun state directly from the chain.

This is the module that makes v2.43 independent of any vendor (section 4).
Everything the Trenches engine needs most — bonding progress, the creator, holder
concentration, buyer history, same-slot grouping — comes from public Solana RPC
methods and the Pump.fun program's own published account layout.  If DEX Screener
is degraded, Fomo is unavailable and Solana Tracker has no credits, this keeps
working.

Two design rules run through it:

**A failed read is ``None``, never a default.**  An unreadable bonding curve
reports ``available=False``; it does not report 0% progress.  Everything
downstream is built to say UNKNOWN rather than to guess.

**Batch, cache, and bound.**  A Trenches candidate is worth a handful of RPC
calls, not fifty (section 71).  Curve reads are batched 100 at a time, wallet
histories are cached with a TTL, and every helper takes an explicit cap.
"""

from __future__ import annotations

import struct
import time
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from solders.pubkey import Pubkey

from .errors import RpcError
from .rpc import SolanaRPC
from .trenches.bundles import SlotTrade
from .trenches.holders import HolderAccount, HolderSnapshot, build_holder_snapshot
from .trenches.lifecycle import (
    BONDING_CURVE_SEED,
    PUMP_PROGRAM_ID,
    BondingCurveState,
)
from .trenches.participants import BuyerRecord

ZERO = Decimal("0")

#: Anchor account discriminator length.
_DISCRIMINATOR = 8
#: ``virtual_token``, ``virtual_sol``, ``real_token``, ``real_sol``, ``supply``.
_RESERVE_STRUCT = struct.Struct("<QQQQQ")

_PUMP_PROGRAM = Pubkey.from_string(PUMP_PROGRAM_ID)

#: Accounts that are infrastructure rather than participants, excluded from
#: holder concentration so a liquidity pool is never counted as a whale.
_INFRASTRUCTURE_OWNERS: frozenset[str] = frozenset(
    {
        PUMP_PROGRAM_ID,
        "11111111111111111111111111111111",
        "1nc1nerator11111111111111111111111111111111",
        # PumpSwap AMM authority; pool-owned accounts are not holders either.
        "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    }
)


def bonding_curve_address(mint: str) -> str:
    """The bonding-curve PDA for a mint, derived exactly as the program does."""

    address, _ = Pubkey.find_program_address(
        [BONDING_CURVE_SEED, bytes(Pubkey.from_string(mint))], _PUMP_PROGRAM
    )
    return str(address)


def decode_bonding_curve(mint: str, data: bytes | None) -> BondingCurveState:
    """Decode a bonding-curve account.

    The layout is the discriminator, five ``u64`` reserve fields, a ``complete``
    flag, and — on newer accounts — the creator pubkey followed by the documented
    special-mode flags.  Older accounts stop after ``complete``, so every field
    past that point is read defensively and simply stays absent when the account
    predates it.  A short or absent buffer produces ``available=False``, never a
    zero-filled state.
    """

    if not data:
        return BondingCurveState(mint=mint, available=False, error="account not found")
    minimum = _DISCRIMINATOR + _RESERVE_STRUCT.size + 1
    if len(data) < minimum:
        return BondingCurveState(
            mint=mint, available=False, error=f"account too short ({len(data)} bytes)"
        )

    offset = _DISCRIMINATOR
    (
        virtual_token,
        virtual_sol,
        real_token,
        real_sol,
        total_supply,
    ) = _RESERVE_STRUCT.unpack_from(data, offset)
    offset += _RESERVE_STRUCT.size
    complete = bool(data[offset])
    offset += 1

    creator = ""
    if len(data) >= offset + 32:
        try:
            creator = str(Pubkey.from_bytes(data[offset : offset + 32]))
        except (ValueError, TypeError):
            creator = ""
        offset += 32

    # Documented special modes (section 28).  Absent on older accounts, which is
    # not the same as "false" — but a token that predates the flag cannot be in
    # the mode either, so False is the correct reading here.
    mayhem = bool(data[offset]) if len(data) > offset else False
    cashback = bool(data[offset + 1]) if len(data) > offset + 1 else False

    return BondingCurveState(
        mint=mint,
        available=True,
        virtual_token_reserves=Decimal(virtual_token),
        virtual_sol_reserves=Decimal(virtual_sol),
        real_token_reserves=Decimal(real_token),
        real_sol_reserves=Decimal(real_sol),
        token_total_supply=Decimal(total_supply),
        complete=complete,
        creator=creator,
        is_mayhem_mode=mayhem,
        is_cashback_coin=cashback,
    )


@dataclass(frozen=True, slots=True)
class WalletHistory:
    """What public signature history says about one wallet."""

    wallet: str
    first_activity_at: int | None = None
    signature_count: int | None = None
    available: bool = False


#: How an RPC failure is classified, so the panel can say which problem it is.
RPC_RATE_LIMITED = "RPC_429_RATE_LIMITED"
RPC_FORBIDDEN = "RPC_403_FORBIDDEN"
RPC_UNSUPPORTED = "RPC_METHOD_UNSUPPORTED"
RPC_TIMEOUT = "RPC_TIMEOUT"
RPC_MALFORMED = "RPC_MALFORMED_RESPONSE"
RPC_NETWORK = "RPC_NETWORK"
RPC_OTHER = "RPC_OTHER"


def classify_rpc_error(error: object) -> str:
    """Name the failure, because "errors 27" tells an operator nothing.

    A public RPC that refuses ``getTokenLargestAccounts`` and one that is
    throttling us look identical in an error count and need opposite responses:
    the first needs a different endpoint, the second needs backing off.
    """

    text = str(error).lower()
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return RPC_RATE_LIMITED
    if "403" in text or "forbidden" in text:
        return RPC_FORBIDDEN
    if (
        "method not found" in text
        or "unsupported" in text
        or "-32601" in text
        or "disabled" in text
    ):
        return RPC_UNSUPPORTED
    if "timeout" in text or "timed out" in text:
        return RPC_TIMEOUT
    if "json" in text or "decode" in text or "unexpected" in text:
        return RPC_MALFORMED
    if isinstance(error, OSError) or "connect" in text or "reset" in text:
        return RPC_NETWORK
    return RPC_OTHER


class PumpChainReader:
    """Public on-chain reads for the Trenches engine.  No vendor required."""

    #: How long a bonding-curve read stays fresh.  The curve moves with every
    #: trade, so this is deliberately short.
    CURVE_TTL_SECONDS = 10
    #: Wallet history barely changes; caching it hard is what keeps the
    #: fresh-wallet analysis affordable.
    WALLET_TTL_SECONDS = 1800
    #: Holder reads are the most expensive per-token call we make.
    HOLDER_TTL_SECONDS = 45

    def __init__(self, rpc: SolanaRPC, *, max_wallet_cache: int = 4000) -> None:
        self.rpc = rpc
        self.max_wallet_cache = max_wallet_cache
        self._curve_cache: dict[str, tuple[float, BondingCurveState]] = {}
        self._wallet_cache: dict[str, tuple[float, WalletHistory]] = {}
        self._holder_cache: dict[str, tuple[float, HolderSnapshot]] = {}
        self.curve_reads = 0
        self.holder_reads = 0
        self.wallet_reads = 0
        self.cache_hits = 0
        self.errors = 0
        self.last_error: str = ""
        # Section 29: "errors 27" is not a diagnosis.  Production showed
        # 28 holder calls against 27 errors and no way to tell an RPC that
        # refuses the method from one that is rate limiting us, which are
        # opposite problems — one needs a different endpoint, the other needs
        # patience.  Every failure is now counted by operation and by cause.
        self.errors_by_operation: dict[str, int] = {}
        self.errors_by_cause: dict[str, int] = {}
        self.calls_by_operation: dict[str, int] = {}

    def _note_error(self, operation: str, error: object) -> None:
        """Record which call failed and why, not just that something did."""

        self.errors += 1
        self.errors_by_operation[operation] = self.errors_by_operation.get(operation, 0) + 1
        cause = classify_rpc_error(error)
        self.errors_by_cause[cause] = self.errors_by_cause.get(cause, 0) + 1
        self.last_error = f"{operation}: {str(error)[:140]}"

    def _note_call(self, operation: str) -> None:
        self.calls_by_operation[operation] = self.calls_by_operation.get(operation, 0) + 1

    def usage_snapshot(self) -> dict[str, Any]:
        return {
            "curve_reads": self.curve_reads,
            "holder_reads": self.holder_reads,
            "wallet_reads": self.wallet_reads,
            "cache_hits": self.cache_hits,
            "errors": self.errors,
            "last_error": self.last_error,
            "calls_by_operation": dict(self.calls_by_operation),
            "errors_by_operation": dict(self.errors_by_operation),
            "errors_by_cause": dict(self.errors_by_cause),
            "success_by_operation": {
                name: max(0, count - self.errors_by_operation.get(name, 0))
                for name, count in self.calls_by_operation.items()
            },
        }

    # ------------------------------------------------------------------
    # bonding curve (sections 7, 8)
    # ------------------------------------------------------------------
    async def bonding_curve(self, mint: str, *, refresh: bool = False) -> BondingCurveState:
        cached = self._curve_cache.get(mint)
        now = time.monotonic()
        if not refresh and cached and now - cached[0] <= self.CURVE_TTL_SECONDS:
            self.cache_hits += 1
            return cached[1]
        try:
            address = bonding_curve_address(mint)
        except (ValueError, TypeError) as exc:
            return BondingCurveState(mint=mint, available=False, error=f"bad mint: {exc}")
        try:
            self.curve_reads += 1
            self._note_call("bonding_curve")
            data = await self.rpc.get_account_data(address)
        except (RpcError, OSError) as exc:
            self._note_error("bonding_curve", exc)
            return BondingCurveState(mint=mint, available=False, error=self.last_error)
        state = decode_bonding_curve(mint, data)
        self._curve_cache[mint] = (now, state)
        return state

    async def bonding_curves(self, mints: list[str]) -> dict[str, BondingCurveState]:
        """Batch the whole board in one or two requests, not one per mint."""

        if not mints:
            return {}
        now = time.monotonic()
        results: dict[str, BondingCurveState] = {}
        pending: dict[str, str] = {}
        for mint in mints:
            cached = self._curve_cache.get(mint)
            if cached and now - cached[0] <= self.CURVE_TTL_SECONDS:
                self.cache_hits += 1
                results[mint] = cached[1]
                continue
            try:
                pending[bonding_curve_address(mint)] = mint
            except (ValueError, TypeError):
                results[mint] = BondingCurveState(
                    mint=mint, available=False, error="bad mint address"
                )
        if not pending:
            return results
        try:
            self.curve_reads += len(pending)
            raw = await self.rpc.get_multiple_account_data(list(pending))
        except (RpcError, OSError) as exc:
            self._note_error("bonding_curves", exc)
            for mint in pending.values():
                results[mint] = BondingCurveState(
                    mint=mint, available=False, error=self.last_error
                )
            return results
        for address, data in raw.items():
            mint = pending[address]
            state = decode_bonding_curve(mint, data)
            self._curve_cache[mint] = (now, state)
            results[mint] = state
        return results

    # ------------------------------------------------------------------
    # holders (sections 20, 21)
    # ------------------------------------------------------------------
    async def holder_snapshot(
        self,
        mint: str,
        *,
        at: int | None = None,
        refresh: bool = False,
    ) -> HolderSnapshot:
        """Top-holder concentration among participants, from public RPC."""

        moment = at if at is not None else int(time.time())
        cached = self._holder_cache.get(mint)
        now = time.monotonic()
        if not refresh and cached and now - cached[0] <= self.HOLDER_TTL_SECONDS:
            self.cache_hits += 1
            return cached[1]
        try:
            self.holder_reads += 1
            self._note_call("holder_snapshot")
            largest = await self.rpc.get_token_largest_accounts(mint)
            supply = await self.rpc.get_token_supply(mint)
        except (RpcError, OSError) as exc:
            self._note_error("holder_snapshot", exc)
            return HolderSnapshot(mint=mint, at=moment)

        total = _decimal(supply.get("amount"))
        curve_address = ""
        with suppress(ValueError, TypeError):
            curve_address = bonding_curve_address(mint)

        accounts: list[HolderAccount] = []
        owners: list[str] = []
        for row in largest:
            if not isinstance(row, dict):
                continue
            amount = _decimal(row.get("amount"))
            if amount is None or amount <= ZERO:
                continue
            accounts.append(
                HolderAccount(address=str(row.get("address") or ""), amount=amount)
            )
            owners.append(str(row.get("address") or ""))

        # Resolve owners so the bonding curve and pool accounts can be excluded.
        infrastructure: set[str] = set()
        if owners:
            try:
                parsed = await self.rpc.get_multiple_parsed_accounts(owners[:100])
            except (RpcError, OSError):
                parsed = []
            for address, account in zip(owners, parsed, strict=False):
                if not isinstance(account, dict):
                    continue
                info = ((account.get("data") or {}).get("parsed") or {}).get("info") or {}
                owner = str(info.get("owner") or "")
                if owner in _INFRASTRUCTURE_OWNERS or owner == curve_address:
                    infrastructure.add(address)

        accounts = [
            HolderAccount(
                address=item.address,
                amount=item.amount,
                infrastructure=item.address in infrastructure,
            )
            for item in accounts
        ]
        snapshot = build_holder_snapshot(
            mint, accounts, total_supply=total, at=moment, holder_count=None
        )
        self._holder_cache[mint] = (now, snapshot)
        return snapshot

    # ------------------------------------------------------------------
    # wallet history (sections 14, 15)
    # ------------------------------------------------------------------
    async def wallet_history(self, wallet: str) -> WalletHistory:
        """How old a wallet is, from its earliest observable signature.

        Heavily cached: a wallet's first activity is immutable once observed, so
        re-reading it is pure waste.
        """

        cached = self._wallet_cache.get(wallet)
        now = time.monotonic()
        if cached and now - cached[0] <= self.WALLET_TTL_SECONDS:
            self.cache_hits += 1
            return cached[1]
        try:
            self.wallet_reads += 1
            self._note_call("wallet_history")
            signatures = await self.rpc.get_signatures_for_address(wallet, limit=200)
        except (RpcError, OSError) as exc:
            self._note_error("wallet_history", exc)
            return WalletHistory(wallet=wallet, available=False)

        times = [
            int(row["blockTime"])
            for row in signatures
            if isinstance(row, dict) and row.get("blockTime")
        ]
        history = WalletHistory(
            wallet=wallet,
            # The oldest signature in the page.  With fewer than the page limit
            # returned, this really is the wallet's first activity; with a full
            # page it is an upper bound, which only ever makes a wallet look
            # older than it is — the conservative direction for "fresh".
            first_activity_at=min(times) if times else None,
            signature_count=len(signatures),
            available=True,
        )
        if len(self._wallet_cache) > self.max_wallet_cache:
            for key in list(self._wallet_cache)[: self.max_wallet_cache // 2]:
                self._wallet_cache.pop(key, None)
        self._wallet_cache[wallet] = (now, history)
        return history

    async def enrich_buyers(
        self,
        buyers: list[BuyerRecord],
        *,
        max_lookups: int = 40,
    ) -> list[BuyerRecord]:
        """Attach public funding history to observed buyers, within a budget.

        The cap is the point: a token with 800 buyers is not worth 800 RPC calls.
        Buyers past the cap keep ``UNKNOWN`` history, which the participant model
        already handles honestly.
        """

        from dataclasses import replace

        enriched: list[BuyerRecord] = []
        looked_up = 0
        seen: dict[str, WalletHistory] = {}
        for record in buyers:
            history = seen.get(record.wallet)
            if history is None:
                cached = self._wallet_cache.get(record.wallet)
                if cached:
                    history = cached[1]
                elif looked_up < max_lookups:
                    history = await self.wallet_history(record.wallet)
                    looked_up += 1
                seen[record.wallet] = history or WalletHistory(wallet=record.wallet)
            if history is None or not history.available:
                enriched.append(record)
                continue
            enriched.append(
                replace(
                    record,
                    first_activity_at=history.first_activity_at,
                    signature_count=history.signature_count,
                )
            )
        return enriched

    # ------------------------------------------------------------------
    # trades and slots (sections 13, 23)
    # ------------------------------------------------------------------
    async def recent_trades(
        self,
        mint: str,
        *,
        limit: int = 120,
    ) -> tuple[list[SlotTrade], str]:
        """Recent signatures against the curve, with their slots.

        Returns the trades and an error string.  Slot grouping is what the bundle
        analysis needs; the direction and size come from the parsed transaction
        only when a caller asks for them, because parsing every transaction is
        the expensive part.
        """

        try:
            address = bonding_curve_address(mint)
        except (ValueError, TypeError) as exc:
            return [], f"bad mint: {exc}"
        try:
            signatures = await self.rpc.get_signatures_for_address(address, limit=limit)
        except (RpcError, OSError) as exc:
            self._note_error("recent_trades", exc)
            return [], self.last_error

        trades: list[SlotTrade] = []
        for row in signatures:
            if not isinstance(row, dict) or row.get("err"):
                continue
            slot = row.get("slot")
            block_time = row.get("blockTime")
            if slot is None or block_time is None:
                continue
            trades.append(
                SlotTrade(
                    # Without parsing the transaction we do not know the trader,
                    # so the signature stands in as the unit of activity and the
                    # bundle detector groups by slot, which is what it needs.
                    wallet=str(row.get("signature") or ""),
                    slot=int(slot),
                    at=int(block_time),
                )
            )
        return trades, ""


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() else None
