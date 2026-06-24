---
layout: default
title: "Edge Social Aggregator"
grand_parent: English
parent: Skill Guides
nav_order: 27
lang_peer: /ja/skills/edge-social-aggregator/
permalink: /en/skills/edge-social-aggregator/
generated: true
---

# Edge Social Aggregator
{: .no_toc }

Score and consolidate social trading signals (from social-signal-ingestor's YouTube/X/Reddit vault) into a per-ticker feed for edge-signal-aggregator. Applies objective social scoring — recency × corroboration (how many independent sources name the ticker), deduping the same ticker across many videos — then hands off; it deliberately does NOT redo cross-source merge, contradiction, or ranking. Use after ingesting social signals and before the main edge aggregation.
{: .fs-6 .fw-300 }

<span class="badge badge-free">No API</span>

[View Source on GitHub](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/edge-social-aggregator){: .btn .fs-5 .mb-4 .mb-md-0 }

<details open markdown="block">
  <summary>Table of Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## 1. Overview

The social-specific scoring stage of the edge pipeline. It reads the signal index
produced by `social-signal-ingestor` (`data/<agent>/vault/current/signals/index.json`),
scores each signal, deduplicates the same ticker across multiple videos/channels, and
emits `reports/edge_social_aggregator_<date>.json` — shaped for the `--social-signals`
parser in `edge-signal-aggregator`, which blends it in as **one low-weight source**.

It owns ONLY the social-specific work that exists nowhere else: recency × corroboration
scoring and per-ticker consolidation. Cross-source merge, contradiction
detection, and final ranking stay in `edge-signal-aggregator` (do not duplicate them here).

---

## 2. When to Use

- After `social-signal-ingestor` has produced/refreshed `signals/index.json`.
- Before running `edge-signal-aggregator`, to feed it a clean social source.
- NOT a standalone watchlist — its output is an input to the unified conviction dashboard.

---

## 3. Prerequisites

- A populated `data/<agent>/vault/current/signals/index.json` (run social-signal-ingestor first).
- No API keys; pure local calculation.

---

## 4. Quick Start

```bash
# 1. Score + consolidate social signals
python3 skills/edge-social-aggregator/scripts/aggregate_social.py \
  --agent social --output-dir reports/

# 2. Merge into the unified dashboard (low weight 0.10)
python3 skills/edge-signal-aggregator/scripts/aggregate_signals.py \
  --social-signals "reports/edge_social_aggregator_*.json" \
  --output-dir reports/
```

---

## 5. Workflow

```bash
# 1. Score + consolidate social signals
python3 skills/edge-social-aggregator/scripts/aggregate_social.py \
  --agent social --output-dir reports/

# 2. Merge into the unified dashboard (low weight 0.10)
python3 skills/edge-signal-aggregator/scripts/aggregate_signals.py \
  --social-signals "reports/edge_social_aggregator_*.json" \
  --output-dir reports/
```

---

## 6. Resources

**Scripts:**

- `skills/edge-social-aggregator/scripts/aggregate_social.py`
