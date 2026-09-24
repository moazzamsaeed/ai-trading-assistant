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
from pathlib import Path

from integrations.discord_bot import TradeMasterBot
from trademaster.config import get_settings
from trademaster.logging import configure_logging, get_logger
from trademaster.reconciler import reconcile_positions
from trademaster.risk_manager import validate_account_is_cash
from trademaster.scheduler import (
    make_directional_trigger,
    make_scheduler,
    run_directional_once,
    run_intraday_once,
    run_iron_condor_once,
    run_premarket_once,
)
from trademaster.timeutils import now_et, to_et

log = get_logger(__name__)


def _git_revision() -> str:
    """Short SHA of the deployed tree, or '?' if git isn't available."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip() or "?"
    except Exception:  # pragma: no cover - diagnostics only
        return "?"


def build_startup_heartbeat(settings, scheduler=None, now=None) -> str:
    """The '#logs' message proving which stack actually booted.

    Every line is config that has silently diverged from intent before, so the
    heartbeat is the at-a-glance diff against what you *think* is deployed.
    """
    stamp = to_et(now) if now is not None else now_et()

    def _flag(on: bool) -> str:
        return "✅" if on else "❌"

    mode = settings.trading_mode.upper()
    icon = "🔴" if settings.trading_mode == "live" else "🟢"

    lines = [
        f"{icon} **TradeMaster started** — `{mode}` · {stamp:%Y-%m-%d %H:%M ET}"
        f" · commit `{_git_revision()}`",
        f"**Account** {settings.account_type} · capital "
        f"${settings.trading_capital_usd:,.0f} · condor {settings.condor_contracts}ct",
        f"**Condor** dist-aware stop {_flag(settings.condor_distance_aware_stop)}"
        f" · assignment close {_flag(settings.condor_assignment_close)}"
        f" · single-leg stop {_flag(settings.condor_stop_single_leg)}"
        f" · event blackout {_flag(settings.enable_event_blackout)}",
        f"**Feed** options={settings.alpaca_options_feed}"
        f" · **Directional** "
        + (
            "signals-only"
            if settings.directional_signals_only
            else ("trading" if settings.enable_directional else "off")
        ),
    ]

    if scheduler is not None:
        job = scheduler.get_job("iron_condor_entry")
        nxt = getattr(job, "next_run_time", None) if job else None
        lines.append(
            f"**Next condor entry** {to_et(nxt):%a %Y-%m-%d %H:%M ET}"
            if nxt
            else "**Next condor entry** ⚠️ not scheduled"
        )

    return "\n".join(lines)


async def _run() -> None:
    configure_logging()
    settings = get_settings()
    settings.require_live_keys()

    # D-001: refuse to start if the live account isn't cash.
    await validate_account_is_cash()

    async with TradeMasterBot() as bot:
        # Reconcile DB open trades against live Alpaca positions before starting
        # the scheduler. Repairs any mismatch from a crash or manual close.
        recon_warnings = await reconcile_positions()
        for w in recon_warnings:
            await bot.post_log(w)
        scheduler = make_scheduler(
            research_poster=bot.post_research,
            signal_poster=bot.post_signal,
            trade_poster=bot.post_trade,
            log_poster=bot.post_log,
            stock_signal_poster=bot.post_stock_signal,
        )
        scheduler.start()

        loop = asyncio.get_running_loop()

        # Real-time directional entry trigger — suppressed in condor-only mode so
        # no new directional positions open (the 15-min scan job is gated the same
        # way in make_scheduler). Directional exits still run via the scheduler.
        stream = None
        if settings.enable_directional:
            # Condor-alerts-only: the directional stream still RUNS (real-time
            # triggers/booking unaffected) but its Discord posts route to no-op so
            # only iron-condor alerts reach Discord. Errors still go to #logs.
            async def _noop(_text: str) -> None:
                return None

            stream = make_directional_trigger(
                main_loop=loop,
                signal_poster=_noop if settings.condor_alerts_only else bot.post_signal,
                trade_poster=_noop if settings.condor_alerts_only else bot.post_trade,
                log_poster=bot.post_log,
            )
            stream.start()
        else:
            log.info("directional_engine_disabled")

        log.info("trademaster_started", trading_mode=settings.trading_mode)

        # Startup heartbeat — journal only, NOT Discord. A good boot is the
        # expected case and posting it every morning is noise; the watchdog
        # (scripts/daemon_watchdog.py) is what speaks, and only on failure.
        # Kept here because it records which config actually loaded, which is
        # the thing you want in the journal when reconstructing a bad day.
        try:
            log.info(
                "startup_heartbeat",
                heartbeat=build_startup_heartbeat(settings, scheduler),
            )
        except Exception as exc:  # pragma: no cover - diagnostics only
            log.warning("startup_heartbeat_failed", error=str(exc))

        stop = asyncio.Event()

        def _on_signal() -> None:
            log.info("shutdown_signal_received")
            stop.set()

        for sig in (_signal.SIGTERM, _signal.SIGINT):
            loop.add_signal_handler(sig, _on_signal)

        try:
            await stop.wait()
        finally:
            if stream is not None:
                stream.stop()
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


async def _run_directional_once() -> None:
    configure_logging()
    get_settings().require_live_keys()
    async with TradeMasterBot() as bot:
        await run_directional_once(bot.post_signal, bot.post_trade, log_poster=bot.post_log)


def main() -> None:
    import sys

    if "--once" in sys.argv:
        asyncio.run(_run_premarket_once())
    elif "--scan-once" in sys.argv:
        asyncio.run(_run_scan_once())
    elif "--ic-once" in sys.argv:
        asyncio.run(_run_iron_condor_once())
    elif "--dir-once" in sys.argv:
        asyncio.run(_run_directional_once())
    else:
        asyncio.run(_run())


if __name__ == "__main__":
    main()
