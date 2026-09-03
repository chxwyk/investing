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


def _opt(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


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

    # ---- direction (v2.48) ----------------------------------------------
    # Momentum: the freshest signed move.  Flat earns nothing; the ramp opens
    # above zero because "not falling" is not the same as "running".
    momentum_floor_percent: Decimal = Decimal("0")
    momentum_target_percent: Decimal = Decimal("25")
    # Drawdown from the token's own high, as a rate.  Scored inverted: at its
    # high is full credit, far below it is none.
    drawdown_good: Decimal = Decimal("0.10")
    drawdown_bad: Decimal = Decimal("0.55")
    # Sells per buy.  One-for-one is a healthy two-way market; three sells per
    # buy is an exit wearing a volume number.
    sell_pressure_good: Decimal = Decimal("1.1")
    sell_pressure_bad: Decimal = Decimal("2.5")

    # ---- weights.  Fee velocity leads because it is the hardest to fake,
    # and direction now outweighs every static level put together.
    weight_fee_velocity: Decimal = Decimal("18")
    weight_momentum: Decimal = Decimal("14")
    weight_drawdown: Decimal = Decimal("12")
    weight_buy_pressure: Decimal = Decimal("10")
    weight_liquidity: Decimal = Decimal("9")
    weight_volume: Decimal = Decimal("7")
    weight_depth_ratio: Decimal = Decimal("7")
    weight_holders: Decimal = Decimal("9")
    weight_swaps: Decimal = Decimal("4")
    weight_ownership: Decimal = Decimal("10")

    # ---- hard disqualifiers (v2.48) --------------------------------------
    # Three states that are not "low scoring" but "not an entry at all", and
    # no amount of volume buys past them.  Each fires only on a value we
    # actually measured: unknown is never disqualifying.
    #: Below its own high by this much and the move already happened to
    #: somebody else.  This is the "ATH MC" column on every board the
    #: operator reads, finally used.
    disqualify_drawdown: Decimal = Decimal("0.70")
    #: More than this many sells per buy and the crowd is leaving, whatever
    #: the volume figure says.  A rug prints the biggest volume of its life
    #: on the way down.
    disqualify_sell_pressure: Decimal = Decimal("3")
    #: Fewer holders than this is not a market yet.
    disqualify_holders: int = 10
    #: A ping requires the holder count to have been *read*, not merely to be
    #: unobjectionable (v2.52).
    #:
    #: The refusal above only ever fired on a number we had. A token whose
    #: holder count came back empty skipped it entirely and pinged on the
    #: strength of everything else — which is how a card went out for a mint
    #: that FOMO showed with "No holders yet". Unknown was acting as
    #: permission, which is the same mistake the gate model exists to prevent,
    #: in the one place that predates it.
    #:
    #: This does not hide such a token: it still publishes, and the card says
    #: the holder count is unverified. It simply cannot interrupt anybody.
    require_verified_holders: bool = True
    #: A collapse this steep inside the freshest window is a chart falling
    #: over, not a dip to buy.
    disqualify_momentum_percent: Decimal = Decimal("-40")

    #: Below this, a candidate we could actually see is thin enough that a
    #: ping would be noise.  v2.47 set the real bar at 30 and let a token
    #: down 99.8% through; this is that bar, raised to where it belongs.
    ping_min_score: Decimal = Decimal("62")
    #: At least this fraction of the score must come from fields we actually
    #: measured before the score bar is allowed to *withhold* anything.
    #:
    #: This is deliberately high, and the reason is the whole shape of the
    #: problem.  A sixty-second-old token cannot have 120 holders, 9 SOL of
    #: fees or a meaningful all-time high — it is thin because it is **early**,
    #: which is the exact moment the operator asked to be told about it.  A
    #: DEX-only snapshot sees about half the weight here and none of the four
    #: most decisive families, so judging it against a mature token's bar
    #: would silence every genuinely early alert.
    #:
    #: The protection against a *fake* token does not live here.  It lives in
    #: the disqualifiers, which are absolute, work on partial data, and do not
    #: care how much else we could see: three holders is not a market at any
    #: age, and thirteen sells per buy is an exit at any age.
    min_measured_fraction: Decimal = Decimal("0.62")


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
    momentum_percent: Decimal | None = None
    drawdown_from_ath: Decimal | None = None
    sell_pressure: Decimal | None = None
    #: Why this is not an entry at all.  Empty for everything else.  A
    #: disqualified token is still published — the operator asked to stop
    #: being *recommended* dead charts, not to stop being able to see them —
    #: it simply cannot interrupt anybody, at any score.
    disqualifiers: tuple[str, ...] = field(default_factory=tuple)

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

    @property
    def disqualified(self) -> bool:
        return bool(self.disqualifiers)

    def weak(self, *, config: QualityConfig = DEFAULT_QUALITY_CONFIG) -> bool:
        """Not fit to interrupt a human.  The one quality state that gates.

        Two ways in.  A **disqualified** token is out whatever it scored — the
        three states in the config are structural, and a big volume number is
        exactly what a dying token produces.  Otherwise a token is weak when we
        could see it clearly and it came in under the bar.

        An *unmeasured* token is still never weak.  A DEX-only snapshot cannot
        see fees, holders or ownership, and treating "we could not look" as
        "there is nothing there" would silently switch off the whole Pump lane
        — the opposite of what the operator asked for.
        """

        if self.disqualified:
            return True
        if config.require_verified_holders and self.holder_count is None:
            # We never read how many people hold this. That is not evidence of
            # anything bad, and it is not evidence of anything good either —
            # so the card goes out and the ping does not.
            return True
        return self.confident(config=config) and self.score < config.ping_min_score

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
            "disqualified": self.disqualified,
            "disqualifiers": list(self.disqualifiers),
            "momentum_percent": _opt(self.momentum_percent),
            "drawdown_from_ath": _opt(self.drawdown_from_ath),
            "sell_pressure": _opt(self.sell_pressure),
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

    # ---- is this an entry at all? ---------------------------------------
    # Before anything is scored.  Each test fires only on a value we actually
    # measured, because "we could not look" must never read as "it is dead".
    disqualifiers: list[str] = []
    drawdown = facts.drawdown_from_ath
    if drawdown is not None and drawdown >= config.disqualify_drawdown:
        disqualifiers.append(
            f"already down {(drawdown * HUNDRED).quantize(CENT)}% from its own high "
            f"— this move happened to somebody else"
        )
    pressure = facts.sell_pressure
    if pressure is not None and pressure >= config.disqualify_sell_pressure:
        disqualifiers.append(
            f"{pressure} sells for every buy — the crowd is leaving, whatever the "
            f"volume says"
        )
    if facts.holder_count is not None and facts.holder_count < config.disqualify_holders:
        disqualifiers.append(f"only {facts.holder_count} holders — not a market yet")
    momentum = facts.momentum_percent
    if momentum is not None and momentum <= config.disqualify_momentum_percent:
        disqualifiers.append(f"{momentum}% in the last minute — the chart is falling over")

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
    # Direction, in three parts.  v2.47 had none of this, which is how a mint
    # down 99.8% with thirteen sells per buy earned full marks on volume,
    # depth ratio and transactions at once.
    parts.append(
        (
            "momentum",
            ramp(momentum, config.momentum_floor_percent, config.momentum_target_percent),
            config.weight_momentum,
            momentum is not None,
        )
    )
    parts.append(
        (
            "drawdown",
            inverse_ramp(drawdown, config.drawdown_good, config.drawdown_bad),
            config.weight_drawdown,
            drawdown is not None,
        )
    )
    parts.append(
        (
            "buy pressure",
            inverse_ramp(pressure, config.sell_pressure_good, config.sell_pressure_bad),
            config.weight_buy_pressure,
            pressure is not None,
        )
    )
    # Volume and transaction count are both scaled by which way the flow is
    # going.  Unscaled, they are the two figures a dump maximises.
    flow = (
        inverse_ramp(pressure, config.sell_pressure_good, config.sell_pressure_bad)
        if pressure is not None
        else ONE
    )
    parts.append(
        (
            "volume",
            ramp(facts.volume_usd, config.volume_floor_usd, config.volume_target_usd) * flow,
            config.weight_volume,
            facts.volume_usd is not None,
        )
    )
    depth = facts.volume_to_liquidity
    parts.append(
        (
            "volume vs liquidity",
            ramp(depth, config.volume_to_liquidity_floor, config.volume_to_liquidity_target)
            * flow,
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
            ramp(swaps, config.swaps_floor, config.swaps_target) * flow,
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

    if facts.holder_count is None and config.require_verified_holders:
        concerns.append("holder count unverified — cannot confirm anyone holds this")
    if momentum is not None and momentum >= config.momentum_target_percent:
        reasons.append(f"+{momentum}% and still moving")
    elif momentum is not None and momentum < ZERO:
        concerns.append(f"{momentum}% over the last minute")
    if drawdown is not None and drawdown <= config.drawdown_good:
        reasons.append("trading at or near its own high")
    elif drawdown is not None and drawdown >= config.drawdown_bad:
        concerns.append(f"{(drawdown * HUNDRED).quantize(CENT)}% below its high")
    if pressure is not None and pressure <= config.sell_pressure_good:
        reasons.append(f"{pressure} sells per buy — buyers still arriving")
    elif pressure is not None and pressure >= config.sell_pressure_bad:
        concerns.append(f"{pressure} sells for every buy")

    # A disqualified token scores zero.  Not "scores low" — the three states
    # above are structural, and leaving a residual score would let a big
    # volume number keep a dying chart near the top of the ranking.
    final_score = ZERO if disqualifiers else earned.quantize(CENT)

    return QualityScore(
        mint=facts.mint,
        fee_velocity_sol_per_minute=fee_velocity,
        liquidity_usd=facts.liquidity_usd,
        holder_count=facts.holder_count,
        momentum_percent=momentum,
        drawdown_from_ath=drawdown,
        sell_pressure=pressure,
        disqualifiers=tuple(disqualifiers),
        score=final_score,
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
