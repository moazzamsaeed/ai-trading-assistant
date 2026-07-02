"""Per-ticker validation of the new equities entry strategy vs the old engine.

Walks real intraday history for each equities-watchlist ticker, rebuilding the
scanner's exact inputs at every bar (trailing-window snapshot + S/R market_ctx),
and runs BOTH the new `strategy.decide_equity` and the old `signal_engine.decide`.
For every non-HOLD signal it measures the SAME-DAY forward favorable move over a
fixed hold (model-free — underlying continuation, no option costs), then reports,
per engine and per conviction: signal count, hit-rate, and avg favorable move.

The question this answers: does the new strategy fire FEWER but BETTER signals
(higher hit-rate / favorable move) than the chase-the-extension engine?

Usage:
  uv run python -m scripts.backtest_equities_strategy [START_YYYY-MM-DD] [TICKER ...]
  (no tickers → the full equities watchlist; default start = 2025-01-01)
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, time as dtime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed

import integrations.alpaca_client as ac
from integrations.alpaca_client import Bar
from trademaster import indicators
from agents.equities.strategy import decide_equity
from agents.directional.signal_engine import decide as old_decide
from agents.equities.scanner import equities_tickers

ET = ZoneInfo("America/New_York")
TF = 15
HOLD_BARS = 4  # 4 × 15m = 60m same-day hold
START = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1][:2] == "20" else "2025-01-01"
TICKERS = [a.upper() for a in sys.argv[1:] if a[:2] != "20"] or None


def _is_rth(ts) -> bool:
    et = ts.astimezone(ET)
    return et.weekday() < 5 and dtime(9, 30) <= et.time() <= dtime(16, 0)


def _to_bar(b) -> Bar:
    return Bar(timestamp=b.timestamp, open=Decimal(str(b.open)), high=Decimal(str(b.high)),
               low=Decimal(str(b.low)), close=Decimal(str(b.close)),
               volume=int(b.volume), vwap=Decimal(str(getattr(b, "vwap", None) or b.close)))


def _fetch(symbol: str) -> list[Bar]:
    req = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame(TF, TimeFrameUnit.Minute),
        start=datetime.fromisoformat(START).replace(tzinfo=timezone.utc),
        end=datetime.now(timezone.utc), feed=DataFeed.IEX,
    )
    raw = ac._stock_client().get_stock_bars(req).data.get(symbol, [])
    return [_to_bar(b) for b in raw if _is_rth(b.timestamp)]


def _market_ctx(prev_day_bars, daily_closes, today_so_far) -> dict:
    md: dict = {}
    if prev_day_bars:
        md["prev_high"] = max(float(b.high) for b in prev_day_bars)
        md["prev_low"] = min(float(b.low) for b in prev_day_bars)
        md["prev_close"] = float(prev_day_bars[-1].close)
    if len(daily_closes) >= 5:
        md["ma5"] = sum(daily_closes[-5:]) / 5.0
    if len(daily_closes) >= 10:
        md["ma10"] = sum(daily_closes[-10:]) / 10.0
    ctx = {"multi_day": md}
    if today_so_far:
        ctx["orb_high"] = float(today_so_far[0].high)
        ctx["orb_low"] = float(today_so_far[0].low)
        ctx["session_high"] = max(float(b.high) for b in today_so_far)
        ctx["session_low"] = min(float(b.low) for b in today_so_far)
    return ctx


def _favorable(day_bars, bi, action) -> float | None:
    j = bi + HOLD_BARS
    if j >= len(day_bars):
        return None
    r = float(day_bars[j].close) / float(day_bars[bi].close) - 1.0
    return r if action == "BUY_CALL" else -r


def _accumulate(stats, engine, decision, favor):
    if decision.action == "HOLD" or favor is None:
        return
    stats[(engine, decision.conviction)].append(favor)
    stats[(engine, "ALL")].append(favor)
    setup = (decision.analysis or {}).get("setup")
    if setup:  # per-setup breakdown (orb_breakout / breakout / pullback)
        stats[(engine, f"~{setup}")].append(favor)


def _run_symbol(symbol: str, stats: dict) -> None:
    bars = _fetch(symbol)
    if not bars:
        print(f"  {symbol}: no bars"); return
    # group by ET day, preserving order
    days: dict = defaultdict(list)
    for b in bars:
        days[b.timestamp.astimezone(ET).date()].append(b)
    day_keys = sorted(days)
    daily_closes: list[float] = []
    local = defaultdict(list)  # per-symbol tally
    for di, dk in enumerate(day_keys):
        day_bars = days[dk]
        prev_bars = days[day_keys[di - 1]] if di > 0 else []
        warmup = prev_bars[-60:]
        session_open = day_bars[0].timestamp.astimezone(ET).replace(
            hour=9, minute=30, second=0, microsecond=0)
        for bi in range(len(day_bars)):
            window = warmup + day_bars[: bi + 1]
            if len(window) < 30:
                continue  # indicators not bootstrapped yet
            snap = indicators.snapshot(window, session_start_et=session_open)
            ctx = _market_ctx(prev_bars, daily_closes, day_bars[: bi + 1])
            new_d = decide_equity(symbol, window, snap, ctx, now=day_bars[bi].timestamp)
            old_d = old_decide(symbol, snap, ctx)
            _accumulate(stats, "NEW", new_d, _favorable(day_bars, bi, new_d.action))
            _accumulate(stats, "OLD", old_d, _favorable(day_bars, bi, old_d.action))
            _accumulate(local, "NEW", new_d, _favorable(day_bars, bi, new_d.action))
            _accumulate(local, "OLD", old_d, _favorable(day_bars, bi, old_d.action))
        daily_closes.append(float(day_bars[-1].close))
    _print_block(f"  {symbol}", local, indent=True)


def _print_block(label, stats, *, indent=False):
    pad = "    " if indent else ""
    print(f"{label}")

    def _row(engine, key, disp):
        fs = stats.get((engine, key))
        if not fs:
            return
        n = len(fs)
        hit = sum(1 for f in fs if f > 0) / n * 100
        avg = sum(fs) / n * 100
        print(f"{pad}{engine:<3} {disp:<14} n={n:>5}  hit={hit:5.1f}%  avg favorable={avg:+.4f}%")

    for engine in ("OLD", "NEW"):
        for conv in ("ALL", "HIGH", "MEDIUM"):
            _row(engine, conv, conv)
        # per-setup breakdown for NEW (orb_breakout / breakout / pullback)
        for setup in ("~orb_breakout", "~breakout", "~pullback"):
            _row(engine, setup, setup[1:])


def main():
    syms = TICKERS or equities_tickers()
    print(f"Equities strategy backtest — {TF}min, {HOLD_BARS*TF}m hold, {START}→now")
    print(f"Tickers: {', '.join(syms)}\n")
    overall: dict = defaultdict(list)
    for s in syms:
        _run_symbol(s, overall)
        print()
    _print_block("=== TOTAL (all tickers) ===", overall)
    print("\nRead: NEW should fire FEWER signals (lower n) with a HIGHER hit-rate / "
          "avg favorable move than OLD. avg favorable is the underlying's same-day "
          "move in the signal's direction (model-free — option theta/spread excluded).")


if __name__ == "__main__":
    main()
