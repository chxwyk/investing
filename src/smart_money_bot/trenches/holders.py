"""Holder concentration and its trend, from public token accounts.

A single snapshot barely means anything.  "Top 10 hold 43%" reads as alarming
until you learn it was 68% ten minutes ago and ownership is broadening; and
"top 10 hold 18%" reads as healthy until you learn it was 9% and someone is
accumulating hard.  So section 21 is the real requirement: persist the *trend*.

Everything here is computed from `getTokenLargestAccounts` and `getTokenSupply`,
both public RPC methods.  Canonical infrastructure accounts — the bonding curve,
the AMM pool, burn addresses — are excluded from concentration where they can be
identified, because counting the liquidity pool as a "holder" makes every token
look either dangerously concentrated or artificially safe depending on stage.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")
HUNDRED = Decimal("100")

CONCENTRATION_IMPROVING = "IMPROVING"
CONCENTRATION_STABLE = "STABLE"
CONCENTRATION_WORSENING = "WORSENING"
CONCENTRATION_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class HolderAccount:
    """One token account and the share of supply it holds."""

    address: str
    amount: Decimal
    owner: str = ""
    #: True for the bonding curve, an AMM pool, a burn address — anything that is
    #: infrastructure rather than a participant.
    infrastructure: bool = False


@dataclass(frozen=True, slots=True)
class HolderSnapshot:
    """Concentration at one moment, with infrastructure excluded."""

    mint: str
    at: int = 0
    total_supply: Decimal | None = None
    holder_count: int | None = None
    top10_percent: Decimal | None = None
    top20_percent: Decimal | None = None
    largest_holder_percent: Decimal | None = None
    #: Supply held by identified infrastructure, reported separately.
    infrastructure_percent: Decimal | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "at": self.at,
            "total_supply": _s(self.total_supply),
            "holder_count": self.holder_count,
            "top10_percent": _s(self.top10_percent),
            "top20_percent": _s(self.top20_percent),
            "largest_holder_percent": _s(self.largest_holder_percent),
            "infrastructure_percent": _s(self.infrastructure_percent),
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def build_holder_snapshot(
    mint: str,
    accounts: Sequence[HolderAccount],
    *,
    total_supply: Decimal | None,
    at: int,
    holder_count: int | None = None,
) -> HolderSnapshot:
    """Concentration among *participants*, not among infrastructure (section 20).

    The denominator is circulating supply — total minus identified
    infrastructure — because measuring a participant's share against a total that
    is mostly still sitting in the bonding curve understates every position.
    """

    if not accounts or total_supply is None or total_supply <= ZERO:
        return HolderSnapshot(
            mint=mint, at=at, total_supply=total_supply, holder_count=holder_count
        )

    infrastructure = sum(
        (item.amount for item in accounts if item.infrastructure), ZERO
    )
    participants = sorted(
        (item for item in accounts if not item.infrastructure),
        key=lambda item: item.amount,
        reverse=True,
    )
    circulating = total_supply - infrastructure
    if circulating <= ZERO:
        # Everything is still in the curve; there is no participant float to
        # measure concentration against yet.
        return HolderSnapshot(
            mint=mint,
            at=at,
            total_supply=total_supply,
            holder_count=holder_count,
            infrastructure_percent=(infrastructure / total_supply * HUNDRED).quantize(
                Decimal("0.01")
            ),
        )

    def share(count: int) -> Decimal | None:
        if not participants:
            return None
        held = sum((item.amount for item in participants[:count]), ZERO)
        return (held / circulating * HUNDRED).quantize(Decimal("0.01"))

    return HolderSnapshot(
        mint=mint,
        at=at,
        total_supply=total_supply,
        holder_count=holder_count,
        top10_percent=share(10),
        top20_percent=share(20),
        largest_holder_percent=share(1),
        infrastructure_percent=(infrastructure / total_supply * HUNDRED).quantize(
            Decimal("0.01")
        ),
    )


@dataclass(frozen=True, slots=True)
class ConcentrationTrend:
    """How ownership is moving — the thing a snapshot cannot tell you (§21)."""

    mint: str
    state: str = CONCENTRATION_UNKNOWN
    first_top10_percent: Decimal | None = None
    current_top10_percent: Decimal | None = None
    change_points: Decimal | None = None
    samples: int = 0
    history: tuple[tuple[int, Decimal], ...] = ()

    @property
    def worsening(self) -> bool:
        return self.state == CONCENTRATION_WORSENING

    def operator_line(self) -> str:
        if not self.history:
            return "concentration: unknown"
        path = " → ".join(f"{value:.0f}%" for _, value in self.history[-4:])
        return f"top 10: {path} (`{self.state}`)"

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "state": self.state,
            "first_top10_percent": _s(self.first_top10_percent),
            "current_top10_percent": _s(self.current_top10_percent),
            "change_points": _s(self.change_points),
            "samples": self.samples,
            "history": [[at, str(value)] for at, value in self.history],
        }


def assess_concentration_trend(
    mint: str,
    snapshots: Sequence[HolderSnapshot],
    *,
    tolerance_points: Decimal = Decimal("3"),
) -> ConcentrationTrend:
    """Read the direction of ownership across the recorded snapshots."""

    usable = [
        (item.at, item.top10_percent)
        for item in sorted(snapshots, key=lambda item: item.at)
        if item.top10_percent is not None
    ]
    if not usable:
        return ConcentrationTrend(mint=mint)
    if len(usable) == 1:
        return ConcentrationTrend(
            mint=mint,
            state=CONCENTRATION_UNKNOWN,
            first_top10_percent=usable[0][1],
            current_top10_percent=usable[0][1],
            samples=1,
            history=tuple(usable),
        )

    first, last = usable[0][1], usable[-1][1]
    change = (last - first).quantize(Decimal("0.01"))
    if change <= -tolerance_points:
        state = CONCENTRATION_IMPROVING
    elif change >= tolerance_points:
        state = CONCENTRATION_WORSENING
    else:
        state = CONCENTRATION_STABLE

    return ConcentrationTrend(
        mint=mint,
        state=state,
        first_top10_percent=first,
        current_top10_percent=last,
        change_points=change,
        samples=len(usable),
        history=tuple(usable),
    )


# --- related-wallet exposure (section 22) ------------------------------------
@dataclass(frozen=True, slots=True)
class RelatedExposure:
    """Holdings by wallets an observable graph relationship links together.

    "Related" here is a statement about transaction structure — a shared funder,
    a creator relationship, a same-slot group.  It is never a claim about who
    anybody is, and this module cannot and does not deanonymise anyone.
    """

    mint: str
    related_wallets: int = 0
    related_percent: Decimal | None = None
    evidence: tuple[str, ...] = ()

    @property
    def significant(self) -> bool:
        return self.related_percent is not None and self.related_percent >= Decimal("10")

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "related_wallets": self.related_wallets,
            "related_percent": _s(self.related_percent),
            "evidence": list(self.evidence),
            "significant": self.significant,
        }


def assess_related_exposure(
    mint: str,
    *,
    related_wallets: Sequence[str],
    holdings: dict[str, Decimal],
    circulating_supply: Decimal | None,
    evidence: Sequence[str] = (),
) -> RelatedExposure:
    """Total the holdings of wallets that objective evidence groups together."""

    if not related_wallets or circulating_supply is None or circulating_supply <= ZERO:
        return RelatedExposure(
            mint=mint, related_wallets=len(related_wallets), evidence=tuple(evidence)
        )
    held = sum((holdings.get(wallet, ZERO) for wallet in set(related_wallets)), ZERO)
    return RelatedExposure(
        mint=mint,
        related_wallets=len(set(related_wallets)),
        related_percent=(held / circulating_supply * HUNDRED).quantize(Decimal("0.01")),
        evidence=tuple(evidence),
    )
