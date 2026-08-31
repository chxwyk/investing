"""Two-stage fast alerts: publish immediately, enrich in place (sections 22-24).

The architecture the product contract demands is

    DETECT -> PERSIST -> NOTIFY -> ENRICH

not "wait for every provider, then maybe notify three minutes later".  So every
fast alert here is deliberately small and built only from facts already in hand.
Deep forensics, safety, route quality and social confirmation arrive later and
*edit the same message* rather than producing a second ping.

This module also closes the gap the v2.37 report flagged: FAST WATCH was
implemented and tested but had no publication path, so it never reached Discord.
It does now — as research visibility only.  Every card class here carries
``entry_eligible = False`` structurally; nothing in this file can authorise a
PAPER entry, and the PAPER engine's gates are untouched by anything it does.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .discord_render import (
    P_ABOUT,
    P_DECISION,
    P_DEMAND,
    P_DIAGNOSTICS,
    P_EDGE,
    P_IDENTITY,
    P_LINKS,
    P_LIQUIDITY,
    P_MOMENTUM,
    P_SAFETY,
    P_SMART_MONEY,
    P_SOCIAL,
    P_WARNINGS,
    P_WHY_SURFACED,
    CardField,
    CardSpec,
)
from .trenches.publicmodel import MODEL_CAVEAT as PUBLIC_MODEL_CAVEAT

logger = logging.getLogger(__name__)

ZERO = Decimal("0")

# --- alert classes (section 29) ----------------------------------------------
FAST_WATCH = "FAST_WATCH"
NOTABLE_TRADER_EARLY = "NOTABLE_TRADER_EARLY"
NOTABLE_TRADER_LATE = "NOTABLE_TRADER_LATE"
NOTABLE_DISTRIBUTION = "NOTABLE_DISTRIBUTION"
BREAKING_CATALYST = "BREAKING_CATALYST"
CATALYST_WATCH = "CATALYST_WATCH"
CONFLUENCE_WATCH = "CONFLUENCE_WATCH"
EARLY_HEADS_UP = "EARLY_HEADS_UP"
EARLY_RUNNER = "EARLY_RUNNER"
SHADOW_ENTRY = "SHADOW_AUTO_ENTRY"
SHADOW_EXIT = "SHADOW_AUTO_EXIT"
# --- Trending-first classes (v2.42) ------------------------------------------
# The primary universe is now Fomo Trending, so it gets its own card classes
# rather than being squeezed into the graduated-runner ones.  TRENDING_HOT_WATCH
# is deliberately absent from PINGABLE: a hot watch is a promise to look again
# soon, not an interruption (section 44).
TRENDING_ALPHA = "TRENDING_ALPHA"
TRENDING_ACCELERATION_ALERT = "TRENDING_ACCELERATION"
TRENDING_CONTINUATION_ALERT = "TRENDING_CONTINUATION"
TRENDING_HOT_WATCH = "TRENDING_HOT_WATCH"
OFF_TRENDING_EXCEPTION = "OFF_TRENDING_EXCEPTION"
# --- Trenches classes (v2.43) ------------------------------------------------
# Pre-graduation candidates get their own classes: a curve token and a Trending
# token are different objects with different evidence, and squeezing one into
# the other's card is how a bonding percentage ends up rendered as a rank.
TRENCH_RUNNER_ALERT = "PUMP_TRENCH_RUNNER"
TRENCH_HEADS_UP_ALERT = "PUMP_TRENCH_HEADS_UP"
ALMOST_BONDED_ALERT = "ALMOST_BONDED_MOMENTUM"
PUBLIC_TRENDING_ALERT = "PUBLIC_TRENDING"

ALERT_CLASSES: tuple[str, ...] = (
    FAST_WATCH,
    NOTABLE_TRADER_EARLY,
    NOTABLE_TRADER_LATE,
    NOTABLE_DISTRIBUTION,
    BREAKING_CATALYST,
    CATALYST_WATCH,
    CONFLUENCE_WATCH,
    EARLY_HEADS_UP,
    EARLY_RUNNER,
    SHADOW_ENTRY,
    SHADOW_EXIT,
    TRENDING_ALPHA,
    TRENDING_ACCELERATION_ALERT,
    TRENDING_CONTINUATION_ALERT,
    TRENDING_HOT_WATCH,
    OFF_TRENDING_EXCEPTION,
    TRENCH_RUNNER_ALERT,
    TRENCH_HEADS_UP_ALERT,
    ALMOST_BONDED_ALERT,
    PUBLIC_TRENDING_ALERT,
)

#: Classes that may interrupt the user.  A late observation never does — it is
#: published, clearly marked, and left for the user to read on their own time.
#: A simulated shadow fill never does either: it is a record, not news.
#: EARLY_RUNNER earns a ping because it is the whole point of the release: a
#: token the bot saw at $31K has to reach the operator while it is still near
#: $31K.  EARLY_HEADS_UP deliberately does not — it is the quiet "watch this"
#: tier, and it publishes to the radar instead.
PINGABLE: frozenset[str] = frozenset(
    {
        NOTABLE_TRADER_EARLY,
        BREAKING_CATALYST,
        CONFLUENCE_WATCH,
        EARLY_RUNNER,
        TRENDING_ALPHA,
        TRENDING_ACCELERATION_ALERT,
        TRENDING_CONTINUATION_ALERT,
        OFF_TRENDING_EXCEPTION,
        # v2.43: the pre-graduation lane is primary too, so its serious classes
        # earn an interruption.  The heads-up tier deliberately does not.
        TRENCH_RUNNER_ALERT,
        ALMOST_BONDED_ALERT,
        PUBLIC_TRENDING_ALERT,
    }
)

# --- the two visibility layers (sections 27-29) ------------------------------
#: Everything worth reading goes somewhere.  LANE_RADAR is the quiet feed a user
#: can scan on their own time; LANE_URGENT is the small set that has earned an
#: interruption.  Nothing is dropped merely because it did not earn a ping —
#: that is exactly the "either everything pings me or I never see it" problem
#: section 29 forbids.
LANE_RADAR = "RADAR"
LANE_URGENT = "URGENT"

LANES: tuple[str, ...] = (LANE_RADAR, LANE_URGENT)

#: Classes that belong in the urgent lane when they fire at all.
URGENT_CLASSES: frozenset[str] = frozenset(
    {
        NOTABLE_TRADER_EARLY,
        BREAKING_CATALYST,
        CONFLUENCE_WATCH,
        CATALYST_WATCH,
        EARLY_RUNNER,
        # Trending is the primary universe (section 59), so its serious classes
        # ride the urgent lane.  TRENDING_HOT_WATCH stays on the radar lane: it
        # is a promise to look again soon, not an interruption.
        TRENDING_ALPHA,
        TRENDING_ACCELERATION_ALERT,
        TRENDING_CONTINUATION_ALERT,
        OFF_TRENDING_EXCEPTION,
        TRENCH_RUNNER_ALERT,
        ALMOST_BONDED_ALERT,
        PUBLIC_TRENDING_ALERT,
    }
)

RESEARCH_ONLY_FOOTER = "⚠ RESEARCH ONLY — NOT ENTRY ELIGIBLE • deep validation still running"
SHADOW_FOOTER = "🧪 SIMULATION ONLY — REAL MONEY $0.00 • no wallet, no signer, no swap"


def _family_field(family: str, why: Sequence[str]) -> CardField:
    """Section 30: every alert states its family and why it is being shown."""

    lines = [f"**{family.replace('_', ' ')}**"]
    lines.extend(f"• {item}" for item in list(why)[:5])
    return CardField("WHY YOU'RE SEEING THIS", "\n".join(lines), P_WHY_SURFACED)


@dataclass(frozen=True, slots=True)
class FastAlert:
    """One publishable fast alert.  Never entry eligible, by construction."""

    kind: str
    mint: str
    alert_key: str
    spec: CardSpec
    ping: bool = False
    ping_reason: str = ""
    fingerprint: str = ""
    stage: int = 1
    #: The Solana mint this card is about, when it is about one at all.  An
    #: event-only catalyst card has none, and must not be given token links.
    token_mint: str = ""
    #: Which visibility layer this card belongs to (sections 27-29).
    lane: str = LANE_RADAR
    #: The shadow signal family, when this card came from one.
    family: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ALERT_CLASSES:
            raise ValueError(f"unknown fast alert class: {self.kind}")
        if self.lane not in LANES:
            raise ValueError(f"unknown fast alert lane: {self.lane}")

    @property
    def entry_eligible(self) -> bool:
        """Structural guarantee shared by every fast alert."""

        return False

    @property
    def may_ping(self) -> bool:
        return self.ping and self.kind in PINGABLE


def _money(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    amount = Decimal(value)
    for threshold, suffix in (
        (Decimal("1e9"), "B"),
        (Decimal("1e6"), "M"),
        (Decimal("1e3"), "K"),
    ):
        if abs(amount) >= threshold:
            return f"${amount / threshold:.2f}{suffix}"
    return f"${amount:.2f}"


def _percent(value: Decimal | None) -> str:
    return "unknown" if value is None else f"{value:+.2f}%"


def _percent_plain(value: Decimal | None) -> str:
    """A share, not a change: no leading sign."""

    return "unknown" if value is None else f"{value:.1f}%"


def _links(mint: str, fomo_url: str) -> str:
    return (
        f"[FOMO]({fomo_url}) • [PUMP.FUN](https://pump.fun/coin/{mint}) • "
        f"[DEX](https://dexscreener.com/solana/{mint}) • "
        f"[SOLSCAN](https://solscan.io/token/{mint})"
    )


def build_fast_watch_alert(
    *,
    mint: str,
    name: str,
    symbol: str,
    fomo_url: str,
    verdict: Any,
    age_seconds: int | None,
    market_cap_usd: Decimal | None,
    first_seen_market_cap_usd: Decimal | None,
    liquidity_usd: Decimal | None,
    move_since_first_seen_percent: Decimal | None,
    momentum_score: Decimal | None,
    organic_score: Decimal | None,
    buys: int | None,
    sells: int | None,
    actionability: Any = None,
    image_url: str = "",
    now: int = 0,
) -> FastAlert:
    """The compact FAST WATCH card (section 51).

    Deliberately small: this is the early-acceleration card, not the forensic
    one.  It states plainly what evidence it did not wait for.
    """

    pending = ", ".join(getattr(verdict, "pending_evidence", ()) or ()) or "none"
    reasons = tuple(getattr(verdict, "reasons", ()) or ())
    blockers = tuple(getattr(verdict, "blockers", ()) or ())
    action_label = getattr(actionability, "label", "") if actionability is not None else ""

    fields = [
        CardField(
            "SETUP",
            (
                f"Age `{age_seconds if age_seconds is not None else 'unknown'}s` • MC "
                f"`{_money(market_cap_usd)}` • first-seen MC "
                f"`{_money(first_seen_market_cap_usd)}`\n"
                f"Move since first seen `{_percent(move_since_first_seen_percent)}` • "
                f"liquidity `{_money(liquidity_usd)}`"
            ),
            P_IDENTITY,
        ),
        CardField(
            "EARLY SIGNAL",
            (
                f"Watch score `{getattr(verdict, 'score', ZERO):.0f}/100` • momentum "
                f"`{momentum_score if momentum_score is not None else 'pending'}` • organic "
                f"`{organic_score if organic_score is not None else 'pending'}`\n"
                f"Flow `{buys if buys is not None else '?'}` buys / "
                f"`{sells if sells is not None else '?'}` sells"
                + (f"\nCurrent state **{action_label}**" if action_label else "")
            ),
            P_DEMAND,
        ),
        _family_field(FAST_WATCH, reasons or ("early acceleration",)),
        CardField(
            "SAFETY",
            f"**UNKNOWN / pending**\nMissing: {pending}",
            P_SAFETY,
        ),
        CardField("LINKS", _links(mint, fomo_url), P_LIQUIDITY),
    ]
    if blockers:
        fields.append(
            CardField(
                "WARNINGS",
                "\n".join(f"• {item}" for item in blockers[:4]),
                P_WARNINGS,
            )
        )

    spec = CardSpec(
        title="🔥 WATCH — HEATING UP",
        description=(
            f"**{name}** `${symbol}`\n`{mint}`\n"
            f"MC `{_money(market_cap_usd)}` • age "
            f"`{age_seconds if age_seconds is not None else '?'}s`\n{_links(mint, fomo_url)}"
        ),
        compact_description=(
            f"🔥 WATCH **{name}** `${symbol}`\n`{mint}`\n"
            f"MC `{_money(market_cap_usd)}` • safety UNKNOWN • RESEARCH ONLY"
        ),
        fields=tuple(fields),
        footer=RESEARCH_ONLY_FOOTER,
        thumbnail_url=image_url,
        colour=0xE67E22,
    )
    return FastAlert(
        kind=FAST_WATCH,
        mint=mint,
        alert_key=f"{FAST_WATCH}:{mint}",
        spec=spec,
        ping=False,
        fingerprint=f"watch-{int(getattr(verdict, 'score', ZERO))}",
        token_mint=mint,
        lane=LANE_RADAR,
        family=FAST_WATCH,
    )


def build_notable_trader_alert(
    *,
    signal: Any,
    fomo_url: str,
    name: str,
    symbol: str,
    consensus: Any = None,
    ping_decision: Any = None,
    catalyst_note: str = "checking",
    image_url: str = "",
) -> FastAlert:
    """The small, fast notable-wallet card (sections 6, 7, 8).

    Entry market cap versus current market cap is mandatory and always present,
    and a late observation is published with its lateness quantified rather than
    hidden.
    """

    trade = signal.trade
    late = not signal.may_chase()
    kind = NOTABLE_TRADER_LATE if late else NOTABLE_TRADER_EARLY
    title = (
        "🐋 NOTABLE TRADER BUY — LATE OBSERVATION"
        if late
        else "🐋 NOTABLE TRADER BUY — EARLY DATA"
    )
    delay = trade.detection_delay_seconds
    age = signal.signal_age_seconds
    age_text = f"{age}s" if age is not None else "unknown"

    fields = [
        CardField(
            "TRADE",
            (
                f"Trader **{signal.display_name}** • reputation "
                f"`{signal.reputation_state}`\n"
                f"Bought `{_money(trade.amount_usd)}`"
                + (f" • observed `{delay}s` after the chain event" if delay is not None else "")
            ),
            P_IDENTITY,
        ),
        CardField(
            "ENTRY vs NOW",
            (
                f"Trader entry MC `{_money(trade.entry_market_cap_usd)}`\n"
                f"Bot detection MC `{_money(signal.detection_market_cap_usd)}`\n"
                f"Current MC `{_money(signal.current_market_cap_usd)}`\n"
                f"Move since trader entry `{_percent(signal.move_since_trader_entry_percent)}` • "
                f"since detection `{_percent(signal.move_since_detection_percent)}`"
            ),
            P_DECISION,
        ),
        CardField(
            "STATUS",
            (
                f"Freshness **{signal.freshness()}** • signal age `{age_text}`\n"
                f"Safety **UNKNOWN** • independent wallet: pending\n"
                f"Catalyst: {catalyst_note}"
            ),
            P_SAFETY,
        ),
        CardField("LINKS", _links(trade.mint, fomo_url), P_LIQUIDITY),
    ]

    if consensus is not None and consensus.raw_wallets > 1:
        fields.append(
            CardField(
                "CONSENSUS",
                (
                    f"Raw notable wallets `{consensus.raw_wallets}` • independent "
                    f"`{consensus.independent_wallets}` • clusters "
                    f"`{consensus.funding_clusters}`\n"
                    f"Earliest entry `{_money(consensus.earliest_entry_market_cap_usd)}` • "
                    f"median `{_money(consensus.median_entry_market_cap_usd)}`"
                    + (
                        "\n" + "\n".join(f"⚠ {item}" for item in consensus.warnings)
                        if consensus.warnings
                        else ""
                    )
                ),
                P_EDGE,
            )
        )

    why = [
        f"{signal.display_name} ({signal.reputation_state}) bought {_money(trade.amount_usd)}",
        f"observed {age_text} after the signal • {signal.freshness()}",
    ]
    if consensus is not None and consensus.independent_wallets > 1:
        why.append(f"{consensus.independent_wallets} independent notable wallets agree")
    fields.append(_family_field(kind, why))

    warnings = signal.warnings()
    if warnings:
        fields.append(CardField("WARNINGS", "\n".join(warnings), P_WARNINGS))

    reason = getattr(ping_decision, "label", "") if ping_decision is not None else ""
    spec = CardSpec(
        title=title,
        description=(
            f"**{name}** `${symbol}`\n`{trade.mint}`\n"
            + (f"{reason}\n" if reason else "")
            + _links(trade.mint, fomo_url)
        ),
        compact_description=(
            f"🐋 **{name}** `${symbol}` — {signal.display_name} "
            f"({signal.reputation_state})\n`{trade.mint}`\n"
            f"Entry `{_money(trade.entry_market_cap_usd)}` → now "
            f"`{_money(signal.current_market_cap_usd)}` "
            f"({_percent(signal.move_since_trader_entry_percent)}) • {signal.freshness()}"
        ),
        fields=tuple(fields),
        footer=RESEARCH_ONLY_FOOTER,
        thumbnail_url=image_url,
        colour=0x95A5A6 if late else 0x2ECC71,
    )
    return FastAlert(
        kind=kind,
        mint=trade.mint,
        alert_key=f"{kind}:{trade.mint}:{trade.signature}:{trade.wallet}",
        spec=spec,
        ping=bool(getattr(ping_decision, "ping", False)) and not late,
        ping_reason=getattr(ping_decision, "reason", ""),
        fingerprint=signal.freshness(),
        token_mint=trade.mint,
        # A late observation is published quietly rather than hidden: it goes to
        # the live radar, where it can still be useful research.
        lane=LANE_RADAR if late else LANE_URGENT,
        family=kind,
    )


def build_catalyst_alert(
    *,
    alert: Any,
    event: Any,
    link: Any = None,
    mint: str = "",
    name: str = "",
    symbol: str = "",
    fomo_url: str = "",
    token_age_seconds: int | None = None,
    market_cap_usd: Decimal | None = None,
    liquidity_usd: Decimal | None = None,
    notable_summary: str = "",
) -> FastAlert:
    """BREAKING CATALYST / CATALYST WATCH / CONFLUENCE WATCH (sections 17-21).

    The event is the subject; any token is secondary and always labelled with
    its own, separate connection confidence.
    """

    kind = {
        "BREAKING_CATALYST": BREAKING_CATALYST,
        "CATALYST_WATCH": CATALYST_WATCH,
        "CONFLUENCE_WATCH": CONFLUENCE_WATCH,
    }.get(alert.kind, CATALYST_WATCH)
    title = {
        BREAKING_CATALYST: "🚨 BREAKING CATALYST",
        CATALYST_WATCH: "⚡ CATALYST WATCH",
        CONFLUENCE_WATCH: "🔥 CONFLUENCE WATCH",
    }[kind]

    primary = next((item.name for item in event.sources if item.is_primary), "none")
    fields = [
        CardField(
            "EVENT",
            (
                f"{event.headline}\n"
                f"Confidence **{event.confidence}** • priority **{event.priority}**\n"
                f"Primary source `{primary}` • independent confirmation "
                f"`{event.independent_confirmations}`"
                + (
                    f"\nDetected `{event.age_seconds_at}s` after the event"
                    if event.age_seconds_at is not None
                    else ""
                )
            ),
            P_IDENTITY,
        )
    ]
    if event.markers:
        fields.append(
            CardField(
                "EVENT INTEGRITY",
                "\n".join(f"• {item.replace('_', ' ').lower()}" for item in event.markers),
                P_WARNINGS,
            )
        )
    if mint:
        fields.append(
            CardField(
                "RELATED FRESH TOKEN",
                (
                    f"**{name}** `${symbol}`\n`{mint}`\n"
                    f"Age `{token_age_seconds if token_age_seconds is not None else '?'}s` • MC "
                    f"`{_money(market_cap_usd)}` • liquidity `{_money(liquidity_usd)}`\n"
                    f"Token ↔ event: **{link.label if link is not None else 'NO EVIDENCE'}**"
                    + (f"\n{notable_summary}" if notable_summary else "")
                ),
                P_DECISION,
            )
        )
        fields.append(CardField("LINKS", _links(mint, fomo_url), P_LIQUIDITY))
    if alert.reasons:
        fields.append(
            CardField(
                "WHY THIS IS URGENT" if kind == CONFLUENCE_WATCH else "WHY SURFACED",
                "\n".join(f"• {item}" for item in alert.reasons),
                P_WHY_SURFACED,
            )
        )
    fields.append(
        CardField("SAFETY", "**UNKNOWN / pending** — research only", P_SAFETY)
    )
    fields.append(
        _family_field(
            kind,
            tuple(alert.reasons)
            or (f"{event.confidence} confidence external event detected",),
        )
    )
    if alert.warnings:
        fields.append(CardField("WARNINGS", "\n".join(alert.warnings), P_WARNINGS))

    compact = f"{title}\n{event.headline}\nEvent **{event.confidence}**"
    if mint:
        compact += (
            f"\n**{name}** `${symbol}` `{mint}`\n"
            f"Token ↔ event: {link.label if link is not None else 'NO EVIDENCE'}"
        )
    spec = CardSpec(
        title=title,
        description=(
            f"**{event.headline}**\n"
            f"Event confidence **{event.confidence}** • priority **{event.priority}**"
            + (f"\n{_links(mint, fomo_url)}" if mint else "")
        ),
        compact_description=compact,
        fields=tuple(fields),
        footer="⚠ EVENT VERIFIED ≠ TOKEN VERIFIED — research only, never an entry",
        colour={
            BREAKING_CATALYST: 0xE74C3C,
            CATALYST_WATCH: 0xF1C40F,
            CONFLUENCE_WATCH: 0x9B59B6,
        }[kind],
    )
    urgent = kind in {BREAKING_CATALYST, CONFLUENCE_WATCH} or (
        # A high-quality CATALYST WATCH earns the urgent lane even though it
        # does not earn an @ mention (section 27B).
        kind == CATALYST_WATCH
        and str(event.confidence) in {"CONFIRMED", "HIGH"}
        and event.independent_confirmations >= 2
    )
    return FastAlert(
        kind=kind,
        mint=mint or event.event_id,
        alert_key=f"{kind}:{event.event_id}:{mint}" if mint else f"{kind}:{event.event_id}",
        spec=spec,
        ping=bool(getattr(alert, "ping", False)),
        ping_reason=getattr(alert, "ping_reason", ""),
        fingerprint=f"{event.confidence}:{event.priority}",
        token_mint=mint,
        lane=LANE_URGENT if urgent else LANE_RADAR,
        family=kind,
    )


def build_early_alert(
    *,
    mint: str,
    name: str,
    symbol: str,
    fomo_url: str,
    verdict: Any,
    age_seconds: int | None,
    first_seen_seconds_ago: int | None,
    first_seen_market_cap_usd: Decimal | None,
    alert_market_cap_usd: Decimal | None,
    current_market_cap_usd: Decimal | None,
    liquidity_usd: Decimal | None,
    buys: int | None,
    sells: int | None,
    story_summary: str = "",
    notable_summary: str = "",
    image_url: str = "",
) -> FastAlert:
    """The EARLY HEADS-UP / EARLY RUNNER card (sections 45, 46).

    Short on purpose.  The operator has to understand it in one glance, and the
    exact mint has to be trivially copyable, so identity and the money numbers
    come first and everything else is one line each.

    A card that arrives after the move says so in its own title.  Printing
    "first seen $31.2K" beside a doubled price without calling it late is the
    exact failure this release exists to fix (sections 10, 47).
    """

    tier = str(getattr(verdict, "tier", "EARLY_HEADS_UP"))
    late = bool(getattr(verdict, "late", False))
    kind = EARLY_RUNNER if tier in {"EARLY_RUNNER", "ORGANIC_RUNNER"} else EARLY_HEADS_UP
    move_before = _percent_or(_move_percent(first_seen_market_cap_usd, alert_market_cap_usd))
    move_after = _percent_or(_move_percent(alert_market_cap_usd, current_market_cap_usd))

    fields = [
        CardField(
            "MARKET CAP",
            (
                f"First seen `{_money(first_seen_market_cap_usd)}`"
                + (
                    f" ({first_seen_seconds_ago}s ago)"
                    if first_seen_seconds_ago is not None
                    else ""
                )
                + f"\n**At this alert `{_money(alert_market_cap_usd)}`**\n"
                f"Current `{_money(current_market_cap_usd)}`\n"
                f"Move before alert `{move_before}` • since alert `{move_after}`"
            ),
            P_DECISION,
        ),
        CardField(
            "MARKET",
            (
                f"Age `{age_seconds if age_seconds is not None else '?'}s` • liquidity "
                f"`{_money(liquidity_usd)}`\n"
                f"Flow `{buys if buys is not None else '?'}` buys / "
                f"`{sells if sells is not None else '?'}` sells • signal "
                f"`{getattr(verdict, 'score', ZERO):.0f}/100`"
                + (f"\n{notable_summary}" if notable_summary else "")
                + (f"\nStory: {story_summary}" if story_summary else "\nStory: NONE FOUND")
            ),
            P_DEMAND,
        ),
    ]

    impulse = getattr(verdict, "impulse", None)
    if impulse is not None and getattr(impulse, "detected", False):
        fields.append(
            CardField(
                "LARGE BUY",
                (
                    f"Quality `{impulse.quality}`"
                    + (
                        f" • {impulse.liquidity_share_percent}% of liquidity"
                        if impulse.liquidity_share_percent is not None
                        else ""
                    )
                    + (
                        f" • {impulse.follow_on_buyers} independent buyers followed"
                        if impulse.follow_on_buyers
                        else ""
                    )
                ),
                P_EDGE,
            )
        )

    categories = tuple(getattr(verdict, "evidence_categories", ()) or ())
    reasons = tuple(getattr(verdict, "reasons", ()) or ())
    fields.append(
        CardField(
            "WHY PINGED" if kind == EARLY_RUNNER and not late else "WHY YOU'RE SEEING THIS",
            (
                "\n".join(f"• {item}" for item in reasons[:5])
                or "• early acceleration on cheap evidence"
            )
            + (
                "\nEvidence: " + ", ".join(item.replace("_", " ").lower() for item in categories)
                if categories
                else ""
            ),
            P_WHY_SURFACED,
        )
    )

    if late:
        why_not = tuple(getattr(verdict, "why_not_pinged", ()) or ())
        fields.append(
            CardField(
                "⚠ WHY THIS IS NOT AN EARLY ALERT",
                (
                    f"The move was already `{move_before}` by the time this fired.\n"
                    + (
                        "\n".join(f"• {item.replace('_', ' ').lower()}" for item in why_not[:4])
                        or "• the gate was reached after the move"
                    )
                ),
                P_WARNINGS,
            )
        )

    fields.append(
        CardField(
            "SAFETY",
            "**UNKNOWN** — deep analysis still running. Research only.",
            P_SAFETY,
        )
    )
    fields.append(CardField("LINKS", _links(mint, fomo_url), P_LIQUIDITY))

    title = str(getattr(verdict, "label", "👀 EARLY HEADS-UP"))
    spec = CardSpec(
        title=title,
        description=(
            f"**{name}** `${symbol}`\n"
            f"Mint: `{mint}`\n"
            f"MC now `{_money(alert_market_cap_usd)}` • first seen "
            f"`{_money(first_seen_market_cap_usd)}` ({move_before})\n"
            f"{_links(mint, fomo_url)}"
        ),
        compact_description=(
            f"{title}\n**{name}** `${symbol}`\n`{mint}`\n"
            f"Alert MC `{_money(alert_market_cap_usd)}` • first seen "
            f"`{_money(first_seen_market_cap_usd)}` ({move_before})"
        ),
        fields=tuple(fields),
        footer=RESEARCH_ONLY_FOOTER,
        thumbnail_url=image_url,
        colour=0x95A5A6 if late else (0xE74C3C if kind == EARLY_RUNNER else 0xF39C12),
    )
    return FastAlert(
        kind=kind,
        mint=mint,
        alert_key=f"{kind}:{mint}",
        spec=spec,
        # A late card never pings, whatever tier the evidence reached.
        ping=kind == EARLY_RUNNER and not late,
        ping_reason=", ".join(categories[:2]),
        fingerprint=f"{tier}:{int(getattr(verdict, 'score', ZERO))}",
        token_mint=mint,
        lane=LANE_URGENT if (kind == EARLY_RUNNER and not late) else LANE_RADAR,
        family=tier,
    )


def _move_percent(base: Decimal | None, current: Decimal | None) -> Decimal | None:
    if base is None or current is None or base <= 0:
        return None
    return ((current - base) / base * Decimal("100")).quantize(Decimal("0.01"))


def _percent_or(value: Decimal | None) -> str:
    return "unknown" if value is None else f"{value:+.2f}%"


def build_shadow_entry_alert(
    *,
    mint: str,
    name: str,
    symbol: str,
    fomo_url: str,
    family: str,
    family_label: str,
    why: Sequence[str],
    size_usd: Decimal,
    fill_market_cap_usd: Decimal | None,
    fill_price_usd: Decimal | None,
    venue: str,
    fill_source: str,
    graduation_state: str,
    modeled_cost_usd: Decimal,
    net_objective_usd: Decimal,
    signal_to_fill_seconds: int | None = None,
    image_url: str = "",
    position_id: str = "",
) -> FastAlert:
    """🧪 SHADOW AUTO-ENTRY (section 31).

    States the real money figure explicitly, every time, because a card that
    looks like a trade must never be mistaken for one.
    """

    fill_note = {
        "EXECUTABLE_QUOTE": "executable quote",
        "SIMULATED_VENUE_STATE": "simulated from live venue state",
        "FALLBACK_PENALISED": "⚠ penalised fallback price — not an executable fill",
    }.get(fill_source, fill_source or "unknown")

    fields = [
        CardField(
            "SIMULATED BUY",
            (
                f"Amount `${size_usd:.2f}` • fill MC `{_money(fill_market_cap_usd)}`\n"
                f"Fill price `{fill_price_usd if fill_price_usd is not None else 'unknown'}`\n"
                f"Modeled costs `-${modeled_cost_usd:.4f}`"
            ),
            P_DECISION,
        ),
        CardField(
            "ROUTE",
            (
                f"Venue `{venue}` • {fill_note}\n"
                f"Pump state `{graduation_state}`"
                + (
                    f"\nSignal → simulated fill `{signal_to_fill_seconds}s`"
                    if signal_to_fill_seconds is not None
                    else ""
                )
            ),
            P_LIQUIDITY,
        ),
        CardField(
            "MANAGEMENT",
            (
                f"NET meaningful-profit objective `+${net_objective_usd:.2f}`\n"
                "Runner management **ACTIVE** — a healthy runner is not dumped at "
                "the objective\n"
                "**REAL MONEY: $0.00**"
            ),
            P_EDGE,
        ),
        _family_field(family_label or family, why),
        CardField("LINKS", _links(mint, fomo_url), P_LIQUIDITY),
    ]

    spec = CardSpec(
        title="🧪 SHADOW AUTO-ENTRY",
        description=(
            f"**{name}** `${symbol}`\n`{mint}`\n"
            f"Signal **{family_label or family}** • simulated buy `${size_usd:.2f}`\n"
            f"{_links(mint, fomo_url)}"
        ),
        compact_description=(
            f"🧪 SHADOW BUY `${size_usd:.2f}` **{name}** `${symbol}`\n`{mint}`\n"
            f"{family_label or family} • fill MC `{_money(fill_market_cap_usd)}` • "
            "REAL MONEY $0.00"
        ),
        fields=tuple(fields),
        footer=SHADOW_FOOTER,
        thumbnail_url=image_url,
        colour=0x1ABC9C,
    )
    return FastAlert(
        kind=SHADOW_ENTRY,
        mint=mint,
        alert_key=f"{SHADOW_ENTRY}:{position_id or mint}",
        spec=spec,
        ping=False,
        fingerprint=f"shadow-entry-{family}",
        token_mint=mint,
        lane=LANE_RADAR,
        family=family,
    )


def build_shadow_exit_alert(
    *,
    mint: str,
    name: str,
    symbol: str,
    fomo_url: str,
    family: str,
    family_label: str,
    size_usd: Decimal,
    entry_market_cap_usd: Decimal | None,
    exit_market_cap_usd: Decimal | None,
    gross_pnl_usd: Decimal,
    cost_usd: Decimal,
    net_pnl_usd: Decimal,
    peak_net_pnl_usd: Decimal,
    given_back_usd: Decimal,
    exit_reason: str,
    venue: str,
    fraction_sold: Decimal,
    final: bool,
    remaining_fraction: Decimal = ZERO,
    why: Sequence[str] = (),
    image_url: str = "",
    position_id: str = "",
    sequence: int = 1,
) -> FastAlert:
    """🧪 SHADOW AUTO-EXIT (section 32).

    Publishes the loss as plainly as the win: peak NET and profit given back are
    always shown, so a slow exit is visible rather than averaged away.
    """

    verdict = "PARTIAL" if not final else "CLOSED"
    fields = [
        CardField(
            "RESULT",
            (
                f"Simulated investment `${size_usd:.2f}`\n"
                f"Entry MC `{_money(entry_market_cap_usd)}` → exit MC "
                f"`{_money(exit_market_cap_usd)}`\n"
                f"Gross `${gross_pnl_usd:+.4f}` • costs `-${cost_usd:.4f}` • "
                f"**NET `${net_pnl_usd:+.4f}`**"
            ),
            P_DECISION,
        ),
        CardField(
            "PEAK vs FINAL",
            (
                f"Peak NET `${peak_net_pnl_usd:+.4f}`\n"
                f"Profit given back `${given_back_usd:.4f}`"
            ),
            P_EDGE,
        ),
        CardField(
            "EXIT",
            (
                f"Reason `{exit_reason}` • {verdict}\n"
                f"Sold `{fraction_sold * 100:.0f}%` of the remainder • still holding "
                f"`{remaining_fraction * 100:.0f}%`\n"
                f"Venue `{venue}`\n"
                "**REAL MONEY: $0.00**"
            ),
            P_LIQUIDITY,
        ),
        _family_field(family_label or family, why or (f"exit reason: {exit_reason}",)),
        CardField("LINKS", _links(mint, fomo_url), P_LIQUIDITY),
    ]

    spec = CardSpec(
        title="🧪 SHADOW AUTO-EXIT",
        description=(
            f"**{name}** `${symbol}`\n`{mint}`\n"
            f"Signal **{family_label or family}** • NET `${net_pnl_usd:+.4f}`\n"
            f"{_links(mint, fomo_url)}"
        ),
        compact_description=(
            f"🧪 SHADOW SELL **{name}** `${symbol}`\n`{mint}`\n"
            f"NET `${net_pnl_usd:+.4f}` • peak `${peak_net_pnl_usd:+.4f}` • "
            f"{exit_reason} • REAL MONEY $0.00"
        ),
        fields=tuple(fields),
        footer=SHADOW_FOOTER,
        thumbnail_url=image_url,
        colour=0x27AE60 if net_pnl_usd >= 0 else 0xC0392B,
    )
    return FastAlert(
        kind=SHADOW_EXIT,
        mint=mint,
        alert_key=f"{SHADOW_EXIT}:{position_id or mint}:{sequence}",
        spec=spec,
        ping=False,
        fingerprint=f"shadow-exit-{exit_reason}",
        token_mint=mint,
        lane=LANE_RADAR,
        family=family,
    )


@dataclass(frozen=True, slots=True)
class EnrichmentUpdate:
    """Stage-2 evidence that edits the original card rather than re-pinging."""

    alert_key: str
    fields: tuple[CardField, ...] = field(default_factory=tuple)
    footer: str = ""
    replace_fields: bool = False

    def apply(self, spec: CardSpec) -> CardSpec:
        from dataclasses import replace as _replace

        if self.replace_fields:
            merged = self.fields
        else:
            by_name = {item.name: item for item in spec.fields}
            for item in self.fields:
                by_name[item.name] = item
            merged = tuple(by_name.values())
        return _replace(spec, fields=merged, footer=self.footer or spec.footer)


def enrichment_from_evidence(
    *,
    alert_key: str,
    safety_status: str | None = None,
    route_status: str | None = None,
    independent_wallets: int | None = None,
    catalyst: str | None = None,
    expected_net_edge_percent: Decimal | None = None,
    cost_percent: Decimal | None = None,
    provider_degraded: str = "",
) -> EnrichmentUpdate:
    """Turn arriving stage-2 evidence into an in-place card update.

    A degraded provider becomes an explicit ``UNKNOWN — provider unavailable``,
    never a pass, and never a reason to drop the alert that already published.
    """

    fields: list[CardField] = []
    if safety_status is not None or route_status is not None or provider_degraded:
        safety = safety_status or "UNKNOWN"
        if provider_degraded:
            safety = f"UNKNOWN — {provider_degraded} unavailable"
        fields.append(
            CardField(
                "SAFETY",
                f"**{safety}**"
                + (f" • route `{route_status}`" if route_status else "")
                + "\nResearch only — safety never becomes PASS by omission",
                P_SAFETY,
            )
        )
    if independent_wallets is not None:
        fields.append(
            CardField(
                "INDEPENDENCE",
                f"Independent notable wallets `{independent_wallets}`",
                P_DEMAND,
            )
        )
    if catalyst:
        fields.append(CardField("CATALYST", catalyst, P_WHY_SURFACED))
    if expected_net_edge_percent is not None or cost_percent is not None:
        fields.append(
            CardField(
                "ECONOMICS",
                (
                    f"Expected NET edge `{_percent(expected_net_edge_percent)}` • "
                    f"round-trip cost `{cost_percent if cost_percent is not None else '?'}%`"
                ),
                P_EDGE,
            )
        )
    return EnrichmentUpdate(alert_key=alert_key, fields=tuple(fields))


def dedupe_alerts(alerts: Sequence[FastAlert]) -> tuple[FastAlert, ...]:
    """One alert per key; the first (highest-priority) wins."""

    seen: set[str] = set()
    unique: list[FastAlert] = []
    for alert in alerts:
        if alert.alert_key in seen:
            continue
        seen.add(alert.alert_key)
        unique.append(alert)
    return tuple(unique)


# --- Trending-first cards (v2.42, sections 10, 12, 60) -----------------------
def build_trending_alert(
    *,
    mint: str,
    name: str,
    symbol: str,
    fomo_url: str,
    kind: str,
    entry: Any,
    event: Any,
    score: Any,
    holders: Any = None,
    risk: Any = None,
    about_summary: str = "",
    project_claim: str = "",
    external_verification: str = "",
    story: str = "",
    thesis_summary: str = "",
    strongest_thesis: str = "",
    social_summary: str = "",
    notable_wallets: int = 0,
    collision_warning: str = "",
    source_caveat: str = "",
    market_cap_velocity: Decimal | None = None,
    promoted_from_hot_watch: bool = False,
    image_url: str = "",
    now: int = 0,
) -> FastAlert:
    """The FOMO TRENDING operator card (section 60).

    Every claim on it is separated from its corroboration: the About text is a
    claim, external verification is a fact, a thesis is an opinion, and the
    Fomo verification badge is a badge.  The card is allowed to say LOOK NOW.
    It is never allowed to say safe, guaranteed or free money (section 61).
    """

    velocity = getattr(event, "rank_velocity", None)
    rank = getattr(entry, "current_rank", None)
    previous_rank = getattr(velocity, "from_rank", None) if velocity else None
    delta = getattr(velocity, "delta", 0) if velocity else 0
    seconds_trending = entry.seconds_trending(now=now) if hasattr(entry, "seconds_trending") else 0
    move = getattr(event, "move_since_entry_percent", None)

    fields = [
        CardField(
            "TRENDING",
            (
                f"Rank `{'#' + str(rank) if rank else 'unranked'}`"
                + (f" • was `#{previous_rank}`" if previous_rank else "")
                + f" • velocity `{delta:+d}`\n"
                f"Trending since `{seconds_trending}s` • stint `{getattr(entry, 'entries', 1)}` • "
                f"health `{getattr(event, 'label', '')}`"
            ),
            P_DECISION,
        ),
        CardField(
            "MARKET",
            (
                f"First Trending MC `{_money(getattr(entry, 'first_market_cap_usd', None))}` → "
                f"now `{_money(getattr(entry, 'current_market_cap_usd', None))}`\n"
                f"Move since entering Trending `{_percent(move)}`"
                + (
                    f" • acceleration `{market_cap_velocity:+}%/min`"
                    if market_cap_velocity is not None
                    else ""
                )
                + f"\nLiquidity `{_money(getattr(entry, 'liquidity_usd', None))}`"
            ),
            P_LIQUIDITY,
        ),
    ]

    if holders is not None:
        fields.append(
            CardField(
                "HOLDERS",
                (
                    "Count `"
                    + (
                        "unknown"
                        if holders.holder_count is None
                        else str(holders.holder_count)
                    )
                    + f"` • growth `{holders.growth_state}`"
                    + (
                        f" (+{holders.holders_added})"
                        if holders.holders_added is not None
                        else ""
                    )
                    + f"\nTop 10 `{_percent_plain(holders.top10_percent)}` • concentration "
                    f"`{holders.concentration_trend}`"
                ),
                P_DEMAND,
            )
        )

    if about_summary or project_claim:
        fields.append(
            CardField(
                "ABOUT (the project's own claim)",
                (
                    (about_summary or "no description supplied")
                    + (f"\n**Claim:** {project_claim}" if project_claim else "")
                    + (
                        f"\n**External verification:** {external_verification}"
                        if external_verification
                        else "\n**External verification:** UNVERIFIED"
                    )
                ),
                P_ABOUT,
            )
        )

    if story:
        fields.append(CardField("STORY", story, P_SOCIAL))

    if thesis_summary:
        fields.append(
            CardField(
                "THESES",
                thesis_summary
                + (f"\n**Strongest:** {strongest_thesis}" if strongest_thesis else ""),
                P_SOCIAL,
            )
        )

    if social_summary:
        fields.append(CardField("PUBLIC / J7", social_summary, P_SOCIAL))

    if notable_wallets:
        fields.append(
            CardField("NOTABLE WALLETS", f"`{notable_wallets}` proven wallet(s)", P_SMART_MONEY)
        )

    if risk is not None:
        fields.append(CardField("RISK", "\n".join(risk.operator_lines()), P_SAFETY))
    else:
        fields.append(
            CardField("RISK", "Safety `UNKNOWN` — that is not a pass.", P_SAFETY)
        )

    reasons = tuple(getattr(score, "reasons", ()) or ())
    fields.append(
        CardField(
            "WHY THIS PINGED",
            "\n".join(f"• {reason.replace('_', ' ').title()}" for reason in reasons)
            or "• no named reason — this card should not have been sent",
            P_WHY_SURFACED,
        )
    )
    fields.append(
        CardField(
            "SCORE",
            f"Trending edge `{getattr(score, 'score', 0)}` • edge state "
            f"`{getattr(score, 'edge_state', 'UNKNOWN')}`"
            + (
                f"\nLegacy opportunity score `{score.legacy_score}` (supporting context only)"
                if getattr(score, "legacy_score", None) is not None
                else ""
            ),
            P_DIAGNOSTICS,
        )
    )

    warnings: list[str] = []
    if collision_warning:
        warnings.append(collision_warning)
    if source_caveat:
        warnings.append(source_caveat)
    if getattr(event, "already_large", False):
        warnings.append("NOT EARLY — this token is already large.")
    if warnings:
        fields.append(CardField("⚠", "\n".join(warnings), P_WARNINGS))

    fields.append(CardField("LINKS", _links(mint, fomo_url), P_LINKS))

    headline = {
        TRENDING_ALPHA: "🔥 FOMO TRENDING — LOOK NOW",
        TRENDING_CONTINUATION_ALERT: "🚀 TRENDING CONTINUATION — LOOK NOW",
        TRENDING_ACCELERATION_ALERT: "🔥 TRENDING ACCELERATION — LOOK NOW",
        OFF_TRENDING_EXCEPTION: "⚡ OFF-TRENDING EXCEPTION — LOOK NOW",
    }.get(kind, "🔥 FOMO TRENDING — LOOK NOW")

    display = f"${symbol}" if symbol else (name or "Unknown token")
    spec = CardSpec(
        title=f"{headline} • {display}",
        description=(
            f"`{mint}`\n"
            + (
                "Promoted from HOT WATCH — evidence strengthened.\n"
                if promoted_from_hot_watch
                else ""
            )
            + "**RESEARCH ONLY. MANUAL DECISION. Nothing was bought and nothing can be.**"
        ),
        compact_description=(
            f"{headline} • {display} `{mint}` — rank "
            f"{'#' + str(rank) if rank else 'unranked'}, MC "
            f"{_money(getattr(entry, 'current_market_cap_usd', None))}. Research only."
        ),
        fields=tuple(fields),
        footer=(
            "Trending is attention, not safety. A verified badge is not rug protection."
        ),
        thumbnail_url=image_url,
        colour=0xE67E22,
    )
    return FastAlert(
        kind=kind,
        mint=mint,
        alert_key=f"{kind}:{mint}",
        spec=spec,
        ping=True,
        ping_reason=", ".join(reasons[:3]),
        token_mint=mint,
        lane=LANE_URGENT,
        family=kind,
    )


def build_trending_hot_watch_card(
    *,
    mint: str,
    symbol: str,
    name: str,
    fomo_url: str,
    entry: Any,
    score: Any,
    gap: Decimal,
    now: int = 0,
) -> FastAlert:
    """The quiet HOT WATCH card.  Radar lane, no ping (section 44)."""

    display = f"${symbol}" if symbol else (name or "Unknown token")
    spec = CardSpec(
        title=f"👀 TRENDING HOT WATCH • {display}",
        description=(
            f"`{mint}`\nStrong near miss — reevaluating on a fast cadence for a bounded "
            "window. This is **not** a ping and **not** a recommendation."
        ),
        compact_description=f"HOT WATCH {display} `{mint}` — near miss, rechecking fast.",
        fields=(
            CardField(
                "WHY",
                f"Trending edge `{getattr(score, 'score', 0)}` — `{gap}` points below the "
                "alpha threshold. It will ping once only if the evidence strengthens.",
                P_DECISION,
            ),
            CardField(
                "STATE",
                f"Rank `{'#' + str(entry.current_rank) if entry.current_rank else 'unranked'}` • "
                f"MC `{_money(entry.current_market_cap_usd)}`",
                P_LIQUIDITY,
            ),
            CardField("LINKS", _links(mint, fomo_url), P_LINKS),
        ),
        footer="Research only. No position was taken.",
        colour=0x95A5A6,
    )
    return FastAlert(
        kind=TRENDING_HOT_WATCH,
        mint=mint,
        alert_key=f"{TRENDING_HOT_WATCH}:{mint}:{now // 300}",
        spec=spec,
        ping=False,
        token_mint=mint,
        lane=LANE_RADAR,
        family=TRENDING_HOT_WATCH,
    )


# --- Trenches cards (v2.43, sections 47, 48) ---------------------------------
def _terminal_url(mint: str) -> str:
    """A plain navigation link, built from the exact mint (section 49).

    Navigation only: no authentication is attempted, nothing is read back, and
    the link is derived from the mint rather than from a name.
    """

    return f"https://trade.padre.gg/trade/solana/{mint}"


def _trench_links(mint: str, fomo_url: str) -> str:
    """Every link is derived from the exact mint — never from a name (§50-52)."""

    return (
        f"[FOMO]({fomo_url}) • [PUMP.FUN](https://pump.fun/coin/{mint}) • "
        f"[TERMINAL]({_terminal_url(mint)}) • "
        f"[JUPITER](https://jup.ag/swap/SOL-{mint}) • "
        f"[DEX](https://dexscreener.com/solana/{mint}) • "
        f"[SOLSCAN](https://solscan.io/token/{mint})"
    )


def _timeframe_line(timeframes: Any) -> str:
    if timeframes is None:
        return "no timeframe data yet"
    headline = timeframes.headline()
    return (
        f"{headline}\nShape `{timeframes.shape}` • momentum `{timeframes.momentum_curve}`"
    )


def build_trench_runner_alert(
    *,
    mint: str,
    name: str,
    symbol: str,
    fomo_url: str,
    kind: str,
    candidate: Any,
    story: str = "",
    chatter: str = "",
    notable_wallets: int = 0,
    reuse_warning: str = "",
    image_url: str = "",
    now: int = 0,
) -> FastAlert:
    """The PUMP TRENCH RUNNER card (section 47).

    Everything on it is public or on-chain, and every uncertain field says
    UNKNOWN rather than guessing.  It is allowed to say LOOK NOW; it is never
    allowed to say buy, safe or guaranteed.
    """

    lifecycle = candidate.lifecycle
    participants = candidate.participants
    dev = candidate.dev
    bundles = candidate.bundles
    risk = candidate.risk

    fields = [
        CardField(
            "STAGE",
            (
                f"`{lifecycle.label}`"
                + (
                    f" • bonding `{lifecycle.progress_percent}%`"
                    if lifecycle.progress_percent is not None
                    else " • bonding `unknown`"
                )
                + (
                    f" • age `{_duration(lifecycle.age_seconds)}`"
                    if lifecycle.age_seconds is not None
                    else ""
                )
                + (
                    f"\n⚠ `{lifecycle.special_mode}` mode — not an ordinary token"
                    if lifecycle.special_mode
                    else ""
                )
            ),
            P_DECISION,
        ),
        CardField(
            "MARKET",
            (
                f"First seen MC `{_money(candidate.first_market_cap_usd)}` → now "
                f"`{_money(candidate.market_cap_usd)}`\n"
                f"Liquidity `{_money(candidate.liquidity_usd)}`\n"
                f"{_timeframe_line(candidate.timeframes)}"
            ),
            P_LIQUIDITY,
        ),
    ]

    if participants is not None:
        ratio = participants.independence_ratio
        fields.append(
            CardField(
                "PARTICIPATION",
                (
                    f"Buys/sells `{participants.buys}` / `{participants.sells}`\n"
                    f"Unique buyers `{participants.unique_buyers}` → **independent "
                    f"`{participants.independent_buyers}`**"
                    + (f" (ratio `{ratio}`)" if ratio is not None else "")
                    + (
                        f"\nFresh wallets `{participants.fresh_wallet_buyers}`, "
                        f"independent `{participants.independent_fresh_buyers}`"
                        if participants.fresh_wallet_buyers
                        else ""
                    )
                    + (
                        f"\nClustered demand `{participants.clustered_percent}%`"
                        if participants.clustered_percent is not None
                        else ""
                    )
                ),
                P_DEMAND,
            )
        )

    holder_bits = []
    if candidate.holders is not None:
        holder_bits.append(f"Holders `{candidate.holders}`")
    if candidate.top10_percent is not None:
        holder_bits.append(f"Top 10 `{candidate.top10_percent}%`")
    if holder_bits:
        fields.append(CardField("HOLDERS", " • ".join(holder_bits), P_DEMAND))

    if dev is not None and dev.wallet:
        fields.append(
            CardField(
                "DEV",
                (
                    (
                        f"Holding `{dev.holding.current_percent}%` "
                        f"(`{dev.holding.posture}`)\n"
                        if dev.holding.current_percent is not None
                        else f"Holding `unknown` (`{dev.holding.posture}`)\n"
                    )
                    + dev.funding.operator_line()
                    + "\n"
                    + dev.history.operator_line()
                ),
                P_SMART_MONEY,
            )
        )

    if bundles is not None:
        fields.append(CardField("BUNDLES", bundles.operator_line(), P_WARNINGS))

    if story:
        fields.append(CardField("STORY", story, P_SOCIAL))
    if chatter:
        fields.append(CardField("PUBLIC CHATTER", chatter, P_SOCIAL))
    if notable_wallets:
        fields.append(
            CardField("NOTABLE WALLETS", f"`{notable_wallets}` proven wallet(s)", P_SMART_MONEY)
        )

    if risk is not None:
        fields.append(
            CardField("RISK", "\n".join(risk.operator_lines()), P_SAFETY)
        )

    if candidate.consensus is not None and candidate.consensus.lane_count:
        fields.append(
            CardField("SOURCES", candidate.consensus.operator_line(), P_DIAGNOSTICS)
        )

    fields.append(
        CardField(
            "WHY PINGED",
            "\n".join(
                f"• {reason.replace('_', ' ').title()}" for reason in candidate.score.reasons
            )
            or "• no named reason — this card should not have been sent",
            P_WHY_SURFACED,
        )
    )
    fields.append(
        CardField(
            "SCORE",
            f"Pump trench score `{candidate.score.score}` • cadence "
            f"`{candidate.cadence}`",
            P_DIAGNOSTICS,
        )
    )

    warnings = []
    if reuse_warning:
        warnings.append(reuse_warning)
    if warnings:
        fields.append(CardField("⚠", "\n".join(warnings), P_WARNINGS))

    fields.append(CardField("LINKS", _trench_links(mint, fomo_url), P_LINKS))

    headline = {
        TRENCH_RUNNER_ALERT: "🚨 PUMP TRENCH RUNNER — LOOK NOW",
        ALMOST_BONDED_ALERT: "⚡ ALMOST BONDED — MOMENTUM",
        PUBLIC_TRENDING_ALERT: "🔥 PUBLIC TRENDING — LOOK NOW",
        TRENCH_HEADS_UP_ALERT: "👀 TRENCH HEADS-UP",
    }.get(kind, "🚨 PUMP TRENCH RUNNER — LOOK NOW")

    display = f"${symbol}" if symbol else (name or "Unknown token")
    spec = CardSpec(
        title=f"{headline} • {display}",
        description=(
            f"`{mint}`\n"
            "**RESEARCH ONLY. MANUAL DECISION.** Nothing was bought and nothing can be."
        ),
        compact_description=(
            f"{headline} • {display} `{mint}` — {lifecycle.label}, MC "
            f"{_money(candidate.market_cap_usd)}. Research only."
        ),
        fields=tuple(fields),
        footer=(
            "Early is not safe. Fresh wallets are not demand until they are "
            "independent, and a bonding curve is not a thesis."
        ),
        thumbnail_url=image_url,
        colour=0x9B59B6,
    )
    return FastAlert(
        kind=kind,
        mint=mint,
        alert_key=f"{kind}:{mint}",
        spec=spec,
        ping=kind in {TRENCH_RUNNER_ALERT, ALMOST_BONDED_ALERT, PUBLIC_TRENDING_ALERT},
        ping_reason=", ".join(candidate.score.reasons[:3]),
        token_mint=mint,
        lane=(
            LANE_URGENT
            if kind in {TRENCH_RUNNER_ALERT, ALMOST_BONDED_ALERT, PUBLIC_TRENDING_ALERT}
            else LANE_RADAR
        ),
        family=kind,
    )


def build_public_trending_alert(
    *,
    mint: str,
    name: str,
    symbol: str,
    fomo_url: str,
    candidate: Any,
    rank: int | None = None,
    previous_rank: int | None = None,
    story: str = "",
    thesis: str = "",
    mentions: str = "",
    notable_wallets: int = 0,
    image_url: str = "",
    now: int = 0,
) -> FastAlert:
    """The PUBLIC TRENDING card (section 48).

    The rank on this card is **ours**.  The caveat is not decoration: it is the
    difference between reporting a model and claiming somebody else's ranking.
    """

    trend = candidate.public_trend
    rank_line = "unranked"
    if rank is not None:
        rank_line = f"#{rank}"
        if previous_rank is not None and previous_rank != rank:
            rank_line += f" (was #{previous_rank}, {previous_rank - rank:+d})"

    fields = [
        CardField(
            "PUBLIC TREND RANK",
            f"**{rank_line}**\n_{PUBLIC_MODEL_CAVEAT}_",
            P_DECISION,
        ),
        CardField("MOMENTUM", _timeframe_line(candidate.timeframes), P_MOMENTUM),
        CardField(
            "MARKET",
            (
                f"MC `{_money(candidate.market_cap_usd)}` • liquidity "
                f"`{_money(candidate.liquidity_usd)}`\n"
                f"Stage `{candidate.lifecycle.label}`"
                + (
                    f" • bonding `{candidate.lifecycle.progress_percent}%`"
                    if candidate.lifecycle.progress_percent is not None
                    else ""
                )
            ),
            P_LIQUIDITY,
        ),
    ]

    if candidate.holders is not None or candidate.top10_percent is not None:
        parts = []
        if candidate.holders is not None:
            parts.append(f"Holders `{candidate.holders}`")
        if candidate.top10_percent is not None:
            parts.append(f"Top 10 `{candidate.top10_percent}%`")
        fields.append(CardField("HOLDERS", " • ".join(parts), P_DEMAND))

    if candidate.participants is not None:
        fields.append(
            CardField(
                "FLOW",
                f"Independent buyers `{candidate.participants.independent_buyers}` "
                f"of `{candidate.participants.unique_buyers}` wallets",
                P_DEMAND,
            )
        )

    if story:
        fields.append(CardField("STORY", story, P_SOCIAL))
    if thesis:
        fields.append(CardField("THESIS", thesis, P_SOCIAL))
    if mentions:
        fields.append(CardField("J7 / PUBLIC", mentions, P_SOCIAL))
    if notable_wallets:
        fields.append(
            CardField("SMART WALLETS", f"`{notable_wallets}`", P_SMART_MONEY)
        )
    if candidate.risk is not None:
        fields.append(CardField("RISK", "\n".join(candidate.risk.operator_lines()), P_SAFETY))

    fields.append(
        CardField(
            "WHY PINGED",
            "\n".join(
                f"• {reason.replace('_', ' ').title()}" for reason in candidate.score.reasons
            )
            or "• no named reason — this card should not have been sent",
            P_WHY_SURFACED,
        )
    )
    if trend is not None:
        fields.append(
            CardField(
                "MODEL",
                f"`{trend.model}` score `{trend.score}` • "
                f"{trend.independent_sources} independent source(s)",
                P_DIAGNOSTICS,
            )
        )
    fields.append(CardField("LINKS", _trench_links(mint, fomo_url), P_LINKS))

    display = f"${symbol}" if symbol else (name or "Unknown token")
    spec = CardSpec(
        title=f"🔥 PUBLIC TRENDING — LOOK NOW • {display}",
        description=(
            f"`{mint}`\n"
            "**RESEARCH ONLY. MANUAL DECISION.** Nothing was bought and nothing can be."
        ),
        compact_description=(
            f"PUBLIC TRENDING {rank_line} • {display} `{mint}` — research only."
        ),
        fields=tuple(fields),
        footer=PUBLIC_MODEL_CAVEAT,
        thumbnail_url=image_url,
        colour=0xE67E22,
    )
    return FastAlert(
        kind=PUBLIC_TRENDING_ALERT,
        mint=mint,
        alert_key=f"{PUBLIC_TRENDING_ALERT}:{mint}",
        spec=spec,
        ping=True,
        ping_reason=", ".join(candidate.score.reasons[:3]),
        token_mint=mint,
        lane=LANE_URGENT,
        family=PUBLIC_TRENDING_ALERT,
    )


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"
