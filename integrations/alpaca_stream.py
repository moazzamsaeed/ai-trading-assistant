"""Alpaca real-time WebSocket stream — volume surge + news trigger.

Runs two daemon threads:
  - alpaca-bar-stream  : 1-min bars → detects volume surges per ticker
  - alpaca-news-stream : real-time news feed → fires on any watchlist mention

Both threads call on_trigger(ticker, reason) on the main asyncio event loop
via run_coroutine_threadsafe when a condition fires. The orchestrator wires
on_trigger to run_directional_scan so the LLM scan happens within seconds
of a catalyst instead of waiting up to 10 minutes for the next poll.

Debounce: the same ticker cannot trigger more than once per DEBOUNCE_SECONDS
to avoid hammering the LLM on a sustained volume spike.
"""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from alpaca.data.live import NewsDataStream, StockDataStream

from trademaster.config import get_settings
from trademaster.logging import get_logger

log = get_logger(__name__)

VOLUME_SURGE_RATIO = 2.0  # bar volume must be >= N× the rolling average
MIN_HISTORY_BARS = 10     # need at least this many bars before checking surge
DEBOUNCE_SECONDS = 120    # cooldown per ticker after a trigger fires


Trigger = Callable[[str, str], Awaitable[None]]  # async(ticker, reason) -> None


class DirectionalStreamTrigger:
    """Watches Alpaca bars + news; fires on_trigger on the main loop on a catalyst.

    Usage:
        trigger = DirectionalStreamTrigger(
            main_loop=asyncio.get_running_loop(),
            on_trigger=my_scan_callback,
            watchlist=("SPY", "NVDA", ...),
        )
        trigger.start()   # non-blocking, launches daemon threads
        ...
        trigger.stop()    # clean shutdown
    """

    def __init__(
        self,
        *,
        main_loop: asyncio.AbstractEventLoop,
        on_trigger: Trigger,
        watchlist: tuple[str, ...],
    ) -> None:
        self._loop = main_loop
        self._on_trigger = on_trigger
        self._watchlist = {t.upper() for t in watchlist}
        self._bar_vols: dict[str, deque[int]] = defaultdict(lambda: deque(maxlen=20))
        self._last_trigger: dict[str, datetime] = {}
        self._stock_stream: StockDataStream | None = None
        self._news_stream: NewsDataStream | None = None
        self._stock_thread: threading.Thread | None = None
        self._news_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # Debounce + dispatch                                                  #
    # ------------------------------------------------------------------ #

    def _can_trigger(self, ticker: str) -> bool:
        last = self._last_trigger.get(ticker)
        if last is None:
            return True
        return (datetime.now(UTC) - last).total_seconds() >= DEBOUNCE_SECONDS

    def _fire(self, ticker: str, reason: str) -> None:
        if not self._can_trigger(ticker):
            log.debug("stream_debounced", ticker=ticker, reason=reason)
            return
        self._last_trigger[ticker] = datetime.now(UTC)
        log.info("stream_trigger", ticker=ticker, reason=reason)
        coro = self._on_trigger(ticker, reason)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            # Same loop (test context or direct call) — schedule as task.
            self._loop.create_task(coro)
        else:
            # Called from a different thread (production stream threads).
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ------------------------------------------------------------------ #
    # Bar handler — 1-min bars, volume surge detection                     #
    # ------------------------------------------------------------------ #

    async def _handle_bar(self, bar) -> None:
        ticker = str(getattr(bar, "symbol", "")).upper()
        if ticker not in self._watchlist:
            return
        vol = int(getattr(bar, "volume", 0) or 0)
        history = self._bar_vols[ticker]
        history.append(vol)
        if len(history) < MIN_HISTORY_BARS:
            return
        avg = sum(history) / len(history)
        if avg > 0 and vol / avg >= VOLUME_SURGE_RATIO:
            self._fire(ticker, f"volume_surge_{vol/avg:.1f}x")

    # ------------------------------------------------------------------ #
    # News handler — any article mentioning a watchlist ticker             #
    # ------------------------------------------------------------------ #

    async def _handle_news(self, news) -> None:
        symbols: list[str] = list(getattr(news, "symbols", []) or [])
        headline = str(getattr(news, "headline", "") or "")[:80]
        for sym in symbols:
            if sym.upper() in self._watchlist:
                self._fire(sym.upper(), f"news:{headline}")

    # ------------------------------------------------------------------ #
    # Thread targets                                                        #
    # ------------------------------------------------------------------ #

    def _run_stock_stream(self) -> None:
        settings = get_settings()
        self._stock_stream = StockDataStream(
            api_key=settings.alpaca_api_key.get_secret_value(),
            secret_key=settings.alpaca_api_secret.get_secret_value(),
        )
        self._stock_stream.subscribe_bars(self._handle_bar, *self._watchlist)
        try:
            self._stock_stream.run()
        except Exception as e:  # noqa: BLE001
            log.error("stock_stream_crashed", error=str(e))

    def _run_news_stream(self) -> None:
        settings = get_settings()
        self._news_stream = NewsDataStream(
            api_key=settings.alpaca_api_key.get_secret_value(),
            secret_key=settings.alpaca_api_secret.get_secret_value(),
        )
        # Subscribe to all news — filter to watchlist in _handle_news.
        self._news_stream.subscribe_news(self._handle_news, "*")
        try:
            self._news_stream.run()
        except Exception as e:  # noqa: BLE001
            log.error("news_stream_crashed", error=str(e))

    # ------------------------------------------------------------------ #
    # Lifecycle                                                             #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Launch both streams in background daemon threads."""
        self._stock_thread = threading.Thread(
            target=self._run_stock_stream,
            daemon=True,
            name="alpaca-bar-stream",
        )
        self._news_thread = threading.Thread(
            target=self._run_news_stream,
            daemon=True,
            name="alpaca-news-stream",
        )
        self._stock_thread.start()
        self._news_thread.start()
        log.info("stream_started", watchlist=sorted(self._watchlist))

    def stop(self) -> None:
        """Signal both streams to shut down."""
        if self._stock_stream:
            try:
                self._stock_stream.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._news_stream:
            try:
                self._news_stream.stop()
            except Exception:  # noqa: BLE001
                pass
        log.info("stream_stopped")
