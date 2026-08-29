"""Performance-based public-wallet reputation (sections V, AF, AG).

A large wallet is not a smart wallet.  Reputation here is earned only from
observed forward outcomes of that wallet's *public* entries, decays with time,
and is never allowed to override safety, overextension or liquidity gates.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal

from .config import DEFAULT_LAB_CONFIG, LabConfig

ZERO = Decimal("0")

PROVEN_EARLY = "PROVEN_EARLY"
USEFUL_CONFIRMATION = "USEFUL_CONFIRMATION"
UNKNOWN = "UNKNOWN"
LATE_CHASER = "LATE_CHASER"
POOR_HISTORY = "POOR_HISTORY"

#: Below this many observed entries a wallet can add colour but not weight.
MIN_REPUTATION_SAMPLE = 8

ACCUMULATING = "ACCUMULATING"
HOLDING = "HOLDING"
DISTRIBUTING = "DISTRIBUTING"
UNCLEAR = "UNCLEAR"


@dataclass(frozen=True, slots=True)
class WalletOutcome:
    """One observed public entry and what happened afterwards."""

    wallet: str
    mint: str
    entered_at: int
    entry_market_cap_usd: Decimal | None = None
    forward_return_percent: Decimal | None = None
    max_favourable_percent: Decimal | None = None
    max_adverse_percent: Decimal | None = None
    entered_after_move_percent: Decimal | None = None
    rugged: bool = False
    distributed_into_strength: bool = False


@dataclass(frozen=True, slots=True)
class WalletReputation:
    """Rolling, decaying, sample-aware reputation for one public wallet."""

    wallet: str
    samples: int = 0
    median_forward_return_percent: Decimal | None = None
    hit_10_percent: Decimal | None = None
    hit_25_percent: Decimal | None = None
    hit_50_percent: Decimal | None = None
    hit_100_percent: Decimal | None = None
    median_drawdown_percent: Decimal | None = None
    rugs_entered: int = 0
    chase_entries: int = 0
    early_entries: int = 0
    distribution_events: int = 0
    recent_return_percent: Decimal | None = None
    score: Decimal = Decimal("50")
    state: str = UNKNOWN
    updated_at: int = 0

    @property
    def has_material_sample(self) -> bool:
        return self.samples >= MIN_REPUTATION_SAMPLE

    @property
    def early_rate_percent(self) -> Decimal | None:
        if not self.samples:
            return None
        return (Decimal(self.early_entries) / Decimal(self.samples) * 100).quantize(
            Decimal("0.01")
        )

    @property
    def chase_rate_percent(self) -> Decimal | None:
        if not self.samples:
            return None
        return (Decimal(self.chase_entries) / Decimal(self.samples) * 100).quantize(
            Decimal("0.01")
        )

    @property
    def rug_rate_percent(self) -> Decimal | None:
        if not self.samples:
            return None
        return (Decimal(self.rugs_entered) / Decimal(self.samples) * 100).quantize(
            Decimal("0.01")
        )


def build_reputation(
    wallet: str,
    outcomes: Sequence[WalletOutcome],
    *,
    now: int = 0,
    recent_window: int = 10,
) -> WalletReputation:
    """Compute reputation from the wallet's observed forward outcomes."""

    measured = [item for item in outcomes if item.forward_return_percent is not None]
    if not outcomes:
        return WalletReputation(wallet=wallet, updated_at=now)

    returns = sorted(
        item.forward_return_percent for item in measured if item.forward_return_percent is not None
    )
    median = _median(returns)
    peaks = [
        item.max_favourable_percent
        for item in outcomes
        if item.max_favourable_percent is not None
    ]
    drawdowns = sorted(
        item.max_adverse_percent for item in outcomes if item.max_adverse_percent is not None
    )
    sample = len(outcomes)
    ordered = sorted(outcomes, key=lambda item: item.entered_at)
    recent = ordered[-recent_window:]
    recent_returns = [
        item.forward_return_percent
        for item in recent
        if item.forward_return_percent is not None
    ]

    early = sum(
        1
        for item in outcomes
        if item.entered_after_move_percent is not None and item.entered_after_move_percent <= 25
    )
    chase = sum(
        1
        for item in outcomes
        if item.entered_after_move_percent is not None and item.entered_after_move_percent >= 100
    )
    rugs = sum(1 for item in outcomes if item.rugged)
    distribution = sum(1 for item in outcomes if item.distributed_into_strength)

    reputation = WalletReputation(
        wallet=wallet,
        samples=sample,
        median_forward_return_percent=median,
        hit_10_percent=_hit_rate(peaks, Decimal("10")),
        hit_25_percent=_hit_rate(peaks, Decimal("25")),
        hit_50_percent=_hit_rate(peaks, Decimal("50")),
        hit_100_percent=_hit_rate(peaks, Decimal("100")),
        median_drawdown_percent=_median(drawdowns),
        rugs_entered=rugs,
        chase_entries=chase,
        early_entries=early,
        distribution_events=distribution,
        recent_return_percent=_median(sorted(recent_returns)),
        updated_at=now,
    )
    scored = replace(reputation, score=_score(reputation))
    return replace(scored, state=classify_wallet(scored))


def classify_wallet(reputation: WalletReputation) -> str:
    """Map a reputation onto its state; small samples stay ``UNKNOWN``."""

    if not reputation.has_material_sample:
        return UNKNOWN
    if reputation.rug_rate_percent is not None and reputation.rug_rate_percent >= 30:
        return POOR_HISTORY
    if (
        reputation.median_forward_return_percent is not None
        and reputation.median_forward_return_percent <= Decimal("-10")
    ):
        return POOR_HISTORY
    if reputation.chase_rate_percent is not None and reputation.chase_rate_percent >= 50:
        return LATE_CHASER
    if (
        reputation.early_rate_percent is not None
        and reputation.early_rate_percent >= 50
        and reputation.hit_50_percent is not None
        and reputation.hit_50_percent >= 30
    ):
        return PROVEN_EARLY
    if (
        reputation.hit_25_percent is not None
        and reputation.hit_25_percent >= 35
        and (reputation.median_forward_return_percent or ZERO) > 0
    ):
        return USEFUL_CONFIRMATION
    return UNKNOWN


def decay_reputation(
    reputation: WalletReputation,
    *,
    now: int,
    half_life_seconds: int = 1_209_600,
) -> WalletReputation:
    """Pull an unrefreshed reputation back towards neutral over time."""

    if not reputation.updated_at or now <= reputation.updated_at:
        return reputation
    elapsed = now - reputation.updated_at
    if elapsed <= 0 or half_life_seconds <= 0:
        return reputation
    periods = Decimal(elapsed) / Decimal(half_life_seconds)
    weight = Decimal("0.5") ** min(periods, Decimal("8"))
    neutral = Decimal("50")
    decayed = (neutral + (reputation.score - neutral) * weight).quantize(Decimal("0.01"))
    updated = replace(reputation, score=decayed, updated_at=now)
    if periods >= 2 and updated.state in {PROVEN_EARLY, USEFUL_CONFIRMATION}:
        updated = replace(updated, state=UNKNOWN)
    return updated


@dataclass(frozen=True, slots=True)
class SmartMoneyAssessment:
    """Aggregate smart-money evidence for one mint at one instant."""

    wallets: tuple[str, ...] = ()
    proven_early: int = 0
    useful_confirmation: int = 0
    late_chasers: int = 0
    poor_history: int = 0
    unknown: int = 0
    independent_clusters: int = 0
    shared_funding: bool = False
    synchronized_entries: bool = False
    strength: Decimal = ZERO
    posture: str = UNCLEAR
    warnings: tuple[str, ...] = ()
    supporting: tuple[str, ...] = ()
    stale_signals: int = 0
    reputations: tuple[WalletReputation, ...] = field(default_factory=tuple)

    @property
    def is_supporting_evidence(self) -> bool:
        """Smart money can strengthen a valid setup; it can never create one."""

        return self.strength > 0 and not self.shared_funding and not self.synchronized_entries


def assess_smart_money(
    reputations: Sequence[WalletReputation],
    *,
    independent_clusters: int = 0,
    shared_funding: bool = False,
    synchronized_entries: bool = False,
    entry_ages_seconds: Sequence[int] = (),
    max_signal_age_seconds: int = 3_600,
    sell_events: int = 0,
    buy_events: int = 0,
    config: LabConfig = DEFAULT_LAB_CONFIG,
) -> SmartMoneyAssessment:
    """Combine per-wallet reputation into one bounded, honest strength score."""

    del config  # thresholds here are structural, not tunable knobs
    if not reputations:
        return SmartMoneyAssessment()

    proven = sum(1 for item in reputations if item.state == PROVEN_EARLY)
    useful = sum(1 for item in reputations if item.state == USEFUL_CONFIRMATION)
    late = sum(1 for item in reputations if item.state == LATE_CHASER)
    poor = sum(1 for item in reputations if item.state == POOR_HISTORY)
    unknown = sum(1 for item in reputations if item.state == UNKNOWN)
    stale = sum(1 for age in entry_ages_seconds if age > max_signal_age_seconds)

    strength = Decimal(proven) * 22 + Decimal(useful) * 10 - Decimal(late) * 8 - Decimal(poor) * 14
    warnings: list[str] = []
    supporting: list[str] = []

    if shared_funding:
        strength -= 25
        warnings.append("Smart wallets share a funder — treated as one actor, not consensus")
    if synchronized_entries:
        strength -= 20
        warnings.append("Smart-wallet entries were synchronized")
    if independent_clusters >= 3:
        strength += 12
        supporting.append("Three or more independently funded wallets")
    elif independent_clusters <= 1 and len(reputations) > 1:
        strength -= 10
        warnings.append("All smart wallets fall in one funding cluster")
    if stale:
        strength -= Decimal(stale) * 6
        warnings.append(f"{stale} smart-wallet signal(s) are stale")
    if unknown and not proven and not useful:
        warnings.append("No wallet has a material forward sample yet")

    if proven:
        supporting.append(f"{proven} wallet(s) with a proven early record")
    if useful:
        supporting.append(f"{useful} wallet(s) useful as confirmation")

    bounded = max(Decimal("-100"), min(Decimal("100"), strength))
    return SmartMoneyAssessment(
        wallets=tuple(item.wallet for item in reputations),
        proven_early=proven,
        useful_confirmation=useful,
        late_chasers=late,
        poor_history=poor,
        unknown=unknown,
        independent_clusters=independent_clusters,
        shared_funding=shared_funding,
        synchronized_entries=synchronized_entries,
        strength=bounded,
        posture=smart_money_posture(buy_events=buy_events, sell_events=sell_events),
        warnings=tuple(warnings),
        supporting=tuple(supporting),
        stale_signals=stale,
        reputations=tuple(reputations),
    )


def smart_money_posture(*, buy_events: int, sell_events: int) -> str:
    """Accumulating / holding / distributing, only where it is defensible."""

    if buy_events <= 0 and sell_events <= 0:
        return UNCLEAR
    if sell_events == 0 and buy_events >= 2:
        return ACCUMULATING
    if buy_events == 0 and sell_events >= 2:
        return DISTRIBUTING
    total = buy_events + sell_events
    if total < 3:
        return UNCLEAR
    buy_share = Decimal(buy_events) / Decimal(total)
    if buy_share >= Decimal("0.7"):
        return ACCUMULATING
    if buy_share <= Decimal("0.3"):
        return DISTRIBUTING
    return HOLDING


def hold_support(
    assessment: SmartMoneyAssessment,
    *,
    organic_healthy: bool,
    momentum_healthy: bool,
    liquidity_healthy: bool,
) -> bool:
    """Whether smart money *may* support keeping more simulated upside.

    Never true on wallet evidence alone — the organic, momentum and liquidity
    conditions must all hold as well.
    """

    return bool(
        assessment.posture == ACCUMULATING
        and assessment.is_supporting_evidence
        and organic_healthy
        and momentum_healthy
        and liquidity_healthy
    )


def exit_pressure(
    assessment: SmartMoneyAssessment,
    *,
    flow_weakening: bool,
    liquidity_worsening: bool,
) -> bool:
    """Whether smart-money behaviour strengthens the case to de-risk.

    One wallet selling is never enough on its own.
    """

    return bool(
        assessment.posture == DISTRIBUTING and (flow_weakening or liquidity_worsening)
    )


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return ((values[middle - 1] + values[middle]) / 2).quantize(Decimal("0.01"))


def _hit_rate(peaks: Sequence[Decimal], threshold: Decimal) -> Decimal | None:
    if not peaks:
        return None
    hits = sum(1 for value in peaks if value >= threshold)
    return (Decimal(hits) / Decimal(len(peaks)) * 100).quantize(Decimal("0.01"))


def _score(reputation: WalletReputation) -> Decimal:
    score = Decimal("50")
    if reputation.median_forward_return_percent is not None:
        score += max(
            Decimal("-25"), min(Decimal("25"), reputation.median_forward_return_percent / 2)
        )
    if reputation.hit_50_percent is not None:
        score += reputation.hit_50_percent / 4
    if reputation.hit_100_percent is not None:
        score += reputation.hit_100_percent / 5
    if reputation.rug_rate_percent is not None:
        score -= reputation.rug_rate_percent / 2
    if reputation.chase_rate_percent is not None:
        score -= reputation.chase_rate_percent / 4
    if reputation.early_rate_percent is not None:
        score += reputation.early_rate_percent / 6
    if not reputation.has_material_sample:
        score = Decimal("50") + (score - Decimal("50")) / 3
    return max(ZERO, min(Decimal("100"), score)).quantize(Decimal("0.01"))
