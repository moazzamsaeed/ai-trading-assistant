"""TradeMaster orchestrator entry point.

Phase 1.3 scope:
- Connect Discord bot
- Start the APScheduler with the 8am ET pre-market briefing job
- Stay alive until Ctrl-C

Phase 1.4 adds: risk-manager wiring, Discord slash commands, intraday
scan loop, EOD summary.
"""

from __future__ import annotations

import asyncio
import signal

from integrations.discord_bot import TradeMasterBot
from trademaster.config import get_settings
from trademaster.logging import configure_logging, get_logger
from trademaster.scheduler import make_scheduler, run_premarket_once

log = get_logger(__name__)


async def _run() -> None:
    configure_logging()
    settings = get_settings()
    settings.require_live_keys()

    async with TradeMasterBot() as poster:
        scheduler = make_scheduler(poster.post_research)
        scheduler.start()
        log.info("trademaster_started", trading_mode=settings.trading_mode)

        stop = asyncio.Event()

        def _on_signal() -> None:
            log.info("shutdown_signal_received")
            stop.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _on_signal)

        try:
            await stop.wait()
        finally:
            scheduler.shutdown(wait=False)
            log.info("trademaster_stopped")


async def _run_once() -> None:
    """Smoke test: post one pre-market briefing now and exit.

    Usage: `python -m trademaster.orchestrator --once`
    """
    configure_logging()
    get_settings().require_live_keys()
    async with TradeMasterBot() as poster:
        await run_premarket_once(poster.post_research)


def main() -> None:
    import sys

    if "--once" in sys.argv:
        asyncio.run(_run_once())
    else:
        asyncio.run(_run())


if __name__ == "__main__":
    main()
