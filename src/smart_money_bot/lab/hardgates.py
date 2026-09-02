"""Named gates with evidence, and the rule that no score may overrule one.

Every release before this one asked *how good does this look?* and answered with
a number.  A number cannot distinguish the four situations the operator actually
cares about, because it collapses them into one axis:

    real token, moving, safe            -> the trade
    real token, moving, safety unknown  -> the trap
    real token, safe, no edge left      -> too late
    fake token, moving, "safe"          -> the thing that keeps getting through

A weighted score puts all four in the same league table and lets a big enough
momentum term buy its way past a missing safety answer.  That is not a tuning
problem, it is a category error, and it is why "stop showing me fake coins" has
been answered four times without being fixed.

So the model here is deliberately not a score:

* Each question is a **named gate** answered ``PASS``, ``FAIL`` or ``UNKNOWN``.
* Every answer carries its evidence, its source, when it was taken, and how old
  it is now.  An answer with no provenance cannot be audited and is treated as
  ``UNKNOWN``.
* **``UNKNOWN`` is never ``PASS``.**  A timeout, an outage, an exception and a
  missing field all land here, and every one of them fails closed.
* **No score can overrule a failed or unknown required gate.**  Conviction
  changes the ranking among candidates that already cleared the gates; it never
  opens one.

Evidence goes stale.  A route that existed five minutes ago is not proof that it
exists now, so every gate carries a maximum age and ages out into ``UNKNOWN``
rather than remaining a stale ``PASS``.

Pure logic: no provider, no database, no signer, no order path.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace

# --- answers -----------------------------------------------------------------
PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"

ANSWERS: tuple[str, ...] = (PASS, FAIL, UNKNOWN)

# --- the gates ---------------------------------------------------------------
#: This is the exact token it claims to be, keyed by chain and address.
CANONICAL_TOKEN = "CANONICAL_TOKEN"
#: Its origin is proven from chain state and a verified launchpad or market.
VERIFIED_ORIGIN = "VERIFIED_ORIGIN"
#: The pool is an allowlisted implementation holding the two expected mints.
VERIFIED_POOL = "VERIFIED_POOL"
#: Liquidity computed from on-chain reserves, not read off a provider card.
VERIFIED_LIQUIDITY = "VERIFIED_LIQUIDITY"
#: A fresh executable buy quote exists.
BUY_ROUTE_OK = "BUY_ROUTE_OK"
#: A fresh executable *sell* quote exists for the same mint and pool.
SELL_ROUTE_OK = "SELL_ROUTE_OK"
#: Independent wallets have actually completed sells on chain.
SELL_EVIDENCE_OK = "SELL_EVIDENCE_OK"
#: The two-sided activity is real rather than recycled between related wallets.
ORGANIC_FLOW_OK = "ORGANIC_FLOW_OK"
#: Holdings are not concentrated enough for one actor to end the market.
CONCENTRATION_OK = "CONCENTRATION_OK"
#: Mint/freeze authority, transfer hooks, fees and taxes are known and benign.
CONTRACT_SAFETY_OK = "CONTRACT_SAFETY_OK"
#: Something is happening right now.
MOMENTUM_OK = "MOMENTUM_OK"
#: And it has not already happened to somebody else.
NOT_LATE_OR_EXHAUSTED = "NOT_LATE_OR_EXHAUSTED"
#: The universal pre-send authorization ran and allowed this card.
FINAL_ALERT_GUARD_OK = "FINAL_ALERT_GUARD_OK"

GATES: tuple[str, ...] = (
    CANONICAL_TOKEN,
    VERIFIED_ORIGIN,
    VERIFIED_POOL,
    VERIFIED_LIQUIDITY,
    BUY_ROUTE_OK,
    SELL_ROUTE_OK,
    SELL_EVIDENCE_OK,
    ORGANIC_FLOW_OK,
    CONCENTRATION_OK,
    CONTRACT_SAFETY_OK,
    MOMENTUM_OK,
    NOT_LATE_OR_EXHAUSTED,
    FINAL_ALERT_GUARD_OK,
)

#: Which gates decide whether a token is *what it says it is* and *tradeable*.
#: Momentum is deliberately absent: a token can be entirely real and entirely
#: safe while there is nothing to do about it, and that is a different answer
#: from "this might be fake".
SAFETY_GATES: frozenset[str] = frozenset(
    {
        CANONICAL_TOKEN,
        VERIFIED_ORIGIN,
        VERIFIED_POOL,
        VERIFIED_LIQUIDITY,
        BUY_ROUTE_OK,
        SELL_ROUTE_OK,
        SELL_EVIDENCE_OK,
        ORGANIC_FLOW_OK,
        CONCENTRATION_OK,
        CONTRACT_SAFETY_OK,
    }
)

#: And which decide whether there is an edge worth acting on now.
EDGE_GATES: frozenset[str] = frozenset({MOMENTUM_OK, NOT_LATE_OR_EXHAUSTED})

#: Everything required before the strongest card this bot produces.
REQUIRED_FOR_ENTRY: frozenset[str] = SAFETY_GATES | EDGE_GATES | {FINAL_ALERT_GUARD_OK}

# --- classifications ---------------------------------------------------------
#: Every hard gate passed.  Research only, and it still says so on the card.
ENTRY_CANDIDATE = "ENTRY_CANDIDATE_RESEARCH"
#: Moving, but safety failed or could not be established.  The trap.
UNSAFE_MOMENTUM = "UNSAFE_MOMENTUM"
#: Real and identifiable, but the sell or liquidity proof is not there yet.
WATCH_ONLY = "VERIFIED_WATCH_ONLY"
#: Real, safe, tradeable — and the move is over or has not started.
NO_ENTRY_NOW = "SAFE_NO_ENTRY_NOW"

CLASSIFICATIONS: tuple[str, ...] = (
    ENTRY_CANDIDATE,
    UNSAFE_MOMENTUM,
    WATCH_ONLY,
    NO_ENTRY_NOW,
)

#: The only classification permitted to interrupt a human.
PINGABLE: frozenset[str] = frozenset({ENTRY_CANDIDATE})

HUMAN_CLASSIFICATION: dict[str, str] = {
    ENTRY_CANDIDATE: "every hard gate passed — research candidate, not advice",
    UNSAFE_MOMENTUM: "it is moving and we cannot show that it is safe",
    WATCH_ONLY: "real token, but the sell or liquidity proof is not there yet",
    NO_ENTRY_NOW: "safe and real, with no edge left to take",
}


@dataclass(frozen=True, slots=True)
class GateResult:
    """One question, one answer, and the receipt for it.

    ``source`` and ``observed_at`` are not decoration.  A ``PASS`` nobody can
    trace is indistinguishable from a bug, and the whole point of this model is
    that every refusal and every approval can be shown to the operator with the
    number that produced it.
    """

    gate: str
    answer: str = UNKNOWN
    reason: str = ""
    #: Where the answer came from, in the source hierarchy: decoded chain state
    #: outranks a launchpad API, which outranks a quote, which outranks a
    #: market-data provider, which outranks a terminal card.
    source: str = ""
    #: When the underlying observation was taken, epoch seconds.
    observed_at: int | None = None
    #: How old that observation may be before it stops counting.  ``None``
    #: means it does not age — an immutable fact like a creation slot.
    max_age_seconds: int | None = None
    evidence: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.answer not in ANSWERS:
            raise ValueError(f"unknown gate answer: {self.answer}")

    def age_seconds(self, now: int) -> int | None:
        if self.observed_at is None:
            return None
        return max(0, now - self.observed_at)

    def stale(self, now: int) -> bool:
        """Whether this answer has aged out of usefulness."""

        if self.max_age_seconds is None:
            return False
        age = self.age_seconds(now)
        if age is None:
            # An answer that cannot say when it was taken cannot be shown to be
            # fresh, and freshness is required, so it is treated as expired.
            return True
        return age > self.max_age_seconds

    def at(self, now: int) -> GateResult:
        """This result as of ``now``.  A stale ``PASS`` decays to ``UNKNOWN``.

        A ``FAIL`` does not decay.  Evidence that something was broken does not
        stop counting because time passed; only a claim that it *worked* has to
        keep being re-earned.
        """

        if self.answer == PASS and self.stale(now):
            return replace(
                self,
                answer=UNKNOWN,
                reason=(
                    f"evidence expired ({self.age_seconds(now)}s old, "
                    f"max {self.max_age_seconds}s) — {self.reason}"
                    if self.observed_at is not None
                    else f"evidence carries no timestamp — {self.reason}"
                ),
            )
        return self

    @property
    def ok(self) -> bool:
        return self.answer == PASS

    @property
    def failed(self) -> bool:
        return self.answer == FAIL

    def to_json(self) -> dict[str, object]:
        return {
            "gate": self.gate,
            "answer": self.answer,
            "reason": self.reason,
            "source": self.source,
            "observed_at": self.observed_at,
            "max_age_seconds": self.max_age_seconds,
            "evidence": [list(item) for item in self.evidence],
        }


def unknown(gate: str, reason: str = "not evaluated") -> GateResult:
    """The default for every gate.  Everything starts here and must earn PASS."""

    return GateResult(gate=gate, answer=UNKNOWN, reason=reason)


@dataclass(frozen=True, slots=True)
class GateReport:
    """Every gate, the classification that follows, and why.

    The classification is *derived*, never assigned.  Nothing may construct this
    with an answer that its own gates do not support, which is what stops a
    caller deciding it likes a candidate and labelling it accordingly.
    """

    mint: str
    now: int = 0
    results: tuple[GateResult, ...] = field(default_factory=tuple)

    @property
    def by_gate(self) -> dict[str, GateResult]:
        known = {item.gate: item.at(self.now) for item in self.results}
        return {gate: known.get(gate, unknown(gate)) for gate in GATES}

    def answer(self, gate: str) -> str:
        return self.by_gate[gate].answer

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(g for g in GATES if self.by_gate[g].answer == FAIL)

    @property
    def unresolved(self) -> tuple[str, ...]:
        return tuple(g for g in GATES if self.by_gate[g].answer == UNKNOWN)

    def blocking(self, required: frozenset[str] = REQUIRED_FOR_ENTRY) -> tuple[str, ...]:
        """Required gates that are not PASS.  Empty means an entry is allowed."""

        answers = self.by_gate
        return tuple(g for g in GATES if g in required and answers[g].answer != PASS)

    @property
    def safety_settled(self) -> bool:
        """Every safety gate answered PASS.  ``UNKNOWN`` does not count.

        The single definition of "safe" in this module.  It had two for about
        an hour, and a mutation test caught the second one going unused — which
        is exactly how "UNKNOWN counts as PASS" gets reintroduced by someone
        editing the copy that nobody reads.
        """

        answers = self.by_gate
        return all(answers[g].answer == PASS for g in GATES if g in SAFETY_GATES)

    @property
    def safety_compromised(self) -> bool:
        return not self.safety_settled

    @property
    def edge_present(self) -> bool:
        answers = self.by_gate
        return all(answers[g].answer == PASS for g in GATES if g in EDGE_GATES)

    def classify(self) -> str:
        """The four answers, in the order that keeps them from blurring.

        Safety is asked before edge, always.  A moving token whose safety we
        could not establish is the single most expensive card this bot can
        send, so it gets its own name rather than being folded into a lower
        score on the same scale as a healthy one.
        """

        if self.safety_compromised:
            # Is it moving as well?  That distinction is the whole reason
            # UNSAFE_MOMENTUM exists as a separate thing from WATCH_ONLY.
            return UNSAFE_MOMENTUM if self.edge_present else WATCH_ONLY
        if not self.edge_present:
            return NO_ENTRY_NOW
        if self.blocking():
            # Safety and edge both clear, so what is left is the final guard.
            return WATCH_ONLY
        return ENTRY_CANDIDATE

    @property
    def may_ping(self) -> bool:
        return self.classify() in PINGABLE

    def human(self) -> str:
        return HUMAN_CLASSIFICATION.get(self.classify(), self.classify())

    def reasons(self) -> tuple[str, ...]:
        """Why this is not an entry, in the operator's words rather than codes."""

        answers = self.by_gate
        lines: list[str] = []
        for gate in self.blocking():
            result = answers[gate]
            label = gate.replace("_OK", "").replace("_", " ").lower()
            lines.append(f"{label}: {result.answer} — {result.reason or 'no evidence'}")
        return tuple(lines)

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "classification": self.classify(),
            "human": self.human(),
            "may_ping": self.may_ping,
            "blocking": list(self.blocking()),
            "failed": list(self.failed),
            "unresolved": list(self.unresolved),
            "reasons": list(self.reasons()),
            "gates": [self.by_gate[g].to_json() for g in GATES],
        }


def build_report(mint: str, results: Sequence[GateResult], *, now: int) -> GateReport:
    """Assemble a report.  Later answers for a gate replace earlier ones."""

    latest: dict[str, GateResult] = {}
    for item in results:
        latest[item.gate] = item
    return GateReport(mint=mint, now=now, results=tuple(latest.values()))
