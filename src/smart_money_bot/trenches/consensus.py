"""The union of discovery sources, and what counts as genuine agreement.

Section 33: the candidate universe is the *union* of every legitimate source, so
no single vendor can decide what the bot is allowed to see.  If DEX Screener is
degraded, Pump on-chain still produces candidates; if Fomo is unavailable, the
public model still produces them.

Section 34 is the subtler half.  Four sources naming the same mint is only strong
evidence if the four are *independent*.  Two market vendors relaying the same
on-chain trades are one observation with two invoices, and counting them as two
manufactures confidence out of nothing.  So consensus is computed over evidence
*families* (defined in :mod:`.provenance`), never over feed count.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .provenance import SourceRef, independent_families

ZERO = Decimal("0")

# --- where a candidate can come from (section 33) ----------------------------
LANE_PUMP_NEW = "PUMP_NEW"
LANE_PUMP_BONDING = "PUMP_BONDING"
LANE_PUMP_GRADUATED = "PUMP_RECENTLY_GRADUATED"
LANE_PUMPSWAP = "PUMPSWAP"
LANE_DEX_ACTIVE = "DEXSCREENER_ACTIVE"
LANE_DEX_BOOSTS = "DEXSCREENER_BOOSTS"
LANE_NOTABLE_WALLET = "NOTABLE_WALLET_BUY"
LANE_FOMO_AUTHORIZED = "FOMO_AUTHORIZED_TRENDING"
LANE_PUBLIC_MODEL = "PUBLIC_TRENDING_MODEL"
LANE_STORY_WATCH = "STORY_WATCH"
LANE_J7 = "J7_AUTHORIZED_MENTION"

DISCOVERY_LANES: tuple[str, ...] = (
    LANE_PUMP_NEW,
    LANE_PUMP_BONDING,
    LANE_PUMP_GRADUATED,
    LANE_PUMPSWAP,
    LANE_DEX_ACTIVE,
    LANE_DEX_BOOSTS,
    LANE_NOTABLE_WALLET,
    LANE_FOMO_AUTHORIZED,
    LANE_PUBLIC_MODEL,
    LANE_STORY_WATCH,
    LANE_J7,
)

#: Lanes that keep working when every third-party vendor is down (section 4).
SELF_SUFFICIENT_LANES: frozenset[str] = frozenset(
    {LANE_PUMP_NEW, LANE_PUMP_BONDING, LANE_PUMP_GRADUATED, LANE_PUMPSWAP, LANE_NOTABLE_WALLET}
)


@dataclass(frozen=True, slots=True)
class Nomination:
    """One source proposing one exact mint."""

    mint: str
    lane: str
    source: SourceRef
    at: int = 0
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ConsensusResult:
    """How many genuinely independent things point at this mint (section 34)."""

    mint: str
    lanes: tuple[str, ...] = ()
    sources: tuple[SourceRef, ...] = ()
    independent_families: tuple[str, ...] = ()
    first_seen_at: int = 0

    @property
    def lane_count(self) -> int:
        return len(self.lanes)

    @property
    def independent_count(self) -> int:
        """The number that actually matters — never the raw feed count."""

        return len(self.independent_families)

    @property
    def strong(self) -> bool:
        return self.independent_count >= 3

    def operator_line(self) -> str:
        return (
            f"{self.lane_count} lane(s), {self.independent_count} independent "
            f"evidence famil{'y' if self.independent_count == 1 else 'ies'}: "
            + ", ".join(self.independent_families)
        )

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "lanes": list(self.lanes),
            "sources": [item.to_json() for item in self.sources],
            "independent_families": list(self.independent_families),
            "lane_count": self.lane_count,
            "independent_count": self.independent_count,
            "strong": self.strong,
            "first_seen_at": self.first_seen_at,
        }


def build_consensus(nominations: Sequence[Nomination]) -> dict[str, ConsensusResult]:
    """Union the nominations by exact mint and count independent agreement."""

    grouped: dict[str, list[Nomination]] = {}
    for nomination in nominations:
        grouped.setdefault(nomination.mint, []).append(nomination)

    results: dict[str, ConsensusResult] = {}
    for mint, group in grouped.items():
        sources = tuple(item.source for item in group)
        results[mint] = ConsensusResult(
            mint=mint,
            lanes=tuple(sorted({item.lane for item in group})),
            sources=sources,
            independent_families=tuple(sorted(independent_families(sources))),
            first_seen_at=min((item.at for item in group if item.at), default=0),
        )
    return results


@dataclass(frozen=True, slots=True)
class LaneHealth:
    """Whether a discovery lane is actually producing anything."""

    lane: str
    enabled: bool = False
    configured: bool = False
    nominations: int = 0
    last_nomination_at: int | None = None
    last_error: str = ""

    @property
    def state(self) -> str:
        if not self.configured:
            return "NO_SOURCE_CONFIGURED"
        if not self.enabled:
            return "DISABLED_BY_CONFIG"
        if self.last_error:
            return "DEGRADED"
        if self.nominations <= 0:
            return "ACTIVE_NO_CANDIDATES"
        return "ACTIVE"

    def to_json(self) -> dict[str, object]:
        return {
            "lane": self.lane,
            "state": self.state,
            "enabled": self.enabled,
            "configured": self.configured,
            "nominations": self.nominations,
            "last_nomination_at": self.last_nomination_at,
            "last_error": self.last_error,
        }


@dataclass(frozen=True, slots=True)
class UniverseHealth:
    """The whole discovery surface, so a silent lane cannot hide (section 4)."""

    lanes: tuple[LaneHealth, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def self_sufficient(self) -> bool:
        """True while at least one vendor-independent lane is still producing."""

        return any(
            item.lane in SELF_SUFFICIENT_LANES and item.state == "ACTIVE"
            for item in self.lanes
        )

    @property
    def active_lanes(self) -> int:
        return sum(1 for item in self.lanes if item.state == "ACTIVE")

    def to_json(self) -> dict[str, object]:
        return {
            "lanes": [item.to_json() for item in self.lanes],
            "active_lanes": self.active_lanes,
            "self_sufficient": self.self_sufficient,
            "notes": list(self.notes),
        }
