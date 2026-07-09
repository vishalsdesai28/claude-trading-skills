# Weekly Income Playbook — ES / NQ

## Core-satellite structure

The skill emits two kinds of signals; treat them as separate books:

1. **core_trend_position** — the compounding leg. Long ES/NQ (typically 1.5x via
   micros) while Friday close > 200d SMA; once long, hold until a close below
   98% of the SMA (hysteresis channel) or below 88% of the 52-week high (crash
   brake). This is where the long-run return comes from (beats buy & hold on
   CAGR at ~half the drawdown over 2000-2026). ~2 flips/year; no per-trade
   stop — the exit line is the stop.
2. **Weekly overlay setups** (below) — small, risk-defined satellite trades
   (1% risk each) that add income in favorable regimes and force a weekly
   regime review. Positive expectancy but modest; never size them up to make
   them the main return driver.

## Regime framework

Two gates decide what may be traded each week. Both are computed at the prior week's close.

**Trend** (daily closes): uptrend = close > 50d SMA and 20d SMA > 50d SMA; downtrend = both inverted; otherwise range.

**Volatility** (VIX for ES, VXN for NQ): calm < 17, normal 17–25, stressed > 25.

| Regime | Futures setups | Options income | Rationale |
|---|---|---|---|
| Uptrend, vol ≤ 25 | pullback_continuation, weekly_breakout (long) | put credit spread at −1 EM | Aligned with index drift |
| Range, vol ≤ 25 | none | put credit spread at −1 EM | Put side works in range; call side does not (NQ condor call side lost) |
| Downtrend | **none** | **none** | Shorts and call spreads both negative expectancy 2010–2026 |
| Stressed vol (> 25) | none | none | Expected-move strikes get run over; wait |

The single organizing fact from the backtest: **every structure aligned with the upward drift of US index futures made money; every structure that fought it lost.** The skill is therefore long-only and put-side-only, and "no trade" is a first-class signal.

## Setups

### pullback_continuation (primary futures setup)
- Buy limit at the 20-day EMA, only when the EMA sits below current price.
- Stop 2.0×ATR(14) below fill; target 3.0×ATR above (1.5R); exit at Friday close if neither hit.
- Wider stops beat tight ones consistently: 1.5×ATR stops were net losers on ES; 2.0–2.5×ATR were profitable on both indices.

### weekly_breakout (secondary, confirmation required)
- Buy stop at prior week's high + 0.1×ATR; same stop/target/exit as above.
- Edge is marginal (ES ≈ breakeven, NQ PF ~1.14). Treat as a candidate, not a trade: require technical-analyst chart confirmation (volume, room to next resistance, no imminent macro event) before entry.

### put_credit_spread (income leg)
- Short strike at spot − 1.0× expected move; wing 0.5×EM lower (min one strike step).
- Expected move = spot × (vol index/100) × √(DTE/365), DTE = 5.
- Sell only in uptrend or range with vol ≤ 25. Hold to Friday expiry.
- ~88–90% win rate but risk:reward < 1 — a single full loss erases roughly 10–15 winners. Never oversize; see sizing below.

## Execution

**Instruments.** Futures: MES ($5/pt) and MNQ ($2/pt) micros for accounts < ~$100k; ES ($50/pt), NQ ($20/pt) full-size above. Options: ES/NQ weekly options (CME), or SPX/XSP (cash-settled, no early assignment; XSP = SPX/10) — SPY/QQQ equivalents divide strikes by ~10 (ES) / ~41 (NQ).

**Sizing.** Futures: risk ≤ 1% of account per trade; contracts = floor(account × 1% ÷ risk_per_contract). The signal JSON's position-sizer handoff pre-multiplies entry/stop by the micro point value so one "share" equals one micro contract. Credit spreads: cap total max-loss at ≤ 2% of account per week across all spreads — the tail loss is −1R by construction and will happen several times a year.

**Timing.** Generate signals after Friday's close or over the weekend; work orders Monday. Signals expire at Friday's close — flat over the weekend is the default.

**Expiry/roll cautions.** Futures roll quarterly (H/M/U/Z); around roll week, volume shifts to the next contract and Yahoo's continuous series gaps. Weekly options on futures settle into the future; SPX/XSP settle to cash. Avoid holding short options through major scheduled events (FOMC, CPI) — check economic-calendar-fetcher.

## Handoffs

- **position-sizer** — contract count from entry/stop (micros pre-scaled).
- **technical-analyst** — mandatory confirmation for weekly_breakout; recommended for pullback entries near support.
- **adversarial-trade-debate** — run the JSON through a bull/bear debate for discretionary conviction.
- **trader-memory-core** — register each taken trade (`--source index-futures-weekly-income`) so signal-postmortem can close the loop.
- **economic-calendar-fetcher** — screen the week for high-impact events before selling premium.
