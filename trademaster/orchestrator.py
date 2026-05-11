"""TradeMaster orchestrator entry point.

Wires the Discord bot and scheduler, and enforces the cash-only account
check at startup. Runs until SIGTERM/SIGINT.

CLI:
  python -m trademaster.orchestrator             # full daemon
  python -m trademaster.orchestrator --once      # one pre-market briefing
  python -m trademaster.orchestrator --scan-once # one intraday scan
  python -m trademaster.orchestrator --ic-once   # one iron-condor strategist run

Channel routing (see RUNBOOK):
  #research → daily briefing
  #signals  → broker-ready manual alerts
  #trades   → automated bot trading activity
  #logs     → scheduler errors / diagnostics
"""

from __future__ import annotations

import asyncio
import signal as _signal

from integrations.discord_bot import TradeMasterBot
from trademaster.config import get_settings
from trademaster.logging import configure_logging, get_logger
from trademaster.risk_manager import validate_account_is_cash
from trademaster.scheduler import (
    make_scheduler,
    run_intraday_once,
    run_iron_condor_once,
    run_premarket_once,
)

log = get_logger(__name__)


async def _run() -> None:
    configure_logging()
    settings = get_settings()
    settings.require_live_keys()

    # D-001: refuse to start if the live account isn't cash.
    await validate_account_is_cash()

    async with TradeMasterBot() as bot:
        scheduler = make_scheduler(
            research_poster=bot.post_research,
            signal_poster=bot.post_signal,
            trade_poster=bot.post_trade,
            log_poster=bot.post_log,
        )
        scheduler.start()
        log.info("trademaster_started", trading_mode=settings.trading_mode)

        stop = asyncio.Event()

        def _on_signal() -> None:
            log.info("shutdown_signal_received")
            stop.set()

        loop = asyncio.get_running_loop()
        for sig in (_signal.SIGTERM, _signal.SIGINT):
            loop.add_signal_handler(sig, _on_signal)

        try:
            await stop.wait()
        finally:
            scheduler.shutdown(wait=False)
            log.info("trademaster_stopped")


async def _run_premarket_once() -> None:
    configure_logging()
    get_settings().require_live_keys()
    async with TradeMasterBot() as bot:
        await run_premarket_once(bot.post_research, log_poster=bot.post_log)


async def _run_scan_once() -> None:
    configure_logging()
    get_settings().require_live_keys()
    async with TradeMasterBot() as bot:
        await run_intraday_once(bot.post_signal, log_poster=bot.post_log)


async def _run_iron_condor_once() -> None:
    configure_logging()
    get_settings().require_live_keys()
    async with TradeMasterBot() as bot:
        await run_iron_condor_once(
            bot.post_signal, bot.post_trade, log_poster=bot.post_log
        )


def main() -> None:
    import sys

    if "--once" in sys.argv:
        asyncio.run(_run_premarket_once())
    elif "--scan-once" in sys.argv:
        asyncio.run(_run_scan_once())
    elif "--ic-once" in sys.argv:
        asyncio.run(_run_iron_condor_once())
    else:
        asyncio.run(_run())


if __name__ == "__main__":
    main()
