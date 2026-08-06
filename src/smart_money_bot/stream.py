from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

import aiohttp

if TYPE_CHECKING:
    from .database import Database


@dataclass(frozen=True, slots=True)
class StreamEvent:
    wallet: str
    signature: str


class RealtimeWalletStream:
    """One reconnecting WebSocket with a logs subscription per hot wallet."""

    def __init__(
        self,
        database: Database,
        *,
        rpc_url: str,
        explicit_ws_url: str | None,
        enabled: bool,
    ) -> None:
        self.database = database
        self.url = explicit_ws_url or derive_ws_url(rpc_url)
        self.enabled = enabled and bool(self.url)
        self.events: asyncio.Queue[StreamEvent] = asyncio.Queue(maxsize=1000)
        self.connected = False
        self.subscription_count = 0
        self.last_event_at: int | None = None
        self.last_error: str | None = None
        self.reconnects = 0
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        self.connected = False
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def run(self) -> None:
        if not self.enabled or self.url is None:
            return
        backoff = 1
        while True:
            try:
                await self._run_connection()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.subscription_count = 0
                self.last_error = _safe_error(exc)
                self.reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _run_connection(self) -> None:
        traders = await self.database.list_traders(enabled_only=True)
        wallets = tuple(sorted(trader.address for trader in traders))
        if not wallets:
            await asyncio.sleep(10)
            return
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=20)
            )

        async with self._session.ws_connect(self.url, heartbeat=20) as websocket:
            pending: dict[int, str] = {}
            subscriptions: dict[int, str] = {}
            for request_id, wallet in enumerate(wallets, start=1):
                pending[request_id] = wallet
                await websocket.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [wallet]},
                            {"commitment": "confirmed"},
                        ],
                    }
                )

            self.connected = True
            self.last_error = None
            fingerprint = wallets
            while True:
                try:
                    message = await websocket.receive(timeout=30)
                except TimeoutError:
                    current = tuple(
                        sorted(
                            trader.address
                            for trader in await self.database.list_traders(
                                enabled_only=True
                            )
                        )
                    )
                    if current != fingerprint:
                        return
                    continue

                if message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE}:
                    raise ConnectionError("Solana wallet stream closed")
                if message.type is aiohttp.WSMsgType.ERROR:
                    raise ConnectionError(f"Solana wallet stream error: {websocket.exception()}")
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(message.data)
                except (json.JSONDecodeError, TypeError):
                    continue

                request_id = payload.get("id")
                if request_id in pending:
                    if payload.get("error") is not None:
                        raise ConnectionError(
                            f"wallet subscription rejected: {payload['error']}"
                        )
                    subscription_id = payload.get("result")
                    if isinstance(subscription_id, int):
                        subscriptions[subscription_id] = pending.pop(request_id)
                        self.subscription_count = len(subscriptions)
                    continue

                params = payload.get("params")
                if not isinstance(params, dict):
                    continue
                subscription_id = params.get("subscription")
                wallet = subscriptions.get(subscription_id)
                result = params.get("result")
                value = result.get("value") if isinstance(result, dict) else None
                signature = value.get("signature") if isinstance(value, dict) else None
                if wallet and isinstance(signature, str) and signature:
                    event = StreamEvent(wallet=wallet, signature=signature)
                    try:
                        self.events.put_nowait(event)
                    except asyncio.QueueFull:
                        _ = self.events.get_nowait()
                        self.events.put_nowait(event)
                    self.last_event_at = int(time.time())


def derive_ws_url(rpc_url: str) -> str | None:
    try:
        parts = urlsplit(rpc_url)
    except ValueError:
        return None
    scheme = {"https": "wss", "http": "ws", "wss": "wss", "ws": "ws"}.get(
        parts.scheme.lower()
    )
    if scheme is None or not parts.netloc:
        return None
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


def _safe_error(error: Exception) -> str:
    text = str(error)
    text = re.sub(
        r"(?i)(api[-_]?key=)[^&\s'\"]+",
        r"\1<redacted>",
        text,
    )
    return f"{type(error).__name__}: {text}"[:500]
