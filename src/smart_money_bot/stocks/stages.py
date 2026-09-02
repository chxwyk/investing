"""Not every verified launch is a buy, and saying so is the whole design.

A stock-anchored launch is interesting the moment it is verified — that is
genuinely early, and the operator asked to be told early.  But a token that
exists and has never traded has no price, no liquidity and no buyers, and
calling that an entry would be the same failure as every fake-coin card this
project has spent four releases fixing, wearing a new badge.

So the lane has stages, and each one says exactly what it is:

    A  VERIFIED STOCK LAUNCH   real anchor, no market yet. Unpriced by design.
    B  TRACTION WATCH          a market appeared; here is why it is not an entry
    C  STONKS ENTRY CANDIDATE  every hard gate passed, and it leads its anchor
    D  UNSAFE MOMENTUM         moving, and we cannot show it is safe

Stage A is the one worth defending.  It is allowed to be unpriced, and it is
labelled ``TOO EARLY FOR ENTRY`` in its own title rather than in small print,
because a card that looks like an opportunity and is captioned as a notice will
be read as an opportunity.  An unresolved anchor never reaches this stage at
all: it stays in diagnostics until it resolves or expires, because "we think
this might be about NVIDIA" is not something to interrupt anyone with.

Stage C requires *everything*: verified anchor, verified launchpad, fresh
canonical-pool data, all thirteen hard gates, a moving stock behind it, and
leadership of its anchor.  Any one of those missing drops it to B, and an
unsafe one drops it to D no matter how well it is moving.

Pure logic: no provider, no database, no signer, no order path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..lab.hardgates import (
    ENTRY_CANDIDATE as GATE_ENTRY,
)
from ..lab.hardgates import (
    UNSAFE_MOMENTUM as GATE_UNSAFE,
)
from ..lab.hardgates import (
    GateReport,
)
from .signal import STOCK_RUNNER, AnchorVerdict
from .verification import AnchorProof

# --- stages -------------------------------------------------------------------
STAGE_VERIFIED_LAUNCH = "VERIFIED_STOCK_LAUNCH"
STAGE_TRACTION_WATCH = "TRACTION_WATCH"
STAGE_ENTRY_CANDIDATE = "STONKS_ENTRY_CANDIDATE"
STAGE_UNSAFE_MOMENTUM = "UNSAFE_MOMENTUM"
#: Not a stage the operator ever sees.  An unresolved anchor stays here.
STAGE_DIAGNOSTIC = "UNRESOLVED_DIAGNOSTIC"

STAGES: tuple[str, ...] = (
    STAGE_DIAGNOSTIC,
    STAGE_VERIFIED_LAUNCH,
    STAGE_TRACTION_WATCH,
    STAGE_ENTRY_CANDIDATE,
    STAGE_UNSAFE_MOMENTUM,
)

#: Stages that may be published to the operator's channel at all.
PUBLISHABLE: frozenset[str] = frozenset(
    {STAGE_VERIFIED_LAUNCH, STAGE_TRACTION_WATCH, STAGE_ENTRY_CANDIDATE, STAGE_UNSAFE_MOMENTUM}
)
#: And the only one that may interrupt.
PINGABLE: frozenset[str] = frozenset({STAGE_ENTRY_CANDIDATE})

TITLE: dict[str, str] = {
    STAGE_VERIFIED_LAUNCH: "🟡 VERIFIED STOCK LAUNCH — RESEARCH\nUNPRICED / TOO EARLY FOR ENTRY",
    STAGE_TRACTION_WATCH: "🔵 TRACTION WATCH — RESEARCH",
    STAGE_ENTRY_CANDIDATE: "🟢 STONKS ENTRY CANDIDATE — RESEARCH",
    STAGE_UNSAFE_MOMENTUM: "🟠 UNSAFE MOMENTUM — NOT AN ENTRY",
    STAGE_DIAGNOSTIC: "unresolved anchor (diagnostics only)",
}

# --- why a candidate is not at stage C ---------------------------------------
W_NO_ANCHOR = "ANCHOR_NOT_VERIFIED"
W_NO_MARKET = "NO_MARKET_DATA_YET"
W_GATES = "HARD_GATES_NOT_ALL_PASSING"
W_ANCHOR_QUIET = "STOCK_NOT_MOVING"
W_NOT_LEADER = "NOT_THE_LEADING_COIN_ON_THIS_ANCHOR"
W_UNSAFE = "SAFETY_NOT_ESTABLISHED"

HUMAN_WAIT: dict[str, str] = {
    W_NO_ANCHOR: "no address-level link to a Robinhood Stock Token",
    W_NO_MARKET: "no market has formed yet — unpriced, which is what early means",
    W_GATES: "not every hard gate passes on current evidence",
    W_ANCHOR_QUIET: "the stock behind it is not moving",
    W_NOT_LEADER: "another coin already leads this anchor",
    W_UNSAFE: "it is moving and we cannot show that it is safe",
}


@dataclass(frozen=True, slots=True)
class MarketState:
    """Whether a market exists for this launch at all.

    Deliberately minimal.  The full market picture lives in the hard gates;
    this only answers "has anything traded", which is the question that
    separates stage A from stage B.
    """

    priced: bool = False
    liquidity_usd: object | None = None
    volume_24h_usd: object | None = None
    observed_at: int | None = None


@dataclass(frozen=True, slots=True)
class StageDecision:
    """Which stage this launch is at, and what the card must say."""

    meme_address: str
    stage: str = STAGE_DIAGNOSTIC
    wait_reasons: tuple[str, ...] = field(default_factory=tuple)
    why_now: tuple[str, ...] = field(default_factory=tuple)
    anchor_ticker: str = ""

    @property
    def publishable(self) -> bool:
        return self.stage in PUBLISHABLE

    @property
    def may_ping(self) -> bool:
        return self.stage in PINGABLE

    def title(self) -> str:
        return TITLE.get(self.stage, self.stage)

    def waits(self) -> tuple[str, ...]:
        return tuple(HUMAN_WAIT.get(code, code) for code in self.wait_reasons)

    def to_json(self) -> dict[str, object]:
        return {
            "meme_address": self.meme_address,
            "stage": self.stage,
            "title": self.title(),
            "publishable": self.publishable,
            "may_ping": self.may_ping,
            "anchor_ticker": self.anchor_ticker,
            "wait_reasons": list(self.wait_reasons),
            "waits": list(self.waits()),
            "why_now": list(self.why_now),
            "research_only": True,
        }


def classify_stage(
    proof: AnchorProof,
    *,
    market: MarketState | None = None,
    gates: GateReport | None = None,
    anchor: AnchorVerdict | None = None,
    is_anchor_leader: bool | None = None,
) -> StageDecision:
    """Decide the stage.  Each missing requirement is named, not summed.

    The order is the order in which things stop being true: without an anchor
    there is no lane at all; without a market there is nothing to evaluate;
    without safety it cannot be an entry however well it is moving.
    """

    address = proof.meme_address
    ticker = proof.primary.symbol if proof.primary else ""

    # --- unresolved anchors never reach the operator's channel -----------
    if not proof.verified:
        return StageDecision(
            meme_address=address,
            stage=STAGE_DIAGNOSTIC,
            wait_reasons=(W_NO_ANCHOR,),
        )

    # --- stage A: verified, unpriced -------------------------------------
    if market is None or not market.priced:
        return StageDecision(
            meme_address=address,
            stage=STAGE_VERIFIED_LAUNCH,
            anchor_ticker=ticker,
            wait_reasons=(W_NO_MARKET,),
            why_now=(f"verified {proof.human()}",),
        )

    waits: list[str] = []

    # --- stage D: moving and unsafe outranks everything below -------------
    if gates is not None:
        classification = gates.classify()
        if classification == GATE_UNSAFE:
            return StageDecision(
                meme_address=address,
                stage=STAGE_UNSAFE_MOMENTUM,
                anchor_ticker=ticker,
                wait_reasons=(W_UNSAFE, *gates.blocking()[:3]),
            )
        if classification != GATE_ENTRY:
            waits.append(W_GATES)
    else:
        waits.append(W_GATES)

    if anchor is not None and anchor.outcome != STOCK_RUNNER:
        waits.append(W_ANCHOR_QUIET if anchor.heat and not anchor.heat.hot() else W_NOT_LEADER)
    elif anchor is None:
        waits.append(W_ANCHOR_QUIET)

    if is_anchor_leader is False:
        waits.append(W_NOT_LEADER)

    if waits:
        return StageDecision(
            meme_address=address,
            stage=STAGE_TRACTION_WATCH,
            anchor_ticker=ticker,
            wait_reasons=tuple(dict.fromkeys(waits)),
        )

    why: list[str] = [f"verified {proof.human()}"]
    if anchor is not None and anchor.heat is not None:
        why.extend(anchor.heat.reasons[:3])
    why.append("leads its anchor by credible value")
    why.append("every hard gate passes on fresh evidence")
    return StageDecision(
        meme_address=address,
        stage=STAGE_ENTRY_CANDIDATE,
        anchor_ticker=ticker,
        why_now=tuple(why),
    )
