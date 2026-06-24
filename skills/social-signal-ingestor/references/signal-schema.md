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
- Set `claim_date` to when the video made the claim — recency is a scoring input.
- Conviction is computed downstream from recency and corroboration (how many sources
  name the ticker); channels are pre-vetted, so there is no per-signal confidence to set.
