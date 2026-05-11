"""Black-Scholes pricing, delta, and implied-volatility solver.

Shared between the backtest harness (synthetic chain generation) and the
live `integrations/alpaca_client` chain enricher (which fills in greeks
when the Alpaca options feed omits them — our subscription returns
prices but no greeks/IV on the indicative feed).

No scipy: `math.erf` covers the normal CDF, bisection covers IV inversion.
"""

from __future__ import annotations

from math import erf, exp, log, sqrt

DEFAULT_RISK_FREE = 0.045


def norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erf."""
    return 0.5 * (1 + erf(x / sqrt(2)))


def bs_call_price(S: float, K: float, T: float, r: float, sigma: float) -> float:  # noqa: N803
    if T <= 0:
        return max(0.0, S - K)
    if sigma <= 0:
        return max(0.0, S - K * exp(-r * T))
    d1 = (log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    return S * norm_cdf(d1) - K * exp(-r * T) * norm_cdf(d2)


def bs_put_price(S: float, K: float, T: float, r: float, sigma: float) -> float:  # noqa: N803
    return bs_call_price(S, K, T, r, sigma) - S + K * exp(-r * T)


def bs_call_delta(S: float, K: float, T: float, r: float, sigma: float) -> float:  # noqa: N803
    if T <= 0:
        return 1.0 if S > K else 0.0
    if sigma <= 0:
        return 1.0 if S > K else 0.0
    d1 = (log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * sqrt(T))
    return norm_cdf(d1)


def bs_put_delta(S: float, K: float, T: float, r: float, sigma: float) -> float:  # noqa: N803
    return bs_call_delta(S, K, T, r, sigma) - 1.0


def solve_implied_vol(
    *,
    target_price: float,
    S: float,  # noqa: N803
    K: float,  # noqa: N803
    T: float,  # noqa: N803
    r: float,
    option_type: str,
    lo: float = 0.005,
    hi: float = 5.0,
    iterations: int = 60,
) -> float | None:
    """Solve for σ such that the BS model price matches `target_price`.

    Bisection in [lo, hi] over the BS price function (which is monotone
    in σ). Returns None if `target_price` is outside the achievable range
    (e.g., below intrinsic value or above the bound at σ=hi).
    """
    if target_price <= 0:
        return None
    price_fn = bs_call_price if option_type == "call" else bs_put_price
    p_lo = price_fn(S, K, T, r, lo)
    p_hi = price_fn(S, K, T, r, hi)
    if not (p_lo <= target_price <= p_hi):
        return None
    for _ in range(iterations):
        mid = (lo + hi) / 2
        p_mid = price_fn(S, K, T, r, mid)
        if p_mid < target_price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def delta_from_market_mid(
    *,
    market_mid: float,
    S: float,  # noqa: N803
    K: float,  # noqa: N803
    T: float,  # noqa: N803
    option_type: str,
    r: float = DEFAULT_RISK_FREE,
) -> tuple[float, float] | None:
    """Convenience: solve IV from `market_mid`, then return (delta, iv).

    Returns None if IV can't be solved (e.g., far-OTM option whose mid is
    pinned at $0.01 minimum tick — those have no meaningful delta).
    """
    iv = solve_implied_vol(
        target_price=market_mid, S=S, K=K, T=T, r=r, option_type=option_type,
    )
    if iv is None:
        return None
    delta_fn = bs_call_delta if option_type == "call" else bs_put_delta
    return delta_fn(S, K, T, r, iv), iv
