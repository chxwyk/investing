"""Tests for wallet-stream self-recovery and honest health (sections 52-54).

The production report these exist to prevent recurring:

    Wallet stream: DISCONNECTED
    subscriptions: 0
    reconnects: 0

Zero reconnects means nothing ever failed, so the lane was not flapping — it was
never started, or never subscribing.  Three unrelated faults produced that exact
output and the surface distinguished none of them, which is why every test below
asserts on a *named state* rather than on a boolean.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from decimal import Decimal

import pytest

from smart_money_bot import stream as stream_module
from smart_money_bot.database import Database
from smart_money_bot.stream import (
    CONFIGURATION_STREAM_STATES,
    STREAM_CONNECTED,
    STREAM_DISABLED,
    STREAM_NO_WALLETS,
    STREAM_NO_WS_URL,
    STREAM_RECONNECTING,
    STREAM_STALE,
    STREAM_STATES,
    RealtimeWalletStream,
    derive_ws_url,
)

D = Decimal


@pytest.fixture
async def database():
    db = Database(":memory:", D("1000"))
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


def _stream(database, **kwargs) -> RealtimeWalletStream:
    defaults = {
        "rpc_url": "https://rpc.example/",
        "explicit_ws_url": None,
        "enabled": True,
    }
    defaults.update(kwargs)
    return RealtimeWalletStream(database, **defaults)


async def test_a_disabled_lane_says_so_instead_of_just_disconnected(database) -> None:
    """Root cause 1: the task was never created, and nothing said why."""

    stream = _stream(database, enabled=False)
    health = stream.health()
    assert health.state == STREAM_DISABLED
    assert health.connected is False
    assert health.healthy is False
    assert "REALTIME_WALLET_STREAM_ENABLED" in health.detail
    assert health.state in CONFIGURATION_STREAM_STATES


async def test_an_underivable_websocket_url_is_named_not_hidden(database) -> None:
    """Root cause 1b: a bad RPC URL is a configuration fault, not a network one."""

    stream = _stream(database, rpc_url="not-a-url", enabled=True)
    assert stream.enabled is False
    health = stream.health()
    assert health.state == STREAM_NO_WS_URL
    assert health.state in CONFIGURATION_STREAM_STATES
    assert derive_ws_url("not-a-url") is None
    assert derive_ws_url("https://rpc.example/x") == "wss://rpc.example/x"


async def test_having_no_wallets_is_its_own_state(database) -> None:
    """Root cause 2: zero enabled traders is not the same as "broken"."""

    stream = _stream(database)
    await stream._run_connection()
    health = stream.health()
    assert health.state == STREAM_NO_WALLETS
    assert health.connected is False
    assert health.subscriptions == 0
    # It is also not an error — nothing to escalate, nothing to retry harder.
    assert health.last_error == ""


async def test_a_disabled_lane_keeps_reporting_instead_of_ending_its_task(database) -> None:
    """The supervisor stays alive so the status surface keeps a reason to show."""

    stream = _stream(database, enabled=False)
    task = asyncio.create_task(stream.run())
    await asyncio.sleep(0)
    assert task.done() is False
    assert stream.health().state == STREAM_DISABLED
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_every_reconnect_attempt_is_counted(database) -> None:
    """Root cause: reconnects stuck at 0 made a dead lane look idle."""

    stream = _stream(database)
    attempts = 0

    async def failing_connection():
        nonlocal attempts
        attempts += 1
        if attempts >= 3:
            raise asyncio.CancelledError
        raise ConnectionError("provider dropped the socket")

    stream._run_connection = failing_connection
    stream.MIN_BACKOFF_SECONDS = 0
    stream.MAX_BACKOFF_SECONDS = 0
    with pytest.raises(asyncio.CancelledError):
        await stream.run()
    assert stream.reconnects >= 2
    assert stream.failed_attempts >= 2
    assert stream.health().state == STREAM_RECONNECTING
    assert "provider dropped" in stream.health().last_error


async def test_the_lane_reports_connected_only_after_a_subscription_is_acknowledged(
    database,
) -> None:
    """An open socket with no live subscription is not a working lane."""

    stream = _stream(database)
    assert stream.health().connected is False
    stream._set_state(STREAM_CONNECTED)
    assert stream.health().connected is True
    assert stream.health().healthy is True
    assert stream.health().fallback_active is False


async def test_a_silent_socket_is_detected_as_stale_rather_than_healthy(database) -> None:
    """Root cause 3: connected-but-dead used to look fine forever."""

    stream = _stream(database)
    stream._set_state(STREAM_CONNECTED)
    assert stream.health().healthy is True
    stream._set_state(STREAM_STALE, error="no WebSocket traffic for 200s")
    health = stream.health()
    assert health.state == STREAM_STALE
    assert health.healthy is False
    assert health.fallback_active is True
    assert "rebuilding subscriptions" in health.detail or health.last_error


async def test_a_lane_that_stays_down_escalates_to_the_operator(database) -> None:
    """Section 54: losing smart-money intelligence silently is not acceptable."""

    warnings = []

    async def record(health):
        warnings.append(health)

    stream = _stream(database, enabled=False, on_health_warning=record)
    stream.WARN_AFTER_SECONDS = 0
    await stream._maybe_warn()
    assert warnings
    assert warnings[0].state == STREAM_DISABLED
    assert warnings[0].needs_operator_warning is True or warnings[0].down_for_seconds == 0


async def test_a_warning_channel_failure_never_kills_the_lane(database) -> None:
    async def explode(health):
        raise RuntimeError("discord is down")

    stream = _stream(database, enabled=False, on_health_warning=explode)
    stream.WARN_AFTER_SECONDS = 0
    await stream._maybe_warn()  # must not raise
    assert stream.health().state == STREAM_DISABLED


async def test_recovery_clears_the_down_clock(database) -> None:
    stream = _stream(database)
    stream._set_state(STREAM_RECONNECTING, error="boom")
    assert stream.unhealthy_since is not None
    stream._set_state(STREAM_CONNECTED)
    assert stream.unhealthy_since is None
    assert stream.health().down_for_seconds is None
    assert stream.health().fallback_active is False


async def test_the_fallback_flag_is_declared_not_accidental(database) -> None:
    """While the socket lane is down, the polling scan lane is the stated source."""

    stream = _stream(database, enabled=False)
    assert stream.fallback_active is True
    stream._set_state(STREAM_CONNECTED)
    assert stream.fallback_active is False


async def test_the_fallback_flag_can_never_contradict_the_state(database) -> None:
    """Two fields that must agree are two fields that can disagree.

    A freshly constructed, enabled lane starts in CONNECTING — not connected —
    so it must already declare the fallback.  Storing the flag separately from
    the state let the two drift apart before the first transition.
    """

    stream = _stream(database, enabled=True)
    assert stream.state != STREAM_CONNECTED
    assert stream.fallback_active is True

    for state in STREAM_STATES:
        stream._set_state(state)
        assert stream.fallback_active == (state != STREAM_CONNECTED), state
        assert stream.health().fallback_active == stream.fallback_active, state
        assert stream.health().connected == (state == STREAM_CONNECTED), state


def test_every_named_state_is_reachable_and_unique() -> None:
    assert len(set(STREAM_STATES)) == len(STREAM_STATES)
    for state in (
        STREAM_DISABLED,
        STREAM_NO_WS_URL,
        STREAM_NO_WALLETS,
        STREAM_CONNECTED,
        STREAM_RECONNECTING,
        STREAM_STALE,
    ):
        assert state in STREAM_STATES


def test_errors_never_leak_an_api_key() -> None:
    redacted = stream_module._safe_error(
        ConnectionError("failed https://rpc.example/?api-key=SUPERSECRET")
    )
    assert "SUPERSECRET" not in redacted
    assert "<redacted>" in redacted
