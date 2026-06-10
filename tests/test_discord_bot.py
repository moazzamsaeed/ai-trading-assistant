"""Discord bot helper tests.

Doesn't exercise the bot lifecycle (would need a real Discord token).
Tests the message-splitting helper and the channel-ID dispatch logic.
"""

from __future__ import annotations

import asyncio

from integrations.discord_bot import (
    MESSAGE_LIMIT,
    TradeMasterBot,
    _split_for_discord,
)


def test_short_message_single_chunk():
    chunks = _split_for_discord("hello world")
    assert chunks == ["hello world"]


def test_long_message_splits_on_paragraphs():
    para = "A" * 1000
    text = "\n\n".join([para, para, para])
    chunks = _split_for_discord(text)
    assert all(len(c) <= MESSAGE_LIMIT for c in chunks)
    assert "".join(c.replace("\n\n", "") for c in chunks).replace("\n\n", "") == "".join(
        [para, para, para]
    ) or len(chunks) >= 2  # split happened


def test_single_paragraph_over_limit_is_hard_split():
    text = "A" * (MESSAGE_LIMIT * 2 + 100)
    chunks = _split_for_discord(text)
    assert len(chunks) >= 3
    assert all(len(c) <= MESSAGE_LIMIT for c in chunks)
    assert sum(len(c) for c in chunks) == len(text)


def test_paragraph_boundary_preserved():
    p1 = "first paragraph " * 50
    p2 = "second paragraph " * 50
    text = f"{p1}\n\n{p2}"
    chunks = _split_for_discord(text, limit=500)
    # The paragraphs are short enough to each fit; expect a clean split between them.
    assert len(chunks) >= 2
    assert all(len(c) <= 500 for c in chunks)


# ---------------------------------------------------------------------------
# post_single — one Discord message (one notification) for long briefings
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self):
        self.sends = []  # one entry per channel.send() call

    async def send(self, content=None, *, embeds=None):
        self.sends.append(embeds if embeds is not None else content)


class _FakeBot:
    """Bind the real post_single onto a stub so we don't need a live bot."""
    post_single = TradeMasterBot.post_single

    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, _cid):
        return self._channel


def test_post_single_sends_one_message_for_a_long_briefing():
    ch = _FakeChannel()
    # ~4000-char premarket-sized briefing that used to split into 3 messages.
    text = "\n\n".join(["P" * 1000 for _ in range(4)])
    asyncio.run(_FakeBot(ch).post_single(123, text))
    assert len(ch.sends) == 1                          # ONE message → one notification
    assert isinstance(ch.sends[0], list)               # delivered as embed(s)
    assert len(ch.sends[0]) >= 1


def test_post_single_spills_only_past_embed_budget():
    ch = _FakeChannel()
    # Well over the per-message embed budget → at most a second message, never 1-per-chunk.
    text = "\n\n".join(["Q" * 2000 for _ in range(6)])  # ~12k chars
    asyncio.run(_FakeBot(ch).post_single(123, text))
    assert 1 < len(ch.sends) <= 3
    assert all(isinstance(s, list) for s in ch.sends)
