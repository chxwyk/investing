"""One authoritative chronological event stream per mint (sections M and N).

Every strategy decision must be reconstructable from only the events that
existed at its timestamp.  That is enforced structurally: replay reads through
:meth:`TokenTimeline.before`, which cannot return a later event, so a future
price can never leak into a past decision.

Events also carry their own provenance — which provider produced them, when the
source observed them, when the lab observed them, and whether the value was read
directly or derived — so stale and fresh evidence are never silently merged.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .decision import EvidenceQuality

# --- event types (section M) ------------------------------------------------
TOKEN_DISCOVERED = "TOKEN_DISCOVERED"
PAIR_CREATED = "PAIR_CREATED"
PRICE_OBSERVED = "PRICE_OBSERVED"
MARKET_CAP_OBSERVED = "MARKET_CAP_OBSERVED"
LIQUIDITY_OBSERVED = "LIQUIDITY_OBSERVED"
HOLDER_OBSERVED = "HOLDER_OBSERVED"
SAFETY_OBSERVED = "SAFETY_OBSERVED"
BUY_FLOW_CHANGED = "BUY_FLOW_CHANGED"
SELL_FLOW_CHANGED = "SELL_FLOW_CHANGED"
VOLUME_CHANGED = "VOLUME_CHANGED"
WALLET_BUY = "WALLET_BUY"
WALLET_SELL = "WALLET_SELL"
INDEPENDENT_BUYER_GROWTH = "INDEPENDENT_BUYER_GROWTH"
FUNDING_CLUSTER_CHANGED = "FUNDING_CLUSTER_CHANGED"
SOCIAL_MENTION = "SOCIAL_MENTION"
KNOWN_TRADER_SIGNAL = "KNOWN_TRADER_SIGNAL"
SMART_WALLET_ENTRY = "SMART_WALLET_ENTRY"
SMART_WALLET_EXIT = "SMART_WALLET_EXIT"
QUALIFIED = "QUALIFIED"
REJECTED = "REJECTED"
ALERTED = "ALERTED"
PAPER_ENTRY = "PAPER_ENTRY"
PAPER_PARTIAL_EXIT = "PAPER_PARTIAL_EXIT"
PAPER_EXIT = "PAPER_EXIT"
LIFECYCLE_CHANGED = "LIFECYCLE_CHANGED"
NEW_HIGH = "NEW_HIGH"
DRAWDOWN = "DRAWDOWN"
LIQUIDITY_DANGER = "LIQUIDITY_DANGER"
SAFETY_DANGER = "SAFETY_DANGER"

EVENT_TYPES: frozenset[str] = frozenset(
    {
        TOKEN_DISCOVERED,
        PAIR_CREATED,
        PRICE_OBSERVED,
        MARKET_CAP_OBSERVED,
        LIQUIDITY_OBSERVED,
        HOLDER_OBSERVED,
        SAFETY_OBSERVED,
        BUY_FLOW_CHANGED,
        SELL_FLOW_CHANGED,
        VOLUME_CHANGED,
        WALLET_BUY,
        WALLET_SELL,
        INDEPENDENT_BUYER_GROWTH,
        FUNDING_CLUSTER_CHANGED,
        SOCIAL_MENTION,
        KNOWN_TRADER_SIGNAL,
        SMART_WALLET_ENTRY,
        SMART_WALLET_EXIT,
        QUALIFIED,
        REJECTED,
        ALERTED,
        PAPER_ENTRY,
        PAPER_PARTIAL_EXIT,
        PAPER_EXIT,
        LIFECYCLE_CHANGED,
        NEW_HIGH,
        DRAWDOWN,
        LIQUIDITY_DANGER,
        SAFETY_DANGER,
    }
)


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where one piece of evidence came from and how much it can be trusted."""

    source: str = "unknown"
    source_timestamp: int | None = None
    observed_at: int = 0
    cached: bool = False
    derived: bool = False
    confidence: Decimal = Decimal("50")
    quality: EvidenceQuality = EvidenceQuality.UNKNOWN

    def age_seconds(self, now: int) -> int | None:
        reference = self.source_timestamp or self.observed_at or None
        if reference is None:
            return None
        return max(0, now - reference)

    def is_stale(self, now: int, *, max_age_seconds: int) -> bool:
        age = self.age_seconds(now)
        return age is None or age > max_age_seconds


@dataclass(frozen=True, slots=True)
class TokenEvent:
    """An immutable fact about one mint at one instant."""

    mint: str
    event_type: str
    occurred_at: int
    payload: dict[str, Any] = field(default_factory=dict)
    provenance: Provenance = field(default_factory=Provenance)
    price_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None

    def __post_init__(self) -> None:
        if self.event_type not in EVENT_TYPES:
            raise ValueError(f"unknown token event type: {self.event_type}")

    @property
    def event_id(self) -> str:
        """Stable id so a retried or restarted write is idempotent."""

        body = json.dumps(
            {
                "mint": self.mint,
                "type": self.event_type,
                "at": self.occurred_at,
                "payload": _jsonable(self.payload),
                "source": self.provenance.source,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


def event_to_json(event: TokenEvent) -> str:
    return json.dumps(
        {
            "mint": event.mint,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at,
            "payload": _jsonable(event.payload),
            "price_usd": _text(event.price_usd),
            "market_cap_usd": _text(event.market_cap_usd),
            "provenance": {
                "source": event.provenance.source,
                "source_timestamp": event.provenance.source_timestamp,
                "observed_at": event.provenance.observed_at,
                "cached": event.provenance.cached,
                "derived": event.provenance.derived,
                "confidence": str(event.provenance.confidence),
                "quality": str(event.provenance.quality),
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def event_from_json(raw: str) -> TokenEvent:
    payload = json.loads(raw)
    provenance = payload.get("provenance") or {}
    return TokenEvent(
        mint=str(payload.get("mint") or ""),
        event_type=str(payload.get("event_type") or PRICE_OBSERVED),
        occurred_at=int(payload.get("occurred_at") or 0),
        payload=dict(payload.get("payload") or {}),
        provenance=Provenance(
            source=str(provenance.get("source") or "unknown"),
            source_timestamp=(
                int(provenance["source_timestamp"])
                if provenance.get("source_timestamp") is not None
                else None
            ),
            observed_at=int(provenance.get("observed_at") or 0),
            cached=bool(provenance.get("cached")),
            derived=bool(provenance.get("derived")),
            confidence=_decimal(provenance.get("confidence")) or Decimal("50"),
            quality=EvidenceQuality(str(provenance.get("quality") or "UNKNOWN")),
        ),
        price_usd=_decimal(payload.get("price_usd")),
        market_cap_usd=_decimal(payload.get("market_cap_usd")),
    )


class TokenTimeline:
    """An ordered, de-duplicated event stream for one mint.

    Ordering is by ``occurred_at`` then insertion order, so two events written
    in the same second keep the order in which they were observed.
    """

    __slots__ = ("_events", "_ids", "_keys", "mint")

    def __init__(self, mint: str, events: Iterable[TokenEvent] = ()) -> None:
        self.mint = mint
        self._events: list[TokenEvent] = []
        self._keys: list[int] = []
        self._ids: set[str] = set()
        for event in events:
            self.append(event)

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[TokenEvent]:
        return iter(self._events)

    @property
    def events(self) -> tuple[TokenEvent, ...]:
        return tuple(self._events)

    def append(self, event: TokenEvent) -> bool:
        """Add one event.  Returns ``False`` when it was already recorded."""

        if event.mint != self.mint:
            raise ValueError("event belongs to a different mint")
        identifier = event.event_id
        if identifier in self._ids:
            return False
        self._ids.add(identifier)
        index = bisect_right(self._keys, event.occurred_at)
        self._keys.insert(index, event.occurred_at)
        self._events.insert(index, event)
        return True

    def extend(self, events: Iterable[TokenEvent]) -> int:
        return sum(1 for event in events if self.append(event))

    def before(self, timestamp: int, *, inclusive: bool = True) -> tuple[TokenEvent, ...]:
        """Every event visible at ``timestamp``.  This is the no-lookahead gate."""

        cut = bisect_right(self._keys, timestamp) if inclusive else _left(self._keys, timestamp)
        return tuple(self._events[:cut])

    def of_type(self, *event_types: str) -> tuple[TokenEvent, ...]:
        wanted = set(event_types)
        return tuple(event for event in self._events if event.event_type in wanted)

    def latest(self, event_type: str, *, at: int | None = None) -> TokenEvent | None:
        source = self.before(at) if at is not None else self._events
        for event in reversed(source):
            if event.event_type == event_type:
                return event
        return None

    def first(self, event_type: str) -> TokenEvent | None:
        for event in self._events:
            if event.event_type == event_type:
                return event
        return None

    def price_path(self, *, until: int | None = None) -> tuple[tuple[int, Decimal], ...]:
        source = self.before(until) if until is not None else self._events
        return tuple(
            (event.occurred_at, event.price_usd)
            for event in source
            if event.price_usd is not None
        )

    def market_cap_path(self, *, until: int | None = None) -> tuple[tuple[int, Decimal], ...]:
        source = self.before(until) if until is not None else self._events
        return tuple(
            (event.occurred_at, event.market_cap_usd)
            for event in source
            if event.market_cap_usd is not None
        )

    def peak_price(self, *, until: int | None = None) -> Decimal | None:
        path = self.price_path(until=until)
        return max((value for _, value in path), default=None)

    def peak_market_cap(self, *, until: int | None = None) -> Decimal | None:
        path = self.market_cap_path(until=until)
        return max((value for _, value in path), default=None)


def _left(keys: Sequence[int], timestamp: int) -> int:
    low, high = 0, len(keys)
    while low < high:
        middle = (low + high) // 2
        if keys[middle] < timestamp:
            low = middle + 1
        else:
            high = middle
    return low


def observation_events(
    mint: str,
    *,
    occurred_at: int,
    price_usd: Decimal | None = None,
    market_cap_usd: Decimal | None = None,
    liquidity_usd: Decimal | None = None,
    holder_count: int | None = None,
    buys: int | None = None,
    sells: int | None = None,
    volume_usd: Decimal | None = None,
    provenance: Provenance | None = None,
) -> tuple[TokenEvent, ...]:
    """Turn one market snapshot into its individual timeline facts."""

    source = provenance or Provenance(observed_at=occurred_at)
    events: list[TokenEvent] = []

    def add(event_type: str, payload: dict[str, Any]) -> None:
        events.append(
            TokenEvent(
                mint=mint,
                event_type=event_type,
                occurred_at=occurred_at,
                payload=payload,
                provenance=source,
                price_usd=price_usd,
                market_cap_usd=market_cap_usd,
            )
        )

    if price_usd is not None:
        add(PRICE_OBSERVED, {"price_usd": str(price_usd)})
    if market_cap_usd is not None:
        add(MARKET_CAP_OBSERVED, {"market_cap_usd": str(market_cap_usd)})
    if liquidity_usd is not None:
        add(LIQUIDITY_OBSERVED, {"liquidity_usd": str(liquidity_usd)})
    if holder_count is not None:
        add(HOLDER_OBSERVED, {"holder_count": holder_count})
    if buys is not None:
        add(BUY_FLOW_CHANGED, {"buys": buys})
    if sells is not None:
        add(SELL_FLOW_CHANGED, {"sells": sells})
    if volume_usd is not None:
        add(VOLUME_CHANGED, {"volume_usd": str(volume_usd)})
    return tuple(events)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, tuple | list | set):
        return [_jsonable(item) for item in value]
    return value
