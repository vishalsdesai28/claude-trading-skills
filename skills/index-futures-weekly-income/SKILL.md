---
name: index-futures-weekly-income
description: Generates trade signals for S&P 500 (ES/MES) and Nasdaq-100 (NQ/MNQ) index futures and their options - a core-satellite system backtested on 25 years of Yahoo Finance data. Core leg is a 200-day-SMA regime hold with a hysteresis channel and 52-week-high crash brake (beats buy-and-hold CAGR at 1.5-2x futures leverage with roughly half the max drawdown); satellite legs are regime-gated weekly setups (long-only pullback/breakout, put-credit-spread income) with stand-aside calls in downtrends and stressed volatility. Use when the user asks about trading index futures, ES/NQ/MES/MNQ signals, weekly income from futures or index options, selling premium on S&P/Nasdaq, or wants futures trade ideas to feed into position sizing and technical analysis. Outputs JSON consumed by position-sizer, technical-analyst, adversarial-trade-debate, and trader-memory-core.
---

# Index Futures Weekly Income

## Overview

Generate a signal set for ES (S&P 500) and NQ (Nasdaq-100) futures from free Yahoo Finance data (no API key). Classify each index into a trend regime (uptrend / range / downtrend) and a volatility regime (calm / normal / stressed from VIX/VXN), then emit a core position state plus only the weekly setups that survived the backtests:

- **core_trend_position** (the compounding leg): long above the 200-day SMA; once long, exit only on a Friday close below 98% of the SMA (channel) or 88% of the 52-week high (crash brake). No per-trade stop — the exit line is the stop. 2000-2026 at 1.5x futures leverage: ES +10.0% CAGR / −29% max DD vs buy-and-hold +8.2% / −57%; NQ +15.5% / −35% vs +13.6% / −54%.
- **pullback_continuation** (futures, long): buy limit at the 20-day EMA in uptrends; stop 2.0×ATR, target 3.0×ATR (1.5R). Best directional expectancy.
- **weekly_breakout** (futures, long): buy stop above prior week's high. Marginal edge — always confirm with technical-analyst before entry.
- **put_credit_spread** (options, weekly income): short strike at −1 expected move, wing at 0.5×EM, in uptrend or range regimes. High win rate (~88-90%), low risk:reward.
- **monthly_bull_put_spread** (options, monthly income engine): sell the ATM put ~30 DTE, wing one expected move lower, managed with a 50%-of-credit profit-take (redeploy if ≥7 days left) and a 2x-credit stop. 2001-2026 per single spread: ES +$180k / NQ +$217k with avg losing month cut 25-35% vs hold-to-expiry. Emitted whenever the core position is not FLAT.
- **stand_aside**: downtrend or stressed vol (VIX > 25). Shorting breakdowns and selling call spreads both backtested to negative expectancy; say so and recommend no trade.

## When to Use

- User asks for index futures trade ideas (ES, NQ, MES, MNQ, S&P futures, Nasdaq futures)
- User wants weekly income from index options or futures options
- User asks whether to sell premium (put spreads, condors) on S&P/Nasdaq this week
- Weekly planning workflows that feed candidates into position-sizer / adversarial-trade-debate

## Workflow

### 1. Generate signals

```bash
python3 skills/index-futures-weekly-income/scripts/futures_signals.py --output-dir reports/
```

Fetches ~15 months of daily ES=F/NQ=F bars plus ^VIX/^VXN, classifies regimes, and writes a plain-language trade plan to `reports/index-futures-weekly/index_futures_signals_<date>.md` (+ machine-readable `.json`). `--account-size` and `--core-leverage` tailor the exact contract counts. Use `--symbols ES` to limit scope; `--fixture <json>` for offline runs.

### 2. Interpret the output

Read `references/weekly_income_playbook.md` for regime logic and execution guidance. Key points to relay:

- The core position carries the long-run return; the weekly setups are small satellites. Never invert that sizing.
- Futures setups are the high risk:reward leg (1.5R); the put credit spread is the income leg (high win rate, RR < 1). State both sides honestly — never present a credit spread as high risk:reward.
- A `stand_aside` signal IS the recommendation. Do not invent a trade in downtrends or stressed vol.
- `risk_per_contract` gives dollar risk for full-size (ES $50/pt, NQ $20/pt) and micro (MES $5/pt, MNQ $2/pt) contracts. Recommend micros for accounts under ~$100k.

### 3. Hand off to downstream skills

Each signal carries a `handoff` block:

- **position-sizer**: command with entry/stop pre-multiplied by the micro point value, so 1 "share" = 1 micro contract.
- **technical-analyst**: confirm the setup on a chart before entry (mandatory for weekly_breakout).
- **trader-memory-core**: `thesis_ingest` command to register the trade for postmortem tracking.
- Optionally feed the JSON to adversarial-trade-debate for a bull/bear conviction check.

### 4. Validate the edge (optional but encouraged)

```bash
python3 skills/index-futures-weekly-income/scripts/backtest_weekly.py --start 2010-01-01 --output-dir reports/
```

Re-runs the full backtest on current Yahoo data with conservative fills (same-bar stop-before-target). Cite `references/backtest_findings.md` when the user asks why a setup is or isn't offered.

## Output Format

`reports/index_futures_signals_<date>.json`:

```json
{
  "skill": "index-futures-weekly-income",
  "week_of": "YYYY-MM-DD",
  "markets": [{
    "symbol": "ES", "spot": 0, "atr": 0, "vix": 0, "expected_move_1w": 0,
    "regime": {"trend": "uptrend", "vol": "calm"},
    "signals": [{"setup": "...", "entry": 0, "stop": 0, "target": 0, "rr": 1.5,
                  "risk_per_contract": {"ES": 0, "MES": 0}, "handoff": {}}]
  }]
}
```

## Risk Disclaimer

Educational analysis, not financial advice. Futures and options on futures involve substantial risk of loss and are not suitable for all investors. Backtest results use continuous-contract Yahoo data (roll gaps included), Black-Scholes-estimated option credits, and exclude commissions and slippage — live results will differ.

## Resources

- `references/weekly_income_playbook.md` — regime framework, setup rules, execution and sizing guidance
- `references/backtest_findings.md` — full backtest methodology, results, and what was rejected
- `references/external_research.md` — published research (CBOE, RFS, NBER, NY Fed) mapped against this skill's own backtests; cite it when users ask why a rule exists
- `scripts/futures_signals.py` — signal generator (Yahoo Finance, keyless)
- `scripts/backtest_weekly.py` — backtest CLI and reusable simulation functions
- `scripts/signal_engine.py` — pure signal/indicator/pricing library (stdlib only)
