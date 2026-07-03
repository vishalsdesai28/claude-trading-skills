---
name: data-quality-checker
description: Validate data quality in market analysis documents and blog articles before publication. Use when checking for price scale inconsistencies (ETF vs futures), instrument notation errors, date/day-of-week mismatches, allocation total errors, unit mismatches, and fabricated or inconsistent price/support/resistance levels cross-checked against a verified market data snapshot. Supports English and Japanese content. Advisory mode -- flags issues as warnings for human review, not as blockers.
---

## Overview

Detect common data quality issues in market analysis documents before
publication. The checker validates six categories: price scale consistency,
instrument notation, date/weekday accuracy, allocation totals, unit usage, and
verified-snapshot consistency (cited price/support/resistance levels
cross-checked against a deterministic ground-truth market snapshot). All
findings are advisory -- they flag potential issues for human review rather
than blocking publication.

## When to Use

- Before publishing a weekly strategy blog or market analysis report
- After generating automated market summaries
- When reviewing translated documents (English/Japanese) for data accuracy
- When combining data from multiple sources (FRED, FMP, FINVIZ) into one report
- As a pre-flight check for any document containing financial data
- Before publishing a single-ticker technical write-up that cites specific
  price, support, or resistance levels -- build a verified snapshot first and
  cross-check the draft against it to catch confabulated or scale-mismatched
  numbers

## Prerequisites

- Python 3.9+
- The core checks (price scale, notation, dates, allocations, units) require no
  API keys and no third-party packages (standard library only)
- The optional verified-snapshot workflow (`market_snapshot.py`) fetches OHLCV
  from yfinance (no key) or FMP (`FMP_API_KEY` or `--api-key`). Indicators are
  computed in pure Python, so the snapshot *cross-check* itself needs no
  packages -- it consumes a snapshot JSON that is already on disk

## Workflow

### Step 1: Receive Input Document

Accept the target markdown file path and optional parameters:
- `--file`: Path to the markdown document to validate (required)
- `--checks`: Comma-separated list of checks to run (optional; default: all)
- `--as-of`: Reference date for year inference in YYYY-MM-DD format (optional)
- `--output-dir`: Directory for report output (optional; default: `reports/`)

### Step 2: Execute Validation Script

Run the data quality checker script:

```bash
python3 skills/data-quality-checker/scripts/check_data_quality.py \
  --file path/to/document.md \
  --output-dir reports/
```

To run specific checks only:

```bash
python3 skills/data-quality-checker/scripts/check_data_quality.py \
  --file path/to/document.md \
  --checks price_scale,dates,allocations
```

To provide a reference date for year inference (useful for documents without
explicit year in dates):

```bash
python3 skills/data-quality-checker/scripts/check_data_quality.py \
  --file path/to/document.md \
  --as-of 2026-02-28
```

### Step 2b: Verified Market Snapshot Cross-Check (optional)

For single-ticker technical reports that cite exact price, support, or
resistance levels, build a deterministic ground-truth snapshot and cross-check
the draft against it. This catches confabulated numbers -- levels far outside
the verified recent range, or a stated current price that contradicts the
verified close.

First build the snapshot (look-ahead rows after the analysis date are excluded
defensively; the fixed 11-indicator set -- EMA/SMA/RSI/Bollinger/MACD/ATR -- is
computed in pure Python):

```bash
# yfinance (no API key)
python3 skills/data-quality-checker/scripts/market_snapshot.py \
  --ticker AAPL --date 2026-02-28 --output-dir reports/

# FMP (requires FMP_API_KEY or --api-key)
python3 skills/data-quality-checker/scripts/market_snapshot.py \
  --ticker AAPL --date 2026-02-28 --source fmp --output-dir reports/
```

The snapshot script writes a `market_snapshot_<TICKER>_<timestamp>.md`
(ground-truth text block with guardrail language) and a matching `.json`
(structured snapshot). Present the `.md` block to any downstream model as the
single source of truth for exact numbers.

Then run the checker with the snapshot JSON to cross-check the draft report:

```bash
python3 skills/data-quality-checker/scripts/check_data_quality.py \
  --file path/to/aapl_writeup.md \
  --snapshot reports/market_snapshot_AAPL_2026-02-28_143000.json \
  --output-dir reports/
```

The `snapshot` check is a no-op unless `--snapshot` is supplied, so default
runs are unaffected.

### Step 3: Load Reference Standards

Read the relevant reference documents to contextualize findings:

- `references/instrument_notation_standard.md` -- Standard ticker notation,
  digit-count hints, and naming conventions for each instrument class
- `references/common_data_errors.md` -- Catalog of frequently observed errors
  including FRED data delays, ETF/futures scale confusion, holiday oversights,
  allocation total pitfalls, and unit confusion patterns

Use these references to explain findings and suggest corrections.

### Step 4: Review Findings

Examine each finding in the output:

- **ERROR** -- High confidence issues (e.g., date-weekday mismatches verified
  by calendar computation, or a stated current price contradicting the verified
  snapshot close by more than 25%). Strongly recommend correction.
- **WARNING** -- Likely issues that need human judgment (e.g., price scale
  anomalies, notation inconsistencies, allocation sums off by more than 0.5%,
  cited support/resistance levels outside the verified snapshot band).
- **INFO** -- Informational notes (e.g., mixed bp/% usage that may be
  intentional).

### Step 5: Generate Quality Report

The script produces two output files:

1. **JSON report** (`data_quality_YYYY-MM-DD_HHMMSS.json`): Machine-readable
   list of findings with severity, category, message, line number, and context.
2. **Markdown report** (`data_quality_YYYY-MM-DD_HHMMSS.md`): Human-readable
   report grouped by severity level.

Present the findings to the user with explanations referencing the knowledge
base. Suggest specific corrections for each issue.

## Output Format

### JSON Finding Structure

```json
{
  "severity": "WARNING",
  "category": "price_scale",
  "message": "GLD: $2,800 has 4 digits (expected 2-3 digits)",
  "line_number": 5,
  "context": "GLD: $2,800"
}
```

A `snapshot`-category finding looks like:

```json
{
  "severity": "WARNING",
  "category": "snapshot",
  "message": "Cited support level $50.00 is outside the verified price band [$131.00, $169.00] (recent range $140.00-$160.00). Possible fabricated or scale-mismatched level.",
  "line_number": 12,
  "context": "support at $50"
}
```

### Snapshot Input Shape (consumed by the `snapshot` check)

`market_snapshot.py` emits, and the `--snapshot` check consumes, this JSON:

```json
{
  "symbol": "AAPL",
  "analysis_date": "2026-02-28",
  "latest_row": {"date": "2026-02-27", "open": 0, "high": 0, "low": 0, "close": 150.0, "volume": 0},
  "indicators": {"close_10_ema": 0, "close_50_sma": 0, "close_200_sma": null, "rsi": 0, "boll": 0, "boll_ub": 0, "boll_lb": 0, "macd": 0, "macds": 0, "macdh": 0, "atr": 3.0},
  "recent_closes": [{"date": "2026-02-27", "close": 150.0}],
  "recent_high": 160.0,
  "recent_low": 140.0,
  "guardrail": "Treat this snapshot as the single source of truth ..."
}
```

The cross-check derives a tolerance band of `[recent_low - 3*ATR,
recent_high + 3*ATR]` (falling back to +/-15% of the latest close when ATR is
`null`). Cited support/resistance/target levels outside the band are flagged,
as are stated current prices that differ from `latest_row.close` by more than
5% (WARNING) or 25% (ERROR).

### Markdown Report Structure

```markdown
# Data Quality Report
**Source:** path/to/document.md
**Generated:** 2026-02-28 14:30:00
**Total findings:** 3

## ERROR (1)
- **[dates]** (line 12): Date-weekday mismatch: January 1, 2026 (Monday) -- actual weekday is Thursday

## WARNING (2)
- **[price_scale]** (line 5): GLD: $2,800 has 4 digits (expected 2-3 digits)
  > `GLD: $2,800`
- **[allocations]**: Allocation total: 110.0% (expected ~100%)
```

## Resources

- `scripts/check_data_quality.py` -- Main validation script (includes the
  `snapshot` cross-check)
- `scripts/market_snapshot.py` -- Builds the verified ground-truth market
  snapshot (OHLCV + fixed 11-indicator set) and renders the guardrail text block
- `references/instrument_notation_standard.md` -- Notation and price scale reference
- `references/common_data_errors.md` -- Common error patterns and prevention

## Key Principles

1. **Advisory mode**: All findings are warnings for human review. The script
   always exits with code 0 on successful execution, even when findings are
   present. Exit code 1 is reserved for script failures (file not found, parse
   errors).

2. **Section-aware allocation checking**: Only percentages within allocation
   sections (identified by headings like "配分", "Allocation", or table columns
   like "ウェイト", "目安比率") are checked. Random percentages in body text
   (probability, RSI, YoY growth) are ignored.

3. **Bilingual support**: Handles both English and Japanese date formats,
   weekday names, and section headings. Full-width characters (％, 〜, en-dash)
   are normalized before processing.

4. **Year inference**: For dates without an explicit year, the checker infers
   the year using (in priority order): the `--as-of` option, a YYYY pattern
   found in the document title/metadata, or the current year with a 6-month
   cross-year heuristic.

5. **Digit-count heuristic**: Price scale validation uses digit counts (number
   of digits before the decimal point) rather than absolute price ranges. This
   approach is resilient to price changes over time while still catching
   ETF/futures confusion errors.

6. **Verified snapshot as source of truth**: The `snapshot` check never
   invents a "correct" number. It flags cited levels that fall well outside the
   deterministically computed recent range (or a stated current price that
   contradicts the verified close), and defers the judgment to a human. The
   look-ahead cutoff is re-applied defensively when the snapshot is built so no
   post-analysis-date row can leak into the ground truth. Indicators are
   computed in pure Python with documented conventions (see `market_snapshot.py`
   module docstring) so the snapshot is byte-stable across runs.
