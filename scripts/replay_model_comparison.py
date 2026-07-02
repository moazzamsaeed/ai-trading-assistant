"""Replay past directional-scan decision points through multiple LLMs.

Feeds the EXACT SAME reconstructed entry prompt at each historical timestamp to
DeepSeek v4-flash (current entry model), Claude Sonnet 4.6, and Claude Opus 4.8,
then records each model's decision (BUY_CALL / BUY_PUT / HOLD + conviction). The
model is the only variable — same bars, same news, same position context — so
divergences are attributable to the model, not the inputs.

Lookahead-safe: historical bar/news fetchers set end=T, and the position context
is reconstructed as-of T (open positions + earlier-today closes only). Nothing
after T leaks into a decision made at T.

Isolation: all scan writes (signals, agent_runs) and the LLM budget check are
pointed at a throwaway temp DB so the production DB is untouched. The as-of-T
position context still reads the real DB (that's the genuine historical state).

Usage:
    uv run python -m scripts.replay_model_comparison --days 2026-06-11 --step 10 \
        --extra-loser-points --out data/replays/cmp.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

import trademaster.router as router
from trademaster.router import TaskType
import agents.directional.intraday as intraday
from agents.directional.intraday import run_directional_scan
from trademaster.db import (
    make_engine,
    make_session_factory,
    init_db,
    Trade,
    AgentRun,
    _DIRECTIONAL_STRATEGIES,
)
import integrations.alpaca_client as ac
from integrations.alpaca_client import _to_bar, _stock_client, _to_article, _unwrap_news
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest, NewsRequest
from alpaca.data.enums import DataFeed

UTC = timezone.utc
ET = ZoneInfo("America/New_York")

# (provider, model) tuples to compare. DeepSeek is the current production entry
# model; the two Claude models are the candidate "smarter" upgrades.
MODELS = [
    ("deepseek", "deepseek-v4-flash"),
    ("anthropic", "claude-sonnet-4-6"),
]

# $/1M tokens (input, output) — used for the cost rollup independent of the repo
# PRICING table.
PRICES = {
    "deepseek-v4-flash": (0.14, 0.28),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-8": (5.0, 25.0),
}

# Real production DB factory — only read, for the as-of-T position context.
_REAL = make_session_factory()


def _is_rth(ts: datetime) -> bool:
    et = ts.astimezone(ET)
    return et.weekday() < 5 and dtime(9, 30) <= et.time() <= dtime(16, 0)


def make_bars_fetcher(T: datetime):
    """get_recent_bars clone anchored to T with end=T (no future bars)."""

    async def fetch(symbol, *, timeframe_minutes=5, limit=30, warmup_days=0):
        def _f():
            tf = TimeFrame(timeframe_minutes, TimeFrameUnit.Minute)
            T_et = T.astimezone(ET)
            if warmup_days > 0:
                weekday = T_et.weekday()
                days_back = warmup_days
                for _ in range(warmup_days):
                    if weekday == 0:
                        days_back += 2
                    weekday = (weekday - 1) % 7
                start = (T_et - timedelta(days=days_back)).replace(
                    hour=9, minute=30, second=0, microsecond=0
                )
                # Larger than prod's 200 so the oldest-first window always reaches
                # end=T (prod can truncate on Mondays; not a concern here, but this
                # removes the risk and stays lookahead-safe via end=T).
                req_limit = 1000
            else:
                rth_open = T_et.replace(hour=9, minute=30, second=0, microsecond=0)
                start = rth_open if T_et >= rth_open else T_et.replace(
                    hour=4, minute=0, second=0, microsecond=0
                )
                req_limit = limit
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=tf,
                limit=req_limit,
                start=start.astimezone(UTC),
                end=T,
                feed=DataFeed.IEX,
            )
            try:
                resp = _stock_client().get_stock_bars(req)
            except Exception:
                return []  # e.g. VIX has no equity feed — matches prod fail-open
            raw = (
                resp.data.get(symbol, [])
                if hasattr(resp, "data") and isinstance(resp.data, dict)
                else []
            )
            bars = [_to_bar(b) for b in raw]
            if warmup_days > 0:
                bars = [b for b in bars if _is_rth(b.timestamp)]
            return bars

        return await asyncio.to_thread(_f)

    return fetch


def make_news_fetcher(T: datetime):
    """get_recent_news clone with end=T (no future headlines)."""

    async def fetch(symbols, *, hours_back=18, limit=50):
        def _f():
            req = NewsRequest(
                symbols=",".join(symbols),
                start=T - timedelta(hours=hours_back),
                end=T,
                limit=limit,
                sort="desc",
            )
            try:
                raw = ac._client().get_news(req)
            except Exception:
                return []
            items = _unwrap_news(raw)
            return [_to_article(a) for a in items if hasattr(a, "headline")]

        return await asyncio.to_thread(_f)

    return fetch


def _aware(dt):
    """Normalize a DB datetime to aware-UTC (sqlite may return naive or aware)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def make_trade_context(T: datetime):
    """As-of-T replacement for db.get_directional_trade_context.

    open      = directional trades opened before T and not yet closed at T
    today_closed = directional trades closed earlier the same ET day, before T
    Same return shape the prompt formatter expects. Filtering is done in Python
    on aware-UTC-normalized timestamps to avoid sqlite naive/aware comparison
    errors, so no time filter is pushed to SQL.
    """
    day_start = datetime.combine(T.astimezone(ET).date(), dtime.min, tzinfo=ET).astimezone(UTC)

    def ctx(_factory_ignored):
        open_pos: list[dict] = []
        today_closed: list[dict] = []
        with _REAL() as s:
            rows = s.execute(
                select(Trade)
                .where(Trade.strategy.in_(_DIRECTIONAL_STRATEGIES))
                .order_by(Trade.opened_at)
            ).scalars().all()
            for r in rows:
                opened = _aware(r.opened_at)
                closed = _aware(r.closed_at)
                if opened is None or opened >= T:
                    continue
                e = r.extra or {}
                if closed is None or closed > T:
                    open_pos.append({
                        "ticker": e.get("ticker") or r.symbol,
                        "action": e.get("action"),
                        "conviction": e.get("conviction"),
                        "qty": int(r.qty) if r.qty is not None else None,
                        "entry_price": float(r.entry_price) if r.entry_price is not None else None,
                        "peak_pnl_pct": e.get("peak_pnl_pct"),
                        "opened_at": opened,
                    })
                elif day_start <= closed < T:
                    today_closed.append((closed, {
                        "ticker": e.get("ticker") or r.symbol,
                        "action": e.get("action"),
                        "realized_pnl": float(r.realized_pnl_usd)
                        if r.realized_pnl_usd is not None else None,
                        "exit_reason": e.get("exit_reason"),
                    }))
        today_closed.sort(key=lambda c: c[0])
        return {"open": open_pos, "today_closed": [c for _, c in today_closed]}

    return ctx


def make_persist_factory():
    """Throwaway temp-DB factory — absorbs all scan writes + budget checks."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="replay_")
    os.close(fd)
    eng = make_engine(f"sqlite:///{path}")
    init_db(eng)
    return make_session_factory(eng), path


async def run_one(T, provider, model, persist_factory):
    router.MODEL_MAP[TaskType.DIRECTIONAL_ENTRY] = (provider, model)  # entry routes via this now
    intraday.get_directional_trade_context = make_trade_context(T)
    try:
        decisions, _msgs, _report = await run_directional_scan(
            watchlist=("SPY",),
            now=T,
            session_factory=persist_factory,
            bars_fetcher=make_bars_fetcher(T),
            news_fetcher=make_news_fetcher(T),
            mode="aggressive",
        )
    except Exception as e:  # noqa: BLE001
        return {"action": "ERROR", "conviction": None, "reasoning": f"{type(e).__name__}: {e}"}
    d = next((x for x in decisions if x.ticker == "SPY"), None)
    if d is None:
        return {"action": "NONE", "conviction": None, "reasoning": "no SPY decision returned"}
    return {"action": d.action, "conviction": d.conviction, "reasoning": (d.reasoning or "")[:500]}


def grid(date_str, start_hm, end_hm, step_min):
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    cur = datetime.combine(d, dtime(*start_hm), tzinfo=ET)
    end = datetime.combine(d, dtime(*end_hm), tzinfo=ET)
    out = []
    while cur <= end:
        out.append(cur.astimezone(UTC))
        cur += timedelta(minutes=step_min)
    return out


def loser_points(date_str):
    """Actual directional entry timestamps on the given ET day (the real trades)."""
    d0 = datetime.combine(datetime.strptime(date_str, "%Y-%m-%d").date(), dtime.min, tzinfo=ET)
    d1 = d0 + timedelta(days=1)
    pts = []
    with _REAL() as s:
        rows = s.execute(
            select(Trade)
            .where(Trade.strategy.in_(_DIRECTIONAL_STRATEGIES))
            .where(Trade.opened_at >= d0.astimezone(UTC).replace(tzinfo=None))
            .where(Trade.opened_at < d1.astimezone(UTC).replace(tzinfo=None))
            .order_by(Trade.opened_at)
        ).scalars().all()
        for r in rows:
            e = r.extra or {}
            t = r.opened_at.replace(tzinfo=UTC)
            pts.append((t, {
                "trade_id": r.id,
                "real_action": e.get("action"),
                "real_pnl": float(r.realized_pnl_usd) if r.realized_pnl_usd is not None else None,
            }))
    return pts


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="+", required=True, help="ET dates YYYY-MM-DD")
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--start", default="9:35")
    ap.add_argument("--end", default="15:15")
    ap.add_argument("--extra-loser-points", action="store_true",
                    help="also replay the exact real-entry timestamps")
    ap.add_argument("--max-points", type=int, default=0, help="cap total grid points (0=all)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    # Disable the INTRADAY_SCAN fallback so a model failure is visible, not
    # silently re-run on Haiku.
    router.FALLBACK_MAP.pop(TaskType.INTRADAY_SCAN, None)
    router.FALLBACK_MAP.pop(TaskType.DIRECTIONAL_ENTRY, None)

    sh = tuple(int(x) for x in args.start.split(":"))
    eh = tuple(int(x) for x in args.end.split(":"))

    points = []  # (T_utc, meta)
    for day in args.days:
        g = grid(day, sh, eh, args.step)
        if args.max_points:
            g = g[: args.max_points]
        for T in g:
            points.append((T, {"kind": "grid", "day": day}))
        if args.extra_loser_points:
            for T, meta in loser_points(day):
                points.append((T, {"kind": "real_entry", "day": day, **meta}))
    points.sort(key=lambda p: p[0])

    persist_factory, persist_path = make_persist_factory()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    n = len(points)
    print(f"Replaying {n} decision points × {len(MODELS)} models = {n * len(MODELS)} LLM calls")

    with open(args.out, "w") as fout:
        for i, (T, meta) in enumerate(points, 1):
            row = {"t": T.isoformat(), "t_et": T.astimezone(ET).strftime("%H:%M ET"), **meta}
            for provider, model in MODELS:
                res = await run_one(T, provider, model, persist_factory)
                row[model] = res
            fout.write(json.dumps(row) + "\n")
            fout.flush()
            tag = meta.get("kind")
            ds = " | ".join(f"{m.split('-')[0][:4]}:{row[m]['action']}/{(row[m]['conviction'] or '-')[:3]}"
                            for _, m in MODELS)
            print(f"[{i}/{n}] {row['t_et']} ({tag})  {ds}")

    # Cost rollup from the throwaway DB's agent_runs.
    with persist_factory() as s:
        runs = s.execute(select(AgentRun)).scalars().all()
    agg = {}
    for r in runs:
        it, ot = r.input_tokens or 0, r.output_tokens or 0
        pi, po = PRICES.get(r.model, (0, 0))
        cost = it / 1e6 * pi + ot / 1e6 * po
        a = agg.setdefault(r.model, {"calls": 0, "in": 0, "out": 0, "cost": 0.0})
        a["calls"] += 1; a["in"] += it; a["out"] += ot; a["cost"] += cost
    print("\n=== COST ROLLUP (this replay) ===")
    for model, a in agg.items():
        print(f"{model:22} {a['calls']:3} calls  in={a['in']:>7} out={a['out']:>6} "
              f"${a['cost']:.4f}  (${a['cost']/max(a['calls'],1):.5f}/call)")
    print(f"\nWrote {args.out}  (throwaway db: {persist_path})")


if __name__ == "__main__":
    asyncio.run(main())
