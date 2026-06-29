#!/usr/bin/env python3
"""Evaluate the TradeMaster strategy hypotheses (H1-H5) against fresh evidence.

The weekly review (scripts/weekly_review.py) summarises *one week* of trades.
This engine is the complement: it takes the standing **hypotheses** in
`data/strategy_kb.md` and, for each, gathers evidence and asks Claude Sonnet 4.6
to issue a verdict against that hypothesis's own "disproves if" criterion, then
proposes a specific KB edit. Like the weekly review, edits are PROPOSED only —
the human applies them.

Two kinds of evidence:
  - Live-DB stats (always; instant, free) — over ALL closed trades, not one week.
  - Historical backtests (opt-in via --backtests; slow, hits the Alpaca API) —
    runs the relevant backtest CLIs and feeds their stdout to the model.

Output is saved as `data/hypotheses/YYYY-MM-DD.md` and a TL;DR is printed for the
Friday Hermes cron to post to #research.

Usage:
    .venv/bin/python scripts/hypothesis_review.py              # DB-only (fast)
    .venv/bin/python scripts/hypothesis_review.py --backtests  # + historical (slow)
    .venv/bin/python scripts/hypothesis_review.py --only H4    # single hypothesis
    .venv/bin/python scripts/hypothesis_review.py --dry-run    # print prompt, no LLM
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB = PROJECT_ROOT / "data" / "trademaster.db"
KB = PROJECT_ROOT / "data" / "strategy_kb.md"
OUT_DIR = PROJECT_ROOT / "data" / "hypotheses"
ET = ZoneInfo("America/New_York")

# The I7 indicator-bootstrap fix (commit 9c7d621). The KB says H4 is "un-tested"
# before this — trades opened earlier ran on silently-broken indicators.
INDICATOR_FIX_DATE = date(2026, 5, 28)


# --- Hypothesis registry ---------------------------------------------------
# Hardcoded (not parsed from the KB) so a wording change in strategy_kb.md can't
# silently misroute evidence. Mirrors the "## Active hypotheses" section of
# data/strategy_kb.md — keep the ids/criteria in sync when the KB changes.
HYPOTHESES = [
    {
        "id": "H1",
        "title": "Scale-out tier 1 at +15% is the right first tier",
        "disprove": "<40% of winning trades hit +15% in 60 days",
        "evidence": ["winner_peak_buckets", "scale_out_tier_hits"],
    },
    {
        "id": "H2",
        "title": "30-second trailing tick catches peaks the 5-min poll missed",
        "disprove": "peak-to-realized gap on wins doesn't shrink vs pre-pivot",
        "evidence": ["peak_to_realized_gap", "scale_out_tier_hits"],
    },
    {
        "id": "H3",
        "title": "SPY-only beats multi-ticker",
        "disprove": (
            "SPY-only win rate stays <=25% after n=40, OR SPY expectancy stays "
            "<=$0/trade after n=40 (a positive win rate with negative expectancy "
            "does NOT validate H3 — beating multi-ticker means making money, not "
            "just winning more often)"
        ),
        "evidence": ["by_ticker", "backtest_trend_0dte"],
    },
    {
        "id": "H4",
        "title": "Standard intraday indicators (VWAP/RSI/EMA/MACD) lack edge",
        "disprove": (
            "post-fix SPY-only win rate >=45% on n>=30 => indicators DO have edge "
            "(H4 disproved); <=30% on n>=30 => H4 confirmed (no edge)"
        ),
        "evidence": ["post_fix_cohort", "backtest_trend_0dte", "backtest_engine"],
    },
    {
        "id": "H5",
        "title": "MEDIUM conviction 0DTE on SPY is acceptable",
        "disprove": (
            "MEDIUM win rate <15% over n=10, OR MEDIUM expectancy is materially "
            "below HIGH expectancy at n>=10 (if MEDIUM loses money or bleeds far "
            "worse than HIGH per trade, 'acceptable' fails even at a decent win rate)"
        ),
        "evidence": ["by_conviction", "backtest_engine"],
    },
]

# Backtest CLIs invoked as `python -m <module> <args...>` from the project root.
BACKTESTS: dict[str, list[str]] = {
    "backtest_trend_0dte": ["-m", "scripts.backtest_trend_0dte", "SPY"],
    "backtest_engine": ["-m", "scripts.backtest_engine"],
}


def _f(x) -> float:
    return float(x) if x is not None else 0.0


def _is_directional(t: dict) -> bool:
    """H1-H5 are about the directional 0DTE engine, not the condor."""
    strat = (t.get("strategy") or "").lower()
    if strat.startswith("directional"):
        return True
    return (t["extra"].get("action") or "").upper() in {"BUY_CALL", "BUY_PUT"}


def fetch_closed_trades(conn: sqlite3.Connection) -> list[dict]:
    """All closed trades, newest fields parsed. (No week filter — hypotheses
    accumulate evidence over the whole history.)"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, opened_at, closed_at, symbol, strategy, qty, entry_price,
               exit_price, realized_pnl_usd, extra
        FROM trades
        WHERE closed_at IS NOT NULL
        ORDER BY closed_at
        """
    )
    cols = [d[0] for d in cur.description]
    rows = []
    for raw in cur.fetchall():
        rec = dict(zip(cols, raw, strict=True))
        try:
            rec["extra"] = json.loads(rec["extra"]) if rec["extra"] else {}
        except json.JSONDecodeError:
            rec["extra"] = {}
        rows.append(rec)
    return rows


def _win_stats(trades: list[dict]) -> dict:
    """Win rate AND expectancy — a strategy can clear a win-rate floor while
    bleeding money (small wins, big losses), so every cohort reports both."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "wins": 0, "win_rate": None, "net_usd": 0.0,
                "avg_win_usd": None, "avg_loss_usd": None, "expectancy_usd": None}
    pnls = [_f(t.get("realized_pnl_usd")) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    net = sum(pnls)
    return {
        "n": n,
        "wins": len(wins),
        "win_rate": round(len(wins) / n, 4),
        "net_usd": round(net, 2),
        "avg_win_usd": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss_usd": round(sum(losses) / len(losses), 2) if losses else None,
        "expectancy_usd": round(net / n, 2),  # mean realized $ per trade
    }


def compute_db_evidence(all_trades: list[dict]) -> dict:
    """One pass over all closed trades → every hypothesis-relevant statistic."""
    directional = [t for t in all_trades if _is_directional(t)]
    condor = [t for t in all_trades if not _is_directional(t)]
    wins = [t for t in directional if _f(t.get("realized_pnl_usd")) > 0]

    # H1: of winning directional trades with peak data, fraction reaching each tier.
    wins_with_peak = [t for t in wins if t["extra"].get("peak_pnl_pct") is not None]
    peaks = [_f(t["extra"]["peak_pnl_pct"]) for t in wins_with_peak]
    winner_peak_buckets = {
        "n_wins": len(wins),
        "n_wins_with_peak_data": len(wins_with_peak),
        "n_wins_missing_peak_data": len(wins) - len(wins_with_peak),
        "pct_reaching_15": round(sum(p >= 15 for p in peaks) / len(peaks), 4) if peaks else None,
        "pct_reaching_30": round(sum(p >= 30 for p in peaks) / len(peaks), 4) if peaks else None,
        "pct_reaching_50": round(sum(p >= 50 for p in peaks) / len(peaks), 4) if peaks else None,
    }

    # H2: peak% minus realized% on wins (did the 30s tick capture the peak?).
    gaps = []
    for t in wins_with_peak:
        cost = _f(t.get("entry_price")) * _f(t.get("qty")) * 100
        if cost <= 0:
            continue
        realized_pct = _f(t.get("realized_pnl_usd")) / cost * 100
        peak = _f(t["extra"]["peak_pnl_pct"])
        gaps.append({"id": t["id"], "peak_pct": round(peak, 1),
                     "realized_pct": round(realized_pct, 1),
                     "gap_pct": round(peak - realized_pct, 1)})
    peak_to_realized_gap = {
        "n": len(gaps),
        "avg_gap_pct": round(sum(g["gap_pct"] for g in gaps) / len(gaps), 1) if gaps else None,
        "trades": gaps,
    }

    # H1/H2: scale-out tier hit counts over directional trades.
    tier_hits = {"15": 0, "30": 0, "50": 0, "75": 0, "100": 0}
    for t in directional:
        for tier in t["extra"].get("scale_out_tiers_fired") or []:
            key = str(int(tier))
            if key in tier_hits:
                tier_hits[key] += 1
    scale_out_tier_hits = {"n_directional": len(directional), "hits": tier_hits}

    # H3: SPY vs non-SPY directional.
    spy = [t for t in directional if (t["extra"].get("ticker") or "").upper() == "SPY"]
    non_spy = [t for t in directional if (t["extra"].get("ticker") or "").upper() not in {"SPY", ""}]
    by_ticker = {"SPY": _win_stats(spy), "non_SPY": _win_stats(non_spy)}

    # H4: post-indicator-fix SPY directional cohort (KB resets H4 after 2026-05-28).
    post_fix = []
    for t in spy:
        try:
            opened = date.fromisoformat(str(t["opened_at"])[:10])
        except ValueError:
            continue
        if opened >= INDICATOR_FIX_DATE:
            post_fix.append(t)
    calls = [t for t in post_fix if (t["extra"].get("action") or "").upper() == "BUY_CALL"]
    puts = [t for t in post_fix if (t["extra"].get("action") or "").upper() == "BUY_PUT"]
    post_fix_cohort = {
        "since": INDICATOR_FIX_DATE.isoformat(),
        "scope": "SPY-only — already filtered to SPY directional trades",
        "all": _win_stats(post_fix),
        "calls": _win_stats(calls),
        "puts": _win_stats(puts),
        "target_n": 30,
    }

    # H5: directional by conviction.
    by_conviction: dict[str, list] = {}
    for t in directional:
        c = t["extra"].get("conviction") or "UNKNOWN"
        by_conviction.setdefault(c, []).append(t)
    by_conviction = {c: _win_stats(ts) for c, ts in by_conviction.items()}

    # Open questions (KB §"Open questions"): hour-of-day and vol-regime effects.
    # Phase-1 of the self-learning loop. Hour is derived from opened_at (works on
    # all history); vol_regime only on trades logged with the Phase-1 fields, so
    # it's sparse until live data accumulates.
    by_hour: dict[str, list] = {}
    for t in directional:
        try:
            opened = datetime.fromisoformat(str(t["opened_at"])).replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue
        by_hour.setdefault(f"{opened.astimezone(ET).hour:02d}_ET", []).append(t)
    by_hour = {h: _win_stats(ts) for h, ts in sorted(by_hour.items())}

    by_vol_regime: dict[str, list] = {}
    for t in directional:
        vr = t["extra"].get("vol_regime")
        if vr:
            by_vol_regime.setdefault(vr, []).append(t)
    by_vol_regime = {k: _win_stats(ts) for k, ts in by_vol_regime.items()}

    return {
        "data_completeness": {
            "n_closed_total": len(all_trades),
            "n_directional": len(directional),
            "n_condor_or_other": len(condor),
            "n_directional_wins": len(wins),
            "n_with_peak_data": sum(1 for t in directional if t["extra"].get("peak_pnl_pct") is not None),
            "n_with_conviction": sum(1 for t in directional if t["extra"].get("conviction")),
            "note": "peak_pnl_pct and conviction are missing on many historical rows; "
                    "stats that depend on them have small n. Do not over-read.",
        },
        "winner_peak_buckets": winner_peak_buckets,
        "peak_to_realized_gap": peak_to_realized_gap,
        "scale_out_tier_hits": scale_out_tier_hits,
        "by_ticker": by_ticker,
        "post_fix_cohort": post_fix_cohort,
        "by_conviction": by_conviction,
        "by_hour_et": by_hour,
        "by_vol_regime": by_vol_regime,
    }


def run_backtests(names: set[str]) -> dict[str, str]:
    """Run each backtest CLI as a subprocess, capture stdout. Degrade gracefully:
    a failed/timed-out backtest yields an error note, never raises."""
    out: dict[str, str] = {}
    for name in sorted(names):
        argv = BACKTESTS.get(name)
        if not argv:
            continue
        cmd = [sys.executable, *argv]
        print(f"[hypothesis_review] running backtest: {' '.join(argv)} (live API, may take ~1-2 min)", file=sys.stderr)
        try:
            proc = subprocess.run(
                cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=300
            )
            if proc.returncode != 0:
                out[name] = f"[backtest FAILED rc={proc.returncode}]\nstderr:\n{proc.stderr[-1500:]}"
            else:
                body = proc.stdout.strip() or "(no stdout)"
                out[name] = body[-4000:]  # cap; tables are compact
        except subprocess.TimeoutExpired:
            out[name] = "[backtest TIMED OUT after 300s — no result this run]"
        except Exception as exc:  # noqa: BLE001 — degrade, never crash the review
            out[name] = f"[backtest ERROR: {exc!r}]"
    return out


def extract_active_hypotheses_section(kb: str) -> str:
    start = kb.find("## Active hypotheses")
    if start == -1:
        return "(could not locate '## Active hypotheses' in strategy_kb.md)"
    rest = kb.find("\n## ", start + 1)
    return kb[start:rest] if rest != -1 else kb[start:]


def build_prompt(*, today: str, hypotheses: list[dict], db_evidence: dict,
                 backtests: dict[str, str], kb_section: str) -> str:
    hyp_block = ""
    for h in hypotheses:
        ev = ", ".join(h["evidence"])
        hyp_block += (
            f"\n### {h['id']} — {h['title']}\n"
            f"- Disproves if: {h['disprove']}\n"
            f"- Relevant evidence keys: {ev}\n"
        )

    bt_block = ""
    if backtests:
        for name, body in backtests.items():
            bt_block += f"\n#### Backtest: {name}\n```\n{body}\n```\n"
    else:
        bt_block = "\n(no backtests run this pass — DB-only mode. " \
                   "Treat backtest-dependent hypotheses on live data alone and say so.)\n"

    db_block = json.dumps(db_evidence, indent=2, default=str)

    return f"""You are the TradeMaster hypothesis reviewer. Today is {today}.

For each ACTIVE hypothesis below, weigh the evidence and issue a verdict against
that hypothesis's own "disproves if" criterion. Your output IS the report markdown;
it is saved verbatim to `data/hypotheses/{today}.md`. Start directly with
`# Hypothesis Review — {today}`. No preamble.

## STRICT RULES
1. Cite n (sample size) for EVERY claim. Never "MEDIUM looks weak" — say "MEDIUM win rate X% (n=Y)".
2. Do NOT call a hypothesis "confirmed" or "disproved" unless its criterion is met AND n>=5
   (and >= any n the criterion names, e.g. H3 needs n>=40, H4 needs n>=30). Otherwise the
   status is "still active — insufficient n".
3. Live-trade n is small and many rows lack peak/conviction data (see data_completeness).
   Lean on the backtests for edge questions (H3/H4) when present; when absent, say the live
   sample can't settle it yet.
4. Proposed KB edits must be SPECIFIC (section + exact text to change), never vague.
5. Proposed KB edits are PROPOSALS — never auto-applied. The human decides.
6. Do not invent evidence. If the data doesn't support a claim, don't make it.

## HYPOTHESES UNDER REVIEW
{hyp_block}

## CURRENT KB "ACTIVE HYPOTHESES" SECTION (for origin/context)

{kb_section}

## LIVE-TRADE EVIDENCE (computed from all closed trades — math already done)

```json
{db_block}
```

## BACKTEST EVIDENCE
{bt_block}

## REQUIRED OUTPUT STRUCTURE

```markdown
# Hypothesis Review — {today}

**Hypotheses evaluated:** <count>
**Backtests run:** <names or "none (DB-only)">
**Live directional trades:** <n_directional from data_completeness>

## TL;DR
<One line per hypothesis: "H1: still active (n=X) — <8-word reason>". This block is
posted to Discord, so keep it tight and scannable.>

## Per-hypothesis findings
<For each hypothesis:>
### <id> — <title>
- **Status:** still active / approaching confirm / approaching disprove / confirmed / disproved (with n)
- **Evidence:** <cite the specific numbers + n you used>
- **Proposed KB edit:** <section + exact text, or "none — insufficient evidence">

## New questions surfaced
<Anything the evidence raised that isn't yet a tracked hypothesis or open question.>
```

Now produce the report. Output ONLY the markdown, starting with `# Hypothesis Review — {today}`."""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtests", action="store_true",
                        help="also run historical backtests (slow, hits Alpaca API)")
    parser.add_argument("--only", default=None,
                        help="evaluate a single hypothesis, e.g. --only H4")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the assembled prompt and exit (no LLM call)")
    args = parser.parse_args()

    today = datetime.now(ET).date().isoformat()

    hypotheses = HYPOTHESES
    if args.only:
        want = args.only.upper()
        hypotheses = [h for h in HYPOTHESES if h["id"] == want]
        if not hypotheses:
            print(f"[hypothesis_review] no hypothesis {want!r}; known: "
                  f"{[h['id'] for h in HYPOTHESES]}", file=sys.stderr)
            return 2

    with sqlite3.connect(str(DB)) as conn:
        all_trades = fetch_closed_trades(conn)
    db_evidence = compute_db_evidence(all_trades)

    backtests: dict[str, str] = {}
    if args.backtests:
        wanted = {e for h in hypotheses for e in h["evidence"] if e in BACKTESTS}
        backtests = run_backtests(wanted)

    kb = KB.read_text() if KB.exists() else "(strategy_kb.md not found)"
    prompt = build_prompt(
        today=today,
        hypotheses=hypotheses,
        db_evidence=db_evidence,
        backtests=backtests,
        kb_section=extract_active_hypotheses_section(kb),
    )

    print(f"[hypothesis_review] hypotheses={len(hypotheses)} "
          f"backtests={len(backtests)} prompt_chars={len(prompt)}", file=sys.stderr)

    if args.dry_run:
        print(prompt)
        return 0

    # Direct SDK streaming call — same pattern as weekly_review.py (the shared
    # trademaster.llm client has a 30s trading-path timeout, too short for synthesis).
    import anthropic

    from trademaster.config import get_settings

    api_key = get_settings().anthropic_api_key.get_secret_value()
    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=600.0)

    print("[hypothesis_review] calling claude-sonnet-4-6 (streaming)...", file=sys.stderr)
    chunks: list[str] = []
    input_tokens = output_tokens = 0
    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            chunks.append(text)
        final = await stream.get_final_message()
        input_tokens = final.usage.input_tokens
        output_tokens = final.usage.output_tokens

    report = "".join(chunks)
    cost = (input_tokens * 3 + output_tokens * 15) / 1_000_000  # Sonnet 4.6 pricing
    print(f"[hypothesis_review] ok in={input_tokens} out={output_tokens} cost=${cost:.4f}",
          file=sys.stderr)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{today}.md"
    out_path.write_text(report)

    print(f"HYP_PATH={out_path}")
    print(f"DATE={today}")
    print(f"SUMMARY=hypotheses={len(hypotheses)} backtests={len(backtests)} "
          f"directional_trades={db_evidence['data_completeness']['n_directional']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
