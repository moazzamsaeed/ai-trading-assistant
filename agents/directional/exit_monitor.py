"""Directional options exit monitor — indicator-driven intelligent exit.

Runs every 5 min during RTH. For each open directional Trade row:

  1. Hard floor (mode-aware): exit immediately, no LLM.
       aggressive: −50% | selective: −30%
  2. Force close on expiry day at 15:45 ET (0DTE every day, weekly on Friday).
  3. Trailing stop + scale-out: at each tier, ratchet stop AND sell a portion.
       +15%→sell 25%, lock +3%   |  +30%→sell 25%, lock +10%
       +50%→sell 25%, lock +25%  |  +75%→hold, lock +40%   |  +100%→lock +60%
       Sell fractions sum to 75% — last 25% rides with the highest stop.
       Stop only ever moves UP. Persisted to DB — survives restarts.
       Fast 30-sec tick (run_trailing_stop_tick) handles peak/scale-out
       between 5-min full sweeps for 0DTE responsiveness.
  4. Stop premium: hard stop set at entry (−50% aggressive / −30% selective).
  5. Indicator scan + LLM confirmation — two triggers:
       a. Any reversal rule fires (thesis protection, loss or gain)
       b. P&L ≥ PROFIT_LOCK_PCT (75%): always consult LLM even if no rules fired
     BUY_CALL rules: price < VWAP, RSI > 75, EMA20 < EMA50, volume fading
     BUY_PUT rules:  price > VWAP, RSI < 25, EMA20 > EMA50, volume fading
  6. LLM decides EXIT or HOLD with full context: P&L, indicators, mode, expiry.
     - No hard profit target — LLM + indicators govern when to take profit.
     - Selective mode: one fading indicator in profit zone is enough to exit.
     - Aggressive mode: require ≥2 confirming signals before exiting a winner.

On a close fill:
  - updates the Trade row
  - returns combined_text: single Discord message for #signals (manual close
    instruction + P&L summary in one)
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from integrations import alpaca_client
from integrations.alpaca_client import (
    OrderResult,
    get_recent_bars,
    get_single_option_quote,
    parse_occ_symbol,
)
from trademaster import indicators
from trademaster.config import get_settings
from trademaster.db import Trade, make_session_factory
from trademaster.logging import get_logger
from trademaster.router import TaskType, route_to_model
from trademaster.timeutils import to_et

log = get_logger(__name__)

FORCE_CLOSE_AFTER = time(15, 45)   # 15 min before the 16:00 ET close (was 15:30)
DIRECTIONAL_STRATEGIES = {"directional_call", "directional_put"}
HARD_FLOOR_PCT = Decimal("0.30")   # −30%: exit unconditionally, no LLM
VOLUME_FADE_THRESHOLD = 0.7        # volume_ratio below this = momentum fading
PROFIT_LOCK_PCT = 75.0             # always consult LLM above this P&L even with no indicator triggers

# Thesis-invalidation force-exit (fix A, 2026-06-09). When a LOSING position
# shows a confirmed momentum reversal, exit immediately WITHOUT asking the LLM —
# the LLM kept holding broken losers (trade #60 held −15%→−25% citing "stop not
# hit / theta negligible"). RSI-9 flipping against the position is the key tell.
RSI_REVERSAL_CALL = 45.0           # RSI-9 falling below this against a call = bullish momentum lost
RSI_REVERSAL_PUT = 55.0            # RSI-9 rising above this against a put = bearish momentum lost
THESIS_INVALIDATION_MIN_SIGNALS = 2  # reversal signals needed to force-exit a loser
# Rules (from _check_exit_rules) that count as the thesis breaking, per direction.
_INVALIDATION_RULES = {
    "BUY_CALL": {"price_below_vwap", "rsi_reversal_bearish", "ema_bearish_cross", "volume_fading"},
    "BUY_PUT": {"price_above_vwap", "rsi_reversal_bullish", "ema_bullish_cross", "volume_fading"},
}

# Trailing stop + scale-out tiers: (trigger_pct, lock_in_pct, sell_fraction)
# Walked from highest to lowest. At each crossed tier:
#   - Ratchet stop on REMAINING position to lock_in_pct
#   - Sell sell_fraction of ORIGINAL qty to lock that portion in
#
# Retuned 2026-06-05 (v2): "ride, then scale once." Sells 25% once at +100%
# (cheap insurance: 0DTE gamma makes +100%+ positions reversal-prone); the
# remaining 75% rides. The discrete locks below are now just FLOORS — since
# 2026-06-08 (v3) the trailing stop trails CONTINUOUSLY at (peak − gap, default
# 10%) across the whole in-profit range (see _trailing_stop_premium), so the
# stop always sits within ~10% of the high-water mark. v2 left the stop on the
# discrete tier (a +70% peak locked only +20% → trade #51 gave back ~$1,200).
# Tracked via the health-check peak-vs-realized metric.
# Override the ladder via settings.trailing_stop_levels, the gap via
# settings.trailing_stop_trail_gap_pct.
DEFAULT_TRAILING_STOP_LEVELS: list[tuple[float, float, float]] = [
    (100.0, 0.60, 0.25),  # +100% → SELL 25% (the one scale-out); above here the
                          #          stop trails continuously (peak − gap)
    (80.0,  0.45, 0.00),  # +80%  → lock +45%, ride (no sell)
    (50.0,  0.20, 0.00),  # +50%  → lock +20%, ride (no sell)
    (25.0,  0.08, 0.00),  # +25%  → lock +8%,  ride (no sell)
]
# Backward-compat alias — tests and the trade_health_check mirror reference this.
TRAILING_STOP_LEVELS = DEFAULT_TRAILING_STOP_LEVELS


def _trailing_stop_levels() -> list[tuple[float, float, float]]:
    """The active scale-out / trailing-stop ladder, sorted high→low.

    Reads settings.trailing_stop_levels (a JSON array of
    [trigger_pct, lock_pct, sell_frac]) when set, so the ladder can be A/B'd
    without a code change; falls back to DEFAULT_TRAILING_STOP_LEVELS on empty
    or invalid config (logged)."""
    raw = (get_settings().trailing_stop_levels or "").strip()
    if not raw:
        return DEFAULT_TRAILING_STOP_LEVELS
    try:
        parsed = json.loads(raw)
        levels = [(float(t), float(lk), float(s)) for t, lk, s in parsed]
        if not levels:
            raise ValueError("empty levels")
        return sorted(levels, key=lambda x: x[0], reverse=True)
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        log.warning("trailing_stop_levels_invalid", error=str(e), raw=raw[:120])
        return DEFAULT_TRAILING_STOP_LEVELS


def scale_out_plan_summary() -> str:
    """Plain-language scale-out description for #signals, derived from the active
    ladder so the signal text never drifts from the real config."""
    sell_tiers = sorted((t for t in _trailing_stop_levels() if t[2] > 0), key=lambda x: x[0])
    if not sell_tiers:
        return "ride the full position with a trailing stop"
    fracs = {int(round(s * 100)) for _, _, s in sell_tiers}
    frac_txt = f"{next(iter(fracs))}%" if len(fracs) == 1 else "a portion"
    tiers_txt = ", ".join(f"+{int(t)}%" for t, _, _ in sell_tiers)
    return f"scale out {frac_txt} at {tiers_txt} gain"

_EXIT_CONFIRM_PROMPT = """You manage an open options position. Decide EXIT or HOLD.

Position:
  Ticker: {ticker} | Action: {action} | Mode: {mode}
  Entry: ${entry_premium}/share | Current bid: ${current_bid}/share
  P&L: {pnl_sign}{pnl_pct}% | Held: {mins_held} min | Expiry: {expiry}
  Reference targets: PT ≥${profit_target}/share · Stop ≤${stop_ref}/share

{ticker} indicators right now:
  Price: ${price} | VWAP: {vwap}
  RSI(9): {rsi} | EMA20: {ema20} | EMA50: {ema50}
  Volume ratio: {vol_ratio}

Rules triggered: {rules}

Original entry reasoning: "{entry_reasoning}"

--- EXIT FRAMEWORK ---

LOSS SIDE — exit if thesis is broken:
  • Price broke VWAP against position direction
  • EMA cross confirmed reversal
  • RSI flipped extreme opposite direction + volume fading

PROFIT SIDE — no hard target, use indicator confluence:
  • P&L 30–74%: exit if ≥2 indicators show fading momentum
  • P&L ≥75%: {profit_lock_rule}
  • P&L ≥150%: EXIT unless ALL indicators unanimously confirm continuation (rare — capture the gain)
  • Volume fade alone while in profit = smart money distributing into your position → EXIT
  • RSI exhaustion + volume fade = momentum spent → EXIT

MODE CONTEXT — {mode_guidance}

TIME CONTEXT — factor in expiry. Theta accelerates in final 90 min.
  If expiry is today and P&L is positive, lean EXIT over HOLD.

HOLD only if: momentum is intact across multiple indicators, thesis unchanged, and remaining upside clearly justifies the risk of giving back current gains.

Respond with JSON only: {{"decision": "EXIT"|"HOLD", "reason": "one brief sentence"}}"""


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _open_directional_trades(session: Session) -> list[Trade]:
    stmt = select(Trade).where(
        Trade.strategy.in_(DIRECTIONAL_STRATEGIES),
        Trade.closed_at.is_(None),
    )
    return list(session.execute(stmt).scalars())


def _close_trade_row(
    session: Session,
    trade: Trade,
    *,
    exit_premium: Decimal,
    order: OrderResult | None,
    reason: str,
    llm_reasoning: str = "",
) -> bool:
    """Close a trade row. Idempotent: if the row is already closed (a concurrent
    exit job got there first), do nothing and return False.

    The 30-sec tick and the 5-min monitor can both decide to exit the same trade
    within the same second. Without this guard the loser of that race overwrites
    the winner's real close — e.g. trade #57: the tick filled the hard-floor sell
    at $1.35 (a real −50% loss) and 40 ms later the monitor's redundant sell was
    rejected ("position not in broker") and re-marked it as a phantom, hiding the
    real loss. First close wins.
    """
    if trade.closed_at is not None:
        log.info(
            "directional_close_skipped_already_closed",
            trade_id=trade.id,
            existing_reason=(trade.extra or {}).get("exit_reason"),
            attempted_reason=reason,
        )
        return False
    qty = Decimal(str(trade.qty))
    entry_p = Decimal(str(trade.entry_price))
    trade.exit_price = exit_premium
    trade.realized_pnl_usd = (exit_premium - entry_p) * 100 * qty
    trade.closed_at = datetime.now(UTC)
    extra = dict(trade.extra or {})
    extra["exit_reason"] = reason
    extra["exit_reasoning"] = llm_reasoning
    extra["close_order_id"] = order.order_id if order else None
    extra["close_status"] = order.status if order else "broker_error"
    trade.extra = extra
    session.commit()
    return True


# ---------------------------------------------------------------------------
# Indicator-based exit rules
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Trailing stop
# ---------------------------------------------------------------------------


def _trailing_stop_premium(
    entry_premium: Decimal,
    peak_pnl_pct: float,
) -> Decimal | None:
    """Return the stop price that locks in gains at the current peak level.

    Once the peak clears the lowest ladder tier, the stop trails CONTINUOUSLY at
    (peak − trailing_stop_trail_gap_pct) the whole way up — so it always sits
    within `gap` of the high-water mark (e.g. gap=0.10 → +70% peak locks +60%,
    +200% locks +190%). The discrete tier locks act only as floors. This stops
    mid-range runners from giving back to a far-below-peak discrete lock (trade
    #51 peaked +70% but the old discrete ladder locked just +20%). Returns None
    if peak is below the lowest trigger (give the trade room early).
    """
    levels = _trailing_stop_levels()  # high → low
    if not levels:
        return None

    lowest_trigger = levels[-1][0]
    if peak_pnl_pct < lowest_trigger:
        return None

    # Highest discrete lock the peak has earned (a floor under the trail).
    discrete_lock = 0.0
    for trigger_pct, lock_pct, _sell_frac in levels:  # high → low
        if peak_pnl_pct >= trigger_pct:
            discrete_lock = lock_pct
            break

    gap = float(get_settings().trailing_stop_trail_gap_pct)
    lock_pct = max(discrete_lock, peak_pnl_pct / 100.0 - gap)
    return (entry_premium * (Decimal("1") + Decimal(str(lock_pct)))).quantize(
        Decimal("0.0001")
    )


def _maybe_ratchet_trailing_stop(
    session_factory,
    trade: Trade,
    current_pnl_pct: float,
    entry_premium: Decimal,
) -> Decimal | None:
    """Update peak P&L and ratchet the trailing stop if a new high-water mark
    has been set. Returns the new stop_premium if the stop was ratcheted, else
    None. The stop only ever moves UP — it never loosens.

    Persists both peak_pnl_pct and stop_premium to the DB so the ratcheted
    stop survives a daemon restart.
    """
    extra = trade.extra or {}
    old_peak = float(extra.get("peak_pnl_pct", 0.0))
    new_peak = max(old_peak, current_pnl_pct)

    new_trail = _trailing_stop_premium(entry_premium, new_peak)

    current_stop_raw = extra.get("stop_premium")
    current_stop = Decimal(str(current_stop_raw)) if current_stop_raw else Decimal("-1")

    peak_moved = new_peak > old_peak
    stop_ratcheted = new_trail is not None and new_trail > current_stop

    if not peak_moved and not stop_ratcheted:
        return None

    with session_factory() as session:
        row = session.get(Trade, trade.id)
        if row is None:
            return None
        new_extra = dict(row.extra or {})
        new_extra["peak_pnl_pct"] = round(new_peak, 2)
        if stop_ratcheted:
            new_extra["stop_premium"] = str(new_trail)
            new_extra["trailing_stop_active"] = True
            log.info(
                "trailing_stop_ratcheted",
                trade_id=trade.id,
                peak_pnl_pct=f"{new_peak:.1f}%",
                old_stop=str(current_stop),
                new_stop=str(new_trail),
                entry_premium=str(entry_premium),
            )
        row.extra = new_extra
        session.commit()

    # Refresh in-memory trade so callers (scale-out) see the updated peak
    trade.extra = new_extra

    return new_trail if stop_ratcheted else None


# Per-trade locks serialize scale-out (and any other) management of a single
# trade across the concurrent 30-sec trailing tick and 5-min exit monitor.
# Both fire on :00-second 5-min boundaries (e.g. 14:45:00) and, without this,
# both passed the "tier already fired?" check before either persisted —
# double-firing the same tier and over-selling (trade #43, 2026-06-02).
# Keyed by trade id; a handful of stale entries over the daemon's life is fine.
_scale_out_locks: dict[int, asyncio.Lock] = {}


def _scale_out_lock(trade_id: int) -> asyncio.Lock:
    lock = _scale_out_locks.get(trade_id)
    if lock is None:
        lock = asyncio.Lock()
        _scale_out_locks[trade_id] = lock
    return lock


async def _maybe_scale_out(
    session_factory,
    trade: Trade,
    current_bid: Decimal,
    submitter,
    waiter,
) -> dict | None:
    """If peak P&L has crossed a new scale-out tier, partial-close that portion.

    Walks TRAILING_STOP_LEVELS from lowest to highest. For each tier with a
    non-zero sell_fraction not yet fired: sells sell_fraction × original_qty,
    updates trade.qty and extra. Returns dict on successful partial close, else None.

    Only fires once per tier per trade. The whole check→submit→persist runs
    under a per-trade lock so the 30-sec tick and 5-min monitor can't both fire
    the same tier; state (fired tiers, qty) is read fresh from the DB inside the
    lock rather than trusting the possibly-stale passed-in trade object.
    """
    async with _scale_out_lock(trade.id):
        # Read fresh state inside the lock — the passed-in `trade` may be stale
        # if a concurrent call just scaled out.
        with session_factory() as session:
            row = session.get(Trade, trade.id)
            if row is None:
                return None
            extra = dict(row.extra or {})
            entry_p = Decimal(str(row.entry_price))
            current_qty = int(row.qty)
            occ = extra.get("occ_symbol", row.symbol)
        peak = float(extra.get("peak_pnl_pct", 0.0))
        fired = set(extra.get("scale_out_tiers_fired", []))
        original_qty = int(extra.get("original_qty", current_qty))

        # Find the lowest unfired tier the peak has crossed (process lowest first)
        new_tier = None
        for trigger, _lock, sell_frac in sorted(_trailing_stop_levels(), key=lambda t: t[0]):
            if peak >= trigger and trigger not in fired and sell_frac > 0:
                new_tier = (trigger, sell_frac)
                break

        if new_tier is None:
            return None

        trigger_pct, sell_frac = new_tier
        sell_qty = min(max(1, int(original_qty * sell_frac)), current_qty)
        if sell_qty <= 0:
            return None

        try:
            order = await submitter(qty=sell_qty, occ_symbol=occ, limit_price=current_bid)
            final = await waiter(order.order_id, timeout_s=30.0)
        except Exception as e:  # noqa: BLE001
            log.warning("scale_out_failed", trade_id=trade.id, tier=trigger_pct, error=str(e))
            return None

        if final.status != "filled":
            log.warning("scale_out_not_filled", trade_id=trade.id, tier=trigger_pct, status=final.status)
            return None

        exit_price = final.filled_avg_price if final.filled_avg_price is not None else current_bid
        partial_pnl = (exit_price - entry_p) * Decimal("100") * Decimal(sell_qty)

        with session_factory() as session:
            row = session.get(Trade, trade.id)
            if row is None:
                return None
            new_extra = dict(row.extra or {})
            new_extra.setdefault("original_qty", original_qty)
            fired_list = list(new_extra.get("scale_out_tiers_fired", []))
            # Idempotent guard: under the lock this can't be a duplicate, but if
            # it ever is, still record the sell — we already executed it.
            if trigger_pct in fired_list:
                log.warning("scale_out_tier_already_recorded", trade_id=trade.id, tier=trigger_pct)
            fired_list.append(trigger_pct)
            new_extra["scale_out_tiers_fired"] = fired_list
            prior = Decimal(str(new_extra.get("partial_realized_pnl_usd", "0")))
            new_extra["partial_realized_pnl_usd"] = str(prior + partial_pnl)
            new_extra["last_partial_close_order_id"] = final.order_id
            row.extra = new_extra
            # Decrement from the FRESH row.qty, never a stale captured value, so
            # concurrent sells can't clobber each other's decrement.
            row.qty = Decimal(max(0, int(row.qty) - sell_qty))
            session.commit()
            trade.qty = row.qty
            trade.extra = new_extra
            remaining = int(row.qty)
            ticker = new_extra.get("ticker", row.symbol[:3])
            action = new_extra.get("action", "BUY_CALL")

    log.info(
        "scale_out_executed",
        trade_id=trade.id,
        tier=trigger_pct,
        sell_qty=sell_qty,
        exit_price=str(exit_price),
        partial_pnl_usd=str(partial_pnl),
        remaining_qty=remaining,
    )

    return {
        "ticker": ticker,
        "action": action,
        "tier": trigger_pct,
        "sell_qty": sell_qty,
        "exit_price": str(exit_price),
        "partial_pnl_usd": str(partial_pnl),
        "remaining_qty": remaining,
    }


# ---------------------------------------------------------------------------
# Indicator-based exit rules
# ---------------------------------------------------------------------------


def _check_exit_rules(action: str, snap: dict) -> list[str]:
    """Return names of triggered exit rules. Empty list = no trigger."""
    triggered: list[str] = []

    price_s = snap.get("last_close")
    if not price_s:
        return triggered  # no bar data yet

    price = float(price_s)
    vwap = float(snap["vwap"]) if snap.get("vwap") else None
    rsi = float(snap["rsi9"]) if snap.get("rsi9") else None
    ema20 = float(snap["ema20"]) if snap.get("ema20") else None
    ema50 = float(snap["ema50"]) if snap.get("ema50") else None
    vol = float(snap["volume_ratio_20"]) if snap.get("volume_ratio_20") else None

    if action == "BUY_CALL":
        if vwap is not None and price < vwap:
            triggered.append("price_below_vwap")
        if rsi is not None and rsi > 75:   # RSI-9: overbought = 75 (wider than RSI-14's 70)
            triggered.append("rsi_overbought")
        if rsi is not None and rsi < RSI_REVERSAL_CALL:  # momentum flipped bearish against the call
            triggered.append("rsi_reversal_bearish")
        if ema20 is not None and ema50 is not None and ema20 < ema50:
            triggered.append("ema_bearish_cross")
        if vol is not None and vol < VOLUME_FADE_THRESHOLD:
            triggered.append("volume_fading")
    else:  # BUY_PUT
        if vwap is not None and price > vwap:
            triggered.append("price_above_vwap")
        if rsi is not None and rsi < 25:   # RSI-9: oversold = 25 (wider than RSI-14's 30)
            triggered.append("rsi_oversold")
        if rsi is not None and rsi > RSI_REVERSAL_PUT:  # momentum flipped bullish against the put
            triggered.append("rsi_reversal_bullish")
        if ema20 is not None and ema50 is not None and ema20 > ema50:
            triggered.append("ema_bullish_cross")
        if vol is not None and vol < VOLUME_FADE_THRESHOLD:
            triggered.append("volume_fading")

    return triggered


def _thesis_invalidated(action: str, triggered_rules: list[str]) -> bool:
    """True when enough reversal signals confirm the trade thesis is broken.

    Used to force-exit a LOSING position without LLM discretion (fix A). Counts
    how many of the direction's invalidation rules fired; ≥ the threshold means
    momentum has flipped against the position and we should cut, not hold.
    """
    invalidating = _INVALIDATION_RULES.get(action, set())
    hits = sum(1 for r in triggered_rules if r in invalidating)
    return hits >= THESIS_INVALIDATION_MIN_SIGNALS


def _rules_exit_confirm(action: str, triggered: list[str], pnl_pct: float, mode: str):
    """Deterministic replacement for the LLM exit judge (_llm_exit_confirm).

    Pure, reproducible. Encodes the exact rule the LLM prompt described — exit on
    a CONFLUENCE of fading-momentum signals (the aggressive bar: ≥2; selective also
    exits on a single fade while in profit to protect gains). Removes the LLM's
    noisy single-signal discretion that made `smart_exit` the biggest P&L drain
    (−$5,235) by cutting correct-direction trades on intraday wiggles. Winners keep
    riding the trailing stop / scale-out untouched. Returns (should_exit, reason).
    """
    fading = [r for r in triggered if r in _INVALIDATION_RULES.get(action, set())]
    n = len(fading)
    rules_str = ",".join(fading) if fading else "none"
    if mode != "aggressive" and pnl_pct > 0 and n >= 1:
        return True, f"deterministic exit: protect gains, {n} fading [{rules_str}]"
    if n >= 2:
        return True, f"deterministic exit: confluence {n} fading [{rules_str}]"
    return False, ""


# ---------------------------------------------------------------------------
# LLM exit confirmation
# ---------------------------------------------------------------------------


async def _llm_exit_confirm(
    *,
    trade: Trade,
    snap: dict,
    triggered_rules: list[str],
    current_bid: Decimal,
    pnl_pct: float,
    session_factory,
    llm_caller=route_to_model,
) -> tuple[bool, str]:
    """Ask DeepSeek V4-Flash whether to exit. Returns (should_exit, reason)."""
    extra = trade.extra or {}
    ticker = extra.get("ticker", "?")
    action = extra.get("action", "BUY_CALL")
    entry_premium = Decimal(str(trade.entry_price))
    mode = extra.get("mode", "selective")
    entry_reasoning = extra.get("entry_reasoning", "momentum setup")

    occ = extra.get("occ_symbol", trade.symbol)
    try:
        _, expiry_date, _, _ = parse_occ_symbol(occ)
        # Pass DTE explicitly — the LLM mis-read the raw ISO date "2026-06-11" as
        # "June 2026, months away, ample time to recover" and held a 0DTE loser
        # to the floor (#64, −$1,490). Make "expires today" unmistakable.
        dte = (expiry_date - to_et(datetime.now(UTC)).date()).days
        if dte <= 0:
            expiry = (
                f"{expiry_date.isoformat()} — ⚠️ 0DTE, EXPIRES TODAY. Theta is "
                f"LETHAL and there is NO time to recover — do NOT hold a loser "
                f"hoping for a bounce; cut losses fast."
            )
        elif dte == 1:
            expiry = f"{expiry_date.isoformat()} — 1 DTE (expires tomorrow); theta heavy"
        else:
            expiry = f"{expiry_date.isoformat()} — {dte} DTE"
    except ValueError:
        expiry = "unknown"

    opened_at = trade.opened_at
    if opened_at and opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=UTC)
    mins_held = (
        int((datetime.now(UTC) - opened_at).total_seconds() / 60)
        if opened_at else "?"
    )

    profit_target = extra.get("profit_target_premium", "N/A")
    stop_ref = extra.get("stop_premium", "N/A")
    mode_guidance = (
        "AGGRESSIVE — let winners run. Require confluence of ≥2 fading indicators "
        "before exiting a profitable position. Single signals are noise."
        if mode == "aggressive" else
        "SELECTIVE — protect gains. A single strong reversal signal while in profit "
        "is sufficient to EXIT. Don't give back gains chasing more."
    )
    # +75% rule, mode-aware so the framework agrees with MODE CONTEXT (was a
    # contradiction: framework said "any single", aggressive guidance said "≥2").
    profit_lock_rule = (
        "still require ≥2 fading indicators to exit (let winners run — one is noise)"
        if mode == "aggressive" else
        "exit if ANY single indicator shows fading momentum (protect gains)"
    )

    pnl_sign = "+" if pnl_pct >= 0 else "-"
    prompt = _EXIT_CONFIRM_PROMPT.format(
        ticker=ticker,
        action=action,
        mode=mode.upper(),
        entry_premium=str(entry_premium),
        current_bid=str(current_bid),
        pnl_sign=pnl_sign,
        pnl_pct=f"{abs(pnl_pct):.1f}",
        mins_held=mins_held,
        expiry=expiry,
        profit_target=profit_target,
        stop_ref=stop_ref,
        profit_lock_rule=profit_lock_rule,
        price=snap.get("last_close", "?"),
        vwap=snap.get("vwap", "N/A"),
        rsi=snap.get("rsi9", "N/A"),
        ema20=snap.get("ema20", "N/A"),
        ema50=snap.get("ema50", "N/A"),
        vol_ratio=snap.get("volume_ratio_20", "N/A"),
        rules=", ".join(triggered_rules) if triggered_rules else "none — profit zone check",
        entry_reasoning=entry_reasoning,
        mode_guidance=mode_guidance,
    )

    try:
        response = await llm_caller(
            TaskType.EXIT_DECISION, prompt, session_factory=session_factory
        )
        text = response.text.strip()
        if text.startswith("```"):
            text = "\n".join(
                line for line in text.splitlines() if not line.startswith("```")
            )
        data = json.loads(text)
        decision = str(data.get("decision", "HOLD")).upper()
        reason = str(data.get("reason", ""))[:200]
        log.info(
            "exit_confirm_llm",
            trade_id=trade.id,
            decision=decision,
            reason=reason,
            rules=triggered_rules,
        )
        return decision == "EXIT", reason
    except Exception as e:  # noqa: BLE001
        log.warning("exit_confirm_llm_failed", trade_id=trade.id, error=str(e))
        return False, ""  # on failure, HOLD


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def format_scale_out(r: dict) -> str:
    """Plain-language #signals message for one scale-out tier firing.

    Part of the trade lifecycle: posted each time a profit tier locks in a
    chunk, between the entry confirmation and the final close."""
    action = r.get("action", "BUY_CALL")
    option_word = "CALL" if action == "BUY_CALL" else "PUT"
    ticker = r.get("ticker", "SPY")
    sell_qty = r.get("sell_qty", 0)
    remaining = r.get("remaining_qty", 0)
    try:
        tier = float(r.get("tier", 0))
    except (TypeError, ValueError):
        tier = 0.0
    try:
        locked = abs(float(r.get("partial_pnl_usd", 0)))
    except (TypeError, ValueError):
        locked = 0.0

    tail = (
        f"holding {remaining}× for higher targets"
        if remaining and remaining > 0
        else "position now fully scaled out"
    )
    return (
        f"💰 **{ticker} {option_word} — scaled out {sell_qty}× at +{tier:.0f}% gain** · "
        f"locked in ${locked:,.0f} · {tail}.\n"
        f"**Manual: Sell {sell_qty}× {ticker} {option_word} at market** to match."
    )


def _format_exit_combined(
    trade: Trade,
    exit_premium: Decimal,
    reason: str,
    llm_reasoning: str = "",
) -> str:
    """Single #signals message combining manual close instruction + P&L."""
    extra = trade.extra or {}
    ticker = extra.get("ticker", trade.symbol[:3])
    action = extra.get("action", "BUY_CALL")
    occ = extra.get("occ_symbol", trade.symbol)
    qty = int(trade.qty)
    mode = extra.get("mode", "selective")

    entry_p = Decimal(str(trade.entry_price))
    pnl_per_share = (exit_premium - entry_p).quantize(Decimal("0.01"))
    pnl_total = (pnl_per_share * 100 * qty).quantize(Decimal("0.01"))
    is_profit = pnl_per_share >= 0
    pnl_icon = "✅" if is_profit else "❌"
    pnl_pct = abs(int(pnl_per_share / entry_p * 100)) if entry_p else 0

    reason_text = {
        "hard_floor_stop": "🛑 hard floor stop",
        "stop_premium": "🛑 stop hit — limiting loss",
        "trailing_stop": "🔒 trailing stop — locking in gains",
        "smart_exit": "🧠 smart exit — thesis reversed",
        "smart_profit_exit": "💰 smart profit exit — indicators say take it",
        "thesis_invalidated": "🚫 thesis invalidated — momentum reversed, cutting loss",
        "zdte_early_cut": "⏱️ 0DTE early cut — losing + below VWAP, no time to recover",
        "force_close": "⏰ closing before market close",
    }.get(reason, f"closing ({reason})")

    option_word = "CALL" if action == "BUY_CALL" else "PUT"
    icon = "📈" if action == "BUY_CALL" else "📉"

    lines = [
        f"{icon} **{ticker} {option_word} — model closed** [{mode.upper()}]",
        "",
        f"Reason: {reason_text}",
    ]
    if llm_reasoning:
        lines.append(f"_{llm_reasoning}_")
    lines += [
        "",
        f"Model sold: {qty}× `{occ}` @ **${exit_premium}**/share",
        f"**Manual close: Sell {qty}× {ticker} {option_word} at market**",
        "",
        f"P&L: **{pnl_icon} ${abs(pnl_total)}** "
        f"({pnl_pct}% {'gain' if is_profit else 'loss'} · "
        f"${abs(pnl_per_share)}/share × {qty} contracts × 100)",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_directional_exit_monitor(
    *,
    now: datetime | None = None,
    session_factory: Callable[[], Session] | None = None,
    quote_fetcher: Callable[..., object] = get_single_option_quote,
    bars_fetcher: Callable[..., object] = get_recent_bars,
    submitter: Callable[..., object] = alpaca_client.submit_single_option_sell,
    waiter: Callable[..., object] = alpaca_client.wait_for_order,
    llm_caller: Callable[..., object] = route_to_model,
    force_close: bool | None = None,
    fill_timeout_s: float = 60.0,
) -> list[dict]:
    """Sweep all open directional trades. Returns one dict per trade processed."""
    now = now or datetime.now(UTC)
    factory = session_factory or make_session_factory()

    et_now = to_et(now)
    past_force_close_time = et_now.time() >= FORCE_CLOSE_AFTER
    today = et_now.date()
    global_force = force_close  # None → decide per-trade

    with factory() as session:
        trades = _open_directional_trades(session)

    results: list[dict] = []
    if not trades:
        return results

    for trade in trades:
        extra = trade.extra or {}
        occ = extra.get("occ_symbol", trade.symbol)
        action = extra.get("action", "BUY_CALL")
        ticker = extra.get("ticker", occ[:3])
        entry_p = Decimal(str(trade.entry_price))

        # ---- determine per-trade force flag ----
        if global_force is not None:
            trade_force = global_force
        elif past_force_close_time:
            try:
                _, expiry, _, _ = parse_occ_symbol(occ)
                trade_force = expiry == today
            except ValueError:
                trade_force = True
        else:
            trade_force = False

        # ---- get current option price ----
        quote = await quote_fetcher(occ)
        if quote is None or quote.bid <= 0:
            log.warning("directional_exit_no_quote", trade_id=trade.id, occ=occ)
            results.append({"trade_id": trade.id, "status": "no_quote"})
            continue

        # Broken quote guard: if ask > 5× bid the data feed is corrupted
        # (e.g. stale last-trade price, crossed market, or Alpaca data gap).
        # Trading on corrupt quotes would compute a wildly wrong P&L and
        # potentially trigger a false hard-floor exit.
        if quote.ask > quote.bid * 5 and quote.bid > 0:
            log.warning(
                "directional_exit_quote_sanity_fail",
                trade_id=trade.id, occ=occ,
                bid=str(quote.bid), ask=str(quote.ask),
            )
            results.append({"trade_id": trade.id, "status": "stale_quote"})
            continue

        current_bid = quote.bid
        pnl_pct = float((current_bid - entry_p) / entry_p * 100)

        # ---- trailing stop ratchet (peak update only — no order submission) ----
        ratcheted = _maybe_ratchet_trailing_stop(factory, trade, pnl_pct, entry_p)

        # ---- exit decision ----
        should_exit = False
        reason = ""
        llm_reasoning = ""

        # Reload stop_p from DB if it was just ratcheted, otherwise use extra.
        if ratcheted is not None:
            stop_p = ratcheted
        else:
            stop_p_raw = extra.get("stop_premium")
            stop_p = Decimal(str(stop_p_raw)) if stop_p_raw else None
        trade_mode = extra.get("mode", "selective")
        # Hard floor is mode-aware: selective uses −30% (same as its stop), aggressive
        # uses −50% to match its wider stop. Both act as unconditional floors with no LLM.
        hard_floor = Decimal("0.50") if trade_mode == "aggressive" else HARD_FLOOR_PCT

        if trade_force:
            should_exit, reason = True, "force_close"

        elif current_bid <= entry_p * (Decimal("1") - hard_floor):
            should_exit, reason = True, "hard_floor_stop"

        elif stop_p is not None and current_bid <= stop_p:
            # Distinguish trailing stop (protecting gains) from original hard stop (limiting losses)
            trailing_active = (extra.get("trailing_stop_active") or ratcheted is not None)
            reason = "trailing_stop" if trailing_active else "stop_premium"
            should_exit = True
            log.info(
                "directional_exit_stop_hit",
                trade_id=trade.id,
                occ=occ,
                reason=reason,
                current_bid=str(current_bid),
                stop_premium=str(stop_p),
            )

        else:
            # Scale-out first: partial-close a chunk if peak crossed a new tier.
            # Only runs when force/hard_floor/stop checks did NOT fire.
            partial = await _maybe_scale_out(factory, trade, current_bid, submitter, waiter)
            if partial:
                results.append({"trade_id": trade.id, "status": "scaled_out", **partial})
                if int(trade.qty) <= 0:
                    continue  # fully scaled out — nothing left for indicators

            # Fetch bars + indicators once — used for both reversal and profit checks.
            snap: dict = {}
            try:
                bars = await bars_fetcher(ticker, timeframe_minutes=5, limit=60)
                snap = indicators.snapshot(bars)
            except Exception as e:  # noqa: BLE001
                log.warning("exit_monitor_bars_failed", trade_id=trade.id, error=str(e))

            triggered = _check_exit_rules(action, snap)
            in_strong_profit = pnl_pct >= PROFIT_LOCK_PCT

            # Is this a 0DTE (expires today)? Used by the early-cut below.
            try:
                _, _exp_date, _, _ = parse_occ_symbol(occ)
                is_0dte = _exp_date <= today
            except ValueError:
                is_0dte = False
            # Price broken through VWAP against the position — the one reversal
            # signal that IS computable early (VWAP needs few bars; RSI/EMA don't).
            vwap_broken = (
                "price_below_vwap" in triggered if action == "BUY_CALL"
                else "price_above_vwap" in triggered
            )
            zdte_cut_pct = float(get_settings().zdte_early_loss_cut_pct) * 100

            # 0DTE indicator-independent early-cut (fix, 2026-06-11): early in the
            # session RSI/EMA/volume aren't warmed up, so fix A's ≥2-signal gate
            # can't fire and the LLM mismanages 0DTE losers ("time to recover" —
            # there is none on a 0DTE). If a 0DTE is past the loss threshold with
            # price through VWAP against us, cut now without the LLM. (#64 rode to
            # −50% because only price_below_vwap was available and the LLM held.)
            if is_0dte and pnl_pct <= -zdte_cut_pct and vwap_broken:
                should_exit, reason = True, "zdte_early_cut"
                log.info(
                    "directional_exit_zdte_early_cut",
                    trade_id=trade.id, pnl_pct=f"{pnl_pct:+.1f}%",
                    triggered_rules=triggered,
                )

            # Thesis-invalidation hard exit (fix A): if the position is at a LOSS
            # and momentum has confirmed-reversed against it, cut immediately —
            # do NOT give the LLM a chance to rationalise holding (#60 was held
            # −15%→−25% on "stop not hit / theta negligible" while RSI climbed
            # back through 55 and volume collapsed). Winners are untouched
            # (gated on pnl_pct < 0); they ride the trailing stop / profit logic.
            elif pnl_pct < 0 and _thesis_invalidated(action, triggered):
                should_exit, reason = True, "thesis_invalidated"
                log.info(
                    "directional_exit_thesis_invalidated",
                    trade_id=trade.id,
                    pnl_pct=f"{pnl_pct:+.1f}%",
                    triggered_rules=triggered,
                )

            # Consult LLM if: (a) any indicator rule fired, OR (b) P&L is high enough
            # that we should proactively check whether to take the gain.
            elif triggered or in_strong_profit:
                # Platform-first: deterministic exit confirm (no LLM) when enabled.
                if get_settings().deterministic_engine:
                    should_exit, llm_reasoning = _rules_exit_confirm(
                        action, triggered, pnl_pct, trade_mode
                    )
                else:
                    should_exit, llm_reasoning = await _llm_exit_confirm(
                        trade=trade,
                        snap=snap,
                        triggered_rules=triggered,
                        current_bid=current_bid,
                        pnl_pct=pnl_pct,
                        session_factory=factory,
                        llm_caller=llm_caller,
                    )
                if should_exit:
                    reason = "smart_profit_exit" if pnl_pct > 0 else "smart_exit"
                    log.info(
                        "directional_exit_smart",
                        trade_id=trade.id,
                        reason=reason,
                        pnl_pct=f"{pnl_pct:+.1f}%",
                        triggered_rules=triggered,
                        in_strong_profit=in_strong_profit,
                    )

        if not should_exit:
            results.append({
                "trade_id": trade.id,
                "status": "hold",
                "current_bid": str(current_bid),
                "pnl_pct": f"{pnl_pct:+.1f}%",
            })
            continue

        # ---- submit close order ----
        # Re-read fresh: the 30-sec tick may have already closed this trade in
        # the gap since we loaded it. Skip the redundant sell (which Alpaca
        # rejects as a phantom short-open) if so. (fix D)
        with factory() as session:
            fresh = session.get(Trade, trade.id)
            if fresh is None or fresh.closed_at is not None:
                results.append({"trade_id": trade.id, "status": "already_closed_by_other_job"})
                continue
        try:
            order = await submitter(
                qty=int(trade.qty),
                occ_symbol=occ,
                limit_price=current_bid,
            )
            final = await waiter(order.order_id, timeout_s=fill_timeout_s)
        except Exception as e:  # noqa: BLE001
            err_str = str(e)
            # Auto-close the DB row only when Alpaca confirms the position is genuinely
            # gone from the broker's book. Match on message text, not just error code —
            # 42210000 is also returned for "IOC not supported" which does NOT mean the
            # position is absent (it's a TIF rejection, position still exists).
            if (
                "position intent" in err_str
                or "uncovered" in err_str
                or "40310000" in err_str
                or ("42210000" in err_str and "time_in_force" not in err_str)
            ):
                # Verify the position is genuinely absent before auto-closing.
                # Alpaca paper sometimes rejects SELL_TO_CLOSE with "position intent
                # mismatch" even when the position IS in the book (timing/tracking
                # inconsistency). Only auto-close if get_positions() confirms it's gone.
                try:
                    live_positions = await alpaca_client.get_positions()
                    still_live = any(
                        getattr(p, "symbol", "") == occ for p in live_positions
                    )
                except Exception:  # noqa: BLE001
                    still_live = False  # can't verify — be conservative and close

                if still_live:
                    # Position IS in Alpaca — the error was a transient rejection.
                    # Log the warning but leave the DB row open to retry next cycle.
                    log.warning(
                        "directional_exit_sell_rejected_position_exists",
                        trade_id=trade.id, occ=occ, error=err_str,
                    )
                    results.append({
                        "trade_id": trade.id,
                        "status": "sell_rejected_retry_next_cycle",
                        "error_text": f"⚠️ Exit rejected for trade #{trade.id} ({occ}) but position exists — will retry.",
                    })
                else:
                    log.warning(
                        "directional_exit_position_not_in_broker",
                        trade_id=trade.id, occ=occ, error=err_str,
                    )
                    with factory() as session:
                        row = session.get(Trade, trade.id)
                        if row is not None:
                            _close_trade_row(
                                session, row,
                                exit_premium=current_bid,
                                order=None,
                                reason="position_not_in_broker",
                                llm_reasoning="Position not found in Alpaca — auto-closed to stop retry loop",
                            )
                    results.append({
                        "trade_id": trade.id,
                        "status": "closed_position_not_in_broker",
                        "error_text": (
                            f"⚠️ Trade #{trade.id} ({occ}) not found in Alpaca broker — "
                            f"marked closed to stop retry loop. Check paper account."
                        ),
                    })
            else:
                log.error("directional_exit_submit_failed", trade_id=trade.id, error=err_str)
                results.append({
                    "trade_id": trade.id,
                    "status": "submit_error",
                    "error_text": f"⚠️ Exit order failed for trade #{trade.id}: `{err_str}`",
                })
            continue

        log.info(
            "directional_exit_terminal",
            trade_id=trade.id,
            reason=reason,
            order_id=final.order_id,
            status=final.status,
        )

        exit_premium = (
            final.filled_avg_price
            if final.filled_avg_price is not None
            else current_bid
        )

        if final.status == "filled":
            with factory() as session:
                row = session.get(Trade, trade.id)
                if row is not None:
                    _close_trade_row(
                        session, row,
                        exit_premium=exit_premium,
                        order=final,
                        reason=reason,
                        llm_reasoning=llm_reasoning,
                    )
            combined_text = _format_exit_combined(
                trade, exit_premium, reason, llm_reasoning
            )
            results.append({
                "trade_id": trade.id,
                "status": "closed",
                "reason": reason,
                "exit_premium": str(exit_premium),
                "pnl_per_share": str(
                    (exit_premium - entry_p).quantize(Decimal("0.01"))
                ),
                "combined_text": combined_text,
            })
        else:
            results.append({
                "trade_id": trade.id,
                "status": f"close_order_{final.status}",
                "reason": reason,
                "error_text": (
                    f"⚠️ Directional close failed — trade #{trade.id} · "
                    f"reason `{reason}` · order status `{final.status}`"
                ),
            })

    return results


# ---------------------------------------------------------------------------
# Lightweight 30-second trailing-stop tick — for fast-moving 0DTE positions
# ---------------------------------------------------------------------------


async def run_trailing_stop_tick(
    *,
    session_factory: Callable[[], Session] | None = None,
    quote_fetcher: Callable[..., object] = get_single_option_quote,
    submitter: Callable[..., object] = alpaca_client.submit_single_option_sell,
    waiter: Callable[..., object] = alpaca_client.wait_for_order,
    fill_timeout_s: float = 30.0,
) -> list[dict]:
    """Fast trailing-stop sweep (no indicators, no LLM).

    Runs every 30 seconds during RTH. For each open directional trade:
      1. Fetch current option bid.
      2. Update peak P&L and ratchet trailing stop if new tier reached.
      3. Partial-close (scale out) if a scale-out tier was newly crossed.
      4. Full exit if current bid <= stop_premium (trailing or hard stop).

    Safe to run alongside the 5-min full exit monitor. The 5-min monitor still
    handles thesis-reversal exits via indicators + LLM; this tick only handles
    price-based stop hits and scaling out.
    """
    factory = session_factory or make_session_factory()
    with factory() as session:
        trades = _open_directional_trades(session)

    results: list[dict] = []
    if not trades:
        return results

    for trade in trades:
        extra = trade.extra or {}
        occ = extra.get("occ_symbol", trade.symbol)
        entry_p = Decimal(str(trade.entry_price))

        quote = await quote_fetcher(occ)
        if quote is None or quote.bid <= 0:
            continue
        if quote.ask > quote.bid * 5 and quote.bid > 0:
            continue  # corrupted quote — skip silently, next tick will retry

        current_bid = quote.bid
        pnl_pct = float((current_bid - entry_p) / entry_p * 100)

        # 1. Update peak + ratchet stop on remainder
        _maybe_ratchet_trailing_stop(factory, trade, pnl_pct, entry_p)

        # 2. Scale out partial if a tier was newly crossed
        partial = await _maybe_scale_out(factory, trade, current_bid, submitter, waiter)
        if partial:
            results.append({
                "trade_id": trade.id,
                "status": "scaled_out",
                **partial,
            })

        # 3. Re-read fresh trade state (qty may have changed)
        if int(trade.qty) <= 0:
            continue  # fully exited via scale-out — shouldn't happen but safe

        extra = trade.extra or {}
        stop_p_raw = extra.get("stop_premium")
        stop_p = Decimal(str(stop_p_raw)) if stop_p_raw else None
        trade_mode = extra.get("mode", "selective")
        hard_floor = Decimal("0.50") if trade_mode == "aggressive" else HARD_FLOOR_PCT

        should_exit = False
        reason = ""
        if current_bid <= entry_p * (Decimal("1") - hard_floor):
            should_exit, reason = True, "hard_floor_stop"
        elif stop_p is not None and current_bid <= stop_p:
            trailing_active = extra.get("trailing_stop_active", False)
            reason = "trailing_stop" if trailing_active else "stop_premium"
            should_exit = True

        if not should_exit:
            continue

        # 4. Full exit of remaining qty — re-read fresh first so we don't fire a
        # redundant sell the 5-min monitor already submitted (fix D).
        with factory() as session:
            fresh = session.get(Trade, trade.id)
            if fresh is None or fresh.closed_at is not None:
                continue
        qty = int(trade.qty)
        try:
            order = await submitter(qty=qty, occ_symbol=occ, limit_price=current_bid)
            final = await waiter(order.order_id, timeout_s=fill_timeout_s)
        except Exception as e:  # noqa: BLE001
            log.warning("tick_exit_submit_failed", trade_id=trade.id, error=str(e))
            continue

        if final.status == "filled":
            exit_premium = final.filled_avg_price or current_bid
            with factory() as session:
                row = session.get(Trade, trade.id)
                if row is not None:
                    _close_trade_row(
                        session, row,
                        exit_premium=exit_premium,
                        order=final,
                        reason=reason,
                    )
            combined_text = _format_exit_combined(trade, exit_premium, reason, "")
            log.info(
                "tick_exit_terminal",
                trade_id=trade.id,
                reason=reason,
                exit_price=str(exit_premium),
            )
            results.append({
                "trade_id": trade.id,
                "status": "closed",
                "reason": reason,
                "exit_premium": str(exit_premium),
                "combined_text": combined_text,
            })

    return results
