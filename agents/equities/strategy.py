"""Accuracy-focused entry strategy for the equities signal scanner.

Why this exists (separate from `signal_engine.decide`):
  The base trend engine fires when price is already 0.10–0.25% past VWAP with high
  ADX — it *chases extensions*, triggering only after the move has largely happened,
  with no check for trend maturity, momentum deceleration, or room left to run. On
  the volatile equities watchlist (PLTR/SNOW/MU vs MSFT) its raw-% bands also misfire.

  This module keeps the proven *trend-direction* qualification but replaces the
  "fire on extension" logic with gates that only signal when there is trend LEFT to
  capture: ATR-normalized so they fit every ticker, room-to-target as a hard gate,
  exhaustion/momentum-deceleration filters, and a pullback-vs-breakout-vs-chase
  classifier that makes conviction mean something.

ISOLATION: this is used ONLY by the alert-only equities scanner. The live SPY bot
keeps using `signal_engine.decide` unchanged — this file imports its S/R helpers
read-only and never mutates it.

Thresholds are module constants — STARTING points. Calibrate them with
scripts/backtest_equities_strategy.py before trusting (every "obvious" filter this
codebase has tried needed a backtest to confirm). Bump EQUITY_ENGINE_VERSION on any
rule change so persisted signals stay auditable.
"""
from __future__ import annotations

from agents.directional.signal_engine import _collect_levels, _f, _headroom
from trademaster import indicators
from trademaster.logging import get_logger

log = get_logger(__name__)

EQUITY_ENGINE_VERSION = "equity_trend_v1"

# --- thresholds (ATR-normalized; calibrate via backtest_equities_strategy.py) ---
EQ_ADX_MIN = 25.0          # trend-strength floor (mirrors base; tuned independently)
DIST_OVEREXT_ATR = 2.5     # > this many ATR from VWAP → too stretched, late → HOLD
ROOM_MIN_ATR = 1.5         # need ≥ this ATR of clear room to the next opposing S/R
RSI_EXH_HI = 72.0          # call: RSI above → overbought/exhausted → HOLD
RSI_EXH_LO = 28.0          # put:  RSI below → oversold/exhausted → HOLD
VOL_FLOOR = 0.80           # volume_ratio_20 below → volume fading → HOLD
VOL_BREAKOUT = 1.30        # a new-extreme breakout needs volume_ratio ≥ this
ADX_ROLLOVER_HI = 35.0     # only treat a FALLING ADX as exhaustion when ADX is already high
ADX_SLOPE_LAG = 3          # bars back to measure ADX slope
PULLBACK_MIN_FRAC = 0.20   # retrace ≥ this fraction of the day's range from the extreme …
PULLBACK_MAX_FRAC = 0.65   # … but ≤ this (deeper = the trend may be breaking, not resuming)
BREAKOUT_EDGE_FRAC = 0.05  # within this fraction of the day's range of the extreme = "at it"
ALLOW_BREAKOUT = True      # post volume-confirmed new-extreme breakouts as MEDIUM


def _macd_hist_slope(bars) -> float | None:
    """MACD-histogram change vs the prior bar. >0 = momentum building."""
    if len(bars) < 2:
        return None
    now_m, now_s = indicators.macd(bars)
    prev_m, prev_s = indicators.macd(bars[:-1])
    if None in (now_m, now_s, prev_m, prev_s):
        return None
    return float(now_m - now_s) - float(prev_m - prev_s)


def _adx_slope(bars, adx_now: float) -> float | None:
    """ADX change over ADX_SLOPE_LAG bars. <0 = trend strength fading."""
    if len(bars) <= ADX_SLOPE_LAG:
        return None
    prev = indicators.adx(bars[:-ADX_SLOPE_LAG])
    if prev is None:
        return None
    return adx_now - float(prev)


def _resuming(bars, up: bool) -> bool | None:
    """Is the latest bar turning back in the trend direction (pullback resuming)?"""
    if len(bars) < 2:
        return None
    last, prev = float(bars[-1].close), float(bars[-2].close)
    return last > prev if up else last < prev


def _range_position(price: float, sh: float | None, sl: float | None, up: bool) -> float | None:
    """Where price sits in the day's range, in the trade's direction.
    0 = at the base of the move, 1 = at the extreme (the high for a call)."""
    sh, sl = _f(sh), _f(sl)
    if sh is None or sl is None or sh <= sl:
        return None
    return (price - sl) / (sh - sl) if up else (sh - price) / (sh - sl)


def _classify_setup(price, sh, sl, vol_ratio, resuming, up):
    """pullback (best) / breakout (ok) / chase (skip).

    pullback = retraced a meaningful slice of the day's range from the extreme and
               is turning back in-trend → entering near the base of the next leg.
    breakout = at/through the session extreme with volume confirmation.
    chase    = mid-extension or stalled near the extreme with no fresh volume.
    """
    pos = _range_position(price, sh, sl, up)
    if pos is None:
        return "chase"  # no range info → can't confirm room/freshness → conservative
    retrace = 1.0 - pos  # how far back from the extreme
    at_extreme = pos >= 1.0 - BREAKOUT_EDGE_FRAC
    if at_extreme:
        if ALLOW_BREAKOUT and vol_ratio is not None and vol_ratio >= VOL_BREAKOUT:
            return "breakout"
        return "chase"  # at the high but no volume → exhaustion, not continuation
    if PULLBACK_MIN_FRAC <= retrace <= PULLBACK_MAX_FRAC and resuming:
        return "pullback"
    return "chase"


def decide_equity(ticker: str, bars, snap: dict, market_ctx: dict | None = None, now=None):
    """Indicator snapshot + bars → TickerDecision for the equities scanner.

    Same TickerDecision contract as signal_engine.decide so the scanner/formatter
    are unchanged. HOLD unless the setup has room, live momentum, and is a pullback
    or a volume-confirmed breakout (never a mid-extension chase).
    """
    from agents.directional.intraday import TickerDecision

    def hold(reason: str):
        return TickerDecision(ticker, "HOLD", None, None, "LOW",
                              f"{EQUITY_ENGINE_VERSION}: {reason}")

    price = _f(snap.get("last_close"))
    vwap = _f(snap.get("vwap"))
    ema = _f(snap.get("ema20"))
    adx = _f(snap.get("adx"))
    atr = _f(snap.get("atr10"))
    rsi = _f(snap.get("rsi9"))
    vol_ratio = _f(snap.get("volume_ratio_20"))

    # Bootstrap guard — explicit per-ticker log (the equities path lacked this; see I7).
    if None in (price, vwap, ema, adx, atr) or price <= 0 or atr <= 0:
        log.info("equities_strategy_unbootstrapped", ticker=ticker,
                 bars=snap.get("bars"), adx=snap.get("adx"), atr10=snap.get("atr10"))
        return hold("indicators not bootstrapped")

    up = price > vwap and price > ema
    down = price < vwap and price < ema
    if not (up or down):
        return hold(f"no trend (price {price:.2f} vs vwap {vwap:.2f}/ema {ema:.2f})")
    if adx < EQ_ADX_MIN:
        return hold(f"weak trend (ADX {adx:.1f} < {EQ_ADX_MIN:.0f})")
    action = "BUY_CALL" if up else "BUY_PUT"

    dist_atr = abs(price - vwap) / atr
    if dist_atr > DIST_OVEREXT_ATR:
        return hold(f"overextended ({dist_atr:.1f} ATR from VWAP > {DIST_OVEREXT_ATR}) — late")

    # GATE 1 — room to the next opposing S/R, in ATR units (hard gate).
    hr_pct, lvl = _headroom(price, action, _collect_levels(market_ctx))
    room_atr = None
    if hr_pct is not None:
        room_atr = (hr_pct / 100.0 * price) / atr
        if room_atr < ROOM_MIN_ATR:
            kind = "resistance" if up else "support"
            return hold(f"no room — {kind} ${lvl:.2f} only {room_atr:.1f} ATR ahead "
                        f"(< {ROOM_MIN_ATR})")

    # GATE 2 — not exhausted / momentum still building.
    if up and rsi is not None and rsi > RSI_EXH_HI:
        return hold(f"RSI exhausted ({rsi:.0f} > {RSI_EXH_HI}) — reversal risk")
    if down and rsi is not None and rsi < RSI_EXH_LO:
        return hold(f"RSI exhausted ({rsi:.0f} < {RSI_EXH_LO}) — reversal risk")
    # ADX rolling over (only meaningful once ADX is already high) — checked before
    # the MACD slope so a clearly-exhausting strong trend reports the trend reason.
    adx_slope = _adx_slope(bars, adx)
    if adx_slope is not None and adx >= ADX_ROLLOVER_HI and adx_slope < 0:
        return hold(f"trend exhausting (ADX {adx:.1f} rolling over, Δ{adx_slope:+.1f})")
    hist_slope = _macd_hist_slope(bars)
    if hist_slope is not None and ((up and hist_slope < 0) or (down and hist_slope > 0)):
        return hold("momentum decelerating (MACD histogram contracting)")
    if vol_ratio is not None and vol_ratio < VOL_FLOOR:
        return hold(f"volume fading (vol_ratio {vol_ratio:.2f} < {VOL_FLOOR})")

    # GATE 4 — setup classification → conviction (subsumes the maturity check).
    sh = (market_ctx or {}).get("session_high")
    sl = (market_ctx or {}).get("session_low")
    setup = _classify_setup(price, sh, sl, vol_ratio, _resuming(bars, up), up)
    if setup == "chase":
        return hold("mid-extension chase — wait for a pullback or a fresh breakout")
    conviction = "HIGH" if setup == "pullback" else "MEDIUM"

    strike = round(price)
    room_str = f"{room_atr:.1f} ATR room" if room_atr is not None else "clean breakout (no level ahead)"
    rsi_str = f"{rsi:.0f}" if rsi is not None else "n/a"
    reason = (
        f"{EQUITY_ENGINE_VERSION}: {'UP' if up else 'DOWN'}-trend {setup} "
        f"(ADX {adx:.1f}, {dist_atr:.1f} ATR from VWAP, {room_str}, "
        f"RSI {rsi_str}) → {action}/{conviction}"
    )
    analysis = {
        "spy_price": price,  # key reused by format_equities_signal (it's the ticker price)
        "session_high": _f(sh), "session_low": _f(sl),
        "dist_atr": round(dist_atr, 2),
        "room_atr": round(room_atr, 2) if room_atr is not None else None,
        "setup": setup, "adx": round(adx, 1),
        "rsi9": round(rsi, 1) if rsi is not None else None,
    }
    return TickerDecision(ticker, action, float(strike), "0DTE", conviction, reason, analysis)
