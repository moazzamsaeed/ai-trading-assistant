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
from trademaster.db import Signal as SignalRow
from trademaster.db import make_session_factory
from trademaster.logging import get_logger
from trademaster.models import SignalAction
from trademaster.router import TaskType, route_to_model
from trademaster.timeutils import to_et
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

PROMPT_TEMPLATE = """You are a professional intraday options trader. Time: {now_iso} | Mode: {mode_upper}

{market_context_block}

Evaluate each ticker using this SEQUENTIAL 5-step hierarchy.
STOP at any failing step and mark that ticker HOLD — do not average conflicting signals.

STEP 1 — MARKET REGIME (hard filter):
  SPY regime is {spy_regime}.
  • BULL regime: BUY_PUT signals require HIGH conviction + ALL 4 indicators aligned.
    MEDIUM conviction puts = HOLD regardless of ticker setup.
  • BEAR regime: BUY_CALL signals require HIGH conviction + ALL 4 indicators aligned.
    MEDIUM conviction calls = HOLD regardless of ticker setup.
  • NEUTRAL: standard selectivity applies.

STEP 2 — RELATIVE STRENGTH (filter):
  • BUY_CALL: ticker must show POSITIVE rel_vs_spy (outperforming) OR volume surge
    with clear catalyst. Do NOT call a ticker lagging the market without a catalyst.
  • BUY_PUT: ticker must show NEGATIVE rel_vs_spy (underperforming the market).
    Never short a ticker that is outperforming SPY — you are fighting momentum.

STEP 3 — INDICATOR CONFLUENCE (the setup):
  Bullish requires ALL of: price > VWAP AND RSI 45–70 AND EMA20 > EMA50 AND volume_ratio > 1.3
  Bearish requires ALL of: price < VWAP AND RSI 30–55 AND EMA20 < EMA50 AND volume_ratio > 1.3
  RSI note: 45–70 is unambiguously bullish. 30–55 is unambiguously bearish.
            RSI 55–70 without other confirmation → lean HOLD.
  Conviction: HIGH = all 4 criteria. MEDIUM = 3 criteria. LOW = 2 or fewer → HOLD.

STEP 4 — STRIKE & EXPIRY:
  • Strike rules:
    - HIGH conviction → ATM (at-the-money, max gamma, tightest spreads, easiest exit).
    - MEDIUM conviction + WEEKLY → 1 strike OTM (delta ~0.35–0.40; still fillable, manageable theta).
    - MEDIUM conviction + 0DTE → HOLD. Do NOT trade. On 0DTE, OTM options decay 15%+/hour
      after 2 PM ET, bid-ask spreads blow out, and the option can go no-bid entirely in the
      final 30 minutes. Theta risk makes OTM 0DTE a near-certain loss even when direction is right.
    The system validates strikes against the real options chain — pick the nearest whole number.
  • Expiry: "0DTE" only for SPY/QQQ/IWM (ETFs with daily options) when HIGH conviction
    AND time before 14:00 ET. All other tickers or MEDIUM conviction: "WEEKLY".

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
    for key in ("vwap", "rsi14", "ema20", "ema50", "atr14", "volume_ratio_20"):
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
    """Fetch SPY regime + per-ticker relative strength vs SPY."""
    try:
        spy_bars = await bars_fetcher("SPY", timeframe_minutes=5, limit=60)
        spy_snap = indicators.snapshot(spy_bars)
        spy_price = float(spy_snap.get("last_close") or 0)
        spy_vwap = float(spy_snap.get("vwap") or spy_price)
        spy_open = float(spy_bars[0].open) if spy_bars else spy_price
        spy_pct = (spy_price - spy_open) / spy_open * 100 if spy_open else 0.0
    except Exception:  # noqa: BLE001
        return {"spy_regime": "NEUTRAL", "spy_price": 0, "spy_vwap": 0,
                "spy_pct": 0, "ticker_perf": {}, "tickers_above_vwap": 0}

    if spy_price > spy_vwap * 1.005:
        spy_regime = "BULL"
    elif spy_price < spy_vwap * 0.995:
        spy_regime = "BEAR"
    else:
        spy_regime = "NEUTRAL"

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

    return {
        "spy_regime": spy_regime,
        "spy_price": round(spy_price, 2),
        "spy_vwap": round(spy_vwap, 2),
        "spy_pct": round(spy_pct, 2),
        "ticker_perf": ticker_perf,
        "tickers_above_vwap": above_vwap_count,
    }


def _format_market_context_block(ctx: dict, truth_social_posts: list[str]) -> str:
    """Format the market context header for the LLM prompt."""
    lines = [
        "═══ MARKET CONTEXT ═══",
        f"SPY: ${ctx['spy_price']:.2f} | VWAP: ${ctx['spy_vwap']:.2f} | "
        f"Day: {ctx['spy_pct']:+.1f}% | Regime: {ctx['spy_regime']}",
        f"Watchlist breadth: {ctx['tickers_above_vwap']} tickers above VWAP",
        "",
        "Relative performance vs SPY today:",
    ]
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

    # Macro context: Trump/China/Fed headlines written by Hermes cron (fail-open)
    try:
        from integrations.macro_context_client import get_macro_headlines
        truth_posts = get_macro_headlines()
    except Exception:  # noqa: BLE001
        truth_posts = []

    market_context_block = _format_market_context_block(market_ctx, truth_posts)

    # Per-ticker context (bars + news + indicators) — gather in parallel-ish.
    # Track tickers with no bar data: the LLM will see an explicit warning in
    # the ticker block, AND we hard-override any BUY decision to HOLD after
    # parsing so a hallucinated "RSI looks bullish" can never slip through.
    ticker_blocks: list[str] = []
    no_bar_tickers: set[str] = set()
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

    prompt = PROMPT_TEMPLATE.format(
        now_iso=now.strftime("%Y-%m-%d %H:%M UTC"),
        mode_upper=mode.upper(),
        spy_regime=market_ctx["spy_regime"],
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

    if mode == "aggressive":
        actionable = [d for d in decisions if d.action != "HOLD" and d.conviction == "HIGH"]
    else:
        actionable = [d for d in decisions if d.action != "HOLD"]

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
