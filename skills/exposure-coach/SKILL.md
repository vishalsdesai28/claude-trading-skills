---
name: exposure-coach
description: Generate a one-page Market Posture summary with net exposure ceiling, growth-vs-value bias, participation breadth, and new-entry-allowed vs cash-priority recommendation by integrating signals from breadth, regime, and flow analysis skills.
---

# Exposure Coach

## Overview

Exposure Coach synthesizes outputs from market-breadth-analyzer, uptrend-analyzer, macro-regime-detector, market-top-detector, ftd-detector, theme-detector, sector-analyst, and institutional-flow-tracker into a unified control-plane decision. The skill answers the solo trader's core question: "How much capital should I commit to equities right now?" before any individual stock analysis begins.

## When to Use

- Before initiating any new stock positions to determine appropriate capital commitment
- At the start of each trading week to calibrate portfolio exposure
- When multiple market signals conflict and a unified posture is needed
- After significant macro or market events to reassess exposure ceiling
- When transitioning between market regimes (broadening, concentration, contraction)

## Prerequisites

- Python 3.9+
- FMP API key (set `FMP_API_KEY` environment variable) for institutional-flow-tracker data
- Input JSON files from upstream skills (see Workflow Step 1)
- Standard library + `argparse`, `json`, `datetime`

## Workflow

### Step 1: Gather Upstream Skill Outputs

Collect the most recent JSON outputs from integrated skills. Each file provides a specific signal dimension:

| Skill | Output File Pattern | Signal Provided |
|-------|---------------------|-----------------|
| market-breadth-analyzer | `breadth_*.json` | Advance/decline ratios, new highs/lows |
| uptrend-analyzer | `uptrend_*.json` | Uptrend participation percentage |
| macro-regime-detector | `regime_*.json` | Current regime (Concentration, Broadening, etc.) |
| market-top-detector | `top_risk_*.json` | Distribution day count, top probability score |
| ftd-detector | `ftd_*.json` | Follow-Through Day quality (market bottom confirmation) |
| theme-detector | `theme_detector_*.json` or `theme_*.json` | Active investment themes and rotation |
| sector-analyst | `sector_*.json` | Sector performance rankings |
| institutional-flow-tracker | `institutional_*.json` | Net institutional buying/selling |

### Step 2: Run Exposure Scoring Engine

Execute the exposure scoring script with paths to upstream outputs:

```bash
python3 skills/exposure-coach/scripts/calculate_exposure.py \
  --breadth reports/breadth_latest.json \
  --uptrend reports/uptrend_latest.json \
  --regime reports/regime_latest.json \
  --top-risk reports/top_risk_latest.json \
  --ftd reports/ftd_latest.json \
  --theme reports/theme_latest.json \
  --sector reports/sector_latest.json \
  --institutional reports/institutional_latest.json \
  --output-dir reports/
```

The script accepts partial inputs; missing files reduce confidence but do not block execution.

**Verification pitfall:** After each run, inspect the generated JSON fields `inputs_provided` and `inputs_missing`. If a file you passed on the CLI still appears in `inputs_missing` (for example a theme-detector JSON that the exposure engine did not recognize), report the affected dimension as degraded and keep confidence capped; do not assume the supplied input was incorporated just because the CLI argument was present.

**Theme-detector ingestion caveat:** The theme detector commonly emits `theme_detector_YYYY-MM-DD_HHMMSS.json` with a `themes` object. If that file is not recognized by `calculate_exposure.py` and `theme` remains in `inputs_missing`, do not fold theme strength into the exposure ceiling manually. Instead, keep the Exposure Coach confidence capped, state that the theme dimension was not incorporated, and summarize theme/sector findings separately in the broader trading brief.

### Step 3: Interpret the Market Posture Summary

Review the generated posture report containing:

1. **Exposure Ceiling** -- Maximum recommended equity allocation (0-100%)
2. **Bias Direction** -- Growth vs Value tilt based on regime and flow
3. **Participation Assessment** -- Broad (healthy) vs Narrow (fragile) market
4. **Action Recommendation** -- NEW_ENTRY_ALLOWED, REDUCE_ONLY, or CASH_PRIORITY
5. **Confidence Level** -- HIGH, MEDIUM, or LOW based on input completeness

### Step 4: Apply Exposure Guidance

Map the posture recommendation to portfolio actions:

| Recommendation | Action |
|----------------|--------|
| NEW_ENTRY_ALLOWED | Proceed with stock-level analysis and new positions |
| REDUCE_ONLY | No new entries; trim existing positions on strength |
| CASH_PRIORITY | Raise cash aggressively; avoid all new commitments |

### Step 5: Gate Individual Candidates Before Execution (Optional)

The posture summary sets the *ceiling*; it does not clear a specific trade. Once
a candidate ticker has been chosen, hand it through the composable risk gates in
`scripts/risk_gates.py` before the order reaches execution. Each gate is an
independent pure function returning `{pass, reason}`; **all** gates are evaluated
(no short-circuit) so every blocking reason is captured for telemetry, then a
single decision object reports pass/fail per gate.

Gates enforced (each independently configurable, snake_case or camelCase keys accepted):

| Gate | Purpose |
|------|---------|
| confidence | Minimum conviction; regime-aware — a **lower** bar for trend-aligned trades |
| max_concurrent | Cap on simultaneously open positions |
| notional_cap | Per-trade dollar ceiling (absolute or % of equity) |
| daily_loss | Daily-loss kill switch (halts once P&L breaches a negative limit) |
| daily_giveback | Give-back halt — locks in a green day once it retraces from its peak |
| liquidity | Dollar-volume floor for longs (from liquidity-execution-cost) |
| short_liquidity | **Separate, deeper** dollar-volume floor for shorts (squeeze risk) |
| correlation | Cap on same-sector, same-direction exposure |
| cooldown | Minimum wait between trades |
| opposite_guard | Blocks flip / pyramid on a ticker already held |
| news | Binary-news blackout (from gdelt-news-catalyst) |

The gates **consume two sibling skills' documented JSON output**:

- `liquidity-execution-cost` supplies average dollar volume for the liquidity floors.
  Accepted keys (first present wins): `dollar_volume_usd`, `avg_dollar_volume_usd`,
  `adv_usd`, `dollar_volume`, or the same nested under a `liquidity` object. When no
  liquidity JSON is supplied, the floors are **not** enforced (a missing upstream
  signal must not silently block every trade).
- `gdelt-news-catalyst` supplies the blackout flag. Accepted keys: `blackout`,
  `has_blackout`, `binary_news_risk`, `blackout_active`; otherwise a `severity` of
  `high`/`critical` is treated as a blackout. `headline`/`catalyst`/`catalyst_type`
  becomes the block reason.

Trend alignment is derived from this skill's own posture recommendation: a **long**
is aligned when the recommendation is `NEW_ENTRY_ALLOWED`; a **short** is aligned
when it is `REDUCE_ONLY` or `CASH_PRIORITY`. Aligned candidates get the lower
`aligned_min_confidence` bar.

```bash
python3 skills/exposure-coach/scripts/risk_gates.py \
  --candidate reports/candidate_AAPL.json \
  --posture reports/exposure_posture_latest.json \
  --portfolio reports/account_state.json \
  --liquidity reports/liquidity_AAPL.json \
  --news reports/gdelt_AAPL.json \
  --config reports/risk_gate_config.json \
  --output-dir reports/
```

A candidate is `approved` only when every gate passes. The JSON decision lists
`results` (per-gate `{pass, reason}`), `passed_gates`, `failed_gates`, and the
aggregated `block_reasons`. Reports are saved as
`risk_gate_decision_<TICKER>_YYYY-MM-DD_HHMMSS.{json,md}`.

## Output Format

### JSON Report

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-03-16T07:00:00Z",
  "exposure_ceiling_pct": 70,
  "bias": "GROWTH",
  "participation": "BROAD",
  "recommendation": "NEW_ENTRY_ALLOWED",
  "confidence": "HIGH",
  "component_scores": {
    "breadth_score": 65,
    "uptrend_score": 72,
    "regime_score": 80,
    "top_risk_score": 25,
    "ftd_score": 10,
    "theme_score": 68,
    "sector_score": 70,
    "institutional_score": 75
  },
  "inputs_provided": ["breadth", "uptrend", "regime", "top_risk"],
  "inputs_missing": ["ftd", "theme", "sector", "institutional"],
  "rationale": "Broad participation with low top risk supports elevated exposure."
}
```

### Markdown Report

The markdown report provides a one-page summary suitable for quick review:

```markdown
# Market Posture Summary
**Date:** 2026-03-16 | **Confidence:** HIGH

## Exposure Ceiling: 70%

| Dimension | Score | Status |
|-----------|-------|--------|
| Breadth | 65 | Healthy |
| Uptrend Participation | 72% | Broad |
| Regime | Broadening | Favorable |
| Top Risk | 25 | Low |

## Recommendation: NEW_ENTRY_ALLOWED

**Bias:** Growth > Value
**Participation:** Broad (healthy internals)

### Rationale
Broad participation with low distribution day count supports elevated equity exposure.
New positions allowed within the 70% ceiling.
```

Reports are saved to `reports/` with filenames `exposure_posture_YYYY-MM-DD_HHMMSS.{json,md}`.

## Resources

- `scripts/calculate_exposure.py` -- Main orchestrator that scores and synthesizes inputs
- `scripts/risk_gates.py` -- Composable pre-execution risk gates (confidence, notional, daily-loss/give-back, liquidity floors, correlation, cooldown, pyramid guard, news blackout); consumes liquidity-execution-cost + gdelt-news-catalyst output
- `references/exposure_framework.md` -- Scoring rules and threshold definitions
- `references/regime_exposure_map.md` -- Regime-to-exposure ceiling mappings

## Key Principles

1. **Safety First** -- Default to lower exposure when inputs are incomplete or conflicting
2. **Regime Alignment** -- Let macro regime set the baseline; breadth adjusts within bounds
3. **Actionable Output** -- Always produce a clear recommendation, not just data aggregation
