"""Track the iron-condor ENTRY FILL HAIRCUT (model credit vs actual fill) across fills.

The condor's edge is cost-fragile: the live MLEG fill comes in below the BS-model
credit, and if that haircut is large/persistent it erodes the edge. This reads the
durable DB record (Trade.extra stores both `expected_credit_per_contract` (model)
and `credit_per_contract` (fill)) and reports the haircut per fill + running stats.

Reads the DB, not journald → survives reboots / log rotation. Re-run any time.

Usage: .venv/bin/python -m scripts.condor_fill_tracker
"""
from __future__ import annotations

from statistics import mean, median

from sqlalchemy import select

from trademaster.db import make_session_factory, Trade
from trademaster.timeutils import to_et


def _f(extra, key):
    try:
        return float(extra.get(key)) if extra.get(key) is not None else None
    except (TypeError, ValueError):
        return None


def main():
    sf = make_session_factory()
    with sf() as s:
        rows = s.execute(
            select(Trade).where(Trade.strategy == "spy_0dte_ic").order_by(Trade.opened_at)
        ).scalars().all()

    if not rows:
        print("No iron-condor fills yet (strategy=spy_0dte_ic).")
        return

    hdr = (f"{'date':<11} | {'model$':>7} | {'fill$':>6} | {'haircut$':>8} | "
           f"{'haircut%':>8} | {'exit$':>6} | {'P&L$':>7} | exit reason")
    print(hdr); print("-" * len(hdr))
    haircut_pcts, haircut_usd = [], []
    for t in rows:
        ex = t.extra or {}
        model = _f(ex, "expected_credit_per_contract")
        fill = _f(ex, "credit_per_contract") or (float(t.entry_price) if t.entry_price else None)
        d = to_et(t.opened_at).strftime("%Y-%m-%d")
        exit_debit = _f(ex, "close_filled_avg_price_per_share")
        exit_str = f"{exit_debit * 100:.0f}" if exit_debit is not None else "-"
        pnl = f"{float(t.realized_pnl_usd):+.0f}" if t.realized_pnl_usd is not None else "open"
        if model and fill and model > 0:
            hc = model - fill
            hcp = hc / model * 100
            haircut_pcts.append(hcp); haircut_usd.append(hc)
            print(f"{d:<11} | {model:>7.2f} | {fill:>6.2f} | {hc:>8.2f} | {hcp:>7.1f}% | "
                  f"{exit_str:>6} | {pnl:>7} | {ex.get('exit_reason','-')}")
        else:
            print(f"{d:<11} | {'?':>7} | {fill or '?':>6} | {'n/a':>8} | {'n/a':>8} | "
                  f"{exit_str:>6} | {pnl:>7} | {ex.get('exit_reason','-')}  (no model credit stored)")

    if haircut_pcts:
        print("-" * len(hdr))
        print(f"FILLS WITH HAIRCUT DATA: {len(haircut_pcts)}")
        print(f"  entry haircut %:  avg {mean(haircut_pcts):.1f}%   median {median(haircut_pcts):.1f}%   "
              f"min {min(haircut_pcts):.1f}%   max {max(haircut_pcts):.1f}%")
        print(f"  entry haircut $:  avg ${mean(haircut_usd):.2f}/ct   total ${sum(haircut_usd):.2f}")
        print("\nRead: a persistent large entry haircut (the model credit you DON'T get on")
        print("the fill) is the 4-leg cost-fragility that decides if the condor edge survives")
        print("live. Watch the avg trend down toward the model, not up, across the week.")


if __name__ == "__main__":
    main()
