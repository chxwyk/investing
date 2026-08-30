"""Executable route selection and venue modelling for simulated fills.

Sections 20-24 of the Shadow contract.  The split this module enforces is the
one the product asks for:

    FOMO / J7 / X / news / public wallets   =   INTELLIGENCE
    Pump / PumpSwap / Jupiter / Solana      =   EXECUTION

Intelligence decides *what* is interesting.  This module decides *what a trade
would actually have cost*, and it is the only place that models a venue.

Everything here is arithmetic on evidence that was already collected.  There is
no network client, no signer, no keypair, no transaction builder and no
submission path in this module, and there is deliberately no seam where one
could be added without rewriting the public functions: :class:`RouteQuote` is a
frozen record of a *price*, never of an order.  A future live executor may reuse
``select_route`` to choose a venue, but it would have to bring its own execution
code — none exists here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal("0")
UNIT = Decimal("0.000001")
HUNDRED = Decimal("100")
BPS = Decimal("10000")

# --- venues (sections 21-24) -------------------------------------------------
VENUE_PUMP_CURVE = "PUMP_BONDING_CURVE"
VENUE_PUMPSWAP = "PUMPSWAP"
VENUE_JUPITER = "JUPITER"
VENUE_AGGREGATED = "AGGREGATED_SOLANA_ROUTE"
VENUE_UNKNOWN = "UNKNOWN"

VENUES: tuple[str, ...] = (
    VENUE_PUMP_CURVE,
    VENUE_PUMPSWAP,
    VENUE_JUPITER,
    VENUE_AGGREGATED,
    VENUE_UNKNOWN,
)

# --- Pump ecosystem graduation state (section 21) ----------------------------
PRE_GRADUATION = "PRE_GRADUATION"
GRADUATED = "GRADUATED"
GRADUATION_UNKNOWN = "UNKNOWN"

GRADUATION_STATES: tuple[str, ...] = (PRE_GRADUATION, GRADUATED, GRADUATION_UNKNOWN)

# --- fill provenance, best first (section 7) ---------------------------------
#: A real executable quote the bot actually obtained.
FILL_EXECUTABLE_QUOTE = "EXECUTABLE_QUOTE"
#: A simulation from live on-chain venue state (bonding-curve reserves, pool
#: reserves).  Executable arithmetic, not an executed order.
FILL_SIMULATED_VENUE = "SIMULATED_VENUE_STATE"
#: Nothing executable was available; the observed price is used and explicitly
#: penalised.  Always labelled, never presented as a real fill.
FILL_FALLBACK_PENALISED = "FALLBACK_PENALISED"

FILL_SOURCES: tuple[str, ...] = (
    FILL_EXECUTABLE_QUOTE,
    FILL_SIMULATED_VENUE,
    FILL_FALLBACK_PENALISED,
)

#: Preference order used when two routes are otherwise comparable.  A real quote
#: always beats a simulation, which always beats a penalised fallback.
_SOURCE_RANK: dict[str, int] = {
    FILL_EXECUTABLE_QUOTE: 0,
    FILL_SIMULATED_VENUE: 1,
    FILL_FALLBACK_PENALISED: 2,
}

#: The penalty a fallback price pays, in basis points, on top of modelled costs.
#: A price the bot could not actually trade against is worth less than one it
#: could, and pretending otherwise is exactly the fantasy fill section 39
#: forbids.
FALLBACK_PENALTY_BPS = 250

#: Pump's published bonding-curve trading fee, in basis points.
PUMP_CURVE_FEE_BPS = 100
#: PumpSwap / AMM pool fee, in basis points.
PUMPSWAP_FEE_BPS = 25

# --- route rejection reasons -------------------------------------------------
ROUTE_NO_LIQUIDITY = "NO_LIQUIDITY"
ROUTE_NO_QUOTE = "NO_QUOTE"
ROUTE_IMPACT_TOO_HIGH = "PRICE_IMPACT_TOO_HIGH"
ROUTE_CURVE_COMPLETE = "BONDING_CURVE_COMPLETE"
ROUTE_UNAVAILABLE = "ROUTE_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class RouteQuote:
    """One venue's answer to "what would this exact trade fill at?".

    This is a *price record*.  It carries no transaction, no instruction set and
    no signer, so nothing downstream can turn it into a submitted swap.
    """

    venue: str = VENUE_UNKNOWN
    side: str = "BUY"
    notional_usd: Decimal = ZERO
    #: Effective USD price per token including the venue's own curve/pool impact.
    fill_price_usd: Decimal | None = None
    #: The mid/observed price the fill is measured against.
    reference_price_usd: Decimal | None = None
    expected_output_tokens: Decimal | None = None
    expected_output_usd: Decimal | None = None
    price_impact_percent: Decimal = ZERO
    slippage_bps: int = 0
    fee_bps: int = 0
    liquidity_usd: Decimal | None = None
    quote_latency_ms: int = 0
    quoted_at: int = 0
    source: str = FILL_FALLBACK_PENALISED
    graduation_state: str = GRADUATION_UNKNOWN
    available: bool = True
    unavailable_reason: str = ""
    notes: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return bool(
            self.available
            and not self.unavailable_reason
            and self.fill_price_usd is not None
            and self.fill_price_usd > 0
        )

    @property
    def total_cost_percent(self) -> Decimal:
        """Everything this venue takes out of the trade, as a percentage."""

        return (
            self.price_impact_percent
            + Decimal(self.slippage_bps) / BPS * HUNDRED
            + Decimal(self.fee_bps) / BPS * HUNDRED
        ).quantize(Decimal("0.0001"))

    @property
    def deterioration_percent(self) -> Decimal | None:
        """How much worse the fill is than the reference price it was quoted from."""

        if (
            self.reference_price_usd is None
            or self.reference_price_usd <= 0
            or self.fill_price_usd is None
        ):
            return None
        move = (self.fill_price_usd - self.reference_price_usd) / self.reference_price_usd
        signed = move if self.side == "BUY" else -move
        return (signed * HUNDRED).quantize(Decimal("0.01"))

    def as_dict(self) -> dict[str, str]:
        return {
            "VENUE": self.venue,
            "SIDE": self.side,
            "SOURCE": self.source,
            "GRADUATION": self.graduation_state,
            "FILL_PRICE": _text(self.fill_price_usd) or "",
            "REFERENCE_PRICE": _text(self.reference_price_usd) or "",
            "EXPECTED_OUTPUT_TOKENS": _text(self.expected_output_tokens) or "",
            "EXPECTED_OUTPUT_USD": _text(self.expected_output_usd) or "",
            "PRICE_IMPACT_PERCENT": str(self.price_impact_percent),
            "SLIPPAGE_BPS": str(self.slippage_bps),
            "FEE_BPS": str(self.fee_bps),
            "LIQUIDITY_USD": _text(self.liquidity_usd) or "",
            "QUOTE_LATENCY_MS": str(self.quote_latency_ms),
            "QUOTED_AT": str(self.quoted_at),
            "TOTAL_COST_PERCENT": str(self.total_cost_percent),
            "AVAILABLE": "1" if self.usable else "0",
            "UNAVAILABLE_REASON": self.unavailable_reason,
        }


@dataclass(frozen=True, slots=True)
class BondingCurveState:
    """Public Pump bonding-curve reserves, as read from on-chain state.

    These are the account fields any authorized RPC exposes.  Nothing in this
    module fetches them; the caller supplies whatever it already read.
    """

    virtual_sol_reserves: Decimal = ZERO
    virtual_token_reserves: Decimal = ZERO
    real_sol_reserves: Decimal | None = None
    real_token_reserves: Decimal | None = None
    complete: bool = False
    sol_price_usd: Decimal | None = None
    fee_bps: int = PUMP_CURVE_FEE_BPS
    observed_at: int = 0

    @property
    def known(self) -> bool:
        return self.virtual_sol_reserves > 0 and self.virtual_token_reserves > 0

    @property
    def graduation_state(self) -> str:
        if not self.known:
            return GRADUATION_UNKNOWN
        return GRADUATED if self.complete else PRE_GRADUATION

    @property
    def spot_price_sol(self) -> Decimal | None:
        if not self.known:
            return None
        return (self.virtual_sol_reserves / self.virtual_token_reserves).quantize(
            Decimal("0.000000000001")
        )

    @property
    def spot_price_usd(self) -> Decimal | None:
        spot = self.spot_price_sol
        if spot is None or self.sol_price_usd is None:
            return None
        return (spot * self.sol_price_usd).quantize(Decimal("0.000000000001"))

    @property
    def liquidity_usd(self) -> Decimal | None:
        """Curve depth expressed in USD, so it is comparable with a pool."""

        if not self.known or self.sol_price_usd is None:
            return None
        return (self.virtual_sol_reserves * self.sol_price_usd).quantize(UNIT)


def bonding_curve_quote(
    state: BondingCurveState,
    *,
    side: str,
    notional_usd: Decimal,
    slippage_bps: int = 0,
    quote_latency_ms: int = 0,
    now: int = 0,
) -> RouteQuote:
    """Price a simulated $N trade against the constant-product bonding curve.

    Uses the standard ``x * y = k`` invariant the public curve implements, plus
    the published trading fee.  A completed (graduated) curve refuses the trade
    rather than inventing a price for it — the caller must route to PumpSwap or
    an aggregator instead, which is what section 22 requires.
    """

    graduation = state.graduation_state
    if not state.known or state.sol_price_usd is None or state.sol_price_usd <= 0:
        return RouteQuote(
            venue=VENUE_PUMP_CURVE,
            side=side,
            notional_usd=notional_usd,
            source=FILL_SIMULATED_VENUE,
            graduation_state=graduation,
            available=False,
            unavailable_reason=ROUTE_NO_LIQUIDITY,
            quoted_at=now,
        )
    if state.complete:
        return RouteQuote(
            venue=VENUE_PUMP_CURVE,
            side=side,
            notional_usd=notional_usd,
            source=FILL_SIMULATED_VENUE,
            graduation_state=GRADUATED,
            available=False,
            unavailable_reason=ROUTE_CURVE_COMPLETE,
            quoted_at=now,
            notes=("bonding curve completed — the token trades on PumpSwap now",),
        )
    if notional_usd <= 0:
        return RouteQuote(
            venue=VENUE_PUMP_CURVE,
            side=side,
            notional_usd=ZERO,
            source=FILL_SIMULATED_VENUE,
            graduation_state=graduation,
            available=False,
            unavailable_reason=ROUTE_NO_QUOTE,
            quoted_at=now,
        )

    spot_usd = state.spot_price_usd
    assert spot_usd is not None  # guarded by ``state.known`` above
    fee_rate = Decimal(state.fee_bps) / BPS
    sol_in = (notional_usd / state.sol_price_usd).quantize(Decimal("0.000000001"))
    x = state.virtual_sol_reserves
    y = state.virtual_token_reserves
    k = x * y

    if side == "BUY":
        # The fee is taken from the SOL that goes in, exactly as the public
        # curve charges it.
        effective_sol = sol_in * (Decimal("1") - fee_rate)
        new_x = x + effective_sol
        tokens_out = y - (k / new_x)
        if tokens_out <= 0:
            return RouteQuote(
                venue=VENUE_PUMP_CURVE,
                side=side,
                notional_usd=notional_usd,
                source=FILL_SIMULATED_VENUE,
                graduation_state=graduation,
                available=False,
                unavailable_reason=ROUTE_NO_LIQUIDITY,
                quoted_at=now,
            )
        available_tokens = state.real_token_reserves
        if available_tokens is not None and tokens_out > available_tokens:
            return RouteQuote(
                venue=VENUE_PUMP_CURVE,
                side=side,
                notional_usd=notional_usd,
                source=FILL_SIMULATED_VENUE,
                graduation_state=graduation,
                available=False,
                unavailable_reason=ROUTE_NO_LIQUIDITY,
                quoted_at=now,
                notes=("curve cannot fill this size before graduation",),
            )
        fill_price = (notional_usd / tokens_out).quantize(Decimal("0.000000000001"))
        impact = _impact_percent(fill_price, spot_usd, side="BUY")
        return RouteQuote(
            venue=VENUE_PUMP_CURVE,
            side="BUY",
            notional_usd=notional_usd,
            fill_price_usd=fill_price,
            reference_price_usd=spot_usd,
            expected_output_tokens=tokens_out.quantize(UNIT),
            expected_output_usd=notional_usd,
            price_impact_percent=impact,
            slippage_bps=slippage_bps,
            fee_bps=state.fee_bps,
            liquidity_usd=state.liquidity_usd,
            quote_latency_ms=quote_latency_ms,
            quoted_at=now,
            source=FILL_SIMULATED_VENUE,
            graduation_state=PRE_GRADUATION,
        )

    # SELL: ``notional_usd`` is the current value of the tokens being sold.
    tokens_in = (notional_usd / spot_usd).quantize(Decimal("0.000000000001"))
    if tokens_in <= 0:
        return RouteQuote(
            venue=VENUE_PUMP_CURVE,
            side="SELL",
            notional_usd=notional_usd,
            source=FILL_SIMULATED_VENUE,
            graduation_state=graduation,
            available=False,
            unavailable_reason=ROUTE_NO_QUOTE,
            quoted_at=now,
        )
    new_y = y + tokens_in
    sol_out = (x - (k / new_y)) * (Decimal("1") - fee_rate)
    if sol_out <= 0:
        return RouteQuote(
            venue=VENUE_PUMP_CURVE,
            side="SELL",
            notional_usd=notional_usd,
            source=FILL_SIMULATED_VENUE,
            graduation_state=graduation,
            available=False,
            unavailable_reason=ROUTE_NO_LIQUIDITY,
            quoted_at=now,
        )
    usd_out = (sol_out * state.sol_price_usd).quantize(UNIT)
    fill_price = (usd_out / tokens_in).quantize(Decimal("0.000000000001"))
    impact = _impact_percent(fill_price, spot_usd, side="SELL")
    return RouteQuote(
        venue=VENUE_PUMP_CURVE,
        side="SELL",
        notional_usd=notional_usd,
        fill_price_usd=fill_price,
        reference_price_usd=spot_usd,
        expected_output_tokens=tokens_in,
        expected_output_usd=usd_out,
        price_impact_percent=impact,
        slippage_bps=slippage_bps,
        fee_bps=state.fee_bps,
        liquidity_usd=state.liquidity_usd,
        quote_latency_ms=quote_latency_ms,
        quoted_at=now,
        source=FILL_SIMULATED_VENUE,
        graduation_state=PRE_GRADUATION,
    )


def pool_quote(
    *,
    venue: str,
    side: str,
    notional_usd: Decimal,
    reference_price_usd: Decimal | None,
    liquidity_usd: Decimal | None,
    fee_bps: int = PUMPSWAP_FEE_BPS,
    slippage_bps: int = 0,
    observed_price_impact_percent: Decimal | None = None,
    quote_latency_ms: int = 0,
    now: int = 0,
    graduation_state: str = GRADUATION_UNKNOWN,
) -> RouteQuote:
    """Price a simulated $N trade against an AMM pool of known depth.

    When the caller already observed a real route impact (the runner records
    one), that measurement wins.  Otherwise the constant-product impact for the
    pool's own depth is used, which is a model, not a measurement, and is marked
    as a venue simulation rather than an executable quote.
    """

    if reference_price_usd is None or reference_price_usd <= 0:
        return RouteQuote(
            venue=venue,
            side=side,
            notional_usd=notional_usd,
            source=FILL_SIMULATED_VENUE,
            graduation_state=graduation_state,
            available=False,
            unavailable_reason=ROUTE_NO_QUOTE,
            quoted_at=now,
        )
    if not liquidity_usd or liquidity_usd <= 0:
        return RouteQuote(
            venue=venue,
            side=side,
            notional_usd=notional_usd,
            source=FILL_SIMULATED_VENUE,
            graduation_state=graduation_state,
            available=False,
            unavailable_reason=ROUTE_NO_LIQUIDITY,
            quoted_at=now,
        )
    if notional_usd <= 0:
        return RouteQuote(
            venue=venue,
            side=side,
            notional_usd=ZERO,
            source=FILL_SIMULATED_VENUE,
            graduation_state=graduation_state,
            available=False,
            unavailable_reason=ROUTE_NO_QUOTE,
            quoted_at=now,
        )

    if observed_price_impact_percent is not None:
        impact = max(ZERO, observed_price_impact_percent)
    else:
        # A constant-product pool holds roughly half its USD depth on each side,
        # so a trade of ``n`` against depth ``L`` moves price by ~n/(L/2).
        half_depth = liquidity_usd / 2
        impact = (notional_usd / half_depth * HUNDRED).quantize(Decimal("0.0001"))
    fill_price = (
        reference_price_usd * (Decimal("1") + impact / HUNDRED)
        if side == "BUY"
        else reference_price_usd * (Decimal("1") - impact / HUNDRED)
    ).quantize(Decimal("0.000000000001"))
    if fill_price <= 0:
        return RouteQuote(
            venue=venue,
            side=side,
            notional_usd=notional_usd,
            source=FILL_SIMULATED_VENUE,
            graduation_state=graduation_state,
            available=False,
            unavailable_reason=ROUTE_NO_LIQUIDITY,
            quoted_at=now,
        )
    tokens = (notional_usd / fill_price).quantize(UNIT)
    return RouteQuote(
        venue=venue,
        side=side,
        notional_usd=notional_usd,
        fill_price_usd=fill_price,
        reference_price_usd=reference_price_usd,
        expected_output_tokens=tokens if side == "BUY" else None,
        expected_output_usd=notional_usd,
        price_impact_percent=impact,
        slippage_bps=slippage_bps,
        fee_bps=fee_bps,
        liquidity_usd=liquidity_usd,
        quote_latency_ms=quote_latency_ms,
        quoted_at=now,
        source=FILL_SIMULATED_VENUE,
        graduation_state=graduation_state,
    )


def executable_quote(
    *,
    venue: str,
    side: str,
    notional_usd: Decimal,
    fill_price_usd: Decimal | None,
    reference_price_usd: Decimal | None = None,
    price_impact_percent: Decimal | None = None,
    slippage_bps: int = 0,
    fee_bps: int = 0,
    liquidity_usd: Decimal | None = None,
    quote_latency_ms: int = 0,
    now: int = 0,
    graduation_state: str = GRADUATION_UNKNOWN,
) -> RouteQuote:
    """Wrap a real executable quote the bot already obtained (section 7, tier 1)."""

    if fill_price_usd is None or fill_price_usd <= 0:
        return RouteQuote(
            venue=venue,
            side=side,
            notional_usd=notional_usd,
            source=FILL_EXECUTABLE_QUOTE,
            graduation_state=graduation_state,
            available=False,
            unavailable_reason=ROUTE_NO_QUOTE,
            quoted_at=now,
        )
    tokens = (notional_usd / fill_price_usd).quantize(UNIT) if notional_usd > 0 else None
    return RouteQuote(
        venue=venue,
        side=side,
        notional_usd=notional_usd,
        fill_price_usd=fill_price_usd,
        reference_price_usd=reference_price_usd or fill_price_usd,
        expected_output_tokens=tokens if side == "BUY" else None,
        expected_output_usd=notional_usd,
        price_impact_percent=max(ZERO, price_impact_percent or ZERO),
        slippage_bps=slippage_bps,
        fee_bps=fee_bps,
        liquidity_usd=liquidity_usd,
        quote_latency_ms=quote_latency_ms,
        quoted_at=now,
        source=FILL_EXECUTABLE_QUOTE,
        graduation_state=graduation_state,
    )


def fallback_quote(
    *,
    side: str,
    notional_usd: Decimal,
    observed_price_usd: Decimal | None,
    liquidity_usd: Decimal | None = None,
    penalty_bps: int = FALLBACK_PENALTY_BPS,
    now: int = 0,
    graduation_state: str = GRADUATION_UNKNOWN,
    reason: str = "no executable quote was available",
) -> RouteQuote:
    """The explicitly-labelled last resort (section 7, tier 3).

    An observed chart price the bot could not actually trade against is *not* a
    fill.  It is used only when nothing executable exists, it is charged an
    explicit penalty, and it is marked ``FILL_FALLBACK_PENALISED`` everywhere it
    is persisted or displayed so no report can quietly treat it as real.
    """

    if observed_price_usd is None or observed_price_usd <= 0:
        return RouteQuote(
            venue=VENUE_UNKNOWN,
            side=side,
            notional_usd=notional_usd,
            source=FILL_FALLBACK_PENALISED,
            graduation_state=graduation_state,
            available=False,
            unavailable_reason=ROUTE_NO_QUOTE,
            quoted_at=now,
        )
    penalty = Decimal(max(0, penalty_bps)) / BPS
    fill = (
        observed_price_usd * (Decimal("1") + penalty)
        if side == "BUY"
        else observed_price_usd * (Decimal("1") - penalty)
    ).quantize(Decimal("0.000000000001"))
    tokens = (notional_usd / fill).quantize(UNIT) if notional_usd > 0 and fill > 0 else None
    return RouteQuote(
        venue=VENUE_UNKNOWN,
        side=side,
        notional_usd=notional_usd,
        fill_price_usd=fill,
        reference_price_usd=observed_price_usd,
        expected_output_tokens=tokens if side == "BUY" else None,
        expected_output_usd=notional_usd,
        price_impact_percent=(penalty * HUNDRED).quantize(Decimal("0.0001")),
        slippage_bps=0,
        fee_bps=0,
        liquidity_usd=liquidity_usd,
        quoted_at=now,
        source=FILL_FALLBACK_PENALISED,
        graduation_state=graduation_state,
        notes=(reason, f"observed price penalised {penalty_bps}bps — not an executable fill"),
    )


@dataclass(frozen=True, slots=True)
class RouteSelection:
    """The chosen route plus every route it beat, for venue comparison."""

    chosen: RouteQuote | None = None
    considered: tuple[RouteQuote, ...] = field(default_factory=tuple)
    rejected: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def available(self) -> bool:
        return self.chosen is not None and self.chosen.usable

    @property
    def venue(self) -> str:
        return self.chosen.venue if self.chosen is not None else VENUE_UNKNOWN


def select_route(
    quotes: Sequence[RouteQuote],
    *,
    max_price_impact_percent: Decimal | None = None,
) -> RouteSelection:
    """Pick the best legitimate executable path at this moment (section 23).

    "Best" is decided by what the trader actually receives, never by a venue
    preference: a BUY takes the lowest effective fill price, a SELL the highest.
    Provenance only breaks ties, so a real quote never loses to a simulation
    that merely rounded better.
    """

    considered = tuple(quotes)
    rejected: list[tuple[str, str]] = []
    usable: list[RouteQuote] = []
    for quote in considered:
        if not quote.usable:
            rejected.append((quote.venue, quote.unavailable_reason or ROUTE_UNAVAILABLE))
            continue
        if (
            max_price_impact_percent is not None
            and quote.price_impact_percent > max_price_impact_percent
        ):
            rejected.append((quote.venue, ROUTE_IMPACT_TOO_HIGH))
            continue
        usable.append(quote)

    if not usable:
        return RouteSelection(chosen=None, considered=considered, rejected=tuple(rejected))

    def buy_key(quote: RouteQuote) -> tuple[Decimal, int, int]:
        price = quote.fill_price_usd or Decimal("999999999")
        return (price, _SOURCE_RANK.get(quote.source, 9), quote.quote_latency_ms)

    def sell_key(quote: RouteQuote) -> tuple[Decimal, int, int]:
        price = quote.fill_price_usd or ZERO
        return (-price, _SOURCE_RANK.get(quote.source, 9), quote.quote_latency_ms)

    side = usable[0].side
    chosen = min(usable, key=sell_key if side == "SELL" else buy_key)
    return RouteSelection(chosen=chosen, considered=considered, rejected=tuple(rejected))


def classify_graduation(
    *,
    curve: BondingCurveState | None = None,
    graduated_at: int | None = None,
    graduation_source: str = "",
    pool_liquidity_usd: Decimal | None = None,
) -> str:
    """Best-effort PRE_GRADUATION / GRADUATED / UNKNOWN (section 21).

    Honest by default: an inference the evidence does not support returns
    ``UNKNOWN`` rather than guessing, and a pair-creation *proxy* is never read
    as an exact Pump graduation.
    """

    if curve is not None and curve.known:
        return curve.graduation_state
    if graduated_at and "PROXY" not in graduation_source.upper():
        return GRADUATED
    if pool_liquidity_usd is not None and pool_liquidity_usd > 0 and graduated_at:
        # A real pool with real depth exists, but the graduation timestamp is a
        # proxy, so this is evidence of an AMM listing, not of graduation.
        return GRADUATION_UNKNOWN
    return GRADUATION_UNKNOWN


def _impact_percent(fill: Decimal, spot: Decimal, *, side: str) -> Decimal:
    if spot <= 0:
        return ZERO
    move = (fill - spot) / spot
    signed = move if side == "BUY" else -move
    return max(ZERO, signed * HUNDRED).quantize(Decimal("0.0001"))


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
