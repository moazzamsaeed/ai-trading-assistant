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
from agents.options.exit_monitor import run_exit_monitor
from agents.options.strategist import run_iron_condor_strategist
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


# ----------------- iron-condor strategist -----------------


async def _iron_condor_entry_job(
    *,
    alert_poster: Callable[[str], Awaitable[None]],
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
) -> None:
    """Run the iron-condor strategist once. Posts to #alerts if approved."""
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
        _signal, alert_text = await run_iron_condor_strategist()
    except Exception as e:  # noqa: BLE001
        log.error(
            "iron_condor_strategist_failed",
            error=str(e),
            error_type=type(e).__name__,
        )
        await alert_poster(
            f"⚠️ Iron-condor strategist failed: `{type(e).__name__}: {e}`"
        )
        return

    if alert_text:
        await alert_poster(alert_text)


# ----------------- iron-condor exit monitor -----------------


async def _iron_condor_exit_job(
    *,
    alert_poster: Callable[[str], Awaitable[None]],
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
    force: bool = False,
) -> None:
    """Sweep open IC positions; close on PT, stop, or force=True."""
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
        await alert_poster(
            f"⚠️ Exit monitor failed: `{type(e).__name__}: {e}`"
        )
        return

    closed = [r for r in results if r.get("status") == "closed"]
    if closed:
        lines = ["**Iron-condor exits:**"]
        for r in closed:
            pnl = r.get("realized_pnl_per_contract", "?")
            lines.append(
                f"trade #{r['trade_id']} · reason: `{r['reason']}` · "
                f"debit ${r['exit_debit']} · P&L/contract ${pnl}"
            )
        await alert_poster("\n".join(lines))


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

    # Iron-condor entry: 9:45 ET, Mon-Fri (matches STRATEGIES.md 9:45-10:30
    # window — using the early edge of the window to leave time for fills).
    scheduler.add_job(
        _iron_condor_entry_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour=9,
            minute=45,
            timezone=PREMARKET_TZ,
        ),
        kwargs={"alert_poster": alert_poster},
        id="iron_condor_entry",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # Exit monitor: every 5 min during RTH, Mon-Fri (10:00-15:45).
    # Triggered slightly after entry so first run gives any 9:45 fill time
    # to populate.
    scheduler.add_job(
        _iron_condor_exit_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour="10-15",
            minute="0,5,10,15,20,25,30,35,40,45",
            timezone=PREMARKET_TZ,
        ),
        kwargs={"alert_poster": alert_poster},
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
        kwargs={"alert_poster": alert_poster, "force": True},
        id="iron_condor_force_close",
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


async def run_iron_condor_once(
    alert_poster: Callable[[str], Awaitable[None]],
    *,
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
) -> None:
    """Trigger the iron-condor entry job immediately."""
    await _iron_condor_entry_job(alert_poster=alert_poster, clock_fetcher=clock_fetcher)


async def run_exit_monitor_once(
    alert_poster: Callable[[str], Awaitable[None]],
    *,
    force: bool = False,
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
) -> None:
    """Trigger the exit-monitor job immediately."""
    await _iron_condor_exit_job(
        alert_poster=alert_poster, clock_fetcher=clock_fetcher, force=force
    )
