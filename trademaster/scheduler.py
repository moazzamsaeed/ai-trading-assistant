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

from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.directional.intraday import run_directional_scan
from agents.intraday.scan import run_intraday_scan
from agents.options.exit_monitor import run_exit_monitor
from agents.options.strategist import run_iron_condor_strategist
from agents.research.premarket import run_premarket_briefing
from integrations import alpaca_client
from trademaster.logging import get_logger
from trademaster.state import get_state

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


# ----------------- directional intraday signals -----------------


async def _directional_scan_job(
    *,
    signal_poster: Poster,
    log_poster: Poster = _noop_poster,
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
) -> None:
    """Sweep watchlist every 10 min, post actionable BUY signals to #signals."""
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

    try:
        _decisions, messages = await run_directional_scan()
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

    for msg in messages:
        await signal_poster(msg)


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


# ----------------- scheduler builder -----------------


def make_scheduler(
    *,
    research_poster: Poster,
    signal_poster: Poster,
    trade_poster: Poster,
    log_poster: Poster | None = None,
) -> AsyncIOScheduler:
    """Build an AsyncIOScheduler with all standing jobs registered.

    Errors caught by individual jobs route to `log_poster` if provided
    (defaults to a no-op for tests that don't care).
    """
    log_post = log_poster or _noop_poster

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

    # Directional intraday signals — every 10 min during RTH, Mon-Fri.
    scheduler.add_job(
        _directional_scan_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute="0,10,20,30,40,50",
            timezone=PREMARKET_TZ,
        ),
        kwargs={"signal_poster": signal_poster, "log_poster": log_post},
        id="directional_scan",
        replace_existing=True,
        misfire_grace_time=120,
    )

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
    *,
    log_poster: Poster | None = None,
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
) -> None:
    """Trigger the directional-intraday scan immediately."""
    await _directional_scan_job(
        signal_poster=signal_poster,
        log_poster=log_poster or _noop_poster,
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
