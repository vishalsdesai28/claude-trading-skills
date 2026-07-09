---
layout: default
title: "Robinhood Trade Executor"
grand_parent: English
parent: Skill Guides
nav_order: 52
lang_peer: /ja/skills/robinhood-trade-executor/
permalink: /en/skills/robinhood-trade-executor/
generated: true
---

# Robinhood Trade Executor
{: .no_toc }

Auto-buy newly surfaced long-stock signals on Robinhood. Reads social-signal-ingestor's signals/index_last.json (the latest run's new records), keeps long stock only, and places one fixed-notional market buy per ticker via the Robinhood MCP connector's place_equity_order. Use as the buy/execution step of the social-signal-daily pipeline, or when the user asks to auto-buy / execute the fresh social-signal (YouTube) stock picks on Robinhood. Do NOT use for options, shorts, sells, or rebalancing — it only opens new long-stock positions.
{: .fs-6 .fw-300 }

<span class="badge badge-free">No API</span>

[Download Skill Package (.skill)](https://github.com/tradermonty/claude-trading-skills/raw/main/skill-packages/robinhood-trade-executor.skill){: .btn .btn-primary .fs-5 .mb-4 .mb-md-0 .mr-2 }
[View Source on GitHub](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/robinhood-trade-executor){: .btn .fs-5 .mb-4 .mb-md-0 }

<details open markdown="block">
  <summary>Table of Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## 1. Overview

Place real-money market buys on Robinhood for the **long-stock** tickers surfaced by
`social-signal-ingestor`'s weekly signal vault. Each selected ticker gets one fixed-notional
buy. The per-ticker dollar amount — the **buy amount** — is specified by the caller; do not
assume one. This skill drives the **Robinhood MCP connector** (Robinhood Agentic Trading,
`agent.robinhood.com`) `place_equity_order` tool directly — no Python order script, no
portfolio / tradability / buying-power pre-checks. It **trusts the upstream selection**: if a
signal is a long stock, it buys.

⚠️ **This places real orders with real money and is fully automated — there is no human
confirmation gate.** It is the only skill in the repo that auto-executes. Robinhood's own
disclosure applies: you are responsible for every trade the agent places. Run it only when the
connected Robinhood Agentic account is funded and intended for this purpose.

---

## 2. When to Use

- As the **buy step of `social-signal-daily`**, after `social-signal-ingestor` has produced
  `signals/index_last.json`. It no longer depends on `ticker-enricher` or `write-supabase` — it
  reads the ingestor's latest-run index directly.
- When the user asks to "buy the new social-signal stocks", "auto-buy fresh YouTube picks",
  or similar.

Do **not** use for options, short ideas, sells, or rebalancing — this skill only opens new
long-stock positions.

---

## 3. Prerequisites

The **Robinhood MCP connector** (Robinhood Agentic Trading) must be available to the session.
Tool used:

| Tool | Use |
|------|-----|
| `place_equity_order` | Place the market buy |

(If `place_equity_order` requires a share **quantity** rather than a dollar amount,
`get_equity_quotes` provides the real-time ask for fractional sizing.)

If the tool is not available, stop and tell the user the Robinhood connector must be
connected — do not attempt any other broker.

---

## 4. Quick Start

### Step 1: Select buy candidates (long stock only, deduped)

Read `social-signal-ingestor`'s `signals/index_last.json` (default
`data/social/vault/current/signals/index_last.json`, or the path given) — this holds only the
records added on the latest ingestor run, so the executor buys just the fresh picks and never
re-buys the week's earlier signals. (`index.json` is the full cumulative week; do not read it
here.) From its `signals` array,
keep a ticker as a buy candidate **only when all** of these hold:

---

## 5. Workflow

### Step 1: Select buy candidates (long stock only, deduped)

Read `social-signal-ingestor`'s `signals/index_last.json` (default
`data/social/vault/current/signals/index_last.json`, or the path given) — this holds only the
records added on the latest ingestor run, so the executor buys just the fresh picks and never
re-buys the week's earlier signals. (`index.json` is the full cumulative week; do not read it
here.) From its `signals` array,
keep a ticker as a buy candidate **only when all** of these hold:

- `instrument` is `null` or `"stock"` — skip `"option"`.
- `direction == "long"` — skip `"short"` and `"watch"`.
- no `watch` trigger object is present — skip conditional / not-yet-triggered longs (don't
  front-run a breakout).
- `ticker` is a non-empty symbol.

Dedupe: a ticker surfaced by multiple channels or notes collapses to one buy. **This selection
is the step that spends money — apply it exactly and buy nothing outside it.**

### Step 2: Execute the buys

For each ticker from Step 1, call `place_equity_order` — a **market BUY of the buy amount**
(dollar-based fractional order). Place **only one** buy per ticker; never retry a rejected order
automatically.

- If the tool requires a share **quantity** instead of a dollar amount, submit a fractional
  quantity of `buy_amount / ask` (from `get_equity_quotes`), rounded down to the supported
  precision so the order never exceeds the buy amount.

### Step 3: Write the order-confirmations report

Save `reports/robinhood_orders_<YYYY-MM-DD>.json` (the `order_confirmations` artifact). A list
(possibly empty on a no-signal day); `dollar_amount` records the buy amount used, e.g.:

```json
{
  "ticker": "AUPH",
  "dollar_amount": "<buy amount used>",
  "quantity": 2.978,
  "order_id": "<robinhood-order-id>",
  "status": "filled | pending | rejected",
  "submitted_at": "2026-06-29T13:31:00Z"
}
```

Then print a one-line-per-ticker summary. Report fills and rejections honestly — if an order was
rejected, say so plainly.

---

## 6. Resources

This skill uses built-in Claude capabilities without external scripts or references.
