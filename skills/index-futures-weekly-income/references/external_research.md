# External Research — What Published Studies Say vs. What Our Backtests Found

Compiled 2026-07-06 via multi-source web research on primary documents. Confidence
labels: **[verified]** = adversarially cross-checked 3-0; **[quoted]** = direct
quote extracted from the primary source, cross-check incomplete (run limits);
**[ours]** = this skill's own 2001-2026 Yahoo-data backtest.

## The edge is real and documented

- **[verified]** Bakshi & Kapadia, *Review of Financial Studies* 2003: delta-hedged
  long S&P 500 option positions systematically lose (−12.2% of option price on
  average; 68% of ATM observations negative) — a negative volatility risk premium
  that compensates option **sellers**. (people.umass.edu/~nkapadia/docs/Bakshi_and_Kapadia_2003_RFS.pdf)
- **[verified]** Same paper: OTM put buyers lose 47-86% of the put price on a
  hedged basis (89-97% of observations negative). Percentage capture is larger
  further OTM; **dollar premium is maximized near the money** — supporting this
  skill's ATM short strike for income.
- **[quoted]** CBOE/Wilshire: VIX exceeded subsequent realized vol in **20 of 21
  years 1998-2018**; ~3.9 vol points average richness 1990-2011.

## The monthly ATM structure is institutional-grade, not exotic

- **[quoted]** The Cboe PUT Index — literally this skill's monthly trade, unhedged —
  returned ~9.5-10.3% annualized at ~2/3 of the S&P 500's volatility across three
  independent studies (Ennis Knupp 1986-2008; Asset Consulting Group 1988-2011;
  Bondarenko 1986-2018), Sharpe ~0.65 vs ~0.49 for the index.
- **[quoted]** Bondarenko 2019: **weekly put selling (WPUT) underperformed monthly
  (PUT)** on both absolute (4.51% vs 5.97% CAGR) and risk-adjusted return.
  **[ours]** agrees: our weekly far-OTM spread earned a fraction of the monthly
  ATM spread's P/L. Monthly is the right cadence.

## Hold-to-expiry's documented weakness is the tail — our management targets exactly that

- **[quoted]** PUT Index worst month (−17.7%) was WORSE than the S&P 500's
  (−16.8%) despite a 76% monthly win rate: naked hold-to-expiry does not truncate
  left tails. **[ours]**: the defined-risk wing plus the adopted management
  (50%-credit profit-take with redeployment + 2x-credit stop) cut avg losing month
  25-35% and worst month 28-36% while total P/L rose (ES) or held (NQ).
- **[quoted]** Cboe PutWrite methodology: the institutional benchmarks use NO
  profit-taking, NO DTE exits, NO event avoidance — the popular 45-DTE/21-DTE and
  "manage at 50%" mechanics have no rigorous academic validation. **[ours]**: TP50
  *alone* cut total P/L ~33%; only TP50 **with redeployment** beat hold-to-expiry.
  Treat bare "manage winners at 50%" claims as marketing-grade.

## VIX-conditioned entries: don't skip high vol

- **[quoted]** Bondarenko: average PUT returns RISE with entry VIX quintile
  (~0.6% → ~1.1%/month), though tail severity rises too. AQR/RIA study: PUT beat
  the S&P 500 risk-adjusted in EVERY VIX quartile. **[ours]** agrees: skipping
  VIX>25 months cost +$40k (ES) / +$56k (NQ) of profit. Size by the current
  spread's max loss instead of skipping.

## FOMC/event avoidance would HURT — sellers are paid most on event days

- **[quoted]** NBER w28306: options expiring on FOMC/employment days carry large
  positive variance risk premia — strongest on FOMC days — and in-sample the
  priced downside tail before FOMC **never materialized**.
- **[quoted]** NY Fed staff report 512: the S&P 500 rose an average +49bp in the
  24h before scheduled FOMC announcements (t > 4.5), 1994-2011.
- Conclusion: no event-avoidance filter in this skill, by evidence, not omission.

## Delta selection caveat

- **[quoted]** Wilshire: 30-delta covered calls (BXMD) beat ATM covered calls
  (BXM) 10.22% vs 8.50% — but that is the CALL side. **[ours]** on the PUT side:
  moving the short strike down to ~30-delta cut total P/L ~30% for little tail
  relief. ATM remains this skill's income strike.

## What no published study covers (our contribution)

The 200d-SMA trend gate on option selling has no rigorous published test that
this research pass could find; our 2001-2026 backtest is the evidence (modest
per-month expectancy improvement, large sequencing/drawdown benefit). Same for
the hysteresis channel + crash brake on the futures core position.
