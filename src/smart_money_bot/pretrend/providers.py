"""Adapters between the outside world and the pre-trend engine's types.

Two seams live here.

**The board observer.**  The repository already resolves Trending provenance and
fetches a board (:mod:`smart_money_bot.trending_source`).  Rather than build a
second fetcher, :class:`BoardObserver` wraps whatever client that resolution
produced and converts its rows into :class:`~...groundtruth.TrendingSnapshot`
objects, attaching the failure information the ground-truth layer needs to tell
"quiet" from "broken".  Crucially, an exception or an empty result becomes a
snapshot **with an error set**, not an empty board — the ground-truth layer then
refuses it instead of emitting a LEFT event for every mint.

**The FOMO activity feed.**  :class:`AuthorisedActivityClient` reads an
operator-configured endpoint.  There is no default: without
``FOMO_ACTIVITY_API_URL`` the engine uses
:class:`~...activity.NullActivityProvider`, reports the lane as unconfigured,
and continues.  This module does not scrape, does not reuse a browser session,
does not replay a cookie and does not call an undocumented authenticated
endpoint it was not given.  That is a hard limit on what this deployment can
measure, and it is recorded as a blocker rather than worked around.

Both adapters are defensive about the shapes they read: a field a provider omits
produces ``None``, never a substituted guess, and a row whose exact mint cannot
be resolved is dropped rather than keyed by ticker.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .activity import ActivityEvent, NullActivityProvider
from .groundtruth import BoardRow, TrendingSnapshot

#: Stamped onto every snapshot and event so a stored row can be traced to the
#: code that produced it (section 70).
COLLECTOR_VERSION = "pretrend-collector/1.0"


class BoardClient(Protocol):
    """The subset of the existing Trending client this adapter needs."""

    last_error: str

    async def fetch_board(self, *, limit: int) -> Sequence[Any]: ...


def _row_from_observation(observation: Any, *, index: int) -> BoardRow | None:
    """Convert an existing ``TrendingObservation`` into a ground-truth row."""

    mint = str(getattr(observation, "mint", "") or "")
    if not mint:
        return None
    source = getattr(observation, "source", None)
    return BoardRow(
        mint=mint,
        rank=getattr(observation, "rank", None) or index,
        symbol=str(getattr(observation, "symbol", "") or "")[:32],
        name=str(getattr(observation, "name", "") or "")[:120],
        tier=str(getattr(observation, "tier", "") or "")[:16],
        market_cap_usd=getattr(observation, "market_cap_usd", None),
        price_usd=getattr(observation, "price_usd", None),
        liquidity_usd=getattr(observation, "liquidity_usd", None),
        volume_usd=getattr(observation, "volume_usd", None),
        holders=getattr(observation, "holder_count", None),
        raw={
            "provider": getattr(source, "provider", "") if source else "",
            "verification": getattr(observation, "verification", ""),
            "displayed_change_percent": str(
                getattr(observation, "displayed_change_percent", "") or ""
            ),
            "change_window": getattr(observation, "change_window", ""),
        },
    )


class BoardObserver:
    """Turns board fetches into snapshots that carry their own failure state."""

    def __init__(
        self,
        client: BoardClient,
        *,
        provider: str = "",
        source_kind: str = "",
        limit: int = 60,
        timeout_seconds: int = 15,
    ) -> None:
        self.client = client
        self.provider = provider
        self.source_kind = source_kind
        self.limit = limit
        self.timeout_seconds = timeout_seconds
        self.attempts = 0
        self.failures = 0
        self.last_error = ""

    async def observe(self, *, now: int | None = None) -> TrendingSnapshot:
        """One reading.  Every failure path yields a snapshot with ``error`` set."""

        moment = now if now is not None else int(time.time())
        self.attempts += 1
        try:
            async with asyncio.timeout(self.timeout_seconds):
                observations = await self.client.fetch_board(limit=self.limit)
        except TimeoutError:
            self.failures += 1
            self.last_error = f"board fetch timed out after {self.timeout_seconds}s"
            return self._failed(moment, self.last_error)
        except Exception as exc:  # pragma: no cover - defensive; loop must survive
            self.failures += 1
            self.last_error = str(exc)[:200] or "board fetch raised"
            return self._failed(moment, self.last_error)

        client_error = str(getattr(self.client, "last_error", "") or "")
        rows = tuple(
            row
            for row in (
                _row_from_observation(observation, index=index)
                for index, observation in enumerate(observations, start=1)
            )
            if row is not None
        )
        if not rows:
            self.failures += 1
            self.last_error = client_error or "board returned no resolvable mints"
            return self._failed(moment, self.last_error)

        self.last_error = client_error
        return TrendingSnapshot(
            observed_at=moment,
            rows=rows,
            provider=self.provider,
            source_kind=self.source_kind,
            # A client-level error alongside usable rows means a partial read;
            # the ground-truth layer treats that as invalid, which is correct:
            # a partial board produces spurious LEFT events.
            error=client_error,
            collector_version=COLLECTOR_VERSION,
        )

    def _failed(self, moment: int, error: str) -> TrendingSnapshot:
        return TrendingSnapshot(
            observed_at=moment,
            rows=(),
            provider=self.provider,
            source_kind=self.source_kind,
            error=error,
            collector_version=COLLECTOR_VERSION,
        )

    def health(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "failures": self.failures,
            "failure_rate": (
                None if self.attempts == 0 else round(self.failures / self.attempts, 4)
            ),
            "last_error": self.last_error,
            "provider": self.provider,
            "source_kind": self.source_kind,
        }


class AuthorisedActivityClient:
    """An operator-configured FOMO activity feed.  There is no default endpoint.

    The response is read defensively and every row must resolve to an exact
    mint.  Rows that do not are counted in ``dropped_rows`` and reported, so a
    feed that mostly returns unusable data is visible as such instead of
    silently producing a thin tape.
    """

    name = "authorised_fomo_activity"

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None = None,
        session_factory: Any = None,
        timeout_seconds: int = 12,
        page_limit: int = 200,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.page_limit = page_limit
        self.available = bool(url)
        self.last_error = "" if url else "no FOMO activity URL configured"
        self.requests = 0
        self.dropped_rows = 0
        self._session_factory = session_factory
        self._session: Any = None

    async def _get_session(self) -> Any:
        if self._session is None or getattr(self._session, "closed", False):
            if self._session_factory is not None:
                self._session = self._session_factory()
            else:
                import aiohttp

                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
                    headers={"User-Agent": COLLECTOR_VERSION},
                )
        return self._session

    async def fetch_since(
        self, *, since: int, limit: int = 200
    ) -> tuple[ActivityEvent, ...]:
        if not self.available:
            return ()
        session = await self._get_session()
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        params = {"since": str(since), "limit": str(min(limit, self.page_limit))}
        self.requests += 1
        received_at = int(time.time())
        try:
            async with session.get(self.url, headers=headers, params=params) as response:
                status = getattr(response, "status", 200)
                if status >= 400:
                    self.last_error = f"FOMO activity feed HTTP {status}"
                    return ()
                payload = await response.json(content_type=None)
        except Exception as exc:
            self.last_error = str(exc)[:200] or "FOMO activity request failed"
            return ()

        rows = payload
        if isinstance(payload, Mapping):
            for key in ("data", "events", "results", "activity"):
                value = payload.get(key)
                if isinstance(value, list):
                    rows = value
                    break
        if not isinstance(rows, list):
            self.last_error = "FOMO activity feed returned an unexpected shape"
            return ()

        events: list[ActivityEvent] = []
        for item in rows:
            if not isinstance(item, Mapping):
                self.dropped_rows += 1
                continue
            event = ActivityEvent.from_payload(
                item, provider=self.name, received_at=received_at
            )
            if event is None:
                # No exact mint, or no usable timestamp.  Not a weak event — not
                # an event, because we cannot say which token it belongs to.
                self.dropped_rows += 1
                continue
            events.append(event)

        self.last_error = "" if events else "FOMO activity feed returned no usable rows"
        return tuple(events)

    async def close(self) -> None:
        if self._session is not None and not getattr(self._session, "closed", True):
            await self._session.close()
        self._session = None

    def health(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "available": self.available,
            "requests": self.requests,
            "dropped_rows": self.dropped_rows,
            "last_error": self.last_error,
        }


@dataclass(frozen=True, slots=True)
class ActivityLaneStatus:
    """Whether the FOMO-native lane is configured, and what it can measure."""

    configured: bool
    provider: str
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "provider": self.provider,
            "detail": self.detail,
        }


def build_activity_provider(
    *, url: str | None, api_key: str | None = None
) -> tuple[Any, ActivityLaneStatus]:
    """Resolve the activity lane from configuration alone.

    There is deliberately no discovery path: the engine never finds a feed by
    guessing a hostname or probing an endpoint.
    """

    cleaned = (url or "").strip()
    if not cleaned:
        provider = NullActivityProvider()
        return (
            provider,
            ActivityLaneStatus(
                configured=False,
                provider="none",
                detail=(
                    "No FOMO activity feed is configured. Every FOMO-native "
                    "feature is UNKNOWN, not zero. Set FOMO_ACTIVITY_API_URL to "
                    "an authorised endpoint to enable the lane."
                ),
            ),
        )
    client = AuthorisedActivityClient(url=cleaned, api_key=api_key)
    return (
        client,
        ActivityLaneStatus(
            configured=True,
            provider=client.name,
            detail=f"Operator-configured FOMO activity feed at {cleaned}",
        ),
    )
