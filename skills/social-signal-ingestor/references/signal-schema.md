# Signal note schema (vault)

Each signal note is one Markdown file in `data/<agent>/vault/current/signals/`
named `YYYY-MM-DD_TICKER_short-slug.md`. The YAML frontmatter below is the
machine contract `build_signal_index.py` reads — keep field names exact.

```yaml
---
title: <human-readable one-line signal>      # required
type: signal                                  # required, literal "signal"
status: watching                              # active | watching | invalidated | resolved
ticker: NVDA                                  # ONE real, quotable symbol per note (never "STRL/POWL")
direction: long                               # long | short | watch  (drives aggregator direction)
time_horizon: weekly                          # intraday | swing | weekly | multiweek | unknown
claim_date: 2026-06-21                        # date the SOURCE made the claim (not today)
probability: 55                               # optional 0-100, only with a real basis
instrument: stock                             # optional: stock | option (default stock)
option_strategy: long_call                    # optional, OPTION only: long_call, covered_call, put_credit_spread, …
option_legs:                                  # optional, OPTION only: one entry per leg
  - {side: buy, right: call, strike: 120.0, expiry: 2026-07-18, ratio: 1}
  - {side: sell, right: call, strike: 130.0, expiry: 2026-07-18, ratio: 1}
net_premium: 3.50                             # optional, OPTION only: net debit (+) / credit (-) at claim
sources:                                      # wikilinks to the source notes that back this signal
  - [[sources/youtube/2026-06-21_abc123]]
tags: [social-signal, signal, youtube]
# Optional — include ONLY when the source states clean numeric levels:
watch:
  trigger: {op: close_above, level: 120.0, window: weekly}
  invalidation: {op: close_below, level: 110.0, window: weekly}
---
```

## Rules
- **One ticker per note.** Split a multi-name idea into separate notes. The index
  build flags `A/B` or `A,B` ticker strings.
- **`direction` is required.** `edge-social-aggregator` reads it directly.
- Omit the `watch` block unless the source gives a clean trigger/invalidation pair.
- Leave `instrument` unset for ordinary stock picks (it defaults to `stock`). Set
  `instrument: option` + an `option_strategy` only when the source actually calls an options trade.
- For options, add one `option_legs` entry per leg (a single long call is a one-leg list) and
  `net_premium` if the source gives the trade's cost — that's the real P&L baseline, since the
  stored `price_at_recommendation` tracks the underlying, not the option.
- Set `claim_date` to when the video made the claim — recency is a scoring input.
- Conviction is computed downstream from recency and corroboration (how many sources
  name the ticker); channels are pre-vetted, so there is no per-signal confidence to set.
