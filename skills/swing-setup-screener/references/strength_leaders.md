# Strength: Leaders — Framework

Stocks within 5% of their 52-week high with persistent relative strength.
Leaders tend to keep leading; pullbacks into support offer the best
risk/reward swing entries. Source: product scanner doc "Strength: Leaders".

## Signal

Gate: close within 5% of the 52-week high (doc) AND `close > SMA50 >
SMA200` (doc playbook conditions).

## Pullback Plan Bands (doc — exact numbers)

Distance of close from SMA20 (`dist20`):

| Label | Band | Meaning |
|-------|------|---------|
| at_sma20 | −3% .. +1% | At dynamic support — the ideal buy zone now |
| sma20_dip_buy | +1% .. +6% | Classic leader entry zone — buy dips toward SMA20 |
| wait_deeper | > +6% | Too extended — wait for the pullback (capped C) |
| below_sma20 | < −3% | Leader status at risk — reassess (capped C) |

Additional doc rule: > 10% above SMA20 = parabolic, never chase (warning
`parabolic_extension_above_sma20`).

## Factor Weights (ours)

proximity_to_high .25 · trend_quality .25 · accumulation .20 (up-day volume
vs down-day volume over 20 sessions — doc: "up days heavier than down
days") · rel_strength .20 (63-day return vs SPY) · entry_timing .10 (by
Pullback Plan band).

## Grade Interpretation (doc)

A = premier leader (proximity + liquidity + healthy pullback structure) —
size up when the pullback trigger fires. B = quality leader, standard
sizing on pullbacks. C = watchlist (thin, too far from highs, or fading
momentum). D = not a leader right now.

## Pullback Entry Blueprint (doc, condensed)

1. Identify the support zone: prior breakout level, key pivot, SMA20, or
   rising trendline.
2. Wait for pullback compression — volume drying up, tight ranges near
   support.
3. Trigger: reclaim of SMA10/20 or break of the pullback trendline with a
   volume uptick.
4. Confirm it holds one additional bar — instant reversal = trap.
5. Manage: partials at the prior swing high, trail the rest; tighten after
   3+ stalled days.

Screener plan levels: stop = tighter of 20d low / SMA50 (doc: close below
the support zone or SMA50 on above-average volume = exit); T1 = 52-week
high, or a 20d-range measured move when already at highs. Reward:risk is
printed honestly — leaders bought mid-range often show poor R:R to T1;
that is the signal to wait for the pullback, not to widen the target.

## Avoid / Traps (doc)

- Near highs WITH distribution (heavy down-day volume, failed breakouts,
  RSI divergence) is a sell signal, not a buy signal.
- Breakouts on thin volume from > 15% above SMA50.
- Earnings within 5 days (auto-rejected when the date is known).
- FOMO entries on "near highs" alone — require the accumulation pattern.
