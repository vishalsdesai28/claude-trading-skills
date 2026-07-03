---
layout: default
title: "Dynamic Exit Engine"
grand_parent: English
parent: Skill Guides
nav_order: 19
lang_peer: /ja/skills/dynamic-exit-engine/
permalink: /en/skills/dynamic-exit-engine/
generated: true
---

# Dynamic Exit Engine
{: .no_toc }

Manage an adaptive trailing exit for an open LONG equity position. Use when the user asks how to trail a stop, where to move a stop on a winner, whether to exit or hold a position, how to lock in profits, breakeven stops, ATR trailing stops, give-back / round-trip protection, or wants a daily "manage my open positions" pass. Two-phase trailing-stop FSM (hard stop -> ratcheting profit floor) with breakeven lock, stale-flat timeout, ATR noise band, and consecutive-breach confirmation. Replays deterministically over a price series and persists a JSON tracker snapshot.
{: .fs-6 .fw-300 }

<span class="badge badge-free">No API</span> <span class="badge badge-optional">FMP Optional</span>

[View Source on GitHub](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/dynamic-exit-engine){: .btn .fs-5 .mb-4 .mb-md-0 }

<details open markdown="block">
  <summary>Table of Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## 1. Overview

Manage the exit of a single open long equity position by replaying an adaptive
trailing-stop state machine over a price series and emitting one action:

- **hold** — still in Phase 1; keep the resting stop at the initial hard stop.
- **raise-stop-to-X** — the profit floor has ratcheted up; move the broker stop to X.
- **exit** — a stop fired during the replay; close the position now.

The engine is designed for cash equities traded via Alpaca or Robinhood. It has
**no leverage-aware ROE cap** (that belongs to leveraged crypto perps) — only the
spot-% and ATR-scaled stop paths apply.

Two-phase design:

- **Phase 1 (loss protection):** a hard stop at the tighter of a fixed spot-%
  drawdown or an ATR-scaled width — `min(spot_stop_pct, clamp(ATR% * mult))`.
- **Phase 2 (profit lock):** once unrealized profit clears `protect_pct`, a
  trailing floor at `entry + (peak - entry) * (1 - retrace_tier)` that ratchets
  one-way and never gives locked profit back.

Layered protections:

- **Breakeven ratchet** — once peak profit clears a trigger, the floor may never
  fall below `entry + fees`, so a medium winner cannot round-trip to a loss.
- **Stale-flat timeout** — cut a position that drifts flat (never arms Phase 2)
  for N bars; it is opportunity-cost dead weight in a scarce slot.
- **ATR noise band** — below the first Phase-2 tier, a give-back inside the name's
  normal volatility is held, not exited (avoids churning out of a live trend).
- **Consecutive-breach confirmation** — require N consecutive bars closing below
  the floor before a trailing exit fires (whipsaw suppression).

The FSM is a pure left-fold over the bars from entry to now, so two runs with the
same inputs produce identical state (replay-deterministic, like Parabolic Short
Phase 3). A JSON snapshot is persisted and rehydrated on the next run **only** to
reconcile against the current broker position (detect a re-entry or a close) and
to diff the recommended stop — it is never fed back into the FSM.

---

## 2. When to Use

- User asks where to move a stop on a winning position, or how to trail it.
- User asks "should I hold or exit this position?"
- User wants to lock in profit / avoid giving back an open gain.
- User mentions breakeven stops, ATR trailing stops, or round-trip protection.
- Running a daily "manage my open positions" pass over held equities.

Do **not** use for entry timing, position sizing, or short positions.

---

## 3. Prerequisites

- Python 3.9+ (standard library only for the calculation and fixture paths).
- A price series covering entry → now: an FMP daily OHLCV pull (needs
  `FMP_API_KEY`) or a local fixture JSON. Alpaca/Robinhood bar exports in the
  same OHLCV shape also work as a fixture.
- No API key is required when using `--bars-source fixture`.

---

## 4. Quick Start

```bash
# Fixture (offline) — recommended for review and testing
python3 skills/dynamic-exit-engine/scripts/manage_exit.py \
  --ticker AAPL --entry 150 --qty 100 --atr 3.2 \
  --bars-source fixture --bars-fixture bars.json \
  --atr-stop --breakeven-trigger-pct 4 --stale-flat-bars 15 \
  --state-dir state/dynamic_exit/ --output-dir reports/

# Live daily OHLCV via FMP (requires FMP_API_KEY)
python3 skills/dynamic-exit-engine/scripts/manage_exit.py \
  --ticker AAPL --entry 150 --qty 100 --atr 3.2 \
  --bars-source fmp --entry-date 2026-01-02 \
  --output-dir reports/
```

---

## 5. Workflow

### Step 1: Gather position inputs

Collect from the user (or from a broker snapshot JSON via `--position-json`):

- **Required:** ticker, entry price.
- **Recommended:** current share quantity (`--qty`; 0 means the position closed),
  ATR in price units (`--atr`; enables the ATR-scaled stop and the noise band),
  and entry date (`--entry-date`; filters the series to bars at/after entry).

### Step 2: Choose a policy profile

Read `references/exit_engine.md` for profile guidance. Defaults are a daily-bar
swing profile (8% hard stop, arm at +3%, 30% give-back). Tune with flags:

- Tighter (scalp): lower `--protect-pct`, lower `--retrace`.
- Looser (trend-ride): higher `--protect-pct`, higher `--retrace`.
- Add `--atr-stop` for a volatility-scaled hard stop.
- Add `--breakeven-trigger-pct` to lock breakeven once a peak clears it.
- Add `--stale-flat-bars` to cut drifters.
- Add `--noise-band` (with `--atr`) to suppress sub-tier give-back exits.

### Step 3: Run the engine

```bash
# Fixture (offline) — recommended for review and testing
python3 skills/dynamic-exit-engine/scripts/manage_exit.py \
  --ticker AAPL --entry 150 --qty 100 --atr 3.2 \
  --bars-source fixture --bars-fixture bars.json \
  --atr-stop --breakeven-trigger-pct 4 --stale-flat-bars 15 \
  --state-dir state/dynamic_exit/ --output-dir reports/

# Live daily OHLCV via FMP (requires FMP_API_KEY)
python3 skills/dynamic-exit-engine/scripts/manage_exit.py \
  --ticker AAPL --entry 150 --qty 100 --atr 3.2 \
  --bars-source fmp --entry-date 2026-01-02 \
  --output-dir reports/
```

### Step 4: Interpret and act

Report the action, the recommended stop, and the reason. On `raise_stop`, tell
the user the exact new stop level. On `exit`, state the exit reason (max_loss,
floor_breach, or stale_flat) and the bar it fired. Re-run daily (or per new bar);
the snapshot lets the engine tell whether the stop moved since the last run.

### Step 5: Reconciliation notes

- `--qty 0` → the position is flat; the engine reports `no_position`.
- Entry price differs from the persisted snapshot → the engine flags `reset`
  (a re-entry or averaged-in position) and starts a fresh trail from the new entry.

---

## 6. Resources

**References:**

- `skills/dynamic-exit-engine/references/exit_engine.md`

**Scripts:**

- `skills/dynamic-exit-engine/scripts/manage_exit.py`
