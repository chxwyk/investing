from __future__ import annotations

import csv
import io
import logging
from decimal import Decimal
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands
from solders.pubkey import Pubkey

from .config import Settings
from .engine import SmartMoneyEngine
from .models import (
    DetectedSwap,
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
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
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

    async def _send_alert(self, embed: discord.Embed) -> None:
        channel = await self._alert_channel()
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not post to alert channel")

    async def on_swap(self, swap: DetectedSwap, trader: TrackedTrader) -> None:
        color = 0x2ECC71 if swap.side is Side.BUY else 0xE74C3C
        embed = discord.Embed(
            title=f"{swap.side.value} detected • {trader.alias}",
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Token", value=f"`{swap.token_mint}`", inline=False)
        embed.add_field(name="Trade value", value=_money(swap.usd_value))
        embed.add_field(name="Entry/exit", value=_money(swap.token_price_usd))
        embed.add_field(
            name="Transaction",
            value=f"[View on Solscan](https://solscan.io/tx/{swap.signature})",
            inline=False,
        )
        embed.set_footer(text="Raw wallet activity • wait for consensus/risk result")
        await self._send_alert(embed)

    async def on_signal(
        self, signal: Signal, token_info: TokenInfo | None, decision: RiskDecision
    ) -> None:
        symbol = token_info.symbol if token_info and token_info.symbol else _short(signal.token_mint)
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
            embed.add_field(name="Checks", value="\n".join(f"• {r}" for r in decision.reasons)[:1024], inline=False)
        embed.add_field(name="Mint", value=f"`{signal.token_mint}`", inline=False)
        await self._send_alert(embed)

    async def on_execution(self, result: ExecutionResult) -> None:
        color = 0x3498DB if result.success else 0xE74C3C
        embed = discord.Embed(
            title=f"{result.mode.value} {result.side.value} • {'FILLED' if result.success else 'FAILED'}",
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
        await self._send_alert(embed)

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

    @app_commands.command(name="setup", description="Choose where wallet and signal alerts are posted.")
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
            f"• **{item.alias}** — `{_short(item.address)}` — weight {item.weight}"
            for item in traders[:25]
        ]
        await interaction.response.send_message("\n".join(lines))

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
        embed = discord.Embed(title="Paper Strategy Scoreboard", color=0x3498DB)
        embed.add_field(name="Equity", value=_money(summary.equity_usd))
        embed.add_field(name="Cash", value=_money(summary.cash_usd))
        embed.add_field(name="Positions", value=_money(summary.positions_value_usd))
        embed.add_field(name="Realized P&L", value=_money(summary.realized_pnl_usd))
        embed.add_field(name="Unrealized P&L", value=_money(summary.unrealized_pnl_usd))
        embed.add_field(name="Max drawdown", value=_money(summary.max_drawdown_usd))
        embed.add_field(name="Trades", value=str(summary.trades))
        embed.add_field(name="Win rate", value=f"{win_rate:.1f}%")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="positions", description="Show open paper positions.")
    async def positions(self, interaction: discord.Interaction) -> None:
        positions = await self.bot.engine.database.paper_positions()
        if not positions:
            await interaction.response.send_message("No open paper positions.")
            return
        lines = [
            f"• `{_short(item['token_mint'])}` — qty `{Decimal(str(item['quantity'])):.6f}` "
            f"• cost `{_money(Decimal(str(item['cost_basis_usd'])))}`"
            for item in positions[:20]
        ]
        await interaction.response.send_message("\n".join(lines))

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
        await interaction.response.send_message("Paper account reset.", ephemeral=True)

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

    @app_commands.command(name="status", description="Check bot, RPC, and monitor health.")
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        status = await self.bot.engine.status()
        last_scan = (
            f"<t:{status['last_scan']}:R>" if status["last_scan"] else "not completed yet"
        )
        text = (
            f"**RPC:** {status['rpc']}\n"
            f"**Mode:** {status['mode']}\n"
            f"**Paused:** {status['paused']}\n"
            f"**Tracked wallets:** {status['wallets']}\n"
            f"**Last scan:** {last_scan}\n"
            f"**Last error:** {status['last_error'] or 'none'}"
        )
        await interaction.followup.send(text, ephemeral=True)

    @app_commands.command(name="limits", description="Show the active strategy and risk limits.")
    async def limits(self, interaction: discord.Interaction) -> None:
        s = self.bot.settings
        text = (
            f"**Consensus:** {s.consensus_min_traders} traders within {s.consensus_window_seconds}s\n"
            f"**Minimum trader score:** {s.min_trader_score}/100\n"
            f"**Copy size:** {_money(s.default_copy_usd)} (max {_money(s.max_copy_usd)})\n"
            f"**Daily stop:** -{_money(s.max_daily_loss_usd)}\n"
            f"**Minimum liquidity:** {_money(s.min_token_liquidity_usd)}\n"
            f"**Max positions:** {s.max_open_positions}\n"
            f"**Signal max age:** {s.max_signal_age_seconds}s\n"
            f"**Stop loss / take profit:** {s.stop_loss_percent}% / {s.take_profit_percent}%\n"
            f"**Maximum hold:** {s.max_hold_seconds // 3600}h"
        )
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="help", description="Show the fastest setup order.")
    async def help(self, interaction: discord.Interaction) -> None:
        text = (
            "1. `/smartmoney setup` — choose the alert channel\n"
            "2. `/smartmoney trader-add` — add each public wallet and alias\n"
            "3. `/smartmoney scan` — run the 24-hour bootstrap\n"
            "4. `/smartmoney leaderboard` — inspect risk-adjusted rankings\n"
            "5. Keep `/smartmoney mode paper` while proving the strategy\n"
            "6. `/smartmoney paper` — compare net P&L and drawdown"
        )
        await interaction.response.send_message(text, ephemeral=True)


def run_bot(settings: Settings) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    bot = SmartMoneyBot(settings)
    bot.run(settings.discord_token, log_handler=None)
