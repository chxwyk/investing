"""Honest calibration against a third-party board an administrator supplies.

Section 83 asks for a way to check our public model against what a professional
terminal actually surfaces — without automating access to a private UI.

So the input is **manual**: an administrator who is looking at a board pastes
what they can see, and this module compares it to our ranking for the same
moment.  Nothing here fetches anything.  There is no client, no URL, no session
and no scraping path; a snapshot exists only because a human typed it in.

The output is calibration, not a target.  Section 84 is explicit that the goal is
not to reproduce a proprietary algorithm — it is to find out whether our public
model finds useful tokens with comparable or better timing.  Overlap is therefore
reported *alongside* lead time, and a low overlap with better lead time is a good
result, not a failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")

#: Where a benchmark observation came from.  Only ever a person.
BENCHMARK_SOURCE_MANUAL = "ADMIN_MANUAL_OBSERVATION"


@dataclass(frozen=True, slots=True)
class BenchmarkEntry:
    """One row an administrator observed on a third-party board."""

    mint: str
    rank: int
    observed_at: int
    market_cap_usd: Decimal | None = None
    note: str = ""


@dataclass(frozen=True, slots=True)
class BenchmarkSnapshot:
    """A manually captured board, with its provenance stated."""

    captured_at: int
    entries: tuple[BenchmarkEntry, ...] = ()
    board_name: str = "third-party terminal"
    source: str = BENCHMARK_SOURCE_MANUAL
    captured_by: str = ""

    def rank_of(self, mint: str) -> int | None:
        return next((item.rank for item in self.entries if item.mint == mint), None)

    def to_json(self) -> dict[str, object]:
        return {
            "captured_at": self.captured_at,
            "board_name": self.board_name,
            "source": self.source,
            "captured_by": self.captured_by,
            "entries": [
                {"mint": item.mint, "rank": item.rank, "observed_at": item.observed_at}
                for item in self.entries
            ],
        }


@dataclass(frozen=True, slots=True)
class BenchmarkComparison:
    """How our model lined up with what a human saw."""

    captured_at: int
    board_name: str
    overlap: int = 0
    benchmark_size: int = 0
    our_size: int = 0
    #: Mints on their board that we also ranked.
    shared: tuple[str, ...] = ()
    #: Mints they surfaced that we did not rank at all.
    missed: tuple[str, ...] = ()
    #: Mints we ranked that their board did not show.
    unique_to_us: tuple[str, ...] = ()
    #: Mean signed rank difference across the shared set; negative means we
    #: ranked it higher (better) than they did.
    mean_rank_difference: Decimal | None = None
    #: For shared mints we had already seen, how long before this snapshot.
    mean_lead_seconds: int | None = None

    @property
    def overlap_ratio(self) -> Decimal | None:
        if self.benchmark_size <= 0:
            return None
        return (Decimal(self.overlap) / Decimal(self.benchmark_size)).quantize(
            Decimal("0.01")
        )

    def summary(self) -> str:
        ratio = self.overlap_ratio
        parts = [
            f"overlap {self.overlap}/{self.benchmark_size}"
            + (f" ({ratio})" if ratio is not None else "")
        ]
        if self.mean_rank_difference is not None:
            direction = "higher" if self.mean_rank_difference < ZERO else "lower"
            parts.append(f"we ranked shared mints {direction} on average")
        if self.mean_lead_seconds is not None:
            parts.append(f"mean lead {self.mean_lead_seconds}s")
        parts.append(f"{len(self.unique_to_us)} only on our board")
        return " • ".join(parts)

    def to_json(self) -> dict[str, object]:
        return {
            "captured_at": self.captured_at,
            "board_name": self.board_name,
            "overlap": self.overlap,
            "benchmark_size": self.benchmark_size,
            "our_size": self.our_size,
            "overlap_ratio": None if self.overlap_ratio is None else str(self.overlap_ratio),
            "shared": list(self.shared),
            "missed": list(self.missed),
            "unique_to_us": list(self.unique_to_us),
            "mean_rank_difference": (
                None if self.mean_rank_difference is None else str(self.mean_rank_difference)
            ),
            "mean_lead_seconds": self.mean_lead_seconds,
            "summary": self.summary(),
        }


def compare_to_benchmark(
    snapshot: BenchmarkSnapshot,
    our_ranks: dict[str, int],
    *,
    first_seen: dict[str, int] | None = None,
) -> BenchmarkComparison:
    """Compare a manually captured board with our own ranking.

    ``first_seen`` lets the comparison answer the question that actually matters
    (section 84): for the tokens we both surfaced, had we already seen it — and
    for how long — before a human saw it there?
    """

    seen = first_seen or {}
    theirs = {item.mint: item.rank for item in snapshot.entries}
    shared = sorted(set(theirs) & set(our_ranks))
    missed = sorted(set(theirs) - set(our_ranks))
    unique = sorted(set(our_ranks) - set(theirs))

    difference = None
    if shared:
        total = sum(our_ranks[mint] - theirs[mint] for mint in shared)
        difference = (Decimal(total) / Decimal(len(shared))).quantize(Decimal("0.01"))

    leads = [
        snapshot.captured_at - seen[mint]
        for mint in shared
        if mint in seen and seen[mint] <= snapshot.captured_at
    ]
    mean_lead = sum(leads) // len(leads) if leads else None

    return BenchmarkComparison(
        captured_at=snapshot.captured_at,
        board_name=snapshot.board_name,
        overlap=len(shared),
        benchmark_size=len(theirs),
        our_size=len(our_ranks),
        shared=tuple(shared),
        missed=tuple(missed),
        unique_to_us=tuple(unique),
        mean_rank_difference=difference,
        mean_lead_seconds=mean_lead,
    )
