# Backtest Findings — 2010–2026 (Yahoo Finance daily data)

Run date: 2026-07-05. Data: ES=F, NQ=F continuous front-month daily bars and ^VIX/^VXN from Yahoo Finance, 2010-01-01 onward (~4,150 bars each). Reproduce with:

```bash
python3 skills/index-futures-weekly-income/scripts/backtest_weekly.py --start 2010-01-01 --output-dir reports/
```

## Methodology

- One decision per ISO week, made on the prior week's last close; trades live only inside the following week (Friday-close time stop).
- Conservative fill model: any bar touching both stop and target counts as a **stop-out**; stop-order fills gap-adjust to the open.
- Option credits priced with Black-Scholes (IV = VIX/VXN, r = 4%), settled at intrinsic value on Friday's close. R for spreads = P&L ÷ max loss.
- No commissions or slippage. Continuous-contract roll gaps are included in the bars.

## Final rule set — results

| Setup | Index | Trades | Win rate | Avg R | Total R | PF | Max DD (R) |
|---|---|---|---|---|---|---|---|
| weekly_breakout (long) | ES | 333 | 55.9% | −0.00 | −0.4 | 0.99 | −9.5 |
| weekly_breakout (long) | NQ | 299 | 58.5% | +0.03 | +9.3 | 1.14 | −6.8 |
| pullback_continuation | ES | 160 | 50.6% | +0.03 | +5.6 | 1.13 | −7.4 |
| pullback_continuation | NQ | 143 | 52.4% | +0.07 | +10.6 | 1.31 | −4.6 |
| put_credit_spread | ES | 641 | 89.7% | +0.02 | +15.2 | 1.30 | −4.7 |
| put_credit_spread | NQ | 564 | 87.2% | +0.01 | +7.3 | 1.15 | −4.7 |

## Core trend position — full history 2000-2026 (the compounding leg)

The weekly setups above cap winners at 5 days, so they cannot compound. The core
position fixes that. Rule (weekly, at Friday's close):

1. **Enter long** on a close above the 200d SMA.
2. **Stay long** inside the hysteresis channel — exit only on a close below
   **98% of the 200d SMA** (kills whipsaw exits; halved regime flips 87 → 45).
3. **Crash brake**: go flat regardless on a close below **88% of the 52-week
   high** (fast waterfall exits the slow SMA misses — 2020 COVID, 2018Q4).
4. No per-trade stop; the exit line IS the stop. Re-enter per rule 1.

25 years of Yahoo ES=F/NQ=F daily data (warmup ends 2001; includes the dot-com
bear, 2008, 2020, 2022), leverage via futures notional:

| Variant | CAGR | Max DD | $1 becomes | Flips (25y) |
|---|---|---|---|---|
| ES buy & hold | +8.2% | −57% | 7.2x | 0 |
| ES core 1.0x | +6.9% | −20% | 5.2x | 45 |
| ES core 1.5x | **+10.0%** | **−29%** | 10.6x | 45 |
| ES core 2.0x | **+12.8%** | **−38%** | 19.8x | 45 |
| NQ buy & hold | +13.6% | −54% | 21.5x | 0 |
| NQ core 1.0x | +10.7% | −25% | 12.3x | 45 |
| NQ core 1.5x | **+15.5%** | **−35%** | 35.3x | 45 |
| NQ core 2.0x | **+19.8%** | **−44%** | 88.6x | 45 |

At 1.5-2.0x the core **beats buy & hold on CAGR with roughly half the max
drawdown**. Worst years shrink too: ES worst year −17% (2022) vs −39% (2008)
for buy & hold; NQ −23% vs −42%.

Design notes:
- **Parameter robustness:** band swept 0.97-0.99 × SMA, brake 0.85-0.90 × 52w
  high — every neighbor beats the plain 200d filter; (0.98, 0.88) is a plateau
  center, not a spike. The 200d length itself was never tuned.
- **Technical overlays tested and rejected** (candlestick/channel family):
  10-week Donchian-low exit (worse everywhere: −38%/−46% max DD), 2-week
  confirmation before exit (raises DD), weekly candle patterns at this scale
  are noise. The two that survived are the hysteresis channel and crash brake
  above.
- **Why no VIX gate on the core:** cut CAGR ~1-2 points without cutting
  drawdown (whipsaw during recoveries). Rejected.
- **Sizing:** contracts = account × leverage ÷ (index × micro point value).
  Example at $100k / 1.5x: ES 7,550 → 150,000 ÷ (7,550 × $5) ≈ 4 MES;
  NQ 29,900 → 150,000 ÷ (29,900 × $2) ≈ 2-3 MNQ. ~2 flips/year.
- **Leverage caveat:** 2x roughly doubles losing years too (NQ 2011 −34%);
  1.5x is the balanced default. Continuous-contract roll gaps and no costs
  apply here as elsewhere.

## Monthly ATM bull put spread — the income engine (2001-2026)

Sell the at-the-money put ~30 DTE, buy the wing one 30-day expected move lower,
hold to expiry. Premium is far richer than the weekly far-OTM spread, and
Black-Scholes is most accurate at the money, so these estimates are the most
trustworthy in this document. Gated on the core trend state (skip when FLAT).
Per **one** spread:

| | Months traded | Profitable | Total P/L | Avg win | Avg loss | Worst month |
|---|---|---|---|---|---|---|
| ES (every month) | 296 | 218 (74%) | +$185,564 | +$1,861 | −$2,822 | −$11,941 |
| ES (core-gated) | 233 | 175 (75%) | +$167,263 | +$1,789 | −$2,513 | −$10,426 |
| NQ (every month) | 296 | 217 (73%) | +$269,414 | +$2,470 | −$3,375 | −$19,054 |
| NQ (core-gated) | 225 | 165 (73%) | +$213,387 | +$2,455 | −$3,194 | −$19,054 |

Notes:
- The ATM bull **call** (debit) spread at the same strikes was also positive
  (ES +$98k, NQ +$183k ungated) but wins only 45-52% of months — same market
  exposure, harder to live with. The put credit version is the skill's pick.
- A 0.5-EM wing halves the worst month but also roughly halves total P/L;
  1.0 EM is the default.
- This is a capped-upside long: the 73-74% monthly win rate is the frequency
  of flat-or-up months plus the premium cushion. Losses run ~2x wins — cap
  total max-loss at ~5% of account per month and expect several −$3-5k months
  per year per ES spread.
- Wing width scales with the vol index, so dollar risk breathes with vol:
  the worst months land in high-VIX regimes. Size by the CURRENT spread's
  max loss, never by the average.

## Trade management on the monthly spread — TESTED; one combo adopted

Daily Black-Scholes mark-to-market inside each month (IV = vol index that day),
core-gated ATM bull put spread, per 1 spread, 2001-2026:

| Management | ES total | ES avg loss | ES worst | NQ total | NQ avg loss | NQ worst |
|---|---|---|---|---|---|---|
| Hold to expiry (old default) | +$167,263 | −$2,513 | −$10,426 | +$213,387 | −$3,194 | −$19,054 |
| Take-profit 50%, no redeploy | +$114,363 | −$3,438 | −$10,426 | +$127,527 | −$3,932 | −$19,054 |
| **TP50 + redeploy + 2x-credit stop** | **+$179,578** | **−$1,627** | **−$6,698** | **+$216,975** | **−$2,400** | **−$13,716** |
| Stop 2x credit only | +$136,399 | −$2,131 | −$6,997 | +$134,544 | −$3,114 | −$13,716 |
| Stop 3x credit only | +$164,332 | −$2,503 | −$13,638 | +$194,334 | −$3,512 | −$22,462 |
| Short strike at −0.5EM (≈30Δ) | +$114,184 | −$2,979 | −$11,197 | +$150,662 | −$3,617 | −$18,939 |

**Adopted: TP50 + redeploy + 2x stop** — the only variant that raises (ES) or
holds (NQ) total profit while cutting avg loss 25-35% and worst month 28-36%.
Split-half robust (improves losses in BOTH 2001-2013 and 2014-2026 on both
symbols). Mechanics: after entry, work a GTC buy-to-close at 50% of the credit
(on fill with ≥7 days to expiry, sell a fresh ATM spread) and a buy-to-close
stop at 2x the credit (on fill, stand down until next month).

Important nuances, tested honestly:
- **"Take profit at 50%" ALONE is a myth for total P/L** — it cut profits ~33%
  and didn't touch the worst month. The benefit only appears when freed capital
  is REDEPLOYED (the part the marketing leaves out).
- **A 3x stop is worse than no stop** on worst-month (locks in marks that would
  have recovered by expiry). 2x is the level that worked.
- Model marks, no slippage/commissions: real stop fills on gap days will be
  worse than modeled; the managed variant trades 2-3x per month vs 1.

## Cross-asset & seasonality filters — tested and ALL rejected (2001-2026)

Every plausible bond/rates/credit/vol/seasonal gate was tested on the core-gated
monthly bull put spread. Verdict column = total P/L of the months each filter
would have skipped (a useful filter skips net-NEGATIVE months; none did):

| Filter (skip when...) | ES: skipped months P/L | NQ: skipped months P/L | Worst month reduced? |
|---|---|---|---|
| Yield curve inverted (10Y < 3M) | +$39,977 (37 mo) | +$70,746 | No |
| Rate shock +50bp/3mo (^TNX) | +$11,078 | +$18,245 | No |
| Rate shock +25bp/1mo | +$11,282 | +$11,935 | No |
| Credit stress (HYG/LQD < 100d SMA) | +$58,572 (82 mo) | +$83,722 | Slightly (ES −10.4k→−9.3k) |
| Bonds down (TLT < 200d SMA) | +$104,224 (115 mo) | +$134,554 | No |
| VIX backwardation (VIX ≥ VIX3M) | +$16,915 | +$175,522 | NQ yes, at 82% of total P/L |
| VIX > 25 | +$40,348 | +$56,337 | No |
| Skip September | +$11,778 | +$17,923 | No |
| Skip Aug + Sep | +$13,319 | +$14,436 | No |
| Skip after a losing month | +$41,154 | +$31,410 | Slightly |

**Why they all fail:** the 200d core gate already removed the toxic months —
these indicators all correlate with broken price trends, and broken trends are
already excluded. What survives the core gate are healthy-looking months, and
in those, "caution" indicators mostly flag high-premium environments where
selling was MORE profitable (VIX>25 months that passed the gate averaged
+$2,500/mo on ES). The remaining losses are sudden within-month declines from
healthy conditions — not forecastable at entry by any of these variables.
The worst months (−$10.4k ES / −$19.1k NQ) survive nearly every filter.

**Seasonality (win% by trade month, core-gated):** no robust seasonal edge.
November/December are the best (ES Nov 95% win, +$1,713 avg), March is the
weakest (ES −$7 avg, NQ −$861), but weak months are inconsistent across the
two indices — noise, not signal. September, the famously bad month, is
actually positive for the gated spread. No seasonal skip is justified.

**The only levers that genuinely change the loss profile are structural, not
filters:** a 0.5-EM wing halves the worst month at roughly half the total
P/L; smaller sizing does the same linearly. Do NOT add entry filters —
further "improvement" from here is curve fitting.

## What was tested and rejected

These are as important as what was kept. Cite them when a user asks for the missing structures.

- **Short futures breakdowns in downtrends**: strongly negative on both indices (ES total −20R at 1.5×ATR stops; including shorts flipped every breakout variant negative). Bear-market rallies stop out shorts relentlessly.
- **Call credit spreads in downtrends**: PF 0.60 (ES) / 0.49 (NQ). Counter-trend rallies blow through +1 EM.
- **Iron condor call side in range regimes**: condor positive on ES (PF 1.41) but negative on NQ (PF 0.80); the put side alone was positive on both (PF 1.61 / 1.38). The skill sells only the put side.
- **1.5×ATR stops**: negative expectancy across every directional variant on ES; 2.0–2.5×ATR consistently better on both indices. Weekly index noise needs room.
- **2R+ targets in a 5-day window**: mostly unreachable; total R was insensitive to targets beyond 3×ATR because the Friday time stop dominates.
- **VIX gate on the core hold**: cut CAGR without reducing drawdown. Kept only on the weekly overlay setups.
- **20/50 trend filter as a hold rule**: ES +2.6% CAGR vs +6.0% for the 200d filter — too whippy for position holding.

## Caveats

- Yahoo continuous contracts splice roll gaps into the price series; entry/stop distances near roll week are approximate.
- Black-Scholes with the vol index as flat IV understates real put-spread credits (index put skew richens the short leg); live credits are typically somewhat better than modeled, but live fills also pay spread/commission.
- Expectancies are thin (avg R +0.01 to +0.07). This is a regime-filtering and risk-discipline edge, not a money printer — sizing errors or trading the rejected structures erases it. Past performance does not guarantee future results.
