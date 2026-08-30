"""The low-latency FAST WATCH path (sections 6, 7, 40, 41).

The bottleneck production exposed was visibility, not intelligence: by the time
the full research pipeline had finished, the interesting part of the move was
often over.  FAST WATCH surfaces promising early acceleration using only the
cheap evidence the earliest pipeline already has, before expensive wallet
forensics, tracker risk and social enrichment complete.

The boundary is absolute and structural, not a convention: a
:class:`FastWatchVerdict` has no path to a PAPER entry.  ``entry_eligible`` is a
hard ``False``, the missing mandatory evidence is listed on the card, and the
PAPER engine's own fail-closed gates are untouched.  FAST WATCH buys speed of
*information*, never speed of *commitment*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .config import DEFAULT_LAB_CONFIG, LabConfig

ZERO = Decimal("0")

#: Evidence a FAST WATCH deliberately does not wait for.
PENDING_SAFETY = "safety"
PENDING_TRACKER_RISK = "tracker risk"
PENDING_BUYER_AUTHENTICITY = "bounded buyer authenticity"
PENDING_FUNDING_FORENSICS = "complete funding forensics"
PENDING_ECONOMIC_AUTHENTICITY = "economic authenticity"

ALL_PENDING: tuple[str, ...] = (
    PENDING_SAFETY,
    PENDING_TRACKER_RISK,
    PENDING_BUYER_AUTHENTICITY,
    PENDING_FUNDING_FORENSICS,
    PENDING_ECONOMIC_AUTHENTICITY,
)

# --- why we are watching -----------------------------------------------------
W_MARKET_CAP_ACCELERATION = "market-cap acceleration"
W_PRICE_VELOCITY = "price velocity"
W_VOLUME_ACCELERATION = "volume acceleration"
W_BUY_PRESSURE = "buy/sell pressure"
W_TRANSACTION_ACCELERATION = "transaction acceleration"
W_HOLDER_GROWTH = "holder growth"
W_LIQUIDITY_GROWTH = "liquidity growth"
W_FRESH_PAIR = "fresh pair"

# --- immediate blockers ------------------------------------------------------
B_NO_ROUTE = "no usable route"
B_LIQUIDITY_TOO_THIN = "liquidity below the watch floor"
B_RUGGED = "rug evidence already present"
B_TOO_OLD = "token is not fresh"
B_NOT_CURRENT = "candidate is no longer current"


@dataclass(frozen=True, slots=True)
class FastWatchSignals:
    """Cheap, already-available evidence.  No expensive provider work here."""

    now: int = 0
    pair_age_seconds: int | None = None
    market_cap_usd: Decimal | None = None
    first_seen_market_cap_usd: Decimal | None = None
    market_cap_acceleration_ratio: Decimal | None = None
    price_change_percent: Decimal | None = None
    volume_acceleration_ratio: Decimal | None = None
    transaction_acceleration_ratio: Decimal | None = None
    buys: int | None = None
    sells: int | None = None
    holder_growth: int | None = None
    liquidity_usd: Decimal | None = None
    liquidity_change_percent: Decimal | None = None
    route_available: bool | None = None
    rugged: bool = False
    hard_blockers: tuple[str, ...] = ()

    @property
    def buy_sell_ratio(self) -> Decimal | None:
        if self.buys is None or self.sells is None:
            return None
        if self.sells <= 0:
            return Decimal("99") if self.buys > 0 else None
        return (Decimal(self.buys) / Decimal(self.sells)).quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class FastWatchVerdict:
    """A research-visibility verdict.  It can never authorise an entry."""

    watch: bool = False
    score: Decimal = ZERO
    reasons: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    pending_evidence: tuple[str, ...] = field(default_factory=lambda: ALL_PENDING)

    @property
    def entry_eligible(self) -> bool:
        """Structural guarantee: FAST WATCH is never entry eligible."""

        return False

    @property
    def label(self) -> str:
        return "WATCH — HEATING UP" if self.watch else "NOT WATCHED"


def evaluate_fast_watch(
    signals: FastWatchSignals,
    *,
    min_score: Decimal = Decimal("55"),
    min_liquidity_usd: Decimal | None = None,
    max_pair_age_seconds: int = 3_600,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> FastWatchVerdict:
    """Decide whether cheap evidence justifies surfacing this as a WATCH.

    Deliberately cheap: everything consulted here is already in hand from the
    first pass, so a slow provider cannot delay the verdict.
    """

    floor = min_liquidity_usd if min_liquidity_usd is not None else config.min_liquidity_usd / 3
    blockers: list[str] = list(signals.hard_blockers)
    if signals.rugged:
        blockers.append(B_RUGGED)
    if signals.route_available is False:
        blockers.append(B_NO_ROUTE)
    if signals.liquidity_usd is not None and signals.liquidity_usd < floor:
        blockers.append(B_LIQUIDITY_TOO_THIN)
    if signals.pair_age_seconds is not None and signals.pair_age_seconds > max_pair_age_seconds:
        blockers.append(B_TOO_OLD)

    reasons: list[str] = []
    score = ZERO

    if signals.pair_age_seconds is not None and signals.pair_age_seconds <= 900:
        reasons.append(W_FRESH_PAIR)
        score += 15
    if (
        signals.market_cap_acceleration_ratio is not None
        and signals.market_cap_acceleration_ratio >= Decimal("1.2")
    ):
        reasons.append(W_MARKET_CAP_ACCELERATION)
        score += 20
    if signals.price_change_percent is not None and signals.price_change_percent >= 10:
        reasons.append(W_PRICE_VELOCITY)
        score += 15
    if (
        signals.volume_acceleration_ratio is not None
        and signals.volume_acceleration_ratio >= Decimal("1.5")
    ):
        reasons.append(W_VOLUME_ACCELERATION)
        score += 18
    if (
        signals.transaction_acceleration_ratio is not None
        and signals.transaction_acceleration_ratio >= Decimal("1.5")
    ):
        reasons.append(W_TRANSACTION_ACCELERATION)
        score += 12
    ratio = signals.buy_sell_ratio
    if ratio is not None and ratio >= Decimal("1.5"):
        reasons.append(W_BUY_PRESSURE)
        score += 15
    if signals.holder_growth is not None and signals.holder_growth >= 10:
        reasons.append(W_HOLDER_GROWTH)
        score += 10
    if (
        signals.liquidity_change_percent is not None
        and signals.liquidity_change_percent >= 15
    ):
        reasons.append(W_LIQUIDITY_GROWTH)
        score += 10

    bounded = max(ZERO, min(Decimal("100"), score))
    watch = bool(reasons) and bounded >= min_score and not blockers
    return FastWatchVerdict(
        watch=watch,
        score=bounded,
        reasons=tuple(dict.fromkeys(reasons)),
        blockers=tuple(dict.fromkeys(blockers)),
        pending_evidence=ALL_PENDING,
    )


def still_current(
    signals: FastWatchSignals,
    *,
    first_seen_at: int | None,
    max_queue_age_seconds: int = 300,
    max_adverse_move_percent: Decimal = Decimal("-12"),
) -> tuple[bool, str]:
    """Cheap publication-time freshness recheck (sections 41, 42).

    A candidate that sat in a queue must not finally publish as "early" after
    the move already happened, and one that has fallen away from its first-seen
    level is no longer the setup that was detected.  This is intentionally
    lightweight: it re-reads what is already in hand and never reruns the
    pipeline.
    """

    if first_seen_at and signals.now:
        age = signals.now - first_seen_at
        if age > max_queue_age_seconds:
            return False, f"queued {age}s before publication"
    if signals.rugged:
        return False, B_RUGGED
    if signals.route_available is False:
        return False, B_NO_ROUTE
    base = signals.first_seen_market_cap_usd
    current = signals.market_cap_usd
    if base and current and base > 0:
        move = (current - base) / base * Decimal("100")
        if move <= max_adverse_move_percent:
            return False, f"already {move.quantize(Decimal('0.01'))}% below first seen"
    return True, ""


def _decimal_field(source: object, name: str) -> Decimal | None:
    value = getattr(source, name, None)
    return value if isinstance(value, Decimal) else None


def _pct(current: Decimal | None, base: Decimal | None) -> Decimal | None:
    if current is None or base is None or base <= 0:
        return None
    return ((current - base) / base * Decimal("100")).quantize(Decimal("0.01"))


def _rate(current: Decimal | None, base: Decimal | None) -> Decimal | None:
    if current is None or base is None or base <= 0:
        return None
    return (current / base).quantize(Decimal("0.01"))


def _gap(current: int | None, base: int | None) -> int | None:
    if current is None or base is None:
        return None
    return current - base


def signals_from_candidate(candidate: object, *, now: int) -> FastWatchSignals:
    """Project a runner candidate onto the cheap FAST WATCH signal set.

    Deliberately structural (``getattr`` only) so the publication path, the lab
    runtime and the tests all read the same evidence from the same fields, and
    so nothing here can reach for a provider.
    """

    current = getattr(candidate, "current", None)
    first = getattr(candidate, "first", None)
    started = getattr(candidate, "pair_created_at", None) or getattr(
        candidate, "graduated_at", None
    )
    age = max(0, now - started) if started and now else None
    return FastWatchSignals(
        now=now,
        pair_age_seconds=age,
        market_cap_usd=_decimal_field(current, "market_cap_usd"),
        first_seen_market_cap_usd=_decimal_field(first, "market_cap_usd"),
        market_cap_acceleration_ratio=_rate(
            _decimal_field(current, "market_cap_usd"),
            _decimal_field(first, "market_cap_usd"),
        ),
        price_change_percent=_pct(
            _decimal_field(current, "price_usd"), _decimal_field(first, "price_usd")
        ),
        volume_acceleration_ratio=_rate(
            _decimal_field(current, "volume_5m_usd"), _decimal_field(first, "volume_5m_usd")
        ),
        buys=int(getattr(current, "buys_5m", 0) or 0),
        sells=int(getattr(current, "sells_5m", 0) or 0),
        holder_growth=_gap(
            getattr(current, "holder_count", None), getattr(first, "holder_count", None)
        ),
        liquidity_usd=_decimal_field(current, "liquidity_usd"),
        liquidity_change_percent=_pct(
            _decimal_field(current, "liquidity_usd"), _decimal_field(first, "liquidity_usd")
        ),
        route_available=bool(getattr(current, "route_available", False)),
        rugged=bool(getattr(current, "rugged", False)),
        hard_blockers=tuple(getattr(candidate, "hard_blockers", ()) or ()),
    )
