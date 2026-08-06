from __future__ import annotations

import csv
import io
import logging
import time
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
    DetectedSwap,
    DiscoveryCandidate,
    DiscoveryRefresh,
    ExecutionMode,
    ExecutionResult,
    RiskDecision,
    Side,
    Signal,
    TokenInfo,
    TrackedTrader,
)

logger = logging.getLogger(__name__)


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


def _discovery_lines(candidates: tuple[DiscoveryCandidate, ...] | list[DiscoveryCandidate]) -> str:
    lines: list[str] = []
    for item in candidates[:10]:
        momentum = item.pnl_momentum_usd
        momentum_text = "new" if momentum is None else f"{momentum:+,.2f} since refresh"
        lines.append(
            f"**{item.rank}. {item.alias}** • `{_short(item.address)}`\n"
            f"24H `{_money(item.realized_pnl_24h)}` / `{item.roi_24h_percent:.1f}%` ROI • "
            f"7D `{_money(item.realized_pnl_7d)}` / `{item.roi_7d_percent:.1f}%` ROI\n"
            f"win `24H {item.win_rate_percent:.1f}%` / `7D {item.win_rate_7d_percent:.1f}%` • "
            f"recent `{item.recent_swaps}` • Pump `{item.pump_swaps}` • "
            f"score `{item.score}` • momentum `{momentum_text}`"
        )
    return "\n\n".join(lines) or "No qualified wallets in the latest snapshot."


class SmartMoneyBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = False
        intents.members = False
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings
        self.engine = SmartMoneyEngine(settings, notifier=self)
        self._engine_started = False

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
                    _token_view(token_mint, self.settings.fomo_referral_code)
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
        embed.add_field(
            name="Pump-verified", value=str(refresh.verified_pump_wallets)
        )
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
            token_info.symbol
            if token_info and token_info.symbol
            else _short(signal.token_mint)
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
        embed.add_field(
            name="Traders", value=", ".join(signal.trader_aliases)[:1024], inline=False
        )
        if token_info:
            embed.add_field(name="Liquidity", value=_money(token_info.liquidity_usd))
            embed.add_field(
                name="Organic score",
                value=str(token_info.organic_score or "unknown"),
            )
            embed.add_field(
                name="Verified", value="Yes" if token_info.verified else "No/unknown"
            )
        if decision.reasons:
            embed.add_field(
                name="Checks",
                value="\n".join(f"• {r}" for r in decision.reasons)[:1024],
                inline=False,
            )
        embed.add_field(name="Mint", value=f"`{signal.token_mint}`", inline=False)
        await self._send_alert(embed, token_mint=signal.token_mint)

    async def on_execution(self, result: ExecutionResult) -> None:
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

    async def on_error(self, context: str, error: Exception) -> None:
        logger.error("%s: %s", context, error)


class SmartMoneyCommands(
    commands.GroupCog,
    group_name="smartmoney",
    group_description="Track profitable public Solana wallets and test copy signals.",
):
    def __init__(self, bot: SmartMoneyBot) -> None:
        self.bot = bot

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        if isinstance(user, discord.Member):
            if user.guild_permissions.administrator:
                return True
            role_ids = {role.id for role in user.roles}
            if role_ids & self.bot.settings.discord_admin_role_ids:
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
    async def trader_remove(
        self, interaction: discord.Interaction, alias_or_wallet: str
    ) -> None:
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
            title="Verified Pump Hot Wallets • 24H + 7D",
            description=_discovery_lines(candidates),
            color=0x9B59B6,
        )
        embed.set_footer(
            text="Strict PnL • recent on-chain Pump activity • public Solana wallets"
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
                f"`{_money(report['paper_pnl'])}` from `{report['paper_fills']}` fills"
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
        name="rotation", description="Show recent automatic wallet admissions and removals."
    )
    async def rotation(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        events = await self.bot.engine.database.rotation_events(limit=10)
        if not events:
            await interaction.followup.send(
                "No rotation events recorded yet.", ephemeral=True
            )
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
        status = await self.bot.engine.status()
        stream_status = (
            f"connected ({status['stream_subscriptions']} subscriptions)"
            if status["stream_connected"]
            else "polling fallback"
        )
        text = (
            "**Solana Tracker:** connected for strict 24H + 7D profitability screening\n"
            f"**Pump.fun:** verified through public Solana swaps and Pump mint identity\n"
            f"**Graduated Pump/Jupiter routes:** covered by the same persistent token mint\n"
            f"**Helius/Solana realtime:** {stream_status}\n"
            "**Fomo:** not connected — no documented official API/webhook credentials are "
            "configured. Fomo-native alerts or legitimately obtained public wallet addresses "
            "can be used; scraping is not used."
        )
        await interaction.response.send_message(text, ephemeral=True)

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
        closed = summary.wins + summary.losses
        win_rate = Decimal(summary.wins) / Decimal(closed) * 100 if closed else Decimal("0")
        total_pnl = summary.equity_usd - summary.starting_cash_usd
        total_roi = _return_percent(summary.equity_usd, summary.starting_cash_usd)
        daily_progress = (
            summary.realized_pnl_24h_usd
            / self.bot.settings.paper_daily_target_usd
            * Decimal("100")
        )
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
            value=(
                f"{_money(summary.average_win_usd)} / "
                f"-{_money(summary.average_loss_usd)}"
            ),
        )
        embed.add_field(
            name="24H realized / test target",
            value=(
                f"{_money(summary.realized_pnl_24h_usd)} / "
                f"{_money(self.bot.settings.paper_daily_target_usd)} "
                f"({daily_progress:+.1f}%)"
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
        quote_configured = s.paper_use_executable_quotes and bool(s.jupiter_api_key)
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
            blockers = "• JUPITER_API_KEY is missing or quote-shadow PAPER is disabled\n" + blockers
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
                "Passing means review a tiny live pilot next—not that $50-$100/day "
                "is guaranteed."
            )
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="paper-trades",
        description="Show recent automatic paper buys, sells, ROI, and exit reasons.",
    )
    async def paper_trades(
        self, interaction: discord.Interaction, limit: int = 10
    ) -> None:
        trades = await self.bot.engine.database.paper_recent_trades(limit)
        if not trades:
            await interaction.response.send_message("No paper fills have been recorded yet.")
            return

        lines: list[str] = []
        for item in trades:
            side = str(item["side"])
            mint = str(item["token_mint"])
            quantity = Decimal(str(item["quantity"]))
            price = Decimal(str(item["execution_price_usd"]))
            gross = Decimal(str(item["gross_value_usd"]))
            fee = Decimal(str(item["fee_usd"]))
            kind = str(item["execution_kind"]).replace("_", " ").title()
            timestamp = int(item["created_at"])
            if side == Side.SELL.value:
                realized = Decimal(str(item["realized_pnl_usd"]))
                matched_cost = (gross - fee) - realized
                roi = (
                    realized / matched_cost * Decimal("100")
                    if matched_cost > 0
                    else Decimal("0")
                )
                reason = item.get("exit_reason")
                reason_text = f" • {reason}" if reason else ""
                detail = (
                    f"P&L `{_money(realized)}` • ROI `{roi:+.2f}%`{reason_text}"
                )
            else:
                detail = f"spent `{_money(gross)}` • fee `{_money(fee)}`"
            if bool(item.get("quote_based")):
                router = str(item.get("quote_router") or "unknown")
                impact = Decimal(str(item.get("price_impact_percent") or 0))
                drift_raw = item.get("price_drift_percent")
                drift_text = (
                    f" • drift `{Decimal(str(drift_raw)):+.2f}%`"
                    if drift_raw is not None
                    else ""
                )
                detail += (
                    f" • quote `{router}` • impact `{impact:.2f}%`{drift_text}"
                )
            lines.append(
                f"**{side} • {kind}** • `{_short(mint)}` • <t:{timestamp}:R>\n"
                f"qty `{quantity:.6f}` @ `{_price(price)}` • {detail}"
            )
        await interaction.response.send_message("\n\n".join(lines)[:2000])

    @app_commands.command(name="positions", description="Show open paper positions.")
    async def positions(self, interaction: discord.Interaction) -> None:
        positions = await self.bot.engine.database.paper_all_positions()
        if not positions:
            await interaction.response.send_message("No open paper positions.")
            return
        traders = {
            trader.address: trader.alias
            for trader in await self.bot.engine.database.list_traders()
        }
        real_mints = sorted(
            {
                str(item["token_mint"])
                for item in positions
                if str(item["token_mint"]) != PAPER_DEMO_MINT
            }
        )
        try:
            prices = await self.bot.engine.market.prices(real_mints) if real_mints else {}
        except JupiterError:
            prices = {}
        lines: list[str] = []
        for item in positions[:20]:
            mint = str(item["token_mint"])
            quantity = Decimal(str(item["quantity"]))
            cost = Decimal(str(item["cost_basis_usd"]))
            entry = Decimal(str(item["average_entry_usd"]))
            price = PAPER_DEMO_ENTRY_PRICE if mint == PAPER_DEMO_MINT else prices.get(mint, entry)
            value = quantity * price
            pnl = value - cost
            roi = _return_percent(value, cost)
            if mint == PAPER_DEMO_MINT:
                label = "Paper Demo (fake token)"
            elif item.get("position_kind") == "RAW_MIRROR":
                source = str(item.get("source_trader") or "unknown")
                label = f"Raw mirror • {traders.get(source, _short(source))} • {_short(mint)}"
            else:
                label = _short(mint)
            lines.append(
                f"• **{label}** — qty `{quantity:.6f}` • value `{_money(value)}`\n"
                f"  cost `{_money(cost)}` • P&L `{_money(pnl)}` • ROI `{roi:+.2f}%`"
            )
        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(
        name="paper-demo",
        description="Instantly test a fake paper buy and a winning or losing paper sell.",
    )
    @app_commands.describe(
        action="Open a fake position, or close it with a simulated win/loss."
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
                'Type `RESET PAPER` exactly to confirm.', ephemeral=True
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
                'Type `ENABLE LIVE` in confirmation. The environment lock must also be configured.',
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
        last_scan = (
            f"<t:{status['last_scan']}:R>" if status["last_scan"] else "not completed yet"
        )
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
        text = (
            f"**Bot version:** {BOT_VERSION}\n"
            f"**RPC:** {status['rpc']}\n"
            f"**RPC throttle:** {self.bot.settings.rpc_requests_per_second}/second • "
            f"{self.bot.settings.rpc_max_retries} retries\n"
            f"**Mode:** {status['mode']}\n"
            f"**Paper auto-copy:** {paper_copy}\n"
            f"**Paper entry price:** "
            f"{'current price required' if s.paper_require_current_price else 'fallback allowed'}\n"
            f"**Raw entry safety gate:** "
            f"{'enabled' if s.paper_raw_entry_filter_enabled else 'disabled'}\n"
            f"**Executable quote shadow:** "
            f"{'ready' if status['quote_ready'] else 'JUPITER_API_KEY needed'}\n"
            f"**Consecutive quote failures:** {status['consecutive_quote_failures']} / "
            f"{s.max_consecutive_quote_failures}\n"
            f"**Raw-buy pings:** "
            f"{'ready' if self.bot.settings.discord_alert_user_id else 'user ID not set'}\n"
            f"**Paused:** {status['paused']}\n"
            f"**Tracked wallets:** {status['wallets']}\n"
            f"**Automatic discovery:** "
            f"{'ready' if status['discovery_configured'] else 'API key needed'}\n"
            f"**Discovered wallets:** {status['discovered_wallets']}\n"
            f"**Strict candidate pool:** {status['candidate_pool_size']}\n"
            f"**24H discovery refresh:** {discovery_refresh}\n"
            f"**7D verification refresh:** {weekly_refresh}\n"
            f"**Five-minute rotation:** {rotation_refresh}\n"
            f"**Realtime wallet stream:** {stream_status}\n"
            f"**Last scan:** {last_scan}\n"
            f"**Last error:** {status['last_error'] or 'none'}"
        )
        await interaction.followup.send(text, ephemeral=True)

    @app_commands.command(name="limits", description="Show the active strategy and risk limits.")
    async def limits(self, interaction: discord.Interaction) -> None:
        s = self.bot.settings
        paper_copy = (
            "every new raw tracked-wallet swap"
            if s.paper_mirror_raw_swaps
            else "consensus only"
        )
        text = (
            f"**Paper auto-copy:** {paper_copy}\n"
            f"**Paper entry price:** "
            f"{'current price required' if s.paper_require_current_price else 'fallback allowed'}\n"
            f"**Raw entry safety gate:** "
            f"{'enabled' if s.paper_raw_entry_filter_enabled else 'disabled'}\n"
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
            f"**24H verification:** every {s.discovery_refresh_seconds // 60}m • "
            f"minimum {_money(s.discovery_min_24h_pnl_usd)} PnL • "
            f"{s.discovery_min_roi_percent}% ROI • "
            f"{s.discovery_min_win_rate_percent}% win\n"
            f"**7D verification:** every {s.discovery_7d_refresh_seconds // 3600}h • "
            f"minimum {_money(s.discovery_min_7d_pnl_usd)} PnL • "
            f"{s.discovery_min_7d_roi_percent}% ROI • "
            f"{s.discovery_min_7d_win_rate_percent}% win\n"
            f"**Hot rotation:** every {s.rotation_refresh_seconds // 60}m • "
            f"idle after {s.rotation_max_idle_seconds // 60}m • "
            f"minimum {s.rotation_min_recent_swaps} recent swap / "
            f"{s.rotation_min_pump_swaps} Pump swap\n"
            f"**RPC scanning:** every {s.poll_interval_seconds}s • "
            f"{s.rpc_requests_per_second} requests/second maximum\n"
            f"**Copy size:** {_money(s.default_copy_usd)} (max {_money(s.max_copy_usd)})\n"
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
            "3. `/smartmoney discover` — verify 24H + 7D profit and recent Pump activity\n"
            "4. `/smartmoney scan` — run an immediate on-chain scan\n"
            "5. `/smartmoney hot-wallets` and `rotation` — inspect evidence and changes\n"
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
