"""SOL economic activity and economic-authenticity forensics (sections R, S, T).

The premise of this module is the one stated in the product contract: **high SOL
spend alone does not prove legitimacy**.  Bots pay real network fees.  So the
aggregation below deliberately reports *concentration* alongside *volume*, and
the authenticity score rewards independence rather than raw activity.

Nothing here deanonymizes a wallet or infers where a person is.  It only reads
publicly observable on-chain relationships that the runner forensics already
collected, and says how coordinated they look.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .config import DEFAULT_LAB_CONFIG, LabConfig
from .decision import EvidenceQuality

ZERO = Decimal("0")

BAND_AUTHENTIC = "AUTHENTIC"
BAND_MIXED = "MIXED"
BAND_SUSPECT = "SUSPECT"
BAND_MANUFACTURED = "MANUFACTURED"
BAND_UNKNOWN = "UNKNOWN"

# Manufactured-activity markers (section S).
MARK_FEW_WALLET_TX_SWARM = "HIGH_TX_FROM_FEW_WALLETS"
MARK_CLUSTER_DOMINATES = "ONE_CLUSTER_DOMINATES_ACTIVITY"
MARK_REPETITIVE_SIZING = "REPETITIVE_IDENTICAL_SIZING"
MARK_CYCLIC_CHURN = "CYCLIC_CHURN"
MARK_SHARED_FUNDING = "SHARED_FUNDING"
MARK_SYNCHRONIZED_FUNDING = "SYNCHRONIZED_FUNDING"
MARK_FRESH_WALLET_SWARM = "FRESH_WALLET_SWARM"
MARK_SYNCHRONIZED_BURSTS = "SYNCHRONIZED_TRANSACTION_BURSTS"
MARK_WASH_LIKE = "WASH_LIKE_BEHAVIOUR"
MARK_MANUFACTURED_HOLDERS = "MANUFACTURED_HOLDER_GROWTH"
MARK_FEE_CONCENTRATION = "FEE_CONCENTRATION"

REWARD_INDEPENDENT_FUNDING = "INDEPENDENTLY_FUNDED_BUYERS"
REWARD_DIVERSE_SIZING = "DIVERSE_TRADE_SIZING"
REWARD_DIVERSE_FEE_PAYERS = "DIVERSE_FEE_PAYERS"
REWARD_SUSTAINED_PARTICIPATION = "SUSTAINED_MULTI_WINDOW_PARTICIPATION"
REWARD_BUYER_RETENTION = "BUYER_RETENTION"
REWARD_BUYER_EXPANSION = "INDEPENDENT_BUYER_EXPANSION"


@dataclass(frozen=True, slots=True)
class WalletActivity:
    """Bounded, publicly observable trading activity for one wallet."""

    wallet: str
    transactions: int = 0
    buys: int = 0
    sells: int = 0
    volume_usd: Decimal = ZERO
    base_fee_sol: Decimal = ZERO
    priority_fee_sol: Decimal = ZERO
    first_seen_at: int = 0
    last_seen_at: int = 0
    cluster_id: str | None = None
    trade_sizes_usd: tuple[Decimal, ...] = ()

    @property
    def total_fee_sol(self) -> Decimal:
        return self.base_fee_sol + self.priority_fee_sol

    @property
    def active_seconds(self) -> int:
        return max(0, self.last_seen_at - self.first_seen_at)


@dataclass(frozen=True, slots=True)
class SolActivityProfile:
    """Aggregated SOL-denominated economic activity for one mint."""

    transactions: int = 0
    unique_fee_payers: int = 0
    unique_buyers: int = 0
    unique_sellers: int = 0
    base_fees_sol: Decimal = ZERO
    priority_fees_sol: Decimal = ZERO
    total_fees_sol: Decimal = ZERO
    buy_side_fees_sol: Decimal = ZERO
    sell_side_fees_sol: Decimal = ZERO
    volume_usd: Decimal = ZERO
    fee_per_independent_buyer_sol: Decimal | None = None
    fee_concentration_top_wallet_percent: Decimal | None = None
    fee_concentration_top_cluster_percent: Decimal | None = None
    volume_concentration_top_wallet_percent: Decimal | None = None
    volume_concentration_top_cluster_percent: Decimal | None = None
    transaction_concentration_top_wallet_percent: Decimal | None = None
    repeated_size_percent: Decimal | None = None
    round_trip_wallets: int = 0
    burst_wallets: int = 0
    multi_window_wallets: int = 0
    fee_acceleration_ratio: Decimal | None = None
    quality: EvidenceQuality = EvidenceQuality.UNKNOWN
    sampled_wallets: int = 0

    @property
    def available(self) -> bool:
        return self.quality is not EvidenceQuality.UNKNOWN and self.sampled_wallets > 0


@dataclass(frozen=True, slots=True)
class AuthenticityAssessment:
    """How real the demand looks, and exactly why."""

    score: Decimal = ZERO
    band: str = BAND_UNKNOWN
    quality: EvidenceQuality = EvidenceQuality.UNKNOWN
    manufactured_markers: tuple[str, ...] = ()
    authentic_markers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    activity: SolActivityProfile = field(default_factory=SolActivityProfile)

    @property
    def looks_manufactured(self) -> bool:
        return self.band in {BAND_SUSPECT, BAND_MANUFACTURED}


def aggregate_sol_activity(
    wallets: Sequence[WalletActivity],
    *,
    independent_buyers: int | None = None,
    prior_total_fees_sol: Decimal | None = None,
    window_seconds: int = 300,
    expected_wallets: int | None = None,
) -> SolActivityProfile:
    """Roll bounded per-wallet activity into one mint-level economic profile.

    ``expected_wallets`` is how many wallets the provider said exist.  When the
    bounded sample covers fewer, the profile is reported ``PARTIAL`` rather than
    pretending the sample is the whole population.
    """

    if not wallets:
        return SolActivityProfile(quality=EvidenceQuality.UNKNOWN)

    transactions = sum(item.transactions for item in wallets)
    base_fees = sum((item.base_fee_sol for item in wallets), ZERO)
    priority_fees = sum((item.priority_fee_sol for item in wallets), ZERO)
    total_fees = base_fees + priority_fees
    volume = sum((item.volume_usd for item in wallets), ZERO)
    buyers = [item for item in wallets if item.buys > 0]
    sellers = [item for item in wallets if item.sells > 0]
    buy_fees = sum(
        (_side_fee(item, item.buys) for item in wallets),
        ZERO,
    )
    sell_fees = sum(
        (_side_fee(item, item.sells) for item in wallets),
        ZERO,
    )

    top_wallet_fee = max((item.total_fee_sol for item in wallets), default=ZERO)
    top_wallet_volume = max((item.volume_usd for item in wallets), default=ZERO)
    top_wallet_tx = max((item.transactions for item in wallets), default=0)

    cluster_fees: dict[str, Decimal] = {}
    cluster_volume: dict[str, Decimal] = {}
    for item in wallets:
        key = item.cluster_id or f"solo:{item.wallet}"
        cluster_fees[key] = cluster_fees.get(key, ZERO) + item.total_fee_sol
        cluster_volume[key] = cluster_volume.get(key, ZERO) + item.volume_usd
    top_cluster_fee = max(cluster_fees.values(), default=ZERO)
    top_cluster_volume = max(cluster_volume.values(), default=ZERO)

    sizes: list[Decimal] = []
    for item in wallets:
        sizes.extend(item.trade_sizes_usd)
    repeated = _repeated_size_percent(sizes)

    round_trips = sum(1 for item in wallets if item.buys > 0 and item.sells > 0)
    bursts = sum(
        1
        for item in wallets
        if item.transactions >= 4 and item.active_seconds <= max(1, window_seconds // 5)
    )
    multi_window = sum(
        1 for item in wallets if item.active_seconds >= window_seconds and item.transactions >= 2
    )

    fee_acceleration = None
    if prior_total_fees_sol and prior_total_fees_sol > 0:
        fee_acceleration = (total_fees / prior_total_fees_sol).quantize(Decimal("0.01"))

    per_buyer = None
    if independent_buyers and independent_buyers > 0 and total_fees > 0:
        per_buyer = (total_fees / Decimal(independent_buyers)).quantize(Decimal("0.000001"))

    quality = EvidenceQuality.COMPLETE
    if expected_wallets is not None and len(wallets) < expected_wallets:
        quality = EvidenceQuality.PARTIAL
    if total_fees <= 0:
        quality = EvidenceQuality.PARTIAL

    return SolActivityProfile(
        transactions=transactions,
        unique_fee_payers=len({item.wallet for item in wallets if item.total_fee_sol > 0}),
        unique_buyers=len(buyers),
        unique_sellers=len(sellers),
        base_fees_sol=base_fees,
        priority_fees_sol=priority_fees,
        total_fees_sol=total_fees,
        buy_side_fees_sol=buy_fees,
        sell_side_fees_sol=sell_fees,
        volume_usd=volume,
        fee_per_independent_buyer_sol=per_buyer,
        fee_concentration_top_wallet_percent=_share(top_wallet_fee, total_fees),
        fee_concentration_top_cluster_percent=_share(top_cluster_fee, total_fees),
        volume_concentration_top_wallet_percent=_share(top_wallet_volume, volume),
        volume_concentration_top_cluster_percent=_share(top_cluster_volume, volume),
        transaction_concentration_top_wallet_percent=_share(
            Decimal(top_wallet_tx), Decimal(transactions)
        ),
        repeated_size_percent=repeated,
        round_trip_wallets=round_trips,
        burst_wallets=bursts,
        multi_window_wallets=multi_window,
        fee_acceleration_ratio=fee_acceleration,
        quality=quality,
        sampled_wallets=len(wallets),
    )


def assess_economic_authenticity(
    activity: SolActivityProfile,
    *,
    demand: Any = None,
    forensics: Any = None,
    holder_growth: int | None = None,
    independent_buyer_growth: int | None = None,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> AuthenticityAssessment:
    """Score how organic the observed economic activity looks.

    Missing evidence produces ``UNKNOWN`` with a neutral score, never a pass.
    """

    if not activity.available:
        return AuthenticityAssessment(
            score=ZERO,
            band=BAND_UNKNOWN,
            quality=EvidenceQuality.UNKNOWN,
            warnings=("No bounded SOL activity sample was available",),
            activity=activity,
        )

    manufactured: list[str] = []
    authentic: list[str] = []
    warnings: list[str] = []
    score = Decimal("50")

    wallets = max(1, activity.sampled_wallets)
    tx_per_wallet = Decimal(activity.transactions) / Decimal(wallets)
    if tx_per_wallet >= 8 and wallets <= 12:
        manufactured.append(MARK_FEW_WALLET_TX_SWARM)
        score -= 18
    elif tx_per_wallet >= 5:
        manufactured.append(MARK_FEW_WALLET_TX_SWARM)
        score -= 8

    top_cluster = activity.fee_concentration_top_cluster_percent
    if top_cluster is not None and top_cluster >= config.max_fee_concentration_percent:
        manufactured.append(MARK_CLUSTER_DOMINATES)
        score -= 20
    top_wallet_fee = activity.fee_concentration_top_wallet_percent
    if top_wallet_fee is not None and top_wallet_fee >= config.max_fee_concentration_percent:
        manufactured.append(MARK_FEE_CONCENTRATION)
        score -= 12
    elif top_wallet_fee is not None and top_wallet_fee <= 20:
        authentic.append(REWARD_DIVERSE_FEE_PAYERS)
        score += 8

    repeated = activity.repeated_size_percent
    if repeated is not None and repeated >= 60:
        manufactured.append(MARK_REPETITIVE_SIZING)
        score -= 15
    elif repeated is not None and repeated <= 25:
        authentic.append(REWARD_DIVERSE_SIZING)
        score += 8

    if activity.round_trip_wallets and wallets:
        churn = Decimal(activity.round_trip_wallets) / Decimal(wallets)
        if churn >= Decimal("0.5"):
            manufactured.append(MARK_CYCLIC_CHURN)
            score -= 14
        if churn >= Decimal("0.7"):
            manufactured.append(MARK_WASH_LIKE)
            score -= 10

    if activity.burst_wallets >= max(3, wallets // 2):
        manufactured.append(MARK_SYNCHRONIZED_BURSTS)
        score -= 12

    if activity.multi_window_wallets >= max(3, wallets // 3):
        authentic.append(REWARD_SUSTAINED_PARTICIPATION)
        score += 8

    shared_groups = tuple(getattr(forensics, "shared_funder_groups", ()) or ())
    time_groups = tuple(getattr(forensics, "time_linked_groups", ()) or ())
    if shared_groups:
        manufactured.append(MARK_SHARED_FUNDING)
        score -= 10 + min(10, 2 * len(shared_groups))
    if time_groups:
        manufactured.append(MARK_SYNCHRONIZED_FUNDING)
        score -= 10 + min(10, 2 * len(time_groups))

    fresh_percent = getattr(demand, "fresh_wallet_percent", None)
    if isinstance(fresh_percent, Decimal) and fresh_percent >= 60:
        manufactured.append(MARK_FRESH_WALLET_SWARM)
        score -= 15

    independence = getattr(demand, "independence_ratio", None)
    if isinstance(independence, Decimal):
        if independence >= config.min_independence_ratio:
            authentic.append(REWARD_INDEPENDENT_FUNDING)
            score += 12
        else:
            score -= 10
    else:
        warnings.append("Buyer independence is unknown and is not assumed")

    if (
        holder_growth is not None
        and independent_buyer_growth is not None
        and holder_growth >= 25
        and independent_buyer_growth <= 2
    ):
        manufactured.append(MARK_MANUFACTURED_HOLDERS)
        score -= 14
    if independent_buyer_growth is not None and independent_buyer_growth >= 5:
        authentic.append(REWARD_BUYER_EXPANSION)
        score += 8

    retention = activity.multi_window_wallets
    if retention and activity.unique_buyers and retention >= activity.unique_buyers // 3:
        authentic.append(REWARD_BUYER_RETENTION)
        score += 5

    if activity.total_fees_sol > 0 and not authentic:
        warnings.append("Real SOL was spent, but nothing independently corroborates it")

    quality = activity.quality
    if quality is EvidenceQuality.PARTIAL:
        warnings.append("SOL activity sample is partial; concentration may be understated")

    bounded = max(ZERO, min(Decimal("100"), score))
    return AuthenticityAssessment(
        score=bounded,
        band=_band(bounded, quality),
        quality=quality,
        manufactured_markers=tuple(dict.fromkeys(manufactured)),
        authentic_markers=tuple(dict.fromkeys(authentic)),
        warnings=tuple(warnings),
        activity=activity,
    )


@dataclass(frozen=True, slots=True)
class ManipulationEdge:
    source: str
    target: str
    kind: str
    confidence: str = "LOW"


@dataclass(frozen=True, slots=True)
class ManipulationGraph:
    """A bounded public relationship graph used only for coordination analysis.

    Correlation is not common ownership, and this graph never claims it is.
    """

    nodes: tuple[str, ...] = ()
    edges: tuple[ManipulationEdge, ...] = ()
    creator: str | None = None
    truncated: bool = False

    def neighbours(self, node: str) -> tuple[str, ...]:
        found = [edge.target for edge in self.edges if edge.source == node]
        found += [edge.source for edge in self.edges if edge.target == node]
        return tuple(dict.fromkeys(found))

    @property
    def coordination_density(self) -> Decimal:
        if len(self.nodes) < 2:
            return ZERO
        possible = Decimal(len(self.nodes) * (len(self.nodes) - 1) / 2)
        return (Decimal(len(self.edges)) / possible * 100).quantize(Decimal("0.01"))


def build_manipulation_graph(
    *,
    mint: str,
    creator: str | None,
    observations: Iterable[Any] = (),
    clusters: Iterable[Any] = (),
    max_nodes: int = 120,
) -> ManipulationGraph:
    """Assemble the bounded coordination graph from existing public evidence."""

    nodes: list[str] = [mint]
    edges: list[ManipulationEdge] = []
    truncated = False

    def add_node(value: object) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        if value in nodes:
            return value
        if len(nodes) >= max_nodes:
            return None
        nodes.append(value)
        return value

    if creator:
        node = add_node(creator)
        if node:
            edges.append(ManipulationEdge(node, mint, "DEPLOYED", "HIGH"))

    for observation in observations:
        wallet = add_node(getattr(observation, "wallet", None))
        if wallet is None:
            truncated = True
            continue
        edges.append(ManipulationEdge(wallet, mint, "TRADED", "HIGH"))
        funder = getattr(observation, "funder", None)
        if funder:
            funder_node = add_node(funder)
            if funder_node:
                edges.append(ManipulationEdge(funder_node, wallet, "FUNDED", "MEDIUM"))
        upstream = getattr(observation, "upstream_funder", None)
        if upstream:
            upstream_node = add_node(upstream)
            if upstream_node:
                edges.append(ManipulationEdge(upstream_node, wallet, "UPSTREAM_FUNDED", "LOW"))

    for cluster in clusters:
        members = tuple(getattr(cluster, "wallets", ()) or ())
        confidence = str(getattr(cluster, "confidence", "LOW"))
        for index, wallet in enumerate(members[:-1]):
            left = add_node(wallet)
            right = add_node(members[index + 1])
            if left and right:
                edges.append(ManipulationEdge(left, right, "CLUSTERED", confidence))

    return ManipulationGraph(
        nodes=tuple(nodes),
        edges=tuple(dict.fromkeys(edges)),
        creator=creator,
        truncated=truncated,
    )


def wallet_activity_from_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[WalletActivity, ...]:
    """Build activity records from bounded provider/RPC rows."""

    built: list[WalletActivity] = []
    for row in rows:
        wallet = str(row.get("wallet") or "").strip()
        if not wallet:
            continue
        sizes = tuple(
            _decimal(value) or ZERO for value in (row.get("trade_sizes_usd") or ()) if value
        )
        built.append(
            WalletActivity(
                wallet=wallet,
                transactions=int(row.get("transactions") or 0),
                buys=int(row.get("buys") or 0),
                sells=int(row.get("sells") or 0),
                volume_usd=_decimal(row.get("volume_usd")) or ZERO,
                base_fee_sol=_decimal(row.get("base_fee_sol")) or ZERO,
                priority_fee_sol=_decimal(row.get("priority_fee_sol")) or ZERO,
                first_seen_at=int(row.get("first_seen_at") or 0),
                last_seen_at=int(row.get("last_seen_at") or 0),
                cluster_id=(str(row["cluster_id"]) if row.get("cluster_id") else None),
                trade_sizes_usd=sizes,
            )
        )
    return tuple(built)


def _side_fee(item: WalletActivity, side_count: int) -> Decimal:
    if item.transactions <= 0 or side_count <= 0:
        return ZERO
    share = Decimal(side_count) / Decimal(item.transactions)
    return (item.total_fee_sol * share).quantize(Decimal("0.000001"))


def _share(part: Decimal, whole: Decimal) -> Decimal | None:
    if whole is None or whole <= 0:
        return None
    return (part / whole * 100).quantize(Decimal("0.01"))


def _repeated_size_percent(sizes: Sequence[Decimal]) -> Decimal | None:
    if len(sizes) < 4:
        return None
    counts: dict[str, int] = {}
    for size in sizes:
        key = str(size.quantize(Decimal("0.01")))
        counts[key] = counts.get(key, 0) + 1
    repeated = sum(count for count in counts.values() if count > 1)
    return (Decimal(repeated) / Decimal(len(sizes)) * 100).quantize(Decimal("0.01"))


def _band(score: Decimal, quality: EvidenceQuality) -> str:
    if quality is EvidenceQuality.UNKNOWN:
        return BAND_UNKNOWN
    if score >= 70:
        return BAND_AUTHENTIC
    if score >= 45:
        return BAND_MIXED
    if score >= 25:
        return BAND_SUSPECT
    return BAND_MANUFACTURED


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
