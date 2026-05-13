# TradingView Charting Setup — Options Premium Seller

Institutional-grade chart setup for selling premium (wheel, iron condors, credit spreads)
on SPX/SPY/QQQ and high-IV individual names.

---

## The Analytical Framework (Always Top-Down)

Never look at a 5-minute chart first. Work from macro to micro:

```
Monthly  → Regime (bull/bear/range)
Weekly   → Major S/R, trend structure
Daily    → Strike placement, entry signal  ← primary timeframe
4H / 1H  → Entry timing
15m / 5m → 0DTE fine-tuning only
```

Five questions before every trade:

1. **Regime** — Is price above or below the 200 EMA? (macro filter)
2. **Levels** — Where are the walls? (PDH/PDL, PWH/PWL, pivots)
3. **Trend** — Short-term direction? (9/21 EMA relationship)
4. **Momentum** — Is the trend exhausted? (RSI, divergence)
5. **Volatility** — Is IV cheap or expensive? (BB width, ATR %)

---

## TradingView Layout

### Chart 1 — SPY Daily (primary analysis)
- Indicators: Key Levels + Trend Overlay + Dashboard panel
- Use this to pick strikes and determine directional bias

### Chart 2 — SPY 1H (entry timing)
- Same indicators, different timeframe
- Enter when 1H aligns with daily bias

### Chart 3 — VIX Daily
- Simple EMA 10 + EMA 20
- VIX > 20 = elevated IV, premium selling is richly paid
- VIX < 15 = low IV, be selective, condors only in tight ranges
- VIX spike + reversal = best premium selling setup

### Watchlist
```
SPY, SPX (for reference), QQQ, IWM, VIX
Add your individual wheel candidates beneath
```

---

## Pine Script 1: Key Levels
*Paste this as a new indicator — overlaid on the chart*

Shows Previous Day/Week/Month Highs & Lows, plus Classic Pivot Points.
Strikes should be placed BEYOND these levels for defense.

```pine
//@version=5
indicator("Hermes | Key Levels", overlay=true, max_lines_count=500, max_labels_count=200)

// ── Toggles ──────────────────────────────────────────────────────────────────
show_daily   = input.bool(true,  "Daily Levels")
show_weekly  = input.bool(true,  "Weekly Levels")
show_monthly = input.bool(false, "Monthly Levels")
show_pivots  = input.bool(true,  "Daily Pivot Points")
label_levels = input.bool(true,  "Show Labels")

// ── Previous Day ─────────────────────────────────────────────────────────────
pd_high  = request.security(syminfo.tickerid, "D", high[1],  lookahead=barmerge.lookahead_on)
pd_low   = request.security(syminfo.tickerid, "D", low[1],   lookahead=barmerge.lookahead_on)
pd_close = request.security(syminfo.tickerid, "D", close[1], lookahead=barmerge.lookahead_on)

// ── Previous Week ─────────────────────────────────────────────────────────────
pw_high = request.security(syminfo.tickerid, "W", high[1], lookahead=barmerge.lookahead_on)
pw_low  = request.security(syminfo.tickerid, "W", low[1],  lookahead=barmerge.lookahead_on)

// ── Previous Month ────────────────────────────────────────────────────────────
pm_high = request.security(syminfo.tickerid, "M", high[1], lookahead=barmerge.lookahead_on)
pm_low  = request.security(syminfo.tickerid, "M", low[1],  lookahead=barmerge.lookahead_on)

// ── Classic Pivot Points (from Previous Day) ──────────────────────────────────
pivot = (pd_high + pd_low + pd_close) / 3
r1 = 2 * pivot - pd_low
r2 = pivot + (pd_high - pd_low)
r3 = pd_high + 2 * (pivot - pd_low)
s1 = 2 * pivot - pd_high
s2 = pivot - (pd_high - pd_low)
s3 = pd_low - 2 * (pd_high - pivot)

// ── Plots ─────────────────────────────────────────────────────────────────────
// Daily
plot(show_daily ? pd_high  : na, "PDH", color=color.new(color.green, 10), linewidth=1, style=plot.style_stepline_diamond)
plot(show_daily ? pd_low   : na, "PDL", color=color.new(color.red,   10), linewidth=1, style=plot.style_stepline_diamond)
plot(show_daily ? pd_close : na, "PDC", color=color.new(color.gray,  50), linewidth=1, style=plot.style_circles)

// Weekly (thicker = more significant)
plot(show_weekly ? pw_high : na, "PWH", color=color.new(color.green, 0), linewidth=2, style=plot.style_stepline_diamond)
plot(show_weekly ? pw_low  : na, "PWL", color=color.new(color.red,   0), linewidth=2, style=plot.style_stepline_diamond)

// Monthly
plot(show_monthly ? pm_high : na, "PMH", color=color.new(color.teal,   0), linewidth=2, style=plot.style_stepline_diamond)
plot(show_monthly ? pm_low  : na, "PML", color=color.new(color.maroon, 0), linewidth=2, style=plot.style_stepline_diamond)

// Pivot Points
plot(show_pivots ? pivot : na, "PP", color=color.new(color.yellow, 0),  linewidth=1, style=plot.style_circles)
plot(show_pivots ? r1    : na, "R1", color=color.new(color.lime,   50), linewidth=1)
plot(show_pivots ? r2    : na, "R2", color=color.new(color.lime,   20), linewidth=1)
plot(show_pivots ? r3    : na, "R3", color=color.new(color.lime,   0),  linewidth=1)
plot(show_pivots ? s1    : na, "S1", color=color.new(color.red,    50), linewidth=1)
plot(show_pivots ? s2    : na, "S2", color=color.new(color.red,    20), linewidth=1)
plot(show_pivots ? s3    : na, "S3", color=color.new(color.red,    0),  linewidth=1)

// ── Labels (on last bar only) ─────────────────────────────────────────────────
if barstate.islast and label_levels
    label.new(bar_index, pd_high,  "PDH "  + str.tostring(pd_high,  "#.##"), style=label.style_label_left, color=color.new(color.green, 80), textcolor=color.green,  size=size.small)
    label.new(bar_index, pd_low,   "PDL "  + str.tostring(pd_low,   "#.##"), style=label.style_label_left, color=color.new(color.red,   80), textcolor=color.red,    size=size.small)
    label.new(bar_index, pw_high,  "PWH "  + str.tostring(pw_high,  "#.##"), style=label.style_label_left, color=color.new(color.green, 60), textcolor=color.green,  size=size.small)
    label.new(bar_index, pw_low,   "PWL "  + str.tostring(pw_low,   "#.##"), style=label.style_label_left, color=color.new(color.red,   60), textcolor=color.red,    size=size.small)
    label.new(bar_index, pivot,    "PP "   + str.tostring(pivot,    "#.##"), style=label.style_label_left, color=color.new(color.yellow,80), textcolor=color.yellow, size=size.small)
    label.new(bar_index, r1,       "R1 "   + str.tostring(r1,       "#.##"), style=label.style_label_left, color=color.new(color.lime,  80), textcolor=color.lime,   size=size.small)
    label.new(bar_index, s1,       "S1 "   + str.tostring(s1,       "#.##"), style=label.style_label_left, color=color.new(color.red,   80), textcolor=color.red,    size=size.small)
```

---

## Pine Script 2: Trend Overlay (EMAs + VWAP + Bollinger Bands)
*Paste this as a second indicator — also overlaid*

```pine
//@version=5
indicator("Hermes | Trend & VWAP", overlay=true)

// ── EMAs ──────────────────────────────────────────────────────────────────────
ema9   = ta.ema(close, 9)
ema21  = ta.ema(close, 21)
ema50  = ta.ema(close, 50)
ema200 = ta.ema(close, 200)

plot(ema9,   "EMA 9",   color=color.new(color.yellow, 0), linewidth=1)
plot(ema21,  "EMA 21",  color=color.new(color.orange, 0), linewidth=1)
plot(ema50,  "EMA 50",  color=color.new(color.blue,   0), linewidth=2)
plot(ema200, "EMA 200", color=color.new(color.red,    0), linewidth=3)

// EMA trend cloud: green when 9 > 21 (bullish), red when 9 < 21 (bearish)
ema_bull = ema9 > ema21
p9  = plot(ema9,  display=display.none)
p21 = plot(ema21, display=display.none)
fill(p9, p21, color=ema_bull ? color.new(color.green, 85) : color.new(color.red, 85), title="EMA Cloud")

// ── VWAP with ±1σ and ±2σ Bands ──────────────────────────────────────────────
// Resets each trading day
var float cum_vol        = 0.0
var float cum_vol_price  = 0.0
var float cum_vol_price2 = 0.0

is_new_day = ta.change(time("D")) != 0 or barstate.isfirst
if is_new_day
    cum_vol        := 0.0
    cum_vol_price  := 0.0
    cum_vol_price2 := 0.0

cum_vol        += volume
cum_vol_price  += hlc3 * volume
cum_vol_price2 += hlc3 * hlc3 * volume

vwap_val  = cum_vol_price / cum_vol
variance  = cum_vol_price2 / cum_vol - vwap_val * vwap_val
vwap_sd   = math.sqrt(math.max(variance, 0))

vwap_u1 = vwap_val + vwap_sd
vwap_l1 = vwap_val - vwap_sd
vwap_u2 = vwap_val + 2 * vwap_sd
vwap_l2 = vwap_val - 2 * vwap_sd

pv   = plot(vwap_val, "VWAP",     color=color.new(color.white, 0),  linewidth=2)
pu1  = plot(vwap_u1,  "VWAP +1σ", color=color.new(color.green, 40), linewidth=1)
pl1  = plot(vwap_l1,  "VWAP -1σ", color=color.new(color.red,   40), linewidth=1)
pu2  = plot(vwap_u2,  "VWAP +2σ", color=color.new(color.green, 10), linewidth=1)
pl2  = plot(vwap_l2,  "VWAP -2σ", color=color.new(color.red,   10), linewidth=1)

fill(pu1, pl1, color=color.new(color.white, 94), title="VWAP ±1σ Zone")
fill(pu2, pu1, color=color.new(color.green, 96), title="VWAP 1-2σ Upper")
fill(pl2, pl1, color=color.new(color.red,   96), title="VWAP 1-2σ Lower")

// ── Bollinger Bands (20, 2) ───────────────────────────────────────────────────
bb_len   = input.int(20, "BB Length", group="Bollinger Bands")
bb_mult  = input.float(2.0, "BB Multiplier", group="Bollinger Bands")
bb_basis = ta.sma(close, bb_len)
bb_dev   = ta.stdev(close, bb_len)
bb_upper = bb_basis + bb_mult * bb_dev
bb_lower = bb_basis - bb_mult * bb_dev

pbu = plot(bb_upper, "BB Upper", color=color.new(color.teal, 50), linewidth=1)
pbl = plot(bb_lower, "BB Lower", color=color.new(color.teal, 50), linewidth=1)
fill(pbu, pbl, color=color.new(color.teal, 95), title="BB Fill")

// ── Candle color: above/below VWAP ───────────────────────────────────────────
barcolor(close > vwap_val ? color.new(color.green, 75) : color.new(color.red, 75), title="VWAP Candle Color")
```

---

## Pine Script 3: Options Dashboard Panel
*Paste this as a third indicator — set to "New pane below"*

Shows RSI, trend status, ATR %, volatility squeeze, and a premium-sell signal — all in a table.

```pine
//@version=5
indicator("Hermes | Options Dashboard", overlay=false)

// ── RSI ───────────────────────────────────────────────────────────────────────
rsi_len = input.int(14, "RSI Length")
rsi_val = ta.rsi(close, rsi_len)

rsi_color = rsi_val > 70 ? color.red : rsi_val < 30 ? color.lime : color.new(color.purple, 0)
plot(rsi_val, "RSI", color=rsi_color, linewidth=2)
hline(70, "Overbought", color=color.new(color.red,   30), linestyle=hline.style_dashed)
hline(60, "Upper Mid",  color=color.new(color.gray,  60), linestyle=hline.style_dotted)
hline(50, "Midline",    color=color.new(color.white, 50), linestyle=hline.style_dotted)
hline(40, "Lower Mid",  color=color.new(color.gray,  60), linestyle=hline.style_dotted)
hline(30, "Oversold",   color=color.new(color.green, 30), linestyle=hline.style_dashed)

bgcolor(rsi_val > 70 ? color.new(color.red,   90) : na, title="OB Zone")
bgcolor(rsi_val < 30 ? color.new(color.green, 90) : na, title="OS Zone")

// ── ATR ───────────────────────────────────────────────────────────────────────
atr_val = ta.atr(14)
atr_pct = atr_val / close * 100   // ATR as % of price — use this for strike spacing

// ── Trend Checks ──────────────────────────────────────────────────────────────
ema21  = ta.ema(close, 21)
ema50  = ta.ema(close, 50)
ema200 = ta.ema(close, 200)

macro_bull = close > ema200
short_bull = ema21 > ema50
ema_aligned = macro_bull and short_bull

// ── Volatility Squeeze ────────────────────────────────────────────────────────
// Squeeze = Bollinger Bands contracting near recent lows → impending breakout
// Do NOT sell premium into a squeeze; wait for expansion
bb_basis = ta.sma(close, 20)
bb_upper = bb_basis + 2 * ta.stdev(close, 20)
bb_lower = bb_basis - 2 * ta.stdev(close, 20)
bb_width = (bb_upper - bb_lower) / bb_basis * 100
bb_width_low = ta.lowest(bb_width, 60)
in_squeeze = bb_width <= bb_width_low * 1.10   // within 10% of 60-bar low

// ── VWAP Position ─────────────────────────────────────────────────────────────
var float cv = 0.0
var float cvp = 0.0
var float cvp2 = 0.0
if ta.change(time("D")) != 0 or barstate.isfirst
    cv := 0.0 | cvp := 0.0 | cvp2 := 0.0
cv += volume | cvp += hlc3 * volume | cvp2 += hlc3 * hlc3 * volume
vwap = cvp / cv
above_vwap = close > vwap

// ── Premium Sell Conditions ───────────────────────────────────────────────────
// All must be true for a high-quality premium sell setup
cond_trend    = macro_bull                        // above 200 EMA
cond_rsi      = rsi_val > 35 and rsi_val < 65    // not at extreme
cond_squeeze  = not in_squeeze                    // vol is expansive, premium is fat
cond_vwap     = above_vwap                        // bullish intraday bias
sell_score    = (cond_trend ? 1 : 0) + (cond_rsi ? 1 : 0) + (cond_squeeze ? 1 : 0) + (cond_vwap ? 1 : 0)
sell_signal   = sell_score >= 3

// ── Dashboard Table ───────────────────────────────────────────────────────────
var table t = table.new(position.top_right, 2, 9,
     bgcolor     = color.new(color.black, 15),
     border_color = color.new(color.gray, 50),
     border_width = 1,
     frame_color  = color.new(color.gray, 30),
     frame_width  = 1)

if barstate.islast
    // Header
    table.cell(t, 0, 0, "SIGNAL",  text_color=color.white, bgcolor=color.new(color.navy, 20), text_size=size.small, text_halign=text.align_left)
    table.cell(t, 1, 0, "STATUS",  text_color=color.white, bgcolor=color.new(color.navy, 20), text_size=size.small, text_halign=text.align_center)

    // Macro trend
    table.cell(t, 0, 1, "Macro Trend (200 EMA)", text_color=color.silver, text_size=size.small, text_halign=text.align_left)
    table.cell(t, 1, 1, macro_bull ? "BULL ▲" : "BEAR ▼",
               text_color=macro_bull ? color.lime : color.red, text_size=size.small, text_halign=text.align_center)

    // Short trend
    table.cell(t, 0, 2, "Short Trend (21/50 EMA)", text_color=color.silver, text_size=size.small, text_halign=text.align_left)
    table.cell(t, 1, 2, short_bull ? "BULL ▲" : "BEAR ▼",
               text_color=short_bull ? color.lime : color.red, text_size=size.small, text_halign=text.align_center)

    // VWAP
    table.cell(t, 0, 3, "vs VWAP", text_color=color.silver, text_size=size.small, text_halign=text.align_left)
    table.cell(t, 1, 3, above_vwap ? "ABOVE ↑" : "BELOW ↓",
               text_color=above_vwap ? color.lime : color.red, text_size=size.small, text_halign=text.align_center)

    // RSI
    table.cell(t, 0, 4, "RSI (14)", text_color=color.silver, text_size=size.small, text_halign=text.align_left)
    table.cell(t, 1, 4, str.tostring(math.round(rsi_val, 1)),
               text_color=rsi_val > 70 ? color.red : rsi_val < 30 ? color.lime : color.white,
               text_size=size.small, text_halign=text.align_center)

    // ATR %
    table.cell(t, 0, 5, "ATR % (move/day)", text_color=color.silver, text_size=size.small, text_halign=text.align_left)
    table.cell(t, 1, 5, str.tostring(math.round(atr_pct, 2)) + "%",
               text_color=color.yellow, text_size=size.small, text_halign=text.align_center)

    // BB Squeeze
    table.cell(t, 0, 6, "Vol Squeeze", text_color=color.silver, text_size=size.small, text_halign=text.align_left)
    table.cell(t, 1, 6, in_squeeze ? "ON ⚡ (wait)" : "OFF (ok)",
               text_color=in_squeeze ? color.orange : color.gray, text_size=size.small, text_halign=text.align_center)

    // Sell score
    table.cell(t, 0, 7, "Setup Score", text_color=color.silver, text_size=size.small, text_halign=text.align_left)
    score_color = sell_score >= 3 ? color.lime : sell_score == 2 ? color.yellow : color.red
    table.cell(t, 1, 7, str.tostring(sell_score) + " / 4",
               text_color=score_color, text_size=size.small, text_halign=text.align_center)

    // Final signal
    sig_bg = sell_signal ? color.new(color.green, 60) : color.new(color.red, 60)
    table.cell(t, 0, 8, "SELL PREMIUM",
               text_color=color.white, bgcolor=sig_bg, text_size=size.small, text_halign=text.align_left)
    table.cell(t, 1, 8, sell_signal ? "FAVORABLE ✓" : "WAIT ✗",
               text_color=color.white, bgcolor=sig_bg, text_size=size.small, text_halign=text.align_center)
```

---

## How to Load These in TradingView

1. Open TradingView → any chart (SPY, daily)
2. Click **Pine Editor** at the bottom
3. Paste Script 1 → **Add to chart**
4. Repeat for Script 2 (also overlay)
5. For Script 3: paste → **Add to chart** → right-click the indicator → **Move to → New pane below**
6. Save each as a Favorite for reuse

To apply the same layout to multiple charts: **Save as Layout** → share across devices.

---

## Reading the Chart — Practical Guide

### Trend Identification (daily timeframe)

| Setup | Signal | Action |
|---|---|---|
| Price > EMA200, 9 EMA > 21 EMA | Uptrend | Sell puts, bull put spreads |
| Price < EMA200, 9 EMA < 21 EMA | Downtrend | Sell calls, bear call spreads |
| Mixed | Ranging | Iron condors, iron flies |
| 9 EMA crossing 21 EMA | Trend change | Wait 1-2 days for confirmation |

### VWAP Zones

| Location | Meaning | Implication |
|---|---|---|
| Between ±1σ | Equilibrium (68% of price action) | Normal — sell premium here |
| Near ±2σ | Extended, mean-reversion likely | Wait for bounce before entry |
| Price breaks above +2σ | Strong momentum | Don't short calls into momentum |
| Price breaks below -2σ | Panic / flush | Wait — could bounce violently |

### Strike Placement Rule

Use ATR % from the dashboard to determine minimum strike distance:
```
ATR % = 1.2%  → price is $500 → daily ATR = $6
Short put strike = PDL minus (ATR × 1.0)
Short call strike = PDH plus (ATR × 1.0)
```

Example (SPY at $580, ATR% = 1.1%, ATR = $6.38):
- PDL = $574 → sell $574 put or lower
- PDH = $583 → sell $584 call or higher
- Iron condor: $570/$574 put spread / $583/$587 call spread

### When NOT to Sell Premium

- BB Squeeze is ON (impending expansion will eat your credit)
- VIX is > 35 (bid-ask spreads blow out, fills terrible, tail risk high)
- Within 2 days of FOMC, CPI, or NFP — skip that week or go very wide
- RSI < 30 or > 70 on daily (trend may accelerate, not reverse)
- Price outside VWAP ±2σ (skip — wait for mean reversion)

---

## Recommended Indicator Stack in TradingView (in order of priority)

| # | Indicator | Settings | Pane |
|---|---|---|---|
| 1 | **Hermes Key Levels** (Script 1) | defaults | Overlay |
| 2 | **Hermes Trend & VWAP** (Script 2) | defaults | Overlay |
| 3 | **Hermes Options Dashboard** (Script 3) | defaults | Below |
| 4 | **Volume** (built-in) | default bars | Below |
| 5 | **VIX** (built-in compare overlay) | separate chart recommended | — |

Optional adds for confirmation:
- **MACD** (12, 26, 9) — trend momentum confirmation
- **Stochastic RSI** (3, 3, 14, 14) — overbought/oversold, faster than RSI
- **Volume Profile** (visible range) — TradingView Pro feature, shows where most contracts traded

---

## The Premium Seller's Daily Checklist

Before entering any position, run through this:

```
[ ] 1. Check VIX level — is IV environment suitable?
[ ] 2. Open SPY daily — what's the regime? (above/below 200 EMA)
[ ] 3. Identify PDH, PDL, PWH, PWL on chart
[ ] 4. Check Dashboard: Score 3/4 or 4/4?
[ ] 5. Is BB squeeze OFF? (don't sell thin premium)
[ ] 6. Pick strikes BEYOND the nearest key level
[ ] 7. Confirm IVR > 30 on broker platform (tastytrade, TOS)
[ ] 8. Size position at 1-2% max risk of account
[ ] 9. Set GTC order to close at 50% profit
[ ] 10. Set alert if price reaches tested strike (don't watch all day)
```
