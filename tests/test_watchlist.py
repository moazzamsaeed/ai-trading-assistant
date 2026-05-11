"""Watchlist storage tests."""

from __future__ import annotations

import json

import pytest

from trademaster import watchlist


@pytest.fixture
def wl_path(tmp_path):
    return tmp_path / "watchlist.json"


# ----------------- load_tickers -----------------


def test_load_tickers_returns_default_when_file_missing(wl_path):
    tickers = watchlist.load_tickers(wl_path)
    assert tickers == watchlist.DEFAULT_TICKERS


def test_load_tickers_returns_file_contents(wl_path):
    wl_path.write_text(json.dumps({"tickers": ["nvda", "AAPL"]}))
    tickers = watchlist.load_tickers(wl_path)
    assert tickers == ("NVDA", "AAPL")  # normalized to uppercase


def test_load_tickers_falls_back_to_default_on_empty_file(wl_path):
    wl_path.write_text(json.dumps({"tickers": []}))
    assert watchlist.load_tickers(wl_path) == watchlist.DEFAULT_TICKERS


def test_load_tickers_handles_corrupt_json(wl_path):
    wl_path.write_text("{not json")
    assert watchlist.load_tickers(wl_path) == watchlist.DEFAULT_TICKERS


def test_load_tickers_handles_bad_shape(wl_path):
    wl_path.write_text(json.dumps({"wrong_key": ["NVDA"]}))
    assert watchlist.load_tickers(wl_path) == watchlist.DEFAULT_TICKERS


# ----------------- add_ticker -----------------


def test_add_new_ticker(wl_path):
    listing, added = watchlist.add_ticker("nvda", wl_path)
    assert added is True
    assert listing == ["NVDA"]


def test_add_existing_ticker_is_noop(wl_path):
    watchlist.add_ticker("NVDA", wl_path)
    listing, added = watchlist.add_ticker("nvda", wl_path)
    assert added is False
    assert listing == ["NVDA"]


def test_add_preserves_insertion_order(wl_path):
    watchlist.add_ticker("AMD", wl_path)
    watchlist.add_ticker("NVDA", wl_path)
    watchlist.add_ticker("META", wl_path)
    assert watchlist.list_tickers(wl_path) == ["AMD", "NVDA", "META"]


def test_add_rejects_invalid_ticker(wl_path):
    with pytest.raises(ValueError):
        watchlist.add_ticker("not a ticker", wl_path)
    with pytest.raises(ValueError):
        watchlist.add_ticker("", wl_path)
    with pytest.raises(ValueError):
        watchlist.add_ticker("WAY_TOO_LONG", wl_path)


def test_add_accepts_brk_b_style(wl_path):
    _, added = watchlist.add_ticker("BRK.B", wl_path)
    assert added is True


# ----------------- remove_ticker -----------------


def test_remove_existing_ticker(wl_path):
    watchlist.add_ticker("NVDA", wl_path)
    listing, removed = watchlist.remove_ticker("nvda", wl_path)
    assert removed is True
    assert listing == []


def test_remove_unknown_ticker_is_noop(wl_path):
    watchlist.add_ticker("NVDA", wl_path)
    listing, removed = watchlist.remove_ticker("AMD", wl_path)
    assert removed is False
    assert listing == ["NVDA"]


# ----------------- seed -----------------


def test_seed_replaces_existing(wl_path):
    watchlist.add_ticker("OLD", wl_path)
    out = watchlist.seed(["NVDA", "AMD", "META"], wl_path)
    assert out == ["NVDA", "AMD", "META"]
    assert watchlist.list_tickers(wl_path) == ["NVDA", "AMD", "META"]


def test_seed_dedups_input(wl_path):
    out = watchlist.seed(["NVDA", "nvda", "AMD"], wl_path)
    assert out == ["NVDA", "AMD"]


def test_seed_rejects_invalid(wl_path):
    with pytest.raises(ValueError):
        watchlist.seed(["NVDA", "bad ticker"], wl_path)
