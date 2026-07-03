---
name: dealer-gamma-analyzer
description: Quantify dealer options gamma (GEX) positioning for an equity or index from FREE CBOE delayed options data. Use when the user asks about dealer gamma, GEX, gamma exposure, gamma walls, call wall / put wall, gamma flip, zero-gamma level, max pain, pin risk, positive vs negative gamma regime, or whether a name is squeeze-prone or mean-reverting. Computes signed dealer gamma per strike, total GEX under two dealer-positioning conventions, and renders the walls as explicit support/resistance price levels. No paid feed required.
---

# Dealer Gamma Analyzer

## Overview

Map where options-dealer hedging pulls or pushes an equity/index by quantifying dealer gamma exposure (GEX) from CBOE's FREE ~15-minute-delayed options JSON. That feed already carries per-contract gamma, delta, IV and open interest, so dealer gamma is computed directly — no Black-Scholes re-derivation and no paid Unusual Whales / SqueezeMetrics subscription.

The 15-minute delay is irrelevant: dealer gamma positioning is a structural/daily map (where price gets pinned vs where it runs), not a tick signal.

Produces:

- **Total GEX** under two dealer-positioning conventions (both reported, always labeled)
- **Dollar gamma per 1% move** = `OI * gamma * spot^2` per strike
- **Call wall** (overhead gamma resistance) and **put wall** (gamma support), rendered as explicit S/R price levels with % distance from spot
- **Gamma-flip strike** — the regime divider between the pin zone and the trend/squeeze zone
- **Max pain** — the expiry pin price
- **Regime classification** — positive-gamma (pin / mean-revert / low realized vol) vs negative-gamma (amplify / trend / squeeze-prone) — plus the magnet strikes

## When to Use

- User asks about dealer gamma, GEX, or gamma exposure for a ticker or index
- User asks "where is the call wall / put wall / gamma flip / zero-gamma level?"
- User asks about max pain or pinning into expiry (OPEX)
- User wants to know if a name is squeeze-prone (negative gamma) or mean-reverting (positive gamma)
- User is planning a trade and wants the structural S/R levels dealer hedging defends
- Combine with technical-analyst or a screener to confirm whether a level is dealer-defended

## Prerequisites

- No API keys required (CBOE delayed feed is public and free)
- Python 3.9+ with the standard library only (network fetch uses stdlib `urllib`)

## Workflow

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

## Conventions (state which one is used)

- **Convention A — "dealers short calls / long puts" (SqueezeMetrics net):**
  `net_gex = call_gamma_$ - put_gamma_$` (signed). This is the headline number and drives the regime: positive -> dealers net long gamma -> stabilizing/pin; negative -> dealers net short gamma -> destabilizing/squeeze fuel.
- **Convention B — "customer-net-long-everything" (dealers short both):**
  `gross_hedge = call_gamma_$ + put_gamma_$` — an upper-bound on total hedging pressure per 1% move.

Both are reported. A net-GEX number with a flipped sign convention is worse than no number at all, so the labels are always attached.

## Output Format

### JSON Report (`reports/dealer_gex_<TICKER>_<timestamp>.json`)

```json
{
  "schema_version": "1.0",
  "ticker": "ZTEST",
  "spot": 100.0,
  "regime": "positive_gamma",
  "regime_label": "Positive gamma (dealers long gamma): pin / mean-revert / low realized vol",
  "gex": {
    "net_gex_mm_per_1pct": 2.125,
    "net_convention": "A: dealers short calls / long puts (SqueezeMetrics net = calls +, puts -)",
    "gross_hedge_mm_per_1pct": 2.875,
    "gross_convention": "B: customer-net-long-everything (dealers short both) = upper-bound hedging",
    "call_gex_mm_per_1pct": 2.5,
    "put_gex_mm_per_1pct": 0.375,
    "units": "$ millions of dealer hedging per 1% move in spot"
  },
  "support_resistance": {
    "resistance_call_wall": 110.0, "resistance_call_wall_pct_from_spot": 10.0,
    "spot": 100.0,
    "support_put_wall": 90.0, "support_put_wall_pct_from_spot": -10.0,
    "gamma_flip": 95.0, "gamma_flip_pct_from_spot": -5.0,
    "max_pain": 95.0, "max_pain_pct_from_spot": -5.0
  },
  "magnet_strikes": [{"strike": 110.0, "net_gex_mm": 0.9, "call_oi": 3000, "put_oi": 0}],
  "risk_indicators": {"call_put_oi_ratio": 5.0, "atm_iv_pct": 29.0, "contracts_analyzed": 9},
  "caveats": ["..."]
}
```

### Markdown Report (`reports/dealer_gex_<TICKER>_<timestamp>.md`)

Generated alongside the JSON. Contains the regime headline, the Support/Resistance map with the walls as explicit price levels, the magnet-strike table, risk indicators, the trading implication, and caveats.

## Resources

- `scripts/analyze_gex.py` — fetch CBOE delayed options JSON, parse OCC symbols, aggregate signed dealer gamma per strike, compute GEX / walls / flip / max pain, classify the regime, render markdown + JSON.
- `references/gex_interpretation.md` — pin-vs-squeeze trading framework: positive vs negative gamma, walls as S/R, max pain / OPEX pinning, gamma-cliff risk, and the squeeze-vs-rally diagnostic checklist.

## Key Principles

1. **Positive gamma pins, negative gamma amplifies.** In positive gamma, dealers sell rallies and buy dips (mean-revert); in negative gamma they buy rallies and sell dips (trend/squeeze).
2. **Walls are dealer-defended S/R, not guarantees.** Real flow can overwhelm a wall; treat them as levels where hedging leans against price, not brick walls.
3. **The gamma flip is the regime switch.** Spot above the flip = pin zone; below = trend/squeeze zone. It is a strike-space proxy for the zero-gamma spot level.
4. **Concentration is fragile.** A single near-dated strike dominating gamma means a sharp gamma cliff once it expires or spot moves past it.
5. **Always label the convention.** Report net (A) and gross (B) with their assumptions; never present an unlabeled GEX number.
6. **Descriptive, not predictive.** GEX maps where hedging pressure sits today; it does not forecast tomorrow's flows.
