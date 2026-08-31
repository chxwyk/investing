"""Trending-aware exit challengers — tested against the champion, never replacing it.

The operator's hypothesis is that a token still climbing the Trending board with
growing holders and an active thesis deserves more patience than the generic exit
rules give it.  That is a reasonable hypothesis.  It is not a fact, and the way
to find out is to run it as a *counterfactual* alongside the current champion,
not to swap the live exit logic and hope (section 69).

So every policy here is a challenger.  They consume the same observation stream
the existing counterfactual engine already records, which means twelve policies
cost zero extra provider requests.

Two invariants are non-negotiable:

* **Hard safety still wins (section 71).**  A confirmed sell failure, a collapsed
  pool or hard malicious evidence exits every policy, including the most patient
  one.  Trending is not rug protection.
* **A pause is not a reversal (section 70).**  ``SOFT_PAUSE`` on a token that is
  still on the board, still gaining holders and still liquid is not a reason to
  dump it on one weak print.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")
HUNDRED = Decimal("100")

# --- momentum states, mirrored from the legacy exit engine (section 70) ------
MOMENTUM_HEALTHY = "HEALTHY"
MOMENTUM_SOFT_PAUSE = "SOFT_PAUSE"
MOMENTUM_CONFIRMED_DECAY = "CONFIRMED_DECAY"
MOMENTUM_HARD_REVERSAL = "HARD_REVERSAL"

# --- challenger policies (section 69) ----------------------------------------
POLICY_CHAMPION = "CURRENT_SHADOW"
POLICY_TRENDING_PERSISTENCE = "TRENDING_PERSISTENCE"
POLICY_RANK_TRAILING = "RANK_TRAILING"
POLICY_THESIS_CONTINUATION = "THESIS_CONTINUATION"
POLICY_STORY_CONTINUATION = "STORY_CONTINUATION"
POLICY_HOLDER_CONTINUATION = "HOLDER_GROWTH_CONTINUATION"
POLICY_ADAPTIVE_TRAIL = "ADAPTIVE_TRAIL"
POLICY_PRINCIPAL_RUNNER = "PRINCIPAL_RECOVERY_RUNNER"
POLICY_HOLD_5M = "HOLD_5M"
POLICY_HOLD_15M = "HOLD_15M"
POLICY_HOLD_30M = "HOLD_30M"
POLICY_HOLD_1H = "HOLD_1H"

TRENDING_EXIT_POLICIES: tuple[str, ...] = (
    POLICY_CHAMPION,
    POLICY_TRENDING_PERSISTENCE,
    POLICY_RANK_TRAILING,
    POLICY_THESIS_CONTINUATION,
    POLICY_STORY_CONTINUATION,
    POLICY_HOLDER_CONTINUATION,
    POLICY_ADAPTIVE_TRAIL,
    POLICY_PRINCIPAL_RUNNER,
    POLICY_HOLD_5M,
    POLICY_HOLD_15M,
    POLICY_HOLD_30M,
    POLICY_HOLD_1H,
)

_HOLD_SECONDS: dict[str, int] = {
    POLICY_HOLD_5M: 300,
    POLICY_HOLD_15M: 900,
    POLICY_HOLD_30M: 1800,
    POLICY_HOLD_1H: 3600,
}


@dataclass(frozen=True, slots=True)
class TrendingExitContext:
    """One post-entry observation, enriched with Trending state (section 72)."""

    at: int
    seconds_held: int
    unrealized_percent: Decimal
    peak_percent: Decimal
    rank: int | None = None
    rank_direction: int = 0
    seconds_trending: int = 0
    on_board: bool = True
    holder_growth: int | None = None
    story_active: bool = False
    thesis_active: bool = False
    social_velocity: Decimal | None = None
    liquidity_usd: Decimal | None = None
    momentum_state: str = MOMENTUM_HEALTHY
    smart_money_distributing: bool = False
    # ---- hard safety, which no policy may ignore -------------------------
    sell_failed: bool = False
    liquidity_collapsed: bool = False
    malicious_evidence: bool = False

    @property
    def hard_failure(self) -> bool:
        return self.sell_failed or self.liquidity_collapsed or self.malicious_evidence

    @property
    def hard_failure_reason(self) -> str:
        if self.sell_failed:
            return "SELL_FAILED"
        if self.liquidity_collapsed:
            return "LIQUIDITY_COLLAPSE"
        if self.malicious_evidence:
            return "MALICIOUS_EVIDENCE"
        return ""


@dataclass(frozen=True, slots=True)
class ExitDecision:
    exit: bool
    reason: str = ""
    at: int = 0
    unrealized_percent: Decimal = ZERO


def _hard_exit(context: TrendingExitContext) -> ExitDecision | None:
    """Section 71, applied identically to every policy including the patient ones."""

    if context.hard_failure:
        return ExitDecision(
            True,
            context.hard_failure_reason,
            context.at,
            context.unrealized_percent,
        )
    return None


def _still_convincing(context: TrendingExitContext) -> bool:
    """Is the Trending case still alive?  A pause alone does not end it (§70)."""

    if context.momentum_state == MOMENTUM_HARD_REVERSAL:
        return False
    if not context.on_board:
        return False
    if context.smart_money_distributing:
        return False
    supports = (
        context.rank_direction >= 0,
        context.holder_growth is not None and context.holder_growth > 0,
        context.story_active,
        context.thesis_active,
    )
    return sum(1 for item in supports if item) >= 2


def evaluate_policy(
    policy: str,
    observations: Sequence[TrendingExitContext],
    *,
    hard_stop_percent: Decimal = Decimal("-35"),
    trail_giveback_percent: Decimal = Decimal("35"),
    objective_percent: Decimal = Decimal("20"),
    max_hold_seconds: int = 5400,
) -> ExitDecision:
    """Replay one policy over the recorded observations.

    Strictly causal: each observation is judged using only itself and the ones
    before it, so a later pump can never reach back and change an earlier
    decision (section 108).
    """

    if not observations:
        return ExitDecision(False)

    ordered = sorted(observations, key=lambda item: item.at)
    peak = ZERO
    for context in ordered:
        hard = _hard_exit(context)
        if hard is not None:
            return hard

        peak = max(peak, context.unrealized_percent)

        if context.unrealized_percent <= hard_stop_percent:
            return ExitDecision(True, "HARD_STOP", context.at, context.unrealized_percent)

        hold_for = _HOLD_SECONDS.get(policy)
        if hold_for is not None:
            if context.seconds_held >= hold_for:
                return ExitDecision(
                    True, f"{policy}_ELAPSED", context.at, context.unrealized_percent
                )
            continue

        if policy == POLICY_CHAMPION:
            if (
                peak >= objective_percent
                and context.unrealized_percent
                <= peak * (HUNDRED - trail_giveback_percent) / HUNDRED
            ):
                return ExitDecision(True, "TRAIL", context.at, context.unrealized_percent)
            if context.momentum_state in {MOMENTUM_CONFIRMED_DECAY, MOMENTUM_HARD_REVERSAL}:
                return ExitDecision(True, "MOMENTUM_DECAY", context.at, context.unrealized_percent)

        elif policy == POLICY_TRENDING_PERSISTENCE:
            # Hold while the token is still on the board and the case holds.
            if not context.on_board:
                return ExitDecision(True, "LEFT_TRENDING", context.at, context.unrealized_percent)
            if context.momentum_state == MOMENTUM_HARD_REVERSAL:
                return ExitDecision(True, "HARD_REVERSAL", context.at, context.unrealized_percent)

        elif policy == POLICY_RANK_TRAILING:
            if context.rank_direction < 0 and peak >= objective_percent:
                return ExitDecision(True, "RANK_FALLING", context.at, context.unrealized_percent)
            if not context.on_board:
                return ExitDecision(True, "LEFT_TRENDING", context.at, context.unrealized_percent)

        elif policy == POLICY_THESIS_CONTINUATION:
            if not context.thesis_active and peak >= objective_percent:
                return ExitDecision(True, "THESIS_ENDED", context.at, context.unrealized_percent)

        elif policy == POLICY_STORY_CONTINUATION:
            if not context.story_active and peak >= objective_percent:
                return ExitDecision(True, "STORY_ENDED", context.at, context.unrealized_percent)

        elif policy == POLICY_HOLDER_CONTINUATION:
            if (
                context.holder_growth is not None
                and context.holder_growth <= 0
                and peak >= objective_percent
            ):
                return ExitDecision(
                    True, "HOLDER_GROWTH_STALLED", context.at, context.unrealized_percent
                )

        elif policy == POLICY_ADAPTIVE_TRAIL:
            # Give a still-convincing runner a wider trail; tighten it the moment
            # the case stops being convincing.
            giveback = (
                trail_giveback_percent
                if _still_convincing(context)
                else trail_giveback_percent / Decimal("2")
            )
            if (
                peak >= objective_percent
                and context.unrealized_percent <= peak * (HUNDRED - giveback) / HUNDRED
            ):
                return ExitDecision(True, "ADAPTIVE_TRAIL", context.at, context.unrealized_percent)

        elif policy == POLICY_PRINCIPAL_RUNNER:
            if (
                peak >= objective_percent * Decimal("3")
                and not _still_convincing(context)
            ):
                return ExitDecision(
                    True, "RUNNER_CASE_ENDED", context.at, context.unrealized_percent
                )

        if context.seconds_held >= max_hold_seconds:
            return ExitDecision(True, "MAX_HOLD", context.at, context.unrealized_percent)

    last = ordered[-1]
    return ExitDecision(False, "STILL_OPEN", last.at, last.unrealized_percent)


@dataclass(frozen=True, slots=True)
class PolicyComparison:
    policy: str
    exited: bool
    reason: str
    exit_percent: Decimal
    versus_champion: Decimal

    def to_json(self) -> dict[str, object]:
        return {
            "policy": self.policy,
            "exited": self.exited,
            "reason": self.reason,
            "exit_percent": str(self.exit_percent),
            "versus_champion": str(self.versus_champion),
        }


def compare_policies(
    observations: Sequence[TrendingExitContext],
    *,
    policies: Sequence[str] = TRENDING_EXIT_POLICIES,
) -> tuple[PolicyComparison, ...]:
    """Run every challenger against the champion on one position's history."""

    results = {
        policy: evaluate_policy(policy, observations) for policy in policies
    }
    champion = results.get(POLICY_CHAMPION)
    baseline = champion.unrealized_percent if champion else ZERO
    return tuple(
        PolicyComparison(
            policy=policy,
            exited=decision.exit,
            reason=decision.reason,
            exit_percent=decision.unrealized_percent,
            versus_champion=(decision.unrealized_percent - baseline),
        )
        for policy, decision in results.items()
    )


# --- post-exit evaluation (section 73) ---------------------------------------
@dataclass(frozen=True, slots=True)
class PostExitReview:
    """What happened after we sold.  Evaluation only — never fed back as input."""

    mint: str
    stayed_trending: bool = False
    rank_kept_climbing: bool = False
    holders_kept_growing: bool = False
    attention_continued: bool = False
    later_mfe_percent: Decimal | None = None

    @property
    def exited_too_early(self) -> bool:
        return bool(
            self.later_mfe_percent is not None
            and self.later_mfe_percent >= Decimal("25")
            and (self.stayed_trending or self.rank_kept_climbing)
        )

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "stayed_trending": self.stayed_trending,
            "rank_kept_climbing": self.rank_kept_climbing,
            "holders_kept_growing": self.holders_kept_growing,
            "attention_continued": self.attention_continued,
            "later_mfe_percent": (
                None if self.later_mfe_percent is None else str(self.later_mfe_percent)
            ),
            "exited_too_early": self.exited_too_early,
        }
