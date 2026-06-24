---
layout: default
title: "Edge Social Aggregator"
grand_parent: 日本語
parent: スキルガイド
nav_order: 27
lang_peer: /en/skills/edge-social-aggregator/
permalink: /ja/skills/edge-social-aggregator/
generated: true
---

# Edge Social Aggregator
{: .no_toc }

Score and consolidate social trading signals (from social-signal-ingestor's YouTube/X/Reddit vault) into a per-ticker feed for edge-signal-aggregator. Applies objective social scoring — recency × corroboration (how many independent sources name the ticker), deduping the same ticker across many videos — then hands off; it deliberately does NOT redo cross-source merge, contradiction, or ranking. Use after ingesting social signals and before the main edge aggregation.
{: .fs-6 .fw-300 }

<span class="badge badge-free">API不要</span>

[GitHubでソースを見る](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/edge-social-aggregator){: .btn .fs-5 .mb-4 .mb-md-0 }

> **Note:** This page has not yet been translated into Japanese.
> Please refer to the [English version]({{ '/en/skills/edge-social-aggregator/' | relative_url }}) for the full guide.
{: .warning }

---

[English版ガイドを見る]({{ '/en/skills/edge-social-aggregator/' | relative_url }}){: .btn .btn-primary .fs-5 .mb-4 .mb-md-0 .mr-2 }
