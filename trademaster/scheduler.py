"""Schedules pre-market, intraday, and end-of-day events.

Equity events follow US market hours (RTH 9:30-16:00 ET).
Crypto events run 24/7 with their own cadence.

Phase 1.3 wires only the pre-market briefing (8am ET, Mon-Fri).
Intraday scans + EOD summary land in Phase 1.4 / Phase 2.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.research.premarket import run_premarket_briefing
from trademaster.logging import get_logger

log = get_logger(__name__)

PREMARKET_TZ = "America/New_York"


async def _premarket_job(poster: Callable[[str], Awaitable[None]]) -> None:
    """Runs the briefing and forwards the text to the Discord poster."""
    try:
        text, _signal = await run_premarket_briefing()
    except Exception as e:  # noqa: BLE001 — log + don't crash the scheduler
        log.error("premarket_job_failed", error=str(e), error_type=type(e).__name__)
        await poster(f"⚠️ Pre-market briefing failed: `{type(e).__name__}: {e}`")
        return
    await poster(text)


def make_scheduler(poster: Callable[[str], Awaitable[None]]) -> AsyncIOScheduler:
    """Build (don't start) an AsyncIOScheduler with the pre-market job registered.

    Caller is responsible for `scheduler.start()` from inside an asyncio loop.
    """
    scheduler = AsyncIOScheduler(timezone=PREMARKET_TZ)
    scheduler.add_job(
        _premarket_job,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone=PREMARKET_TZ),
        kwargs={"poster": poster},
        id="premarket_briefing",
        replace_existing=True,
        misfire_grace_time=900,  # 15 min grace if the process restarts near 8am
    )
    log.info("scheduler_built", jobs=[j.id for j in scheduler.get_jobs()])
    return scheduler


async def run_premarket_once(poster: Callable[[str], Awaitable[None]]) -> None:
    """Trigger the pre-market job immediately. Used for `python -m` smoke tests."""
    await _premarket_job(poster)
