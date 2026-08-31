"""Trending latency, and the four ways an opportunity gets missed.

v2.41 fixed "the bot saw the coin early but the human saw it late".  It did not
fix "the bot itself saw the coin late" (section 78), which is a different
problem with a different measurement: the gap between the token appearing on the
*source* and the bot's first observation of it.

Four stamps, each written once, in causal order:

``SOURCE_APPEARANCE`` → ``BOT_OBSERVATION`` → ``CHEAP_VERDICT`` → ``DISCORD_SEND``

The percentiles below are reported per stage so a regression can be attributed:
a slow ``BOT_OBSERVATION`` is a polling-cadence problem, a slow ``CHEAP_VERDICT``
is an enrichment problem, and a slow ``DISCORD_SEND`` is a queue problem.  They
have completely different fixes and lumping them into one number hides which one
is actually broken.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")

STAGE_SOURCE_APPEARANCE = "SOURCE_APPEARANCE"
STAGE_BOT_OBSERVATION = "BOT_OBSERVATION"
STAGE_CHEAP_VERDICT = "CHEAP_VERDICT"
STAGE_DISCORD_SEND = "DISCORD_SEND"

TRENDING_LATENCY_STAGES: tuple[str, ...] = (
    STAGE_SOURCE_APPEARANCE,
    STAGE_BOT_OBSERVATION,
    STAGE_CHEAP_VERDICT,
    STAGE_DISCORD_SEND,
)

# --- missed-opportunity classes (sections 80-82) -----------------------------
MISSED_TRENDING_RUNNER = "MISSED_TRENDING_RUNNER"
MISSED_THESIS_ALPHA = "MISSED_THESIS_ALPHA"
MISSED_STORY_ALPHA = "MISSED_STORY_ALPHA"
MISSED_SOCIAL_ALPHA = "MISSED_SOCIAL_ALPHA"
MISSED_WALLET_ALPHA = "MISSED_WALLET_ALPHA"
MISSED_AI_PROJECT_ALPHA = "MISSED_AI_PROJECT_ALPHA"

MISS_CLASSES: tuple[str, ...] = (
    MISSED_TRENDING_RUNNER,
    MISSED_THESIS_ALPHA,
    MISSED_STORY_ALPHA,
    MISSED_SOCIAL_ALPHA,
    MISSED_WALLET_ALPHA,
    MISSED_AI_PROJECT_ALPHA,
)


@dataclass(frozen=True, slots=True)
class LatencySample:
    mint: str
    source_appearance_at: int | None = None
    bot_observation_at: int | None = None
    cheap_verdict_at: int | None = None
    discord_send_at: int | None = None

    def stage_seconds(self) -> dict[str, int | None]:
        """Per-stage elapsed seconds, ``None`` where a stamp is missing."""

        def gap(start: int | None, end: int | None) -> int | None:
            if start is None or end is None:
                return None
            return max(0, end - start)

        return {
            "source_to_observation": gap(self.source_appearance_at, self.bot_observation_at),
            "observation_to_verdict": gap(self.bot_observation_at, self.cheap_verdict_at),
            "verdict_to_send": gap(self.cheap_verdict_at, self.discord_send_at),
            "source_to_send": gap(self.source_appearance_at, self.discord_send_at),
        }


def percentile(values: Sequence[int], fraction: Decimal) -> int | None:
    """Nearest-rank percentile.  ``None`` on an empty sample, never zero."""

    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = int((fraction * Decimal(len(ordered))).to_integral_value(rounding="ROUND_CEILING"))
    index = max(1, min(len(ordered), rank)) - 1
    return ordered[index]


@dataclass(frozen=True, slots=True)
class LatencyReport:
    stage: str
    samples: int = 0
    p50: int | None = None
    p90: int | None = None
    p99: int | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "samples": self.samples,
            "p50": self.p50,
            "p90": self.p90,
            "p99": self.p99,
        }


def build_latency_reports(samples: Sequence[LatencySample]) -> tuple[LatencyReport, ...]:
    """p50 / p90 / p99 per stage (section 79)."""

    buckets: dict[str, list[int]] = {
        "source_to_observation": [],
        "observation_to_verdict": [],
        "verdict_to_send": [],
        "source_to_send": [],
    }
    for sample in samples:
        for stage, value in sample.stage_seconds().items():
            if value is not None:
                buckets[stage].append(value)
    return tuple(
        LatencyReport(
            stage=stage,
            samples=len(values),
            p50=percentile(values, Decimal("0.5")),
            p90=percentile(values, Decimal("0.9")),
            p99=percentile(values, Decimal("0.99")),
        )
        for stage, values in buckets.items()
    )


@dataclass(frozen=True, slots=True)
class MissedOpportunity:
    """A token that moved after we saw it, with the exact reason we said nothing."""

    mint: str
    miss_class: str
    observed_at: int
    market_cap_at_observation_usd: Decimal | None = None
    peak_market_cap_usd: Decimal | None = None
    move_percent: Decimal | None = None
    suppression_reason: str = ""
    detail: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "miss_class": self.miss_class,
            "observed_at": self.observed_at,
            "market_cap_at_observation_usd": _s(self.market_cap_at_observation_usd),
            "peak_market_cap_usd": _s(self.peak_market_cap_usd),
            "move_percent": _s(self.move_percent),
            "suppression_reason": self.suppression_reason,
            "detail": self.detail,
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def classify_miss(
    mint: str,
    *,
    observed_at: int,
    market_cap_at_observation_usd: Decimal | None,
    peak_market_cap_usd: Decimal | None,
    alerted: bool,
    suppression_reason: str = "",
    had_quality_thesis: bool = False,
    had_verified_story: bool = False,
    had_social_acceleration: bool = False,
    had_proven_wallet: bool = False,
    had_supported_ai_project: bool = False,
    min_move_percent: Decimal = Decimal("50"),
) -> MissedOpportunity | None:
    """Record a miss only when the bot actually saw it and said nothing useful.

    A token the bot never observed is a coverage gap, not a miss — those are a
    different measurement and mixing them makes both meaningless.
    """

    if alerted:
        return None
    if market_cap_at_observation_usd is None or peak_market_cap_usd is None:
        return None
    if market_cap_at_observation_usd <= ZERO:
        return None
    move = (
        (peak_market_cap_usd - market_cap_at_observation_usd)
        / market_cap_at_observation_usd
        * Decimal("100")
    ).quantize(Decimal("0.1"))
    if move < min_move_percent:
        return None

    if had_quality_thesis:
        miss_class = MISSED_THESIS_ALPHA
    elif had_supported_ai_project:
        miss_class = MISSED_AI_PROJECT_ALPHA
    elif had_verified_story:
        miss_class = MISSED_STORY_ALPHA
    elif had_proven_wallet:
        miss_class = MISSED_WALLET_ALPHA
    elif had_social_acceleration:
        miss_class = MISSED_SOCIAL_ALPHA
    else:
        miss_class = MISSED_TRENDING_RUNNER

    return MissedOpportunity(
        mint=mint,
        miss_class=miss_class,
        observed_at=observed_at,
        market_cap_at_observation_usd=market_cap_at_observation_usd,
        peak_market_cap_usd=peak_market_cap_usd,
        move_percent=move,
        suppression_reason=suppression_reason,
        detail=f"moved {move}% after we saw it and stayed silent",
    )


@dataclass(frozen=True, slots=True)
class AlertPerformanceRow:
    """Was the alert early enough to be worth sending? (section 83)"""

    mint: str
    market_cap_at_source_usd: Decimal | None = None
    market_cap_at_observation_usd: Decimal | None = None
    market_cap_at_alert_usd: Decimal | None = None
    later_mfe_percent: Decimal | None = None
    later_mae_percent: Decimal | None = None

    def _before(self, threshold: Decimal) -> bool | None:
        base = self.market_cap_at_observation_usd
        alert = self.market_cap_at_alert_usd
        if base is None or alert is None or base <= ZERO:
            return None
        move = (alert - base) / base * Decimal("100")
        return move < threshold

    @property
    def alerted_before_10(self) -> bool | None:
        return self._before(Decimal("10"))

    @property
    def alerted_before_25(self) -> bool | None:
        return self._before(Decimal("25"))

    @property
    def alerted_before_50(self) -> bool | None:
        return self._before(Decimal("50"))

    @property
    def alerted_before_100(self) -> bool | None:
        return self._before(Decimal("100"))

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "market_cap_at_source_usd": _s(self.market_cap_at_source_usd),
            "market_cap_at_observation_usd": _s(self.market_cap_at_observation_usd),
            "market_cap_at_alert_usd": _s(self.market_cap_at_alert_usd),
            "later_mfe_percent": _s(self.later_mfe_percent),
            "later_mae_percent": _s(self.later_mae_percent),
            "alerted_before_10": self.alerted_before_10,
            "alerted_before_25": self.alerted_before_25,
            "alerted_before_50": self.alerted_before_50,
            "alerted_before_100": self.alerted_before_100,
        }
