"""The scoreboard that answers the only question that finally matters.

    If I started with $100 on LEGACY and $100 on TRENDING, which one actually
    made more money — and which one actually got rugged less?

Nothing in this module guesses.  Every number is computed from resolved forward
outcomes of the two isolated experiments (sections 66-68).  Where a sample is
too small to mean anything, it says so rather than printing a ratio built from
four trades.

The safety comparison (section 67) is deliberately separate from the upside
comparison (section 68), because the operator's hypothesis has two independent
halves — "Trending rugs less" and "Trending goes up more" — and they can easily
come out in opposite directions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")
HUNDRED = Decimal("100")

#: Below this many resolved trades, every derived rate is reported as
#: provisional.  Ratios from tiny samples are the most confidently wrong numbers
#: in trading software.
MIN_MEANINGFUL_SAMPLE = 10


@dataclass(frozen=True, slots=True)
class UniverseTrade:
    """One resolved simulated trade from either universe."""

    mint: str
    family: str
    opened_at: int
    closed_at: int
    net_pnl_usd: Decimal
    size_usd: Decimal = Decimal("10")
    mfe_percent: Decimal | None = None
    mae_percent: Decimal | None = None
    #: A drawdown severe enough to count as a structural failure.
    severe_failure: bool = False
    #: Confirmed rug or liquidity collapse while the position was open.
    rugged: bool = False
    liquidity_collapsed: bool = False
    unsellable: bool = False


@dataclass(frozen=True, slots=True)
class UniverseReport:
    """One universe's forward record."""

    universe: str
    starting_bankroll_usd: Decimal = Decimal("100")
    current_bankroll_usd: Decimal = Decimal("100")
    trades: int = 0
    wins: int = 0
    losses: int = 0
    net_usd: Decimal = ZERO
    roi_percent: Decimal = ZERO
    win_rate: Decimal = ZERO
    profit_factor: Decimal | None = None
    expectancy_usd: Decimal = ZERO
    max_drawdown_usd: Decimal = ZERO
    avg_mfe_percent: Decimal | None = None
    avg_mae_percent: Decimal | None = None
    severe_failures: int = 0
    rug_rate: Decimal = ZERO
    liquidity_collapse_rate: Decimal = ZERO
    unsellable_rate: Decimal = ZERO
    hit_rate_25: Decimal = ZERO
    hit_rate_50: Decimal = ZERO
    hit_rate_100: Decimal = ZERO
    hit_rate_200: Decimal = ZERO

    @property
    def provisional(self) -> bool:
        return self.trades < MIN_MEANINGFUL_SAMPLE

    def to_json(self) -> dict[str, object]:
        return {
            "universe": self.universe,
            "starting_bankroll_usd": str(self.starting_bankroll_usd),
            "current_bankroll_usd": str(self.current_bankroll_usd),
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "net_usd": str(self.net_usd),
            "roi_percent": str(self.roi_percent),
            "win_rate": str(self.win_rate),
            "profit_factor": None if self.profit_factor is None else str(self.profit_factor),
            "expectancy_usd": str(self.expectancy_usd),
            "max_drawdown_usd": str(self.max_drawdown_usd),
            "avg_mfe_percent": None if self.avg_mfe_percent is None else str(self.avg_mfe_percent),
            "avg_mae_percent": None if self.avg_mae_percent is None else str(self.avg_mae_percent),
            "severe_failures": self.severe_failures,
            "rug_rate": str(self.rug_rate),
            "liquidity_collapse_rate": str(self.liquidity_collapse_rate),
            "unsellable_rate": str(self.unsellable_rate),
            "hit_rate_25": str(self.hit_rate_25),
            "hit_rate_50": str(self.hit_rate_50),
            "hit_rate_100": str(self.hit_rate_100),
            "hit_rate_200": str(self.hit_rate_200),
            "provisional": self.provisional,
        }


def _rate(count: int, total: int) -> Decimal:
    if total <= 0:
        return ZERO
    return (Decimal(count) / Decimal(total)).quantize(Decimal("0.001"))


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return (sum(values, ZERO) / Decimal(len(values))).quantize(Decimal("0.01"))


def build_universe_report(
    universe: str,
    trades: Sequence[UniverseTrade],
    *,
    starting_bankroll_usd: Decimal = Decimal("100"),
    current_bankroll_usd: Decimal | None = None,
) -> UniverseReport:
    """Summarise one bankroll's resolved forward record."""

    if not trades:
        return UniverseReport(
            universe=universe,
            starting_bankroll_usd=starting_bankroll_usd,
            current_bankroll_usd=(
                current_bankroll_usd if current_bankroll_usd is not None else starting_bankroll_usd
            ),
        )

    ordered = sorted(trades, key=lambda item: item.closed_at)
    net = sum((trade.net_pnl_usd for trade in ordered), ZERO)
    wins = [trade for trade in ordered if trade.net_pnl_usd > ZERO]
    losses = [trade for trade in ordered if trade.net_pnl_usd <= ZERO]
    gross_profit = sum((trade.net_pnl_usd for trade in wins), ZERO)
    gross_loss = -sum((trade.net_pnl_usd for trade in losses), ZERO)

    equity = starting_bankroll_usd
    peak = equity
    drawdown = ZERO
    for trade in ordered:
        equity += trade.net_pnl_usd
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)

    mfes = [trade.mfe_percent for trade in ordered if trade.mfe_percent is not None]
    maes = [trade.mae_percent for trade in ordered if trade.mae_percent is not None]

    def hit_rate(threshold: Decimal) -> Decimal:
        if not mfes:
            return ZERO
        return _rate(sum(1 for value in mfes if value >= threshold), len(mfes))

    return UniverseReport(
        universe=universe,
        starting_bankroll_usd=starting_bankroll_usd,
        current_bankroll_usd=(
            current_bankroll_usd if current_bankroll_usd is not None else equity
        ),
        trades=len(ordered),
        wins=len(wins),
        losses=len(losses),
        net_usd=net.quantize(Decimal("0.01")),
        roi_percent=(
            (net / starting_bankroll_usd * HUNDRED).quantize(Decimal("0.01"))
            if starting_bankroll_usd > ZERO
            else ZERO
        ),
        win_rate=_rate(len(wins), len(ordered)),
        profit_factor=(
            (gross_profit / gross_loss).quantize(Decimal("0.01")) if gross_loss > ZERO else None
        ),
        expectancy_usd=(net / Decimal(len(ordered))).quantize(Decimal("0.01")),
        max_drawdown_usd=drawdown.quantize(Decimal("0.01")),
        avg_mfe_percent=_mean(mfes),
        avg_mae_percent=_mean(maes),
        severe_failures=sum(1 for trade in ordered if trade.severe_failure),
        rug_rate=_rate(sum(1 for trade in ordered if trade.rugged), len(ordered)),
        liquidity_collapse_rate=_rate(
            sum(1 for trade in ordered if trade.liquidity_collapsed), len(ordered)
        ),
        unsellable_rate=_rate(sum(1 for trade in ordered if trade.unsellable), len(ordered)),
        hit_rate_25=hit_rate(Decimal("25")),
        hit_rate_50=hit_rate(Decimal("50")),
        hit_rate_100=hit_rate(Decimal("100")),
        hit_rate_200=hit_rate(Decimal("200")),
    )


@dataclass(frozen=True, slots=True)
class UniverseComparison:
    """Trending versus legacy, side by side, with an honest verdict."""

    trending: UniverseReport
    legacy: UniverseReport

    @property
    def comparable(self) -> bool:
        """Both books need a real sample before a verdict means anything."""

        return not self.trending.provisional and not self.legacy.provisional

    @property
    def net_leader(self) -> str:
        if self.trending.net_usd == self.legacy.net_usd:
            return "TIE"
        return "TRENDING" if self.trending.net_usd > self.legacy.net_usd else "LEGACY"

    @property
    def safety_leader(self) -> str:
        """Which universe actually got rugged less (section 67)."""

        trending_bad = self.trending.rug_rate + self.trending.liquidity_collapse_rate
        legacy_bad = self.legacy.rug_rate + self.legacy.liquidity_collapse_rate
        if trending_bad == legacy_bad:
            return "TIE"
        return "TRENDING" if trending_bad < legacy_bad else "LEGACY"

    @property
    def upside_leader(self) -> str:
        """Which universe actually ran further (section 68)."""

        if self.trending.hit_rate_100 == self.legacy.hit_rate_100:
            return "TIE"
        return "TRENDING" if self.trending.hit_rate_100 > self.legacy.hit_rate_100 else "LEGACY"

    def verdict(self) -> str:
        if not self.comparable:
            needed = max(
                MIN_MEANINGFUL_SAMPLE - self.trending.trades,
                MIN_MEANINGFUL_SAMPLE - self.legacy.trades,
            )
            return (
                "NOT ENOUGH FORWARD DATA — "
                f"{max(0, needed)} more resolved trades before this means anything"
            )
        return (
            f"NET: {self.net_leader} • SAFETY: {self.safety_leader} • "
            f"UPSIDE: {self.upside_leader}"
        )

    def to_json(self) -> dict[str, object]:
        return {
            "trending": self.trending.to_json(),
            "legacy": self.legacy.to_json(),
            "comparable": self.comparable,
            "net_leader": self.net_leader,
            "safety_leader": self.safety_leader,
            "upside_leader": self.upside_leader,
            "verdict": self.verdict(),
        }


def compare_universes(
    trending: UniverseReport,
    legacy: UniverseReport,
) -> UniverseComparison:
    return UniverseComparison(trending=trending, legacy=legacy)


# --- evidence-value comparison (section 85) ----------------------------------
EVIDENCE_NONE = "NO_CONTEXT"
EVIDENCE_ABOUT = "ABOUT_ONLY"
EVIDENCE_STORY = "STORY"
EVIDENCE_THESIS = "THESIS"
EVIDENCE_AI_PROJECT = "AI_PROJECT"
EVIDENCE_SOCIAL = "SOCIAL"
EVIDENCE_SMART_MONEY = "SMART_MONEY"
EVIDENCE_CONFLUENCE = "CONFLUENCE"

EVIDENCE_CLASSES: tuple[str, ...] = (
    EVIDENCE_NONE,
    EVIDENCE_ABOUT,
    EVIDENCE_STORY,
    EVIDENCE_THESIS,
    EVIDENCE_AI_PROJECT,
    EVIDENCE_SOCIAL,
    EVIDENCE_SMART_MONEY,
    EVIDENCE_CONFLUENCE,
)


@dataclass(frozen=True, slots=True)
class EvidencePerformance:
    evidence: str
    sample: int
    net_usd: Decimal
    expectancy_usd: Decimal
    win_rate: Decimal
    avg_mfe_percent: Decimal | None

    @property
    def provisional(self) -> bool:
        return self.sample < MIN_MEANINGFUL_SAMPLE

    def to_json(self) -> dict[str, object]:
        return {
            "evidence": self.evidence,
            "sample": self.sample,
            "net_usd": str(self.net_usd),
            "expectancy_usd": str(self.expectancy_usd),
            "win_rate": str(self.win_rate),
            "avg_mfe_percent": None if self.avg_mfe_percent is None else str(self.avg_mfe_percent),
            "provisional": self.provisional,
        }


def evidence_performance(
    trades_by_evidence: dict[str, Sequence[UniverseTrade]],
) -> tuple[EvidencePerformance, ...]:
    """Which kinds of evidence actually improve forward results (section 85)?"""

    rows: list[EvidencePerformance] = []
    for evidence, trades in trades_by_evidence.items():
        if not trades:
            rows.append(
                EvidencePerformance(evidence, 0, ZERO, ZERO, ZERO, None)
            )
            continue
        net = sum((trade.net_pnl_usd for trade in trades), ZERO)
        wins = sum(1 for trade in trades if trade.net_pnl_usd > ZERO)
        mfes = [trade.mfe_percent for trade in trades if trade.mfe_percent is not None]
        rows.append(
            EvidencePerformance(
                evidence=evidence,
                sample=len(trades),
                net_usd=net.quantize(Decimal("0.01")),
                expectancy_usd=(net / Decimal(len(trades))).quantize(Decimal("0.01")),
                win_rate=_rate(wins, len(trades)),
                avg_mfe_percent=_mean(mfes),
            )
        )
    rows.sort(key=lambda row: row.expectancy_usd, reverse=True)
    return tuple(rows)
