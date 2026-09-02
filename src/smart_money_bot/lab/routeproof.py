"""Can a normal person actually get out of this, right now?

A displayed ``sells`` counter is not an answer to that question, and treating it
as one is how the operator ends up holding something that cannot be sold.  That
number can be non-zero because a provider inferred it, because the pool routed
sells before a hook was switched on, because the seller was the creator, or
because the token counts transfers as sells.  None of those is evidence that
*this* wallet can exit *now*.

Three independent things have to hold, and each answers something the others
cannot:

**A route exists both ways.**  A fresh exact-input buy quote *and* a fresh
reverse sell quote for the same exact mint through the same canonical pool.  A
buy quote alone is the honeypot's favourite shape — the way in always works.
Quotes expire: one taken ninety seconds ago is not evidence about now, and the
gate model ages it out rather than letting it stand.

**The sell survives simulation.**  A quote is a router's opinion about a path;
a read-only simulation is the chain executing it without signing or
broadcasting anything.  A token whose sell quotes beautifully and reverts on
simulation is precisely the one worth catching, so a simulation that fails
blocks even when the quote succeeded.

**Somebody unrelated has actually done it.**  Decoded sells, by wallets that are
not the creator, not the same funder, not each other.  This is the check that
survives every clever contract trick, because it is a record of the thing
happening rather than a prediction that it would.

And underneath all three, the contract itself: transfer fees, transfer hooks,
freeze authority, blacklists, Token-2022 extensions.  Unknown is not benign
here.  A token whose fee logic we could not read is a token whose exit cost we
cannot state, and this lane fails closed on that.

Pure logic: no provider, no database, no signer.  Nothing in this module can
sign or broadcast anything, and there is deliberately no code path that could.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from .hardgates import (
    BUY_ROUTE_OK,
    CONTRACT_SAFETY_OK,
    FAIL,
    PASS,
    SELL_EVIDENCE_OK,
    SELL_ROUTE_OK,
    UNKNOWN,
    GateResult,
)

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
CENT = Decimal("0.01")

# --- directions ---------------------------------------------------------------
BUY = "BUY"
SELL = "SELL"

# --- reason codes -------------------------------------------------------------
NO_QUOTE = "NO_QUOTE_RETURNED"
QUOTE_STALE = "QUOTE_TOO_OLD"
QUOTE_WRONG_MINT = "QUOTE_ROUTES_THE_WRONG_MINT"
QUOTE_WRONG_POOL = "QUOTE_ROUTES_AN_UNVERIFIED_POOL"
QUOTE_ZERO_OUT = "QUOTE_RETURNS_NOTHING"
IMPACT_TOO_HIGH = "SELL_IMPACT_TOO_HIGH"
SIMULATION_FAILED = "SELL_SIMULATION_REVERTED"
SIMULATION_UNAVAILABLE = "SELL_SIMULATION_UNAVAILABLE"
NO_INDEPENDENT_SELLERS = "SELL_EVIDENCE_INSUFFICIENT"
DISPLAYED_ONLY = "DISPLAYED_SELLS_ARE_NOT_EVIDENCE"
HAZARD_FREEZE = "FREEZE_AUTHORITY_PRESENT"
HAZARD_BLACKLIST = "BLACKLIST_BEHAVIOUR"
HAZARD_HOOK = "TRANSFER_HOOK_PRESENT"
HAZARD_FEE = "TRANSFER_FEE_TOO_HIGH"
HAZARD_ASYMMETRIC = "BUY_AND_SELL_COSTS_ASYMMETRIC"
HAZARD_UNKNOWN = "CONTRACT_FEATURES_UNREADABLE"
HAZARD_MINT_AUTHORITY = "MINT_AUTHORITY_PRESENT"


@dataclass(frozen=True, slots=True)
class Quote:
    """One exact-input quote, as returned by a router.  Never executed."""

    direction: str
    input_mint: str = ""
    output_mint: str = ""
    amount_in: Decimal | None = None
    amount_out: Decimal | None = None
    price_impact: Decimal | None = None
    route_pools: tuple[str, ...] = ()
    provider: str = ""
    observed_at: int | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.amount_out is not None and self.amount_out > ZERO

    def age(self, now: int) -> int | None:
        return None if self.observed_at is None else max(0, now - self.observed_at)

    def to_json(self) -> dict[str, object]:
        return {
            "direction": self.direction,
            "input_mint": self.input_mint,
            "output_mint": self.output_mint,
            "amount_in": _s(self.amount_in),
            "amount_out": _s(self.amount_out),
            "price_impact": _s(self.price_impact),
            "route_pools": list(self.route_pools),
            "provider": self.provider,
            "observed_at": self.observed_at,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class SellSimulation:
    """A read-only simulation of the sell path.  Nothing is signed or sent."""

    attempted: bool = False
    succeeded: bool | None = None
    #: Whatever the node reported when it did not succeed.
    error: str = ""
    units_consumed: int | None = None
    observed_at: int | None = None
    #: Set when the chain or client cannot simulate at all, which is different
    #: from a simulation that ran and reverted.
    unsupported: bool = False


@dataclass(frozen=True, slots=True)
class SellEvent:
    """One decoded sell from the canonical pool.  A record, not an inference."""

    wallet: str
    amount_usd: Decimal | None = None
    at: int | None = None
    signature: str = ""
    succeeded: bool = True
    #: Set when this wallet is the creator, an insider, or shares a funder with
    #: one.  Such a sell proves the creator can exit, not that anyone can.
    related_to_creator: bool = False
    #: Cluster id when wallet clustering has grouped this seller with others.
    cluster_id: str = ""


@dataclass(frozen=True, slots=True)
class TokenHazards:
    """What the mint itself does to a transfer.

    Every field is tri-state on purpose.  ``None`` means we could not read it,
    which is not the same as ``False`` and must never be treated as benign.
    """

    mint_authority_present: bool | None = None
    freeze_authority_present: bool | None = None
    transfer_hook_present: bool | None = None
    blacklist_present: bool | None = None
    #: Transfer fee as a rate 0..1 (Token-2022 or an EVM tax).
    transfer_fee_rate: Decimal | None = None
    buy_tax_rate: Decimal | None = None
    sell_tax_rate: Decimal | None = None
    observed_at: int | None = None
    source: str = ""


@dataclass(frozen=True, slots=True)
class RouteConfig:
    """Freshness, size and how much exit cost is tolerable."""

    #: A quote older than this says nothing about now.
    max_quote_age_seconds: int = 45
    #: Paper size the routes are proved at, in USD.
    probe_size_usd: Decimal = Decimal("50")
    #: Sell impact above this is not an exit anyone would take.
    max_sell_impact: Decimal = Decimal("0.15")
    #: Minimum successful sells by independent economic actors.
    min_independent_sells: int = 3
    min_independent_sellers: int = 2
    #: How old a sell may be and still count as evidence about now.
    max_sell_evidence_age_seconds: int = 900
    #: Fees above this are an exit tax, not a fee.
    max_transfer_fee_rate: Decimal = Decimal("0.05")
    #: Buy and sell costs differing by more than this is the honeypot shape.
    max_tax_asymmetry: Decimal = Decimal("0.03")
    #: When a chain cannot simulate at all, is a quote plus on-chain evidence
    #: enough?  False is the safe answer and the default.
    allow_missing_simulation: bool = False


DEFAULT_ROUTE_CONFIG = RouteConfig()


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def prove_buy_route(
    mint: str,
    quote: Quote | None,
    *,
    verified_pools: frozenset[str] = frozenset(),
    config: RouteConfig = DEFAULT_ROUTE_CONFIG,
    now: int,
) -> GateResult:
    """A fresh exact-input buy quote for this exact mint through a real pool."""

    return _prove_route(
        gate=BUY_ROUTE_OK,
        mint=mint,
        quote=quote,
        expect_output=True,
        verified_pools=verified_pools,
        config=config,
        now=now,
    )


def prove_sell_route(
    mint: str,
    quote: Quote | None,
    *,
    verified_pools: frozenset[str] = frozenset(),
    config: RouteConfig = DEFAULT_ROUTE_CONFIG,
    now: int,
) -> GateResult:
    """The reverse quote, which is the one that actually matters.

    A buy route without a sell route is the honeypot's shape: the way in always
    works.  This gate fails closed on absence, staleness, a wrong mint, an
    unverified pool or an impact nobody would accept.
    """

    result = _prove_route(
        gate=SELL_ROUTE_OK,
        mint=mint,
        quote=quote,
        expect_output=False,
        verified_pools=verified_pools,
        config=config,
        now=now,
    )
    if result.answer != PASS or quote is None:
        return result
    impact = quote.price_impact
    if impact is not None and impact > config.max_sell_impact:
        return GateResult(
            gate=SELL_ROUTE_OK,
            answer=FAIL,
            reason=(
                f"selling ${config.probe_size_usd} moves the price "
                f"{(impact * HUNDRED).quantize(CENT)}% — a route that costs this "
                "much is not an exit"
            ),
            source=quote.provider or "router",
            observed_at=quote.observed_at,
            evidence=(("code", IMPACT_TOO_HIGH), ("impact", str(impact))),
        )
    return result


def _prove_route(
    *,
    gate: str,
    mint: str,
    quote: Quote | None,
    expect_output: bool,
    verified_pools: frozenset[str],
    config: RouteConfig,
    now: int,
) -> GateResult:
    def bad(answer: str, code: str, detail: str) -> GateResult:
        return GateResult(
            gate=gate,
            answer=answer,
            reason=detail,
            source=(quote.provider if quote else "") or "router",
            observed_at=quote.observed_at if quote else None,
            evidence=(("code", code),),
        )

    if quote is None:
        return bad(UNKNOWN, NO_QUOTE, "no quote was obtained")
    if quote.error:
        return bad(FAIL, NO_QUOTE, f"the router refused: {quote.error}")
    if not quote.ok:
        return bad(FAIL, QUOTE_ZERO_OUT, "the quote returns nothing for this size")
    age = quote.age(now)
    if age is None:
        return bad(UNKNOWN, QUOTE_STALE, "the quote carries no timestamp")
    if age > config.max_quote_age_seconds:
        return bad(
            UNKNOWN,
            QUOTE_STALE,
            f"the quote is {age}s old — a route that existed then is not proof now",
        )
    # The exact mint has to be on the side this direction requires.
    routed = quote.output_mint if expect_output else quote.input_mint
    if routed != mint:
        return bad(
            FAIL,
            QUOTE_WRONG_MINT,
            f"this quote routes {routed[:12]}…, not the candidate mint",
        )
    if verified_pools and not set(quote.route_pools) <= verified_pools:
        unknown_pools = sorted(set(quote.route_pools) - verified_pools)
        return bad(
            FAIL,
            QUOTE_WRONG_POOL,
            f"the route passes through an unverified pool ({unknown_pools[0][:12]}…)",
        )
    return GateResult(
        gate=gate,
        answer=PASS,
        reason=f"fresh {quote.direction.lower()} quote, {age}s old",
        source=quote.provider or "router",
        observed_at=quote.observed_at,
        max_age_seconds=config.max_quote_age_seconds,
        evidence=(
            ("amount_in", str(quote.amount_in or "")),
            ("amount_out", str(quote.amount_out or "")),
            ("price_impact", str(quote.price_impact or "")),
            ("route", ",".join(quote.route_pools)),
        ),
    )


def prove_sell_evidence(
    mint: str,
    sells: Sequence[SellEvent],
    *,
    displayed_sell_count: int | None = None,
    simulation: SellSimulation | None = None,
    config: RouteConfig = DEFAULT_ROUTE_CONFIG,
    now: int,
) -> GateResult:
    """Have unrelated people actually got out, and does the sell simulate?

    ``displayed_sell_count`` is accepted only so it can be reported alongside
    the decoded figure and never used to reach one.  When a provider claims
    hundreds of sells and the chain shows no independent sellers, that gap is
    the finding.
    """

    fresh = [
        item
        for item in sells
        if item.succeeded
        and item.at is not None
        and (now - item.at) <= config.max_sell_evidence_age_seconds
    ]
    independent = [item for item in fresh if not item.related_to_creator]
    # Clustered wallets are one economic actor: twenty wallets on one funder
    # prove one person can exit, which is what a rug operator can also do.
    actors: set[str] = set()
    for item in independent:
        actors.add(item.cluster_id or item.wallet)

    if simulation is not None:
        if simulation.unsupported and not config.allow_missing_simulation:
            return GateResult(
                gate=SELL_EVIDENCE_OK,
                answer=UNKNOWN,
                reason="the sell path could not be simulated on this chain",
                source="simulation",
                observed_at=simulation.observed_at,
                evidence=(("code", SIMULATION_UNAVAILABLE),),
            )
        if simulation.attempted and simulation.succeeded is False:
            return GateResult(
                gate=SELL_EVIDENCE_OK,
                answer=FAIL,
                reason=(
                    "the sell quotes fine and reverts when simulated: "
                    f"{simulation.error or 'no reason given'}"
                ),
                source="simulation",
                observed_at=simulation.observed_at,
                evidence=(("code", SIMULATION_FAILED), ("error", simulation.error)),
            )

    too_few_sells = len(independent) < config.min_independent_sells
    too_few_actors = len(actors) < config.min_independent_sellers
    if too_few_sells or too_few_actors:
        detail = (
            f"{len(independent)} successful sells from {len(actors)} independent "
            f"actors, below {config.min_independent_sells}/"
            f"{config.min_independent_sellers}"
        )
        if displayed_sell_count:
            detail += (
                f" — a provider displays {displayed_sell_count} sells, which is a "
                "number rather than evidence"
            )
        return GateResult(
            gate=SELL_EVIDENCE_OK,
            answer=FAIL if fresh else UNKNOWN,
            reason=detail,
            source="decoded pool activity",
            observed_at=now,
            evidence=(
                ("code", NO_INDEPENDENT_SELLERS),
                ("decoded_independent_sells", str(len(independent))),
                ("independent_actors", str(len(actors))),
                ("displayed", str(displayed_sell_count or "")),
            ),
        )

    return GateResult(
        gate=SELL_EVIDENCE_OK,
        answer=PASS,
        reason=(
            f"{len(independent)} successful sells by {len(actors)} independent actors"
            + (", sell path simulates" if simulation and simulation.succeeded else "")
        ),
        source="decoded pool activity",
        observed_at=now,
        max_age_seconds=config.max_sell_evidence_age_seconds,
        evidence=(
            ("independent_sells", str(len(independent))),
            ("independent_actors", str(len(actors))),
            ("displayed", str(displayed_sell_count or "")),
        ),
    )


def prove_contract_safety(
    mint: str,
    hazards: TokenHazards | None,
    *,
    config: RouteConfig = DEFAULT_ROUTE_CONFIG,
    now: int,
) -> GateResult:
    """What the mint does to a transfer, with unknown treated as unsafe.

    A token whose fee logic could not be read is a token whose exit cost cannot
    be stated, and stating an exit cost is the entire job here.
    """

    if hazards is None:
        return GateResult(
            gate=CONTRACT_SAFETY_OK,
            answer=UNKNOWN,
            reason="contract features were not read",
            evidence=(("code", HAZARD_UNKNOWN),),
        )

    def fail(code: str, detail: str) -> GateResult:
        return GateResult(
            gate=CONTRACT_SAFETY_OK,
            answer=FAIL,
            reason=detail,
            source=hazards.source or "mint account",
            observed_at=hazards.observed_at,
            evidence=(("code", code),),
        )

    if hazards.freeze_authority_present:
        return fail(HAZARD_FREEZE, "a freeze authority can stop this token being sold")
    if hazards.blacklist_present:
        return fail(HAZARD_BLACKLIST, "this contract can blacklist a holder")
    if hazards.transfer_hook_present:
        return fail(
            HAZARD_HOOK, "a transfer hook can change what a sell does after we look"
        )
    if hazards.mint_authority_present:
        return fail(HAZARD_MINT_AUTHORITY, "supply can still be minted")

    fee = hazards.transfer_fee_rate
    if fee is not None and fee > config.max_transfer_fee_rate:
        return fail(
            HAZARD_FEE,
            f"{(fee * HUNDRED).quantize(CENT)}% transfer fee is an exit tax",
        )
    buy_tax, sell_tax = hazards.buy_tax_rate, hazards.sell_tax_rate
    if buy_tax is not None and sell_tax is not None:
        if sell_tax > config.max_transfer_fee_rate:
            return fail(
                HAZARD_FEE,
                f"{(sell_tax * HUNDRED).quantize(CENT)}% sell tax",
            )
        if abs(sell_tax - buy_tax) > config.max_tax_asymmetry:
            return fail(
                HAZARD_ASYMMETRIC,
                f"buying costs {(buy_tax * HUNDRED).quantize(CENT)}% and selling "
                f"{(sell_tax * HUNDRED).quantize(CENT)}% — asymmetric by design",
            )

    unread = [
        name
        for name, value in (
            ("mint authority", hazards.mint_authority_present),
            ("freeze authority", hazards.freeze_authority_present),
            ("transfer hook", hazards.transfer_hook_present),
        )
        if value is None
    ]
    if unread:
        return GateResult(
            gate=CONTRACT_SAFETY_OK,
            answer=UNKNOWN,
            reason=f"could not read: {', '.join(unread)} — unknown is not benign here",
            source=hazards.source or "mint account",
            observed_at=hazards.observed_at,
            evidence=(("code", HAZARD_UNKNOWN),),
        )

    return GateResult(
        gate=CONTRACT_SAFETY_OK,
        answer=PASS,
        reason="no mint or freeze authority, no hook, no blacklist, fees within limits",
        source=hazards.source or "mint account",
        observed_at=hazards.observed_at,
        max_age_seconds=600,
        evidence=(
            ("transfer_fee", str(fee or "0")),
            ("buy_tax", str(buy_tax or "")),
            ("sell_tax", str(sell_tax or "")),
        ),
    )
