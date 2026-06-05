"""Effective-capital tests — account-tracking sizing.

Verifies that the 20% exposure cap, 15% daily loss limit, and 10% position
sizing all scale with realized account performance: losses shrink the pool,
gains grow it.

Examples from the user's spec:
- $5k start, $1k realized loss → next day's capital is $4k → 20% cap = $800
- Account grows to $10k → 20% cap = $2k
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from integrations import alpaca_client
from trademaster.capital import get_effective_capital
from trademaster.config import get_settings
from trademaster.db import Base, Trade, get_cumulative_realized_pnl, make_engine, make_session_factory


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _closed_trade(session_factory, pnl: float) -> None:
    with session_factory() as session:
        session.add(Trade(
            symbol="SPY", asset_class="option", side="buy", strategy="directional_call",
            qty=Decimal("1"), entry_price=Decimal("2"), exit_price=Decimal("3"),
            realized_pnl_usd=Decimal(str(pnl)),
            opened_at=datetime.now(UTC), closed_at=datetime.now(UTC),
        ))
        session.commit()


# ---------------------------------------------------------------------------
# get_cumulative_realized_pnl
# ---------------------------------------------------------------------------


def test_cumulative_realized_zero_when_no_trades(session_factory):
    assert get_cumulative_realized_pnl(session_factory) == Decimal("0")


def test_cumulative_realized_sums_all_closed_trades(session_factory):
    _closed_trade(session_factory, pnl=-200.00)
    _closed_trade(session_factory, pnl=350.50)
    _closed_trade(session_factory, pnl=-100.00)
    assert get_cumulative_realized_pnl(session_factory) == Decimal("50.50")


def test_cumulative_realized_ignores_open_trades(session_factory):
    # Open trade (no exit) → no realized_pnl_usd
    with session_factory() as s:
        s.add(Trade(
            symbol="SPY", asset_class="option", side="buy", strategy="directional_call",
            qty=Decimal("1"), entry_price=Decimal("2"),
            opened_at=datetime.now(UTC),
        ))
        s.commit()
    _closed_trade(session_factory, pnl=100.00)
    assert get_cumulative_realized_pnl(session_factory) == Decimal("100.00")


# ---------------------------------------------------------------------------
# Effective capital — paper mode
# ---------------------------------------------------------------------------


async def test_effective_capital_paper_no_trades_equals_base(session_factory, monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    get_settings.cache_clear()

    capital = await get_effective_capital(session_factory)
    assert capital == get_settings().trading_capital_usd  # base = $5k


async def test_effective_capital_paper_shrinks_with_loss(session_factory, monkeypatch):
    """User example: $5k start, $1k loss → $4k capital."""
    monkeypatch.setenv("TRADING_MODE", "paper")
    get_settings.cache_clear()

    _closed_trade(session_factory, pnl=-1000.00)

    capital = await get_effective_capital(session_factory)
    assert capital == Decimal("4000.00")


async def test_effective_capital_paper_grows_with_gain(session_factory, monkeypatch):
    """User example: account grows to $10k → cap = $2k.
    Starting from $5k, a +$5k cumulative gain gets us to $10k.
    """
    monkeypatch.setenv("TRADING_MODE", "paper")
    get_settings.cache_clear()

    _closed_trade(session_factory, pnl=3000.00)
    _closed_trade(session_factory, pnl=2000.00)

    capital = await get_effective_capital(session_factory)
    assert capital == Decimal("10000.00")
    # 20% of $10k = $2k
    cap_at_20pct = capital * Decimal("0.20")
    assert cap_at_20pct == Decimal("2000.00")


async def test_baseline_reset_excludes_pre_reset_trades(session_factory, monkeypatch):
    """Trades closed before baseline_reset_at don't drag the capital down."""
    monkeypatch.setenv("TRADING_MODE", "paper")
    get_settings.cache_clear()

    # Pre-reset huge loss
    with session_factory() as s:
        s.add(Trade(
            symbol="SPY", asset_class="option", side="buy", strategy="directional_call",
            qty=Decimal("1"), entry_price=Decimal("2"), exit_price=Decimal("0.5"),
            realized_pnl_usd=Decimal("-2000"),
            opened_at=datetime(2026, 5, 13, 14, 0, tzinfo=UTC),
            closed_at=datetime(2026, 5, 13, 20, 0, tzinfo=UTC),
        ))
        # Post-reset small loss
        s.add(Trade(
            symbol="SPY", asset_class="option", side="buy", strategy="directional_call",
            qty=Decimal("1"), entry_price=Decimal("2"), exit_price=Decimal("1.5"),
            realized_pnl_usd=Decimal("-50"),
            opened_at=datetime(2026, 5, 14, 14, 0, tzinfo=UTC),
            closed_at=datetime(2026, 5, 14, 20, 0, tzinfo=UTC),
        ))
        s.commit()

    # Reset baseline between the two trades — only the -$50 should count
    settings = get_settings()
    monkeypatch.setattr(
        settings, "baseline_reset_at",
        datetime(2026, 5, 14, 0, 0, tzinfo=UTC),
    )

    capital = await get_effective_capital(session_factory)
    assert capital == Decimal("4950.00"), f"Expected 5000 + (-50) = 4950, got {capital}"


async def test_directional_deployed_only_counts_directional(session_factory, monkeypatch):
    """`directional_deployed_usd` must skip non-directional rows (e.g. IC)."""
    from trademaster.capital import directional_deployed_usd

    with session_factory() as s:
        # Directional: $2 × 2 contracts × 100 = $400
        s.add(Trade(
            symbol="SPY260101C00500000", asset_class="option", side="buy",
            strategy="directional_call",
            qty=Decimal("2"), entry_price=Decimal("2"),
            opened_at=datetime.now(UTC),
        ))
        # Iron condor — should NOT be counted by this helper
        s.add(Trade(
            symbol="SPY_IC", asset_class="option", side="sell",
            strategy="spy_0dte_ic",
            qty=Decimal("1"), entry_price=Decimal("3"),
            opened_at=datetime.now(UTC),
            extra={"structure": "iron_condor", "max_loss_per_contract": "200"},
        ))
        s.commit()

    with session_factory() as s:
        total = directional_deployed_usd(s)
    assert total == Decimal("400")


async def test_directional_deployed_multiplies_options_by_100(session_factory):
    """Single long-option contract at $1.50 premium → $150 at risk, not $1.50."""
    from trademaster.capital import directional_deployed_usd

    with session_factory() as s:
        s.add(Trade(
            symbol="SPY260101C00500000", asset_class="option", side="buy",
            strategy="directional_put",
            qty=Decimal("1"), entry_price=Decimal("1.50"),
            opened_at=datetime.now(UTC),
        ))
        s.commit()

    with session_factory() as s:
        total = directional_deployed_usd(s)
    assert total == Decimal("150.00")


# ---------------------------------------------------------------------------
# End-to-end: loss → capital shrinks → next position sizing is smaller
# ---------------------------------------------------------------------------


async def test_integration_loss_shrinks_next_position(session_factory, monkeypatch):
    """Simulate a closed loss, then verify the NEXT executor call uses the
    new shrunken capital for sizing.
    """
    from datetime import date

    from agents.directional.executor import execute_directional_signal, _SelectedStrike
    from agents.directional.intraday import TickerDecision
    from integrations.alpaca_client import OptionQuote, OrderResult

    monkeypatch.setenv("TRADING_MODE", "paper")
    get_settings.cache_clear()

    # Day 1 closed at -$500
    _closed_trade(session_factory, pnl=-500.00)

    # Expected capital: 5000 - 500 = 4500. Sizing budget: 10% = $450.
    captured = {}

    async def strike_selector(_t, _e, _o, _target, budget):
        captured["budget"] = budget
        return _SelectedStrike(
            strike=Decimal("500"),
            occ="SPY260101C00500000",
            quote=OptionQuote(
                occ_symbol="SPY260101C00500000",
                underlying="SPY", strike=Decimal("500"),
                expiry=date(2026, 1, 1), option_type="call",
                bid=Decimal("1.50"), ask=Decimal("1.60"), mid=Decimal("1.55"),
                delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
            ),
        )

    async def fake_submit(**_k):
        return OrderResult(
            order_id="x", status="filled", filled_avg_price=Decimal("1.60"),
            filled_qty=Decimal("2"), submitted_at=datetime.now(UTC), raw_status="filled",
        )

    async def fake_wait(order_id, **_k):
        return await fake_submit()

    result = await execute_directional_signal(
        TickerDecision(
            ticker="SPY", action="BUY_CALL", strike=500.0, expiry="0DTE",
            conviction="HIGH", reasoning="t",
        ),
        today=date(2026, 1, 2),
        mode="selective",
        session_factory=session_factory,
        strike_selector=strike_selector,
        submitter=fake_submit,
        waiter=fake_wait,
    )

    assert result.executed
    # Full capital passed as budget (no per-trade fraction): $5,000 - $500 loss = $4,500
    assert captured["budget"] == 4500.0


async def test_executor_rejects_when_capital_is_zero(session_factory, monkeypatch):
    """Catastrophic loss → capital floored to $0 → no new trades."""
    from datetime import date
    from agents.directional.executor import execute_directional_signal
    from agents.directional.intraday import TickerDecision

    monkeypatch.setenv("TRADING_MODE", "paper")
    get_settings.cache_clear()

    # Total loss exceeds base by a lot
    _closed_trade(session_factory, pnl=-10_000.00)

    # The strike_selector must not be called at all
    async def must_not_be_called(*a, **k):
        raise AssertionError("strike_selector must not be invoked when capital=0")

    result = await execute_directional_signal(
        TickerDecision(
            ticker="SPY", action="BUY_CALL", strike=500.0, expiry="0DTE",
            conviction="HIGH", reasoning="t",
        ),
        today=date(2026, 1, 2),
        mode="selective",
        session_factory=session_factory,
        strike_selector=must_not_be_called,
    )

    assert not result.executed
    assert "$0" in result.reason or "0" in result.reason


async def test_effective_capital_paper_never_negative(session_factory, monkeypatch):
    """Losses larger than base shouldn't produce negative capital."""
    monkeypatch.setenv("TRADING_MODE", "paper")
    get_settings.cache_clear()

    _closed_trade(session_factory, pnl=-10000.00)  # bigger than the $5k base

    capital = await get_effective_capital(session_factory)
    assert capital == Decimal("0")


# ---------------------------------------------------------------------------
# Effective capital — live mode
# ---------------------------------------------------------------------------


async def test_effective_capital_live_uses_alpaca_equity(session_factory, monkeypatch):
    """Live: capital == Alpaca account.equity. DB realized P&L is irrelevant
    because the live account already reflects it via real money movement.
    """
    monkeypatch.setenv("TRADING_MODE", "live")
    get_settings.cache_clear()

    # Seed a $1k DB loss — should NOT affect live capital
    _closed_trade(session_factory, pnl=-1000.00)

    class FakeTrading:
        def __init__(self, **_): pass
        def get_account(self):
            return SimpleNamespace(
                account_number="A1", status="ACTIVE", multiplier="1",
                cash="4500", buying_power="9000", equity="4700",
                portfolio_value="4700",
                pattern_day_trader=False, trading_blocked=False, account_blocked=False,
            )

    monkeypatch.setattr(alpaca_client, "_trading_client", lambda: FakeTrading())

    capital = await get_effective_capital(session_factory)
    assert capital == Decimal("4700")

    # Restore paper default for downstream tests
    monkeypatch.setenv("TRADING_MODE", "paper")
    get_settings.cache_clear()


async def test_effective_capital_live_returns_zero_on_alpaca_error(
    session_factory, monkeypatch,
):
    """If Alpaca is unreachable in live mode, return $0 so the exposure cap
    blocks new trades. Falling back to a stale base could over-deploy if
    the real account is smaller than the configured base.
    """
    monkeypatch.setenv("TRADING_MODE", "live")
    get_settings.cache_clear()

    class BrokenTrading:
        def __init__(self, **_): pass
        def get_account(self): raise RuntimeError("alpaca timeout")

    monkeypatch.setattr(alpaca_client, "_trading_client", lambda: BrokenTrading())

    capital = await get_effective_capital(session_factory)
    assert capital == Decimal("0")

    monkeypatch.setenv("TRADING_MODE", "paper")
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# End-to-end scaling: position sizing uses dynamic capital
# ---------------------------------------------------------------------------


async def test_position_sizing_scales_with_capital(session_factory, monkeypatch):
    """After a $1k realized loss, capital shrinks to $4k — full $4k is the budget."""
    from agents.directional.executor import execute_directional_signal
    from agents.directional.intraday import TickerDecision
    from agents.directional.executor import _SelectedStrike
    from integrations.alpaca_client import OptionQuote, OrderResult
    from datetime import date

    monkeypatch.setenv("TRADING_MODE", "paper")
    get_settings.cache_clear()

    # Realized loss of $1k → capital is now $4k
    _closed_trade(session_factory, pnl=-1000.00)

    # Track what budget the strike selector received
    captured = {}

    async def strike_selector(_ticker, _expiry, _opt_type, _target, budget):
        captured["budget"] = budget
        return _SelectedStrike(
            strike=Decimal("500"),
            occ="SPY260101C00500000",
            quote=OptionQuote(
                occ_symbol="SPY260101C00500000",
                underlying="SPY", strike=Decimal("500"),
                expiry=date(2026, 1, 1), option_type="call",
                bid=Decimal("1.90"), ask=Decimal("2.00"), mid=Decimal("1.95"),
                delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
            ),
        )

    async def fake_submit(**_k):
        return OrderResult(
            order_id="x", status="filled",
            filled_avg_price=Decimal("2"), filled_qty=Decimal("1"),
            submitted_at=datetime.now(UTC), raw_status="filled",
        )

    async def fake_wait(order_id, **_k):
        return await fake_submit()

    decision = TickerDecision(
        ticker="SPY", action="BUY_CALL", strike=500.0, expiry="0DTE",
        conviction="HIGH", reasoning="test",
    )

    await execute_directional_signal(
        decision,
        today=date(2026, 1, 2),
        mode="aggressive",
        session_factory=session_factory,
        strike_selector=strike_selector,
        submitter=fake_submit,
        waiter=fake_wait,
    )

    # Full capital as budget — $5k - $1k loss = $4k (no per-trade fraction)
    assert captured["budget"] == 4000.00, f"Expected $4000 budget, got ${captured['budget']}"


# ---------------------------------------------------------------------------
# scale-out partials counted in realized P&L (governor fix, 2026-06-05)
# ---------------------------------------------------------------------------


def _trade_with_partial(session_factory, *, final_pnl, partial, closed=True):
    with session_factory() as s:
        s.add(Trade(
            symbol="SPY", asset_class="option", side="buy", strategy="directional_call",
            qty=Decimal("1"), entry_price=Decimal("1"),
            exit_price=Decimal("2") if closed else None,
            realized_pnl_usd=Decimal(str(final_pnl)) if closed else None,
            opened_at=datetime.now(UTC),
            closed_at=datetime.now(UTC) if closed else None,
            extra={"partial_realized_pnl_usd": str(partial)},
        ))
        s.commit()


def test_cumulative_includes_scale_out_partials(session_factory):
    # 100 final leg + 585 scale-out partial
    _trade_with_partial(session_factory, final_pnl=100.0, partial=585.0)
    assert get_cumulative_realized_pnl(session_factory) == Decimal("685.00")


def test_partials_counted_even_for_open_trade(session_factory):
    # An open trade that scaled out has realized partials the governor must see.
    _trade_with_partial(session_factory, final_pnl=0, partial=200.0, closed=False)
    assert get_cumulative_realized_pnl(session_factory) == Decimal("200.00")


def test_today_realized_includes_partials(session_factory):
    from trademaster.db import get_today_realized_pnl
    _trade_with_partial(session_factory, final_pnl=-50.0, partial=300.0)
    assert get_today_realized_pnl(session_factory) == Decimal("250.00")  # -50 + 300
