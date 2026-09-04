"""The headline is the verdict.  Not the momentum, not the score.

The failure this exists for, in the operator's own screenshots:

    🔥 WATCH — HEATING UP
    Safety: UNKNOWN • Route: UNKNOWN
    Independent notable wallets: 0
    Expected NET edge: +27.52%
    RESEARCH ONLY — NOT ENTRY ELIGIBLE

Every line after the first contradicts the first, and the first is the one a
person reads.  A footer does not undo a headline.  Worse, that card computed an
*expected edge* — a modelled gain — for a token whose route it could not even
confirm existed, and then presented it next to a real transaction cost.  That is
not a display bug; it is the system reporting an opportunity it has no basis to
believe in.

The title in that card was a hardcoded string.  It could not have said anything
else, because nothing about the evidence ever reached it.

So this module inverts the relationship.  The verdict is computed from the
evidence first, and the headline is *derived* from the verdict — there is no
path by which a builder can choose a title.  The default is
``DO_NOT_ENTER_INSUFFICIENT``: optimism has to be earned by evidence, and
absence of evidence produces refusal rather than enthusiasm.

Two rules run through all of it.

**UNKNOWN is not PASS.**  A gate nobody answered, a provider that timed out, an
exception, a stale quote — all of them refuse.  The strongest thing this system
may ever say is that everything it checked currently passes, which is a
statement about evidence and not a prediction.

**Cost is not netted against a guess.**  An expected edge is a model; a
round-trip cost is arithmetic on real quotes.  Subtracting the second from the
first and printing the difference as an opportunity gives a modelled number the
authority of a measured one.  When the evidence for the model is missing, the
edge is not computed at all.

Pure logic: no provider, no database, no signer, no order path.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .hardgates import (
    BUY_ROUTE_OK,
    CANONICAL_TOKEN,
    CONTRACT_SAFETY_OK,
    FAIL,
    NOT_LATE_OR_EXHAUSTED,
    ORGANIC_FLOW_OK,
    PASS,
    SELL_ROUTE_OK,
    GateReport,
)

# --- verdicts, worst first ----------------------------------------------------
DO_NOT_ENTER_IDENTITY = "DO_NOT_ENTER_IDENTITY_UNRESOLVED"
DO_NOT_ENTER_VAMP = "DO_NOT_ENTER_VAMP_CLONE_RISK"
DO_NOT_ENTER_SAFETY = "DO_NOT_ENTER_SAFETY_UNKNOWN"
DO_NOT_ENTER_MARKET_CONFLICT = "DO_NOT_ENTER_MARKET_DATA_CONFLICT"
DO_NOT_ENTER_HOLDER_CONFLICT = "DO_NOT_ENTER_HOLDER_DATA_CONFLICT"
DO_NOT_ENTER_WASH = "DO_NOT_ENTER_WASH_CLUSTERED_FLOW"
DO_NOT_ENTER_SELL_ROUTE = "DO_NOT_ENTER_SELL_ROUTE_UNPROVEN"
DO_NOT_ENTER_COST = "DO_NOT_ENTER_EXCESSIVE_COST_IMPACT"
DO_NOT_ENTER_DISTRIBUTION = "DO_NOT_ENTER_DISTRIBUTION_LIQUIDITY_DECAY"
DO_NOT_ENTER_LATE = "DO_NOT_ENTER_LATE_EXTENDED"
DO_NOT_ENTER_UNRESOLVED = "DO_NOT_ENTER_SAFETY_ROUTE_INDEPENDENCE_UNRESOLVED"
DO_NOT_ENTER_INSUFFICIENT = "DO_NOT_ENTER_INSUFFICIENT_PROOF"
DATA_INTEGRITY_HOLD = "DATA_INTEGRITY_HOLD"
WATCH_VERIFIED = "WATCH_VERIFIED_NOT_YET_ELIGIBLE"
PAPER_ENTRY = "PAPER_ENTRY_CANDIDATE"
INVALIDATED = "INVALIDATED"

VERDICTS: tuple[str, ...] = (
    DO_NOT_ENTER_IDENTITY, DO_NOT_ENTER_VAMP, DO_NOT_ENTER_SAFETY,
    DO_NOT_ENTER_MARKET_CONFLICT, DO_NOT_ENTER_HOLDER_CONFLICT, DO_NOT_ENTER_WASH,
    DO_NOT_ENTER_SELL_ROUTE, DO_NOT_ENTER_COST, DO_NOT_ENTER_DISTRIBUTION,
    DO_NOT_ENTER_LATE, DO_NOT_ENTER_UNRESOLVED, DO_NOT_ENTER_INSUFFICIENT,
    DATA_INTEGRITY_HOLD,
    WATCH_VERIFIED, PAPER_ENTRY, INVALIDATED,
)

#: The only verdict permitted to interrupt anybody, and even it says research.
PINGABLE: frozenset[str] = frozenset({PAPER_ENTRY})

#: Titles are derived, never chosen.  Each states the verdict in words before
#: any emoji, because colour and emoji are not readable as the primary signal.
TITLE: dict[str, str] = {
    DO_NOT_ENTER_IDENTITY: "⛔ DO NOT ENTER — IDENTITY UNRESOLVED",
    DO_NOT_ENTER_VAMP: "⛔ DO NOT ENTER — VAMP/CLONE RISK",
    DO_NOT_ENTER_SAFETY: "⛔ DO NOT ENTER — SAFETY UNKNOWN",
    DO_NOT_ENTER_MARKET_CONFLICT: "⚠ DATA INTEGRITY HOLD — MARKET DATA CONFLICT",
    DO_NOT_ENTER_HOLDER_CONFLICT: "⚠ DATA INTEGRITY HOLD — HOLDER DATA CONFLICT",
    DO_NOT_ENTER_WASH: "⚠ UNSAFE MOMENTUM — WASH/CLUSTER RISK",
    DO_NOT_ENTER_SELL_ROUTE: "⛔ DO NOT ENTER — SELL ROUTE UNPROVEN",
    DO_NOT_ENTER_COST: "⛔ DO NOT ENTER — EXCESSIVE COST/IMPACT",
    DO_NOT_ENTER_DISTRIBUTION: "📉 DO NOT ENTER — DISTRIBUTION/LIQUIDITY DECAY",
    DO_NOT_ENTER_LATE: "⛔ DO NOT ENTER — LATE/EXTENDED",
    DO_NOT_ENTER_UNRESOLVED: "⛔ DO NOT ENTER — SAFETY/ROUTE/INDEPENDENCE UNRESOLVED",
    DO_NOT_ENTER_INSUFFICIENT: "⛔ DO NOT ENTER — INSUFFICIENT PROOF",
    DATA_INTEGRITY_HOLD: "⚠ DATA INTEGRITY HOLD — DO NOT ENTER",
    WATCH_VERIFIED: "🔎 VERIFIED WATCH — NOT ENTRY ELIGIBLE",
    PAPER_ENTRY: "🧪 PAPER ENTRY CANDIDATE — ALL GATES PASS",
    INVALIDATED: "📉 INVALIDATED — RISK OFF",
}

#: Language a card may never use while anything material is unresolved.  Checked
#: rather than trusted: the offending title in production was a literal nobody
#: had connected to the evidence at all.
FORBIDDEN_PHRASES: tuple[str, ...] = (
    "LOOK NOW", "HEATING UP", "EARLY RUNNER", "BUY NOW", "APE", "SEND IT",
    "HIGH CONVICTION", "GUARANTEED",
)


#: Matched on word boundaries, with an optional leading separator so that
#: removing a phrase also removes the dash it hung from.  Boundaries are not
#: pedantry here: ``APE`` is a substring of ``PAPER ENTRY CANDIDATE``, so a
#: plain substring test forbids the one verdict this system may ever reach.
_FORBIDDEN_RE = re.compile(
    r"(?:\s*[—–\-•]\s*)?\b(?:"
    + "|".join(re.escape(phrase) for phrase in FORBIDDEN_PHRASES)
    + r")\b",
    re.IGNORECASE,
)


def contains_forbidden(text: str) -> tuple[str, ...]:
    """Which promotional phrases appear in a piece of card text."""

    found = {
        match.group(0).strip(" —–-•").upper()
        for match in _FORBIDDEN_RE.finditer(text or "")
    }
    return tuple(phrase for phrase in FORBIDDEN_PHRASES if phrase in found)


@dataclass(frozen=True, slots=True)
class CostConfig:
    """What a round trip may cost before it stops being an opportunity."""

    max_price_impact_pct: Decimal = Decimal("2.0")
    max_round_trip_cost_pct: Decimal = Decimal("5.0")
    min_liquidity_usd: Decimal = Decimal("20000")
    quote_max_age_seconds: int = 20
    liquidity_stability_min_seconds: int = 120


DEFAULT_COST_CONFIG = CostConfig()


@dataclass(frozen=True, slots=True)
class RiskOffConfig:
    """When a move stops being a move and becomes an exit."""

    max_drawdown_1m_pct: Decimal = Decimal("15")
    max_drawdown_2m_pct: Decimal = Decimal("25")
    max_liquidity_drop_2m_pct: Decimal = Decimal("10")
    max_clustered_sell_share_pct: Decimal = Decimal("35")
    max_creator_cluster_sell_share_pct: Decimal = Decimal("10")


DEFAULT_RISK_OFF_CONFIG = RiskOffConfig()


@dataclass(frozen=True, slots=True)
class MarketEvidence:
    """Everything a verdict is allowed to consider, with its provenance.

    Tri-state throughout.  ``None`` means nobody established it, which is a
    different thing from a measured zero and never reads as permission.
    """

    chain_id: str = ""
    token_address: str = ""
    # --- identity and family -------------------------------------------
    identity_verified: bool | None = None
    canonicality: str = ""
    # --- integrity ------------------------------------------------------
    chart_reconstructed: bool | None = None
    market_data_conflict: bool = False
    holder_data_conflict: bool = False
    organic_flow_ok: bool | None = None
    # --- route and cost --------------------------------------------------
    safety_ok: bool | None = None
    buy_route_ok: bool | None = None
    sell_route_ok: bool | None = None
    round_trip_cost_pct: Decimal | None = None
    price_impact_pct: Decimal | None = None
    liquidity_usd: Decimal | None = None
    quote_age_seconds: int | None = None
    # --- independence ----------------------------------------------------
    independent_notable_wallets: int | None = None
    # --- timing ----------------------------------------------------------
    drawdown_1m_pct: Decimal | None = None
    drawdown_2m_pct: Decimal | None = None
    liquidity_drop_2m_pct: Decimal | None = None
    clustered_sell_share_pct: Decimal | None = None
    creator_cluster_sell_share_pct: Decimal | None = None
    late_or_extended: bool | None = None
    observed_at: int | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    """The verdict, why, and what would change it."""

    verdict: str = DO_NOT_ENTER_INSUFFICIENT
    reasons: tuple[str, ...] = field(default_factory=tuple)
    what_must_change: tuple[str, ...] = field(default_factory=tuple)
    unresolved: tuple[str, ...] = field(default_factory=tuple)

    @property
    def entry_eligible(self) -> bool:
        return self.verdict == PAPER_ENTRY

    @property
    def may_ping(self) -> bool:
        return self.verdict in PINGABLE

    def title(self) -> str:
        return TITLE.get(self.verdict, TITLE[DO_NOT_ENTER_INSUFFICIENT])

    def edge_is_computable(self) -> bool:
        """Whether an expected edge may be shown at all.

        Only when the verdict says every checked gate passes.  Anywhere else,
        a modelled gain beside a real cost is a number that reads as an
        opportunity and is not one.
        """

        return self.verdict == PAPER_ENTRY

    def to_json(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "title": self.title(),
            "entry_eligible": self.entry_eligible,
            "may_ping": self.may_ping,
            "edge_computable": self.edge_is_computable(),
            "reasons": list(self.reasons),
            "what_must_change": list(self.what_must_change),
            "unresolved": list(self.unresolved),
            "research_only": True,
        }


def decide(
    evidence: MarketEvidence,
    *,
    gates: GateReport | None = None,
    cost: CostConfig = DEFAULT_COST_CONFIG,
    risk_off: RiskOffConfig = DEFAULT_RISK_OFF_CONFIG,
) -> Decision:
    """Evaluate in the order in which things stop being worth continuing.

    Identity before safety before market integrity before route before cost
    before timing, because each answers a question that makes the ones after it
    meaningless if it fails.  A momentum score appears nowhere in this function
    — there is nothing here for one to influence.
    """

    unresolved: list[str] = []
    change: list[str] = []

    def unknown(label: str, fix: str) -> None:
        unresolved.append(label)
        change.append(fix)

    # 1. identity ---------------------------------------------------------
    if evidence.identity_verified is False:
        return _verdict(DO_NOT_ENTER_IDENTITY, ["the exact mint could not be verified"], change)
    if evidence.identity_verified is None:
        unknown("identity", "verify the exact chain and mint")

    # 2. clone / vamp family ----------------------------------------------
    if evidence.canonicality in {"VAMP_OR_CLONE_LIKELY", "IMPERSONATION_CONFLICT"}:
        return _verdict(
            DO_NOT_ENTER_VAMP,
            [f"token family says {evidence.canonicality.replace('_', ' ').lower()}"],
            ["prove the official contract, or treat this as a copy"],
        )

    # 3. integrity conflicts outrank everything downstream -----------------
    if evidence.holder_data_conflict:
        return _verdict(
            DO_NOT_ENTER_HOLDER_CONFLICT,
            ["a provider's holder count contradicts the on-chain ledger"],
            ["reconcile the holder count against Transfer logs"],
        )
    if evidence.market_data_conflict:
        return _verdict(
            DO_NOT_ENTER_MARKET_CONFLICT,
            ["provider market data disagrees with the reconstructed chart"],
            ["reconcile the chart against canonical pool swaps"],
        )

    # 4. safety ------------------------------------------------------------
    if evidence.safety_ok is False:
        return _verdict(DO_NOT_ENTER_SAFETY, ["contract safety failed"], change)
    if evidence.safety_ok is None:
        unknown("safety", "establish contract safety")

    # 5. the exit -----------------------------------------------------------
    if evidence.sell_route_ok is False:
        return _verdict(
            DO_NOT_ENTER_SELL_ROUTE, ["no working sell route"],
            ["obtain a fresh sell quote and simulate the exit"],
        )
    if evidence.sell_route_ok is None:
        unknown("sell route", "obtain a fresh reverse quote")
    if evidence.buy_route_ok is None:
        unknown("buy route", "obtain a fresh buy quote")
    if (
        evidence.quote_age_seconds is not None
        and evidence.quote_age_seconds > cost.quote_max_age_seconds
    ):
        unknown("quote freshness", f"re-quote within {cost.quote_max_age_seconds}s")

    # 6. risk-off outranks any remaining optimism ---------------------------
    breached = _risk_off_breaches(evidence, risk_off)
    if breached:
        return _verdict(DO_NOT_ENTER_DISTRIBUTION, breached,
                        ["wait for a confirmed recovery window with stable liquidity"])

    # 7. anything material we never established.  This sits ahead of cost
    #    deliberately: safety is step 4 of the specification's order and cost
    #    is step 10, and a token whose safety is unknown should be refused for
    #    that rather than for the price of a trade nobody should be making.
    if evidence.independent_notable_wallets == 0:
        unknown(
            "independent participation",
            "at least one independently confirmed notable wallet",
        )
    if (
        evidence.liquidity_usd is not None
        and evidence.liquidity_usd < cost.min_liquidity_usd
    ):
        unknown("liquidity", f"at least ${cost.min_liquidity_usd} of verified liquidity")
    if evidence.chart_reconstructed is None:
        unknown("chart", "reconstruct the chart from canonical swaps")
    if evidence.organic_flow_ok is None:
        unknown("organic flow", "decode canonical pool swaps")

    if unresolved:
        # Which kind of unknown dominates decides which hold this is.  An
        # unreconstructed chart or undecoded flow is an *integrity* problem —
        # we cannot trust the numbers at all — while unknown safety and route
        # are questions about a market we can at least see.
        integrity = {"chart", "organic flow"} & set(unresolved)
        verdict = DATA_INTEGRITY_HOLD if integrity else DO_NOT_ENTER_UNRESOLVED
        detail = [f"{item} is unresolved" for item in unresolved[:4]]
        if (
            evidence.round_trip_cost_pct is not None
            and evidence.round_trip_cost_pct > cost.max_round_trip_cost_pct
        ):
            # Report it, because it is disqualifying on its own — but it is not
            # the headline while something more fundamental is missing.
            detail.append(
                f"and a round trip would cost {evidence.round_trip_cost_pct}%, "
                f"above the {cost.max_round_trip_cost_pct}% limit"
            )
        return Decision(
            verdict=verdict,
            reasons=tuple(detail),
            what_must_change=tuple(dict.fromkeys(change))[:4],
            unresolved=tuple(unresolved),
        )

    # 8. cost, which is arithmetic rather than a model -----------------------
    if (
        evidence.round_trip_cost_pct is not None
        and evidence.round_trip_cost_pct > cost.max_round_trip_cost_pct
    ):
        return _verdict(
            DO_NOT_ENTER_COST,
            [
                f"a round trip costs {evidence.round_trip_cost_pct}%, above the "
                f"{cost.max_round_trip_cost_pct}% limit"
            ],
            ["deeper liquidity or a cheaper route"],
        )
    if (
        evidence.price_impact_pct is not None
        and evidence.price_impact_pct > cost.max_price_impact_pct
    ):
        return _verdict(
            DO_NOT_ENTER_COST,
            [f"price impact {evidence.price_impact_pct}% at the paper size"],
            ["deeper liquidity"],
        )

    # 9. organic flow --------------------------------------------------------
    if evidence.organic_flow_ok is False:
        return _verdict(
            DO_NOT_ENTER_WASH, ["the flow is clustered or recycled rather than organic"],
            ["independent actors trading at meaningful size"],
        )

    # 10. the chart, and being late --------------------------------------------
    if evidence.chart_reconstructed is False:
        return _verdict(
            DO_NOT_ENTER_MARKET_CONFLICT, ["the chart could not be reconstructed on chain"],
            ["decode swaps from the canonical pool"],
        )
    if evidence.late_or_extended:
        return _verdict(DO_NOT_ENTER_LATE, ["the move is already extended"],
                        ["a fresh, verified second expansion"])

    if gates is not None and gates.blocking():
        return Decision(
            verdict=WATCH_VERIFIED,
            reasons=gates.reasons()[:4],
            what_must_change=("clear the remaining hard gates",),
            unresolved=tuple(gates.unresolved),
        )

    return Decision(
        verdict=PAPER_ENTRY,
        reasons=("every gate checked currently passes",),
        what_must_change=(),
    )


def _verdict(verdict: str, reasons: Sequence[str], change: Sequence[str]) -> Decision:
    return Decision(
        verdict=verdict,
        reasons=tuple(reasons),
        what_must_change=tuple(dict.fromkeys(change))[:4],
    )


def _risk_off_breaches(
    evidence: MarketEvidence, config: RiskOffConfig
) -> tuple[str, ...]:
    """Which risk-off thresholds this observation has crossed."""

    found: list[str] = []
    checks = (
        (evidence.drawdown_1m_pct, config.max_drawdown_1m_pct, "down {v}% in a minute"),
        (evidence.drawdown_2m_pct, config.max_drawdown_2m_pct, "down {v}% in two minutes"),
        (
            evidence.liquidity_drop_2m_pct,
            config.max_liquidity_drop_2m_pct,
            "liquidity down {v}% in two minutes",
        ),
        (
            evidence.clustered_sell_share_pct,
            config.max_clustered_sell_share_pct,
            "{v}% of selling is one cluster",
        ),
        (
            evidence.creator_cluster_sell_share_pct,
            config.max_creator_cluster_sell_share_pct,
            "the creator's cluster is {v}% of selling",
        ),
    )
    for value, limit, template in checks:
        if value is not None and value >= limit:
            found.append(template.format(v=value))
    return tuple(found)


# --- headlines ----------------------------------------------------------------
#: What the economics section says when the evidence for a model is missing.
#: Spelling out *why* matters: a blank field reads as zero, and zero edge and
#: unknowable edge are not the same statement.
EDGE_NOT_COMPUTABLE = "NOT COMPUTABLE — REQUIRED EVIDENCE MISSING"


#: Every derived title, for recognising one that a builder has passed back.
_TITLE_TEXTS: frozenset[str] = frozenset(TITLE.values())


def sanitize(text: str) -> str:
    """Remove promotional language, keeping whatever information was around it.

    Deliberately not a rejection.  A builder's title usually carries something
    real — which lane found this, what kind of event it was — and throwing that
    away to punish the adjective would cost the operator information they use.
    Only the instruction goes.
    """

    cleaned = _FORBIDDEN_RE.sub("", text or "")
    return " ".join(cleaned.split()).strip(" —–-•")


def family_of(title: str) -> str:
    """The informative remainder of a builder's title: which lane, what event.

    ``🔥 WATCH — HEATING UP`` keeps ``WATCH``; ``🚨 EARLY RUNNER — LOOK NOW``
    keeps nothing at all, because both halves of it were the instruction.
    """

    cleaned = sanitize(title)
    # Drop leading emoji/punctuation so the result is words.
    words = [part for part in cleaned.split() if any(ch.isalnum() for ch in part)]
    return " ".join(words).strip(" —-•")


def headline(decision: Decision, *, context: str = "") -> str:
    """The card title, derived from the verdict and never chosen by a builder."""

    base = decision.title()
    tail = family_of(context)
    return f"{base} ({tail})" if tail else base


def enforce_title(title: str, decision: Decision | None) -> str:
    """The title a card is *allowed* to carry, given what was established.

    The backstop at the publish choke point.  ``None`` means no decision was
    ever computed for this mint, which is the strongest reason of all to refuse
    the builder's own wording — nothing looked at the evidence, so the headline
    cannot be a claim about it.

    Note what this does *not* condition on: whether the card pings.  The card
    the operator complained about had ``ping=False`` already, and the previous
    sanitizer only ran on pinging cards, so ``WATCH — HEATING UP`` reached the
    channel with its instruction intact.  A headline is read whether or not it
    made a phone buzz.
    """

    if decision is not None and decision.entry_eligible:
        return title
    # A title that is *already* a verdict is a claim, not a lane label.  Keeping
    # it as the parenthetical would produce "DO NOT ENTER — INSUFFICIENT PROOF
    # (VERIFIED WATCH NOT ENTRY ELIGIBLE)", which decorates a stale claim with a
    # current one and reads as neither.  A stale claim is replaced outright.
    context = "" if title.strip() in _TITLE_TEXTS else title
    return headline(decision or Decision(), context=context)


def evidence_from_gates(report: GateReport, **overrides: object) -> MarketEvidence:
    """Translate a hard-gate report into the evidence a verdict reasons over.

    ``UNKNOWN`` becomes ``None`` rather than ``False``: the two are different
    claims, and only one of them is something we measured.

    One consequence worth stating rather than discovering.  There is no hard
    gate for ``chart_reconstructed`` — reconstructing OHLCV from canonical pool
    swaps is not implemented — so a report in which *every* gate passes still
    produces ``DATA INTEGRITY HOLD`` rather than ``PAPER ENTRY CANDIDATE``.
    That is the correct answer under this module's own rule: nothing has
    verified that the chart on the card describes the mint on the card, and
    unknown is not pass. It does mean the strongest verdict is unreachable
    from gates alone until that reconstruction exists; a caller that *has*
    reconstructed a chart says so through ``overrides``.

    The alternative — treating a passing organic-flow gate as though it were a
    reconstructed chart — would make the green verdict reachable by deciding
    that a different question had been answered. That is the substitution this
    whole module exists to prevent.
    """

    def tri(gate: str) -> bool | None:
        answer = report.answer(gate)
        if answer == PASS:
            return True
        return False if answer == FAIL else None

    not_late = tri(NOT_LATE_OR_EXHAUSTED)
    fields: dict[str, object] = {
        "token_address": report.mint,
        "identity_verified": tri(CANONICAL_TOKEN),
        "safety_ok": tri(CONTRACT_SAFETY_OK),
        "buy_route_ok": tri(BUY_ROUTE_OK),
        "sell_route_ok": tri(SELL_ROUTE_OK),
        "organic_flow_ok": tri(ORGANIC_FLOW_OK),
        "late_or_extended": None if not_late is None else not not_late,
    }
    fields.update(overrides)
    return MarketEvidence(**fields)  # type: ignore[arg-type]


def cost_config_from_settings(settings: object) -> CostConfig:
    """Read the cost ceilings from production configuration.

    Defined here rather than in ``config`` so this module keeps its one useful
    property: it imports nothing that can reach a network, a database or a
    wallet, and can therefore be reasoned about — and tested — on its own.
    """

    return CostConfig(
        max_price_impact_pct=_decimal(settings, "entry_max_price_impact_pct", "2.0"),
        max_round_trip_cost_pct=_decimal(settings, "entry_max_round_trip_cost_pct", "5.0"),
        min_liquidity_usd=_decimal(settings, "entry_min_liquidity_usd", "20000"),
        quote_max_age_seconds=_int(settings, "entry_quote_max_age_seconds", 20),
        liquidity_stability_min_seconds=_int(
            settings, "entry_liquidity_stability_min_seconds", 120
        ),
    )


def risk_off_config_from_settings(settings: object) -> RiskOffConfig:
    """Read the risk-off thresholds from production configuration."""

    return RiskOffConfig(
        max_drawdown_1m_pct=_decimal(settings, "risk_off_max_drawdown_1m_pct", "15"),
        max_drawdown_2m_pct=_decimal(settings, "risk_off_max_drawdown_2m_pct", "25"),
        max_liquidity_drop_2m_pct=_decimal(
            settings, "risk_off_max_liquidity_drop_2m_pct", "10"
        ),
        max_clustered_sell_share_pct=_decimal(
            settings, "risk_off_max_clustered_sell_share_pct", "35"
        ),
        max_creator_cluster_sell_share_pct=_decimal(
            settings, "risk_off_max_creator_cluster_sell_share_pct", "10"
        ),
    )


def _decimal(settings: object, name: str, fallback: str) -> Decimal:
    value = getattr(settings, name, None)
    return Decimal(str(value)) if value is not None else Decimal(fallback)


def _int(settings: object, name: str, fallback: int) -> int:
    value = getattr(settings, name, None)
    return int(value) if value is not None else fallback
