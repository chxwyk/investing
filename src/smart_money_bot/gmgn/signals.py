"""GMGN's documented enums, restated as internal names — and nothing guessed.

Every value here comes from the official ``GMGNAI/gmgn-skills`` client and its
`docs/cli-usage.md`, read at commit ``267ff6b``.  Two rules govern the file:

**Nothing is invented.**  If GMGN emits a signal type this table does not know,
it maps to :data:`SIGNAL_UNKNOWN` and carries its raw code, which is honest and
visible.  Silently folding an unrecognised code into a known family is how a
system starts reporting evidence it never received.

**A provider tag is evidence, not a verdict.**  ``SMART_DEGEN_BUY`` means GMGN
classified the buyer that way; it does not mean the buyer makes money, and it is
deliberately a different thing from this bot's own forward-measured reputation
(section 24).  The same separation applies to KOLs: famous is not profitable.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- chains ------------------------------------------------------------------
CHAIN_SOLANA = "sol"

# --- documented rank / hot-search intervals (docs/cli-usage.md) --------------
INTERVAL_1M = "1m"
INTERVAL_5M = "5m"
INTERVAL_1H = "1h"
INTERVAL_6H = "6h"
INTERVAL_24H = "24h"

#: The intervals GMGN documents for ``/v1/market/rank`` and hot searches.  We do
#: not request windows outside this set: an unsupported interval is not a
#: smaller answer, it is a different endpoint's error.
RANK_INTERVALS: tuple[str, ...] = (
    INTERVAL_1M,
    INTERVAL_5M,
    INTERVAL_1H,
    INTERVAL_6H,
    INTERVAL_24H,
)

#: Intervals for which GMGN documents `price_change_percent` as meaningful.
PRICE_CHANGE_INTERVALS: frozenset[str] = frozenset({INTERVAL_1M, INTERVAL_5M, INTERVAL_1H})

# --- documented trenches sections (buildTrenchesBody) ------------------------
TRENCH_NEW_CREATION = "new_creation"
TRENCH_NEAR_COMPLETION = "near_completion"
TRENCH_COMPLETED = "completed"

TRENCH_TYPES: tuple[str, ...] = (
    TRENCH_NEW_CREATION,
    TRENCH_NEAR_COMPLETION,
    TRENCH_COMPLETED,
)

#: Solana's documented quote-address preference order for the trenches body.
TRENCHES_QUOTE_ADDRESS_TYPES_SOL: tuple[int, ...] = (4, 5, 3, 1, 13, 0)

# --- documented signal types (docs/cli-usage.md, values 1-21) ----------------
SIGNAL_UNKNOWN = "GMGN_SIGNAL_UNKNOWN"

SIGNAL_TYPES: dict[int, str] = {
    1: "KLINE_PRICE_SPIKE",
    2: "DEX_AD",
    3: "DEX_SOCIAL_LINK_UPDATED",
    4: "DEX_TRENDING_BAR",
    5: "DEX_BOOST",
    6: "PRICE_UP",
    7: "PRICE_ALL_TIME_HIGH",
    8: "MARKET_CAP_KEY_LEVEL",
    9: "LIVE_STREAM",
    10: "BUNDLER_SELL",
    11: "COMMUNITY_TAKEOVER",
    12: "SMART_DEGEN_BUY",
    13: "PLATFORM_CALL",
    14: "LARGE_AMOUNT_BUY",
    15: "MULTI_BUY",
    16: "MULTI_LARGE_BUY",
    17: "BAGS_CLAIM",
    18: "PUMP_CLAIM",
    19: "PLATFORM_CALL_V2",
    20: "KOL_BUY",
    21: "BANKER_CLAIM",
}

#: Signals that say *someone bought*, which is the only family that can
#: contribute to a demand case.  A Dex ad placement is paid placement; a social
#: link update is a text edit.  Neither is demand, and grouping them with a buy
#: is how "signals: 4" becomes a reason to interrupt a human.
DEMAND_SIGNALS: frozenset[str] = frozenset(
    {
        "SMART_DEGEN_BUY",
        "LARGE_AMOUNT_BUY",
        "MULTI_BUY",
        "MULTI_LARGE_BUY",
        "KOL_BUY",
    }
)

#: Signals that are paid or cosmetic placement rather than market evidence.
PLACEMENT_SIGNALS: frozenset[str] = frozenset(
    {"DEX_AD", "DEX_BOOST", "DEX_TRENDING_BAR", "DEX_SOCIAL_LINK_UPDATED"}
)

#: Signals that are, on their face, someone leaving.
DISTRIBUTION_SIGNALS: frozenset[str] = frozenset({"BUNDLER_SELL"})


def signal_name(code: object) -> str:
    """Map a provider signal code to an internal name, or say we do not know it.

    An unrecognised code is reported as unknown rather than dropped or guessed
    at, so a new GMGN signal type shows up as something to go and read about
    instead of quietly changing what an alert means.
    """

    try:
        return SIGNAL_TYPES[int(code)]  # type: ignore[arg-type]
    except (TypeError, ValueError, KeyError):
        return SIGNAL_UNKNOWN


def is_demand_signal(code: object) -> bool:
    return signal_name(code) in DEMAND_SIGNALS


# --- provider participant tags (sections 19, 20, 24) -------------------------
#: GMGN classified the wallet as smart money.
TAG_GMGN_SMART_MONEY = "GMGN_SMART_MONEY"
#: GMGN classified the wallet as a KOL.  Attention, not expectancy.
TAG_GMGN_KOL = "GMGN_KOL"
#: This bot's own forward-measured labels, kept separate on purpose.
TAG_BOT_PROVEN_EARLY = "BOT_PROVEN_EARLY"
TAG_BOT_PROVEN_DISTRIBUTOR = "BOT_PROVEN_DISTRIBUTOR"

PARTICIPANT_TAGS: tuple[str, ...] = (
    TAG_GMGN_SMART_MONEY,
    TAG_GMGN_KOL,
    TAG_BOT_PROVEN_EARLY,
    TAG_BOT_PROVEN_DISTRIBUTOR,
)

#: Tags that came from the provider rather than from our own forward record.
PROVIDER_TAGS: frozenset[str] = frozenset({TAG_GMGN_SMART_MONEY, TAG_GMGN_KOL})


@dataclass(frozen=True, slots=True)
class SignalClassification:
    """One provider signal, named and attributed."""

    raw_code: object
    name: str
    demand: bool
    placement: bool
    distribution: bool

    @property
    def known(self) -> bool:
        return self.name != SIGNAL_UNKNOWN

    def to_json(self) -> dict[str, object]:
        return {
            "raw_code": self.raw_code,
            "name": self.name,
            "known": self.known,
            "demand": self.demand,
            "placement": self.placement,
            "distribution": self.distribution,
        }


def classify_signal(code: object) -> SignalClassification:
    name = signal_name(code)
    return SignalClassification(
        raw_code=code,
        name=name,
        demand=name in DEMAND_SIGNALS,
        placement=name in PLACEMENT_SIGNALS,
        distribution=name in DISTRIBUTION_SIGNALS,
    )
