# DCF Methodology — Detailed Reference

Expands on the workflow in `SKILL.md`. Read this when building the FCFF projection,
choosing terminal value, or handling industry-specific treatment. All formulas below
are implemented in `scripts/value_company.py` as pure (network-free) functions.

## When a DCF Is Appropriate

**Good fit:** mature companies with predictable cash flows; businesses whose revenue and
margin trajectory can be estimated within a reasonable band; a cross-check on a relative
valuation.

**Poor fit:** pre-revenue / early stage (no cash-flow history); banks and insurers (use a
dividend-discount or excess-return model); REITs (use NAV / P-FFO); highly cyclical names
without a clear mid-cycle baseline. The sector router in the script flags these and reweights
methods automatically.

## Unlevered Free Cash Flow (FCFF)

Project 5 years of unlevered free cash flow to the firm:

```
FCFF_t = EBIT_t x (1 - tax_rate)          # NOPAT
       + Depreciation & Amortization       # non-cash add-back
       - Capital Expenditures
       - Change in Net Working Capital
```

- **Revenue path** fades linearly from a Year-1 growth rate (analyst consensus or the
  historical revenue CAGR) down to the terminal growth rate by Year 5.
- **Margins** default to the 3-year median of each ratio to smooth cyclical noise.
- **Tax rate** uses the 3-year median effective rate, floored at 15% and capped at 30%.
- **Change in NWC** is modeled as an operating-NWC intensity applied to *incremental*
  revenue: `dNWC_t = nwc_intensity x (Revenue_t - Revenue_{t-1})`, where
  `nwc_intensity = (current assets - cash) - (current liabilities - short-term debt)` over
  base-year revenue. Growth consumes working capital; a decline releases it.

| Assumption | How derived | Typical range |
|---|---|---|
| Tax rate | 3-yr median effective rate | 15-25% US |
| D&A | % of revenue | 3-8% most; 15-25% telecom/utilities |
| CapEx | % of revenue | 3-8% SaaS; 8-15% industrials; 15-25% telecom |
| NWC intensity | operating NWC / revenue | 0-20% |
| Terminal g | long-run nominal GDP anchor | 1.5-3.0% US |

## WACC (CAPM)

```
Ke   = risk_free_rate + beta x equity_risk_premium + size_premium
Kd   = pre-tax cost of debt (interest_expense / total_debt, else IG-spread default)
Kd_at= Kd x (1 - tax_rate)                       # after-tax cost of debt
WACC = (E/V) x Ke + (D/V) x Kd_at
```

- **E** = market cap; **D** = total debt (book value proxies market value of debt); **V = E + D**.
- **Risk-free rate** is the live 10-year US Treasury yield; the script pulls FMP `treasury-rates`
  (field `year10`) or falls back to a supplied `--rf`, then a stale 4.5% default.
- **Size premium** adds return for smaller caps (see `wacc_rates.md`).

## Terminal Value — compute both, take the midpoint

```
TV_gordon = FCFF_5 x (1 + g) / (WACC - g)            # perpetuity growth
TV_exit   = EBITDA_5 x exit_EV/EBITDA_multiple        # exit multiple
TV_base   = 0.5 x (TV_gordon + TV_exit)
```

`EBITDA_5 = Revenue_5 x EBIT_margin + Revenue_5 x D&A%`. Taking the midpoint of the two
independent methods avoids over-anchoring on either the perpetuity assumption or a single
peer multiple. If the two diverge by more than ~30%, reconcile the growth and multiple inputs.

## Bridge to Equity Value

```
PV_FCFF = sum( FCFF_t / (1 + WACC)^t )   for t = 1..5
PV_TV   = TV_base / (1 + WACC)^5
EV      = PV_FCFF + PV_TV
Equity  = EV + Cash - Total Debt - Minority Interest - Preferred Stock
Implied share price = Equity / diluted shares outstanding
```

## Guardrail Gates

The script refuses or flags implausible outputs rather than printing a false-precision number:

- **`WACC <= g` gate (hard):** the Gordon formula explodes. For the base case the terminal
  growth is capped at `WACC - 0.5%` and flagged; each sensitivity cell where `w <= g` is left
  blank (null).
- **Terminal-value share of EV band:** if `PV_TV / EV > 0.85` the model is really a
  multiple-expansion bet; if `< 0.45` the terminal assumptions may be too conservative. Both
  emit a warning and the report shows both TV methods.
- **WACC sector sanity band:** a WACC outside the sector range in `wacc_rates.md` is flagged.

## Sensitivity & Scenarios

- **WACC x terminal-growth grid (5x5):** WACC +/-1% in 0.5% steps, `g` from 1.5% to 3.5% in
  0.5% steps. **Every cell re-runs the full DCF** (recomputes terminal value and re-discounts) —
  no linear approximation. Cells that violate the `WACC <= g` gate are null.
- **Bull / Base / Bear:** shift revenue growth +/-300bps, EBIT margin +/-200bps, WACC -/+100bps,
  and set terminal `g` to 3.0% / 2.5% / 1.5%. Each scenario re-runs the full DCF.

## Industry-Specific Notes

- **SaaS / software:** EV/Revenue often more meaningful than P/E; anchor with the Rule of 40
  (revenue growth% + FCF margin% >= 40). CapEx-light, R&D-heavy; decide SBC treatment up front.
- **Banks / insurance:** standard FCFF is wrong — use P/TBV or an excess-return model.
- **REITs:** use NAV or P/FFO; project NOI rather than FCFF.
- **Cyclicals (energy, semis, industrials):** normalize to mid-cycle margins.
- **CPG / staples / telecom:** stable and predictable — strong DCF candidates.

## Common Pitfalls

- Terminal value dominating EV (>85%) — disclose it as a multiple bet.
- Terminal growth exceeding WACC or long-run GDP.
- Double-counting stock-based comp (subtract from FCFF *or* use fully diluted shares, not both).
- Ignoring minority interest / preferred — they rank ahead of common equity in the bridge.
- Stale or implausible beta — fall back to the sector-default beta in `wacc_rates.md`.
</content>
</invoke>
