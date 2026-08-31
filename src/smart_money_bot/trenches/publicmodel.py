"""PUBLIC_TRENDING_MODEL — our own ranking, built from what is actually happening.

This replaces the core of v2.42's approximation.  That version ordered candidates
largely by DEX Screener boost and profile position, which is **paid placement**:
a token ranks because someone bought a slot, not because anyone is trading it.
Section 31 is blunt about it — stop treating boost rank as the approximation.

So the ranking here is computed from activity: multi-timeframe momentum,
independent participants, holder expansion, liquidity depth and turnover.  A
paid boost survives as *one small capped feature* worth a few points, because it
does weakly correlate with attention, and it is structurally incapable of
lifting a token that nothing is happening to (test 95).

What this is **not**: Terminal's ranking, Fomo's ranking, or a reconstruction of
either.  It is our model over public data, it is labelled that way on every
surface, and :func:`~.provenance.assert_honest_ranking_name` makes a dishonest
label a crash rather than a card.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from ..trending.score import ScoreComponent, ramp
from .provenance import (
    DERIVED_PUBLIC_MODEL,
    PUBLIC_TRENDING_MODEL,
    SourceRef,
    assert_honest_ranking_name,
    count_independent,
)
from .timeframes import (
    MOMENTUM_COOLING,
    MOMENTUM_INCREASING,
    MOMENTUM_REVERSING,
    SHAPE_FADING,
    SHAPE_SUSTAINED_TREND,
    SHAPE_VERY_EARLY_ACCELERATION,
    TF_1H,
    TF_1M,
    TF_5M,
    TF_15M,
    TF_30M,
    DepthProfile,
    TimeframeProfile,
)

ZERO = Decimal("0")
HUNDRED = Decimal("100")

#: The honest name for this ranking, asserted at construction.
MODEL_NAME = PUBLIC_TRENDING_MODEL

#: The one line every surface showing this rank must carry.
MODEL_CAVEAT = (
    "This is OUR public-data model, not Terminal's proprietary rank and not Fomo's rank."
)


@dataclass(frozen=True, slots=True)
class PublicTrendWeights:
    """Activity outweighs paid placement by roughly twenty to one."""

    momentum_1m: Decimal = Decimal("14")
    momentum_5m: Decimal = Decimal("18")
    momentum_15m: Decimal = Decimal("14")
    momentum_30m: Decimal = Decimal("8")
    momentum_1h: Decimal = Decimal("6")
    independent_buyers: Decimal = Decimal("18")
    holder_growth: Decimal = Decimal("12")
    liquidity: Decimal = Decimal("10")
    volume_quality: Decimal = Decimal("8")
    #: Deliberately tiny.  A purchase is not a trend (sections 26, 31, 95).
    dex_placement: Decimal = Decimal("3")
    # Penalties
    fading_penalty: Decimal = Decimal("20")
    thin_penalty: Decimal = Decimal("12")
    churn_penalty: Decimal = Decimal("10")


DEFAULT_PUBLIC_WEIGHTS = PublicTrendWeights()


@dataclass(frozen=True, slots=True)
class PublicTrendScore:
    """One token's score under our own model."""

    mint: str
    score: Decimal = ZERO
    components: tuple[ScoreComponent, ...] = ()
    shape: str = ""
    momentum_curve: str = ""
    sources: tuple[SourceRef, ...] = ()
    model: str = MODEL_NAME

    def __post_init__(self) -> None:
        assert_honest_ranking_name(self.model)

    @property
    def independent_sources(self) -> int:
        return count_independent(self.sources)

    def breakdown_lines(self) -> tuple[str, ...]:
        return tuple(
            f"{item.name}: {item.points}/{item.maximum}"
            + (f" — {item.detail}" if item.detail else "")
            for item in self.components
            if item.points != ZERO
        )

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "score": str(self.score),
            "components": [item.to_json() for item in self.components],
            "shape": self.shape,
            "momentum_curve": self.momentum_curve,
            "sources": [item.to_json() for item in self.sources],
            "independent_sources": self.independent_sources,
            "model": self.model,
            "caveat": MODEL_CAVEAT,
        }


def score_public_trend(
    mint: str,
    *,
    timeframes: TimeframeProfile,
    depth: DepthProfile | None = None,
    independent_buyers: int | None = None,
    holder_velocity: Decimal | None = None,
    dex_paid: bool = False,
    dex_boosts: int = 0,
    sources: Sequence[SourceRef] = (),
    weights: PublicTrendWeights = DEFAULT_PUBLIC_WEIGHTS,
) -> PublicTrendScore:
    """Rank by what is happening, not by what was paid for."""

    components: list[ScoreComponent] = []

    for timeframe, weight, target in (
        (TF_1M, weights.momentum_1m, Decimal("10")),
        (TF_5M, weights.momentum_5m, Decimal("25")),
        (TF_15M, weights.momentum_15m, Decimal("45")),
        (TF_30M, weights.momentum_30m, Decimal("70")),
        (TF_1H, weights.momentum_1h, Decimal("100")),
    ):
        change = timeframes.change(timeframe)
        components.append(
            ScoreComponent(
                f"momentum_{timeframe}",
                ramp(change, floor=ZERO, target=target, weight=weight),
                weight,
                "no usable window" if change is None else f"{change:+.1f}%",
            )
        )

    components.append(
        ScoreComponent(
            "independent_buyers",
            ramp(
                None if independent_buyers is None else Decimal(independent_buyers),
                floor=Decimal("3"),
                target=Decimal("150"),
                weight=weights.independent_buyers,
            ),
            weights.independent_buyers,
            "unknown" if independent_buyers is None else f"{independent_buyers}",
        )
    )

    components.append(
        ScoreComponent(
            "holder_growth",
            ramp(holder_velocity, floor=ZERO, target=Decimal("15"), weight=weights.holder_growth),
            weights.holder_growth,
            "unknown" if holder_velocity is None else f"{holder_velocity}/min",
        )
    )

    liquidity = depth.liquidity_usd if depth else None
    components.append(
        ScoreComponent(
            "liquidity",
            ramp(
                liquidity,
                floor=Decimal("3000"),
                target=Decimal("80000"),
                weight=weights.liquidity,
            ),
            weights.liquidity,
            "unknown" if liquidity is None else f"${liquidity:,.0f}",
        )
    )

    # Turnover in a healthy band: some churn is life, extreme churn is recycling.
    turnover = depth.volume_to_liquidity if depth else None
    volume_points = ZERO
    if turnover is not None:
        volume_points = ramp(
            turnover, floor=Decimal("0.2"), target=Decimal("3"), weight=weights.volume_quality
        )
    components.append(
        ScoreComponent(
            "volume_quality",
            volume_points,
            weights.volume_quality,
            "unknown" if turnover is None else f"volume/liquidity {turnover}",
        )
    )

    # Paid placement: capped so low it can never carry a token on its own.
    placement = ZERO
    if dex_paid or dex_boosts:
        placement = min(
            weights.dex_placement,
            ramp(
                Decimal(dex_boosts or 1),
                floor=ZERO,
                target=Decimal("5"),
                weight=weights.dex_placement,
            ),
        )
    components.append(
        ScoreComponent(
            "dex_placement",
            placement,
            weights.dex_placement,
            "purchased attention — capped, never a trend on its own",
        )
    )

    total = sum((item.points for item in components), ZERO)

    if timeframes.shape == SHAPE_FADING or timeframes.momentum_curve == MOMENTUM_REVERSING:
        total -= weights.fading_penalty
        components.append(
            ScoreComponent(
                "fading_penalty",
                -weights.fading_penalty,
                weights.fading_penalty,
                "the move is rolling over",
            )
        )
    elif timeframes.momentum_curve == MOMENTUM_COOLING:
        half = weights.fading_penalty / 2
        total -= half
        components.append(
            ScoreComponent(
                "cooling_penalty", -half, weights.fading_penalty, "still rising, slower"
            )
        )

    if depth is not None and depth.thin:
        total -= weights.thin_penalty
        components.append(
            ScoreComponent(
                "thin_penalty",
                -weights.thin_penalty,
                weights.thin_penalty,
                "liquidity too thin for the size",
            )
        )
    if depth is not None and depth.churning:
        total -= weights.churn_penalty
        components.append(
            ScoreComponent(
                "churn_penalty",
                -weights.churn_penalty,
                weights.churn_penalty,
                "volume far beyond what the pool supports — likely recycling",
            )
        )

    if timeframes.momentum_curve == MOMENTUM_INCREASING and timeframes.shape in {
        SHAPE_VERY_EARLY_ACCELERATION,
        SHAPE_SUSTAINED_TREND,
    }:
        bonus = Decimal("5")
        total += bonus
        components.append(
            ScoreComponent("acceleration_bonus", bonus, bonus, "acceleration is itself increasing")
        )

    refs = tuple(sources) or (
        SourceRef(kind=DERIVED_PUBLIC_MODEL, detail="computed from public market data"),
    )

    return PublicTrendScore(
        mint=mint,
        score=max(ZERO, min(HUNDRED, total)).quantize(Decimal("0.1")),
        components=tuple(components),
        shape=timeframes.shape,
        momentum_curve=timeframes.momentum_curve,
        sources=refs,
    )


@dataclass(frozen=True, slots=True)
class RankedToken:
    """One row of our public Trending board."""

    mint: str
    rank: int
    score: PublicTrendScore
    previous_rank: int | None = None

    @property
    def rank_delta(self) -> int | None:
        """Positive means climbing (the rank number went down)."""

        if self.previous_rank is None:
            return None
        return self.previous_rank - self.rank

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "rank": self.rank,
            "previous_rank": self.previous_rank,
            "rank_delta": self.rank_delta,
            "score": self.score.to_json(),
        }


def rank_public_trend(
    scores: Sequence[PublicTrendScore],
    *,
    previous_ranks: dict[str, int] | None = None,
    limit: int = 50,
    min_score: Decimal = Decimal("10"),
) -> tuple[RankedToken, ...]:
    """Order by our own score.  Ties break on mint so the board is stable."""

    prior = previous_ranks or {}
    ordered = sorted(
        (item for item in scores if item.score >= min_score),
        key=lambda item: (item.score, item.mint),
        reverse=True,
    )
    return tuple(
        RankedToken(
            mint=item.mint,
            rank=index,
            score=item,
            previous_rank=prior.get(item.mint),
        )
        for index, item in enumerate(ordered[:limit], start=1)
    )
