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
from datetime import UTC, datetime, time as _time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.directional.executor import execute_directional_signal
from agents.directional.exit_monitor import run_directional_exit_monitor, run_trailing_stop_tick
from agents.directional.intraday import format_directional_signal, format_entry_combined, run_directional_scan
from agents.intraday.scan import run_intraday_scan
from agents.options.exit_monitor import run_exit_monitor
from agents.options.strategist import run_iron_condor_strategist
from agents.research.premarket import run_premarket_briefing
from decimal import Decimal

from integrations import alpaca_client
from trademaster.capital import directional_deployed_usd, get_effective_capital
from trademaster.config import get_settings
from trademaster.db import get_today_realized_pnl, get_this_week_realized_pnl, get_today_trade_count, get_today_trade_count_by_conviction, make_session_factory
from trademaster.event_calendar import is_blackout_day
from trademaster.logging import get_logger
from trademaster.state import get_state
from trademaster.timeutils import today_et, to_et
from trademaster.watchlist import load_tickers

# Prevents concurrent directional scans (stream + scheduler can both trigger).
_scan_in_progress: bool = False

# Throttle #research posts to at most once per hour across all scan triggers.
# Trade execution is unaffected — signals are still acted on immediately.
_last_research_post: datetime | None = None
_RESEARCH_POST_INTERVAL_SECONDS = 3600

# Per-ticker 15-min cooldown: short re-entry gap for SPY 0DTE where
# missing a 60-min window means missing the entire move.
_last_trade_open: dict[str, datetime] = {}
_TICKER_COOLDOWN_SECONDS = 900

# Per-(ticker, action) signal dedup: suppress Discord #signals spam when the
# same BUY_CALL/BUY_PUT fires repeatedly within 30 minutes.
_last_signal_posted: dict[tuple[str, str], datetime] = {}
_SIGNAL_DEDUP_SECONDS = 1800

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

    # Daily loss limit: halt if realized + unrealized P&L exceeds 15% of capital.
    # Capital tracks the actual account size (paper: base + cumulative realized;
    # live: Alpaca equity), so the limit shrinks with prior losses and grows
    # with gains automatically.
    settings = get_settings()
    capital = await get_effective_capital(make_session_factory())

    # Capital floor: with 0 capital there's nothing to deploy and dividing
    # by it for the limit gives 0, which would tautologically trip "loss <= 0".
    # Halt outright instead of erroring.
    if capital <= Decimal("0"):
        get_state().pause(hours=24)
        await log_poster(
            "🛑 Effective capital is $0 (cumulative losses exceed base). "
            "Trading halted until tomorrow."
        )
        log.warning("scan_skipped_capital_zero")
        return

    # ---- Daily loss limit ----
    limit_usd = capital * Decimal(str(settings.daily_loss_limit_pct))
    realized = get_today_realized_pnl(make_session_factory())
    unrealized = await alpaca_client.get_unrealized_pnl()
    total_pnl = realized + unrealized
    if total_pnl <= -limit_usd:
        get_state().pause(hours=24)
        pct = float(-total_pnl / capital * 100)
        await log_poster(
            f"🛑 Daily loss limit hit: **${float(total_pnl):.0f}** loss "
            f"({pct:.0f}% of ${float(capital):.0f} capital). "
            f"Trading halted until tomorrow. "
            f"Realized: ${float(realized):.0f} | Unrealized: ${float(unrealized):.0f}"
        )
        log.warning(
            "daily_loss_limit_hit",
            total_pnl=float(total_pnl),
            limit_usd=float(limit_usd),
            capital=float(capital),
            realized=float(realized),
            unrealized=float(unrealized),
        )
        return

    # ---- Weekly loss limit ----
    weekly_limit_usd = capital * Decimal(str(settings.weekly_loss_limit_pct))
    weekly_realized = get_this_week_realized_pnl(make_session_factory())
    weekly_total = weekly_realized + unrealized
    if weekly_total <= -weekly_limit_usd:
        days_until_monday = (7 - today_et().weekday()) % 7 or 7
        get_state().pause(hours=days_until_monday * 24)
        pct = float(-weekly_total / capital * 100)
        await log_poster(
            f"🛑 Weekly loss limit hit: **${float(weekly_total):.0f}** loss "
            f"({pct:.0f}% of ${float(capital):.0f} capital). "
            f"Trading halted until Monday."
        )
        log.warning(
            "weekly_loss_limit_hit",
            weekly_total=float(weekly_total),
            weekly_limit_usd=float(weekly_limit_usd),
            capital=float(capital),
        )
        return

    # ---- Tiered max trades per day ----
    conviction_counts = get_today_trade_count_by_conviction(make_session_factory())
    total_today = sum(conviction_counts.values())
    if total_today >= settings.max_trades_per_day:
        log.info("scan_skipped_max_trades_per_day", total=total_today, limit=settings.max_trades_per_day)
        return

    # ---- Event blackout calendar ----
    blackout_event = is_blackout_day(today_et())
    if blackout_event:
        log.info("scan_skipped_event_blackout", blackout=blackout_event)
        return

    # ---- Time of day filter ----
    et_now = to_et(datetime.now(UTC))
    h, m = settings.no_entry_before_et.split(":")
    no_entry_before = _time(int(h), int(m))
    h, m = settings.no_entry_after_et.split(":")
    no_entry_after = _time(int(h), int(m))
    if et_now.time() < no_entry_before or et_now.time() > no_entry_after:
        log.info("scan_skipped_time_filter", et_time=et_now.strftime("%H:%M"), window=f"{settings.no_entry_before_et}–{settings.no_entry_after_et}")
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

    # Auto-execute top 3 decisions by conviction. Built-in guards:
    # - 20% max total exposure cap (no new trades if too much deployed)
    # - 60-min per-ticker cooldown (no re-entry after a stop-loss)
    today = today_et()

    settings = get_settings()
    mode = settings.directional_mode
    # Reuse the capital value computed for the loss-limit check above — both
    # gates need a consistent view of capital, and avoiding a second fetch
    # also halves Alpaca round-trips in live mode.
    max_exposure = capital * Decimal(str(settings.max_total_exposure_pct))

    conviction_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    to_execute = sorted(
        [d for d in decisions if d.action != "HOLD"
         and d.conviction in ({"HIGH"} if mode == "selective" else {"MEDIUM", "HIGH"})],
        key=lambda d: (conviction_rank.get(d.conviction, 2), d.ticker),
    )[:3]

    global _last_trade_open, _last_signal_posted

    for decision in to_execute:
        # Tiered conviction cap: MEDIUM signals are limited to max_medium_trades_per_day.
        # Re-query each iteration since a previous trade in this loop may have incremented it.
        if decision.conviction == "MEDIUM":
            current_counts = get_today_trade_count_by_conviction(make_session_factory())
            if current_counts.get("MEDIUM", 0) >= settings.max_medium_trades_per_day:
                log.info(
                    "directional_execute_skipped_medium_cap",
                    ticker=decision.ticker,
                    medium_today=current_counts.get("MEDIUM", 0),
                    limit=settings.max_medium_trades_per_day,
                )
                continue

        # 60-min per-ticker cooldown
        last_open = _last_trade_open.get(decision.ticker)
        now_ts = datetime.now(UTC)
        if last_open and (now_ts - last_open).total_seconds() < _TICKER_COOLDOWN_SECONDS:
            log.info(
                "directional_execute_skipped_ticker_cooldown",
                ticker=decision.ticker,
                minutes_since_last=int((now_ts - last_open).total_seconds() / 60),
            )
            continue

        # Exposure cap: deploy the remaining budget (max_exposure - deployed).
        # No per-trade fraction — the full remaining budget is the position size.
        factory = make_session_factory()
        with factory() as session:
            deployed = directional_deployed_usd(session)
        available = max_exposure - deployed
        if available <= Decimal("0"):
            log.info(
                "directional_execute_skipped_exposure_cap",
                deployed=float(deployed),
                cap=float(max_exposure),
            )
            continue

        try:
            result = await execute_directional_signal(decision, mode=mode, capital_usd=available)
            if result.executed and result.trade_id is not None:
                _last_trade_open[decision.ticker] = datetime.now(UTC)
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
                # Post to #signals for manual entry — deduplicated at 30 min per (ticker, action)
                sig_key = (decision.ticker, decision.action)
                last_sig = _last_signal_posted.get(sig_key)
                if not last_sig or (datetime.now(UTC) - last_sig).total_seconds() >= _SIGNAL_DEDUP_SECONDS:
                    manual = format_directional_signal(decision, today=today, mode=mode)
                    manual += f"\n⚠️ _Auto-execute skipped: {result.reason}_"
                    await signal_poster(manual)
                    _last_signal_posted[sig_key] = datetime.now(UTC)
                else:
                    log.info("signal_dedup_suppressed", ticker=decision.ticker, action=decision.action)
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
    log_poster: Poster = _noop_poster,
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
    force: bool = False,
) -> None:
    """Check open directional positions; post exits to #signals."""
    # Do NOT skip when paused — pausing blocks new entries, not exit monitoring.
    # Open positions still need stop-loss and force-close protection.

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


async def _trailing_stop_tick_job(
    *,
    signal_poster: Poster,
    log_poster: Poster = _noop_poster,
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
) -> None:
    """Fast 30-sec trailing stop tick — peak update, scale-out, hard stop only.

    Lightweight version of the exit monitor that runs every 30 seconds during
    RTH. No indicators, no LLM. Just price-based trailing stop ratchet and
    partial scale-out at each profit tier. Skips silently when market is closed
    or there are no open positions. All network calls have hard timeouts to
    prevent any single tick from hanging and blocking subsequent firings.
    """
    try:
        clock = await asyncio.wait_for(clock_fetcher(), timeout=5.0)
    except Exception:  # noqa: BLE001
        return  # silent fail (timeout or transient) — next tick will retry
    if not clock.is_open:
        return

    try:
        results = await asyncio.wait_for(run_trailing_stop_tick(), timeout=20.0)
    except Exception as e:  # noqa: BLE001
        log.warning("trailing_stop_tick_failed", error=str(e))
        return

    for r in results:
        if r.get("status") == "scaled_out":
            tier = r.get("tier", 0)
            sell_qty = r.get("sell_qty", 0)
            pnl = r.get("partial_pnl_usd", "?")
            await signal_poster(
                f"💰 **Scaled out {sell_qty} contracts at +{tier:.0f}% tier** · "
                f"partial P&L: ${pnl} · {r.get('remaining_qty', 0)} contracts remaining"
            )
        elif r.get("combined_text"):
            await signal_poster(r["combined_text"])


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

    # Directional fallback scan — every 15 min during RTH.
    # Real-time triggers come from the WebSocket stream (alpaca_stream.py).
    # This fallback catches slow-building setups and guards against stream gaps.
    # SPY 0DTE timing is critical — 15 min ensures no setup is missed between surges.
    scheduler.add_job(
        _directional_scan_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute="0,15,30,45",
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
            "log_poster": log_post,
        },
        id="directional_exit",
        replace_existing=True,
        misfire_grace_time=120,
    )

    # Safety net: re-run at 15:50 ET so any 0DTE position that failed to close
    # at 15:45 gets one final attempt. force=False — market is still open at
    # 15:50 so clock check passes, and per-trade expiry==today guard still
    # applies so weekly positions are never touched.
    scheduler.add_job(
        _directional_exit_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour=15,
            minute=50,
            timezone=PREMARKET_TZ,
        ),
        kwargs={
            "signal_poster": signal_poster,
            "log_poster": log_post,
        },
        id="directional_0dte_final_close",
        replace_existing=True,
        misfire_grace_time=120,
    )

    # Fast trailing stop tick — every 30 sec during RTH for 0DTE responsiveness.
    # Lightweight: no indicators, no LLM. Just peak update, scale-out, hard stop.
    scheduler.add_job(
        _trailing_stop_tick_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute="*",
            second="0,30",
            timezone=PREMARKET_TZ,
        ),
        kwargs={
            "signal_poster": signal_poster,
            "log_poster": log_post,
        },
        id="trailing_stop_tick",
        replace_existing=True,
        misfire_grace_time=15,
        max_instances=2,  # tolerate a slow tick without blocking the next
        coalesce=True,    # collapse missed firings on restart
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
    *,
    log_poster: Poster | None = None,
    force: bool = False,
    clock_fetcher: ClockFetcher = alpaca_client.get_market_clock,
) -> None:
    """Trigger the directional exit monitor immediately."""
    await _directional_exit_job(
        signal_poster=signal_poster,
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
