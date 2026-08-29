"""Evidence-completeness caps and buyer-population consistency.

Two display defects motivated this module, and both were symptoms of the same
thing: a number was being shown without the evidence that would justify it.

* **Confidence.** ``edge_confidence`` was derived from the organic-demand score
  alone, so a candidate with a 100/100 organic score rendered ``100%``
  confidence even when economic authenticity was ``UNKNOWN``, the bounded SOL
  activity sample was missing, and safety was not ``PASS``.  Confidence is a
  statement about *how much we know*, so it is now capped by the weakest piece
  of evidence behind it.

* **Buyer populations.** Five distinct populations were collapsed into one
  number, producing cards that read "Raw unique buyers 0 • tracked wallets 14 •
  independent among tracked 12".  They are measured by different mechanisms:
  every on-chain buyer (never sampled here, so *unavailable* — not zero), the
  bounded holder trace, the independent share of that trace, the buys this bot
  verified itself (where ``0`` is a real observation), and the funding clusters
  found.  Each is now typed separately, ``None`` means unavailable, ``0`` means
  observed zero, and impossible combinations raise rather than render.

Nothing here changes a score, a threshold or an entry decision.  It constrains
what may be *claimed* from the evidence that exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .decision import EvidenceQuality

ZERO = Decimal("0")
HUNDRED = Decimal("100")

# --- confidence ceilings by evidence completeness ----------------------------
#: Confidence can never exceed the weakest ceiling that applies.  A full 100
#: requires complete evidence, a PASS safety verdict, a sampled activity
#: profile and a high-confidence demand trace — all four.
CAP_EVIDENCE_PARTIAL = Decimal("60")
CAP_EVIDENCE_UNKNOWN = Decimal("30")
CAP_AUTHENTICITY_PARTIAL = Decimal("70")
CAP_AUTHENTICITY_UNKNOWN = Decimal("50")
CAP_ACTIVITY_UNAVAILABLE = Decimal("50")
CAP_SAFETY_UNKNOWN = Decimal("40")
CAP_SAFETY_FAIL = Decimal("20")
CAP_DEMAND_UNKNOWN = Decimal("35")
CAP_DEMAND_LOW = Decimal("50")
CAP_DEMAND_MEDIUM = Decimal("70")
CAP_DATA_DEGRADED = Decimal("45")

#: Labels for organic-demand evidence that has not been corroborated.
ORGANIC_CONFIRMED = "CONFIRMED"
ORGANIC_RAW = "RAW"
ORGANIC_UNVERIFIED = "UNVERIFIED"

_ORGANIC_SUFFIX = {
    ORGANIC_CONFIRMED: "",
    ORGANIC_RAW: " (RAW — unverified authenticity)",
    ORGANIC_UNVERIFIED: " (UNVERIFIED — partial authenticity evidence)",
}


@dataclass(frozen=True, slots=True)
class ConfidenceCap:
    """The ceiling that evidence completeness places on confidence."""

    ceiling: Decimal = HUNDRED
    reasons: tuple[str, ...] = ()

    @property
    def limited(self) -> bool:
        return self.ceiling < HUNDRED

    def apply(self, raw: Decimal | None) -> Decimal | None:
        if raw is None:
            return None
        return min(raw, self.ceiling).quantize(Decimal("0.01"))


def confidence_cap(
    *,
    evidence_quality: EvidenceQuality | str | None = None,
    authenticity_quality: EvidenceQuality | str | None = None,
    activity_available: bool | None = None,
    safety_status: str | None = None,
    demand_confidence: str | None = None,
    data_degraded: bool = False,
) -> ConfidenceCap:
    """Return the strictest ceiling justified by the evidence that exists.

    Every argument is optional so a caller can cap on whatever it actually
    knows; an argument left out simply contributes no ceiling.
    """

    ceiling = HUNDRED
    reasons: list[str] = []

    def limit(value: Decimal, reason: str) -> None:
        nonlocal ceiling
        if value < ceiling:
            ceiling = value
        reasons.append(reason)

    quality = _quality_name(evidence_quality)
    if quality == "UNKNOWN":
        limit(CAP_EVIDENCE_UNKNOWN, "evidence quality is UNKNOWN")
    elif quality == "PARTIAL":
        limit(CAP_EVIDENCE_PARTIAL, "evidence quality is PARTIAL")

    authenticity = _quality_name(authenticity_quality)
    if authenticity == "UNKNOWN":
        limit(CAP_AUTHENTICITY_UNKNOWN, "economic authenticity is UNKNOWN")
    elif authenticity == "PARTIAL":
        limit(CAP_AUTHENTICITY_PARTIAL, "economic authenticity is PARTIAL")

    if activity_available is False:
        limit(CAP_ACTIVITY_UNAVAILABLE, "no bounded SOL activity sample")

    if safety_status is not None:
        status = str(safety_status).upper()
        if status == "FAIL":
            limit(CAP_SAFETY_FAIL, "safety is FAIL")
        elif status != "PASS":
            limit(CAP_SAFETY_UNKNOWN, "safety is not PASS")

    if demand_confidence is not None:
        demand = str(demand_confidence).upper()
        if demand == "UNKNOWN":
            limit(CAP_DEMAND_UNKNOWN, "buyer-independence trace did not run")
        elif demand == "LOW":
            limit(CAP_DEMAND_LOW, "buyer-independence trace is LOW confidence")
        elif demand == "MEDIUM":
            limit(CAP_DEMAND_MEDIUM, "buyer-independence trace is MEDIUM confidence")

    if data_degraded:
        limit(CAP_DATA_DEGRADED, "providers disagree or evidence is stale")

    return ConfidenceCap(ceiling=ceiling, reasons=tuple(dict.fromkeys(reasons)))


def cap_confidence(raw: Decimal | None, cap: ConfidenceCap) -> Decimal | None:
    return cap.apply(raw)


@dataclass(frozen=True, slots=True)
class BuyerEvidence:
    """The distinct buyer populations, each with explicit availability.

    Five different things were previously collapsed into one number, which is
    how a card came to read "Raw unique buyers 0 • tracked wallets 14 •
    independent among tracked 12".  They are measured by different mechanisms
    over different populations:

    * ``raw_unique_buyers`` — every buyer on chain.  This system does not
      sample that population at all, so it is ``None``/unavailable, not zero.
    * ``tracked_wallets`` — the bounded holder/funder trace actually performed.
    * ``independent_tracked_wallets`` — of those traced, how many look unlinked.
    * ``verified_buyers`` — buys this bot itself confirmed from tracked-wallet
      swaps.  Here a zero is a real, informative observation.
    * ``wallet_clusters`` — funding clusters found within the trace.

    ``None`` means unavailable.  ``0`` means observed zero.  The two are never
    rendered the same way.
    """

    raw_unique_buyers: int | None = None
    tracked_wallets: int = 0
    independent_tracked_wallets: int | None = None
    verified_buyers: int | None = None
    wallet_clusters: int | None = None
    largest_cluster_wallets: int | None = None
    independence_ratio: Decimal | None = None
    confidence: str = "UNKNOWN"

    def __post_init__(self) -> None:
        # Structural guarantee: independence can never exceed the population it
        # was measured over.  An impossible pair is a bug, not a rendering.
        if (
            self.independent_tracked_wallets is not None
            and self.tracked_wallets
            and self.independent_tracked_wallets > self.tracked_wallets
        ):
            raise ValueError(
                "independent tracked wallets cannot exceed the traced population"
            )
        # Two measurements of the *same* population must stay coherent.
        if (
            self.raw_unique_buyers is not None
            and self.verified_buyers is not None
            and self.verified_buyers > self.raw_unique_buyers
        ):
            raise ValueError("verified buyers cannot exceed the sampled raw buyers")

    @property
    def traced(self) -> bool:
        return self.tracked_wallets > 0 and self.independent_tracked_wallets is not None

    @property
    def raw_buyers_text(self) -> str:
        """Never sampled means unavailable, which is not the same as zero."""

        return "unavailable" if self.raw_unique_buyers is None else str(self.raw_unique_buyers)

    @property
    def verified_buyers_text(self) -> str:
        """A verified zero is a real observation and is shown as ``0``."""

        return "unavailable" if self.verified_buyers is None else str(self.verified_buyers)

    @property
    def tracked_text(self) -> str:
        return "not traced" if self.tracked_wallets <= 0 else str(self.tracked_wallets)

    @property
    def independence_text(self) -> str:
        """Always "N of M traced" — never a bare count beside an unrelated one."""

        if not self.traced:
            return "not traced"
        return f"{self.independent_tracked_wallets} of {self.tracked_wallets} traced"

    @property
    def summary(self) -> str:
        return (
            f"raw buyers {self.raw_buyers_text} • tracked {self.tracked_text} • "
            f"independent {self.independence_text} • verified {self.verified_buyers_text}"
        )


def buyer_evidence(
    demand: Any = None,
    forensics: Any = None,
) -> BuyerEvidence:
    """Build the one authoritative buyer view from the runner's own fields.

    ``demand`` is preferred because it is the persisted decision-time profile;
    ``forensics`` fills in only what the profile does not carry.
    """

    traced = _int(getattr(demand, "traced_wallets", None))
    if traced is None:
        traced = _int(getattr(forensics, "traced_wallets", None)) or 0

    independent = _int(getattr(demand, "estimated_independent_buyers", None))
    if independent is None:
        independent = _int(getattr(forensics, "estimated_independent_clusters", None))

    # The runner's ``raw_unique_buyers`` counts buys this bot verified from
    # tracked-wallet swaps.  It is the *verified* population, not a sample of
    # every buyer on chain, which nothing currently measures.
    verified = _int(getattr(demand, "raw_buyers", None))
    if verified is None:
        verified = _int(getattr(forensics, "raw_unique_buyers", None))

    raw_sampled = _int(getattr(demand, "sampled_raw_buyers", None))
    if raw_sampled is None:
        raw_sampled = _int(getattr(forensics, "sampled_raw_buyers", None))

    if independent is not None and traced and independent > traced:
        # Defensive clamp: a caller mixing populations can never render an
        # impossible pair through this type.
        independent = traced

    clusters = _int(getattr(forensics, "shared_funder_group_count", None))
    if clusters is None:
        groups = getattr(forensics, "shared_funder_groups", None)
        clusters = len(groups) if groups is not None else None

    ratio = getattr(demand, "independence_ratio", None)
    return BuyerEvidence(
        raw_unique_buyers=raw_sampled,
        tracked_wallets=traced,
        independent_tracked_wallets=independent,
        verified_buyers=verified,
        wallet_clusters=clusters,
        largest_cluster_wallets=_int(getattr(demand, "largest_cluster_wallets", None))
        or _int(getattr(forensics, "largest_cluster_size", None)),
        independence_ratio=ratio if isinstance(ratio, Decimal) else None,
        confidence=str(getattr(demand, "confidence", None) or "UNKNOWN"),
    )


def organic_demand_state(
    *,
    authenticity_quality: EvidenceQuality | str | None,
    demand_confidence: str | None = None,
) -> str:
    """Whether an organic-demand score is corroborated or still raw.

    A 100/100 organic score with no authenticity evidence is a measurement of
    *visible* demand, not proof that the demand is real.  Labelling it stops a
    reader from confusing the two.
    """

    quality = _quality_name(authenticity_quality)
    if quality == "UNKNOWN":
        return ORGANIC_RAW
    if quality == "PARTIAL":
        return ORGANIC_UNVERIFIED
    if demand_confidence is not None and str(demand_confidence).upper() in {
        "UNKNOWN",
        "LOW",
    }:
        return ORGANIC_UNVERIFIED
    return ORGANIC_CONFIRMED


def organic_demand_text(
    score: Decimal | int | None,
    *,
    authenticity_quality: EvidenceQuality | str | None,
    demand_confidence: str | None = None,
) -> str:
    """Render an organic score with an honest qualifier attached."""

    state = organic_demand_state(
        authenticity_quality=authenticity_quality,
        demand_confidence=demand_confidence,
    )
    rendered = "unknown" if score is None else f"{Decimal(score):.0f}"
    return f"{rendered}{_ORGANIC_SUFFIX.get(state, '')}"


def _quality_name(value: EvidenceQuality | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, EvidenceQuality):
        return str(value)
    return str(value).upper()


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
