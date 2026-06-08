"""Tests for #trades reporting: close detail + daily/weekly summaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trademaster.db import (
    Base, Trade, get_closed_directional_trades, get_trade_detail,
    make_engine, make_session_factory,
)
from trademaster.reporting import format_trade_closed, format_trades_summary


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _closed(sf, *, tid_extra, pnl, partial=0.0, when=None):
    when = when or datetime.now(UTC)
    with sf() as s:
        t = Trade(
            symbol="SPY260605P00742000", asset_class="option", side="buy",
            strategy="directional_put", qty=Decimal("10"), entry_price=Decimal("1.10"),
            exit_price=Decimal("3.25"), realized_pnl_usd=Decimal(str(pnl)),
            opened_at=when, closed_at=when,
            extra={"ticker": "SPY", "action": "BUY_PUT", "mode": "aggressive",
                   "occ_symbol": "SPY260605P00742000", "original_qty": 20,
                   "peak_pnl_pct": 242.0, "exit_reason": "manual_close",
                   "partial_realized_pnl_usd": str(partial), **tid_extra},
        )
        s.add(t)
        s.commit()
        return t.id


def test_format_trade_closed_has_all_details():
    t = {
        "id": 50, "ticker": "SPY", "action": "BUY_PUT", "mode": "aggressive",
        "occ": "SPY260605P00742000", "original_qty": 20, "final_qty": 10,
        "entry_price": 1.10, "exit_price": 3.25, "final_pnl": 2150.0,
        "partial_pnl": 585.0, "total_pnl": 2735.0, "peak_pnl_pct": 242.0,
        "exit_reason": "manual_close",
        "opened_at": datetime(2026, 6, 5, 18, 15, tzinfo=UTC),
        "closed_at": datetime(2026, 6, 5, 19, 15, tzinfo=UTC),
    }
    out = format_trade_closed(t)
    assert "SPY PUT — CLOSED" in out
    assert "SPY260605P00742000" in out
    assert "$+2,735" in out                  # total incl partials
    assert "scale-outs $+585" in out
    assert "peak +242%" in out
    assert "manual_close" in out
    assert "held 1h 0m" in out


def test_format_trades_summary_table_and_net_includes_partials():
    trades = [
        {"id": 50, "action": "BUY_PUT", "original_qty": 20, "entry_price": 1.10,
         "exit_price": 3.25, "total_pnl": 2735.0, "exit_reason": "manual_close"},
        {"id": 48, "action": "BUY_PUT", "original_qty": 4, "entry_price": 1.12,
         "exit_price": 0.53, "total_pnl": -236.0, "exit_reason": "hard_floor_stop"},
    ]
    out = format_trades_summary(trades, title="Daily Trade Summary", period="2026-06-05")
    assert "Daily Trade Summary — 2026-06-05" in out
    assert "```" in out  # monospace table
    assert "net $+2,499" in out          # 2735 - 236
    assert "1W/1L" in out


def test_format_trades_summary_empty():
    assert "No trades" in format_trades_summary([], title="Daily Trade Summary", period="x")


def test_db_closed_trades_and_detail_roundtrip(session_factory):
    tid = _closed(session_factory, tid_extra={}, pnl=2150.0, partial=585.0)
    detail = get_trade_detail(session_factory, tid)
    assert detail["total_pnl"] == 2735.0      # final 2150 + partial 585
    assert detail["original_qty"] == 20
    assert detail["occ"] == "SPY260605P00742000"

    start = datetime.now(UTC) - timedelta(hours=1)
    end = datetime.now(UTC) + timedelta(hours=1)
    rows = get_closed_directional_trades(session_factory, start=start, end=end)
    assert len(rows) == 1 and rows[0]["total_pnl"] == 2735.0
