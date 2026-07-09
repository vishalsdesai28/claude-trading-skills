# Swing Trend: Longs — Framework

Multi-day swing longs in confirmed uptrends: price above both SMA50 and
SMA200 with a rising SMA50. Trends persist because institutions accumulate
over days and weeks, producing higher highs and higher lows. Source:
product scanner doc "Swing Trend: Longs"; thresholds marked *doc* come from
it, *ours* are this repo's documented defaults (CLI-overridable).

## Signal and Triggers

Gate: `close > SMA50 > SMA200` and SMA50 slope over 10 sessions > 0.

Trigger classification (checked in this order):

| Trigger | Rule | Provenance |
|---------|------|------------|
| extended | close > 10% above SMA50 | doc ("avoid entries when extended >10% above SMA50") |
| breakout_ready | close within 3% of 20d high AND 5-session range < 5% of price | 3% ours; 5%/5-day tightness doc (leaders guide) |
| pullback_zone | close ≤ SMA20 × 1.01 | doc band edge (At-SMA20 tops at +1%) |
| none | uptrend intact, no entry structure | — |

`extended` and `none` are capped at grade C (doc: Extended = wait for
pullback; long consolidation without resolution = watchlist).

## Factor Weights (ours)

trend_structure .25 (higher lows across 3×5-session blocks) ·
ma_alignment .25 (SMA50 slope, SMA50-vs-SMA200 gap, SMA200 slope) ·
entry_timing .25 (pullback 100 / breakout_ready 85 / none 40 / extended 15) ·
rel_strength .15 (63-day return vs SPY) ·
volume_character .10 (with-trend days on heavier volume than pullback days).

## Grade Interpretation (doc)

- **A** — textbook: HH/HL structure, perfect SMA alignment, Pullback Zone or
  Breakout Ready trigger, relative strength vs market. Full swing size with
  defined risk.
- **B** — solid with one imperfection (weaker RS, pullback not quite ideal).
  Standard sizing.
- **C** — trend intact but timing wrong (Extended, or unresolved
  consolidation). Watchlist; wait.
- **D** — marginal trend, flattening SMA50, fading momentum. Skip.

## Playbooks (doc, condensed)

**Pullback Zone entry (primary).** Controlled pullback to SMA50 or the
prior breakout level on lighter volume. Buy the first bullish reversal
candle at support with a volume uptick. Stop: below the pullback swing low,
or a decisive close below SMA50 (screener plan uses the tighter of the
two). T1: prior swing high; scale 1/3, trail below higher lows. Avoid when
the pullback is on heavy distribution volume or earnings are within 5 days.

**Breakout Ready entry.** Tight range near highs on declining volume. Buy a
clean break of consolidation resistance closing in the upper quarter on
1.5×+ volume, or the successful retest. Failed hold next session = exit.
Avoid after 6+ weeks of dead consolidation or multiple failed attempts.

**Extended (momentum continuation).** Only on a 1-2 day micro-pullback
holding the 10-day MA — never chase an extended close. Reduce size 30-50%.
The screener grades these C so they stay watchlist by default.

Measured tradeoff (3y backtest, 53 cutoffs, next-open proxy entries):
extended names produced the BEST median 20-session return of any trigger
(+1.2% vs +0.9% pullback_zone) but with a median max adverse excursion of
**−7.3%**, versus −5.7% for pullback_zone and −4.0% for breakout_ready —
the doc's "extended pullbacks are violent" claim, quantified. The C cap is
kept as a deliberate risk-control choice: it trades bull-tape return for
materially shallower drawdowns. Survivorship bias inflates the extended
bucket's returns most (its blowups left the universe), so the true return
give-up is smaller than it looks.

## Common Traps (doc)

- Extended above SMA50 without consolidation — no nearby stop structure;
  first pullback retraces 8-15%.
- False breakout above resistance — exit immediately if price closes back
  below within 1-2 sessions.
- Low-volume pullback that accelerates down — if pullback volume ever
  exceeds the prior rally's, character changed; stop buying the dip.
- Entering within 5 days of earnings — gap risk voids the stop math
  (screener auto-rejects when Yahoo publishes a date; UNKNOWN warns).

## Trade Management (doc)

Day 1 entry with structure stop (≤1% portfolio risk — hand off to
position-sizer). Day 2-3 confirmation: close below entry = tighten. Day 4-7
trail below higher lows / rising SMA20, take 1/3 at T1. Week 2+: trail
SMA50, exit on a close below it or a lower low.
