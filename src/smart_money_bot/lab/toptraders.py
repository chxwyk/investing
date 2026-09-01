"""Who is actually holding this exact mint, and are any of them useful.

Terminal's documented Top Traders view answers a question a price chart cannot:
*the people on the other side of this move — are they early, are they adding, or
are they selling into the people arriving now?*  This module builds an
independent equivalent from public Solana fills, and it is deliberately built
around three refusals:

**A large wallet is not a smart wallet** (section 7).  Reputation lives in
:mod:`.smartmoney` and is earned only from observed forward outcomes.  Nothing
here promotes a wallet for being big; size decides *ranking*, history decides
*weight*, and the two are never mixed.

**Five wallets sharing one funder are one actor** (section 8).  Confirmations
are collapsed per cluster before they are counted, so a sybil group cannot
manufacture its own consensus.

**A position is a story over time, not a snapshot** (section 9).  A known trader
who bought and is still adding, and one who bought and is now distributing into
later buyers, look identical if you only count the entry.

Pure logic: no provider, no database, no signer.  Fills come in, an assessment
goes out, and every number belongs to one exact mint.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal("0")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")

BUY = "BUY"
SELL = "SELL"

# --- position lifecycle (section 9) -----------------------------------------
#: Bought once and has not been seen doing anything since.
POS_BUYING = "BUYING"
#: Entered more than once, growing the position.
POS_ADDING = "ADDING"
#: Entered and has sold nothing.
POS_HOLDING = "HOLDING"
#: Has taken some off, but most of the position is still on.
POS_PARTIAL_SELLING = "PARTIAL_SELLING"
#: Selling the majority of what they bought while the token is still live.
POS_DISTRIBUTING = "DISTRIBUTING"
#: Effectively out.
POS_EXITED = "EXITED"
#: Not enough observed fills to say anything.
POS_UNKNOWN = "UNKNOWN"

#: Postures that mean the trader is on the same side as a new buyer.
SUPPORTIVE_STATES: frozenset[str] = frozenset({POS_BUYING, POS_ADDING, POS_HOLDING})
#: Postures that mean early money is selling into later money.
EXIT_STATES: frozenset[str] = frozenset({POS_DISTRIBUTING, POS_EXITED})


@dataclass(frozen=True, slots=True)
class TraderFill:
    """One observed public fill, already resolved to an exact mint.

    ``market_cap_usd`` is the market cap *at the time of the fill*, which is what
    makes "entered at $69K, it is $210K now" sayable at all.  It is optional
    because it is not always knowable, and an unknown entry is reported as
    unknown rather than back-filled from the current price.
    """

    wallet: str
    mint: str
    side: str
    at: int
    amount_usd: Decimal = ZERO
    tokens: Decimal = ZERO
    market_cap_usd: Decimal | None = None
    signature: str = ""

    @property
    def is_buy(self) -> bool:
        return self.side.upper() == BUY

    @property
    def is_sell(self) -> bool:
        return self.side.upper() == SELL


@dataclass(frozen=True, slots=True)
class TraderPosition:
    """What one wallet did to one mint, over every fill we observed."""

    wallet: str
    mint: str
    buys: int = 0
    sells: int = 0
    bought_usd: Decimal = ZERO
    sold_usd: Decimal = ZERO
    tokens_bought: Decimal = ZERO
    tokens_sold: Decimal = ZERO
    first_buy_at: int | None = None
    last_event_at: int | None = None
    first_buy_market_cap_usd: Decimal | None = None
    last_sell_market_cap_usd: Decimal | None = None
    state: str = POS_UNKNOWN

    @property
    def net_tokens(self) -> Decimal:
        return self.tokens_bought - self.tokens_sold

    @property
    def sold_fraction(self) -> Decimal | None:
        """How much of the acquired position has been sold, 0..1."""

        if self.tokens_bought <= ZERO:
            return None
        return min(Decimal("1"), self.tokens_sold / self.tokens_bought)

    @property
    def realised_usd(self) -> Decimal:
        """Cash out minus cash in.  Not a P&L — the remaining bag is not priced."""

        return self.sold_usd - self.bought_usd

    @property
    def supportive(self) -> bool:
        return self.state in SUPPORTIVE_STATES

    @property
    def exiting(self) -> bool:
        return self.state in EXIT_STATES

    def move_since_entry_percent(self, current_market_cap_usd: Decimal | None) -> Decimal | None:
        base = self.first_buy_market_cap_usd
        if base is None or base <= ZERO or current_market_cap_usd is None:
            return None
        return ((current_market_cap_usd - base) / base * HUNDRED).quantize(CENT)

    def to_json(self) -> dict[str, object]:
        return {
            "wallet": self.wallet,
            "mint": self.mint,
            "buys": self.buys,
            "sells": self.sells,
            "bought_usd": _s(self.bought_usd),
            "sold_usd": _s(self.sold_usd),
            "tokens_bought": _s(self.tokens_bought),
            "tokens_sold": _s(self.tokens_sold),
            "net_tokens": _s(self.net_tokens),
            "sold_fraction": _s(self.sold_fraction),
            "realised_usd": _s(self.realised_usd),
            "first_buy_at": self.first_buy_at,
            "last_event_at": self.last_event_at,
            "first_buy_market_cap_usd": _s(self.first_buy_market_cap_usd),
            "state": self.state,
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True, slots=True)
class PositionConfig:
    """Where one posture ends and the next begins."""

    #: Sold this fraction or more of the position: distributing.
    distributing_fraction: Decimal = Decimal("0.5")
    #: Below this much of the position remaining: out.
    exited_fraction: Decimal = Decimal("0.95")
    #: More than one buy and no sells: adding.
    adding_min_buys: int = 2


DEFAULT_POSITION_CONFIG = PositionConfig()


def classify_position(
    *,
    buys: int,
    sells: int,
    tokens_bought: Decimal,
    tokens_sold: Decimal,
    config: PositionConfig = DEFAULT_POSITION_CONFIG,
) -> str:
    """Name the posture from the fills alone (section 9).

    Token amounts decide it rather than USD, because a wallet that sold half its
    tokens into a tripling price has taken *more* dollars out than it put in
    while still holding half the position — that is not distribution, and a
    dollar-based rule would call it that.
    """

    if buys <= 0:
        return POS_UNKNOWN
    if tokens_bought <= ZERO:
        # We saw buys but could not size them; sells are still informative.
        return POS_PARTIAL_SELLING if sells > 0 else POS_BUYING
    sold = min(Decimal("1"), tokens_sold / tokens_bought)
    if sold >= config.exited_fraction:
        return POS_EXITED
    if sold >= config.distributing_fraction:
        return POS_DISTRIBUTING
    if sells > 0:
        return POS_PARTIAL_SELLING
    if buys >= config.adding_min_buys:
        return POS_ADDING
    return POS_BUYING


def build_positions(
    fills: Sequence[TraderFill],
    *,
    mint: str,
    config: PositionConfig = DEFAULT_POSITION_CONFIG,
) -> tuple[TraderPosition, ...]:
    """Fold fills into one position per wallet, for this exact mint only.

    Fills for any other mint are dropped rather than merged.  A wallet's history
    on a same-ticker token is not evidence about this one (section 27).
    """

    grouped: dict[str, list[TraderFill]] = {}
    for fill in fills:
        if fill.mint != mint or not fill.wallet:
            continue
        grouped.setdefault(fill.wallet, []).append(fill)

    positions: list[TraderPosition] = []
    for wallet, wallet_fills in grouped.items():
        ordered = sorted(wallet_fills, key=lambda item: item.at)
        buys = [item for item in ordered if item.is_buy]
        sells = [item for item in ordered if item.is_sell]
        tokens_bought = sum((item.tokens for item in buys), ZERO)
        tokens_sold = sum((item.tokens for item in sells), ZERO)
        first_buy = buys[0] if buys else None
        last_sell = sells[-1] if sells else None
        positions.append(
            TraderPosition(
                wallet=wallet,
                mint=mint,
                buys=len(buys),
                sells=len(sells),
                bought_usd=sum((item.amount_usd for item in buys), ZERO),
                sold_usd=sum((item.amount_usd for item in sells), ZERO),
                tokens_bought=tokens_bought,
                tokens_sold=tokens_sold,
                first_buy_at=first_buy.at if first_buy else None,
                last_event_at=ordered[-1].at if ordered else None,
                first_buy_market_cap_usd=first_buy.market_cap_usd if first_buy else None,
                last_sell_market_cap_usd=last_sell.market_cap_usd if last_sell else None,
                state=classify_position(
                    buys=len(buys),
                    sells=len(sells),
                    tokens_bought=tokens_bought,
                    tokens_sold=tokens_sold,
                    config=config,
                ),
            )
        )
    return tuple(sorted(positions, key=lambda item: (-item.bought_usd, item.wallet)))


# --- ranking (section 5) -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class TopTraderBoard:
    """The public participant picture for one exact mint."""

    mint: str
    top_buyers: tuple[TraderPosition, ...] = ()
    top_sellers: tuple[TraderPosition, ...] = ()
    largest_holders: tuple[TraderPosition, ...] = ()
    early_entrants: tuple[TraderPosition, ...] = ()
    adding: tuple[TraderPosition, ...] = ()
    distributing: tuple[TraderPosition, ...] = ()

    @property
    def observed_wallets(self) -> int:
        seen = {item.wallet for item in self.top_buyers}
        seen |= {item.wallet for item in self.top_sellers}
        return len(seen)

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "top_buyers": [item.to_json() for item in self.top_buyers],
            "top_sellers": [item.to_json() for item in self.top_sellers],
            "largest_holders": [item.to_json() for item in self.largest_holders],
            "early_entrants": [item.to_json() for item in self.early_entrants],
            "adding": [item.to_json() for item in self.adding],
            "distributing": [item.to_json() for item in self.distributing],
            "observed_wallets": self.observed_wallets,
        }


def rank_top_traders(
    positions: Sequence[TraderPosition],
    *,
    mint: str,
    limit: int = 10,
) -> TopTraderBoard:
    """Rank observed participants six ways, without ranking *reputation*.

    Being the largest buyer earns a place on this board and nothing else.  What
    a wallet's history is worth is decided in :mod:`.smartmoney`, separately, on
    forward outcomes.
    """

    own = [item for item in positions if item.mint == mint]
    buyers = sorted(
        (item for item in own if item.bought_usd > ZERO),
        key=lambda item: (-item.bought_usd, item.wallet),
    )
    sellers = sorted(
        (item for item in own if item.sold_usd > ZERO),
        key=lambda item: (-item.sold_usd, item.wallet),
    )
    holders = sorted(
        (item for item in own if item.net_tokens > ZERO),
        key=lambda item: (-item.net_tokens, item.wallet),
    )
    entrants = sorted(
        (item for item in own if item.first_buy_at is not None),
        key=lambda item: (item.first_buy_at or 0, item.wallet),
    )
    return TopTraderBoard(
        mint=mint,
        top_buyers=tuple(buyers[:limit]),
        top_sellers=tuple(sellers[:limit]),
        largest_holders=tuple(holders[:limit]),
        early_entrants=tuple(entrants[:limit]),
        adding=tuple(item for item in own if item.state == POS_ADDING)[:limit],
        distributing=tuple(item for item in own if item.exiting)[:limit],
    )


# --- known traders and independence (sections 6, 7, 8) -----------------------


@dataclass(frozen=True, slots=True)
class KnownTrader:
    """A ranked participant that the registry already knows something about."""

    wallet: str
    mint: str
    display_name: str = ""
    reputation_state: str = "UNKNOWN"
    reputation_samples: int = 0
    position: TraderPosition | None = None
    cluster_id: str = ""

    @property
    def state(self) -> str:
        return self.position.state if self.position is not None else POS_UNKNOWN

    @property
    def proven(self) -> bool:
        """Whether this wallet's history has actually earned weight.

        ``PROVEN_EARLY`` on a two-sample history is a coincidence with a label,
        so the sample floor is part of the question, not a footnote.
        """

        return (
            self.reputation_state in {"PROVEN_EARLY", "USEFUL_CONFIRMATION"}
            and self.reputation_samples >= MIN_PROVEN_SAMPLES
        )

    def to_json(self) -> dict[str, object]:
        return {
            "wallet": self.wallet,
            "mint": self.mint,
            "display_name": self.display_name,
            "reputation_state": self.reputation_state,
            "reputation_samples": self.reputation_samples,
            "state": self.state,
            "cluster_id": self.cluster_id,
            "proven": self.proven,
            "position": self.position.to_json() if self.position is not None else None,
        }


#: Below this many observed forward outcomes a reputation label is decoration.
MIN_PROVEN_SAMPLES = 8


def join_known_traders(
    positions: Sequence[TraderPosition],
    *,
    mint: str,
    registry: Mapping[str, str],
    reputations: Mapping[str, tuple[str, int]] | None = None,
    clusters: Mapping[str, str] | None = None,
) -> tuple[KnownTrader, ...]:
    """Attach registry identity and reputation to the wallets we observed.

    ``registry`` maps wallet → display name; ``reputations`` maps wallet →
    ``(state, samples)``; ``clusters`` maps wallet → cluster id.  Every lookup is
    by exact wallet address, and every position is for the exact mint — a known
    wallet's activity on another token never transfers here (section 27).
    """

    reputations = reputations or {}
    clusters = clusters or {}
    known: list[KnownTrader] = []
    for position in positions:
        if position.mint != mint or position.wallet not in registry:
            continue
        state, samples = reputations.get(position.wallet, ("UNKNOWN", 0))
        known.append(
            KnownTrader(
                wallet=position.wallet,
                mint=mint,
                display_name=registry[position.wallet],
                reputation_state=state,
                reputation_samples=samples,
                position=position,
                cluster_id=clusters.get(position.wallet, ""),
            )
        )
    return tuple(sorted(known, key=lambda item: (-(item.position.bought_usd), item.wallet)))


@dataclass(frozen=True, slots=True)
class TraderConfirmation:
    """How much independent agreement the known wallets actually represent."""

    mint: str
    traders: tuple[KnownTrader, ...] = ()
    #: Distinct wallets, before any cluster collapsing.
    wallet_count: int = 0
    #: Distinct actors, after collapsing each cluster to one.
    independent_count: int = 0
    #: Independent actors whose reputation has enough samples to carry weight.
    proven_independent_count: int = 0
    clusters: tuple[str, ...] = ()
    supportive: int = 0
    distributing: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def cluster_adjusted(self) -> bool:
        """True when collapsing clusters actually reduced the count."""

        return self.independent_count < self.wallet_count

    @property
    def confirms(self) -> bool:
        """At least one proven, independent wallet that is not on the way out."""

        return self.proven_independent_count >= 1 and self.supportive > 0

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "traders": [item.to_json() for item in self.traders],
            "wallet_count": self.wallet_count,
            "independent_count": self.independent_count,
            "proven_independent_count": self.proven_independent_count,
            "clusters": list(self.clusters),
            "supportive": self.supportive,
            "distributing": self.distributing,
            "cluster_adjusted": self.cluster_adjusted,
            "confirms": self.confirms,
            "notes": list(self.notes),
        }


def independent_confirmations(
    traders: Sequence[KnownTrader],
    *,
    mint: str,
) -> TraderConfirmation:
    """Collapse clustered wallets to one actor before counting agreement.

    Section 8 in one line: five top traders sharing one funder are one opinion
    held five times, and counting them as five is how a sybil group writes its
    own confirmation.  Wallets with no cluster evidence each count once, because
    absence of a detected link is not evidence of a link.
    """

    own = [item for item in traders if item.mint == mint]
    wallets = {item.wallet for item in own}
    actors: set[str] = set()
    proven_actors: set[str] = set()
    for trader in own:
        actor = trader.cluster_id or f"wallet:{trader.wallet}"
        actors.add(actor)
        if trader.proven:
            proven_actors.add(actor)

    clusters = sorted({item.cluster_id for item in own if item.cluster_id})
    notes: list[str] = []
    if clusters and len(actors) < len(wallets):
        notes.append(
            f"{len(wallets)} known wallets collapse to {len(actors)} independent "
            f"actor(s) across {len(clusters)} cluster(s)"
        )
    return TraderConfirmation(
        mint=mint,
        traders=tuple(own),
        wallet_count=len(wallets),
        independent_count=len(actors),
        proven_independent_count=len(proven_actors),
        clusters=tuple(clusters),
        supportive=sum(1 for item in own if item.position and item.position.supportive),
        distributing=sum(1 for item in own if item.position and item.position.exiting),
        notes=tuple(notes),
    )


# --- accumulation versus distribution over time (section 9) ------------------

FLOW_ACCUMULATING = "KNOWN_MONEY_ACCUMULATING"
FLOW_MIXED = "KNOWN_MONEY_MIXED"
FLOW_DISTRIBUTING = "KNOWN_MONEY_DISTRIBUTING"
FLOW_UNKNOWN = "KNOWN_MONEY_UNKNOWN"


def known_money_flow(confirmation: TraderConfirmation) -> str:
    """Which way the wallets that already know this token are leaning.

    A token where known traders are still adding and one where they are selling
    into new buyers can print the same price candle.  They are not the same
    trade, and the operator is told which one this is.
    """

    if not confirmation.traders:
        return FLOW_UNKNOWN
    if confirmation.distributing and not confirmation.supportive:
        return FLOW_DISTRIBUTING
    if confirmation.supportive and not confirmation.distributing:
        return FLOW_ACCUMULATING
    if confirmation.supportive and confirmation.distributing:
        return FLOW_MIXED
    return FLOW_UNKNOWN


def summarise_board(board: TopTraderBoard, confirmation: TraderConfirmation) -> tuple[str, ...]:
    """Short operator-facing lines.  Only what the fills actually support."""

    lines: list[str] = []
    if board.top_buyers:
        largest = board.top_buyers[0]
        lines.append(
            f"largest observed buyer {largest.wallet[:6]}… "
            f"${largest.bought_usd:,.0f} • {largest.state.replace('_', ' ').lower()}"
        )
    if confirmation.traders:
        lines.append(
            f"{confirmation.proven_independent_count} proven independent "
            f"known wallet(s) of {confirmation.wallet_count} observed"
        )
    lines.extend(confirmation.notes)
    if board.distributing:
        lines.append(f"{len(board.distributing)} observed wallet(s) distributing")
    return tuple(lines)


def wallets_of(positions: Iterable[TraderPosition]) -> tuple[str, ...]:
    return tuple(sorted({item.wallet for item in positions}))
