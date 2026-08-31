"""The realtime wallet stream: one reconnecting WebSocket, and honest health.

The production symptom this file was rewritten to fix, exactly as reported:

    /fomo realtime
    Wallet stream: DISCONNECTED
    subscriptions: 0
    reconnects: 0

That triple is diagnostic.  ``reconnects: 0`` means no connection ever failed —
so the lane was not "flapping", it was **never started or never subscribing**,
and the surface could not say which.  Three distinct root causes produced the
identical, useless output:

1.  ``enabled`` was false (no WS URL could be derived, or the flag was off), so
    the engine never created the task at all.  A lane that was never started
    reports "disconnected" forever and no amount of waiting fixes it.
2.  Zero enabled traders, so the connection routine returned before subscribing,
    slept, and returned again — no error, no reconnect, no subscriptions.
3.  A socket that stayed *open* while silently delivering nothing.  ``connected``
    was set once and never re-examined, so a dead-but-open subscription looked
    healthy indefinitely.

So this rewrite does four things.  It replaces the boolean ``connected`` with a
named :data:`STREAM_STATES` value that distinguishes those cases; it adds a
heartbeat with stale detection so an open-but-silent socket is torn down and
rebuilt; it counts every reconnect attempt including the ones that never raised;
and it always runs the supervisor loop — even when the lane is disabled — so the
status surface reports a *reason* rather than a blank.

Section 54: when the lane stays down long enough to matter, that is an
infrastructure problem an operator has to be told about, not smart-money
intelligence we quietly stop collecting.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

import aiohttp

if TYPE_CHECKING:
    from .database import Database

# --- lane states (section 53) ------------------------------------------------
#: The feature is switched off by configuration.
STREAM_DISABLED = "DISABLED_BY_CONFIG"
#: No usable WebSocket URL could be derived from the RPC URL.
STREAM_NO_WS_URL = "NO_WS_URL"
#: Running, but there is nothing to subscribe to yet.
STREAM_NO_WALLETS = "NO_WALLETS_SUBSCRIBED"
#: Opening a connection right now.
STREAM_CONNECTING = "CONNECTING"
#: Connected with at least one live subscription.
STREAM_CONNECTED = "CONNECTED"
#: Backing off between attempts after a failure.
STREAM_RECONNECTING = "RECONNECTING"
#: Socket open, subscriptions live, nothing received for too long.
STREAM_STALE = "STALE_NO_TRAFFIC"

STREAM_STATES: tuple[str, ...] = (
    STREAM_DISABLED,
    STREAM_NO_WS_URL,
    STREAM_NO_WALLETS,
    STREAM_CONNECTING,
    STREAM_CONNECTED,
    STREAM_RECONNECTING,
    STREAM_STALE,
)

#: States where the lane is doing its job.
HEALTHY_STREAM_STATES: frozenset[str] = frozenset({STREAM_CONNECTED})

#: States that are the deployment's fault rather than the network's, and which
#: therefore will not fix themselves by waiting.
CONFIGURATION_STREAM_STATES: frozenset[str] = frozenset({STREAM_DISABLED, STREAM_NO_WS_URL})


@dataclass(frozen=True, slots=True)
class StreamEvent:
    wallet: str
    signature: str


@dataclass(frozen=True, slots=True)
class StreamHealth:
    """What an operator surface is allowed to claim about the lane."""

    state: str
    connected: bool
    subscriptions: int
    reconnects: int
    #: Attempts that failed before a subscription was established.
    failed_attempts: int
    last_message_age: int | None
    last_event_age: int | None
    last_error: str
    fallback_active: bool
    down_for_seconds: int | None
    detail: str

    @property
    def healthy(self) -> bool:
        return self.state in HEALTHY_STREAM_STATES

    @property
    def needs_operator_warning(self) -> bool:
        """Section 54: losing this lane silently is not acceptable."""

        return not self.healthy and (self.down_for_seconds or 0) > 0

    def to_json(self) -> dict[str, object]:
        return {
            "state": self.state,
            "connected": self.connected,
            "subscriptions": self.subscriptions,
            "reconnects": self.reconnects,
            "failed_attempts": self.failed_attempts,
            "last_message_age": self.last_message_age,
            "last_event_age": self.last_event_age,
            "last_error": self.last_error,
            "fallback_active": self.fallback_active,
            "down_for_seconds": self.down_for_seconds,
            "detail": self.detail,
            "healthy": self.healthy,
        }


class RealtimeWalletStream:
    """One reconnecting WebSocket with a logs subscription per hot wallet."""

    #: No traffic at all for this long on an open socket means the subscriptions
    #: are dead even though the TCP connection is not.  Solana sends nothing when
    #: the watched wallets are quiet, so this is deliberately generous and is
    #: reset by *any* frame, including pongs — it detects a broken subscription,
    #: not an idle wallet.
    STALE_SECONDS = 180
    #: Backoff bounds for reconnection.
    MIN_BACKOFF_SECONDS = 1
    MAX_BACKOFF_SECONDS = 30
    #: How long the lane may be unhealthy before it is an operator problem.
    WARN_AFTER_SECONDS = 300

    def __init__(
        self,
        database: Database,
        *,
        rpc_url: str,
        explicit_ws_url: str | None,
        enabled: bool,
        commitment: str = "processed",
        on_health_warning: Callable[[StreamHealth], Awaitable[None]] | None = None,
    ) -> None:
        self.database = database
        self.url = explicit_ws_url or derive_ws_url(rpc_url)
        self.configured_enabled = bool(enabled)
        self.enabled = enabled and bool(self.url)
        self.commitment = commitment
        self.events: asyncio.Queue[StreamEvent] = asyncio.Queue(maxsize=1000)
        self.connected = False
        self.subscription_count = 0
        self.last_event_at: int | None = None
        #: Any frame at all, not only a swap.  This is what stale detection uses:
        #: a quiet wallet is normal, a socket that stops acknowledging is not.
        self.last_message_at: int | None = None
        self.last_error: str | None = None
        self.reconnects = 0
        self.failed_attempts = 0
        self.state = (
            STREAM_DISABLED
            if not enabled
            else (STREAM_NO_WS_URL if not self.url else STREAM_CONNECTING)
        )
        self.unhealthy_since: int | None = None if self.enabled else int(time.time())
        self._on_health_warning = on_health_warning
        self._warned_at: int | None = None
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    @property
    def fallback_active(self) -> bool:
        """True while the polling scan lane is carrying wallet activity.

        Derived from the state rather than stored alongside it: two fields that
        must agree are two fields that can disagree, and a diagnostic surface
        that contradicts itself is worse than one that says less.
        """

        return self.state != STREAM_CONNECTED

    def _set_state(self, state: str, *, error: str | None = None) -> None:
        self.state = state
        self.connected = state == STREAM_CONNECTED
        if error is not None:
            self.last_error = error
        now = int(time.time())
        if state == STREAM_CONNECTED:
            self.unhealthy_since = None
            self._warned_at = None
        elif self.unhealthy_since is None:
            self.unhealthy_since = now

    def health(self, *, now: int | None = None) -> StreamHealth:
        """The honest lane state.  Never reports green because a task exists."""

        moment = now if now is not None else int(time.time())
        detail = {
            STREAM_DISABLED: "REALTIME_WALLET_STREAM_ENABLED is false",
            STREAM_NO_WS_URL: "no WebSocket URL could be derived from SOLANA_RPC_URL",
            STREAM_NO_WALLETS: "no enabled wallets to subscribe to yet",
            STREAM_CONNECTING: "opening the WebSocket",
            STREAM_CONNECTED: "subscriptions live",
            STREAM_RECONNECTING: "backing off before the next attempt",
            STREAM_STALE: "socket open but silent — forcing a reconnect",
        }.get(self.state, "")
        return StreamHealth(
            state=self.state,
            connected=self.connected,
            subscriptions=self.subscription_count,
            reconnects=self.reconnects,
            failed_attempts=self.failed_attempts,
            last_message_age=(
                None if self.last_message_at is None else max(0, moment - self.last_message_at)
            ),
            last_event_age=(
                None if self.last_event_at is None else max(0, moment - self.last_event_at)
            ),
            last_error=self.last_error or "",
            fallback_active=self.fallback_active,
            down_for_seconds=(
                None if self.unhealthy_since is None else max(0, moment - self.unhealthy_since)
            ),
            detail=detail,
        )

    async def _maybe_warn(self) -> None:
        """Escalate a lane that has been down long enough to matter (section 54)."""

        if self._on_health_warning is None:
            return
        health = self.health()
        if health.healthy:
            return
        down_for = health.down_for_seconds or 0
        if down_for < self.WARN_AFTER_SECONDS:
            return
        now = int(time.time())
        # One warning per WARN_AFTER_SECONDS window, so a long outage does not
        # turn into a notification loop.
        if self._warned_at is not None and now - self._warned_at < self.WARN_AFTER_SECONDS:
            return
        self._warned_at = now
        # A failing warning channel must never take the wallet lane down with it.
        with suppress(Exception):
            await self._on_health_warning(health)

    async def close(self) -> None:
        self._set_state(STREAM_DISABLED if not self.configured_enabled else STREAM_RECONNECTING)
        self.connected = False
        self.subscription_count = 0
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Supervise the connection forever.

        This loop runs even when the lane cannot connect.  A disabled or
        misconfigured lane used to end its task immediately, which is why the
        status surface could only ever say "DISCONNECTED" with no reconnects and
        no way to tell a switched-off lane from a broken one.
        """

        if not self.enabled or self.url is None:
            self._set_state(
                STREAM_DISABLED if not self.configured_enabled else STREAM_NO_WS_URL,
                error=(
                    "wallet stream disabled by configuration"
                    if not self.configured_enabled
                    else "no WebSocket URL could be derived from the RPC URL"
                ),
            )
            # Stay alive to keep reporting *why*, and to escalate once.
            while True:
                await self._maybe_warn()
                await asyncio.sleep(60)

        backoff = float(self.MIN_BACKOFF_SECONDS)
        while True:
            try:
                await self._run_connection()
                backoff = float(self.MIN_BACKOFF_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.subscription_count = 0
                self.failed_attempts += 1
                self._set_state(STREAM_RECONNECTING, error=_safe_error(exc))
            else:
                # A clean return means the wallet set changed or there was
                # nothing to subscribe to.  Reconnecting is still the right move;
                # it just is not an error.
                self.subscription_count = 0
                if self.state != STREAM_NO_WALLETS:
                    self._set_state(STREAM_RECONNECTING)
            self.reconnects += 1
            await self._maybe_warn()
            # Jitter stops every deployment reconnecting on the same tick after a
            # provider blip.
            await asyncio.sleep(backoff * (0.75 + random.random() * 0.5))
            backoff = min(backoff * 2, float(self.MAX_BACKOFF_SECONDS))

    async def _run_connection(self) -> None:
        traders = await self.database.list_traders(enabled_only=True)
        wallets = tuple(sorted(trader.address for trader in traders))
        if not wallets:
            # Not an error, and not "connected" either.  Saying so is the whole
            # point: this is the state that used to render as a bare
            # DISCONNECTED with zero reconnects.
            self._set_state(STREAM_NO_WALLETS, error="")
            await asyncio.sleep(10)
            return
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=20)
            )

        self._set_state(STREAM_CONNECTING)
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
                            {"commitment": self.commitment},
                        ],
                    }
                )

            self.last_message_at = int(time.time())
            self.last_error = None
            fingerprint = wallets
            while True:
                try:
                    message = await websocket.receive(timeout=5)
                except TimeoutError:
                    now = int(time.time())
                    # Stale detection.  An open socket that has acknowledged
                    # nothing for STALE_SECONDS has dead subscriptions; returning
                    # rebuilds them from scratch rather than sitting on a
                    # connection that will never deliver again.
                    if (
                        self.last_message_at is not None
                        and now - self.last_message_at >= self.STALE_SECONDS
                    ):
                        self._set_state(
                            STREAM_STALE,
                            error=(
                                f"no WebSocket traffic for {now - self.last_message_at}s — "
                                "rebuilding subscriptions"
                            ),
                        )
                        return
                    current = tuple(
                        sorted(
                            trader.address
                            for trader in await self.database.list_traders(enabled_only=True)
                        )
                    )
                    if current != fingerprint:
                        # The wallet set changed; resubscribe to the new set.
                        return
                    continue

                self.last_message_at = int(time.time())

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
                        raise ConnectionError(f"wallet subscription rejected: {payload['error']}")
                    subscription_id = payload.get("result")
                    if isinstance(subscription_id, int):
                        subscriptions[subscription_id] = pending.pop(request_id)
                        self.subscription_count = len(subscriptions)
                        # Only now is the lane genuinely usable.  Reporting
                        # CONNECTED before a subscription is acknowledged is what
                        # made a subscribe-less socket look healthy.
                        self._set_state(STREAM_CONNECTED)
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
    scheme = {"https": "wss", "http": "ws", "wss": "wss", "ws": "ws"}.get(parts.scheme.lower())
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
