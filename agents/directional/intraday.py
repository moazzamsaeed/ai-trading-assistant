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
from trademaster.watchlist import load_tickers

log = get_logger(__name__)

AGENT_NAME = "intraday_directional"

# Strategy parameters
BARS_TIMEFRAME_MIN = 5
BARS_LIMIT = 30
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
        "selectivity": (
            "Only signal HIGH conviction setups (3+ indicators aligning strongly). "
            "MEDIUM conviction → HOLD. Miss opportunities before taking a bad trade."
        ),
        "strike": "Choose strike: ATM for all signals (max gamma exposure).",
        "exit_hint": "Exits: take profit at +100% on premium, stop at -50% on premium.",
        "profit_target": "+100%",
        "stop_loss": "-50%",
    },
    "selective": {
        "selectivity": (
            "Selectivity matters. False positives are worse than missed opportunities. "
            "Default to HOLD unless at least 2-3 indicators align."
        ),
        "strike": "Choose strike: ATM if HIGH conviction (max gamma); 1 strike OTM if MEDIUM.",
        "exit_hint": "Exits: take profit at +50% on premium, stop at -30% on premium.",
        "profit_target": "+50%",
        "stop_loss": "-30%",
    },
}

PROMPT_TEMPLATE = """You are an intraday directional options trader scanning at {now_iso}.
Mode: {mode_upper}

You see {n} tickers below with recent indicators and news. For each ticker, decide:
- BUY_CALL  : bullish setup, buy a call option
- BUY_PUT   : bearish setup, buy a put option
- HOLD      : no edge

{selectivity} A typical strong setup:

  - Bullish: price > VWAP, RSI(14) in 40-65 range, EMA20 > EMA50, volume_ratio > 1.3,
             news catalyst or breaking out of recent range
  - Bearish: price < VWAP, RSI(14) in 35-60 range, EMA20 < EMA50, volume_ratio > 1.3,
             news headwind or breaking down

When you BUY:
- {strike}
- Choose expiry: "0DTE" if HIGH conviction and current time before 14:00 ET; otherwise "WEEKLY"
- Conviction reflects how many indicators align: HIGH = 3+, MEDIUM = 2, LOW = 1 (HOLD instead)
- {exit_hint}

Output a JSON array of objects, one per ticker, in the same order as input. Schema:
[
  {{"ticker": "SYM", "action": "BUY_CALL"|"BUY_PUT"|"HOLD", "strike": number|null,
    "expiry": "0DTE"|"WEEKLY"|null, "conviction": "HIGH"|"MEDIUM"|"LOW",
    "reasoning": "short 1-2 sentence justification"}}
]

No prose, no markdown, just the JSON array.

--- Tickers ---
{ticker_blocks}
"""


def _format_ticker_block(
    ticker: str, snap: dict, news_headlines: list[str]
) -> str:
    """Compact per-ticker context for the LLM prompt."""
    lines = [f"## {ticker}"]
    lines.append(f"last_close: ${snap.get('last_close')}")
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
    et_time = now.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
    time_str = et_time.strftime("%I:%M %p ET").lstrip("0")

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

    # Per-ticker context (bars + news + indicators) — gather in parallel-ish.
    ticker_blocks: list[str] = []
    for t in tickers:
        try:
            bars: list[Bar] = await bars_fetcher(
                t, timeframe_minutes=BARS_TIMEFRAME_MIN, limit=BARS_LIMIT
            )
        except Exception as e:  # noqa: BLE001
            log.warning("directional_bars_failed", ticker=t, error=str(e))
            bars = []
        snap = indicators.snapshot(bars)
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
        ticker_blocks.append(_format_ticker_block(t, snap, headlines))

    prompt = PROMPT_TEMPLATE.format(
        now_iso=now.strftime("%Y-%m-%d %H:%M UTC"),
        mode_upper=mode.upper(),
        n=len(tickers),
        selectivity=mc["selectivity"],
        strike=mc["strike"],
        exit_hint=mc["exit_hint"],
        ticker_blocks="\n\n".join(ticker_blocks),
    )

    response = await route_to_model(
        TaskType.INTRADAY_SCAN,
        prompt,
        session_factory=factory,
    )
    decisions = _parse_decisions(response.text, tickers)
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
