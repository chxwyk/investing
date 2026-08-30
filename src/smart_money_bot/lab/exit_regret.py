"""Did that exit save money, or cost it? (sections 7, 23)

An exit journal alone cannot tell you whether an exit rule is any good.  Selling
at +12% looks fine until you learn the token went to +300% forty minutes later,
and selling at -20% looks terrible until you learn it went to -95%.

This module scores every exit against **what actually happened afterwards**,
using the observation stream the shadow experiment already persists.  It answers
one question per exit reason: *was this rule defending the account, or leaking
from it?*

The lookahead here is legitimate and confined: it is evaluation after the fact,
never an input to a decision.  :func:`score_exit` is deliberately pure and takes
observations explicitly, so nothing in the live exit path can reach it — the
no-look-ahead tests assert that separation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .shadow_metrics import ShadowObservation

ZERO = Decimal("0")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")

#: How far after an exit to look when judging it.  Long enough for a real
#: continuation to show up, short enough that a week-later pump is not counted
#: as a missed opportunity the strategy could have held for.
DEFAULT_HORIZON_SECONDS = 3_600

#: A continuation this much above the exit price means the exit gave up real
#: money rather than merely mistiming a wobble.
PREMATURE_UPSIDE_PERCENT = Decimal("25")

#: A fall this far below the exit price means the exit genuinely defended.
DEFENSIVE_DOWNSIDE_PERCENT = Decimal("-15")

# --- verdicts ----------------------------------------------------------------
PREMATURE = "PREMATURE"
GOOD_DEFENSIVE = "GOOD_DEFENSIVE"
NEUTRAL = "NEUTRAL"
UNKNOWN = "UNKNOWN_NO_DATA"

VERDICTS: tuple[str, ...] = (PREMATURE, GOOD_DEFENSIVE, NEUTRAL, UNKNOWN)


@dataclass(frozen=True, slots=True)
class ExitRecord:
    """One persisted partial or full exit, with what it realized."""

    position_id: str
    mint: str
    family: str
    reason_code: str
    occurred_at: int
    exit_price_usd: Decimal
    fraction_sold: Decimal = ZERO
    net_pnl_usd: Decimal = ZERO
    final: bool = False


@dataclass(frozen=True, slots=True)
class ExitScore:
    """What happened after one exit."""

    record: ExitRecord
    verdict: str = UNKNOWN
    best_price_after_usd: Decimal | None = None
    worst_price_after_usd: Decimal | None = None
    upside_missed_percent: Decimal | None = None
    loss_avoided_percent: Decimal | None = None
    #: Dollars the sold fraction would have gained had it been held to the peak.
    upside_missed_usd: Decimal = ZERO
    #: Dollars the sold fraction would have lost had it been held to the trough.
    loss_avoided_usd: Decimal = ZERO
    observations_after: int = 0

    @property
    def premature(self) -> bool:
        return self.verdict == PREMATURE

    @property
    def defensive(self) -> bool:
        return self.verdict == GOOD_DEFENSIVE


def score_exit(
    record: ExitRecord,
    observations: Sequence[ShadowObservation],
    *,
    horizon_seconds: int = DEFAULT_HORIZON_SECONDS,
) -> ExitScore:
    """Judge one exit against the observations that followed it.

    Evaluation only.  This function is never called from the exit path — it
    reads the future by construction, which is exactly why it must not be.
    """

    if record.exit_price_usd <= 0:
        return ExitScore(record=record, verdict=UNKNOWN)

    window = [
        item
        for item in observations
        if record.occurred_at < item.at <= record.occurred_at + horizon_seconds
        and item.price_usd > 0
    ]
    if not window:
        return ExitScore(record=record, verdict=UNKNOWN)

    best = max(item.price_usd for item in window)
    worst = min(item.price_usd for item in window)
    upside = ((best - record.exit_price_usd) / record.exit_price_usd * HUNDRED).quantize(CENT)
    downside = ((worst - record.exit_price_usd) / record.exit_price_usd * HUNDRED).quantize(
        CENT
    )

    # Value the regret on the money that actually left the position.
    proceeds = abs(record.net_pnl_usd) if record.net_pnl_usd else ZERO
    notional = proceeds if proceeds > 0 else ZERO
    upside_usd = (notional * max(ZERO, upside) / HUNDRED).quantize(Decimal("0.000001"))
    avoided_usd = (notional * abs(min(ZERO, downside)) / HUNDRED).quantize(
        Decimal("0.000001")
    )

    if upside >= PREMATURE_UPSIDE_PERCENT:
        verdict = PREMATURE
    elif downside <= DEFENSIVE_DOWNSIDE_PERCENT:
        verdict = GOOD_DEFENSIVE
    else:
        verdict = NEUTRAL

    return ExitScore(
        record=record,
        verdict=verdict,
        best_price_after_usd=best,
        worst_price_after_usd=worst,
        upside_missed_percent=max(ZERO, upside),
        loss_avoided_percent=abs(min(ZERO, downside)),
        upside_missed_usd=upside_usd,
        loss_avoided_usd=avoided_usd,
        observations_after=len(window),
    )


@dataclass(frozen=True, slots=True)
class ExitReasonReport:
    """One row of `/fomo profit exits` — is this rule earning its keep?"""

    reason_code: str
    count: int = 0
    scored: int = 0
    premature: int = 0
    defensive: int = 0
    neutral: int = 0
    average_net_usd: Decimal | None = None
    total_net_usd: Decimal = ZERO
    upside_missed_usd: Decimal = ZERO
    loss_avoided_usd: Decimal = ZERO

    @property
    def premature_rate_percent(self) -> Decimal | None:
        if self.scored <= 0:
            return None
        return (Decimal(self.premature) / Decimal(self.scored) * HUNDRED).quantize(CENT)

    @property
    def defensive_rate_percent(self) -> Decimal | None:
        if self.scored <= 0:
            return None
        return (Decimal(self.defensive) / Decimal(self.scored) * HUNDRED).quantize(CENT)

    @property
    def net_regret_usd(self) -> Decimal:
        """Upside given up minus loss avoided.  Positive means this rule leaks."""

        return (self.upside_missed_usd - self.loss_avoided_usd).quantize(
            Decimal("0.000001")
        )

    @property
    def verdict(self) -> str:
        if self.scored <= 0:
            return UNKNOWN
        if self.net_regret_usd > 0:
            return "COSTING_MONEY"
        return "DEFENDING_MONEY"


def summarize_exit_reasons(scores: Sequence[ExitScore]) -> tuple[ExitReasonReport, ...]:
    """Aggregate per exit reason, worst offender first (section 23)."""

    grouped: dict[str, list[ExitScore]] = {}
    for score in scores:
        grouped.setdefault(score.record.reason_code, []).append(score)

    reports: list[ExitReasonReport] = []
    for reason, group in grouped.items():
        scored = [item for item in group if item.verdict != UNKNOWN]
        nets = [item.record.net_pnl_usd for item in group]
        reports.append(
            ExitReasonReport(
                reason_code=reason,
                count=len(group),
                scored=len(scored),
                premature=sum(1 for item in scored if item.premature),
                defensive=sum(1 for item in scored if item.defensive),
                neutral=sum(1 for item in scored if item.verdict == NEUTRAL),
                average_net_usd=(
                    (sum(nets, ZERO) / Decimal(len(nets))).quantize(Decimal("0.000001"))
                    if nets
                    else None
                ),
                total_net_usd=sum(nets, ZERO).quantize(Decimal("0.000001")),
                upside_missed_usd=sum(
                    (item.upside_missed_usd for item in group), ZERO
                ).quantize(Decimal("0.000001")),
                loss_avoided_usd=sum(
                    (item.loss_avoided_usd for item in group), ZERO
                ).quantize(Decimal("0.000001")),
            )
        )
    # The rule leaking the most money sorts first: that is the one to fix.
    return tuple(sorted(reports, key=lambda item: item.net_regret_usd, reverse=True))


@dataclass(frozen=True, slots=True)
class ExitQualityReport:
    """The account-level answer: are the exits helping or hurting?"""

    exits: int = 0
    scored: int = 0
    premature_rate_percent: Decimal | None = None
    defensive_rate_percent: Decimal | None = None
    total_upside_missed_usd: Decimal = ZERO
    total_loss_avoided_usd: Decimal = ZERO
    by_reason: Mapping[str, ExitReasonReport] = field(default_factory=dict)
    worst_reason: str = ""
    best_reason: str = ""

    @property
    def net_regret_usd(self) -> Decimal:
        return (self.total_upside_missed_usd - self.total_loss_avoided_usd).quantize(
            Decimal("0.000001")
        )

    @property
    def exits_are_leaking(self) -> bool:
        return self.net_regret_usd > 0


def summarize_exit_quality(scores: Sequence[ExitScore]) -> ExitQualityReport:
    if not scores:
        return ExitQualityReport()
    reports = summarize_exit_reasons(scores)
    scored = [item for item in scores if item.verdict != UNKNOWN]
    return ExitQualityReport(
        exits=len(scores),
        scored=len(scored),
        premature_rate_percent=_rate(
            sum(1 for item in scored if item.premature), len(scored)
        ),
        defensive_rate_percent=_rate(
            sum(1 for item in scored if item.defensive), len(scored)
        ),
        total_upside_missed_usd=sum(
            (item.upside_missed_usd for item in scores), ZERO
        ).quantize(Decimal("0.000001")),
        total_loss_avoided_usd=sum(
            (item.loss_avoided_usd for item in scores), ZERO
        ).quantize(Decimal("0.000001")),
        by_reason={item.reason_code: item for item in reports},
        worst_reason=reports[0].reason_code if reports else "",
        best_reason=reports[-1].reason_code if reports else "",
    )


def _rate(count: int, total: int) -> Decimal | None:
    if total <= 0:
        return None
    return (Decimal(count) / Decimal(total) * HUNDRED).quantize(CENT)
