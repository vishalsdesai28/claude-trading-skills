---
layout: default
title: "Dynamic Exit Engine"
grand_parent: 日本語
parent: スキルガイド
nav_order: 19
lang_peer: /en/skills/dynamic-exit-engine/
permalink: /ja/skills/dynamic-exit-engine/
generated: true
---

# Dynamic Exit Engine
{: .no_toc }

Manage an adaptive trailing exit for an open LONG equity position. Use when the user asks how to trail a stop, where to move a stop on a winner, whether to exit or hold a position, how to lock in profits, breakeven stops, ATR trailing stops, give-back / round-trip protection, or wants a daily "manage my open positions" pass. Two-phase trailing-stop FSM (hard stop -> ratcheting profit floor) with breakeven lock, stale-flat timeout, ATR noise band, and consecutive-breach confirmation. Replays deterministically over a price series and persists a JSON tracker snapshot.
{: .fs-6 .fw-300 }

<span class="badge badge-free">API不要</span> <span class="badge badge-optional">FMP任意</span>

[GitHubでソースを見る](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/dynamic-exit-engine){: .btn .fs-5 .mb-4 .mb-md-0 }

> **Note:** This page has not yet been translated into Japanese.
> Please refer to the [English version]({{ '/en/skills/dynamic-exit-engine/' | relative_url }}) for the full guide.
{: .warning }

---

[English版ガイドを見る]({{ '/en/skills/dynamic-exit-engine/' | relative_url }}){: .btn .btn-primary .fs-5 .mb-4 .mb-md-0 .mr-2 }
