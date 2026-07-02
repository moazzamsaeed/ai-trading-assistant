"""Market-analysis synthesis for #research.

Reuses the directional scan's market-context builders to produce a readable,
predictive update. Posted just twice a day by the scheduler:
- `mode="intraday"` — a single mid-day update (trend/regime, what's driving it,
  setups forming, indicator behaviour, next-hour outlook).
- `mode="close"` — an end-of-day wrap with tomorrow's outlook.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agents.directional.intraday import (
    _build_market_context,
    _format_market_context_block,
)
from integrations import alpaca_client
from trademaster.db import make_session_factory
from trademaster.logging import get_logger
from trademaster.router import TaskType, route_to_model
from trademaster.timeutils import fmt_et
from trademaster.watchlist import load_tickers

log = get_logger(__name__)

_PROMPT_INTRADAY = """You are a markets analyst writing a mid-day #research update for SPY options traders.
Time: {now}.

{context_block}

Recent headlines (today so far):
{news_block}

Write a tight, predictive update — markdown, ~250-350 words — in these sections:
1. **Trend & Regime** — where SPY is and which way it's leaning, with the key levels.
2. **What's Driving It** — the catalysts/news behind the move (or "quiet tape").
3. **Setups Forming** — any building directional setup, the level/trigger to watch,
   and the potential move if it confirms. If none, say so plainly.
4. **Indicators** — what VWAP / RSI / EMA / volume / MACD are saying right now.
5. **Rest of Day** — a concrete prediction into the close and what would change it.

Ground everything in the context above; be specific with levels. No disclaimers."""

_PROMPT_CLOSE = """You are a markets analyst writing the end-of-day #research wrap for SPY options traders.
Time: {now} (market has closed).

{context_block}

Recent headlines (today):
{news_block}

Write a tight wrap-up + next-day outlook — markdown, ~250-350 words — in these sections:
1. **How Today Closed** — where SPY finished, the day's range, and the regime.
2. **What Drove It** — the catalysts/news behind today's tape.
3. **Levels Into Tomorrow** — key support/resistance to watch at the next open.
4. **Catalysts Ahead** — overnight/pre-market events, earnings, or data on deck.
5. **Tomorrow's Bias** — a concrete prediction for tomorrow's direction and what would change it.

Ground everything in the context above; be specific with levels. No disclaimers."""

_PROMPTS = {"intraday": _PROMPT_INTRADAY, "close": _PROMPT_CLOSE}


async def run_market_analysis(
    *,
    now: datetime | None = None,
    mode: str = "intraday",
    bars_fetcher=alpaca_client.get_recent_bars,
    news_fetcher=alpaca_client.get_recent_news,
    session_factory=None,
) -> str:
    """Build market context → ask the LLM for a predictive #research narrative.

    `mode="intraday"` produces the mid-day update; `mode="close"` produces the
    end-of-day wrap with tomorrow's outlook. Injectable for tests.
    """
    now = now or datetime.now(UTC)
    factory = session_factory or make_session_factory()
    tickers = list(load_tickers())
    news_hours = 8 if mode == "close" else 4

    ctx = await _build_market_context(tickers, bars_fetcher)

    try:
        from integrations.macro_context_client import get_macro_headlines
        macro = get_macro_headlines()
    except Exception:  # noqa: BLE001
        macro = []
    context_block = _format_market_context_block(ctx, macro)

    try:
        arts = await news_fetcher(tuple(tickers), hours_back=news_hours, limit=10)
        news_block = "\n".join(f"- {a.headline}" for a in arts[:10]) or "(no fresh headlines)"
    except Exception as e:  # noqa: BLE001
        log.warning("market_analysis_news_failed", error=str(e))
        news_block = "(news fetch failed)"

    prompt = _PROMPTS.get(mode, _PROMPT_INTRADAY).format(
        now=fmt_et(now, "%Y-%m-%d %H:%M ET"),
        context_block=context_block,
        news_block=news_block,
    )
    resp = await route_to_model(TaskType.INTRADAY_SCAN, prompt, session_factory=factory)

    time_str = fmt_et(now, "%I:%M %p ET").lstrip("0")
    header = (
        f"📈 **Closing Wrap & Tomorrow's Outlook — {time_str}**"
        if mode == "close"
        else f"📊 **Mid-Day Market Analysis — {time_str}**"
    )
    log.info("market_analysis_generated", chars=len(resp.text), mode=mode,
             cost_usd=str(resp.cost_usd))
    return f"{header}\n\n{resp.text.strip()}"
