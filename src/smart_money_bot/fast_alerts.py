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

from .constants import TERMINAL_TOKEN_URL_TEMPLATE
from .discord_render import (
    P_ABOUT,
    P_DECISION,
    P_DEMAND,
    P_DIAGNOSTICS,
    P_EDGE,
    P_IDENTITY,
    P_LIFECYCLE,
    P_LINKS,
    P_LIQUIDITY,
    P_MOMENTUM,
    P_OPPORTUNITY,
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

# --- early-candidate promotion (sections 3, 29, 35) --------------------------
#: A hot-watched near-miss whose evidence developed while the edge was still
#: available.  This is the card the production failure in section 1 never got.
EARLY_PROMOTION = "EARLY_PROMOTION"

# --- GMGN participant alerts (v2.45, sections 22, 23) ------------------------
#: A wallet GMGN classifies as smart money entered.  A classification, not a
#: track record — the card says which it is.
GMGN_SMART_MONEY_ALERT = "GMGN_SMART_MONEY"
#: A KOL entered.  Attention, explicitly not expectancy.  Kept as its own class
#: so the forward record can answer whether famous buyers are worth anything.
GMGN_KOL_ALERT = "GMGN_KOL"

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
    EARLY_PROMOTION,
    GMGN_SMART_MONEY_ALERT,
    GMGN_KOL_ALERT,
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
        # A promotion is by definition the moment the evidence became worth
        # an interruption, so it is the one card that must reach the human.
        EARLY_PROMOTION,
        # A provider-classified smart-money entry is worth a look while the
        # edge exists.  A KOL entry deliberately is NOT: fame is attention, and
        # attention is what the radar lane is for.
        GMGN_SMART_MONEY_ALERT,
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
        # A promotion is the moment a watched near-miss became worth an
        # interruption, so it belongs in the lane interruptions live in.
        EARLY_PROMOTION,
        GMGN_SMART_MONEY_ALERT,
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
    #: Whether this candidate has earned an actionable buy control.  Default
    #: False: a card must *prove* eligibility to get a buy CTA, because
    #: attaching one to a candidate whose safety is UNKNOWN or whose identity is
    #: unverified presents a guess as an opportunity.
    trade_eligible: bool = False
    #: Whether the exact mint on this card was verified rather than inferred.
    identity_verified: bool = True
    #: Set when other live tokens share this one's symbol.
    symbol_collision: bool = False

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


#: Actionable phrases a card may only use once validation has actually passed.
_ACTIONABLE_PHRASES: tuple[str, ...] = ("LOOK NOW", "BUY NOW", "APE", "SEND IT")


def strip_actionable(title: str) -> str:
    """Take the instruction out of a title without taking the tier out (v2.50).

    Used at the single publish choke point so that a card refused on quality
    cannot lead with "LOOK NOW" no matter which builder produced it.  The
    production case: a promotion card titled **🚨 EARLY RUNNER — LOOK NOW**
    for a token its own body reported at **-63.70% over five minutes** with
    819 sells against 765 buys.
    """

    cleaned = title
    for phrase in _ACTIONABLE_PHRASES:
        cleaned = cleaned.replace(f" — {phrase}", "").replace(f" - {phrase}", "")
        cleaned = cleaned.replace(phrase, "")
    cleaned = cleaned.strip(" —-•").strip()
    return cleaned or "RESEARCH CANDIDATE"


def _validation_pending_title(tier_label: str, *, late: bool = False) -> str:
    """Strip actionable language from a card whose validation is not finished.

    The tier is preserved in parentheses because it is genuine information —
    "this reached the organic-runner bar" is worth knowing.  What it may not do
    is *lead* with an instruction the evidence does not support.
    """

    cleaned = tier_label
    for phrase in _ACTIONABLE_PHRASES:
        cleaned = cleaned.replace(f" — {phrase}", "").replace(f" - {phrase}", "")
        cleaned = cleaned.replace(phrase, "")
    cleaned = cleaned.strip(" —-•").strip()
    if late:
        return (
            f"🕒 RESEARCH CANDIDATE — LATE ({cleaned})"
            if cleaned
            else "🕒 RESEARCH CANDIDATE — LATE"
        )
    return (
        f"🔬 RESEARCH CANDIDATE — VALIDATION PENDING ({cleaned})"
        if cleaned
        else "🔬 RESEARCH CANDIDATE — VALIDATION PENDING"
    )


#: How a discovery family reads on a card.  Compact on purpose: the operator
#: needs "why did the bot see this", not a twenty-line provider dump — the
#: detail belongs in `view:detail` (section 15).
SOURCE_LABELS: dict[str, str] = {
    "GMGN_TRENDING": "GMGN Trending",
    "GMGN_TRENCH_NEW": "GMGN Trenches — new creation",
    "GMGN_TRENCH_FINAL_STRETCH": "GMGN Trenches — final stretch",
    "GMGN_TRENCH_MIGRATED": "GMGN Trenches — migrated",
    "GMGN_MARKET_SIGNAL": "GMGN market signal",
    "GMGN_HOT_SEARCH": "GMGN hot search",
    "GMGN_SMART_MONEY": "GMGN Smart Money",
    "GMGN_KOL": "GMGN KOL",
    "pump_realtime": "Pump on-chain realtime",
    "PUBLIC_TRENDING_MODEL": "our public Trending model",
    "dex_snapshot": "DEX exact-mint snapshot",
    "story_watch": "story watch",
    "early_lane": "early lane",
}


def discovery_line(sources: Any, *, interval: str = "") -> str:
    """``GMGN Trending 1m • Pump on-chain confirmed`` — one line, not twenty.

    The first source is the one that found it; the rest are corroboration, and
    only a couple are shown because a card that scrolls is a card nobody reads.
    """

    ordered = [str(item) for item in (sources or ()) if str(item)]
    if not ordered:
        return ""
    primary = SOURCE_LABELS.get(ordered[0], ordered[0].replace("_", " ").title())
    if interval and "Trending" in primary:
        primary = f"{primary} {interval}"
    others = [
        SOURCE_LABELS.get(item, item.replace("_", " ").lower()) for item in ordered[1:3]
    ]
    return primary + (" • " + " • ".join(others) if others else "")


#: The operator's own trading surfaces, keyed by exact mint.  Every one of
#: these is built from the address and nothing else — a link assembled from a
#: ticker would send them to whichever token happened to claim the name.
GMGN_TOKEN_URL = "https://gmgn.ai/sol/token/{mint}"


def _links(mint: str, fomo_url: str) -> str:
    return (
        f"[FOMO]({fomo_url}) • [GMGN]({GMGN_TOKEN_URL.format(mint=mint)}) • "
        f"[PUMP.FUN](https://pump.fun/coin/{mint}) • "
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
    token_state: str = "",
    story_summary: str = "",
    safety_status: str = "UNKNOWN",
    proven: bool = False,
    terminal_url: str = "",
) -> FastAlert:
    """The small, fast notable-wallet card (sections 6, 7, 8).

    Entry market cap versus current market cap is mandatory and always present,
    and a late observation is published with its lateness quantified rather than
    hidden.

    A wallet whose *forward history* has earned weight gets the louder headline
    and does not wait for deep enrichment.  A wallet that is merely large does
    not: size is not a track record, and calling every whale smart money is how
    a channel stops meaning anything.
    """

    trade = signal.trade
    late = not signal.may_chase()
    kind = NOTABLE_TRADER_LATE if late else NOTABLE_TRADER_EARLY
    if late:
        title = "🐋 NOTABLE TRADER BUY — LATE OBSERVATION"
    elif proven:
        title = "🐋 KNOWN TRADER BUY — LOOK NOW"
    else:
        title = "🐋 NOTABLE TRADER BUY — EARLY DATA"
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

    # Section 6: what the token itself is doing, and what we do *not* yet know
    # about it.  The card is published before deep enrichment finishes, so the
    # honest thing is to name the gap rather than leave it implied.
    fields.append(
        CardField(
            "TOKEN STATE",
            (
                (f"Stage `{token_state}`\n" if token_state else "")
                + (f"Story: {story_summary}\n" if story_summary else "Story: none found\n")
                + f"Safety: **{safety_status}** — this is not a safety pass\n"
                + "Entry eligible: **NO** • Trade CTA: **DISABLED**"
            ),
            P_SAFETY,
        )
    )

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
            + (f" • [TERMINAL]({terminal_url})" if terminal_url else "")
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
    safety_status: str = "UNKNOWN",
    identity_verified: bool = True,
    symbol_collision: bool = False,
    discovered_via: str = "",
    clone_verdict: Any = None,
    quality: Any = None,
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

    # v2.47.  "You can tell when there's a fake coin" — so print the numbers
    # that tell you.  Fees are the lead line because fees are money that has
    # already left somebody's wallet, which is the one figure on this card that
    # cannot be walked up on nothing.
    if quality is not None:
        measured = getattr(quality, "measured_fraction", None)
        fees = getattr(quality, "fee_velocity_sol_per_minute", None)
        holders = getattr(quality, "holder_count", None)
        depth = getattr(quality, "liquidity_usd", None)
        body = (
            f"Real-money score **{getattr(quality, 'score', 0)}/100**"
            + (f" • measured `{measured}` of the picture" if measured is not None else "")
            # Fees first, and printed whether they are good or bad.  A number
            # shown only once it clears a threshold is a number the operator
            # cannot use to form their own judgement.
            + f"\n**Fees `{fees if fees is not None else '?'}` SOL/min**"
            + f" • liquidity `{_money(depth)}`"
            + f" • holders `{holders if holders is not None else '?'}`"
        )
        strengths = tuple(getattr(quality, "reasons", ()) or ())
        if strengths:
            body += "\n" + "\n".join(f"• {item}" for item in strengths[:4])
        concerns_list = tuple(getattr(quality, "concerns", ()) or ())
        if concerns_list:
            body += "\n" + "\n".join(f"⚠ {item}" for item in concerns_list[:4])
        fields.append(CardField("REAL MONEY", body, P_DEMAND))

    # v2.48.  When the answer is "this is not an entry", say it at the top of
    # the card in the plainest words available, with the number that decided
    # it.  The operator's complaint was fake charts reaching them looking like
    # opportunities; a card that buries the reason is the same failure.
    disqualifiers = tuple(getattr(quality, "disqualifiers", ()) or ())
    if disqualifiers:
        fields.append(
            CardField(
                "⛔ NOT AN ENTRY",
                "\n".join(f"• {item}" for item in disqualifiers[:3])
                + "\nShown so you can see it exists. It cannot ping you.",
                P_WARNINGS,
            )
        )

    if clone_verdict is not None and getattr(clone_verdict, "collision", False):
        fields.append(
            CardField(
                "⚠ ANOTHER TOKEN USES THIS NAME",
                (
                    f"{clone_verdict.warning_line()}\n"
                    f"Verdict: **{getattr(clone_verdict, 'verdict', '')}** — "
                    f"{clone_verdict.human()}\n"
                    + (
                        "\n".join(
                            f"• {item}" for item in getattr(clone_verdict, "reasons", ())[:3]
                        )
                    )
                ),
                P_WARNINGS,
            )
        )

    # The early lane is cheap by design: it publishes before safety, identity and
    # deep validation have finished.  That is fine — being early is the point —
    # but the card must then say what it does not know, in the same breath as
    # what it does.  A card that prints "SAFETY: UNKNOWN" under the title
    # "ORGANIC RUNNER — LOOK NOW" is telling the operator two contradictory
    # things and letting the louder one win.
    fields.append(
        CardField(
            "STATE",
            (
                f"Identity: **{'VERIFIED' if identity_verified else 'UNVERIFIED'}**"
                f" • Symbol collision: **{'YES' if symbol_collision else 'NO'}**\n"
                f"Safety: **{safety_status}** — deep analysis still running\n"
                "Entry eligible: **NO** • Trade CTA: **DISABLED**"
            ),
            P_SAFETY,
        )
    )
    if symbol_collision:
        fields.append(
            CardField(
                "⚠ SYMBOL COLLISION",
                (
                    f"Other live tokens use `${symbol}`. This card is for the exact "
                    f"mint `{mint}` and no other. A shared ticker is not a shared "
                    "token."
                ),
                P_WARNINGS,
            )
        )
    if not identity_verified:
        fields.append(
            CardField(
                "⚠ IDENTITY UNVERIFIED",
                (
                    "This mint was not confirmed against the discovery source. "
                    "Treat it as a lead to check, not as the token you were "
                    "looking at."
                ),
                P_WARNINGS,
            )
        )
    if discovered_via:
        fields.append(CardField("DISCOVERED VIA", discovered_via, P_WHY_SURFACED))
    fields.append(CardField("LINKS", _links(mint, fomo_url), P_LIQUIDITY))

    # Nothing from this lane is actionable, so nothing from it may use
    # actionable language.  The tier still travels on the card — the operator
    # wants to know *why* it surfaced — it just no longer sets the headline.
    tier_label = str(getattr(verdict, "label", "👀 EARLY HEADS-UP"))
    title = _validation_pending_title(tier_label, late=late)
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
    # v2.47.  Only an original — or a token nobody is imitating — may
    # interrupt a human, and only if what we could measure of it was not
    # measurably thin.  Neither test hides the card: both of these still
    # publish to the radar, where the warning above is there to be read.  The
    # operator's words were "stop recommending copied coins", not "stop
    # showing me that they exist".
    clone_ok = clone_verdict is None or bool(getattr(clone_verdict, "may_ping", True))
    quality_ok = quality is None or not bool(quality.weak())
    may_ping = kind == EARLY_RUNNER and not late and clone_ok and quality_ok

    return FastAlert(
        kind=kind,
        mint=mint,
        alert_key=f"{kind}:{mint}",
        spec=spec,
        # A late card never pings, whatever tier the evidence reached.
        ping=may_ping,
        ping_reason=", ".join(categories[:2]),
        # This lane publishes before validation finishes, so it can never hand
        # out a buy control.
        trade_eligible=False,
        identity_verified=identity_verified,
        symbol_collision=symbol_collision,
        fingerprint=f"{tier}:{int(getattr(verdict, 'score', ZERO))}",
        token_mint=mint,
        lane=LANE_URGENT if may_ping else LANE_RADAR,
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
    #: Set when late-arriving exact-mint metadata should rewrite the card's
    #: identity block — the name, ticker and thumbnail (v2.46, section 13).
    description: str = ""
    compact_description: str = ""
    thumbnail_url: str = ""

    def apply(self, spec: CardSpec) -> CardSpec:
        from dataclasses import replace as _replace

        if self.replace_fields:
            merged = self.fields
        else:
            by_name = {item.name: item for item in spec.fields}
            for item in self.fields:
                by_name[item.name] = item
            merged = tuple(by_name.values())
        return _replace(
            spec,
            fields=merged,
            footer=self.footer or spec.footer,
            # Each is applied only when supplied: an enrichment pass that
            # learned nothing new must never blank a field the card already
            # had, which is the whole "never move backwards" rule.
            description=self.description or spec.description,
            compact_description=self.compact_description or spec.compact_description,
            thumbnail_url=self.thumbnail_url or spec.thumbnail_url,
        )


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

    return TERMINAL_TOKEN_URL_TEMPLATE.replace("{mint}", mint)


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


# --- the promotion card (sections 3, 29, 35) ---------------------------------


def _known_trader_lines(traders: Any) -> tuple[str, ...]:
    """Render at most a few known wallets, always with what they are doing."""

    lines: list[str] = []
    for trader in list(traders or ())[:4]:
        state = str(getattr(trader, "state", "") or "UNKNOWN").replace("_", " ").lower()
        wallet = str(getattr(trader, "wallet", ""))
        name = str(getattr(trader, "display_name", "") or wallet[:6] + "…")
        reputation = str(getattr(trader, "reputation_state", "UNKNOWN"))
        samples = int(getattr(trader, "reputation_samples", 0) or 0)
        position = getattr(trader, "position", None)
        entry = getattr(position, "first_buy_market_cap_usd", None)
        amount = getattr(position, "bought_usd", None)
        line = f"**{name}** `{reputation}` ({samples} samples) • {state}"
        if amount is not None and entry is not None:
            line += f"\n  entered `{_money(amount)}` @ `{_money(entry)}` MC"
        lines.append(line)
    return tuple(lines)


def build_promotion_alert(
    *,
    mint: str,
    name: str,
    symbol: str,
    fomo_url: str,
    decision: Any,
    entry: Any,
    age_seconds: int | None = None,
    current_market_cap_usd: Decimal | None = None,
    liquidity_usd: Decimal | None = None,
    change_1m_percent: Decimal | None = None,
    change_5m_percent: Decimal | None = None,
    buys: int | None = None,
    sells: int | None = None,
    holder_series: str = "",
    holders_added: int | None = None,
    holder_window_seconds: int | None = None,
    top10_percent: Decimal | None = None,
    concentration_trend: str = "",
    dev_percent: Decimal | None = None,
    dev_posture: str = "",
    fresh_wallets: int | None = None,
    independent_buyers: int | None = None,
    known_traders: Any = (),
    known_money_flow: str = "",
    cluster_note: str = "",
    story_summary: str = "",
    thesis_summary: str = "",
    safety_status: str = "UNKNOWN",
    identity_verified: bool = True,
    symbol_collision: bool = False,
    image_url: str = "",
    terminal_url: str = "",
    discovered_via: str = "",
) -> FastAlert:
    """The card a hot-watched near-miss earns when new evidence arrives.

    This is the answer to the section 1 failure.  The heads-up card said what was
    true at second three; this one says what changed between then and now, and it
    leads with the *reason it is interrupting you* rather than with a score.

    Identity still gates the language.  Section 32 permits an actionable research
    alert while safety is UNKNOWN provided the card says so plainly — but an
    unverified mint is a different thing entirely, and no amount of market
    evidence makes a card about a token we could not identify actionable.
    """

    family = str(getattr(decision, "family", "") or "")
    families = tuple(getattr(decision, "families", ()) or ())
    reasons = tuple(getattr(decision, "reasons", ()) or ())
    trustworthy = identity_verified
    title = (
        "🚨 EARLY RUNNER — LOOK NOW"
        if trustworthy
        else "🔬 RESEARCH CANDIDATE — IDENTITY UNVERIFIED"
    )

    entry_mc = getattr(entry, "entry_market_cap_usd", None)
    first_seen_mc = getattr(entry, "first_seen_market_cap_usd", None)
    fields = [
        CardField(
            "WHY THIS IS INTERRUPTING YOU",
            (
                f"**{family.replace('_', ' ')}**"
                + (
                    f" • {len(families) - 1} other famil"
                    + ("y" if len(families) == 2 else "ies")
                    if len(families) > 1
                    else ""
                )
                + "\n"
                + ("\n".join(f"• {item}" for item in reasons[:5]) or "• evidence developed")
            ),
            P_DECISION,
        ),
        CardField(
            "MARKET CAP",
            (
                f"First seen `{_money(first_seen_mc)}`\n"
                f"At heads-up `{_money(entry_mc)}`\n"
                f"**Now `{_money(current_market_cap_usd)}`**\n"
                f"Move since heads-up "
                f"`{_percent_or(_move_percent(entry_mc, current_market_cap_usd))}`"
            ),
            P_OPPORTUNITY,
        ),
        CardField(
            "MOMENTUM",
            (
                f"Age `{age_seconds if age_seconds is not None else '?'}s` • 1m "
                f"`{_percent_or(change_1m_percent)}` • 5m `{_percent_or(change_5m_percent)}`\n"
                f"Liquidity `{_money(liquidity_usd)}` • flow "
                f"`{buys if buys is not None else '?'}` buys / "
                f"`{sells if sells is not None else '?'}` sells"
            ),
            P_MOMENTUM,
        ),
    ]

    if holder_series or holders_added is not None:
        window = (
            f" in {holder_window_seconds // 60}m"
            if holder_window_seconds and holder_window_seconds >= 60
            else (f" in {holder_window_seconds}s" if holder_window_seconds else "")
        )
        fields.append(
            CardField(
                "HOLDERS",
                (
                    (f"`{holder_series}`" if holder_series else "")
                    + (
                        f"\n**+{holders_added}**{window}"
                        if holders_added is not None
                        else ""
                    )
                    + (
                        f"\nTop 10 `{top10_percent}%`"
                        if top10_percent is not None
                        else ""
                    )
                    + (
                        f" • concentration `{concentration_trend.replace('CONCENTRATION_', '')}`"
                        if concentration_trend
                        else ""
                    )
                ).strip()
                or "unknown",
                P_DEMAND,
            )
        )

    if known_traders:
        lines = _known_trader_lines(known_traders)
        fields.append(
            CardField(
                "KNOWN TRADERS ON THIS EXACT MINT",
                "\n".join(lines)
                + (f"\n{known_money_flow.replace('_', ' ').lower()}" if known_money_flow else "")
                + (f"\n⚠ {cluster_note}" if cluster_note else ""),
                P_SMART_MONEY,
            )
        )

    participation: list[str] = []
    if independent_buyers is not None:
        participation.append(f"Independent buyers `{independent_buyers}`")
    if fresh_wallets is not None:
        participation.append(f"Fresh wallets `{fresh_wallets}`")
    if dev_percent is not None or dev_posture:
        participation.append(
            f"Dev `{dev_percent}%`" if dev_percent is not None else "Dev `?`"
            + (f" • {dev_posture.replace('DEV_HOLDING_', '').lower()}" if dev_posture else "")
        )
    if participation:
        fields.append(CardField("PARTICIPATION", " • ".join(participation), P_DEMAND))

    if story_summary or thesis_summary:
        fields.append(
            CardField(
                "STORY & THESIS",
                (f"Story: {story_summary}\n" if story_summary else "Story: none found\n")
                + (f"Thesis: {thesis_summary}" if thesis_summary else "Thesis: none graded"),
                P_SOCIAL,
            )
        )

    # Section 32: an actionable research alert may fire while safety is unknown,
    # but only if the card says so in the same breath.  It is never a claim that
    # the token is safe, and it never comes with a way to buy.
    fields.append(
        CardField(
            "STATE",
            (
                f"Identity: **{'VERIFIED' if identity_verified else 'UNVERIFIED'}**"
                f" • Symbol collision: **{'YES' if symbol_collision else 'NO'}**\n"
                f"Safety: **{safety_status}** — this is not a safety pass\n"
                "Entry eligible: **NO** • Trade CTA: **DISABLED**"
            ),
            P_SAFETY,
        )
    )
    if symbol_collision:
        fields.append(
            CardField(
                "⚠ SYMBOL COLLISION",
                (
                    f"Other live tokens use `${symbol}`. Every number above belongs "
                    f"to `{mint}` and no other."
                ),
                P_WARNINGS,
            )
        )
    if discovered_via:
        fields.append(CardField("DISCOVERED VIA", discovered_via, P_WHY_SURFACED))
    links = _links(mint, fomo_url)
    if terminal_url:
        links += f" • [TERMINAL]({terminal_url})"
    fields.append(CardField("LINKS", links, P_LIQUIDITY))

    spec = CardSpec(
        title=title,
        description=(
            f"**{name}** `${symbol}`\n"
            f"Mint: `{mint}`\n"
            f"Heads-up `{_money(entry_mc)}` → now `{_money(current_market_cap_usd)}` "
            f"({_percent_or(_move_percent(entry_mc, current_market_cap_usd))})\n"
            f"{links}"
        ),
        compact_description=(
            f"{title}\n**{name}** `${symbol}`\n`{mint}`\n"
            f"{family.replace('_', ' ')} • heads-up `{_money(entry_mc)}` → "
            f"`{_money(current_market_cap_usd)}`"
        ),
        fields=tuple(fields),
        footer=RESEARCH_ONLY_FOOTER,
        thumbnail_url=image_url,
        colour=0xE74C3C if trustworthy else 0x95A5A6,
    )
    return FastAlert(
        kind=EARLY_PROMOTION,
        mint=mint,
        # One key per mint per promotion: a candidate is promoted exactly once,
        # and the deduplicator is the second guarantee behind that latch.
        alert_key=f"{EARLY_PROMOTION}:{mint}",
        spec=spec,
        ping=trustworthy,
        ping_reason=", ".join(families[:2]),
        # Real money stays disabled everywhere (section 38).
        trade_eligible=False,
        identity_verified=identity_verified,
        symbol_collision=symbol_collision,
        fingerprint=f"{family}:{int(getattr(decision, 'promote', False))}",
        token_mint=mint,
        lane=LANE_URGENT if trustworthy else LANE_RADAR,
        family=family,
    )


# --- GMGN participant cards (sections 21, 22, 23) ----------------------------


def build_gmgn_participant_alert(
    *,
    mint: str,
    name: str,
    symbol: str,
    fomo_url: str,
    wallet: str,
    wallet_label: str = "",
    kind: str = GMGN_SMART_MONEY_ALERT,
    trade_usd: Decimal | None = None,
    wallet_entry_market_cap_usd: Decimal | None = None,
    detection_market_cap_usd: Decimal | None = None,
    current_market_cap_usd: Decimal | None = None,
    seconds_late: int | None = None,
    lifecycle_stage: str = "",
    trending_rank: int | None = None,
    trending_interval: str = "",
    independent_wallets: int = 1,
    cluster_note: str = "",
    bot_reputation: str = "",
    bot_reputation_samples: int = 0,
    story_summary: str = "",
    safety_status: str = "UNKNOWN",
    identity_verified: bool = True,
    symbol_collision: bool = False,
    edge_consumed: bool = False,
    image_url: str = "",
    terminal_url: str = "",
) -> FastAlert:
    """The stage-1 card for a provider-classified wallet entering a mint.

    Published before deep enrichment finishes, because that is the entire point
    (section 21): an operator finding out four minutes later has found out that
    they missed it.

    Two honesty rules shape the wording.  **A GMGN tag is a classification, not
    a track record** — the card says "GMGN classification" and shows this bot's
    own forward-measured reputation separately, so a wallet GMGN calls smart
    money and our record calls a late chaser reads as exactly that.  And **a KOL
    is not smart money**: famous is attention, so the KOL card says KOL, never
    borrows the smart-money headline, and never pings.
    """

    is_kol = kind == GMGN_KOL_ALERT
    trustworthy = identity_verified and not edge_consumed
    if not identity_verified:
        title = "🔬 RESEARCH CANDIDATE — IDENTITY UNVERIFIED"
    elif edge_consumed:
        title = (
            "⚠ KOL ACTIVITY — EDGE CONSUMED"
            if is_kol
            else "⚠ SMART MONEY BUY — EDGE CONSUMED"
        )
    elif is_kol:
        title = "📣 KOL ACTIVITY — LOOK NOW"
    else:
        title = "🐋 SMART MONEY BUY — LOOK NOW"

    display = wallet_label or f"{wallet[:6]}…{wallet[-4:] if len(wallet) > 10 else ''}"
    current_mc = current_market_cap_usd
    classification = "KOL" if is_kol else "SMART MONEY"

    fields = [
        CardField(
            "WALLET",
            (
                f"**{display}**\n`{wallet}`\n"
                f"GMGN classification: **{classification}**"
                + (
                    f"\nOur own forward record: `{bot_reputation}` "
                    f"({bot_reputation_samples} samples)"
                    if bot_reputation
                    else "\nOur own forward record: `no sample yet`"
                )
                + (
                    "\n_A provider label is a classification, not a track record._"
                    if not bot_reputation
                    else ""
                )
            ),
            P_IDENTITY,
        ),
        CardField(
            "ENTRY vs NOW",
            (
                f"Trade `{_money(trade_usd)}`\n"
                f"Wallet entry MC `{_money(wallet_entry_market_cap_usd)}`\n"
                f"Bot detected `{_money(detection_market_cap_usd)}`\n"
                f"Current MC `{_money(current_market_cap_usd)}`\n"
                "Move since wallet entry "
                f"`{_percent_or(_move_percent(wallet_entry_market_cap_usd, current_mc))}`"
                + (
                    f" • seconds late `{seconds_late}`"
                    if seconds_late is not None
                    else ""
                )
            ),
            P_DECISION,
        ),
        CardField(
            "TOKEN STATE",
            (
                (f"Lifecycle `{lifecycle_stage}`\n" if lifecycle_stage else "")
                + (
                    f"GMGN Trending `{trending_interval} #{trending_rank}`\n"
                    if trending_rank is not None
                    else ""
                )
                + (f"Story: {story_summary}\n" if story_summary else "Story: none found\n")
                + f"Safety: **{safety_status}** — validation running, not a safety pass"
            ),
            P_LIFECYCLE,
        ),
    ]

    why: list[str] = []
    if is_kol:
        why.append("a KOL entered — this is attention, not proven expectancy")
    else:
        why.append("a wallet GMGN classifies as smart money entered")
    if independent_wallets > 1:
        why.append(f"{independent_wallets} independent tagged wallets, after cluster collapse")
    if not edge_consumed:
        why.append("current edge still available")
    else:
        why.append("the move was already made by the time we saw it")
    if cluster_note:
        why.append(cluster_note)
    fields.append(
        CardField(
            "WHY YOU'RE SEEING THIS",
            "\n".join(f"• {item}" for item in why),
            P_WHY_SURFACED,
        )
    )

    fields.append(
        CardField(
            "STATE",
            (
                f"Identity: **{'VERIFIED' if identity_verified else 'UNVERIFIED'}**"
                f" • Symbol collision: **{'YES' if symbol_collision else 'NO'}**\n"
                f"Safety: **{safety_status}**\n"
                "Entry eligible: **NO** • Trade CTA: **DISABLED**"
            ),
            P_SAFETY,
        )
    )
    links = _links(mint, fomo_url)
    if terminal_url:
        links += f" • [TERMINAL]({terminal_url})"
    fields.append(CardField("LINKS", links, P_LIQUIDITY))

    spec = CardSpec(
        title=title,
        description=(
            f"**{name}** `${symbol}`\nMint: `{mint}`\n"
            f"{display} • {classification}\n{links}"
        ),
        compact_description=(
            f"{title}\n**{name}** `${symbol}`\n`{mint}`\n"
            f"{display} ({classification}) • entry "
            f"`{_money(wallet_entry_market_cap_usd)}` → now "
            f"`{_money(current_market_cap_usd)}`"
        ),
        fields=tuple(fields),
        footer=RESEARCH_ONLY_FOOTER,
        thumbnail_url=image_url,
        colour=0x95A5A6 if not trustworthy else (0x9B59B6 if is_kol else 0x2ECC71),
    )
    return FastAlert(
        kind=kind,
        mint=mint,
        alert_key=f"{kind}:{mint}:{wallet}",
        spec=spec,
        # A KOL never interrupts: fame is attention, and attention belongs on
        # the radar until the forward record says otherwise.
        ping=trustworthy and not is_kol,
        ping_reason=kind,
        trade_eligible=False,
        identity_verified=identity_verified,
        symbol_collision=symbol_collision,
        fingerprint=f"{kind}:{wallet}",
        token_mint=mint,
        lane=LANE_URGENT if (trustworthy and not is_kol) else LANE_RADAR,
        family=kind,
    )


def enrichment_from_presentation(
    *,
    alert_key: str,
    mint: str,
    presentation: Any,
    fomo_url: str,
    terminal_url: str = "",
    headline: str = "",
) -> EnrichmentUpdate:
    """Rewrite a published card's identity block once metadata resolves.

    This is the second half of the "publish fast, enrich in place" contract
    (sections 4, 13).  The alert already went out — possibly reading
    ``Metadata pending`` — and this edits *that same message* so the operator
    ends up looking at a named token with its real icon, without a second ping
    and without having waited for a metadata call before being told anything.

    The presentation is passed whole rather than as loose strings so the card
    cannot end up describing a different token than the one the record is for.
    """

    name = str(getattr(presentation, "display_name", "") or "")
    symbol = str(getattr(presentation, "display_symbol", "") or "")
    thumbnail = str(getattr(presentation, "thumbnail", "") or "")
    links = _links(mint, fomo_url)
    if terminal_url:
        links += f" • [TERMINAL]({terminal_url})"
    return EnrichmentUpdate(
        alert_key=alert_key,
        description=(
            (f"{headline}\n" if headline else "")
            + f"**{name}** `${symbol}`\nMint: `{mint}`\n{links}"
        ),
        compact_description=f"**{name}** `${symbol}`\n`{mint}`",
        thumbnail_url=thumbnail,
    )
