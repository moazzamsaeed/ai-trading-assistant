"""Minimal Discord bot.

Phase 1.3 scope: connect to Discord, post a long-form message to
#research when called. Slash commands (`/kill`, `/approve`, `/status`,
etc.) land in Phase 1.4 alongside the risk-manager wiring.

Discord limits a single message to 2000 chars; long briefings are split
on paragraph boundaries.
"""

from __future__ import annotations

import asyncio
import contextlib

import discord

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
        # Single paragraph longer than limit — hard split.
        while len(para) > limit:
            chunks.append(para[:limit])
            para = para[limit:]
        buf = para
    if buf:
        chunks.append(buf)
    return chunks


class DiscordPoster:
    """Thin async client that connects to Discord and exposes `post(channel, text)`.

    Designed to share an event loop with the scheduler. Use `async with`:

        async with DiscordPoster() as poster:
            await poster.post_research("hello")
            ...
    """

    def __init__(self, token: str | None = None) -> None:
        settings = get_settings()
        self._token = token or settings.discord_bot_token.get_secret_value()
        self._research_channel_id = (
            int(settings.discord_channel_research) if settings.discord_channel_research else 0
        )
        intents = discord.Intents.default()
        self._client = discord.Client(intents=intents)
        self._ready = asyncio.Event()

        @self._client.event
        async def on_ready() -> None:
            log.info("discord_ready", user=str(self._client.user))
            self._ready.set()

    async def __aenter__(self) -> DiscordPoster:
        if not self._token:
            raise RuntimeError("DISCORD_BOT_TOKEN is empty")
        self._task = asyncio.create_task(self._client.start(self._token))
        await self._ready.wait()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self._client.close()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def post(self, channel_id: int, text: str) -> None:
        if channel_id == 0:
            log.warning("discord_post_skipped_no_channel")
            return
        channel = self._client.get_channel(channel_id) or await self._client.fetch_channel(
            channel_id
        )
        for chunk in _split_for_discord(text):
            await channel.send(chunk)
        log.info("discord_posted", channel_id=channel_id, chars=len(text))

    async def post_research(self, text: str) -> None:
        await self.post(self._research_channel_id, text)
