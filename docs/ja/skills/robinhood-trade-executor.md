---
layout: default
title: "Robinhood Trade Executor"
grand_parent: 日本語
parent: スキルガイド
nav_order: 43
lang_peer: /en/skills/robinhood-trade-executor/
permalink: /ja/skills/robinhood-trade-executor/
generated: true
---

# Robinhood Trade Executor
{: .no_toc }

Auto-buy newly surfaced long-stock signals on Robinhood. Reads social-signal-ingestor's signals/index_last.json (the latest run's new records), keeps long stock only, and places one fixed-notional market buy per ticker via the Robinhood MCP connector's place_equity_order. Use as the buy/execution step of the social-signal-daily pipeline, or when the user asks to auto-buy / execute the fresh social-signal (YouTube) stock picks on Robinhood. Do NOT use for options, shorts, sells, or rebalancing — it only opens new long-stock positions.
{: .fs-6 .fw-300 }

<span class="badge badge-free">API不要</span>

[GitHubでソースを見る](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/robinhood-trade-executor){: .btn .fs-5 .mb-4 .mb-md-0 }

> **Note:** This page has not yet been translated into Japanese.
> Please refer to the [English version]({{ '/en/skills/robinhood-trade-executor/' | relative_url }}) for the full guide.
{: .warning }

---

[English版ガイドを見る]({{ '/en/skills/robinhood-trade-executor/' | relative_url }}){: .btn .btn-primary .fs-5 .mb-4 .mb-md-0 .mr-2 }
