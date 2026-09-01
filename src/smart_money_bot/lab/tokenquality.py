"""Is real money going into this, or is it a name with a chart?

The operator's own manual filter is the reference for what "real" looks like:
Pump.fun only, no wash trading, original socials and avatar, no mint authority,
age 2m–360m, MC $25K–$10M, liquidity ≥ $10K, 5m volume ≥ $5K, 5m transactions
≥ 40, total fees ≥ 0.5 SOL, holders ≥ 50, top 10 ≤ 40%, dev ≤ 10%, insiders
≤ 30%, bundlers ≤ 30%, snipers ≤ 30%.

That filter is used here as a **cohort, not a cliff**.  A token with 47 holders
instead of 50, or 0.4 SOL of fees instead of 0.5, is not categorically different
from one that clears — and if it also has smart money, a real story and
accelerating rank, refusing to look at it is the expensive mistake.  So every
bar below is a continuous ramp: falling short costs points, it does not delete
the candidate.

The one thing this module treats as near-decisive is **fee velocity**.  Market
cap can be walked up on almost nothing; liquidity can be parked and pulled; but
fees are money that has already left someone's wallet, and 9.2 SOL/min against
0.8 SOL/min is the clearest separation the two same-name $SNP500 tokens ever
showed.  It is still a ramp — just a steeper one.

Pure logic: no provider, no database, no signer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .clone import TokenFacts

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
CENT = Decimal("0.01")


def ramp(value: Decimal | None, floor: Decimal, target: Decimal) -> Decimal:
    """0 at or below ``floor``, 1 at or above ``target``, linear between.

    ``None`` scores zero rather than being skipped: an unmeasured token has not
    demonstrated anything.  It is not *penalised* either — the weight simply
    goes unearned, and :attr:`QualityScore.measured` says how much of the score
    was actually observable so a thin token cannot look strong by being unknown.
    """

    if value is None or target <= floor:
        return ZERO
    if value <= floor:
        return ZERO
    if value >= target:
        return ONE
    return ((value - floor) / (target - floor)).quantize(Decimal("0.0001"))


def inverse_ramp(value: Decimal | None, good: Decimal, bad: Decimal) -> Decimal:
    """1 at or below ``good``, 0 at or above ``bad``.  For risk rates."""

    if value is None or bad <= good:
        return ZERO
    if value <= good:
        return ONE
    if value >= bad:
        return ZERO
    return ((bad - value) / (bad - good)).quantize(Decimal("0.0001"))


@dataclass(frozen=True, slots=True)
class QualityConfig:
    """Where each ramp starts and finishes.

    Every pair is (no credit, full credit).  The operator's manual thresholds
    sit inside these ranges rather than at their edges, so clearing the manual
    filter scores well and just missing it still scores.
    """

    # ---- depth ---------------------------------------------------------
    liquidity_floor_usd: Decimal = Decimal("4000")
    liquidity_target_usd: Decimal = Decimal("15000")
    # ---- real trading --------------------------------------------------
    volume_floor_usd: Decimal = Decimal("2000")
    volume_target_usd: Decimal = Decimal("15000")
    volume_to_liquidity_floor: Decimal = Decimal("0.3")
    volume_to_liquidity_target: Decimal = Decimal("2")
    # ---- fees: money that already moved --------------------------------
    fee_velocity_floor: Decimal = Decimal("0.05")
    fee_velocity_target: Decimal = Decimal("1.5")
    # ---- participation --------------------------------------------------
    holders_floor: Decimal = Decimal("20")
    holders_target: Decimal = Decimal("120")
    swaps_floor: Decimal = Decimal("20")
    swaps_target: Decimal = Decimal("120")
    # ---- ownership risk, as rates 0..1 ---------------------------------
    top10_good: Decimal = Decimal("0.25")
    top10_bad: Decimal = Decimal("0.55")
    dev_good: Decimal = Decimal("0.03")
    dev_bad: Decimal = Decimal("0.15")
    bundler_good: Decimal = Decimal("0.10")
    bundler_bad: Decimal = Decimal("0.40")
    sniper_good: Decimal = Decimal("0.10")
    sniper_bad: Decimal = Decimal("0.40")
    insider_good: Decimal = Decimal("0.10")
    insider_bad: Decimal = Decimal("0.35")

    # ---- weights.  Fee velocity leads because it is the hardest to fake.
    weight_fee_velocity: Decimal = Decimal("26")
    weight_liquidity: Decimal = Decimal("16")
    weight_volume: Decimal = Decimal("14")
    weight_depth_ratio: Decimal = Decimal("10")
    weight_holders: Decimal = Decimal("12")
    weight_swaps: Decimal = Decimal("6")
    weight_ownership: Decimal = Decimal("16")

    #: Below this, a candidate is thin enough that a ping would be noise.
    ping_min_score: Decimal = Decimal("55")
    #: The only score that actually *withholds* a ping, and it does so only for
    #: a token we could see clearly.  Everything between this and
    #: ``ping_min_score`` still reaches the operator — the operator's complaint
    #: was fake coins getting through, not real ones being shown.
    ping_block_score: Decimal = Decimal("30")
    #: At least this fraction of the score must come from fields we actually
    #: measured.  Otherwise a token wins by being unknown.
    min_measured_fraction: Decimal = Decimal("0.5")


DEFAULT_QUALITY_CONFIG = QualityConfig()


@dataclass(frozen=True, slots=True)
class QualityScore:
    """How real this looks, and which parts of that we actually observed."""

    mint: str
    score: Decimal = ZERO
    measured_weight: Decimal = ZERO
    total_weight: Decimal = ZERO
    components: tuple[tuple[str, str], ...] = ()
    reasons: tuple[str, ...] = field(default_factory=tuple)
    concerns: tuple[str, ...] = field(default_factory=tuple)
    # The three figures the operator named, carried verbatim so the card can
    # print them whatever they are.  A number only shown once it clears a
    # threshold is a number the operator cannot use to form a judgement — and
    # forming that judgement themselves is the whole point of showing it.
    fee_velocity_sol_per_minute: Decimal | None = None
    liquidity_usd: Decimal | None = None
    holder_count: int | None = None

    @property
    def measured_fraction(self) -> Decimal:
        if self.total_weight <= ZERO:
            return ZERO
        return (self.measured_weight / self.total_weight).quantize(Decimal("0.01"))

    def confident(self, *, config: QualityConfig = DEFAULT_QUALITY_CONFIG) -> bool:
        """Enough of the picture was visible for the score to mean anything."""

        return self.measured_fraction >= config.min_measured_fraction

    def strong(self, *, config: QualityConfig = DEFAULT_QUALITY_CONFIG) -> bool:
        return self.score >= config.ping_min_score and self.confident(config=config)

    def weak(self, *, config: QualityConfig = DEFAULT_QUALITY_CONFIG) -> bool:
        """Measurable, and measured badly.  The only quality state that gates.

        An *unmeasured* token is never weak.  A DEX-only snapshot cannot see
        fees, holders or ownership, and treating "we could not look" as "there
        is nothing there" would silently switch off the whole Pump lane — the
        opposite of what the operator asked for.
        """

        return self.confident(config=config) and self.score < config.ping_block_score

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "score": str(self.score),
            "fee_velocity_sol_per_minute": (
                None
                if self.fee_velocity_sol_per_minute is None
                else str(self.fee_velocity_sol_per_minute)
            ),
            "liquidity_usd": None if self.liquidity_usd is None else str(self.liquidity_usd),
            "holder_count": self.holder_count,
            "measured_fraction": str(self.measured_fraction),
            "confident": self.confident(),
            "strong": self.strong(),
            "weak": self.weak(),
            "components": [list(item) for item in self.components],
            "reasons": list(self.reasons),
            "concerns": list(self.concerns),
        }


def score_quality(
    facts: TokenFacts,
    *,
    config: QualityConfig = DEFAULT_QUALITY_CONFIG,
) -> QualityScore:
    """Score how much real money and real participation this token shows.

    Nothing here is a gate.  The score raises or lowers conviction, and the
    caller decides — which is what keeps a 47-holder token with smart money and
    a story reachable while a 50-holder token with nothing behind it is not.
    """

    parts: list[tuple[str, Decimal, Decimal, bool]] = []

    fee_velocity = facts.fee_velocity_sol_per_minute
    parts.append(
        (
            "fee velocity",
            ramp(fee_velocity, config.fee_velocity_floor, config.fee_velocity_target),
            config.weight_fee_velocity,
            fee_velocity is not None,
        )
    )
    parts.append(
        (
            "liquidity",
            ramp(facts.liquidity_usd, config.liquidity_floor_usd, config.liquidity_target_usd),
            config.weight_liquidity,
            facts.liquidity_usd is not None,
        )
    )
    parts.append(
        (
            "volume",
            ramp(facts.volume_usd, config.volume_floor_usd, config.volume_target_usd),
            config.weight_volume,
            facts.volume_usd is not None,
        )
    )
    depth = facts.volume_to_liquidity
    parts.append(
        (
            "volume vs liquidity",
            ramp(depth, config.volume_to_liquidity_floor, config.volume_to_liquidity_target),
            config.weight_depth_ratio,
            depth is not None,
        )
    )
    holders = None if facts.holder_count is None else Decimal(facts.holder_count)
    parts.append(
        (
            "holders",
            ramp(holders, config.holders_floor, config.holders_target),
            config.weight_holders,
            holders is not None,
        )
    )
    swaps = (
        Decimal((facts.buys or 0) + (facts.sells or 0))
        if (facts.buys is not None or facts.sells is not None)
        else None
    )
    parts.append(
        (
            "transactions",
            ramp(swaps, config.swaps_floor, config.swaps_target),
            config.weight_swaps,
            swaps is not None,
        )
    )

    # Ownership risk is one weighted family: four rates that all say the same
    # kind of thing, so counting them separately would let concentration
    # outvote every measure of real demand.
    risk_parts = [
        (facts.top10_holder_rate, config.top10_good, config.top10_bad, "top 10 concentration"),
        (facts.dev_hold_rate, config.dev_good, config.dev_bad, "dev holding"),
        (facts.bundler_rate, config.bundler_good, config.bundler_bad, "bundlers"),
        (facts.sniper_hold_rate, config.sniper_good, config.sniper_bad, "snipers"),
        (facts.insider_rate, config.insider_good, config.insider_bad, "insiders"),
    ]
    measured_risk = [item for item in risk_parts if item[0] is not None]
    if measured_risk:
        risk_score = sum(
            (inverse_ramp(value, good, bad) for value, good, bad, _ in measured_risk), ZERO
        ) / Decimal(len(measured_risk))
    else:
        risk_score = ZERO
    parts.append(("ownership", risk_score, config.weight_ownership, bool(measured_risk)))

    total_weight = sum((weight for _, _, weight, _ in parts), ZERO)
    earned = sum((fraction * weight for _, fraction, weight, _ in parts), ZERO)
    measured_weight = sum((weight for _, _, weight, seen in parts if seen), ZERO)

    reasons: list[str] = []
    concerns: list[str] = []
    if fee_velocity is not None and fee_velocity >= config.fee_velocity_target:
        reasons.append(f"{fee_velocity} SOL/min in fees — real money is moving now")
    elif fee_velocity is not None and fee_velocity <= config.fee_velocity_floor:
        concerns.append(f"only {fee_velocity} SOL/min in fees for its age")
    if facts.liquidity_usd is not None and facts.liquidity_usd >= config.liquidity_target_usd:
        reasons.append(f"liquidity ${facts.liquidity_usd:,.0f}")
    elif facts.liquidity_usd is not None and facts.liquidity_usd < config.liquidity_floor_usd:
        concerns.append(f"thin liquidity ${facts.liquidity_usd:,.0f}")
    if depth is not None and depth >= config.volume_to_liquidity_target:
        reasons.append(f"volume {depth}x liquidity")
    if holders is not None and holders >= config.holders_target:
        reasons.append(f"{int(holders)} holders")
    elif holders is not None and holders < config.holders_floor:
        concerns.append(f"only {int(holders)} holders")
    for value, _good, bad, label in measured_risk:
        if value is not None and value >= bad:
            concerns.append(f"{label} at {(value * HUNDRED).quantize(CENT)}%")

    return QualityScore(
        mint=facts.mint,
        fee_velocity_sol_per_minute=fee_velocity,
        liquidity_usd=facts.liquidity_usd,
        holder_count=facts.holder_count,
        score=(earned).quantize(CENT),
        measured_weight=measured_weight,
        total_weight=total_weight,
        components=tuple(
            (name, str((fraction * weight).quantize(CENT))) for name, fraction, weight, _ in parts
        ),
        reasons=tuple(reasons),
        concerns=tuple(concerns),
    )


def rank_candidates(
    facts: list[TokenFacts],
    *,
    config: QualityConfig = DEFAULT_QUALITY_CONFIG,
) -> list[tuple[TokenFacts, QualityScore]]:
    """Best first.

    This is what stops a real runner sitting behind two hundred dead launches in
    feed order — the production symptom was a token first seen at $9.87K that
    was not evaluated until it had already reached $40.71K.
    """

    scored = [(item, score_quality(item, config=config)) for item in facts]
    scored.sort(key=lambda pair: (-pair[1].score, pair[0].mint))
    return scored
