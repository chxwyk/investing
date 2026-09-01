"""One mint, one life — from new pair to second leg.

The operator's workflow video organises a board by lifecycle: NEW PAIRS, FINAL
STRETCH, MIGRATED.  That is a genuinely better shape than the one this bot grew
into, where a token that graduated became a *new candidate* and arrived with no
memory of having been watched at $18K twenty minutes earlier.

So this module holds a single record per exact mint, and stages are transitions
on it rather than separate objects.  Two consequences that matter:

* **Lateness becomes measurable.**  Every stage records the market cap and the
  time it happened, so "first seen $100K, promoted $300K" is a fact the card can
  state rather than something an operator has to notice (section 49).
* **A stage change is not a new discovery.**  A mint that reappears in a
  different feed is the same token, and it keeps its history.

Pure logic: no provider, no database, no signer.  The stage names are ours, not
a vendor's — they are derived from bonding progress and observable market state,
which is why they survive a provider being down.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal

ZERO = Decimal("0")
HUNDRED = Decimal("100")

# --- stages (sections 9, 10) -------------------------------------------------
#: Observed at creation, before a market exists in any useful sense.
TOKEN_CREATED = "TOKEN_CREATED"
#: Trading, brand new.  The video's "NEW PAIRS".
NEW_PAIR = "NEW_PAIR"
#: On the curve, early.
EARLY_CURVE = "EARLY_CURVE"
#: On the curve, established.
MID_CURVE = "MID_CURVE"
#: The video's "FINAL STRETCH" — close enough to completion that it is a
#: distinct trade rather than a further stage of the same one.
FINAL_STRETCH = "FINAL_STRETCH"
#: Nearly done.
NEAR_COMPLETION = "NEAR_COMPLETION"
#: Curve complete, migration in flight.
GRADUATING = "GRADUATING"
#: The video's "MIGRATED".
RECENTLY_MIGRATED = "RECENTLY_MIGRATED"
#: Trading on the AMM.
PUMPSWAP = "PUMPSWAP"
#: Earning attention on a ranking board.
TRENDING = "TRENDING"
#: A later leg, after a first move already happened.
CONTINUATION = "CONTINUATION"
#: Attention gone.
FADING = "FADING"
#: Effectively over.
DEAD = "DEAD"
#: We have a mint and nothing else yet.
UNKNOWN = "UNKNOWN"

STAGES: tuple[str, ...] = (
    TOKEN_CREATED,
    NEW_PAIR,
    EARLY_CURVE,
    MID_CURVE,
    FINAL_STRETCH,
    NEAR_COMPLETION,
    GRADUATING,
    RECENTLY_MIGRATED,
    PUMPSWAP,
    TRENDING,
    CONTINUATION,
    FADING,
    DEAD,
    UNKNOWN,
)

#: How far along the ordered path each stage sits.  Used to tell a genuine
#: advance from a noisy provider re-report; it is deliberately *not* a score.
_ORDER: dict[str, int] = {name: index for index, name in enumerate(STAGES)}

#: Stages still on the bonding curve.
PRE_GRADUATION: frozenset[str] = frozenset(
    {NEW_PAIR, EARLY_CURVE, MID_CURVE, FINAL_STRETCH, NEAR_COMPLETION}
)
#: Stages after the curve completed.
POST_GRADUATION: frozenset[str] = frozenset(
    {GRADUATING, RECENTLY_MIGRATED, PUMPSWAP, TRENDING, CONTINUATION}
)

#: The three board sections the operator's workflow is organised around.
BOARD_NEW_PAIRS = "NEW_PAIRS"
BOARD_FINAL_STRETCH = "FINAL_STRETCH"
BOARD_MIGRATED = "MIGRATED"

BOARD_SECTIONS: dict[str, frozenset[str]] = {
    BOARD_NEW_PAIRS: frozenset({TOKEN_CREATED, NEW_PAIR, EARLY_CURVE, MID_CURVE}),
    BOARD_FINAL_STRETCH: frozenset({FINAL_STRETCH, NEAR_COMPLETION, GRADUATING}),
    BOARD_MIGRATED: frozenset({RECENTLY_MIGRATED, PUMPSWAP, TRENDING, CONTINUATION}),
}


def board_section(stage: str) -> str:
    for section, members in BOARD_SECTIONS.items():
        if stage in members:
            return section
    return ""


@dataclass(frozen=True, slots=True)
class StageMark:
    """When a stage was first reached, and what the market cap was then."""

    stage: str
    at: int
    market_cap_usd: Decimal | None = None
    source: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "at": self.at,
            "market_cap_usd": _s(self.market_cap_usd),
            "source": self.source,
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True, slots=True)
class StageConfig:
    """Where one stage ends and the next begins, on bonding progress."""

    #: Below this fraction of the curve consumed: brand new.
    new_pair_progress: Decimal = Decimal("0.05")
    early_progress: Decimal = Decimal("0.35")
    mid_progress: Decimal = Decimal("0.70")
    #: The video's "final stretch" band.
    final_stretch_progress: Decimal = Decimal("0.90")
    near_completion_progress: Decimal = Decimal("0.98")
    #: A pair younger than this is a new pair whatever the curve says.
    new_pair_max_age_seconds: int = 300
    #: How long after migration a token still counts as recently migrated.
    recently_migrated_seconds: int = 1_800


DEFAULT_STAGE_CONFIG = StageConfig()


def classify_stage(
    *,
    bonding_progress: Decimal | None,
    complete: bool | None,
    pair_age_seconds: int | None = None,
    seconds_since_migration: int | None = None,
    on_amm: bool = False,
    trending_rank: int | None = None,
    config: StageConfig = DEFAULT_STAGE_CONFIG,
) -> str:
    """Name the stage from observable state, never from a vendor's label.

    Order matters: post-graduation facts win over curve progress, because a
    completed curve makes the progress number meaningless, and a stale progress
    read must not drag a migrated token back onto the curve.
    """

    if complete or on_amm or seconds_since_migration is not None:
        if trending_rank is not None:
            return TRENDING
        if (
            seconds_since_migration is not None
            and seconds_since_migration <= config.recently_migrated_seconds
        ):
            return RECENTLY_MIGRATED
        if on_amm:
            return PUMPSWAP
        return GRADUATING

    if bonding_progress is None:
        # No curve reading.  A very young pair is still a new pair; anything
        # else is honestly unknown rather than assumed early.
        if pair_age_seconds is not None and pair_age_seconds <= config.new_pair_max_age_seconds:
            return NEW_PAIR
        return UNKNOWN

    if (
        pair_age_seconds is not None
        and pair_age_seconds <= config.new_pair_max_age_seconds
        and bonding_progress < config.early_progress
    ):
        return NEW_PAIR
    if bonding_progress < config.new_pair_progress:
        return NEW_PAIR
    if bonding_progress < config.early_progress:
        return EARLY_CURVE
    if bonding_progress < config.mid_progress:
        return MID_CURVE
    if bonding_progress < config.final_stretch_progress:
        return FINAL_STRETCH
    if bonding_progress < config.near_completion_progress:
        return NEAR_COMPLETION
    return GRADUATING


@dataclass(frozen=True, slots=True)
class TokenLifecycle:
    """One exact mint, followed from creation to whatever it becomes.

    ``first_seen_*`` is written once.  Everything downstream that says "this was
    late" is a comparison against it, so an enrichment pass that overwrote it
    would delete the only evidence of lateness the system has.
    """

    mint: str
    chain: str = "solana"
    stage: str = UNKNOWN
    first_seen_at: int = 0
    first_seen_market_cap_usd: Decimal | None = None
    first_seen_source: str = ""
    updated_at: int = 0
    current_market_cap_usd: Decimal | None = None
    peak_market_cap_usd: Decimal | None = None
    marks: tuple[StageMark, ...] = field(default_factory=tuple)
    #: Every distinct feed that has reported this mint, in order of first sight.
    sources: tuple[str, ...] = field(default_factory=tuple)

    def mark_for(self, stage: str) -> StageMark | None:
        for item in self.marks:
            if item.stage == stage:
                return item
        return None

    def reached(self, stage: str) -> bool:
        return self.mark_for(stage) is not None

    @property
    def board_section(self) -> str:
        return board_section(self.stage)

    @property
    def pre_graduation(self) -> bool:
        return self.stage in PRE_GRADUATION

    def move_since_first_seen_percent(self) -> Decimal | None:
        base = self.first_seen_market_cap_usd
        if base is None or base <= ZERO or self.current_market_cap_usd is None:
            return None
        return ((self.current_market_cap_usd - base) / base * HUNDRED).quantize(Decimal("0.01"))

    def seconds_at_stage(self, stage: str) -> int | None:
        mark = self.mark_for(stage)
        return None if mark is None else self.updated_at - mark.at

    def lead_over(self, stage: str, other_stage: str) -> int | None:
        """How many seconds earlier ``stage`` happened than ``other_stage``.

        This is how section 95 gets answered: whether Pump realtime, GMGN
        trenches or a story watch actually saw something first.
        """

        first, second = self.mark_for(stage), self.mark_for(other_stage)
        if first is None or second is None:
            return None
        return second.at - first.at

    def render_path(self) -> str:
        return " → ".join(item.stage for item in self.marks) or self.stage

    def to_json(self) -> dict[str, object]:
        return {
            "mint": self.mint,
            "chain": self.chain,
            "stage": self.stage,
            "board_section": self.board_section,
            "first_seen_at": self.first_seen_at,
            "first_seen_market_cap_usd": _s(self.first_seen_market_cap_usd),
            "first_seen_source": self.first_seen_source,
            "updated_at": self.updated_at,
            "current_market_cap_usd": _s(self.current_market_cap_usd),
            "peak_market_cap_usd": _s(self.peak_market_cap_usd),
            "move_since_first_seen_percent": _s(self.move_since_first_seen_percent()),
            "marks": [item.to_json() for item in self.marks],
            "sources": list(self.sources),
            "path": self.render_path(),
        }


def open_lifecycle(
    mint: str,
    *,
    stage: str,
    at: int,
    market_cap_usd: Decimal | None = None,
    source: str = "",
    chain: str = "solana",
) -> TokenLifecycle:
    """First sight of a mint.  Everything later is measured against this."""

    return TokenLifecycle(
        mint=mint,
        chain=chain,
        stage=stage,
        first_seen_at=at,
        first_seen_market_cap_usd=market_cap_usd,
        first_seen_source=source,
        updated_at=at,
        current_market_cap_usd=market_cap_usd,
        peak_market_cap_usd=market_cap_usd,
        marks=(StageMark(stage=stage, at=at, market_cap_usd=market_cap_usd, source=source),),
        sources=(source,) if source else (),
    )


def advance(
    lifecycle: TokenLifecycle,
    *,
    stage: str,
    at: int,
    market_cap_usd: Decimal | None = None,
    source: str = "",
) -> TokenLifecycle:
    """Record an observation.  The same mint keeps its history (section 10).

    A stage is marked the *first* time it is reached and never re-marked, so the
    timeline stays a record of what happened rather than of how often a feed
    repeated itself.  Going backwards — a stale read after a fresh one — updates
    the market cap but does not rewrite the stage, because a token does not
    un-graduate.
    """

    marks = lifecycle.marks
    if not lifecycle.reached(stage):
        marks = (
            *marks,
            StageMark(stage=stage, at=at, market_cap_usd=market_cap_usd, source=source),
        )

    forward = _ORDER.get(stage, -1) >= _ORDER.get(lifecycle.stage, -1)
    unknown_now = stage == UNKNOWN
    sources = lifecycle.sources
    if source and source not in sources:
        sources = (*sources, source)

    return replace(
        lifecycle,
        stage=lifecycle.stage if (unknown_now or not forward) else stage,
        updated_at=max(lifecycle.updated_at, at),
        current_market_cap_usd=(
            market_cap_usd if market_cap_usd is not None else lifecycle.current_market_cap_usd
        ),
        peak_market_cap_usd=_max_optional(lifecycle.peak_market_cap_usd, market_cap_usd),
        marks=marks,
        sources=sources,
    )


def _max_optional(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def lifecycle_from_json(payload: dict[str, object]) -> TokenLifecycle:
    raw_marks = payload.get("marks") or ()
    marks: list[StageMark] = []
    for item in raw_marks:
        if not isinstance(item, dict):
            continue
        value = item.get("market_cap_usd")
        marks.append(
            StageMark(
                stage=str(item.get("stage") or UNKNOWN),
                at=int(item.get("at") or 0),
                market_cap_usd=None if value in (None, "") else Decimal(str(value)),
                source=str(item.get("source") or ""),
            )
        )
    return TokenLifecycle(
        mint=str(payload.get("mint") or ""),
        chain=str(payload.get("chain") or "solana"),
        stage=str(payload.get("stage") or UNKNOWN),
        first_seen_at=int(payload.get("first_seen_at") or 0),
        first_seen_market_cap_usd=_d(payload.get("first_seen_market_cap_usd")),
        first_seen_source=str(payload.get("first_seen_source") or ""),
        updated_at=int(payload.get("updated_at") or 0),
        current_market_cap_usd=_d(payload.get("current_market_cap_usd")),
        peak_market_cap_usd=_d(payload.get("peak_market_cap_usd")),
        marks=tuple(marks),
        sources=tuple(str(item) for item in (payload.get("sources") or ())),
    )


def _d(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def group_board(
    lifecycles: Sequence[TokenLifecycle],
) -> dict[str, tuple[TokenLifecycle, ...]]:
    """Split live candidates into the three sections the operator works from."""

    board: dict[str, list[TokenLifecycle]] = {
        BOARD_NEW_PAIRS: [],
        BOARD_FINAL_STRETCH: [],
        BOARD_MIGRATED: [],
    }
    for item in lifecycles:
        section = item.board_section
        if section in board:
            board[section].append(item)
    return {
        name: tuple(sorted(rows, key=lambda row: -row.updated_at))
        for name, rows in board.items()
    }
