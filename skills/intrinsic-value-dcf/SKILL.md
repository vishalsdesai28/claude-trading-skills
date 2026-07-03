---
name: intrinsic-value-dcf
description: Produce a triangulated intrinsic-value estimate for a US ticker by blending a discounted-cash-flow (DCF) model, peer-median relative multiples, and an optional sum-of-the-parts. Use when the user asks "what is X worth", "fair value of X", "intrinsic value", "build a DCF", "DCF for X", "WACC", "terminal value", "implied share price", "upside to fair value", "is X overvalued/undervalued", "peer/relative valuation", "EV/EBITDA target", or "sum of the parts". Builds WACC from CAPM with a live 10Y UST risk-free rate, projects 5-year unlevered FCFF with growth fade, computes dual terminal value, emits a fully-recalculated WACC x terminal-growth sensitivity grid plus Bull/Base/Bear scenarios, applies guardrail gates, and routes by sector (banks -> P/TBV, REITs -> P/FFO, SaaS -> EV/Revenue + Rule of 40).
---

# Intrinsic Value (DCF)

## Overview

Estimate the intrinsic value of a US company by triangulating three methods and blending them
into a single fair value with upside/downside versus the current market price:

1. **DCF** — project 5 years of unlevered free cash flow to the firm (FCFF) with a growth fade,
   discount at a CAPM-derived WACC, and take the midpoint of a Gordon-growth and an
   exit-multiple terminal value.
2. **Relative** — apply peer-median P/E, EV/Revenue, and EV/EBITDA to the target's fundamentals.
3. **SOTP** — optional sum-of-the-parts when distinct operating segments are supplied.

The skill emits a fully-recalculated WACC × terminal-growth sensitivity grid (every cell is a
genuine DCF re-run), Bull/Base/Bear scenarios, guardrail gates, and sector-aware method routing.
Output is research/educational, not financial advice.

## When to Use

- User asks what a company is worth, its fair value, or intrinsic value.
- User wants a DCF, WACC, terminal value, or implied share price.
- User asks whether a stock is over- or under-valued versus fundamentals.
- User wants a peer/relative valuation or a sum-of-the-parts.

## Prerequisites

- **Data (pick one):** FMP API key (`FMP_API_KEY` env var or `--api-key`); the keyless
  `--yfinance` fallback; or a pre-fetched FMP-shaped JSON via `--input-json` for offline runs.
- Python 3.9+. Only `requests` (FMP path) or `yfinance` (fallback) are needed, and both are
  imported lazily — the calculation engine runs on the standard library alone.

## Workflow

### Step 1: Identify the ticker and data path

Confirm the US ticker. Choose the data source: FMP (richest), yfinance (keyless), or a supplied
JSON. If the user provides custom assumptions (growth, terminal g, ERP, exit multiple), collect
them; otherwise the script derives sensible defaults from the financials.

### Step 2: Run the valuation

```bash
# FMP (uses FMP_API_KEY env var; fetches statements, ratios, peers, and the live 10Y UST)
python3 skills/intrinsic-value-dcf/scripts/value_company.py AAPL --output-dir reports/

# Keyless fallback
python3 skills/intrinsic-value-dcf/scripts/value_company.py AAPL --yfinance --output-dir reports/

# Offline / reproducible (pre-fetched FMP-shaped JSON; may embed a "risk_free_rate")
python3 skills/intrinsic-value-dcf/scripts/value_company.py ACME \
  --input-json data/acme_fmp.json --output-dir reports/

# Override assumptions
python3 skills/intrinsic-value-dcf/scripts/value_company.py MSFT \
  --erp 0.05 --terminal-growth 0.03 --exit-multiple 14 --y1-growth 0.12 --output-dir reports/

# Add a sum-of-the-parts (JSON list of segment dicts)
python3 skills/intrinsic-value-dcf/scripts/value_company.py CONGLOM \
  --segments-json data/segments.json --corporate-ev-deduction 2000000000 --output-dir reports/
```

Segment JSON items take either `{ "name", "ebitda", "ev_ebitda" }` or
`{ "name", "revenue", "ev_rev" }`.

### Step 3: Load the reference frameworks

Read `references/dcf_method.md` for the FCFF build, terminal-value, and bridge methodology, and
`references/wacc_rates.md` for cost-of-capital inputs, sector betas, WACC sanity bands, terminal-
growth ceilings, and the sector method-routing table. Cite the relevant framework when
explaining an assumption.

### Step 4: Interpret sector routing

The script routes by sector before valuing:

- **Banks / insurers** → relative on **P/TBV** (DCF suppressed).
- **REITs** → relative on **P/FFO** / NAV (DCF suppressed).
- **SaaS / high-growth software** → **EV/Revenue** + a Rule-of-40 anchor; DCF runs but carries
  low weight.
- **Mature cash-flow names** → DCF primary, cross-checked by peers.

When the DCF is suppressed, the blended fair value comes from the relative (and SOTP) methods.

### Step 5: Read the guardrails before quoting a number

Surface any guardrail warnings to the user rather than reporting a false-precision point value:

- **`WACC <= g` gate:** terminal growth is capped below WACC and flagged; the point estimate is
  unreliable if this fires.
- **Terminal-value share of EV** outside 45–85%: the model leans on terminal assumptions (or is
  overly conservative).
- **WACC outside the sector sanity band:** re-check beta, capital structure, and cost of debt.

### Step 6: Present the result

Lead with the headline (blended fair value, current price, % upside/downside, most bullish/
bearish method). Then walk through the snapshot, three-method summary, WACC and DCF build,
peer comparison, sensitivity grid, scenarios, and key risks — emphasizing which assumption
moves the answer most. Always note the "not financial advice" disclaimer.

## Output Format

Both a JSON and a Markdown report are written to `--output-dir` (default `reports/`) as
`intrinsic_value_<TICKER>_<YYYY-MM-DD_HHMMSS>.{json,md}`.

The JSON payload contains: `snapshot`, `sector_routing`, `assumptions` (WACC components, growth
path, margins), `dcf` (revenue/FCFF path, dual terminal value, EV-to-equity bridge, implied
price, TV share of EV), `relative` (peer medians and implied prices), `sotp` (when supplied),
`rule_of_40` (SaaS), `blended` (fair value, weights, upside %), `sensitivity` (5×5 WACC × g grid,
each cell a full re-run), `scenarios` (Bull/Base/Bear), and `guardrail_warnings`.

## Resources

- `references/dcf_method.md` — FCFF projection, WACC (CAPM), dual terminal value, equity bridge,
  guardrail gates, sensitivity/scenario mechanics, and industry-specific notes.
- `references/wacc_rates.md` — risk-free rates, ERP, cost-of-debt spreads, sector-default betas,
  WACC sanity bands, size premia, terminal-growth ceilings, sector method routing, Rule of 40.
- `scripts/value_company.py` — CLI valuation engine (FMP + yfinance fetch, pure calculation core).

## Key Principles

1. **Triangulate, don't trust one method.** A blended fair value across DCF, relative, and SOTP
   is more robust than any single point estimate.
2. **Sensitivity over precision.** A DCF is garbage-in/garbage-out; the WACC × g grid and
   scenarios matter more than the base-case number.
3. **Guardrails first.** Reject `WACC <= g`; flag terminal-value dominance and out-of-band WACC.
4. **Route by sector.** Do not force a DCF onto a bank or REIT — use P/TBV or P/FFO instead.
5. **Use medians for peers.** Peer-median multiples are robust to outliers; adjust ±10–30% with a
   stated reason when the target's growth or margin profile diverges.
6. **Live cost of capital.** Anchor WACC on the current 10Y UST and an after-tax cost of debt.
7. **Not financial advice.** Cross-check any decision against primary filings.
</content>
