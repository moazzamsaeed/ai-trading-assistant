#!/usr/bin/env python3
"""Fetch recent macro news from Alpaca and write macro_context.json.

Searches the last 30 minutes (but falls back to 2 hours if sparse)
for headlines related to: Trump, tariffs, China, Fed, CPI, macro.
Uses Claude/Anthropic to distill into ≤8 actionable headlines.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Make sure we can import from the project
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from integrations.alpaca_client import (
    NewsArticle,
    _client as alpaca_news_client,
)
from trademaster.config import get_settings

try:
    from alpaca.data.requests import NewsRequest
except ImportError:
    # Older alpaca-py version
    from alpaca.data.requests import NewsRequest  # type: ignore


MACRO_CONTEXT_PATH = PROJECT_ROOT / "data" / "macro_context.json"

MACRO_KEYWORDS = [
    "trump", "tariff", "china", "trade", "fed", "federal reserve",
    "inflation", "cpi", "jobs", "employment", "gdp", "recession",
    "interest rate", "rate cut", "rate hike", "yuan", "renminbi",
    "taiwan", "sanctions", "geopolitical", "war", "ukraine", "russia",
    "oil", "opec", "treasury", "yields", "dollar", "macro",
    "powell", "fomc", "supply chain", "tech ban", "export control",
    "semiconductor", "chips act", "economy", "market", "nasdaq",
    "earnings", "guidance", "outlook", "beat", "miss",
]

WATCHLIST_TICKERS = ["SPY", "NVDA", "TSLA", "AMD", "PLTR", "GOOG"]

# Broad macro symbols for news search
MACRO_SYMBOLS = ["SPY", "QQQ", "IWM", "TLT", "GLD", "USO", "DXY",
                 "NVDA", "TSLA", "AMD", "PLTR", "GOOG"]


def is_macro_relevant(article: NewsArticle) -> bool:
    """Return True if the article headline/summary touches macro topics."""
    text = (article.headline + " " + (article.summary or "")).lower()
    return any(kw in text for kw in MACRO_KEYWORDS)


def fetch_news(symbols: list[str], hours_back: float = 0.5, limit: int = 100) -> list[NewsArticle]:
    """Synchronous fetch of news articles."""
    client = alpaca_news_client()
    start = datetime.now(UTC) - timedelta(hours=hours_back)
    end = datetime.now(UTC)

    req = NewsRequest(
        symbols=",".join(symbols),
        start=start,
        end=end,
        limit=limit,
        sort="desc",
    )
    raw = client.get_news(req)
    if hasattr(raw, "news"):
        items = raw.news
    elif hasattr(raw, "data"):
        items = raw.data
    else:
        items = raw or []

    articles = []
    for a in items:
        articles.append(NewsArticle(
            headline=getattr(a, "headline", "") or "",
            summary=getattr(a, "summary", "") or "",
            url=getattr(a, "url", "") or "",
            created_at=getattr(a, "created_at", datetime.now(UTC)),
            symbols=tuple(getattr(a, "symbols", []) or []),
            source=getattr(a, "source", "alpaca") or "alpaca",
        ))
    return articles


def format_articles_for_llm(articles: list[NewsArticle]) -> str:
    lines = []
    for a in articles:
        syms = ", ".join(a.symbols) if a.symbols else "—"
        ts = a.created_at.strftime("%H:%M UTC")
        summary = (a.summary or "").strip().replace("\n", " ")[:300]
        lines.append(f"[{ts}] ({syms}) {a.headline.strip()}")
        if summary:
            lines.append(f"   {summary}")
    return "\n".join(lines)


def call_anthropic_for_headlines(news_block: str, now_iso: str) -> list[str]:
    """Use Anthropic to distill headlines into ≤8 actionable one-liners."""
    import anthropic

    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())

    prompt = f"""You are a macro news analyst for an options trading desk. Current time: {now_iso}.

Below are recent news articles (last 30-120 minutes). Your job:
1. Identify up to 8 DISTINCT macro headlines that are actionable for options traders watching: SPY, NVDA, TSLA, AMD, PLTR, GOOG.
2. Focus on: Trump statements, China/tariff moves, Fed speakers, CPI/jobs surprises, geopolitical flares, major earnings surprises, sector rotations.
3. Each headline must be ONE sentence, factual, specific — include ticker/sector impact where clear.
4. Format: bearish/bullish impact tagged where obvious, e.g. "— bearish NVDA supply chain" or "— bullish SPY".
5. Skip generic filler. If multiple articles say the same thing, merge into one headline.
6. If there is NOTHING significant (pure fluff, no macro impact), return an empty list.

News articles:
{news_block}

Respond with ONLY a JSON array of strings (the headlines), no explanation, no markdown. Example:
["Trump announces 25% tariff on Chinese EVs effective June 1 — bearish TSLA, bullish domestic auto", "Fed's Powell signals no rate cuts before Q4 — bearish SPY"]

If nothing significant: []
"""

    msg = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    block = msg.content[0]
    text = (block.text if hasattr(block, "text") else str(block)).strip()
    # Extract JSON array from response
    start_idx = text.find("[")
    end_idx = text.rfind("]") + 1
    if start_idx == -1 or end_idx == 0:
        return []

    parsed = json.loads(text[start_idx:end_idx])
    return [str(h) for h in parsed[:8]]


def main():
    now = datetime.now(UTC)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    print(f"[fetch_macro_context] Starting at {now_iso}")

    # Try 30 minutes first
    articles_30m = fetch_news(MACRO_SYMBOLS, hours_back=0.5, limit=100)
    macro_30m = [a for a in articles_30m if is_macro_relevant(a)]
    print(f"  30-min window: {len(articles_30m)} total articles, {len(macro_30m)} macro-relevant")

    # If sparse, extend to 2 hours to get broader picture
    if len(macro_30m) < 3:
        articles_2h = fetch_news(MACRO_SYMBOLS, hours_back=2.0, limit=150)
        macro_articles = [a for a in articles_2h if is_macro_relevant(a)]
        print(f"  2-hour window: {len(articles_2h)} total, {len(macro_articles)} macro-relevant")
    else:
        macro_articles = macro_30m

    # Deduplicate by headline
    seen = set()
    unique_articles = []
    for a in macro_articles:
        key = a.headline.strip().lower()[:80]
        if key not in seen:
            seen.add(key)
            unique_articles.append(a)

    unique_articles = unique_articles[:60]  # cap for LLM
    print(f"  Unique macro articles to analyze: {len(unique_articles)}")

    if not unique_articles:
        print("  No macro articles found — writing empty headlines")
        headlines = []
    else:
        news_block = format_articles_for_llm(unique_articles)
        print("  Calling Claude to distill headlines...")
        try:
            headlines = call_anthropic_for_headlines(news_block, now_iso)
        except Exception as e:
            print(f"  ERROR calling Claude: {e}")
            # Fall back: use raw headlines
            headlines = []
            for a in unique_articles[:8]:
                syms = ", ".join(a.symbols[:3]) if a.symbols else ""
                ticker_tag = f" — {syms}" if syms else ""
                headlines.append(f"{a.headline.strip()}{ticker_tag}")

    print(f"  Generated {len(headlines)} headlines")
    for h in headlines:
        print(f"    • {h}")

    output = {
        "updated_at": now_iso,
        "headlines": headlines,
    }

    MACRO_CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    MACRO_CONTEXT_PATH.write_text(json.dumps(output, indent=2) + "\n")
    print(f"  Written to {MACRO_CONTEXT_PATH}")


if __name__ == "__main__":
    main()
