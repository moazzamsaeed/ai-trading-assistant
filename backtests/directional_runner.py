"""Directional options backtest — rule-based, synthetic GBM paths.

One trade per day (first qualifying signal). Exit logic: profit target,
stop loss, or force-close N minutes before market close.

Rule decision (no LLM — burns budget):
  BUY_CALL : price > VWAP  AND  EMA20 > EMA50*  AND  RSI in [40,65]  AND  vol_ratio > 1.3
  BUY_PUT  : price < VWAP  AND  EMA20 < EMA50*  AND  RSI in [35,60]  AND  vol_ratio > 1.3
  (*) EMA50 condition skipped when fewer than 50 bars are available.

This tests whether the *rule structure* has edge. The LLM version should beat
rule-only. If rule shows −3% expectancy, the strategy is broken.

CLI: python -m backtests.directional_cli
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from random import Random

import pandas as pd

from backtests.price_paths import PriceTick, generate_gbm_path
from integrations.alpaca_client import Bar
from trademaster import indicators
from trademaster.options_math import DEFAULT_RISK_FREE, bs_call_price, bs_put_price

SESSION_MINUTES = 375  # 9:45–16:00 ET
SESSION_START = time(9, 45)
MINUTES_PER_YEAR = 252 * SESSION_MINUTES  # 94 500 trading minutes / year

# Minimum bars before first scan — ensures RSI(14), EMA(20), and vol_ratio(20) are ready.
MIN_BARS_TO_SCAN = 24  # bar index 0-based; at idx=24 we have 25 bars


@dataclass(frozen=True)
class DirectionalBacktestConfig:
    start_date: date
    end_date: date
    mode: str = "aggressive"  # "aggressive" | "selective"
    spy_start: float = 500.0
    annual_vol: float = 0.15
    annual_drift: float = 0.0
    iv: float = 0.18
    seed: int = 42
    position_size_usd: float = 750.0  # $ risked per trade
    profit_target_pct: float = 1.0  # 100% gain on premium
    stop_loss_pct: float = 0.50  # 50% loss on premium
    force_close_mins_before_close: int = 30
    scan_interval_min: int = 10
    bar_size_min: int = 5


@dataclass
class DirectionalTradeResult:
    sim_date: date
    entered: bool
    action: str  # BUY_CALL | BUY_PUT | no_signal
    entry_minute: int | None
    entry_spy: float | None
    strike: float | None
    entry_premium: float | None
    exit_minute: int | None
    exit_spy: float | None
    exit_premium: float | None
    exit_reason: str  # profit_target | stop_loss | force_close | no_entry
    pnl_pct: float
    pnl_usd: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ticks_to_bars(
    ticks: list[PriceTick],
    bar_size_min: int = 5,
    volumes: list[int] | None = None,
) -> list[Bar]:
    """Aggregate 1-min GBM ticks into OHLCV bars."""
    bars: list[Bar] = []
    bar_idx = 0
    for i in range(0, len(ticks) - 1, bar_size_min):
        chunk = ticks[i : i + bar_size_min]
        if len(chunk) < bar_size_min:
            break
        prices = [t.price for t in chunk]
        vol = volumes[bar_idx] if (volumes and bar_idx < len(volumes)) else 10_000
        avg_price = sum(prices) / len(prices)
        bars.append(
            Bar(
                timestamp=chunk[0].timestamp,
                open=Decimal(f"{prices[0]:.2f}"),
                high=Decimal(f"{max(prices):.2f}"),
                low=Decimal(f"{min(prices):.2f}"),
                close=Decimal(f"{prices[-1]:.2f}"),
                volume=vol,
                vwap=Decimal(f"{avg_price:.2f}"),
            )
        )
        bar_idx += 1
    return bars


def _option_price(
    action: str,
    spy: float,
    strike: float,
    minutes_remaining: int,
    iv: float,
) -> float:
    T = max(minutes_remaining, 1) / MINUTES_PER_YEAR
    r = DEFAULT_RISK_FREE
    if action == "BUY_CALL":
        return bs_call_price(spy, strike, T, r, iv)
    return bs_put_price(spy, strike, T, r, iv)


def _choose_strike(spy: float, action: str, mode: str) -> float:
    atm = round(spy)
    if mode == "aggressive":
        return float(atm)
    # selective: 1 strike OTM
    if action == "BUY_CALL":
        return float(atm + 1)
    return float(atm - 1)


def _rule_decision(bars: list[Bar]) -> str:
    snap = indicators.snapshot(bars)

    price_s = snap.get("last_close")
    vwap_s = snap.get("vwap")
    rsi_s = snap.get("rsi14")
    ema20_s = snap.get("ema20")
    ema50_s = snap.get("ema50")
    vol_s = snap.get("volume_ratio_20")

    if not all([price_s, vwap_s, rsi_s, ema20_s, vol_s]):
        return "HOLD"

    price = float(price_s)
    vwap = float(vwap_s)
    rsi = float(rsi_s)
    ema20 = float(ema20_s)
    ema50 = float(ema50_s) if ema50_s else None
    vol = float(vol_s)

    bullish = (
        price > vwap
        and 40.0 <= rsi <= 65.0
        and (ema50 is None or ema20 > ema50)
        and vol > 1.3
    )
    bearish = (
        price < vwap
        and 35.0 <= rsi <= 60.0
        and (ema50 is None or ema20 < ema50)
        and vol > 1.3
    )

    if bullish:
        return "BUY_CALL"
    if bearish:
        return "BUY_PUT"
    return "HOLD"


def _make_result(
    sim_date: date,
    position: dict,
    exit_bar: Bar,
    exit_minute: int,
    exit_premium: float,
    exit_reason: str,
    cfg: DirectionalBacktestConfig,
) -> DirectionalTradeResult:
    entry_premium = position["entry_premium"]
    pnl_pct = (exit_premium - entry_premium) / entry_premium
    contracts = max(1, int(cfg.position_size_usd / (entry_premium * 100)))
    pnl_usd = contracts * 100 * (exit_premium - entry_premium)
    return DirectionalTradeResult(
        sim_date=sim_date,
        entered=True,
        action=position["action"],
        entry_minute=position["entry_minute"],
        entry_spy=position["entry_spy"],
        strike=position["strike"],
        entry_premium=round(entry_premium, 4),
        exit_minute=exit_minute,
        exit_spy=round(float(exit_bar.close), 2),
        exit_premium=round(exit_premium, 4),
        exit_reason=exit_reason,
        pnl_pct=round(pnl_pct, 4),
        pnl_usd=round(pnl_usd, 2),
    )


# ---------------------------------------------------------------------------
# Core simulation
# ---------------------------------------------------------------------------


def simulate_directional_day(
    *,
    sim_date: date,
    price_path: list[PriceTick],
    cfg: DirectionalBacktestConfig,
    volumes: list[int] | None = None,
) -> DirectionalTradeResult:
    """Simulate one RTH session. Returns one trade result (one position max per day)."""
    bars = _ticks_to_bars(price_path, bar_size_min=cfg.bar_size_min, volumes=volumes)
    force_close_bar = (SESSION_MINUTES - cfg.force_close_mins_before_close) // cfg.bar_size_min
    scan_every = max(1, cfg.scan_interval_min // cfg.bar_size_min)

    position: dict | None = None

    for bar_idx, bar in enumerate(bars):
        current_minute = bar_idx * cfg.bar_size_min
        minutes_rem = SESSION_MINUTES - current_minute

        # Force-close gate
        if position and bar_idx >= force_close_bar:
            exit_p = max(0.01, _option_price(
                position["action"], float(bar.close), position["strike"], minutes_rem, cfg.iv
            ))
            return _make_result(sim_date, position, bar, current_minute, exit_p, "force_close", cfg)

        # Exit check for open position
        if position:
            current_p = max(0.01, _option_price(
                position["action"], float(bar.close), position["strike"], minutes_rem, cfg.iv
            ))
            pnl_pct = (current_p - position["entry_premium"]) / position["entry_premium"]
            if pnl_pct >= cfg.profit_target_pct:
                return _make_result(sim_date, position, bar, current_minute, current_p, "profit_target", cfg)
            if pnl_pct <= -cfg.stop_loss_pct:
                return _make_result(sim_date, position, bar, current_minute, current_p, "stop_loss", cfg)

        # Signal scan (no position, warmed up, at scan cadence)
        if position is None and bar_idx >= MIN_BARS_TO_SCAN and bar_idx % scan_every == 0:
            action = _rule_decision(bars[: bar_idx + 1])
            if action != "HOLD":
                spy = float(bar.close)
                strike = _choose_strike(spy, action, cfg.mode)
                entry_p = _option_price(action, spy, strike, minutes_rem, cfg.iv)
                if entry_p >= 0.01:
                    position = {
                        "action": action,
                        "entry_minute": current_minute,
                        "entry_spy": spy,
                        "strike": strike,
                        "entry_premium": entry_p,
                    }

    # Session ended with open position — force close at last bar
    if position:
        last_bar = bars[-1]
        last_minute = (len(bars) - 1) * cfg.bar_size_min
        exit_p = max(0.01, _option_price(
            position["action"], float(last_bar.close), position["strike"],
            max(1, SESSION_MINUTES - last_minute), cfg.iv,
        ))
        return _make_result(sim_date, position, last_bar, last_minute, exit_p, "force_close", cfg)

    return DirectionalTradeResult(
        sim_date=sim_date,
        entered=False,
        action="no_signal",
        entry_minute=None,
        entry_spy=None,
        strike=None,
        entry_premium=None,
        exit_minute=None,
        exit_spy=None,
        exit_premium=None,
        exit_reason="no_entry",
        pnl_pct=0.0,
        pnl_usd=0.0,
    )


# ---------------------------------------------------------------------------
# Multi-day runner
# ---------------------------------------------------------------------------


def _trading_days(start: date, end: date) -> list[date]:
    """Mon–Fri only (no holiday filter — synthetic backtest)."""
    out: list[date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def run_directional_backtest(
    cfg: DirectionalBacktestConfig,
) -> tuple[list[DirectionalTradeResult], pd.DataFrame]:
    """Run the backtest day by day. Returns (raw results, summary DataFrame)."""
    days = _trading_days(cfg.start_date, cfg.end_date)
    results: list[DirectionalTradeResult] = []
    spy_price = cfg.spy_start

    for i, d in enumerate(days):
        path = generate_gbm_path(
            start_price=spy_price,
            minutes=SESSION_MINUTES,
            annual_vol=cfg.annual_vol,
            annual_drift=cfg.annual_drift,
            seed=cfg.seed + i,
            start_time=datetime.combine(d, SESSION_START),
        )
        # Synthetic volumes: lognormal-ish so vol_ratio can exceed 1.3.
        vol_rng = Random(cfg.seed + i * 997 + 1)
        n_bars = SESSION_MINUTES // cfg.bar_size_min
        volumes = [max(100, int(vol_rng.gauss(10_000, 4_000))) for _ in range(n_bars)]

        r = simulate_directional_day(
            sim_date=d, price_path=path, cfg=cfg, volumes=volumes
        )
        results.append(r)
        spy_price = path[-1].price

    df = results_to_dataframe(results)
    return results, df


def results_to_dataframe(results: list[DirectionalTradeResult]) -> pd.DataFrame:
    rows = [
        {
            "date": r.sim_date,
            "entered": r.entered,
            "action": r.action,
            "entry_minute": r.entry_minute,
            "entry_spy": r.entry_spy,
            "strike": r.strike,
            "entry_premium": r.entry_premium,
            "exit_minute": r.exit_minute,
            "exit_spy": r.exit_spy,
            "exit_premium": r.exit_premium,
            "exit_reason": r.exit_reason,
            "pnl_pct": r.pnl_pct,
            "pnl_usd": r.pnl_usd,
        }
        for r in results
    ]
    return pd.DataFrame(rows)


def compute_directional_stats(df: pd.DataFrame) -> dict:
    n_total = len(df)
    if n_total == 0:
        return {"n_days": 0, "n_entered": 0, "win_rate": 0.0,
                "avg_win_usd": 0.0, "avg_loss_usd": 0.0,
                "expectancy_usd": 0.0, "total_pnl_usd": 0.0, "max_drawdown_usd": 0.0}

    entered = df[df["entered"]]
    n_entered = len(entered)
    if n_entered == 0:
        return {"n_days": n_total, "n_entered": 0, "win_rate": 0.0,
                "avg_win_usd": 0.0, "avg_loss_usd": 0.0,
                "expectancy_usd": 0.0, "total_pnl_usd": 0.0, "max_drawdown_usd": 0.0}

    wins = entered[entered["pnl_usd"] > 0]
    losses = entered[entered["pnl_usd"] <= 0]
    win_rate = len(wins) / n_entered
    avg_win = float(wins["pnl_usd"].mean()) if len(wins) > 0 else 0.0
    avg_loss = float(losses["pnl_usd"].mean()) if len(losses) > 0 else 0.0
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
    total_pnl = float(entered["pnl_usd"].sum())

    equity = entered["pnl_usd"].cumsum()
    running_max = equity.cummax()
    max_dd = float((equity - running_max).min()) if len(equity) > 0 else 0.0

    reason_counts = entered["exit_reason"].value_counts().to_dict()

    return {
        "n_days": n_total,
        "n_entered": n_entered,
        "win_rate": round(win_rate, 4),
        "avg_win_usd": round(avg_win, 2),
        "avg_loss_usd": round(avg_loss, 2),
        "expectancy_usd": round(expectancy, 2),
        "total_pnl_usd": round(total_pnl, 2),
        "max_drawdown_usd": round(max_dd, 2),
        "exit_reasons": {k: int(v) for k, v in reason_counts.items()},
    }
