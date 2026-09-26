"""Weekly #trades summary must cover the condor, not just directional.

Regression: the job queried directional trades ONLY, so once directional went
signals-only (2026-09-04) every Friday posted "No trades" — including the week
of 2026-09-21, which realized +$2,968 on the condor.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from trademaster import scheduler


def _condor(tid: int, credit: float, qty: int, pnl: float) -> dict:
    """A closed (already booked) condor row as get_condor_trades returns it."""
    return {
        "id": tid,
        "credit": credit,
        "qty": qty,
        "closed_at": datetime(2026, 9, 25, 20, 0, tzinfo=UTC),
        "realized_pnl_usd": pnl,
        "exit_reason": "expired_settled_legout",
    }


@pytest.fixture
def posted(monkeypatch):
    """Run the weekly job with stubbed data; return what it posted."""
    out: list[str] = []

    async def _poster(text: str) -> None:
        out.append(text)

    def _run(*, directional: list[dict], condors: list[dict]) -> str:
        import trademaster.db as db

        monkeypatch.setattr(scheduler, "make_session_factory", lambda: (lambda: None))
        monkeypatch.setattr(
            db, "get_closed_directional_trades", lambda *a, **k: directional
        )
        monkeypatch.setattr(db, "get_condor_trades", lambda *a, **k: condors)
        out.clear()
        asyncio.run(scheduler._weekly_summary_job(trade_poster=_poster))
        assert len(out) == 1, "weekly summary should post exactly one message"
        return out[0]

    return _run


def test_condor_only_week_reports_the_condor(posted):
    """The exact bug: condors traded, directional silent -> must NOT say 'No trades'."""
    msg = posted(
        directional=[],
        condors=[
            _condor(177, 41, 28, 1036),
            _condor(178, 40, 28, 896),
            _condor(179, 41, 28, 1036),
        ],
    )
    assert "No trades" not in msg
    assert "Iron Condor" in msg
    # 1036 + 896 + 1036
    assert "+2,968" in msg
    for tid in ("177", "178", "179"):
        assert tid in msg


def test_losing_condor_week_nets_negative(posted):
    msg = posted(directional=[], condors=[_condor(173, 59, 28, -6020)])
    assert "-6,020" in msg
    assert "🔴" in msg


def test_genuinely_empty_week_still_reports(posted):
    """A week with nothing at all means something was down — say so, don't go silent."""
    msg = posted(directional=[], condors=[])
    assert "No trades" in msg
    assert "Weekly Trade Summary" in msg


def test_both_strategies_appear_when_both_traded(posted):
    directional = [{
        "id": 42, "action": "BUY_CALL", "original_qty": 2,
        "entry_price": 1.5, "exit_price": 2.0, "total_pnl": 100.0,
        "exit_reason": "target",
    }]
    msg = posted(directional=directional, condors=[_condor(179, 41, 28, 1036)])
    assert "Weekly Trade Summary" in msg
    assert "Iron Condor" in msg
    assert "42" in msg and "179" in msg
