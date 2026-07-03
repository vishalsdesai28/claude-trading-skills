# Yahoo Finance Screener Fields (Keyless yfinance Path)

These are the field names accepted by `scripts/yf_boolean_screen.py` (via the
yfinance `EquityQuery` screener). They are **Yahoo field names, not FinViz filter
codes** — do not mix them with the `fa_*` / `ta_*` codes used by the URL builder.

If a query uses an unknown field or an invalid `eq` / `is-in` value, yfinance
raises a clear `ValueError` naming the offending token.

## Operators

| Operator | Aliases | Operands | Meaning |
|---|---|---|---|
| `gt` / `lt` | `>` / `<` | `[field, number]` | greater / less than |
| `gte` / `lte` | `>=` / `<=` | `[field, number]` | greater / less than or equal |
| `eq` | `=`, `==` | `[field, value]` | equals (used for `sector`, `region`, `exchange`, `industry`) |
| `btwn` | `between` | `[field, low, high]` | inclusive numeric range |
| `is-in` | `in`, `is_in` | `[field, v1, v2, ...]` | membership in a value set |
| `and` / `or` | — | `[node, node, ...]` | boolean combination (2+ children) |

`and` binds tighter than `or` in the DSL; use parentheses to override.

## Commonly used fields

### Price / performance
- `intradayprice`, `eodprice` — current / prior-close price
- `intradaymarketcap`, `lastclosemarketcap.lasttwelvemonths` — market capitalization
- `percentchange` — intraday % change
- `fiftytwowkpercentchange` — 52-week % change (fraction, e.g. `0.25` = +25%)
- `intradaypricechange`, `lastclose52weekhigh.lasttwelvemonths`, `lastclose52weeklow.lasttwelvemonths`

### Trading / ownership
- `beta`, `dayvolume`, `eodvolume`, `avgdailyvol3m`
- `pctheldinst`, `pctheldinsider`

### Valuation
- `peratio.lasttwelvemonths` — trailing P/E
- `pricebookratio.quarterly` — P/B
- `pegratio_5y`
- `lastclosetevtotalrevenue.lasttwelvemonths`, `lastclosemarketcaptotalrevenue.lasttwelvemonths`

### Profitability / dividends
- `returnonequity.lasttwelvemonths`, `returnonassets.lasttwelvemonths`, `returnontotalcapital.lasttwelvemonths`
- `forward_dividend_yield` (fraction, e.g. `0.03` = 3%), `forward_dividend_per_share`
- `consecutive_years_of_dividend_growth_count`

### Income statement / growth
- `totalrevenues.lasttwelvemonths`, `totalrevenues1yrgrowth.lasttwelvemonths`
- `netincomeis.lasttwelvemonths`, `netincome1yrgrowth.lasttwelvemonths`
- `epsgrowth.lasttwelvemonths`, `quarterlyrevenuegrowth.quarterly`
- `ebitdamargin.lasttwelvemonths`, `grossprofitmargin.lasttwelvemonths`

### Leverage / liquidity
- `totaldebtequity.lasttwelvemonths`, `ltdebtequity.lasttwelvemonths`, `netdebtebitda.lasttwelvemonths`
- `currentratio.lasttwelvemonths`, `quickratio.lasttwelvemonths`

### Short interest
- `short_percentage_of_float.value`, `short_percentage_of_shares_outstanding.value`
- `days_to_cover_short.value`

## Categorical (`eq` / `is-in`) fields and valid values

- `region` — two-letter country codes, lowercase. Use `region eq us` to restrict
  to US-listed names (Yahoo's default universe is global). Examples: `us`, `ca`,
  `gb`, `au`, `de`, `jp`, `cn`, `in`, `fr`.
- `sector` — one of: `Technology`, `Consumer Defensive`, `Industrials`,
  `Real Estate`, `Basic Materials`, `Consumer Cyclical`, `Financial Services`,
  `Utilities`, `Communication Services`, `Energy`, `Healthcare`.
- `exchange`, `industry`, `peer_group` — additional categorical fields (see
  yfinance `EquityQuery.valid_values` for the full enumerations).

## Sort fields

Any numeric field above can be used as `--sort-field` (default
`intradaymarketcap`, descending). Common choices: `percentchange`,
`intradaymarketcap`, `forward_dividend_yield`, `peratio.lasttwelvemonths`.

## Notes

- Yields and percentage-change fields are expressed as **fractions**
  (`0.03` = 3%), not whole percents.
- The result universe is global; combine with `region eq us` (or an `exchange`
  filter) for US-only screens.
- Field availability tracks Yahoo Finance; run `EquityQuery` and inspect
  `valid_fields` / `valid_values` for the authoritative, current list.
