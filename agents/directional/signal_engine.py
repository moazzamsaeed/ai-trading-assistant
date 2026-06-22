"""Deterministic directional signal engine — the LLM's replacement in the
decision hot-path (platform-first architecture).

Rules-only, reproducible, backtestable, auditable. Same `TickerDecision` output
the LLM path produced, so the executor / risk-manager / exit logic downstream are
untouched — we swap the *brain*, keep the *platform*.

Signal: the trend-following edge validated in scripts/backtest_trend_0dte.py —
trade WITH an established intraday trend (price on the trend side of session VWAP
*and* a short EMA, with ADX confirming strength). Conviction uses the inverted-U
"sweet spot" found in the conviction stratification: the edge is real at MODERATE
trend strength/separation and DECAYS at the extremes (very high ADX or far from
VWAP = overextended → reverts), so extremes are filtered out, not sized up.

⚠️ HONEST SCOPE: validation showed this edge is real (+2–3pp directional, both
sides on SPY) but THIN, and does NOT clear 0DTE option costs once the
vol-risk-premium is priced in (scripts/model_0dte_pnl.py). This engine exists to
(1) prove the deterministic-core architecture and (2) capture real paper-fill
data — NOT as a profit expectation on 0DTE. The platform can host a better
signal (e.g. premium-selling) later without a rebuild.

Bump ENGINE_VERSION on any rule change so persisted decisions stay auditable.
"""
from __future__ import annotations

ENGINE_VERSION = "trend_follow_v2"   # v2: S/R-aware (don't buy into the level just ahead)

# Thresholds — calibrated from the conviction stratification on SPY 15-min,
# 2023→2026 (scripts/backtest_trend_0dte.py). Kept as module constants for now;
# promote to settings.* when tuning.
ADX_MIN = 25.0            # below → no tradeable trend, HOLD
ADX_SWEET_LO = 30.0      # HIGH-conviction ADX band [30,40)
ADX_SWEET_HI = 40.0
ADX_OVEREXT = 50.0       # at/above → overextended, reverts → HOLD
DIST_MIN = 0.10          # % |close−VWAP|; below → no separation / no edge → HOLD
DIST_SWEET_HI = 0.25     # HIGH-conviction upper VWAP-distance bound
DIST_OVEREXT = 0.50      # above → overextended → HOLD

# S/R headroom (v2): a trend entry needs room to run to the next level. The risk
# is buying calls into overhead resistance, or puts into support just below.
HEADROOM_MIN = 0.15      # % to the nearest opposing level; below → no room → HOLD
HEADROOM_COMFORT = 0.30  # % below → room is tight → cap conviction at MEDIUM


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


FORMING_ADX_LO = 20.0   # trend building below the ADX_MIN entry threshold
FORMING_DIST_LO = 0.05  # VWAP separation building below the DIST_MIN entry threshold


def forming_signal(ticker: str, snap: dict) -> dict | None:
    """Engine-native near-miss: a setup that's CLOSE to an entry but not there yet.

    Replaces the old 4-criteria near-miss (which used vol/RSI thresholds the engine
    ignores → inconsistent "setup forming" alerts). Puts-focus: only forming PUT
    setups are surfaced when puts-only is on. Returns a `forming` dict the
    scheduler/format_setup_forming consume, or None.
    """
    price = _f(snap.get("last_close"))
    vwap = _f(snap.get("vwap"))
    ema = _f(snap.get("ema20"))
    adx = _f(snap.get("adx"))
    if None in (price, vwap, ema, adx) or price <= 0:
        return None
    up = price > vwap and price > ema
    down = price < vwap and price < ema
    if not (up or down):
        return None
    if up and _puts_only():
        return None
    dist = abs(price - vwap) / price * 100.0
    if adx >= ADX_OVEREXT or dist > DIST_OVEREXT:
        return None  # overextended — not "forming", and the engine wouldn't enter
    action = "BUY_CALL" if up else "BUY_PUT"
    word = "up" if up else "down"
    if FORMING_ADX_LO <= adx < ADX_MIN and dist >= DIST_MIN:
        note = f"{word}-trend in place, ADX {adx:.1f} building toward {ADX_MIN:.0f}"
    elif adx >= ADX_MIN and FORMING_DIST_LO <= dist < DIST_MIN:
        note = (f"{word}-trend + ADX {adx:.1f}, VWAP separation {dist:.2f}% "
                f"building toward {DIST_MIN:.2f}%")
    else:
        return None
    return {"would_be_action": action, "engine": True, "note": note,
            "adx": round(adx, 1), "dist": round(dist, 2)}


def _puts_only() -> bool:
    try:
        from trademaster.config import get_settings
        return bool(get_settings().directional_puts_only)
    except Exception:  # noqa: BLE001 — never let a config read break the decision
        return False


def _collect_levels(market_ctx: dict | None) -> list[float]:
    """Candidate S/R price levels from market_ctx — same sources as the LLM
    key-levels block: prior-day high/low/close, MA5/MA10, ORB high/low, session
    high/low. Returns sorted positive floats. Fail-open ([] when unavailable)."""
    if not market_ctx:
        return []
    md = market_ctx.get("multi_day") or {}
    out = []
    for v in (md.get("prev_high"), md.get("prev_low"), md.get("prev_close"),
              md.get("ma5"), md.get("ma10"),
              market_ctx.get("orb_high"), market_ctx.get("orb_low"),
              market_ctx.get("session_high"), market_ctx.get("session_low")):
        f = _f(v)
        if f and f > 0:
            out.append(f)
    return sorted(out)


def _headroom(price: float, action: str, levels: list[float]):
    """% distance to the level standing in the trade's way (overhead resistance
    for a call, support below for a put), and that level. (None, None) if none —
    e.g. a clean breakout with nothing ahead. Fail-open."""
    if not levels or price <= 0:
        return None, None
    if action == "BUY_CALL":
        ahead = [l for l in levels if l > price]
        if not ahead:
            return None, None
        lvl = min(ahead)
        return (lvl - price) / price * 100.0, lvl
    ahead = [l for l in levels if l < price]   # BUY_PUT → support below
    if not ahead:
        return None, None
    lvl = max(ahead)
    return (price - lvl) / price * 100.0, lvl


def decide(ticker: str, snap: dict, market_ctx: dict | None = None, now=None):
    """Pure function: indicator snapshot → TickerDecision. No I/O, no LLM."""
    # Imported here to avoid a circular import (intraday imports this lazily).
    from agents.directional.intraday import TickerDecision, _build_analysis

    def hold(reason: str):
        return TickerDecision(ticker, "HOLD", None, None, "LOW", f"{ENGINE_VERSION}: {reason}")

    price = _f(snap.get("last_close"))
    vwap = _f(snap.get("vwap"))
    ema = _f(snap.get("ema20"))   # short trend EMA (validated rule used a short EMA)
    adx = _f(snap.get("adx"))
    if None in (price, vwap, ema, adx) or price <= 0:
        return hold("indicators not bootstrapped")

    dist = abs(price - vwap) / price * 100.0
    up = price > vwap and price > ema
    down = price < vwap and price < ema
    if not (up or down):
        return hold(f"no trend (price {price:.2f} vs vwap {vwap:.2f} / ema {ema:.2f})")
    if adx < ADX_MIN:
        return hold(f"weak trend (ADX {adx:.1f} < {ADX_MIN:.0f})")
    if adx >= ADX_OVEREXT or dist > DIST_OVEREXT:
        return hold(f"overextended (ADX {adx:.1f}, dist {dist:.2f}%) — reverts")
    if dist < DIST_MIN:
        return hold(f"too close to VWAP (dist {dist:.2f}% < {DIST_MIN:.2f}%) — no edge")

    action = "BUY_CALL" if up else "BUY_PUT"
    if up and _puts_only():
        return hold("puts-only mode: skipping long-call signal (robust side is puts)")
    sweet = ADX_SWEET_LO <= adx < ADX_SWEET_HI and DIST_MIN <= dist <= DIST_SWEET_HI
    conviction = "HIGH" if sweet else "MEDIUM"

    # S/R gate (v2): don't buy into the level just ahead. Fail-open: if market_ctx
    # carries no usable levels, _headroom returns None and the trade proceeds.
    hr, lvl = _headroom(price, action, _collect_levels(market_ctx))
    sr_note = ""
    if hr is not None:
        kind = "resistance" if action == "BUY_CALL" else "support"
        if hr < HEADROOM_MIN:
            return hold(f"{action} blocked — {kind} ${lvl:.2f} only {hr:.2f}% "
                        f"ahead (< {HEADROOM_MIN:.2f}%), no room to run")
        if hr < HEADROOM_COMFORT and conviction == "HIGH":
            conviction = "MEDIUM"  # tight room → don't size up
            sr_note = f", {kind} ${lvl:.2f} {hr:.2f}% ahead (capped)"
        else:
            sr_note = f", {hr:.2f}% to {kind} ${lvl:.2f}"

    strike = round(price)  # ATM reference; executor's select_best_strike refines it
    reason = (
        f"{ENGINE_VERSION}: {'UP' if up else 'DOWN'}-trend "
        f"(price {price:.2f} {'>' if up else '<'} vwap {vwap:.2f} & ema {ema:.2f}), "
        f"ADX {adx:.1f}, VWAP-dist {dist:.2f}%{sr_note} → {action}/{conviction}"
    )
    analysis = _build_analysis(action, snap, market_ctx or {})
    return TickerDecision(ticker, action, float(strike), "0DTE", conviction, reason, analysis)
