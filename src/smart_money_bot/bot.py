from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from urllib.parse import urlencode, urlparse

import discord
from discord import app_commands
from discord.ext import commands
from solders.pubkey import Pubkey

from .config import Settings
from .constants import BOT_VERSION, PAPER_DEMO_ENTRY_PRICE_USD, PAPER_DEMO_MINT
from .engine import SmartMoneyEngine
from .errors import DiscoveryError, JupiterError, PumpLaunchError
from .launch import (
    NO_X_LAUNCH_VERDICT,
    X_VERIFIED_LAUNCH_VERDICT,
    default_launch_draft,
    is_launch_lab_eligible,
    is_manual_launch_opportunity,
    validate_launch_draft,
)
from .models import (
    CoinCallout,
    DetectedSwap,
    DiscoveryCandidate,
    DiscoveryRefresh,
    ExecutionMode,
    ExecutionResult,
    LaunchDraft,
    LaunchOpportunity,
    NarrativePairMatch,
    NewsAlert,
    PaperDailyLockStatus,
    PumpLaunchResult,
    RiskDecision,
    RunnerCandidate,
    Side,
    Signal,
    TokenInfo,
    TrackedTrader,
)
from .quality import STAGE_LABELS, why_surfaced

logger = logging.getLogger(__name__)


def _member_is_admin(user: discord.abc.User, settings: Settings) -> bool:
    if not isinstance(user, discord.Member):
        return False
    if user.guild_permissions.administrator:
        return True
    role_ids = {role.id for role in user.roles}
    return bool(role_ids & settings.discord_admin_role_ids)


def _money(value: Decimal | None) -> str:
    return "unknown" if value is None else f"${value:,.2f}"


def _short(value: str, left: int = 5, right: int = 5) -> str:
    return value if len(value) <= left + right + 3 else f"{value[:left]}…{value[-right:]}"


def _price(value: Decimal | None) -> str:
    return "unavailable" if value is None or value <= 0 else f"${value:,.8f}"


def _return_percent(current_value: Decimal, cost_basis: Decimal) -> Decimal:
    if cost_basis <= 0:
        return Decimal("0")
    return (current_value - cost_basis) / cost_basis * Decimal("100")


def _raw_entry_gate_status(enabled: bool) -> str:
    return "enabled" if enabled else "DISABLED — set PAPER_RAW_ENTRY_FILTER_ENABLED=true"


def _split_discord_text(text: str, *, limit: int = 1900) -> tuple[str, ...]:
    """Split a long ephemeral response at line boundaries under Discord's hard limit."""

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current.rstrip())
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current.rstrip())
            current = line
        else:
            current += line
    if current:
        chunks.append(current.rstrip())
    return tuple(chunks) or ("No status data was produced.",)


FOMO_SOLANA_CHAIN_ID = "1399811149"
PAPER_DEMO_ENTRY_PRICE = Decimal(PAPER_DEMO_ENTRY_PRICE_USD)


def _fomo_coin_url(mint: str, referral_code: str | None = None) -> str:
    query = {
        "address": mint,
        "chainId": FOMO_SOLANA_CHAIN_ID,
    }
    if referral_code:
        query["r"] = referral_code
    query["source"] = "share_link"
    return f"https://fomo.family/coin?{urlencode(query)}"


def _token_view(mint: str, fomo_referral_code: str | None = None) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Open in Fomo",
            style=discord.ButtonStyle.link,
            url=_fomo_coin_url(mint, fomo_referral_code),
            row=0,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Open in Pump.fun",
            style=discord.ButtonStyle.link,
            url=f"https://pump.fun/coin/{mint}",
            row=0,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Buy on Jupiter",
            style=discord.ButtonStyle.link,
            url=f"https://jup.ag/swap/SOL-{mint}",
            row=0,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Sell on Jupiter",
            style=discord.ButtonStyle.link,
            url=f"https://jup.ag/swap/{mint}-SOL",
            row=0,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Chart",
            style=discord.ButtonStyle.link,
            url=f"https://dexscreener.com/solana/{mint}",
            row=1,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Solscan",
            style=discord.ButtonStyle.link,
            url=f"https://solscan.io/token/{mint}",
            row=1,
        )
    )
    return view


# Cache read (5s) plus the bounded live refresh (40s) must both fit inside the
# outer deadline, so a slow stage still produces its own specific message.
FOMO_LAB_CACHE_DEADLINE_SECONDS = 5
FOMO_LAB_REFRESH_DEADLINE_SECONDS = 40
FOMO_LAB_TOTAL_DEADLINE_SECONDS = 60

DISCORD_EMBED_TITLE_LIMIT = 256
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096
DISCORD_EMBED_FIELD_VALUE_LIMIT = 1024
DISCORD_EMBED_TOTAL_LIMIT = 6000
RUNNER_TOKEN_NAME_LIMIT = 80
RUNNER_TOKEN_SYMBOL_LIMIT = 20


def _clamp_embed(embed: discord.Embed) -> discord.Embed:
    """Keep a rendered card inside Discord's documented embed limits.

    Token ``name``/``symbol`` come from on-chain metadata, so an unvetted
    RAW_DISCOVERY/SILENT_WATCH candidate can carry an arbitrarily long name.
    Discord rejects an oversized embed with HTTP 400, which used to escape the
    `/fomo lab` response path and leave the deferred interaction parked on
    "Investing is thinking...". Trimming here keeps the card renderable instead
    of failing the whole interaction.
    """

    if embed.title and len(embed.title) > DISCORD_EMBED_TITLE_LIMIT:
        embed.title = embed.title[:DISCORD_EMBED_TITLE_LIMIT]
    if embed.description and len(embed.description) > DISCORD_EMBED_DESCRIPTION_LIMIT:
        embed.description = embed.description[: DISCORD_EMBED_DESCRIPTION_LIMIT - 1] + "…"
    # Fields are individually capped at the call sites; the running total is
    # not, so drop trailing detail until the whole card fits.
    while len(embed) > DISCORD_EMBED_TOTAL_LIMIT and embed.fields:
        embed.remove_field(len(embed.fields) - 1)
    return embed


def _runner_dex_url(candidate: RunnerCandidate) -> str:
    parsed = urlparse(candidate.pair_url)
    if (
        parsed.scheme == "https"
        and parsed.netloc.casefold() in {"dexscreener.com", "www.dexscreener.com"}
        and parsed.path.casefold().startswith("/solana/")
    ):
        return candidate.pair_url
    return f"https://dexscreener.com/solana/{candidate.mint}"


def _runner_links(candidate: RunnerCandidate, referral_code: str | None) -> str:
    mint = candidate.mint
    return (
        f"[FOMO]({_fomo_coin_url(mint, referral_code)}) • "
        f"[PUMP.FUN](https://pump.fun/coin/{mint}) • "
        f"[DEX]({_runner_dex_url(candidate)}) • "
        f"[SOLSCAN](https://solscan.io/token/{mint})"
    )


class RunnerForensicsButton(discord.ui.Button):
    def __init__(self, bot: SmartMoneyBot, candidate: RunnerCandidate) -> None:
        super().__init__(
            label="FORENSICS",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        self.bot = bot
        self.candidate = candidate

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _member_is_admin(interaction.user, self.bot.settings):
            await interaction.response.send_message(
                "Administrator access is required for the bounded forensic refresh.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        candidate = await self.bot.engine.runner_forensic(self.candidate.mint)
        await interaction.edit_original_response(
            embed=_runner_forensic_embed(candidate),
            view=RunnerAlertView(self.bot, candidate),
        )


class RunnerAlertView(discord.ui.View):
    """Runner-only navigation. It has no buy, sell, launch, sign, or spend control."""

    def __init__(self, bot: SmartMoneyBot, candidate: RunnerCandidate) -> None:
        super().__init__(timeout=900)
        mint = candidate.mint
        for label, url in (
            ("OPEN FOMO", _fomo_coin_url(mint, bot.settings.fomo_referral_code)),
            ("OPEN PUMP", f"https://pump.fun/coin/{mint}"),
            ("DEXSCREENER", _runner_dex_url(candidate)),
            ("SOLSCAN", f"https://solscan.io/token/{mint}"),
        ):
            self.add_item(
                discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.link,
                    url=url,
                    row=0,
                )
            )
        self.add_item(RunnerForensicsButton(bot, candidate))


def _news_lead_view(alert: NewsAlert) -> discord.ui.View:
    query = " ".join(alert.narrative_terms[:3]) or alert.headline[:100]
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Search Matching Coins",
            style=discord.ButtonStyle.link,
            url=f"https://dexscreener.com/search?{urlencode({'q': query})}",
            row=0,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Explore Pump.fun",
            style=discord.ButtonStyle.link,
            url="https://pump.fun/coins",
            row=0,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Create on Pump.fun",
            style=discord.ButtonStyle.link,
            url="https://pump.fun/create",
            row=0,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Search X Live",
            style=discord.ButtonStyle.link,
            url=f"https://x.com/search?{urlencode({'q': query, 'f': 'live'})}",
            row=1,
        )
    )
    if alert.url:
        view.add_item(
            discord.ui.Button(
                label="Original News",
                style=discord.ButtonStyle.link,
                url=alert.url,
                row=1,
            )
        )
    return view


class NewsOpportunityView(discord.ui.View):
    def __init__(self, bot: SmartMoneyBot, opportunity: LaunchOpportunity) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.opportunity = opportunity
        alert = opportunity.alert
        query = " ".join(alert.narrative_terms[:3]) or alert.headline[:100]
        self.add_item(
            discord.ui.Button(
                label="Search Matching Coins",
                style=discord.ButtonStyle.link,
                url=f"https://dexscreener.com/search?{urlencode({'q': query})}",
                row=0,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Explore Pump.fun",
                style=discord.ButtonStyle.link,
                url="https://pump.fun/coins",
                row=0,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Search X Live",
                style=discord.ButtonStyle.link,
                url=f"https://x.com/search?{urlencode({'q': query, 'f': 'live'})}",
                row=1,
            )
        )
        if alert.url:
            self.add_item(
                discord.ui.Button(
                    label="Original News",
                    style=discord.ButtonStyle.link,
                    url=alert.url,
                    row=1,
                )
            )
        launchable = (
            is_manual_launch_opportunity(opportunity)
            and opportunity.score >= bot.settings.pump_launch_min_score
        )
        self.launch_button.disabled = not launchable or not bot.engine.pump_launcher.configured
        if not launchable:
            self.launch_button.label = "Internal research only"
        elif not bot.engine.pump_launcher.configured:
            self.launch_button.label = "One-click launch locked"
        else:
            self.launch_button.label = f"Launch via {bot.engine.pump_launcher.provider}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _member_is_admin(interaction.user, self.bot.settings):
            return True
        await interaction.response.send_message(
            "Only a configured bot administrator can launch from an alert.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="Launch on Pump.fun",
        style=discord.ButtonStyle.danger,
        row=2,
        custom_id="smartmoney:launch-news-coin:v1",
    )
    async def launch_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer()
        button.disabled = True
        button.label = "Launch submitted…"
        if interaction.message:
            with suppress(discord.HTTPException):
                await interaction.message.edit(view=self)
        result = await self.bot.engine.launch_news_opportunity(
            self.opportunity,
            requested_by=str(interaction.user.id),
        )
        result_embed = _pump_launch_result_embed(
            result,
            self.bot.settings.fomo_referral_code,
        )
        await interaction.followup.send(embed=result_embed, ephemeral=True)
        if result.success:
            await self.bot._send_alert(
                result_embed,
                token_mint=result.mint,
                ping_user=True,
            )
            button.label = "Launched"
        else:
            button.label = f"Launch {result.status.lower()}"[:80]
        if interaction.message:
            with suppress(discord.HTTPException):
                await interaction.message.edit(view=self)

    @discord.ui.button(
        label="Ignore",
        style=discord.ButtonStyle.secondary,
        row=2,
        custom_id="smartmoney:ignore-news-coin:v1",
    )
    async def ignore_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.launch_button.disabled = True
        button.disabled = True
        button.label = "Ignored"
        await interaction.response.send_message(
            "Candidate ignored. No launch was submitted.",
            ephemeral=True,
        )
        if interaction.message:
            with suppress(discord.HTTPException):
                await interaction.message.edit(view=self)


def _launch_readiness_embed(report: dict[str, object]) -> discord.Embed:
    ready = bool(report["overall_ready"])
    wallet = str(report["wallet"] or "NOT CONFIGURED / INVALID")
    balance = report["wallet_balance"]
    balance_text = f"{Decimal(str(balance)):.6f} SOL" if balance is not None else "UNAVAILABLE"
    embed = discord.Embed(
        title="J7 LAUNCH READINESS",
        color=0x2ECC71 if ready else 0xE74C3C,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Bot", value=f"v{report['bot_version']}")
    embed.add_field(name="Provider", value="J7 Tracker")
    embed.add_field(
        name="J7 configuration",
        value="READY" if report["j7_configured"] else "NOT READY",
    )
    embed.add_field(
        name="J7 API key",
        value="CONFIGURED" if report["j7_api_key_configured"] else "MISSING",
    )
    embed.add_field(
        name="J7 session",
        value="CONFIGURED" if report["j7_session_configured"] else "MISSING",
    )
    embed.add_field(name="J7 region", value=str(report["j7_region"]))
    embed.add_field(name="J7 endpoint", value=str(report["j7_endpoint"]))
    embed.add_field(name="Pinata/IPFS", value=str(report["pinata"]))
    embed.add_field(name="Launch wallet", value=f"`{_short(wallet)}`")
    embed.add_field(name="Wallet SOL", value=balance_text)
    embed.add_field(name="Creator buy", value=f"{report['creator_buy']} SOL")
    embed.add_field(
        name="Launches today",
        value=f"{report['launches_today']} / {report['launches_limit']}",
    )
    embed.add_field(
        name="SOL used today",
        value=f"{report['sol_today']} / {report['sol_limit']}",
    )
    embed.add_field(name="Duplicate protection", value=str(report["duplicate_protection"]))
    embed.add_field(
        name="Launch reservations",
        value=(
            f"{report['reservations']} • pending {report['pending_reservations']} • "
            f"unknown {report['unknown_results']}"
        ),
        inline=False,
    )
    stats = report["candidate_stats"]
    if isinstance(stats, dict):
        embed.add_field(
            name="Launch Lab observations today",
            value=(
                f"evaluated `{stats.get('evaluated', 0)}` • highest `{stats.get('highest', 0)}/100`"
            ),
            inline=False,
        )
    x_budget = report.get("x_budget")
    if isinstance(x_budget, dict):
        embed.add_field(
            name="Targeted X budget",
            value=(
                f"{'READY' if x_budget.get('guard_enabled') else 'GUARD DISABLED'} • "
                f"{x_budget.get('verifications', 0)}/{x_budget.get('verification_limit', 0)} "
                f"checks • requests {x_budget.get('requests', 0)} • "
                f"Posts {x_budget.get('post_resources', 0)} • "
                f"Users {x_budget.get('user_resources', 0)} • local estimate "
                f"${x_budget.get('estimated_spend_today', 0)}/"
                f"${x_budget.get('daily_budget', 0)}"
            ),
            inline=False,
        )
    last_lab = f"<t:{report['last_lab_candidate']}:R>" if report["last_lab_candidate"] else "none"
    last_pinata = (
        f"<t:{report['last_pinata_success']}:R>" if report["last_pinata_success"] else "none"
    )
    last_attempt = (
        f"<t:{report['last_launch_attempt']}:R>" if report["last_launch_attempt"] else "none"
    )
    last_mint = (
        f"`{_short(str(report['last_successful_mint']))}`"
        if report["last_successful_mint"]
        else "none"
    )
    embed.add_field(
        name="Recent launch state",
        value=(
            f"Last Lab candidate: {last_lab}\n"
            f"Last Pinata success: {last_pinata}\n"
            f"Last launch attempt: {last_attempt}\n"
            f"Last successful mint: {last_mint}"
        ),
        inline=False,
    )
    failures = tuple(report["failures"])
    embed.add_field(
        name="Overall",
        value=(
            "**READY FOR CONTROLLED LIVE LAUNCH**"
            if ready
            else "**NOT READY**\n" + "\n".join(f"• {item}" for item in failures)
        ),
        inline=False,
    )
    embed.set_footer(text="Read-only check • no J7 submit call • no SOL spent • secrets hidden")
    return embed


def _launch_lab_embed(
    draft: LaunchDraft,
    *,
    index: int,
    total: int,
    settings: Settings,
    wallet: str | None,
    balance: Decimal | None,
    research_test: bool = False,
    production_eligible: bool = True,
    x_budget: dict[str, object] | None = None,
) -> discord.Embed:
    opportunity = draft.opportunity
    now = int(time.time())
    age = max(0, now - opportunity.alert.created_at) if opportunity.alert.created_at else None
    competition = opportunity.competition
    strongest = (
        "none"
        if competition.matching_pairs == 0
        else (
            f"{competition.strongest_symbol or 'unknown'} • "
            f"{_money(competition.strongest_liquidity_usd)} liquidity"
        )
    )
    positives = opportunity.positives[:5] or ("credible current source",)
    weaknesses = (
        opportunity.blockers
        + tuple(item for item in opportunity.warnings if "stricter free" not in item)
    )[:6] or ("No material weakness recorded by the current checks",)
    x = opportunity.x_evidence
    if x.available:
        x_label = (
            "X VERIFIED — STRONG SIGNAL"
            if opportunity.x_verified and opportunity.crypto_attention_ready
            else "X CHECKED — WEAK SIGNAL"
        )
        free_score = opportunity.pre_x_score or max(0, opportunity.score - opportunity.x_score)
        impact = opportunity.score - free_score
        notable = ", ".join(x.notable_accounts[:3]) or "none"
        post_links = (
            " • ".join(
                f"[post {index + 1}]({url})" for index, url in enumerate(x.notable_posts[:3])
            )
            or "none"
        )
        x_detail = (
            f"**{x_label}**\nPosts: `{x.posts}` • unique authors: `{x.unique_authors}`\n"
            f"Credible crypto accounts: `{x.credible_crypto_authors}` • "
            f"trusted: `{x.trusted_crypto_authors}`\n"
            f"Velocity: `{x.posts_per_minute}/min` • engagement: `{x.engagements}` • "
            f"duplicate text: `{x.duplicate_percent}%`\n"
            f"Notable accounts: {notable}\nNotable posts: {post_links}\n"
            f"Confidence impact: `{impact:+d}`"
        )
    else:
        x_label = "X NOT VERIFIED"
        x_detail = f"**{x_label}**\n{x.error or 'Targeted X verification has not run.'}"
    title = "🧪 CONTROLLED PIPELINE TEST" if research_test else "🔥 LIVE LAUNCH LAB"
    publication = (
        f"<t:{opportunity.alert.created_at}:F> • <t:{opportunity.alert.created_at}:R>"
        if opportunity.alert.created_at
        else "unavailable from source metadata"
    )
    detection_delay = (
        max(0, opportunity.alert.received_at - opportunity.alert.created_at)
        if opportunity.alert.received_at and opportunity.alert.created_at
        else None
    )
    research_prefix = "**RESEARCH ONLY**\n\n" if research_test else ""
    embed = discord.Embed(
        title=title,
        description=(
            f"{research_prefix}**TOPIC**\n{opportunity.alert.headline}\n\n"
            f"Candidate `{index + 1}/{total}` • age "
            f"`{f'{age}s' if age is not None else 'unknown'}`\n"
            f"**Published:** {publication}"
            + (
                f"\n**Detection delay:** `{detection_delay}s`"
                if detection_delay is not None
                else ""
            )
        ),
        color=0xF39C12,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Category", value=opportunity.category)
    embed.add_field(name="Source", value=opportunity.alert.source or "unknown")
    embed.add_field(
        name="Candidate Score",
        value=(
            f"**Actual score: {opportunity.score}/100**\n"
            f"Normal Launch Lab floor: **{settings.launch_lab_min_score}/100**\n"
            f"Automatic NO-X floor: **{settings.no_x_launch_min_score}/100**"
        ),
    )
    embed.add_field(
        name="SCORE BREAKDOWN",
        value=(
            f"Source `{opportunity.source_score}/15` • speed `{opportunity.speed_score}/15`\n"
            f"Meme `{opportunity.viral_score}/25` • X `{opportunity.x_score}/15`\n"
            f"Confirmation `{opportunity.confirmation_score}/10` • "
            f"competition `{opportunity.competition_score}/10` • "
            f"identity `{opportunity.identity_score}/10`"
        ),
        inline=False,
    )
    embed.add_field(
        name="WHY IT COULD WORK",
        value="\n".join(f"• {item}" for item in positives),
        inline=False,
    )
    embed.add_field(
        name="WEAKNESSES",
        value="\n".join(f"• {item}" for item in weaknesses),
        inline=False,
    )
    embed.add_field(
        name="COMPETITION",
        value=(
            f"Matching concepts: `{competition.matching_pairs}`\n"
            f"Strong competing token: {strongest}"
        ),
        inline=False,
    )
    embed.add_field(
        name="COIN IDEA",
        value=(
            f"**Name:** {draft.name}\n**Ticker:** `${draft.symbol}`\n"
            f"**Description:** {draft.description[:500]}"
        ),
        inline=False,
    )
    embed.add_field(
        name="X VERIFICATION",
        value=x_detail,
        inline=False,
    )
    if x_budget is not None:
        embed.add_field(
            name="X LOCAL BUDGET ACCOUNTING",
            value=(
                f"Checks: `{x_budget.get('verifications', 0)}` / "
                f"`{x_budget.get('verification_limit', 0)}` • "
                f"requests: `{x_budget.get('requests', 0)}` / "
                f"`{x_budget.get('request_limit', 0)}`\n"
                f"Posts recorded: `{x_budget.get('post_resources', 0)}` • "
                f"users recorded: `{x_budget.get('user_resources', 0)}`\n"
                f"Local estimate: `${x_budget.get('estimated_spend_today', 0)}` / "
                f"`${x_budget.get('daily_budget', 0)}` today"
            ),
            inline=False,
        )
    embed.add_field(
        name="Launch controls",
        value=(
            "**Provider:** J7 Tracker\n"
            f"**Creator Buy:** {draft.creator_buy_sol} SOL\n"
            f"**Wallet:** `{_short(wallet or 'not configured')}`\n"
            f"**Balance:** {f'{balance:.6f} SOL' if balance is not None else 'unavailable'}\n"
            f"**Daily Launch Limit:** {settings.pump_launch_max_per_day}\n"
            f"**Daily SOL Limit:** {settings.pump_launch_max_sol_per_day} SOL\n"
            f"**Live eligibility:** "
            f"{'QUALIFIED' if production_eligible else 'LOCKED — RESEARCH ONLY'}"
        ),
        inline=False,
    )
    if opportunity.alert.url:
        embed.add_field(
            name="Original evidence",
            value=f"[Open legitimate source]({opportunity.alert.url})",
            inline=False,
        )
    embed.set_image(url="attachment://launch-preview.png")
    embed.set_footer(
        text=(
            "Research display only • no J7 submit • no reservation • no SOL spent"
            if research_test and not production_eligible
            else "Preview only • no SOL spent until final confirmation • never promises profit"
        )
    )
    return embed


class LaunchLabEditModal(discord.ui.Modal, title="Edit J7 launch draft"):
    def __init__(self, view: LaunchLabView) -> None:
        super().__init__(timeout=300)
        self.lab_view = view
        draft = view.draft
        self.name_input = discord.ui.TextInput(label="Name", default=draft.name, max_length=32)
        self.symbol_input = discord.ui.TextInput(
            label="Ticker", default=draft.symbol, min_length=2, max_length=10
        )
        self.description_input = discord.ui.TextInput(
            label="Description",
            default=draft.description[:500],
            style=discord.TextStyle.paragraph,
            max_length=500,
        )
        self.buy_input = discord.ui.TextInput(
            label="Creator buy (SOL)",
            default=str(draft.creator_buy_sol),
            max_length=16,
        )
        links = " | ".join(item for item in (draft.website_url, draft.x_url) if item)
        self.links_input = discord.ui.TextInput(
            label="Website | X URL",
            default=links[:400],
            required=False,
            max_length=400,
        )
        for item in (
            self.name_input,
            self.symbol_input,
            self.description_input,
            self.buy_input,
            self.links_input,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        links = [item.strip() for item in str(self.links_input).split("|") if item.strip()]
        website = next(
            (item for item in links if "x.com/" not in item and "twitter.com/" not in item),
            "",
        )
        x_url = next(
            (item for item in links if "x.com/" in item or "twitter.com/" in item),
            "",
        )
        try:
            updated = validate_launch_draft(
                replace(
                    self.lab_view.draft,
                    name=str(self.name_input),
                    symbol=str(self.symbol_input),
                    description=str(self.description_input),
                    creator_buy_sol=Decimal(str(self.buy_input)),
                    website_url=website,
                    x_url=x_url,
                ),
                maximum_buy_sol=self.lab_view.bot.settings.pump_launch_initial_buy_sol,
            )
        except (PumpLaunchError, ArithmeticError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.lab_view.drafts[self.lab_view.index] = updated
        await self.lab_view.refresh_message(interaction)


class LaunchConfirmationView(discord.ui.View):
    def __init__(self, lab_view: LaunchLabView) -> None:
        super().__init__(timeout=300)
        self.lab_view = lab_view
        self.submitting = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.lab_view.interaction_check(interaction)

    @discord.ui.button(label="CONFIRM REAL LAUNCH", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.submitting:
            await interaction.response.send_message(
                "Launch is already being submitted.", ephemeral=True
            )
            return
        self.submitting = True
        button.disabled = True
        await interaction.response.defer()
        await interaction.edit_original_response(view=self)
        result = await self.lab_view.bot.engine.launch_lab_draft(
            self.lab_view.draft,
            requested_by=str(interaction.user.id),
        )
        result_embed = _pump_launch_result_embed(
            result,
            self.lab_view.bot.settings.fomo_referral_code,
        )
        await interaction.edit_original_response(
            embed=result_embed,
            attachments=[],
            view=_launch_result_view(
                result,
                self.lab_view.bot.settings.fomo_referral_code,
            ),
        )
        if result.success:
            await self.lab_view.bot._send_alert(
                result_embed,
                token_mint=result.mint,
                ping_user=True,
            )

    @discord.ui.button(label="CANCEL", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Real launch canceled. No SOL was spent.",
            embed=None,
            attachments=[],
            view=None,
        )


class XVerificationConfirmationView(discord.ui.View):
    def __init__(self, lab_view: LaunchLabView) -> None:
        super().__init__(timeout=300)
        self.lab_view = lab_view
        self.running = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.lab_view.interaction_check(interaction)

    @discord.ui.button(label="RUN X VERIFICATION", style=discord.ButtonStyle.success)
    async def run(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.running:
            await interaction.response.send_message(
                "X verification is already running.", ephemeral=True
            )
            return
        self.running = True
        button.disabled = True
        await interaction.response.defer()
        updated = await self.lab_view.bot.engine.verify_launch_lab_candidate(
            self.lab_view.draft.opportunity,
            research_test=self.lab_view.research_test,
        )
        self.lab_view.drafts[self.lab_view.index] = replace(
            self.lab_view.draft,
            opportunity=updated,
        )
        self.lab_view.sync_controls()
        embed, file = await self.lab_view.preview()
        await interaction.edit_original_response(
            embed=embed,
            attachments=[file],
            view=self.lab_view,
        )

    @discord.ui.button(label="CANCEL", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.lab_view.refresh_message(interaction)


class LaunchLabView(discord.ui.View):
    def __init__(
        self,
        bot: SmartMoneyBot,
        opportunities: tuple[LaunchOpportunity, ...],
        *,
        owner_id: int,
        balance: Decimal | None,
        research_test: bool = False,
    ) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.opportunities = opportunities
        self.owner_id = owner_id
        self.balance = balance
        self.research_test = research_test
        self.index = 0
        self.drafts = [
            default_launch_draft(item, bot.settings.pump_launch_initial_buy_sol)
            for item in opportunities
        ]
        self.sync_controls()

    @property
    def draft(self) -> LaunchDraft:
        return self.drafts[self.index]

    @property
    def production_eligible(self) -> bool:
        return is_launch_lab_eligible(
            self.draft.opportunity,
            minimum_score=self.bot.settings.launch_lab_min_score,
            max_age_seconds=self.bot.settings.launch_lab_max_age_seconds,
        )

    def sync_controls(self) -> None:
        locked = self.research_test and not self.production_eligible
        self.launch.disabled = locked
        self.launch.label = "J7 LAUNCH LOCKED — RESEARCH ONLY" if locked else "LAUNCH VIA J7"
        self.launch.style = discord.ButtonStyle.secondary if locked else discord.ButtonStyle.danger
        self.verify_x.label = "TEST X VERIFY" if self.research_test else "X VERIFY"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id and _member_is_admin(
            interaction.user, self.bot.settings
        ):
            return True
        await interaction.response.send_message(
            "Only the administrator who opened this Launch Lab can use these controls.",
            ephemeral=True,
        )
        return False

    async def preview(self) -> tuple[discord.Embed, discord.File]:
        art = await self.bot.engine.pump_launcher.j7.render_draft_art(self.draft)
        try:
            x_budget = await self.bot.engine.x_budget.status()
        except (AttributeError, RuntimeError):
            x_budget = None
        embed = _launch_lab_embed(
            self.draft,
            index=self.index,
            total=len(self.drafts),
            settings=self.bot.settings,
            wallet=self.bot.engine.pump_launcher.j7.wallet_address,
            balance=self.balance,
            research_test=self.research_test,
            production_eligible=self.production_eligible,
            x_budget=x_budget,
        )
        return embed, discord.File(io.BytesIO(art), filename="launch-preview.png")

    async def refresh_message(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        embed, file = await self.preview()
        # Ephemeral component responses do not expose an editable channel message.
        # After a deferred component/modal response, Discord requires editing the
        # interaction's original webhook response.
        await interaction.edit_original_response(
            embed=embed,
            attachments=[file],
            view=self,
        )

    @discord.ui.button(label="LAUNCH VIA J7", style=discord.ButtonStyle.danger, row=0)
    async def launch(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if not self.production_eligible:
            await interaction.response.send_message(
                "RESEARCH ONLY — this item does not satisfy the normal Launch Lab "
                "rules. J7 was not called, no launch was reserved, and no SOL was spent.",
                ephemeral=True,
            )
            return
        draft = self.draft
        embed = discord.Embed(
            title="THIS CREATES A REAL PUBLIC TOKEN AND SPENDS REAL SOL",
            description=(
                f"**Name:** {draft.name}\n**Ticker:** `${draft.symbol}`\n"
                f"**Creator buy:** {draft.creator_buy_sol} SOL\n**Provider:** J7 Tracker"
            ),
            color=0xE74C3C,
        )
        await interaction.response.edit_message(
            embed=embed,
            attachments=[],
            view=LaunchConfirmationView(self),
        )

    @discord.ui.button(label="EDIT", style=discord.ButtonStyle.primary, row=0)
    async def edit(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(LaunchLabEditModal(self))

    @discord.ui.button(label="REGENERATE ART", style=discord.ButtonStyle.primary, row=0)
    async def regenerate(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.drafts[self.index] = replace(
            self.draft,
            art_variant=self.draft.art_variant + 1,
        )
        await self.refresh_message(interaction)

    @discord.ui.button(label="X VERIFY", style=discord.ButtonStyle.success, row=1)
    async def verify_x(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if not self.bot.settings.x_paid_search_enabled:
            await interaction.response.send_message(
                "X verification is disabled. Free Launch Lab and J7 remain active.",
                ephemeral=True,
            )
            return
        if not self.bot.settings.x_api_bearer_token:
            await interaction.response.send_message(
                "X_API_BEARER_TOKEN is not configured. Free Launch Lab remains active.",
                ephemeral=True,
            )
            return
        budget = await self.bot.engine.x_budget.status()
        ceiling = self.bot.settings.x_verify_max_posts * self.bot.settings.x_estimated_post_read_usd
        test_prefix = (
            "This is a manual test X lookup. "
            f"Up to {self.bot.settings.x_verify_max_posts} Posts may be read.\n\n"
            if self.research_test
            else ""
        )
        embed = discord.Embed(
            title="TEST X VERIFICATION" if self.research_test else "X VERIFICATION",
            description=(
                f"{test_prefix}**Candidate:** "
                f"{self.draft.opportunity.alert.headline[:220]}\n"
                f"**Free score:** {self.draft.opportunity.score}/100\n"
                f"**Search limit:** up to {self.bot.settings.x_verify_max_posts} Posts\n"
                f"**Estimated Post-read ceiling:** approximately ${ceiling:.3f}\n"
                "User resources are separately estimated only if Post-level evidence "
                "justifies author hydration.\n\n"
                f"**Local estimate today:** ${budget['estimated_spend_today']} / "
                f"${budget['daily_budget']}\n"
                f"**Targeted verifications:** {budget['verifications']} / "
                f"{budget['verification_limit']}"
            ),
            color=0x1DA1F2,
        )
        embed.set_footer(text="Official X API • local estimate, not the X invoice • never calls J7")
        confirmation = XVerificationConfirmationView(self)
        if self.research_test:
            confirmation.children[0].label = "RUN TEST X VERIFY"
        await interaction.response.edit_message(
            embed=embed,
            attachments=[],
            view=confirmation,
        )

    @discord.ui.button(label="NEXT CANDIDATE", style=discord.ButtonStyle.secondary, row=1)
    async def next_candidate(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.index = (self.index + 1) % len(self.drafts)
        self.sync_controls()
        await self.refresh_message(interaction)

    @discord.ui.button(label="CANCEL", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Launch Lab closed. No SOL was spent.",
            embed=None,
            attachments=[],
            view=None,
        )


class RunnerXVerificationConfirmationView(discord.ui.View):
    def __init__(self, lab_view: FomoRunnerLabView) -> None:
        super().__init__(timeout=300)
        self.lab_view = lab_view
        self.running = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.lab_view.interaction_check(interaction)

    @discord.ui.button(label="RUN TARGETED X VERIFY", style=discord.ButtonStyle.success)
    async def run(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.running:
            await interaction.response.send_message(
                "Runner X verification is already running.", ephemeral=True
            )
            return
        self.running = True
        button.disabled = True
        await interaction.response.defer(ephemeral=True, thinking=True)
        updated = await self.lab_view.bot.engine.verify_runner_x(self.lab_view.candidate)
        self.lab_view.candidates[self.lab_view.index] = updated
        self.lab_view.sync_links()
        await interaction.edit_original_response(embed=self.lab_view.embed(), view=self.lab_view)

    @discord.ui.button(label="CANCEL", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.lab_view.refresh_message(interaction, fetch=False)


class FomoRunnerLabView(discord.ui.View):
    """Existing-token shadow research controls; no control can buy or call J7."""

    def __init__(
        self,
        bot: SmartMoneyBot,
        candidates: tuple[RunnerCandidate, ...],
        *,
        owner_id: int,
        research_test: bool,
    ) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.candidates = list(candidates)
        self.owner_id = owner_id
        self.research_test = research_test
        self.index = 0
        self.fomo_link = discord.ui.Button(label="OPEN FOMO", style=discord.ButtonStyle.link, row=1)
        self.pump_link = discord.ui.Button(label="OPEN PUMP", style=discord.ButtonStyle.link, row=1)
        self.dex_link = discord.ui.Button(
            label="DEXSCREENER", style=discord.ButtonStyle.link, row=1
        )
        self.solscan_link = discord.ui.Button(
            label="SOLSCAN", style=discord.ButtonStyle.link, row=1
        )
        for item in (self.fomo_link, self.pump_link, self.dex_link, self.solscan_link):
            self.add_item(item)
        self.sync_links()

    @property
    def candidate(self) -> RunnerCandidate:
        return self.candidates[self.index]

    def sync_links(self) -> None:
        mint = self.candidate.mint
        self.fomo_link.url = _fomo_coin_url(mint, self.bot.settings.fomo_referral_code)
        self.pump_link.url = f"https://pump.fun/coin/{mint}"
        self.dex_link.url = _runner_dex_url(self.candidate)
        self.solscan_link.url = f"https://solscan.io/token/{mint}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id and _member_is_admin(
            interaction.user, self.bot.settings
        ):
            return True
        await interaction.response.send_message(
            "Only the administrator who opened this Fomo Runner Lab can use it.",
            ephemeral=True,
        )
        return False

    def embed(self) -> discord.Embed:
        return _runner_embed(
            self.candidate,
            index=self.index,
            total=len(self.candidates),
            research_test=self.research_test,
            fomo_referral_code=self.bot.settings.fomo_referral_code,
        )

    async def refresh_message(
        self,
        interaction: discord.Interaction,
        *,
        fetch: bool,
    ) -> None:
        await interaction.response.defer()
        if fetch:
            self.candidates[self.index] = await self.bot.engine.analyze_runner(
                self.candidate.mint,
                refresh_market=True,
                allow_automatic_x=False,
            )
        self.sync_links()
        await interaction.edit_original_response(embed=self.embed(), view=self)

    @discord.ui.button(label="NEXT CANDIDATE", style=discord.ButtonStyle.primary, row=0)
    async def next_candidate(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.index = (self.index + 1) % len(self.candidates)
        self.sync_links()
        await self.refresh_message(interaction, fetch=False)

    @discord.ui.button(label="REFRESH", style=discord.ButtonStyle.primary, row=0)
    async def refresh(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.refresh_message(interaction, fetch=True)

    @discord.ui.button(label="VERIFY ON X", style=discord.ButtonStyle.success, row=0)
    async def verify_x(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if not self.bot.settings.x_paid_search_enabled:
            await interaction.response.send_message(
                "Official X verification is disabled; free runner research remains active.",
                ephemeral=True,
            )
            return
        if not self.bot.settings.x_api_bearer_token:
            await interaction.response.send_message(
                "X_API_BEARER_TOKEN is not configured; no request was made.",
                ephemeral=True,
            )
            return
        budget = await self.bot.engine.x_budget.status()
        ceiling = self.bot.settings.x_verify_max_posts * self.bot.settings.x_estimated_post_read_usd
        embed = discord.Embed(
            title="TARGETED FOMO RUNNER X VERIFICATION",
            description=(
                f"Exact contract: `{self.candidate.mint}`\n"
                f"Up to `{self.bot.settings.x_verify_max_posts}` Posts may be read.\n"
                f"Estimated Post-read ceiling: approximately `${ceiling:.3f}`.\n"
                f"Local estimate today: `${budget['estimated_spend_today']}` / "
                f"`${budget['daily_budget']}`.\n\n"
                "This uses the same central Launch Lab/manual-X budget. It never buys "
                "the token and never calls J7."
            ),
            color=0x1DA1F2,
        )
        await interaction.response.edit_message(
            embed=embed,
            view=RunnerXVerificationConfirmationView(self),
        )

    @discord.ui.button(label="CLOSE", style=discord.ButtonStyle.secondary, row=0)
    async def close_lab(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Fomo Runner Lab closed. No SOL was spent and no token was bought.",
            embed=None,
            view=None,
        )

    @discord.ui.button(label="FORENSICS", style=discord.ButtonStyle.secondary, row=1)
    async def forensics(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        self.candidates[self.index] = await self.bot.engine.runner_forensic(
            self.candidate.mint
        )
        self.sync_links()
        await interaction.edit_original_response(
            embed=_runner_forensic_embed(self.candidate),
            view=self,
        )


def _discovery_lines(candidates: tuple[DiscoveryCandidate, ...] | list[DiscoveryCandidate]) -> str:
    lines: list[str] = []
    for item in candidates[:10]:
        # Incomplete provider rows are nomination-only evidence. They must never
        # appear as an automatically copied wallet or as an "unavailable" metric.
        if item.metrics_limited_24h or item.metrics_limited_7d:
            continue
        momentum = item.pnl_momentum_usd
        momentum_text = "new" if momentum is None else f"{momentum:+,.2f} since refresh"
        roi_24h = f"{item.roi_24h_percent:.1f}%"
        roi_7d = f"{item.roi_7d_percent:.1f}%"
        win_24h = f"{item.win_rate_percent:.1f}%"
        win_7d = f"{item.win_rate_7d_percent:.1f}%"
        lines.append(
            f"**{item.rank}. {item.alias}** • `{_short(item.address)}`\n"
            f"24H `{_money(item.realized_pnl_24h)}` / `{roi_24h}` ROI • "
            f"7D `{_money(item.realized_pnl_7d)}` / `{roi_7d}` ROI\n"
            f"win `24H {win_24h}` / `7D {win_7d}` • "
            f"recent `{item.recent_swaps}` • Pump `{item.pump_swaps}` • "
            f"score `{item.score}` • momentum `{momentum_text}`\n"
            f"why: {item.selection_reason or 'strict dual-window evidence'}"
        )
    return "\n\n".join(lines) or "No qualified wallets in the latest snapshot."


def _coin_callout_embed(callout: CoinCallout) -> discord.Embed:
    colors = {
        "VERIFIED TREND": 0x00D084,
        "DEVELOPING — NOT PUBLIC": 0xF1C40F,
        "INCOMPLETE — NOT PUBLIC": 0x95A5A6,
        "BLOCKED": 0xE74C3C,
    }
    label = callout.symbol or _short(callout.mint)
    embed = discord.Embed(
        title=f"COIN CALLOUT • {callout.verdict} • {label}",
        description=(
            f"Evidence score **{callout.score}/100** • confidence **{callout.confidence}**\n"
            + (
                "This passed the exact-contract, market, X-account, rug-risk, and "
                "executable-route gates. This is evidence—not a profit promise."
                if callout.public_alert_eligible
                else "This is research evidence—not a buy signal or a profit promise."
            )
        ),
        color=colors.get(callout.verdict, 0x95A5A6),
        timestamp=datetime.fromtimestamp(callout.generated_at, tz=UTC),
    )
    embed.add_field(
        name="Verified smart money",
        value=(
            f"{len(callout.smart_wallets)} independent buyer(s) in the live window\n"
            + (", ".join(callout.smart_wallets)[:800] if callout.smart_wallets else "None yet")
        ),
        inline=False,
    )
    dex = callout.dex
    if dex.available:
        total_5m = dex.buys_5m + dex.sells_5m
        price_change = (
            dex.price_change_5m_percent if dex.price_change_5m_percent is not None else "unknown"
        )
        embed.add_field(
            name="Verified market activity",
            value=(
                f"Reported liquidity `{_money(dex.liquidity_usd)}` • market cap "
                f"`{_money(dex.market_cap_usd)}`\n"
                f"5m buys/sells `{dex.buys_5m}/{dex.sells_5m}` ({total_5m} tx) • "
                f"trading volume `{_money(dex.volume_5m_usd)}`\n"
                f"5m price `{price_change}%`\n"
                "Market cap/volume are **not** dollars invested; liquidity can still move."
            ),
            inline=False,
        )
        if dex.x_handle:
            embed.add_field(
                name="Token community link",
                value=(
                    f"[Open @{dex.x_handle} on X](https://x.com/{dex.x_handle}) • "
                    "profile link found in public token metadata; tweet views and "
                    "engagement are not verified in zero-cost mode"
                ),
                inline=False,
            )
    else:
        embed.add_field(name="DEX flow", value="Official pair data unavailable", inline=False)
    social = callout.social
    if social.available:
        embed.add_field(
            name="Crypto X promotion",
            value=(
                f"Posts `{social.posts}` • contract `{social.contract_posts}` • "
                f"promotion `{social.promoter_posts}`\n"
                f"Unique authors `{social.unique_authors}` • "
                f"established `{social.established_authors}` • "
                f"crypto-native `{social.crypto_authors}` • credible crypto "
                f"`{social.credible_crypto_authors}`\n"
                f"Exact-contract authors `{social.contract_authors}` • credible exact-contract "
                f"authors `{social.credible_contract_authors}` • trusted crypto "
                f"`{social.trusted_crypto_authors}` • million-follower "
                f"`{social.million_follower_authors}`\n"
                f"Velocity `{social.posts_per_minute}/min` • engagements `{social.engagements}` • "
                f"duplicate text `{social.duplicate_percent}%`"
            ),
            inline=False,
        )
        if social.notable_accounts:
            embed.add_field(
                name="Notable crypto accounts pushing it",
                value=" • ".join(f"`{item}`" for item in social.notable_accounts)[:1024],
                inline=False,
            )
        if social.notable_posts:
            embed.add_field(
                name="Open the X posts",
                value=" • ".join(
                    f"[Post {index}]({url})"
                    for index, url in enumerate(social.notable_posts, start=1)
                )[:1024],
                inline=False,
            )
    else:
        error = social.error or "not configured or no response"
        embed.add_field(
            name="X/Twitter evidence",
            value=f"Not scored—`{error[:180]}`",
            inline=False,
        )
    tracker = callout.tracker_risk
    if tracker.available:
        risk_score = tracker.score if tracker.score is not None else "unknown"
        bundled = tracker.bundlers_percent if tracker.bundlers_percent is not None else "unknown"
        insiders = tracker.insiders_percent if tracker.insiders_percent is not None else "unknown"
        snipers = tracker.snipers_percent if tracker.snipers_percent is not None else "unknown"
        embed.add_field(
            name="Rug / launch manipulation",
            value=(
                f"Risk `{risk_score}/10` • "
                f"rugged `{'yes' if tracker.rugged else 'no'}`\n"
                f"Bundlers `{bundled}%` • insiders `{insiders}%` • snipers `{snipers}%`"
            ),
            inline=False,
        )
    else:
        risk_error = tracker.error or "token-risk evidence unavailable"
        embed.add_field(
            name="Rug / launch manipulation",
            value=f"Solana Tracker `{risk_error[:180]}`.",
            inline=False,
        )
    if callout.executable_quote is not None:
        quote = callout.executable_quote
        embed.add_field(
            name="$5 executable route check",
            value=(
                f"Jupiter route `{quote.router}` • price impact "
                f"`{quote.price_impact_percent:.2f}%` • quoted output `{quote.output_amount}`"
            ),
            inline=False,
        )
    if callout.positives:
        embed.add_field(
            name="Positive evidence",
            value="\n".join(f"• {item}" for item in callout.positives)[:1024],
            inline=False,
        )
    risks = callout.hard_blockers + callout.warnings
    if risks:
        embed.add_field(
            name="Risks / missing proof",
            value="\n".join(f"• {item}" for item in risks)[:1024],
            inline=False,
        )
    embed.add_field(name="Contract", value=f"`{callout.mint}`", inline=False)
    embed.set_footer(text=f"Scan stage: {callout.scan_stage} • {callout.scan_reason[:140]}")
    return embed


def _coin_watch_embed(callout: CoinCallout) -> discord.Embed:
    embed = _coin_callout_embed(callout)
    label = callout.symbol or _short(callout.mint)
    embed.title = f"X COIN WATCH • DEVELOPING • {label}"
    embed.description = (
        f"Evidence score **{callout.score}/100** • paid X verification found developing "
        "exact-contract activity. It has **not** passed the VERIFIED TREND gate and is not "
        "an automatic buy signal. Use the research links and wait for stronger confirmation."
    )
    embed.color = 0xF1C40F
    embed.set_footer(
        text="WATCH only • no user ping • VERIFIED TREND requires the complete 70+ evidence gate"
    )
    return embed


def _fomo_watch_embed(callout: CoinCallout) -> discord.Embed:
    embed = _coin_callout_embed(callout)
    label = callout.symbol or _short(callout.mint)
    embed.title = f"FOMO WATCH • ON-CHAIN MOMENTUM • {label}"
    embed.description = (
        f"Free-data score **{callout.score}/100** • this passed the complete public "
        "liquidity, holder, authority, Tracker-risk, five-minute buy-flow, volume, and "
        "executable-Jupiter-route gates. Paid X searches are disabled, so community post "
        "views are **not** claimed. Open the X profile and Fomo links to inspect it manually."
    )
    embed.color = 0x5865F2
    embed.set_footer(
        text="FOMO WATCH • public on-chain/DEX evidence • research alert, not a profit promise"
    )
    return embed


def _percent_change(current: Decimal | None, first: Decimal | None) -> str:
    if current is None or first is None or first <= 0:
        return "unavailable"
    value = ((current / first) - Decimal("1")) * Decimal("100")
    return f"{value:+.2f}%"


def _runner_embed(
    candidate: RunnerCandidate,
    *,
    index: int = 0,
    total: int = 1,
    research_test: bool = False,
    fomo_referral_code: str | None = None,
) -> discord.Embed:
    current = candidate.current
    first = candidate.first
    title = "🧪 FOMO RUNNER PIPELINE TEST — RESEARCH ONLY" if research_test else f"{candidate.tier}"
    chain_created = (
        f"<t:{candidate.chain_created_at}:R>" if candidate.chain_created_at else "unavailable"
    )
    pair_created = (
        f"<t:{candidate.pair_created_at}:R>" if candidate.pair_created_at else "unavailable"
    )
    graduated = f"<t:{candidate.graduated_at}:R>" if candidate.graduated_at else "unavailable"
    x = candidate.x_evidence
    x_text = (
        f"CHECKED • exact-contract posts `{x.contract_posts}` • authors "
        f"`{x.contract_authors}` • credible `{x.credible_contract_authors}` • "
        f"velocity `{x.posts_per_minute}/min` • duplicate `{x.duplicate_percent}%`"
        if x.available
        else f"NOT VERIFIED • {x.error or 'zero-cost/manual-check state'}"
    )
    total_5m = current.buys_5m + current.sells_5m
    holder_count = current.holder_count if current.holder_count is not None else "unavailable"
    holder_growth = (
        current.holder_count - first.holder_count
        if current.holder_count is not None and first.holder_count is not None
        else "unavailable"
    )
    earliest_entry = (
        candidate.earliest_smart_entry_age_seconds
        if candidate.earliest_smart_entry_age_seconds is not None
        else "unavailable"
    )
    raw_smart_wallets = candidate.raw_smart_wallet_count or len(candidate.smart_wallets)

    def evidence(value: Decimal | None) -> Decimal | str:
        return value if value is not None else "unavailable"

    embed = discord.Embed(
        title=title[:256],
        description=(
            f"**[{(candidate.name or 'Unknown token')[:RUNNER_TOKEN_NAME_LIMIT]}]"
            f"({_fomo_coin_url(candidate.mint, fomo_referral_code)})** "
            f"`${(candidate.symbol or 'UNKNOWN')[:RUNNER_TOKEN_SYMBOL_LIMIT]}`"
            f"\n`{candidate.mint}`\n\n"
            f"Candidate `{index + 1}/{total}` • stage "
            f"**{STAGE_LABELS.get(candidate.stage, candidate.stage)}**\n"
            f"Opportunity **{candidate.quality.opportunity_score:.0f}/100** • momentum "
            f"**{candidate.quality.momentum_score:.0f}/100** • organic demand "
            f"**{candidate.quality.organic_score:.0f}/100** • legacy score "
            f"`{candidate.score}/100`\n"
            f"{_runner_links(candidate, fomo_referral_code)}\n"
            "Links only navigate • no automatic buying"
        ),
        color=(0xE74C3C if candidate.hard_blockers else 0xF1C40F if research_test else 0x5865F2),
        timestamp=datetime.fromtimestamp(candidate.generated_at, tz=UTC),
    )
    embed.add_field(
        name="Source / detection timing",
        value=(
            f"Chain created: {chain_created} • pair created: {pair_created}\n"
            f"Graduated: {graduated}\n"
            f"Source: `{candidate.graduation_source}`\n"
            f"Radar first seen: <t:{candidate.radar_first_seen_at or candidate.first_seen_at}:R> "
            "• first market data: "
            f"<t:{candidate.first_market_data_at or candidate.first_seen_at}:R>"
        ),
        inline=False,
    )
    embed.add_field(
        name="Market since first seen",
        value=(
            f"Price `{_price(first.price_usd)}` → `{_price(current.price_usd)}` "
            f"(`{_percent_change(current.price_usd, first.price_usd)}`)\n"
            f"MC `{_money(first.market_cap_usd)}` → `{_money(current.market_cap_usd)}` "
            f"(`{_percent_change(current.market_cap_usd, first.market_cap_usd)}`)\n"
            f"Liquidity `{_money(current.liquidity_usd)}` • "
            f"5m volume `{_money(current.volume_5m_usd)}`"
        ),
        inline=False,
    )
    if candidate.momentum_windows:
        window_lines: list[str] = []
        for window in candidate.momentum_windows:
            label = f"{window.seconds}s" if window.seconds < 60 else f"{window.seconds // 60}m"
            window_lines.append(
                f"**{label}:** price `{evidence(window.price_change_percent)}%` • "
                f"MC `{evidence(window.market_cap_change_percent)}%` • "
                f"rolling-5m volume `{evidence(window.rolling_volume_change_percent)}%` • "
                f"transactions `{evidence(window.rolling_transactions_change_percent)}%`"
            )
        embed.add_field(
            name="Short-interval momentum / acceleration",
            value="\n".join(window_lines)[:1024],
            inline=False,
        )
    embed.add_field(
        name="Flow / holder evidence",
        value=(
            f"5m buys/sells `{current.buys_5m}/{current.sells_5m}` "
            f"(`{total_5m}` transactions)\n"
            f"Holders `{holder_count}` • growth `{holder_growth}`\n"
            f"Verified smart-wallet buyers `{current.verified_unique_buyers}` • "
            "scope: tracked financially verified wallets only"
        ),
        inline=False,
    )
    embed.add_field(
        name="Wallet overlap",
        value=(
            f"Raw smart wallets `{raw_smart_wallets}` • "
            f"estimated independent `{candidate.estimated_independent_smart_wallets}` • "
            f"{', '.join(candidate.smart_wallets[:5]) or 'none yet'}\n"
            f"Earliest smart entry after source creation: `{earliest_entry}s`\n"
            "Public Fomo top-trader overlap: `not available through an authorized API`"
        ),
        inline=False,
    )
    risk = current
    if candidate.safety.status == "FAIL":
        embed.add_field(
            name="🛑 SAFETY: FAIL — " + candidate.safety.scam_risk_level,
            value=(
                "\n".join(f"• {item}" for item in candidate.safety.failures[:6])
                or "• fail-closed safety rejection"
            )[:1024],
            inline=False,
        )
    elif candidate.safety.status == "UNKNOWN":
        embed.add_field(
            name="⚠️ SAFETY: UNKNOWN — never treated as a pass",
            value=(
                "Missing evidence: "
                + ", ".join(candidate.safety.critical_unknowns[:6])
                + "\nEntry eligibility stays blocked while any of this is unavailable."
            )[:1024],
            inline=False,
        )
    embed.add_field(
        name="Risk / route",
        value=(
            f"Scam risk `{candidate.safety.scam_risk_score}/100 — "
            f"{candidate.safety.scam_risk_level}` • safety `{candidate.safety.status}`\n"
            f"Tracker risk `{evidence(risk.risk_score)}/10` • "
            f"bundlers `{evidence(risk.bundlers_percent)}%` • "
            f"insiders `{evidence(risk.insiders_percent)}%`\n"
            f"snipers `{evidence(risk.snipers_percent)}%` • "
            f"dev `{evidence(risk.dev_percent)}%` • "
            f"top holders `{evidence(risk.top10_percent)}%`\n"
            f"BUY ROUTE `{risk.buy_route_status}` impact "
            f"`{evidence(risk.route_price_impact_percent)}%` • SELL ROUTE "
            f"`{risk.sell_route_status}` impact "
            f"`{evidence(risk.sell_route_price_impact_percent)}%`"
        ),
        inline=False,
    )
    if candidate.score_history:
        values = " → ".join(f"{value:.0f}" for value in candidate.score_history[-5:])
        delta = (
            candidate.score_history[-1] - candidate.score_history[-2]
            if len(candidate.score_history) >= 2
            else Decimal("0")
        )
        arrow = "↑" if delta >= 5 else "↓" if delta <= -5 else "→"
        embed.add_field(
            name="Signal progression",
            value=f"`{values} {arrow}` • meaningful delta `{delta:+.0f}`",
            inline=False,
        )
    forensics = candidate.forensics
    independent = (
        forensics.estimated_independent_clusters
        if forensics.estimated_independent_clusters is not None
        else "unknown"
    )
    largest_size = (
        forensics.largest_cluster_size
        if forensics.largest_cluster_size is not None
        else "unknown"
    )
    demand = candidate.quality.demand
    fresh_wallets = (
        f"{forensics.fresh_wallet_count}/{forensics.traced_wallets}"
        if forensics.fresh_wallet_count is not None
        else "unknown"
    )
    embed.add_field(
        name="Cluster / buyer independence",
        value=(
            f"Raw unique buyers `{forensics.raw_unique_buyers}` • traced wallets "
            f"`{forensics.traced_wallets}` • independent among traced `{independent}` "
            f"(confidence `{demand.confidence}`)\n"
            f"Shared-funder groups `{len(forensics.shared_funder_groups)}` • time-linked "
            f"groups `{len(forensics.time_linked_groups)}` ({demand.time_linked_wallets} "
            f"wallets) • upstream-linked `{demand.upstream_linked_clusters}` • largest "
            f"cluster wallets `{largest_size}`\n"
            f"Fresh wallets among traced `{fresh_wallets}` • raw Top10 "
            f"`{evidence(risk.top10_percent)}%` • cluster-adjusted "
            f"`{evidence(forensics.cluster_adjusted_percent)}%`\n"
            "Funding links describe public-chain coordination only, never wallet ownership."
        )[:1024],
        inline=False,
    )
    breakdown = candidate.breakdown
    embed.add_field(
        name="Runner score breakdown",
        value=(
            f"recency `{breakdown.graduation_recency}` • momentum `{breakdown.momentum}` • "
            f"acceleration `{breakdown.acceleration}` • buy quality `{breakdown.buy_quality}`\n"
            f"liquidity `{breakdown.liquidity}` • holders `{breakdown.holders}` • "
            f"smart wallets `{breakdown.smart_wallets}` • safety/route "
            f"`{breakdown.safety_route}` • X `{breakdown.x_social}` • "
            f"penalties `{breakdown.penalties}`"
        ),
        inline=False,
    )
    embed.add_field(name="X exact-contract verification", value=x_text[:1024], inline=False)
    reasons = candidate.why_surfaced or why_surfaced(candidate.quality)
    if reasons:
        embed.add_field(
            name="WHY SURFACED",
            value="\n".join(f"• {item}" for item in reasons)[:1024],
            inline=False,
        )
    if candidate.quality.quality_warnings:
        embed.add_field(
            name="QUALITY WARNINGS",
            value="\n".join(f"• {item}" for item in candidate.quality.quality_warnings)[:1024],
            inline=False,
        )
    if candidate.positives:
        embed.add_field(
            name="Why it is being measured",
            value="\n".join(f"• {item}" for item in candidate.positives)[:1024],
            inline=False,
        )
    risks = candidate.hard_blockers + candidate.warnings
    if risks:
        embed.add_field(
            name="Warnings / hard blockers",
            value="\n".join(f"• {item}" for item in risks)[:1024],
            inline=False,
        )
    embed.set_footer(
        text=("SHADOW RESEARCH • existing token • no J7 • no auto-buy • no profit promise")
    )
    return _clamp_embed(embed)


def _runner_digest_embed(
    candidates: tuple[RunnerCandidate, ...],
    public_floor: Decimal,
    fomo_referral_code: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title="FOMO RUNNER RADAR — RESEARCH",
        description=(
            "Qualified existing-token setups, ranked by opportunity quality, "
            "acceleration, buyer independence and freshness. Tokens that only "
            "graduated are watched silently and never listed here. Non-pinging; "
            f"still below the `{public_floor}/100` public-alert floor."
        ),
        color=0xF1C40F,
        timestamp=discord.utils.utcnow(),
    )
    for index, candidate in enumerate(candidates, start=1):
        current = candidate.current
        quality = candidate.quality
        demand = quality.demand
        source_created_at = (
            candidate.chain_created_at
            or candidate.graduated_at
            or candidate.pair_created_at
        )
        graduation_age = (
            max(0, candidate.generated_at - source_created_at) // 60
            if source_created_at
            else None
        )
        x_status = "verified" if candidate.x_evidence.available else "not verified"
        reasons = candidate.why_surfaced or why_surfaced(quality)
        why = "\n".join(f"• {item}" for item in reasons[:4]) or "• qualified on market quality"
        warnings = "\n".join(f"• {item}" for item in quality.quality_warnings[:3])
        independence = (
            f"{demand.estimated_independent_buyers}/{demand.traced_wallets} traced"
            if demand.estimated_independent_buyers is not None
            else "not traced yet"
        )
        safety_line = (
            f"Safety `{candidate.safety.status}` • scam risk "
            f"`{candidate.safety.scam_risk_score}/100 — {candidate.safety.scam_risk_level}`"
        )
        if candidate.safety.status == "FAIL" and candidate.safety.failures:
            safety_line = (
                f"🛑 **SAFETY FAIL — {candidate.safety.scam_risk_level}**\n"
                + "\n".join(f"• {item}" for item in candidate.safety.failures[:3])
            )
        elif candidate.hard_blockers:
            safety_line += "\nBlockers: " + "; ".join(candidate.hard_blockers[:2])
        embed.add_field(
            name=(
                f"#{index} {candidate.name or 'Unknown'} "
                f"${candidate.symbol or 'UNKNOWN'} — "
                f"opp {quality.opportunity_score:.0f} / mom {quality.momentum_score:.0f}"
            )[:256],
            value=(
                f"**[{candidate.name or 'Unknown'}]"
                f"({_fomo_coin_url(candidate.mint, fomo_referral_code)})** "
                f"`${candidate.symbol or 'UNKNOWN'}`\n"
                f"Age proxy `{f'{graduation_age}m' if graduation_age is not None else 'unknown'}` "
                f"• mint `{candidate.mint}`\n"
                f"MC `{_money(candidate.first.market_cap_usd)}` → "
                f"`{_money(current.market_cap_usd)}` "
                f"(`{_percent_change(current.market_cap_usd, candidate.first.market_cap_usd)}`)\n"
                "Price since first seen "
                f"`{_percent_change(current.price_usd, candidate.first.price_usd)}` "
                f"• 5m buys/sells `{current.buys_5m}/{current.sells_5m}`\n"
                f"Volume `{_money(current.volume_5m_usd)}` • liquidity "
                f"`{_money(current.liquidity_usd)}` • holders "
                f"`{current.holder_count if current.holder_count is not None else 'unknown'}`\n"
                f"Independent buyers `{independence}` • smart wallets "
                f"`{demand.raw_smart_wallets}` raw / "
                f"`{demand.independent_smart_clusters}` independent • X `{x_status}`\n"
                f"{safety_line}\n"
                f"**WHY SURFACED**\n{why}"
                + (f"\n**WARNINGS**\n{warnings}" if warnings else "")
                + f"\n{_runner_links(candidate, fomo_referral_code)}"
            )[:1024],
            inline=False,
        )
    embed.set_footer(
        text=(
            f"RESEARCH ONLY — BELOW {public_floor}/100 PUBLIC ALERT FLOOR • "
            "no buy, SOL spend, X lookup, or J7 call"
        )
    )
    return embed


def _runner_fresh_embed(
    candidate: RunnerCandidate,
    fomo_referral_code: str | None = None,
) -> discord.Embed:
    current = candidate.current
    source_created_at = (
        candidate.chain_created_at or candidate.graduated_at or candidate.pair_created_at
    )
    age = (
        max(0, candidate.generated_at - source_created_at)
        if source_created_at is not None
        else None
    )
    first_seen_ago = max(
        0,
        candidate.generated_at
        - (candidate.radar_first_seen_at or candidate.first_seen_at),
    )
    embed = discord.Embed(
        title="⚡ FRESH RUNNER DETECTED",
        description=(
            f"**[{candidate.name or 'Unknown token'}]"
            f"({_fomo_coin_url(candidate.mint, fomo_referral_code)})** "
            f"`${candidate.symbol or 'UNKNOWN'}`\n`{candidate.mint}`\n\n"
            f"**Age:** `{f'{age}s' if age is not None else 'unknown'}`\n"
            f"**Bot first saw:** `{first_seen_ago}s ago`\n"
            f"**MC:** `{_money(current.market_cap_usd)}`\n"
            f"**Liquidity:** `{_money(current.liquidity_usd)}`\n"
            f"**Activity:** `{current.buys_5m} buys / {current.sells_5m} sells`\n\n"
            f"**Signal:** `{candidate.score}/100 — EARLY DATA`\n"
            f"**Safety:** `{candidate.safety.status} — "
            f"{candidate.safety.scam_risk_level}`\n\n"
            f"{_runner_links(candidate, fomo_referral_code)}\n\n"
            "**RESEARCH ONLY.**"
        ),
        color=0x3498DB,
        timestamp=datetime.fromtimestamp(candidate.generated_at, tz=UTC),
    )
    embed.set_footer(
        text="Non-pinging fresh lane • no buy • no sell • no J7 • no signing • no SOL spend"
    )
    return embed


def _runner_forensic_embed(
    candidate: RunnerCandidate,
    fomo_referral_code: str | None = None,
) -> discord.Embed:
    current = candidate.current
    forensic = candidate.forensics

    def value(item: object, suffix: str = "") -> str:
        return "unknown" if item is None else f"{item}{suffix}"

    shared = "\n".join(
        f"• `{_short(group.cluster_id)}` • {group.wallet_count} wallets • "
        f"{value(group.supply_percent, '%')} supply • interval "
        f"{value(group.funding_interval_seconds, 's')} • {group.confidence}"
        for group in forensic.shared_funder_groups[:5]
    ) or "No shared-funder group confirmed in the bounded trace."
    pair_age = (
        candidate.generated_at - candidate.pair_created_at
        if candidate.pair_created_at
        else None
    )
    entry = "YES" if candidate.safety.entry_eligible and candidate.score >= 70 else "NO"
    embed = discord.Embed(
        title="FOMO FORENSICS — READ ONLY",
        description=(
            f"**[{candidate.name or 'Unknown token'}]"
            f"({_fomo_coin_url(candidate.mint, fomo_referral_code)})** "
            f"`${candidate.symbol or 'UNKNOWN'}`\n`{candidate.mint}`\n"
            f"{_runner_links(candidate, fomo_referral_code)}"
        ),
        color=0xE67E22 if candidate.safety.status == "FAIL" else 0x5865F2,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="Market / signal",
        value=(
            f"Age `{value(pair_age, 's')}` • MC `{_money(current.market_cap_usd)}` • "
            f"liquidity `{_money(current.liquidity_usd)}` • "
            f"holders `{value(current.holder_count)}`\n"
            f"Runner signal `{candidate.score}/100` • history "
            f"`{' → '.join(f'{item:.0f}' for item in candidate.score_history[-5:]) or 'none'}`"
        )[:1024],
        inline=False,
    )
    embed.add_field(
        name="Scam risk / entry safety",
        value=(
            f"Scam risk `{candidate.safety.scam_risk_score}/100 — "
            f"{candidate.safety.scam_risk_level}` • safety `{candidate.safety.status}` • "
            f"entry `{entry}`\n"
            f"Top10 `{value(current.top10_percent, '%')}` • dev "
            f"`{value(current.dev_percent, '%')}` • bundlers "
            f"`{value(current.bundlers_percent, '%')}` • insiders "
            f"`{value(current.insiders_percent, '%')}` • snipers "
            f"`{value(current.snipers_percent, '%')}`"
        )[:1024],
        inline=False,
    )
    embed.add_field(
        name="Shared funding / independence",
        value=(
            f"Raw buyers `{forensic.raw_unique_buyers}` • independent clusters "
            f"`{value(forensic.estimated_independent_clusters)}` • largest cluster "
            f"`{value(forensic.largest_cluster_size)}` wallets / "
            f"`{value(forensic.largest_cluster_supply_percent, '%')}` supply\n"
            f"Cluster-adjusted ownership `{value(forensic.cluster_adjusted_percent, '%')}` • "
            f"time-linked groups `{len(forensic.time_linked_groups)}`\n{shared}"
        )[:1024],
        inline=False,
    )
    embed.add_field(
        name="Routes / creator evidence",
        value=(
            f"BUY ROUTE `{current.buy_route_status}` impact "
            f"`{value(current.route_price_impact_percent, '%')}` • SELL ROUTE "
            f"`{current.sell_route_status}` impact "
            f"`{value(current.sell_route_price_impact_percent, '%')}`\n"
            f"Creator wallet `{forensic.creator_wallet or 'not reliably identified'}` • "
            f"previous deployments `{value(forensic.previous_token_deployments)}` • "
            f"previous severe collapses `{value(forensic.previous_severe_collapses)}`"
        )[:1024],
        inline=False,
    )
    warnings = candidate.safety.failures + candidate.safety.critical_unknowns + forensic.warnings
    if warnings:
        embed.add_field(
            name="Warnings / unknowns",
            value="\n".join(f"• {item}" for item in warnings)[:1024],
            inline=False,
        )
    embed.set_footer(text="No buy • no sell • no J7 • no transaction • no signature • no SOL")
    return embed


def _runner_risk_escalation_embed(
    candidate: RunnerCandidate,
    changes: tuple[str, ...],
    fomo_referral_code: str | None = None,
) -> discord.Embed:
    return discord.Embed(
        title="⚠️ RUNNER RISK ESCALATION",
        description=(
            f"**[{candidate.name or 'Unknown token'}]"
            f"({_fomo_coin_url(candidate.mint, fomo_referral_code)})** "
            f"`${candidate.symbol or 'UNKNOWN'}`\n`{candidate.mint}`\n\n"
            + "\n".join(f"• {item}" for item in changes)
            + f"\n\nSafety `{candidate.safety.status}` • scam risk "
            f"`{candidate.safety.scam_risk_score}/100 — "
            f"{candidate.safety.scam_risk_level}`\n"
            f"{_runner_links(candidate, fomo_referral_code)}\n\nNo automatic sell."
        )[:4096],
        color=0xE67E22,
        timestamp=discord.utils.utcnow(),
    )


def _runner_invalidated_embed(
    candidate: RunnerCandidate,
    metrics: dict[str, object],
    reasons: tuple[str, ...],
    fomo_referral_code: str | None = None,
) -> discord.Embed:
    def number(key: str) -> str:
        value = metrics.get(key)
        return "unknown" if value is None else f"{Decimal(str(value)):+.2f}%"

    embed = discord.Embed(
        title="🛑 SETUP INVALIDATED",
        description=(
            f"**[{candidate.name or 'Unknown token'}]"
            f"({_fomo_coin_url(candidate.mint, fomo_referral_code)})** "
            f"`${candidate.symbol or 'UNKNOWN'}`\n`{candidate.mint}`\n"
            f"{_runner_links(candidate, fomo_referral_code)}"
        ),
        color=0xE74C3C,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="Path after detection",
        value=(
            f"First detected MC `{_money(metrics.get('first_market_cap'))}` • peak MC "
            f"`{_money(metrics.get('peak_market_cap'))}` • current MC "
            f"`{_money(metrics.get('current_market_cap'))}`\n"
            f"Peak return `{number('peak_return')}` • drawdown from peak "
            f"`{number('drawdown_from_peak')}` • liquidity deterioration "
            f"`{number('liquidity_decline')}`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Reason",
        value="\n".join(f"• {item}" for item in reasons)[:1024],
        inline=False,
    )
    embed.set_footer(text="Deduplicated research warning • no automatic sell or transaction")
    return embed


def _news_alert_embed(
    alert: NewsAlert,
    opportunity: LaunchOpportunity,
) -> discord.Embed:
    colors = {
        "COIN FOUND": 0x3498DB,
        X_VERIFIED_LAUNCH_VERDICT: 0xE74C3C,
        NO_X_LAUNCH_VERDICT: 0xF39C12,
        "WATCH": 0xF1C40F,
        "SKIP": 0x95A5A6,
    }
    source = alert.author or alert.source
    delay = max(0, alert.received_at - alert.created_at) if alert.created_at else None
    timing = f" • received `{delay}s` after publication" if delay is not None else ""
    if opportunity.verdict == NO_X_LAUNCH_VERDICT:
        title = f"🔥 {NO_X_LAUNCH_VERDICT} • {opportunity.category} • {source}"
    elif opportunity.verdict == X_VERIFIED_LAUNCH_VERDICT:
        title = f"🚀 LAUNCH READY — X VERIFIED • {opportunity.category} • {source}"
    else:
        title = f"NEWS RADAR • {opportunity.verdict} • {opportunity.category} • {source}"
    embed = discord.Embed(
        title=title[:256],
        description=(
            f"**{alert.headline[:700]}**\n\n"
            + (
                "A Solana contract is already in the source. Use the direct research/buy "
                "links; this alert cannot launch a duplicate coin."
                if alert.token_mints
                else (
                    "No source contract was found. This passed the stricter free-source, "
                    "freshness, independent-confirmation, competition, and identity gates. "
                    "**X/social velocity was not verified.**"
                    if opportunity.verdict == NO_X_LAUNCH_VERDICT
                    else "No source contract was found. This public alert passed the "
                    "X-verified crypto-demand, source, competition, and launch-identity gates."
                )
            )
        ),
        color=colors.get(opportunity.verdict, 0x95A5A6),
        timestamp=datetime.fromtimestamp(alert.received_at or int(time.time()), tz=UTC),
    )
    embed.add_field(
        name="Score",
        value=(
            f"**{opportunity.score}/100** • confidence **{opportunity.confidence}**{timing}\n"
            f"Proposed identity: **{opportunity.coin_name}** (`${opportunity.coin_symbol}`)"
        ),
        inline=False,
    )
    embed.add_field(
        name="Admission lane",
        value=(
            f"**{opportunity.lane}** • free candidate gate "
            f"`{'passed' if opportunity.no_x_candidate_ready else 'not used'}` • "
            f"X-verified gate `{'passed' if opportunity.x_verified else 'not verified'}` • "
            f"U.S. relevance `{'yes' if opportunity.us_relevant else 'no'}`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Why it scored this way",
        value=(
            f"Source `{opportunity.source_score}/15` • speed `{opportunity.speed_score}/15` • "
            f"meme potential `{opportunity.viral_score}/25`\n"
            f"X traction "
            f"`{f'{opportunity.x_score}/15' if opportunity.x_verified else 'unverified'}` • "
            "independent confirmation "
            f"`{opportunity.confirmation_score}/10` • open coin space "
            f"`{opportunity.competition_score}/10` • identity "
            f"`{opportunity.identity_score}/10`"
        ),
        inline=False,
    )
    if alert.author:
        embed.add_field(
            name="Account proof",
            value=(
                f"Followers `{alert.author_followers:,}` • "
                f"verified `{'yes' if alert.author_verified else 'no'}`"
            ),
            inline=False,
        )
    if alert.narrative_terms:
        embed.add_field(
            name="Narratives being watched",
            value=" • ".join(f"`{item}`" for item in alert.narrative_terms)[:1024],
            inline=False,
        )
    if opportunity.x_evidence.available:
        x = opportunity.x_evidence
        embed.add_field(
            name="Crypto X evidence",
            value=(
                f"Posts `{x.posts}` • authors `{x.unique_authors}` • established "
                f"`{x.established_authors}` • influential `{x.influential_authors}`\n"
                f"Crypto-native authors `{x.crypto_authors}` • credible crypto authors "
                f"`{x.credible_crypto_authors}` • promotion posts `{x.promoter_posts}` • "
                f"exact-contract posts `{x.contract_posts}`\n"
                f"Trusted crypto `{x.trusted_crypto_authors}` • million-follower "
                f"`{x.million_follower_authors}`\n"
                f"Velocity `{x.posts_per_minute}/min` • engagements `{x.engagements}` • "
                f"duplicate text `{x.duplicate_percent}%`"
            ),
            inline=False,
        )
        if x.notable_accounts:
            embed.add_field(
                name="Notable crypto accounts discussing it",
                value=" • ".join(f"`{item}`" for item in x.notable_accounts)[:1024],
                inline=False,
            )
    else:
        embed.add_field(
            name="X verification",
            value="⚠️ **X/social velocity was not verified.**",
            inline=False,
        )
    if opportunity.positives:
        embed.add_field(
            name="Positive evidence",
            value="\n".join(f"• {item}" for item in opportunity.positives)[:1024],
            inline=False,
        )
    risks = opportunity.blockers + opportunity.warnings
    if risks:
        embed.add_field(
            name="Blocks / missing proof",
            value="\n".join(f"• {item}" for item in risks)[:1024],
            inline=False,
        )
    if alert.token_mints:
        embed.add_field(
            name="Contracts found in the source",
            value="\n".join(f"`{item}`" for item in alert.token_mints)[:1024],
            inline=False,
        )
    if alert.url:
        embed.add_field(name="Original source", value=f"[Open source]({alert.url})", inline=False)
    if alert.image_urls:
        embed.add_field(
            name="Recommended coin image",
            value=(
                "The source feed's lead image will be cropped, labeled as an unofficial meme, "
                "uploaded to IPFS, and sent to the launch backend."
            ),
            inline=False,
        )
        embed.set_image(url=alert.image_urls[0])
    embed.set_footer(
        text=(
            "COIN FOUND • direct links below • token-risk callout follows"
            if alert.token_mints
            else (
                "Manual J7 candidate • never auto-launches • launch button is admin-only"
                if opportunity.verdict == NO_X_LAUNCH_VERDICT
                else "No coin yet • DEX matcher keeps checking • launch button is admin-only"
            )
        )
    )
    return embed


def _pump_launch_result_embed(
    result: PumpLaunchResult,
    fomo_referral_code: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=(
            f"✅ J7 LAUNCH SUCCESS • ${result.symbol}"
            if result.success
            else f"J7 LAUNCH • {result.status.replace('_', ' ')}"
        ),
        description=result.message,
        color=0x2ECC71 if result.success else 0xE74C3C,
        timestamp=datetime.fromtimestamp(result.created_at or int(time.time()), tz=UTC),
    )
    embed.add_field(name="Name", value=result.name or "unknown")
    embed.add_field(name="Symbol", value=f"`${result.symbol}`" if result.symbol else "unknown")
    embed.add_field(name="Provider", value=result.provider or "configured launch provider")
    if result.mint:
        embed.add_field(name="Mint", value=f"`{result.mint}`", inline=False)
        embed.add_field(
            name="Pump.fun",
            value=f"[Open coin](https://pump.fun/coin/{result.mint})",
        )
        embed.add_field(
            name="Fomo",
            value=f"[Open coin in Fomo]({_fomo_coin_url(result.mint, fomo_referral_code)})",
        )
    if result.signature:
        embed.add_field(
            name="Transaction",
            value=f"[View on Solscan](https://solscan.io/tx/{result.signature})",
            inline=False,
        )
    embed.set_footer(
        text=(
            "Public Solana coin • Pump.fun is immediate • Fomo may need time to index the mint • "
            "dedicated wallet/daily limits"
        )
    )
    return embed


def _launch_result_view(
    result: PumpLaunchResult,
    fomo_referral_code: str | None,
) -> discord.ui.View | None:
    if not result.success or not result.mint:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="OPEN PUMP.FUN",
            style=discord.ButtonStyle.link,
            url=f"https://pump.fun/coin/{result.mint}",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="OPEN FOMO",
            style=discord.ButtonStyle.link,
            url=_fomo_coin_url(result.mint, fomo_referral_code),
        )
    )
    if result.signature:
        view.add_item(
            discord.ui.Button(
                label="SOLSCAN",
                style=discord.ButtonStyle.link,
                url=f"https://solscan.io/tx/{result.signature}",
            )
        )
    return view


def _narrative_match_embed(alert: NewsAlert, match: NarrativePairMatch) -> discord.Embed:
    embed = discord.Embed(
        title=f"NEWS → NEW COIN MATCH • {match.symbol or _short(match.mint)}"[:256],
        description=(
            f"A new Solana pair matches the `{match.narrative}` narrative from "
            f"**{alert.author or alert.source}**. Name matching is an early lead, not proof "
            "that the source created or endorsed this token."
        ),
        color=0xF39C12,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Token", value=f"**{match.name or 'unknown'}** (`{match.symbol or '?'}`)")
    embed.add_field(name="Pair age", value=f"`{match.pair_age_minutes} min`")
    embed.add_field(name="Liquidity", value=_money(match.liquidity_usd))
    embed.add_field(name="Market cap", value=_money(match.market_cap_usd))
    embed.add_field(
        name="5m flow",
        value=(
            f"buys/sells `{match.buys_5m}/{match.sells_5m}` • "
            f"volume `{_money(match.volume_5m_usd)}`"
        ),
        inline=False,
    )
    embed.add_field(name="Contract", value=f"`{match.mint}`", inline=False)
    if alert.url:
        embed.add_field(name="Triggering news", value=f"[Open source]({alert.url})", inline=False)
    embed.set_footer(text="Automatic coin-risk callout follows • PAPER/research only")
    return embed


def _paper_trade_field(item: dict[str, object], traders: dict[str, str]) -> tuple[str, str]:
    side = str(item["side"])
    mint = str(item["token_mint"])
    quantity = Decimal(str(item["quantity"]))
    price = Decimal(str(item["execution_price_usd"]))
    gross = Decimal(str(item["gross_value_usd"]))
    fee = Decimal(str(item["fee_usd"]))
    kind = str(item["execution_kind"]).replace("_", " ").title()
    timestamp = int(item["created_at"])
    source = item.get("source_trader")
    source_text = ""
    if source:
        source_address = str(source)
        source_text = f" • source `{traders.get(source_address, _short(source_address))}`"

    if side == Side.SELL.value:
        realized = Decimal(str(item["realized_pnl_usd"]))
        matched_cost = (gross - fee) - realized
        roi = realized / matched_cost * Decimal("100") if matched_cost > 0 else Decimal("0")
        reason = item.get("exit_reason")
        reason_text = f"\nExit: {reason}" if reason else ""
        result = f"P&L `{_money(realized)}` • ROI `{roi:+.2f}%`"
    else:
        result = f"Spent `{_money(gross)}` • fee `{_money(fee)}`"
        reason_text = ""

    quote_text = ""
    if bool(item.get("quote_based")):
        router = str(item.get("quote_router") or "unknown")
        impact = Decimal(str(item.get("price_impact_percent") or 0))
        drift_raw = item.get("price_drift_percent")
        drift_text = f" • drift `{Decimal(str(drift_raw)):+.2f}%`" if drift_raw is not None else ""
        quote_text = f"\nQuote `{router}` • impact `{impact:.2f}%`{drift_text}"

    name = f"{side} • {kind} • {_short(mint)}"
    value = (
        f"<t:{timestamp}:R>{source_text}\n"
        f"Qty `{quantity:.6f}` @ `{_price(price)}`\n"
        f"{result}{quote_text}{reason_text}"
    )
    return name[:256], value[:1024]


def _position_embed(
    bot: SmartMoneyBot,
    position: dict[str, object],
    traders: dict[str, str],
    prices: dict[str, Decimal],
    *,
    index: int,
    total: int,
) -> discord.Embed:
    mint = str(position["token_mint"])
    quantity = Decimal(str(position["quantity"]))
    cost = Decimal(str(position["cost_basis_usd"]))
    entry = Decimal(str(position["average_entry_usd"]))
    live_price = prices.get(mint)
    price = PAPER_DEMO_ENTRY_PRICE if mint == PAPER_DEMO_MINT else (live_price or entry)
    value = quantity * price
    pnl = value - cost
    roi = _return_percent(value, cost)
    source = str(position.get("source_trader") or "")
    raw_mirror = position.get("position_kind") == "RAW_MIRROR"
    if mint == PAPER_DEMO_MINT:
        title = "Paper Demo (fake token)"
    elif raw_mirror:
        title = f"Raw mirror • {traders.get(source, _short(source))}"
    else:
        title = "Consensus strategy position"

    color = 0x2ECC71 if pnl >= 0 else 0xE74C3C
    embed = discord.Embed(
        title=f"Open PAPER Position • {index + 1}/{total}",
        description=f"**{title}**\n`{mint}`",
        color=color,
    )
    embed.add_field(name="Cost basis", value=_money(cost))
    embed.add_field(name="Current value", value=_money(value))
    embed.add_field(name="Unrealized P&L", value=f"{_money(pnl)} ({roi:+.2f}%)")
    embed.add_field(name="Average entry", value=_price(entry))
    embed.add_field(
        name="Current price",
        value=(
            _price(price)
            if live_price is not None or mint == PAPER_DEMO_MINT
            else f"{_price(entry)} (entry fallback; refresh later)"
        ),
    )
    embed.add_field(name="Quantity", value=f"{quantity:.6f}")
    embed.add_field(
        name="Opened",
        value=f"<t:{int(position['opened_at'])}:R>",
    )
    if raw_mirror:
        if bot.settings.paper_force_observation_mode:
            exit_policy = (
                "Waiting for this source wallet's SELL. Automatic stop/take-profit exits "
                "are off in forced-observation mode; the manual PAPER sell below overrides it."
            )
        else:
            exit_policy = (
                "Source-wallet SELL, hard stop, take profit, trailing-profit lock, or "
                "maximum hold—whichever closes the fake lot first."
            )
        embed.add_field(name="Automatic exit policy", value=exit_policy, inline=False)
    embed.set_footer(
        text=(
            "Refresh updates the mark. Manual sell changes only fake PAPER accounting; "
            "it cannot move real funds."
        )
    )
    return embed


class PaperTradesView(discord.ui.View):
    def __init__(
        self,
        bot: SmartMoneyBot,
        requester_id: int,
        *,
        page_size: int = 5,
    ) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.requester_id = requester_id
        self.page_size = max(1, min(page_size, 5))
        self.page = 0
        self.total = 0
        self.rows: list[dict[str, object]] = []
        self.traders: dict[str, str] = {}

    @classmethod
    async def create(
        cls, bot: SmartMoneyBot, requester_id: int, *, page_size: int = 5
    ) -> PaperTradesView:
        view = cls(bot, requester_id, page_size=page_size)
        await view.reload()
        return view

    @property
    def page_count(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)

    async def reload(self) -> None:
        self.total = await self.bot.engine.database.paper_trade_count()
        self.page = min(self.page, self.page_count - 1)
        self.rows = await self.bot.engine.database.paper_trades_page(
            limit=self.page_size,
            offset=self.page * self.page_size,
        )
        self.traders = {
            trader.address: trader.alias for trader in await self.bot.engine.database.list_traders()
        }
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.page_count - 1
        self.page_button.label = f"{self.page + 1}/{self.page_count}"

    def embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="PAPER Trade History",
            description=(
                f"Showing {len(self.rows)} fills on page {self.page + 1}. "
                "Use the buttons instead of posting another wall of text."
            ),
            color=0x3498DB,
        )
        for row in self.rows:
            name, value = _paper_trade_field(row, self.traders)
            embed.add_field(name=name, value=value, inline=False)
        embed.set_footer(text=f"{self.total} total PAPER fills • newest first")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Run `/smartmoney paper-trades` to open your own controls.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=0)
    async def previous_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        self.page = max(0, self.page - 1)
        await self.reload()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True, row=0)
    async def page_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del interaction, button

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        self.page = min(self.page_count - 1, self.page + 1)
        await self.reload()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, row=0)
    async def refresh_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        await self.reload()
        await interaction.response.edit_message(embed=self.embed(), view=self)


class PaperPositionsView(discord.ui.View):
    def __init__(
        self,
        bot: SmartMoneyBot,
        requester_id: int,
        *,
        can_sell: bool,
    ) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.requester_id = requester_id
        self.can_sell = can_sell
        self.index = 0
        self.positions: list[dict[str, object]] = []
        self.traders: dict[str, str] = {}
        self.prices: dict[str, Decimal] = {}
        self.link_buttons: list[discord.ui.Button] = []

    @classmethod
    async def create(
        cls,
        bot: SmartMoneyBot,
        requester_id: int,
        *,
        can_sell: bool,
    ) -> PaperPositionsView:
        view = cls(bot, requester_id, can_sell=can_sell)
        await view.reload()
        return view

    @property
    def current(self) -> dict[str, object]:
        return self.positions[self.index]

    async def reload(self) -> None:
        self.positions = await self.bot.engine.database.paper_all_positions()
        self.index = min(self.index, max(0, len(self.positions) - 1))
        self.traders = {
            trader.address: trader.alias for trader in await self.bot.engine.database.list_traders()
        }
        mints = sorted(
            {
                str(item["token_mint"])
                for item in self.positions
                if str(item["token_mint"]) != PAPER_DEMO_MINT
            }
        )
        try:
            self.prices = await self.bot.engine.market.prices(mints) if mints else {}
        except JupiterError:
            self.prices = {}
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        total = len(self.positions)
        self.previous_button.disabled = self.index <= 0
        self.next_button.disabled = total == 0 or self.index >= total - 1
        self.page_button.label = f"{self.index + 1 if total else 0}/{total}"
        self.sell_button.disabled = not self.can_sell or total == 0
        for button in self.link_buttons:
            self.remove_item(button)
        self.link_buttons = []
        if not total:
            return
        mint = str(self.current["token_mint"])
        if mint == PAPER_DEMO_MINT:
            return
        self.link_buttons = [
            discord.ui.Button(
                label="Fomo",
                style=discord.ButtonStyle.link,
                url=_fomo_coin_url(mint, self.bot.settings.fomo_referral_code),
                row=1,
            ),
            discord.ui.Button(
                label="Pump.fun",
                style=discord.ButtonStyle.link,
                url=f"https://pump.fun/coin/{mint}",
                row=1,
            ),
            discord.ui.Button(
                label="Jupiter",
                style=discord.ButtonStyle.link,
                url=f"https://jup.ag/swap/SOL-{mint}",
                row=1,
            ),
            discord.ui.Button(
                label="Chart",
                style=discord.ButtonStyle.link,
                url=f"https://dexscreener.com/solana/{mint}",
                row=1,
            ),
            discord.ui.Button(
                label="Solscan",
                style=discord.ButtonStyle.link,
                url=f"https://solscan.io/token/{mint}",
                row=1,
            ),
        ]
        for button in self.link_buttons:
            self.add_item(button)

    def embed(self) -> discord.Embed:
        return _position_embed(
            self.bot,
            self.current,
            self.traders,
            self.prices,
            index=self.index,
            total=len(self.positions),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Run `/smartmoney positions` to open your own controls.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=0)
    async def previous_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        self.index = max(0, self.index - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True, row=0)
    async def page_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del interaction, button

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        self.index = min(len(self.positions) - 1, self.index + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Refresh prices", style=discord.ButtonStyle.primary, row=0)
    async def refresh_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        await self.reload()
        if not self.positions:
            await interaction.response.edit_message(
                content="No open paper positions.", embed=None, view=None
            )
            return
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(
        label="Sell this PAPER position",
        style=discord.ButtonStyle.danger,
        row=2,
    )
    async def sell_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        position = dict(self.current)
        mint = str(position["token_mint"])
        if mint == PAPER_DEMO_MINT:
            await interaction.response.send_message(
                "Use `/smartmoney paper-demo` to close the demo position.",
                ephemeral=True,
            )
            return
        confirmation = PaperSellConfirmationView(self, position)
        cost = Decimal(str(position["cost_basis_usd"]))
        embed = discord.Embed(
            title="Confirm manual PAPER sell",
            description=(
                f"Close this full fake position now?\n`{mint}`\n\n"
                f"Cost basis: **{_money(cost)}**\n"
                "This does not touch real money. It will convert the current fake lot "
                "to realized PAPER P&L, and a later source-wallet SELL will be skipped "
                "because this linked lot is already closed."
            ),
            color=0xE67E22,
        )
        await interaction.response.edit_message(embed=embed, view=confirmation)


class PaperSellConfirmationView(discord.ui.View):
    def __init__(
        self,
        parent: PaperPositionsView,
        position: dict[str, object],
    ) -> None:
        super().__init__(timeout=60)
        self.parent = parent
        self.position = position

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.parent.interaction_check(interaction)

    @discord.ui.button(
        label="Confirm PAPER sell",
        style=discord.ButtonStyle.danger,
    )
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        await interaction.response.defer()
        result = await self.parent.bot.engine.manual_paper_exit(
            position_kind=str(self.position.get("position_kind") or "STRATEGY"),
            token_mint=str(self.position["token_mint"]),
            source_trader=(
                str(self.position["source_trader"]) if self.position.get("source_trader") else None
            ),
            requested_by=str(interaction.user),
        )
        result_embed = discord.Embed(
            title=(
                "Manual PAPER sell filled" if result.success else "Manual PAPER sell not filled"
            ),
            description=result.message,
            color=0x2ECC71 if result.success else 0xE74C3C,
        )
        await self.parent.reload()
        if not self.parent.positions:
            await interaction.edit_original_response(embed=result_embed, view=None)
            return
        await interaction.edit_original_response(
            embeds=[result_embed, self.parent.embed()],
            view=self.parent,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        await self.parent.reload()
        if not self.parent.positions:
            await interaction.response.edit_message(
                content="No open paper positions.", embed=None, view=None
            )
            return
        await interaction.response.edit_message(embed=self.parent.embed(), view=self.parent)


class SmartMoneyBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = False
        intents.members = False
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings
        self.engine = SmartMoneyEngine(settings, notifier=self)
        self._engine_started = False
        self._last_unmatched_sell_alert: dict[str, int] = {}

    async def setup_hook(self) -> None:
        await self.engine.initialize()
        await self.add_cog(SmartMoneyCommands(self))
        await self.add_cog(FomoCommands(self))
        if self.settings.discord_guild_id:
            guild = discord.Object(id=self.settings.discord_guild_id)
            # Testing uses guild-scoped commands so updates appear immediately. Clear the
            # cached guild tree before copying the current command set, then remove any
            # obsolete global commands left behind by an older deployment.
            self.tree.clear_commands(guild=guild)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        if not self._engine_started:
            await self.engine.start()
            self._engine_started = True
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")

    async def close(self) -> None:
        await self.engine.close()
        await super().close()

    async def _alert_channel(self) -> discord.abc.Messageable | None:
        raw = await self.engine.database.get_setting("alert_channel_id")
        if not raw:
            return None
        try:
            channel_id = int(raw)
        except ValueError:
            return None
        channel = self.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def _send_alert(
        self,
        embed: discord.Embed,
        *,
        token_mint: str | None = None,
        ping_user: bool = False,
        view: discord.ui.View | None = None,
    ) -> bool:
        channel = await self._alert_channel()
        if channel is None:
            return False
        alert_user_id = self.settings.discord_alert_user_id
        should_ping = ping_user and alert_user_id is not None
        content = f"<@{alert_user_id}>" if should_ping else None
        allowed_mentions = (
            discord.AllowedMentions(users=True, roles=False, everyone=False)
            if should_ping
            else discord.AllowedMentions.none()
        )
        try:
            await channel.send(
                content=content,
                embed=embed,
                view=(
                    view
                    if view is not None
                    else _token_view(token_mint, self.settings.fomo_referral_code)
                    if token_mint
                    else None
                ),
                allowed_mentions=allowed_mentions,
            )
            return True
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not post to alert channel")
            return False

    async def on_swap(self, swap: DetectedSwap, trader: TrackedTrader) -> None:
        if not self.settings.trade_activity_alerts_enabled:
            return
        color = 0x2ECC71 if swap.side is Side.BUY else 0xE74C3C
        embed = discord.Embed(
            title=f"RAW {swap.side.value} detected • {trader.alias}",
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Token contract",
            value=f"`{swap.token_mint}`",
            inline=False,
        )
        embed.add_field(name="Trade value", value=_money(swap.usd_value))
        embed.add_field(name="Detected price", value=_price(swap.token_price_usd))
        embed.add_field(
            name="Transaction",
            value=f"[View on Solscan](https://solscan.io/tx/{swap.signature})",
            inline=False,
        )
        mirrors_immediately = (
            self.settings.paper_mirror_raw_swaps
            and await self.engine.execution_mode() is ExecutionMode.PAPER
        )
        embed.set_footer(
            text=(
                "RAW activity • an automatic guarded paper-fill evaluation follows"
                if mirrors_immediately
                else "RAW activity • verify the mint • wait for consensus/risk result"
            )
        )
        await self._send_alert(
            embed,
            token_mint=swap.token_mint,
            ping_user=swap.side is Side.BUY,
        )

    async def on_discovery(self, refresh: DiscoveryRefresh) -> None:
        embed = discord.Embed(
            title="Verified Pump hot-wallet rotation refreshed",
            description=_discovery_lines(refresh.candidates),
            color=0x9B59B6,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Added", value=str(len(refresh.added_wallets)))
        embed.add_field(name="Rotated out", value=str(len(refresh.disabled_wallets)))
        embed.add_field(name="Watching", value=str(len(refresh.candidates)))
        embed.add_field(name="Strict candidate pool", value=str(refresh.candidate_pool_size))
        embed.add_field(name="Pump-verified", value=str(refresh.verified_pump_wallets))
        if refresh.removal_events:
            removal_text = "\n".join(
                f"• `{_short(event.address)}` — {event.reason}; "
                f"observed `{_money(event.observed_source_pnl_usd)}`, "
                f"paper `{_money(event.paper_pnl_usd)}`"
                for event in refresh.removal_events[:3]
            )
            embed.add_field(name="Why wallets left", value=removal_text[:1024], inline=False)
        embed.set_footer(
            text="Strict 24H + 7D PnL • recent Pump activity required • arbitrage excluded"
        )
        await self._send_alert(embed)

    async def on_signal(
        self, signal: Signal, token_info: TokenInfo | None, decision: RiskDecision
    ) -> None:
        if not self.settings.trade_activity_alerts_enabled:
            return
        symbol = (
            token_info.symbol if token_info and token_info.symbol else _short(signal.token_mint)
        )
        status = "PASSED" if decision.allowed else "BLOCKED"
        color = 0x00D084 if decision.allowed else 0xF1C40F
        embed = discord.Embed(
            title=f"Consensus {signal.side.value} • {symbol}",
            description=f"**{len(signal.trader_addresses)} independent tracked traders** aligned.",
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Trader score", value=f"{signal.combined_score}/100")
        embed.add_field(name="Reference price", value=_money(signal.reference_price_usd))
        embed.add_field(name="Risk gate", value=status)
        embed.add_field(name="Traders", value=", ".join(signal.trader_aliases)[:1024], inline=False)
        if token_info:
            embed.add_field(name="Liquidity", value=_money(token_info.liquidity_usd))
            embed.add_field(
                name="Organic score",
                value=str(token_info.organic_score or "unknown"),
            )
            embed.add_field(name="Verified", value="Yes" if token_info.verified else "No/unknown")
        if decision.reasons:
            embed.add_field(
                name="Checks",
                value="\n".join(f"• {r}" for r in decision.reasons)[:1024],
                inline=False,
            )
        embed.add_field(name="Mint", value=f"`{signal.token_mint}`", inline=False)
        await self._send_alert(embed, token_mint=signal.token_mint)

    async def on_execution(self, result: ExecutionResult) -> None:
        if not self.settings.trade_activity_alerts_enabled:
            return
        unmatched_sell = (
            result.side is Side.SELL
            and not result.success
            and "no open paper lot" in result.message.lower()
        )
        if unmatched_sell:
            now = int(time.time())
            previous = self._last_unmatched_sell_alert.get(result.token_mint)
            if previous and now - previous < 300:
                return
            self._last_unmatched_sell_alert[result.token_mint] = now
        skipped = not result.success and result.message.startswith("Skipped:")
        status = "FILLED" if result.success else ("SKIPPED" if skipped else "FAILED")
        color = 0x3498DB if result.success else (0xF1C40F if skipped else 0xE74C3C)
        embed = discord.Embed(
            title=f"{result.mode.value} {result.side.value} • {status}",
            description=result.message,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Token", value=f"`{result.token_mint}`", inline=False)
        embed.add_field(name="Configured size", value=_money(result.size_usd))
        if result.signature:
            embed.add_field(
                name="Transaction",
                value=f"[View on Solscan](https://solscan.io/tx/{result.signature})",
                inline=False,
            )
        await self._send_alert(embed, token_mint=result.token_mint)

    async def on_coin_callout(self, callout: CoinCallout) -> None:
        await self._send_alert(
            _coin_callout_embed(callout),
            token_mint=callout.mint,
            ping_user=callout.public_alert_eligible,
        )

    async def on_coin_watch(self, callout: CoinCallout) -> None:
        await self._send_alert(
            _coin_watch_embed(callout),
            token_mint=callout.mint,
            ping_user=False,
        )

    async def on_fomo_watch(self, callout: CoinCallout) -> None:
        await self._send_alert(
            _fomo_watch_embed(callout),
            token_mint=callout.mint,
            ping_user=True,
        )

    async def on_runner_alert(self, candidate: RunnerCandidate) -> bool:
        return await self._send_alert(
            _runner_embed(
                candidate,
                fomo_referral_code=self.settings.fomo_referral_code,
            ),
            ping_user=True,
            view=RunnerAlertView(self, candidate),
        )

    async def on_runner_fresh(self, candidate: RunnerCandidate) -> bool:
        return await self._send_alert(
            _runner_fresh_embed(candidate, self.settings.fomo_referral_code),
            ping_user=False,
            view=RunnerAlertView(self, candidate),
        )

    async def on_runner_risk_escalation(
        self,
        candidate: RunnerCandidate,
        changes: tuple[str, ...],
    ) -> bool:
        return await self._send_alert(
            _runner_risk_escalation_embed(
                candidate,
                changes,
                self.settings.fomo_referral_code,
            ),
            ping_user=False,
            view=RunnerAlertView(self, candidate),
        )

    async def on_runner_invalidated(
        self,
        candidate: RunnerCandidate,
        metrics: dict[str, object],
        reasons: tuple[str, ...],
    ) -> bool:
        return await self._send_alert(
            _runner_invalidated_embed(
                candidate,
                metrics,
                reasons,
                self.settings.fomo_referral_code,
            ),
            ping_user=False,
            view=RunnerAlertView(self, candidate),
        )

    async def on_runner_digest(
        self,
        candidates: tuple[RunnerCandidate, ...],
        public_floor: Decimal,
    ) -> None:
        await self._send_alert(
            _runner_digest_embed(
                candidates,
                public_floor,
                getattr(getattr(self, "settings", None), "fomo_referral_code", None),
            ),
            ping_user=False,
        )

    async def on_news_alert(
        self,
        alert: NewsAlert,
        opportunity: LaunchOpportunity,
    ) -> None:
        mint = alert.token_mints[0] if alert.token_mints else None
        await self._send_alert(
            _news_alert_embed(alert, opportunity),
            token_mint=mint,
            ping_user=opportunity.verdict
            in {"COIN FOUND", X_VERIFIED_LAUNCH_VERDICT, NO_X_LAUNCH_VERDICT},
            view=None if mint else NewsOpportunityView(self, opportunity),
        )

    async def on_narrative_match(self, alert: NewsAlert, match: NarrativePairMatch) -> None:
        await self._send_alert(
            _narrative_match_embed(alert, match),
            token_mint=match.mint,
            ping_user=True,
        )

    async def on_daily_profit_lock(self, status: PaperDailyLockStatus) -> None:
        loss_lock = status.lock_reason == "LOSS_LIMIT"
        embed = discord.Embed(
            title=(
                "PAPER daily loss lock triggered"
                if loss_lock
                else "PAPER daily profit lock triggered"
            ),
            description=(
                "The account reached today's marked-loss limit. All open PAPER "
                "positions are being sold and new PAPER buys are blocked until the "
                "next local trading day."
                if loss_lock
                else "The account reached today's marked-profit target. All open "
                "PAPER positions are being sold and new PAPER buys are blocked until "
                "the next local trading day."
            ),
            color=0xE74C3C if loss_lock else 0x2ECC71,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Marked P&L", value=_money(status.marked_pnl_usd))
        embed.add_field(
            name="Triggered limit",
            value=(
                f"-{_money(status.loss_limit_usd)} loss"
                if loss_lock
                else f"+{_money(status.target_usd)} profit"
            ),
        )
        embed.add_field(name="Positions to close", value=str(status.open_positions))
        embed.add_field(
            name="Automatic reset",
            value=f"Next day in `{self.settings.paper_daily_lock_timezone}`",
            inline=False,
        )
        embed.set_footer(
            text="PAPER only • exit pricing and configured simulated costs still apply"
        )
        await self._send_alert(embed, ping_user=True)

    async def on_error(self, context: str, error: Exception) -> None:
        logger.error("%s: %s", context, error)


class SmartMoneyCommands(
    commands.GroupCog,
    group_name="smartmoney",
    group_description="Track profitable public Solana wallets and test copy signals.",
):
    def __init__(self, bot: SmartMoneyBot) -> None:
        self.bot = bot

    def _is_admin(self, user: discord.abc.User) -> bool:
        return _member_is_admin(user, self.bot.settings)

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        if self._is_admin(interaction.user):
            return True
        await interaction.response.send_message(
            "You need Administrator or a configured bot-admin role for that command.",
            ephemeral=True,
        )
        return False

    @app_commands.command(
        name="setup", description="Choose where wallet and signal alerts are posted."
    )
    async def setup(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if not await self._require_admin(interaction):
            return
        await self.bot.engine.database.set_setting("alert_channel_id", str(channel.id))
        await interaction.response.send_message(
            f"Alerts will be posted in {channel.mention}.", ephemeral=True
        )

    @app_commands.command(name="trader-add", description="Add a public Solana wallet to monitor.")
    async def trader_add(
        self,
        interaction: discord.Interaction,
        alias: str,
        wallet: str,
        weight: app_commands.Range[float, 0.1, 3.0] = 1.0,
    ) -> None:
        if not await self._require_admin(interaction):
            return
        try:
            normalized = str(Pubkey.from_string(wallet.strip()))
        except ValueError:
            await interaction.response.send_message(
                "That is not a valid Solana public wallet address.", ephemeral=True
            )
            return
        await self.bot.engine.database.add_trader(
            normalized, alias.strip()[:50], Decimal(str(weight))
        )
        await interaction.response.send_message(
            f"Added **{alias}** (`{_short(normalized)}`). The first scan backfills up to "
            f"{self.bot.settings.bootstrap_hours} hours without firing old signals.",
            ephemeral=True,
        )

    @app_commands.command(
        name="trader-import", description="Bulk import a CSV with alias,wallet,weight columns."
    )
    async def trader_import(
        self, interaction: discord.Interaction, csv_file: discord.Attachment
    ) -> None:
        if not await self._require_admin(interaction):
            return
        if csv_file.size > 100_000:
            await interaction.response.send_message("CSV must be under 100 KB.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            content = (await csv_file.read()).decode("utf-8-sig")
            rows = csv.DictReader(io.StringIO(content))
            if not rows.fieldnames or not {"alias", "wallet"}.issubset(rows.fieldnames):
                raise ValueError("CSV needs alias and wallet headers")
            added = 0
            errors: list[str] = []
            for line_number, row in enumerate(rows, start=2):
                if added >= 100:
                    errors.append("Stopped at 100 wallets")
                    break
                try:
                    alias = (row.get("alias") or "").strip()[:50]
                    wallet = str(Pubkey.from_string((row.get("wallet") or "").strip()))
                    weight = Decimal((row.get("weight") or "1").strip())
                    if not alias or not Decimal("0.1") <= weight <= Decimal("3"):
                        raise ValueError
                    await self.bot.engine.database.add_trader(wallet, alias, weight)
                    added += 1
                except (ValueError, ArithmeticError):
                    errors.append(f"line {line_number}")
            message = f"Imported **{added}** wallets."
            if errors:
                message += f" Skipped: {', '.join(errors[:15])}."
            await interaction.followup.send(message, ephemeral=True)
        except (UnicodeDecodeError, ValueError) as exc:
            await interaction.followup.send(f"Could not import CSV: {exc}", ephemeral=True)

    @app_commands.command(name="trader-remove", description="Stop monitoring a wallet.")
    async def trader_remove(self, interaction: discord.Interaction, alias_or_wallet: str) -> None:
        if not await self._require_admin(interaction):
            return
        removed = await self.bot.engine.database.remove_trader(alias_or_wallet.strip())
        await interaction.response.send_message(
            "Trader removed." if removed else "No matching trader was found.", ephemeral=True
        )

    @app_commands.command(name="traders", description="List every monitored wallet.")
    async def traders(self, interaction: discord.Interaction) -> None:
        traders = await self.bot.engine.database.list_traders()
        if not traders:
            await interaction.response.send_message("No wallets are being monitored yet.")
            return
        lines = [
            f"• **{item.alias}** — `{_short(item.address)}` — "
            f"weight {item.weight} — {item.source} — "
            f"{'enabled' if item.enabled else 'rotated out'}"
            for item in traders[:25]
        ]
        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(
        name="discover", description="Refresh strict 24H/7D metrics and rotate Pump wallets."
    )
    async def discover(self, interaction: discord.Interaction) -> None:
        if not await self._require_admin(interaction):
            return
        if not self.bot.settings.discovery_is_configured:
            await interaction.response.send_message(
                "Automatic discovery needs `SOLANA_TRACKER_API_KEY` in Railway Variables, "
                "with `AUTO_DISCOVERY_ENABLED=true`.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            refresh = await self.bot.engine.refresh_discovery(force=True)
        except DiscoveryError as exc:
            await interaction.followup.send(f"Discovery failed: {exc}", ephemeral=True)
            return
        except Exception:
            logger.exception("Manual wallet discovery failed")
            await interaction.followup.send(
                "Discovery hit an unexpected error. Open Railway Logs and copy the newest "
                "red error line (never include your API key).",
                ephemeral=True,
            )
            return
        candidates = (
            list(refresh.candidates)
            if refresh is not None
            else await self.bot.engine.database.list_discovered(limit=10)
        )
        embed = discord.Embed(
            title="Verified Meme-Coin Hot Wallets • 24H + 7D",
            description=_discovery_lines(candidates),
            color=0x9B59B6,
        )
        status = await self.bot.engine.status()
        embed.add_field(
            name="Pump social nominations",
            value=str(status["pump_profile_nominations"]),
        )
        embed.add_field(
            name="Social + financial matches",
            value=str(status["pump_profile_verified_matches"]),
        )
        embed.set_footer(
            text=(
                "Every displayed wallet has complete 24H + 7D PnL/ROI/win/trade "
                "evidence • social profiles nominate only"
            )
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="hot-wallets",
        description="Show why each rotating Pump wallet is active and how it performed.",
    )
    async def hot_wallets(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        reports = await self.bot.engine.database.hot_wallet_reports(limit=25)
        if not reports:
            await interaction.followup.send(
                "No Pump-verified hot wallets are active yet. Run `/smartmoney discover`.",
                ephemeral=True,
            )
            return
        lines: list[str] = []
        for report in reports[:10]:
            rolling_24h_change = report["pnl_24h"] - report["baseline_24h"]
            rolling_7d_change = report["pnl_7d"] - report["baseline_7d"]
            lines.append(
                f"**{report['rank']}. {report['alias']}** • `{_short(report['address'])}` • "
                f"score `{report['score']}`\n"
                f"24H `{_money(report['pnl_24h'])}` / `{report['roi_24h']:.1f}%` ROI / "
                f"`{report['win_24h']:.1f}%` win • "
                f"7D `{_money(report['pnl_7d'])}` / `{report['roi_7d']:.1f}%` ROI / "
                f"`{report['win_7d']:.1f}%` win\n"
                f"Recent swaps `{report['recent_swaps']}` • Pump `{report['pump_swaps']}` • "
                f"rolling change `24H {rolling_24h_change:+,.2f}` / "
                f"`7D {rolling_7d_change:+,.2f}`\n"
                f"Observed after admission: source PnL `{_money(report['observed_source_pnl'])}` "
                f"from `{report['observed_swaps']}` swaps • our PAPER PnL "
                f"`{_money(report['paper_pnl'])}` from `{report['paper_fills']}` fills / "
                f"`{report['paper_closed_sells']}` exits • PAPER PF "
                f"`{report['paper_profit_factor']:.2f}`"
            )
        embed = discord.Embed(
            title="Pump Hot-Wallet Evidence",
            description="\n\n".join(lines)[:4096],
            color=0x00B894,
        )
        embed.set_footer(
            text=(
                "Rolling PnL change is not exact period profit because old trades roll out; "
                "observed and PAPER PnL use only activity recorded after admission."
            )
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="candidates",
        description="Show the latest discovery funnel and exact rejection reasons.",
    )
    async def candidates(self, interaction: discord.Interaction) -> None:
        result = self.bot.engine.last_rotation_result
        if result is None:
            await interaction.response.send_message(
                "No in-memory candidate funnel yet. Run `/smartmoney discover` once after "
                "this deployment.",
                ephemeral=True,
            )
            return
        selected = {candidate.address for candidate in result.selected}
        lines = [
            f"Pool `{result.pool_size}` • Pump-verified `{result.verified_pump_wallets}` • "
            f"active `{len(result.selected)}`"
        ]
        for candidate in result.selected[:8]:
            lines.append(
                f"✅ **{candidate.alias}** `{_short(candidate.address)}` • "
                f"score `{candidate.score}` • Pump `{candidate.pump_swaps}`"
            )
        rejected = [
            candidate for candidate in result.evaluated if candidate.address not in selected
        ]
        for candidate in rejected[:12]:
            reason = result.rejection_reasons.get(
                candidate.address, "outranked by stronger current candidates"
            )
            lines.append(f"❌ **{candidate.alias}** `{_short(candidate.address)}` • {reason}")
        await interaction.response.send_message("\n".join(lines)[:4000], ephemeral=True)

    @app_commands.command(
        name="rotation", description="Show recent automatic wallet admissions and removals."
    )
    async def rotation(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        events = await self.bot.engine.database.rotation_events(limit=10)
        if not events:
            await interaction.followup.send("No rotation events recorded yet.", ephemeral=True)
            return
        lines = []
        for event in events:
            pnl_24h_change = event.pnl_24h_usd - event.baseline_pnl_24h_usd
            pnl_7d_change = event.pnl_7d_usd - event.baseline_pnl_7d_usd
            lines.append(
                f"**{event.action} • {event.alias}** • `{_short(event.address)}` • "
                f"<t:{event.recorded_at}:R>\n"
                f"{event.reason}\n"
                f"Rolling change `24H {pnl_24h_change:+,.2f}` / "
                f"`7D {pnl_7d_change:+,.2f}` • observed source "
                f"`{_money(event.observed_source_pnl_usd)}` • our PAPER "
                f"`{_money(event.paper_pnl_usd)}`"
            )
        embed = discord.Embed(
            title="Wallet Rotation Audit",
            description="\n\n".join(lines)[:4096],
            color=0x6C5CE7,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="sources", description="Show which platform data sources are actually connected."
    )
    async def sources(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        status = await self.bot.engine.status()
        stream_status = (
            f"connected ({status['stream_subscriptions']} subscriptions)"
            if status["stream_connected"]
            else "polling fallback"
        )
        x_status = (
            "disabled (zero-cost mode)"
            if not status["x_paid_search_enabled"]
            else f"working (last success <t:{status['x_social_last_success']}:R>)"
            if status["x_social_last_success"]
            else (
                f"configured but failing: {status['x_social_last_error']}"
                if status["x_social_last_error"]
                else "key configured; waiting for first search"
                if status["x_social_configured"]
                else "API key needed"
            )
        )
        news_stream_status = (
            "disabled (zero-cost mode)"
            if not status["x_paid_search_enabled"]
            else "disabled (budget mode)"
            if not status["x_news_stream_enabled"]
            else (
                "connected"
                if status["x_news_stream_connected"]
                else (
                    f"error: {status['x_news_stream_last_error']}"
                    if status["x_news_stream_last_error"]
                    else "starting"
                    if status["x_news_stream_configured"]
                    else "not configured"
                )
            )
        )
        x_launch_lane_status = (
            "disabled (zero-cost mode)"
            if not status["x_paid_search_enabled"]
            else "enabled"
            if status["x_social_configured"]
            else "credentials unavailable"
        )
        scan_counts = status["coin_scan_counts"]
        assert isinstance(scan_counts, dict)
        recent_scans = self.bot.engine.recent_coin_scans()
        recent_scan_lines: list[str] = []
        for item in recent_scans[:5]:
            social = item.social
            x_text = (
                f"X {social.posts} posts / {social.contract_authors} exact-contract authors"
                if social.available
                else f"X {social.error or 'not requested'}"
            )
            recent_scan_lines.append(
                f"• **{item.scan_stage}** `{_short(item.mint)}` • score `{item.score}` • "
                f"{x_text} • {item.scan_reason or item.verdict}"
            )
        recent_scan_text = (
            "\n**Recent coin/X scans:**\n" + "\n".join(recent_scan_lines) + "\n"
            if recent_scan_lines
            else "\n**Recent coin/X scans:** none completed since deployment\n"
        )
        radar_last_scan_text = (
            f"<t:{status['x_radar_last_scan']}:R>"
            if status["x_radar_last_scan"]
            else "not completed"
        )
        radar_error_text = (
            f" • error: {status['x_radar_last_error']}" if status["x_radar_last_error"] else ""
        )
        fomo_radar_error_text = (
            f" • {status['fomo_radar_last_error']}" if status["fomo_radar_last_error"] else ""
        )
        x_budget_text = (
            "disabled—zero X API spend"
            if not status["x_paid_search_enabled"]
            else (
                f"{status['x_budget']['verifications']}/"
                f"{status['x_budget']['verification_limit']} targeted checks • "
                f"requests {status['x_budget']['requests']} • "
                f"Posts {status['x_budget']['post_resources']} • "
                f"Users {status['x_budget']['user_resources']} • local estimate "
                f"${status['x_budget']['estimated_spend_today']}/"
                f"${status['x_budget']['daily_budget']} today"
            )
        )
        x_budget_last_success = status["x_budget"]["last_success"]
        x_budget_last_success_text = (
            f"<t:{x_budget_last_success}:R>" if x_budget_last_success else "none"
        )
        text = (
            "**Solana Tracker:** connected for strict 24H + 7D general-trader screening\n"
            "**Solana Tracker token safety:** risk score, rugged state, bundlers, "
            "insiders, snipers, developer and holder concentration\n"
            f"**Public-KOL period feed:** "
            f"{'enabled' if status['kol_discovery_enabled'] else 'disabled'} • authorized "
            "24H/7D nominations, never automatic trust\n"
            f"**Pump public profiles:** "
            f"{'enabled' if status['pump_profile_discovery_enabled'] else 'disabled'} • "
            f"{status['pump_profile_nominations']} public-wallet nominations • "
            f"{status['pump_profile_verified_matches']} also passed complete 24H + 7D "
            "financial verification\n"
            f"**Pump.fun activity:** verified through public Solana swaps and Pump mint "
            "identity\n"
            f"**Graduated Pump/Jupiter routes:** covered by the same persistent token mint\n"
            f"**Helius/Solana realtime:** {stream_status}\n"
            "**DEX Screener coin intelligence:** enabled for live pair liquidity, flow, "
            "volume, profiles, and paid-boost labeling\n"
            f"**X/Twitter coin intelligence:** {x_status}"
            " • exact-contract promotion • crypto-author quality • duplicate-text checks\n"
            f"**Paid X search budget:** {x_budget_text}\n"
            f"**X paid mode:** TARGETED VERIFICATION • max "
            f"{status['x_budget']['max_posts']} Posts/check • experiment local estimate "
            f"${status['x_budget']['estimated_spend_period']}/"
            f"${status['x_budget']['total_budget']} • actual invoice: Developer Console\n"
            f"**X outcomes today:** upgraded {status['x_budget']['upgraded']} • weak "
            f"{status['x_budget']['weak']} • last success "
            f"{x_budget_last_success_text}\n"
            f"**Proactive X radar:** "
            f"{'enabled' if status['x_radar_enabled'] else 'disabled'} • searches immediately "
            f"and every {status['x_radar_poll_seconds'] // 60}m • "
            f"{status['x_radar_scans']} completed • last scan "
            f"{radar_last_scan_text}"
            f" • last batch {status['x_radar_last_posts']} posts / "
            f"{status['x_radar_last_new_posts']} new / "
            f"{len(status['x_radar_last_contracts'])} contracts"
            f"{radar_error_text}\n"
            f"**Coin scan visibility:** {scan_counts.get('total', 0)} analyzed since restart • "
            f"{scan_counts.get('free_rejected', 0)} rejected before X • "
            f"{scan_counts.get('free_checked', 0)} free-data checked • "
            f"{scan_counts.get('x_checked', 0)} X-checked • "
            f"{scan_counts.get('watch', 0)} developing WATCH alerts • "
            f"{scan_counts.get('verified', 0)} VERIFIED TREND alerts • "
            f"{scan_counts.get('fomo_watch', 0)} FOMO WATCH alerts\n"
            + recent_scan_text
            + f"**Free Fomo radar:** "
            f"{'enabled' if status['fomo_radar_enabled'] else 'disabled'} • public DEX/Pump "
            f"profiles and trending nominations every {status['fomo_radar_poll_seconds'] // 60}m"
            f" • {status['fomo_radar_scans']} scans • last batch "
            f"{len(status['fomo_radar_last_candidates'])} Solana candidates"
            f"{fomo_radar_error_text}\n"
            f"**Fomo Runner Radar:** "
            f"{'shadow research enabled' if status['fomo_runner_enabled'] else 'disabled'} • "
            f"two-stage broad discovery + {status['fomo_runner_fast_watch_seconds']}s "
            f"temporary fast watch • {status['fomo_runner_observations']} persisted candidates • "
            f"digest {'enabled' if status['fomo_runner_digest_enabled'] else 'disabled'} • "
            "no auto-buy\n"
            f"**X near-realtime news stream:** {news_stream_status} • configured account/news "
            "rule • crypto-first filtering • exceptional U.S. event lane\n"
            f"**RSS/news radar:** {'ready' if status['news_rss_ready'] else 'starting'} • "
            "U.S. government/markets plus crypto sources; routine culture/sports removed\n"
            f"**Free launch candidates:** "
            f"{'enabled' if status['no_x_launch_candidates_enabled'] else 'disabled'} • "
            f"{status['no_x_launch_min_score']}+ • no X claim • manual launch only\n"
            f"**X-verified launch lane:** "
            f"{x_launch_lane_status}\n"
            f"**Launch artwork:** "
            f"{'source-led image' if status['news_source_image_enabled'] else 'fallback art'} "
            "• 1024x1024 • unofficial-meme label • IPFS upload\n"
            f"**One-click token launch:** "
            f"{'unlocked' if status['pump_launch_unlocked'] else 'locked'} • "
            f"provider {status['launch_provider']} • admin-only • topic artwork • "
            "public IPFS metadata\n"
            f"**J7 Tracker:** "
            + (
                f"authorized feed {status['j7_feed_health']}\n"
                if status["j7_feed_configured"]
                else "deploy API supported; social-feed API not publicly documented\n"
            )
            + "**Fomo:** the official app exposes leaderboards, profiles, follows, and alerts, "
            "but no documented public API/webhook was found. The bot will not claim a "
            "private endpoint is authorized; Fomo candidates require an official feed or a "
            "public wallet identity before the same full verification can run."
        )
        for chunk in _split_discord_text(text):
            await interaction.followup.send(chunk, ephemeral=True)

    @app_commands.command(
        name="coin", description="Cross-check a Solana coin across smart money, DEX flow, and X."
    )
    @app_commands.describe(mint="Solana token contract address")
    async def coin(self, interaction: discord.Interaction, mint: str) -> None:
        try:
            mint = str(Pubkey.from_string(mint.strip()))
        except ValueError:
            await interaction.response.send_message(
                "That is not a valid Solana token contract address.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        callout = await self.bot.engine.analyze_coin(mint, force_x_search=True)
        await interaction.followup.send(
            embed=_coin_callout_embed(callout),
            view=_token_view(mint, self.bot.settings.fomo_referral_code),
            ephemeral=True,
        )

    @app_commands.command(name="leaderboard", description="Risk-adjusted tracked-wallet ranking.")
    @app_commands.describe(window="Show 24-hour or 7-day performance")
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        window: Literal["24h", "7d"] = "24h",
    ) -> None:
        await interaction.response.defer(thinking=True)
        rankings = await self.bot.engine.rankings()
        if not rankings:
            await interaction.followup.send("No tracked-wallet data yet.")
            return
        lines: list[str] = []
        for index, item in enumerate(rankings[:10], start=1):
            metrics = item.metrics_24h if window == "24h" else item.metrics_7d
            lines.append(
                f"**{index}. {metrics.alias}** — score `{item.score}` • "
                f"PnL `{_money(metrics.realized_pnl_usd)}` • "
                f"win `{metrics.win_rate * 100:.1f}%` • trades `{metrics.trades}` • "
                f"DD `{_money(metrics.max_drawdown_usd)}`"
            )
        embed = discord.Embed(
            title=f"Tracked Wallet Leaderboard • {window}",
            description="\n".join(lines),
            color=0x8E44AD,
        )
        embed.set_footer(text="Score rewards repeatability and penalizes drawdown")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="scan", description="Run one wallet scan immediately.")
    async def scan(self, interaction: discord.Interaction) -> None:
        if not await self._require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        totals = await self.bot.engine.scan_once()
        await interaction.followup.send(
            f"Scan complete: {totals['wallets']} wallets, "
            f"{totals['transactions']} transactions, {totals['swaps']} swaps.",
            ephemeral=True,
        )

    @app_commands.command(name="paper", description="Show the paper-copy account scoreboard.")
    async def paper(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        summary = await self.bot.engine.paper_summary()
        daily_lock = await self.bot.engine.paper_daily_lock_status()
        closed = summary.wins + summary.losses
        win_rate = Decimal(summary.wins) / Decimal(closed) * 100 if closed else Decimal("0")
        total_pnl = summary.equity_usd - summary.starting_cash_usd
        total_roi = _return_percent(summary.equity_usd, summary.starting_cash_usd)
        daily_progress = daily_lock.marked_pnl_usd / daily_lock.target_usd * 100
        profit_factor = (
            f"{summary.profit_factor:.2f}"
            if summary.profit_factor is not None
            else "n/a (no losses yet)"
        )
        embed = discord.Embed(title="Paper Strategy Scoreboard", color=0x3498DB)
        embed.add_field(name="Equity", value=_money(summary.equity_usd))
        embed.add_field(name="Total P&L", value=_money(total_pnl))
        embed.add_field(name="Total ROI", value=f"{total_roi:+.2f}%")
        embed.add_field(name="Cash", value=_money(summary.cash_usd))
        embed.add_field(name="Positions", value=_money(summary.positions_value_usd))
        embed.add_field(name="Realized P&L", value=_money(summary.realized_pnl_usd))
        embed.add_field(name="Unrealized P&L", value=_money(summary.unrealized_pnl_usd))
        embed.add_field(
            name="Current giveback",
            value=_money(summary.current_drawdown_usd),
        )
        embed.add_field(name="Max drawdown", value=_money(summary.max_drawdown_usd))
        embed.add_field(name="Closed sells", value=str(summary.trades))
        embed.add_field(name="Win rate", value=f"{win_rate:.1f}%")
        embed.add_field(name="Profit factor", value=profit_factor)
        embed.add_field(name="Expectancy / exit", value=_money(summary.expectancy_usd))
        embed.add_field(
            name="Average win / loss",
            value=(f"{_money(summary.average_win_usd)} / -{_money(summary.average_loss_usd)}"),
        )
        embed.add_field(
            name="Today marked / guardrails",
            value=(
                f"{_money(daily_lock.marked_pnl_usd)} • "
                f"profit +{_money(daily_lock.target_usd)} "
                f"({daily_progress:+.1f}%) • "
                f"loss -{_money(daily_lock.loss_limit_usd)}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Daily entry status",
            value=(
                "LOCKED — positions liquidating; no more buys today"
                if daily_lock.locked
                else "ARMED — selective entries continue inside both daily limits"
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                "Quote-shadow PAPER uses Jupiter order quotes plus a conservative output buffer; "
                "the target is a benchmark, not a profit promise."
            )
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="readiness",
        description="Show whether the quote-shadow trial has passed every live-review gate.",
    )
    async def readiness(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        report = await self.bot.engine.paper_readiness()
        s = self.bot.settings
        quote_configured = (
            s.paper_use_executable_quotes
            and bool(s.jupiter_api_key)
            and not s.paper_force_observation_mode
        )
        display_ready = report.ready and quote_configured
        status = "READY FOR MICRO-LIVE REVIEW" if display_ready else "KEEP TESTING"
        color = 0x2ECC71 if display_ready else 0xF1C40F
        profit_factor = (
            f"{report.profit_factor:.2f}"
            if report.profit_factor is not None
            else ("no quoted losses yet" if report.gross_profit_usd > 0 else "n/a")
        )
        blockers = (
            "All configured gates passed. This is not a profit guarantee."
            if display_ready
            else "\n".join(f"• {item}" for item in report.blockers)
        )
        if not quote_configured:
            quote_blocker = (
                "• Forced observation mode is enabled; those fills are excluded from "
                "live-executable readiness. Disable it for the later quote-shadow trial.\n"
                if s.paper_force_observation_mode
                else "• JUPITER_API_KEY is missing or quote-shadow PAPER is disabled\n"
            )
            blockers = quote_blocker + blockers
        embed = discord.Embed(title=f"Paper Trial Readiness • {status}", color=color)
        embed.add_field(
            name="Trial started",
            value=f"<t:{report.trial_started_at}:f>",
            inline=False,
        )
        embed.add_field(
            name="Active test days",
            value=f"{report.active_days} / {s.readiness_min_active_days}",
        )
        embed.add_field(
            name="Quoted exits",
            value=f"{report.closed_trades} / {s.readiness_min_closed_trades}",
        )
        embed.add_field(name="Accepted entries", value=str(report.accepted_entries))
        embed.add_field(
            name="Quote reliability",
            value=(
                f"{report.quote_success_percent:.1f}% "
                f"({report.quote_successes}/{report.quote_attempts})"
            ),
        )
        embed.add_field(
            name="Profit factor",
            value=f"{profit_factor} / {s.readiness_min_profit_factor:.2f}+",
        )
        embed.add_field(name="Expectancy / exit", value=_money(report.expectancy_usd))
        embed.add_field(
            name="Trial max drawdown",
            value=(
                f"{_money(report.max_drawdown_usd)} "
                f"({report.max_drawdown_percent:.2f}% / "
                f"{s.readiness_max_drawdown_percent:.2f}% max)"
            ),
            inline=False,
        )
        embed.add_field(name="Remaining gates", value=blockers[:1024], inline=False)
        embed.set_footer(
            text=(
                "Passing means review a tiny live pilot next—not that $50-$100/day is guaranteed."
            )
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="paper-trades",
        description="Browse all paper buys, sells, ROI, and exit reasons with page buttons.",
    )
    async def paper_trades(
        self,
        interaction: discord.Interaction,
        page_size: app_commands.Range[int, 1, 5] = 5,
    ) -> None:
        view = await PaperTradesView.create(
            self.bot,
            interaction.user.id,
            page_size=page_size,
        )
        if not view.rows:
            await interaction.response.send_message(
                "No paper fills have been recorded yet.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=view.embed(),
            view=view,
            ephemeral=True,
        )

    @app_commands.command(name="positions", description="Show open paper positions.")
    async def positions(self, interaction: discord.Interaction) -> None:
        view = await PaperPositionsView.create(
            self.bot,
            interaction.user.id,
            can_sell=self._is_admin(interaction.user),
        )
        if not view.positions:
            await interaction.response.send_message("No open paper positions.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=view.embed(),
            view=view,
            ephemeral=True,
        )

    async def paper_demo(
        self,
        interaction: discord.Interaction,
        action: Literal["open", "close-win", "close-loss"] = "open",
    ) -> None:
        if not await self._require_admin(interaction):
            return
        if await self.bot.engine.execution_mode() is not ExecutionMode.PAPER:
            await interaction.response.send_message(
                "Paper demo only works in PAPER mode. Run `/smartmoney mode new_mode:paper` first.",
                ephemeral=True,
            )
            return

        positions = await self.bot.engine.database.paper_positions()
        position = next(
            (item for item in positions if item["token_mint"] == PAPER_DEMO_MINT),
            None,
        )
        if action == "open" and position is not None:
            await interaction.response.send_message(
                "The fake demo position is already open. Run `/smartmoney positions`, then "
                "close it with `close-win` or `close-loss`.",
                ephemeral=True,
            )
            return
        if action != "open" and position is None:
            await interaction.response.send_message(
                "No fake demo position is open. Run this command with action `open` first.",
                ephemeral=True,
            )
            return

        now = int(time.time())
        side = Side.BUY if action == "open" else Side.SELL
        market_price = {
            "open": PAPER_DEMO_ENTRY_PRICE,
            "close-win": PAPER_DEMO_ENTRY_PRICE * Decimal("1.30"),
            "close-loss": PAPER_DEMO_ENTRY_PRICE * Decimal("0.88"),
        }[action]
        signal = Signal(
            token_mint=PAPER_DEMO_MINT,
            side=side,
            created_at=now,
            trader_addresses=("PAPER_DEMO_1", "PAPER_DEMO_2", "PAPER_DEMO_3"),
            trader_aliases=("Paper Demo 1", "Paper Demo 2", "Paper Demo 3"),
            source_signatures=(f"paper-demo-{action}-{now}",),
            combined_score=Decimal("100"),
            reference_price_usd=market_price,
        )
        signal_id = await self.bot.engine.database.record_signal(signal)
        size = min(
            self.bot.settings.default_copy_usd,
            self.bot.settings.max_copy_usd,
        )
        fill = await self.bot.engine.database.paper_execute(
            signal_id=signal_id,
            token_mint=PAPER_DEMO_MINT,
            side=side,
            market_price_usd=market_price,
            size_usd=size,
            fee_bps=self.bot.settings.simulated_fee_bps,
            slippage_bps=self.bot.settings.simulated_slippage_bps,
        )
        if fill is None:
            await interaction.response.send_message(
                "The demo could not fill because the fake bankroll has no available cash. "
                "Reset it with `/smartmoney paper-reset confirmation:RESET PAPER`.",
                ephemeral=True,
            )
            return

        summary = await self.bot.engine.database.paper_summary({})
        if side is Side.BUY:
            embed = discord.Embed(
                title="DEMO PAPER BUY • FILLED",
                description=(
                    "A forced fake-money purchase was added to the same paper ledger used "
                    "by detected wallet signals. No real token or wallet was touched."
                ),
                color=0x2ECC71,
            )
            embed.add_field(name="Fake token", value="Paper Demo")
            embed.add_field(name="Paper spend", value=_money(size))
            embed.add_field(name="Execution price", value=_price(fill["price"]))
            embed.add_field(name="Quantity", value=f"{fill['quantity']:.6f}")
            embed.add_field(name="Simulated fee", value=_money(fill["fee"]))
            embed.add_field(name="Fake cash left", value=_money(summary.cash_usd))
            embed.set_footer(
                text="Next: /smartmoney positions → /smartmoney paper → paper-demo close-win"
            )
        else:
            cost = Decimal(str(position["cost_basis_usd"]))
            realized_roi = fill["realized_pnl"] / cost * Decimal("100") if cost else Decimal("0")
            scenario = "+30% market move" if action == "close-win" else "-12% market move"
            embed = discord.Embed(
                title=(
                    "DEMO PAPER SELL • WIN"
                    if fill["realized_pnl"] > 0
                    else "DEMO PAPER SELL • LOSS"
                ),
                description=(
                    "The fake position was sold with configured slippage and fees included. "
                    "This result now appears in the paper scoreboard."
                ),
                color=0x2ECC71 if fill["realized_pnl"] > 0 else 0xE74C3C,
            )
            embed.add_field(name="Scenario", value=scenario)
            embed.add_field(name="Exit price", value=_price(fill["price"]))
            embed.add_field(name="Sell fee", value=_money(fill["fee"]))
            embed.add_field(name="Realized P&L", value=_money(fill["realized_pnl"]))
            embed.add_field(name="Net trade ROI", value=f"{realized_roi:+.2f}%")
            embed.add_field(name="Ending equity", value=_money(summary.equity_usd))
            embed.set_footer(
                text="Run /smartmoney paper. Use paper-reset when you want a clean real test."
            )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="paper-reset", description="Reset the paper bankroll and history.")
    async def paper_reset(self, interaction: discord.Interaction, confirmation: str) -> None:
        if not await self._require_admin(interaction):
            return
        if confirmation != "RESET PAPER":
            await interaction.response.send_message(
                "Type `RESET PAPER` exactly to confirm.", ephemeral=True
            )
            return
        await self.bot.engine.database.reset_paper()
        await interaction.response.send_message(
            "Paper account and quote-readiness trial reset. The new test clock starts now.",
            ephemeral=True,
        )

    @app_commands.command(name="mode", description="Show or change execution mode.")
    async def mode(
        self,
        interaction: discord.Interaction,
        new_mode: Literal["show", "alerts", "paper", "live"] = "show",
        confirmation: str | None = None,
    ) -> None:
        if new_mode == "show":
            current = await self.bot.engine.execution_mode()
            await interaction.response.send_message(
                f"Current mode: **{current.value}**. Live environment lock: "
                f"**{'unlocked' if self.bot.settings.live_is_unlocked else 'locked'}**.",
                ephemeral=True,
            )
            return
        if not await self._require_admin(interaction):
            return
        chosen = ExecutionMode(new_mode.upper())
        if chosen is ExecutionMode.LIVE and confirmation != "ENABLE LIVE":
            await interaction.response.send_message(
                "Type `ENABLE LIVE` in confirmation. The environment lock must also be configured.",
                ephemeral=True,
            )
            return
        try:
            await self.bot.engine.set_execution_mode(chosen)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Execution mode changed to **{chosen.value}**.", ephemeral=True
        )

    @app_commands.command(name="pause", description="Pause or resume background monitoring.")
    async def pause(
        self, interaction: discord.Interaction, action: Literal["pause", "resume"]
    ) -> None:
        if not await self._require_admin(interaction):
            return
        paused = action == "pause"
        await self.bot.engine.set_paused(paused)
        await interaction.response.send_message(
            "Monitoring paused." if paused else "Monitoring resumed.", ephemeral=True
        )

    @app_commands.command(
        name="kill-switch",
        description="Immediately pause discovery, scanning, and new paper actions.",
    )
    async def kill_switch(self, interaction: discord.Interaction) -> None:
        if not await self._require_admin(interaction):
            return
        await self.bot.engine.set_paused(True)
        await interaction.response.send_message(
            "Kill switch engaged. Monitoring is paused; existing PAPER data is preserved. "
            "Resume with `/smartmoney pause action:resume`.",
            ephemeral=True,
        )

    @app_commands.command(
        name="launch-check",
        description="Verify J7, IPFS, wallet balance, and launch guards without spending SOL.",
    )
    async def launch_check(self, interaction: discord.Interaction) -> None:
        if not await self._require_admin(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        report = await self.bot.engine.launch_readiness()
        await interaction.followup.send(embed=_launch_readiness_embed(report), ephemeral=True)

    @app_commands.command(
        name="launch-lab",
        description="Browse and prepare the strongest current real narrative candidates.",
    )
    @app_commands.describe(
        mode="Production candidates or a deterministic research-only pipeline test",
        topic="Optional topic preference or legitimate public HTTPS article URL",
    )
    async def launch_lab(
        self,
        interaction: discord.Interaction,
        mode: Literal["production", "test"] = "production",
        topic: str = "",
    ) -> None:
        if not await self._require_admin(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        research_test = mode == "test"
        candidates = (
            await self.bot.engine.launch_lab_test_candidates(topic=topic)
            if research_test
            else await self.bot.engine.launch_lab_candidates(topic=topic)
        )
        if not candidates:
            if research_test:
                await interaction.followup.send(
                    "No usable recent item was returned by the configured public RSS feeds. "
                    "No candidate was fabricated. Retry with `/smartmoney launch-lab "
                    "mode:test topic:https://legitimate-public-source/article`.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                "No recent candidate currently passes the Launch Lab floor. The automatic "
                f"{self.bot.settings.no_x_launch_min_score}+ threshold was not lowered. "
                "The bot refreshed authorized RSS feeds; try again after the next real event"
                + (f" matching `{topic[:100]}`." if topic else "."),
                ephemeral=True,
            )
            return
        try:
            balance = await self.bot.engine.pump_launcher.j7.wallet_balance()
        except PumpLaunchError:
            balance = None
        view = LaunchLabView(
            self.bot,
            candidates,
            owner_id=interaction.user.id,
            balance=balance,
            research_test=research_test,
        )
        embed, file = await view.preview()
        await interaction.followup.send(
            embed=embed,
            file=file,
            view=view,
            ephemeral=True,
            wait=True,
        )

    @app_commands.command(name="status", description="Check bot, RPC, and monitor health.")
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        status = await self.bot.engine.status()
        s = self.bot.settings
        daily_lock = status["paper_daily_profit_lock"]
        assert isinstance(daily_lock, PaperDailyLockStatus)
        last_scan = f"<t:{status['last_scan']}:R>" if status["last_scan"] else "not completed yet"
        discovery_refresh = (
            f"<t:{status['discovery_last_refresh']}:R>"
            if status["discovery_last_refresh"]
            else "not completed yet"
        )
        weekly_refresh = (
            f"<t:{status['discovery_7d_last_refresh']}:R>"
            if status["discovery_7d_last_refresh"]
            else "not completed yet"
        )
        rotation_refresh = (
            f"<t:{status['rotation_last_refresh']}:R>"
            if status["rotation_last_refresh"]
            else "not completed yet"
        )
        x_callout_health = (
            "disabled • zero X API spend"
            if not status["x_paid_search_enabled"]
            else f"working • last success <t:{status['x_social_last_success']}:R>"
            if status["x_social_last_success"]
            else (
                f"ERROR • {status['x_social_last_error']}"
                if status["x_social_last_error"]
                else "key configured • awaiting first request"
                if status["x_social_configured"]
                else "key needed"
            )
        )
        x_news_health = (
            "disabled (zero-cost mode)"
            if not status["x_paid_search_enabled"]
            else "disabled (budget mode)"
            if not status["x_news_stream_enabled"]
            else (
                "connected"
                if status["x_news_stream_connected"]
                else (
                    f"error • {status['x_news_stream_last_error']}"
                    if status["x_news_stream_last_error"]
                    else "starting"
                    if status["x_news_stream_configured"]
                    else "not configured"
                )
            )
        )
        x_verified_lane_status = (
            "disabled (zero-cost mode)"
            if not status["x_paid_search_enabled"]
            else "enabled"
            if status["x_social_configured"]
            else "unavailable (X credentials missing)"
        )
        j7_launch_status = (
            "ready"
            if status["pump_launch_unlocked"] and status["launch_provider"] == "J7 Tracker"
            else "not selected"
            if status["pump_launch_unlocked"]
            else "locked"
        )
        x_radar_health = (
            "active"
            if status["x_paid_search_enabled"]
            and status["x_radar_enabled"]
            and status["x_social_configured"]
            else "inactive"
        )
        raw_buy_ping_status = (
            "muted"
            if not status["trade_activity_alerts_enabled"]
            else "ready"
            if self.bot.settings.discord_alert_user_id
            else "user ID not set"
        )
        x_search_text = (
            "disabled—zero X API spend"
            if not status["x_paid_search_enabled"]
            else (
                f"targeted {status['x_budget']['verifications']}/"
                f"{status['x_budget']['verification_limit']} • requests "
                f"{status['x_budget']['requests']} • Posts "
                f"{status['x_budget']['post_resources']} • local estimate "
                f"${status['x_budget']['estimated_spend_today']}/"
                f"${status['x_budget']['daily_budget']}"
            )
        )
        rss_health = (
            f"ready • last refresh <t:{status['news_rss_last_refresh']}:R>"
            if status["news_rss_last_refresh"]
            else (
                f"error • {status['news_rss_last_error']}"
                if status["news_rss_last_error"]
                else "starting"
            )
        )
        runner_last = (
            f"<t:{status['fomo_runner_last_evaluated']}:R>"
            if status["fomo_runner_last_evaluated"]
            else "not completed yet"
        )
        stream_status = (
            f"connected • {status['stream_subscriptions']} wallet subscriptions"
            if status["stream_connected"]
            else (
                f"fallback polling • {status['stream_last_error']}"
                if status["stream_last_error"]
                else "starting/fallback polling"
            )
        )
        paper_copy = (
            "every new tracked-wallet BUY/SELL"
            if status["paper_mirror_raw_swaps"]
            else "consensus signals only"
        )
        observation_mode = bool(status["paper_force_observation_mode"])
        paper_entry = (
            "source transaction price + configured adverse penalty"
            if observation_mode
            else ("current price required" if s.paper_require_current_price else "fallback allowed")
        )
        fill_policy = (
            "FORCE OBSERVATION (PAPER only)" if observation_mode else "executable quote shadow"
        )
        raw_entry_gate = (
            "bypassed by forced PAPER observation"
            if observation_mode
            else _raw_entry_gate_status(s.paper_raw_entry_filter_enabled)
        )
        pump_verified = status["rotation_verified_pump_wallets"]
        pump_verified_text = str(pump_verified) if pump_verified is not None else "not checked yet"
        text = (
            f"**Bot version:** {BOT_VERSION}\n"
            f"**RPC:** {status['rpc']}\n"
            f"**RPC throttle:** {self.bot.settings.rpc_requests_per_second}/second • "
            f"{self.bot.settings.rpc_max_retries} retries\n"
            f"**Mode:** {status['mode']}\n"
            f"**Paper auto-copy:** {paper_copy}\n"
            f"**Paper fill policy:** {fill_policy}\n"
            f"**Daily PAPER guard:** "
            f"{'LOCKED' if daily_lock.locked else 'armed'} • "
            f"{_money(daily_lock.marked_pnl_usd)} • limits "
            f"+{_money(daily_lock.target_usd)} / "
            f"-{_money(daily_lock.loss_limit_usd)} • "
            f"{s.paper_daily_lock_timezone} • "
            f"check every {s.paper_daily_profit_check_seconds}s\n"
            f"**Daily-lock open positions:** {daily_lock.open_positions}\n"
            f"**Paper entry price:** {paper_entry}\n"
            f"**Observation penalty:** {s.paper_observation_penalty_bps}bps/side\n"
            f"**Existing-holding baselines:** "
            f"{'enabled' if s.paper_seed_tracking_baselines else 'disabled'} • up to "
            f"{s.paper_baseline_max_positions_per_wallet} per wallet\n"
            f"**Pump PAPER fallback:** "
            f"{'enabled' if s.paper_allow_pump_source_fallback else 'disabled'}"
            f" • {s.paper_pump_source_fallback_bps}bps adverse penalty\n"
            f"**Sniper PAPER lane:** "
            f"{'enabled' if s.paper_sniper_test_enabled else 'disabled'} • "
            f"{_money(s.paper_sniper_copy_usd)} max • "
            f"minimum {_money(s.paper_sniper_min_liquidity_usd)} liquidity / "
            f"{s.paper_sniper_min_holders} holders • PAPER only\n"
            f"**Raw entry safety gate:** {raw_entry_gate}\n"
            f"**Executable quote shadow:** "
            f"{'ready' if status['quote_ready'] else 'JUPITER_API_KEY needed'}\n"
            f"**Consecutive quote failures:** {status['consecutive_quote_failures']} / "
            f"{s.max_consecutive_quote_failures}\n"
            f"**Raw-buy pings:** {raw_buy_ping_status}\n"
            f"**Trade activity alerts:** "
            f"{'enabled' if status['trade_activity_alerts_enabled'] else 'muted—callouts only'}\n"
            f"**Coin callouts:** "
            f"{'enabled' if status['coin_callouts_enabled'] else 'disabled'} • "
            "free FOMO WATCH plus optional X VERIFIED TREND • cross-source liquidity • "
            "$5 executable route • complete Tracker/holder proof • "
            f"X {x_callout_health} • "
            f"X minimum {max(s.coin_callout_min_alert_score, Decimal('70'))}/100 • "
            f"free Fomo minimum {s.fomo_watch_min_score}/100\n"
            f"**X search budget:** {x_search_text} • free prefilter "
            f"{s.coin_x_prefilter_min_score}/100 • optional X WATCH alerts "
            f"{'enabled' if status['coin_watch_alerts_enabled'] else 'disabled'}\n"
            f"**Proactive X radar:** {x_radar_health}"
            f" • every {status['x_radar_poll_seconds'] // 60}m • "
            f"{status['x_radar_scans']} searches since restart • last batch "
            f"{status['x_radar_last_posts']} posts / "
            f"{len(status['x_radar_last_contracts'])} contracts\n"
            f"**Free Fomo radar:** "
            f"{'active' if status['fomo_radar_enabled'] else 'disabled'} • every "
            f"{status['fomo_radar_poll_seconds'] // 60}m • "
            f"{status['fomo_radar_scans']} scans • latest public candidate batch "
            f"{len(status['fomo_radar_last_candidates'])}\n"
            f"**Fomo Runner Radar:** "
            f"{'SHADOW / RESEARCH' if status['fomo_runner_enabled'] else 'disabled'} • "
            f"fast-watch `{status['fomo_runner_fast_watch_active']}` • observations "
            f"`{status['fomo_runner_observations']}` • last evaluation "
            f"{runner_last} • "
            f"research digest "
            f"{'enabled' if status['fomo_runner_digest_enabled'] else 'disabled'} • "
            "no automatic buys\n"
            f"**News radar:** {'enabled' if status['news_radar_enabled'] else 'disabled'} • "
            "crypto-first • exceptional U.S. events require independent confirmation • "
            f"X stream {x_news_health} • RSS {rss_health}\n"
            f"**Free launch candidates:** "
            f"{'enabled' if status['no_x_launch_candidates_enabled'] else 'disabled'} • "
            f"minimum {status['no_x_launch_min_score']}/100 • manual launch only\n"
            f"**X verified launch lane:** {x_verified_lane_status}\n"
            f"**J7 launch provider:** {j7_launch_status}\n"
            f"**Manual Launch Lab:** "
            f"{'enabled' if status['launch_lab_enabled'] else 'disabled'} • recent candidates "
            f"{status['launch_lab_min_score']}+ • J7-only • public wallet "
            f"{'configured' if status['j7_public_wallet_configured'] else 'needed'}\n"
            f"**Launch artwork:** "
            f"{'source-led' if status['news_source_image_enabled'] else 'fallback only'} • "
            "three ranked candidates • 1024x1024 IPFS PNG\n"
            f"**Launch hierarchy:** WATCH • NO-X CANDIDATE {s.no_x_launch_min_score}+ • "
            f"X VERIFIED READY {s.news_launch_ready_score}+ • "
            "source/speed/meme/confirmation/competition/identity\n"
            f"**One-click launch:** "
            f"{'unlocked' if status['pump_launch_unlocked'] else 'locked'} • "
            f"provider {status['launch_provider']} • "
            f"{s.pump_launch_initial_buy_sol} SOL initial buy • max "
            f"{s.pump_launch_max_per_day}/day and "
            f"{s.pump_launch_max_sol_per_day} SOL/day\n"
            f"**Narrative pair matching:** "
            f"{'enabled' if s.news_dex_match_enabled else 'disabled'} • minimum "
            f"{_money(s.news_dex_match_min_liquidity_usd)} liquidity • maximum "
            f"{s.news_dex_match_max_age_minutes}m old\n"
            f"**Paused:** {status['paused']}\n"
            f"**Tracked wallets:** {status['wallets']}\n"
            f"**Exit-only wallets:** {status['exit_only_wallets']} "
            "(kept subscribed until linked PAPER lots close)\n"
            f"**Automatic discovery:** "
            f"{'ready' if status['discovery_configured'] else 'API key needed'}\n"
            f"**Discovered wallets:** {status['discovered_wallets']}\n"
            f"**Multi-source strict candidate pool:** {status['candidate_pool_size']}\n"
            f"**Pump social nominations:** {status['pump_profile_nominations']} • "
            f"financially verified matches {status['pump_profile_verified_matches']}\n"
            f"**Pump-verified candidates:** {pump_verified_text}\n"
            f"**24H discovery refresh:** {discovery_refresh}\n"
            f"**7D verification refresh:** {weekly_refresh}\n"
            f"**Five-minute rotation:** {rotation_refresh}\n"
            f"**Realtime wallet stream:** {stream_status} • "
            f"{status['stream_commitment']} trigger\n"
            f"**Last scan:** {last_scan}\n"
            f"**Last error:** {status['last_error'] or 'none'}"
        )
        chunks = _split_discord_text(text)
        for index, chunk in enumerate(chunks, start=1):
            prefix = f"**Status {index}/{len(chunks)}**\n" if len(chunks) > 1 else ""
            await interaction.followup.send(f"{prefix}{chunk}", ephemeral=True)

    @app_commands.command(name="limits", description="Show the active strategy and risk limits.")
    async def limits(self, interaction: discord.Interaction) -> None:
        s = self.bot.settings
        paper_copy = (
            "every new raw tracked-wallet swap" if s.paper_mirror_raw_swaps else "consensus only"
        )
        fill_policy = (
            "force every valid detected swap (observation only)"
            if s.paper_force_observation_mode
            else "executable quote shadow"
        )
        raw_entry_gate = (
            "bypassed by forced PAPER observation"
            if s.paper_force_observation_mode
            else _raw_entry_gate_status(s.paper_raw_entry_filter_enabled)
        )
        text = (
            f"**Paper auto-copy:** {paper_copy}\n"
            f"**Paper fill policy:** {fill_policy}\n"
            f"**Observation penalty:** {s.paper_observation_penalty_bps}bps/side\n"
            f"**Existing-holding baselines:** "
            f"{'enabled' if s.paper_seed_tracking_baselines else 'disabled'} • up to "
            f"{s.paper_baseline_max_positions_per_wallet} per wallet\n"
            f"**Paper entry price:** "
            f"{'current price required' if s.paper_require_current_price else 'fallback allowed'}\n"
            f"**Pump source-price fallback:** "
            f"{'enabled' if s.paper_allow_pump_source_fallback else 'disabled'}"
            f" • {s.paper_pump_source_fallback_bps}bps adverse penalty"
            " • PAPER only\n"
            f"**Sniper PAPER lane:** "
            f"{'enabled' if s.paper_sniper_test_enabled else 'disabled'} • "
            f"{_money(s.paper_sniper_copy_usd)} launch-stage position • "
            f"floor {_money(s.paper_sniper_min_liquidity_usd)} liquidity / "
            f"{s.paper_sniper_min_holders} holders • "
            f"max {s.paper_sniper_max_entry_drift_percent}% drift / "
            f"{s.paper_sniper_max_quote_price_impact_percent}% impact • "
            f"{s.paper_sniper_source_penalty_bps}bps source fallback • "
            "excluded from live readiness\n"
            f"**Raw entry safety gate:** {raw_entry_gate}\n"
            f"**Quote-shadow PAPER:** "
            f"{'enabled' if s.paper_use_executable_quotes else 'disabled'}\n"
            f"**Entry chase limit:** +{s.max_adverse_entry_drift_percent}%\n"
            f"**Maximum entry price impact:** {s.max_quote_price_impact_percent}%\n"
            f"**Quote latency / failure lock:** {s.max_quote_latency_ms}ms / "
            f"{s.max_consecutive_quote_failures} consecutive failures\n"
            f"**Paper quote output buffer:** {s.paper_quote_output_buffer_bps}bps\n"
            f"**Consensus:** {s.consensus_min_traders} traders within "
            f"{s.consensus_window_seconds}s\n"
            f"**Minimum trader score:** {s.min_trader_score}/100\n"
            f"**Candidate pool / hot set:** up to {s.discovery_fetch_limit} / "
            f"{s.discovery_max_wallets}\n"
            f"**Leaderboard pages/window:** {s.discovery_candidate_pages} • "
            f"up to {s.discovery_candidate_pages * min(s.discovery_fetch_limit, 100)} rows\n"
            f"**Public-KOL period feed:** "
            f"{'enabled' if s.discovery_include_kols else 'disabled'} • up to "
            f"{s.discovery_kol_limit} rows/window\n"
            f"**24H verification:** every {s.effective_discovery_refresh_seconds // 60}m • "
            f"minimum {_money(s.discovery_min_24h_pnl_usd)} PnL • "
            f"{s.discovery_min_roi_percent}% ROI • "
            f"{s.discovery_min_win_rate_percent}% win\n"
            f"**7D verification:** every {s.effective_discovery_7d_refresh_seconds // 3600}h • "
            f"minimum {_money(s.discovery_min_7d_pnl_usd)} PnL • "
            f"{s.discovery_min_7d_roi_percent}% ROI • "
            f"{s.discovery_min_7d_win_rate_percent}% win\n"
            f"**Hot rotation:** every {s.rotation_refresh_seconds // 60}m • "
            f"idle after {s.rotation_max_idle_seconds // 60}m • "
            f"minimum {s.rotation_min_recent_swaps} recent swap / "
            f"{s.rotation_min_pump_swaps} Pump swap\n"
            f"**Mature forward-wallet removal:** after "
            f"{s.forward_evidence_min_closed_sells} PAPER exits • PF below "
            f"{s.forward_evidence_min_profit_factor} or loss at least "
            f"-{_money(s.forward_evidence_max_loss_usd)}\n"
            f"**RPC scanning:** every {s.poll_interval_seconds}s • "
            f"{s.rpc_requests_per_second} requests/second maximum\n"
            f"**Copy size:** {_money(s.default_copy_usd)} (max {_money(s.max_copy_usd)})\n"
            f"**Daily profit/loss lock:** +"
            f"{_money(s.paper_daily_target_usd)} / "
            f"-{_money(s.paper_daily_loss_limit_usd)} marked P&L • "
            f"profit {'on' if s.paper_daily_profit_lock_enabled else 'off'} / "
            f"loss {'on' if s.paper_daily_loss_lock_enabled else 'off'} • "
            f"check every {s.paper_daily_profit_check_seconds}s • "
            f"reset {s.paper_daily_lock_timezone}\n"
            f"**Daily stop:** -{_money(s.max_daily_loss_usd)}\n"
            f"**Minimum liquidity:** {_money(s.min_token_liquidity_usd)}\n"
            f"**Max positions:** {s.max_open_positions}\n"
            f"**Signal max age:** {s.max_signal_age_seconds}s\n"
            f"**Raw-lot exposure cap:** {_money(s.max_copy_usd)} per wallet/token\n"
            f"**Raw hard stop / take profit:** "
            f"{s.raw_mirror_stop_loss_percent}% / "
            f"{s.raw_mirror_take_profit_percent}%\n"
            f"**Raw trailing lock:** activates +"
            f"{s.raw_mirror_trailing_activation_percent}% • trails "
            f"{s.raw_mirror_trailing_stop_percent}%\n"
            f"**Raw maximum hold:** {s.raw_mirror_max_hold_seconds // 60}m\n"
            f"**Source sells:** still mirrored while the guarded lot remains open\n"
            f"**Consensus/live stop / take profit:** "
            f"{s.stop_loss_percent}% / {s.take_profit_percent}%\n"
            f"**Maximum hold:** {s.max_hold_seconds // 3600}h"
        )
        await interaction.response.send_message(text, ephemeral=True)

    async def help(self, interaction: discord.Interaction) -> None:
        text = (
            "1. `/smartmoney setup` — choose the alert channel\n"
            "2. Add `SOLANA_TRACKER_API_KEY` in Railway for automatic discovery\n"
            "3. `/smartmoney discover` — verify general + public-KOL 24H/7D profit "
            "and Pump activity\n"
            "4. `/smartmoney scan` — run an immediate on-chain scan\n"
            "5. `/smartmoney hot-wallets`, `/smartmoney candidates`, and "
            "`/smartmoney rotation` — inspect the funnel\n"
            "6. Keep `/smartmoney mode paper` to auto-mirror every new tracked-wallet swap\n"
            "7. `/smartmoney positions`, `paper`, and `paper-trades` — inspect results\n"
            "8. `/smartmoney readiness` — see the exact gates before any tiny live pilot\n"
            "Emergency stop: `/smartmoney kill-switch`\n"
            "Manual `trader-add` and CSV import remain optional overrides."
        )
        await interaction.response.send_message(text, ephemeral=True)


class FomoCommands(
    commands.GroupCog,
    group_name="fomo",
    group_description="Research existing-token runner candidates without buying.",
):
    """Separate existing-token product; avoids Discord's 25-child smartmoney limit."""

    def __init__(self, bot: SmartMoneyBot) -> None:
        self.bot = bot

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        if _member_is_admin(interaction.user, self.bot.settings):
            return True
        await interaction.response.send_message(
            "You need Administrator or a configured bot-admin role for Fomo Runner Lab.",
            ephemeral=True,
        )
        return False

    @app_commands.command(
        name="lab",
        description="Browse real current existing-token runner research candidates.",
    )
    @app_commands.describe(mode="Production research floor or deterministic real-token test")
    async def lab(
        self,
        interaction: discord.Interaction,
        mode: Literal["production", "test"] = "production",
    ) -> None:
        if not await self._require_admin(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        # Every exit below must replace the deferred response. An exception that
        # escapes this callback leaves Discord parked on "Investing is
        # thinking..." forever, which is exactly the v2.33.2/v2.33.3 regression.
        try:
            async with asyncio.timeout(FOMO_LAB_TOTAL_DEADLINE_SECONDS):
                await self._lab_response(interaction, research_test=mode == "test")
        except TimeoutError:
            await self._resolve_lab(
                interaction,
                content=(
                    "Fomo Runner Lab exceeded its "
                    f"{FOMO_LAB_TOTAL_DEADLINE_SECONDS}-second hard deadline and was "
                    "cancelled. No X credits, SOL, buy, or J7 launch was used."
                ),
            )
        except Exception as exc:
            await self._resolve_lab(
                interaction,
                content=(
                    "Fomo Runner Lab failed unexpectedly: "
                    f"`{type(exc).__name__}`. No buy or launch was attempted."
                ),
            )

    async def _resolve_lab(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> None:
        """Replace the deferred response, degrading to text if the card is rejected.

        Discord can refuse a card (oversized embed, malformed component) with
        HTTP 400.  Letting that propagate would strand the spinner, so fall back
        to a visible plain-text explanation instead.
        """

        try:
            await interaction.edit_original_response(
                content=content, embed=embed, view=view
            )
            return
        except Exception:
            if embed is None and view is None:
                # Nothing left to degrade to; the interaction itself is gone.
                logger.exception("Fomo Runner Lab could not resolve its deferred response")
                return
            logger.exception("Fomo Runner Lab card was rejected by Discord")
        with suppress(Exception):
            await interaction.edit_original_response(
                content=(
                    "Fomo Runner Lab found a real candidate but Discord rejected the "
                    "rendered card. Nothing was fabricated and no buy or launch was "
                    "attempted."
                ),
                embed=None,
                view=None,
            )

    async def _lab_response(
        self,
        interaction: discord.Interaction,
        *,
        research_test: bool,
    ) -> None:
        if research_test:
            try:
                async with asyncio.timeout(FOMO_LAB_CACHE_DEADLINE_SECONDS):
                    cached = await self.bot.engine.runner_lab_cached_candidates(
                        research_test=True,
                        max_age_seconds=86_400,
                    )
            except TimeoutError:
                await self._resolve_lab(
                    interaction,
                    content=(
                        "Fomo Runner Lab could not read the saved runner pool within "
                        "five seconds. No provider, X, buy, SOL, or J7 action was used."
                    ),
                    embed=None,
                    view=None,
                )
                return
            except Exception as exc:
                await self._resolve_lab(
                    interaction,
                    content=(
                        "Fomo Runner Lab could not read the saved runner pool: "
                        f"`{type(exc).__name__}`. No buy or launch was attempted."
                    ),
                    embed=None,
                    view=None,
                )
                return
            if cached:
                view = FomoRunnerLabView(
                    self.bot,
                    cached,
                    owner_id=interaction.user.id,
                    research_test=True,
                )
                await self._resolve_lab(
                    interaction,
                    content=None,
                    embed=view.embed(),
                    view=view,
                )
                return
            await self._resolve_lab(
                interaction,
                content="Refreshing one real public candidate...",
                embed=None,
                view=None,
            )
        try:
            async with asyncio.timeout(FOMO_LAB_REFRESH_DEADLINE_SECONDS):
                candidates = await self.bot.engine.runner_lab_candidates(
                    research_test=research_test
                )
        except TimeoutError:
            await self._resolve_lab(
                interaction,
                content=(
                    "Fomo Runner Lab timed out while live providers were responding. "
                    "The command was safely cancelled; no X credits, SOL, buy, or J7 "
                    "launch was used. Try again after the background radar caches a "
                    "current candidate."
                ),
                embed=None,
                view=None,
            )
            return
        except Exception as exc:
            await self._resolve_lab(
                interaction,
                content=(
                    "Fomo Runner Lab could not complete the current public-data refresh: "
                    f"`{type(exc).__name__}`. No buy or launch was attempted."
                ),
                embed=None,
                view=None,
            )
            return
        if not candidates:
            await self._resolve_lab(
                interaction,
                content=(
                    "No real public Solana token with usable current market data was "
                    "returned. Nothing was fabricated and no X request or buy was made."
                ),
                embed=None,
                view=None,
            )
            return
        view = FomoRunnerLabView(
            self.bot,
            candidates,
            owner_id=interaction.user.id,
            research_test=research_test,
        )
        await self._resolve_lab(
            interaction,
            content=None,
            embed=view.embed(),
            view=view,
        )

    @app_commands.command(
        name="results",
        description="Show forward runner outcomes and hit rates without profit claims.",
    )
    async def results(self, interaction: discord.Interaction) -> None:
        if not await self._require_admin(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        result = await self.bot.engine.runner_results()
        horizon_names = {
            60: "1m",
            300: "5m",
            900: "15m",
            1_800: "30m",
            3_600: "1h",
            14_400: "4h",
            86_400: "24h",
        }
        lines: list[str] = []
        by_horizon = result["by_horizon"]
        assert isinstance(by_horizon, dict)
        for horizon, name in horizon_names.items():
            row = by_horizon[horizon]
            assert isinstance(row, dict)
            average = row["average"]
            lines.append(
                f"**{name}:** `{row['count']}` outcomes • avg "
                f"`{f'{average:+.2f}%' if isinstance(average, Decimal) else 'pending'}` • "
                f"hits +10/+25/+50/+100 `"
                f"{row['hit_10']}/{row['hit_25']}/{row['hit_50']}/{row['hit_100']}` • "
                f"rug/liquidity failures `{row['failures']}`"
            )
        embed = discord.Embed(
            title="FOMO RUNNER SHADOW RESULTS",
            description=(
                f"Candidates tracked: **{result['candidates']}**\n"
                f"Forward observations: **{result['outcomes']}**\n"
                f"All-horizon average: `{result['average_return']:+.2f}%` • "
                f"median: `{result['median_return']:+.2f}%`\n\n" + "\n".join(lines)
            ),
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Are we early?",
            value=(
                "Average detection delay from DEX pair creation proxy: "
                f"`{result['average_detection_delay_seconds']}s`"
                if result["average_detection_delay_seconds"] is not None
                else "Pair-creation timing evidence is still collecting."
            ),
            inline=False,
        )
        breakdowns = result["breakdowns"]
        assert isinstance(breakdowns, dict)

        def bucket_lines(group: str) -> str:
            rows = breakdowns.get(group, {})
            assert isinstance(rows, dict)
            values: list[str] = []
            for label, row in rows.items():
                assert isinstance(row, dict)
                average = row["average"]
                average_text = f"{average:+.2f}%" if isinstance(average, Decimal) else "pending"
                hit_rate = row["hit_25_percent"]
                failure_rate = row["failure_rate_percent"]
                values.append(
                    f"`{label}` n={row['count']} • avg `{average_text}` • "
                    f"+25 hit `"
                    f"{f'{hit_rate:.2f}%' if isinstance(hit_rate, Decimal) else 'pending'}` • "
                    f"failure `"
                    f"{f'{failure_rate:.2f}%' if isinstance(failure_rate, Decimal) else 'pending'}`"
                )
            return "\n".join(values) or "Collecting outcomes."

        embed.add_field(name="By score bucket", value=bucket_lines("score"), inline=False)
        embed.add_field(
            name="By graduation-age proxy",
            value=bucket_lines("graduation_age"),
            inline=False,
        )
        embed.add_field(
            name="By smart-wallet overlap",
            value=bucket_lines("smart_wallets"),
            inline=False,
        )
        embed.add_field(name="By X status", value=bucket_lines("x"), inline=False)
        embed.add_field(name="By detection safety", value=bucket_lines("safety"), inline=False)
        embed.add_field(
            name="Baseline comparison",
            value=str(result["baseline_status"]),
            inline=False,
        )
        distribution = result["score_distribution"]
        assert isinstance(distribution, dict)

        def score_text(value: object) -> str:
            return f"{value:.2f}" if isinstance(value, Decimal) else "pending"

        embed.add_field(
            name="Current score distribution",
            value=(
                f"max `{score_text(distribution['max'])}` • "
                f"median `{score_text(distribution['median'])}` • "
                f"p90 `{score_text(distribution['p90'])}` • "
                f"p95 `{score_text(distribution['p95'])}`\n"
                f"15+ `{distribution['gte_15']}` • 20+ `{distribution['gte_20']}` • "
                f"35+ `{distribution['gte_35']}` • 50+ `{distribution['gte_50']}` • "
                f"60+ `{distribution['gte_60']}` • 70+ `{distribution['gte_70']}`"
            ),
            inline=False,
        )
        best = result["best_current_candidates"]
        assert isinstance(best, tuple)
        best_lines = [
            f"`{item.score}` • **{item.name or 'Unknown'}** "
            f"`${item.symbol or 'UNKNOWN'}` • `{_short(item.mint)}`"
            for item in best
            if isinstance(item, RunnerCandidate)
        ]
        embed.add_field(
            name="Best current research candidates",
            value="\n".join(best_lines) or "No persisted runner candidates yet.",
            inline=False,
        )

        def relative_timestamp(value: object) -> str:
            return f"<t:{value}:R>" if isinstance(value, int) else "none yet"

        radar_visibility = (
            f"Last strong alert: {relative_timestamp(result['last_strong_alert_at'])}\n"
            f"Last digest: {relative_timestamp(result['last_digest_at'])}\n"
            "Last fast-watch token: none yet"
        )
        if result["last_fast_watch_mint"]:
            radar_visibility = (
                f"Last strong alert: "
                f"{relative_timestamp(result['last_strong_alert_at'])}\n"
                f"Last digest: {relative_timestamp(result['last_digest_at'])}\n"
                f"Last fast-watch token: "
                f"`{_short(str(result['last_fast_watch_mint']))}` "
                f"({relative_timestamp(result['last_fast_watch_at'])})"
            )
        embed.add_field(
            name="Radar visibility",
            value=radar_visibility,
            inline=False,
        )
        path = result["path_analytics"]
        assert isinstance(path, dict)

        def percent(value: object) -> str:
            return f"{value:.2f}%" if isinstance(value, Decimal) else "pending"

        def seconds(value: object) -> str:
            return f"{value:.0f}s" if isinstance(value, Decimal) else "pending"

        embed.add_field(
            name="Usable path outcomes",
            value=(
                f"+10 before -25 `{percent(path['plus_10_before_minus_25_rate'])}` • "
                f"+25 before -25 `{percent(path['plus_25_before_minus_25_rate'])}`\n"
                f"+50 before -50 `{percent(path['plus_50_before_minus_50_rate'])}` • "
                f"+100 before -50 `{percent(path['plus_100_before_minus_50_rate'])}`\n"
                f"Median time +25 `{seconds(path['median_time_to_25_seconds'])}` • "
                f"+50 `{seconds(path['median_time_to_50_seconds'])}`\n"
                f"Median MFE `{percent(path['median_maximum_favorable_excursion'])}` • "
                f"MAE `{percent(path['median_maximum_adverse_excursion'])}` • severe failure "
                f"`{percent(path['severe_failure_rate'])}`"
            ),
            inline=False,
        )
        embed.set_footer(
            text="No look-ahead scoring • shadow research only • no auto-buy or profit claim"
        )
        await interaction.edit_original_response(content=None, embed=embed, view=None)

    @app_commands.command(
        name="latency",
        description="Measure source-to-detection and first-Discord runner latency.",
    )
    @app_commands.describe(sample="Use the most recent 50 or 100 candidates")
    async def latency(
        self,
        interaction: discord.Interaction,
        sample: Literal[50, 100] = 100,
    ) -> None:
        if not await self._require_admin(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        result = await self.bot.engine.runner_latency(limit=sample)

        def metric(value: object, suffix: str = "s") -> str:
            return f"{value:.2f}{suffix}" if isinstance(value, Decimal) else "pending"

        within = result["discovered_within"]
        assert isinstance(within, dict)
        appreciation = result["median_mc_appreciation_to_visible"]
        warning = (
            "\n\n⚠️ Median candidate already doubled before Discord. The pipeline is too slow."
            if isinstance(appreciation, Decimal) and appreciation >= 100
            else ""
        )
        embed = discord.Embed(
            title=f"FOMO RUNNER LATENCY — RECENT {sample}",
            description=(
                f"Candidates `{result['count']}` • source samples "
                f"`{result['source_samples']}` • visible samples "
                f"`{result['visible_samples']}`\n\n"
                f"Source → first seen median "
                f"`{metric(result['source_to_first_seen_median'])}` • p90 "
                f"`{metric(result['source_to_first_seen_p90'])}`\n"
                f"First seen → Discord median "
                f"`{metric(result['first_seen_to_discord_median'])}` • p90 "
                f"`{metric(result['first_seen_to_discord_p90'])}`\n\n"
                f"Within 30s `{metric(within['30s'], '%')}` • 60s "
                f"`{metric(within['60s'], '%')}` • 2m `{metric(within['2m'], '%')}` • "
                f"5m `{metric(within['5m'], '%')}` • 10m "
                f"`{metric(within['10m'], '%')}`\n\n"
                f"Median MC first seen `{_money(result['median_mc_first_seen'])}` • "
                f"first visible `{_money(result['median_mc_first_visible'])}`\n"
                f"Median MC appreciation before visible "
                f"`{metric(appreciation, '%')}`{warning}"
            ),
            color=0x3498DB,
            timestamp=discord.utils.utcnow(),
        )
        recent = result["tokens"]
        assert isinstance(recent, tuple)
        lines = [
            f"`{_short(str(row['mint']))}` • first `{_money(row['mc_at_first_seen'])}` • "
            f"visible `{_money(row['mc_at_first_visible_alert'])}` • entry "
            f"`{_money(row['mc_at_entry_eligible'])}` • peak "
            f"`{_money(row['peak_mc_after_detection'])}`"
            for row in recent[:5]
            if isinstance(row, dict)
        ]
        embed.add_field(
            name="MC_AT_FIRST_SEEN → FIRST_VISIBLE → ENTRY → PEAK",
            value="\n".join(lines) or "No complete market-cap path yet.",
            inline=False,
        )
        embed.set_footer(text="Digest time is never used as first-seen time")
        await interaction.edit_original_response(embed=embed, view=None)

    @app_commands.command(
        name="forensic",
        description="Run bounded read-only public-chain forensics for an exact Solana mint.",
    )
    @app_commands.describe(mint="Exact Solana token mint; ticker searches are not accepted")
    async def forensic(self, interaction: discord.Interaction, mint: str) -> None:
        if not await self._require_admin(interaction):
            return
        try:
            exact_mint = str(Pubkey.from_string(mint.strip()))
        except ValueError:
            await interaction.response.send_message(
                "Enter an exact valid Solana mint. Ticker searches are not accepted.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            async with asyncio.timeout(45):
                candidate = await self.bot.engine.runner_forensic(exact_mint)
        except TimeoutError:
            await interaction.edit_original_response(
                content=(
                    "The bounded public-chain forensic trace timed out. No buy, sell, "
                    "J7, signature, transaction, or SOL action was attempted."
                ),
                embed=None,
                view=None,
            )
            return
        await interaction.edit_original_response(
            content=None,
            embed=_runner_forensic_embed(
                candidate,
                self.bot.settings.fomo_referral_code,
            ),
            view=RunnerAlertView(self.bot, candidate),
        )

    @app_commands.command(
        name="quality",
        description="Funnel throughput, alert precision and missed-runner analysis.",
    )
    @app_commands.describe(days="How many days of observations to summarize")
    async def quality(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 30] = 7,
    ) -> None:
        if not await self._require_admin(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        result = await self.bot.engine.runner_quality_report(since_days=days)
        stages = result["stage_counts"]
        assert isinstance(stages, dict)
        latency = result["latency"]
        assert isinstance(latency, dict)

        def number(value: object, suffix: str = "") -> str:
            if value is None:
                return "pending"
            if isinstance(value, Decimal):
                return f"{value:.2f}{suffix}"
            return f"{value}{suffix}"

        def cohort(label: str, row: object) -> str:
            assert isinstance(row, dict)
            return (
                f"**{label}** n=`{row['count']}` measured=`{row['measured']}`\n"
                f"+10 before −25 `{row['plus_10_before_minus_25']}` • "
                f"+25 before −25 `{row['plus_25_before_minus_25']}` • "
                f"+50 before −50 `{row['plus_50_before_minus_50']}` • "
                f"+100 before −50 `{row['plus_100_before_minus_50']}`\n"
                f"reached +50 `{row['reached_50']}` • +100 `{row['reached_100']}` • "
                f"+200 `{row['reached_200']}` • severe failures `{row['severe_failures']}` "
                f"(`{number(row['severe_failure_rate_percent'], '%')}`)"
            )

        embed = discord.Embed(
            title=f"FOMO RUNNER QUALITY — LAST {days}D",
            description=(
                f"Raw universe observed `{result['raw_universe']}` • silent watched "
                f"`{result['silent_watched']}` • qualified `{result['qualified']}`\n"
                "Stage counts: "
                + (
                    " • ".join(f"`{name}` {count}" for name, count in sorted(stages.items()))
                    or "collecting"
                )
                + f"\nAlert precision (+25 before −25) "
                f"`{number(result['alert_precision_percent'], '%')}` • missed-runner rate "
                f"`{number(result['missed_runner_rate_percent'], '%')}`"
            ),
            color=0x1ABC9C,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Qualified candidates (from first observation)",
            value=cohort("QUALIFIED", result["qualified_performance"])[:1024],
            inline=False,
        )
        embed.add_field(
            name="Qualified candidates (from the alert forward)",
            value=cohort("POST-ALERT", result["post_alert_performance"])[:1024],
            inline=False,
        )
        embed.add_field(
            name="Silent / rejected candidates — the counterfactual",
            value=cohort("SILENT", result["silent_performance"])[:1024],
            inline=False,
        )
        missed = result["missed_runner_examples"]
        assert isinstance(missed, tuple)
        embed.add_field(
            name="Missed runners (never qualified, later reached +50)",
            value=(
                f"Count `{result['missed_runners']}`\n"
                + ("\n".join(f"• `{item}`" for item in missed) or "• none in this window")
            )[:1024],
            inline=False,
        )
        embed.add_field(
            name="Latency / timing",
            value=(
                f"source → first seen p50 `{number(latency['source_to_first_seen_p50'], 's')}` • "
                f"p90 `{number(latency['source_to_first_seen_p90'], 's')}`\n"
                "first seen → qualified p50 "
                f"`{number(latency['first_seen_to_qualified_p50'], 's')}` • p90 "
                f"`{number(latency['first_seen_to_qualified_p90'], 's')}`\n"
                "median move already gone before qualification "
                f"`{number(result['move_lost_before_visibility_median'], '%')}`"
            )[:1024],
            inline=False,
        )
        calls = result["provider_calls"]
        assert isinstance(calls, tuple)
        degraded = result["degraded_providers"]
        assert isinstance(degraded, tuple)
        embed.add_field(
            name="Provider cost today",
            value=(
                (
                    "\n".join(
                        f"`{row['provider']}/{row['feature']}` calls `{row['calls']}` • "
                        f"cache `{row['cache_hits']}` • errors `{row['errors']}`"
                        for row in calls[:8]
                    )
                    or "No provider requests recorded today."
                )
                + (
                    f"\n⚠️ degraded: {', '.join(degraded)} — affected safety fields report "
                    "UNKNOWN, never PASS"
                    if degraded
                    else ""
                )
            )[:1024],
            inline=False,
        )
        embed.set_footer(
            text=(
                "No look-ahead • silent candidates measured with the same forward maths • "
                "thresholds not changed by this command"
            )
        )
        await interaction.edit_original_response(embed=embed, view=None)

    @app_commands.command(
        name="calibration",
        description="Compare detection-time runner evidence with forward outcomes.",
    )
    async def calibration(self, interaction: discord.Interaction) -> None:
        if not await self._require_admin(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        result = await self.bot.engine.runner_calibration()
        distribution = result["score_distribution"]
        assert isinstance(distribution, dict)

        def score(value: object) -> str:
            return f"{value:.2f}" if isinstance(value, Decimal) else "pending"

        def characteristics(label: str, row: object) -> str:
            assert isinstance(row, dict)
            return (
                f"**{label}** n=`{row['count']}` • initial MC "
                f"`{_money(row['initial_market_cap'])}` • liquidity "
                f"`{_money(row['liquidity'])}` • holders `{score(row['holders'])}`\n"
                f"age `{score(row['pair_age_seconds'])}s` • Top10 "
                f"`{score(row['top10'])}%` • dev `{score(row['dev'])}%` • bundlers "
                f"`{score(row['bundlers'])}%` • insiders `{score(row['insiders'])}%` • "
                f"snipers `{score(row['snipers'])}%`\n"
                f"largest cluster `{score(row['largest_cluster'])}%` • shared funders "
                f"`{score(row['shared_funders'])}` • independent clusters "
                f"`{score(row['buyer_independence'])}` • smart overlap "
                f"`{score(row['smart_wallet_overlap'])}` • sell PASS "
                f"`{score(row['sell_route_pass_rate'])}%`"
            )

        embed = discord.Embed(
            title="FOMO RUNNER CALIBRATION — OBSERVE ONLY",
            description=(
                f"Candidates `{result['candidate_count']}` • outcomes "
                f"`{result['outcome_count']}`\n"
                f"Score max `{score(distribution['max'])}` • median "
                f"`{score(distribution['median'])}` • p90 "
                f"`{score(distribution['p90'])}` • p95 "
                f"`{score(distribution['p95'])}`\n"
                f"15+ `{distribution['gte_15']}` • 20+ `{distribution['gte_20']}` • "
                f"35+ `{distribution['gte_35']}` • 50+ `{distribution['gte_50']}` • "
                f"60+ `{distribution['gte_60']}` • 70+ `{distribution['gte_70']}`"
            ),
            color=0x9B59B6,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Winner characteristics",
            value=characteristics("+25 MFE", result["winner_characteristics"])[:1024],
            inline=False,
        )
        embed.add_field(
            name="Failure characteristics",
            value=characteristics("Severe failure", result["failure_characteristics"])[
                :1024
            ],
            inline=False,
        )
        safety = result["safety_buckets"]
        assert isinstance(safety, dict)
        safety_lines = []
        for label, row in safety.items():
            assert isinstance(row, dict)
            safety_lines.append(
                f"`{label}` n={row['count']} • avg "
                f"`{score(row['average'])}%` • +25 "
                f"`{score(row['hit_25_percent'])}%` • failure "
                f"`{score(row['failure_rate_percent'])}%`"
            )
        embed.add_field(
            name="Forward performance by detection safety",
            value="\n".join(safety_lines) or "Collecting outcomes.",
            inline=False,
        )
        embed.set_footer(
            text="No look-ahead • detection snapshots stay immutable • thresholds not changed"
        )
        await interaction.edit_original_response(embed=embed, view=None)


def run_bot(settings: Settings) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    bot = SmartMoneyBot(settings)
    bot.run(settings.discord_token, log_handler=None)
