from __future__ import annotations

import asyncio
import base64
import json
import time
from decimal import Decimal
from typing import Any

import aiohttp
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction

from .errors import JupiterError
from .models import SwapQuote, TokenInfo


class JupiterClient:
    BASE_URL = "https://api.jup.ag"

    def __init__(self, api_key: str | None, *, timeout_seconds: int = 25) -> None:
        self.api_key = api_key
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None
        self._price_cache: dict[str, tuple[float, Decimal]] = {}
        self._token_cache: dict[str, tuple[float, TokenInfo]] = {}
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._minimum_request_interval = 1.05 if api_key else 2.05

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key} if self.api_key else {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        session = await self._get_session()
        url = f"{self.BASE_URL}{path}"
        headers = self._headers()
        if payload is not None:
            headers["Content-Type"] = "application/json"

        # Keyless Jupiter access is intentionally low-rate. Serializing calls keeps
        # this bot respectful and predictable; production keys raise the limit.
        async with self._request_lock:
            for attempt in range(3):
                delay = self._minimum_request_interval - (time.monotonic() - self._last_request_at)
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    async with session.request(
                        method, url, params=params, json=payload, headers=headers
                    ) as response:
                        body_text = await response.text()
                        self._last_request_at = time.monotonic()
                        if response.status == 429 and attempt < 2:
                            await asyncio.sleep(2**attempt)
                            continue
                        if response.status >= 400:
                            raise JupiterError(
                                f"Jupiter HTTP {response.status} for {path}: {body_text[:500]}"
                            )
                        break
                except (TimeoutError, aiohttp.ClientError) as exc:
                    if attempt < 2:
                        await asyncio.sleep(2**attempt)
                        continue
                    raise JupiterError(f"Jupiter request failed for {path}: {exc}") from exc
            else:  # defensive; every successful branch breaks
                raise JupiterError(f"Jupiter request exhausted retries for {path}")

        try:
            return json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise JupiterError(f"Jupiter returned invalid JSON for {path}") from exc

    async def prices(self, mints: list[str]) -> dict[str, Decimal]:
        now = time.monotonic()
        result: dict[str, Decimal] = {}
        missing: list[str] = []
        for mint in dict.fromkeys(mints):
            cached = self._price_cache.get(mint)
            if cached and now - cached[0] <= 8:
                result[mint] = cached[1]
            else:
                missing.append(mint)

        for start in range(0, len(missing), 50):
            batch = missing[start : start + 50]
            if not batch:
                continue
            data = await self._request("GET", "/price/v3", params={"ids": ",".join(batch)})
            for mint, item in data.items():
                raw_price = item.get("usdPrice") if isinstance(item, dict) else None
                if raw_price is None:
                    continue
                price = Decimal(str(raw_price))
                self._price_cache[mint] = (now, price)
                result[mint] = price
        return result

    async def price(self, mint: str) -> Decimal | None:
        return (await self.prices([mint])).get(mint)

    async def token_info(self, mint: str) -> TokenInfo | None:
        now = time.monotonic()
        cached = self._token_cache.get(mint)
        if cached and now - cached[0] <= 60:
            return cached[1]
        if not self.api_key:
            return None

        data = await self._request("GET", "/tokens/v2/search", params={"query": mint})
        if not isinstance(data, list):
            return None
        exact = next((item for item in data if item.get("id") == mint), None)
        if not exact:
            return None

        audit = exact.get("audit") or {}
        info = TokenInfo(
            mint=mint,
            symbol=exact.get("symbol"),
            name=exact.get("name"),
            decimals=exact.get("decimals"),
            usd_price=_decimal_or_none(exact.get("usdPrice")),
            liquidity_usd=_decimal_or_none(exact.get("liquidity")),
            market_cap_usd=_decimal_or_none(exact.get("mcap")),
            holder_count=exact.get("holderCount"),
            organic_score=_decimal_or_none(exact.get("organicScore")),
            verified=exact.get("isVerified"),
            suspicious=bool(audit.get("isSus", False)),
            mint_authority_disabled=audit.get("mintAuthorityDisabled"),
            freeze_authority_disabled=audit.get("freezeAuthorityDisabled"),
            top_holders_percent=_decimal_or_none(audit.get("topHoldersPercentage")),
            dev_balance_percent=_decimal_or_none(audit.get("devBalancePercentage")),
        )
        self._token_cache[mint] = (now, info)
        return info

    async def create_order(
        self,
        *,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        taker: str,
    ) -> dict[str, Any]:
        if amount_raw <= 0:
            raise ValueError("amount_raw must be positive")
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "taker": taker,
        }
        return await self._request("GET", "/swap/v2/order", params=params)

    async def quote_order(
        self,
        *,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        input_decimals: int,
        output_decimals: int,
    ) -> SwapQuote:
        """Return a quote-only Swap V2 order without asking for a transaction."""

        if not self.api_key:
            raise JupiterError("JUPITER_API_KEY is required for executable PAPER order quotes")
        if amount_raw <= 0:
            raise ValueError("amount_raw must be positive")
        if input_decimals < 0 or output_decimals < 0:
            raise ValueError("token decimals cannot be negative")

        started = time.monotonic()
        data = await self._request(
            "GET",
            "/swap/v2/order",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount_raw),
            },
        )
        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        if not isinstance(data, dict):
            raise JupiterError("Jupiter returned an invalid order quote")
        if data.get("error") or data.get("errorMessage"):
            raise JupiterError(
                f"Jupiter order quote failed: {data.get('errorMessage') or data.get('error')}"
            )
        if data.get("inputMint") != input_mint or data.get("outputMint") != output_mint:
            raise JupiterError("Jupiter order quote returned the wrong token pair")

        try:
            quoted_input_raw = int(data["inAmount"])
            quoted_output_raw = int(data["outAmount"])
        except (KeyError, TypeError, ValueError) as exc:
            raise JupiterError("Jupiter order quote omitted valid token amounts") from exc
        if quoted_input_raw <= 0 or quoted_output_raw <= 0:
            raise JupiterError("Jupiter order quote returned a zero token amount")

        threshold_raw: int | None = None
        raw_threshold = data.get("otherAmountThreshold")
        if raw_threshold is not None:
            try:
                threshold_raw = int(raw_threshold)
            except (TypeError, ValueError):
                threshold_raw = None

        raw_impact = data.get("priceImpact")
        if raw_impact is not None:
            price_impact = abs(Decimal(str(raw_impact)))
        else:
            deprecated_ratio = _decimal_or_none(data.get("priceImpactPct"))
            price_impact = (
                abs(deprecated_ratio * Decimal("100")) if deprecated_ratio else Decimal("0")
            )

        return SwapQuote(
            input_mint=input_mint,
            output_mint=output_mint,
            input_amount_raw=quoted_input_raw,
            output_amount_raw=quoted_output_raw,
            other_amount_threshold_raw=threshold_raw,
            input_amount=Decimal(quoted_input_raw) / (Decimal(10) ** input_decimals),
            output_amount=Decimal(quoted_output_raw) / (Decimal(10) ** output_decimals),
            input_usd_value=_decimal_or_none(data.get("inUsdValue")),
            output_usd_value=_decimal_or_none(data.get("outUsdValue")),
            price_impact_percent=price_impact,
            router=str(data.get("router") or "unknown"),
            fee_bps=int(data.get("feeBps") or 0),
            api_time_ms=(int(data["totalTime"]) if data.get("totalTime") is not None else None),
            observed_latency_ms=latency_ms,
            quoted_at=int(time.time()),
        )

    async def execute_order(
        self,
        *,
        signed_transaction: str,
        request_id: str,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/swap/v2/execute",
            payload={"signedTransaction": signed_transaction, "requestId": request_id},
        )

    async def swap(
        self,
        *,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        keypair: Keypair,
    ) -> dict[str, Any]:
        order = await self.create_order(
            input_mint=input_mint,
            output_mint=output_mint,
            amount_raw=amount_raw,
            taker=str(keypair.pubkey()),
        )
        encoded = order.get("transaction")
        request_id = order.get("requestId")
        if not encoded or not request_id:
            raise JupiterError(
                f"Jupiter could not build the order: {order.get('errorMessage', 'unknown error')}"
            )

        signed = sign_versioned_transaction(encoded, keypair)
        result = await self.execute_order(signed_transaction=signed, request_id=request_id)
        if result.get("status") != "Success":
            raise JupiterError(
                f"Jupiter execution failed ({result.get('code')}): {result.get('error', result)}"
            )
        return result


def _decimal_or_none(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def load_keypair(secret: str, *, variable_name: str = "TRADING_PRIVATE_KEY") -> Keypair:
    secret = secret.strip()
    try:
        if secret.startswith("["):
            values = json.loads(secret)
            raw = bytes(int(item) for item in values)
            if len(raw) != 64:
                raise ValueError("JSON private key must contain exactly 64 bytes")
            return Keypair.from_bytes(raw)
        else:
            return Keypair.from_base58_string(secret)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"{variable_name} must be base58 or a 64-byte JSON array"
        ) from exc


def sign_versioned_transaction(
    encoded: str,
    keypair: Keypair,
    *,
    provider: str = "Jupiter",
) -> str:
    try:
        transaction = VersionedTransaction.from_bytes(base64.b64decode(encoded))
    except Exception as exc:  # solders raises several low-level decode errors
        raise JupiterError(f"{provider} returned an invalid versioned transaction") from exc

    required = transaction.message.header.num_required_signatures
    signer_keys = list(transaction.message.account_keys)[:required]
    try:
        signer_index = signer_keys.index(keypair.pubkey())
    except ValueError as exc:
        raise JupiterError(
            f"Configured signing key is not a required signer on the {provider} transaction"
        ) from exc

    signatures = list(transaction.signatures)
    signatures[signer_index] = keypair.sign_message(to_bytes_versioned(transaction.message))
    transaction.signatures = signatures
    return base64.b64encode(bytes(transaction)).decode("ascii")
