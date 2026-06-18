"""Unit tests for the deterministic exit confirm (_rules_exit_confirm) — the
LLM-free replacement for the `smart_exit` judge that was the biggest P&L drain.

Rule: exit on a confluence of fading-momentum signals (≥2). Selective mode also
exits on a single fade while in profit (protect gains). A single fade on a loser
HOLDs — the over-cutting fix.
"""
from agents.directional.exit_monitor import _rules_exit_confirm

CALL_FADE = ["price_below_vwap", "ema_bearish_cross"]   # 2 invalidation signals
ONE_FADE = ["volume_fading"]                            # 1 invalidation signal
NON_INVAL = ["rsi_overbought"]                          # fired rule, NOT an invalidation signal


def test_confluence_two_signals_exits_aggressive():
    go, reason = _rules_exit_confirm("BUY_CALL", CALL_FADE, pnl_pct=-3.0, mode="aggressive")
    assert go is True and "confluence" in reason


def test_single_signal_on_loser_holds_aggressive():
    # THE over-cutting fix: 1 fade on a losing trade → HOLD (was the LLM's bleed)
    go, _ = _rules_exit_confirm("BUY_CALL", ONE_FADE, pnl_pct=-4.0, mode="aggressive")
    assert go is False


def test_single_signal_in_profit_holds_aggressive():
    # aggressive lets winners run — a single fade is noise
    go, _ = _rules_exit_confirm("BUY_CALL", ONE_FADE, pnl_pct=+20.0, mode="aggressive")
    assert go is False


def test_single_signal_in_profit_exits_selective():
    go, reason = _rules_exit_confirm("BUY_CALL", ONE_FADE, pnl_pct=+15.0, mode="selective")
    assert go is True and "protect gains" in reason


def test_single_signal_at_loss_holds_selective():
    go, _ = _rules_exit_confirm("BUY_CALL", ONE_FADE, pnl_pct=-5.0, mode="selective")
    assert go is False


def test_non_invalidation_signal_does_not_count():
    # rsi_overbought is a profit-taking flag, not a thesis-break; alone it must not exit
    go, _ = _rules_exit_confirm("BUY_CALL", NON_INVAL, pnl_pct=+30.0, mode="aggressive")
    assert go is False


def test_no_signals_holds_even_in_strong_profit():
    # no fade → ride the trailing stop, don't proactively cut
    go, _ = _rules_exit_confirm("BUY_PUT", [], pnl_pct=+90.0, mode="aggressive")
    assert go is False


def test_put_confluence_exits():
    go, _ = _rules_exit_confirm("BUY_PUT", ["price_above_vwap", "volume_fading"],
                                pnl_pct=-2.0, mode="aggressive")
    assert go is True
