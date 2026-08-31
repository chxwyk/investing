"""The Trending risk panel, and the rule that Trending never overrides safety.

Trending is attention.  Attention is not rug protection, a Fomo verification
badge is not rug protection, and neither of them can outvote a confirmed sell
failure or a collapsing pool (sections 37, 71).  This module keeps that ordering
explicit and mechanical rather than leaving it to the judgement of whichever
surface happens to be rendering a card.

Safety is reported as PASS / UNKNOWN / FAIL and ``UNKNOWN`` never quietly
becomes ``PASS``: a provider that is degraded, unconfigured or out of credit
produces "we do not know", which is a different thing from "it is fine".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .holders import CONCENTRATION_WORSENING, HolderProfile

ZERO = Decimal("0")

SAFETY_PASS = "PASS"
SAFETY_UNKNOWN = "UNKNOWN"
SAFETY_FAIL = "FAIL"

#: Hard evidence that beats every attention signal there is.
HARD_SELL_FAILURE = "SELL_FAILED"
HARD_LIQUIDITY_COLLAPSE = "LIQUIDITY_COLLAPSE"
HARD_ROUTE_LOST = "SELL_ROUTE_UNAVAILABLE"
HARD_MALICIOUS = "MALICIOUS_EVIDENCE"

HARD_FAILURES: tuple[str, ...] = (
    HARD_SELL_FAILURE,
    HARD_LIQUIDITY_COLLAPSE,
    HARD_ROUTE_LOST,
    HARD_MALICIOUS,
)


@dataclass(frozen=True, slots=True)
class TrendingRiskPanel:
    """Everything a serious Trending candidate has to disclose (section 38)."""

    mint: str
    liquidity_usd: Decimal | None = None
    sell_route_status: str = SAFETY_UNKNOWN
    sellable: bool | None = None
    top10_percent: Decimal | None = None
    creator_percent: Decimal | None = None
    largest_cluster_percent: Decimal | None = None
    smart_money_distributing: bool = False
    story_authenticity: str = "UNKNOWN"
    exact_mint_confirmed: bool = True
    fomo_verified: str = "UNKNOWN"
    safety_status: str = SAFETY_UNKNOWN
    hard_failures: tuple[str, ...] = ()
    concerns: tuple[str, ...] = field(default_factory=tuple)

    @property
    def blocked(self) -> bool:
        """A hard failure blocks promotion regardless of how hot the token is."""

        return bool(self.hard_failures) or self.safety_status == SAFETY_FAIL

    def operator_lines(self) -> tuple[str, ...]:
        lines = [
            "Liquidity: "
            + ("unknown" if self.liquidity_usd is None else f"${self.liquidity_usd:,.0f}"),
            f"Sell route: {self.sell_route_status}",
            f"Top 10: {'unknown' if self.top10_percent is None else f'{self.top10_percent:.1f}%'}",
            f"Safety: {self.safety_status}",
        ]
        if self.fomo_verified != "UNKNOWN":
            lines.append(
                f"Fomo verified: {self.fomo_verified} "
                "(a badge, not rug protection)"
            )
        lines.extend(f"⚠ {concern}" for concern in self.concerns)
        return tuple(lines)

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "liquidity_usd": _s(self.liquidity_usd),
            "sell_route_status": self.sell_route_status,
            "sellable": self.sellable,
            "top10_percent": _s(self.top10_percent),
            "creator_percent": _s(self.creator_percent),
            "largest_cluster_percent": _s(self.largest_cluster_percent),
            "smart_money_distributing": self.smart_money_distributing,
            "story_authenticity": self.story_authenticity,
            "exact_mint_confirmed": self.exact_mint_confirmed,
            "fomo_verified": self.fomo_verified,
            "safety_status": self.safety_status,
            "hard_failures": list(self.hard_failures),
            "concerns": list(self.concerns),
            "blocked": self.blocked,
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def build_risk_panel(
    mint: str,
    *,
    liquidity_usd: Decimal | None = None,
    sell_route_status: str = SAFETY_UNKNOWN,
    sellable: bool | None = None,
    holders: HolderProfile | None = None,
    creator_percent: Decimal | None = None,
    largest_cluster_percent: Decimal | None = None,
    smart_money_distributing: bool = False,
    story_authenticity: str = "UNKNOWN",
    exact_mint_confirmed: bool = True,
    fomo_verified: str = "UNKNOWN",
    safety_status: str = SAFETY_UNKNOWN,
    sell_failed: bool = False,
    liquidity_collapsed: bool = False,
    malicious_evidence: bool = False,
    min_liquidity_usd: Decimal = Decimal("8000"),
    max_top10_percent: Decimal = Decimal("55"),
    max_creator_percent: Decimal = Decimal("15"),
) -> TrendingRiskPanel:
    """Assemble the panel, separating hard failures from soft concerns."""

    hard: list[str] = []
    if sell_failed:
        hard.append(HARD_SELL_FAILURE)
    if liquidity_collapsed:
        hard.append(HARD_LIQUIDITY_COLLAPSE)
    if sell_route_status == SAFETY_FAIL or sellable is False:
        hard.append(HARD_ROUTE_LOST)
    if malicious_evidence:
        hard.append(HARD_MALICIOUS)

    concerns: list[str] = []
    top10 = holders.top10_percent if holders else None
    if liquidity_usd is not None and liquidity_usd < min_liquidity_usd:
        concerns.append(f"liquidity ${liquidity_usd:,.0f} is thin for this size")
    if top10 is not None and top10 > max_top10_percent:
        concerns.append(f"top 10 hold {top10:.1f}%")
    if holders is not None and holders.concentration_trend == CONCENTRATION_WORSENING:
        concerns.append("concentration is getting worse, not better")
    if creator_percent is not None and creator_percent > max_creator_percent:
        concerns.append(f"creator holds {creator_percent:.1f}%")
    if smart_money_distributing:
        concerns.append("proven wallets are distributing, not accumulating")
    if not exact_mint_confirmed:
        concerns.append("this mint's identity could not be confirmed against the source")
    if safety_status == SAFETY_UNKNOWN:
        concerns.append("safety evidence is UNKNOWN — that is not a pass")
    if fomo_verified == "VERIFIED":
        concerns.append(
            "verified on Fomo — that is a badge, not a safety guarantee and not a rug shield"
        )

    return TrendingRiskPanel(
        mint=mint,
        liquidity_usd=liquidity_usd,
        sell_route_status=sell_route_status,
        sellable=sellable,
        top10_percent=top10,
        creator_percent=creator_percent,
        largest_cluster_percent=largest_cluster_percent,
        smart_money_distributing=smart_money_distributing,
        story_authenticity=story_authenticity,
        exact_mint_confirmed=exact_mint_confirmed,
        fomo_verified=fomo_verified,
        safety_status=safety_status,
        hard_failures=tuple(hard),
        concerns=tuple(concerns),
    )
