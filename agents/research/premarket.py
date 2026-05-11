"""Pre-market research agent.

Runs once per trading day at 8am ET (scheduled by `trademaster.scheduler`).
Pulls overnight news from Alpaca, asks Gemini 3.1 Pro for a structured
briefing, persists the result as an `ALERT_ONLY` Signal, and returns the
text so the Discord bot can post it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from integrations.alpaca_client import DEFAULT_WATCHLIST, NewsArticle, get_recent_news
from trademaster.db import Signal as SignalRow
from trademaster.db import make_session_factory
from trademaster.logging import get_logger
from trademaster.models import Signal, SignalAction
from trademaster.router import TaskType, route_to_model

log = get_logger(__name__)

AGENT_NAME = "research"

PROMPT_TEMPLATE = """You are a pre-market trading analyst. Today is {date_iso} (US Eastern Time).

Watchlist: {watchlist}

Overnight news (last {hours} hours, {count} articles):
{news_block}

Produce a concise pre-market briefing in five short sections, in this exact order:

1. **Overnight Summary** — what moved, what to watch.
2. **Earnings Today** — companies reporting and consensus, only if mentioned in the news.
3. **Macro Events** — FOMC, CPI, NFP, jobless claims, etc. that are mentioned or implied.
4. **Sector Signals** — sector ETF moves and rotation cues if discernible.
5. **Synthesis** — one paragraph: what today looks like and what to pay attention to.

Rules:
- Stay grounded in the news provided. Do not fabricate tickers, prices, or events.
- If a section has no relevant content, write "No notable items." for that section.
- Keep the whole briefing under 1200 words.
- Use markdown. Bold section headers. Bullet points within sections.
"""


def _format_news_block(articles: list[NewsArticle]) -> str:
    if not articles:
        return "(no articles in window)"
    lines: list[str] = []
    for a in articles:
        symbols = ",".join(a.symbols) if a.symbols else "—"
        ts = a.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        # Trim long summaries to keep token use predictable.
        summary = (a.summary or "").strip().replace("\n", " ")
        if len(summary) > 400:
            summary = summary[:397] + "..."
        lines.append(f"- [{ts}] ({symbols}) {a.headline.strip()}\n  {summary}")
    return "\n".join(lines)


async def run_premarket_briefing(
    *,
    watchlist: tuple[str, ...] = DEFAULT_WATCHLIST,
    hours_back: int = 18,
    news_limit: int = 50,
    now: datetime | None = None,
    session_factory: Callable[[], Session] | None = None,
    news_fetcher=get_recent_news,
) -> tuple[str, Signal]:
    """Fetch news → ask Gemini → persist Signal → return (text, Signal).

    `news_fetcher` and `session_factory` are injectable for tests.
    """
    now = now or datetime.now(UTC)
    factory = session_factory or make_session_factory()

    articles = await news_fetcher(watchlist, hours_back=hours_back, limit=news_limit)
    log.info(
        "premarket_news_fetched",
        watchlist=list(watchlist),
        count=len(articles),
        hours_back=hours_back,
    )

    prompt = PROMPT_TEMPLATE.format(
        date_iso=now.strftime("%Y-%m-%d"),
        watchlist=", ".join(watchlist),
        hours=hours_back,
        count=len(articles),
        news_block=_format_news_block(articles),
    )

    response = await route_to_model(
        TaskType.PRE_MARKET_RESEARCH,
        prompt,
        session_factory=factory,
    )

    signal = Signal(
        task_type=TaskType.PRE_MARKET_RESEARCH.value,
        agent=AGENT_NAME,
        action=SignalAction.ALERT_ONLY,
        reasoning=response.text,
        extra={
            "watchlist": list(watchlist),
            "news_count": len(articles),
            "hours_back": hours_back,
            "model": response.model,
            "cost_usd": str(response.cost_usd),
        },
    )

    with factory() as session:
        row = SignalRow(
            task_type=signal.task_type,
            agent=signal.agent,
            action=signal.action.value,
            symbol=None,
            confidence=signal.confidence,
            reasoning=signal.reasoning,
            payload=signal.extra,
            accepted=True,  # alert-only signals have no risk decision
        )
        session.add(row)
        session.commit()

    log.info(
        "premarket_briefing_generated",
        chars=len(response.text),
        articles=len(articles),
        cost_usd=str(response.cost_usd),
    )
    return response.text, signal
