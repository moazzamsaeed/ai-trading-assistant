"""Async wrapper around `alpaca-py` for market data, news, and trading.

`alpaca-py` is synchronous; we wrap calls in `asyncio.to_thread()` to avoid
blocking the event loop that Discord + the scheduler share.

Read paths (account, positions, orders, news) are cheap. Write paths
(cancel, close) are exercised by the risk manager's kill switch and by
Phase 2 trade execution. Every write call logs structured event lines.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum

from alpaca.data.historical.news import NewsClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.enums import DataFeed
from alpaca.data.requests import (
    NewsRequest,
    OptionChainRequest,
    StockBarsRequest,
    StockLatestQuoteRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    OrderClass,
    OrderSide,
    PositionIntent,
    TimeInForce,
)
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, OptionLegRequest

from trademaster.config import get_settings
from trademaster.logging import get_logger
from trademaster.timeutils import to_et

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


def _enum_str(v) -> str:
    """alpaca-py returns some fields as enums; we want the underlying string value."""
    if isinstance(v, Enum):
        return str(v.value)
    return str(v)


def _to_account(raw) -> AccountSnapshot:
    return AccountSnapshot(
        account_number=_enum_str(getattr(raw, "account_number", "")),
        status=_enum_str(getattr(raw, "status", "")),
        multiplier=_enum_str(getattr(raw, "multiplier", "")),
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


async def get_unrealized_pnl() -> Decimal:
    """Sum of unrealized_pl across all open positions.

    Returns Decimal("0") on any error so a connectivity blip never halts trading.
    """
    try:
        positions = await get_positions()
        return sum(
            Decimal(str(getattr(p, "unrealized_pl", 0) or 0)) for p in positions
        )
    except Exception:  # noqa: BLE001
        return Decimal("0")


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


# ====================================================================
# Options
# ====================================================================


_OCC_RE = re.compile(
    r"^(?P<root>[A-Z]+)(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})"
    r"(?P<kind>[CP])(?P<strike>\d{8})$"
)


def build_occ_symbol(
    ticker: str,
    expiry: date,
    option_type: str,
    strike: float,
) -> str:
    """Build an OCC option symbol, e.g. 'SPY260512C00500000'.

    `option_type` is "call", "put", "BUY_CALL", or "BUY_PUT".
    """
    kind = "C" if "call" in option_type.lower() else "P"
    strike_int = round(strike * 1000)
    return f"{ticker}{expiry.strftime('%y%m%d')}{kind}{strike_int:08d}"


def parse_occ_symbol(occ: str) -> tuple[str, date, str, Decimal]:
    """Parse an OCC option symbol like 'SPY240315P00495000'.

    Returns (underlying, expiry, option_type, strike). Raises ValueError if
    the symbol does not match the OCC 21-char format.
    """
    m = _OCC_RE.match(occ.strip())
    if not m:
        raise ValueError(f"not a valid OCC symbol: {occ!r}")
    yy, mm, dd = int(m["yy"]), int(m["mm"]), int(m["dd"])
    year = 2000 + yy
    expiry = date(year, mm, dd)
    option_type = "call" if m["kind"] == "C" else "put"
    strike = Decimal(m["strike"]) / Decimal("1000")
    return m["root"], expiry, option_type, strike


def _options_client() -> OptionHistoricalDataClient:
    settings = get_settings()
    return OptionHistoricalDataClient(
        api_key=settings.alpaca_api_key.get_secret_value(),
        secret_key=settings.alpaca_api_secret.get_secret_value(),
    )


@dataclass(frozen=True)
class OptionQuote:
    """Normalized snapshot for one option contract."""

    occ_symbol: str
    underlying: str
    strike: Decimal
    expiry: date
    option_type: str  # "call" or "put"
    bid: Decimal
    ask: Decimal
    mid: Decimal
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    implied_volatility: Decimal | None

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


def _to_decimal(v) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (ValueError, ArithmeticError):
        return None


def _snapshot_to_quote(occ_symbol: str, snap) -> OptionQuote | None:
    """Convert an alpaca-py OptionsSnapshot to OptionQuote. Skips malformed entries."""
    try:
        underlying, expiry, option_type, strike = parse_occ_symbol(occ_symbol)
    except ValueError:
        return None

    quote = getattr(snap, "latest_quote", None)
    bid = _to_decimal(getattr(quote, "bid_price", None)) or Decimal("0")
    ask = _to_decimal(getattr(quote, "ask_price", None)) or Decimal("0")
    if bid <= 0 and ask <= 0:
        return None  # no live market

    greeks = getattr(snap, "greeks", None)
    return OptionQuote(
        occ_symbol=occ_symbol,
        underlying=underlying,
        strike=strike,
        expiry=expiry,
        option_type=option_type,
        bid=bid,
        ask=ask,
        mid=(bid + ask) / 2,
        delta=_to_decimal(getattr(greeks, "delta", None)) if greeks else None,
        gamma=_to_decimal(getattr(greeks, "gamma", None)) if greeks else None,
        theta=_to_decimal(getattr(greeks, "theta", None)) if greeks else None,
        vega=_to_decimal(getattr(greeks, "vega", None)) if greeks else None,
        implied_volatility=_to_decimal(getattr(snap, "implied_volatility", None)),
    )


async def get_options_chain(
    underlying: str,
    *,
    expiry: date | None = None,
    strike_lo: Decimal | None = None,
    strike_hi: Decimal | None = None,
) -> list[OptionQuote]:
    """Fetch the option chain for `underlying`, optionally filtered.

    `expiry` filters to a single expiration date (used for 0DTE: today).
    `strike_lo`/`strike_hi` clamp the strike range to keep the response small.
    Returns options with at least one quoted side. Sorted by (option_type, strike).
    """

    def _fetch() -> list[OptionQuote]:
        kwargs: dict = {"underlying_symbol": underlying}
        if expiry is not None:
            kwargs["expiration_date"] = expiry
        if strike_lo is not None:
            kwargs["strike_price_gte"] = float(strike_lo)
        if strike_hi is not None:
            kwargs["strike_price_lte"] = float(strike_hi)
        req = OptionChainRequest(**kwargs)
        snapshots = _options_client().get_option_chain(req)
        # snapshots is dict[occ_symbol -> OptionsSnapshot]
        quotes: list[OptionQuote] = []
        for occ, snap in (snapshots or {}).items():
            q = _snapshot_to_quote(occ, snap)
            if q is not None:
                quotes.append(q)
        quotes.sort(key=lambda q: (q.option_type, q.strike))
        return quotes

    return await asyncio.to_thread(_fetch)


# ====================================================================
# Stock quotes
# ====================================================================


@dataclass(frozen=True)
class StockQuote:
    symbol: str
    bid: Decimal
    ask: Decimal
    mid: Decimal
    timestamp: datetime


def _stock_client() -> StockHistoricalDataClient:
    settings = get_settings()
    return StockHistoricalDataClient(
        api_key=settings.alpaca_api_key.get_secret_value(),
        secret_key=settings.alpaca_api_secret.get_secret_value(),
    )


async def get_latest_stock_quote(symbol: str) -> StockQuote:
    """Return the latest top-of-book quote for `symbol`."""

    def _fetch() -> StockQuote:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        resp = _stock_client().get_stock_latest_quote(req)
        # alpaca-py returns dict[symbol -> Quote]
        q = resp.get(symbol) if isinstance(resp, dict) else None
        if q is None:
            raise RuntimeError(f"no quote returned for {symbol}")
        bid = _to_decimal(getattr(q, "bid_price", None)) or Decimal("0")
        ask = _to_decimal(getattr(q, "ask_price", None)) or Decimal("0")
        return StockQuote(
            symbol=symbol,
            bid=bid,
            ask=ask,
            mid=(bid + ask) / 2 if (bid > 0 and ask > 0) else (bid or ask),
            timestamp=getattr(q, "timestamp", datetime.now(UTC)),
        )

    return await asyncio.to_thread(_fetch)


# ====================================================================
# Multi-leg option orders (iron condor entries + exits)
# ====================================================================


# Statuses that mean the order is settled — no need to keep polling.
_TERMINAL_ORDER_STATUSES = {
    "filled",
    "canceled",
    "cancelled",
    "expired",
    "rejected",
    "done_for_day",
    "replaced",
    "suspended",
}


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    status: str
    filled_avg_price: Decimal | None
    filled_qty: Decimal
    submitted_at: datetime
    raw_status: str  # exact string Alpaca returned


def _to_order_result(raw) -> OrderResult:
    raw_status_field = getattr(raw, "status", "")
    return OrderResult(
        order_id=str(getattr(raw, "id", "")),
        status=_enum_str(raw_status_field).lower(),
        filled_avg_price=_to_decimal(getattr(raw, "filled_avg_price", None)),
        filled_qty=_to_decimal(getattr(raw, "filled_qty", 0)) or Decimal("0"),
        submitted_at=getattr(raw, "submitted_at", datetime.now(UTC)),
        raw_status=_enum_str(raw_status_field),
    )


@dataclass(frozen=True)
class IronCondorLegSpec:
    """One leg in a multi-leg order request."""

    occ_symbol: str
    side: str  # "buy" or "sell"
    position_intent: str  # "sell_to_open", "buy_to_open", "sell_to_close", "buy_to_close"
    ratio_qty: int = 1


def _build_limit_order(
    *,
    qty: int,
    limit_price: Decimal,
    side: str,  # net posture: "sell" for credit, "buy" for debit
    legs: list[IronCondorLegSpec],
) -> LimitOrderRequest:
    side_enum = OrderSide.SELL if side == "sell" else OrderSide.BUY
    return LimitOrderRequest(
        symbol="",  # ignored for MLEG
        qty=qty,
        side=side_enum,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.MLEG,
        limit_price=float(limit_price),
        legs=[
            OptionLegRequest(
                symbol=leg.occ_symbol,
                ratio_qty=leg.ratio_qty,
                side=OrderSide.SELL if leg.side == "sell" else OrderSide.BUY,
                position_intent=PositionIntent(leg.position_intent),
            )
            for leg in legs
        ],
    )


async def submit_iron_condor_entry(
    *,
    qty: int,
    limit_credit_per_contract: Decimal,
    short_put: str,
    long_put: str,
    short_call: str,
    long_call: str,
) -> OrderResult:
    """Submit a 4-leg iron-condor open order (net credit, day TIF).

    `limit_credit_per_contract` is in dollars (e.g., 0.80 means $0.80 credit).
    Alpaca expects the per-share net price, not the per-contract dollars.
    Iron condor: short put + long put + short call + long call, all opening.
    """
    # Net price for the spread, in per-share dollars
    limit_price = (limit_credit_per_contract / Decimal("100")).quantize(Decimal("0.01"))

    legs = [
        IronCondorLegSpec(short_put, "sell", "sell_to_open"),
        IronCondorLegSpec(long_put, "buy", "buy_to_open"),
        IronCondorLegSpec(short_call, "sell", "sell_to_open"),
        IronCondorLegSpec(long_call, "buy", "buy_to_open"),
    ]
    order_req = _build_limit_order(
        qty=qty, limit_price=limit_price, side="sell", legs=legs
    )

    def _do() -> OrderResult:
        resp = _trading_client().submit_order(order_req)
        result = _to_order_result(resp)
        log.info(
            "alpaca_iron_condor_submitted",
            order_id=result.order_id,
            status=result.status,
            qty=qty,
            limit_price=str(limit_price),
        )
        return result

    return await asyncio.to_thread(_do)


async def submit_iron_condor_close(
    *,
    qty: int,
    limit_debit_per_contract: Decimal,
    short_put: str,
    long_put: str,
    short_call: str,
    long_call: str,
) -> OrderResult:
    """Close a 4-leg iron condor at a target net debit. Reverses each leg."""
    limit_price = (limit_debit_per_contract / Decimal("100")).quantize(Decimal("0.01"))
    legs = [
        IronCondorLegSpec(short_put, "buy", "buy_to_close"),
        IronCondorLegSpec(long_put, "sell", "sell_to_close"),
        IronCondorLegSpec(short_call, "buy", "buy_to_close"),
        IronCondorLegSpec(long_call, "sell", "sell_to_close"),
    ]
    order_req = _build_limit_order(
        qty=qty, limit_price=limit_price, side="buy", legs=legs
    )

    def _do() -> OrderResult:
        resp = _trading_client().submit_order(order_req)
        result = _to_order_result(resp)
        log.info(
            "alpaca_iron_condor_close_submitted",
            order_id=result.order_id,
            status=result.status,
            qty=qty,
            limit_price=str(limit_price),
        )
        return result

    return await asyncio.to_thread(_do)


async def get_single_option_quote(occ_symbol: str) -> "OptionQuote | None":
    """Fetch current bid/ask for one option contract by its OCC symbol.

    Returns None when the symbol is not listed or has no live market.
    """
    try:
        underlying, expiry, _opt_type, strike = parse_occ_symbol(occ_symbol)
    except ValueError:
        return None
    chain = await get_options_chain(
        underlying,
        expiry=expiry,
        strike_lo=strike - Decimal("0.5"),
        strike_hi=strike + Decimal("0.5"),
    )
    for q in chain:
        if q.occ_symbol == occ_symbol:
            return q
    return None


async def submit_single_option_buy(
    *,
    qty: int,
    occ_symbol: str,
    limit_price: Decimal,
) -> OrderResult:
    """Market buy-to-open with DAY — fills at best ask immediately; cancels at 4 PM if not.

    IOC is not supported for option orders on Alpaca (paper or live) — same
    restriction as sells. DAY achieves the same intent for liquid ATM options
    during RTH. `limit_price` is kept for logging reference only.
    """
    order_req = MarketOrderRequest(
        symbol=occ_symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        position_intent=PositionIntent.BUY_TO_OPEN,
    )

    def _do() -> OrderResult:
        resp = _trading_client().submit_order(order_req)
        result = _to_order_result(resp)
        log.info(
            "alpaca_single_option_buy_submitted",
            occ=occ_symbol,
            qty=qty,
            ref_ask=str(limit_price),
            order_id=result.order_id,
            status=result.status,
        )
        return result

    return await asyncio.to_thread(_do)


async def submit_single_option_sell(
    *,
    qty: int,
    occ_symbol: str,
    limit_price: Decimal,
) -> OrderResult:
    """Market sell-to-close with DAY — fills at best bid immediately; cancels at 4 PM if not.

    IOC is not supported for option orders on Alpaca (paper or live).
    DAY achieves the same intent — fills instantly when liquidity exists for
    ATM/near-ATM options during RTH, and auto-cancels at market close.
    `limit_price` is kept for logging reference only.
    """
    order_req = MarketOrderRequest(
        symbol=occ_symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        position_intent=PositionIntent.SELL_TO_CLOSE,
    )

    def _do() -> OrderResult:
        resp = _trading_client().submit_order(order_req)
        result = _to_order_result(resp)
        log.info(
            "alpaca_single_option_sell_submitted",
            occ=occ_symbol,
            qty=qty,
            ref_bid=str(limit_price),
            order_id=result.order_id,
            status=result.status,
        )
        return result

    return await asyncio.to_thread(_do)


async def get_order(order_id: str) -> OrderResult:
    """Fetch a single order's current status."""

    def _do() -> OrderResult:
        return _to_order_result(_trading_client().get_order_by_id(order_id))

    return await asyncio.to_thread(_do)


async def cancel_order(order_id: str) -> None:
    """Cancel an open order. No-op if the order is already in a terminal state."""

    def _do() -> None:
        try:
            _trading_client().cancel_order_by_id(order_id)
        except Exception as e:  # noqa: BLE001
            # Already filled/cancelled — safe to ignore
            log.debug("alpaca_cancel_order_noop", order_id=order_id, reason=str(e))

    await asyncio.to_thread(_do)


async def wait_for_order(
    order_id: str,
    *,
    timeout_s: float = 120.0,
    poll_interval_s: float = 1.5,
) -> OrderResult:
    """Poll `get_order` until the status is terminal or `timeout_s` elapses.

    Returns the last seen OrderResult — caller inspects `.status` to decide
    next steps (filled vs cancelled vs rejected). On timeout, returns the
    last observed state without raising; caller can choose to cancel.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    last: OrderResult | None = None
    while True:
        result = await get_order(order_id)
        last = result
        if result.status in _TERMINAL_ORDER_STATUSES:
            return result
        if asyncio.get_event_loop().time() >= deadline:
            log.warning(
                "alpaca_wait_for_order_timeout",
                order_id=order_id,
                last_status=result.status,
            )
            return result
        await asyncio.sleep(poll_interval_s)
    # Unreachable; mypy appeasement.
    return last  # type: ignore[return-value]


# ====================================================================
# Stock bars (for technical indicators)
# ====================================================================


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar."""

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    vwap: Decimal | None


def _to_bar(raw) -> Bar:
    return Bar(
        timestamp=getattr(raw, "timestamp", datetime.now(UTC)),
        open=_to_decimal(getattr(raw, "open", 0)) or Decimal("0"),
        high=_to_decimal(getattr(raw, "high", 0)) or Decimal("0"),
        low=_to_decimal(getattr(raw, "low", 0)) or Decimal("0"),
        close=_to_decimal(getattr(raw, "close", 0)) or Decimal("0"),
        volume=int(getattr(raw, "volume", 0) or 0),
        vwap=_to_decimal(getattr(raw, "vwap", None)),
    )


async def get_daily_bars(
    symbol: str,
    *,
    limit: int = 10,
) -> list[Bar]:
    """Fetch the last `limit` daily bars (one bar per session).

    Unlike get_recent_bars, this does NOT anchor to today's open —
    it looks back across multiple calendar days to build multi-session context.
    Used for week-trend, previous close, MA5/MA10 calculations.
    """
    def _fetch() -> list[Bar]:
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(1, TimeFrameUnit.Day),
            limit=limit,
            feed=DataFeed.IEX,
        )
        resp = _stock_client().get_stock_bars(req)
        if isinstance(resp, dict):
            raw_bars = resp.get(symbol, [])
        elif hasattr(resp, "data"):
            raw_bars = resp.data.get(symbol, []) if isinstance(resp.data, dict) else resp.data
        else:
            raw_bars = []
        return [_to_bar(b) for b in raw_bars]

    return await asyncio.to_thread(_fetch)


async def get_recent_bars(
    symbol: str,
    *,
    timeframe_minutes: int = 5,
    limit: int = 30,
    warmup_days: int = 0,
) -> list[Bar]:
    """Fetch the last `limit` bars at `timeframe_minutes` granularity.

    Returns oldest-first.

    By default (warmup_days=0), anchors to today's RTH open (9:30 AM ET) so
    the intraday agent sees real-time price action, not stale pre-market bars.
    Without an explicit start, Alpaca returns extended-hours bars from ~4 AM
    which causes the agent to miss the entire RTH session.

    With warmup_days>0, the fetch spans warmup_days+2 calendar days back so
    trend indicators that need long lookback (EMA50, volume_ratio_20) have
    valid values at today's market open instead of None for the first 1–4
    hours of the session. Extended-hours bars (pre-market 4-9:30 and
    post-market 16-20) ARE returned by IEX on multi-day spans and are
    filtered out in Python here so vol_ratio (current vs prior 20) compares
    today's RTH bar against prior RTH bars, not against a low-volume
    overnight tick. VWAP must still be session-anchored by the caller (see
    indicators.snapshot session_start_et parameter).
    """
    def _fetch() -> list[Bar]:
        tf = TimeFrame(timeframe_minutes, TimeFrameUnit.Minute)
        now_et = to_et(datetime.now(UTC))
        rth_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)

        if warmup_days > 0:
            # Alpaca returns OLDEST-first up to req_limit, so a 3-day-back start
            # with limit=250 can fully exhaust on prior-session bars and never
            # reach today's. Anchor to the start of the most recent trading day
            # that's `warmup_days` sessions back, padding for weekends so the
            # response always reaches NOW.
            weekday = now_et.weekday()  # 0=Mon ... 4=Fri
            days_back = warmup_days
            # Walking back across the weekend costs 2 extra calendar days.
            for _ in range(warmup_days):
                if weekday == 0:  # Monday → previous Friday
                    days_back += 2
                weekday = (weekday - 1) % 7
            start = (now_et - timedelta(days=days_back)).replace(
                hour=9, minute=30, second=0, microsecond=0
            )
            req_limit = max(limit + 120, 200)
        else:
            start = rth_open if now_et >= rth_open else now_et.replace(hour=4, minute=0, second=0, microsecond=0)
            req_limit = limit

        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            limit=req_limit,
            start=start,
            feed=DataFeed.IEX,
        )
        resp = _stock_client().get_stock_bars(req)
        # BarSet is dict[symbol -> list[Bar]] or has a .data attribute
        if isinstance(resp, dict):
            raw_bars = resp.get(symbol, [])
        elif hasattr(resp, "data"):
            raw_bars = resp.data.get(symbol, []) if isinstance(resp.data, dict) else resp.data
        else:
            raw_bars = []
        bars = [_to_bar(b) for b in raw_bars]
        if warmup_days > 0:
            bars = [b for b in bars if _is_rth_et(b.timestamp)]
        return bars[-limit:]

    return await asyncio.to_thread(_fetch)


def _is_rth_et(ts: datetime) -> bool:
    """True if ts (any tz) falls inside US equity RTH: 9:30-16:00 ET, Mon-Fri."""
    et = to_et(ts)
    if et.weekday() >= 5:
        return False
    minutes = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= minutes < 16 * 60
