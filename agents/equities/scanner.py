"""Isolated equities signal scanner — ALERT-ONLY, fully separate from the SPY
condor/trend strategies.

Runs the user's stock/ETF watchlist through the EXISTING deterministic trend
engine (`agents/directional/signal_engine.decide` — ticker-agnostic) each scan
and emits plain-language BUY CALL / BUY PUT signals. NO execution, no positions,
no shared capital/risk with the live SPY strategies — it only reads market data
and posts signals. Gated by `settings.enable_equities_scanner`; the scheduler
job lives in `trademaster/scheduler.py`.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from agents.directional.intraday import TickerDecision
from agents.equities.strategy import decide_equity as _decide
from integrations import alpaca_client
from trademaster import indicators, watchlist
from trademaster.logging import get_logger
from trademaster.timeutils import to_et

log = get_logger(__name__)

EQUITIES_WATCHLIST_PATH = Path("data/watchlist_equities.json")
# Current-state snapshot (latest decision per ticker) consumed read-only by the
# Mission Control dashboard. Written every scan; includes HOLDs.
EQUITIES_SIGNALS_PATH = Path("data/equities_signals.json")
CONVICTIONS_POSTED = ("HIGH", "MEDIUM")


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def equities_tickers() -> list[str]:
    """The equities watchlist (separate file; the SPY watchlist is untouched)."""
    try:
        return list(watchlist.load_tickers(EQUITIES_WATCHLIST_PATH))
    except Exception as e:  # noqa: BLE001
        log.warning("equities_watchlist_load_failed", error=str(e))
        return []


def _ticker_market_ctx(intraday_bars, daily_bars, now: datetime) -> dict:
    """Per-ticker S/R context shaped for `signal_engine._collect_levels`:
    multi_day {prev_high/low/close, ma5, ma10} from the last COMPLETED daily
    sessions, plus orb_high/low and session_high/low from today's bars.
    Everything is fail-open — missing levels just give the S/R gate less to act on.
    """
    today = to_et(now).date()
    completed = [b for b in (daily_bars or []) if to_et(b.timestamp).date() < today]
    multi_day: dict = {}
    if completed:
        prev = completed[-1]
        multi_day["prev_high"] = float(prev.high)
        multi_day["prev_low"] = float(prev.low)
        multi_day["prev_close"] = float(prev.close)
        closes = [float(b.close) for b in completed]
        if len(closes) >= 5:
            multi_day["ma5"] = sum(closes[-5:]) / 5.0
        if len(closes) >= 10:
            multi_day["ma10"] = sum(closes[-10:]) / 10.0

    today_bars = [b for b in (intraday_bars or []) if to_et(b.timestamp).date() == today]
    ctx: dict = {"multi_day": multi_day}
    if today_bars:
        ctx["orb_high"] = float(today_bars[0].high)
        ctx["orb_low"] = float(today_bars[0].low)
        ctx["session_high"] = max(float(b.high) for b in today_bars)
        ctx["session_low"] = min(float(b.low) for b in today_bars)
    return ctx


async def run_equities_scan(
    now: datetime | None = None,
    *,
    bars_fetcher=alpaca_client.get_recent_bars,
    daily_fetcher=alpaca_client.get_daily_bars,
) -> list[TickerDecision]:
    """Run the trend engine over the equities watchlist. One TickerDecision per
    ticker (HOLD included). Fail-open per ticker — one bad fetch never breaks the
    rest of the scan. Pure read → decide; NEVER executes anything."""
    now = now or datetime.now(UTC)
    session_open = to_et(now).replace(hour=9, minute=30, second=0, microsecond=0)
    decisions: list[TickerDecision] = []
    for t in equities_tickers():
        try:
            bars = await bars_fetcher(t, timeframe_minutes=5, limit=60, warmup_days=1)
            if not bars:
                log.info("equities_scan_no_bars", ticker=t)
                continue
            snap = indicators.snapshot(bars, session_start_et=session_open)
            try:
                daily = await daily_fetcher(t, limit=12)
            except Exception:  # noqa: BLE001 — S/R is fail-open without dailies
                daily = []
            decisions.append(_decide(t, bars, snap, _ticker_market_ctx(bars, daily, now), now))
        except Exception as e:  # noqa: BLE001 — isolate per-ticker failures
            log.warning("equities_scan_ticker_failed", ticker=t, error=str(e))
    return decisions


def write_signals_snapshot(
    decisions: list[TickerDecision],
    *,
    now: datetime | None = None,
    path: Path = EQUITIES_SIGNALS_PATH,
) -> None:
    """Persist the latest decision per ticker to a JSON file for the Mission
    Control dashboard (a read-only consumer). Includes HOLDs so the table shows
    current state for every stock. Best-effort — never raises into the scan.

    Shape: {"updated_at": iso, "signals": [{ticker, action, conviction,
    reasoning, price}]}. `price` is the scan-time last_close when available
    (None for HOLDs); the dashboard shows a live price separately.
    """
    now = now or datetime.now(UTC)
    try:
        out = {
            "updated_at": now.isoformat(),
            "signals": [
                {
                    "ticker": d.ticker,
                    "action": d.action,
                    "conviction": d.conviction,
                    "reasoning": d.reasoning,
                    "price": (d.analysis or {}).get("spy_price"),  # legacy key = ticker price
                }
                for d in decisions
            ],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(out, indent=2))
        tmp.replace(path)  # atomic swap so the dashboard never reads a half-written file
    except Exception as e:  # noqa: BLE001
        log.warning("equities_signals_snapshot_failed", error=str(e))


def format_equities_signal(d: TickerDecision, *, price: float | None = None) -> str:
    """Plain-language buy signal (no options jargon — honors the #signals rule).
    Direction + ATM strike + why; the user picks the expiry (these are stocks,
    not 0DTE), so `d.expiry` is intentionally ignored."""
    icon = "📈" if d.action == "BUY_CALL" else "📉"
    action_word = "BUY a CALL" if d.action == "BUY_CALL" else "BUY a PUT"
    p = _f(price)
    price_str = f" (now ${p:.2f})" if p else ""
    strike_str = f"${d.strike:g}" if d.strike is not None else "at-the-money"
    return (
        f"{icon} **{d.ticker} — {action_word}** [{d.conviction}]\n"
        f"\n"
        f"**{action_word} on {d.ticker}**{price_str} · suggested strike "
        f"**{strike_str}** (at-the-money)\n"
        f"Why: {d.reasoning}\n"
        f"\n"
        f"_Pick an expiry that fits your timeframe. Signal only — size and "
        f"manage the trade yourself._"
    )


# In-memory dedup: ticker -> (action, conviction) last posted. Post only on a
# CHANGE so the same HIGH signal doesn't repost every 15 min. Resets on restart.
_last_posted: dict[str, tuple[str, str]] = {}


def actionable_changed(d: TickerDecision) -> bool:
    """True iff this is a postable (HIGH/MEDIUM) signal that is NEW or CHANGED vs
    the last one posted for the ticker. A HOLD (or LOW) clears the ticker's state
    so a fresh setup later re-posts."""
    if d.action == "HOLD" or d.conviction not in CONVICTIONS_POSTED:
        _last_posted.pop(d.ticker, None)
        return False
    key = (d.action, d.conviction)
    if _last_posted.get(d.ticker) == key:
        return False
    _last_posted[d.ticker] = key
    return True
