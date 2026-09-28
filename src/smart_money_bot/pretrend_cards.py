"""Discord cards for the pre-trend lane.

Two design rules separate these cards from the ones that made the bot noisy.

**A probability, with its base rate, or nothing.**  The headline number is a
calibrated probability and it is always printed next to the base rate it should
be compared against and the sample behind it.  "72%" on its own is a number the
reader cannot check; "72% (base rate 0.9%, lift 80x, n=214 entries)" is.

**Every claim names its evidence or says UNKNOWN.**  A field whose input was
unavailable prints ``unknown``, never ``0`` and never a comfortable default.  A
reader must be able to tell "there was no FOMO buying" from "we could not see
the FOMO tape", because those justify opposite actions.

The confirmation card is the scoreboard.  It always states whether we predicted
the entry, how early, and at what market cap — including when the answer is
"missed by the model", because a card that only appears when the bot was right
is marketing.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from .constants import fomo_coin_url
from .discord_render import (
    P_DECISION,
    P_DEMAND,
    P_EDGE,
    P_IDENTITY,
    P_LIFECYCLE,
    P_LINKS,
    P_MOMENTUM,
    P_SMART_MONEY,
    P_SOCIAL,
    P_WARNINGS,
    P_WHY_SURFACED,
    CardField,
    CardSpec,
)
from .fast_alerts import GMGN_TOKEN_URL
from .pretrend.affinity import AffinityRecord
from .pretrend.features import FeatureVector
from .pretrend.forensics import ForensicReport
from .pretrend_runtime import PretrendSignal, TrendConfirmation

ZERO = Decimal("0")

#: A deliberately unglamorous colour.  This lane is research output.
PRE_TREND_COLOUR = 0xE67E22
CONFIRMED_COLOUR = 0x2ECC71
MISSED_COLOUR = 0x95A5A6

UNKNOWN = "unknown"


def _money(value: Decimal | None) -> str:
    """Format USD, or admit we do not know.  Never prints ``$0`` for missing."""

    if value is None:
        return UNKNOWN
    amount = abs(value)
    sign = "-" if value < ZERO else ""
    if amount >= 1_000_000:
        return f"{sign}${amount / 1_000_000:.2f}M"
    if amount >= 1_000:
        return f"{sign}${amount / 1_000:.1f}K"
    return f"{sign}${amount:.2f}"


def _number(value: Decimal | None, *, places: int = 2) -> str:
    if value is None:
        return UNKNOWN
    return f"{value:.{places}f}".rstrip("0").rstrip(".") or "0"


def _pct_value(value: Decimal | None) -> str:
    """A value already expressed in percent, or ``unknown`` without a stray ``%``."""

    return UNKNOWN if value is None else f"{_number(value)}%"


def _integer(value: Decimal | None) -> str:
    if value is None:
        return UNKNOWN
    return str(int(value))


def _percent(value: Decimal | None) -> str:
    if value is None:
        return UNKNOWN
    return f"{value * 100:.1f}%"


def _or_unknown(value: object) -> str:
    """Render a value, or ``unknown``.  Missing is never rendered as a number."""

    return UNKNOWN if value is None else str(value)


def _age_text(seconds: Decimal | None) -> str:
    return UNKNOWN if seconds is None else _duration(int(seconds))


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return UNKNOWN
    if seconds < 0:
        return f"-{_duration(-seconds)}"
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remainder}s" if remainder else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _links(mint: str, referral_code: str | None) -> str:
    return (
        f"[FOMO]({fomo_coin_url(mint, referral_code)}) • "
        f"[GMGN]({GMGN_TOKEN_URL.format(mint=mint)}) • "
        f"[PUMP.FUN](https://pump.fun/coin/{mint}) • "
        f"[DEX](https://dexscreener.com/solana/{mint}) • "
        f"[SOLSCAN](https://solscan.io/token/{mint})"
    )


def _horizon_label(seconds: int) -> str:
    return f"{seconds // 60}m" if seconds >= 60 else f"{seconds}s"


def _why_it_fired(vector: FeatureVector, signal: PretrendSignal) -> tuple[str, ...]:
    """Named, checkable reasons.  Each one cites a value the reader can verify.

    The reasons come from the model's own contributions where available, so the
    card explains the decision that was actually made rather than a plausible
    story told alongside it.
    """

    lines: list[str] = []
    for name, contribution in signal.prediction.reason_codes[:6]:
        base = name.removesuffix("__missing")
        value = vector.get(base)
        direction = "+" if contribution > ZERO else "−"
        if name.endswith("__missing"):
            lines.append(f"{direction} {base}: value was UNKNOWN (itself informative)")
        else:
            lines.append(f"{direction} {base} = {_number(value, places=4)}")
    if not lines:
        lines.append("no individual feature dominated this score")
    return tuple(lines)


def _risks(vector: FeatureVector) -> tuple[str, ...]:
    """Risks stated as measured facts, or as explicit gaps in what we can see."""

    risks: list[str] = []
    liquidity = vector.get("liquidity_usd")
    if liquidity is None:
        risks.append("liquidity UNKNOWN — route quality unverified")
    elif liquidity < Decimal("10000"):
        risks.append(f"thin liquidity {_money(liquidity)}")

    top10 = vector.get("top10_percent")
    if top10 is None:
        risks.append("holder concentration UNKNOWN")
    elif top10 > Decimal("40"):
        risks.append(f"top-10 concentration {_pct_value(top10)}")

    wash = vector.get("wash_risk")
    if wash is not None and wash > Decimal("0.4"):
        risks.append(f"elevated wash-flow risk {_number(wash)}")

    organic = vector.get("organic_volume_ratio")
    if organic is not None and organic < Decimal("0.5"):
        risks.append(f"only {_percent(organic)} of volume looks organic")

    clusters = vector.get("possible_follow_cluster_count")
    if clusters is not None and clusters > ZERO:
        risks.append(
            f"{_integer(clusters)} possible follow-cluster(s) — buyers may not be independent"
        )

    if vector.completeness < Decimal("0.5"):
        risks.append(
            f"only {_percent(vector.completeness)} of features were computable; "
            "treat this signal as low-confidence"
        )
    if not risks:
        risks.append("no measured risk flags — this is not the same as safe")
    return tuple(risks)


def build_pretrend_card(
    signal: PretrendSignal,
    *,
    referral_code: str | None = None,
    symbol: str = "",
    name: str = "",
) -> CardSpec:
    """The PRE-FOMO TREND SIGNAL card."""

    vector = signal.vector
    horizon = _horizon_label(signal.horizon_seconds)
    title = f"🚨 PRE-FOMO TREND SIGNAL — {_percent(signal.probability)} / {horizon}"

    identity = f"**{name or 'Unknown name'}**"
    if symbol:
        identity += f" / ${symbol}"

    # The decision line carries the base rate and the sample.  Without those a
    # probability is not checkable, and an unchecked probability is a score.
    decision_lines = [
        f"P(enters FOMO Trending within {horizon}) = **{_percent(signal.probability)}**",
        f"Base rate for a comparable token: {_percent(signal.base_rate)}",
        f"Lift over base rate: {_number(signal.lift)}x"
        if signal.lift is not None
        else "Lift over base rate: unknown",
        f"Model {signal.model_version} • features {signal.feature_version} • "
        f"trained on n={signal.sample_support}",
    ]

    fields = [
        CardField(name="TOKEN", value=identity, priority=P_IDENTITY),
        CardField(
            name="TREND ESTIMATE",
            value="\n".join(decision_lines),
            priority=P_DECISION,
        ),
        CardField(
            name="MARKET",
            value=(
                f"MC {_money(vector.get('market_cap_usd'))} • "
                f"LIQ {_money(vector.get('liquidity_usd'))} • "
                f"AGE {_age_text(vector.get('token_age_seconds'))}"
            ),
            priority=P_LIFECYCLE,
        ),
        CardField(
            name="FOMO NATIVE",
            value=(
                f"buyers 1m: {_integer(vector.get('unique_fomo_buyers_1m'))} • "
                f"buyers 3m: {_integer(vector.get('unique_fomo_buyers_3m'))}\n"
                f"new buyers/min: {_number(vector.get('new_fomo_buyer_velocity_1m'), places=4)}\n"
                f"quality buyers 1m: {_integer(vector.get('quality_fomo_buyers_1m'))} • "
                f"independent: {_integer(vector.get('independent_quality_buyers'))}\n"
                f"net FOMO buy 3m: {_money(vector.get('fomo_net_buy_usd_3m'))}\n"
                f"theses 3m: {_integer(vector.get('fomo_thesis_count_3m'))}"
            ),
            priority=P_DEMAND,
        ),
        CardField(
            name="ON-CHAIN",
            value=(
                f"vol 1m {_money(vector.get('volume_usd_level_1m'))} • "
                f"vol 5m {_money(vector.get('volume_usd_level_5m'))}\n"
                f"unique buyers {_integer(vector.get('unique_buyers_level_1m'))} • "
                f"accel {_number(vector.get('unique_buyers_acceleration_1m'), places=4)}\n"
                f"net inflow 3m {_money(vector.get('net_flow_usd_level_3m'))}\n"
                f"holders {_integer(vector.get('holders_level_1m'))} • "
                f"accel {_number(vector.get('holders_acceleration_3m'), places=4)}\n"
                f"top-10 {_pct_value(vector.get('top10_percent'))}"
            ),
            priority=P_MOMENTUM,
        ),
        CardField(
            name="WALLETS",
            value=(
                f"smart wallets: {_integer(vector.get('smart_wallets'))} • "
                f"independent clusters: {_integer(vector.get('independent_wallet_clusters'))}\n"
                f"buyer arrival entropy: {_number(vector.get('buyer_arrival_entropy'), places=3)} "
                f"(1.0 = evenly spread, low = one burst)"
            ),
            priority=P_SMART_MONEY,
        ),
        CardField(
            name="WHY THIS FIRED",
            value="\n".join(_why_it_fired(vector, signal)),
            priority=P_WHY_SURFACED,
        ),
        CardField(
            name="RISKS",
            value="\n".join(f"• {risk}" for risk in _risks(vector)),
            priority=P_WARNINGS,
        ),
        CardField(name="CA", value=f"`{signal.mint}`", priority=P_IDENTITY),
        CardField(
            name="LINKS", value=_links(signal.mint, referral_code), priority=P_LINKS
        ),
    ]

    if signal.notable_actors:
        fields.insert(
            -2,
            CardField(
                name="PRE-TREND AFFINITY (public accounts, measured)",
                value="\n".join(_actor_lines(signal.notable_actors[:5])),
                priority=P_EDGE,
            ),
        )

    social = vector.get("social_quality")
    if social is not None:
        fields.insert(
            -2,
            CardField(
                name="SOCIAL",
                value=(
                    f"exact-CA match: "
                    f"{'yes' if vector.get('exact_ca_social_match') else 'no/unknown'} • "
                    f"quality {_number(social)} • "
                    f"bot risk {_number(vector.get('social_bot_risk'))}"
                ),
                priority=P_SOCIAL,
            ),
        )

    return CardSpec(
        title=title,
        description=(
            "Research signal, shadow mode. No trade was placed and none will be "
            "placed from this lane."
        ),
        compact_description=(
            f"PRE-TREND {_percent(signal.probability)}/{horizon} "
            f"(base {_percent(signal.base_rate)}) `{signal.mint}`"
        ),
        fields=tuple(fields),
        colour=PRE_TREND_COLOUR,
        footer=(
            f"feature completeness {_percent(vector.completeness)} • "
            f"{len(vector.missing)} features UNKNOWN"
        ),
        timestamp=datetime.fromtimestamp(signal.at, tz=UTC),
    )


def _actor_lines(records: Sequence[AffinityRecord]) -> tuple[str, ...]:
    """One line per account, always carrying the sample size.

    These are public accounts with a measured record of being early.  They are
    not insiders and the card does not call them one.
    """

    lines: list[str] = []
    for record in records:
        result = record.primary(300)
        if result is None:
            continue
        lines.append(
            f"• {record.handle or record.actor_id[:10]} — "
            f"adj rate {_percent(result.adjusted_rate)} "
            f"(raw {_percent(result.raw_rate)}, n={result.observations}, "
            f"lift {_number(result.lift)}x, "
            f"median lead {_duration(record.median_lead_seconds)})"
        )
    if not lines:
        lines.append("no account on this token has a statistically meaningful record")
    return tuple(lines)


def build_confirmation_card(
    confirmation: TrendConfirmation, *, referral_code: str | None = None
) -> CardSpec:
    """The NEW FOMO TRENDING ENTRY card — the scoreboard, win or lose."""

    identity = f"**{confirmation.name or 'Unknown name'}**"
    if confirmation.symbol:
        identity += f" / ${confirmation.symbol}"

    if confirmation.predicted:
        verdict_name = "✅ DID WE PREDICT IT? — YES"
        verdict = "\n".join(
            [
                f"First PRE-TREND signal: **{_duration(confirmation.lead_seconds)} early**",
                f"MC at first signal: {_money(confirmation.first_alert_market_cap_usd)}",
                f"MC at Trending entry: {_money(confirmation.market_cap_usd)}",
                f"P at first signal: {_percent(confirmation.first_alert_probability)}",
            ]
        )
        colour = CONFIRMED_COLOUR
    else:
        verdict_name = "❌ DID WE PREDICT IT? — MISSED BY MODEL"
        verdict = (
            "No PRE_TREND alert preceded this entry. Recorded for false-negative "
            "analysis; see /missedtrends."
        )
        colour = MISSED_COLOUR

    fields = [
        CardField(name="TOKEN", value=identity, priority=P_IDENTITY),
        CardField(name="CA", value=f"`{confirmation.mint}`", priority=P_IDENTITY),
        CardField(name=verdict_name, value=verdict, priority=P_DECISION),
        CardField(
            name="AT ENTRY",
            value=(
                f"rank {_or_unknown(confirmation.initial_rank)} • "
                f"tier {confirmation.tier or UNKNOWN}\n"
                f"MC {_money(confirmation.market_cap_usd)} • "
                f"LIQ {_money(confirmation.liquidity_usd)}\n"
                f"VOL {_money(confirmation.volume_usd)} • "
                f"holders {confirmation.holders if confirmation.holders is not None else UNKNOWN}\n"
                f"age {_duration(confirmation.token_age_seconds)}"
            ),
            priority=P_LIFECYCLE,
        ),
        CardField(
            name="LINKS", value=_links(confirmation.mint, referral_code), priority=P_LINKS
        ),
    ]

    return CardSpec(
        title="🔥 NEW FOMO TRENDING ENTRY",
        description="Ground truth. This is the event every prediction is scored against.",
        compact_description=(
            f"TRENDING ENTRY `{confirmation.mint}` "
            + (
                f"predicted {_duration(confirmation.lead_seconds)} early"
                if confirmation.predicted
                else "MISSED"
            )
        ),
        fields=tuple(fields),
        colour=colour,
        timestamp=datetime.fromtimestamp(confirmation.entered_at, tz=UTC),
    )


def render_forensics(report: ForensicReport) -> str:
    """The ``/trendforensics`` text body.

    Rendered as text rather than fields because the value is the timeline, and
    a timeline in an embed field grid is unreadable.
    """

    if report.entry is None:
        return (
            f"No FOMO_TREND_ENTER event is recorded for `{report.mint}`.\n"
            "Either it never entered the board while this collector was running, "
            "or the mint is wrong. Nothing is inferred from a ticker."
        )

    entry = report.entry
    lines = [
        f"**FOMO TREND FORENSICS — `{report.mint}`**",
        "",
        "**FIRST FOMO TRENDING**",
        f"  when: <t:{entry.occurred_at}:f> (epoch {entry.occurred_at})",
        f"  initial rank: {entry.initial_rank if entry.initial_rank is not None else UNKNOWN}",
        f"  tier (raw, uninterpreted): {entry.tier or UNKNOWN}",
        f"  MC: {_money(entry.market_cap_usd)} • LIQ: {_money(entry.liquidity_usd)}",
        f"  age: {_duration(entry.token_age_seconds)}",
        f"  provider: {entry.provider or UNKNOWN} ({entry.source_kind or UNKNOWN})",
        "",
        "**PRE-ENTRY TIMELINE** (reconstructed with the live feature code; "
        "nothing below reads past its own offset)",
        "```",
        f"{'offset':<8}{'MC':>10}{'fomoBuy1m':>11}{'qual':>6}{'indep':>7}"
        f"{'thesis':>8}{'onchain':>9}{'holders':>9}{'complete':>10}",
    ]
    for snapshot in report.snapshots:
        row = snapshot.to_json()
        lines.append(
            f"{snapshot.label:<8}"
            f"{_money(snapshot.value('market_cap_usd')):>10}"
            f"{str(row['fomo_buyers_1m'] or '-'):>11}"
            f"{str(row['quality_fomo_buyers_1m'] or '-'):>6}"
            f"{str(row['independent_quality_buyers'] or '-'):>7}"
            f"{str(row['theses_3m'] or '-'):>8}"
            f"{str(row['onchain_unique_buyers'] or '-'):>9}"
            f"{str(row['holders'] or '-'):>9}"
            f"{str(row['completeness']):>10}"
        )
    lines.append("```")

    lines.append("")
    lines.append("**EARLIEST NOTABLE FOMO ACCOUNTS** (public activity; not insiders)")
    if report.early_actors:
        for actor in report.early_actors[:10]:
            payload = actor.to_json()
            lines.append(
                f"  • {actor.handle or actor.actor_id[:12]} — {actor.event_type} "
                f"{_duration(actor.lead_seconds)} before entry at "
                f"MC {_money(actor.market_cap_at_action_usd)}; "
                f"record n={payload['affinity_observations']}, "
                f"adj rate {payload['affinity_adjusted_rate_5m'] or UNKNOWN}, "
                f"lift {payload['affinity_lift_5m'] or UNKNOWN}"
                + ("" if payload["statistically_meaningful"] else " [sample too thin to rank]")
            )
    else:
        lines.append(
            "  No FOMO-native activity was collected for this mint. "
            "This is an unconfigured provider, not an absence of activity."
        )

    lines.append("")
    lines.append("**WHAT CHANGED BEFORE TRENDING** (T-10m → T0)")
    if report.changes:
        for change in report.changes[:8]:
            if change.ratio is None:
                lines.append(
                    f"  • {change.metric}: {_or_unknown(change.early_value)}"
                    f" → {_or_unknown(change.late_value)}"
                )
            else:
                lines.append(
                    f"  • {change.metric}: {_number(change.early_value)} → "
                    f"{_number(change.late_value)} ({_number(change.ratio)}x)"
                )
    else:
        lines.append("  Not enough offsets were reconstructable to compare.")

    if report.cascade is not None and report.cascade.order:
        lines.append("")
        lines.append("**WHICH SIGNAL MOVED FIRST** (measured, not assumed)")
        for family in report.cascade.order:
            lead = report.cascade.lead(family)
            lines.append(f"  {family}: {_duration(lead)} before entry")

    lines.append("")
    if report.predicted:
        lines.append(
            f"**OUR CALL:** alerted {_duration(report.lead_seconds)} early at "
            f"MC {_money(report.first_alert_market_cap_usd)} with "
            f"P={_percent(report.first_alert_probability)}"
        )
    else:
        lines.append("**OUR CALL:** no PRE_TREND alert preceded this entry.")

    if report.limitations:
        lines.append("")
        lines.append("**LIMITATIONS OF THIS RECONSTRUCTION**")
        for limitation in report.limitations:
            lines.append(f"  • {limitation}")

    return "\n".join(lines)
