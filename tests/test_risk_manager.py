"""Risk manager tests.

Mocks the Alpaca account fetcher; uses in-memory SQLite for trades/risk_events.
Covers every rejection path enumerated in `validate_signal`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from integrations.alpaca_client import AccountSnapshot
from trademaster.config import get_settings
from trademaster.db import Base, RiskEvent, Trade, make_engine, make_session_factory
from trademaster.models import (
    AssetClass,
    OptionLeg,
    Side,
    Signal,
    SignalAction,
    TradeOrder,
)
from trademaster.risk_manager import (
    RiskRejectionError,
    _is_defined_risk,
    kill_all_positions,
    validate_account_is_cash,
    validate_signal,
)

# ----------------- fixtures -----------------


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _account(
    *,
    multiplier: str = "1",
    cash: str = "10000",
    status: str = "ACTIVE",
    account_blocked: bool = False,
    trading_blocked: bool = False,
) -> AccountSnapshot:
    return AccountSnapshot(
        account_number="abc",
        status=status,
        multiplier=multiplier,
        cash=Decimal(cash),
        buying_power=Decimal(cash),
        equity=Decimal(cash),
        portfolio_value=Decimal(cash),
        pattern_day_trader=False,
        trading_blocked=trading_blocked,
        account_blocked=account_blocked,
    )


def _equity_order(notional: str = "1500", qty: str = "10") -> TradeOrder:
    return TradeOrder(
        symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        side=Side.BUY,
        qty=Decimal(qty),
        limit_price=Decimal("150"),
        strategy="vwap_reclaim",
        notional_usd=Decimal(notional),
    )


def _iron_condor_order(qty: int = 1, notional: str = "380") -> TradeOrder:
    expiry = date(2024, 3, 15)
    legs = [
        OptionLeg(
            occ_symbol="SPY240315P00495000",
            side=Side.SELL, qty=qty, strike=Decimal("495"),
            expiry=expiry, option_type="put",
        ),
        OptionLeg(
            occ_symbol="SPY240315P00490000",
            side=Side.BUY, qty=qty, strike=Decimal("490"),
            expiry=expiry, option_type="put",
        ),
        OptionLeg(
            occ_symbol="SPY240315C00505000",
            side=Side.SELL, qty=qty, strike=Decimal("505"),
            expiry=expiry, option_type="call",
        ),
        OptionLeg(
            occ_symbol="SPY240315C00510000",
            side=Side.BUY, qty=qty, strike=Decimal("510"),
            expiry=expiry, option_type="call",
        ),
    ]
    return TradeOrder(
        symbol="SPY",
        asset_class=AssetClass.OPTION,
        side=Side.SELL,
        qty=Decimal("1"),
        strategy="spy_0dte_ic",
        notional_usd=Decimal(notional),
        legs=legs,
    )


def _open_signal(order: TradeOrder) -> Signal:
    return Signal(
        task_type="options_strategy",
        agent="options",
        action=SignalAction.OPEN,
        symbol=order.symbol,
        confidence=0.7,
        reasoning="test",
        order=order,
    )


async def _fetch(account: AccountSnapshot):
    async def f() -> AccountSnapshot:
        return account
    return f


# ----------------- _is_defined_risk -----------------


def test_defined_risk_accepts_iron_condor():
    ok, _ = _is_defined_risk(_iron_condor_order())
    assert ok


def test_defined_risk_rejects_naked_short_call():
    expiry = date(2024, 3, 15)
    order = TradeOrder(
        symbol="SPY",
        asset_class=AssetClass.OPTION,
        side=Side.SELL,
        qty=Decimal("1"),
        strategy="naked_call",
        notional_usd=Decimal("100"),
        legs=[
            OptionLeg(
                occ_symbol="SPY240315C00500000",
                side=Side.SELL, qty=1, strike=Decimal("500"),
                expiry=expiry, option_type="call",
            ),
        ],
    )
    ok, why = _is_defined_risk(order)
    assert not ok
    assert "naked" in why.lower()


def test_defined_risk_rejects_short_without_covering_long():
    expiry = date(2024, 3, 15)
    order = TradeOrder(
        symbol="SPY",
        asset_class=AssetClass.OPTION,
        side=Side.SELL,
        qty=Decimal("1"),
        strategy="mismatched",
        notional_usd=Decimal("100"),
        legs=[
            OptionLeg(
                occ_symbol="SPY240315C00500000",
                side=Side.SELL, qty=1, strike=Decimal("500"),
                expiry=expiry, option_type="call",
            ),
            # Long PUT (wrong option type — does not cover the short call)
            OptionLeg(
                occ_symbol="SPY240315P00495000",
                side=Side.BUY, qty=1, strike=Decimal("495"),
                expiry=expiry, option_type="put",
            ),
        ],
    )
    ok, why = _is_defined_risk(order)
    assert not ok
    assert "covering long leg" in why.lower()


def test_defined_risk_skips_for_equities():
    ok, _ = _is_defined_risk(_equity_order())
    assert ok


# ----------------- validate_account_is_cash -----------------


async def test_account_check_passes_for_cash_active(session_factory):
    account = await validate_account_is_cash(
        account_fetcher=await _fetch(_account()),
        session_factory=session_factory,
    )
    assert account.multiplier == "1"


async def test_account_check_rejects_margin(session_factory):
    with pytest.raises(RiskRejectionError) as exc:
        await validate_account_is_cash(
            account_fetcher=await _fetch(_account(multiplier="4")),
            session_factory=session_factory,
        )
    assert "multiplier" in str(exc.value).lower()
    with session_factory() as s:
        ev = s.query(RiskEvent).one()
        assert ev.event_type == "account_check_failed"
        assert ev.severity == "critical"


async def test_account_check_rejects_blocked(session_factory):
    with pytest.raises(RiskRejectionError):
        await validate_account_is_cash(
            account_fetcher=await _fetch(_account(trading_blocked=True)),
            session_factory=session_factory,
        )


async def test_account_check_rejects_non_active_status(session_factory):
    with pytest.raises(RiskRejectionError):
        await validate_account_is_cash(
            account_fetcher=await _fetch(_account(status="SUSPENDED")),
            session_factory=session_factory,
        )


# ----------------- validate_signal: gates -----------------


async def test_validate_skips_for_hold_signal(session_factory):
    signal = Signal(
        task_type="intraday_scan",
        agent="options",
        action=SignalAction.HOLD,
        reasoning="IV too low",
    )
    # Must not raise, must not write a risk_event.
    await validate_signal(signal, session_factory=session_factory)
    with session_factory() as s:
        assert s.query(RiskEvent).count() == 0


async def test_validate_skips_for_alert_only(session_factory):
    signal = Signal(
        task_type="pre_market_research",
        agent="research",
        action=SignalAction.ALERT_ONLY,
        reasoning="briefing",
    )
    await validate_signal(signal, session_factory=session_factory)
    with session_factory() as s:
        assert s.query(RiskEvent).count() == 0


# ----------------- validate_signal: rejection paths -----------------


async def test_reject_naked_option(session_factory):
    expiry = date(2024, 3, 15)
    naked = TradeOrder(
        symbol="SPY",
        asset_class=AssetClass.OPTION,
        side=Side.SELL,
        qty=Decimal("1"),
        strategy="naked",
        notional_usd=Decimal("100"),
        legs=[
            OptionLeg(
                occ_symbol="X", side=Side.SELL, qty=1, strike=Decimal("500"),
                expiry=expiry, option_type="call",
            ),
        ],
    )
    with pytest.raises(RiskRejectionError) as exc:
        await validate_signal(
            _open_signal(naked),
            account_fetcher=await _fetch(_account()),
            session_factory=session_factory,
        )
    assert "defined-risk" in str(exc.value).lower()


async def test_reject_over_max_position_size(session_factory):
    settings = get_settings()
    big = _equity_order(notional=str(settings.max_position_size_usd + 1))
    with pytest.raises(RiskRejectionError) as exc:
        await validate_signal(
            _open_signal(big),
            account_fetcher=await _fetch(_account()),
            session_factory=session_factory,
        )
    assert "max_position_size_usd" in str(exc.value).lower()


async def test_reject_over_max_options_contracts(session_factory):
    settings = get_settings()
    over = settings.max_options_contracts_per_trade + 1
    ic = _iron_condor_order(qty=over)
    with pytest.raises(RiskRejectionError) as exc:
        await validate_signal(
            _open_signal(ic),
            account_fetcher=await _fetch(_account()),
            session_factory=session_factory,
        )
    assert "max_options_contracts_per_trade" in str(exc.value).lower()


async def test_reject_at_max_concurrent_positions(session_factory):
    settings = get_settings()
    with session_factory() as s:
        for i in range(settings.max_concurrent_positions):
            s.add(
                Trade(
                    symbol=f"SYM{i}",
                    asset_class="equity",
                    side="buy",
                    strategy="x",
                    qty=Decimal("1"),
                    entry_price=Decimal("100"),
                )
            )
        s.commit()

    with pytest.raises(RiskRejectionError) as exc:
        await validate_signal(
            _open_signal(_equity_order()),
            account_fetcher=await _fetch(_account()),
            session_factory=session_factory,
        )
    assert "max_concurrent_positions" in str(exc.value).lower()


async def test_reject_when_daily_loss_limit_hit(session_factory):
    settings = get_settings()
    now = datetime.now(UTC)
    with session_factory() as s:
        s.add(
            Trade(
                symbol="SPY",
                asset_class="equity",
                side="buy",
                strategy="x",
                qty=Decimal("1"),
                entry_price=Decimal("100"),
                exit_price=Decimal("0"),
                realized_pnl_usd=-(settings.daily_loss_limit_usd + Decimal("10")),
                closed_at=now,
            )
        )
        s.commit()

    with pytest.raises(RiskRejectionError) as exc:
        await validate_signal(
            _open_signal(_equity_order()),
            account_fetcher=await _fetch(_account()),
            session_factory=session_factory,
            now=now,
        )
    assert "daily_loss_limit_usd" in str(exc.value).lower()


async def test_daily_loss_ignores_prior_day_loss(session_factory):
    """A big loss yesterday must not block today's trades."""
    settings = get_settings()
    yesterday = datetime.now(UTC) - timedelta(days=1)
    with session_factory() as s:
        s.add(
            Trade(
                symbol="SPY",
                asset_class="equity",
                side="buy",
                strategy="x",
                qty=Decimal("1"),
                entry_price=Decimal("100"),
                exit_price=Decimal("0"),
                realized_pnl_usd=-(settings.daily_loss_limit_usd + Decimal("100")),
                closed_at=yesterday,
            )
        )
        s.commit()

    # Should pass.
    await validate_signal(
        _open_signal(_equity_order()),
        account_fetcher=await _fetch(_account()),
        session_factory=session_factory,
    )


async def test_reject_when_runtime_account_is_margin(session_factory):
    with pytest.raises(RiskRejectionError) as exc:
        await validate_signal(
            _open_signal(_equity_order()),
            account_fetcher=await _fetch(_account(multiplier="4")),
            session_factory=session_factory,
        )
    assert "cash-only" in str(exc.value).lower()


async def test_reject_when_cash_insufficient(session_factory):
    with pytest.raises(RiskRejectionError) as exc:
        await validate_signal(
            _open_signal(_equity_order(notional="1500")),
            account_fetcher=await _fetch(_account(cash="100")),
            session_factory=session_factory,
        )
    assert "cash" in str(exc.value).lower()


# ----------------- validate_signal: approval path -----------------


async def test_approval_writes_risk_event(session_factory):
    await validate_signal(
        _open_signal(_iron_condor_order(qty=1, notional="380")),
        account_fetcher=await _fetch(_account(cash="5000")),
        session_factory=session_factory,
    )
    with session_factory() as s:
        rows = s.query(RiskEvent).all()
        assert len(rows) == 1
        assert rows[0].event_type == "approval"
        assert rows[0].severity == "info"


# ----------------- kill_all_positions -----------------


async def test_kill_switch_calls_both_apis_and_logs(session_factory):
    cancel_calls: list[int] = []
    close_calls: list[bool] = []

    async def fake_cancel():
        cancel_calls.append(1)
        return 3

    async def fake_close(cancel: bool):
        close_calls.append(cancel)
        return 2

    result = await kill_all_positions(
        cancel=fake_cancel,
        close=fake_close,
        session_factory=session_factory,
        reason="test",
    )
    assert result == {"orders_cancelled": 3, "positions_closed": 2}
    assert cancel_calls == [1]
    assert close_calls == [True]

    with session_factory() as s:
        ev = s.query(RiskEvent).one()
        assert ev.event_type == "kill_switch"
        assert ev.severity == "critical"
        assert ev.details["orders_cancelled"] == 3
        assert ev.details["positions_closed"] == 2
