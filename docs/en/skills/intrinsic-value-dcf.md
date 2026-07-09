---
layout: default
title: "Intrinsic Value Dcf"
grand_parent: English
parent: Skill Guides
nav_order: 36
lang_peer: /ja/skills/intrinsic-value-dcf/
permalink: /en/skills/intrinsic-value-dcf/
generated: true
---

# Intrinsic Value Dcf
{: .no_toc }

Produce a triangulated intrinsic-value estimate for a US ticker by blending a discounted-cash-flow (DCF) model, peer-median relative multiples, and an optional sum-of-the-parts. Use when the user asks "what is X worth", "fair value of X", "intrinsic value", "build a DCF", "DCF for X", "WACC", "terminal value", "implied share price", "upside to fair value", "is X overvalued/undervalued", "peer/relative valuation", "EV/EBITDA target", or "sum of the parts". Builds WACC from CAPM with a live 10Y UST risk-free rate, projects 5-year unlevered FCFF with growth fade, computes dual terminal value, emits a fully-recalculated WACC x terminal-growth sensitivity grid plus Bull/Base/Bear scenarios, applies guardrail gates, and routes by sector (banks -> P/TBV, REITs -> P/FFO, SaaS -> EV/Revenue + Rule of 40).
{: .fs-6 .fw-300 }

<span class="badge badge-api">FMP Required</span>

[Download Skill Package (.skill)](https://github.com/tradermonty/claude-trading-skills/raw/main/skill-packages/intrinsic-value-dcf.skill){: .btn .btn-primary .fs-5 .mb-4 .mb-md-0 .mr-2 }
[View Source on GitHub](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/intrinsic-value-dcf){: .btn .fs-5 .mb-4 .mb-md-0 }

<details open markdown="block">
  <summary>Table of Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## 1. Overview

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

---

## 2. When to Use

- User asks what a company is worth, its fair value, or intrinsic value.
- User wants a DCF, WACC, terminal value, or implied share price.
- User asks whether a stock is over- or under-valued versus fundamentals.
- User wants a peer/relative valuation or a sum-of-the-parts.

---

## 3. Prerequisites

- **Data (pick one):** FMP API key (`FMP_API_KEY` env var or `--api-key`); the keyless
  `--yfinance` fallback; or a pre-fetched FMP-shaped JSON via `--input-json` for offline runs.
- Python 3.9+. Only `requests` (FMP path) or `yfinance` (fallback) are needed, and both are
  imported lazily — the calculation engine runs on the standard library alone.

---

## 4. Quick Start

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

---

## 5. Workflow

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

---

## 6. Resources

**References:**

- `skills/intrinsic-value-dcf/references/dcf_method.md`
- `skills/intrinsic-value-dcf/references/wacc_rates.md`

**Scripts:**

- `skills/intrinsic-value-dcf/scripts/value_company.py`
