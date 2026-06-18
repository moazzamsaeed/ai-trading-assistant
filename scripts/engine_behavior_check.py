"""Post-close behavior check for the deterministic engine (entry + exit).

Reports what the platform-first deterministic core actually did on a given trading
day — NOT a P&L verdict (paper fills are too clean to price the edge). The point is
to confirm the architecture ran clean and traded as designed:

  • entries made by the RULES engine (model tag), not the LLM
  • puts-only behavior, conviction mix, entry ADX / VWAP-distance
  • exits by reason — deterministic confirm vs mechanical, and ZERO LLM exits
  • selectivity (HOLD-heavy), and a hard check that NO directional decision LLM
    calls happened (directional_entry / exit_decision should be 0)
  • any errors or position desync

Run on the NUC after close:  uv run python -m scripts.engine_behavior_check [YYYY-MM-DD]
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import date, datetime


def _today_et() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _journal(day: str, grep: str) -> list[str]:
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", "trademaster", "--since", f"{day} 00:00",
             "--until", f"{day} 23:59", "--no-pager"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception as e:  # noqa: BLE001
        return [f"(journal unavailable: {e})"]
    import re
    return [ln for ln in out.splitlines() if re.search(grep, ln)]


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else _today_et()
    c = sqlite3.connect("data/trademaster.db")
    print(f"═══ DETERMINISTIC ENGINE — behavior check for {day} ═══\n")

    # 1. Entries: directional trades opened that day
    rows = list(c.execute(
        "select id,opened_at,closed_at,realized_pnl_usd,extra from trades "
        "where strategy in ('directional_call','directional_put') and opened_at >= ? "
        "and opened_at < ? order by opened_at",
        (day, day + "T99"),
    ))
    print(f"TRADES OPENED: {len(rows)}")
    convs, acts, exits = Counter(), Counter(), Counter()
    net = 0.0
    for r in rows:
        e = json.loads(r[4]) if r[4] else {}
        pnl = float(r[3] or 0) + float(e.get("partial_realized_pnl_usd", 0) or 0)
        net += pnl
        convs[e.get("conviction") or "?"] += 1
        acts[e.get("action") or "?"] += 1
        if r[2]:
            exits[e.get("exit_reason") or "?"] += 1
        st = "OPEN" if not r[2] else r[2][11:16]
        print(f"  #{r[0]} {r[1][11:16]}→{st} {e.get('action')}/{e.get('conviction')} "
              f"adx={e.get('entry_adx')} pnl=${pnl:+.0f} exit={e.get('exit_reason')}")
    if rows:
        print(f"  → actions={dict(acts)}  conviction={dict(convs)}  net=${net:+.0f}")
        from trademaster.config import get_settings
        if acts.get("BUY_CALL") and get_settings().directional_puts_only:
            print("  ⚠️  CALLS present despite DIRECTIONAL_PUTS_ONLY=true.")
        # Track call-vs-put P&L separately — the call side is on probation.
        call_pnl = sum((float(json.loads(r[4] or '{}').get('partial_realized_pnl_usd', 0) or 0)
                        + float(r[3] or 0)) for r in rows
                       if (json.loads(r[4]) if r[4] else {}).get('action') == 'BUY_CALL')
        put_pnl = net - call_pnl
        print(f"  → CALL net=${call_pnl:+.0f}  PUT net=${put_pnl:+.0f}")

    # 2. Entry source — should be the rules engine, not an LLM model
    sigs = list(c.execute(
        "select payload from signals where task_type='directional_entry' and created_at >= ? "
        "and created_at < ?", (day, day + "T99")))
    models = Counter()
    for (p,) in sigs:
        try:
            models[json.loads(p).get("model", "?")] += 1
        except Exception:  # noqa: BLE001
            models["?"] += 1
    print(f"\nENTRY DECISION SOURCE (signal model tag): {dict(models) or 'none'}")
    if any(m != "rules_engine" for m in models):
        print("  ⚠️  Non-rules_engine entries — LLM may still be in the hot-path.")

    # 3. Exit reasons
    if exits:
        print(f"\nEXITS BY REASON: {dict(exits)}")
        if any("smart_exit" in k or "smart_profit" in k for k in exits):
            print("  ⚠️  LLM smart_exit fired — deterministic exit not active?")

    # 4. HARD CHECK — zero directional-decision LLM calls
    llm = _journal(day, r'"task_type": "(directional_entry|exit_decision)"')
    print(f"\nLLM DECISION CALLS (should be 0): {len(llm)}")
    if llm:
        print("  ⚠️  LLM was called in the decision hot-path:")
        for ln in llm[:3]:
            print(f"     {ln[-110:]}")

    # 5. Selectivity + errors
    scans = _journal(day, r'directional_scan_complete')
    actionable = sum(1 for ln in scans if '"n_actionable": 0' not in ln)
    print(f"\nSCANS: {len(scans)} total, {actionable} actionable (HOLD-heavy is expected/good)")
    errs = _journal(day, r'"level": ?"error"|Traceback|position_not_in_broker|position_not_found')
    print(f"ERRORS / desync: {len(errs)}" + ("  ⚠️" if errs else "  ✓ clean"))
    for ln in errs[:3]:
        print(f"     {ln[-110:]}")

    print("\nNOTE: paper fills are ~mid/no-impact — read this for ARCHITECTURE + behavior, "
          "NOT as a profitability verdict.")


if __name__ == "__main__":
    main()
