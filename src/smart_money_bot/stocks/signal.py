"""Hot anchor times top coin.  Both sides, or nobody gets woken up.

The operator asked for the loudest alert this bot produces on this lane, and the
reason is sound: a coin anchored to a stock has a catalyst that exists off-chain
and in public, which is a thing no trench launch has ever had.  Loud is
appropriate.  Loud and wrong is what the last four releases have been about.

So the bar here is deliberately *higher* than the memecoin lanes, not lower.
Three things have to hold at once, and each of them kills the alert on its own:

**The anchor has to be hot.**  A stock nobody is trading is not a catalyst.
Movement is measured against the instrument's own normal — relative volume, not
share count — because a hundred million shares is enormous for one company and a
quiet morning for another.

**The claim has to be real.**  A coin named $NVDA that no launchpad and no
contract ever linked to NVIDIA is a memecoin in a costume, and the entire premise
of this lane does not apply to it.  Name resemblance is never enough.

**It has to be the coin that owns the anchor.**  When four launchpads each mint
something against the same hot ticker, exactly one of them is the trade.  Being
fourth is not a smaller version of being first — it is the thing the operator
keeps getting shown and keeps telling us to stop showing.

Pure logic: no provider, no database, no signer, no order path.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .anchors import HUMAN_ANCHOR as HUMAN_ANCHOR_CLAIM
from .anchors import AnchoredCoin, StockAnchor

ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")

# --- outcomes ----------------------------------------------------------------
#: Hot anchor, verified claim, and this coin owns it.  The one that interrupts.
STOCK_RUNNER = "STOCK_RUNNER"
#: Everything holds except that this coin is not the leader on its anchor.
NOT_THE_LEADER = "NOT_THE_LEADER"
#: The coin is fine; the stock is doing nothing.  No catalyst, no alert.
ANCHOR_QUIET = "ANCHOR_QUIET"
#: The coin only *claims* the stock.  A name is not a link.
CLAIM_UNVERIFIED = "CLAIM_UNVERIFIED"
#: The anchor is hot and no coin on it is worth buying.  Worth saying out loud:
#: it is the one state where the operator may want to act before the bot can.
ANCHOR_HOT_NO_COIN = "ANCHOR_HOT_NO_COIN"

PINGABLE: frozenset[str] = frozenset({STOCK_RUNNER})

HUMAN_OUTCOME: dict[str, str] = {
    STOCK_RUNNER: "the leading coin on a stock that is moving right now",
    NOT_THE_LEADER: "another coin already owns this anchor",
    ANCHOR_QUIET: "the stock behind this coin is not moving",
    CLAIM_UNVERIFIED: "nothing links this coin to that stock except its name",
    ANCHOR_HOT_NO_COIN: "the stock is moving and no coin on it is worth buying yet",
}


@dataclass(frozen=True, slots=True)
class AnchorConfig:
    """When a stock counts as hot, and when a coin counts as owning it."""

    #: Session move, either direction.  Down is a catalyst too — the coins that
    #: launch against a crashing stock are some of the fastest movers there are,
    #: and refusing to look at them because the number is negative would be
    #: reading the sign instead of the size.
    move_percent_floor: Decimal = Decimal("3")
    move_percent_target: Decimal = Decimal("12")
    #: Volume against the instrument's own recent average.
    relative_volume_floor: Decimal = Decimal("1.5")
    relative_volume_target: Decimal = Decimal("5")
    #: Independent outlets carrying the story.  Duplicates of one wire report
    #: are one source, which the caller is responsible for collapsing.
    news_floor: int = 1
    news_target: int = 4
    #: A hot anchor needs this much of the above before it is a catalyst.
    hot_score: Decimal = Decimal("45")

    #: The leader must hold this multiple of the runner-up's liquidity before
    #: "top coin on this stock" means anything.  Below it, several coins are
    #: simply sharing the anchor and none of them owns it.
    leader_multiple: Decimal = Decimal("1.4")
    #: Absolute floors, so the "leader" of four dead coins is not promoted.
    min_liquidity_usd: Decimal = Decimal("6000")
    min_holders: int = 25
    #: Same refusal as the memecoin lane: more sellers than buyers by this much
    #: is an exit, whatever the anchor is doing.
    max_sell_pressure: Decimal = Decimal("3")


DEFAULT_ANCHOR_CONFIG = AnchorConfig()


def ramp(value: Decimal | None, floor: Decimal, target: Decimal) -> Decimal:
    """0 at or below ``floor``, 1 at or above ``target``, linear between."""

    if value is None or target <= floor:
        return ZERO
    if value <= floor:
        return ZERO
    if value >= target:
        return ONE
    return ((value - floor) / (target - floor)).quantize(Decimal("0.0001"))


@dataclass(frozen=True, slots=True)
class AnchorHeat:
    """How much attention the real instrument is getting."""

    ticker: str
    score: Decimal = ZERO
    measured: bool = False
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def hot(self, *, config: AnchorConfig = DEFAULT_ANCHOR_CONFIG) -> bool:
        return self.measured and self.score >= config.hot_score

    def to_json(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "score": str(self.score),
            "measured": self.measured,
            "hot": self.hot(),
            "reasons": list(self.reasons),
        }


def score_anchor(
    anchor: StockAnchor,
    *,
    config: AnchorConfig = DEFAULT_ANCHOR_CONFIG,
) -> AnchorHeat:
    """Is this stock actually doing something right now?

    Movement is scored on its absolute size.  A stock down 9% on four times its
    usual volume is exactly as much of a catalyst as one up 9%, and the coins
    that launch against a crash are some of the fastest movers in this market.
    """

    move = ramp(anchor.absolute_move, config.move_percent_floor, config.move_percent_target)
    volume = ramp(
        anchor.relative_volume, config.relative_volume_floor, config.relative_volume_target
    )
    news = ramp(
        None if anchor.news_sources is None else Decimal(anchor.news_sources),
        Decimal(config.news_floor),
        Decimal(config.news_target),
    )
    measured = any(
        value is not None
        for value in (anchor.absolute_move, anchor.relative_volume, anchor.news_sources)
    )
    score = (move * Decimal("45") + volume * Decimal("35") + news * Decimal("20")).quantize(CENT)

    reasons: list[str] = []
    moved = anchor.absolute_move
    if moved is not None and moved >= config.move_percent_floor:
        direction = "up" if (anchor.change_percent or ZERO) > ZERO else "down"
        reasons.append(f"{anchor.ticker} {direction} {abs(anchor.change_percent)}% this session")
    if (
        anchor.relative_volume is not None
        and anchor.relative_volume >= config.relative_volume_floor
    ):
        reasons.append(f"{anchor.relative_volume}x its usual volume")
    if anchor.news_sources:
        reasons.append(f"{anchor.news_sources} independent outlets carrying it")
    if anchor.corporate_action:
        # Not a score component.  Prices either side of a split are not
        # comparable, so this is said rather than counted.
        reasons.append(f"corporate action in flight: {anchor.corporate_action}")

    return AnchorHeat(
        ticker=anchor.ticker, score=score, measured=measured, reasons=tuple(reasons)
    )


@dataclass(frozen=True, slots=True)
class AnchorVerdict:
    """What to do about one coin on one stock."""

    mint: str
    outcome: str = ANCHOR_QUIET
    anchor_ticker: str = ""
    heat: AnchorHeat | None = None
    rivals: tuple[str, ...] = ()
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def may_ping(self) -> bool:
        return self.outcome in PINGABLE

    def human(self) -> str:
        return HUMAN_OUTCOME.get(self.outcome, self.outcome)

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "outcome": self.outcome,
            "human": self.human(),
            "anchor_ticker": self.anchor_ticker,
            "heat": None if self.heat is None else self.heat.to_json(),
            "rivals": list(self.rivals),
            "reasons": list(self.reasons),
            "may_ping": self.may_ping,
        }


def _tradeable(coin: AnchoredCoin, *, config: AnchorConfig) -> bool:
    """Floors that stop the leader of four dead coins being promoted."""

    if coin.liquidity_usd is not None and coin.liquidity_usd < config.min_liquidity_usd:
        return False
    if coin.holder_count is not None and coin.holder_count < config.min_holders:
        return False
    pressure = coin.sell_pressure
    return not (pressure is not None and pressure >= config.max_sell_pressure)


def evaluate_anchor(
    coin: AnchoredCoin,
    anchor: StockAnchor,
    rivals: Sequence[AnchoredCoin] = (),
    *,
    config: AnchorConfig = DEFAULT_ANCHOR_CONFIG,
) -> AnchorVerdict:
    """The whole decision, in the order the reasons actually rule things out.

    Claim first, because an unverified claim makes every other question moot;
    then the anchor, because a real link to a stock nobody is trading is still
    not a catalyst; then leadership, because that is the difference between the
    trade and the noise that surrounds it.
    """

    heat = score_anchor(anchor, config=config)

    if not coin.verified_anchor:
        return AnchorVerdict(
            mint=coin.mint,
            outcome=CLAIM_UNVERIFIED,
            anchor_ticker=anchor.ticker,
            heat=heat,
            reasons=(
                f"nothing links this coin to {anchor.ticker} except its name",
                "a ticker in a coin's name is a claim, not a link",
            ),
        )

    if not heat.hot(config=config):
        return AnchorVerdict(
            mint=coin.mint,
            outcome=ANCHOR_QUIET,
            anchor_ticker=anchor.ticker,
            heat=heat,
            reasons=(f"{anchor.ticker} is not moving enough to be a catalyst",),
        )

    # Only coins with a real claim on the same anchor are rivals.  A coin that
    # merely named itself after the ticker cannot take the anchor away from one
    # that was actually minted against it.
    peers = [
        item
        for item in rivals
        if item.mint != coin.mint
        and item.verified_anchor
        and item.anchor_key == coin.anchor_key
    ]
    best_rival = max(
        (item.liquidity_usd for item in peers if item.liquidity_usd is not None),
        default=None,
    )
    leads = (
        coin.liquidity_usd is not None
        and (
            best_rival is None
            or best_rival <= ZERO
            or coin.liquidity_usd >= best_rival * config.leader_multiple
        )
    )

    if not leads:
        return AnchorVerdict(
            mint=coin.mint,
            outcome=NOT_THE_LEADER,
            anchor_ticker=anchor.ticker,
            heat=heat,
            rivals=tuple(sorted(item.mint for item in peers)),
            reasons=(
                f"{len(peers)} other coin(s) hold this anchor and one is deeper",
                "being fourth on a hot ticker is the noise, not the trade",
            ),
        )

    if not _tradeable(coin, config=config):
        return AnchorVerdict(
            mint=coin.mint,
            outcome=ANCHOR_HOT_NO_COIN,
            anchor_ticker=anchor.ticker,
            heat=heat,
            rivals=tuple(sorted(item.mint for item in peers)),
            reasons=(
                f"{anchor.ticker} is moving and the leading coin on it is not tradeable",
                "said out loud because this is the one case worth acting on "
                "before the bot can",
            ),
        )

    return AnchorVerdict(
        mint=coin.mint,
        outcome=STOCK_RUNNER,
        anchor_ticker=anchor.ticker,
        heat=heat,
        rivals=tuple(sorted(item.mint for item in peers)),
        reasons=(
            *heat.reasons,
            f"top coin on {anchor.ticker} by liquidity",
            HUMAN_ANCHOR_CLAIM.get(coin.anchor_claim, coin.anchor_claim),
        ),
    )
