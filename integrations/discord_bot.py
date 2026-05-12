"""TradeMaster Discord bot.

Routes four kinds of output to four channels:

- post_research(text) → #research — daily briefing
- post_signal(text)   → #signals  — broker-ready alerts for MANUAL trading
                                    (strike, expiry, call/put, side, prices)
- post_trade(text)    → #trades   — automated bot activity (orders, fills, exits)
- post_log(text)      → #logs     — scheduler errors, system diagnostics

Every slash command is bot-owner-only via app_commands.check. Owner ID is
auto-detected from application info on the first ready event.
"""

from __future__ import annotations

import asyncio
import contextlib

import discord
from discord import app_commands
from discord.ext import commands

from integrations import discord_commands
from trademaster.config import get_settings
from trademaster.logging import get_logger

log = get_logger(__name__)

MESSAGE_LIMIT = 1900  # leave headroom under Discord's 2000-char cap


def _split_for_discord(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split `text` into chunks ≤ `limit` chars, preferring paragraph breaks."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        candidate = (buf + "\n\n" + para) if buf else para
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        while len(para) > limit:
            chunks.append(para[:limit])
            para = para[limit:]
        buf = para
    if buf:
        chunks.append(buf)
    return chunks


class TradeMasterBot(commands.Bot):
    """Bot with slash commands + post helpers.

    Use as an async context manager so the asyncio loop drives the
    underlying client lifecycle:

        async with TradeMasterBot() as bot:
            await bot.post_research("hello")
            ...
    """

    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        settings = get_settings()
        self._token = settings.discord_bot_token.get_secret_value()

        def _chan(value: str) -> int:
            return int(value) if value else 0

        self._research_channel_id = _chan(settings.discord_channel_research)
        self._signals_channel_id = _chan(settings.discord_channel_signals)
        self._trades_channel_id = _chan(settings.discord_channel_trades)
        self._logs_channel_id = _chan(settings.discord_channel_logs)
        self._watchlist_channel_id = _chan(settings.discord_channel_watchlist)
        self._guild_id = _chan(settings.discord_guild_id)
        self._app_ready = asyncio.Event()
        self._owner_id: int | None = None
        self._task: asyncio.Task | None = None
        self._register_commands()

    async def on_ready(self) -> None:
        log.info("discord_ready", user=str(self.user))
        # Cache owner ID once so the check in every slash command is instant
        # (avoids a round-trip that eats into Discord's 3-second response window).
        app = await self.application_info()
        self._owner_id = app.owner.id
        # Sync slash commands. Per-guild sync is fast; global takes up to an hour.
        if self._guild_id:
            guild = discord.Object(id=self._guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("discord_commands_synced", count=len(synced))
        self._app_ready.set()

    # ---------- lifecycle ----------

    async def __aenter__(self) -> TradeMasterBot:
        if not self._token:
            raise RuntimeError("DISCORD_BOT_TOKEN is empty")
        self._task = asyncio.create_task(self.start(self._token))
        await self._app_ready.wait()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()
        if self._task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # ---------- post helpers ----------

    async def post(self, channel_id: int, text: str) -> None:
        if channel_id == 0:
            log.warning("discord_post_skipped_no_channel")
            return
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        for chunk in _split_for_discord(text):
            await channel.send(chunk)
        log.info("discord_posted", channel_id=channel_id, chars=len(text))

    async def post_research(self, text: str) -> None:
        await self.post(self._research_channel_id, text)

    async def post_signal(self, text: str) -> None:
        """Broker-ready manual-trading signal."""
        await self.post(self._signals_channel_id, text)

    async def post_trade(self, text: str) -> None:
        """Automated trade activity (orders, fills, exits)."""
        await self.post(self._trades_channel_id, text)

    async def post_log(self, text: str) -> None:
        """System-level error or diagnostic."""
        await self.post(self._logs_channel_id, text)

    async def post_watchlist(self, text: str) -> None:
        """Current watchlist snapshot — posted on add/remove/seed."""
        await self.post(self._watchlist_channel_id, text)

    # ---------- slash commands ----------

    def _register_commands(self) -> None:
        def owner_only(interaction: discord.Interaction) -> bool:
            return self._owner_id is not None and interaction.user.id == self._owner_id

        tree = self.tree

        @tree.command(name="status", description="Show TradeMaster status")
        @app_commands.check(owner_only)
        async def _status(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            text = await discord_commands.status()
            await interaction.followup.send(text)

        @tree.command(name="positions", description="List open Alpaca positions")
        @app_commands.check(owner_only)
        async def _positions(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            text = await discord_commands.positions()
            await interaction.followup.send(text)

        @tree.command(name="cash", description="Show account cash, buying power, equity")
        @app_commands.check(owner_only)
        async def _cash(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            text = await discord_commands.cash()
            await interaction.followup.send(text)

        @tree.command(
            name="kill",
            description="EMERGENCY: cancel all orders, close all positions, pause 24h",
        )
        @app_commands.check(owner_only)
        async def _kill(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            text = await discord_commands.kill(
                reason=f"discord /kill by {interaction.user}"
            )
            await interaction.followup.send(text)

        @tree.command(name="pause", description="Pause new trades for N minutes")
        @app_commands.describe(minutes="How many minutes to pause (1-1440)")
        @app_commands.check(owner_only)
        async def _pause(interaction: discord.Interaction, minutes: int):
            await interaction.response.send_message(await discord_commands.pause(minutes))

        @tree.command(name="resume", description="Resume trading after a pause")
        @app_commands.check(owner_only)
        async def _resume(interaction: discord.Interaction):
            await interaction.response.send_message(await discord_commands.resume())

        @tree.command(name="pending", description="List trade approvals waiting (live mode)")
        @app_commands.check(owner_only)
        async def _pending(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            text = await discord_commands.pending()
            await interaction.followup.send(text)

        @tree.command(
            name="approve",
            description="Approve a pending live-mode trade and submit it to Alpaca",
        )
        @app_commands.describe(pending_id="Pending order id (see /pending)")
        @app_commands.check(owner_only)
        async def _approve(interaction: discord.Interaction, pending_id: int):
            await interaction.response.defer(thinking=True)
            text = await discord_commands.approve(
                pending_id, user_label=str(interaction.user)
            )
            await interaction.followup.send(text)

        @tree.command(
            name="reject",
            description="Reject a pending live-mode trade and discard it",
        )
        @app_commands.describe(pending_id="Pending order id (see /pending)")
        @app_commands.check(owner_only)
        async def _reject(interaction: discord.Interaction, pending_id: int):
            await interaction.response.defer(thinking=True)
            text = await discord_commands.reject(
                pending_id, user_label=str(interaction.user)
            )
            await interaction.followup.send(text)

        @tree.command(
            name="watchlist",
            description="Show the current ticker watchlist",
        )
        @app_commands.check(owner_only)
        async def _watchlist(interaction: discord.Interaction):
            text = await discord_commands.watchlist_show()
            await interaction.response.send_message(text)

        @tree.command(
            name="watchlist_add",
            description="Add a ticker to the watchlist",
        )
        @app_commands.describe(ticker="Symbol to add (e.g. NVDA, BRK.B)")
        @app_commands.check(owner_only)
        async def _watchlist_add(interaction: discord.Interaction, ticker: str):
            reply, current, changed = await discord_commands.watchlist_add(ticker)
            await interaction.response.send_message(reply)
            if changed:
                await self.post_watchlist(
                    discord_commands._format_watchlist(current)
                    + f"\n_changed by {interaction.user}_"
                )

        @tree.command(
            name="watchlist_remove",
            description="Remove a ticker from the watchlist",
        )
        @app_commands.describe(ticker="Symbol to remove")
        @app_commands.check(owner_only)
        async def _watchlist_remove(interaction: discord.Interaction, ticker: str):
            reply, current, changed = await discord_commands.watchlist_remove(ticker)
            await interaction.response.send_message(reply)
            if changed:
                await self.post_watchlist(
                    discord_commands._format_watchlist(current)
                    + f"\n_changed by {interaction.user}_"
                )

        @tree.error
        async def _on_app_command_error(
            interaction: discord.Interaction, error: app_commands.AppCommandError
        ):
            if isinstance(error, app_commands.CheckFailure):
                msg = "🔒 This command is owner-only."
            else:
                log.error("app_command_error", error=str(error))
                msg = f"⚠️ Command error: `{type(error).__name__}: {error}`"
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except discord.HTTPException:
                # Interaction expired before we could reply — nothing to do.
                pass
