#!/usr/bin/env python3
"""Generate the TradeMaster weekly strategy review.

Reads trades from the SQLite DB, the strategy KB, and the last 4 prior
reviews, then asks Claude Sonnet 4.6 to synthesize a markdown review and
propose KB edits. Output is saved as `data/reviews/YYYY-Www.md` (ISO week).

KB edits are PROPOSED in the review — never auto-applied. Human decides.

Usage:
    .venv/bin/python scripts/weekly_review.py                  # current ISO week
    .venv/bin/python scripts/weekly_review.py --week-offset 1  # last ISO week
    .venv/bin/python scripts/weekly_review.py --dry-run        # print prompt, skip LLM
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB = PROJECT_ROOT / "data" / "trademaster.db"
KB = PROJECT_ROOT / "data" / "strategy_kb.md"
REVIEWS_DIR = PROJECT_ROOT / "data" / "reviews"
ET = ZoneInfo("America/New_York")


def compute_week_range(offset: int) -> tuple[datetime, datetime, str]:
    """Return (start_utc, end_utc, iso_label) for the ISO week ending offset
    weeks ago. offset=0 = current week, offset=1 = previous, etc."""
    today_et = datetime.now(ET).date()
    monday_this_week = today_et - timedelta(days=today_et.isoweekday() - 1)
    target_monday = monday_this_week - timedelta(weeks=offset)
    start_et = datetime.combine(target_monday, datetime.min.time(), ET)
    end_et = start_et + timedelta(days=7)
    iso_year, iso_week, _ = target_monday.isocalendar()
    return start_et.astimezone(UTC), end_et.astimezone(UTC), f"{iso_year}-W{iso_week:02d}"


def _sql_dt(dt: datetime) -> str:
    """Format UTC datetime the way SQLAlchemy stored it (naive ISO, no tz)."""
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def fetch_closed_trades(
    conn: sqlite3.Connection, start_utc: datetime, end_utc: datetime
) -> list[dict]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, opened_at, closed_at, symbol, strategy, qty, entry_price,
               exit_price, realized_pnl_usd, extra
        FROM trades
        WHERE closed_at IS NOT NULL
          AND closed_at >= ?
          AND closed_at < ?
        ORDER BY closed_at
        """,
        (_sql_dt(start_utc), _sql_dt(end_utc)),
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


def _f(x) -> float:
    return float(x) if x is not None else 0.0


def compute_metrics(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0}

    wins = [t for t in trades if _f(t.get("realized_pnl_usd")) > 0]
    losses = [t for t in trades if _f(t.get("realized_pnl_usd")) < 0]
    net = sum(_f(t.get("realized_pnl_usd")) for t in trades)
    avg_win = sum(_f(t["realized_pnl_usd"]) for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(_f(t["realized_pnl_usd"]) for t in losses) / len(losses) if losses else 0.0
    win_rate = len(wins) / n
    expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)

    tier_hits = {"15": 0, "30": 0, "50": 0, "75": 0, "100": 0}
    for t in trades:
        for tier in t["extra"].get("scale_out_tiers_fired") or []:
            key = str(int(tier))
            if key in tier_hits:
                tier_hits[key] += 1

    peak_vs_realized = []
    for t in trades:
        peak = t["extra"].get("peak_pnl_pct")
        if peak is None:
            continue
        peak_vs_realized.append(
            {
                "id": t["id"],
                "peak_pct": peak,
                "realized_usd": _f(t.get("realized_pnl_usd")),
            }
        )

    conviction = {}
    for t in trades:
        c = t["extra"].get("conviction") or "UNKNOWN"
        pnl = _f(t.get("realized_pnl_usd"))
        b = conviction.setdefault(c, {"n": 0, "wins": 0, "net": 0.0})
        b["n"] += 1
        b["net"] += pnl
        if pnl > 0:
            b["wins"] += 1

    return {
        "n": n,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "n_breakeven": n - len(wins) - len(losses),
        "win_rate": round(win_rate, 4),
        "net_pnl_usd": round(net, 2),
        "avg_win_usd": round(avg_win, 2),
        "avg_loss_usd": round(avg_loss, 2),
        "expectancy_per_trade_usd": round(expectancy, 2),
        "scale_out_tier_hits": tier_hits,
        "peak_vs_realized": peak_vs_realized,
        "conviction_breakdown": conviction,
    }


def format_trades_table(trades: list[dict]) -> str:
    if not trades:
        return "(no closed trades this week)"
    head = (
        "| id | symbol | conv | qty | entry | exit | peak% | realized $ | exit reason |\n"
        "|----|--------|------|-----|-------|------|-------|-----------|-------------|"
    )
    rows = [head]
    for t in trades:
        e = t["extra"]
        rows.append(
            f"| {t['id']} | {t['symbol']} | {e.get('conviction', '?')} | {t['qty']} | "
            f"{t['entry_price']} | {t.get('exit_price') or '-'} | "
            f"{e.get('peak_pnl_pct') if e.get('peak_pnl_pct') is not None else '-'} | "
            f"{_f(t.get('realized_pnl_usd')):.2f} | "
            f"{e.get('exit_reason') or '?'} |"
        )
    return "\n".join(rows)


def load_kb() -> str:
    return KB.read_text() if KB.exists() else "(strategy_kb.md not found)"


def load_recent_reviews(n: int = 4) -> list[tuple[str, str]]:
    if not REVIEWS_DIR.exists():
        return []
    files = sorted(REVIEWS_DIR.glob("*.md"), reverse=True)[:n]
    return [(f.name, f.read_text()) for f in files]


def build_prompt(
    *,
    week_label: str,
    start_utc: datetime,
    end_utc: datetime,
    metrics: dict,
    trades: list[dict],
    kb: str,
    recent_reviews: list[tuple[str, str]],
) -> str:
    metrics_block = json.dumps(metrics, indent=2, default=str)
    trades_block = format_trades_table(trades)

    if recent_reviews:
        history = "\n\n## PRIOR REVIEWS (most recent first)\n\n"
        for name, content in recent_reviews:
            history += f"### {name}\n\n{content}\n\n---\n"
    else:
        history = "\n\n## PRIOR REVIEWS\n(none — this is the first review)\n"

    return f"""You are the TradeMaster strategy reviewer. Produce the weekly review for {week_label} (covers {start_utc.date()} to {end_utc.date()} UTC).

Your output IS the weekly review markdown — it will be saved as-is to `data/reviews/{week_label}.md`. Start directly with `# Weekly Review — {week_label}`. No preamble like "Here is the review".

## STRICT RULES
1. Cite n (sample size) for every claim. "scale-out tier 1 hit on 4 of 7 wins (n=7)" — never just "scale-out worked well".
2. Don't promote anything to "confirmed pattern" with n<5.
3. Don't invent patterns. If the data doesn't support a claim, don't make it.
4. Proposed KB edits must be SPECIFIC (e.g., "change H1 disprove threshold from 40% to 30%"), not vague ("consider tightening").
5. If sample size is too small for meaningful claims (n<3), say so explicitly rather than confabulating.
6. "Proposed KB edits" is PROPOSAL ONLY — never auto-applied. Human decides.

## STRATEGY KB

{kb}

## PRECOMPUTED METRICS (use these — math is already done)

```json
{metrics_block}
```

## TRADES CLOSED THIS WEEK

{trades_block}
{history}

## REQUIRED OUTPUT STRUCTURE

```markdown
# Weekly Review — {week_label}

**Period:** {start_utc.date()} to {end_utc.date()} (UTC)
**Trades closed:** <n>
**Win rate:** <pct> (<wins>/<n>)
**Net P&L:** <$amount>
**Expectancy:** <$ per trade>

## Trend vs prior weeks
<1-2 sentences vs prior reviews. If none, say "first review — no trend yet". Cite specific numbers.>

## What fired and what didn't
- **Scale-out tiers:** <tier hit breakdown from metrics>
- **Conviction breakdown:** <HIGH vs MEDIUM with win rate each, from metrics>
- **Peak-to-realized gap:** <are wins capturing peaks? cite trade ids with big gaps>

## Notable trades
<2-4 most informative trades — biggest win, biggest loss, anything that matches a watched incident pattern. Each: trade id, what happened in 1 line, why it matters.>

## Hypothesis status updates
<For each active hypothesis (H1-H5) that got new data this week:>
- **H1:** <new evidence>. Status: still active / approaching confirm / approaching disprove. Cumulative n: <n>.
<Skip hypotheses with no new data this week.>

## Proposed KB edits
<Concrete edit proposals. Each:>
- **Section:** <which KB section>
- **Change:** <exact text to add/modify/remove>
- **Reason:** <why, citing this week's data>
- **Decision:** approve / reject (human decides — not auto-applied)

<If sample is too small for any defensible edits, say so explicitly.>

## New questions surfaced
<Open questions raised by this week's data. Examples: "do losses cluster around FOMC days?", "does scale-out tier 1 hit more on calls vs puts?". Get watched in future reviews.>

## Watch list for next week
<Specific things to monitor — usually tied to incident watch-conditions in the KB. Examples: "any single trade with potential loss >$500 (incident I1)", "verify tier 1 hit rate when win count >3".>
```

Now produce the review. Output ONLY the markdown, starting with `# Weekly Review — {week_label}`."""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--week-offset", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    start_utc, end_utc, week_label = compute_week_range(args.week_offset)
    print(
        f"[weekly_review] week={week_label} range={start_utc.date()}..{end_utc.date()} UTC",
        file=sys.stderr,
    )

    with sqlite3.connect(str(DB)) as conn:
        trades = fetch_closed_trades(conn, start_utc, end_utc)

    metrics = compute_metrics(trades)
    kb = load_kb()
    recent_reviews = load_recent_reviews(n=4)

    prompt = build_prompt(
        week_label=week_label,
        start_utc=start_utc,
        end_utc=end_utc,
        metrics=metrics,
        trades=trades,
        kb=kb,
        recent_reviews=recent_reviews,
    )

    print(
        f"[weekly_review] trades={metrics.get('n', 0)} prompt_chars={len(prompt)}",
        file=sys.stderr,
    )

    if args.dry_run:
        print(prompt)
        return 0

    # Direct SDK call (not the shared trademaster.llm client, which has a 30s
    # timeout suited to trading-path decisions). Long-form synthesis needs
    # streaming per Anthropic's docs.
    import anthropic

    from trademaster.config import get_settings

    api_key = get_settings().anthropic_api_key.get_secret_value()
    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=600.0)

    print("[weekly_review] calling claude-sonnet-4-6 (streaming)...", file=sys.stderr)
    chunks: list[str] = []
    input_tokens = 0
    output_tokens = 0
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

    text = "".join(chunks)
    # Sonnet 4.6 pricing: $3/MTok input, $15/MTok output
    cost = (input_tokens * 3 + output_tokens * 15) / 1_000_000
    print(
        f"[weekly_review] ok in={input_tokens} out={output_tokens} cost=${cost:.4f}",
        file=sys.stderr,
    )

    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REVIEWS_DIR / f"{week_label}.md"
    out_path.write_text(text)

    print(f"REVIEW_PATH={out_path}")
    print(f"WEEK={week_label}")
    print(
        f"SUMMARY=trades={metrics.get('n', 0)} "
        f"win_rate={metrics.get('win_rate', 0) * 100:.0f}% "
        f"net=${metrics.get('net_pnl_usd', 0):.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
