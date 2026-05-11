"""Async wrapper around `alpaca-py` for market data, news, and trading.

`alpaca-py` is synchronous; we wrap calls in `asyncio.to_thread()` to avoid
blocking the event loop that Discord + the scheduler share.

Read paths (account, positions, orders, news) are cheap. Write paths
(cancel, close) are exercised by the risk manager's kill switch and by
Phase 2 trade execution. Every write call logs structured event lines.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest
from alpaca.trading.client import TradingClient

from trademaster.config import get_settings
from trademaster.logging import get_logger

log = get_logger(__name__)

DEFAULT_WATCHLIST = ("SPY", "QQQ", "IWM", "DIA")


@dataclass(frozen=True)
class NewsArticle:
    headline: str
    summary: str
    url: str
    created_at: datetime
    symbols: tuple[str, ...]
    source: str


def _client() -> NewsClient:
    settings = get_settings()
    return NewsClient(
        api_key=settings.alpaca_api_key.get_secret_value(),
        secret_key=settings.alpaca_api_secret.get_secret_value(),
    )


def _trading_client() -> TradingClient:
    """Trading client. Uses `paper=True` when TRADING_MODE=paper."""
    settings = get_settings()
    return TradingClient(
        api_key=settings.alpaca_api_key.get_secret_value(),
        secret_key=settings.alpaca_api_secret.get_secret_value(),
        paper=settings.trading_mode == "paper",
    )


@dataclass(frozen=True)
class AccountSnapshot:
    """Normalized account fields the risk manager needs."""

    account_number: str
    status: str
    multiplier: str  # "1" for cash account; "2"/"4" for margin (D-001)
    cash: Decimal
    buying_power: Decimal
    equity: Decimal
    portfolio_value: Decimal
    pattern_day_trader: bool
    trading_blocked: bool
    account_blocked: bool


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    qty: Decimal  # signed: positive long, negative short
    avg_entry_price: Decimal
    market_value: Decimal
    unrealized_pl: Decimal
    current_price: Decimal
    side: str  # "long" or "short"
    asset_class: str


def _to_account(raw) -> AccountSnapshot:
    return AccountSnapshot(
        account_number=str(getattr(raw, "account_number", "")),
        status=str(getattr(raw, "status", "")),
        multiplier=str(getattr(raw, "multiplier", "")),
        cash=Decimal(str(getattr(raw, "cash", "0") or "0")),
        buying_power=Decimal(str(getattr(raw, "buying_power", "0") or "0")),
        equity=Decimal(str(getattr(raw, "equity", "0") or "0")),
        portfolio_value=Decimal(str(getattr(raw, "portfolio_value", "0") or "0")),
        pattern_day_trader=bool(getattr(raw, "pattern_day_trader", False)),
        trading_blocked=bool(getattr(raw, "trading_blocked", False)),
        account_blocked=bool(getattr(raw, "account_blocked", False)),
    )


def _to_position(raw) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=str(getattr(raw, "symbol", "")),
        qty=Decimal(str(getattr(raw, "qty", "0") or "0")),
        avg_entry_price=Decimal(str(getattr(raw, "avg_entry_price", "0") or "0")),
        market_value=Decimal(str(getattr(raw, "market_value", "0") or "0")),
        unrealized_pl=Decimal(str(getattr(raw, "unrealized_pl", "0") or "0")),
        current_price=Decimal(str(getattr(raw, "current_price", "0") or "0")),
        side=str(getattr(raw, "side", "")),
        asset_class=str(getattr(raw, "asset_class", "")),
    )


async def get_account() -> AccountSnapshot:
    """Fetch the current Alpaca account snapshot."""

    def _fetch() -> AccountSnapshot:
        return _to_account(_trading_client().get_account())

    return await asyncio.to_thread(_fetch)


@dataclass(frozen=True)
class MarketClock:
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


async def get_market_clock() -> MarketClock:
    """Authoritative market-open check (handles holidays)."""

    def _fetch() -> MarketClock:
        c = _trading_client().get_clock()
        return MarketClock(
            timestamp=getattr(c, "timestamp", datetime.now(UTC)),
            is_open=bool(getattr(c, "is_open", False)),
            next_open=getattr(c, "next_open", datetime.now(UTC)),
            next_close=getattr(c, "next_close", datetime.now(UTC)),
        )

    return await asyncio.to_thread(_fetch)


async def get_positions() -> list[PositionSnapshot]:
    """List all open positions."""

    def _fetch() -> list[PositionSnapshot]:
        return [_to_position(p) for p in _trading_client().get_all_positions()]

    return await asyncio.to_thread(_fetch)


async def cancel_all_orders() -> int:
    """Cancel every open order. Returns count cancelled."""

    def _do() -> int:
        results = _trading_client().cancel_orders()
        n = len(results) if hasattr(results, "__len__") else 0
        log.warning("alpaca_cancel_all_orders", count=n)
        return n

    return await asyncio.to_thread(_do)


async def close_all_positions(cancel_orders: bool = True) -> int:
    """Close every position at market. Returns count closed.

    `cancel_orders=True` also cancels any open orders first.
    """

    def _do() -> int:
        results = _trading_client().close_all_positions(cancel_orders=cancel_orders)
        n = len(results) if hasattr(results, "__len__") else 0
        log.warning("alpaca_close_all_positions", count=n, cancelled_orders=cancel_orders)
        return n

    return await asyncio.to_thread(_do)


def _to_article(raw) -> NewsArticle:
    """Normalize an alpaca-py news object to our dataclass."""
    return NewsArticle(
        headline=getattr(raw, "headline", "") or "",
        summary=getattr(raw, "summary", "") or "",
        url=getattr(raw, "url", "") or "",
        created_at=getattr(raw, "created_at", datetime.now(UTC)),
        symbols=tuple(getattr(raw, "symbols", []) or []),
        source=getattr(raw, "source", "alpaca") or "alpaca",
    )


async def get_recent_news(
    symbols: tuple[str, ...] = DEFAULT_WATCHLIST,
    *,
    hours_back: int = 18,
    limit: int = 50,
) -> list[NewsArticle]:
    """Fetch news articles for the given symbols in the last `hours_back` hours.

    Sorted newest-first. Returns at most `limit` articles.
    """

    def _fetch() -> list[NewsArticle]:
        req = NewsRequest(
            symbols=",".join(symbols),
            start=datetime.now(UTC) - timedelta(hours=hours_back),
            end=datetime.now(UTC),
            limit=limit,
            sort="desc",
        )
        raw = _client().get_news(req)
        if hasattr(raw, "news"):
            items = raw.news
        elif hasattr(raw, "data"):
            items = raw.data
        else:
            items = raw
        return [_to_article(a) for a in items]

    return await asyncio.to_thread(_fetch)
