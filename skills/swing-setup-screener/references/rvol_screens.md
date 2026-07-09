# RVOL Screens: In-Play · Unusual Volume · Weak — Framework

Three screens whose live-product counterparts are **intraday scanners**
(sources: "Breakout: In-Play", "Volume: Unusual", "Breakdown: Weak"). This
skill reconstructs them honestly from the last completed daily bar. Read
the timing contract first — it is the difference between using these
correctly and losing money with them.

## The Timing Contract (why these are next-session watchlists)

The live product measures RVOL mid-session against expected pace at that
time of day, flags names at 10:04, and keys entries off VWAP and premarket
levels. Daily bars know none of that:

- The full-day RVOL, day change, ATR%, dollar volume, and close location
  computed here are **exact** — but they exist only after the close.
- A +8% spike that faded to +1% by the close and a steady +1% grind look
  nearly identical in a daily bar (close location recovers some of this —
  hence the faded-close cap).
- VWAP, premarket high/low, spreads, halts, float: **not present, never
  synthesized**. Plan blocks say `mark_live`.

So these screens answer *"what showed abnormal participation today"* →
tomorrow's focus list. That matches the In-Play doc's own framework: "the
best setups happen on Day 2 or Day 3… Day 2-3 consolidation is where the
best risk/reward entries happen." What they can never support is a live
Day-1 momentum entry — the report banner says so on every run.

## In-Play (long momentum watchlist)

Gate: full-day RVOL ≥ 2 (doc: "IN-PLAY RVOL 2x+") and day change ≥ +3%
(*ours*). Factors (ours): rvol_strength .30 · day_move .20 ·
close_strength .20 · range_expansion .15 · liquidity .15.

Cap: close in the lower half of the day's range (`faded_close…`) → C. A
strong day that closes weak is the daily-bar signature of the doc's "big
early move then volume fades" trap.

Multi-day framework (doc): Day 1 discovery (identify catalyst, mark
levels), Day 2-3 consolidation → pullback-to-support entries (tight stop
below support, T1 = Day-1 high), Day 4+ trend or exhaustion. The plan block
carries `day1_high` / `day1_low` for exactly this.

## Unusual Volume (signal, not direction)

Gate: full-day RVOL ≥ 3 (doc gauge: "scanner triggers here" at 3x+).
Direction-agnostic. The doc's four-quadrant read, computed from daily OHLC:

| Quadrant | Daily-bar rule | Bias |
|----------|----------------|------|
| accumulation | close location ≥ 0.7 | bullish |
| distribution | close location ≤ 0.3 | bearish |
| absorption | day range < 0.8× prior ATR (*ours*) | wait_for_break (capped C) |
| chop | everything else | skip (graded D per doc) |

Factors (ours): rvol_magnitude .35 · quadrant_clarity .25 ·
range_participation .20 · liquidity .20. Doc reminders embedded as
warnings: high RVOL without a known catalyst often means news is coming —
watchlist and monitor; volume signals go stale fast — re-check
participation at the next open before acting.

## Weak (short-side watchlist)

Gate: day change ≤ −3% (*ours*) and RVOL ≥ 1.5 (doc checklist). Factors
(ours): decline_magnitude .25 · rvol_strength .25 · weakness_structure .20
(lower highs) · trend_alignment .15 · close_weakness .15.

**Regime hard-gate (backtest-validated, ours):** while SPY is `risk_on`,
every candidate is capped at C. The 3-year backtest's harshest finding was
here: in a risk_on tape, stocks down ≥3% on heavy volume *bounced* ~4-5%
(median) over the next 20 sessions — mechanically shorting the weak list
was a contrarian buy signal. Treat this screen as tradeable only in
`mixed`/`risk_off` regimes, and only via the weak-bounce playbook (wait for
the lower high), never at the next open. `--no-regime-gate` disables the cap.

The label is the doc's highest-conviction rule made explicit:
`downtrend_aligned` (close < SMA50 < SMA200 — intraday weakness agreeing
with the multi-day downtrend, "significantly higher probability") versus
`counter_trend` (warned; cross-check swing-short before acting).

Caps and warnings: gap-down that closed in the top 40% of its range →
`gap_down_bought_back` capped C (doc: "avoid stocks weak only because of a
gap down that's already being bought back" — that is demand). Every
candidate carries `short_interest_unknown_check_short_squeeze_radar`; the
doc's squeeze warning (crowded shorts + catalyst = violent reversal) cannot
be evaluated from daily bars.

Playbooks (doc, condensed): short weak bounces (lower high forms on fading
volume, short the roll-over; stop = bounce reclaiming 60% of the selloff or
holding above VWAP — VWAP is live-only, mark it), or breakdown continuation
(short the support break on expanding volume closing near lows; bear trap =
instant snap-back above support, cover immediately). Intraday trend shorts
are scalps — out by strength into the close.

## Shared Discipline

- Resolve every UNKNOWN warning before risking money: catalyst, earnings
  date, short interest, live spread.
- Re-check the signal at the next open. A watchlist built at 16:15 is a
  hypothesis, not an entry.
- These three screens never override the market-regime line: fresh longs in
  `risk_off` and fresh shorts in `risk_on` need extra justification.
