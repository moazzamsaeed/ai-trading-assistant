"""Monday pre-open dry-run for the deterministic VRP condor.

Run AFTER 9:30 ET (market open) but BEFORE the 10:00 ET entry job, to validate the
live path the backtest can't:

  READ-ONLY (default):
    1. Market clock — confirm open.
    2. SPY quote → spot.
    3. Prior-day Wilder ADX (the regime gate input).
    4. 0DTE chain fetch + ⭐ GREEKS/IV CHECK — does the feed carry IV natively, or
       must we BS-invert? (memory: indicative feed lacks greeks). Reports counts
       before/after _enrich_chain_with_bs_greeks.
    5. Live VIX1D via vix1d_from_chain — the no-live-feed solution; sanity-check it.
    6. decide_condor() — what would the engine do right now?
    7. If it would trade, build the condor from the LIVE chain (strikes/credit/max-loss).

  EXECUTION PROBE (--submit):
    8. Build a test condor near ATM and submit a NON-MARKETABLE MLEG order (limit
       credit set 3× above market so it will NOT fill), confirm the account ACCEPTS
       a 4-leg defined-risk order at Level 3, then immediately CANCEL it. Proves
       the account executes MLEG without opening a position. Paper only.

Usage:
  uv run python -m scripts.condor_preopen_check            # read-only
  uv run python -m scripts.condor_preopen_check --submit   # + live MLEG probe (paper)
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from agents.options.condor_engine import decide_condor, vix1d_from_chain
from agents.options.strategist import (
    CHAIN_HALF_WIDTH, UNDERLYING, _enrich_chain_with_bs_greeks, _prior_day_adx,
)
from integrations import alpaca_client
from strategies.spy_0dte_iron_condor import IronCondorBuildError, build_condor_at_strikes
from trademaster.config import get_settings
from trademaster.timeutils import to_et


def _line(label, value):
    print(f"  {label:32} {value}")


async def main(submit: bool) -> None:
    now = datetime.now(UTC)
    s = get_settings()
    print("\n═══ CONDOR PRE-OPEN DRY-RUN ═══")
    _line("mode", s.trading_mode)
    _line("now (ET)", to_et(now).strftime("%Y-%m-%d %H:%M:%S ET"))
    if s.trading_mode != "paper":
        print("  ⚠️  NOT paper mode — refusing the --submit probe.") if submit else None
        submit = False

    # 1. clock
    try:
        clock = await alpaca_client.get_market_clock()
        _line("market open?", clock.is_open)
        if not clock.is_open:
            print("  ⚠️  Market CLOSED — chain/quotes may be stale. Run after 9:30 ET.")
    except Exception as e:  # noqa: BLE001
        _line("clock ERROR", repr(e))

    # 2. SPY quote
    spy = await alpaca_client.get_latest_stock_quote(UNDERLYING)
    spot_dec = spy.mid if spy.mid and spy.mid > 0 else (spy.bid or spy.ask)
    spot = float(spot_dec)
    _line("SPY spot", f"${spot:.2f}")

    # 3. prior-day ADX
    adx = await _prior_day_adx(alpaca_client.get_daily_bars, now=now)
    _line("prior-day ADX(14)", f"{adx:.2f}" if adx is not None else "None (FAIL — gate can't run)")

    # 4. chain + greeks/IV check
    print("\n── CHAIN & GREEKS ──")
    chain = await alpaca_client.get_options_chain(
        UNDERLYING, expiry=now.date(),
        strike_lo=spot_dec - CHAIN_HALF_WIDTH, strike_hi=spot_dec + CHAIN_HALF_WIDTH,
    )
    n = len(chain)
    native_iv = sum(1 for q in chain if q.implied_volatility is not None)
    native_delta = sum(1 for q in chain if q.delta is not None)
    _line("chain contracts", n)
    _line("native IV present", f"{native_iv}/{n}" + ("" if native_iv else "  → feed has NO IV (BS fallback)"))
    _line("native delta present", f"{native_delta}/{n}")
    if n == 0:
        print("  ⚠️  EMPTY chain — cannot proceed. Likely closed/pre-open or wrong expiry.")
        return
    enriched = _enrich_chain_with_bs_greeks(chain, spot=spot_dec, now=now)
    enr_iv = sum(1 for q in enriched if q.implied_volatility is not None)
    _line("IV after BS-enrich", f"{enr_iv}/{n}")

    # 5. VIX1D from chain
    now_et = to_et(now)
    close_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    mtc = max((close_et - now_et).total_seconds() / 60.0, 1.0)
    vix1d = vix1d_from_chain(enriched, spot, mtc)
    print("\n── VIX1D (derived) ──")
    _line("minutes to close", f"{mtc:.0f}")
    _line("VIX1D (vol pts)", f"{vix1d:.2f}" if vix1d is not None else "None (FAIL)")
    if vix1d is not None and not (3.0 <= vix1d <= 80.0):
        print(f"  ⚠️  VIX1D {vix1d:.1f} outside plausible 3–80 — check derivation.")

    # 6. engine decision
    print("\n── ENGINE DECISION ──")
    d = decide_condor(spot, vix1d, adx, mtc)
    _line("action", d.action)
    _line("reason", d.reason)

    # 7. build from live chain if it would trade
    plan = None
    if d.is_trade:
        try:
            plan = build_condor_at_strikes(
                enriched,
                short_put=Decimal(str(d.short_put)), long_put=Decimal(str(d.long_put)),
                short_call=Decimal(str(d.short_call)), long_call=Decimal(str(d.long_call)),
                qty=1,
            )
            print("\n── LIVE CONDOR PLAN (would trade) ──")
            _line("short put / long put", f"{plan.short_put.strike} / {plan.long_put.strike}")
            _line("short call / long call", f"{plan.short_call.strike} / {plan.long_call.strike}")
            _line("credit / contract", f"${plan.credit_per_contract}")
            _line("max loss / contract", f"${plan.max_loss_per_contract}")
        except IronCondorBuildError as e:
            print(f"\n  ⚠️  build_condor_at_strikes FAILED on live chain: {e}")
    else:
        print("  (engine HOLDs today — no live plan; this is expected on trend/crisis days)")

    # 8. execution probe — independent of today's regime decision.
    # NOTE: the paper simulator fills MLEG orders regardless of marketability, so a
    # "non-marketable" credit limit does NOT prevent a fill (confirmed 2026-06-22 —
    # the probe filled and left a live position). So we treat a fill as a possible
    # outcome and CLOSE it immediately; we only fall back to cancel if it rested.
    if submit:
        print("\n── MLEG EXECUTION PROBE (submits a real test order; self-closes) ──")
        try:
            # build a test condor near ATM regardless of the regime gate
            kp, lp = round(spot) - 3, round(spot) - 8
            kc, lc = round(spot) + 3, round(spot) + 8
            test = build_condor_at_strikes(
                enriched, short_put=Decimal(kp), long_put=Decimal(lp),
                short_call=Decimal(kc), long_call=Decimal(lc), qty=1,
            )
            limit = (test.credit_per_contract * Decimal("3")).quantize(Decimal("0.01"))
            order = await alpaca_client.submit_iron_condor_entry(
                qty=1, limit_credit_per_contract=limit,
                short_put=test.short_put.occ_symbol, long_put=test.long_put.occ_symbol,
                short_call=test.short_call.occ_symbol, long_call=test.long_call.occ_symbol,
            )
            _line("MLEG submit status", f"{order.status} (id {order.order_id})")
            print("  ✅ account ACCEPTED a 4-leg MLEG order — Level-3 execution confirmed.")

            # Settle, then determine fill vs rest and clean up accordingly.
            await asyncio.sleep(2.0)
            final = await alpaca_client.get_order(order.order_id)
            _line("settled status", final.status)
            if str(final.status).lower().endswith("filled") or "filled" in str(final.status).lower():
                print("  ⚠️  paper engine FILLED the test order — closing the position to stay flat.")
                close = await alpaca_client.submit_iron_condor_close(
                    qty=1, limit_debit_per_contract=Decimal("150"),
                    short_put=test.short_put.occ_symbol, long_put=test.long_put.occ_symbol,
                    short_call=test.short_call.occ_symbol, long_call=test.long_call.occ_symbol,
                )
                _line("close order", f"{close.status} (id {close.order_id})")
            else:
                await alpaca_client.cancel_order(order.order_id)
                _line("cancel", "sent ✅")

            await asyncio.sleep(2.0)
            poss = [p for p in await alpaca_client.get_positions()
                    if p.symbol in {test.short_put.occ_symbol, test.long_put.occ_symbol,
                                    test.short_call.occ_symbol, test.long_call.occ_symbol}]
            _line("flat after probe?", "yes ✅" if not poss else f"NO — {len(poss)} legs OPEN ⚠️")
            for p in poss:
                print(f"     still open: {p.symbol} {p.qty} {p.side}")
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ MLEG probe FAILED: {type(e).__name__}: {e}")
            print("     → the account may not execute 4-leg defined-risk orders; investigate before go-live.")
    else:
        print("\n  (run with --submit to probe live MLEG execution — paper only)")

    print("\n═══ END DRY-RUN ═══\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--submit", action="store_true",
                    help="also place + cancel a non-marketable MLEG order (paper only)")
    asyncio.run(main(ap.parse_args().submit))
