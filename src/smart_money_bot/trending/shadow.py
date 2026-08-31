"""The FOMO_TRENDING shadow experiment — a second, completely separate bankroll.

The legacy shadow experiment is not touched by anything in this module.  It keeps
its own ``strategy_version``, its own bankroll row, its own positions and its
whole forward history, because the entire point is a *fair comparison* and you
cannot compare two strategies that share an account (sections 62, 63, 106).

Isolation is structural rather than conventional: the shadow store keys bankroll
rows by ``strategy_version`` and open positions by
``(mint, family, strategy_version)``, so a distinct version string is a hard
partition.  A Trending fill and a legacy fill on the same mint are two unrelated
positions in two unrelated books.

Both experiments run the same shape — $100 bankroll, $10 per position, at most 5
open, at most $50 exposed — so the only variable is the strategy (section 52).
And, as everywhere else in this codebase: this is simulation.  There is no
signer, no key, and no path to a real swap.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

#: Hard partition from ``shadow-v1``.  Never reuse the legacy version string.
TRENDING_STRATEGY_VERSION = "trending-shadow-v1"
TRENDING_EXPERIMENT_VERSION = "trending-shadow-experiment-1"

#: The legacy experiment's identifiers, imported here only so that the isolation
#: test has one obvious place to assert they differ.
LEGACY_STRATEGY_VERSION = "shadow-v1"
LEGACY_EXPERIMENT_VERSION = "shadow-experiment-1"

# --- Trending signal families (section 64) -----------------------------------
FAMILY_NEW_ENTRY = "TRENDING_NEW_ENTRY"
FAMILY_ACCELERATION = "TRENDING_ACCELERATION"
FAMILY_STORY = "TRENDING_STORY"
FAMILY_THESIS = "TRENDING_THESIS"
FAMILY_AI_PROJECT = "TRENDING_AI_PROJECT"
FAMILY_SMART_MONEY = "TRENDING_SMART_MONEY"
FAMILY_CONTINUATION = "TRENDING_CONTINUATION"
FAMILY_CONFLUENCE = "TRENDING_CONFLUENCE"

TRENDING_FAMILIES: tuple[str, ...] = (
    FAMILY_NEW_ENTRY,
    FAMILY_ACCELERATION,
    FAMILY_STORY,
    FAMILY_THESIS,
    FAMILY_AI_PROJECT,
    FAMILY_SMART_MONEY,
    FAMILY_CONTINUATION,
    FAMILY_CONFLUENCE,
)

TRENDING_FAMILY_LABELS: dict[str, str] = {
    FAMILY_NEW_ENTRY: "TRENDING NEW ENTRY",
    FAMILY_ACCELERATION: "TRENDING ACCELERATION",
    FAMILY_STORY: "TRENDING STORY",
    FAMILY_THESIS: "TRENDING THESIS",
    FAMILY_AI_PROJECT: "TRENDING AI / PROJECT",
    FAMILY_SMART_MONEY: "TRENDING SMART MONEY",
    FAMILY_CONTINUATION: "TRENDING CONTINUATION",
    FAMILY_CONFLUENCE: "TRENDING CONFLUENCE",
}

#: Map a named alert reason onto the shadow family it belongs to.  Only these
#: reasons can produce a simulated entry: Trending Radar shows everything
#: relevant, but the shadow experiment trades a configured strategy, not every
#: token on the board (section 65).
REASON_TO_FAMILY: dict[str, str] = {
    "TRENDING_NEW_ENTRY": FAMILY_NEW_ENTRY,
    "TRENDING_ACCELERATION": FAMILY_ACCELERATION,
    "STORY": FAMILY_STORY,
    "THESIS": FAMILY_THESIS,
    "AI_PROJECT": FAMILY_AI_PROJECT,
    "SMART_MONEY": FAMILY_SMART_MONEY,
    "TRENDING_CONTINUATION": FAMILY_CONTINUATION,
    "CONFLUENCE": FAMILY_CONFLUENCE,
}

#: Reason families that are never, on their own, enough for a simulated entry.
#: Chatter without market confirmation is the classic one (section 102).
NON_TRADEABLE_ALONE: frozenset[str] = frozenset({"PUBLIC_SOCIAL", "HOLDER_EXPANSION"})


@dataclass(frozen=True, slots=True)
class TrendingShadowConfig:
    """$100 / $10 / 5 / $50 — identical shape to legacy, separate bankroll."""

    strategy_version: str = TRENDING_STRATEGY_VERSION
    experiment_version: str = TRENDING_EXPERIMENT_VERSION
    enabled: bool = True

    bankroll_usd: Decimal = Decimal("100")
    position_usd: Decimal = Decimal("10")
    max_concurrent_positions: int = 5
    max_total_exposure_usd: Decimal = Decimal("50")

    #: Minimum Trending edge score a signal must carry to be simulated.
    min_edge_score: Decimal = Decimal("62")
    #: A signal older than this is not acted on.
    max_signal_age_seconds: int = 900

    def __post_init__(self) -> None:
        # These are structural invariants of the comparison, not preferences: a
        # deployment that changes the shape breaks the experiment it is meant to
        # be running, so it fails loudly at construction.
        if self.position_usd != Decimal("10"):
            raise ValueError("the Trending shadow position size is $10 by design")
        if self.bankroll_usd != Decimal("100"):
            raise ValueError("the Trending shadow bankroll is $100 by design")
        if self.max_concurrent_positions != 5:
            raise ValueError("the Trending shadow holds at most 5 positions by design")
        if self.max_total_exposure_usd != Decimal("50"):
            raise ValueError("the Trending shadow exposes at most $50 by design")
        if self.strategy_version == LEGACY_STRATEGY_VERSION:
            raise ValueError(
                "the Trending shadow must never share the legacy shadow's strategy version"
            )

    def config_hash(self) -> str:
        return (
            f"{self.strategy_version}:{self.bankroll_usd}:{self.position_usd}:"
            f"{self.max_concurrent_positions}:{self.max_total_exposure_usd}:"
            f"{self.min_edge_score}"
        )


DEFAULT_TRENDING_SHADOW_CONFIG = TrendingShadowConfig()


def family_for_reasons(reasons: tuple[str, ...]) -> str | None:
    """Pick the family a Trending signal belongs to, or ``None`` if untradeable.

    Preference order follows the reason list's own ordering, so the strongest
    named reason wins rather than whichever happened to be checked first.
    """

    tradeable = [reason for reason in reasons if reason not in NON_TRADEABLE_ALONE]
    if not tradeable:
        return None
    # Confluence, when present, is the honest description of the signal.
    if "CONFLUENCE" in tradeable:
        return FAMILY_CONFLUENCE
    for reason in tradeable:
        family = REASON_TO_FAMILY.get(reason)
        if family is not None:
            return family
    return None
