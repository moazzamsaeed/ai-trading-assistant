"""Intraday scan agent.

Runs every 15 minutes during RTH (Mon-Fri). Pulls recent news, asks
DeepSeek V4-Flash whether anything is actionable for the watchlist,
and emits a Signal (ALERT_ONLY if actionable, HOLD if not).

Phase 1.4c: alert-only mode. No order execution — that lands in Phase 2
with the iron-condor strategist. The signal still goes through the
risk manager (which short-circuits on HOLD/ALERT_ONLY) for the audit
trail.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from integrations.alpaca_client import NewsArticle, get_recent_news
from trademaster.db import Signal as SignalRow
from trademaster.db import make_session_factory
from trademaster.logging import get_logger
from trademaster.models import Signal, SignalAction
from trademaster.router import TaskType, route_to_model
from trademaster.timeutils import fmt_et
from trademaster.watchlist import load_tickers

log = get_logger(__name__)

AGENT_NAME = "intraday"
HOLD_MARKER = "HOLD"

PROMPT_TEMPLATE = """You are an intraday trading scanner running at {now_iso}.

Watchlist: {watchlist}

News from the last {minutes} minutes:
{news_block}

Identify any actionable signal for the watchlist tickers. Rules:

- If nothing is actionable, respond with exactly the single word: HOLD
- Otherwise respond with a terse alert (under 250 words) containing:
  * Ticker
  * What changed (1-2 lines from the news)
  * Direction (bullish/bearish/neutral-volatility)
  * Confidence (low/medium/high)
  * Suggested action TYPE only (e.g., "watch for VWAP reclaim", "iron condor
    candidate if IV spikes") — DO NOT propose specific strike prices or orders.

False positives create alert fatigue. Be selective. Stay grounded in the news.
"""


def _format_news(articles: list[NewsArticle]) -> str:
    if not articles:
        return "(no recent news)"
    lines: list[str] = []
    for a in articles:
        symbols = ",".join(a.symbols) if a.symbols else "—"
        ts = fmt_et(a.created_at, "%H:%M ET")
        summary = (a.summary or "").strip().replace("\n", " ")
        if len(summary) > 250:
            summary = summary[:247] + "..."
        lines.append(f"- [{ts}] ({symbols}) {a.headline.strip()}\n  {summary}")
    return "\n".join(lines)


async def run_intraday_scan(
    *,
    watchlist: tuple[str, ...] | None = None,
    minutes_back: int = 30,
    news_limit: int = 30,
    now: datetime | None = None,
    session_factory: Callable[[], Session] | None = None,
    news_fetcher=get_recent_news,
) -> tuple[Signal, str | None]:
    """Pull news → ask DeepSeek Flash → persist Signal.

    Returns (signal, alert_text). `alert_text` is None when the scanner
    decides HOLD; the caller (scheduler) skips posting in that case.
    """
    now = now or datetime.now(UTC)
    factory = session_factory or make_session_factory()
    if watchlist is None:
        watchlist = load_tickers()
    hours_back = max(1, (minutes_back + 59) // 60)

    articles = await news_fetcher(watchlist, hours_back=hours_back, limit=news_limit)
    # Trim to actual minutes_back window since news_fetcher uses hours.
    cutoff = now - timedelta(minutes=minutes_back)
    articles = [a for a in articles if a.created_at >= cutoff]

    prompt = PROMPT_TEMPLATE.format(
        now_iso=fmt_et(now, "%Y-%m-%d %H:%M ET"),
        watchlist=", ".join(watchlist),
        minutes=minutes_back,
        news_block=_format_news(articles),
    )

    response = await route_to_model(
        TaskType.INTRADAY_SCAN,
        prompt,
        session_factory=factory,
    )

    body = response.text.strip()
    is_hold = body.upper().startswith(HOLD_MARKER) and len(body) <= 16

    if is_hold:
        signal = Signal(
            task_type=TaskType.INTRADAY_SCAN.value,
            agent=AGENT_NAME,
            action=SignalAction.HOLD,
            reasoning=body,
            extra={
                "watchlist": list(watchlist),
                "news_count": len(articles),
                "model": response.model,
                "cost_usd": str(response.cost_usd),
            },
        )
        alert_text: str | None = None
    else:
        signal = Signal(
            task_type=TaskType.INTRADAY_SCAN.value,
            agent=AGENT_NAME,
            action=SignalAction.ALERT_ONLY,
            reasoning=body,
            extra={
                "watchlist": list(watchlist),
                "news_count": len(articles),
                "model": response.model,
                "cost_usd": str(response.cost_usd),
            },
        )
        alert_text = body

    with factory() as session:
        session.add(
            SignalRow(
                task_type=signal.task_type,
                agent=signal.agent,
                action=signal.action.value,
                symbol=None,
                confidence=signal.confidence,
                reasoning=signal.reasoning,
                payload=signal.extra,
                accepted=True,  # alert-only / hold have no risk decision
            )
        )
        session.commit()

    log.info(
        "intraday_scan_complete",
        action=signal.action.value,
        articles=len(articles),
        cost_usd=str(response.cost_usd),
    )
    return signal, alert_text
