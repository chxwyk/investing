from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from .errors import RpcError


class SolanaRPC:
    def __init__(self, url: str, *, timeout_seconds: int = 25) -> None:
        self.url = url
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

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
        try:
            async with session.post(self.url, json=payload) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise RpcError(f"RPC HTTP {response.status}: {body}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise RpcError(f"RPC request failed for {method}: {exc}") from exc

        if "error" in body:
            raise RpcError(f"RPC {method} error: {body['error']}")
        return body.get("result")

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
