from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

from .errors import RpcError


class SolanaRPC:
    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: int = 25,
        max_requests_per_second: int = 8,
        max_retries: int = 4,
    ) -> None:
        self.url = url
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.max_retries = max_retries
        self._minimum_request_interval = 1 / max_requests_per_second
        self._session: aiohttp.ClientSession | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._rate_lock = asyncio.Lock()
        self._next_request_at = 0.0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _wait_for_rate_slot(self) -> None:
        """Space request starts so a fast RPC cannot burst above its plan limit."""

        async with self._rate_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if self._next_request_at > now:
                await asyncio.sleep(self._next_request_at - now)
                now = loop.time()
            self._next_request_at = now + self._minimum_request_interval

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
        exponential = min(float(2**attempt), 8.0)
        if retry_after:
            try:
                return max(exponential, min(float(retry_after), 30.0))
            except ValueError:
                pass
        return exponential

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        async with self._lock:
            self._request_id += 1
            request_id = self._request_id

        session = await self._get_session()
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or [],
        }
        for attempt in range(self.max_retries + 1):
            await self._wait_for_rate_slot()
            try:
                async with session.post(self.url, json=payload) as response:
                    body_text = await response.text()
                    try:
                        body = json.loads(body_text)
                    except (json.JSONDecodeError, TypeError):
                        body = {"raw": body_text[:500]}

                    rpc_error = body.get("error") if isinstance(body, dict) else None
                    rpc_error_code = rpc_error.get("code") if isinstance(rpc_error, dict) else None
                    retryable = response.status == 429 or rpc_error_code == 429
                    retryable = retryable or response.status >= 500
                    if retryable and attempt < self.max_retries:
                        await asyncio.sleep(
                            self._retry_delay(attempt, response.headers.get("Retry-After"))
                        )
                        continue
                    if response.status >= 400:
                        raise RpcError(f"RPC HTTP {response.status}: {body}")
                    if not isinstance(body, dict):
                        raise RpcError(f"RPC {method} returned an unexpected response")
                    if rpc_error is not None:
                        raise RpcError(f"RPC {method} error: {rpc_error}")
                    return body.get("result")
            except (TimeoutError, aiohttp.ClientError) as exc:
                if attempt < self.max_retries:
                    await asyncio.sleep(self._retry_delay(attempt))
                    continue
                raise RpcError(f"RPC request failed for {method}: {exc}") from exc

        raise RpcError(f"RPC request retries exhausted for {method}")

    async def health(self) -> str:
        result = await self.call("getHealth")
        return str(result)

    async def get_signatures_for_address(
        self,
        address: str,
        *,
        limit: int = 100,
        before: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        config: dict[str, Any] = {"limit": max(1, min(limit, 1000)), "commitment": "confirmed"}
        if before:
            config["before"] = before
        if until:
            config["until"] = until
        result = await self.call("getSignaturesForAddress", [address, config])
        return result or []

    async def get_transaction(self, signature: str) -> dict[str, Any] | None:
        return await self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )

    async def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        result = await self.call(
            "getTokenLargestAccounts",
            [mint, {"commitment": "confirmed"}],
        )
        return list((result or {}).get("value") or [])

    async def get_token_supply(self, mint: str) -> dict[str, Any]:
        result = await self.call(
            "getTokenSupply",
            [mint, {"commitment": "confirmed"}],
        )
        return dict((result or {}).get("value") or {})

    async def get_multiple_parsed_accounts(
        self,
        addresses: list[str],
    ) -> list[dict[str, Any] | None]:
        if not addresses:
            return []
        result = await self.call(
            "getMultipleAccounts",
            [
                addresses[:100],
                {"encoding": "jsonParsed", "commitment": "confirmed"},
            ],
        )
        return list((result or {}).get("value") or [])
