"""One feature pipeline, used by training, replay and live inference alike.

The failure this module is designed to make impossible is the most expensive one
in applied quantitative work: a backtest that works and a live system that does
not, because the two computed their inputs differently.  It happens quietly —
the offline job has the whole history in a dataframe and computes a 5-minute
window cleanly, while the live path has partial data and fills a gap with a
default — and the gap between the two is reported as alpha.

So there is exactly one function, :func:`build_features`, and it consumes a
:class:`PretrendState`.  Live collection builds that state from what has arrived
so far; replay builds it from storage, truncated at the simulated instant.
Neither path can compute a feature the other cannot, because neither path has
its own feature code.

Three further rules are enforced here rather than left to callers.

**Missing is UNKNOWN, never zero** (section 67).  A feature we could not compute
is recorded as ``None`` and its name is added to ``missing``.  Substituting 0
would tell the model "there was no FOMO buying" when the truth is "we could not
see the FOMO tape", and those two states lead to opposite decisions.

**Every value carries a version and a provenance.**  ``FEATURE_VERSION`` changes
whenever the meaning of any feature changes, so a model trained on v3 refuses a
v4 vector instead of scoring it wrongly.

**Nothing is read past the instant.**  Every input arrives through
:class:`~smart_money_bot.pretrend.windows.Series` or
:class:`~smart_money_bot.pretrend.activity.ActivityTape`, both of which bound
reads at the requested time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .activity import ActivityTape, activity_window_features
from .affinity import AffinityRecord, quality_weight
from .cohorts import (
    AGE_COHORT_UNKNOWN,
    MC_COHORT_UNKNOWN,
    PopulationSnapshot,
    RelativeAttention,
    age_cohort,
    build_relative_attention,
    market_cap_cohort,
)
from .independence import Arrival, IndependenceProfile, build_independence
from .windows import (
    Series,
    counter_dynamics,
    level_dynamics,
    sum_dynamics,
    window_label,
)

ZERO = Decimal("0")

#: Bumped whenever any feature's *meaning* changes.  A model records the version
#: it was trained on and refuses a vector built by a different one.
FEATURE_VERSION = "pretrend.v1"

#: FOMO-native windows that reach the feature vector.  The full ladder is
#: computed for cards and forensics; the model gets the subset that is not
#: almost perfectly collinear with its neighbours.
FOMO_FEATURE_WINDOWS: tuple[int, ...] = (30, 60, 180, 300)
#: Market windows for the same reason.
MARKET_FEATURE_WINDOWS: tuple[int, ...] = (60, 180, 300)


@dataclass(frozen=True, slots=True)
class MarketSeries:
    """The market-side inputs for one mint, each an append-only series."""

    price_usd: Series = field(default_factory=lambda: Series("price_usd"))
    market_cap_usd: Series = field(default_factory=lambda: Series("market_cap_usd"))
    liquidity_usd: Series = field(default_factory=lambda: Series("liquidity_usd"))
    volume_usd: Series = field(default_factory=lambda: Series("volume_usd"))
    buys: Series = field(default_factory=lambda: Series("buys"))
    sells: Series = field(default_factory=lambda: Series("sells"))
    unique_buyers: Series = field(default_factory=lambda: Series("unique_buyers"))
    holders: Series = field(default_factory=lambda: Series("holders"))
    net_flow_usd: Series = field(default_factory=lambda: Series("net_flow_usd"))
    social_engagement: Series = field(
        default_factory=lambda: Series("social_engagement")
    )

    def latest_timestamps(self) -> dict[str, int | None]:
        """The newest sample in each series — used by the leakage audit."""

        result: dict[str, int | None] = {}
        for name in (
            "price_usd",
            "market_cap_usd",
            "liquidity_usd",
            "volume_usd",
            "buys",
            "sells",
            "unique_buyers",
            "holders",
            "net_flow_usd",
            "social_engagement",
        ):
            series: Series = getattr(self, name)
            samples = series.samples
            result[name] = samples[-1].at if samples else None
        return result


@dataclass(frozen=True, slots=True)
class PretrendState:
    """Everything known about one mint, as of one instant.

    This is the seam between collection and computation.  It carries data, never
    behaviour, so a replayed state and a live state are literally the same type.
    """

    mint: str
    #: The instant features are being computed for.  Nothing later may be read.
    at: int
    market: MarketSeries = field(default_factory=MarketSeries)
    tape: ActivityTape | None = None
    #: First action per actor on this mint, for the independence block.
    arrivals: tuple[Arrival, ...] = ()
    #: Affinity records for actors seen on this mint, keyed by actor id.
    affinities: Mapping[str, AffinityRecord] = field(default_factory=dict)
    #: Population snapshots for relative attention.
    populations: Sequence[PopulationSnapshot] = ()
    token_age_seconds: int | None = None
    pair_age_seconds: int | None = None
    launch_source: str = ""
    #: Cross-source first-seen times: source name -> timestamp (section 35).
    first_seen_by_source: Mapping[str, int] = field(default_factory=dict)
    #: Optional risk/organic-flow inputs already computed elsewhere.
    organic_volume_ratio: Decimal | None = None
    wash_risk: Decimal | None = None
    top10_percent: Decimal | None = None
    creator_percent: Decimal | None = None
    fresh_wallet_ratio: Decimal | None = None
    independent_wallet_clusters: int | None = None
    smart_wallets: int | None = None
    social_quality: Decimal | None = None
    social_bot_risk: Decimal | None = None
    exact_ca_social_match: bool | None = None
    #: Per-input source timestamps, for the leakage audit.
    source_timestamps: Mapping[str, int | None] = field(default_factory=dict)

    @property
    def market_cap_usd(self) -> Decimal | None:
        return self.market.market_cap_usd.value_at(self.at)

    @property
    def liquidity_usd(self) -> Decimal | None:
        return self.market.liquidity_usd.value_at(self.at)


@dataclass(frozen=True, slots=True)
class FeatureVector:
    """A versioned, point-in-time feature vector with an explicit missing set."""

    mint: str
    observed_at: int
    feature_version: str
    values: dict[str, Decimal | None] = field(default_factory=dict)
    #: Names whose value could not be established.  Never silently zeroed.
    missing: frozenset[str] = frozenset()
    source_timestamps: Mapping[str, int | None] = field(default_factory=dict)
    series_latest: Mapping[str, int | None] = field(default_factory=dict)
    mc_cohort: str = MC_COHORT_UNKNOWN
    age_cohort: str = AGE_COHORT_UNKNOWN
    #: Sub-blocks kept for cards and forensics; the model reads ``values`` only.
    independence: IndependenceProfile | None = None
    relative: RelativeAttention | None = None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.values))

    @property
    def completeness(self) -> Decimal:
        """Share of features that were actually computable."""

        if not self.values:
            return ZERO
        known = len(self.values) - len(self.missing)
        return (Decimal(known) / Decimal(len(self.values))).quantize(Decimal("0.0001"))

    def get(self, name: str) -> Decimal | None:
        return self.values.get(name)

    def to_json(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "observed_at": self.observed_at,
            "feature_version": self.feature_version,
            "mc_cohort": self.mc_cohort,
            "age_cohort": self.age_cohort,
            "completeness": str(self.completeness),
            "missing": sorted(self.missing),
            "values": {
                name: (None if value is None else str(value))
                for name, value in sorted(self.values.items())
            },
        }


class _Builder:
    """Accumulates named values, tracking which ones were unknowable."""

    def __init__(self) -> None:
        self.values: dict[str, Decimal | None] = {}
        self.missing: set[str] = set()

    def put(self, name: str, value: Decimal | int | None) -> None:
        if value is None:
            self.values[name] = None
            self.missing.add(name)
            return
        self.values[name] = value if isinstance(value, Decimal) else Decimal(value)

    def put_bool(self, name: str, value: bool | None) -> None:
        if value is None:
            self.values[name] = None
            self.missing.add(name)
            return
        self.values[name] = Decimal("1") if value else ZERO


def build_features(state: PretrendState) -> FeatureVector:
    """Build the point-in-time feature vector for ``state.mint`` at ``state.at``.

    The one function.  Training, replay and live inference all call this.
    """

    at = state.at
    builder = _Builder()

    # --- FOMO-native block (sections 14, 15) --------------------------------
    for seconds in FOMO_FEATURE_WINDOWS:
        label = window_label(seconds)
        if state.tape is None:
            for suffix in (
                "fomo_buys",
                "fomo_sells",
                "fomo_buy_usd",
                "fomo_net_buy_usd",
                "unique_fomo_buyers",
                "new_fomo_buyers",
                "new_fomo_buyer_velocity",
                "new_fomo_buyer_acceleration",
                "fomo_buy_volume_velocity",
                "fomo_buy_volume_acceleration",
                "fomo_buy_sell_tx_ratio",
                "median_fomo_buy_size",
                "largest_fomo_buy",
                "fomo_thesis_count",
                "thesis_velocity",
                "thesis_acceleration",
            ):
                builder.put(f"{suffix}_{label}", None)
            continue
        block = activity_window_features(state.tape, at=at, window_seconds=seconds)
        builder.put(f"fomo_buys_{label}", block.fomo_buys)
        builder.put(f"fomo_sells_{label}", block.fomo_sells)
        builder.put(f"fomo_buy_usd_{label}", block.fomo_buy_usd)
        builder.put(f"fomo_net_buy_usd_{label}", block.fomo_net_buy_usd)
        builder.put(f"unique_fomo_buyers_{label}", block.unique_fomo_buyers)
        builder.put(f"new_fomo_buyers_{label}", block.new_fomo_buyers)
        builder.put(f"new_fomo_buyer_velocity_{label}", block.new_fomo_buyer_velocity)
        builder.put(
            f"new_fomo_buyer_acceleration_{label}", block.new_fomo_buyer_acceleration
        )
        builder.put(f"fomo_buy_volume_velocity_{label}", block.fomo_buy_volume_velocity)
        builder.put(
            f"fomo_buy_volume_acceleration_{label}", block.fomo_buy_volume_acceleration
        )
        builder.put(f"fomo_buy_sell_tx_ratio_{label}", block.fomo_buy_sell_tx_ratio)
        builder.put(f"median_fomo_buy_size_{label}", block.median_fomo_buy_size)
        builder.put(f"largest_fomo_buy_{label}", block.largest_fomo_buy)
        builder.put(f"fomo_thesis_count_{label}", block.fomo_thesis_count)
        builder.put(f"thesis_velocity_{label}", block.thesis_velocity)
        builder.put(f"thesis_acceleration_{label}", block.thesis_acceleration)

    # --- market block (sections 25, 26, 27) ---------------------------------
    market = state.market
    for seconds in MARKET_FEATURE_WINDOWS:
        label = window_label(seconds)
        for name, series, kind in (
            ("market_cap_usd", market.market_cap_usd, "level"),
            ("price_usd", market.price_usd, "level"),
            ("liquidity_usd", market.liquidity_usd, "level"),
            ("holders", market.holders, "level"),
            ("unique_buyers", market.unique_buyers, "level"),
            ("volume_usd", market.volume_usd, "sum"),
            ("net_flow_usd", market.net_flow_usd, "sum"),
            ("buys", market.buys, "counter"),
            ("sells", market.sells, "counter"),
            ("social_engagement", market.social_engagement, "level"),
        ):
            fn = (
                level_dynamics
                if kind == "level"
                else (sum_dynamics if kind == "sum" else counter_dynamics)
            )
            dynamics = fn(series, end=at, seconds=seconds)
            builder.put(f"{name}_level_{label}", dynamics.level)
            builder.put(f"{name}_velocity_{label}", dynamics.velocity)
            builder.put(f"{name}_acceleration_{label}", dynamics.acceleration)
            builder.put(f"{name}_ratio_prior_{label}", dynamics.ratio_to_prior)

    market_cap = state.market_cap_usd
    liquidity = state.liquidity_usd
    volume_5m = sum_dynamics(market.volume_usd, end=at, seconds=300).level

    builder.put("market_cap_usd", market_cap)
    builder.put("liquidity_usd", liquidity)
    builder.put(
        "liquidity_over_market_cap",
        None
        if liquidity is None or market_cap is None or market_cap <= ZERO
        else (liquidity / market_cap).quantize(Decimal("0.000001")),
    )
    builder.put(
        "volume_over_market_cap_5m",
        None
        if volume_5m is None or market_cap is None or market_cap <= ZERO
        else (volume_5m / market_cap).quantize(Decimal("0.000001")),
    )
    builder.put(
        "volume_over_liquidity_5m",
        None
        if volume_5m is None or liquidity is None or liquidity <= ZERO
        else (volume_5m / liquidity).quantize(Decimal("0.000001")),
    )
    builder.put("token_age_seconds", state.token_age_seconds)
    builder.put("pair_age_seconds", state.pair_age_seconds)

    # --- independence block (section 19) ------------------------------------
    weights = {
        actor_id: quality_weight(record) for actor_id, record in state.affinities.items()
    }
    arrivals = tuple(
        Arrival(
            actor_id=arrival.actor_id,
            at=arrival.at,
            amount_usd=arrival.amount_usd,
            cluster_id=arrival.cluster_id,
            weight=weights.get(arrival.actor_id, arrival.weight),
        )
        for arrival in state.arrivals
        if arrival.at <= at
    )
    independence = build_independence(state.mint, arrivals, at=at)
    builder.put("quality_fomo_buyers_30s", independence.quality_buyers_30s)
    builder.put("quality_fomo_buyers_1m", independence.quality_buyers_1m)
    builder.put("quality_fomo_buyers_3m", independence.quality_buyers_3m)
    builder.put("independent_quality_buyers", independence.independent_quality_buyers)
    builder.put(
        "possible_follow_cluster_count", independence.possible_follow_cluster_count
    )
    builder.put("buyer_arrival_entropy", independence.buyer_arrival_entropy)
    builder.put("buyer_concentration", independence.buyer_concentration)
    builder.put(
        "median_seconds_between_buyers", independence.median_seconds_between_buyers
    )
    builder.put("arrival_velocity", independence.arrival_velocity)
    builder.put("arrival_acceleration", independence.arrival_acceleration)
    builder.put("independence_ratio", independence.independence_ratio)
    builder.put(
        "seconds_since_first_quality_buyer",
        None
        if independence.first_quality_buyer_at is None
        else at - independence.first_quality_buyer_at,
    )

    # --- quality-weighted FOMO flow (section 56) ----------------------------
    # The weights are measured lift, not invented multipliers: an unmeasured
    # actor contributes exactly 1, so this reduces to the raw count when we know
    # nothing about anyone.
    if state.tape is None:
        builder.put("quality_weighted_fomo_flow_3m", None)
        builder.put("raw_fomo_flow_3m", None)
    else:
        recent = [
            event
            for event in state.tape.in_window(end=at, seconds=180)
            if event.is_buy
        ]
        builder.put("raw_fomo_flow_3m", len(recent))
        builder.put(
            "quality_weighted_fomo_flow_3m",
            sum(
                (weights.get(event.trader_id, Decimal("1")) for event in recent),
                ZERO,
            ),
        )

    # --- relative attention (section 21) ------------------------------------
    mc_cohort = market_cap_cohort(market_cap)
    token_age = age_cohort(state.token_age_seconds)
    relative = build_relative_attention(
        state.mint,
        [snapshot for snapshot in state.populations if snapshot.at <= at],
        at=at,
        mc_cohort=mc_cohort,
        token_age_cohort=token_age,
    )
    for metric, value in relative.percentiles.items():
        builder.put(f"pct_{metric}", value)
    for metric, value in relative.cohort_percentiles.items():
        builder.put(f"cohort_pct_{metric}", value)
    for metric, value in relative.shares.items():
        builder.put(f"share_{metric}", value)

    # --- cross-source arrival (section 35) ----------------------------------
    for source in (
        "pumpfun",
        "fomo_latest",
        "fomo_graduated",
        "fomo_activity",
        "gmgn",
        "dexscreener",
    ):
        seen = state.first_seen_by_source.get(source)
        builder.put(
            f"seconds_since_first_seen_{source}",
            None if seen is None or seen > at else at - seen,
        )
    known_sources = [
        seen for seen in state.first_seen_by_source.values() if seen is not None and seen <= at
    ]
    builder.put("distinct_sources_seen", len(known_sources) if known_sources else None)
    builder.put(
        "source_arrival_spread_seconds",
        None if len(known_sources) < 2 else max(known_sources) - min(known_sources),
    )

    # --- risk / organic-flow block (sections 28, 29) ------------------------
    builder.put("organic_volume_ratio", state.organic_volume_ratio)
    builder.put("wash_risk", state.wash_risk)
    builder.put("top10_percent", state.top10_percent)
    builder.put("creator_percent", state.creator_percent)
    builder.put("fresh_wallet_ratio", state.fresh_wallet_ratio)
    builder.put("independent_wallet_clusters", state.independent_wallet_clusters)
    builder.put("smart_wallets", state.smart_wallets)

    # --- social block (sections 32, 33) -------------------------------------
    builder.put("social_quality", state.social_quality)
    builder.put("social_bot_risk", state.social_bot_risk)
    builder.put_bool("exact_ca_social_match", state.exact_ca_social_match)

    return FeatureVector(
        mint=state.mint,
        observed_at=at,
        feature_version=FEATURE_VERSION,
        values=builder.values,
        missing=frozenset(builder.missing),
        source_timestamps=dict(state.source_timestamps),
        series_latest=market.latest_timestamps(),
        mc_cohort=mc_cohort,
        age_cohort=token_age,
        independence=independence,
        relative=relative,
    )


def feature_names(state_template: PretrendState | None = None) -> tuple[str, ...]:
    """The full ordered feature name list, derived from an empty state.

    Deriving it rather than hardcoding it means the list cannot drift away from
    what :func:`build_features` actually produces.
    """

    state = state_template or PretrendState(mint="1" * 32, at=0)
    return build_features(state).names
