# WACC, ERP, Risk-Free Rates & Sector Benchmarks

Reference values for cost-of-capital inputs and relative-valuation routing. Prefer live
values over these defaults whenever available. Consumed by `scripts/value_company.py`.

## Risk-Free Rate

Use the 10-year sovereign yield of the company's reporting currency. For US names the script
pulls the FMP `treasury-rates` endpoint and reads the `year10` field; the yfinance fallback
reads `^TNX` (quoted in %, divide by 100).

| Market | Instrument | Typical range |
|---|---|---|
| US | 10Y Treasury | 3.5-5.0% |
| Developed Europe | 10Y Bund/Gilt | 2.0-4.5% |
| Japan | 10Y JGB | 0.5-1.5% |

**Default when the live fetch fails:** `rf = 0.045` (4.5%), flagged as stale in the output.

## Equity Risk Premium (ERP)

Anchor on Damodaran's implied ERP. Intra-year, 5.5% is a reasonable US mid-range.

| Market | ERP default |
|---|---|
| US | 5.5% |
| Developed Europe | 6.0-6.5% |
| Japan | 6.0% |
| Emerging (broad) | 8.0-10.0% |

For emerging markets add a country risk premium: `ERP_country = ERP_mature + CRP`.

## Cost of Debt

**Preferred:** `interest_expense / total_debt` from the financial statements.
**Fallback:** a credit-rating spread over the risk-free rate.

| Rating | Spread over RF | Kd at RF=4.5% |
|---|---|---|
| AAA-AA | 0.5-1.2% | 5.0-5.7% |
| A | 1.2-1.8% | 5.7-6.3% |
| BBB | 1.8-2.5% | 6.3-7.0% |
| BB | 3.5-5.0% | 8.0-9.5% |
| B or below | 5.5%+ | 10.0%+ |

**Default when unknown:** `kd = 0.055` for large caps, `0.07` for mid caps. The after-tax cost
of debt is `kd x (1 - tax_rate)`.

## Levered Beta Defaults (by sector)

Used when the data source returns `None` or an implausible beta.

| Sector | Default beta |
|---|---|
| Utilities | 0.55 |
| Consumer staples | 0.70 |
| Telecom | 0.85 |
| Healthcare / pharma | 0.90 |
| REITs | 0.90 |
| Industrials | 1.05 |
| Financials (banks) | 1.15 |
| Consumer discretionary | 1.20 |
| Energy (integrated) | 1.10 |
| Technology (large-cap) | 1.15 |
| Technology (SaaS high-growth) | 1.35 |
| Semiconductors | 1.45 |
| Biotech (clinical) | 1.60 |

## WACC Sanity Ranges by Sector

A computed WACC outside these bands is flagged for input review.

| Sector | WACC range |
|---|---|
| Utilities | 5-7% |
| Consumer staples | 7-9% |
| Telecom (large) | 7-9% |
| Healthcare / pharma | 8-10% |
| Industrials | 8-11% |
| Consumer discretionary | 9-11% |
| Energy (majors) | 8-10% |
| Technology (large-cap) | 8-11% |
| SaaS high-growth | 10-13% |
| Semiconductors | 10-12% |
| Biotech | 11-14% |

## Size Premium (added to Ke)

| Market cap | Size premium |
|---|---|
| > $10B (large/mega) | 0% |
| $2-10B (mid) | 0.5-1.0% |
| $500M-2B (small) | 1.5-2.5% |
| $100-500M (micro) | 2.5-4.0% |
| < $100M (nano) | 4.0%+ |

## Terminal Growth Ceilings

Terminal `g` must be plausible relative to long-run nominal GDP.

| Economy | Max defensible g |
|---|---|
| US | 3.0% |
| Developed Europe | 2.5% |
| Japan | 1.5% |
| Emerging (India/China) | 4.0-5.0% |

## Sector Method Routing

The script routes to the right primary method by sector/industry rather than forcing a DCF:

| Company type | Primary method | Relative multiples | DCF? |
|---|---|---|---|
| Banks / insurance | Relative | **P/TBV**, P/B | No — DCF suppressed |
| REITs | Relative / NAV | **P/FFO**, P/AFFO | No — DCF suppressed |
| SaaS / high-growth software | Relative + DCF (with care) | **EV/Revenue** + Rule of 40 | Yes, low weight |
| Mature cash-flow (CPG, telecom, utilities) | DCF | P/E, EV/EBITDA | Yes, primary |
| Multi-segment conglomerate | SOTP | segment EV/EBITDA | Yes |

**Rule of 40** (SaaS anchor): `revenue_growth% + FCF_margin%`. Score >= 40 supports a
premium EV/Revenue multiple; < 30 warrants a discount.

## Peer Multiple Conventions

- Aim for 4-6 peers; use the **median** (robust to outliers), not the mean.
- Compute implied prices from at least two multiples and cross-check agreement.
- Adjust the peer median +/-10-30% when the target's growth or margin profile diverges
  materially, and always state the adjustment and its reason.
</content>
