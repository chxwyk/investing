"""The Early Traction card: three separate blocks, and no profit claims.

Two rules shape every line of this card.

**The blocks stay apart.**  Momentum, quality and safety are rendered as three
labelled fields with no combined number anywhere.  A reader must be able to see
"accelerating" and "62% in the top ten wallets" at the same time and draw their
own conclusion, which is impossible once those have been averaged into a score.

**Nothing here predicts a profit.**  This is a research candidate that matched a
screen, and the card says so.  The risks are "observed", the traction is
"measured", and no field implies an outcome.  That is not legal hedging: the lane
has no forward record for these thresholds yet, so a confident phrasing would be
asserting something nobody has measured.

The safety block is deliberately rendered **before** enrichment returns, showing
``⏳ enrichment in flight`` rather than being omitted.  An absent block reads as
"no risks found"; a pending one reads as "not checked yet", and those are very
different statements about a two-minute-old token.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .constants import axiom_token_url, terminal_token_url
from .discord_render import (
    P_DECISION,
    P_DEMAND,
    P_IDENTITY,
    P_LIFECYCLE,
    P_LINKS,
    P_MOMENTUM,
    P_SAFETY,
    P_WARNINGS,
    CardField,
    CardSpec,
)
from .traction.candidate import TractionCandidate
from .traction.launchpads import POST_MIGRATION, PRE_MIGRATION

#: Research colour: deliberately not green.  Nothing here is a buy.
TRACTION_COLOUR = 0x5DADE2

UNKNOWN = "unknown"


def _money(value: Decimal | None) -> str:
    if value is None:
        return UNKNOWN
    amount = abs(value)
    sign = "-" if value < 0 else ""
    if amount >= 1_000_000:
        return f"{sign}${amount / 1_000_000:.2f}M"
    if amount >= 1_000:
        return f"{sign}${amount / 1_000:.1f}K"
    return f"{sign}${amount:.0f}"


def _age(seconds: int | None) -> str:
    if seconds is None:
        return UNKNOWN
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}m {remainder}s" if remainder else f"{minutes}m"


def _dex_paid(value: bool | None) -> str:
    """Three states, because unknown is not the same as unpaid."""

    if value is None:
        return UNKNOWN
    return "yes" if value else "no"


def _migration(state: str) -> str:
    if state == PRE_MIGRATION:
        return "PRE-migration (bonding curve)"
    if state == POST_MIGRATION:
        return "POST-migration (AMM pool)"
    return "migration state unknown"


def _links(mint: str) -> str:
    """Navigation only.  Every URL is derived from the exact mint.

    The Axiom and Terminal templates come from :mod:`smart_money_bot.constants`
    rather than being written here, because an architecture test requires each
    third-party URL template to exist in exactly one place -- one thing for an
    audit to check, and one place to disable it.
    """

    parts = []
    axiom = axiom_token_url(mint)
    if axiom:
        parts.append(f"[AXIOM]({axiom})")
    terminal = terminal_token_url(mint)
    if terminal:
        parts.append(f"[PADRE]({terminal})")
    parts.append(f"[DEXSCREENER](https://dexscreener.com/solana/{mint})")
    parts.append(f"[SOLSCAN](https://solscan.io/token/{mint})")
    return " • ".join(parts)


def build_traction_card(candidate: TractionCandidate) -> CardSpec:
    """Render one Early Traction candidate."""

    observation = candidate.observation
    identity = f"**{observation.name or 'Unknown name'}**"
    if observation.symbol:
        identity += f" / ${observation.symbol}"

    age = candidate.age_seconds()
    latency = candidate.alert_latency_seconds
    detection = candidate.detection_latency_seconds

    fields = [
        CardField(name="TOKEN", value=identity, priority=P_IDENTITY),
        CardField(name="CA", value=f"`{candidate.mint}`", priority=P_IDENTITY),
        CardField(
            name="MATCHED EARLY TRACTION",
            value=(
                f"launchpad: **{candidate.launchpad}** · {_migration(candidate.migration_state)}\n"
                f"age: **{_age(age)}** · mcap: **{_money(observation.market_cap_usd)}** "
                f"· vol: **{_money(observation.volume_usd)}**\n"
                f"liquidity: {_money(observation.liquidity_usd)} · "
                f"dex paid: {_dex_paid(observation.dex_paid)}"
                " (not required by this profile)"
            ),
            priority=P_DECISION,
        ),
        CardField(
            name="MOMENTUM (is it moving?)",
            value="\n".join(candidate.momentum.render_lines()),
            priority=P_MOMENTUM,
        ),
        CardField(
            name="QUALITY (is the move in proportion?)",
            value="\n".join(candidate.quality.render_lines()),
            priority=P_DEMAND,
        ),
    ]

    # Safety is its own field, always present, and never folded into the above.
    safety = candidate.safety
    fields.append(
        CardField(
            name=(
                "SAFETY — observed risk (this profile does NOT filter on these)"
                if safety is None or not safety.pending
                else "SAFETY — pending (this profile does NOT filter on these)"
            ),
            value=(
                "\n".join(safety.render_lines())
                if safety is not None
                else "not collected"
            ),
            priority=P_SAFETY,
        )
    )

    x_link = candidate.x_link
    if x_link is not None:
        value = f"{observation.x_link or UNKNOWN}\n{x_link.summary}"
        if x_link.reuse.reused:
            others = ", ".join(f"`{mint[:8]}…`" for mint in x_link.reuse.other_mints[:5])
            value += f"\nreuse ({x_link.reuse.severity}): {others}"
        fields.append(
            CardField(name="X / TWITTER", value=value[:1024], priority=P_LIFECYCLE)
        )

    fields.append(
        CardField(
            name="DETECTION SPEED",
            value=(
                f"creation → detection: {_age(detection)}\n"
                f"creation → this alert: {_age(latency)}"
            ),
            priority=P_WARNINGS,
        )
    )
    fields.append(CardField(name="LINKS", value=_links(candidate.mint), priority=P_LINKS))

    return CardSpec(
        title=f"🔎 EARLY TRACTION — {candidate.launchpad} · {_age(age)} old",
        description=(
            "Research candidate matching the Early Traction screen. Read-only: no "
            "buy, sell or SOL spend originates from this lane. Momentum, quality "
            "and safety are reported separately and are not combined into a score. "
            "No outcome is predicted."
        ),
        compact_description=(
            f"EARLY TRACTION {candidate.launchpad} · {_age(age)} · "
            f"mcap {_money(observation.market_cap_usd)} · `{candidate.mint}`"
        ),
        fields=tuple(fields),
        colour=TRACTION_COLOUR,
        footer=(
            "Matched a screen; not a recommendation. Safety values shown are "
            "observed, and 'unknown' means unmeasured — never zero."
        ),
        timestamp=datetime.fromtimestamp(
            candidate.alert_sent_at or candidate.qualified_at or 0, tz=UTC
        ),
    )


def enrichment_fields(candidate: TractionCandidate) -> dict[str, Any]:
    """The safety field as the edit applies it, for the enrichment pass."""

    safety = candidate.safety
    return {
        "name": "SAFETY — observed risk (this profile does NOT filter on these)",
        "value": "\n".join(safety.render_lines()) if safety else "not collected",
    }
