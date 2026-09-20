"""The adversary: machinery whose only job is to prove this system is cheating.

Every finding in this engine is worth exactly as much as the guarantee that no
input to a prediction at time ``T`` carries information from after ``T``.  That
guarantee cannot be established by being careful, because leakage is almost
never deliberate — it arrives through a provider that retroactively updates a
field, a feature computed from a series that was appended to after the fact, a
duplicated row, or a mint that appears in both the training and the test split.

So leakage is checked mechanically, and the checks are run as tests.

The specific failures this module is built to catch:

``FUTURE_SOURCE_TIMESTAMP``
    A feature whose source timestamp is later than the observation it feeds.
    The classic shape: a provider refreshes a value, we store it against the old
    observation, and the "point-in-time" row now contains tomorrow's number.

``FUTURE_OBSERVATION``
    Any sample in a feature's input series that postdates the decision instant.

``LABEL_LEAKAGE``
    A feature that is a deterministic function of the outcome — most often a
    board rank or a Trending flag that only exists once the token has entered.

``SPLIT_CONTAMINATION``
    The same mint on both sides of a temporal split.  Meme tokens produce many
    correlated observations; one mint straddling the boundary lets the model
    memorise it and report the memory as generalisation.

``DUPLICATE_OBSERVATION``
    The same ``(mint, timestamp)`` twice.  Doubles a token's weight and,
    when the token is a positive, doubles the apparent signal.

``TEMPORAL_DISORDER``
    A test fold whose rows predate a training fold's.  Silently turns a
    walk-forward evaluation into a random split.

``RETROACTIVE_UPDATE``
    Two readings of the same field, for the same observation instant, with
    different values — proof the store is being rewritten rather than appended.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

FUTURE_SOURCE_TIMESTAMP = "FUTURE_SOURCE_TIMESTAMP"
FUTURE_OBSERVATION = "FUTURE_OBSERVATION"
LABEL_LEAKAGE = "LABEL_LEAKAGE"
SPLIT_CONTAMINATION = "SPLIT_CONTAMINATION"
DUPLICATE_OBSERVATION = "DUPLICATE_OBSERVATION"
TEMPORAL_DISORDER = "TEMPORAL_DISORDER"
RETROACTIVE_UPDATE = "RETROACTIVE_UPDATE"

LEAKAGE_KINDS: tuple[str, ...] = (
    FUTURE_SOURCE_TIMESTAMP,
    FUTURE_OBSERVATION,
    LABEL_LEAKAGE,
    SPLIT_CONTAMINATION,
    DUPLICATE_OBSERVATION,
    TEMPORAL_DISORDER,
    RETROACTIVE_UPDATE,
)

#: Feature names that are, by construction, only knowable once a token is on the
#: board.  Any of these appearing in a pre-trend feature vector is a bug, not a
#: strong signal.
FORBIDDEN_FEATURE_SUBSTRINGS: tuple[str, ...] = (
    "trending_rank",
    "board_rank",
    "first_trending_at",
    "trend_entered",
    "is_trending",
    "on_board",
    "initial_rank",
    "peak_market_cap",
    "max_drawdown",
    "future_",
    "outcome_",
    "label_",
)


@dataclass(frozen=True, slots=True)
class LeakageFinding:
    """One concrete way the dataset could be lying."""

    kind: str
    detail: str
    mint: str = ""
    at: int | None = None
    feature: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "mint": self.mint,
            "at": self.at,
            "feature": self.feature,
        }


@dataclass(frozen=True, slots=True)
class LeakageReport:
    """The verdict.  ``clean`` is the only acceptable state before training."""

    findings: tuple[LeakageFinding, ...] = ()
    rows_checked: int = 0

    @property
    def clean(self) -> bool:
        return not self.findings

    def by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.kind] = counts.get(finding.kind, 0) + 1
        return dict(sorted(counts.items()))

    def to_json(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "rows_checked": self.rows_checked,
            "by_kind": self.by_kind(),
            "findings": [finding.to_json() for finding in self.findings[:50]],
            "total_findings": len(self.findings),
        }


def check_feature_names(names: Sequence[str]) -> tuple[LeakageFinding, ...]:
    """Reject feature names that could only be known after the event."""

    findings: list[LeakageFinding] = []
    for name in names:
        lowered = name.casefold()
        for forbidden in FORBIDDEN_FEATURE_SUBSTRINGS:
            if forbidden in lowered:
                findings.append(
                    LeakageFinding(
                        kind=LABEL_LEAKAGE,
                        feature=name,
                        detail=(
                            f"feature name contains {forbidden!r}, which is only "
                            "knowable at or after the board entry"
                        ),
                    )
                )
                break
    return tuple(findings)


def check_row_timestamps(
    *,
    mint: str,
    observed_at: int,
    source_timestamps: Mapping[str, int | None],
) -> tuple[LeakageFinding, ...]:
    """No input may carry a source timestamp later than the decision instant."""

    findings: list[LeakageFinding] = []
    for feature, source_at in source_timestamps.items():
        if source_at is None:
            continue
        if source_at > observed_at:
            findings.append(
                LeakageFinding(
                    kind=FUTURE_SOURCE_TIMESTAMP,
                    mint=mint,
                    at=observed_at,
                    feature=feature,
                    detail=(
                        f"source timestamp {source_at} is {source_at - observed_at}s "
                        f"after the observation at {observed_at}"
                    ),
                )
            )
    return tuple(findings)


def check_duplicates(
    rows: Sequence[tuple[str, int]]
) -> tuple[LeakageFinding, ...]:
    """Each ``(mint, timestamp)`` may appear at most once."""

    seen: set[tuple[str, int]] = set()
    findings: list[LeakageFinding] = []
    for mint, at in rows:
        key = (mint, at)
        if key in seen:
            findings.append(
                LeakageFinding(
                    kind=DUPLICATE_OBSERVATION,
                    mint=mint,
                    at=at,
                    detail="the same (mint, observed_at) appears more than once",
                )
            )
        seen.add(key)
    return tuple(findings)


def check_split(
    *,
    train: Sequence[tuple[str, int]],
    test: Sequence[tuple[str, int]],
) -> tuple[LeakageFinding, ...]:
    """A split must be strictly temporal and must not share a mint."""

    findings: list[LeakageFinding] = []
    if train and test:
        latest_train = max(at for _, at in train)
        earliest_test = min(at for _, at in test)
        if earliest_test <= latest_train:
            findings.append(
                LeakageFinding(
                    kind=TEMPORAL_DISORDER,
                    at=earliest_test,
                    detail=(
                        f"test fold starts at {earliest_test} which is not after the "
                        f"training fold's last row at {latest_train}"
                    ),
                )
            )
    train_mints = {mint for mint, _ in train}
    for mint in sorted({mint for mint, _ in test} & train_mints):
        findings.append(
            LeakageFinding(
                kind=SPLIT_CONTAMINATION,
                mint=mint,
                detail="the same mint appears in both the training and the test fold",
            )
        )
    return tuple(findings)


def check_series_bounds(
    *,
    mint: str,
    observed_at: int,
    series_latest: Mapping[str, int | None],
) -> tuple[LeakageFinding, ...]:
    """No input series may contain a sample later than the decision instant."""

    findings: list[LeakageFinding] = []
    for name, latest in series_latest.items():
        if latest is not None and latest > observed_at:
            findings.append(
                LeakageFinding(
                    kind=FUTURE_OBSERVATION,
                    mint=mint,
                    at=observed_at,
                    feature=name,
                    detail=(
                        f"series {name!r} contains a sample at {latest}, "
                        f"{latest - observed_at}s after the decision instant"
                    ),
                )
            )
    return tuple(findings)


def check_retroactive_updates(
    readings: Sequence[tuple[str, int, str, Decimal | None]]
) -> tuple[LeakageFinding, ...]:
    """Detect a stored value for one instant changing between two writes.

    ``readings`` is ``(mint, observed_at, field, value)``.  Two different values
    for the same triple means the store is being rewritten rather than appended,
    which invalidates every historical row that field participates in.
    """

    seen: dict[tuple[str, int, str], Decimal | None] = {}
    findings: list[LeakageFinding] = []
    for mint, at, field_name, value in readings:
        key = (mint, at, field_name)
        if key in seen and seen[key] != value:
            findings.append(
                LeakageFinding(
                    kind=RETROACTIVE_UPDATE,
                    mint=mint,
                    at=at,
                    feature=field_name,
                    detail=(
                        f"value changed from {seen[key]} to {value} for the same "
                        "observation instant; historical rows must be append-only"
                    ),
                )
            )
        seen[key] = value
    return tuple(findings)


def audit_dataset(rows: Sequence[Any]) -> LeakageReport:
    """Run every applicable check over assembled feature rows.

    Rows are duck-typed so this can audit a live feature vector, a replayed one
    or a stored one without importing any of them.
    """

    findings: list[LeakageFinding] = []
    pairs: list[tuple[str, int]] = []
    names_checked = False

    for row in rows:
        mint = str(getattr(row, "mint", "") or "")
        observed_at = getattr(row, "observed_at", None)
        if observed_at is None:
            continue
        pairs.append((mint, int(observed_at)))

        values = getattr(row, "values", None)
        if not names_checked and isinstance(values, Mapping):
            findings.extend(check_feature_names(sorted(values)))
            names_checked = True

        sources = getattr(row, "source_timestamps", None)
        if isinstance(sources, Mapping):
            findings.extend(
                check_row_timestamps(
                    mint=mint, observed_at=int(observed_at), source_timestamps=sources
                )
            )

        latest = getattr(row, "series_latest", None)
        if isinstance(latest, Mapping):
            findings.extend(
                check_series_bounds(
                    mint=mint, observed_at=int(observed_at), series_latest=latest
                )
            )

    findings.extend(check_duplicates(pairs))
    return LeakageReport(findings=tuple(findings), rows_checked=len(pairs))
