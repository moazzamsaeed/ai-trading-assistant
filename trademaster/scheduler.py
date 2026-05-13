"""Schedules pre-market, intraday, and end-of-day events.

Equity events follow US market hours (RTH 9:30-16:00 ET).
Crypto events run 24/7 with their own cadence.

Channel routing (passed as named posters by the orchestrator):
- research_poster → #research (pre-market briefing)
- signal_poster  → #signals  (broker-ready manual alerts: intraday scans,
                              iron-condor manual entry/exit signals)
- trade_poster   → #trades   (automated bot activity: order fills, exits,
                              P&L summaries)
- log_poster     → #logs     (scheduler errors, system diagnostics)
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.directional.executor import execute_directional_signal
from agents.directional.exit_monitor import run_directional_exit_monitor
from agents.directional.intraday import format_directional_signal, format_entry_combined, run_directional_scan
from agents.intraday.scan import run_intraday_scan
from agents.options.exit_monitor import run_exit_monitor
from agents.options.strategist import run_iron_condor_strategist
from agents.research.premarket import run_premarket_briefing
from integrations import alpaca_client
from trademaster.config import get_settings
from trademaster.logging import get_logger
from trademaster.state import get_state
from trademaster.watchlist import load_tickers

# Prevents concurrent directional scans (stream + scheduler can both trigger).
_scan_in_progress: bool = False

# Throttle #research posts to at most once per hour across all scan triggers.
# Trade execution is unaffected — signals are still acted on immediately.
_last_research_post: datetime | None = None
_RESEARCH_POST_INTERVAL_SECONDS = 3600

log = get_logger(__name__)

PREMARKET_TZ = "America/New_York"


Poster = Callable[[str], Awaitable[None]]
ClockFetcher = Callable[[], Awaitable[alpaca_client.MarketClock]]


async def _noop_poster(_text: str) -> None:
    return None


# ----------------- premarket -----------------


async def _premarket_job(
    *,
    research_poster: Poster,
    log_poster: Poster = _noop_poster,
) -> None:
    """Runs the briefing and forwards the text to #research."""
    try:
        text, _signal = await run_premarket_briefing()
    except Exception as e:  # noqa: BLE001
        log.error("premarket_job_failed", error=str(e), error_type=type(e).__name__)
        await log_poster(f"⚠️ Pre-market briefing failed: `{type(e).__name__}: {e}`")
        return
    await research_poster(text)


# ----------------- intraday scan -----------------


async def _intraday_scan_job(
    *,
    signal_poster: Poster,
    log_poster: Poster = _noop_poster,
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
) -> None:
    """Skip if paused or market closed; otherwise scan and post manual signal."""
    state = get_state()
    if state.is_paused():
        log.info("intraday_scan_skipped_paused", paused_until=str(state.paused_until))
        return

    try:
        clock = await clock_fetcher()
    except Exception as e:  # noqa: BLE001
        log.warning("intraday_scan_clock_failed", error=str(e))
        return

    if not clock.is_open:
        log.info("intraday_scan_skipped_closed", next_open=str(clock.next_open))
        return

    try:
        _signal, alert_text = await run_intraday_scan()
    except Exception as e:  # noqa: BLE001
        log.error("intraday_scan_failed", error=str(e), error_type=type(e).__name__)
        await log_poster(f"⚠️ Intraday scan failed: `{type(e).__name__}: {e}`")
        return

    if alert_text:
        await signal_poster(alert_text)


# ----------------- directional intraday signals + execution -----------------


async def _directional_scan_job(
    *,
    signal_poster: Poster,
    trade_poster: Poster,
    research_poster: Poster,
    log_poster: Poster = _noop_poster,
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
    post_report_on_hold: bool = True,
) -> None:
    """Sweep watchlist for directional signals.

    - Always posts BUY signals to #signals and executes via Alpaca.
    - `post_report_on_hold`: when False (stream-triggered), only posts to
      #research if there's at least one BUY signal. When True (30-min
      fallback), always posts the full per-ticker summary.
    """
    if get_state().is_paused():
        log.info("directional_scan_skipped_paused")
        return

    try:
        clock = await clock_fetcher()
    except Exception as e:  # noqa: BLE001
        log.warning("directional_scan_clock_failed", error=str(e))
        return

    if not clock.is_open:
        log.info("directional_scan_skipped_closed", next_open=str(clock.next_open))
        return

    global _scan_in_progress
    if _scan_in_progress:
        log.info("directional_scan_skipped_already_running")
        return
    _scan_in_progress = True
    try:
        decisions, messages, scan_report = await run_directional_scan()
    except Exception as e:  # noqa: BLE001
        log.error(
            "directional_scan_failed",
            error=str(e),
            error_type=type(e).__name__,
        )
        await log_poster(
            f"⚠️ Directional intraday scan failed: `{type(e).__name__}: {e}`"
        )
        return
    finally:
        _scan_in_progress = False

    # Post scan report to #research at most once per hour.
    # Scans and trade execution run on every trigger; only the Discord post is throttled.
    global _last_research_post
    now = datetime.now(UTC)
    elapsed = (now - _last_research_post).total_seconds() if _last_research_post else float("inf")
    if elapsed >= _RESEARCH_POST_INTERVAL_SECONDS and (post_report_on_hold or messages):
        await research_poster(scan_report)
        _last_research_post = now

    # Auto-execute top 2 decisions (capped to avoid multi-signal floods).
    # Combined entry+execution message posted to #signals — no separate #trades post.
    from decimal import Decimal
    from zoneinfo import ZoneInfo
    today = datetime.now(UTC).astimezone(ZoneInfo("America/New_York")).date()

    mode = get_settings().directional_mode
    conviction_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    to_execute = sorted(
        [d for d in decisions if d.action != "HOLD"
         and (mode != "aggressive" or d.conviction == "HIGH")],
        key=lambda d: (conviction_rank.get(d.conviction, 2), d.ticker),
    )[:3]  # cap at 3 per scan — enough to cover all watchlist signals in a busy move

    for decision in to_execute:
        try:
            result = await execute_directional_signal(decision, mode=mode)
            if result.executed and result.trade_id is not None:
                entry_premium = result.entry_premium or Decimal("0")
                total_cost = (entry_premium * 100 * result.qty).quantize(Decimal("0.01"))
                combined = format_entry_combined(
                    decision,
                    today=today,
                    mode=mode,
                    trade_id=result.trade_id,
                    qty=result.qty,
                    occ=result.occ or "",
                    entry_premium=entry_premium,
                    total_cost=total_cost,
                )
                await signal_poster(combined)
            elif not result.executed:
                log.info(
                    "directional_execute_skipped",
                    ticker=decision.ticker,
                    reason=result.reason,
                )
                # Always post the manual signal to #signals even when auto-execution
                # fails — user can act on it manually. Append skip reason so it's
                # clear the bot did not trade it.
                manual = format_directional_signal(decision, today=today, mode=mode)
                manual += f"\n⚠️ _Auto-execute skipped: {result.reason}_"
                await signal_poster(manual)
        except Exception as e:  # noqa: BLE001
            log.error(
                "directional_execute_error",
                ticker=decision.ticker,
                error=str(e),
                error_type=type(e).__name__,
            )
            await log_poster(
                f"⚠️ Directional execute failed for {decision.ticker}: "
                f"`{type(e).__name__}: {e}`"
            )


# ----------------- directional exit monitor -----------------


async def _directional_exit_job(
    *,
    signal_poster: Poster,
    trade_poster: Poster,
    log_poster: Poster = _noop_poster,
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
    force: bool = False,
) -> None:
    """Check open directional positions; post exits to #signals and #trades."""
    if get_state().is_paused():
        log.info("directional_exit_skipped_paused")
        return

    if not force:
        try:
            clock = await clock_fetcher()
        except Exception as e:  # noqa: BLE001
            log.warning("directional_exit_clock_failed", error=str(e))
            return
        if not clock.is_open:
            log.info("directional_exit_skipped_closed", next_open=str(clock.next_open))
            return

    try:
        results = await run_directional_exit_monitor(force_close=force or None)
    except Exception as e:  # noqa: BLE001
        log.error(
            "directional_exit_monitor_failed",
            error=str(e),
            error_type=type(e).__name__,
        )
        await log_poster(
            f"⚠️ Directional exit monitor failed: `{type(e).__name__}: {e}`"
        )
        return

    for r in results:
        combined_text = r.get("combined_text")
        if combined_text:
            await signal_poster(combined_text)  # one message to #signals
        elif r.get("error_text"):
            await log_poster(r["error_text"])


# ----------------- iron-condor strategist -----------------


async def _iron_condor_entry_job(
    *,
    signal_poster: Poster,
    trade_poster: Poster,
    log_poster: Poster = _noop_poster,
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
) -> None:
    """Strategist run. Manual instructions → #signals; execution telem → #trades."""
    state = get_state()
    if state.is_paused():
        log.info("iron_condor_skipped_paused", paused_until=str(state.paused_until))
        return

    try:
        clock = await clock_fetcher()
    except Exception as e:  # noqa: BLE001
        log.warning("iron_condor_clock_failed", error=str(e))
        return

    if not clock.is_open:
        log.info("iron_condor_skipped_closed", next_open=str(clock.next_open))
        return

    try:
        _signal, signals_text, trade_text = await run_iron_condor_strategist()
    except Exception as e:  # noqa: BLE001
        log.error(
            "iron_condor_strategist_failed",
            error=str(e),
            error_type=type(e).__name__,
        )
        await log_poster(
            f"⚠️ Iron-condor strategist failed: `{type(e).__name__}: {e}`"
        )
        return

    if signals_text:
        await signal_poster(signals_text)
    if trade_text:
        await trade_poster(trade_text)


# ----------------- iron-condor exit monitor -----------------


async def _iron_condor_exit_job(
    *,
    signal_poster: Poster,
    trade_poster: Poster,
    log_poster: Poster = _noop_poster,
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
    force: bool = False,
) -> None:
    """Sweep open IC positions; post exit instructions + trade telemetry."""
    if get_state().is_paused():
        log.info("exit_monitor_skipped_paused")
        return

    if not force:
        try:
            clock = await clock_fetcher()
        except Exception as e:  # noqa: BLE001
            log.warning("exit_monitor_clock_failed", error=str(e))
            return
        if not clock.is_open:
            log.info("exit_monitor_skipped_closed", next_open=str(clock.next_open))
            return

    try:
        results = await run_exit_monitor(force_close=force or None)
    except Exception as e:  # noqa: BLE001
        log.error(
            "exit_monitor_failed",
            error=str(e),
            error_type=type(e).__name__,
        )
        await log_poster(f"⚠️ Exit monitor failed: `{type(e).__name__}: {e}`")
        return

    for r in results:
        signal_text = r.get("signal_text")
        trade_text = r.get("trade_text")
        if signal_text:
            await signal_poster(signal_text)
        if trade_text:
            await trade_poster(trade_text)


def make_scheduler(
    *,
    research_poster: Poster,
    signal_poster: Poster,
    trade_poster: Poster,
    log_poster: Poster | None = None,
    enable_iron_condor: bool | None = None,
) -> AsyncIOScheduler:
    """Build an AsyncIOScheduler with all standing jobs registered.

    Errors caught by individual jobs route to `log_poster` if provided
    (defaults to a no-op for tests that don't care).
    IC jobs are omitted unless `enable_iron_condor=True` (or ENABLE_IRON_CONDOR=true in env).
    """
    log_post = log_poster or _noop_poster
    if enable_iron_condor is None:
        enable_iron_condor = get_settings().enable_iron_condor

    scheduler = AsyncIOScheduler(timezone=PREMARKET_TZ)

    scheduler.add_job(
        _premarket_job,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone=PREMARKET_TZ),
        kwargs={"research_poster": research_poster, "log_poster": log_post},
        id="premarket_briefing",
        replace_existing=True,
        misfire_grace_time=900,
    )

    # RTH is 9:30-16:00 ET. The cron fires every 15 min from 9:00-15:45 to
    # be permissive; the in-job Alpaca clock check is authoritative for
    # holidays and early-close days.
    scheduler.add_job(
        _intraday_scan_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute="0,15,30,45",
            timezone=PREMARKET_TZ,
        ),
        kwargs={"signal_poster": signal_poster, "log_poster": log_post},
        id="intraday_scan",
        replace_existing=True,
        misfire_grace_time=120,
    )

    # Directional fallback scan — every 60 min during RTH.
    # Real-time triggers come from the WebSocket stream (alpaca_stream.py).
    # This fallback catches slow-building setups and guards against stream gaps.
    scheduler.add_job(
        _directional_scan_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute="0",
            timezone=PREMARKET_TZ,
        ),
        kwargs={
            "signal_poster": signal_poster,
            "trade_poster": trade_poster,
            "research_poster": research_poster,
            "log_poster": log_post,
        },
        id="directional_scan",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # Directional exit monitor — every 5 min during RTH (10:00–15:25 ET).
    scheduler.add_job(
        _directional_exit_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour="10-15",
            minute="0,5,10,15,20,25,30,35,40,45",
            timezone=PREMARKET_TZ,
        ),
        kwargs={
            "signal_poster": signal_poster,
            "trade_poster": trade_poster,
            "log_poster": log_post,
        },
        id="directional_exit",
        replace_existing=True,
        misfire_grace_time=120,
    )

    # Force-close all directional positions at 15:30 ET — 30 min before bell.
    scheduler.add_job(
        _directional_exit_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour=15,
            minute=30,
            timezone=PREMARKET_TZ,
        ),
        kwargs={
            "signal_poster": signal_poster,
            "trade_poster": trade_poster,
            "log_poster": log_post,
            "force": True,
        },
        id="directional_force_close",
        replace_existing=True,
        misfire_grace_time=120,
    )

    if enable_iron_condor:
        # Iron-condor entry: 9:45 ET Mon-Fri (STRATEGIES.md 9:45-10:30 window).
        scheduler.add_job(
            _iron_condor_entry_job,
            CronTrigger(
                day_of_week="mon-fri",
                hour=9,
                minute=45,
                timezone=PREMARKET_TZ,
            ),
            kwargs={
                "signal_poster": signal_poster,
                "trade_poster": trade_poster,
                "log_poster": log_post,
            },
            id="iron_condor_entry",
            replace_existing=True,
            misfire_grace_time=300,
        )

        # Exit monitor: every 5 min during RTH (10:00-15:45 ET).
        scheduler.add_job(
            _iron_condor_exit_job,
            CronTrigger(
                day_of_week="mon-fri",
                hour="10-15",
                minute="0,5,10,15,20,25,30,35,40,45",
                timezone=PREMARKET_TZ,
            ),
            kwargs={
                "signal_poster": signal_poster,
                "trade_poster": trade_poster,
                "log_poster": log_post,
            },
            id="iron_condor_exit",
            replace_existing=True,
            misfire_grace_time=120,
        )

        # Force-close at 15:50 ET — last call regardless of P&L.
        scheduler.add_job(
            _iron_condor_exit_job,
            CronTrigger(
                day_of_week="mon-fri",
                hour=15,
                minute=50,
                timezone=PREMARKET_TZ,
            ),
            kwargs={
                "signal_poster": signal_poster,
                "trade_poster": trade_poster,
                "log_poster": log_post,
                "force": True,
            },
            id="iron_condor_force_close",
            replace_existing=True,
            misfire_grace_time=120,
        )

    log.info("scheduler_built", jobs=[j.id for j in scheduler.get_jobs()])
    return scheduler


# ----------------- WebSocket stream trigger factory -----------------


def make_directional_trigger(
    *,
    main_loop: "asyncio.AbstractEventLoop",
    research_poster: Poster,
    signal_poster: Poster,
    trade_poster: Poster,
    log_poster: Poster | None = None,
) -> "DirectionalStreamTrigger":
    """Build a DirectionalStreamTrigger wired to the directional scan job.

    The trigger fires when Alpaca's real-time stream detects a volume surge
    or news drop on a watchlist ticker, then immediately calls the full
    directional scan (same logic as the 30-min fallback, but demand-driven).
    """
    from integrations.alpaca_stream import DirectionalStreamTrigger

    log_post = log_poster or _noop_poster
    watchlist = load_tickers()

    async def on_trigger(ticker: str, reason: str) -> None:
        log.info("stream_triggered_scan", ticker=ticker, reason=reason)
        await _directional_scan_job(
            signal_poster=signal_poster,
            trade_poster=trade_poster,
            research_poster=research_poster,
            log_poster=log_post,
            post_report_on_hold=False,  # silent if all HOLD — only post on BUY signals
        )

    return DirectionalStreamTrigger(
        main_loop=main_loop,
        on_trigger=on_trigger,
        watchlist=watchlist,
    )


# ----------------- manual runners (for `--once` and tests) -----------------


async def run_premarket_once(
    research_poster: Poster, *, log_poster: Poster | None = None
) -> None:
    """Trigger the pre-market job immediately."""
    await _premarket_job(
        research_poster=research_poster, log_poster=log_poster or _noop_poster
    )


async def run_intraday_once(
    signal_poster: Poster,
    *,
    log_poster: Poster | None = None,
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
) -> None:
    """Trigger the intraday-scan job immediately."""
    await _intraday_scan_job(
        signal_poster=signal_poster,
        log_poster=log_poster or _noop_poster,
        clock_fetcher=clock_fetcher,
    )


async def run_directional_once(
    signal_poster: Poster,
    trade_poster: Poster,
    research_poster: Poster,
    *,
    log_poster: Poster | None = None,
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
) -> None:
    """Trigger the directional-intraday scan + execute immediately."""
    await _directional_scan_job(
        signal_poster=signal_poster,
        trade_poster=trade_poster,
        research_poster=research_poster,
        log_poster=log_poster or _noop_poster,
        clock_fetcher=clock_fetcher,
    )


async def run_directional_exit_once(
    signal_poster: Poster,
    trade_poster: Poster,
    *,
    log_poster: Poster | None = None,
    force: bool = False,
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
) -> None:
    """Trigger the directional exit monitor immediately."""
    await _directional_exit_job(
        signal_poster=signal_poster,
        trade_poster=trade_poster,
        log_poster=log_poster or _noop_poster,
        force=force,
        clock_fetcher=clock_fetcher,
    )


async def run_iron_condor_once(
    signal_poster: Poster,
    trade_poster: Poster,
    *,
    log_poster: Poster | None = None,
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
) -> None:
    """Trigger the iron-condor entry job immediately."""
    await _iron_condor_entry_job(
        signal_poster=signal_poster,
        trade_poster=trade_poster,
        log_poster=log_poster or _noop_poster,
        clock_fetcher=clock_fetcher,
    )


async def run_exit_monitor_once(
    signal_poster: Poster,
    trade_poster: Poster,
    *,
    log_poster: Poster | None = None,
    force: bool = False,
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
) -> None:
    """Trigger the exit-monitor job immediately."""
    await _iron_condor_exit_job(
        signal_poster=signal_poster,
        trade_poster=trade_poster,
        log_poster=log_poster or _noop_poster,
        clock_fetcher=clock_fetcher,
        force=force,
    )
