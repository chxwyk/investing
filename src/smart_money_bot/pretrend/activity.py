"""The FOMO-native tape: what happens *inside* FOMO before a token trends.

The hypothesis this module exists to test — and it is a hypothesis, not a
finding — is that generic DEX volume is already late.  By the time a token's
1-minute volume is visibly unusual on DEX Screener, the people who made it
unusual have been buying for several minutes somewhere with a shorter feedback
loop.  If FOMO is that place, then FOMO-native activity leads the board entry,
and the lead time is measurable.  If it does not, section 58 obliges us to say
so; :mod:`smart_money_bot.pretrend.cascade` is where that gets measured rather
than assumed.

**Access status.**  This deployment has no authorised FOMO activity feed.  There
is no documented public endpoint for per-token FOMO buys, sells and theses that
this code may call, and this module will not scrape one, reuse a browser
session, replay a cookie or reverse a private endpoint to get it.  So what is
built here is the full shape of the thing:

* a typed event model with mandatory exact-mint identity,
* a provider protocol an operator can satisfy by configuring an authorised feed,
* a :class:`NullActivityProvider` that returns nothing and says why,
* a :class:`ReplayActivityProvider` that serves recorded events for tests and
  historical replay,
* the complete rolling-window feature computation that runs identically over
  live or replayed events.

When an authorised feed appears, one adapter class is the only new code needed.
Everything downstream — features, labels, affinity, models, cards — already
works, because it works today against recorded events.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from .identity import extract_mint
from .windows import (
    Sample,
    Series,
    counter_dynamics,
    sum_dynamics,
    to_decimal,
    window_label,
)

ZERO = Decimal("0")

# --- event types -------------------------------------------------------------
ACTIVITY_BUY = "BUY"
ACTIVITY_SELL = "SELL"
ACTIVITY_THESIS = "THESIS"
ACTIVITY_DISCUSSION = "TOKEN_DISCUSSION"
ACTIVITY_OTHER = "OTHER"

ACTIVITY_TYPES: tuple[str, ...] = (
    ACTIVITY_BUY,
    ACTIVITY_SELL,
    ACTIVITY_THESIS,
    ACTIVITY_DISCUSSION,
    ACTIVITY_OTHER,
)

#: The windows the FOMO-native features are computed over (section 15).
ACTIVITY_WINDOWS_SECONDS: tuple[int, ...] = (15, 30, 60, 120, 180, 300, 600, 900)


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    """One public FOMO action, keyed to an exact mint.

    ``mint`` is mandatory.  An activity record we cannot attribute to a specific
    token is not a weak signal, it is not a signal: attributing it by ticker
    would let one token's tape contaminate another's, and the contamination
    would flow straight into the affinity numbers and the labels.
    """

    event_id: str
    mint: str
    occurred_at: int
    event_type: str
    #: Opaque, stable identifier for the FOMO account.  A public handle is a
    #: display name and can change; the id is what makes a history joinable.
    trader_id: str = ""
    handle: str = ""
    profile_url: str = ""
    amount_usd: Decimal | None = None
    token_amount: Decimal | None = None
    market_cap_usd: Decimal | None = None
    price_usd: Decimal | None = None
    thesis_text: str = ""
    token_name: str = ""
    token_symbol: str = ""
    chain: str = "solana"
    provider: str = ""
    #: When our collector received it, as distinct from when it happened.
    received_at: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.mint:
            raise ValueError("a FOMO activity event requires the exact mint")
        if self.event_type not in ACTIVITY_TYPES:
            object.__setattr__(self, "event_type", ACTIVITY_OTHER)

    @property
    def is_buy(self) -> bool:
        return self.event_type == ACTIVITY_BUY

    @property
    def is_sell(self) -> bool:
        return self.event_type == ACTIVITY_SELL

    @property
    def is_thesis(self) -> bool:
        return self.event_type == ACTIVITY_THESIS

    @property
    def lag_seconds(self) -> int | None:
        return None if self.received_at is None else self.received_at - self.occurred_at

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, provider: str = "", received_at: int | None = None
    ) -> ActivityEvent | None:
        """Parse a provider payload, or return ``None`` when it is unusable."""

        mint = None
        for key in ("mint", "address", "tokenAddress", "token_address", "ca"):
            mint = extract_mint(payload.get(key))
            if mint:
                break
        if not mint:
            return None
        occurred = payload.get("occurred_at") or payload.get("timestamp") or payload.get("time")
        try:
            occurred_at = int(occurred)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        raw_type = str(payload.get("type") or payload.get("event_type") or "").upper()
        event_type = raw_type if raw_type in ACTIVITY_TYPES else ACTIVITY_OTHER
        trader_id = str(
            payload.get("trader_id") or payload.get("user_id") or payload.get("userId") or ""
        )[:120]
        event_id = str(payload.get("id") or payload.get("event_id") or "")[:160]
        if not event_id:
            # A deterministic id so a provider that replays the same event twice
            # is de-duplicated rather than counted twice (section 78).
            event_id = f"{provider}:{mint}:{trader_id}:{event_type}:{occurred_at}"
        return cls(
            event_id=event_id,
            mint=mint,
            occurred_at=occurred_at,
            event_type=event_type,
            trader_id=trader_id,
            handle=str(payload.get("handle") or payload.get("username") or "")[:120],
            profile_url=str(payload.get("profile_url") or payload.get("profileUrl") or "")[:300],
            amount_usd=to_decimal(payload.get("amount_usd") or payload.get("usd")),
            token_amount=to_decimal(payload.get("token_amount") or payload.get("amount")),
            market_cap_usd=to_decimal(payload.get("market_cap") or payload.get("marketCap")),
            price_usd=to_decimal(payload.get("price") or payload.get("priceUsd")),
            thesis_text=str(payload.get("thesis") or payload.get("text") or "")[:2000],
            token_name=str(payload.get("name") or "")[:120],
            token_symbol=str(payload.get("symbol") or "")[:32],
            chain=str(payload.get("chain") or "solana")[:24],
            provider=provider,
            received_at=received_at,
            raw=dict(payload),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "mint": self.mint,
            "occurred_at": self.occurred_at,
            "received_at": self.received_at,
            "event_type": self.event_type,
            "trader_id": self.trader_id,
            "handle": self.handle,
            "profile_url": self.profile_url,
            "amount_usd": None if self.amount_usd is None else str(self.amount_usd),
            "token_amount": None if self.token_amount is None else str(self.token_amount),
            "market_cap_usd": None if self.market_cap_usd is None else str(self.market_cap_usd),
            "price_usd": None if self.price_usd is None else str(self.price_usd),
            "thesis_text": self.thesis_text,
            "token_name": self.token_name,
            "token_symbol": self.token_symbol,
            "chain": self.chain,
            "provider": self.provider,
        }


# --- provider seam -----------------------------------------------------------
class ActivityProvider(Protocol):
    """What an authorised FOMO activity feed must offer.

    ``available`` is part of the contract so a caller can distinguish "the feed
    says nothing happened" from "there is no feed" without inspecting types.
    """

    name: str
    available: bool
    last_error: str

    async def fetch_since(self, *, since: int, limit: int) -> tuple[ActivityEvent, ...]: ...

    async def close(self) -> None: ...


class NullActivityProvider:
    """No authorised feed is configured.  Returns nothing and says so."""

    name = "none"

    def __init__(self, reason: str = "") -> None:
        self.available = False
        self.last_error = reason or (
            "no authorised FOMO activity feed is configured "
            "(set FOMO_ACTIVITY_API_URL); the engine will not scrape one"
        )

    async def fetch_since(self, *, since: int, limit: int) -> tuple[ActivityEvent, ...]:
        return ()

    async def close(self) -> None:
        return None


class ReplayActivityProvider:
    """Serves recorded events in time order.  Used by tests and replay.

    It refuses to hand back an event later than the cursor it was asked for, so
    a replay cannot accidentally obtain future tape.
    """

    name = "replay"

    def __init__(self, events: Iterable[ActivityEvent]) -> None:
        self._events = sorted(events, key=lambda event: event.occurred_at)
        self.available = True
        self.last_error = ""

    async def fetch_since(self, *, since: int, limit: int) -> tuple[ActivityEvent, ...]:
        selected = [event for event in self._events if event.occurred_at > since]
        return tuple(selected[:limit])

    def until(self, moment: int) -> tuple[ActivityEvent, ...]:
        return tuple(event for event in self._events if event.occurred_at <= moment)

    async def close(self) -> None:
        return None


# --- the per-mint tape -------------------------------------------------------
class ActivityTape:
    """Append-only FOMO-native events for ONE exact mint, plus derived series.

    Duplicate ``event_id`` values are ignored.  Provider retries and overlapping
    polls both replay events routinely, and a double-counted buy inflates every
    velocity, every percentile and ultimately the model's confidence in a signal
    that never happened twice (section 78).
    """

    __slots__ = (
        "mint",
        "_ids",
        "_events",
        "buys",
        "sells",
        "theses",
        "buy_usd",
        "sell_usd",
        "_first_seen_by_trader",
    )

    def __init__(self, mint: str) -> None:
        self.mint = mint
        self._ids: set[str] = set()
        self._events: list[ActivityEvent] = []
        self.buys = Series("fomo_buys")
        self.sells = Series("fomo_sells")
        self.theses = Series("fomo_theses")
        self.buy_usd = Series("fomo_buy_usd")
        self.sell_usd = Series("fomo_sell_usd")
        self._first_seen_by_trader: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._events)

    @property
    def events(self) -> tuple[ActivityEvent, ...]:
        return tuple(self._events)

    def add(self, event: ActivityEvent) -> bool:
        """Record an event.  Returns False for a duplicate or a foreign mint."""

        if event.mint != self.mint or event.event_id in self._ids:
            return False
        self._ids.add(event.event_id)
        index = len(self._events)
        while index > 0 and self._events[index - 1].occurred_at > event.occurred_at:
            index -= 1
        self._events.insert(index, event)

        if event.trader_id:
            seen = self._first_seen_by_trader.get(event.trader_id)
            if seen is None or event.occurred_at < seen:
                self._first_seen_by_trader[event.trader_id] = event.occurred_at

        unit = Sample(at=event.occurred_at, value=Decimal("1"), provider=event.provider)
        if event.is_buy:
            self.buys.append(unit)
            if event.amount_usd is not None:
                self.buy_usd.append(
                    Sample(at=event.occurred_at, value=event.amount_usd, provider=event.provider)
                )
        elif event.is_sell:
            self.sells.append(unit)
            if event.amount_usd is not None:
                self.sell_usd.append(
                    Sample(at=event.occurred_at, value=event.amount_usd, provider=event.provider)
                )
        elif event.is_thesis:
            self.theses.append(unit)
        return True

    def extend(self, events: Iterable[ActivityEvent]) -> int:
        return sum(1 for event in events if self.add(event))

    # ------------------------------------------------------------------
    def before(self, at: int) -> tuple[ActivityEvent, ...]:
        """Every event at or before ``at``.  The only door for feature code."""

        return tuple(event for event in self._events if event.occurred_at <= at)

    def in_window(self, *, end: int, seconds: int) -> tuple[ActivityEvent, ...]:
        start = end - seconds
        return tuple(
            event for event in self._events if start < event.occurred_at <= end
        )

    def first_seen_at(self, trader_id: str) -> int | None:
        return self._first_seen_by_trader.get(trader_id)

    def traders_before(self, at: int) -> frozenset[str]:
        return frozenset(
            event.trader_id
            for event in self._events
            if event.occurred_at <= at and event.trader_id
        )


def _percentile(values: Sequence[Decimal], fraction: Decimal) -> Decimal | None:
    """Nearest-rank percentile.  Returns ``None`` rather than guessing on empty."""

    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = int((fraction * Decimal(len(ordered) - 1)).to_integral_value(rounding="ROUND_HALF_UP"))
    return ordered[max(0, min(index, len(ordered) - 1))]


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return ((ordered[middle - 1] + ordered[middle]) / 2).quantize(Decimal("0.00000001"))


@dataclass(frozen=True, slots=True)
class ActivityWindowFeatures:
    """The section-15 feature block for one mint over one window, at one instant."""

    mint: str
    at: int
    window_seconds: int

    fomo_buys: int = 0
    fomo_sells: int = 0
    fomo_buy_usd: Decimal | None = None
    fomo_sell_usd: Decimal | None = None
    fomo_net_buy_usd: Decimal | None = None

    unique_fomo_buyers: int = 0
    unique_fomo_sellers: int = 0
    #: Buyers whose first-ever event on this mint falls inside this window.
    new_fomo_buyers: int = 0

    new_fomo_buyers_per_min: Decimal | None = None
    new_fomo_buyer_velocity: Decimal | None = None
    new_fomo_buyer_acceleration: Decimal | None = None

    fomo_buy_volume_velocity: Decimal | None = None
    fomo_buy_volume_acceleration: Decimal | None = None

    fomo_buy_sell_tx_ratio: Decimal | None = None
    fomo_buy_sell_notional_ratio: Decimal | None = None

    median_fomo_buy_size: Decimal | None = None
    mean_fomo_buy_size: Decimal | None = None
    p90_fomo_buy_size: Decimal | None = None
    largest_fomo_buy: Decimal | None = None

    fomo_thesis_count: int = 0
    new_theses_per_min: Decimal | None = None
    unique_thesis_authors: int = 0
    thesis_velocity: Decimal | None = None
    thesis_acceleration: Decimal | None = None

    #: How many buy events carried no USD amount.  Notional features are
    #: unreliable when this is high, and the card says so rather than treating
    #: the missing amounts as zero.
    buys_missing_amount: int = 0

    @property
    def window(self) -> str:
        return window_label(self.window_seconds)

    def to_json(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "mint": self.mint,
            "at": self.at,
            "window_seconds": self.window_seconds,
            "window": self.window,
            "fomo_buys": self.fomo_buys,
            "fomo_sells": self.fomo_sells,
            "fomo_buy_usd": s(self.fomo_buy_usd),
            "fomo_sell_usd": s(self.fomo_sell_usd),
            "fomo_net_buy_usd": s(self.fomo_net_buy_usd),
            "unique_fomo_buyers": self.unique_fomo_buyers,
            "unique_fomo_sellers": self.unique_fomo_sellers,
            "new_fomo_buyers": self.new_fomo_buyers,
            "new_fomo_buyers_per_min": s(self.new_fomo_buyers_per_min),
            "new_fomo_buyer_velocity": s(self.new_fomo_buyer_velocity),
            "new_fomo_buyer_acceleration": s(self.new_fomo_buyer_acceleration),
            "fomo_buy_volume_velocity": s(self.fomo_buy_volume_velocity),
            "fomo_buy_volume_acceleration": s(self.fomo_buy_volume_acceleration),
            "fomo_buy_sell_tx_ratio": s(self.fomo_buy_sell_tx_ratio),
            "fomo_buy_sell_notional_ratio": s(self.fomo_buy_sell_notional_ratio),
            "median_fomo_buy_size": s(self.median_fomo_buy_size),
            "mean_fomo_buy_size": s(self.mean_fomo_buy_size),
            "p90_fomo_buy_size": s(self.p90_fomo_buy_size),
            "largest_fomo_buy": s(self.largest_fomo_buy),
            "fomo_thesis_count": self.fomo_thesis_count,
            "new_theses_per_min": s(self.new_theses_per_min),
            "unique_thesis_authors": self.unique_thesis_authors,
            "thesis_velocity": s(self.thesis_velocity),
            "thesis_acceleration": s(self.thesis_acceleration),
            "buys_missing_amount": self.buys_missing_amount,
        }


def activity_window_features(
    tape: ActivityTape, *, at: int, window_seconds: int
) -> ActivityWindowFeatures:
    """Compute the section-15 block.  Reads only events at or before ``at``."""

    window = tape.in_window(end=at, seconds=window_seconds)
    prior = tape.in_window(end=at - window_seconds, seconds=window_seconds)

    buys = [event for event in window if event.is_buy]
    sells = [event for event in window if event.is_sell]
    theses = [event for event in window if event.is_thesis]
    prior_buys = [event for event in prior if event.is_buy]

    buy_amounts = [event.amount_usd for event in buys if event.amount_usd is not None]
    sell_amounts = [event.amount_usd for event in sells if event.amount_usd is not None]

    buy_usd = sum(buy_amounts, ZERO) if buy_amounts else None
    sell_usd = sum(sell_amounts, ZERO) if sell_amounts else None
    net = None
    if buy_usd is not None or sell_usd is not None:
        net = (buy_usd or ZERO) - (sell_usd or ZERO)

    start = at - window_seconds
    new_buyers = {
        event.trader_id
        for event in buys
        if event.trader_id and (tape.first_seen_at(event.trader_id) or 0) > start
    }
    prior_start = start - window_seconds
    prior_new_buyers = {
        event.trader_id
        for event in prior_buys
        if event.trader_id and prior_start < (tape.first_seen_at(event.trader_id) or 0) <= start
    }

    minutes = Decimal(window_seconds) / Decimal(60)
    seconds = Decimal(window_seconds)

    new_buyer_velocity = (Decimal(len(new_buyers)) / seconds).quantize(Decimal("0.000001"))
    prior_new_buyer_velocity = (Decimal(len(prior_new_buyers)) / seconds).quantize(
        Decimal("0.000001")
    )

    buy_volume_dynamics = sum_dynamics(tape.buy_usd, end=at, seconds=window_seconds)
    thesis_dynamics = counter_dynamics(tape.theses, end=at, seconds=window_seconds)

    tx_ratio: Decimal | None = None
    if sells:
        tx_ratio = (Decimal(len(buys)) / Decimal(len(sells))).quantize(Decimal("0.0001"))
    elif buys:
        # All buys and no sells is genuinely one-sided.  It is reported as
        # unknown rather than as an enormous ratio, because a divide-by-zero
        # sentinel would top every percentile ranking it entered.
        tx_ratio = None

    notional_ratio: Decimal | None = None
    if sell_usd is not None and sell_usd > ZERO and buy_usd is not None:
        notional_ratio = (buy_usd / sell_usd).quantize(Decimal("0.0001"))

    mean_buy: Decimal | None = None
    if buy_amounts:
        mean_buy = (sum(buy_amounts, ZERO) / Decimal(len(buy_amounts))).quantize(
            Decimal("0.00000001")
        )

    return ActivityWindowFeatures(
        mint=tape.mint,
        at=at,
        window_seconds=window_seconds,
        fomo_buys=len(buys),
        fomo_sells=len(sells),
        fomo_buy_usd=buy_usd,
        fomo_sell_usd=sell_usd,
        fomo_net_buy_usd=net,
        unique_fomo_buyers=len({event.trader_id for event in buys if event.trader_id}),
        unique_fomo_sellers=len({event.trader_id for event in sells if event.trader_id}),
        new_fomo_buyers=len(new_buyers),
        new_fomo_buyers_per_min=(
            (Decimal(len(new_buyers)) / minutes).quantize(Decimal("0.0001"))
            if minutes > ZERO
            else None
        ),
        new_fomo_buyer_velocity=new_buyer_velocity,
        new_fomo_buyer_acceleration=new_buyer_velocity - prior_new_buyer_velocity,
        fomo_buy_volume_velocity=buy_volume_dynamics.velocity,
        fomo_buy_volume_acceleration=buy_volume_dynamics.acceleration,
        fomo_buy_sell_tx_ratio=tx_ratio,
        fomo_buy_sell_notional_ratio=notional_ratio,
        median_fomo_buy_size=_median(buy_amounts),
        mean_fomo_buy_size=mean_buy,
        p90_fomo_buy_size=_percentile(buy_amounts, Decimal("0.9")),
        largest_fomo_buy=max(buy_amounts) if buy_amounts else None,
        fomo_thesis_count=len(theses),
        new_theses_per_min=(
            (Decimal(len(theses)) / minutes).quantize(Decimal("0.0001"))
            if minutes > ZERO
            else None
        ),
        unique_thesis_authors=len({event.trader_id for event in theses if event.trader_id}),
        thesis_velocity=thesis_dynamics.velocity,
        thesis_acceleration=thesis_dynamics.acceleration,
        buys_missing_amount=sum(1 for event in buys if event.amount_usd is None),
    )


def activity_feature_ladder(
    tape: ActivityTape,
    *,
    at: int,
    windows: Sequence[int] = ACTIVITY_WINDOWS_SECONDS,
) -> dict[str, ActivityWindowFeatures]:
    """The whole section-15 ladder, keyed by window label."""

    return {
        window_label(seconds): activity_window_features(tape, at=at, window_seconds=seconds)
        for seconds in windows
    }
