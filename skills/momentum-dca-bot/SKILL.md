---
name: momentum-dca-bot
description: Fully mechanical daily momentum DCA system - screen momentum-pullback stocks (Yahoo Finance keyless, or Finviz Elite/CSV), buy a fixed $300 notional of the top-ranked name via Alpaca (paper by default), book profits by selling half at +20%, and exit on 50DMA break or 15% trailing stop. Use when the user wants a daily momentum buying bot, dollar-cost averaging into momentum leaders, automated profit taking, or a Finviz-style momentum pullback screen without paid APIs.
---

# Momentum DCA Bot

## Overview

A daily, fully mechanical momentum sleeve: screen for strong uptrending stocks
pulling back within a bull regime, allocate a fixed notional every trading day,
book profits into strength, and exit on trend breaks. No discretion at any step;
every action lands in an append-only trade log.

**Strategy rules:**

| Stage | Rule |
|-------|------|
| Screen | YTD +50%, above 20/50/200 SMA, down on the week, mid-cap+ ($2B+), price > $10, avg vol > 500K, within 10% of 52-week high, analyst buy-or-better |
| Rank | Perf Month desc; tiebreak Perf Week desc (shallowest pullback = relative strength) |
| Regime gate | Buys only while SPY closes above its 50DMA; exits always active |
| Buy | $300 notional/day, top-ranked name, max 3 lots ($900) per name, one buy/day |
| Book profits | Sell HALF the first time a name closes ≥ +20% over average entry |
| Exit | Sell ALL on close < 50DMA or 15% off high-water close, whichever first |

After the scale-out, the 15% trail mathematically locks the remainder in above
breakeven on a daily-close basis (0.85 × 1.20 = 1.02): a booked winner cannot
round-trip into a loss, barring overnight gaps through the stop.

## When to Use

- The user wants a daily momentum buying bot or fixed-dollar DCA into momentum leaders
- The user asks to run/replicate a Finviz momentum-pullback screen keyless
- The user asks to check exits or book profits on bot-managed positions

## Requirements

- **Yahoo Finance (default):** keyless via `yfinance` — screener, regime gate, exit bars
- **Alpaca (required for orders):** `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`; paper
  unless `ALPACA_PAPER=false`. `scan` and `--dry-run` need no Alpaca keys.
- **Finviz Elite (optional):** `FINVIZ_API_KEY` for `--source finviz`, or pass a
  manual screener CSV export with `--csv`

## Workflow

1. Run the screen and review candidates:
   ```bash
   python3 skills/momentum-dca-bot/scripts/momentum_bot.py scan --top 5
   ```
2. Execute (or preview) the daily buy — idempotent, safe to re-run:
   ```bash
   python3 skills/momentum-dca-bot/scripts/momentum_bot.py buy --dry-run
   ```
3. Near the close, book profits and check exits:
   ```bash
   python3 skills/momentum-dca-bot/scripts/momentum_bot.py manage --dry-run
   ```
4. Drop `--dry-run` only after the user has Alpaca paper keys configured.
   Report what was bought/sold/booked and why (reasons are in the output).
5. Inspect state or the audit log when asked:
   ```bash
   python3 skills/momentum-dca-bot/scripts/momentum_bot.py status
   cat state/momentum_bot/trades.jsonl
   ```

For automation, suggest cron (adjust for local timezone vs. US/Eastern):

```cron
35 9  * * 1-5  cd <repo> && set -a && . ./.env && set +a && python3 skills/momentum-dca-bot/scripts/momentum_bot.py buy    >> logs/momentum_bot.log 2>&1
50 15 * * 1-5  cd <repo> && set -a && . ./.env && set +a && python3 skills/momentum-dca-bot/scripts/momentum_bot.py manage >> logs/momentum_bot.log 2>&1
```

## Output Format

- `scan` prints ranked candidates as JSON (ticker, perf_month, perf_week, perf_ytd, price)
  and saves the full list to `reports/momentum_scan_<date>.json` — a dated audit trail
  of every screen run. `buy` reuses the same-day saved scan instead of re-screening.
- `buy`/`manage` print one action line per decision with the triggering rule
- State: `state/momentum_bot/positions.json`; audit log: `state/momentum_bot/trades.jsonl` (gitignored)

## Risk Notes

Always surface these when the user discusses going live: momentum strategies
suffer sharp regime-change drawdowns (the SPY 50DMA gate and exits mitigate but
do not eliminate); daily-close checks cannot stop overnight gaps; ~$75K/year of
short-term trades creates material tax drag and wash-sale complexity; paper
trade for at least one quarter before `ALPACA_PAPER=false`. This skill provides
mechanical execution, not investment advice.

## Resources

- `scripts/momentum_bot.py` — screener, ranking, regime gate, orders, exits, profit booking
- `scripts/tests/test_momentum_bot.py` — offline tests for all decision logic
- Related skills: `position-sizer` (risk-based sizing), `trader-memory-core`
  (thesis journal), `portfolio-manager` (Alpaca MCP), `market-breadth-analyzer`
  (regime cross-check)
