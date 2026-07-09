# Volatility: High — Framework

Stocks whose ATR% (14-day average true range as a percent of price) is
elevated — wider daily swings, larger opportunity, and strictly smaller
position sizes. Source: product scanner doc "Volatility: High".

## Signal

Gate: ATR14 ≥ 4% of price (*ours*; doc visuals put "normal" at 2-3% and
in-play names at 5-10%). Signal fields: `atr_pct`, `expansion_ratio`
(ATR% now vs 60 sessions ago; `atr_baseline_unavailable` warning when
history is short), `body_ratio` (mean body/range over 10 sessions).

## Structured vs Chaotic (doc — the core distinction)

Not all volatility is tradeable. **Structured** volatility has clear
support/resistance, identifiable consolidations, and real liquidity —
bodies dominate ranges. **Chaotic** volatility — wide spreads, erratic
wicks, thin books — is untradeable. Proxy in daily bars: `body_ratio` <
0.35 (*ours*) = chaotic → capped C with `chaotic_tape_watchlist_only`.
Spread and order-book depth are NOT in daily bars — check them live before
entry (the screener says so via `catalyst_unknown` + checklist).

## Factor Weights (ours)

expansion .35 · structure_quality .30 (body_ratio bands) · liquidity .20
(dollar-volume bands) · participation .15 (last-session RVOL).

## Position Sizing (doc — non-negotiable)

Reduce size 30-50% versus normal. Same dollar risk per trade, fewer shares:
the stop must live outside the (wider) noise band. The plan block carries
`size_note: reduce_position_30_50pct_per_doc`; hand off to position-sizer
with the ATR-based mode (`--atr`).

## The Volatility Cycle (doc)

Compression (tight ranges, energy building — build the watchlist, define
levels) → Expansion (trade here: breakouts, range bounces, momentum
continuation with defined risk) → Consolidation (take profits, ranges
narrow) → repeat. Compression always leads to expansion eventually.

## Playbooks (doc, condensed)

**Breakout.** Tight consolidation after a volatile phase, volume building.
Enter the break of consolidation resistance on the first wide-range candle
with volume; failed same-day hold = exit. T1 measured move; take 50%,
trail below the breakout level.

**Range trading.** Only in a range with ≥ 2 touches per side that has held
multiple sessions, wide enough to clear costs. Buy support / short
resistance ONLY on a reaction candle at the level. Exit on a close outside
the range. Avoid converging ranges (that is compression) and ranges into a
catalyst.

**Momentum expansion.** ATR% jumping with expanding bodies and volume.
Enter the first pullback holding the prior candle's midpoint. A candle
reversing > 60% of the expansion move = exhausted, exit. Never chase after
3+ expansion candles.

## Traps (doc)

- High ATR% from illiquidity, not demand — grade D territory; skip.
- ATR% is highest near the open and around news — the screener is an EOD
  snapshot; re-check live spread and depth before entering.
- A major catalyst pending breaks ranges violently — check the calendar.
