"""Hourly market-analysis synthesis for #research.

Reuses the directional scan's market-context builders to produce a readable,
predictive update — trend/regime, what's driving it, setups forming, indicator
behaviour, next-hour outlook — instead of the terse per-ticker scan dump. Run
hourly during RTH (and on significant news events) by the scheduler.
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

_PROMPT = """You are a markets analyst writing a #research update for SPY options traders.
Time: {now}.

{context_block}

Recent headlines (last ~hour):
{news_block}

Write a tight, predictive update — markdown, ~250-350 words — in these sections:
1. **Trend & Regime** — where SPY is and which way it's leaning, with the key levels.
2. **What's Driving It** — the catalysts/news behind the move (or "quiet tape").
3. **Setups Forming** — any building directional setup, the level/trigger to watch,
   and the potential move if it confirms. If none, say so plainly.
4. **Indicators** — what VWAP / RSI / EMA / volume / MACD are saying right now.
5. **Next Hour** — a concrete prediction for the next 30-60 min and what would change it.

Ground everything in the context above; be specific with levels. No disclaimers."""


async def run_market_analysis(
    *,
    now: datetime | None = None,
    bars_fetcher=alpaca_client.get_recent_bars,
    news_fetcher=alpaca_client.get_recent_news,
    session_factory=None,
    trigger: str | None = None,
) -> str:
    """Build market context → ask the LLM for a predictive #research narrative.

    `trigger` (e.g. a news reason) is noted in the header when the update was
    fired by an event rather than the hourly cadence. Injectable for tests.
    """
    now = now or datetime.now(UTC)
    factory = session_factory or make_session_factory()
    tickers = list(load_tickers())

    ctx = await _build_market_context(tickers, bars_fetcher)

    try:
        from integrations.macro_context_client import get_macro_headlines
        macro = get_macro_headlines()
    except Exception:  # noqa: BLE001
        macro = []
    context_block = _format_market_context_block(ctx, macro)

    try:
        arts = await news_fetcher(tuple(tickers), hours_back=1, limit=10)
        news_block = "\n".join(f"- {a.headline}" for a in arts[:10]) or "(no fresh headlines)"
    except Exception as e:  # noqa: BLE001
        log.warning("market_analysis_news_failed", error=str(e))
        news_block = "(news fetch failed)"

    prompt = _PROMPT.format(
        now=fmt_et(now, "%Y-%m-%d %H:%M ET"),
        context_block=context_block,
        news_block=news_block,
    )
    resp = await route_to_model(TaskType.INTRADAY_SCAN, prompt, session_factory=factory)

    time_str = fmt_et(now, "%I:%M %p ET").lstrip("0")
    tag = " ⚡ (news-triggered)" if trigger else ""
    log.info("market_analysis_generated", chars=len(resp.text), trigger=trigger,
             cost_usd=str(resp.cost_usd))
    return f"📊 **Market Analysis — {time_str}{tag}**\n\n{resp.text.strip()}"
