"""Unit tests for the isolated equities signal scanner."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agents.directional.intraday import TickerDecision
from agents.equities import scanner
from integrations.alpaca_client import Bar

NOW = datetime(2026, 6, 26, 14, 0, tzinfo=UTC)  # 10:00 ET, today = 2026-06-26 ET


def _bar(ts: datetime, o, h, l, c, v=1000) -> Bar:
    return Bar(timestamp=ts, open=Decimal(str(o)), high=Decimal(str(h)),
               low=Decimal(str(l)), close=Decimal(str(c)), volume=v, vwap=Decimal(str(c)))


def _decision(action="BUY_CALL", conv="HIGH", ticker="META", strike=500.0):
    return TickerDecision(ticker, action, strike, "0DTE", conv,
                          f"trend_follow_v2: UP-trend, ADX 33.0 → {action}/{conv}",
                          analysis={"spy_price": strike})


# ── per-ticker S/R context ────────────────────────────────────────────────────
def test_ticker_market_ctx_builds_levels():
    # 10 completed daily sessions (closes 100..109), dated before today
    daily = [
        _bar(datetime(2026, 6, 12, 20, tzinfo=UTC) + timedelta(days=i),
             100 + i, 100 + i + 2, 100 + i - 2, 100 + i)
        for i in range(10)
    ]
    # today's intraday bars (ET 2026-06-26)
    intraday = [
        _bar(datetime(2026, 6, 26, 13, 35, tzinfo=UTC), 110, 112, 109, 111),  # ORB
        _bar(datetime(2026, 6, 26, 13, 40, tzinfo=UTC), 111, 115, 110, 114),
        _bar(datetime(2026, 6, 26, 13, 45, tzinfo=UTC), 114, 114, 108, 109),
    ]
    ctx = scanner._ticker_market_ctx(intraday, daily, NOW)
    md = ctx["multi_day"]
    assert md["prev_close"] == 109.0          # last completed session
    assert md["ma5"] == pytest.approx(107.0)  # mean of 105..109
    assert md["ma10"] == pytest.approx(104.5)
    assert ctx["orb_high"] == 112.0           # first today bar's high
    assert ctx["session_high"] == 115.0       # max across today's bars
    assert ctx["session_low"] == 108.0


def test_ticker_market_ctx_excludes_today_from_daily():
    # a daily bar dated today must NOT be treated as a completed prior session
    daily = [_bar(datetime(2026, 6, 25, 20, tzinfo=UTC), 50, 55, 49, 52),
             _bar(datetime(2026, 6, 26, 20, tzinfo=UTC), 52, 60, 51, 59)]  # today partial
    ctx = scanner._ticker_market_ctx([], daily, NOW)
    assert ctx["multi_day"]["prev_close"] == 52.0  # yesterday, not today's 59


# ── plain-language formatting (no options jargon) ─────────────────────────────
def test_format_equities_signal_plain_language():
    msg = scanner.format_equities_signal(_decision("BUY_CALL", "HIGH", "NVDA", 120.0), price=119.7)
    assert "BUY a CALL" in msg and "NVDA" in msg
    assert "$120" in msg and "119.7" in msg and "[HIGH]" in msg
    low = msg.lower()
    for jargon in ("delta", "theta", "gamma", "vega", "condor", "spread", "0dte", "straddle"):
        assert jargon not in low


def test_format_equities_put():
    msg = scanner.format_equities_signal(_decision("BUY_PUT", "MEDIUM", "MU", 90.0), price=90.2)
    assert "BUY a PUT" in msg and "MU" in msg and "[MEDIUM]" in msg


# ── dedup: post only on change ────────────────────────────────────────────────
def test_dedup_posts_on_change_only():
    scanner._last_posted.clear()
    d = _decision("BUY_CALL", "HIGH", "AMZN", 200.0)
    assert scanner.actionable_changed(d) is True    # first time → post
    assert scanner.actionable_changed(d) is False   # unchanged → skip
    # conviction change → post again
    assert scanner.actionable_changed(_decision("BUY_CALL", "MEDIUM", "AMZN", 200.0)) is True
    # direction flip → post
    assert scanner.actionable_changed(_decision("BUY_PUT", "MEDIUM", "AMZN", 200.0)) is True


def test_dedup_hold_and_low_clear_state():
    scanner._last_posted.clear()
    assert scanner.actionable_changed(_decision("BUY_CALL", "HIGH", "PLTR", 40.0)) is True
    # a HOLD clears state so the next fresh setup re-posts
    assert scanner.actionable_changed(TickerDecision("PLTR", "HOLD", None, None, "LOW", "weak")) is False
    assert scanner.actionable_changed(_decision("BUY_CALL", "HIGH", "PLTR", 40.0)) is True
    # LOW conviction is never postable
    assert scanner.actionable_changed(_decision("BUY_CALL", "LOW", "PLTR", 40.0)) is False


# ── scan is fail-open per ticker ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_run_equities_scan_fail_open(monkeypatch):
    monkeypatch.setattr(scanner, "equities_tickers", lambda: ["AAA", "BBB"])

    async def bars(t, **_kw):
        if t == "BBB":
            raise RuntimeError("feed glitch")
        return [_bar(datetime(2026, 6, 26, 13, 35, tzinfo=UTC) + timedelta(minutes=5 * i),
                     100, 101, 99, 100 + i) for i in range(5)]

    async def daily(_t, **_kw):
        return []

    decisions = await scanner.run_equities_scan(NOW, bars_fetcher=bars, daily_fetcher=daily)
    tickers = [d.ticker for d in decisions]
    assert "AAA" in tickers          # good ticker produced a decision
    assert "BBB" not in tickers      # raising ticker was isolated, not fatal


def test_write_signals_snapshot(tmp_path):
    import json
    decisions = [
        _decision("BUY_PUT", "MEDIUM", "META", 550.0),
        TickerDecision("QQQ", "HOLD", None, None, "LOW", "trend_follow_v2: weak trend"),
    ]
    path = tmp_path / "equities_signals.json"
    scanner.write_signals_snapshot(decisions, now=NOW, path=path)
    data = json.loads(path.read_text())
    assert "updated_at" in data
    by = {s["ticker"]: s for s in data["signals"]}
    assert by["META"]["action"] == "BUY_PUT" and by["META"]["conviction"] == "MEDIUM"
    assert by["META"]["price"] == 550.0           # from analysis (actionable)
    assert by["QQQ"]["action"] == "HOLD" and by["QQQ"]["price"] is None  # HOLD included, no price


@pytest.mark.asyncio
async def test_run_equities_scan_skips_empty_bars(monkeypatch):
    monkeypatch.setattr(scanner, "equities_tickers", lambda: ["AAA"])

    async def bars(_t, **_kw):
        return []

    async def daily(_t, **_kw):
        return []

    decisions = await scanner.run_equities_scan(NOW, bars_fetcher=bars, daily_fetcher=daily)
    assert decisions == []  # no bars → no decision, no crash
