"""Evidence-based candidate qualification for the Fomo Runner funnel.

v2.34 collapsed momentum, setup quality and scam risk into a single 0-100
number, then used that number for every decision.  A token could reach the
mid-thirties on recency, a working Jupiter route and a holder count alone, with
no evidence at all that anybody independent was buying it.  That is why the
research feed read like a mirror of the Fomo "Graduated" tab.

This module keeps three concepts logically separate:

* ``MOMENTUM``     — how strongly the token is accelerating right now.
* ``OPPORTUNITY``  — how interesting the setup actually is, dominated by how
  much of the visible activity looks like independent demand.
* ``SAFETY``       — how dangerous the token appears.  Owned by
  :func:`smart_money_bot.runner.assess_runner_safety`; it fails closed and an
  ``UNKNOWN`` never becomes a ``PASS``.

Everything here is a pure function over evidence that exists at one evaluation
time.  No provider calls, no database, no future information.  Funding links
described here are public-chain coordination evidence only; they never claim
that separate wallets belong to one real person.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

from .models import (
    RunnerDemandProfile,
    RunnerForensics,
    RunnerMarketSnapshot,
    RunnerQualityAssessment,
    RunnerSafetyAssessment,
)

QUALITY_DECISION_VERSION = "quality-v1"

STAGE_RAW = "RAW_DISCOVERY"
STAGE_SILENT_WATCH = "SILENT_WATCH"
STAGE_QUALIFIED = "QUALIFIED_RESEARCH"
STAGE_HEATING = "HEATING_UP"
STAGE_UNSAFE = "UNSAFE_MOMENTUM"
STAGE_ENTRY = "ENTRY_CANDIDATE"
STAGE_STRONG = "STRONG_RUNNER"

#: Ordering used for "best stage reached" and for digest ranking.  ``UNSAFE``
#: deliberately sits above ``QUALIFIED`` for visibility but below ``ENTRY``.
STAGE_RANK: dict[str, int] = {
    STAGE_RAW: 0,
    STAGE_SILENT_WATCH: 1,
    STAGE_QUALIFIED: 2,
    STAGE_HEATING: 3,
    STAGE_UNSAFE: 4,
    STAGE_ENTRY: 5,
    STAGE_STRONG: 6,
}

#: Stages whose candidates are allowed to reach the main research feed.
USER_FACING_STAGES = frozenset(
    {STAGE_QUALIFIED, STAGE_HEATING, STAGE_UNSAFE, STAGE_ENTRY, STAGE_STRONG}
)

STAGE_LABELS: dict[str, str] = {
    STAGE_RAW: "👁️ RAW",
    STAGE_SILENT_WATCH: "🔎 SILENT WATCH",
    STAGE_QUALIFIED: "🟢 QUALIFIED RESEARCH",
    STAGE_HEATING: "🔥 ACCELERATING",
    STAGE_UNSAFE: "⚠️ UNSAFE MOMENTUM",
    STAGE_ENTRY: "✅ ENTRY CANDIDATE",
    STAGE_STRONG: "🚨 STRONG RUNNER",
}

#: Distinct affirmative observations.  Every one of these requires evidence to
#: be *present*; none of them can be satisfied by missing data.  Qualification
#: counts how many independent families fired, so a single loud field cannot
#: carry a candidate on its own.
EVIDENCE_BUY_ACCELERATION = "buy acceleration"
EVIDENCE_TRANSACTION_ACCELERATION = "transaction acceleration"
EVIDENCE_VOLUME_ACCELERATION = "volume acceleration"
EVIDENCE_HOLDER_GROWTH = "holder growth"
EVIDENCE_INDEPENDENT_BUYER_GROWTH = "independent buyer growth"
EVIDENCE_LIQUIDITY_GROWTH = "liquidity growth"
EVIDENCE_LIQUIDITY_RATIO = "liquidity depth vs valuation"
EVIDENCE_SMART_WALLETS = "independent smart-wallet confirmation"
EVIDENCE_BUYER_BREADTH = "organic buy/sell breadth"
EVIDENCE_PRICE_PROGRESSION = "confirmed price progression"
EVIDENCE_LOW_CONCENTRATION = "verified low cluster concentration"
EVIDENCE_SCORE_VELOCITY = "signal velocity"

#: Evidence families.  Two signals from the same family count once, so
#: "volume up + transactions up" (the same flow measured twice) cannot
#: masquerade as two independent confirmations.
EVIDENCE_FAMILIES: dict[str, str] = {
    EVIDENCE_BUY_ACCELERATION: "flow",
    EVIDENCE_TRANSACTION_ACCELERATION: "flow",
    EVIDENCE_VOLUME_ACCELERATION: "flow",
    EVIDENCE_BUYER_BREADTH: "flow",
    EVIDENCE_HOLDER_GROWTH: "holders",
    EVIDENCE_INDEPENDENT_BUYER_GROWTH: "holders",
    EVIDENCE_LIQUIDITY_GROWTH: "liquidity",
    EVIDENCE_LIQUIDITY_RATIO: "liquidity",
    EVIDENCE_SMART_WALLETS: "smart_money",
    EVIDENCE_PRICE_PROGRESSION: "price",
    EVIDENCE_SCORE_VELOCITY: "price",
    EVIDENCE_LOW_CONCENTRATION: "forensics",
}


@dataclass(frozen=True, slots=True)
class RunnerQualityConfig:
    """Grouped, overridable qualification thresholds.

    Defaults are deliberately conservative starting points, not calibrated
    constants.  ``/fomo quality`` and ``/fomo calibration`` report the live
    distributions these should be tuned against once the production database
    has enough forward observations.
    """

    min_evidence_families: int = 2
    min_opportunity_score: Decimal = Decimal("45")
    acceleration_route_discount: Decimal = Decimal("15")
    heating_min_opportunity: Decimal = Decimal("55")
    heating_min_momentum: Decimal = Decimal("60")
    unsafe_min_momentum: Decimal = Decimal("70")
    entry_min_opportunity: Decimal = Decimal("65")
    entry_min_momentum: Decimal = Decimal("50")
    strong_min_opportunity: Decimal = Decimal("82")
    strong_min_momentum: Decimal = Decimal("75")
    max_qualified_age_seconds: int = 1_800
    min_liquidity_to_market_cap: Decimal = Decimal("0.03")
    fragile_liquidity_to_market_cap: Decimal = Decimal("0.05")
    wash_volume_to_liquidity: Decimal = Decimal("12")
    min_independence_ratio: Decimal = Decimal("0.45")
    max_cluster_supply_percent: Decimal = Decimal("25")
    max_fresh_wallet_percent: Decimal = Decimal("70")
    veto_time_linked_wallets: int = 5
    overextended_price_percent: Decimal = Decimal("200")
    overextended_dex_5m_percent: Decimal = Decimal("100")
    parabolic_price_percent: Decimal = Decimal("80")


DEFAULT_QUALITY_CONFIG = RunnerQualityConfig()


def quality_config_from_settings(settings: object) -> RunnerQualityConfig:
    """Build the qualification config from ``Settings`` without importing it.

    Duck-typed on purpose so ``config`` never has to import this module.  Any
    attribute that is missing falls back to the documented default.
    """

    def value(name: str, default):
        return getattr(settings, name, default)

    base = DEFAULT_QUALITY_CONFIG
    return RunnerQualityConfig(
        min_evidence_families=int(
            value("fomo_runner_min_evidence_families", base.min_evidence_families)
        ),
        min_opportunity_score=Decimal(
            str(value("fomo_runner_min_opportunity_score", base.min_opportunity_score))
        ),
        acceleration_route_discount=base.acceleration_route_discount,
        heating_min_opportunity=Decimal(
            str(value("fomo_runner_heating_min_opportunity", base.heating_min_opportunity))
        ),
        heating_min_momentum=Decimal(
            str(value("fomo_runner_heating_min_momentum", base.heating_min_momentum))
        ),
        unsafe_min_momentum=base.unsafe_min_momentum,
        entry_min_opportunity=Decimal(
            str(value("fomo_runner_entry_min_opportunity", base.entry_min_opportunity))
        ),
        entry_min_momentum=Decimal(
            str(value("fomo_runner_entry_min_momentum", base.entry_min_momentum))
        ),
        strong_min_opportunity=base.strong_min_opportunity,
        strong_min_momentum=base.strong_min_momentum,
        max_qualified_age_seconds=int(
            value("fomo_runner_max_graduation_age_minutes", 30) * 60
        ),
        min_liquidity_to_market_cap=base.min_liquidity_to_market_cap,
        fragile_liquidity_to_market_cap=base.fragile_liquidity_to_market_cap,
        wash_volume_to_liquidity=base.wash_volume_to_liquidity,
        min_independence_ratio=Decimal(
            str(value("fomo_runner_min_independence_ratio", base.min_independence_ratio))
        ),
        max_cluster_supply_percent=Decimal(
            str(
                value(
                    "fomo_runner_max_cluster_supply_percent",
                    base.max_cluster_supply_percent,
                )
            )
        ),
        max_fresh_wallet_percent=base.max_fresh_wallet_percent,
        veto_time_linked_wallets=base.veto_time_linked_wallets,
        overextended_price_percent=base.overextended_price_percent,
        overextended_dex_5m_percent=base.overextended_dex_5m_percent,
        parabolic_price_percent=base.parabolic_price_percent,
    )


def _pct(current: Decimal | None, base: Decimal | None) -> Decimal | None:
    if current is None or base is None or base <= 0:
        return None
    return ((current / base) - Decimal("1")) * Decimal("100")


def _ratio(current: Decimal | int | None, base: Decimal | int | None) -> Decimal | None:
    if current is None or base is None:
        return None
    denominator = Decimal(str(base))
    if denominator <= 0:
        return None
    return Decimal(str(current)) / denominator


def _clamp(value: Decimal, low: Decimal = Decimal("0"), high: Decimal = Decimal("100")) -> Decimal:
    return max(low, min(high, value)).quantize(Decimal("0.01"))


def _band(value: Decimal | None, bands: Sequence[tuple[Decimal, int]]) -> int:
    """Return the points for the first band whose floor ``value`` clears."""

    if value is None:
        return 0
    for floor, points in bands:
        if value >= floor:
            return points
    return 0


# ---------------------------------------------------------------------------
# Organic demand
# ---------------------------------------------------------------------------


def build_demand_profile(
    *,
    forensics: RunnerForensics | None,
    current: RunnerMarketSnapshot,
    raw_smart_wallets: int = 0,
    independent_smart_clusters: int | None = None,
) -> RunnerDemandProfile:
    """Summarize who is actually buying, not how many transactions there were.

    ``estimated_independent_buyers`` stays ``None`` whenever the bounded trace
    did not run.  An unknown independence level is never optimistically treated
    as full independence.
    """

    forensics = forensics or RunnerForensics()
    raw_buyers = max(
        forensics.raw_unique_buyers,
        current.verified_unique_buyers,
    )
    independent = forensics.estimated_independent_clusters
    # Independence is measured over the wallets actually traced, never
    # extrapolated across every raw buyer.  Tracing 12 top holders and finding
    # them unlinked says nothing about the other 75 addresses that traded.
    ratio = (
        _ratio(independent, forensics.traced_wallets)
        if independent is not None and forensics.traced_wallets
        else None
    )
    fresh_count = forensics.fresh_wallet_count
    fresh_percent = None
    if fresh_count is not None and forensics.traced_wallets:
        fresh_percent = (
            Decimal(fresh_count) / Decimal(forensics.traced_wallets) * Decimal("100")
        ).quantize(Decimal("0.01"))
    largest = forensics.shared_funder_groups[0] if forensics.shared_funder_groups else None
    smart_clusters = (
        independent_smart_clusters
        if independent_smart_clusters is not None
        else raw_smart_wallets
    )
    if not forensics.available:
        confidence = "UNKNOWN"
    elif forensics.traced_wallets >= 8 and independent is not None:
        confidence = "HIGH"
    elif forensics.traced_wallets >= 4:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"
    return RunnerDemandProfile(
        raw_buyers=raw_buyers,
        estimated_independent_buyers=independent,
        independence_ratio=(ratio.quantize(Decimal("0.0001")) if ratio is not None else None),
        largest_cluster_wallets=forensics.largest_cluster_size,
        cluster_supply_percent=forensics.largest_cluster_supply_percent,
        fresh_wallet_count=fresh_count,
        fresh_wallet_percent=fresh_percent,
        traced_wallets=forensics.traced_wallets,
        time_linked_clusters=len(forensics.time_linked_groups),
        time_linked_wallets=sum(item.wallet_count for item in forensics.time_linked_groups),
        upstream_linked_clusters=len(
            tuple(
                item
                for item in forensics.shared_funder_groups
                if item.cluster_kind == "UPSTREAM_FUNDER"
            )
        ),
        largest_cluster_id=(largest.cluster_id if largest else None),
        raw_smart_wallets=raw_smart_wallets,
        independent_smart_clusters=max(0, smart_clusters),
        confidence=confidence,
    )


def organic_demand_score(
    demand: RunnerDemandProfile,
    *,
    current: RunnerMarketSnapshot,
    config: RunnerQualityConfig = DEFAULT_QUALITY_CONFIG,
) -> tuple[Decimal, tuple[str, ...]]:
    """Score how independent the visible demand looks, 0-100, plus warnings.

    Starts from a neutral 50 when independence is unknown: an untraced token is
    neither confirmed organic nor confirmed coordinated, and the qualification
    gate handles unknowns through the evidence requirement rather than by
    pretending the trace passed.
    """

    warnings: list[str] = []
    total = current.buys_5m + current.sells_5m
    buy_ratio = _ratio(current.buys_5m, total) or Decimal("0")

    if demand.independence_ratio is None:
        score = Decimal("50")
    else:
        # 100% independent -> 100, at the floor -> 45, fully collapsed -> 0.
        score = _clamp(demand.independence_ratio * Decimal("100"))
        if demand.independence_ratio < config.min_independence_ratio:
            warnings.append(
                f"{demand.raw_buyers} raw buyers but only "
                f"{demand.estimated_independent_buyers} independent clusters"
            )

    if total >= 12:
        if buy_ratio >= Decimal("0.65"):
            score += Decimal("12")
        elif buy_ratio >= Decimal("0.55"):
            score += Decimal("6")
        elif buy_ratio < Decimal("0.45"):
            score -= Decimal("12")
            warnings.append("five-minute flow favors sellers")
    if (
        current.largest_verified_buyer_percent is not None
        and current.largest_verified_buyer_percent > Decimal("50")
    ):
        score -= Decimal("15")
        warnings.append(
            "largest observed buyer controls "
            f"{current.largest_verified_buyer_percent:.1f}% of measured buy value"
        )

    if demand.cluster_supply_percent is not None:
        if demand.cluster_supply_percent > config.max_cluster_supply_percent:
            score -= Decimal("25")
            warnings.append(
                f"largest linked cluster holds {demand.cluster_supply_percent:.1f}% of supply "
                f"across {demand.largest_cluster_wallets or 0} wallets"
            )
        elif demand.cluster_supply_percent > config.max_cluster_supply_percent / 2:
            score -= Decimal("10")
    if demand.time_linked_clusters:
        # Weight by how many wallets are actually linked: two wallets funded
        # alike is a hint, seventeen is a bundle.
        score -= Decimal(min(35, 6 + demand.time_linked_wallets * 3))
        warnings.append(
            f"{demand.time_linked_wallets} wallets across {demand.time_linked_clusters} "
            "time-linked funding group(s): funded close together with similar amounts, "
            "then bought close together"
        )
    if demand.upstream_linked_clusters:
        score -= Decimal(min(15, demand.upstream_linked_clusters * 8))
        warnings.append(
            f"{demand.upstream_linked_clusters} group(s) share a common upstream funder "
            "through an intermediary wallet"
        )
    if (
        demand.fresh_wallet_percent is not None
        and demand.fresh_wallet_percent > config.max_fresh_wallet_percent
    ):
        score -= Decimal("15")
        warnings.append(
            f"{demand.fresh_wallet_count}/{demand.traced_wallets} traced holders are "
            "wallets first active in the last few hours"
        )
    if demand.raw_smart_wallets >= 2 and demand.independent_smart_clusters <= 1:
        warnings.append(
            f"{demand.raw_smart_wallets} smart wallets resolve to "
            f"{demand.independent_smart_clusters} independent cluster(s)"
        )
    return _clamp(score), tuple(dict.fromkeys(warnings))


# ---------------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------------


def momentum_score(
    *,
    first: RunnerMarketSnapshot,
    current: RunnerMarketSnapshot,
    prior: RunnerMarketSnapshot | None,
    dex_price_change_5m: Decimal | None,
    score_history: Sequence[Decimal] = (),
) -> tuple[Decimal, tuple[str, ...]]:
    """Score acceleration only.  Says nothing about whether the flow is real."""

    evidence: list[str] = []
    score = Decimal("0")
    elapsed = max(1, current.captured_at - (prior.captured_at if prior else first.captured_at))

    if prior is not None and prior.captured_at < current.captured_at:
        buy_ratio = _ratio(current.buys_5m, prior.buys_5m)
        tx_ratio = _ratio(
            current.buys_5m + current.sells_5m,
            prior.buys_5m + prior.sells_5m,
        )
        volume_ratio = _ratio(current.volume_5m_usd, prior.volume_5m_usd)
        score += _band(
            buy_ratio,
            (
                (Decimal("2.0"), 22),
                (Decimal("1.5"), 17),
                (Decimal("1.35"), 12),
                (Decimal("1.15"), 6),
            ),
        )
        if buy_ratio is not None and buy_ratio >= Decimal("1.35"):
            evidence.append(EVIDENCE_BUY_ACCELERATION)
        score += _band(
            tx_ratio,
            (
                (Decimal("2.0"), 16),
                (Decimal("1.5"), 12),
                (Decimal("1.35"), 8),
                (Decimal("1.15"), 4),
            ),
        )
        if tx_ratio is not None and tx_ratio >= Decimal("1.35"):
            evidence.append(EVIDENCE_TRANSACTION_ACCELERATION)
        score += _band(
            volume_ratio,
            ((Decimal("3.0"), 20), (Decimal("2.0"), 15), (Decimal("1.5"), 10), (Decimal("1.2"), 5)),
        )
        if volume_ratio is not None and volume_ratio >= Decimal("1.5"):
            evidence.append(EVIDENCE_VOLUME_ACCELERATION)
        if (
            current.holder_count is not None
            and prior.holder_count is not None
            and current.holder_count > prior.holder_count
        ):
            growth = Decimal(current.holder_count - prior.holder_count)
            per_minute = growth * Decimal("60") / Decimal(elapsed)
            score += _band(
                per_minute,
                ((Decimal("25"), 18), (Decimal("12"), 14), (Decimal("5"), 9), (Decimal("2"), 5)),
            )
            if per_minute >= Decimal("5"):
                evidence.append(EVIDENCE_HOLDER_GROWTH)

    price_change = _pct(current.price_usd, first.price_usd)
    if price_change is not None:
        score += _band(
            price_change,
            ((Decimal("60"), 14), (Decimal("25"), 12), (Decimal("10"), 9), (Decimal("3"), 5)),
        )
    if dex_price_change_5m is not None:
        score += _band(
            dex_price_change_5m,
            ((Decimal("30"), 6), (Decimal("10"), 4), (Decimal("3"), 2)),
        )

    velocity = score_velocity(score_history)
    if velocity is not None and velocity > 0:
        score += _band(velocity, ((Decimal("12"), 10), (Decimal("7"), 7), (Decimal("3"), 4)))
        if velocity >= Decimal("5") and len(score_history) >= 3:
            evidence.append(EVIDENCE_SCORE_VELOCITY)
    elif velocity is not None and velocity < Decimal("-3"):
        score -= Decimal("8")
    return _clamp(score), tuple(dict.fromkeys(evidence))


def score_velocity(history: Sequence[Decimal]) -> Decimal | None:
    """Average per-step change across the recent signal history."""

    values = [Decimal(str(item)) for item in history][-4:]
    if len(values) < 2:
        return None
    steps = [values[index + 1] - values[index] for index in range(len(values) - 1)]
    return (sum(steps, Decimal("0")) / Decimal(len(steps))).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Market quality
# ---------------------------------------------------------------------------


def liquidity_quality(
    *,
    first: RunnerMarketSnapshot,
    current: RunnerMarketSnapshot,
    config: RunnerQualityConfig = DEFAULT_QUALITY_CONFIG,
) -> tuple[Decimal | None, Decimal | None, tuple[str, ...], tuple[str, ...]]:
    """Depth relative to valuation and to the move, not an absolute dollar floor."""

    warnings: list[str] = []
    evidence: list[str] = []
    liquidity = current.liquidity_usd
    if liquidity is None:
        return None, None, (), ("liquidity is unavailable",)
    ratio = _ratio(liquidity, current.market_cap_usd)
    score = Decimal("40")
    if ratio is not None:
        if ratio >= Decimal("0.15"):
            score = Decimal("85")
        elif ratio >= Decimal("0.08"):
            score = Decimal("70")
        elif ratio >= config.fragile_liquidity_to_market_cap:
            score = Decimal("55")
        elif ratio >= config.min_liquidity_to_market_cap:
            score = Decimal("35")
        else:
            score = Decimal("12")
            warnings.append(
                f"liquidity is only {ratio * 100:.1f}% of market cap "
                f"({_short_money(liquidity)} vs {_short_money(current.market_cap_usd)})"
            )
        if ratio >= config.fragile_liquidity_to_market_cap:
            evidence.append(EVIDENCE_LIQUIDITY_RATIO)
    # Slippage is absolute, not relative.  A $2.5K pool can look healthy against
    # a $30K cap and still be moved several percent by a single $100 buy, so an
    # absolute-depth ceiling applies on top of the ratio.
    if liquidity < Decimal("5000"):
        score = min(score, Decimal("40"))
        warnings.append(f"only {_short_money(liquidity)} of liquidity backs the pair")
    elif liquidity < Decimal("10000"):
        score = min(score, Decimal("62"))
    change = _pct(liquidity, first.liquidity_usd)
    if change is not None:
        if change >= Decimal("10"):
            score += Decimal("12")
            evidence.append(EVIDENCE_LIQUIDITY_GROWTH)
        elif change <= Decimal("-25"):
            score -= Decimal("30")
            warnings.append(f"liquidity fell {change:.1f}% since first seen")
        elif change <= Decimal("-10"):
            score -= Decimal("12")
    mc_change = _pct(current.market_cap_usd, first.market_cap_usd)
    if (
        mc_change is not None
        and change is not None
        and mc_change >= Decimal("50")
        and change <= Decimal("2")
    ):
        score -= Decimal("18")
        warnings.append("market cap is expanding while liquidity stays flat")
    return (
        _clamp(score),
        (ratio.quantize(Decimal("0.0001")) if ratio is not None else None),
        tuple(dict.fromkeys(evidence)),
        tuple(dict.fromkeys(warnings)),
    )


def volume_quality(
    *,
    first: RunnerMarketSnapshot,
    current: RunnerMarketSnapshot,
    config: RunnerQualityConfig = DEFAULT_QUALITY_CONFIG,
) -> tuple[Decimal | None, Decimal | None, Decimal | None, tuple[str, ...]]:
    """Volume is only interesting relative to depth, valuation and new holders."""

    warnings: list[str] = []
    volume = current.volume_5m_usd
    to_liquidity = _ratio(volume, current.liquidity_usd)
    to_market_cap = _ratio(volume, current.market_cap_usd)
    if current.liquidity_usd is None and current.market_cap_usd is None:
        return None, None, None, ("volume cannot be scaled without liquidity or market cap",)
    score = Decimal("35")
    if to_liquidity is not None:
        if to_liquidity >= config.wash_volume_to_liquidity:
            score = Decimal("25")
            warnings.append(
                f"five-minute volume is {to_liquidity:.1f}x liquidity, which is more "
                "consistent with circular flow than with new demand"
            )
        elif to_liquidity >= Decimal("0.5"):
            score = Decimal("80")
        elif to_liquidity >= Decimal("0.15"):
            score = Decimal("65")
        elif to_liquidity >= Decimal("0.05"):
            score = Decimal("48")
        else:
            score = Decimal("28")
    # A ratio computed from four transactions is not evidence of anything.
    # Thin flow is capped rather than allowed to inherit a healthy-looking ratio.
    total_5m = current.buys_5m + current.sells_5m
    if total_5m < 6:
        score = min(score, Decimal("22"))
        warnings.append(f"only {total_5m} transactions in the five-minute window")
    elif total_5m < 12:
        score = min(score, Decimal("42"))
    volume_change = _pct(volume, first.volume_5m_usd)
    holder_change = (
        current.holder_count - first.holder_count
        if current.holder_count is not None and first.holder_count is not None
        else None
    )
    if (
        volume_change is not None
        and volume_change >= Decimal("100")
        and holder_change is not None
        and holder_change <= 2
    ):
        score -= Decimal("22")
        warnings.append(
            f"five-minute volume grew {volume_change:.0f}% while holders grew by "
            f"{holder_change}"
        )
    return (
        _clamp(score),
        (to_liquidity.quantize(Decimal("0.0001")) if to_liquidity is not None else None),
        (to_market_cap.quantize(Decimal("0.0001")) if to_market_cap is not None else None),
        tuple(dict.fromkeys(warnings)),
    )


def holder_quality(
    *,
    first: RunnerMarketSnapshot,
    current: RunnerMarketSnapshot,
    demand: RunnerDemandProfile,
) -> tuple[Decimal | None, tuple[str, ...], tuple[str, ...]]:
    """Reward real holder expansion; treat sybil-shaped distribution as noise."""

    if current.holder_count is None:
        return None, (), ("holder count is unavailable",)
    evidence: list[str] = []
    warnings: list[str] = []
    score = _band(
        Decimal(current.holder_count),
        ((Decimal("400"), 60), (Decimal("200"), 52), (Decimal("100"), 44), (Decimal("40"), 32)),
    )
    score = Decimal(score or 18)
    growth = (
        current.holder_count - first.holder_count if first.holder_count is not None else None
    )
    if growth is not None:
        elapsed_minutes = max(
            Decimal("0.5"),
            Decimal(max(1, current.captured_at - first.captured_at)) / Decimal("60"),
        )
        per_minute = Decimal(growth) / elapsed_minutes
        score += _band(
            per_minute,
            ((Decimal("20"), 30), (Decimal("8"), 22), (Decimal("3"), 14), (Decimal("1"), 7)),
        )
        if growth >= 8 or per_minute >= Decimal("3"):
            evidence.append(EVIDENCE_HOLDER_GROWTH)
        elif growth <= -5:
            score -= Decimal("15")
            warnings.append(f"holder count fell by {abs(growth)} since first seen")
    if (
        demand.independence_ratio is not None
        and demand.independence_ratio >= Decimal("0.6")
        and demand.traced_wallets >= 4
    ):
        evidence.append(EVIDENCE_LOW_CONCENTRATION)
    if current.top10_percent is not None and current.top10_percent > Decimal("45"):
        score -= Decimal("18")
    return _clamp(score), tuple(dict.fromkeys(evidence)), tuple(dict.fromkeys(warnings))


def price_quality(
    *,
    first: RunnerMarketSnapshot,
    current: RunnerMarketSnapshot,
    dex_price_change_5m: Decimal | None,
    config: RunnerQualityConfig = DEFAULT_QUALITY_CONFIG,
) -> tuple[Decimal | None, bool, tuple[str, ...], tuple[str, ...]]:
    """Prefer confirmed early acceleration over a vertical unconfirmed candle."""

    evidence: list[str] = []
    warnings: list[str] = []
    price_change = _pct(current.price_usd, first.price_usd)
    mc_change = _pct(current.market_cap_usd, first.market_cap_usd)
    move = price_change if price_change is not None else mc_change
    holder_change = (
        current.holder_count - first.holder_count
        if current.holder_count is not None and first.holder_count is not None
        else None
    )
    liquidity_change = _pct(current.liquidity_usd, first.liquidity_usd)
    overextended = bool(
        (price_change is not None and price_change >= config.overextended_price_percent)
        or (
            dex_price_change_5m is not None
            and dex_price_change_5m >= config.overextended_dex_5m_percent
        )
        or (
            mc_change is not None
            and mc_change >= config.overextended_price_percent * Decimal("1.5")
        )
    )
    if move is None:
        return None, overextended, (), ()
    confirmed = bool(
        (holder_change is not None and holder_change > 0)
        or (liquidity_change is not None and liquidity_change > 0)
    )
    if overextended:
        score = Decimal("20")
        warnings.append(f"move is already {move:.0f}% from first seen; late-chase risk")
    elif move >= config.parabolic_price_percent and not confirmed:
        score = Decimal("25")
        warnings.append(
            f"price expanded {move:.0f}% without holder or liquidity confirmation"
        )
    elif move >= Decimal("10") and confirmed:
        score = Decimal("80")
        evidence.append(EVIDENCE_PRICE_PROGRESSION)
    elif move >= Decimal("3"):
        score = Decimal("62")
    elif move > Decimal("-10"):
        score = Decimal("45")
    else:
        score = Decimal("22")
        warnings.append(f"price is {move:.0f}% below first seen")
    return _clamp(score), overextended, tuple(evidence), tuple(dict.fromkeys(warnings))


def _short_money(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


# ---------------------------------------------------------------------------
# Qualification
# ---------------------------------------------------------------------------


def _families(signals: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(EVIDENCE_FAMILIES.get(item, item) for item in signals))


def assess_runner_quality(
    *,
    first: RunnerMarketSnapshot,
    current: RunnerMarketSnapshot,
    history: Sequence[RunnerMarketSnapshot] = (),
    forensics: RunnerForensics | None = None,
    safety: RunnerSafetyAssessment | None = None,
    dex_price_change_5m: Decimal | None = None,
    score_history: Sequence[Decimal] = (),
    raw_smart_wallets: int = 0,
    independent_smart_clusters: int | None = None,
    age_seconds: int | None = None,
    hard_blockers: Sequence[str] = (),
    now: int = 0,
    config: RunnerQualityConfig = DEFAULT_QUALITY_CONFIG,
) -> RunnerQualityAssessment:
    """Produce the separated momentum / opportunity / stage decision at time ``now``.

    Only evidence available at ``now`` is used.  ``history`` must contain
    snapshots captured strictly before ``current``.
    """

    safety = safety or RunnerSafetyAssessment()
    ordered = tuple(sorted(history, key=lambda item: item.captured_at))
    prior = next(
        (item for item in reversed(ordered) if item.captured_at < current.captured_at),
        None,
    )
    demand = build_demand_profile(
        forensics=forensics,
        current=current,
        raw_smart_wallets=raw_smart_wallets,
        independent_smart_clusters=independent_smart_clusters,
    )
    organic, organic_warnings = organic_demand_score(demand, current=current, config=config)
    momentum, momentum_evidence = momentum_score(
        first=first,
        current=current,
        prior=prior,
        dex_price_change_5m=dex_price_change_5m,
        score_history=score_history,
    )
    liquidity, liquidity_ratio, liquidity_evidence, liquidity_warnings = liquidity_quality(
        first=first, current=current, config=config
    )
    volume, volume_to_liquidity, volume_to_market_cap, volume_warnings = volume_quality(
        first=first, current=current, config=config
    )
    holders, holder_evidence, holder_warnings = holder_quality(
        first=first, current=current, demand=demand
    )
    price, overextended, price_evidence, price_warnings = price_quality(
        first=first,
        current=current,
        dex_price_change_5m=dex_price_change_5m,
        config=config,
    )

    evidence: list[str] = [
        *momentum_evidence,
        *liquidity_evidence,
        *holder_evidence,
        *price_evidence,
    ]
    total_5m = current.buys_5m + current.sells_5m
    buy_ratio = _ratio(current.buys_5m, total_5m) or Decimal("0")
    # Smaller samples need higher purity before they count as breadth evidence.
    if (total_5m >= 20 and buy_ratio >= Decimal("0.60")) or (
        total_5m >= 12 and buy_ratio >= Decimal("0.65")
    ):
        evidence.append(EVIDENCE_BUYER_BREADTH)
    if demand.independent_smart_clusters >= 1 and demand.raw_smart_wallets >= 1:
        evidence.append(EVIDENCE_SMART_WALLETS)
    if (
        demand.estimated_independent_buyers is not None
        and demand.estimated_independent_buyers >= 5
        and (demand.independence_ratio or Decimal("0")) >= config.min_independence_ratio
    ):
        evidence.append(EVIDENCE_INDEPENDENT_BUYER_GROWTH)
    evidence = list(dict.fromkeys(evidence))
    families = _families(evidence)

    # Opportunity: organic demand dominates, market quality supports it.
    components: list[tuple[Decimal, Decimal]] = [(organic, Decimal("0.34"))]
    for value, weight in (
        (liquidity, Decimal("0.20")),
        (volume, Decimal("0.14")),
        (holders, Decimal("0.18")),
        (price, Decimal("0.14")),
    ):
        if value is not None:
            components.append((value, weight))
    weight_total = sum(weight for _value, weight in components)
    opportunity = (
        sum(value * weight for value, weight in components) / weight_total
        if weight_total > 0
        else Decimal("0")
    )
    # A setup with no measurable market quality at all cannot ride organic's
    # neutral 50 into qualification.
    if len(components) <= 2:
        opportunity -= Decimal("10")
    if overextended:
        # A vertical candle can still be worth watching, but it must never
        # outrank an early setup or reach entry quality on chart colour alone.
        opportunity = min(opportunity - Decimal("12"), config.heating_min_opportunity - 5)
    opportunity = _clamp(opportunity)

    warnings = tuple(
        dict.fromkeys(
            (
                *organic_warnings,
                *liquidity_warnings,
                *volume_warnings,
                *holder_warnings,
                *price_warnings,
            )
        )
    )

    catastrophic = bool(
        current.rugged
        or current.sell_route_status == "FAIL"
        or (current.liquidity_usd is not None and current.liquidity_usd < Decimal("2000"))
        or current.liquidity_usd is None
        or current.market_cap_usd is None
    )
    stale = bool(
        age_seconds is not None and age_seconds > config.max_qualified_age_seconds
    )
    # The veto is proportionate to the linked *mass*, not to the number of
    # groups: a single two-wallet coincidence must not block a candidate, and a
    # seventeen-wallet bundle must not be scored away as a mild penalty.
    coordination_veto = bool(
        (
            demand.independence_ratio is not None
            and demand.independence_ratio < config.min_independence_ratio
        )
        or (
            demand.cluster_supply_percent is not None
            and demand.cluster_supply_percent > config.max_cluster_supply_percent
        )
        or demand.time_linked_wallets >= config.veto_time_linked_wallets
    )

    evidence_ok = len(families) >= config.min_evidence_families
    base_route = evidence_ok and opportunity >= config.min_opportunity_score
    acceleration_route = (
        evidence_ok
        and momentum >= config.heating_min_momentum
        and opportunity >= config.min_opportunity_score - config.acceleration_route_discount
    )
    qualified = bool(
        (base_route or acceleration_route)
        and not catastrophic
        and not stale
        and not coordination_veto
        and not hard_blockers
    )

    if not qualified:
        stage = STAGE_SILENT_WATCH if not catastrophic else STAGE_RAW
    elif safety.status == "FAIL":
        stage = (
            STAGE_UNSAFE if momentum >= config.unsafe_min_momentum else STAGE_SILENT_WATCH
        )
    elif (
        safety.status == "PASS"
        and safety.entry_eligible
        and not overextended
        and opportunity >= config.strong_min_opportunity
        and momentum >= config.strong_min_momentum
    ):
        stage = STAGE_STRONG
    elif (
        safety.status == "PASS"
        and safety.entry_eligible
        and not overextended
        and opportunity >= config.entry_min_opportunity
        and momentum >= config.entry_min_momentum
    ):
        stage = STAGE_ENTRY
    elif (
        opportunity >= config.heating_min_opportunity
        and momentum >= config.heating_min_momentum
    ):
        stage = STAGE_HEATING
    else:
        stage = STAGE_QUALIFIED

    velocity = score_velocity(score_history)
    return RunnerQualityAssessment(
        momentum_score=momentum,
        opportunity_score=opportunity,
        organic_score=organic,
        liquidity_quality=liquidity,
        volume_quality=volume,
        holder_quality=holders,
        price_quality=price,
        stage=stage,
        qualified=qualified and stage in USER_FACING_STAGES,
        evidence=tuple(evidence),
        evidence_families=families,
        quality_warnings=warnings,
        score_velocity=velocity,
        liquidity_to_market_cap=liquidity_ratio,
        volume_to_liquidity=volume_to_liquidity,
        volume_to_market_cap=volume_to_market_cap,
        overextended=overextended,
        coordination_veto=coordination_veto,
        demand=demand,
        decision_version=QUALITY_DECISION_VERSION,
        evaluated_at=now,
    )


def why_surfaced(quality: RunnerQualityAssessment, *, limit: int = 5) -> tuple[str, ...]:
    """Two to five concise affirmative reasons the candidate reached the feed."""

    reasons: list[str] = []
    demand = quality.demand
    for signal in quality.evidence:
        if signal == EVIDENCE_HOLDER_GROWTH and demand.raw_buyers:
            reasons.append("holder growth confirmed")
        elif signal == EVIDENCE_SMART_WALLETS:
            reasons.append(
                f"{demand.independent_smart_clusters} independent smart-wallet cluster(s)"
            )
        elif signal == EVIDENCE_INDEPENDENT_BUYER_GROWTH:
            reasons.append(
                f"{demand.estimated_independent_buyers} independent buyer clusters of "
                f"{demand.raw_buyers} raw buyers"
            )
        elif signal == EVIDENCE_LIQUIDITY_RATIO and quality.liquidity_to_market_cap is not None:
            reasons.append(
                f"liquidity is {quality.liquidity_to_market_cap * 100:.0f}% of market cap"
            )
        elif signal == EVIDENCE_SCORE_VELOCITY and quality.score_velocity is not None:
            reasons.append(f"signal velocity {quality.score_velocity:+.0f}/observation")
        else:
            reasons.append(signal)
    if quality.organic_score >= Decimal("70"):
        reasons.append(f"organic-demand score {quality.organic_score:.0f}/100")
    return tuple(dict.fromkeys(reasons))[:limit]


def attention_rank_key(
    quality: RunnerQualityAssessment,
    *,
    safety: RunnerSafetyAssessment,
    age_seconds: int | None,
) -> tuple[Decimal, ...]:
    """Ranking for limited user attention: quality first, then acceleration.

    Freshness breaks ties in favor of the earlier setup, and a candidate whose
    safety evidence is actually complete outranks an equally interesting one
    whose safety is still unknown.
    """

    stage = Decimal(STAGE_RANK.get(quality.stage, 0))
    safety_confidence = (
        Decimal("2")
        if safety.status == "PASS"
        else Decimal("1")
        if safety.status == "UNKNOWN"
        else Decimal("0")
    )
    freshness = Decimal("0")
    if age_seconds is not None:
        freshness = _clamp(
            Decimal("100") - Decimal(min(age_seconds, 3_600)) / Decimal("36")
        )
    composite = (
        quality.opportunity_score * Decimal("0.45")
        + quality.momentum_score * Decimal("0.30")
        + quality.organic_score * Decimal("0.15")
        + freshness * Decimal("0.10")
    )
    return (stage, composite.quantize(Decimal("0.01")), safety_confidence, freshness)


def rank_for_attention(
    items: Sequence[tuple[RunnerQualityAssessment, RunnerSafetyAssessment, int | None, object]],
    *,
    limit: int,
) -> tuple[object, ...]:
    """Order ``(quality, safety, age, payload)`` rows and keep the best ``limit``."""

    ordered = sorted(
        items,
        key=lambda row: attention_rank_key(row[0], safety=row[1], age_seconds=row[2]),
        reverse=True,
    )
    return tuple(row[3] for row in ordered[:limit])


def merge_best_stage(previous: str | None, current: str) -> str:
    """Keep the highest funnel stage a candidate has ever reached."""

    if not previous:
        return current
    return current if STAGE_RANK.get(current, 0) >= STAGE_RANK.get(previous, 0) else previous


def with_stage(quality: RunnerQualityAssessment, stage: str) -> RunnerQualityAssessment:
    return replace(quality, stage=stage, qualified=stage in USER_FACING_STAGES)
