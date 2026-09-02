"""Coins anchored to real stocks, and which of those is worth waking someone for.

The operator's ask, in their words: *"the top coin on every stock"*, and *"if
there's a coin linked with a stock and it's a crazy popular stock, you gotta
ping me"*.

The shape of the opportunity is different from every other lane in this bot, and
that difference is the whole reason this module exists.  A memecoin normally has
no referent — nothing outside its own chart says whether it should be moving.  A
coin **anchored to a stock token** does: the stock is a real instrument with a
real price, real volume and real news, and it moves for reasons that have nothing
to do with crypto.  That gives a signal the trenches never can — *the reason the
coin is about to move already exists off-chain and is publicly visible.*

So the question this module answers is deliberately two-sided:

1. **Is the anchor hot?**  A stock nobody is trading is not a catalyst, however
   good the coin on it looks.
2. **Is this the coin that owns the anchor?**  Several launchpads can each mint
   something against the same ticker.  Being the top coin on a hot stock is the
   thing worth an interruption; being the fourth coin on it is noise wearing a
   famous name.

Both sides have to be true.  Either alone is the failure mode: a hot stock with
a dead coin is a story with nothing to buy, and a lively coin on a stock nobody
cares about is just a memecoin with a ticker for a name.

**Anchoring is an identity claim, and it is checked like one.**  A coin calling
itself $NVDA is making an assertion about a real instrument; the assertion is
either backed by the anchor recorded on-chain at launch, or it is a name.  This
module never resolves a coin from a ticker — the same rule as v2.43.1, for the
same reason.  Ticker collision across launchpads is expected here rather than
exceptional, so it is a first-class output rather than a warning.

Pure logic: no provider, no database, no signer, no order path.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")

# --- how an anchor claim was established -------------------------------------
#: The launch contract records the stock token it was minted against.  This is
#: the only claim strong enough to call a coin "anchored" without hedging.
ANCHOR_ONCHAIN = "ONCHAIN_ANCHOR"
#: The launchpad's own listing names the anchor.  Good evidence, one step
#: removed from the chain.
ANCHOR_LAUNCHPAD = "LAUNCHPAD_DECLARED"
#: Nothing but the coin's own name looks like a ticker.  That is a claim, not a
#: link, and it never earns an interruption on its own.
ANCHOR_NAME_ONLY = "NAME_RESEMBLANCE"

#: Claims strong enough for the coin to be treated as genuinely anchored.
VERIFIED_ANCHORS: frozenset[str] = frozenset({ANCHOR_ONCHAIN, ANCHOR_LAUNCHPAD})

HUMAN_ANCHOR: dict[str, str] = {
    ANCHOR_ONCHAIN: "the launch contract records this stock token as its anchor",
    ANCHOR_LAUNCHPAD: "the launchpad lists this coin against that stock",
    ANCHOR_NAME_ONLY: "only the coin's name resembles the ticker — unverified",
}


@dataclass(frozen=True, slots=True)
class StockAnchor:
    """One real instrument, and how much attention it is getting right now.

    Sourced from Robinhood's documented read-only stock-token endpoints and the
    chain's public RPC.  Every measurement is optional, because a provider that
    did not answer must never read as a stock that nobody is trading — that
    conflation is how a degraded feed starts looking like a quiet market.
    """

    ticker: str
    name: str = ""
    #: The stock token's ERC-20 address on Robinhood Chain.  Identity is this,
    #: never the ticker string.
    token_address: str = ""
    price_usd: Decimal | None = None
    #: Session move, signed, in percent.
    change_percent: Decimal | None = None
    #: Multiple of the stock's own recent-average volume.  A ratio rather than a
    #: raw figure, because a hundred million shares is enormous for one company
    #: and a slow morning for another.
    relative_volume: Decimal | None = None
    #: Independent outlets carrying a story about this instrument right now.
    #: Count, not sentiment: this module never guesses whether news is good.
    news_sources: int | None = None
    #: Set when a corporate action is in flight.  Prices around one are not
    #: comparable to prices before it.
    corporate_action: str = ""

    @property
    def identity_key(self) -> str:
        """Address when we have one, ticker only as a last resort."""

        return (self.token_address or f"ticker:{self.ticker}").lower()

    @property
    def absolute_move(self) -> Decimal | None:
        return None if self.change_percent is None else abs(self.change_percent)

    def to_json(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "name": self.name,
            "token_address": self.token_address,
            "price_usd": _s(self.price_usd),
            "change_percent": _s(self.change_percent),
            "relative_volume": _s(self.relative_volume),
            "news_sources": self.news_sources,
            "corporate_action": self.corporate_action,
        }


@dataclass(frozen=True, slots=True)
class AnchoredCoin:
    """One coin claiming one stock, and how strong that claim is.

    ``anchor_claim`` is the field that does the work.  A coin whose only link to
    NVIDIA is that it called itself $NVDA is a memecoin with a costume on, and
    the whole premise of this lane — that the catalyst is real and public —
    does not apply to it.
    """

    mint: str
    symbol: str = ""
    name: str = ""
    launchpad: str = ""
    #: Which anchor this coin claims, by the anchor's own identity key.
    anchor_key: str = ""
    anchor_ticker: str = ""
    anchor_claim: str = ANCHOR_NAME_ONLY
    created_at: int | None = None
    age_seconds: int | None = None
    market_cap_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_usd: Decimal | None = None
    holder_count: int | None = None
    buys: int | None = None
    sells: int | None = None

    @property
    def verified_anchor(self) -> bool:
        return self.anchor_claim in VERIFIED_ANCHORS

    @property
    def sell_pressure(self) -> Decimal | None:
        if self.buys is None or self.sells is None:
            return None
        if self.buys <= 0:
            return Decimal(self.sells) if self.sells else None
        return (Decimal(self.sells) / Decimal(self.buys)).quantize(CENT)

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "name": self.name,
            "launchpad": self.launchpad,
            "anchor_key": self.anchor_key,
            "anchor_ticker": self.anchor_ticker,
            "anchor_claim": self.anchor_claim,
            "verified_anchor": self.verified_anchor,
            "age_seconds": self.age_seconds,
            "market_cap_usd": _s(self.market_cap_usd),
            "liquidity_usd": _s(self.liquidity_usd),
            "volume_usd": _s(self.volume_usd),
            "holder_count": self.holder_count,
            "sell_pressure": _s(self.sell_pressure),
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
