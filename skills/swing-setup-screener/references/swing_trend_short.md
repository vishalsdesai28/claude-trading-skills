# Swing Trend: Shorts — Framework

Multi-day swing shorts in confirmed downtrends: price below both SMA50 and
SMA200 with a falling SMA50. Downtrends persist because institutions
distribute over days and weeks, producing lower highs and lower lows.
Source: product scanner doc "Swing Trend: Shorts"; *doc* vs *ours*
provenance as in the long reference.

## Signal and Triggers

Gate: `close < SMA50 < SMA200` and SMA50 slope over 10 sessions < 0.

**Regime hard-gate (backtest-validated, ours):** while SPY is `risk_on`
(above its SMA50 and SMA200), every candidate is capped at C — in the
3-year backtest all short grades lost money in a risk_on tape, and the
best-graded shorts were not an exception. Shorts are only tradeable-grade
in `mixed` or `risk_off` regimes. `--no-regime-gate` disables the cap.

| Trigger | Rule | Provenance |
|---------|------|------------|
| oversold | close > 10% below SMA50 | mirror of doc's extended band, ours |
| bear_flag | prior 10-session leg ≤ −8%, last 5 sessions drift 0..+4% on volume below the prior 10-session average | ours (doc describes the pattern qualitatively) |
| breakdown_ready | close within 2% of 20d low with descending 5-session block highs | ours |
| none | downtrend intact, no entry structure | — |

`oversold` and `none` are capped at grade C (doc: "Never short into
capitulation — wait for the bounce to form a bear flag").

## Mandatory Squeeze Protocol

Every candidate carries `short_interest_unknown_check_short_squeeze_radar`.
Daily bars contain no float, short interest, or locate data. Doc's explicit
warning: crowded shorts + catalyst = violent reversal; avoid low-float and
high-short-interest names. **Run short-squeeze-radar on every candidate
before shorting**, and confirm locate/HTB status at the broker.

## Factor Weights (ours)

trend_structure .25 (lower highs across 3×5-session blocks) ·
ma_alignment .25 (bearish mirror) · entry_timing .25 (bear_flag 100 /
breakdown_ready 85 / none 40 / oversold 15) · rel_weakness .15 (63-day
underperformance vs SPY) · volume_character .10 (selloffs heavier than
bounces).

## Grade Interpretation (doc)

A = textbook LH/LL with Bear Flag or Breakdown Ready and volume
confirmation, full size with defined risk. B = solid, minor imperfections.
C = downtrend intact but timing wrong (Oversold, or sitting on support) —
wait for the bear flag after the bounce. D = flattening slope or bottoming
signs — skip.

## Playbooks (doc, condensed)

**Bear Flag Breakdown (primary).** Strong selloff (flagpole), then a tight
low-volume drift up (flag). Short the break of flag support on expanding
volume, closing in the lower third; prefer the failed-reclaim retest from
below. Stop: close above the flag high (screener plan: tighter of 10d high
/ SMA50). T1 prior swing low; T2 measured move (flagpole projected). Avoid
when major support sits directly below or the flag is 10+ sessions old.

**Breakdown Ready.** Support tested repeatedly with weakening bounces.
Short a daily close below support or the failed intraday reclaim, volume
expanding. Bear trap rule: breakdown that snaps back above support —
cover immediately. Avoid after multiple failed breakdowns (level is
strengthening) or into oversold + major historical support.

**Oversold bounce → bear flag (higher risk).** Do not short the oversold
condition. Wait for the bounce to exhaust into a lower high, then short the
breakdown. If the bounce reclaims SMA50 for 3+ sessions on volume, the
downtrend may be reversing — stand down.

## Common Traps (doc)

- Shorting into capitulation "because it's oversold" — worst outcomes.
- Massive-volume bullish reversal candles are not a dead-cat bounce.
- End-of-day shorts into an already-large decline — covering bounces.
- Bounces that reclaim key MAs on expanding volume signal trend change.
