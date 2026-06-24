"""Pre-market research agent.

Runs once per trading day at 8am ET (scheduled by `trademaster.scheduler`).
Pulls a WEEK of market + mega-cap-tech news from Alpaca plus the upcoming
macro-event calendar, asks the LLM for a PREDICTIVE briefing (week-in-review →
upcoming catalysts → tech-earnings watch → today's setup & prediction), then
persists an `ALERT_ONLY` Signal, writes the daily bias for intraday scans, and
returns the text for Discord.
"""

from __future__ import annotations

import json as _json
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from integrations.alpaca_client import NewsArticle, get_recent_news
from integrations.daily_bias import write_daily_bias
from trademaster.db import Signal as SignalRow
from trademaster.db import make_session_factory
from trademaster.event_calendar import upcoming_events
from trademaster.logging import get_logger
from trademaster.models import Signal, SignalAction
from trademaster.router import TaskType, route_to_model
from trademaster.timeutils import fmt_et

log = get_logger(__name__)

AGENT_NAME = "research"

# News universe for the briefing: SPY/QQQ + the mega-caps that actually drive
# the index. Broader than the trading watchlist (SPY only) so the briefing has
# real market/tech context to predict from.
NEWS_TICKERS: tuple[str, ...] = (
    "SPY", "QQQ", "NVDA", "AAPL", "MSFT", "AMZN", "GOOG", "META", "TSLA", "AMD",
)

PROMPT_TEMPLATE = """You are a pre-market strategist for SPY 0DTE/weekly options. \
Today is {date_iso} (US Eastern).
Your job is to PREDICT the day, not just summarize — synthesize the past week
and what's ahead into an actionable bias.

Coverage tickers (market + mega-cap tech that drive SPY): {tickers}

News from the last {days} days ({count} articles):
{news_block}

Scheduled macro events ahead (authoritative calendar):
{events_block}

Produce a pre-market briefing in these sections, in order:

1. **Last Week in Review** — what actually drove SPY and big tech over the past
   week (themes, not a headline dump). What's the prevailing trend/regime going in?
2. **Upcoming Catalysts** — combine the macro calendar above with the news. Call
   out the NEXT high-impact event and when. Flag if today/this week is an event
   day (expect volatility).
3. **Tech Earnings Watch** — from the news, which mega-caps report in the next
   few days and the likely SPY impact. (No earnings-calendar feed — infer from
   the news; say "none flagged in news" if so.)
4. **Today's Setup & Prediction** — GIVEN the week's trend + what's ahead,
   predict the likely path for SPY today: direction lean, key levels to watch,
   and the scenario that would confirm or invalidate it. Be specific.
5. **Synthesis** — one paragraph: the day's thesis and what to watch.
6. **BIAS_JSON** — a single JSON object (no markdown/code fence) on its own line:
   {{"bias":"BULLISH"|"BEARISH"|"NEUTRAL","summary":"one sentence thesis","catalysts":["...","..."],"risks":["...","..."]}}
   bias is exactly one of BULLISH, BEARISH, NEUTRAL. catalysts/risks: ≤3 each.

Rules:
- Ground claims in the news + calendar provided; do not fabricate tickers,
  prices, or earnings dates.
- The prediction (section 4) is the point — commit to a lean and the
  levels/scenario behind it, even if NEUTRAL (then say why it's range-bound).
- If a section has no content, write "No notable items."
- Markdown, bold headers, bullets. Keep under 1300 words (excluding BIAS_JSON).
"""


def _format_events_block(events: list[tuple], today) -> str:
    """Render upcoming macro events with how-many-days-out, for the prompt."""
    if not events:
        return "(no scheduled high-impact macro events in the next 10 days)"
    lines = []
    for d, name in events:
        delta = (d - today).days
        when = "TODAY" if delta == 0 else ("tomorrow" if delta == 1 else f"in {delta} days")
        lines.append(f"- {d.isoformat()} ({when}): {name}")
    return "\n".join(lines)


def _format_news_block(articles: list[NewsArticle]) -> str:
    if not articles:
        return "(no articles in window)"
    lines: list[str] = []
    for a in articles:
        symbols = ",".join(a.symbols) if a.symbols else "—"
        ts = fmt_et(a.created_at, "%Y-%m-%d %H:%M ET")
        # Trim long summaries to keep token use predictable.
        summary = (a.summary or "").strip().replace("\n", " ")
        if len(summary) > 400:
            summary = summary[:397] + "..."
        lines.append(f"- [{ts}] ({symbols}) {a.headline.strip()}\n  {summary}")
    return "\n".join(lines)


async def run_premarket_briefing(
    *,
    watchlist: tuple[str, ...] | None = None,
    hours_back: int = 168,  # 7 days — enough to read the week's trend, not just overnight
    news_limit: int = 90,
    now: datetime | None = None,
    session_factory: Callable[[], Session] | None = None,
    news_fetcher=get_recent_news,
) -> tuple[str, Signal]:
    """Fetch a week of market/tech news + upcoming macro events → ask the LLM for
    a predictive briefing → persist Signal + daily bias → return (text, Signal).

    `watchlist` overrides the news universe (defaults to NEWS_TICKERS, the broad
    market+tech set — NOT the SPY-only trading watchlist). Injectable for tests.
    """
    now = now or datetime.now(UTC)
    factory = session_factory or make_session_factory()
    news_universe = watchlist or NEWS_TICKERS

    articles = await news_fetcher(news_universe, hours_back=hours_back, limit=news_limit)
    events = upcoming_events(now.date(), days=10)
    log.info(
        "premarket_news_fetched",
        tickers=list(news_universe),
        count=len(articles),
        hours_back=hours_back,
        upcoming_events=len(events),
    )

    prompt = PROMPT_TEMPLATE.format(
        date_iso=now.strftime("%Y-%m-%d"),
        tickers=", ".join(news_universe),
        days=round(hours_back / 24),
        count=len(articles),
        news_block=_format_news_block(articles),
        events_block=_format_events_block(events, now.date()),
    )

    # Long-form synthesis, once-daily, latency-insensitive → generous timeout so
    # a multi-thousand-token briefing doesn't trip the hot-path 30s default (the
    # bug that failed the briefing 3 mornings running). Applies to primary AND
    # fallback (route_to_model forwards client_kwargs to both).
    response = await route_to_model(
        TaskType.PRE_MARKET_RESEARCH,
        prompt,
        session_factory=factory,
        timeout_s=180.0,
    )

    signal = Signal(
        task_type=TaskType.PRE_MARKET_RESEARCH.value,
        agent=AGENT_NAME,
        action=SignalAction.ALERT_ONLY,
        reasoning=response.text,
        extra={
            "news_tickers": list(news_universe),
            "news_count": len(articles),
            "hours_back": hours_back,
            "upcoming_events": [f"{d.isoformat()}:{n}" for d, n in events],
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

    # Extract BIAS_JSON line and write daily bias file for intraday scans.
    try:
        for line in response.text.splitlines():
            line = line.strip()
            if line.startswith("{") and "bias" in line and "summary" in line:
                bias_data = _json.loads(line)
                write_daily_bias(
                    bias=bias_data.get("bias", "NEUTRAL"),
                    summary=bias_data.get("summary", ""),
                    catalysts=bias_data.get("catalysts", []),
                    risks=bias_data.get("risks", []),
                    date_str=now.strftime("%Y-%m-%d"),
                )
                break
    except Exception as e:  # noqa: BLE001
        log.debug("premarket_bias_extract_failed", error=str(e))

    log.info(
        "premarket_briefing_generated",
        chars=len(response.text),
        articles=len(articles),
        cost_usd=str(response.cost_usd),
    )
    return response.text, signal
