---
name: macro-regime-detector
description: Detect structural macro regime transitions (1-2 year horizon) using cross-asset ratio analysis. Analyze RSP/SPY concentration, yield curve, credit conditions, size factor, equity-bond relationship, and sector rotation to identify regime shifts between Concentration, Broadening, Contraction, Inflationary, and Transitional states. Run when user asks about macro regime, market regime change, structural rotation, or long-term market positioning.
---

# Macro Regime Detector

Detect structural macro regime transitions using monthly-frequency cross-asset ratio analysis. This skill identifies 1-2 year regime shifts that inform strategic portfolio positioning.

## When to Use

- User asks about current macro regime or regime transitions
- User wants to understand structural market rotations (concentration vs broadening)
- User asks about long-term positioning based on yield curve, credit, or cross-asset signals
- User references RSP/SPY ratio, IWM/SPY, HYG/LQD, or other cross-asset ratios
- User wants to assess whether a regime change is underway

## Workflow

1. Load reference documents for methodology context:
   - `references/regime_detection_methodology.md`
   - `references/indicator_interpretation_guide.md`

2. Execute the main analysis script:
   ```bash
   uv run python3 skills/macro-regime-detector/scripts/macro_regime_detector.py --output-dir reports/
   ```
   This fetches 600 days of data for 9 ETFs + Treasury rates (~10 API calls total).
   An **FMP API key is required** to run this skill (the client raises if it is
   missing). For individual ETFs whose FMP historical-price endpoint returns
   nothing, the client automatically falls back to yfinance — this fallback
   needs no additional API key, but it does not remove the FMP key requirement.

3. Read the generated Markdown report and present findings to user.

4. Provide additional context using `references/historical_regimes.md` when user asks about historical parallels.

5. **Optional — anchor the regime in real macro numbers (FRED):** add `--with-fred`
   to the command above (or run `fred_series.py` standalone) to attach current
   yield-curve, inflation, policy-rate, and labor prints + trends to the classified
   regime. This grounds a named state (e.g. "Contraction") in actual figures — an
   inverted 10Y-2Y spread, core PCE running hot, unemployment ticking up — rather
   than cross-asset ratios alone. Needs a free FRED API key; if it is missing the
   run degrades gracefully and skips grounding.

## Prerequisites

- **FMP API Key** (required): Set `FMP_API_KEY` environment variable or pass `--api-key`
- Free tier (250 calls/day) is sufficient (script uses ~10 calls)
- **FRED API Key** (optional, for `--with-fred` / `fred_series.py`): free key from
  https://fred.stlouisfed.org/docs/api/api_key.html. Set `FRED_API_KEY` or pass
  `--fred-api-key` / `--api-key`. Enables real yield-curve/inflation grounding;
  omitted grounding never blocks the core detector run.

## 6 Components

| # | Component | Ratio/Data | Weight | What It Detects |
|---|-----------|------------|--------|-----------------|
| 1 | Market Concentration | RSP/SPY | 25% | Mega-cap concentration vs market broadening |
| 2 | Yield Curve | 10Y-2Y spread | 20% | Interest rate cycle transitions |
| 3 | Credit Conditions | HYG/LQD | 15% | Credit cycle risk appetite |
| 4 | Size Factor | IWM/SPY | 15% | Small vs large cap rotation |
| 5 | Equity-Bond | SPY/TLT + correlation | 15% | Stock-bond relationship regime |
| 6 | Sector Rotation | XLY/XLP | 10% | Cyclical vs defensive appetite |

## 5 Regime Classifications

- **Concentration**: Mega-cap leadership, narrow market
- **Broadening**: Expanding participation, small-cap/value rotation
- **Contraction**: Credit tightening, defensive rotation, risk-off
- **Inflationary**: Positive stock-bond correlation, traditional hedging fails
- **Transitional**: Multiple signals but unclear pattern

## Output

- `macro_regime_YYYY-MM-DD_HHMMSS.json` — Structured data for programmatic use
- `macro_regime_YYYY-MM-DD_HHMMSS.md` — Human-readable report with:
  1. Current Regime Assessment
  2. Transition Signal Dashboard
  3. Component Details
  4. Regime Classification Evidence
  5. Portfolio Posture Recommendations

## Relationship to Other Skills

| Aspect | Macro Regime Detector | Market Top Detector | Market Breadth Analyzer |
|--------|----------------------|--------------------|-----------------------|
| Time Horizon | 1-2 years (structural) | 2-8 weeks (tactical) | Current snapshot |
| Data Granularity | Monthly (6M/12M SMA) | Daily (25 business days) | Daily CSV |
| Detection Target | Regime transitions | 10-20% corrections | Breadth health score |
| API Calls | ~10 | ~33 | 0 (Free CSV) |

## Script Arguments

```bash
python3 macro_regime_detector.py [options]

Options:
  --api-key KEY       FMP API key (default: $FMP_API_KEY)
  --output-dir DIR    Output directory (default: current directory)
  --days N            Days of history to fetch (default: 600)
  --with-fred         Anchor the regime in real FRED prints + trends
  --fred-api-key KEY  FRED API key (default: $FRED_API_KEY)
```

## Macro Grounding (FRED)

`scripts/fred_series.py` fetches macro time series from the free FRED API and
feeds current prints + trends into the regime classification. It exposes a
`MACRO_SERIES` alias dictionary (`yield_curve`→T10Y2Y, `cpi`→CPIAUCSL,
`core_pce`→PCEPILFE, `fed_funds_rate`→FEDFUNDS, `unemployment`→UNRATE,
`ust2y`/`ust10y`/`ust30y`→DGS2/DGS10/DGS30, …); any unknown token is treated as a
raw FRED series ID. A configurable trailing window (`--look-back-days`, default
365 for the YoY base) and a per-series row cap (`--max-rows`, default 40) keep
daily series from flooding context.

```bash
# Standalone grounding report (JSON + Markdown) to reports/
export FRED_API_KEY=YOUR_KEY
uv run python3 skills/macro-regime-detector/scripts/fred_series.py --output-dir reports/

# Pick specific series and a longer window
uv run python3 skills/macro-regime-detector/scripts/fred_series.py \
  --series yield_curve cpi core_pce fed_funds_rate --look-back-days 540
```

Programmatic use: `build_macro_grounding()` returns `{"available": bool,
"series": {...}, ...}` (graceful `available: False` when `FRED_API_KEY` is
missing), and `anchor_regime(regime, grounding)` returns a copy of the regime
dict augmented with a `macro_grounding` block (headline prints, a one-line
summary, and consistency notes cross-checking the named regime against the real
yield-curve/inflation numbers). It never mutates its input, so it composes with
the scorer's `classify_regime` output without touching that module.

## Resources

- `references/regime_detection_methodology.md` — Detection methodology and signal interpretation
- `references/indicator_interpretation_guide.md` — Guide for interpreting cross-asset ratios
- `references/historical_regimes.md` — Historical regime examples for context
