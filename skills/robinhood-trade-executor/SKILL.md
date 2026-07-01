---
name: robinhood-trade-executor
description: Auto-buy newly surfaced long-stock signals on Robinhood. Reads social-signal-ingestor's signals/index_last.json (the latest run's new records), keeps long stock only, and places one fixed-notional market buy per ticker via the Robinhood MCP connector's place_equity_order. Use as the buy/execution step of the social-signal-daily pipeline, or when the user asks to auto-buy / execute the fresh social-signal (YouTube) stock picks on Robinhood. Do NOT use for options, shorts, sells, or rebalancing — it only opens new long-stock positions.
---

# Robinhood Trade Executor

## Overview

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

## When to Use

- As the **buy step of `social-signal-daily`**, after `social-signal-ingestor` has produced
  `signals/index_last.json`. It no longer depends on `ticker-enricher` or `write-supabase` — it
  reads the ingestor's latest-run index directly.
- When the user asks to "buy the new social-signal stocks", "auto-buy fresh YouTube picks",
  or similar.

Do **not** use for options, short ideas, sells, or rebalancing — this skill only opens new
long-stock positions.

## Prerequisites

The **Robinhood MCP connector** (Robinhood Agentic Trading) must be available to the session.
Tool used:

| Tool | Use |
|------|-----|
| `place_equity_order` | Place the market buy |

(If `place_equity_order` requires a share **quantity** rather than a dollar amount,
`get_equity_quotes` provides the real-time ask for fractional sizing.)

If the tool is not available, stop and tell the user the Robinhood connector must be
connected — do not attempt any other broker.

## Workflow

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

## Safety Rails

These matter because this skill spends real money with no human gate — the connector is happy to
do far more than this skill should, so the rails keep it inside its lane.

- **Long stock only.** The connector can also place options and shorts; this skill must not. Buy
  nothing Step 1 didn't select as long stock, so a mislabeled option or short can never become an
  equity buy.
- **One buy per ticker per run, no auto-retry.** A rejected order stays rejected — retrying blind
  is how you double-spend or fight a halted symbol. Surface the rejection instead.
- **No improvisation.** Execute exactly the Step 1 selection, nothing more. Don't add tickers,
  resize positions, or "fix" a signal you disagree with — that's an upstream concern.

## Reference

- Input: `social-signal-ingestor` `signals/index_last.json` (built by
  `skills/social-signal-ingestor/scripts/build_signal_index.py` — this run's new records only,
  same schema as `index.json`). Relevant per-signal fields:
  `ticker`, `direction` (`long|short|watch`), `instrument` (`null|stock|option`), `watch`
  (trigger/invalidation object when the long is conditional).
- Robinhood tools: the connected **Robinhood MCP connector** (Robinhood Agentic Trading) — call
  `place_equity_order` directly; options/crypto/scanner tools are out of scope.
- Output artifact `order_confirmations` is the audit trail for the day's automated buys; consumed
  by no other step.
