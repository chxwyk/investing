"""Creator intelligence: funding, holdings and prior record — as evidence, not verdict.

Terminal's documented Trenches view surfaces dev holding, dev funding source and
dev funding timing, and traders clearly find those useful.  This module builds an
independent equivalent from public Solana history.

The discipline that matters is restraint about what any of it *means*
(sections 17, 19).  A dev funded from a CEX three minutes before launch is
**context**, not proof of anything — that is also what a first-time launcher who
just bought SOL looks like.  A creator whose previous tokens all collapsed gets
the neutral label ``DEV_HISTORY_HIGH_FAILURE_RATE``; this module never calls a
person a scammer, never asserts identity beyond an observed funding edge, and
never deanonymises anyone.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")
HUNDRED = Decimal("100")

# --- funding source types (section 16) ---------------------------------------
FUNDING_CEX = "CEX"
FUNDING_KNOWN_WALLET = "KNOWN_WALLET"
FUNDING_FRESH_WALLET = "FRESH_WALLET"
FUNDING_UNKNOWN = "UNKNOWN"

FUNDING_SOURCES: tuple[str, ...] = (
    FUNDING_CEX,
    FUNDING_KNOWN_WALLET,
    FUNDING_FRESH_WALLET,
    FUNDING_UNKNOWN,
)

# --- dev holding posture (section 18) ----------------------------------------
DEV_HOLDING_STABLE = "STABLE"
DEV_HOLDING_REDUCED = "DEV_REDUCED_POSITION"
DEV_HOLDING_SELLING = "DEV_SELLING"
DEV_HOLDING_EXITED = "DEV_DISTRIBUTED"
DEV_HOLDING_UNKNOWN = "UNKNOWN"

# --- dev record labels (section 19) ------------------------------------------
#: Deliberately neutral.  Poor token outcomes are not a criminal accusation.
DEV_HISTORY_UNKNOWN = "DEV_HISTORY_UNKNOWN"
DEV_HISTORY_FIRST_TOKEN = "DEV_FIRST_OBSERVED_TOKEN"
DEV_HISTORY_MIXED = "DEV_HISTORY_MIXED"
DEV_HISTORY_HIGH_FAILURE = "DEV_HISTORY_HIGH_FAILURE_RATE"
DEV_HISTORY_HAS_GRADUATES = "DEV_HAS_PRIOR_GRADUATES"


@dataclass(frozen=True, slots=True)
class DevFunding:
    """How the creator's wallet was funded, where that is publicly observable."""

    wallet: str = ""
    source_type: str = FUNDING_UNKNOWN
    source_wallet: str = ""
    funded_at: int | None = None
    amount_sol: Decimal | None = None
    seconds_before_launch: int | None = None
    prior_signature_count: int | None = None

    @property
    def funded_just_before_launch(self) -> bool:
        """Context worth showing.  Explicitly not a scam determination (§17)."""

        return (
            self.seconds_before_launch is not None
            and 0 <= self.seconds_before_launch <= 900
        )

    def operator_line(self) -> str:
        if not self.wallet:
            return "dev funding: unknown"
        parts = [f"source `{self.source_type}`"]
        if self.amount_sol is not None:
            parts.append(f"{self.amount_sol} SOL")
        if self.seconds_before_launch is not None:
            parts.append(f"funded {self.seconds_before_launch}s before launch")
        return "dev funding: " + " • ".join(parts)

    def to_json(self) -> dict[str, object]:
        return {
            "wallet": self.wallet,
            "source_type": self.source_type,
            "source_wallet": self.source_wallet,
            "funded_at": self.funded_at,
            "amount_sol": _s(self.amount_sol),
            "seconds_before_launch": self.seconds_before_launch,
            "prior_signature_count": self.prior_signature_count,
            "funded_just_before_launch": self.funded_just_before_launch,
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True, slots=True)
class DevHolding:
    """What the creator holds now versus what they started with (section 18)."""

    wallet: str = ""
    initial_percent: Decimal | None = None
    current_percent: Decimal | None = None
    posture: str = DEV_HOLDING_UNKNOWN

    @property
    def change_percent(self) -> Decimal | None:
        if self.initial_percent is None or self.current_percent is None:
            return None
        return (self.current_percent - self.initial_percent).quantize(Decimal("0.01"))

    @property
    def selling(self) -> bool:
        return self.posture in {DEV_HOLDING_SELLING, DEV_HOLDING_EXITED}

    def to_json(self) -> dict[str, object]:
        return {
            "wallet": self.wallet,
            "initial_percent": _s(self.initial_percent),
            "current_percent": _s(self.current_percent),
            "change_percent": _s(self.change_percent),
            "posture": self.posture,
            "selling": self.selling,
        }


def assess_dev_holding(
    *,
    wallet: str,
    initial_percent: Decimal | None,
    current_percent: Decimal | None,
    reduced_threshold: Decimal = Decimal("-1"),
    selling_threshold: Decimal = Decimal("-25"),
    exited_percent: Decimal = Decimal("0.5"),
) -> DevHolding:
    """Grade the creator's posture from the change in their own position."""

    if initial_percent is None or current_percent is None:
        return DevHolding(
            wallet=wallet,
            initial_percent=initial_percent,
            current_percent=current_percent,
            posture=DEV_HOLDING_UNKNOWN,
        )
    if current_percent <= exited_percent and initial_percent > exited_percent:
        posture = DEV_HOLDING_EXITED
    else:
        relative = (
            (current_percent - initial_percent) / initial_percent * HUNDRED
            if initial_percent > ZERO
            else ZERO
        )
        if relative <= selling_threshold:
            posture = DEV_HOLDING_SELLING
        elif current_percent - initial_percent <= reduced_threshold:
            posture = DEV_HOLDING_REDUCED
        else:
            posture = DEV_HOLDING_STABLE
    return DevHolding(
        wallet=wallet,
        initial_percent=initial_percent,
        current_percent=current_percent,
        posture=posture,
    )


@dataclass(frozen=True, slots=True)
class PriorToken:
    """One publicly observable earlier token from the same creator."""

    mint: str
    created_at: int | None = None
    graduated: bool = False
    #: Liquidity effectively gone.
    collapsed: bool = False
    retained_liquidity: bool = False


@dataclass(frozen=True, slots=True)
class DevHistory:
    """A creator's observable record.  Neutral labels only (section 19)."""

    wallet: str = ""
    tokens_created: int = 0
    graduated: int = 0
    collapsed: int = 0
    retained_liquidity: int = 0
    label: str = DEV_HISTORY_UNKNOWN

    @property
    def failure_rate(self) -> Decimal | None:
        if self.tokens_created <= 0:
            return None
        return (Decimal(self.collapsed) / Decimal(self.tokens_created)).quantize(
            Decimal("0.01")
        )

    @property
    def graduation_rate(self) -> Decimal | None:
        if self.tokens_created <= 0:
            return None
        return (Decimal(self.graduated) / Decimal(self.tokens_created)).quantize(
            Decimal("0.01")
        )

    def operator_line(self) -> str:
        if self.label == DEV_HISTORY_UNKNOWN:
            return "dev history: not observable"
        return (
            f"dev history: {self.tokens_created} prior token(s), "
            f"{self.graduated} graduated, {self.collapsed} collapsed "
            f"(`{self.label}`)"
        )

    def to_json(self) -> dict[str, object]:
        return {
            "wallet": self.wallet,
            "tokens_created": self.tokens_created,
            "graduated": self.graduated,
            "collapsed": self.collapsed,
            "retained_liquidity": self.retained_liquidity,
            "failure_rate": _s(self.failure_rate),
            "graduation_rate": _s(self.graduation_rate),
            "label": self.label,
        }


def assess_dev_history(
    wallet: str,
    prior: Sequence[PriorToken],
    *,
    high_failure_rate: Decimal = Decimal("0.7"),
    min_sample: int = 3,
) -> DevHistory:
    """Summarise a creator's prior tokens without accusing anyone of anything."""

    if not prior:
        return DevHistory(wallet=wallet, label=DEV_HISTORY_UNKNOWN)

    graduated = sum(1 for item in prior if item.graduated)
    collapsed = sum(1 for item in prior if item.collapsed)
    retained = sum(1 for item in prior if item.retained_liquidity)
    history = DevHistory(
        wallet=wallet,
        tokens_created=len(prior),
        graduated=graduated,
        collapsed=collapsed,
        retained_liquidity=retained,
    )

    if len(prior) == 1:
        label = DEV_HISTORY_FIRST_TOKEN
    else:
        rate = history.failure_rate
        if len(prior) >= min_sample and rate is not None and rate >= high_failure_rate:
            label = DEV_HISTORY_HIGH_FAILURE
        elif graduated > 0:
            label = DEV_HISTORY_HAS_GRADUATES
        else:
            label = DEV_HISTORY_MIXED

    return DevHistory(
        wallet=wallet,
        tokens_created=len(prior),
        graduated=graduated,
        collapsed=collapsed,
        retained_liquidity=retained,
        label=label,
    )


@dataclass(frozen=True, slots=True)
class DevProfile:
    """Everything observable about the creator, in one object."""

    mint: str
    wallet: str = ""
    funding: DevFunding = DevFunding()
    holding: DevHolding = DevHolding()
    history: DevHistory = DevHistory()

    @property
    def concerns(self) -> tuple[str, ...]:
        """Things worth telling an operator.  None of them is a verdict."""

        notes: list[str] = []
        if self.holding.selling:
            notes.append(f"creator position `{self.holding.posture}`")
        if self.history.label == DEV_HISTORY_HIGH_FAILURE:
            rate = self.history.failure_rate
            notes.append(
                "creator's prior tokens mostly collapsed "
                f"({rate} failure rate over {self.history.tokens_created})"
            )
        if self.funding.funded_just_before_launch:
            notes.append(
                f"creator wallet funded {self.funding.seconds_before_launch}s before "
                "launch — context, not proof of anything"
            )
        if (
            self.holding.current_percent is not None
            and self.holding.current_percent >= Decimal("15")
        ):
            notes.append(f"creator holds {self.holding.current_percent}%")
        return tuple(notes)

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "wallet": self.wallet,
            "funding": self.funding.to_json(),
            "holding": self.holding.to_json(),
            "history": self.history.to_json(),
            "concerns": list(self.concerns),
        }
