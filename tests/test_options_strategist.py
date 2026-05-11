"""Options strategist agent tests.

Mocks the SPY quote fetcher, options-chain fetcher, and the router so no
external calls happen. Verifies decision parsing, persistence, risk-manager
integration, and alert formatting.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from agents.options import strategist
from integrations.alpaca_client import AccountSnapshot, OptionQuote, StockQuote
from trademaster.db import Base, make_engine, make_session_factory
from trademaster.db import Signal as SignalRow
from trademaster.llm.types import LLMResponse


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


# ----------------- fixtures -----------------


def _opt(
    *,
    kind: str,
    strike: int,
    delta: str,
    bid: str,
    ask: str,
    expiry: date = date(2026, 5, 11),
    underlying: str = "SPY",
) -> OptionQuote:
    pad = f"{int(strike * 1000):08d}"
    yy, mm, dd = expiry.year % 100, expiry.month, expiry.day
    letter = "C" if kind == "call" else "P"
    occ = f"{underlying}{yy:02d}{mm:02d}{dd:02d}{letter}{pad}"
    b, a = Decimal(bid), Decimal(ask)
    return OptionQuote(
        occ_symbol=occ,
        underlying=underlying,
        strike=Decimal(strike),
        expiry=expiry,
        option_type=kind,
        bid=b,
        ask=a,
        mid=(b + a) / 2,
        delta=Decimal(delta),
        gamma=Decimal("0.01"),
        theta=Decimal("-0.05"),
        vega=Decimal("0.10"),
        implied_volatility=Decimal("0.20"),
    )


def _good_chain() -> list[OptionQuote]:
    """SPY @ ~$500, supports a clean 16-delta short with $5 wings."""
    items = [
        # Puts
        _opt(kind="put", strike=490, delta="-0.08", bid="0.25", ask="0.30"),
        _opt(kind="put", strike=495, delta="-0.16", bid="0.65", ask="0.70"),
        _opt(kind="put", strike=500, delta="-0.50", bid="2.50", ask="2.55"),
        # Calls
        _opt(kind="call", strike=500, delta="0.50", bid="2.55", ask="2.60"),
        _opt(kind="call", strike=505, delta="0.16", bid="0.60", ask="0.65"),
        _opt(kind="call", strike=510, delta="0.08", bid="0.20", ask="0.25"),
    ]
    return items


def _spy_quote() -> StockQuote:
    return StockQuote(
        symbol="SPY",
        bid=Decimal("499.95"),
        ask=Decimal("500.05"),
        mid=Decimal("500.00"),
        timestamp=datetime.now(UTC),
    )


def _llm_response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        provider="deepseek",
        model="deepseek-v4-pro",
        input_tokens=600,
        output_tokens=120,
        cost_usd=Decimal("0.000365"),
        duration_ms=1200,
    )


async def _stock(symbol: str) -> StockQuote:
    return _spy_quote()


async def _chain(*_args, **_kwargs) -> list[OptionQuote]:
    return _good_chain()


def _account(cash: str = "10000", multiplier: str = "1") -> AccountSnapshot:
    return AccountSnapshot(
        account_number="x",
        status="ACTIVE",
        multiplier=multiplier,
        cash=Decimal(cash),
        buying_power=Decimal(cash),
        equity=Decimal(cash),
        portfolio_value=Decimal(cash),
        pattern_day_trader=False,
        trading_blocked=False,
        account_blocked=False,
    )


async def _account_fetcher(account: AccountSnapshot):
    async def f() -> AccountSnapshot:
        return account
    return f


# ----------------- _parse_decision -----------------


def test_parse_decision_clean_open():
    decision, conf, reason = strategist._parse_decision(
        '{"decision": "OPEN", "confidence": 0.75, "reasoning": "Good IV"}'
    )
    assert decision == "OPEN"
    assert conf == 0.75
    assert reason == "Good IV"


def test_parse_decision_hold():
    decision, _, _ = strategist._parse_decision(
        '{"decision": "HOLD", "confidence": 0.3, "reasoning": "IV too low"}'
    )
    assert decision == "HOLD"


def test_parse_decision_strips_code_fence():
    decision, _, _ = strategist._parse_decision(
        '```json\n{"decision": "OPEN", "confidence": 0.6, "reasoning": "ok"}\n```'
    )
    assert decision == "OPEN"


def test_parse_decision_garbage_defaults_to_hold():
    decision, conf, reason = strategist._parse_decision("not json at all")
    assert decision == "HOLD"
    assert conf is None
    assert "unparseable" in reason.lower()


def test_parse_decision_invalid_decision_field_becomes_hold():
    decision, _, _ = strategist._parse_decision(
        '{"decision": "MAYBE", "confidence": 0.5, "reasoning": "uncertain"}'
    )
    assert decision == "HOLD"


# ----------------- _atm_iv -----------------


def test_atm_iv_averages_atm_options():
    iv = strategist._atm_iv(_good_chain(), Decimal("500"))
    assert iv == Decimal("0.20")


def test_atm_iv_returns_none_when_no_iv():
    chain = [
        OptionQuote(
            occ_symbol="SPY260511P00500000",
            underlying="SPY",
            strike=Decimal("500"),
            expiry=date(2026, 5, 11),
            option_type="put",
            bid=Decimal("1"),
            ask=Decimal("1.1"),
            mid=Decimal("1.05"),
            delta=Decimal("-0.5"),
            gamma=None,
            theta=None,
            vega=None,
            implied_volatility=None,
        )
    ]
    assert strategist._atm_iv(chain, Decimal("500")) is None


# ----------------- pipeline: HOLD path -----------------


async def test_strategist_hold_persists_no_alert(monkeypatch, session_factory):
    async def route(_task_type, _prompt, **_k):
        return _llm_response(
            '{"decision": "HOLD", "confidence": 0.4, "reasoning": "spreads too wide"}'
        )

    monkeypatch.setattr(strategist, "route_to_model", route)

    signal, signals_text, trade_text = await strategist.run_iron_condor_strategist(
        session_factory=session_factory,
        stock_fetcher=_stock,
        chain_fetcher=_chain,
    )
    assert signal.action.value == "hold"
    assert signals_text is None
    assert trade_text is None

    with session_factory() as s:
        row = s.query(SignalRow).one()
        assert row.action == "hold"
        assert "spreads" in row.reasoning


# ----------------- pipeline: build error path -----------------


async def test_strategist_records_hold_on_build_error(monkeypatch, session_factory):
    """Bad chain → IronCondorBuildError surfaces as a HOLD signal, no LLM call."""

    async def empty_chain(*_a, **_k) -> list[OptionQuote]:
        return []

    async def route(*_a, **_k):
        raise AssertionError("router should not be called when build fails")

    monkeypatch.setattr(strategist, "route_to_model", route)

    signal, signals_text, trade_text = await strategist.run_iron_condor_strategist(
        session_factory=session_factory,
        stock_fetcher=_stock,
        chain_fetcher=empty_chain,
    )
    assert signal.action.value == "hold"
    assert signals_text is None
    assert trade_text is None
    with session_factory() as s:
        row = s.query(SignalRow).one()
        assert "plan construction failed" in row.reasoning.lower()


# ----------------- pipeline: OPEN approved -----------------


async def test_strategist_open_approved_emits_alert(monkeypatch, session_factory):
    async def route(_task_type, _prompt, **_k):
        return _llm_response(
            '{"decision": "OPEN", "confidence": 0.7, '
            '"reasoning": "IV elevated, tight spreads"}'
        )

    async def fake_executor(plan, **_kwargs):
        from agents.options.executor import ExecutionResult
        return ExecutionResult(
            executed=True,
            order=None,
            trade_id=42,
            reason="filled at $80.00/contract",
        )

    monkeypatch.setattr(strategist, "route_to_model", route)
    fetch_account = await _account_fetcher(_account("10000"))

    signal, signals_text, trade_text = await strategist.run_iron_condor_strategist(
        session_factory=session_factory,
        stock_fetcher=_stock,
        chain_fetcher=_chain,
        account_fetcher=fetch_account,
        executor=fake_executor,
    )
    assert signal.action.value == "open"

    # Manual signal in #signals: plain-language buy/sell instructions, no jargon
    assert signals_text is not None
    assert "spy signals" in signals_text.lower() or "spy" in signals_text.lower()
    assert "$495 PUT" in signals_text  # short put strike
    assert "$510 CALL" in signals_text  # long call strike
    # Plain-language verbs, not "SELL_TO_OPEN" type jargon
    assert "Sell" in signals_text and "Buy" in signals_text
    # No iron-condor / credit-spread / profit-target jargon
    assert "iron condor" not in signals_text.lower()
    assert "credit spread" not in signals_text.lower()
    # Hold rule + close-by time in plain English
    assert "15:50 ET" in signals_text

    # Trade telemetry in #trades: execution status
    assert trade_text is not None
    assert "executed" in trade_text.lower()
    assert "#42" in trade_text

    with session_factory() as s:
        rows = s.query(SignalRow).all()
        assert len(rows) == 1
        assert rows[0].action == "open"
        assert rows[0].accepted is True


async def test_strategist_open_executor_failure_still_emits_alert(
    monkeypatch, session_factory
):
    """If submission fails post-approval, we still alert with the failure status."""
    async def route(_task_type, _prompt, **_k):
        return _llm_response(
            '{"decision": "OPEN", "confidence": 0.7, "reasoning": "go"}'
        )

    async def fake_executor(plan, **_kwargs):
        from agents.options.executor import ExecutionResult
        return ExecutionResult(
            executed=False,
            order=None,
            trade_id=None,
            reason="order ended in status=rejected",
        )

    monkeypatch.setattr(strategist, "route_to_model", route)
    fetch_account = await _account_fetcher(_account("10000"))

    _signal, signals_text, trade_text = await strategist.run_iron_condor_strategist(
        session_factory=session_factory,
        stock_fetcher=_stock,
        chain_fetcher=_chain,
        account_fetcher=fetch_account,
        executor=fake_executor,
    )
    # User should still see the manual signal — they may want to trade it themselves.
    assert signals_text is not None
    assert "spy" in signals_text.lower()
    assert "Sell" in signals_text  # plain-language buy/sell instructions
    # Trade telemetry must show the failure.
    assert trade_text is not None
    assert "not executed" in trade_text.lower()
    assert "rejected" in trade_text.lower()


# ----------------- pipeline: OPEN rejected by risk -----------------


async def test_strategist_open_rejected_by_risk_no_alert(monkeypatch, session_factory):
    async def route(_task_type, _prompt, **_k):
        return _llm_response(
            '{"decision": "OPEN", "confidence": 0.7, "reasoning": "good setup"}'
        )

    monkeypatch.setattr(strategist, "route_to_model", route)
    # Cash insufficient — risk manager will reject (max loss > cash).
    fetch_account = await _account_fetcher(_account("10"))

    signal, signals_text, trade_text = await strategist.run_iron_condor_strategist(
        session_factory=session_factory,
        stock_fetcher=_stock,
        chain_fetcher=_chain,
        account_fetcher=fetch_account,
    )
    assert signal.action.value == "open"  # the agent's decision
    # Risk rejection blocks BOTH outputs — don't post a signal we can't back.
    assert signals_text is None
    assert trade_text is None

    with session_factory() as s:
        row = s.query(SignalRow).one()
        assert row.action == "open"
        assert row.accepted is False
        assert row.rejection_reason is not None
