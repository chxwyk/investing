"""One documented policy deciding which lanes may interrupt a human.

The original complaint was that the bot alerts too much.  Adding a quiet new
lane does not fix that, because the volume was never coming from one place: this
bot has several independent publishers — the early lane, fast alerts, the runner
lanes, the trenches lanes, GMGN participants, the Trending lane and now the
pre-trend lane — and each one enforced its own threshold and its own hourly cap.
The rate a human actually experiences is the **sum** of those caps, and no
single lane could see that total, let alone bound it.

So the decision moves here, to one table, consulted at the single publication
choke point every card already passes through.  Each alert class maps to one of
three dispositions per mode:

``PING``
    Publishes and is allowed to interrupt (role mention, push).

``RADAR``
    Publishes to the channel with the interruption removed.  The card is still
    there to scroll; it just does not claim urgency.

``SUPPRESS``
    Not published at all.  Collection, persistence, scoring and the forward
    record are untouched — this governs *messages*, never data.

The last point is the one that makes the quiet modes safe to use: suppressing a
card never suppresses the observation behind it, so a mode change costs
visibility and never costs research.

**Modes**, quietest first:

``SILENT``
    Nothing publishes. Everything is still collected and recorded. For running
    the bot as a pure data collector.

``GROUND_TRUTH``
    Only ``TRENDING_CONFIRMED`` publishes — the record of what actually reached
    the board. Nothing predictive or speculative is shown. This is the mode in
    which the pre-trend research can be evaluated against reality without any
    lane arguing for itself.

``CURATED``
    The pre-trend lane's gated ``PRE_TREND_SIGNAL`` and ``TRENDING_CONFIRMED``
    may ping. Every legacy lane is demoted to ``RADAR``: still visible, never
    interrupting. This is the mode that actually implements "three exceptional
    alerts beat eighty mediocre ones", because exactly one lane can interrupt
    and that lane's own budget is four an hour with cooldowns.

``LEGACY``
    What the bot did before this release: every previously-pinging class still
    pings, and the pre-trend lane behaves per its own settings. **This is the
    default**, so shipping the policy changes nothing until an operator opts in.
    Defaulting to ``CURATED`` would have been a silent change to the behaviour
    of lanes this work did not otherwise touch.

WATCH is not listed because WATCH never produces a card in any mode. It is a
state in the pre-trend state machine, deliberately without a publisher, which is
what "silent WATCH" means: interesting enough to keep watching, not interesting
enough to say anything about.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .fast_alerts import (
    ALERT_CLASSES,
    PINGABLE,
    PRE_TREND_SIGNAL,
    TRENDING_CONFIRMED_ALERT,
)

# --- dispositions ------------------------------------------------------------
PING = "PING"
RADAR = "RADAR"
SUPPRESS = "SUPPRESS"

DISPOSITIONS: tuple[str, ...] = (PING, RADAR, SUPPRESS)

# --- modes -------------------------------------------------------------------
MODE_SILENT = "SILENT"
MODE_GROUND_TRUTH = "GROUND_TRUTH"
MODE_CURATED = "CURATED"
MODE_LEGACY = "LEGACY"

ALERT_MODES: tuple[str, ...] = (
    MODE_SILENT,
    MODE_GROUND_TRUTH,
    MODE_CURATED,
    MODE_LEGACY,
)

DEFAULT_ALERT_MODE = MODE_LEGACY

#: The classes the pre-trend lane owns.  Everything else is a legacy lane.
PRETREND_CLASSES: frozenset[str] = frozenset(
    {PRE_TREND_SIGNAL, TRENDING_CONFIRMED_ALERT}
)

MODE_SUMMARIES: dict[str, str] = {
    MODE_SILENT: "Nothing publishes. Collection and scoring continue.",
    MODE_GROUND_TRUTH: (
        "Only TRENDING_CONFIRMED publishes. No predictive card of any kind."
    ),
    MODE_CURATED: (
        "Only the pre-trend lane may ping (budget: 4/hour with cooldowns). "
        "Every legacy lane is demoted to radar visibility."
    ),
    MODE_LEGACY: (
        "Pre-release behaviour: every previously-pinging class still pings."
    ),
}


def disposition(kind: str, *, mode: str) -> str:
    """What may happen to one alert class under one mode.

    Unknown modes fall back to ``LEGACY`` rather than to silence: a typo in a
    configuration value must not quietly switch the operator's alerting off.
    """

    if mode == MODE_SILENT:
        return SUPPRESS

    if mode == MODE_GROUND_TRUTH:
        return PING if kind == TRENDING_CONFIRMED_ALERT else SUPPRESS

    if mode == MODE_CURATED:
        if kind in PRETREND_CLASSES:
            return PING
        # Legacy lanes keep their card and lose their interruption.
        return RADAR

    # LEGACY, and any unrecognised value.
    return PING if kind in PINGABLE else RADAR


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The disposition for one card, with the reason attached for the log."""

    kind: str
    mode: str
    disposition: str

    @property
    def publish(self) -> bool:
        return self.disposition != SUPPRESS

    @property
    def may_ping(self) -> bool:
        return self.disposition == PING

    @property
    def reason(self) -> str:
        if self.disposition == SUPPRESS:
            return f"{self.kind} does not publish under alert mode {self.mode}"
        if self.disposition == RADAR:
            return f"{self.kind} is radar-only under alert mode {self.mode}"
        return f"{self.kind} may interrupt under alert mode {self.mode}"


def decide(kind: str, *, mode: str = DEFAULT_ALERT_MODE) -> PolicyDecision:
    return PolicyDecision(kind=kind, mode=mode, disposition=disposition(kind, mode=mode))


def normalise_mode(value: object) -> str:
    """Resolve a configured mode, falling back to LEGACY on anything unknown."""

    text = str(value or "").strip().upper().replace("-", "_")
    return text if text in ALERT_MODES else DEFAULT_ALERT_MODE


def describe_policy(mode: str) -> dict[str, Any]:
    """The full table for one mode, for ``/status`` and the README.

    Generated from :func:`disposition` rather than written out separately, so
    the documentation cannot drift away from the behaviour.
    """

    by_disposition: dict[str, list[str]] = {PING: [], RADAR: [], SUPPRESS: []}
    for kind in sorted(ALERT_CLASSES):
        by_disposition[disposition(kind, mode=mode)].append(kind)
    return {
        "mode": mode,
        "summary": MODE_SUMMARIES.get(mode, MODE_SUMMARIES[MODE_LEGACY]),
        "may_ping": by_disposition[PING],
        "radar_only": by_disposition[RADAR],
        "suppressed": by_disposition[SUPPRESS],
        "ping_classes": len(by_disposition[PING]),
        # Collection is never affected by any mode.  Stated in the payload so an
        # operator reading /status sees it next to the counts.
        "collection_affected": False,
    }
