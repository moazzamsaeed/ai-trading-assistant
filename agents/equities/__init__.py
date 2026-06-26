"""Isolated equities signal scanner (alert-only).

Monitors a separate stock/ETF watchlist with the EXISTING deterministic trend
engine and posts plain-language buy-call/buy-put signals to a dedicated Discord
channel. Fully separate from the live SPY condor/trend strategies: no execution,
no shared capital/risk/positions.
"""
