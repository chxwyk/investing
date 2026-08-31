"""Explicit risk dimensions — never collapsed into one number that pretends to know.

Section 59's requirement is the interesting one: *do not collapse everything into
one fake certainty*.  A single "risk: 62/100" hides which thing is wrong, and the
operator's decision usually depends entirely on which thing is wrong.  Thin
liquidity, a distributing bundle and an unverified story are three different
problems with three different responses.

So each dimension is graded on its own and reported on its own, with three
possible verdicts:

``PASS``     evidence we actually have, and it is fine
``UNKNOWN``  we could not establish it — **this never becomes PASS** (section 61)
``FAIL``     evidence we actually have, and it is bad

A provider being down produces ``UNKNOWN``, not ``FAIL``: an outage is not a
finding about the token (section 61).  And hard failures — a confirmed sell
failure, a collapsed pool, hard malicious evidence, a lost route — outrank every
positive signal in the system (section 60).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal("0")

PASS = "PASS"
UNKNOWN = "UNKNOWN"
FAIL = "FAIL"

# --- the dimensions (section 59) ---------------------------------------------
RISK_LIQUIDITY = "LIQUIDITY"
RISK_DEV = "DEV"
RISK_CONCENTRATION = "CONCENTRATION"
RISK_BUNDLE = "BUNDLE"
RISK_RELATED_WALLETS = "RELATED_WALLETS"
RISK_FRESH_CLUSTER = "FRESH_WALLET_CLUSTER"
RISK_ROUTE = "ROUTE"
RISK_SELLABILITY = "SELLABILITY"
RISK_STORY_PROVENANCE = "STORY_PROVENANCE"
RISK_THESIS_PROVENANCE = "THESIS_PROVENANCE"

RISK_DIMENSIONS: tuple[str, ...] = (
    RISK_LIQUIDITY,
    RISK_DEV,
    RISK_CONCENTRATION,
    RISK_BUNDLE,
    RISK_RELATED_WALLETS,
    RISK_FRESH_CLUSTER,
    RISK_ROUTE,
    RISK_SELLABILITY,
    RISK_STORY_PROVENANCE,
    RISK_THESIS_PROVENANCE,
)

# --- hard failures (section 60) ----------------------------------------------
HARD_SELL_FAILED = "SELL_FAILED"
HARD_LIQUIDITY_COLLAPSE = "LIQUIDITY_COLLAPSE"
HARD_ROUTE_LOST = "SELL_ROUTE_UNAVAILABLE"
HARD_MALICIOUS = "MALICIOUS_CONTRACT_EVIDENCE"

HARD_FAILURES: tuple[str, ...] = (
    HARD_SELL_FAILED,
    HARD_LIQUIDITY_COLLAPSE,
    HARD_ROUTE_LOST,
    HARD_MALICIOUS,
)


@dataclass(frozen=True, slots=True)
class RiskDimension:
    """One graded dimension, with the reason it was graded that way."""

    name: str
    verdict: str = UNKNOWN
    detail: str = ""

    def __post_init__(self) -> None:
        if self.verdict not in {PASS, UNKNOWN, FAIL}:
            raise ValueError(f"invalid risk verdict: {self.verdict}")

    def to_json(self) -> dict[str, object]:
        return {"name": self.name, "verdict": self.verdict, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class RiskProfile:
    """Every dimension, separately.  No single blended score (section 59)."""

    mint: str
    dimensions: tuple[RiskDimension, ...] = ()
    hard_failures: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def blocked(self) -> bool:
        """A hard failure outranks everything else in the system (section 60)."""

        return bool(self.hard_failures)

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.dimensions if item.verdict == FAIL)

    @property
    def unknown(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.dimensions if item.verdict == UNKNOWN)

    @property
    def passed(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.dimensions if item.verdict == PASS)

    def dimension(self, name: str) -> RiskDimension | None:
        return next((item for item in self.dimensions if item.name == name), None)

    def operator_lines(self) -> tuple[str, ...]:
        lines = []
        if self.hard_failures:
            lines.append(f"⛔ HARD FAIL: {', '.join(self.hard_failures)}")
        for item in self.dimensions:
            if item.verdict == PASS and not item.detail:
                continue
            marker = {PASS: "✓", UNKNOWN: "?", FAIL: "✗"}[item.verdict]
            lines.append(
                f"{marker} {item.name}: {item.verdict}"
                + (f" — {item.detail}" if item.detail else "")
            )
        if self.unknown:
            lines.append(
                f"{len(self.unknown)} dimension(s) UNKNOWN — that is not a pass."
            )
        return tuple(lines)

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "dimensions": [item.to_json() for item in self.dimensions],
            "hard_failures": list(self.hard_failures),
            "failed": list(self.failed),
            "unknown": list(self.unknown),
            "passed": list(self.passed),
            "blocked": self.blocked,
            "notes": list(self.notes),
        }


def build_risk_profile(
    mint: str,
    *,
    liquidity_usd: Decimal | None = None,
    liquidity_to_market_cap: Decimal | None = None,
    dev_selling: bool | None = None,
    dev_history_label: str = "",
    dev_percent: Decimal | None = None,
    top10_percent: Decimal | None = None,
    concentration_worsening: bool | None = None,
    bundle_risk: str = "UNKNOWN",
    bundle_distributing: bool = False,
    related_percent: Decimal | None = None,
    clustered_percent: Decimal | None = None,
    route_available: bool | None = None,
    sell_verified: bool | None = None,
    story_verified: bool | None = None,
    thesis_supported: bool | None = None,
    sell_failed: bool = False,
    liquidity_collapsed: bool = False,
    malicious_evidence: bool = False,
    min_liquidity_usd: Decimal = Decimal("4000"),
    max_top10_percent: Decimal = Decimal("55"),
    max_dev_percent: Decimal = Decimal("15"),
    max_related_percent: Decimal = Decimal("25"),
    max_clustered_percent: Decimal = Decimal("50"),
) -> RiskProfile:
    """Grade every dimension independently, keeping UNKNOWN honest."""

    dimensions: list[RiskDimension] = []
    notes: list[str] = []

    def add(name: str, verdict: str, detail: str = "") -> None:
        dimensions.append(RiskDimension(name=name, verdict=verdict, detail=detail))

    # Liquidity: depth relative to the size the token is valued at.
    if liquidity_usd is None:
        add(RISK_LIQUIDITY, UNKNOWN, "no liquidity reading")
    elif liquidity_usd < min_liquidity_usd:
        add(RISK_LIQUIDITY, FAIL, f"${liquidity_usd:,.0f} is below the floor a $10 exit needs")
    elif liquidity_to_market_cap is not None and liquidity_to_market_cap < Decimal("0.02"):
        add(
            RISK_LIQUIDITY,
            FAIL,
            f"liquidity is only {liquidity_to_market_cap:.2%} of market cap",
        )
    else:
        add(RISK_LIQUIDITY, PASS, f"${liquidity_usd:,.0f}")

    # Dev.
    if dev_selling is None and not dev_history_label and dev_percent is None:
        add(RISK_DEV, UNKNOWN, "creator behaviour not observable")
    elif dev_selling:
        add(RISK_DEV, FAIL, "creator is reducing their position")
    elif dev_percent is not None and dev_percent > max_dev_percent:
        add(RISK_DEV, FAIL, f"creator holds {dev_percent}%")
    elif dev_history_label == "DEV_HISTORY_HIGH_FAILURE_RATE":
        add(RISK_DEV, FAIL, "creator's prior tokens mostly collapsed")
    else:
        add(RISK_DEV, PASS, dev_history_label or "no adverse creator evidence")

    # Concentration.
    if top10_percent is None:
        add(RISK_CONCENTRATION, UNKNOWN, "holder distribution not readable")
    elif top10_percent > max_top10_percent:
        add(RISK_CONCENTRATION, FAIL, f"top 10 hold {top10_percent}%")
    elif concentration_worsening:
        add(RISK_CONCENTRATION, FAIL, "ownership is concentrating, not broadening")
    else:
        add(RISK_CONCENTRATION, PASS, f"top 10 {top10_percent}%")

    # Bundles.
    if bundle_risk == "UNKNOWN":
        add(RISK_BUNDLE, UNKNOWN, "bundle exposure not determinable")
    elif bundle_distributing:
        add(RISK_BUNDLE, FAIL, "bundle wallets are distributing")
    elif bundle_risk == "HIGH":
        add(RISK_BUNDLE, FAIL, "launch bundles took a large share of supply")
    elif bundle_risk == "MODERATE":
        add(RISK_BUNDLE, UNKNOWN, "moderate launch bundling — watch for distribution")
    else:
        add(RISK_BUNDLE, PASS, bundle_risk)

    # Related wallets.
    if related_percent is None:
        add(RISK_RELATED_WALLETS, UNKNOWN, "wallet graph not resolvable")
    elif related_percent > max_related_percent:
        add(RISK_RELATED_WALLETS, FAIL, f"related wallets hold {related_percent}%")
    else:
        add(RISK_RELATED_WALLETS, PASS, f"{related_percent}%")

    # Fresh-wallet clustering.
    if clustered_percent is None:
        add(RISK_FRESH_CLUSTER, UNKNOWN, "buyer independence not resolvable")
    elif clustered_percent > max_clustered_percent:
        add(
            RISK_FRESH_CLUSTER,
            FAIL,
            f"{clustered_percent}% of demand came from coordinated wallets",
        )
    else:
        add(RISK_FRESH_CLUSTER, PASS, f"{clustered_percent}% clustered")

    # Route and sellability.
    if route_available is None:
        add(RISK_ROUTE, UNKNOWN, "no route check performed")
    elif route_available:
        add(RISK_ROUTE, PASS)
    else:
        add(RISK_ROUTE, FAIL, "no sell route available")

    if sell_verified is None:
        add(RISK_SELLABILITY, UNKNOWN, "sellability not verified")
    elif sell_verified:
        add(RISK_SELLABILITY, PASS)
    else:
        add(RISK_SELLABILITY, FAIL, "a sell could not be simulated")

    # Provenance of the soft evidence.
    if story_verified is None:
        add(RISK_STORY_PROVENANCE, UNKNOWN, "no story attached")
    elif story_verified:
        add(RISK_STORY_PROVENANCE, PASS, "story corroborated for this exact mint")
    else:
        add(RISK_STORY_PROVENANCE, FAIL, "story is unverified for this mint")

    if thesis_supported is None:
        add(RISK_THESIS_PROVENANCE, UNKNOWN, "no thesis attached")
    elif thesis_supported:
        add(RISK_THESIS_PROVENANCE, PASS, "thesis externally supported")
    else:
        add(RISK_THESIS_PROVENANCE, FAIL, "thesis is unsupported")

    hard: list[str] = []
    if sell_failed:
        hard.append(HARD_SELL_FAILED)
    if liquidity_collapsed:
        hard.append(HARD_LIQUIDITY_COLLAPSE)
    if route_available is False:
        hard.append(HARD_ROUTE_LOST)
    if malicious_evidence:
        hard.append(HARD_MALICIOUS)

    unknown_count = sum(1 for item in dimensions if item.verdict == UNKNOWN)
    if unknown_count >= 5:
        notes.append(
            f"{unknown_count} of {len(dimensions)} risk dimensions are UNKNOWN — "
            "this is a thin picture, not a clean one"
        )

    return RiskProfile(
        mint=mint,
        dimensions=tuple(dimensions),
        hard_failures=tuple(hard),
        notes=tuple(notes),
    )
