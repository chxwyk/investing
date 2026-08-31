"""Who is actually buying: independence, fresh wallets, and coordination.

Section 13 states the problem exactly: 1000 buys from 4 bots must not score like
300 buys from 250 independent participants.  Raw trade counts measure *activity*;
what a trader needs to know is how many distinct, unrelated people are choosing
to own the token.

Terminal's documented Trenches view tracks fresh-wallet buys — buys from wallets
funded very recently — and this module builds an independent public/on-chain
equivalent (section 14).  The important discipline is that a fresh wallet is
**not inherently bullish**.  It can be a genuinely new trader, a bot, an insider
or a sybil, and the only way to tell is coordination: wallets funded around the
same time, from the same source, buying in the same window are one actor wearing
many hats (section 15), and they are discounted to a single unit of demand.

Nothing here deanonymises anyone.  A wallet is described by observable funding
and timing relationships only, and a cluster is a statement about transaction
graph structure, never about a person.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal("0")
HUNDRED = Decimal("100")

# --- wallet age classes (section 14) -----------------------------------------
WALLET_VERY_NEW = "VERY_NEW"
WALLET_RECENTLY_FUNDED = "RECENTLY_FUNDED"
WALLET_ESTABLISHED = "ESTABLISHED"
WALLET_UNKNOWN = "UNKNOWN"

WALLET_CLASSES: tuple[str, ...] = (
    WALLET_VERY_NEW,
    WALLET_RECENTLY_FUNDED,
    WALLET_ESTABLISHED,
    WALLET_UNKNOWN,
)

#: Wallets younger than this at the time of their buy count as very new.
VERY_NEW_SECONDS = 2 * 3600
#: Terminal's documented fresh-wallet window is the last two hours; ours matches
#: that horizon but is computed from first observable signature, not a vendor.
RECENTLY_FUNDED_SECONDS = 24 * 3600


def classify_wallet_age(
    *,
    first_activity_at: int | None,
    at: int,
    signature_count: int | None = None,
) -> str:
    """Classify a wallet from observable history alone.

    ``None`` first-activity means we could not read its history, which is
    ``UNKNOWN`` — never quietly ``ESTABLISHED``.
    """

    if first_activity_at is None:
        return WALLET_UNKNOWN
    age = max(0, at - first_activity_at)
    if age <= VERY_NEW_SECONDS:
        return WALLET_VERY_NEW
    if age <= RECENTLY_FUNDED_SECONDS or (
        signature_count is not None and signature_count <= 5
    ):
        return WALLET_RECENTLY_FUNDED
    return WALLET_ESTABLISHED


@dataclass(frozen=True, slots=True)
class BuyerRecord:
    """One observed buy, with whatever public history we could attach to it."""

    wallet: str
    at: int
    amount_usd: Decimal = ZERO
    #: Earliest signature we could observe for this wallet.
    first_activity_at: int | None = None
    signature_count: int | None = None
    #: The wallet that funded this one, when a funding edge was observable.
    funded_by: str = ""
    funded_at: int | None = None
    #: Slot the buy landed in, for same-slot bundle analysis.
    slot: int | None = None

    def age_class(self) -> str:
        return classify_wallet_age(
            first_activity_at=self.first_activity_at,
            at=self.at,
            signature_count=self.signature_count,
        )


@dataclass(frozen=True, slots=True)
class WalletCluster:
    """A set of wallets that observable evidence says act together.

    ``kind`` names *why* they were grouped, so a card can state the reason rather
    than asserting an unexplained relationship.
    """

    cluster_id: str
    kind: str
    wallets: tuple[str, ...]
    #: Combined buy value attributed to the cluster.
    amount_usd: Decimal = ZERO

    @property
    def size(self) -> int:
        return len(self.wallets)


CLUSTER_SHARED_FUNDER = "SHARED_FUNDER"
CLUSTER_FUNDING_BURST = "FUNDED_IN_ONE_WINDOW"
CLUSTER_SAME_SLOT = "SAME_SLOT_BUYS"


@dataclass(frozen=True, slots=True)
class ParticipantProfile:
    """The honest picture of demand behind a token."""

    mint: str
    buys: int = 0
    sells: int = 0
    unique_buyers: int = 0
    unique_sellers: int = 0
    repeat_buyers: int = 0
    #: Buyers after collapsing every detected cluster to a single actor.
    independent_buyers: int = 0
    clusters: tuple[WalletCluster, ...] = ()
    clustered_wallets: int = 0
    clustered_amount_usd: Decimal = ZERO
    total_amount_usd: Decimal = ZERO
    fresh_wallet_buyers: int = 0
    fresh_wallet_amount_usd: Decimal = ZERO
    independent_fresh_buyers: int = 0
    established_buyers: int = 0
    unknown_history_buyers: int = 0
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def independence_ratio(self) -> Decimal | None:
        """Independent actors per unique buying wallet.  1.0 is fully organic."""

        if self.unique_buyers <= 0:
            return None
        return (Decimal(self.independent_buyers) / Decimal(self.unique_buyers)).quantize(
            Decimal("0.01")
        )

    @property
    def buys_per_independent_buyer(self) -> Decimal | None:
        """High values mean few actors trading a lot — activity, not demand."""

        if self.independent_buyers <= 0:
            return None
        return (Decimal(self.buys) / Decimal(self.independent_buyers)).quantize(
            Decimal("0.01")
        )

    @property
    def fresh_wallet_percent(self) -> Decimal | None:
        if self.total_amount_usd <= ZERO:
            return None
        return (self.fresh_wallet_amount_usd / self.total_amount_usd * HUNDRED).quantize(
            Decimal("0.1")
        )

    @property
    def clustered_percent(self) -> Decimal | None:
        if self.total_amount_usd <= ZERO:
            return None
        return (self.clustered_amount_usd / self.total_amount_usd * HUNDRED).quantize(
            Decimal("0.1")
        )

    @property
    def organic(self) -> bool:
        """Genuinely broad demand: many independent actors, little coordination."""

        ratio = self.independence_ratio
        clustered = self.clustered_percent
        return (
            self.independent_buyers >= 15
            and ratio is not None
            and ratio >= Decimal("0.6")
            and (clustered is None or clustered < Decimal("40"))
        )

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "buys": self.buys,
            "sells": self.sells,
            "unique_buyers": self.unique_buyers,
            "unique_sellers": self.unique_sellers,
            "repeat_buyers": self.repeat_buyers,
            "independent_buyers": self.independent_buyers,
            "clusters": [
                {
                    "cluster_id": item.cluster_id,
                    "kind": item.kind,
                    "size": item.size,
                    "amount_usd": str(item.amount_usd),
                }
                for item in self.clusters
            ],
            "clustered_wallets": self.clustered_wallets,
            "clustered_amount_usd": str(self.clustered_amount_usd),
            "total_amount_usd": str(self.total_amount_usd),
            "fresh_wallet_buyers": self.fresh_wallet_buyers,
            "fresh_wallet_amount_usd": str(self.fresh_wallet_amount_usd),
            "independent_fresh_buyers": self.independent_fresh_buyers,
            "established_buyers": self.established_buyers,
            "unknown_history_buyers": self.unknown_history_buyers,
            "independence_ratio": _s(self.independence_ratio),
            "buys_per_independent_buyer": _s(self.buys_per_independent_buyer),
            "fresh_wallet_percent": _s(self.fresh_wallet_percent),
            "clustered_percent": _s(self.clustered_percent),
            "organic": self.organic,
            "reasons": list(self.reasons),
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True, slots=True)
class ClusterConfig:
    """How coordinated wallets have to be before we stop counting them twice."""

    #: Wallets funded within this window of each other, from the same source.
    funding_window_seconds: int = 900
    #: Buys landing within this window of each other.
    buy_window_seconds: int = 30
    #: Minimum members before a group counts as a cluster at all.
    min_cluster_size: int = 3


DEFAULT_CLUSTER_CONFIG = ClusterConfig()


def detect_clusters(
    buyers: Sequence[BuyerRecord],
    *,
    config: ClusterConfig = DEFAULT_CLUSTER_CONFIG,
) -> tuple[WalletCluster, ...]:
    """Group wallets that observable evidence says are acting together (§15).

    Three independent signals, each stated as its own cluster kind:

    * a **shared funder** — several buyers funded by the same wallet;
    * a **funding burst** — several buyers first funded inside one short window
      by the same source;
    * **same-slot buys** — several buyers landing in the identical slot, which
      does not happen by coincidence among strangers.
    """

    clusters: list[WalletCluster] = []

    by_funder: dict[str, list[BuyerRecord]] = {}
    for record in buyers:
        if record.funded_by:
            by_funder.setdefault(record.funded_by, []).append(record)
    for funder, group in by_funder.items():
        wallets = sorted({item.wallet for item in group})
        if len(wallets) < config.min_cluster_size:
            continue
        funded_times = [item.funded_at for item in group if item.funded_at is not None]
        burst = (
            len(funded_times) >= config.min_cluster_size
            and max(funded_times) - min(funded_times) <= config.funding_window_seconds
        )
        clusters.append(
            WalletCluster(
                cluster_id=f"funder:{funder}",
                kind=CLUSTER_FUNDING_BURST if burst else CLUSTER_SHARED_FUNDER,
                wallets=tuple(wallets),
                amount_usd=sum((item.amount_usd for item in group), ZERO),
            )
        )

    by_slot: dict[int, list[BuyerRecord]] = {}
    for record in buyers:
        if record.slot is not None:
            by_slot.setdefault(record.slot, []).append(record)
    for slot, group in by_slot.items():
        wallets = sorted({item.wallet for item in group})
        if len(wallets) < config.min_cluster_size:
            continue
        clusters.append(
            WalletCluster(
                cluster_id=f"slot:{slot}",
                kind=CLUSTER_SAME_SLOT,
                wallets=tuple(wallets),
                amount_usd=sum((item.amount_usd for item in group), ZERO),
            )
        )

    return tuple(clusters)


def assess_participants(
    mint: str,
    buyers: Sequence[BuyerRecord],
    *,
    sellers: Sequence[BuyerRecord] = (),
    buys: int | None = None,
    sells: int | None = None,
    config: ClusterConfig = DEFAULT_CLUSTER_CONFIG,
) -> ParticipantProfile:
    """Collapse coordinated wallets, then report what independent demand remains.

    Every wallet in a detected cluster collapses to **one** independent actor, so
    twenty sybils funded from one source contribute exactly as much independent
    demand as one wallet does.
    """

    reasons: list[str] = []
    if not buyers:
        return ParticipantProfile(
            mint=mint,
            buys=buys or 0,
            sells=sells or 0,
            reasons=("no buyer detail available — independence is unknown",),
        )

    clusters = detect_clusters(buyers, config=config)
    clustered: dict[str, str] = {}
    for cluster in clusters:
        for wallet in cluster.wallets:
            # A wallet in several clusters belongs to the first that claimed it;
            # it must never be collapsed twice.
            clustered.setdefault(wallet, cluster.cluster_id)

    unique_buyers = {record.wallet for record in buyers}
    buy_counts: dict[str, int] = {}
    for record in buyers:
        buy_counts[record.wallet] = buy_counts.get(record.wallet, 0) + 1

    # One actor per cluster, plus every unclustered wallet.
    actors: set[str] = {clustered[wallet] for wallet in unique_buyers if wallet in clustered}
    actors |= {wallet for wallet in unique_buyers if wallet not in clustered}

    fresh = [
        record
        for record in buyers
        if record.age_class() in {WALLET_VERY_NEW, WALLET_RECENTLY_FUNDED}
    ]
    fresh_wallets = {record.wallet for record in fresh}
    independent_fresh = {wallet for wallet in fresh_wallets if wallet not in clustered}

    total = sum((record.amount_usd for record in buyers), ZERO)
    clustered_amount = sum(
        (record.amount_usd for record in buyers if record.wallet in clustered), ZERO
    )

    if clusters:
        reasons.append(
            f"{len(clusters)} coordinated group(s) collapsed to one actor each"
        )
    if fresh_wallets and len(independent_fresh) < len(fresh_wallets):
        reasons.append(
            f"{len(fresh_wallets) - len(independent_fresh)} fresh wallet(s) are clustered — "
            "not independent new demand"
        )
    if not reasons:
        reasons.append("no coordination detected in the observed buys")

    return ParticipantProfile(
        mint=mint,
        buys=buys if buys is not None else len(buyers),
        sells=sells if sells is not None else len(sellers),
        unique_buyers=len(unique_buyers),
        unique_sellers=len({record.wallet for record in sellers}),
        repeat_buyers=sum(1 for count in buy_counts.values() if count > 1),
        independent_buyers=len(actors),
        clusters=clusters,
        clustered_wallets=len(clustered),
        clustered_amount_usd=clustered_amount,
        total_amount_usd=total,
        fresh_wallet_buyers=len(fresh_wallets),
        fresh_wallet_amount_usd=sum((record.amount_usd for record in fresh), ZERO),
        independent_fresh_buyers=len(independent_fresh),
        established_buyers=len(
            {
                record.wallet
                for record in buyers
                if record.age_class() == WALLET_ESTABLISHED
            }
        ),
        unknown_history_buyers=len(
            {record.wallet for record in buyers if record.age_class() == WALLET_UNKNOWN}
        ),
        reasons=tuple(reasons),
    )


# --- large buys, judged relative to the pool (section 44) --------------------
@dataclass(frozen=True, slots=True)
class LargeBuyAssessment:
    """A buy is large relative to the token, never in absolute dollars."""

    wallet: str
    amount_usd: Decimal
    liquidity_share_percent: Decimal | None = None
    market_cap_share_percent: Decimal | None = None
    versus_average_trade: Decimal | None = None
    significant: bool = False
    #: Set once we have looked at what happened after it.
    followed_by_independent_demand: bool | None = None

    @property
    def confirmed_demand(self) -> bool:
        """A large buy only counts as demand once others follow it."""

        return self.significant and self.followed_by_independent_demand is True

    def to_json(self) -> dict[str, object]:
        return {
            "wallet": self.wallet,
            "amount_usd": str(self.amount_usd),
            "liquidity_share_percent": _s(self.liquidity_share_percent),
            "market_cap_share_percent": _s(self.market_cap_share_percent),
            "versus_average_trade": _s(self.versus_average_trade),
            "significant": self.significant,
            "followed_by_independent_demand": self.followed_by_independent_demand,
            "confirmed_demand": self.confirmed_demand,
        }


def assess_large_buy(
    *,
    wallet: str,
    amount_usd: Decimal,
    liquidity_usd: Decimal | None,
    market_cap_usd: Decimal | None,
    average_trade_usd: Decimal | None,
    followed_by_independent_demand: bool | None = None,
    liquidity_share_threshold: Decimal = Decimal("2"),
    average_multiple_threshold: Decimal = Decimal("8"),
) -> LargeBuyAssessment:
    """Size a buy against the pool it landed in (section 44)."""

    liquidity_share = (
        (amount_usd / liquidity_usd * HUNDRED).quantize(Decimal("0.01"))
        if liquidity_usd and liquidity_usd > ZERO
        else None
    )
    market_share = (
        (amount_usd / market_cap_usd * HUNDRED).quantize(Decimal("0.01"))
        if market_cap_usd and market_cap_usd > ZERO
        else None
    )
    multiple = (
        (amount_usd / average_trade_usd).quantize(Decimal("0.01"))
        if average_trade_usd and average_trade_usd > ZERO
        else None
    )
    significant = bool(
        (liquidity_share is not None and liquidity_share >= liquidity_share_threshold)
        or (multiple is not None and multiple >= average_multiple_threshold)
    )
    return LargeBuyAssessment(
        wallet=wallet,
        amount_usd=amount_usd,
        liquidity_share_percent=liquidity_share,
        market_cap_share_percent=market_share,
        versus_average_trade=multiple,
        significant=significant,
        followed_by_independent_demand=followed_by_independent_demand,
    )
