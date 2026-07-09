---
name: swing-setup-screener
description: Seven-in-one EOD screener over keyless Yahoo daily bars — swing-trend longs (SMA50/200 alignment + Pullback/Breakout/Extended trigger), swing-trend shorts (Bear Flag/Breakdown/Oversold), 52-week-high strength leaders with SMA20 pullback plans, high-ATR% volatility candidates, and three next-session RVOL watchlists (in-play movers, unusual volume with accumulation/distribution quadrant, weakness on selling volume). A-D grades from documented factor weights, plan levels, hard timing contract (last completed session only; RVOL screens are explicitly next-session watchlists, never live Day-1 signals). No paid API required.
---

## Overview

Screen US equities after the close across seven complementary setups from a
single script with shared universe, indicators, grading, and reports. Four
swing screens (swing-long, swing-short, leaders, volatility) are fully
faithful on daily bars; three RVOL screens (in-play, unusual-volume, weak)
are honest EOD reconstructions of live intraday scanners and are labeled
**next-session watchlists** in every report. Detection-only: never sends
orders.

Sibling of trend-reclaim-screener (same keyless Yahoo stack and
invalidation → factor-score → A-D grade architecture); reclaim setups stay
in that skill.

## Data-Fidelity Contract

These rules are enforced in code, not just documented:

- **Last completed session only.** A same-day bar fetched before ~16:15 ET
  is dropped (`--allow-partial-today` overrides, deliberately loudly named).
- **Next-session watchlists.** in-play / unusual-volume / weak reports open
  with a timing banner: the live product computes these intraday
  (time-of-day RVOL pace, VWAP, premarket levels) — daily bars cannot, so
  these screens build tomorrow's focus list (the source docs' own Day 2-3
  entry framework), never a live Day-1 entry feed.
- **UNKNOWN is never safe.** Missing earnings dates warn
  (`earnings_date_unknown_verify_manually`); short screens always warn that
  short interest / locate status is unknown; catalyst is always marked
  unknown. Tickers whose data lags the session are flagged `stale_data_*`.
- **No synthesized fields.** Nothing derived from VWAP, float, spreads, or
  premarket data appears anywhere.
- **No black boxes.** Every candidate row prints its factor scores; every
  report echoes the full params block (thresholds + weights). Grade caps
  always disclose their reason as a warning.

## When to Use

Invoke this skill when the user wants to:

- Build an after-hours watchlist of trend-following longs or shorts
  (`--screen swing-long` / `swing-short`).
- Find 52-week-high leaders and know whether to buy now or wait
  (`--screen leaders`, Pullback Plan labels).
- Rank tradeable high-ATR% names (`--screen volatility`).
- See which stocks had abnormal participation today for tomorrow's focus
  list (`--screen in-play`, `unusual-volume`, `weak`).
- Run the whole board: `--screen all` (seven report pairs).

Do NOT invoke for:

- SMA50 reclaim-after-reset setups — use trend-reclaim-screener.
- Live intraday HOD breaks, runners, or gap plays — daily bars cannot do
  this; use a real-time scanner and interpret with technical-analyst.
- Stockbee-method momentum bursts — use stockbee-momentum-burst-screener.

## Workflow

1. Run after the close (no API key needed):
   ```bash
   python3 skills/swing-setup-screener/scripts/screen_swing_setups.py \
     --screen swing-long --output-dir reports/
   ```
   Universe options, in precedence order: `--tickers NVDA,AMD`,
   `--universe-csv file.csv` (Ticker/Symbol column), default keyless Yahoo
   screen of every US name ≥ $2B / price > $5 / avg vol > 500K (~1.8k
   tickers, paginated 250 per request; `--universe-size 250` for a quick
   most-liquid scan), or `--fixture path.json` offline. Partial coverage is
   disclosed in the report header, never silent.
2. Read `reports/swing_setups_<screen>_<date>.md`. Check the market-regime
   line first (SPY vs SMA50/SMA200) — longs fight a `risk_off` tape, shorts
   fight `risk_on`.
3. Interpret grades and labels with the matching reference file
   (`references/`). A = actionable per plan, B = standard sizing on a clean
   entry, C = watchlist only, D excluded. Grade caps (Extended, Oversold,
   Wait Deeper, chaotic tape, chop quadrant, faded close, gap bought back)
   are printed as warnings.
4. Walk the report's Pre-Entry Checklist; resolve every UNKNOWN warning
   manually (catalyst, earnings without a date, short interest).
5. Hand off before entry: position-sizer (size from `risk_pct`),
   technical-analyst (confirm the chart), short-squeeze-radar (mandatory for
   swing-short/weak candidates), trader-memory-core (register theses).

## The Seven Screens

| Screen | Finds | Key gates (doc-sourced where noted) | Labels |
|--------|-------|-------------------------------------|--------|
| swing-long | Confirmed uptrends | close > SMA50 > SMA200, rising SMA50 | pullback_zone / breakout_ready / extended (doc: >10% above SMA50) / none |
| swing-short | Confirmed downtrends | close < SMA50 < SMA200, falling SMA50 | bear_flag / breakdown_ready / oversold / none |
| leaders | Near 52-week highs | within 5% of 52w high (doc), uptrend | Pullback Plan bands from dist-to-SMA20: at_sma20 −3..+1%, dip_buy +1..+6%, wait_deeper >+6% (all doc) |
| volatility | Wide daily ranges | ATR% ≥ 4 (ours) | atr_<x>pct; chaotic tape capped |
| in-play | Abnormal attention | RVOL ≥ 2 (doc) + day move ≥ +3% (ours) | rvol_<x>x; faded close capped |
| unusual-volume | Volume signal, no direction | RVOL ≥ 3 (doc) | quadrant: accumulation / distribution / absorption / chop (doc framework) |
| weak | Decline on selling volume | day ≤ −3% (ours) + RVOL ≥ 1.5 (doc) | downtrend_aligned / counter_trend |

Shared hard invalidations: ≥210 bars history, price ≥ $5, 20-day dollar
volume ≥ $10M, and earnings within 5 days (doc) — swing screens and leaders
**reject**; next-session screens **warn** instead (the event may be the
catalyst). All thresholds CLI-overridable and echoed in `params`.

**Regime hard-gate (backtest-validated):** while SPY closes above both its
SMA50 and SMA200 (`risk_on`), every swing-short and weak candidate is
capped at C with a `regime_risk_on_short_capped_watchlist_only` warning —
in the 3-year backtest every short grade lost money in a risk_on tape.
`--no-regime-gate` disables it; doing so is on you.

Grades: A ≥ 85, B ≥ 70, C ≥ 50, D < 50, from per-screen weighted factors
(weights in the report's params block and the reference files).

## Output Format

`reports/swing_setups_<screen>_<date>.json` and `.md`, one pair per screen:

- Header: as-of date + **session evaluated** (the completed bar actually
  scored), timing banner on next-session screens, SPY market regime.
- **Top Picks** table (grade, composite, label, close, key levels).
- **Watchlist**: every candidate ≥ `--watch-min-grade` with factor
  breakdown, plan levels, next earnings, and all warnings.
- **Pre-Entry Checklist** per screen.
- **Rejected** tickers with per-rule reasons, for audit.

## Validating the Screens (Backtest + Forward Log)

`scripts/backtest_swing_setups.py` replays every screen at historical
cutoffs (bars truncated at T) and measures forward outcomes under a fixed,
disclosed execution model (next-open entry, stop-first on ambiguous bars,
gap-through-stop at the open, horizon time-stop). It answers the
**cross-sectional** question — do A grades beat B beat C beat D, and do the
doc-mandated caps underperform as claimed — and prints why absolute
performance claims are NOT defensible (survivorship-biased universe,
earnings gate off, discretionary playbook steps unmodeled):

```bash
python3 skills/swing-setup-screener/scripts/backtest_swing_setups.py \
  --period 3y --cadence 10 --horizon 20 --output-dir reports/
```

Output: `reports/swing_setups_backtest_<date>.{md,json}` — per-screen,
per-grade and per-label stats (win rate, direction-signed returns, median
R, median MAE/MFE) with a PASS / FAIL / INSUFFICIENT_DATA monotonicity
verdict (n ≥ 30 per grade required for a verdict).

**Forward log (zero pick-list bias):** the nightly report JSONs are the
log. `scripts/evaluate_forward_log.py` scores every report whose session is
≥ 20 trading sessions old under the *same* execution model as the backtest
(shared `candidate_outcome_row`), emitting directly comparable grade
tables in `reports/swing_setups_forward_eval_<date>.{md,json}`. Immature
reports are counted, never part-scored. Cron on this box runs the screener
nightly (16:45 ET, Mon-Fri) and the evaluator weekly (Fri 17:30 ET); first
matured cohorts appear ~one month after logging starts. Never tune
thresholds on the full history — train/validation split per
backtest-expert.

## Resources

- `scripts/screen_swing_setups.py` — the screener (stdlib scoring; yfinance
  only for live data).
- `scripts/backtest_swing_setups.py` — grade-monotonicity backtest harness
  (see Validating the Screens).
- `scripts/evaluate_forward_log.py` — scores matured nightly picks with the
  identical execution model; the zero-bias half of the validation loop.
- `references/swing_trend_long.md` — long trigger definitions, grades,
  playbooks, traps.
- `references/swing_trend_short.md` — short mirror + squeeze-risk protocol.
- `references/strength_leaders.md` — Pullback Plan bands and leader entries.
- `references/volatility_high.md` — structured vs chaotic volatility,
  sizing rules.
- `references/rvol_screens.md` — in-play / unusual-volume / weak: the
  timing contract, quadrant framework, Day 2-3 playbooks.
