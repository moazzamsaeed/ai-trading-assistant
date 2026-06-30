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


def build_condor_outcome(c: dict, spot: float | None) -> dict:
    """Resolve a condor's day-end outcome for the report.

    Closed → its booked P&L. Open (held to expiry, not yet booked) → the SAME
    payoff the reconciler books next morning, projected from the underlying's
    expiry-day close. Pure: `spot` is that close (or None if unavailable). This is
    read-only — it does NOT book the trade; the reconciler remains the source of
    truth, so a worthless expiry shows up in the day-end report instead of vanishing.
    """
    from decimal import Decimal

    from trademaster.reconciler import _condor_settlement_debit, _strike_from_occ

    credit = float(c.get("credit") or 0.0)
    qty = int(c.get("qty") or 0)
    base = {"id": c.get("id"), "credit": credit, "qty": qty}

    if c.get("closed_at") is not None:
        return {**base, "realized": c.get("realized_pnl_usd"), "projected": False,
                "status": f"closed ({c.get('exit_reason') or '?'})"}

    legs = (c.get("short_put"), c.get("long_put"), c.get("short_call"), c.get("long_call"))
    if spot is None or not all(legs):
        return {**base, "realized": None, "projected": True,
                "status": "open — mark unavailable"}

    mlc = c.get("max_loss_per_contract")
    sp, lp = _strike_from_occ(legs[0]), _strike_from_occ(legs[1])
    sc, lc = _strike_from_occ(legs[2]), _strike_from_occ(legs[3])
    debit = _condor_settlement_debit(
        spot, short_put=sp, long_put=lp, short_call=sc, long_call=lc,
        max_loss_per_contract=Decimal(str(mlc)) if mlc is not None else None)
    realized = float((Decimal(str(credit)) - debit) * Decimal(qty))
    if debit == 0:
        status = (f"expired worthless — full credit (SPY {spot:.2f} inside "
                  f"{float(sp):.0f}–{float(sc):.0f}); books next AM")
    else:
        status = f"expired breached @ SPY {spot:.2f} (projected); books next AM"
    return {**base, "realized": realized, "projected": True, "status": status}


def format_condor_summary(outcomes: list[dict], *, period: str) -> str:
    """Iron-condor block for the daily #trades summary (empty string if none)."""
    if not outcomes:
        return ""
    rows = [f"{'#':>3} {'CREDIT':>6} {'QTY':>3} {'P&L':>8} STATUS"]
    net = 0.0
    for o in outcomes:
        r = o.get("realized")
        pnl_str = f"{r:>+8.0f}" if r is not None else f"{'?':>8}"
        if r is not None:
            net += r
        rows.append(f"{(o.get('id') or 0):>3} {o.get('credit', 0):>6.0f} "
                    f"{o.get('qty', 0):>3} {pnl_str} {o.get('status', '')}")
    table = "```\n" + "\n".join(rows) + "\n```"
    note = ("\n_Projected = expired at the 4 PM close; the reconciler books it at "
            "the next startup._") if any(o.get("projected") for o in outcomes) else ""
    sign = "🟢" if net >= 0 else "🔴"
    return f"🦅 **Iron Condor — {period}**\n{table}\n{sign} **net ${net:+,.0f}**{note}"


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
