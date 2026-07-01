---
name: edge-social-aggregator
description: Score and consolidate social trading signals (from social-signal-ingestor's YouTube/X/Reddit vault) into a per-ticker feed for edge-signal-aggregator. Applies objective social scoring — recency × corroboration (how many independent sources name the ticker), deduping the same ticker across many videos — then hands off; it deliberately does NOT redo cross-source merge, contradiction, or ranking. Use after ingesting social signals and before the main edge aggregation.
---

# Edge Social Aggregator

## Overview

The social-specific scoring stage of the edge pipeline. It reads the signal index
produced by `social-signal-ingestor` (`data/<agent>/vault/current/signals/index.json`),
scores each signal, deduplicates the same ticker across multiple videos/channels, and
emits `reports/edge_social_aggregator_<date>.json` — shaped for the `--social-signals`
parser in `edge-signal-aggregator`, which blends it in as **one low-weight source**.

It owns ONLY the social-specific work that exists nowhere else: recency × corroboration
scoring and per-ticker consolidation. Cross-source merge, contradiction
detection, and final ranking stay in `edge-signal-aggregator` (do not duplicate them here).

## When to Use

- After `social-signal-ingestor` has produced/refreshed `signals/index.json`.
- Before running `edge-signal-aggregator`, to feed it a clean social source.
- NOT a standalone watchlist — its output is an input to the unified conviction dashboard.

## Prerequisites

- A populated `data/<agent>/vault/current/signals/index.json` (run social-signal-ingestor first).
- No API keys; pure local calculation.

## Workflow

```bash
# 1. Score + consolidate social signals
python3 skills/edge-social-aggregator/scripts/aggregate_social.py \
  --agent social --output-dir reports/

# 2. Merge into the unified dashboard (low weight 0.10)
python3 skills/edge-signal-aggregator/scripts/aggregate_signals.py \
  --social-signals "reports/edge_social_aggregator_*.json" \
  --output-dir reports/
```

## Scoring

`social_conviction (0-1) = recency_factor × source_factor`

- `recency_factor`: 1.00 (≤1d), 0.95 (≤3d), 0.90 (≤7d), 0.85 (older/undated) — from `claim_date`.
- `source_factor`: `min(1.0, 0.5 + 0.1 × n_sources)` — corroboration: 1→0.6, 2→0.7, 5+→1.0.

Channels are pre-vetted before entering `channels.yaml`, so there is no per-channel
credibility weighting and no per-signal confidence — conviction is purely how recent
the claim is and how many independent sources name the ticker.

Multiple signals for the same ticker collapse into one: the strongest mention's framing
(title/direction/horizon) is kept, conviction is the max, and source links are unioned.
Multi-symbol or invented tickers are skipped (reported as `skipped_invalid_ticker`).

## Output

`reports/edge_social_aggregator_<date>.json`:

```json
{
  "source_skill": "edge_social_aggregator",
  "signals": [
    {"ticker": "NVDA", "direction": "long", "social_conviction": 0.68,
     "n_sources": 3, "top_sources": ["sources/youtube/..."],
     "time_horizon": "weekly", "title": "...", "timestamp": "2026-06-22"}
  ]
}
```

## Notes

- Channels are pre-vetted before entering `channels.yaml`, so there is no per-channel
  credibility weighting — every listed channel is treated as equally trustworthy.
- Weighting social below screener/regime sources is enforced downstream: the
  `edge_social_aggregator` weight in `edge-signal-aggregator` is the lowest (0.10).
