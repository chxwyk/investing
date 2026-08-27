from __future__ import annotations

import csv
import io
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from urllib.parse import urlencode

import discord
from discord import app_commands
from discord.ext import commands
from solders.pubkey import Pubkey

from .config import Settings
from .constants import BOT_VERSION, PAPER_DEMO_ENTRY_PRICE_USD, PAPER_DEMO_MINT
from .engine import SmartMoneyEngine
from .errors import DiscoveryError, JupiterError
from .models import (
    CoinCallout,
    DetectedSwap,
    DiscoveryCandidate,
    DiscoveryRefresh,
    ExecutionMode,
    ExecutionResult,
    LaunchOpportunity,
    NarrativePairMatch,
    NewsAlert,
    PaperDailyLockStatus,
    PumpLaunchResult,
    RiskDecision,
    Side,
    Signal,
    TokenInfo,
    TrackedTrader,
)

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
        self.launch_button.disabled = (
            opportunity.verdict != "LAUNCH READY" or not bot.engine.pump_launcher.configured
        )
        if opportunity.verdict != "LAUNCH READY":
            self.launch_button.label = "Internal research only"
        elif not bot.engine.pump_launcher.configured:
            self.launch_button.label = "One-click launch locked"

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
        await interaction.response.defer(ephemeral=True, thinking=True)
        button.disabled = True
        button.label = "Launch submitted…"
        if interaction.message:
            with suppress(discord.HTTPException):
                await interaction.message.edit(view=self)
        result = await self.bot.engine.launch_news_opportunity(
            self.opportunity,
            requested_by=str(interaction.user.id),
        )
        result_embed = _pump_launch_result_embed(result)
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


def _news_alert_embed(
    alert: NewsAlert,
    opportunity: LaunchOpportunity,
) -> discord.Embed:
    colors = {
        "COIN FOUND": 0x3498DB,
        "LAUNCH READY": 0xE74C3C,
        "WATCH": 0xF1C40F,
        "SKIP": 0x95A5A6,
    }
    source = alert.author or alert.source
    delay = max(0, alert.received_at - alert.created_at) if alert.created_at else None
    timing = f" • received `{delay}s` after publication" if delay is not None else ""
    embed = discord.Embed(
        title=f"NEWS RADAR • {opportunity.verdict} • {opportunity.category} • {source}"[:256],
        description=(
            f"**{alert.headline[:700]}**\n\n"
            + (
                "A Solana contract is already in the source. Use the direct research/buy "
                "links; this alert cannot launch a duplicate coin."
                if alert.token_mints
                else "No source contract was found. This public alert passed the crypto-demand, "
                "source, competition, and launch-identity gates."
            )
        ),
        color=colors.get(opportunity.verdict, 0x95A5A6),
        timestamp=datetime.fromtimestamp(alert.received_at or int(time.time()), tz=UTC),
    )
    embed.add_field(
        name="Crypto-attention opportunity",
        value=(
            f"**{opportunity.score}/100** • confidence **{opportunity.confidence}**{timing}\n"
            f"Proposed identity: **{opportunity.coin_name}** (`${opportunity.coin_symbol}`)"
        ),
        inline=False,
    )
    embed.add_field(
        name="Admission lane",
        value=(
            f"**{opportunity.lane}** • crypto-demand gate "
            f"`{'passed' if opportunity.crypto_attention_ready else 'not passed'}` • "
            f"U.S. relevance `{'yes' if opportunity.us_relevant else 'no'}`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Why it scored this way",
        value=(
            f"Source `{opportunity.source_score}/15` • speed `{opportunity.speed_score}/15` • "
            f"meme potential `{opportunity.viral_score}/25`\n"
            f"Crypto X traction `{opportunity.x_score}/15` • independent confirmation "
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
    embed.set_footer(
        text=(
            "COIN FOUND • direct links below • token-risk callout follows"
            if alert.token_mints
            else "No coin yet • DEX matcher keeps checking • launch button is admin-only"
        )
    )
    return embed


def _pump_launch_result_embed(result: PumpLaunchResult) -> discord.Embed:
    embed = discord.Embed(
        title=(
            f"PUMP LAUNCH • {result.status} • ${result.symbol}"
            if result.success
            else f"PUMP LAUNCH • {result.status}"
        ),
        description=result.message,
        color=0x2ECC71 if result.success else 0xE74C3C,
        timestamp=datetime.fromtimestamp(result.created_at or int(time.time()), tz=UTC),
    )
    embed.add_field(name="Name", value=result.name or "unknown")
    embed.add_field(name="Symbol", value=f"`${result.symbol}`" if result.symbol else "unknown")
    if result.mint:
        embed.add_field(name="Mint", value=f"`{result.mint}`", inline=False)
        embed.add_field(
            name="Pump.fun",
            value=f"[Open coin](https://pump.fun/coin/{result.mint})",
        )
    if result.signature:
        embed.add_field(
            name="Transaction",
            value=f"[View on Solscan](https://solscan.io/tx/{result.signature})",
            inline=False,
        )
    embed.set_footer(
        text=(
            "Public Pump.fun coin after confirmation • anyone can use the Open coin link • "
            "dedicated wallet/daily limits"
        )
    )
    return embed


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
    ) -> None:
        channel = await self._alert_channel()
        if channel is None:
            return
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
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not post to alert channel")

    async def on_swap(self, swap: DetectedSwap, trader: TrackedTrader) -> None:
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

    async def on_news_alert(
        self,
        alert: NewsAlert,
        opportunity: LaunchOpportunity,
    ) -> None:
        mint = alert.token_mints[0] if alert.token_mints else None
        await self._send_alert(
            _news_alert_embed(alert, opportunity),
            token_mint=mint,
            ping_user=opportunity.verdict in {"COIN FOUND", "LAUNCH READY"},
            view=None if mint else NewsOpportunityView(self, opportunity),
        )

    async def on_narrative_match(
        self, alert: NewsAlert, match: NarrativePairMatch
    ) -> None:
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
            f"working (last success <t:{status['x_social_last_success']}:R>)"
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
            "disabled (budget mode)"
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
        scan_counts = status["coin_scan_counts"]
        assert isinstance(scan_counts, dict)
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
            f"**Paid X search budget:** {status['x_search_usage_today']}/"
            f"{status['x_search_daily_limit']} used today • free prefilter before every "
            "automatic paid search\n"
            f"**Coin scan visibility:** {scan_counts.get('total', 0)} analyzed since restart • "
            f"{scan_counts.get('free_rejected', 0)} rejected before X • "
            f"{scan_counts.get('x_checked', 0)} X-checked • "
            f"{scan_counts.get('watch', 0)} developing WATCH alerts • "
            f"{scan_counts.get('verified', 0)} VERIFIED TREND alerts\n"
            f"**X near-realtime news stream:** {news_stream_status} • configured account/news "
            "rule • crypto-first filtering • exceptional U.S. event lane\n"
            f"**RSS/news radar:** {'ready' if status['news_rss_ready'] else 'starting'} • "
            "U.S. government/markets plus crypto sources; routine culture/sports removed\n"
            f"**One-click Pump.fun launch:** "
            f"{'unlocked' if status['pump_launch_unlocked'] else 'locked'} • "
            "admin-only • separate capped wallet • public IPFS metadata\n"
            f"**J7 Tracker:** "
            + (
                f"authorized feed {status['j7_feed_health']}\n"
                if status["j7_feed_configured"]
                else "optional authorized RSS/Atom adapter ready; no public API documented\n"
            )
            +
            "**Fomo:** the official app exposes leaderboards, profiles, follows, and alerts, "
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

    @app_commands.command(
        name="scans", description="Show recent free-prefilter and paid-X coin scan results."
    )
    async def scans(self, interaction: discord.Interaction) -> None:
        rows = self.bot.engine.recent_coin_scans()
        if not rows:
            await interaction.response.send_message(
                "No coin scans have completed since this deployment.", ephemeral=True
            )
            return
        lines: list[str] = []
        for item in rows[:10]:
            social = item.social
            x_text = (
                f"X `{social.posts}` posts / `{social.contract_authors}` exact-contract authors"
                if social.available
                else f"X `{social.error or 'not requested'}`"
            )
            lines.append(
                f"**{item.scan_stage} • {item.symbol or _short(item.mint)}** • "
                f"score `{item.score}` • prefilter `{item.prefilter_score}`\n"
                f"`{_short(item.mint)}` • {x_text}\n"
                f"why: {item.scan_reason or item.verdict}"
            )
        embed = discord.Embed(
            title="Recent Coin / X Scan Audit",
            description="\n\n".join(lines)[:4096],
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

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

    @app_commands.command(
        name="paper-demo",
        description="Instantly test a fake paper buy and a winning or losing paper sell.",
    )
    @app_commands.describe(action="Open a fake position, or close it with a simulated win/loss.")
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
            f"working • last success <t:{status['x_social_last_success']}:R>"
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
            "disabled (budget mode)"
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
        rss_health = (
            f"ready • last refresh <t:{status['news_rss_last_refresh']}:R>"
            if status["news_rss_last_refresh"]
            else (
                f"error • {status['news_rss_last_error']}"
                if status["news_rss_last_error"]
                else "starting"
            )
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
            f"**Raw-buy pings:** "
            f"{'ready' if self.bot.settings.discord_alert_user_id else 'user ID not set'}\n"
            f"**Coin callouts:** "
            f"{'enabled' if status['coin_callouts_enabled'] else 'disabled'} • "
            "VERIFIED TREND only • exact-contract X promotion • cross-source liquidity • "
            "$5 executable route • complete Tracker/holder proof • "
            f"X {x_callout_health} • "
            f"minimum final score {max(s.coin_callout_min_alert_score, Decimal('70'))}/100\n"
            f"**X search budget:** {status['x_search_usage_today']}/"
            f"{status['x_search_daily_limit']} today • free prefilter "
            f"{s.coin_x_prefilter_min_score}/100 • developing WATCH alerts "
            f"{'enabled' if status['coin_watch_alerts_enabled'] else 'disabled'}\n"
            f"**News radar:** {'enabled' if status['news_radar_enabled'] else 'disabled'} • "
            "crypto-first • exceptional U.S. events require broad crypto pickup • "
            f"X stream {x_news_health} • RSS {rss_health}\n"
            f"**Crypto-demand launch score:** public alerts are LAUNCH READY only • "
            f"{s.news_launch_ready_score}+ plus authentic crypto-account promotion gate • "
            "source/speed/meme/X/confirmation/competition/identity\n"
            f"**One-click Pump launch:** "
            f"{'unlocked' if status['pump_launch_unlocked'] else 'locked'} • "
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

    @app_commands.command(name="help", description="Show the fastest setup order.")
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


def run_bot(settings: Settings) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    bot = SmartMoneyBot(settings)
    bot.run(settings.discord_token, log_handler=None)
