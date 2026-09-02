"""Is the volume real, or is it the same money going in circles?

Raw volume and transaction counts are the two easiest numbers in this market to
manufacture, and both of them look identical whether they came from a thousand
people or from one person with a thousand wallets.  The operator has been shown
plenty of the second kind.

The unit that matters is therefore not the wallet.  It is the **economic
actor**: wallets sharing a funder, wallets trading only with each other, wallets
that appeared minutes before the launch and have done nothing since.  Twenty
wallets on one funder are one person, and counting them as twenty independent
buyers is exactly how a wash-traded token reads as broad demand.

What this module looks for, all of it from decoded pool activity:

* **Circularity.**  Wallets that both bought and sold repeatedly inside a short
  window are moving inventory, not expressing an opinion.
* **Uniformity.**  Real buyers pick odd amounts.  A cluster of identical trade
  sizes is a script.
* **Concentration.**  One actor responsible for most of the volume means the
  volume ends when they stop.
* **Direction.**  Whether new participants are still arriving or the same ones
  are recycling, and whether the flow is improving or deteriorating across
  short windows rather than over a flattering 24-hour total.
* **Distribution.**  Creator, insider and sniper wallets selling into the move,
  which is the difference between a rally and an exit.

Medians and trimmed means rather than averages throughout, because one $40,000
print drags an average anywhere its author wants it.

Pure logic: no provider, no database, no signer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .hardgates import (
    CONCENTRATION_OK,
    FAIL,
    ORGANIC_FLOW_OK,
    PASS,
    UNKNOWN,
    GateResult,
)

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
CENT = Decimal("0.01")

BUY = "BUY"
SELL = "SELL"

# --- findings -----------------------------------------------------------------
WASH_CIRCULAR = "CIRCULAR_TRADING"
WASH_UNIFORM = "UNIFORM_TRADE_SIZES"
WASH_CLUSTERED = "VOLUME_FROM_ONE_FUNDER_CLUSTER"
WASH_SELF_TRADE = "SELF_TRADING"
FLOW_CONCENTRATED = "ONE_ACTOR_DOMINATES_VOLUME"
FLOW_NO_NEW_BUYERS = "NO_NEW_PARTICIPANTS"
FLOW_DISTRIBUTION = "DISTRIBUTION"
FLOW_INSUFFICIENT = "NOT_ENOUGH_DECODED_ACTIVITY"
CONCENTRATION_TOP_HEAVY = "HOLDINGS_TOO_CONCENTRATED"
CONCENTRATION_INSIDER = "INSIDER_AND_SNIPER_SUPPLY"

HUMAN_FINDING: dict[str, str] = {
    WASH_CIRCULAR: "wallets buying and selling in loops rather than taking a position",
    WASH_UNIFORM: "trade sizes are too uniform to be people choosing amounts",
    WASH_CLUSTERED: "most of the volume comes from wallets sharing one funder",
    WASH_SELF_TRADE: "wallets trading with themselves",
    FLOW_CONCENTRATED: "one actor is most of the volume — it ends when they stop",
    FLOW_NO_NEW_BUYERS: "no new participants; the same wallets are recycling",
    FLOW_DISTRIBUTION: "creator, insider or sniper wallets are selling into the move",
    FLOW_INSUFFICIENT: "not enough decoded activity to judge",
    CONCENTRATION_TOP_HEAVY: "a few holders can end this market",
    CONCENTRATION_INSIDER: "insiders and snipers hold too much of the supply",
}


@dataclass(frozen=True, slots=True)
class Trade:
    """One decoded swap against the canonical pool."""

    wallet: str
    direction: str
    amount_usd: Decimal
    at: int
    signature: str = ""
    #: Funding-graph cluster.  Empty means the wallet stands alone as far as we
    #: could establish, which is not the same as proven independent.
    cluster_id: str = ""
    is_creator: bool = False
    is_insider: bool = False
    is_sniper: bool = False
    #: Whether this wallet held the token before this window opened.
    returning: bool = False

    @property
    def actor(self) -> str:
        """The economic actor behind this trade, which is rarely the wallet."""

        return self.cluster_id or self.wallet


@dataclass(frozen=True, slots=True)
class FlowConfig:
    """Where organic stops and manufactured begins."""

    #: Below this there is nothing to judge and the answer is UNKNOWN.
    min_trades: int = 12
    min_actors: int = 6
    #: One actor above this share of volume dominates it.
    max_actor_volume_share: Decimal = Decimal("0.45")
    #: One cluster above this share means the volume is the cluster's.
    max_cluster_volume_share: Decimal = Decimal("0.5")
    #: Actors that both bought and sold, as a share of all actors.
    max_circular_actor_share: Decimal = Decimal("0.35")
    #: Identical trade sizes as a share of all trades.
    max_uniform_share: Decimal = Decimal("0.4")
    #: At least this share of buyers must be new to the token.
    min_new_buyer_share: Decimal = Decimal("0.3")
    #: Sell volume over buy volume above this is distribution, not a dip.
    distribution_ratio: Decimal = Decimal("1.6")
    #: Creator/insider/sniper sell volume as a share of all sells.
    max_insider_sell_share: Decimal = Decimal("0.25")
    #: Holder concentration limits.
    max_top10_rate: Decimal = Decimal("0.5")
    max_insider_supply_rate: Decimal = Decimal("0.3")


DEFAULT_FLOW_CONFIG = FlowConfig()


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return ((ordered[mid - 1] + ordered[mid]) / Decimal(2)).quantize(CENT)


def _trimmed_mean(values: Sequence[Decimal], *, trim: Decimal = Decimal("0.1")) -> Decimal | None:
    """Mean with the tails removed, so one enormous print cannot set it."""

    if not values:
        return None
    ordered = sorted(values)
    cut = int(len(ordered) * float(trim))
    kept = ordered[cut : len(ordered) - cut] or ordered
    return (sum(kept, ZERO) / Decimal(len(kept))).quantize(CENT)


@dataclass(frozen=True, slots=True)
class FlowReport:
    """The decoded picture, in the units that survive wallet-splitting."""

    mint: str
    trades: int = 0
    buys: int = 0
    sells: int = 0
    buy_volume_usd: Decimal = ZERO
    sell_volume_usd: Decimal = ZERO
    unique_wallets: int = 0
    #: Wallets collapsed into funding clusters.  This is the honest count.
    unique_actors: int = 0
    unique_buyers: int = 0
    unique_sellers: int = 0
    new_buyers: int = 0
    returning_buyers: int = 0
    median_trade_usd: Decimal | None = None
    trimmed_mean_trade_usd: Decimal | None = None
    top_actor_volume_share: Decimal | None = None
    largest_cluster_share: Decimal | None = None
    circular_actor_share: Decimal | None = None
    uniform_size_share: Decimal | None = None
    insider_sell_share: Decimal | None = None
    findings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def sell_to_buy_ratio(self) -> Decimal | None:
        if self.buy_volume_usd <= ZERO:
            return None
        return (self.sell_volume_usd / self.buy_volume_usd).quantize(CENT)

    @property
    def wallets_per_actor(self) -> Decimal | None:
        """How much wallet-splitting is going on.  1.0 is a normal market."""

        if self.unique_actors <= 0:
            return None
        return (Decimal(self.unique_wallets) / Decimal(self.unique_actors)).quantize(CENT)

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "trades": self.trades,
            "buys": self.buys,
            "sells": self.sells,
            "buy_volume_usd": str(self.buy_volume_usd),
            "sell_volume_usd": str(self.sell_volume_usd),
            "unique_wallets": self.unique_wallets,
            "unique_actors": self.unique_actors,
            "unique_buyers": self.unique_buyers,
            "unique_sellers": self.unique_sellers,
            "new_buyers": self.new_buyers,
            "returning_buyers": self.returning_buyers,
            "median_trade_usd": _s(self.median_trade_usd),
            "trimmed_mean_trade_usd": _s(self.trimmed_mean_trade_usd),
            "top_actor_volume_share": _s(self.top_actor_volume_share),
            "largest_cluster_share": _s(self.largest_cluster_share),
            "circular_actor_share": _s(self.circular_actor_share),
            "uniform_size_share": _s(self.uniform_size_share),
            "insider_sell_share": _s(self.insider_sell_share),
            "sell_to_buy_ratio": _s(self.sell_to_buy_ratio),
            "wallets_per_actor": _s(self.wallets_per_actor),
            "findings": [HUMAN_FINDING.get(item, item) for item in self.findings],
            "finding_codes": list(self.findings),
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def analyse_flow(
    mint: str,
    trades: Sequence[Trade],
    *,
    config: FlowConfig = DEFAULT_FLOW_CONFIG,
) -> FlowReport:
    """Decode the activity into actors, shares and findings.  No verdict yet."""

    if not trades:
        return FlowReport(mint=mint, findings=(FLOW_INSUFFICIENT,))

    buys = [t for t in trades if t.direction == BUY]
    sells = [t for t in trades if t.direction == SELL]
    buy_volume = sum((t.amount_usd for t in buys), ZERO)
    sell_volume = sum((t.amount_usd for t in sells), ZERO)
    total_volume = buy_volume + sell_volume

    by_actor: dict[str, Decimal] = {}
    actor_directions: dict[str, set[str]] = {}
    for trade in trades:
        by_actor[trade.actor] = by_actor.get(trade.actor, ZERO) + trade.amount_usd
        actor_directions.setdefault(trade.actor, set()).add(trade.direction)

    clusters: dict[str, Decimal] = {}
    for trade in trades:
        if trade.cluster_id:
            clusters[trade.cluster_id] = clusters.get(trade.cluster_id, ZERO) + trade.amount_usd

    sizes = [t.amount_usd for t in trades]
    counts: dict[Decimal, int] = {}
    for size in sizes:
        counts[size] = counts.get(size, 0) + 1
    repeated = sum(count for count in counts.values() if count > 1)

    circular = [a for a, dirs in actor_directions.items() if len(dirs) > 1]
    insider_sells = sum(
        (t.amount_usd for t in sells if t.is_creator or t.is_insider or t.is_sniper), ZERO
    )
    buyer_actors = {t.actor for t in buys}
    new_buyers = {t.actor for t in buys if not t.returning}

    def share(part: Decimal, whole: Decimal) -> Decimal | None:
        return None if whole <= ZERO else (part / whole).quantize(Decimal("0.0001"))

    findings: list[str] = []
    top_share = share(max(by_actor.values(), default=ZERO), total_volume)
    cluster_share = share(max(clusters.values(), default=ZERO), total_volume)
    circular_share = (
        (Decimal(len(circular)) / Decimal(len(by_actor))).quantize(Decimal("0.0001"))
        if by_actor
        else None
    )
    uniform_share = (
        (Decimal(repeated) / Decimal(len(sizes))).quantize(Decimal("0.0001"))
        if sizes
        else None
    )
    insider_share = share(insider_sells, sell_volume)

    # Two different shortfalls that must not be confused.
    #
    # Few *trades* means we could not see enough to judge — honestly UNKNOWN.
    #
    # Plenty of trades collapsing into few *actors* is the opposite: it is a
    # finding. Forty trades from one funding cluster is demonstrated wash
    # trading, and reporting that as "not enough data" would hand the exact
    # case this module exists for the benefit of the doubt.
    if len(trades) < config.min_trades:
        findings.append(FLOW_INSUFFICIENT)
    elif len(by_actor) < config.min_actors:
        findings.append(FLOW_CONCENTRATED)
    if top_share is not None and top_share > config.max_actor_volume_share:
        findings.append(FLOW_CONCENTRATED)
    if cluster_share is not None and cluster_share > config.max_cluster_volume_share:
        findings.append(WASH_CLUSTERED)
    if circular_share is not None and circular_share > config.max_circular_actor_share:
        findings.append(WASH_CIRCULAR)
    if uniform_share is not None and uniform_share > config.max_uniform_share:
        findings.append(WASH_UNIFORM)
    if buyer_actors:
        new_share = (Decimal(len(new_buyers)) / Decimal(len(buyer_actors))).quantize(CENT)
        if new_share < config.min_new_buyer_share:
            findings.append(FLOW_NO_NEW_BUYERS)
    ratio = None if buy_volume <= ZERO else sell_volume / buy_volume
    if ratio is not None and ratio > config.distribution_ratio:
        findings.append(FLOW_DISTRIBUTION)
    if insider_share is not None and insider_share > config.max_insider_sell_share:
        findings.append(FLOW_DISTRIBUTION)

    return FlowReport(
        mint=mint,
        trades=len(trades),
        buys=len(buys),
        sells=len(sells),
        buy_volume_usd=buy_volume,
        sell_volume_usd=sell_volume,
        unique_wallets=len({t.wallet for t in trades}),
        unique_actors=len(by_actor),
        unique_buyers=len(buyer_actors),
        unique_sellers=len({t.actor for t in sells}),
        new_buyers=len(new_buyers),
        returning_buyers=len(buyer_actors) - len(new_buyers),
        median_trade_usd=_median(sizes),
        trimmed_mean_trade_usd=_trimmed_mean(sizes),
        top_actor_volume_share=top_share,
        largest_cluster_share=cluster_share,
        circular_actor_share=circular_share,
        uniform_size_share=uniform_share,
        insider_sell_share=insider_share,
        findings=tuple(dict.fromkeys(findings)),
    )


def prove_organic_flow(
    report: FlowReport,
    *,
    config: FlowConfig = DEFAULT_FLOW_CONFIG,
    now: int,
) -> GateResult:
    """Turn the decoded picture into the gate, with the numbers attached."""

    evidence = (
        ("trades", str(report.trades)),
        ("unique_wallets", str(report.unique_wallets)),
        ("unique_actors", str(report.unique_actors)),
        ("wallets_per_actor", str(report.wallets_per_actor or "")),
        ("top_actor_share", str(report.top_actor_volume_share or "")),
        ("circular_share", str(report.circular_actor_share or "")),
        ("uniform_share", str(report.uniform_size_share or "")),
        ("median_trade_usd", str(report.median_trade_usd or "")),
    )
    if FLOW_INSUFFICIENT in report.findings:
        return GateResult(
            gate=ORGANIC_FLOW_OK,
            answer=UNKNOWN,
            reason=(
                f"only {report.trades} decoded trades from {report.unique_actors} "
                "actors — not enough to tell real demand from a script"
            ),
            source="decoded pool activity",
            observed_at=now,
            evidence=evidence,
        )
    manufactured = [f for f in report.findings if f != FLOW_INSUFFICIENT]
    if manufactured:
        return GateResult(
            gate=ORGANIC_FLOW_OK,
            answer=FAIL,
            reason="; ".join(HUMAN_FINDING.get(item, item) for item in manufactured[:3]),
            source="decoded pool activity",
            observed_at=now,
            evidence=evidence + tuple(("finding", item) for item in manufactured),
        )
    return GateResult(
        gate=ORGANIC_FLOW_OK,
        answer=PASS,
        reason=(
            f"{report.trades} trades from {report.unique_actors} independent actors, "
            f"median ${report.median_trade_usd}, top actor "
            f"{report.top_actor_volume_share}"
        ),
        source="decoded pool activity",
        observed_at=now,
        max_age_seconds=600,
        evidence=evidence,
    )


def prove_concentration(
    mint: str,
    *,
    top10_rate: Decimal | None,
    insider_supply_rate: Decimal | None = None,
    observed_at: int | None = None,
    config: FlowConfig = DEFAULT_FLOW_CONFIG,
) -> GateResult:
    """Whether a handful of holders can end this market."""

    if top10_rate is None:
        return GateResult(
            gate=CONCENTRATION_OK,
            answer=UNKNOWN,
            reason="holder distribution was not read",
            observed_at=observed_at,
        )
    if top10_rate > config.max_top10_rate:
        return GateResult(
            gate=CONCENTRATION_OK,
            answer=FAIL,
            reason=(
                f"the top ten hold {(top10_rate * HUNDRED).quantize(CENT)}% — "
                "a few wallets can end this market"
            ),
            source="holder accounts",
            observed_at=observed_at,
            evidence=(("code", CONCENTRATION_TOP_HEAVY), ("top10", str(top10_rate))),
        )
    if (
        insider_supply_rate is not None
        and insider_supply_rate > config.max_insider_supply_rate
    ):
        return GateResult(
            gate=CONCENTRATION_OK,
            answer=FAIL,
            reason=(
                f"insiders and snipers hold "
                f"{(insider_supply_rate * HUNDRED).quantize(CENT)}% of supply"
            ),
            source="holder accounts",
            observed_at=observed_at,
            evidence=(("code", CONCENTRATION_INSIDER),),
        )
    return GateResult(
        gate=CONCENTRATION_OK,
        answer=PASS,
        reason=f"top ten hold {(top10_rate * HUNDRED).quantize(CENT)}%",
        source="holder accounts",
        observed_at=observed_at,
        max_age_seconds=900,
        evidence=(("top10", str(top10_rate)),),
    )
