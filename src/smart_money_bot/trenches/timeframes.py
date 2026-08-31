"""Multi-timeframe momentum: five independent windows, and what their shape means.

The change this module makes is the largest in the release (section 9).  v2.42
reduced momentum to essentially one 5-minute number, which cannot distinguish
between a token that just started moving and one that has been moving for an
hour and is now stalling.  Those are opposite trades.

So five windows — 1m, 5m, 15m, 30m, 1h — are computed **independently** from the
same observation history, with no leakage between them (section 85).  Each is
built only from samples inside its own span; a window with too few samples
reports ``None`` rather than borrowing a neighbour's number, because a fabricated
1-minute reading is worse than an absent one.

The *shape across* the windows is then the actual signal (section 10):

    1m exploding, 5m improving, 15m flat   → VERY EARLY ACCELERATION
    1m + 5m + 15m all strong               → SUSTAINED TREND
    1m declining, 5m + 15m strong          → COOLING / CONSOLIDATING
    1m + 5m falling, 15m formerly strong   → FADING

And section 11 asks a second-derivative question the first one cannot answer: is
the acceleration itself increasing, steady, cooling or reversing?  "Price is
currently green" is not that.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")
HUNDRED = Decimal("100")

# --- the five windows (section 9) --------------------------------------------
TF_1M = "1m"
TF_5M = "5m"
TF_15M = "15m"
TF_30M = "30m"
TF_1H = "1h"

TIMEFRAMES: tuple[str, ...] = (TF_1M, TF_5M, TF_15M, TF_30M, TF_1H)

TIMEFRAME_SECONDS: dict[str, int] = {
    TF_1M: 60,
    TF_5M: 300,
    TF_15M: 900,
    TF_30M: 1800,
    TF_1H: 3600,
}

#: A window needs at least this many observations before it reports anything.
#: One sample cannot describe a change.
MIN_SAMPLES = 2


@dataclass(frozen=True, slots=True)
class MarketObservation:
    """One reading of a token's market state.

    Everything optional is genuinely optional: a source that does not supply
    unique buyers leaves ``None``, and every derived metric that needs it then
    reports ``None`` too rather than substituting the raw trade count.
    """

    at: int
    price_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    #: Cumulative counters where the source is cumulative, windowed where it is
    #: windowed; :func:`window_metrics` only ever takes differences, so either
    #: convention works as long as one source is consistent with itself.
    buys: int = 0
    sells: int = 0
    volume_usd: Decimal = ZERO
    unique_buyers: int | None = None
    unique_sellers: int | None = None
    independent_buyers: int | None = None
    holders: int | None = None


@dataclass(frozen=True, slots=True)
class WindowMetrics:
    """What one timeframe can honestly say."""

    timeframe: str
    span_seconds: int = 0
    samples: int = 0
    price_change_percent: Decimal | None = None
    market_cap_change_percent: Decimal | None = None
    market_cap_velocity: Decimal | None = None
    liquidity_change_percent: Decimal | None = None
    buys: int = 0
    sells: int = 0
    buy_sell_ratio: Decimal | None = None
    volume_usd: Decimal = ZERO
    volume_per_minute: Decimal | None = None
    unique_buyers: int | None = None
    unique_sellers: int | None = None
    independent_buyers: int | None = None
    holder_change: int | None = None
    holder_velocity: Decimal | None = None

    @property
    def usable(self) -> bool:
        """Whether this window has enough data to be worth reading at all."""

        return self.samples >= MIN_SAMPLES and self.span_seconds > 0

    @property
    def rising(self) -> bool:
        return (
            self.market_cap_change_percent is not None
            and self.market_cap_change_percent > ZERO
        )

    def to_json(self) -> dict[str, object]:
        return {
            "timeframe": self.timeframe,
            "span_seconds": self.span_seconds,
            "samples": self.samples,
            "usable": self.usable,
            "price_change_percent": _s(self.price_change_percent),
            "market_cap_change_percent": _s(self.market_cap_change_percent),
            "market_cap_velocity": _s(self.market_cap_velocity),
            "liquidity_change_percent": _s(self.liquidity_change_percent),
            "buys": self.buys,
            "sells": self.sells,
            "buy_sell_ratio": _s(self.buy_sell_ratio),
            "volume_usd": str(self.volume_usd),
            "volume_per_minute": _s(self.volume_per_minute),
            "unique_buyers": self.unique_buyers,
            "unique_sellers": self.unique_sellers,
            "independent_buyers": self.independent_buyers,
            "holder_change": self.holder_change,
            "holder_velocity": _s(self.holder_velocity),
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _percent_change(before: Decimal | None, after: Decimal | None) -> Decimal | None:
    if before is None or after is None or before <= ZERO:
        return None
    return ((after - before) / before * HUNDRED).quantize(Decimal("0.01"))


def window_metrics(
    observations: Sequence[MarketObservation],
    *,
    timeframe: str,
    now: int,
) -> WindowMetrics:
    """Compute one window from only the samples inside it.

    The window is a half-open interval ending at ``now``.  Samples outside it are
    not consulted at all — that is what makes the five windows independent and
    what test 85 checks.
    """

    span = TIMEFRAME_SECONDS.get(timeframe)
    if span is None:
        raise ValueError(f"unknown timeframe: {timeframe}")

    inside = sorted(
        (item for item in observations if 0 <= now - item.at <= span),
        key=lambda item: item.at,
    )
    if len(inside) < MIN_SAMPLES:
        return WindowMetrics(timeframe=timeframe, samples=len(inside))

    first, last = inside[0], inside[-1]
    elapsed = max(1, last.at - first.at)
    minutes = Decimal(elapsed) / Decimal(60)

    market_change = _percent_change(first.market_cap_usd, last.market_cap_usd)
    buys = max(0, last.buys - first.buys) if last.buys >= first.buys else last.buys
    sells = max(0, last.sells - first.sells) if last.sells >= first.sells else last.sells
    volume = (
        last.volume_usd - first.volume_usd
        if last.volume_usd >= first.volume_usd
        else last.volume_usd
    )

    def counter_delta(attribute: str) -> int | None:
        start = getattr(first, attribute)
        end = getattr(last, attribute)
        if start is None or end is None:
            return None
        return max(0, end - start) if end >= start else end

    holder_change = None
    if first.holders is not None and last.holders is not None:
        holder_change = last.holders - first.holders

    return WindowMetrics(
        timeframe=timeframe,
        span_seconds=elapsed,
        samples=len(inside),
        price_change_percent=_percent_change(first.price_usd, last.price_usd),
        market_cap_change_percent=market_change,
        market_cap_velocity=(
            (market_change / minutes).quantize(Decimal("0.01"))
            if market_change is not None and minutes > ZERO
            else None
        ),
        liquidity_change_percent=_percent_change(first.liquidity_usd, last.liquidity_usd),
        buys=buys,
        sells=sells,
        buy_sell_ratio=(
            (Decimal(buys) / Decimal(sells)).quantize(Decimal("0.01"))
            if sells > 0
            else (Decimal(buys) if buys else None)
        ),
        volume_usd=volume,
        volume_per_minute=(
            (volume / minutes).quantize(Decimal("0.01")) if minutes > ZERO else None
        ),
        unique_buyers=counter_delta("unique_buyers"),
        unique_sellers=counter_delta("unique_sellers"),
        independent_buyers=counter_delta("independent_buyers"),
        holder_change=holder_change,
        holder_velocity=(
            (Decimal(holder_change) / minutes).quantize(Decimal("0.01"))
            if holder_change is not None and minutes > ZERO
            else None
        ),
    )


# --- the shape across windows (section 10) -----------------------------------
SHAPE_VERY_EARLY_ACCELERATION = "VERY_EARLY_ACCELERATION"
SHAPE_SUSTAINED_TREND = "SUSTAINED_TREND"
SHAPE_COOLING = "COOLING"
SHAPE_FADING = "FADING"
SHAPE_BUILDING = "BUILDING"
SHAPE_FLAT = "FLAT"
SHAPE_UNKNOWN = "INSUFFICIENT_DATA"

TREND_SHAPES: tuple[str, ...] = (
    SHAPE_VERY_EARLY_ACCELERATION,
    SHAPE_SUSTAINED_TREND,
    SHAPE_COOLING,
    SHAPE_FADING,
    SHAPE_BUILDING,
    SHAPE_FLAT,
    SHAPE_UNKNOWN,
)

#: Shapes worth an operator's attention.  COOLING and FADING are not — they are
#: the states that stop a stale card being sold as fresh.
ACTIONABLE_SHAPES: frozenset[str] = frozenset(
    {SHAPE_VERY_EARLY_ACCELERATION, SHAPE_SUSTAINED_TREND, SHAPE_BUILDING}
)

# --- the second derivative (section 11) --------------------------------------
MOMENTUM_INCREASING = "INCREASING"
MOMENTUM_STEADY = "STEADY"
MOMENTUM_COOLING = "COOLING"
MOMENTUM_REVERSING = "REVERSING"
MOMENTUM_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class TimeframeProfile:
    """The five windows, their shape, and whether acceleration is itself rising."""

    mint: str
    windows: dict[str, WindowMetrics]
    shape: str = SHAPE_UNKNOWN
    momentum_curve: str = MOMENTUM_UNKNOWN
    reasons: tuple[str, ...] = ()

    def window(self, timeframe: str) -> WindowMetrics | None:
        found = self.windows.get(timeframe)
        return found if found is not None and found.usable else None

    def change(self, timeframe: str) -> Decimal | None:
        found = self.window(timeframe)
        return None if found is None else found.market_cap_change_percent

    @property
    def usable_windows(self) -> int:
        return sum(1 for item in self.windows.values() if item.usable)

    @property
    def actionable(self) -> bool:
        return self.shape in ACTIONABLE_SHAPES

    def headline(self) -> str:
        parts = []
        for timeframe in TIMEFRAMES:
            change = self.change(timeframe)
            if change is not None:
                parts.append(f"{timeframe.upper()} {change:+.1f}%")
        return " • ".join(parts) if parts else "no usable window yet"

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "windows": {key: value.to_json() for key, value in self.windows.items()},
            "shape": self.shape,
            "momentum_curve": self.momentum_curve,
            "usable_windows": self.usable_windows,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class TrendConfig:
    """Named thresholds.  Every one is a percent market-cap change per window."""

    strong_percent: Decimal = Decimal("8")
    improving_percent: Decimal = Decimal("2")
    flat_percent: Decimal = Decimal("2")
    declining_percent: Decimal = Decimal("-2")
    #: How much faster the short window must be running than the long one before
    #: acceleration counts as genuinely increasing.
    acceleration_ratio: Decimal = Decimal("1.5")


DEFAULT_TREND_CONFIG = TrendConfig()


def build_timeframe_profile(
    mint: str,
    observations: Sequence[MarketObservation],
    *,
    now: int,
    config: TrendConfig = DEFAULT_TREND_CONFIG,
) -> TimeframeProfile:
    """Compute all five windows, then read their shape."""

    windows = {
        timeframe: window_metrics(observations, timeframe=timeframe, now=now)
        for timeframe in TIMEFRAMES
    }
    profile = TimeframeProfile(mint=mint, windows=windows)

    # Read the shape off the three shortest *usable* windows rather than off the
    # 1m/5m/15m specifically.  A slow observation cadence leaves the 1m window
    # empty, and treating an empty window as a flat one made a token up 100% on
    # every window it had report FLAT — the absence of data is not evidence of
    # calm.  Falling back to the shortest windows we actually have keeps the
    # shape meaningful at any cadence, and the windows themselves stay strictly
    # independent.
    usable_order = [
        timeframe for timeframe in TIMEFRAMES if profile.change(timeframe) is not None
    ]
    short = profile.change(usable_order[0]) if usable_order else None
    medium = profile.change(usable_order[1]) if len(usable_order) > 1 else None
    long_ = profile.change(usable_order[2]) if len(usable_order) > 2 else None
    longer = profile.change(usable_order[3]) if len(usable_order) > 3 else None

    reasons: list[str] = []
    known = [value for value in (short, medium, long_) if value is not None]
    if not known:
        return TimeframeProfile(
            mint=mint,
            windows=windows,
            shape=SHAPE_UNKNOWN,
            momentum_curve=MOMENTUM_UNKNOWN,
            reasons=("not enough observations in any window yet",),
        )

    def strong(value: Decimal | None) -> bool:
        return value is not None and value >= config.strong_percent

    def improving(value: Decimal | None) -> bool:
        return value is not None and value >= config.improving_percent

    def declining(value: Decimal | None) -> bool:
        return value is not None and value <= config.declining_percent

    def flat(value: Decimal | None) -> bool:
        return value is not None and abs(value) < config.flat_percent

    names = [timeframe.upper() for timeframe in usable_order[:4]]
    short_name = names[0] if names else "short"
    medium_name = names[1] if len(names) > 1 else "medium"
    long_name = names[2] if len(names) > 2 else "long"

    # "1m exploding while 15m is flat" is a statement about *rates*, not levels.
    # A spike in the last minute is inside the fifteen-minute window too, so its
    # level comparison always looks like a trend; only the per-minute velocity
    # separates "just started moving" from "has been moving for a while".
    short_velocity = (
        profile.window(usable_order[0]).market_cap_velocity if usable_order else None
    )
    long_velocity = (
        profile.window(usable_order[-1]).market_cap_velocity
        if len(usable_order) > 1
        else None
    )
    velocity_spike = (
        short_velocity is not None
        and long_velocity is not None
        and long_velocity > ZERO
        and short_velocity >= long_velocity * Decimal("3")
    )

    shape = SHAPE_FLAT
    if strong(short) and velocity_spike:
        shape = SHAPE_VERY_EARLY_ACCELERATION
        reasons.append(
            f"{short_name} is running at {short_velocity}%/min against "
            f"{long_velocity}%/min over {long_name} — the move just started"
        )
    elif strong(short) and strong(medium) and strong(long_):
        shape = SHAPE_SUSTAINED_TREND
        reasons.append(f"{short_name}, {medium_name} and {long_name} are all strong")
    elif strong(short) and strong(medium) and long_ is None:
        # Only two usable windows, both strong: a real trend as far as we can
        # see, and the reason says exactly how far that is.
        shape = SHAPE_SUSTAINED_TREND
        reasons.append(
            f"{short_name} and {medium_name} are both strong "
            f"({len(usable_order)} usable window(s) so far)"
        )
    elif declining(short) and strong(medium) and strong(long_):
        shape = SHAPE_COOLING
        reasons.append(f"{short_name} has turned while the longer windows hold")
    elif declining(short) and declining(medium) and (strong(long_) or strong(longer)):
        shape = SHAPE_FADING
        reasons.append(
            f"{short_name} and {medium_name} are falling after a strong longer window"
        )
    elif improving(short) and improving(medium):
        shape = SHAPE_BUILDING
        reasons.append("the shorter windows are improving together")
    elif strong(short) and medium is None:
        shape = SHAPE_BUILDING
        reasons.append(f"{short_name} is strong; no longer window yet to confirm it")
    else:
        reasons.append("no clear multi-timeframe shape")

    # Section 11: is the acceleration itself increasing?  Compare per-minute
    # velocity in the short window against the longer one, so a token that made
    # its whole move twenty minutes ago is not mistaken for one moving now.
    fast = profile.window(usable_order[0]) if usable_order else None
    slow = next(
        (
            profile.window(timeframe)
            for timeframe in reversed(usable_order)
            if timeframe != (usable_order[0] if usable_order else None)
        ),
        None,
    )
    curve = MOMENTUM_UNKNOWN
    if fast is not None and slow is not None:
        fast_velocity = fast.market_cap_velocity
        slow_velocity = slow.market_cap_velocity
        if fast_velocity is not None and slow_velocity is not None:
            if fast_velocity <= ZERO and slow_velocity > ZERO:
                curve = MOMENTUM_REVERSING
                reasons.append("acceleration has turned negative against a positive trend")
            elif slow_velocity <= ZERO:
                curve = MOMENTUM_INCREASING if fast_velocity > ZERO else MOMENTUM_COOLING
            elif fast_velocity >= slow_velocity * config.acceleration_ratio:
                curve = MOMENTUM_INCREASING
                reasons.append("the move is accelerating, not just continuing")
            elif fast_velocity * config.acceleration_ratio <= slow_velocity:
                curve = MOMENTUM_COOLING
                reasons.append("still rising, but slower than it was")
            else:
                curve = MOMENTUM_STEADY

    return TimeframeProfile(
        mint=mint,
        windows=windows,
        shape=shape,
        momentum_curve=curve,
        reasons=tuple(reasons),
    )


# --- market cap versus liquidity (section 12) --------------------------------
@dataclass(frozen=True, slots=True)
class DepthProfile:
    """$50K MC on $1K liquidity is not $50K MC on $15K liquidity."""

    market_cap_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_usd: Decimal | None = None
    liquidity_to_market_cap: Decimal | None = None
    volume_to_liquidity: Decimal | None = None
    estimated_impact_percent: Decimal | None = None

    @property
    def thin(self) -> bool:
        """Liquidity too thin for the size the token is being valued at."""

        return (
            self.liquidity_to_market_cap is not None
            and self.liquidity_to_market_cap < Decimal("0.03")
        )

    @property
    def churning(self) -> bool:
        """Volume far beyond what the pool can support without recycling."""

        return (
            self.volume_to_liquidity is not None
            and self.volume_to_liquidity > Decimal("8")
        )

    def to_json(self) -> dict[str, object]:
        return {
            "market_cap_usd": _s(self.market_cap_usd),
            "liquidity_usd": _s(self.liquidity_usd),
            "volume_usd": _s(self.volume_usd),
            "liquidity_to_market_cap": _s(self.liquidity_to_market_cap),
            "volume_to_liquidity": _s(self.volume_to_liquidity),
            "estimated_impact_percent": _s(self.estimated_impact_percent),
            "thin": self.thin,
            "churning": self.churning,
        }


def assess_depth(
    *,
    market_cap_usd: Decimal | None,
    liquidity_usd: Decimal | None,
    volume_usd: Decimal | None = None,
    trade_size_usd: Decimal = Decimal("10"),
) -> DepthProfile:
    """Relate size, depth and turnover instead of scoring market cap alone."""

    ratio = None
    if market_cap_usd is not None and liquidity_usd is not None and market_cap_usd > ZERO:
        ratio = (liquidity_usd / market_cap_usd).quantize(Decimal("0.0001"))
    turnover = None
    if volume_usd is not None and liquidity_usd is not None and liquidity_usd > ZERO:
        turnover = (volume_usd / liquidity_usd).quantize(Decimal("0.01"))
    impact = None
    if liquidity_usd is not None and liquidity_usd > ZERO:
        # Constant-product impact for a small trade against half the pool.
        impact = (trade_size_usd / (liquidity_usd / 2) * HUNDRED).quantize(Decimal("0.01"))
    return DepthProfile(
        market_cap_usd=market_cap_usd,
        liquidity_usd=liquidity_usd,
        volume_usd=volume_usd,
        liquidity_to_market_cap=ratio,
        volume_to_liquidity=turnover,
        estimated_impact_percent=impact,
    )
