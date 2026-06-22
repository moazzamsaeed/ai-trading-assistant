"""Wiring tests for the deterministic (LLM-free) iron-condor strategist.

Mocks SPY quote, chain, prior-day ADX, account, and executor — no external
calls, no LLM. Verifies the calm-day SELL path executes at engine strikes and
the trending-day path HOLDs.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from agents.options import strategist
from agents.options.executor import ExecutionResult
from integrations.alpaca_client import AccountSnapshot, OptionQuote, StockQuote
from trademaster.db import Base, make_engine, make_session_factory


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _opt(kind, strike, *, expiry=date(2026, 6, 19)):
    pad = f"{int(strike * 1000):08d}"
    letter = "C" if kind == "call" else "P"
    occ = f"SPY{expiry.year % 100:02d}{expiry.month:02d}{expiry.day:02d}{letter}{pad}"
    # price by distance from ATM (500): closer = richer, so shorts > wings → +credit
    mid = max(0.05, 2.0 - 0.2 * abs(strike - 500))
    b, a = Decimal(f"{mid - 0.05:.2f}"), Decimal(f"{mid + 0.05:.2f}")
    return OptionQuote(
        occ_symbol=occ, underlying="SPY", strike=Decimal(strike), expiry=expiry,
        option_type=kind, bid=b, ask=a, mid=Decimal(f"{mid:.2f}"),
        delta=Decimal("0.3"), gamma=Decimal("0.01"), theta=Decimal("-0.05"),
        vega=Decimal("0.10"), implied_volatility=Decimal("0.20"),
    )


def _chain():
    strikes = [488, 490, 492, 495, 497, 500, 503, 505, 508, 510, 512]
    return [_opt(k, s) for s in strikes for k in ("put", "call")]


def _spy_fetcher(mid="500"):
    async def f(_sym):
        return StockQuote(symbol="SPY", bid=Decimal(mid), ask=Decimal(mid),
                          mid=Decimal(mid), timestamp=datetime(2026, 6, 19, 14, tzinfo=UTC))
    return f


async def _chain_fetcher(_sym, **_kw):
    return _chain()


async def _daily_fetcher(_sym, **_kw):
    return []  # unused; ADX is monkeypatched


def _account(cash="10000"):
    return AccountSnapshot(
        account_number="x", status="ACTIVE", multiplier="1", cash=Decimal(cash),
        buying_power=Decimal(cash), equity=Decimal(cash), portfolio_value=Decimal(cash),
        pattern_day_trader=False, trading_blocked=False, account_blocked=False,
    )


async def _account_fetcher():
    return _account()


async def _fake_executor(plan, **_kw):
    return ExecutionResult(executed=True, reason="paper-filled", trade_id=1,
                           order=plan.to_trade_order())


NOW = datetime(2026, 6, 19, 14, 0, tzinfo=UTC)  # 10:00 ET


@pytest.mark.asyncio
async def test_calm_day_sells_condor(session_factory, monkeypatch):
    async def _adx(*_a, **_k):
        return 18.0  # calm
    monkeypatch.setattr(strategist, "_prior_day_adx", _adx)
    sig, signals_text, trade_text = await strategist.run_deterministic_condor(
        now=NOW, session_factory=session_factory,
        stock_fetcher=_spy_fetcher(), chain_fetcher=_chain_fetcher,
        daily_fetcher=_daily_fetcher, account_fetcher=_account_fetcher,
        executor=_fake_executor,
    )
    assert sig.action.value == "open"
    assert signals_text is not None and "SELL" in signals_text.upper()
    assert trade_text is not None and "EXECUTED" in trade_text
    assert sig.extra["engine"].startswith("vrp_condor")


@pytest.mark.asyncio
async def test_trending_day_holds(session_factory, monkeypatch):
    async def _adx(*_a, **_k):
        return 30.0  # trending → engine HOLDs
    monkeypatch.setattr(strategist, "_prior_day_adx", _adx)
    sig, signals_text, trade_text = await strategist.run_deterministic_condor(
        now=NOW, session_factory=session_factory,
        stock_fetcher=_spy_fetcher(), chain_fetcher=_chain_fetcher,
        daily_fetcher=_daily_fetcher, account_fetcher=_account_fetcher,
        executor=_fake_executor,
    )
    assert sig.action.value == "hold"
    assert signals_text is None and trade_text is None
    assert "trending" in sig.reasoning
