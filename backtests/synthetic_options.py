"""Black-Scholes pricing + synthetic option-chain generation.

Phase 2.4 uses these to drive the iron-condor backtest without needing
historical options data — we compute prices and deltas from the
underlying SPY price + a chosen implied volatility, then feed the
resulting `OptionQuote` list into the real `build_iron_condor`
strategy code.

Replaces stand: real Alpaca historical options chains can substitute
these later. The OptionQuote shape is identical so the strategy code
doesn't care about the source.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from math import erf, exp, log, sqrt

from integrations.alpaca_client import OptionQuote

# Reasonable default risk-free rate for 0DTE — doesn't materially affect strikes.
DEFAULT_RISK_FREE = 0.045
# Default IV proxy for SPY 0DTE. Real-world 16-delta short put strikes are
# typically ~0.5-1.0% OTM at this IV.
DEFAULT_IV = 0.18
# Bid/ask spread: % of mid. Conservative for liquid SPY 0DTE.
DEFAULT_SPREAD_PCT = 0.05


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erf — avoids the scipy dependency."""
    return 0.5 * (1 + erf(x / sqrt(2)))


def bs_call_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0:
        return max(0.0, S - K)
    if sigma <= 0:
        return max(0.0, S - K * exp(-r * T))
    d1 = (log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    return S * _norm_cdf(d1) - K * exp(-r * T) * _norm_cdf(d2)


def bs_put_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    # Put–call parity: P = C − S + K·e^(−rT)
    return bs_call_price(S, K, T, r, sigma) - S + K * exp(-r * T)


def bs_call_delta(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0:
        return 1.0 if S > K else 0.0
    if sigma <= 0:
        return 1.0 if S > K else 0.0
    d1 = (log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * sqrt(T))
    return _norm_cdf(d1)


def bs_put_delta(S: float, K: float, T: float, r: float, sigma: float) -> float:
    return bs_call_delta(S, K, T, r, sigma) - 1.0


def _occ_symbol(
    underlying: str, expiry: date, option_type: str, strike: float
) -> str:
    """Build a standard OCC 21-char symbol."""
    yy, mm, dd = expiry.year % 100, expiry.month, expiry.day
    letter = "C" if option_type == "call" else "P"
    pad = f"{int(round(strike * 1000)):08d}"
    return f"{underlying}{yy:02d}{mm:02d}{dd:02d}{letter}{pad}"


def generate_chain(
    *,
    spy_price: float,
    expiry: date,
    hours_to_expiry: float,
    iv: float = DEFAULT_IV,
    risk_free: float = DEFAULT_RISK_FREE,
    strike_step: float = 1.0,
    half_width: int = 25,
    spread_pct: float = DEFAULT_SPREAD_PCT,
    underlying: str = "SPY",
) -> list[OptionQuote]:
    """Synthesize a SPY options chain around the current spot.

    Returns puts AND calls at `half_width` strikes either side of ATM at
    `strike_step` spacing. Each quote has BS-implied mid plus a synthetic
    bid/ask centered on it (so build_iron_condor's mid-based credit math
    works as it does on live data).
    """
    T = max(hours_to_expiry, 0.0) / (24 * 365)  # calendar-day annualization
    atm_strike = round(spy_price)

    quotes: list[OptionQuote] = []
    for offset in range(-half_width, half_width + 1):
        strike = atm_strike + offset * strike_step
        if strike <= 0:
            continue
        for kind in ("call", "put"):
            if kind == "call":
                mid = bs_call_price(spy_price, strike, T, risk_free, iv)
                delta = bs_call_delta(spy_price, strike, T, risk_free, iv)
            else:
                mid = bs_put_price(spy_price, strike, T, risk_free, iv)
                delta = bs_put_delta(spy_price, strike, T, risk_free, iv)
            # Floor mid at $0.01 so very-far-OTM wings remain quotable
            # (real chains keep these listed even at minimum tick).
            mid = max(mid, 0.01)
            half = max(mid * spread_pct / 2, 0.01)
            bid = max(0.01, mid - half)
            ask = mid + half
            quotes.append(
                OptionQuote(
                    occ_symbol=_occ_symbol(underlying, expiry, kind, strike),
                    underlying=underlying,
                    strike=Decimal(str(strike)),
                    expiry=expiry,
                    option_type=kind,
                    bid=Decimal(f"{bid:.4f}"),
                    ask=Decimal(f"{ask:.4f}"),
                    mid=Decimal(f"{mid:.4f}"),
                    delta=Decimal(f"{delta:.4f}"),
                    gamma=None,
                    theta=None,
                    vega=None,
                    implied_volatility=Decimal(f"{iv:.4f}"),
                )
            )
    return quotes
