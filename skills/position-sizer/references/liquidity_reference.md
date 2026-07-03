# Liquidity and Execution-Cost Reference

Reference for the metrics computed by `scripts/liquidity_check.py` and for the
`--max-slippage-bps` gate in `scripts/position_sizer.py`. Liquidity determines
the *real* cost of a trade: the quoted price is not what you pay once spread,
slippage, and market impact are accounted for. A risk-optimal share count you
cannot execute cheaply is not optimal.

All thresholds below are rough guides for US equities and should inform, not
replace, judgment. These are models, not guarantees; realized cost depends on
execution strategy, time of day, and prevailing conditions.

## Bid-Ask Spread

- Absolute spread = `ask - bid`
- Midpoint = `(ask + bid) / 2`
- Relative spread (%) = `spread / midpoint * 100`
- Spread (bps) = `spread / midpoint * 10,000`

Always report the relative spread, not just the absolute figure: a $0.01 tick is
0.20% on a $5 stock but 0.002% on a $500 stock. A round trip (buy + sell) pays
the spread twice, so a 0.10% quoted spread costs roughly 0.20% per completed
trade before any market impact. Quotes are snapshots and may be delayed; treat a
single bid/ask reading as indicative, not live.

## Volume Metrics

- Average daily volume (ADV) = mean of daily share volume over the lookback
- Average daily dollar volume (ADDV) = mean of `close * volume`; the better
  cross-stock comparison because it normalizes for price level
- Volume coefficient of variation (CV) = `stdev(volume) / mean(volume)`. A CV
  above 1.0 means volume is spiky — the average is not reliably available on any
  given day, which matters for execution planning.

## Turnover vs. Float

- Daily turnover = `ADV / base_shares`, where `base_shares` is free float when
  available, otherwise shares outstanding (float is more informative because it
  measures trading against the actually tradable supply)
- Annualized turnover (%) = `daily_turnover * 252 * 100`
- Days to trade the float = `base_shares / ADV` (in trading days)

| Annualized float turnover | Interpretation |
|---|---|
| > 500% | Hyper-active — speculative / momentum / squeeze |
| 100–500% | Actively traded |
| 30–100% | Moderate, normal institutional pattern |
| < 30% | Thinly traded / closely held |

## Amihud Illiquidity Ratio

Amihud (2002) measures the daily price response per dollar of volume:

```
ILLIQ = mean( |daily return| / daily dollar volume ) over the lookback, x 1e9
```

Days with zero dollar volume are skipped (division by zero). Higher = less
liquid — more price "bang" per dollar traded. The ×1e9 scaling is the standard
readability convention.

| Amihud (×1e9) | Liquidity level |
|---|---|
| < 0.01 | Mega-cap, extremely liquid |
| 0.01–0.1 | Large-cap, highly liquid |
| 0.1–1.0 | Mid-cap, moderately liquid |
| 1.0–10 | Small-cap, less liquid |
| > 10 | Micro-cap, illiquid |

## Square-Root Market-Impact Model

The square-root law is one of the most robust empirical findings in market
microstructure: price impact scales with the square root of order size relative
to volume.

```
impact_bps = sigma * sqrt(order_shares / ADV) * 1e4
```

where `sigma` is daily return volatility (standard deviation of daily returns,
as a decimal). Concavity means doubling order size raises impact by only ~41%
(√2), not 100% — large orders are typically worked across time.

Inverting the model gives the largest order that fits a basis-point budget, which
is exactly how the position sizer's `--max-slippage-bps` gate caps a position:

```
Q_max = ADV * (max_slippage_bps / (sigma * 1e4)) ** 2
```

### Slippage grades

| Estimated impact (bps) | Grade |
|---|---|
| ≤ 10 | minimal |
| ≤ 25 | low |
| ≤ 50 | moderate |
| ≤ 100 | high |
| > 100 | severe |

Practical rule: an estimated impact above ~25 bps suggests the position is large
for the name's liquidity; above ~50 bps, consider splitting the order across days
or using an execution algorithm (VWAP/TWAP).

## Liquidity Grade

The overall grade is driven primarily by average daily dollar volume, then
downgraded one notch when the Amihud ratio is illiquid (> 1.0) or the spread is
wide (> 1%).

| Grade | Avg dollar volume |
|---|---|
| very_high | > $500M/day |
| high | $50M–$500M/day |
| moderate | $5M–$50M/day |
| low | $500K–$5M/day |
| very_low | < $500K/day |

## Warnings

`liquidity_check.py` flags:

- **micro-cap** — average dollar volume below $1M/day (execute carefully)
- **wide spread** — relative spread above 2%
- **spiky volume** — volume CV above 1.0 (average volume unreliable day to day)
- **high market impact** — estimated slippage above 50 bps for the given order

## Edge Cases

- Zero-volume days are excluded from the Amihud ratio to avoid division by zero.
- Use regular-hours data; extended-hours quotes have wider spreads and thin
  volume.
- ETFs can look illiquid on-screen while their underlying basket is liquid via
  create/redeem — screen-level metrics understate true ETF liquidity.
- For low-priced (penny) stocks the $0.01 minimum tick floors the absolute
  spread, so relative spread is the metric that matters.
