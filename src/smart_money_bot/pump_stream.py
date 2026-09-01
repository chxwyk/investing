"""Realtime Pump.fun token discovery from public program logs.

Section 73 asks the question v2.41 and v2.42 both left open.  v2.41 fixed
*observation → alert*; v2.42 improved what happens after that.  Neither fixed
**launch → observation**, which on a 45–60 second poll is a coin's entire early
life.

A public Solana `logsSubscribe` on the Pump.fun program emits a log line for
every create instruction, in the same second it lands.  That is public data from
the same websocket mechanism the wallet lane already uses, so this costs one
extra subscription and turns first-observation latency from *up to a poll
interval* into *sub-second*.

The design mirrors the v2.42 wallet-stream rewrite deliberately, because the same
failure modes apply: a named state rather than a boolean, a supervisor that stays
alive to report *why* when it cannot connect, stale detection for an open-but-
silent socket, and every reconnect counted.  A lane that silently stops
delivering new tokens would be indistinguishable from a quiet market.

Persistence is immediate and enrichment is later (section 74): the moment a mint
is seen it is stamped, because that stamp is the number every latency metric is
measured against.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiohttp

from .stream import (
    STREAM_CONNECTED,
    STREAM_CONNECTING,
    STREAM_DISABLED,
    STREAM_NO_WS_URL,
    STREAM_RECONNECTING,
    STREAM_STALE,
    derive_ws_url,
)
from .trenches.lifecycle import PUMP_PROGRAM_ID

logger = logging.getLogger(__name__)

#: Connected, but the RPC never answered the ``logsSubscribe`` request.  A
#: distinct state because it is a distinct fault: the socket is fine and the
#: subscription is not, which no amount of reconnecting will fix.
STREAM_NO_ACK = "NO_SUBSCRIPTION_ACK"

#: Log fragments the Pump.fun program emits when a coin is created.  Matching a
#: set rather than one string keeps this working across log-format tweaks; a
#: create we fail to recognise costs latency, never correctness, because the
#: polling lane still finds it.
_CREATE_MARKERS: tuple[str, ...] = (
    "Program log: Instruction: Create",
    "Program log: Instruction: CreateEvent",
    "Instruction: Create\n",
)

#: A Solana address as it appears in a log line.
_ADDRESS_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

#: Pump-created mints conventionally carry this suffix.  Used as a *preference*
#: when a log mentions several addresses, never as a filter — a create whose mint
#: does not match the convention is still a create.
_PUMP_SUFFIX = "pump"


@dataclass(frozen=True, slots=True)
class PumpCreation:
    """One newly created Pump.fun coin, seen the moment it landed."""

    mint: str
    signature: str
    observed_at: int
    slot: int | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "signature": self.signature,
            "observed_at": self.observed_at,
            "slot": self.slot,
        }


def extract_created_mint(logs: list[str], *, signature: str = "") -> str:
    """Pull the created mint out of a create instruction's logs.

    Deliberately conservative: it returns a mint only when the logs actually
    contain a create marker, so ordinary trade logs cannot be mistaken for
    launches.  Among the addresses present it prefers one carrying the ``pump``
    suffix, then the longest remaining candidate, and it never returns the
    program's own address.
    """

    blob = "\n".join(logs)
    if not any(marker in blob for marker in _CREATE_MARKERS):
        return ""
    candidates = [
        address
        for address in _ADDRESS_RE.findall(blob)
        if address != PUMP_PROGRAM_ID and address != signature
    ]
    if not candidates:
        return ""
    suffixed = [item for item in candidates if item.endswith(_PUMP_SUFFIX)]
    if suffixed:
        return suffixed[0]
    return max(candidates, key=len)


class PumpCreationStream:
    """A reconnecting `logsSubscribe` on the Pump.fun program."""

    STALE_SECONDS = 300
    #: How long to wait for the ``logsSubscribe`` reply before calling it
    #: unacknowledged.  A healthy RPC answers in well under a second.
    ACK_TIMEOUT_SECONDS = 20
    MIN_BACKOFF_SECONDS = 1
    MAX_BACKOFF_SECONDS = 30
    WARN_AFTER_SECONDS = 600

    def __init__(
        self,
        *,
        rpc_url: str,
        explicit_ws_url: str | None = None,
        enabled: bool = True,
        commitment: str = "processed",
        on_creation: Callable[[PumpCreation], Awaitable[None]] | None = None,
    ) -> None:
        self.url = explicit_ws_url or derive_ws_url(rpc_url)
        self.configured_enabled = bool(enabled)
        self.enabled = enabled and bool(self.url)
        self.commitment = commitment
        self._on_creation = on_creation
        self.events: asyncio.Queue[PumpCreation] = asyncio.Queue(maxsize=500)
        self.state = (
            STREAM_DISABLED
            if not enabled
            else (STREAM_NO_WS_URL if not self.url else STREAM_CONNECTING)
        )
        self.subscription_id: int | None = None
        self.creations_seen = 0
        self.reconnects = 0
        self.failed_attempts = 0
        # --- v2.46 diagnostics ------------------------------------------
        # Production showed RECONNECTING with 0 creations and 35 reconnects,
        # and the logs said nothing at all, so the cause could not be named.
        # These make the next deployment tell us which of the plausible causes
        # it actually is instead of leaving it to be guessed at.
        self.subscribe_acks = 0
        self.notifications = 0
        self.stale_rebuilds = 0
        self.ack_timeouts = 0
        self.last_close_code: int | None = None
        self.last_ack_at: int | None = None
        self.last_notification_at: int | None = None
        self.last_message_at: int | None = None
        self.last_creation_at: int | None = None
        self.last_error: str = ""
        self.unhealthy_since: int | None = None if self.enabled else int(time.time())
        self._session: aiohttp.ClientSession | None = None

    @property
    def connected(self) -> bool:
        return self.state == STREAM_CONNECTED

    @property
    def subscribed(self) -> bool:
        return self.subscription_id is not None

    def _set_state(self, state: str, *, error: str | None = None) -> None:
        self.state = state
        if error is not None:
            self.last_error = error
        if state == STREAM_CONNECTED:
            self.unhealthy_since = None
        elif self.unhealthy_since is None:
            self.unhealthy_since = int(time.time())

    def status(self, *, now: int | None = None) -> dict[str, object]:
        """The honest lane state, in the same shape as the wallet lane's."""

        moment = now if now is not None else int(time.time())
        detail = {
            STREAM_DISABLED: "PUMP_CREATION_STREAM_ENABLED is false",
            STREAM_NO_WS_URL: "no WebSocket URL could be derived from SOLANA_RPC_URL",
            STREAM_CONNECTING: "opening the Pump program subscription",
            STREAM_CONNECTED: "subscribed to Pump.fun program logs",
            STREAM_RECONNECTING: "backing off before the next attempt",
            STREAM_STALE: "subscribed but silent — rebuilding",
            STREAM_NO_ACK: "connected, but logsSubscribe was never acknowledged",
        }.get(self.state, "")
        return {
            "state": self.state,
            "connected": self.connected,
            "subscribed": self.subscribed,
            "creations_seen": self.creations_seen,
            "reconnects": self.reconnects,
            "failed_attempts": self.failed_attempts,
            "subscribe_acks": self.subscribe_acks,
            "notifications": self.notifications,
            "stale_rebuilds": self.stale_rebuilds,
            "ack_timeouts": self.ack_timeouts,
            "last_close_code": self.last_close_code,
            "last_ack_at": self.last_ack_at,
            "last_notification_at": self.last_notification_at,
            # What the operator should read this lane as right now.  When the
            # socket is not delivering, the trenches poller is the source —
            # and it must never be described as a realtime websocket (§28).
            "fallback_active": self.state != STREAM_CONNECTED,
            "fallback_source": (
                "" if self.state == STREAM_CONNECTED else "GMGN_TRENCH_POLLING"
            ),
            "last_creation_at": self.last_creation_at,
            "last_creation_age": (
                None
                if self.last_creation_at is None
                else max(0, moment - self.last_creation_at)
            ),
            "last_message_age": (
                None if self.last_message_at is None else max(0, moment - self.last_message_at)
            ),
            "down_for_seconds": (
                None if self.unhealthy_since is None else max(0, moment - self.unhealthy_since)
            ),
            "last_error": self.last_error,
            "detail": detail,
            "healthy": self.state == STREAM_CONNECTED,
        }

    async def close(self) -> None:
        self._set_state(
            STREAM_DISABLED if not self.configured_enabled else STREAM_RECONNECTING
        )
        self.subscription_id = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def run(self) -> None:
        """Supervise the subscription forever, reporting why when it cannot run."""

        if not self.enabled or self.url is None:
            self._set_state(
                STREAM_DISABLED if not self.configured_enabled else STREAM_NO_WS_URL,
                error=(
                    "Pump creation stream disabled by configuration"
                    if not self.configured_enabled
                    else "no WebSocket URL could be derived from the RPC URL"
                ),
            )
            while True:
                await asyncio.sleep(60)

        backoff = float(self.MIN_BACKOFF_SECONDS)
        while True:
            healthy_run = False
            try:
                healthy_run = await self._run_connection()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failed_attempts += 1
                detail = _safe_error(exc)
                self._set_state(STREAM_RECONNECTING, error=detail)
                logger.warning("Pump creation stream dropped: %s", detail)
            else:
                if healthy_run:
                    # It delivered something before going away.  Only then is a
                    # fast reconnect the right move.
                    backoff = float(self.MIN_BACKOFF_SECONDS)
                else:
                    # It connected and stayed silent.  Resetting the backoff
                    # here is what turned one quiet socket into a reconnect
                    # every few seconds forever, so the backoff is allowed to
                    # grow instead.
                    self.failed_attempts += 1
                self._set_state(STREAM_RECONNECTING)
            self.subscription_id = None
            self.reconnects += 1
            await asyncio.sleep(backoff * (0.75 + random.random() * 0.5))
            backoff = min(backoff * 2, float(self.MAX_BACKOFF_SECONDS))

    async def _run_connection(self) -> bool:
        """One socket, until it dies.  Returns whether it ever delivered.

        The return value is the difference between "the provider closed on us"
        and "we were subscribed and nothing ever arrived", which are different
        problems with different fixes and used to look identical.
        """

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=20)
            )
        self._set_state(STREAM_CONNECTING)
        async with self._session.ws_connect(self.url, heartbeat=20) as websocket:
            await websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [PUMP_PROGRAM_ID]},
                        {"commitment": self.commitment},
                    ],
                }
            )
            self.last_message_at = int(time.time())
            opened_at = self.last_message_at
            delivered = False

            while True:
                try:
                    message = await websocket.receive(timeout=5)
                except TimeoutError:
                    now = int(time.time())
                    # Never acknowledged is a different fault from acknowledged
                    # and silent: the first is the subscription being refused
                    # or ignored, the second is a filter that matches nothing
                    # or an RPC that does not forward logs.
                    if (
                        self.subscription_id is None
                        and now - opened_at >= self.ACK_TIMEOUT_SECONDS
                    ):
                        self.ack_timeouts += 1
                        self._set_state(
                            STREAM_NO_ACK,
                            error=(
                                "logsSubscribe was never acknowledged in "
                                f"{now - opened_at}s — the RPC accepted the socket "
                                "but not the subscription"
                            ),
                        )
                        logger.warning(
                            "Pump creation stream: no logsSubscribe ack after %ss",
                            now - opened_at,
                        )
                        return delivered
                    if (
                        self.last_message_at is not None
                        and now - self.last_message_at >= self.STALE_SECONDS
                    ):
                        self.stale_rebuilds += 1
                        silent_for = now - self.last_message_at
                        self._set_state(
                            STREAM_STALE,
                            error=(
                                f"subscribed (id {self.subscription_id}) but no Pump "
                                f"program traffic for {silent_for}s — the RPC is not "
                                "forwarding logs for this filter"
                            ),
                        )
                        logger.warning(
                            "Pump creation stream silent for %ss after %d notification(s); "
                            "rebuilding",
                            silent_for,
                            self.notifications,
                        )
                        return delivered
                    continue

                self.last_message_at = int(time.time())
                if message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE}:
                    self.last_close_code = getattr(websocket, "close_code", None)
                    raise ConnectionError(
                        f"Pump creation stream closed (code {self.last_close_code})"
                    )
                if message.type is aiohttp.WSMsgType.ERROR:
                    raise ConnectionError(
                        f"Pump creation stream error: {websocket.exception()}"
                    )
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(message.data)
                except (json.JSONDecodeError, TypeError):
                    continue

                if payload.get("id") == 1:
                    if payload.get("error") is not None:
                        raise ConnectionError(
                            f"Pump log subscription rejected: {payload['error']}"
                        )
                    result = payload.get("result")
                    if isinstance(result, int):
                        self.subscription_id = result
                        self.subscribe_acks += 1
                        self.last_ack_at = int(time.time())
                        self._set_state(STREAM_CONNECTED)
                        logger.info(
                            "Pump creation stream subscribed (id %s) on %s",
                            result,
                            self.commitment,
                        )
                    continue

                self.notifications += 1
                self.last_notification_at = int(time.time())
                delivered = True
                await self._handle_notification(payload)

    async def _handle_notification(self, payload: dict) -> None:
        params = payload.get("params")
        if not isinstance(params, dict):
            return
        result = params.get("result")
        if not isinstance(result, dict):
            return
        context = result.get("context") or {}
        value = result.get("value")
        if not isinstance(value, dict) or value.get("err"):
            return
        logs = value.get("logs")
        signature = str(value.get("signature") or "")
        if not isinstance(logs, list) or not signature:
            return

        mint = extract_created_mint([str(item) for item in logs], signature=signature)
        if not mint:
            return

        creation = PumpCreation(
            mint=mint,
            signature=signature,
            observed_at=int(time.time()),
            slot=context.get("slot") if isinstance(context.get("slot"), int) else None,
        )
        self.creations_seen += 1
        self.last_creation_at = creation.observed_at
        try:
            self.events.put_nowait(creation)
        except asyncio.QueueFull:
            _ = self.events.get_nowait()
            self.events.put_nowait(creation)
        if self._on_creation is not None:
            # A slow consumer must never stall the socket; the queue is the
            # buffer and the callback is best-effort.
            with contextlib.suppress(Exception):
                await self._on_creation(creation)


def _safe_error(error: Exception) -> str:
    text = re.sub(r"(?i)(api[-_]?key=)[^&\s'\"]+", r"\1<redacted>", str(error))
    return f"{type(error).__name__}: {text}"[:400]
