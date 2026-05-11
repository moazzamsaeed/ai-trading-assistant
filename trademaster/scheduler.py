"""Schedules pre-market, intraday, and end-of-day events.

Equity events follow US market hours (RTH 9:30-16:00 ET).
Crypto events run 24/7 with their own cadence.

Phase 1.3 wired the pre-market briefing (8am ET, Mon-Fri).
Phase 1.4c adds the intraday scan loop (every 15 min during RTH).
EOD summary and crypto ticks land in Phase 2/3.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.intraday.scan import run_intraday_scan
from agents.research.premarket import run_premarket_briefing
from integrations import alpaca_client
from trademaster.logging import get_logger
from trademaster.state import get_state

log = get_logger(__name__)

PREMARKET_TZ = "America/New_York"


# Injectable for tests.
ClockFetcher = Callable[[], Awaitable[alpaca_client.MarketClock]]


# ----------------- premarket -----------------


async def _premarket_job(poster: Callable[[str], Awaitable[None]]) -> None:
    """Runs the briefing and forwards the text to the Discord poster."""
    try:
        text, _signal = await run_premarket_briefing()
    except Exception as e:  # noqa: BLE001
        log.error("premarket_job_failed", error=str(e), error_type=type(e).__name__)
        await poster(f"⚠️ Pre-market briefing failed: `{type(e).__name__}: {e}`")
        return
    await poster(text)


# ----------------- intraday scan -----------------


async def _intraday_scan_job(
    *,
    alert_poster: Callable[[str], Awaitable[None]],
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
) -> None:
    """Skip if paused or market closed; otherwise run a scan and post alerts."""
    state = get_state()
    if state.is_paused():
        log.info("intraday_scan_skipped_paused", paused_until=str(state.paused_until))
        return

    try:
        clock = await clock_fetcher()
    except Exception as e:  # noqa: BLE001
        log.warning("intraday_scan_clock_failed", error=str(e))
        return  # don't post; transient

    if not clock.is_open:
        log.info("intraday_scan_skipped_closed", next_open=str(clock.next_open))
        return

    try:
        _signal, alert_text = await run_intraday_scan()
    except Exception as e:  # noqa: BLE001
        log.error("intraday_scan_failed", error=str(e), error_type=type(e).__name__)
        await alert_poster(f"⚠️ Intraday scan failed: `{type(e).__name__}: {e}`")
        return

    if alert_text:
        await alert_poster(alert_text)


# ----------------- scheduler builder -----------------


def make_scheduler(
    research_poster: Callable[[str], Awaitable[None]],
    alert_poster: Callable[[str], Awaitable[None]] | None = None,
) -> AsyncIOScheduler:
    """Build (don't start) an AsyncIOScheduler with both jobs registered.

    `alert_poster` is optional for backward compat — if omitted, intraday
    alerts route to `research_poster`.
    """
    if alert_poster is None:
        alert_poster = research_poster

    scheduler = AsyncIOScheduler(timezone=PREMARKET_TZ)

    scheduler.add_job(
        _premarket_job,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone=PREMARKET_TZ),
        kwargs={"poster": research_poster},
        id="premarket_briefing",
        replace_existing=True,
        misfire_grace_time=900,
    )

    # RTH is 9:30-16:00 ET. The cron fires every 15 min from 9:30 to 15:45.
    # Market-hours and holidays are double-checked inside the job via the
    # Alpaca clock so this still does the right thing on early-close days.
    scheduler.add_job(
        _intraday_scan_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute="0,15,30,45",
            timezone=PREMARKET_TZ,
        ),
        kwargs={"alert_poster": alert_poster},
        id="intraday_scan",
        replace_existing=True,
        misfire_grace_time=120,
    )

    log.info("scheduler_built", jobs=[j.id for j in scheduler.get_jobs()])
    return scheduler


# ----------------- manual runners (for `--once` and tests) -----------------


async def run_premarket_once(poster: Callable[[str], Awaitable[None]]) -> None:
    """Trigger the pre-market job immediately."""
    await _premarket_job(poster)


async def run_intraday_once(
    alert_poster: Callable[[str], Awaitable[None]],
    *,
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
) -> None:
    """Trigger the intraday-scan job immediately."""
    await _intraday_scan_job(alert_poster=alert_poster, clock_fetcher=clock_fetcher)
