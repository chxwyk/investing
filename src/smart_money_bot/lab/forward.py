"""Forward-calibrated signal weighting and the current-edge score.

Sections 3, 15, 16, 17 and 18.

This is deliberately *not* a model.  The decision engine stays exactly where it
is; what this module produces is a bounded, auditable multiplier per signal
family, derived only from that family's own closed forward trades, plus a
current-edge score that ranks what is worth surfacing right now.

Four rules keep it honest:

* **No look-ahead.**  Only trades that closed at or before ``as_of`` are read,
  and every weight records the ``as_of`` it was computed at, so any ranking
  decision can be replayed exactly.
* **Small samples do nothing.**  Below :data:`MIN_SAMPLE` a family's weight is
  exactly neutral.  One coin doing 10x cannot move the ranking.
* **Shrinkage, not raw estimates.**  Above the floor, a family's expectancy is
  pulled toward the pooled mean by ``n / (n + K)``, so a family needs a genuine
  sample before it moves the weight much.
* **Bounded.**  A weight can never leave :data:`WEIGHT_FLOOR`..:data:`WEIGHT_CEILING`,
  and disabling a family outright needs a much larger sample than demoting it.

Weights only ever change *ranking, publication priority and shadow research
budget*.  They never touch a safety gate, an entry requirement or a cost floor.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .shadow import SIGNAL_FAMILIES
from .shadow_metrics import ShadowTradeRecord

ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")

#: The calibration contract version.  Persisted with every weight so an old
#: ranking decision stays attributable to the exact rules that produced it.
CALIBRATION_VERSION = "forward-calibration-v1"

#: Below this many closed trades a family's weight is exactly neutral.
MIN_SAMPLE = 10

#: Shrinkage constant.  At n == K a family gets half of its measured deviation.
SHRINKAGE_K = Decimal("20")

#: A family's weight can never leave these bounds, whatever the data says.
WEIGHT_FLOOR = Decimal("0.5")
WEIGHT_CEILING = Decimal("1.5")

#: Disabling a family in SHADOW is a much bigger claim than demoting it, so it
#: needs a much bigger sample and an unambiguous result.
DISABLE_MIN_SAMPLE = 25
DISABLE_MAX_EXPECTANCY = Decimal("-0.50")
DISABLE_MIN_SEVERE_FAILURE_PERCENT = Decimal("40")

#: Severe failure: a trade that closed on a rug, a safety failure or a liquidity
#: collapse.  These are the outcomes that make a family unusable regardless of
#: how its averages look.
SEVERE_CLOSE_REASONS: frozenset[str] = frozenset(
    {
        "SAFETY_DETERIORATION",
        "LIQUIDITY_COLLAPSE_EMERGENCY",
        "HARD_LOSS_PROTECTION",
    }
)

# --- weight verdicts ---------------------------------------------------------
VERDICT_INSUFFICIENT = "INSUFFICIENT_SAMPLE"
VERDICT_PROMOTED = "PROMOTED"
VERDICT_NEUTRAL = "NEUTRAL"
VERDICT_DEMOTED = "DEMOTED"
VERDICT_DISABLED = "DISABLED"

VERDICTS: tuple[str, ...] = (
    VERDICT_INSUFFICIENT,
    VERDICT_PROMOTED,
    VERDICT_NEUTRAL,
    VERDICT_DEMOTED,
    VERDICT_DISABLED,
)


@dataclass(frozen=True, slots=True)
class FamilyStats:
    """One family's closed forward record, as measured at ``as_of``."""

    family: str
    sample: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl_usd: Decimal = ZERO
    expectancy_usd: Decimal | None = None
    profit_factor: Decimal | None = None
    max_drawdown_usd: Decimal = ZERO
    severe_failures: int = 0
    average_mfe_percent: Decimal | None = None
    average_mae_percent: Decimal | None = None

    @property
    def severe_failure_percent(self) -> Decimal | None:
        if self.sample <= 0:
            return None
        return (Decimal(self.severe_failures) / Decimal(self.sample) * HUNDRED).quantize(CENT)

    @property
    def win_rate_percent(self) -> Decimal | None:
        if self.sample <= 0:
            return None
        return (Decimal(self.wins) / Decimal(self.sample) * HUNDRED).quantize(CENT)


@dataclass(frozen=True, slots=True)
class FamilyWeight:
    """A bounded, auditable, replayable ranking multiplier for one family."""

    family: str
    weight: Decimal = ONE
    verdict: str = VERDICT_INSUFFICIENT
    sample: int = 0
    raw_expectancy_usd: Decimal | None = None
    shrunk_expectancy_usd: Decimal | None = None
    pooled_expectancy_usd: Decimal | None = None
    shrinkage: Decimal = ZERO
    severe_failure_percent: Decimal | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)
    as_of: int = 0
    calibration_version: str = CALIBRATION_VERSION

    @property
    def enabled(self) -> bool:
        """Whether SHADOW should still take this family's signals."""

        return self.verdict != VERDICT_DISABLED

    @property
    def audit(self) -> dict[str, str]:
        """Everything needed to re-derive this weight by hand."""

        return {
            "FAMILY": self.family,
            "WEIGHT": str(self.weight),
            "VERDICT": self.verdict,
            "SAMPLE": str(self.sample),
            "RAW_EXPECTANCY": _text(self.raw_expectancy_usd),
            "SHRUNK_EXPECTANCY": _text(self.shrunk_expectancy_usd),
            "POOLED_EXPECTANCY": _text(self.pooled_expectancy_usd),
            "SHRINKAGE": str(self.shrinkage),
            "SEVERE_FAILURE_PERCENT": _text(self.severe_failure_percent),
            "AS_OF": str(self.as_of),
            "CALIBRATION_VERSION": self.calibration_version,
        }


def measure_families(
    trades: Sequence[ShadowTradeRecord],
    *,
    as_of: int,
) -> dict[str, FamilyStats]:
    """Per-family stats from closed trades only, none of them after ``as_of``."""

    closed = [
        trade
        for trade in trades
        if not trade.open and trade.closed_at is not None and trade.closed_at <= as_of
    ]
    stats: dict[str, FamilyStats] = {}
    for family in SIGNAL_FAMILIES:
        cohort = [trade for trade in closed if trade.family == family]
        if not cohort:
            continue
        nets = [trade.net_pnl_usd for trade in cohort]
        wins = [value for value in nets if value > 0]
        losses = [value for value in nets if value <= 0]
        loss_total = sum((abs(value) for value in losses), ZERO)

        equity = ZERO
        peak = ZERO
        drawdown = ZERO
        for trade in sorted(cohort, key=lambda item: item.closed_at or 0):
            equity += trade.net_pnl_usd
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)

        stats[family] = FamilyStats(
            family=family,
            sample=len(cohort),
            wins=len(wins),
            losses=len(losses),
            net_pnl_usd=sum(nets, ZERO).quantize(Decimal("0.000001")),
            expectancy_usd=(sum(nets, ZERO) / Decimal(len(cohort))).quantize(
                Decimal("0.000001")
            ),
            profit_factor=(
                (sum(wins, ZERO) / loss_total).quantize(CENT) if loss_total > 0 else None
            ),
            max_drawdown_usd=drawdown.quantize(Decimal("0.000001")),
            severe_failures=sum(
                1 for trade in cohort if trade.close_reason in SEVERE_CLOSE_REASONS
            ),
            average_mfe_percent=_mean([trade.max_favourable_percent for trade in cohort]),
            average_mae_percent=_mean([trade.max_adverse_percent for trade in cohort]),
        )
    return stats


# --- v2.44 evidence cohorts (section 36) -------------------------------------
# The families above answer "which lane found it".  These answer a different
# and, for this release, more pointed question: **do the new signals actually
# help?**  A holder-expansion promotion and a known-trader promotion are both
# EARLY_PROMOTION, and if only one of them makes money we need to be able to
# see that rather than average them together.
#
# Every label is derived from evidence that existed *at entry*, so a cohort can
# never be assigned with hindsight.

COHORT_NO_KNOWN_TRADER = "NO_KNOWN_TRADER"
COHORT_ONE_KNOWN_TRADER = "ONE_KNOWN_TRADER"
COHORT_MULTI_KNOWN_TRADER = "TWO_PLUS_INDEPENDENT_KNOWN_TRADERS"
COHORT_HOLDER_EXPANSION = "HOLDER_EXPANSION"
COHORT_CONCENTRATION_IMPROVING = "CONCENTRATION_IMPROVING"
COHORT_DEV_DISTRIBUTING = "DEV_DISTRIBUTING"
COHORT_FRESH_WALLET_CLUSTER = "FRESH_WALLET_CLUSTER"
COHORT_STORY_AND_TRADER = "STORY_PLUS_TRADER"
COHORT_THESIS_AND_TRADER = "THESIS_PLUS_TRADER"
COHORT_TRENDING_AND_TRADER = "TRENDING_PLUS_TRADER"

EVIDENCE_COHORTS: tuple[str, ...] = (
    COHORT_NO_KNOWN_TRADER,
    COHORT_ONE_KNOWN_TRADER,
    COHORT_MULTI_KNOWN_TRADER,
    COHORT_HOLDER_EXPANSION,
    COHORT_CONCENTRATION_IMPROVING,
    COHORT_DEV_DISTRIBUTING,
    COHORT_FRESH_WALLET_CLUSTER,
    COHORT_STORY_AND_TRADER,
    COHORT_THESIS_AND_TRADER,
    COHORT_TRENDING_AND_TRADER,
)


def assign_cohorts(
    *,
    proven_independent_traders: int = 0,
    holder_expansion: bool = False,
    concentration_trend: str = "",
    dev_posture: str = "",
    fresh_wallet_cluster: bool = False,
    story_confirmed: bool = False,
    thesis_confirmed: bool = False,
    trending_confirmed: bool = False,
) -> tuple[str, ...]:
    """Label one entry by the evidence that was present when it was taken.

    A candidate lands in several cohorts at once on purpose — "two known traders"
    and "story plus trader" are different questions about the same trade, and
    forcing a single bucket would make both unanswerable.
    """

    labels: list[str] = []
    if proven_independent_traders <= 0:
        labels.append(COHORT_NO_KNOWN_TRADER)
    elif proven_independent_traders == 1:
        labels.append(COHORT_ONE_KNOWN_TRADER)
    else:
        labels.append(COHORT_MULTI_KNOWN_TRADER)
    if holder_expansion:
        labels.append(COHORT_HOLDER_EXPANSION)
    # "IMPROVING" is the holder modules' own value; see
    # ``lab.promotion`` for why it is restated rather than imported.
    if concentration_trend == "IMPROVING":
        labels.append(COHORT_CONCENTRATION_IMPROVING)
    if dev_posture in {"DEV_HOLDING_SELLING", "DEV_HOLDING_EXITED"}:
        labels.append(COHORT_DEV_DISTRIBUTING)
    if fresh_wallet_cluster:
        labels.append(COHORT_FRESH_WALLET_CLUSTER)
    # The combination cohorts require a known trader *and* the other evidence:
    # a story on its own is already measured by the STORY family.
    if proven_independent_traders > 0:
        if story_confirmed:
            labels.append(COHORT_STORY_AND_TRADER)
        if thesis_confirmed:
            labels.append(COHORT_THESIS_AND_TRADER)
        if trending_confirmed:
            labels.append(COHORT_TRENDING_AND_TRADER)
    return tuple(dict.fromkeys(labels))


def measure_cohorts(
    trades: Sequence[ShadowTradeRecord],
    *,
    cohorts: Mapping[str, Sequence[str]],
    as_of: int,
) -> dict[str, FamilyStats]:
    """Forward record per evidence cohort, using the same maths as the families.

    ``cohorts`` maps a ``position_id`` to the labels assigned at entry.  A trade
    with no labels is simply absent: guessing a cohort after the fact is exactly
    the hindsight this measurement exists to avoid.
    """

    closed = [
        trade
        for trade in trades
        if not trade.open and trade.closed_at is not None and trade.closed_at <= as_of
    ]
    stats: dict[str, FamilyStats] = {}
    for cohort in EVIDENCE_COHORTS:
        members = [
            trade for trade in closed if cohort in tuple(cohorts.get(trade.position_id, ()))
        ]
        if not members:
            continue
        stats[cohort] = _cohort_stats(cohort, members)
    return stats


def _cohort_stats(name: str, members: Sequence[ShadowTradeRecord]) -> FamilyStats:
    nets = [trade.net_pnl_usd for trade in members]
    wins = [value for value in nets if value > 0]
    losses = [value for value in nets if value <= 0]
    loss_total = sum((abs(value) for value in losses), ZERO)

    equity = ZERO
    peak = ZERO
    drawdown = ZERO
    for trade in sorted(members, key=lambda item: item.closed_at or 0):
        equity += trade.net_pnl_usd
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)

    return FamilyStats(
        family=name,
        sample=len(members),
        wins=len(wins),
        losses=len(losses),
        net_pnl_usd=sum(nets, ZERO).quantize(Decimal("0.000001")),
        expectancy_usd=(sum(nets, ZERO) / Decimal(len(members))).quantize(
            Decimal("0.000001")
        ),
        profit_factor=(
            (sum(wins, ZERO) / loss_total).quantize(CENT) if loss_total > 0 else None
        ),
        max_drawdown_usd=drawdown.quantize(Decimal("0.000001")),
        severe_failures=sum(
            1 for trade in members if trade.close_reason in SEVERE_CLOSE_REASONS
        ),
        average_mfe_percent=_mean([trade.max_favourable_percent for trade in members]),
        average_mae_percent=_mean([trade.max_adverse_percent for trade in members]),
    )


def calibrate_families(
    trades: Sequence[ShadowTradeRecord],
    *,
    as_of: int,
) -> dict[str, FamilyWeight]:
    """Turn forward results into bounded ranking weights (sections 3, 15, 16).

    Every family present in the data gets a weight; families with too little
    evidence get exactly ``1.0`` and say so, which is what stops a lucky coin
    from rewriting the ranking.
    """

    stats = measure_families(trades, as_of=as_of)
    measured = [item for item in stats.values() if item.expectancy_usd is not None]
    total_sample = sum(item.sample for item in measured)
    pooled = (
        (
            sum((item.expectancy_usd or ZERO) * item.sample for item in measured)
            / Decimal(total_sample)
        ).quantize(Decimal("0.000001"))
        if total_sample > 0
        else ZERO
    )

    weights: dict[str, FamilyWeight] = {}
    for family, item in stats.items():
        weights[family] = _weigh(item, pooled=pooled, as_of=as_of)
    return weights


def _weigh(stats: FamilyStats, *, pooled: Decimal, as_of: int) -> FamilyWeight:
    reasons: list[str] = []
    if stats.sample < MIN_SAMPLE or stats.expectancy_usd is None:
        return FamilyWeight(
            family=stats.family,
            weight=ONE,
            verdict=VERDICT_INSUFFICIENT,
            sample=stats.sample,
            raw_expectancy_usd=stats.expectancy_usd,
            pooled_expectancy_usd=pooled,
            severe_failure_percent=stats.severe_failure_percent,
            reasons=(
                f"only {stats.sample} closed trades; {MIN_SAMPLE} needed before "
                "forward results may move the ranking",
            ),
            as_of=as_of,
        )

    # Shrink the measured expectancy toward the pooled mean.  A family with 10
    # trades keeps a third of its deviation; one with 60 keeps three quarters.
    n = Decimal(stats.sample)
    shrinkage = (n / (n + SHRINKAGE_K)).quantize(Decimal("0.0001"))
    raw = stats.expectancy_usd
    shrunk = (pooled + (raw - pooled) * shrinkage).quantize(Decimal("0.000001"))

    severe = stats.severe_failure_percent or ZERO
    if (
        stats.sample >= DISABLE_MIN_SAMPLE
        and shrunk <= DISABLE_MAX_EXPECTANCY
        and severe >= DISABLE_MIN_SEVERE_FAILURE_PERCENT
    ):
        return FamilyWeight(
            family=stats.family,
            weight=WEIGHT_FLOOR,
            verdict=VERDICT_DISABLED,
            sample=stats.sample,
            raw_expectancy_usd=raw,
            shrunk_expectancy_usd=shrunk,
            pooled_expectancy_usd=pooled,
            shrinkage=shrinkage,
            severe_failure_percent=severe,
            reasons=(
                f"{stats.sample} closed trades, shrunk expectancy {shrunk} and "
                f"{severe}% severe failures — this family loses money and rugs",
            ),
            as_of=as_of,
        )

    # A $10 stake makes a dollar of expectancy a 10% edge, so scale the
    # deviation by the stake to keep the multiplier in a sane range.
    deviation = shrunk - pooled
    weight = (ONE + deviation / Decimal("10")).quantize(Decimal("0.0001"))

    if severe >= Decimal("25"):
        weight *= Decimal("0.8")
        reasons.append(f"{severe}% of closed trades ended in a rug or safety failure")
    if stats.profit_factor is not None and stats.profit_factor < ONE:
        weight *= Decimal("0.9")
        reasons.append(f"profit factor {stats.profit_factor} is below 1")

    weight = max(WEIGHT_FLOOR, min(WEIGHT_CEILING, weight)).quantize(Decimal("0.0001"))
    if weight > ONE:
        verdict = VERDICT_PROMOTED
        reasons.insert(0, f"shrunk expectancy {shrunk} beats the pooled {pooled}")
    elif weight < ONE:
        verdict = VERDICT_DEMOTED
        reasons.insert(0, f"shrunk expectancy {shrunk} trails the pooled {pooled}")
    else:
        verdict = VERDICT_NEUTRAL
        reasons.insert(0, "forward results are indistinguishable from the pool")

    return FamilyWeight(
        family=stats.family,
        weight=weight,
        verdict=verdict,
        sample=stats.sample,
        raw_expectancy_usd=raw,
        shrunk_expectancy_usd=shrunk,
        pooled_expectancy_usd=pooled,
        shrinkage=shrinkage,
        severe_failure_percent=severe,
        reasons=tuple(reasons),
        as_of=as_of,
    )


def family_weight(
    weights: Mapping[str, FamilyWeight],
    family: str,
) -> Decimal:
    """The multiplier for one family; neutral when nothing is known about it."""

    entry = weights.get(family)
    return entry.weight if entry is not None else ONE


def family_enabled(weights: Mapping[str, FamilyWeight], family: str) -> bool:
    entry = weights.get(family)
    return entry.enabled if entry is not None else True


@dataclass(frozen=True, slots=True)
class EdgeInputs:
    """Current evidence for the forward-edge score (section 17)."""

    family: str = ""
    #: How actionable the setup is *now* — not how interesting it once was.
    actionability_score: Decimal | None = None
    freshness_seconds: int | None = None
    expected_net_edge_percent: Decimal | None = None
    independent_buyers: int | None = None
    liquidity_usd: Decimal | None = None
    route_price_impact_percent: Decimal | None = None
    route_available: bool = True
    catalyst_confidence: str = ""
    notable_lead_percent: Decimal | None = None
    organic_score: Decimal | None = None
    momentum_score: Decimal | None = None
    #: The historical opportunity score.  Deliberately capped in influence.
    historical_opportunity_score: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ForwardEdge:
    """What is worth surfacing right now, and why."""

    score: Decimal = ZERO
    family_weight: Decimal = ONE
    components: Mapping[str, Decimal] = field(default_factory=dict)
    reasons: tuple[str, ...] = field(default_factory=tuple)
    blockers: tuple[str, ...] = field(default_factory=tuple)

    @property
    def actionable(self) -> bool:
        return self.score >= 55 and not self.blockers


def forward_edge_score(
    inputs: EdgeInputs,
    *,
    weights: Mapping[str, FamilyWeight] | None = None,
) -> ForwardEdge:
    """Rank by *current* edge, weighted by the family's forward record.

    Historical opportunity contributes at most 10 of 100 points on purpose:
    section 17 requires that a high past score cannot keep a spent setup ranked
    beside a fresh one.
    """

    components: dict[str, Decimal] = {}
    reasons: list[str] = []
    blockers: list[str] = []

    if not inputs.route_available:
        blockers.append("no usable route")
    if inputs.liquidity_usd is not None and inputs.liquidity_usd <= 0:
        blockers.append("no liquidity")

    action = inputs.actionability_score
    if action is not None:
        components["actionability"] = (action * Decimal("0.30")).quantize(CENT)
        if action >= 70:
            reasons.append("still actionable right now")
    else:
        components["actionability"] = Decimal("9")

    fresh = inputs.freshness_seconds
    if fresh is not None:
        points = (
            Decimal("15")
            if fresh <= 300
            else Decimal("10")
            if fresh <= 900
            else Decimal("4")
            if fresh <= 3_600
            else ZERO
        )
        components["freshness"] = points
        if points >= 10:
            reasons.append("signal is still fresh")
    else:
        components["freshness"] = Decimal("4")

    edge = inputs.expected_net_edge_percent
    if edge is not None:
        components["net_edge"] = max(ZERO, min(Decimal("20"), edge / 2)).quantize(CENT)
        if edge >= 20:
            reasons.append(f"expected NET edge {edge}%")
    else:
        components["net_edge"] = ZERO

    buyers = inputs.independent_buyers
    if buyers is not None:
        components["independent_buyers"] = min(Decimal("10"), Decimal(buyers) / 3).quantize(
            CENT
        )
        if buyers >= 20:
            reasons.append(f"{buyers} independently funded buyers")
    else:
        components["independent_buyers"] = ZERO

    impact = inputs.route_price_impact_percent
    if impact is not None:
        components["route_quality"] = max(
            ZERO, Decimal("10") - impact * 2
        ).quantize(CENT)
        if impact <= 1:
            reasons.append("clean executable route")
    else:
        components["route_quality"] = Decimal("3")

    if inputs.catalyst_confidence in {"CONFIRMED", "HIGH"}:
        components["catalyst"] = Decimal("8")
        reasons.append(f"{inputs.catalyst_confidence.lower()} external catalyst")
    elif inputs.catalyst_confidence:
        components["catalyst"] = Decimal("3")
    else:
        components["catalyst"] = ZERO

    lead = inputs.notable_lead_percent
    if lead is not None and lead > 0:
        components["notable_lead"] = min(Decimal("7"), lead / 5).quantize(CENT)
        reasons.append(f"tracked wallet entered {lead}% earlier")
    else:
        components["notable_lead"] = ZERO

    organic = inputs.organic_score
    momentum = inputs.momentum_score
    flow = [value for value in (organic, momentum) if value is not None]
    components["flow"] = (
        (sum(flow, ZERO) / Decimal(len(flow)) * Decimal("0.10")).quantize(CENT)
        if flow
        else ZERO
    )

    historical = inputs.historical_opportunity_score
    components["historical"] = (
        min(Decimal("10"), historical * Decimal("0.10")).quantize(CENT)
        if historical is not None
        else ZERO
    )

    base = sum(components.values(), ZERO)
    weight = family_weight(weights or {}, inputs.family)
    score = max(ZERO, min(HUNDRED, base * weight)).quantize(CENT)
    if weight > ONE:
        reasons.append(f"{inputs.family.replace('_', ' ')} has a positive forward record")
    elif weight < ONE:
        reasons.append(f"{inputs.family.replace('_', ' ')} is demoted on forward results")

    return ForwardEdge(
        score=score,
        family_weight=weight,
        components=components,
        reasons=tuple(reasons),
        blockers=tuple(blockers),
    )


# --- urgent ping policy (section 18) -----------------------------------------
PING_MIN_EDGE = Decimal("70")
PING_MIN_INDEPENDENT_CONFIRMATIONS = 2


@dataclass(frozen=True, slots=True)
class PingVerdict:
    ping: bool = False
    reasons: tuple[str, ...] = field(default_factory=tuple)
    blockers: tuple[str, ...] = field(default_factory=tuple)


def should_ping(
    edge: ForwardEdge,
    *,
    family: str,
    independent_confirmations: int = 0,
    still_early: bool = True,
    move_already_made_percent: Decimal | None = None,
    weights: Mapping[str, FamilyWeight] | None = None,
) -> PingVerdict:
    """Interrupt a person only for a high-edge, early, confirmed setup.

    Volume, a famous wallet or one viral post are explicitly not enough, and a
    family the forward data has demoted cannot ping at all — which is the whole
    point of measuring families in the first place.
    """

    reasons: list[str] = []
    blockers: list[str] = []

    if edge.blockers:
        blockers.extend(edge.blockers)
    if edge.score < PING_MIN_EDGE:
        blockers.append(f"current edge {edge.score} below the {PING_MIN_EDGE} ping floor")
    if not still_early:
        blockers.append("the move has already happened")
    if move_already_made_percent is not None and move_already_made_percent >= Decimal("120"):
        blockers.append(f"already {move_already_made_percent}% above first seen")
    if independent_confirmations < PING_MIN_INDEPENDENT_CONFIRMATIONS:
        blockers.append(
            f"only {independent_confirmations} independent confirmation(s); "
            f"{PING_MIN_INDEPENDENT_CONFIRMATIONS} required"
        )

    entry = (weights or {}).get(family)
    if entry is not None and entry.verdict in {VERDICT_DEMOTED, VERDICT_DISABLED}:
        blockers.append(
            f"{family.replace('_', ' ')} is {entry.verdict.lower()} on forward results"
        )
    elif entry is not None and entry.verdict == VERDICT_PROMOTED:
        reasons.append(f"{family.replace('_', ' ')} has proven forward usefulness")

    if not blockers:
        reasons.extend(edge.reasons[:3])
        reasons.append(f"{independent_confirmations} independent confirmations")
    return PingVerdict(ping=not blockers, reasons=tuple(reasons), blockers=tuple(blockers))


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return (sum(values, ZERO) / Decimal(len(values))).quantize(CENT)


def _text(value: Decimal | None) -> str:
    return "" if value is None else str(value)
