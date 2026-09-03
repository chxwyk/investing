"""Catching the climb from a few real holders toward fifty, not waiting for 265.

The operator's actual complaint about this lane, stated plainly: they saw SOL on
INDA near $30K and had no way to know whether it was real, and by the time the
board showed it leading at +7,081% the trade was somebody else's.  A system that
only speaks once a token has 265 holders is a system that only ever describes
history.

So the ladder here is about *change*, and it is deliberately four rungs rather
than one bar:

    Stage 0  VERIFIED STOCK LAUNCH   identity and anchor proven. No holder
                                     minimum at all — this is the earliest
                                     honest thing that can be said.
    Stage 1  HOLDER SPARK            real people are arriving. High risk, and
                                     the label says so.
    Stage 2  TRACTION CONFIRMED      the arrival held up and broadened.
    Stage 3  ENTRY CANDIDATE         every hard gate, plus depth of ownership.

Stage 1 exists precisely so the operator sees something at twenty-five holders
instead of nothing until a hundred.  It is not an entry claim and never renders
as one.

Two rules shape everything below.

**Velocity beats level.**  Twenty-five holders reached in ninety seconds and
twenty-five reached over a day are different tokens; a threshold on the count
alone cannot tell them apart, so the ladder asks for growth as well as size.

**Age changes the question.**  At two minutes the useful evidence is a working
sell route and the first independent buyers.  At an hour, cumulative volume
proves nothing and only retention and broadening ownership do.  A single fixed
rule applied at both ages is wrong at one of them.

Pure logic: no provider, no RPC, no database, no signer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal("0")
HUNDRED = Decimal("100")
CENT = Decimal("0.01")

# --- stages -------------------------------------------------------------------
STAGE_0_VERIFIED = "VERIFIED_STOCK_LAUNCH"
STAGE_1_SPARK = "HOLDER_SPARK_HIGH_RISK_WATCH"
STAGE_2_TRACTION = "TRACTION_CONFIRMED"
STAGE_3_ENTRY = "STONKS_ENTRY_CANDIDATE"

LADDER: tuple[str, ...] = (STAGE_0_VERIFIED, STAGE_1_SPARK, STAGE_2_TRACTION, STAGE_3_ENTRY)

LADDER_LABEL: dict[str, str] = {
    STAGE_0_VERIFIED: "🟡 VERIFIED STOCK LAUNCH — RESEARCH\nUNPRICED / TOO EARLY FOR ENTRY",
    STAGE_1_SPARK: "🟠 HOLDER SPARK — HIGH RISK WATCH",
    STAGE_2_TRACTION: "🔵 TRACTION CONFIRMED — RESEARCH",
    STAGE_3_ENTRY: "🟢 STONKS ENTRY CANDIDATE — RESEARCH",
}

# --- stage 4 outcomes, all inspectable even when they do not ping -------------
OUT_UNRESOLVED = "UNRESOLVED_NOT_STOCK_LINKED"
OUT_NO_TRACTION = "VERIFIED_NO_TRACTION_YET"
OUT_SELL_EVIDENCE = "SELL_EVIDENCE_INSUFFICIENT"
OUT_HOLDER_CONFLICT = "HOLDER_DATA_CONFLICT"
OUT_UNSAFE = "UNSAFE_MOMENTUM"
OUT_WASH = "WASH_CLUSTERED_ACTIVITY"
OUT_DISTRIBUTION = "DISTRIBUTION"
OUT_LATE = "LATE_EXTENDED_DO_NOT_CHASE"
OUT_NO_ENTRY = "SAFE_NO_ENTRY_NOW"
OUT_INVALIDATED = "INVALIDATED"

OUTCOMES: tuple[str, ...] = (
    OUT_UNRESOLVED, OUT_NO_TRACTION, OUT_SELL_EVIDENCE, OUT_HOLDER_CONFLICT,
    OUT_UNSAFE, OUT_WASH, OUT_DISTRIBUTION, OUT_LATE, OUT_NO_ENTRY, OUT_INVALIDATED,
)

HUMAN_OUTCOME: dict[str, str] = {
    OUT_UNRESOLVED: "no address-level link to a Robinhood Stock Token",
    OUT_NO_TRACTION: "verified, and nobody has arrived yet",
    OUT_SELL_EVIDENCE: "not enough independent sellers to show anyone can exit",
    OUT_HOLDER_CONFLICT: "a provider's holder count contradicts the chain ledger",
    OUT_UNSAFE: "it is moving and we cannot show that it is safe",
    OUT_WASH: "the activity is clustered or recycled rather than organic",
    OUT_DISTRIBUTION: "creator, insider or large holders are selling into it",
    OUT_LATE: "the move already happened — do not chase it",
    OUT_NO_ENTRY: "safe and real, with no edge left to take",
    OUT_INVALIDATED: "the reason to be here is gone",
}

#: Milestones the card reports the time to, because how fast is the question.
MILESTONES: tuple[int, ...] = (10, 25, 50, 100, 250)


@dataclass(frozen=True, slots=True)
class TractionConfig:
    """The ladder's rungs.  Every one is configuration, not a buried literal.

    Defaults match the specification. They are starting points to calibrate
    against forward outcomes, which is why they live here rather than inline.
    """

    scout_min_economic_holders: int = 25
    scout_min_independent_buyers: int = 10
    scout_min_independent_sellers: int = 2
    scout_min_liquidity_usd: Decimal = Decimal("10000")
    scout_max_cluster_top10: Decimal = Decimal("0.70")

    traction_min_economic_holders: int = 50
    traction_min_independent_buyers: int = 20
    traction_min_independent_sellers: int = 5
    traction_min_holder_delta_5m: int = 10
    traction_min_holder_growth_pct_15m: Decimal = Decimal("25")
    traction_min_liquidity_usd: Decimal = Decimal("15000")
    traction_max_cluster_top10: Decimal = Decimal("0.60")

    entry_min_economic_holders: int = 100
    entry_min_independent_buyers: int = 30
    entry_min_independent_sellers: int = 8
    entry_min_liquidity_usd: Decimal = Decimal("20000")
    entry_max_cluster_top10: Decimal = Decimal("0.40")

    #: Under this age a token may substitute a verified sell route for sell
    #: history, and the card must say SELL HISTORY FORMING.
    sell_history_grace_seconds: int = 180
    #: Past this, cumulative volume proves nothing and only retention does.
    mature_age_seconds: int = 3_600


DEFAULT_TRACTION_CONFIG = TractionConfig()


@dataclass(frozen=True, slots=True)
class HolderSnapshot:
    """One immutable reading of the ledger.  Never rewritten by a later one."""

    at: int
    block_number: int | None = None
    economic_holders: int = 0
    raw_holders: int = 0
    independent_buyers: int = 0
    independent_sellers: int = 0
    liquidity_usd: Decimal | None = None
    cluster_adjusted_top10: Decimal | None = None
    volume_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class TractionMetrics:
    """Velocity, milestones and retention, derived from a snapshot series."""

    holder_delta_5m: int | None = None
    holder_delta_15m: int | None = None
    holder_growth_pct_15m: Decimal | None = None
    holder_velocity_per_minute: Decimal | None = None
    buyer_delta_5m: int | None = None
    volume_acceleration: Decimal | None = None
    liquidity_stable: bool | None = None
    milestone_seconds: dict[int, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {
            "holder_delta_5m": self.holder_delta_5m,
            "holder_delta_15m": self.holder_delta_15m,
            "holder_growth_pct_15m": _s(self.holder_growth_pct_15m),
            "holder_velocity_per_minute": _s(self.holder_velocity_per_minute),
            "buyer_delta_5m": self.buyer_delta_5m,
            "volume_acceleration": _s(self.volume_acceleration),
            "liquidity_stable": self.liquidity_stable,
            "time_to_holders": {str(k): v for k, v in sorted(self.milestone_seconds.items())},
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def measure(
    snapshots: Sequence[HolderSnapshot],
    *,
    launched_at: int | None = None,
    now: int,
) -> TractionMetrics:
    """Turn a snapshot series into velocity.  Missing history stays ``None``.

    ``None`` is used throughout rather than zero, because "we have not been
    watching long enough" and "nothing happened" are different findings and
    only one of them is a reason to refuse.
    """

    if not snapshots:
        return TractionMetrics()
    ordered = sorted(snapshots, key=lambda item: item.at)
    latest = ordered[-1]

    def at_or_before(seconds: int) -> HolderSnapshot | None:
        cutoff = now - seconds
        earlier = [item for item in ordered if item.at <= cutoff]
        return earlier[-1] if earlier else None

    five = at_or_before(300)
    fifteen = at_or_before(900)

    growth_pct: Decimal | None = None
    if fifteen is not None and fifteen.economic_holders > 0:
        growth_pct = (
            (Decimal(latest.economic_holders - fifteen.economic_holders)
             / Decimal(fifteen.economic_holders)) * HUNDRED
        ).quantize(CENT)

    velocity: Decimal | None = None
    span = latest.at - ordered[0].at
    if span > 0:
        velocity = (
            Decimal(latest.economic_holders - ordered[0].economic_holders)
            * Decimal(60) / Decimal(span)
        ).quantize(CENT)

    acceleration: Decimal | None = None
    if (
        five is not None
        and five.volume_usd
        and five.volume_usd > ZERO
        and latest.volume_usd is not None
    ):
        acceleration = (latest.volume_usd / five.volume_usd).quantize(CENT)

    stable: bool | None = None
    window = [item for item in ordered if now - item.at <= 300 and item.liquidity_usd is not None]
    if len(window) >= 2:
        first, last = window[0].liquidity_usd, window[-1].liquidity_usd
        stable = bool(first and last and last >= first * Decimal("0.8"))

    milestones: dict[int, int] = {}
    if launched_at is not None:
        for target in MILESTONES:
            reached = next(
                (item for item in ordered if item.economic_holders >= target), None
            )
            if reached is not None:
                milestones[target] = max(0, reached.at - launched_at)

    return TractionMetrics(
        holder_delta_5m=(
            latest.economic_holders - five.economic_holders if five is not None else None
        ),
        holder_delta_15m=(
            latest.economic_holders - fifteen.economic_holders if fifteen is not None else None
        ),
        holder_growth_pct_15m=growth_pct,
        holder_velocity_per_minute=velocity,
        buyer_delta_5m=(
            latest.independent_buyers - five.independent_buyers if five is not None else None
        ),
        volume_acceleration=acceleration,
        liquidity_stable=stable,
        milestone_seconds=milestones,
    )


# --- the explainable ranking score, applied only after verification -----------
SCORE_WEIGHTS: dict[str, Decimal] = {
    "holder velocity and retention": Decimal("25"),
    "independent buyer velocity": Decimal("20"),
    "organic two-sided flow": Decimal("15"),
    "volume acceleration": Decimal("15"),
    "liquidity level and stability": Decimal("15"),
    "distribution quality": Decimal("10"),
}


def traction_score(
    snapshot: HolderSnapshot,
    metrics: TractionMetrics,
    *,
    organic: bool | None = None,
    config: TractionConfig = DEFAULT_TRACTION_CONFIG,
) -> tuple[Decimal, tuple[tuple[str, str], ...]]:
    """Rank verified watches against each other.  It cannot open a gate.

    Only ever used to order candidates that have already proven identity and
    anchor; there is no path from this number to a classification.
    """

    def ramp(value: Decimal | None, target: Decimal) -> Decimal:
        if value is None or target <= ZERO:
            return ZERO
        return min(Decimal("1"), max(ZERO, value / target))

    parts: list[tuple[str, Decimal]] = [
        (
            "holder velocity and retention",
            ramp(metrics.holder_velocity_per_minute, Decimal("5")),
        ),
        ("independent buyer velocity", ramp(
            None if metrics.buyer_delta_5m is None else Decimal(metrics.buyer_delta_5m),
            Decimal("15"),
        )),
        ("organic two-sided flow", Decimal("1") if organic else ZERO),
        ("volume acceleration", ramp(metrics.volume_acceleration, Decimal("3"))),
        ("liquidity level and stability", ramp(
            snapshot.liquidity_usd, config.entry_min_liquidity_usd
        ) * (Decimal("1") if metrics.liquidity_stable is not False else Decimal("0.5"))),
        ("distribution quality", (
            ZERO if snapshot.cluster_adjusted_top10 is None
            else max(ZERO, Decimal("1") - (snapshot.cluster_adjusted_top10 / Decimal("0.7")))
        )),
    ]
    total = sum((fraction * SCORE_WEIGHTS[name] for name, fraction in parts), ZERO)
    breakdown = tuple(
        (name, str((fraction * SCORE_WEIGHTS[name]).quantize(CENT))) for name, fraction in parts
    )
    return total.quantize(CENT), breakdown


@dataclass(frozen=True, slots=True)
class LadderResult:
    """Which rung this candidate is on, and exactly what held it back."""

    stage: str = STAGE_0_VERIFIED
    outcome: str = ""
    blocked_by: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)
    score: Decimal | None = None

    def label(self) -> str:
        return LADDER_LABEL.get(self.stage, self.stage)

    def to_json(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "label": self.label(),
            "outcome": self.outcome,
            "outcome_human": HUMAN_OUTCOME.get(self.outcome, ""),
            "blocked_by": list(self.blocked_by),
            "notes": list(self.notes),
            "traction_score": _s(self.score),
            "research_only": True,
        }


def climb(
    snapshot: HolderSnapshot,
    metrics: TractionMetrics,
    *,
    age_seconds: int | None,
    sell_route_ok: bool | None = None,
    holder_conflict: bool = False,
    config: TractionConfig = DEFAULT_TRACTION_CONFIG,
) -> LadderResult:
    """How far up the ladder this candidate has actually climbed.

    Identity and anchor proof are the caller's job and are assumed done: this
    function starts at Stage 0 and asks only what the traction evidence adds.
    """

    blocked: list[str] = []
    notes: list[str] = []

    if holder_conflict:
        # A contested holder count cannot support any claim built on holders.
        return LadderResult(
            stage=STAGE_0_VERIFIED,
            outcome=OUT_HOLDER_CONFLICT,
            blocked_by=("holder count contradicts the chain ledger",),
        )

    young = age_seconds is not None and age_seconds < config.sell_history_grace_seconds
    sellers = snapshot.independent_sellers
    if young and sellers < config.scout_min_independent_sellers and sell_route_ok:
        notes.append("SELL HISTORY FORMING — a verified route exists, nobody has used it yet")
        sellers = config.scout_min_independent_sellers

    def short(reason: str) -> None:
        blocked.append(reason)

    # --- rung 1 ----------------------------------------------------------
    if snapshot.economic_holders < config.scout_min_economic_holders:
        short(f"{snapshot.economic_holders} economic holders, need "
              f"{config.scout_min_economic_holders}")
    if snapshot.independent_buyers < config.scout_min_independent_buyers:
        short(f"{snapshot.independent_buyers} independent buyers, need "
              f"{config.scout_min_independent_buyers}")
    if sellers < config.scout_min_independent_sellers:
        short(f"{sellers} independent sellers, need {config.scout_min_independent_sellers}")
    if (
        snapshot.liquidity_usd is None
        or snapshot.liquidity_usd < config.scout_min_liquidity_usd
    ):
        short(f"liquidity below ${config.scout_min_liquidity_usd}")
    if (
        snapshot.cluster_adjusted_top10 is not None
        and snapshot.cluster_adjusted_top10 > config.scout_max_cluster_top10
    ):
        short("cluster-adjusted top 10 above the scout ceiling")
    if blocked:
        return LadderResult(
            stage=STAGE_0_VERIFIED,
            outcome=OUT_NO_TRACTION,
            blocked_by=tuple(blocked),
            notes=tuple(notes),
        )

    # --- rung 2 ----------------------------------------------------------
    growing = (
        (metrics.holder_delta_5m is not None
         and metrics.holder_delta_5m >= config.traction_min_holder_delta_5m)
        or (metrics.holder_growth_pct_15m is not None
            and metrics.holder_growth_pct_15m >= config.traction_min_holder_growth_pct_15m)
    )
    if snapshot.economic_holders < config.traction_min_economic_holders:
        short(f"{snapshot.economic_holders} holders, need "
              f"{config.traction_min_economic_holders} for traction")
    if snapshot.independent_buyers < config.traction_min_independent_buyers:
        short("not enough independent buyers for traction")
    if sellers < config.traction_min_independent_sellers:
        short("not enough independent sellers for traction")
    if not growing:
        short("holder growth has not met either traction threshold")
    if (
        snapshot.liquidity_usd is None
        or snapshot.liquidity_usd < config.traction_min_liquidity_usd
    ):
        short(f"liquidity below ${config.traction_min_liquidity_usd}")
    if (
        snapshot.cluster_adjusted_top10 is not None
        and snapshot.cluster_adjusted_top10 > config.traction_max_cluster_top10
    ):
        short("cluster-adjusted top 10 above the traction ceiling")
    if blocked:
        return LadderResult(
            stage=STAGE_1_SPARK, blocked_by=tuple(blocked), notes=tuple(notes)
        )

    # --- rung 3 ----------------------------------------------------------
    if snapshot.economic_holders < config.entry_min_economic_holders:
        short(f"{snapshot.economic_holders} holders, need "
              f"{config.entry_min_economic_holders} for entry")
    if snapshot.independent_buyers < config.entry_min_independent_buyers:
        short("not enough independent buyers for entry")
    if sellers < config.entry_min_independent_sellers:
        short("not enough independent sellers for entry")
    if (
        snapshot.liquidity_usd is None
        or snapshot.liquidity_usd < config.entry_min_liquidity_usd
    ):
        short(f"liquidity below ${config.entry_min_liquidity_usd}")
    if (
        snapshot.cluster_adjusted_top10 is None
        or snapshot.cluster_adjusted_top10 > config.entry_max_cluster_top10
    ):
        short("cluster-adjusted top 10 above the entry ceiling, or unread")
    if blocked:
        return LadderResult(
            stage=STAGE_2_TRACTION, blocked_by=tuple(blocked), notes=tuple(notes)
        )

    return LadderResult(stage=STAGE_3_ENTRY, notes=tuple(notes))


def config_from_settings(settings: object) -> TractionConfig:
    """Build the ladder from operator configuration.

    Percentages arrive as whole numbers in the environment (70 rather than
    0.70) because that is how the specification writes them and how an
    operator thinks about them; they are converted here, once, rather than at
    each comparison.
    """

    def value(name: str, fallback: object) -> object:
        return getattr(settings, name, fallback)

    def rate(name: str, fallback: Decimal) -> Decimal:
        raw = value(name, None)
        return (Decimal(str(raw)) / HUNDRED) if raw is not None else fallback

    base = DEFAULT_TRACTION_CONFIG
    return TractionConfig(
        scout_min_economic_holders=int(
            value("stonks_scout_min_economic_holders", base.scout_min_economic_holders)
        ),
        scout_min_independent_buyers=int(
            value("stonks_scout_min_independent_buyers", base.scout_min_independent_buyers)
        ),
        scout_min_independent_sellers=int(
            value("stonks_scout_min_independent_sellers", base.scout_min_independent_sellers)
        ),
        scout_min_liquidity_usd=Decimal(
            str(value("stonks_scout_min_liquidity_usd", base.scout_min_liquidity_usd))
        ),
        scout_max_cluster_top10=rate(
            "stonks_scout_max_cluster_top10_pct", base.scout_max_cluster_top10
        ),
        traction_min_economic_holders=int(
            value("stonks_traction_min_economic_holders", base.traction_min_economic_holders)
        ),
        traction_min_independent_buyers=int(
            value("stonks_traction_min_independent_buyers", base.traction_min_independent_buyers)
        ),
        traction_min_independent_sellers=int(
            value(
                "stonks_traction_min_independent_sellers", base.traction_min_independent_sellers
            )
        ),
        traction_min_holder_delta_5m=int(
            value("stonks_traction_min_holder_delta_5m", base.traction_min_holder_delta_5m)
        ),
        traction_min_holder_growth_pct_15m=Decimal(
            str(
                value(
                    "stonks_traction_min_holder_growth_pct_15m",
                    base.traction_min_holder_growth_pct_15m,
                )
            )
        ),
        traction_min_liquidity_usd=Decimal(
            str(value("stonks_traction_min_liquidity_usd", base.traction_min_liquidity_usd))
        ),
        traction_max_cluster_top10=rate(
            "stonks_traction_max_cluster_top10_pct", base.traction_max_cluster_top10
        ),
        entry_min_economic_holders=int(
            value("stonks_entry_min_economic_holders", base.entry_min_economic_holders)
        ),
        entry_min_independent_buyers=int(
            value("stonks_entry_min_independent_buyers", base.entry_min_independent_buyers)
        ),
        entry_min_independent_sellers=int(
            value("stonks_entry_min_independent_sellers", base.entry_min_independent_sellers)
        ),
        entry_min_liquidity_usd=Decimal(
            str(value("stonks_entry_min_liquidity_usd", base.entry_min_liquidity_usd))
        ),
        entry_max_cluster_top10=rate(
            "stonks_entry_max_cluster_top10_pct", base.entry_max_cluster_top10
        ),
    )
