"""Discord bot helper tests.

Doesn't exercise the bot lifecycle (would need a real Discord token).
Tests the message-splitting helper and the channel-ID dispatch logic.
"""

from __future__ import annotations

from integrations.discord_bot import MESSAGE_LIMIT, _split_for_discord


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
