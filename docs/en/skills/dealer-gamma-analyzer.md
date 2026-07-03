---
layout: default
title: "Dealer Gamma Analyzer"
grand_parent: English
parent: Skill Guides
nav_order: 15
lang_peer: /ja/skills/dealer-gamma-analyzer/
permalink: /en/skills/dealer-gamma-analyzer/
generated: true
---

# Dealer Gamma Analyzer
{: .no_toc }

Quantify dealer options gamma (GEX) positioning for an equity or index from FREE CBOE delayed options data. Use when the user asks about dealer gamma, GEX, gamma exposure, gamma walls, call wall / put wall, gamma flip, zero-gamma level, max pain, pin risk, positive vs negative gamma regime, or whether a name is squeeze-prone or mean-reverting. Computes signed dealer gamma per strike, total GEX under two dealer-positioning conventions, and renders the walls as explicit support/resistance price levels. No paid feed required.
{: .fs-6 .fw-300 }

<span class="badge badge-free">No API</span>

[View Source on GitHub](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/dealer-gamma-analyzer){: .btn .fs-5 .mb-4 .mb-md-0 }

<details open markdown="block">
  <summary>Table of Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## 1. Overview

Map where options-dealer hedging pulls or pushes an equity/index by quantifying dealer gamma exposure (GEX) from CBOE's FREE ~15-minute-delayed options JSON. That feed already carries per-contract gamma, delta, IV and open interest, so dealer gamma is computed directly — no Black-Scholes re-derivation and no paid Unusual Whales / SqueezeMetrics subscription.

The 15-minute delay is irrelevant: dealer gamma positioning is a structural/daily map (where price gets pinned vs where it runs), not a tick signal.

Produces:

- **Total GEX** under two dealer-positioning conventions (both reported, always labeled)
- **Dollar gamma per 1% move** = `OI * gamma * spot^2` per strike
- **Call wall** (overhead gamma resistance) and **put wall** (gamma support), rendered as explicit S/R price levels with % distance from spot
- **Gamma-flip strike** — the regime divider between the pin zone and the trend/squeeze zone
- **Max pain** — the expiry pin price
- **Regime classification** — positive-gamma (pin / mean-revert / low realized vol) vs negative-gamma (amplify / trend / squeeze-prone) — plus the magnet strikes

---

## 2. When to Use

- User asks about dealer gamma, GEX, or gamma exposure for a ticker or index
- User asks "where is the call wall / put wall / gamma flip / zero-gamma level?"
- User asks about max pain or pinning into expiry (OPEX)
- User wants to know if a name is squeeze-prone (negative gamma) or mean-reverting (positive gamma)
- User is planning a trade and wants the structural S/R levels dealer hedging defends
- Combine with technical-analyst or a screener to confirm whether a level is dealer-defended

---

## 3. Prerequisites

- No API keys required (CBOE delayed feed is public and free)
- Python 3.9+ with the standard library only (network fetch uses stdlib `urllib`)

---

## 4. Quick Start

```bash
# Live equity (fetches the free CBOE delayed feed)
python3 skills/dealer-gamma-analyzer/scripts/analyze_gex.py NVDA --output-dir reports/

# Index underlying (alias auto-mapped to _SPX)
python3 skills/dealer-gamma-analyzer/scripts/analyze_gex.py SPX --output-dir reports/

# Offline / reproducible: analyze a previously saved CBOE payload
python3 skills/dealer-gamma-analyzer/scripts/analyze_gex.py NVDA \
  --payload-json path/to/cboe_nvda.json --output-dir reports/

# Show more magnet strikes
python3 skills/dealer-gamma-analyzer/scripts/analyze_gex.py TSLA --top-magnets 8 --output-dir reports/
```

---

## 5. Workflow

### Step 1: Resolve the Underlying

For an index, map the alias to the CBOE symbol namespace. The script's `underlying_for()` handles this: `SPX`/`SP500` -> `_SPX`, `NDX`/`NASDAQ100` -> `_NDX`, `RUT` -> `_RUT`, `VIX` -> `_VIX`, `DJX`/`DOW` -> `_DJX`, `XSP`, `OEX`, `XEO`. Equity tickers (e.g. `NVDA`) pass through unchanged. A leading `^` or `$` is stripped.

### Step 2: Run the Analyzer

```bash
# Live equity (fetches the free CBOE delayed feed)
python3 skills/dealer-gamma-analyzer/scripts/analyze_gex.py NVDA --output-dir reports/

# Index underlying (alias auto-mapped to _SPX)
python3 skills/dealer-gamma-analyzer/scripts/analyze_gex.py SPX --output-dir reports/

# Offline / reproducible: analyze a previously saved CBOE payload
python3 skills/dealer-gamma-analyzer/scripts/analyze_gex.py NVDA \
  --payload-json path/to/cboe_nvda.json --output-dir reports/

# Show more magnet strikes
python3 skills/dealer-gamma-analyzer/scripts/analyze_gex.py TSLA --top-magnets 8 --output-dir reports/
```

If the feed is unavailable for a symbol, the script exits non-zero with a clear stderr message; retry, or pass `--payload-json` with a saved payload.

### Step 3: Load the Interpretation Reference

Read `references/gex_interpretation.md` for the pin-vs-squeeze framework: what positive vs negative gamma means for realized volatility, how to trade toward or against the walls, how max pain and OPEX pinning interact, and the gamma-cliff risk after a concentrated near-dated strike expires.

### Step 4: Present the Gamma Map

Report in this order:

1. **Regime headline** — positive-gamma (pin / mean-revert) or negative-gamma (amplify / squeeze-prone), with the net GEX figure.
2. **Support / Resistance map** — call wall (resistance), spot, put wall (support), gamma flip, max pain, each as a price level with % distance from spot.
3. **Magnet strikes** — the largest-|net gamma| strikes price tends to gravitate toward.
4. **Risk indicators** — call/put OI ratio and ATM IV (a call-heavy ratio + elevated IV is squeeze-consistent).
5. **Trading implication** — fade extremes toward the walls in a pin regime; respect momentum and squeeze risk in a negative-gamma regime.
6. **Caveats** — GEX assumes uniform dealer positioning; the gamma flip is a strike-space proxy; the data is delayed; this is descriptive, not predictive.

---

## 6. Resources

**References:**

- `skills/dealer-gamma-analyzer/references/gex_interpretation.md`

**Scripts:**

- `skills/dealer-gamma-analyzer/scripts/analyze_gex.py`
