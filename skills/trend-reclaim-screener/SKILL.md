---
name: trend-reclaim-screener
description: After-hours screener for long-side SMA50 reclaim setups — stocks that pulled back below their 50-day moving average, reset/based, and recently re-established above it. Scores reclaim quality, momentum, structure, volume participation, and trend alignment into A-D grades with top-3 picks, plan levels (next-open entry, stop below the reclaim level, T1 at pre-reset resistance), and a pre-entry checklist. Keyless Yahoo Finance data; no paid API required.
---

## Overview

Screen US equities for second-chance continuation setups: stocks reclaiming
their SMA50 after a genuine reset (pullback, base, or failed move). The best
multi-week movers often don't offer their cleanest entry on the first
breakout — they reset, stabilize, then reclaim trend control. This skill
finds those reclaims the evening they confirm, grades them, and emits a
ranked watchlist with defined-risk plan levels. Detection-only: it never
sends orders.

Long-side mirror of parabolic-short-trade-planner Phase 1 — same
hard-invalidation → 5-factor weighted score → A-D grade architecture.

## When to Use

Invoke this skill when the user wants to:

- Find stocks that recently reclaimed their SMA50 after a pullback below it.
- Build an after-hours (post-close) watchlist of trend-restoration setups
  for next-open entries.
- Grade a specific ticker's reclaim quality (`--tickers`).

Do NOT invoke for:

- First-breakout momentum screening — use vcp-screener or
  stockbee-momentum-burst-screener.
- Short-side exhaustion — use parabolic-short-trade-planner.
- Intraday signals — this scans daily bars only.

## Workflow

1. Run the screener after the close (no API key needed):
   ```bash
   python3 skills/trend-reclaim-screener/scripts/screen_trend_reclaim.py \
     --output-dir reports/
   ```
   Universe options, in precedence order:
   - `--tickers NVDA,AMD,...` — explicit list
   - `--universe-csv path.csv` — any CSV with a Ticker/Symbol column
     (e.g. a Finviz export)
   - default — keyless Yahoo screen of the ~250 most liquid US names
     ≥ $2B market cap (`--universe-size` to widen)
   - `--fixture path.json` — offline bars for testing
2. Read `reports/trend_reclaim_<date>.md`. Top Picks are the highest
   composite scores; the Watchlist lists every candidate at or above
   `--watch-min-grade` (default C) with factor breakdowns and warnings.
3. Interpret grades using `references/trend_reclaim_framework.md`:
   A = enter per plan; B = tradeable with standard sizing on a clean entry;
   C = watchlist, wait for maturation; D is excluded.
4. Walk the Pre-Entry Checklist in the report — especially the earnings
   check, which the screener does NOT perform.
5. Hand off before entry:
   - position-sizer — size from the report's stop distance (`risk_pct`).
   - technical-analyst — confirm the chart on candidates before committing.
   - trader-memory-core — register picks as theses for postmortem tracking.

## Scoring Model

Hard invalidations (reject before scoring): <200 bars history, price below
`--min-price` ($5), 20-day dollar volume below `--min-adv-usd` ($10M),
close extended more than `--max-ext-pct` (10%) above the SMA50 (chasing),
≥2 prior failed cross-aboves in the last 30 sessions (chop), and next
earnings within `--exclude-earnings-within-days` (7; 0 disables) — dates
fetched keylessly from Yahoo for surviving candidates only.

A reclaim requires: last close above SMA50, the below→above cross within
`--reclaim-window` (5) sessions, and at least `--min-days-below` (5) of the
prior 30 sessions below the SMA50 — proving a real reset, not noise.

Five factors, weighted composite (0-100):

| Factor | Weight | Measures |
|--------|--------|----------|
| Reclaim quality | 30% | Closes held above SMA50, healthy distance (1-6%), SMA50 slope |
| Momentum | 25% | 10-day ROC positive and improved vs. the reset low, SMA20 rising |
| Structure | 20% | Higher lows (3×5-session blocks), ATR contraction vs. the reset low |
| Volume | 15% | Cross-day relative volume, up/down volume ratio since the low |
| Trend alignment | 10% | Close vs. SMA200, SMA50 vs. SMA200, SMA200 slope |

Cross-day RVOL bands follow the source doc's volume diagram: ≥3× = strong
break, <0.8× = fade risk. A fade-risk reclaim can never grade A/B — it is
capped at C (`grade_capped_fade_risk` warning) regardless of composite.

Grades: A ≥ 85, B ≥ 70, C ≥ 50, D < 50. Phase: `reclaim_attempt`
(< 3 closes above) or `reclaimed_trend` (≥ 3 closes above — the
higher-conviction phase).

Every report opens with a market-regime line (SPY vs. its SMA50/SMA200:
`risk_on` / `mixed` / `risk_off`) — the don't-fight-the-tape check from the
reclaim checklist.

## Output Format

`reports/trend_reclaim_<date>.json` and `.md`:

- **Market regime** header line — SPY posture vs. SMA50/SMA200.
- **Top Picks** — top 3 by composite with close/stop/T1/T2/reward-risk
  (T2 = measured move: base depth projected from the reclaim level).
- **Watchlist** — every candidate ≥ watch grade: factor scores, days since
  cross, next earnings date, plan levels, advisory warnings
  (`fade_risk_volume`, `low_participation_reclaim`,
  `marginal_reclaim_distance`, `sma50_still_declining`,
  `grade_capped_fade_risk`).
- **Pre-Entry Checklist** — the reclaim-quality checklist; earnings are
  auto-rejected, other binary catalysts (FDA, M&A) stay manual.
- **Rejected** — tickers with hard-invalidation reasons, for audit.

## Resources

- `scripts/screen_trend_reclaim.py` — the screener (stdlib scoring; yfinance
  only for live data).
- `references/trend_reclaim_framework.md` — reclaim lifecycle, grade
  interpretation, playbooks (reclaimed continuation, reclaim-attempt entry,
  unconfirmed-reclaim avoidance), common traps, risk management.
