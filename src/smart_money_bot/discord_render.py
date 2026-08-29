"""One shared safe renderer for rich Fomo cards (sections 24-29).

Production hit Discord ``HTTP 400 / 50035 Invalid Form Body — Embed size
exceeds maximum size of 6000`` on both ``/fomo opportunities`` and
``/fomo lab mode:test`` while the discovery engine had found perfectly valid
candidates.  The trading system did not fail; the presentation layer did.

The root cause was a *message-level* limit being checked at *embed* level.
Discord's 6000-character budget applies to the sum of every embed in one
message, but each card was clamped individually, so three or five
individually-legal cards could still exceed the message budget together.

This module owns the whole problem:

* one conservative aggregate budget with headroom below Discord's hard limit,
* priority-ordered trimming so identity, decision, safety and the exact mint
  survive while long ABOUT text and verbose narratives go first,
* a compact card that keeps only what a reader must have,
* and exactly one emergency minimal retry if Discord still refuses.

A real candidate must never disappear because its optional metadata was large,
and a render failure must never fabricate a candidate or trigger a trade.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

import discord

logger = logging.getLogger(__name__)

# --- Discord's documented hard limits ---------------------------------------
EMBED_TITLE_LIMIT = 256
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_FIELD_NAME_LIMIT = 256
EMBED_FIELD_VALUE_LIMIT = 1024
EMBED_FOOTER_LIMIT = 2048
EMBED_FIELD_COUNT_LIMIT = 25
EMBED_COUNT_LIMIT = 10
MESSAGE_EMBED_LIMIT = 6000

#: Conservative aggregate budget.  Discord counts a few things we do not model
#: exactly, so the renderer never plans to use the last ~13% of the allowance.
SAFE_MESSAGE_BUDGET = 5200

#: Budget for the compact fallback pass.
COMPACT_MESSAGE_BUDGET = 3600

#: Budget for the emergency minimal response.
MINIMAL_MESSAGE_BUDGET = 1500

# --- field priorities (section 26) ------------------------------------------
# Lower number = kept longer.  These are the "PRESERVE FIRST" list, in order.
P_IDENTITY = 0
P_DECISION = 10
P_LIFECYCLE = 20
P_SAFETY = 30
P_OPPORTUNITY = 40
P_MOMENTUM = 50
P_DEMAND = 60
P_LIQUIDITY = 70
P_EDGE = 80
P_WHY_NOT_ENTRY = 90
P_LINKS = 100
# --- "TRIM/DROP FIRST" list, in reverse order of usefulness ----------------
P_WARNINGS = 200
P_WHY_SURFACED = 210
P_SMART_MONEY = 300
P_SOCIAL = 310
P_ABOUT = 320
P_DIAGNOSTICS = 400

#: Anything at or above this priority is optional enrichment.
OPTIONAL_PRIORITY_FLOOR = 200


@dataclass(frozen=True, slots=True)
class CardField:
    """One embed field with an explicit trimming priority."""

    name: str
    value: str
    priority: int = P_DIAGNOSTICS
    inline: bool = False

    @property
    def cost(self) -> int:
        return len(self.name) + len(self.value)

    def bounded(self) -> CardField:
        return replace(
            self,
            name=_cut(self.name, EMBED_FIELD_NAME_LIMIT),
            value=_cut(self.value, EMBED_FIELD_VALUE_LIMIT) or "unavailable",
        )


@dataclass(frozen=True, slots=True)
class CardSpec:
    """A rich card described independently of Discord's object model.

    ``compact_description`` is the honest minimum: what the reader must still
    see if everything optional has to go.
    """

    title: str = ""
    description: str = ""
    compact_description: str = ""
    fields: tuple[CardField, ...] = ()
    footer: str = ""
    thumbnail_url: str = ""
    colour: int = 0x5865F2
    timestamp: datetime | None = None

    def with_fields(self, fields: Iterable[CardField]) -> CardSpec:
        return replace(self, fields=tuple(fields))

    @property
    def essential_fields(self) -> tuple[CardField, ...]:
        return tuple(item for item in self.fields if item.priority < OPTIONAL_PRIORITY_FLOOR)


def _cut(value: str, limit: int) -> str:
    text = value or ""
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def build_embed(spec: CardSpec, *, fields: Sequence[CardField] | None = None) -> discord.Embed:
    """Turn a spec into a Discord embed, respecting every per-object limit."""

    chosen = list(fields if fields is not None else spec.fields)[:EMBED_FIELD_COUNT_LIMIT]
    embed = discord.Embed(
        title=_cut(spec.title, EMBED_TITLE_LIMIT) or None,
        description=_cut(spec.description, EMBED_DESCRIPTION_LIMIT) or None,
        colour=spec.colour,
        timestamp=spec.timestamp,
    )
    for item in chosen:
        bounded = item.bounded()
        embed.add_field(name=bounded.name, value=bounded.value, inline=bounded.inline)
    if spec.footer:
        embed.set_footer(text=_cut(spec.footer, EMBED_FOOTER_LIMIT))
    if spec.thumbnail_url:
        embed.set_thumbnail(url=spec.thumbnail_url)
    return embed


def compact_spec(spec: CardSpec) -> CardSpec:
    """Reduce a card to identity plus the decision-critical evidence only."""

    return replace(
        spec,
        description=spec.compact_description or spec.description,
        fields=spec.essential_fields,
        thumbnail_url="",
        footer=spec.footer,
    )


def minimal_spec(spec: CardSpec) -> CardSpec:
    """The last honest thing we can say about this candidate."""

    return replace(
        spec,
        description=_cut(spec.compact_description or spec.description, 900),
        fields=(),
        thumbnail_url="",
        footer="",
    )


def render_message(
    specs: Sequence[CardSpec],
    *,
    budget: int = SAFE_MESSAGE_BUDGET,
) -> tuple[list[discord.Embed], tuple[str, ...]]:
    """Fit any number of cards inside one message budget.

    Returns the embeds plus the human-readable notes describing what had to be
    dropped, so the card can be honest about being trimmed rather than silently
    losing evidence.

    The trimming order is fixed: optional enrichment first (highest priority
    number), then compact cards, then fewer cards.  Identity, the exact mint,
    the decision and safety are the last things standing.
    """

    if not specs:
        return [], ()

    working = [_bounded(spec) for spec in specs[:EMBED_COUNT_LIMIT]]
    notes: list[str] = []
    # The card-count note is rewritten rather than appended, so shrinking from
    # five cards to one reports the outcome, not every step on the way.
    shown_note = (
        f"showing {EMBED_COUNT_LIMIT} of {len(specs)} cards"
        if len(specs) > EMBED_COUNT_LIMIT
        else ""
    )

    # Pass 1: drop optional fields, worst-priority first, across every card.
    embeds = [build_embed(spec) for spec in working]
    if _total(embeds) > budget:
        working, dropped = _trim_by_priority(working, budget)
        if dropped:
            notes.append("optional detail trimmed to fit Discord's message budget")
        embeds = [build_embed(spec) for spec in working]

    # Pass 2: compact every card.
    if _total(embeds) > budget:
        working = [compact_spec(spec) for spec in working]
        embeds = [build_embed(spec) for spec in working]
        notes.append("compact cards")

    # Pass 3: show fewer cards rather than a broken one.  The list is already
    # ranked, so the first card is the one most worth keeping.
    while len(embeds) > 1 and _total(embeds) > budget:
        working = working[:-1]
        embeds = [build_embed(spec) for spec in working]
        shown_note = f"showing {len(embeds)} of {len(specs)} cards"

    # Pass 4: a single card that still will not fit becomes minimal.
    if _total(embeds) > budget and embeds:
        working = [minimal_spec(working[0])]
        embeds = [build_embed(working[0])]
        notes.append("minimal card")

    if shown_note:
        notes.append(shown_note)
    return embeds, tuple(dict.fromkeys(notes))


def _bounded(spec: CardSpec) -> CardSpec:
    return replace(
        spec,
        title=_cut(spec.title, EMBED_TITLE_LIMIT),
        description=_cut(spec.description, EMBED_DESCRIPTION_LIMIT),
        fields=tuple(item.bounded() for item in spec.fields),
        footer=_cut(spec.footer, EMBED_FOOTER_LIMIT),
    )


def _trim_by_priority(
    specs: Sequence[CardSpec],
    budget: int,
) -> tuple[list[CardSpec], bool]:
    """Drop the least important field anywhere until the message fits."""

    working = list(specs)
    dropped = False
    while True:
        embeds = [build_embed(spec) for spec in working]
        if _total(embeds) <= budget:
            return working, dropped
        victim: tuple[int, int, int] | None = None  # (priority, card index, field index)
        for card_index, spec in enumerate(working):
            for field_index, item in enumerate(spec.fields):
                if item.priority < OPTIONAL_PRIORITY_FLOOR:
                    continue
                key = (item.priority, card_index, field_index)
                if victim is None or key[0] > victim[0]:
                    victim = key
        if victim is None:
            return working, dropped
        _, card_index, field_index = victim
        spec = working[card_index]
        working[card_index] = replace(
            spec,
            fields=tuple(
                item for index, item in enumerate(spec.fields) if index != field_index
            ),
        )
        dropped = True


def _total(embeds: Sequence[discord.Embed]) -> int:
    return sum(len(embed) for embed in embeds)


def is_embed_too_large(error: BaseException) -> bool:
    """Detect Discord's specific oversized-embed rejection.

    Matched by error code where discord.py exposes one, and by message text
    otherwise, so a library version that reshapes the exception still routes to
    the emergency fallback instead of stranding the interaction.
    """

    code = getattr(error, "code", None)
    if code == 50035:
        return True
    text = str(error).casefold()
    return "50035" in text or ("embed" in text and "6000" in text)


async def resolve_with_cards(
    interaction: Any,
    specs: Sequence[CardSpec],
    *,
    view: Any = None,
    fallback_text: str,
    budget: int = SAFE_MESSAGE_BUDGET,
) -> bool:
    """Replace a deferred response with the richest cards that actually fit.

    Guarantees the v2.35.1 contract: the interaction always ends as cards, a
    compact card, a minimal card, or visible bounded text — never a permanent
    "Investing is thinking…".  Exactly one emergency retry is attempted, and a
    render failure never fabricates a candidate or triggers a trade.
    """

    embeds, notes = render_message(specs, budget=budget)
    content = f"⚠️ {' • '.join(notes)}" if notes else None
    try:
        await interaction.edit_original_response(content=content, embeds=embeds, view=view)
        return True
    except Exception as error:  # noqa: BLE001 - every failure must still resolve
        if not is_embed_too_large(error):
            logger.exception("Fomo card could not be delivered")
        else:
            logger.warning("Discord refused the budgeted card set: %s", error)

    # Exactly one emergency retry, at the minimal budget, with no view.
    try:
        minimal, _ = render_message(
            [minimal_spec(spec) for spec in specs[:1]],
            budget=MINIMAL_MESSAGE_BUDGET,
        )
        await interaction.edit_original_response(
            content=(
                "⚠️ The full card exceeded Discord's limits; showing the minimum. "
                "Nothing was fabricated and no buy was attempted."
            ),
            embeds=minimal,
            view=None,
        )
        return True
    except Exception:  # noqa: BLE001 - fall through to plain text
        logger.exception("Fomo minimal card was also rejected")

    # Final: plain visible text.  No further retries.
    try:
        await interaction.edit_original_response(content=fallback_text, embeds=[], view=None)
        return True
    except Exception:  # noqa: BLE001 - the interaction itself is gone
        logger.exception("Fomo response could not be resolved at all")
        return False


@dataclass(frozen=True, slots=True)
class RenderBudgetReport:
    """What the renderer had to do to make a message fit; used by tests."""

    embeds: int = 0
    total_characters: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def within_hard_limit(self) -> bool:
        return self.total_characters <= MESSAGE_EMBED_LIMIT


def describe_render(
    specs: Sequence[CardSpec],
    *,
    budget: int = SAFE_MESSAGE_BUDGET,
) -> RenderBudgetReport:
    embeds, notes = render_message(specs, budget=budget)
    return RenderBudgetReport(
        embeds=len(embeds),
        total_characters=_total(embeds),
        notes=notes,
    )
