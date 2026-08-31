"""Pump.fun lifecycle and bonding progress, from the program's own on-chain state.

The failure mode this replaces (section 6): *wait until the token graduates, then
analyse it.*  By graduation the interesting part is frequently over.  So the
engine now tracks a token across its whole life, and every stage below is a
first-class state a candidate can be surfaced in.

**Progress is computed, not guessed (section 7).**  The Pump.fun bonding curve
account publishes `virtual_token_reserves`, `virtual_sol_reserves`,
`real_token_reserves`, `real_sol_reserves`, `token_total_supply` and `complete`.
A curve is finished when the purchasable supply is exhausted and `complete` flips
true — so progress is *(sold / available)*, read from chain, and graduation is
read from the `complete` flag.  Age is never used to infer graduation: a
six-hour-old token can be at 4% and a four-minute-old one can be at 96%.

The constants below are the documented launch parameters of the curve. They are
used only as a *fallback denominator* when an account read did not return the
real reserves; whenever the chain gives us the real numbers, the chain wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")
HUNDRED = Decimal("100")

#: The Pump.fun program.  Public, and already used elsewhere in this codebase.
PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
#: The seed the bonding-curve PDA is derived from.
BONDING_CURVE_SEED = b"bonding-curve"

#: Documented launch parameters, in base units.
INITIAL_VIRTUAL_TOKEN_RESERVES = Decimal("1073000000000000")
INITIAL_VIRTUAL_SOL_RESERVES = Decimal("30000000000")
INITIAL_REAL_TOKEN_RESERVES = Decimal("793100000000000")
TOTAL_TOKEN_SUPPLY = Decimal("1000000000000000")

# --- lifecycle states (section 7) --------------------------------------------
#: Just created; almost no trading yet.
STAGE_NEW = "NEW"
#: On the curve, early.
STAGE_EARLY_CURVE = "EARLY_CURVE"
#: On the curve, meaningfully filled.
STAGE_MID_CURVE = "MID_CURVE"
#: Close enough to graduation that the route is about to change.
STAGE_ALMOST_BONDED = "ALMOST_BONDED"
#: The curve is complete but migration has not been observed yet.
STAGE_GRADUATING = "GRADUATING"
#: Migrated recently.
STAGE_RECENTLY_BONDED = "RECENTLY_BONDED"
#: Trading on PumpSwap.
STAGE_PUMPSWAP = "PUMPSWAP"
#: Old enough that "new" is not a useful description.
STAGE_MATURE = "MATURE"
#: We could not read the curve at all.  Not a stage — an admission.
STAGE_UNKNOWN = "UNKNOWN"

LIFECYCLE_STAGES: tuple[str, ...] = (
    STAGE_NEW,
    STAGE_EARLY_CURVE,
    STAGE_MID_CURVE,
    STAGE_ALMOST_BONDED,
    STAGE_GRADUATING,
    STAGE_RECENTLY_BONDED,
    STAGE_PUMPSWAP,
    STAGE_MATURE,
    STAGE_UNKNOWN,
)

#: Stages before the curve completes.
PRE_GRADUATION_STAGES: frozenset[str] = frozenset(
    {STAGE_NEW, STAGE_EARLY_CURVE, STAGE_MID_CURVE, STAGE_ALMOST_BONDED}
)
#: Stages after it completes.
POST_GRADUATION_STAGES: frozenset[str] = frozenset(
    {STAGE_GRADUATING, STAGE_RECENTLY_BONDED, STAGE_PUMPSWAP, STAGE_MATURE}
)
#: The three Trenches sections an operator actually browses.
TRENCH_NEW_STAGES: frozenset[str] = frozenset({STAGE_NEW, STAGE_EARLY_CURVE})
TRENCH_ALMOST_BONDED_STAGES: frozenset[str] = frozenset(
    {STAGE_ALMOST_BONDED, STAGE_GRADUATING}
)
TRENCH_RECENTLY_BONDED_STAGES: frozenset[str] = frozenset(
    {STAGE_RECENTLY_BONDED, STAGE_PUMPSWAP}
)

STAGE_LABELS: dict[str, str] = {
    STAGE_NEW: "NEW",
    STAGE_EARLY_CURVE: "EARLY CURVE",
    STAGE_MID_CURVE: "MID CURVE",
    STAGE_ALMOST_BONDED: "ALMOST BONDED",
    STAGE_GRADUATING: "GRADUATING",
    STAGE_RECENTLY_BONDED: "RECENTLY BONDED",
    STAGE_PUMPSWAP: "PUMPSWAP",
    STAGE_MATURE: "MATURE",
    STAGE_UNKNOWN: "UNKNOWN",
}


@dataclass(frozen=True, slots=True)
class BondingCurveState:
    """A decoded Pump.fun bonding-curve account.

    ``available`` is ``None`` when the read failed.  Every derived value then
    reports ``None`` rather than falling back to a comfortable number — an
    unreadable curve is an unknown curve, not an empty one.
    """

    mint: str
    available: bool = False
    virtual_token_reserves: Decimal | None = None
    virtual_sol_reserves: Decimal | None = None
    real_token_reserves: Decimal | None = None
    real_sol_reserves: Decimal | None = None
    token_total_supply: Decimal | None = None
    complete: bool = False
    creator: str = ""
    #: Documented special modes that change supply or trading behaviour.  A token
    #: in one of these is not a normal token and must not be scored as one
    #: (section 28).
    is_mayhem_mode: bool = False
    is_cashback_coin: bool = False
    error: str = ""

    @property
    def special_mode(self) -> bool:
        return self.is_mayhem_mode or self.is_cashback_coin

    @property
    def special_mode_label(self) -> str:
        modes = []
        if self.is_mayhem_mode:
            modes.append("MAYHEM")
        if self.is_cashback_coin:
            modes.append("CASHBACK")
        return "+".join(modes)

    def progress_percent(self) -> Decimal | None:
        """How much of the purchasable supply has been bought (section 8).

        Uses the account's own ``token_total_supply`` where the chain supplies a
        sane initial figure, and the documented launch parameter only as a
        fallback denominator.  Returns ``None`` when we could not read enough to
        say anything honest.
        """

        if not self.available:
            return None
        if self.complete:
            return HUNDRED
        remaining = self.real_token_reserves
        if remaining is None:
            return None
        initial = INITIAL_REAL_TOKEN_RESERVES
        if initial <= ZERO:
            return None
        sold = initial - remaining
        if sold <= ZERO:
            return ZERO
        return min(HUNDRED, (sold / initial * HUNDRED)).quantize(Decimal("0.01"))

    def price_sol(self) -> Decimal | None:
        """Spot price from the constant-product virtual reserves."""

        if (
            not self.available
            or self.virtual_sol_reserves is None
            or self.virtual_token_reserves is None
            or self.virtual_token_reserves <= ZERO
        ):
            return None
        # Both reserves are in base units; the 1e9 / 1e6 decimal ratio is what
        # converts lamports-per-base-unit into SOL per whole token.
        return (
            self.virtual_sol_reserves / self.virtual_token_reserves * Decimal("1000")
        )

    def sol_in_curve(self) -> Decimal | None:
        """Real SOL actually accumulated, in SOL rather than lamports."""

        if not self.available or self.real_sol_reserves is None:
            return None
        return (self.real_sol_reserves / Decimal("1000000000")).quantize(Decimal("0.001"))

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "available": self.available,
            "virtual_token_reserves": _s(self.virtual_token_reserves),
            "virtual_sol_reserves": _s(self.virtual_sol_reserves),
            "real_token_reserves": _s(self.real_token_reserves),
            "real_sol_reserves": _s(self.real_sol_reserves),
            "token_total_supply": _s(self.token_total_supply),
            "complete": self.complete,
            "creator": self.creator,
            "is_mayhem_mode": self.is_mayhem_mode,
            "is_cashback_coin": self.is_cashback_coin,
            "progress_percent": _s(self.progress_percent()),
            "sol_in_curve": _s(self.sol_in_curve()),
            "error": self.error,
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True, slots=True)
class LifecycleConfig:
    """Bounded, named thresholds so no magic number lives in the logic."""

    new_seconds: int = 300
    early_curve_percent: Decimal = Decimal("15")
    mid_curve_percent: Decimal = Decimal("75")
    almost_bonded_percent: Decimal = Decimal("75")
    recently_bonded_seconds: int = 3_600
    mature_seconds: int = 86_400


DEFAULT_LIFECYCLE_CONFIG = LifecycleConfig()


@dataclass(frozen=True, slots=True)
class LifecycleState:
    mint: str
    stage: str = STAGE_UNKNOWN
    progress_percent: Decimal | None = None
    age_seconds: int | None = None
    seconds_since_first_observed: int | None = None
    graduated_at: int | None = None
    special_mode: str = ""
    reasons: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return STAGE_LABELS.get(self.stage, self.stage)

    @property
    def pre_graduation(self) -> bool:
        return self.stage in PRE_GRADUATION_STAGES

    @property
    def almost_bonded(self) -> bool:
        return self.stage in TRENCH_ALMOST_BONDED_STAGES

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "stage": self.stage,
            "progress_percent": _s(self.progress_percent),
            "age_seconds": self.age_seconds,
            "seconds_since_first_observed": self.seconds_since_first_observed,
            "graduated_at": self.graduated_at,
            "special_mode": self.special_mode,
            "reasons": list(self.reasons),
        }


def classify_lifecycle(
    curve: BondingCurveState,
    *,
    now: int,
    created_at: int | None = None,
    first_observed_at: int | None = None,
    graduated_at: int | None = None,
    on_pumpswap: bool = False,
    config: LifecycleConfig = DEFAULT_LIFECYCLE_CONFIG,
) -> LifecycleState:
    """Decide the stage from on-chain state, never from age alone (section 7)."""

    reasons: list[str] = []
    progress = curve.progress_percent()
    age = None if created_at is None else max(0, now - created_at)
    since_seen = None if first_observed_at is None else max(0, now - first_observed_at)

    def build(stage: str) -> LifecycleState:
        return LifecycleState(
            mint=curve.mint,
            stage=stage,
            progress_percent=progress,
            age_seconds=age,
            seconds_since_first_observed=since_seen,
            graduated_at=graduated_at,
            special_mode=curve.special_mode_label,
            reasons=tuple(reasons),
        )

    if not curve.available:
        reasons.append("the bonding-curve account could not be read")
        # A token trading on PumpSwap has no curve account left to read, so an
        # unreadable curve plus observed PumpSwap activity is a graduated token,
        # not an unknown one.
        if on_pumpswap:
            reasons.append("observed trading on PumpSwap")
            return build(STAGE_PUMPSWAP)
        return build(STAGE_UNKNOWN)

    if curve.complete or on_pumpswap:
        reasons.append(
            "the curve reports complete" if curve.complete else "observed on PumpSwap"
        )
        if graduated_at is None:
            reasons.append("migration not yet observed")
            return build(STAGE_GRADUATING)
        since_graduation = max(0, now - graduated_at)
        if since_graduation <= config.recently_bonded_seconds:
            return build(STAGE_RECENTLY_BONDED)
        if age is not None and age >= config.mature_seconds:
            return build(STAGE_MATURE)
        return build(STAGE_PUMPSWAP)

    if progress is None:
        reasons.append("the curve is readable but its progress is not derivable")
        return build(STAGE_UNKNOWN)

    reasons.append(f"bonding progress {progress}%")
    if progress >= config.almost_bonded_percent:
        reasons.append("approaching graduation — the trading route is about to change")
        return build(STAGE_ALMOST_BONDED)
    if progress >= config.early_curve_percent:
        return build(STAGE_MID_CURVE)
    if age is not None and age <= config.new_seconds:
        reasons.append(f"created {age}s ago")
        return build(STAGE_NEW)
    return build(STAGE_EARLY_CURVE)


def bonding_milestones(
    previous_percent: Decimal | None,
    current_percent: Decimal | None,
    *,
    milestones: tuple[Decimal, ...] = (
        Decimal("25"),
        Decimal("50"),
        Decimal("75"),
        Decimal("90"),
        Decimal("95"),
    ),
) -> tuple[Decimal, ...]:
    """Milestones crossed since the last reading (section 43).

    Crossing one is an *event*, and an event is a reason to re-evaluate a
    candidate now rather than at the next scheduled tick.
    """

    if previous_percent is None or current_percent is None:
        return ()
    return tuple(
        milestone
        for milestone in milestones
        if previous_percent < milestone <= current_percent
    )
