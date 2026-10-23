"""Settings loader.

Loads from `.env` via pydantic-settings. The `account_type` field is locked
to `"cash"` (D-001) — TradeMaster will refuse to start if anything else is set.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    trading_mode: Literal["paper", "live"] = "paper"
    account_type: Literal["cash"] = "cash"

    enable_iron_condor: bool = False
    # Condor-alerts-only Discord mode (2026-08-19). When True, ONLY iron-condor posts
    # reach Discord — entry/exit signals (#signals), fills/settlement/summaries
    # (#trades), and the condor EOD line + all system errors (#logs). Every other job
    # (directional scan/exit, intraday macro scan, equities scanner, #research
    # briefings, the directional WebSocket stream) still RUNS in code — it just posts
    # to a no-op instead of Discord. Reversible: flip the flag, no code change. Note
    # this only silences Discord; it does NOT stop directional from booking trades
    # (that's enable_directional / directional_signals_only).
    condor_alerts_only: bool = False
    # #research channel posts: the 8 AM pre-market briefing + the 12:30 mid-day and
    # 16:05 closing market-analysis updates. False = register none of them (stops
    # the #research notifications AND saves the market-analysis LLM cost).
    enable_research: bool = True
    # Master switch for the directional (SPY/QQQ 0DTE trend) engine. When False,
    # NO new directional entries are taken — both the 15-min fallback scan job and
    # the real-time WebSocket trigger are suppressed. The directional EXIT monitor
    # still runs so any open position is managed to the close. Set False to run a
    # condor-only regime (e.g. the 2026-07-28 $50k condor-only test).
    enable_directional: bool = True
    # Study mode: when True the directional engine still scans and posts its
    # broker-ready signals to #signals, but places NO orders and books NO trades
    # (execute_directional_signal is skipped). Lets us watch what the engine WOULD
    # do — and record its live signal quality — without risking capital, while the
    # "why is directional −EV" question is open. Requires enable_directional=True.
    directional_signals_only: bool = False
    # Drop MEDIUM: post/act on HIGH-conviction signals only, for BOTH the
    # directional engine and the equities scanner. Independent of directional_mode
    # (filters conviction, not exit aggressiveness). Set 2026-07-29 after the loss
    # analysis showed MEDIUM was −$6,558 and HIGH the only profitable bucket.
    high_conviction_only: bool = False
    directional_mode: Literal["aggressive", "selective"] = "selective"
    # Platform-first: when True BOTH the directional ENTRY decision
    # (signal_engine.decide) AND the EXIT confirm (exit_monitor._rules_exit_confirm)
    # are made by deterministic rules instead of the LLM — reproducible,
    # backtestable, auditable, no LLM in the decision hot-path. The LLM path remains
    # as the fallback (flag off). See docs/STRATEGY_REVIEW_2026-06-18.md.
    deterministic_engine: bool = False
    # Puts-only: the long-CALL side fights two structural headwinds on SPY
    # (vol-crush on rallies + slow grind-up theta) and barely clears costs;
    # long PUTS harvest the negative price-vol correlation + fast down-moves and
    # are the robust side (backtest_engine.py fill-stress). When True the engine
    # skips would-be calls. Flat sizing (medium_conviction_size_mult=1.0) is the
    # companion change — conviction did not predict edge, so don't up-size HIGH.
    directional_puts_only: bool = False

    # QQQ directional restriction. QQQ shares SPY's signal quality (same engine,
    # ~identical hit-rate/edge) but pays ~2-3x the 0DTE option spread, so on the
    # SHARED directional capital cap it's dilutive — each QQQ trade displaces a
    # cheaper SPY trade. Backtest 2023-2026 (scripts/backtest_trend_0dte.py):
    #   * QQQ PUTS mean-revert wholesale at high ADX — a losing regime, NOT an
    #     entry-timing artifact (verified: extended-vs-flat entries lose equally)
    #     → blocked entirely.
    #   * QQQ CALLS are the one slice where its higher beta beats its spread, and
    #     they KEEP working at extreme ADX (59.5% hit / +0.051% move at ADX>=50)
    #     where SPY reverts → allowed, gated to ADX>=qqq_call_adx_min, and exempt
    #     from the SPY overextension HOLD.
    qqq_calls_only: bool = True
    qqq_call_adx_min: float = 40.0

    # Isolated, alert-only equities signal scanner (separate stock watchlist +
    # Discord channel; no execution, no shared capital/risk with SPY strategies).
    enable_equities_scanner: bool = False

    # Event-day blackout (NFP/CPI/FOMC). Disabled 2026-06-05 to let the LLM
    # trade event days during the paper-validation phase — we want data on every
    # regime, including high-volatility catalyst days, before deciding whether
    # the blackout earns its keep. Flip to True to restore the skip.
    enable_event_blackout: bool = False

    # Per-trade catastrophic-loss cap, as a fraction of EFFECTIVE capital
    # (auto-scales with the account). Caps deployed premium so qty × premium ×
    # 100 ≤ pct × capital. Was a fixed $500; converted to 10% on 2026-06-05 when
    # capital moved to $25k so position size scales with the budget.
    max_loss_per_trade_pct: float = Field(default=0.10, gt=0, le=1.0)

    # Conviction-scaled sizing (fix C, 2026-06-09). MEDIUM-conviction trades
    # deploy this fraction of the per-trade budget that a HIGH trade would —
    # weaker edge gets less capital. Trade #60 (MEDIUM, RSI not confirming) lost
    # full size; HIGH puts the same day all won.
    medium_conviction_size_mult: float = Field(default=0.5, gt=0, le=1.0)
    # Extra downsize when RSI-9 does not confirm the trade direction at entry
    # (a put with RSI ≥ 50, or a call with RSI ≤ 50 — i.e. momentum is not yet
    # on our side). Multiplies on top of the conviction multiplier.
    weak_rsi_size_mult: float = Field(default=0.5, gt=0, le=1.0)

    # Re-entry freshness gate (fix B, 2026-06-09; tightened 2026-06-10). Once
    # this many consecutive same-direction trades have opened today, a further
    # same-direction entry must look like a FRESH leg — not a late chase of an
    # exhausted move. The 3rd same-direction trade was 0-for-4 (−$5,198): it
    # always entered as the morning move was spent and got caught by the bounce
    # (peaks of 0–10% vs 16–242% for trades 1–2). Default 2 → the 3rd+ entry is
    # gated. A direction flip resets the streak. 0 = disabled.
    reentry_same_direction_limit: int = Field(default=2, ge=0)
    # A gated re-entry is allowed only if EITHER it's pulled back ≥ this fraction
    # of the day's range from the move's extreme (consolidation → room for a new
    # leg), OR it's a fresh break to a new extreme with volume ≥ the floor below.
    reentry_pullback_range_frac: float = Field(default=0.30, gt=0, le=1.0)
    reentry_fresh_volume_min: float = Field(default=1.5, gt=0)

    # 0DTE indicator-independent early-cut (fix, 2026-06-11). Early in the session
    # RSI/EMA/volume haven't warmed up, so the ≥2-signal thesis gate can't fire
    # and the LLM mismanages 0DTE losers. On a 0DTE position past this loss with
    # price through VWAP against it, cut immediately — no LLM, no indicator wait.
    # (#64: a 0DTE call rode to −50% / −$1,490 while the LLM held it.)
    zdte_early_loss_cut_pct: float = Field(default=0.25, gt=0, le=1.0)

    # Evidence-based chop filter (fix, 2026-06-11). After this many "failed
    # breakouts" today — entries that peaked below chop_failed_peak_pct and
    # closed at a loss (immediate reversals) — pause ALL new directional entries
    # for chop_pause_minutes. A cluster of failed breakouts means the regime is
    # choppy and momentum entries keep getting trapped (today #64/#65/#66 each
    # peaked <10% then reversed). 0 = disabled. Pause measured from the most
    # recent failed-breakout close.
    chop_failed_breakout_limit: int = Field(default=2, ge=0)
    chop_failed_peak_pct: float = Field(default=10.0, gt=0)
    # 90 (was 45): trades are spaced ~65–75 min apart, so a 45-min pause leaked
    # one entry per hour (#67 slipped through 2026-06-11). 90 covers the cadence.
    chop_pause_minutes: int = Field(default=90, ge=0)

    # ADX trend-strength gate (proactive chop filter, fix 2026-06-11). ADX
    # measures trend STRENGTH (0–100, direction-agnostic): low = choppy, no trend
    # to ride, so momentum breakouts fail. Unlike the evidence-based filter (which
    # waits for 2 failed breakouts), this reads it from price and can refuse the
    # FIRST bad entry. Below adx_block_below → skip the entry; between that and
    # adx_full_above → deploy adx_weak_size_mult of normal; at/above → full size.
    # ADX unavailable (early session, too few bars) → no gate (fail-open).
    # Intraday 5-min thresholds run lower than the daily-chart "25" convention;
    # tune from live data.
    adx_block_below: float = Field(default=18.0, ge=0)
    adx_full_above: float = Field(default=25.0, gt=0)
    adx_weak_size_mult: float = Field(default=0.5, gt=0, le=1.0)

    # The trailing stop trails CONTINUOUSLY at (peak − this gap) across the
    # whole in-profit range (once past the lowest ladder tier), so the stop
    # always sits within `gap` of the high-water mark. 0.10 → a +70% peak locks
    # +60%, +200% locks +190%, etc. (tightened 2026-06-08 from 0.20: trade #51
    # peaked +70% but the discrete ladder only locked +20%, giving back ~$1,200).
    trailing_stop_trail_gap_pct: float = Field(default=0.10, gt=0, le=1.0)

    # Scale-out / trailing-stop ladder override. Empty = use the code default
    # (DEFAULT_TRAILING_STOP_LEVELS in exit_monitor). Set a JSON array of
    # [trigger_pct, lock_pct, sell_frac] to A/B a different ladder without a
    # code change, e.g. '[[120,0.75,0],[80,0.45,0],[50,0.20,0.25],[25,0.08,0.25]]'.
    trailing_stop_levels: str = ""

    # Starting capital baseline for the directional flow. The actual
    # effective capital is computed dynamically — see trademaster/capital.py.
    # In paper mode, effective = this base + cumulative realized P&L (since
    # baseline_reset_at). In live mode, effective = account.equity directly.
    trading_capital_usd: Decimal = Field(default=Decimal("5000"), gt=0)

    # Isolated directional capital pool (2026-07-29). The directional engine's
    # sizing (exposure + per-trade caps) and its loss limits run off THIS pool —
    # directional_capital_usd + directional-only realized P&L — kept fully
    # separate from the iron condor's trading_capital_usd. Set $10k directional /
    # $50k condor to test the two strategies side-by-side without shared risk.
    # Paper-mode only (a single live account can't be split). See
    # capital.get_effective_capital(strategy_group="directional").
    directional_capital_usd: Decimal = Field(default=Decimal("10000"), gt=0)

    # Baseline reset: if set, the dynamic-capital calc ignores all trades
    # closed before this UTC timestamp. Use to start fresh after major
    # strategy changes without losing the audit history. Set via .env:
    #   BASELINE_RESET_AT=2026-05-14T03:30:00Z
    baseline_reset_at: datetime | None = None

    # Daily loss limit: 15% of effective capital. Counts realized P&L (closed
    # trades today) + unrealized (open positions). When hit, trading halts
    # until the next calendar day (ET). Note: because capital itself shrinks
    # with today's realized losses, the actual halt point is base × pct/(1+pct)
    # ≈ $652 on a $5k account, not the nominal $750. Conservative by design.
    daily_loss_limit_pct: float = Field(default=0.15, gt=0, le=1.0)

    # Weekly loss limit: 25% of effective capital Mon–Sun (ET). Prevents a
    # string of bad days compounding into a full account wipeout. Halts trading
    # for the remainder of the week when hit.
    weekly_loss_limit_pct: float = Field(default=0.25, gt=0, le=1.0)

    # Iron-condor position size (contracts per entry). Defined-risk, so P&L and
    # drawdown scale LINEARLY with this (defined-risk P&L is linear in contracts,
    # so the edge — win%, Sharpe, DSR — is size-invariant). The cap exists only to
    # keep the drawdown sane vs the pool at the CURRENT capital. Re-validated at
    # $50k (scripts/backtest_condor_sizing.py 50000 10,20): 10ct worst week −$5,398,
    # 20ct −$10,796 (both < the $12,500 weekly limit); 20ct's worst SINGLE trade
    # −$7,684 = 15.4% of $50k breached the 15% daily line ONCE in 3.5y, 10ct never.
    # Cap raised 12→20 (07-29) then 20→60 (07-30) as the user scaled per-trade risk
    # 9.1%→18.2%→50% of $50k (~55ct ≈ $25k committed). At this size the condor has a
    # WEEKLY loss-halt (condor_weekly_loss_limit_pct) as the circuit-breaker — a
    # single max-loss day (~$25k) still can't be capped (defined risk is on before
    # the halt checks), but the halt stops the rest of the week after a bad day.
    condor_contracts: int = Field(default=1, ge=1, le=60)

    # Condor WEEKLY loss-halt as a fraction of the condor pool (trading_capital_usd).
    # 0 = disabled (default). Checked before the daily 10:00 entry: if this week's
    # realized condor P&L ≤ −(pct × pool), the condor is paused (condor-only) until
    # Monday — directional keeps trading. Set 0.125 (= $6,250 on $50k) 2026-07-30.
    condor_weekly_loss_limit_pct: Decimal = Field(default=Decimal("0"), ge=0)

    # Condor DAILY hard loss cap as a fraction of the pool (0 = disabled). Enforced
    # intraday by the exit monitor: if an open condor's per-contract loss reaches
    # pct × pool ÷ qty, it force-closes MARKETABLY — so no single day can lose more
    # than this % of the pool. Higher threshold than the 1.5× stop (which cuts
    # first at a smaller loss); this is the backstop for a fast gap that jumps past
    # the stop between sweeps. Set 0.15 (= $3,750 on $25k) 2026-07-31.
    condor_daily_loss_limit_pct: Decimal = Field(default=Decimal("0"), ge=0)

    # DISTANCE-AWARE STOP (opt-in, default OFF). The 1.5× intraday stop triggers on
    # the mark-to-market loss alone. On thin-credit days 1.5× of a small credit is a
    # tiny move, so the stop fires on the OTM APPROACH — while SPY is still short of a
    # short strike — then SPY reverts and the position would have expired for full
    # credit (a whipsaw; the 2026-09-01..04 week was 4/4 such whipsaws). When True,
    # the stop is SUPPRESSED unless SPY is within condor_stop_arm_band_pct of a short
    # strike (or through it) — i.e. only cut on a real breach, not an approach. The
    # daily_loss_cap (fast-gap backstop) and force_close are NOT gated. Backtest
    # (scripts/backtest_condor_stop_compare.py): ~halves the worst day (−29%→−15%)
    # while lifting total, vs just widening the multiplier which does neither. Flip
    # CONDOR_DISTANCE_AWARE_STOP=true in .env to test in paper.
    condor_distance_aware_stop: bool = False
    # Band around a short strike within which the distance-aware stop is armed.
    # 0.0 = fire only once SPY is AT or THROUGH a short strike (the best-worst-day
    # backtest point). A small positive value arms slightly earlier (more protective,
    # a touch more whipsaw). Ignored unless condor_distance_aware_stop is True.
    condor_stop_arm_band_pct: float = Field(default=0.0, ge=0)

    # TIME-SCALED STOP ARMING (opt-in; both 0 = off, keeps the band behaviour above).
    #
    # The 1.5x stop fires when SPY sits ON a short strike — which is exactly when
    # the short's TIME VALUE is at its maximum. Measured over the 11 stop-outs to
    # 2026-10-22: $1,786 per stop was time value, only the remainder was intrinsic
    # (#192 was 95% time value, #196 73%). That premium decays to zero on its own;
    # buying it back converts a recoverable position into a realised loss.
    #
    # So require SPY to be measurably THROUGH the strike before arming, and scale
    # the requirement by time remaining — deep early (expensive to close, lots of
    # time to recover), shallow late (cheap to close, a late breach tends to stick):
    #
    #   depth(mtc) = close_pct + (open_pct - close_pct) x (mtc / 345)
    #
    # armed  <=>  spot <= short_put x (1 - depth)  OR  spot >= short_call x (1 + depth)
    #
    # 0.0026 ~= $2.00 on SPY at 770; 0.0003 ~= $0.25. On those settings neither
    # #196 (0.36 through, 73% TV) nor #192 (0.07 through, 95% TV) would have armed
    # in the morning, and both settled back inside their bands.
    #
    # ⚠️ TRADE-OFF: a deeper requirement lets a genuine trend-day breach run further
    # before cutting. The band sweep (scratchpad, 2026-10-22) showed 3 whipsaws
    # recovered but 2 real breaches ran ~$5k deeper, non-monotonically — which is
    # why this is time-scaled rather than a flat deeper band.
    condor_stop_arm_through_pct_open: float = Field(default=0.0, ge=0, lt=0.05)
    condor_stop_arm_through_pct_close: float = Field(default=0.0, ge=0, lt=0.05)

    # On a LOSS-CUT (stop / daily cap), also sell the paired LONG leg instead of
    # letting it expire. The stop fires with SPY at/through the short, so the long
    # is only the wing width further out and still carries premium — measured at
    # $186 per stop across the 11 stop-outs. One extra sell order at the same
    # moment recovers it. Not applied to the 15:45 assignment leg-out, where the
    # position is usually a winner and the longs really are worthless.
    condor_sell_longs_on_stop: bool = True

    # STOP PERSISTENCE (de-bounce). Require the stop condition — debit over the
    # 1.5x trigger AND SPY through a short strike — to hold CONTINUOUSLY for this
    # many seconds before cutting. 0 = fire on first observation (old behaviour).
    #
    # This is the only filter tested that separates a whipsaw from a real breach,
    # and the reason is mechanical: a real breach STAYS through the strike, a
    # whipsaw pops back. Depth filters ("how far through") cannot tell them apart
    # because both start shallow; persistence ("how long through") can.
    #
    # Replayed on the 7 stop-outs with settlement data (real 1-min bars, within
    # replay so the fill model is constant):
    #     fire-on-sight -14,980 | 2min -13,608 | 3min -9,940 | 5min -10,220
    #     8min -9,996 | 12min -8,764      (HOLD everything = -16,688)
    # Improves across the whole range rather than at one value, and the genuine
    # breaches (#192, #176) still get cut — only the whipsaws (#196, #173, #187)
    # are allowed to recover.
    #
    # ⚠️ n=7. And with a 2-minute sweep the granularity is coarse: 180s needs two
    # or three consecutive armed sweeps. A tier-1 spot poll at 15s would make this
    # precise — that is the real argument for two-tier monitoring.
    condor_stop_persist_seconds: int = Field(default=0, ge=0, le=900)

    # TIER-1 SPOT POLLING. Seconds between cheap spot-only proximity checks
    # (0 = disabled; tier-2's own cron is then the only sweep).
    #
    # Two-tier exit monitoring: tier-1 fetches ONE SPY quote and escalates to the
    # full tier-2 sweep (chain fetch, debit, stop/target/force-close) only when
    # SPY is within condor_tier1_band_pct of a short strike. The rest of the
    # session costs one quote per tick and no chain at all.
    #
    # The payoff is PRECISION, not raw speed. The stop de-bounce measures how
    # long a breach has HELD, and at a 2-minute sweep 180s is only 2-3 coarse
    # samples; at 15s it is 12. Speed alone was shown to be harmful (#196,
    # 2026-10-22, cost $3,808 when the */2 sweep caught a 2-minute arming
    # window the */5 sweep would have missed) — it only pays WITH the de-bounce.
    condor_tier1_seconds: int = Field(default=0, ge=0, le=300)
    # Proximity band for escalation. Deliberately WIDER than the stop's own arm
    # band so tier-1 wakes tier-2 just BEFORE the touch — otherwise the de-bounce
    # clock would not start until the breach was already underway.
    condor_tier1_band_pct: float = Field(default=0.001, ge=0, lt=0.02)

    # ASSIGNMENT CONFIG (opt-in, default OFF; SPY-only — XSP is cash-settled and needs
    # none). SPY options are physically settled, so an ITM short at expiry delivers
    # shares (assignment) = unbounded overnight risk. When on, at the force-close
    # deadline the exit monitor closes ONLY the short leg(s) within
    # condor_assign_close_buffer_pct of spot (or ITM) via single-leg BUY-to-close
    # (marketable), and lets comfortably-OTM shorts + all longs expire — a cheaper
    # leg-out that replaces the near-strike 4-leg force-close. Backtest
    # (scripts/backtest_selective_close.py): 0 assignment at ~−23% (ideal) to −64%
    # (realistic slippage) of profit. Flip CONDOR_ASSIGNMENT_CLOSE=true to enable.
    condor_assignment_close: bool = False
    condor_assign_close_buffer_pct: float = Field(default=0.003, ge=0)

    # PROFIT TARGET (opt-in, default OFF). Close the condor once the buy-back debit
    # falls to (1 - pct) x credit, i.e. banking `pct` of max profit, instead of
    # holding for the full credit at expiry. 0.5 = take 50% and go flat.
    #
    # ⚠️ TESTED AND NOT RECOMMENDED AS AN EDGE PLAY — see
    # [project_condor_whipsaw_verdict]. Five formulations were measured on real
    # option bars; every one's gain was the 2-3 whipsaw trades it rescues and a
    # loss on everything else (the 75% variant helped 7 trades and hurt 29). What
    # IS real and robust is VARIANCE reduction: ~-40% per-trade sd and roughly
    # half the max drawdown, at a return cost between $0 and ~$1,500/month
    # depending on real 4-leg close slippage, which paper cannot measure. Enable
    # only as a deliberate smoothness-for-return trade, not expecting more profit.
    condor_profit_target_pct: Decimal = Field(default=Decimal("0"), ge=0, lt=1)
    # ABSOLUTE-dollar profit target: close once the position's OPEN PROFIT across
    # all contracts reaches this many dollars. 0 = disabled.
    #
    # Behaves very differently from the pct target on HIGH-credit days: a fixed
    # $500 is reached far earlier than 50% of a rich credit. On #173 (09-16,
    # $59/ct credit) $500 needed the mark to fall to $41.10 and banked out in the
    # morning (+$560), while 50% needed $29.50, was never reached, and the trade
    # rode to -$6,020. That single trade is most of the $500 rule's historical
    # advantage — and it was the FOMC-blackout bug, which is now fixed and cannot
    # recur, so do not expect that edge to repeat.
    #
    # If BOTH this and condor_profit_target_pct are set, whichever is reached
    # FIRST wins (the higher debit threshold, since the debit falls as profit
    # accrues).
    condor_profit_target_usd: Decimal = Field(default=Decimal("0"), ge=0)

    # TRAILING profit lock (opt-in, 0 = off). Instead of one fixed take-profit,
    # ratchet a floor upward as profit climbs and exit when profit falls back to
    # it — so a trade that runs to $900 and reverses books ~$800 rather than
    # giving it all back, while one that keeps running is not capped early.
    #
    #   trail_usd      the FIRST rung and the minimum the floor can ever be.
    #                  Below this there is no floor and the existing strategy
    #                  (stop / leg-out / full-credit expiry) runs untouched.
    #   trail_step_usd rung spacing. The floor is the highest rung profit has
    #                  touched; exit when profit falls back to it.
    #   trail_cap_usd  hard take-profit — bank it and go flat. 0 = no cap.
    #
    # Worked example at 500/100/1000: peak $720 -> floor $700 -> exit if profit
    # retreats to $700. Peak $650 -> floor $600. Peak $1,000 -> exit at once.
    #
    # ⚠️ The rungs are ABSOLUTE DOLLARS while max profit per trade varies a lot
    # (28ct era: min $644, median $1,008, max $1,652), so the ladder bites very
    # differently by day. $500-700 is reachable on ~100% of days, $800 on 80%,
    # $1,000 on 53%. On a thin-credit day the whole ladder sits near max profit.
    # ⚠️ The floor is only checked on the 5-minute exit sweep, so a fast
    # round-trip between sweeps can skip past it.
    # On a profit-target exit, buy back only the short(s) near the money and let
    # the far short + both longs expire free, instead of closing both shorts.
    # The far short is nearly worthless, so closing it is pure spread cost: on
    # #189 (2026-10-12) closing BOTH cost $5/ct where the near leg alone was
    # $3/ct, turning a +$644 baseline into +$588.
    #
    # ⚠️ Leaves the far short spread open to expiry. Harmless when the target
    # fires late in the session (both firings so far were 15:40) but an early
    # firing leaves that leg exposed for hours. Set false to close both shorts.
    condor_profit_target_selective: bool = True
    condor_profit_trail_usd: Decimal = Field(default=Decimal("0"), ge=0)
    condor_profit_trail_step_usd: Decimal = Field(default=Decimal("100"), gt=0)
    condor_profit_trail_cap_usd: Decimal = Field(default=Decimal("0"), ge=0)
    # Gate the profit target on the session's OPENING RANGE — SPY's 09:30-10:00
    # high-low as a percentage of the opening price. 0 = apply on EVERY day.
    # Measured distribution (28ct era, n=45): min 0.11, q1 0.20, median 0.24,
    # q3 0.31, max 0.59.
    condor_profit_target_open_range_pct: float = Field(default=0.0, ge=0)
    # Which SIDE of that threshold the target applies to.
    #
    # True (default) = apply on QUIET mornings (range <= threshold). This is the
    # direction the data supports: quiet mornings produce a LOWER VIX1D, so the
    # engine sets TIGHTER strikes (mean band 7.0pt vs 8.0pt) with less cushion,
    # and they are where the losses are — calm mornings ran 13W/9L at -$416/trade
    # vs active mornings 20W/3L at +$680/trade, and all seven worst trades in the
    # 28ct era had a below-median opening range.
    #
    # False = apply on ACTIVE mornings (range >= threshold). Intuitive ("big
    # movement will break the range") but BACKWARDS on this evidence: it caps the
    # days that are already winning and leaves the losing days unprotected.
    # Exposed so the intuition can be tested rather than argued about.
    condor_profit_target_on_quiet: bool = True

    # STOP SINGLE-LEG (opt-in). The 1.5× stop / daily cap / force-close historically
    # unwound via a 4-leg MLEG combo order, which won't fill on illiquid/fast books —
    # it canceled unfilled on the 2026-09-16 FOMC breach and the loss rode to −$6k
    # (~−24%) instead of ~1.5× credit. When True, these loss-cuts go SINGLE-LEG FIRST:
    # buy back BOTH shorts via single-leg market orders directly (each hits its own deep
    # book → fills reliably, no MLEG attempt / no 60s timeout); longs expire and the
    # reconciler prices the residual off the fills — the same reliable mechanism the
    # assignment config uses. When False, the legacy 4-leg MLEG close is tried first,
    # but it ALWAYS falls back to single-leg if the MLEG fails (unconditional safety).
    condor_stop_single_leg: bool = False

    # XSP SLEEVE (foundation; default OFF). Run a cash-settled Mini-SPX (XSP) condor
    # side-by-side with SPY on its OWN isolated pool — same signal/strikes/gate (XSP
    # tracks the same S&P index), so it's a clean A/B: SPY (physical, assignment config)
    # vs XSP (European, cash-settled → needs NO assignment config, ~zero exit slippage).
    # Books as 'xsp_0dte_ic'. Distance-aware stop applies; assignment config does NOT.
    # NOT yet wired into entry/exit/reconciler — flipping this alone does nothing until
    # the parallel scheduler jobs land (Phase 2). See [[project_xsp_cash_settled]].
    enable_xsp_condor: bool = False
    xsp_capital_usd: Decimal = Field(default=Decimal("25000"), ge=0)

    # Condor TREND filter: HOLD the condor when the prior-day Wilder ADX is ≥ this
    # (0 = disabled). A trending prior day breaches the range-bound condor. The v2
    # engine dropped the ADX gate as over-conservative, but a re-backtest
    # (scripts/backtest_condor_vix_gate.py, 2026-08-04) showed VIX1D<35 & ADX<25
    # IMPROVES OOS Sharpe (+2.54→+3.10) and win rate (73→76%) while still trading
    # ~495/858 days. <22 over-filters (FAILS the DSR gate). Set 25 on 2026-08-04.
    condor_max_adx: float = Field(default=0.0, ge=0)

    # Tiered daily trade caps. **0 = UNLIMITED** (no per-day count cap) — set as
    # the default 2026-06-07: with capital at $25k and risk bounded by the daily
    # loss limit (15%), per-trade cap (10%), 30% exposure cap, and the per-ticker
    # / per-action cooldowns, the trade-count cap was the binding throttle and is
    # no longer wanted. Set a positive number to re-impose a cap.
    max_trades_per_day: int = Field(default=0, ge=0)        # 0 = unlimited
    max_medium_trades_per_day: int = Field(default=0, ge=0)  # 0 = unlimited

    # No entries before this ET time (opening volatility) or after no_entry_after_et.
    # Format: "HH:MM" 24-hour ET.
    no_entry_before_et: str = Field(default="10:00")
    no_entry_after_et: str = Field(default="14:30")

    # Max bid/ask spread as a fraction of mid price. Options with wider spreads
    # are illiquid — you pay too much slippage entering and exiting.
    # 0.50 = reject any option where spread > 50% of mid (e.g. bid=0.80, ask=1.20).
    max_bid_ask_spread_pct: float = Field(default=0.50, gt=0, le=1.0)

    # Iron-condor-only legacy caps (risk_manager path). The directional flow
    # uses dynamic capital sizing in capital.py + scheduler.py and ignores
    # these. Keep them for the iron-condor strategist if/when it's re-enabled.
    max_position_size_usd: Decimal = Field(default=Decimal("2000"), gt=0)
    max_concurrent_positions: int = Field(default=5, gt=0)
    max_options_contracts_per_trade: int = Field(default=5, gt=0)

    # Max total capital deployed across all open directional positions at once.
    # 30% of effective capital — the full remaining budget is used per trade
    # (no per-trade fraction). With SPY-only focus, concentration is the goal.
    max_total_exposure_pct: float = Field(default=0.30, gt=0, le=1.0)

    monthly_llm_budget_usd: Decimal = Field(default=Decimal("100"), gt=0)

    alpaca_api_key: SecretStr = SecretStr("")
    alpaca_api_secret: SecretStr = SecretStr("")
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_url: str = "https://data.alpaca.markets"
    # Options market-data feed. "indicative" (default) = free, delayed/estimated
    # quotes with no greeks — fine for paper. "opra" = real-time OPRA, which
    # REQUIRES the Alpaca Algo Trader Plus subscription (~$99/mo). Must be "opra"
    # for LIVE 0DTE trading: strike selection and the exit stops (1.5× stop, daily
    # cap) need real-time marks. Cutover is this one env var — no code change.
    # See the go-live checklist. An "opra" request without the subscription will
    # error/return empty, so only flip this once the subscription is active.
    alpaca_options_feed: Literal["indicative", "opra"] = "indicative"

    anthropic_api_key: SecretStr = SecretStr("")
    deepseek_api_key: SecretStr = SecretStr("")
    google_api_key: SecretStr = SecretStr("")

    discord_bot_token: SecretStr = SecretStr("")
    discord_guild_id: str = ""
    discord_channel_signals: str = ""
    discord_channel_trades: str = ""
    discord_channel_research: str = ""
    discord_channel_logs: str = ""
    discord_channel_commands: str = ""
    discord_channel_watchlist: str = ""
    discord_channel_stock_signals: str = ""  # equities scanner alerts (#stock-signals)

    database_url: str = "sqlite:///data/trademaster.db"

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("account_type", mode="before")
    @classmethod
    def _cash_only(cls, v: object) -> object:
        if isinstance(v, str):
            if v.lower() != "cash":
                raise ValueError(
                    "ACCOUNT_TYPE must be 'cash'. Margin and leverage are forbidden (D-001)."
                )
            return v.lower()
        return v

    def require_live_keys(self) -> None:
        """Fail fast if any provider key needed for runtime is missing.

        Called by the orchestrator at startup, not by tests.
        """
        missing = [
            name
            for name, value in {
                "ALPACA_API_KEY": self.alpaca_api_key,
                "ALPACA_API_SECRET": self.alpaca_api_secret,
                "DEEPSEEK_API_KEY": self.deepseek_api_key,
                "GOOGLE_API_KEY": self.google_api_key,
                "DISCORD_BOT_TOKEN": self.discord_bot_token,
            }.items()
            if not value.get_secret_value()
        ]
        if missing:
            raise RuntimeError(
                f"Missing required environment variables: {', '.join(missing)}"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
