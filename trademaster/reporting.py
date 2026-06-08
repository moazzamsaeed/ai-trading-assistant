"""#trades reporting — per-trade close detail + daily/weekly summaries.

P&L everywhere folds in scale-out partials (the trades table's realized_pnl_usd
is only the final leg). All formatters return plain Discord markdown.
"""

from __future__ import annotations


def _opt(action: str | None) -> str:
    return "CALL" if action == "BUY_CALL" else "PUT" if action == "BUY_PUT" else "?"


def _hold_str(opened, closed) -> str:
    if not opened or not closed:
        return "?"
    o = opened.replace(tzinfo=None)
    c = closed.replace(tzinfo=None)
    mins = max(0, int((c - o).total_seconds() // 60))
    h, m = divmod(mins, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def format_trade_closed(t: dict) -> str:
    """Full per-trade close report for #trades."""
    opt = _opt(t.get("action"))
    icon = "📈" if t.get("action") == "BUY_CALL" else "📉"
    total = float(t.get("total_pnl") or 0)
    final = float(t.get("final_pnl") or 0)
    partial = float(t.get("partial_pnl") or 0)
    pnl_icon = "✅" if total >= 0 else "❌"
    mode = (t.get("mode") or "").upper()
    oq, fq = t.get("original_qty"), t.get("final_qty")
    entry, exit_ = t.get("entry_price"), t.get("exit_price")
    scaled = (oq - fq) if (isinstance(oq, int) and isinstance(fq, int)) else None

    lines = [
        f"{icon} **{t.get('ticker','SPY')} {opt} — CLOSED**"
        + (f" [{mode}]" if mode else "")
        + (f" · #{t['id']}" if t.get("id") else ""),
        f"Contract: `{t.get('occ','?')}`",
        f"Entry: {oq if oq is not None else fq}× @ ${entry}  →  Exit: {fq}× @ ${exit_}"
        + (f"  ({scaled}× scaled out earlier)" if scaled else ""),
    ]
    pnl_line = f"P&L: {pnl_icon} **${total:+,.0f}**"
    if partial:
        pnl_line += f"  (final ${final:+,.0f} + scale-outs ${partial:+,.0f})"
    lines.append(pnl_line)
    peak = t.get("peak_pnl_pct")
    meta = []
    if peak is not None:
        meta.append(f"peak +{float(peak):.0f}%")
    if t.get("exit_reason"):
        meta.append(f"reason: {t['exit_reason']}")
    meta.append(f"held {_hold_str(t.get('opened_at'), t.get('closed_at'))}")
    lines.append(" · ".join(meta))
    return "\n".join(lines)


def format_trades_summary(trades: list[dict], *, title: str, period: str) -> str:
    """Tabular daily/weekly summary for #trades. P&L includes scale-out partials."""
    if not trades:
        return f"📊 **{title} — {period}**\n_No trades._"

    net = sum(float(t.get("total_pnl") or 0) for t in trades)
    wins = sum(1 for t in trades if float(t.get("total_pnl") or 0) > 0)
    losses = sum(1 for t in trades if float(t.get("total_pnl") or 0) < 0)

    rows = [f"{'#':>3} {'DIR':<4} {'QTY':>3} {'ENTRY':>6} {'EXIT':>6} {'P&L':>8} REASON"]
    for t in trades:
        rows.append(
            f"{(t.get('id') or 0):>3} "
            f"{_opt(t.get('action')):<4} "
            f"{(t.get('original_qty') or t.get('final_qty') or 0):>3} "
            f"{(t.get('entry_price') or 0):>6.2f} "
            f"{(t.get('exit_price') or 0):>6.2f} "
            f"{float(t.get('total_pnl') or 0):>+8.0f} "
            f"{(t.get('exit_reason') or '-')}"
        )
    table = "```\n" + "\n".join(rows) + "\n```"
    sign = "🟢" if net >= 0 else "🔴"
    footer = f"{sign} **{len(trades)} trades · {wins}W/{losses}L · net ${net:+,.0f}**"
    return f"📊 **{title} — {period}**\n{table}\n{footer}"
