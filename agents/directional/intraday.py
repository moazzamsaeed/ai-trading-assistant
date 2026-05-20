"""Directional intraday options-signal agent.

Runs every 10 min during RTH. For each watchlist ticker:
  1. Fetch last 30 5-min bars
  2. Compute indicators (VWAP, RSI, EMA20/50, ATR, volume ratio)
  3. Pull last-30-min news for that ticker
  4. Send compact summary to DeepSeek V4-Pro
  5. Model decides BUY_CALL / BUY_PUT / HOLD per ticker, with strike +
     expiry conviction (0DTE for high conviction, weekly otherwise).
  6. Emit ONLY the BUY signals to #signals (HOLDs are silent).

Alert-only for now. Auto-execution + exit monitor for single-leg options
land in a follow-up. The user trades these manually for the first paper
cycle so we can validate signal quality before letting the bot trade.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from sqlalchemy.orm import Session

from integrations import alpaca_client
from integrations.alpaca_client import Bar, NewsArticle
from trademaster import indicators
from trademaster.config import get_settings
from trademaster.db import NearMiss, Signal as SignalRow
from trademaster.db import make_session_factory
from trademaster.logging import get_logger
from trademaster.models import SignalAction
from trademaster.router import TaskType, route_to_model
from trademaster.timeutils import fmt_et, to_et
from trademaster.watchlist import load_tickers

log = get_logger(__name__)

AGENT_NAME = "intraday_directional"

# Strategy parameters
BARS_TIMEFRAME_MIN = 5
BARS_LIMIT = 60  # EMA50 needs ≥50 bars; 60 gives comfortable headroom
NEWS_LOOKBACK_MIN = 30


@dataclass(frozen=True)
class TickerDecision:
    ticker: str
    action: Literal["BUY_CALL", "BUY_PUT", "HOLD"]
    strike: float | None
    expiry: str | None  # "0DTE" or "WEEKLY"
    conviction: Literal["LOW", "MEDIUM", "HIGH"]
    reasoning: str


_MODE_CONFIG = {
    "aggressive": {
        "exit_hint": "Exits: take profit at +100% on premium, stop at -50% on premium.",
        "profit_target": "+100%",
        "stop_loss": "-50%",
    },
    "selective": {
        "exit_hint": "Exits: take profit at +50% on premium, stop at -30% on premium.",
        "profit_target": "+50%",
        "stop_loss": "-30%",
    },
}

PROMPT_TEMPLATE = """You are a professional SPY options trader. Time: {now_iso} | Mode: {mode_upper}

You trade SPY options ONLY. SPY IS the market — no relative strength analysis needed.
Your only job: determine if SPY is likely to move UP (buy call) or DOWN (buy put) in
the next 30–90 minutes, or stay flat (hold).

{market_context_block}

Evaluate SPY using this SEQUENTIAL 4-step hierarchy.
STOP at any failing step and mark HOLD — do not average conflicting signals.

STEP 1 — TREND + VOLATILITY FILTER:
  SPY 5-min regime: {spy_regime} | SPY 15-min bias: {spy_15min_bias}
  Volatility: {vol_regime}
  Opening Range: H=${orb_high} L=${orb_low}

  ── MULTI-DAY TREND (from market context above) ──
  Use week trend, MA5/MA10 position, and prev close/H/L to assess macro bias:
  • SPY ABOVE MA5 + MA10 + positive WTD → macro bull — favour calls, puts need strong reason.
  • SPY BELOW MA5 + MA10 + negative WTD → macro bear — favour puts, calls need strong reason.
  • Prev day high/low = key intraday S/R — price breaking above prev high = bullish; below prev low = bearish.
  • Gap UP open + already above MA5/MA10 = strong bull context — lean calls.
  • Gap DOWN open + below MA5/MA10 = strong bear context — lean puts.
  The multi-day trend is the highest-timeframe bias. Intraday signals should CONFIRM it, not fight it.

  ── TREND ALIGNMENT ──
  The 5-min and 15-min regimes must AGREE for a high-conviction trade.

  BULL + 15-min BULL → strong call setup. Puts need overwhelming evidence.
  BEAR + 15-min BEAR → strong put setup. Calls need overwhelming evidence.
  BULL + 15-min BEAR (or vice versa) → conflicting. HIGH conviction only in either direction.
  NEUTRAL → both directions valid; indicator confluence (Step 2) decides.

  ── OPENING RANGE BIAS ──
  • Price ABOVE ORH: bullish breakout confirmed — favour calls.
  • Price BELOW ORL: bearish breakdown confirmed — favour puts.
  • Price INSIDE range: no confirmed direction — be conservative, require stronger indicators.

  ── VOLATILITY ──
  • FLAT (ATR too low): premium won't move enough. HOLD all.
  • VOLATILE (ATR too high): whipsaw risk, HIGH conviction only.
  • NORMAL: full-size trades allowed.

STEP 2 — INDICATOR CONFLUENCE:
  RSI uses period 9 — faster signal on 5-min bars.

  ── STANDARD SESSION (after 11:30 ET, EMA50 fully established) ──
  BULLISH — requires ALL of:
    price > VWAP  AND  RSI9 between 45–72  AND  EMA20 > EMA50  AND  volume_ratio > 1.3

  BEARISH — requires ALL of:
    price < VWAP  AND  RSI9 between 28–55  AND  EMA20 < EMA50  AND  volume_ratio > 1.3

  Conviction: HIGH = all 4 criteria + MACD aligned. MEDIUM = 3 of 4. LOW = 2 or fewer → HOLD.

  ── EARLY SESSION (before 11:30 ET, EMA50 not yet reliable) ──
  EMA50 needs 250 min of data — before 11:30 ET it may be null or unreliable.
  If ema50 is null or time < 11:30 ET, DROP EMA50 from the required set and score on 3 factors:

  BULLISH (early): price > VWAP  AND  RSI9 between 45–72  AND  volume_ratio > 1.3
  BEARISH (early): price < VWAP  AND  RSI9 between 28–55  AND  volume_ratio > 1.3

  Early conviction: HIGH = all 3 criteria + MACD aligned. MEDIUM = 2 of 3. LOW = 1 or fewer → HOLD.

  ── ORB BREAKOUT OVERRIDE (before 10:30 ET) ──
  The first 60 minutes produce the day's most explosive moves. If ALL of these are true:
    • Time is before 10:30 ET
    • Price has broken ABOVE ORH (for calls) or BELOW ORL (for puts)
    • volume_ratio ≥ 2.0 (institutional conviction behind the breakout)
    • price is on the correct side of VWAP
  → Score as HIGH conviction immediately. EMA requirement is waived.
    This is a confirmed opening range breakout with institutional volume — the highest
    probability setup of the day. Do NOT hold waiting for EMA confirmation.

  ── INDICATORS ──
  RSI9 context:
  • > 75: overbought — momentum likely exhausted, put bias.
  • < 25: oversold — potential snap-back, call bias.
  • 55–72 without VWAP + volume confirmation: HOLD.

  MACD (6-13-4, intraday optimised):
  • macd > signal AND rising: bullish acceleration — confirms calls.
  • macd < signal AND falling: bearish acceleration — confirms puts.
  • Divergence (new price high + MACD falling): weakening — caution on calls.
  • Use as confirmation, not standalone trigger.

  ATR10: higher = wider expected move = better for directional options.

STEP 3 — STRIKE & EXPIRY:
  SPY has 0DTE options every trading day (Mon–Fri). Always prefer 0DTE for clean,
  focused intraday exposure with maximum gamma. Weekly only if time > 14:30 ET.

  Strike rules:
  • HIGH conviction → ATM (max gamma, tightest spreads, easiest exit).
  • MEDIUM conviction → 1 strike OTM (delta ~0.35–0.40).
  • MEDIUM + time > 14:30 ET → HOLD. Theta decay accelerates sharply after 2:30 PM on 0DTE.

  Expiry rule: always "0DTE" before 14:30 ET. After 14:30 ET: "WEEKLY" only if HIGH conviction.

  Opening Range tips:
  • Breakout above ORH → ATM strike near ORH level (acts as support on retests).
  • Breakdown below ORL → ATM strike near ORL level (acts as resistance on retests).

STEP 4 — FINAL SANITY CHECK:
  Before recommending a trade, ask: "Is SPY actually moving? Or is it choppy/flat?"
  • If SPY has been ranging ±0.1% for the last 30 min → HOLD. Options decay on flat days.
  • If volume is fading (volume_ratio < 1.0) → HOLD. No fuel for continuation.
  • If the signal is only marginally bullish/bearish → HOLD. Wait for a cleaner setup.
  • {exit_hint}

STEP 5 — CAPITAL EFFICIENCY:
  • Max 3 signals per scan. If more qualify, pick the 3 with strongest setups.
  • Do NOT signal the same ticker if it had a recent trade — prefer fresh opportunities.
  • {exit_hint}

Output a JSON array, one object per ticker, SAME ORDER as input:
[
  {{"ticker": "SYM", "action": "BUY_CALL"|"BUY_PUT"|"HOLD", "strike": number|null,
    "expiry": "0DTE"|"WEEKLY"|null, "conviction": "HIGH"|"MEDIUM"|"LOW",
    "reasoning": "STEP1:... STEP2:... STEP3:... decision and why"}}
]

No prose, no markdown — JSON array only.

--- TICKER DATA ---
{ticker_blocks}
"""


def _format_ticker_block(
    ticker: str,
    snap: dict,
    news_headlines: list[str],
    perf: dict | None = None,
) -> str:
    """Compact per-ticker context for the LLM prompt."""
    lines = [f"## {ticker}"]
    lines.append(f"last_close: ${snap.get('last_close')}")
    if perf:
        lines.append(
            f"day_pct: {perf.get('pct', 0):+.1f}%  "
            f"rel_vs_spy: {perf.get('rel_vs_spy', 0):+.1f}%  "
            f"above_vwap: {perf.get('above_vwap', False)}"
        )
    # Core indicators — rsi9 replaces rsi14; atr10 replaces atr14; macd added
    for key in ("vwap", "rsi9", "ema20", "ema50", "atr10", "macd", "macd_signal", "volume_ratio_20"):
        v = snap.get(key)
        if v is not None:
            lines.append(f"{key}: {v}")
    if news_headlines:
        lines.append(f"recent news ({len(news_headlines)}):")
        for h in news_headlines[:5]:
            lines.append(f"  - {h}")
    else:
        lines.append("recent news: (none)")
    return "\n".join(lines)


def _parse_decisions(text: str, tickers: list[str]) -> list[TickerDecision]:
    """Parse JSON array from LLM. On any failure, return all-HOLD."""
    s = text.strip()
    if s.startswith("```"):
        s = "\n".join(line for line in s.splitlines() if not line.startswith("```"))
    try:
        arr = json.loads(s)
    except json.JSONDecodeError:
        log.warning("directional_parse_failed", text=text[:200])
        return [
            TickerDecision(t, "HOLD", None, None, "LOW", "parse failed")
            for t in tickers
        ]
    if not isinstance(arr, list):
        return [
            TickerDecision(t, "HOLD", None, None, "LOW", "non-array response")
            for t in tickers
        ]

    by_ticker: dict[str, TickerDecision] = {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        t = str(item.get("ticker", "")).upper()
        action = str(item.get("action", "HOLD")).upper()
        if action not in ("BUY_CALL", "BUY_PUT", "HOLD"):
            action = "HOLD"
        try:
            strike = float(item["strike"]) if item.get("strike") is not None else None
        except (TypeError, ValueError):
            strike = None
        expiry = item.get("expiry")
        if expiry not in ("0DTE", "WEEKLY"):
            expiry = None
        conviction = str(item.get("conviction", "LOW")).upper()
        if conviction not in ("LOW", "MEDIUM", "HIGH"):
            conviction = "LOW"
        reasoning = str(item.get("reasoning", ""))[:300]
        by_ticker[t] = TickerDecision(t, action, strike, expiry, conviction, reasoning)

    return [
        by_ticker.get(t, TickerDecision(t, "HOLD", None, None, "LOW", "missing"))
        for t in tickers
    ]


def _next_friday(today: date) -> date:
    """Next Friday from `today` (or today itself if today is Friday)."""
    days = (4 - today.weekday()) % 7  # 4 = Friday
    if days == 0:
        days = 7  # never return same-day-Friday as "weekly"; use following Fri
    return today + timedelta(days=days)


def format_scan_report(
    decisions: list[TickerDecision],
    *,
    now: datetime,
    mode: str,
    cost_usd: str,
    n_actionable: int,
) -> str:
    """One-message scan summary for #research — posted after every scan."""
    time_str = to_et(now).strftime("%I:%M %p ET").lstrip("0")

    lines = [
        f"📊 **Directional Scan — {time_str}** [{mode.upper()}]",
        f"{len(decisions)} tickers · {n_actionable} signal{'s' if n_actionable != 1 else ''} · cost ${cost_usd}",
        "",
    ]
    for d in decisions:
        if d.action == "BUY_CALL":
            icon = "📈"
            action_str = f"BUY CALL · strike ${d.strike} · {d.expiry} · {d.conviction}"
        elif d.action == "BUY_PUT":
            icon = "📉"
            action_str = f"BUY PUT · strike ${d.strike} · {d.expiry} · {d.conviction}"
        else:
            icon = "⬜"
            action_str = f"HOLD · {d.conviction}"
        lines.append(f"{icon} **{d.ticker}** — {action_str}")
        if d.reasoning:
            lines.append(f"  _{d.reasoning}_")
    return "\n".join(lines)


def format_entry_combined(
    decision: TickerDecision,
    *,
    today: date,
    mode: str,
    trade_id: int,
    qty: int,
    occ: str,
    entry_premium: Decimal,
    total_cost: Decimal,
) -> str:
    """Single #signals message combining entry signal + execution confirmation."""
    action_word = "BUY CALL" if decision.action == "BUY_CALL" else "BUY PUT"
    icon = "📈" if decision.action == "BUY_CALL" else "📉"
    option_word = "Call" if decision.action == "BUY_CALL" else "Put"

    if decision.expiry == "0DTE":
        manual_expiry = today.strftime("%b %-d")
    else:
        wk = _next_friday(today)
        manual_expiry = wk.strftime("%b %-d")

    return (
        f"{icon} **{decision.ticker} {action_word} — bot entered** [{mode.upper()}]\n"
        f"\n"
        f"Bot: **{qty}×** `{occ}` @ **${entry_premium}**/share (${total_cost} total)\n"
        f"**Manual entry: Buy {decision.ticker} {manual_expiry} "
        f"${decision.strike} {option_word}**\n"
        f"\n"
        f"Why: {decision.reasoning}\n"
        f"Conviction: {decision.conviction} · Smart exit active (hard floor: −30%)"
    )


def format_directional_signal(
    d: TickerDecision, *, today: date, mode: str = "selective"
) -> str:
    """Plain-language buy signal for #signals."""
    action_word = "BUY a CALL" if d.action == "BUY_CALL" else "BUY a PUT"
    icon = "📈" if d.action == "BUY_CALL" else "📉"
    if d.expiry == "0DTE":
        expiry_str = f"today ({today.isoformat()})"
    else:
        wk = _next_friday(today)
        expiry_str = f"this Friday ({wk.isoformat()})"
    mc = _MODE_CONFIG.get(mode, _MODE_CONFIG["selective"])
    pt = mc["profit_target"]
    sl = mc["stop_loss"]
    return (
        f"{icon} **{d.ticker} signal — {action_word}** [{mode.upper()}]\n"
        f"\n"
        f"**{action_word} on {d.ticker}** · strike **${d.strike}** · "
        f"expiry **{expiry_str}**\n"
        f"\n"
        f"Conviction: {d.conviction}\n"
        f"Why: {d.reasoning}\n"
        f"\n"
        f"Suggested exits: take profit at **{pt} premium**, "
        f"stop loss at **{sl} premium**, close before market close if 0DTE."
    )


async def _build_market_context(
    tickers: list[str],
    bars_fetcher=alpaca_client.get_recent_bars,
) -> dict:
    """Fetch SPY regime + per-ticker relative strength, plus:
    - Opening Range (9:30-9:35 high/low) — key S/R all day
    - 15-min SPY trend bias — multi-timeframe confirmation
    - ATR-based volatility regime — skip if market too flat or too wild
    """
    try:
        spy_bars = await bars_fetcher("SPY", timeframe_minutes=5, limit=60)
        spy_snap = indicators.snapshot(spy_bars)
        spy_price = float(spy_snap.get("last_close") or 0)
        spy_vwap = float(spy_snap.get("vwap") or spy_price)
        spy_open = float(spy_bars[0].open) if spy_bars else spy_price
        spy_pct = (spy_price - spy_open) / spy_open * 100 if spy_open else 0.0
        spy_atr = float(spy_snap.get("atr10") or 0)

        # Opening Range: first 5-min bar's high/low (9:30-9:35 ET)
        orb_high = float(spy_bars[0].high) if spy_bars else 0.0
        orb_low = float(spy_bars[0].low) if spy_bars else 0.0

        # Volatility regime from ATR as % of price
        atr_pct = (spy_atr / spy_price * 100) if spy_price else 0.0
        if atr_pct < 0.05:
            vol_regime = "FLAT"       # too quiet for options — premium not moving
        elif atr_pct > 0.35:
            vol_regime = "VOLATILE"   # wide ATR — options expensive, whipsaw risk
        else:
            vol_regime = "NORMAL"     # ideal options-buying conditions

    except Exception:  # noqa: BLE001
        return {
            "spy_regime": "NEUTRAL", "spy_price": 0, "spy_vwap": 0,
            "spy_pct": 0, "ticker_perf": {}, "tickers_above_vwap": 0,
            "orb_high": 0, "orb_low": 0, "vol_regime": "NORMAL",
            "spy_15min_bias": "NEUTRAL",
        }

    if spy_price > spy_vwap * 1.005:
        spy_regime = "BULL"
    elif spy_price < spy_vwap * 0.995:
        spy_regime = "BEAR"
    else:
        spy_regime = "NEUTRAL"

    # 15-min SPY bias — multi-timeframe trend confirmation
    spy_15min_bias = "NEUTRAL"
    try:
        spy_15 = await bars_fetcher("SPY", timeframe_minutes=15, limit=20)
        if spy_15:
            snap15 = indicators.snapshot(spy_15)
            p15 = float(snap15.get("last_close") or 0)
            v15 = float(snap15.get("vwap") or p15)
            e20_15 = float(snap15.get("ema20") or 0)
            if p15 > v15 * 1.003 and e20_15 > 0 and p15 > e20_15:
                spy_15min_bias = "BULL"
            elif p15 < v15 * 0.997 and e20_15 > 0 and p15 < e20_15:
                spy_15min_bias = "BEAR"
    except Exception:  # noqa: BLE001
        pass

    ticker_perf: dict[str, dict] = {}
    above_vwap_count = 0
    for t in tickers:
        if t == "SPY":
            continue
        try:
            bars = await bars_fetcher(t, timeframe_minutes=5, limit=60)
            snap = indicators.snapshot(bars)
            t_price = float(snap.get("last_close") or 0)
            t_open = float(bars[0].open) if bars else t_price
            t_pct = (t_price - t_open) / t_open * 100 if t_open else 0.0
            t_vwap = float(snap.get("vwap") or t_price)
            above = t_price > t_vwap
            if above:
                above_vwap_count += 1
            ticker_perf[t] = {
                "pct": round(t_pct, 2),
                "rel_vs_spy": round(t_pct - spy_pct, 2),
                "above_vwap": above,
            }
        except Exception:  # noqa: BLE001
            ticker_perf[t] = {"pct": 0.0, "rel_vs_spy": 0.0, "above_vwap": False}

    # VIX — proxy for market-wide IV. Fetch last close from recent bars (fail-open).
    vix_level: float | None = None
    try:
        vix_bars = await bars_fetcher("VIX", timeframe_minutes=5, limit=5)
        if vix_bars:
            vix_level = float(vix_bars[-1].close)
    except Exception:  # noqa: BLE001
        pass

    # Multi-day context — previous close, key S/R levels, week trend, MA5/MA10.
    # Uses daily bars anchored to the last 10 sessions (fail-open).
    multi_day: dict = {}
    try:
        daily_bars = await alpaca_client.get_daily_bars("SPY", limit=12)
        if len(daily_bars) >= 2:
            closes = [float(b.close) for b in daily_bars]
            prev_bar = daily_bars[-2]           # yesterday's full session
            prev_close = float(prev_bar.close)
            prev_high = float(prev_bar.high)
            prev_low = float(prev_bar.low)
            ma5 = sum(closes[-5:]) / min(5, len(closes[-5:]))
            ma10 = sum(closes[-10:]) / min(10, len(closes[-10:]))
            wtd_pct = (spy_price - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0.0
            gap_pct = (spy_price - prev_close) / prev_close * 100 if prev_close else 0.0
            multi_day = {
                "prev_close": round(prev_close, 2),
                "prev_high": round(prev_high, 2),
                "prev_low": round(prev_low, 2),
                "ma5": round(ma5, 2),
                "ma10": round(ma10, 2),
                "above_ma5": spy_price > ma5,
                "above_ma10": spy_price > ma10,
                "week_pct": round(wtd_pct, 2),
                "gap_pct": round(gap_pct, 2),
            }
    except Exception:  # noqa: BLE001
        pass

    return {
        "spy_regime": spy_regime,
        "spy_price": round(spy_price, 2),
        "spy_vwap": round(spy_vwap, 2),
        "spy_pct": round(spy_pct, 2),
        "ticker_perf": ticker_perf,
        "tickers_above_vwap": above_vwap_count,
        "orb_high": round(orb_high, 2),
        "orb_low": round(orb_low, 2),
        "vol_regime": vol_regime,
        "spy_15min_bias": spy_15min_bias,
        "vix": round(vix_level, 2) if vix_level else None,
        "multi_day": multi_day,
    }


def _format_market_context_block(ctx: dict, truth_social_posts: list[str]) -> str:
    """Format the market context header for the LLM prompt."""
    lines = [
        "═══ MARKET CONTEXT ═══",
        f"SPY: ${ctx['spy_price']:.2f} | VWAP: ${ctx['spy_vwap']:.2f} | "
        f"Day: {ctx['spy_pct']:+.1f}% | Regime: {ctx['spy_regime']} | "
        f"15-min: {ctx.get('spy_15min_bias','?')}",
        f"Opening Range: H=${ctx.get('orb_high',0):.2f} L=${ctx.get('orb_low',0):.2f} "
        f"(price {'ABOVE ORH — breakout bullish' if ctx['spy_price'] > ctx.get('orb_high',0) else 'BELOW ORL — breakout bearish' if ctx['spy_price'] < ctx.get('orb_low',0) else 'INSIDE range — wait for breakout'})",
        f"Volatility: {ctx.get('vol_regime','NORMAL')} | VIX: {ctx['vix']:.1f} | Breadth: {ctx['tickers_above_vwap']} tickers above VWAP"
        if ctx.get('vix') else
        f"Volatility: {ctx.get('vol_regime','NORMAL')} | Breadth: {ctx['tickers_above_vwap']} tickers above VWAP",
        "",
        "Relative performance vs SPY today:",
    ]
    # Multi-day context block
    md = ctx.get("multi_day", {})
    if md:
        week_dir = "▲" if md.get("week_pct", 0) > 0 else "▼"
        gap_dir = "gap UP" if md.get("gap_pct", 0) > 0.1 else ("gap DOWN" if md.get("gap_pct", 0) < -0.1 else "flat open")
        ma5_pos = "ABOVE" if md.get("above_ma5") else "BELOW"
        ma10_pos = "ABOVE" if md.get("above_ma10") else "BELOW"
        lines.append(
            f"Week trend: {week_dir}{abs(md.get('week_pct',0)):.1f}% WTD | "
            f"Today's open: {gap_dir} ({md.get('gap_pct',0):+.2f}%)"
        )
        lines.append(
            f"Prev close: ${md.get('prev_close',0):.2f} | "
            f"Prev H/L: ${md.get('prev_high',0):.2f} / ${md.get('prev_low',0):.2f}"
        )
        lines.append(
            f"MA5: ${md.get('ma5',0):.2f} ({ma5_pos}) | "
            f"MA10: ${md.get('ma10',0):.2f} ({ma10_pos})"
        )
        lines.append("")

    perf = ctx.get("ticker_perf", {})
    leaders = [t for t, v in perf.items() if v["rel_vs_spy"] > 0]
    laggards = [t for t, v in perf.items() if v["rel_vs_spy"] < 0]
    for t, v in sorted(perf.items(), key=lambda x: -x[1]["rel_vs_spy"]):
        vwap_flag = "↑VWAP" if v["above_vwap"] else "↓VWAP"
        lines.append(
            f"  {t:<6} {v['pct']:+.1f}% (vs SPY: {v['rel_vs_spy']:+.1f}%)  {vwap_flag}"
        )
    lines.append(f"  Leaders: {', '.join(leaders) or 'none'}")
    lines.append(f"  Laggards: {', '.join(laggards) or 'none'}")
    if truth_social_posts:
        lines.append("")
        lines.append("⚡ MACRO HEADLINES (Trump/China/Fed, last 60 min):")
        for post in truth_social_posts[:5]:
            lines.append(f"  • {post[:200]}")
        lines.append("  Note: These headlines may move SPY, QQQ, TSLA, NVDA, and tech broadly.")
    lines.append("═══════════════════════")
    return "\n".join(lines)


def _log_near_misses(
    hold_decisions: list[TickerDecision],
    *,
    ticker_snaps: dict[str, dict],
    spy_regime: str,
    session_factory,
) -> None:
    """Persist near-miss rows for HOLDs that would fire at relaxed 1.0× volume.

    Bullish near-miss: price>VWAP + RSI 45-70 + EMA20>EMA50 + vol>=1.0 → ≥3 criteria.
    Bearish near-miss: price<VWAP + RSI 30-55 + EMA20<EMA50 + vol>=1.0 → ≥3 criteria.
    """
    rows: list[NearMiss] = []
    for d in hold_decisions:
        snap = ticker_snaps.get(d.ticker, {})
        price = float(snap.get("last_close") or 0)
        vwap = float(snap.get("vwap") or 0)
        rsi = float(snap.get("rsi9") or 0)
        ema20 = float(snap.get("ema20") or 0)
        ema50 = float(snap.get("ema50") or 0)
        vr = float(snap.get("volume_ratio_20") or 0)

        if price == 0 or vwap == 0:
            continue

        above_vwap = price > vwap
        ema_bull = ema20 > ema50 > 0
        ema_bear = 0 < ema20 < ema50
        vol_relaxed = vr >= 1.0

        bullish = sum([above_vwap, 45 <= rsi <= 72, ema_bull, vol_relaxed])
        bearish = sum([not above_vwap, 28 <= rsi <= 55, ema_bear, vol_relaxed])

        if bullish >= 3:
            criteria_met, would_be, ema_flag = bullish, "BUY_CALL", ema_bull
        elif bearish >= 3:
            criteria_met, would_be, ema_flag = bearish, "BUY_PUT", ema_bear
        else:
            continue  # genuine HOLD, not a near-miss

        rows.append(NearMiss(
            ticker=d.ticker,
            would_be_action=would_be,
            criteria_met=criteria_met,
            volume_ratio=vr if vr > 0 else None,
            rsi=rsi if rsi > 0 else None,
            above_vwap=above_vwap,
            ema_confirmed=ema_flag,
            spy_regime=spy_regime,
            llm_reasoning=d.reasoning[:300] if d.reasoning else None,
        ))

    if rows:
        try:
            with session_factory() as session:
                for r in rows:
                    session.add(r)
                session.commit()
            log.info("near_misses_logged", count=len(rows),
                     tickers=[r.ticker for r in rows])
        except Exception as e:  # noqa: BLE001
            log.debug("near_miss_log_failed", error=str(e))


async def run_directional_scan(
    *,
    watchlist: tuple[str, ...] | None = None,
    now: datetime | None = None,
    session_factory: Callable[[], Session] | None = None,
    bars_fetcher=alpaca_client.get_recent_bars,
    news_fetcher=alpaca_client.get_recent_news,
    mode: str | None = None,
) -> tuple[list[TickerDecision], list[str], str]:
    """Scan the watchlist, return (decisions, signal_messages, scan_report).

    `signal_messages` contains formatted strings for #signals (BUY signals only).
    `scan_report` is a per-ticker summary for #research (always, including HOLDs).
    """
    now = now or datetime.now(UTC)
    factory = session_factory or make_session_factory()
    if mode is None:
        mode = get_settings().directional_mode
    mc = _MODE_CONFIG.get(mode, _MODE_CONFIG["selective"])
    if watchlist is None:
        watchlist = load_tickers()
    tickers = list(watchlist)
    if not tickers:
        return [], [], ""

    # Market context: SPY regime + relative strength across watchlist
    market_ctx = await _build_market_context(tickers, bars_fetcher)

    # VIX gate: skip the scan if IV is dangerously elevated or dead quiet.
    # VIX > 35: violent whipsaw, options gaps make entries unreliable.
    # VIX < 12: too quiet, moves too small to overcome bid/ask spread costs.
    vix = market_ctx.get("vix")
    if vix is not None:
        if vix > 35:
            log.info("scan_skipped_vix_too_high", vix=vix)
            return [], [], f"Scan skipped — VIX {vix:.1f} too high (>35), whipsaw risk"
        if vix < 12:
            log.info("scan_skipped_vix_too_low", vix=vix)
            return [], [], f"Scan skipped — VIX {vix:.1f} too low (<12), moves too small for options"

    # Macro context: Trump/China/Fed headlines written by Hermes cron (fail-open)
    try:
        from integrations.macro_context_client import get_macro_headlines
        truth_posts = get_macro_headlines()
    except Exception:  # noqa: BLE001
        truth_posts = []

    # Daily bias: written by 8 AM premarket briefing, read here (fail-open)
    try:
        from integrations.daily_bias import get_daily_bias, format_bias_block
        bias = get_daily_bias()
        daily_bias_line = format_bias_block(bias) if bias else ""
    except Exception:  # noqa: BLE001
        daily_bias_line = ""

    market_context_block = _format_market_context_block(market_ctx, truth_posts)
    if daily_bias_line:
        market_context_block = daily_bias_line + "\n\n" + market_context_block

    # Per-ticker context (bars + news + indicators) — gather in parallel-ish.
    # Also retain snaps for post-LLM near-miss analysis.
    ticker_blocks: list[str] = []
    no_bar_tickers: set[str] = set()
    ticker_snaps: dict[str, dict] = {}
    for t in tickers:
        try:
            bars: list[Bar] = await bars_fetcher(
                t, timeframe_minutes=BARS_TIMEFRAME_MIN, limit=BARS_LIMIT
            )
        except Exception as e:  # noqa: BLE001
            log.warning("directional_bars_failed", ticker=t, error=str(e))
            bars = []
        snap = indicators.snapshot(bars)
        if not bars:
            no_bar_tickers.add(t)
        try:
            news_articles: list[NewsArticle] = await news_fetcher(
                (t,),
                hours_back=max(1, (NEWS_LOOKBACK_MIN + 59) // 60),
                limit=10,
            )
            cutoff = now - timedelta(minutes=NEWS_LOOKBACK_MIN)
            news_articles = [a for a in news_articles if a.created_at >= cutoff]
            headlines = [a.headline for a in news_articles]
        except Exception as e:  # noqa: BLE001
            log.warning("directional_news_failed", ticker=t, error=str(e))
            headlines = []
        t_perf = market_ctx["ticker_perf"].get(t, {})
        block = _format_ticker_block(t, snap, headlines, t_perf)
        if not bars:
            block += "\n⚠️ NO BAR DATA — MUST HOLD (cannot evaluate indicators)"
        ticker_blocks.append(block)
        ticker_snaps[t] = snap

    prompt = PROMPT_TEMPLATE.format(
        now_iso=fmt_et(now, "%Y-%m-%d %H:%M ET"),
        mode_upper=mode.upper(),
        spy_regime=market_ctx["spy_regime"],
        spy_15min_bias=market_ctx.get("spy_15min_bias", "NEUTRAL"),
        vol_regime=market_ctx.get("vol_regime", "NORMAL"),
        orb_high=market_ctx.get("orb_high", 0),
        orb_low=market_ctx.get("orb_low", 0),
        exit_hint=mc["exit_hint"],
        market_context_block=market_context_block,
        ticker_blocks="\n\n".join(ticker_blocks),
    )

    response = await route_to_model(
        TaskType.INTRADAY_SCAN,
        prompt,
        session_factory=factory,
    )
    decisions = _parse_decisions(response.text, tickers)

    # Hard-override: any BUY on a ticker with no bar data becomes HOLD.
    # This prevents hallucinated "RSI looks bullish" from slipping through
    # when the bars feed was unavailable.
    if no_bar_tickers:
        overridden = []
        for d in decisions:
            if d.ticker in no_bar_tickers and d.action != "HOLD":
                log.warning(
                    "directional_decision_overridden_no_bars",
                    ticker=d.ticker,
                    original_action=d.action,
                )
                overridden.append(
                    TickerDecision(d.ticker, "HOLD", None, None, "LOW",
                                   "no_bar_data — overridden to HOLD")
                )
            else:
                overridden.append(d)
        decisions = overridden

    if mode == "selective":
        actionable = [d for d in decisions if d.action != "HOLD" and d.conviction == "HIGH"]
    else:  # aggressive
        actionable = [d for d in decisions if d.action != "HOLD" and d.conviction in ("MEDIUM", "HIGH")]

    # Near-miss logging: for every HOLD, check if it would have triggered
    # BUY at a relaxed 1.0× volume threshold. Persisted for post-hoc analysis
    # so we can calibrate the 1.3× volume filter over time.
    _log_near_misses(
        [d for d in decisions if d.action == "HOLD" and d.ticker not in no_bar_tickers],
        ticker_snaps=ticker_snaps,
        spy_regime=market_ctx.get("spy_regime", "NEUTRAL"),
        session_factory=factory,
    )

    # Persist one Signal row per actionable decision.
    today = now.date()
    messages: list[str] = []
    if actionable:
        with factory() as session:
            for d in actionable:
                row = SignalRow(
                    task_type=TaskType.INTRADAY_SCAN.value,
                    agent=AGENT_NAME,
                    action=SignalAction.ALERT_ONLY.value,
                    symbol=d.ticker,
                    confidence=(
                        1.0 if d.conviction == "HIGH"
                        else 0.66 if d.conviction == "MEDIUM"
                        else 0.33
                    ),
                    reasoning=d.reasoning,
                    payload={
                        "action": d.action,
                        "strike": d.strike,
                        "expiry": d.expiry,
                        "conviction": d.conviction,
                        "model": response.model,
                        "cost_usd": str(response.cost_usd),
                    },
                    accepted=True,
                )
                session.add(row)
            session.commit()
        messages = [format_directional_signal(d, today=today, mode=mode) for d in actionable]

    log.info(
        "directional_scan_complete",
        n_tickers=len(tickers),
        n_actionable=len(actionable),
        actions=[f"{d.ticker}:{d.action}" for d in actionable],
        cost_usd=str(response.cost_usd),
    )
    scan_report = format_scan_report(
        decisions,
        now=now,
        mode=mode,
        cost_usd=str(response.cost_usd),
        n_actionable=len(actionable),
    )
    return decisions, messages, scan_report
