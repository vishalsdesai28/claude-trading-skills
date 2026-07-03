---
name: dynamic-exit-engine
description: Manage an adaptive trailing exit for an open LONG equity position. Use when the user asks how to trail a stop, where to move a stop on a winner, whether to exit or hold a position, how to lock in profits, breakeven stops, ATR trailing stops, give-back / round-trip protection, or wants a daily "manage my open positions" pass. Two-phase trailing-stop FSM (hard stop -> ratcheting profit floor) with breakeven lock, stale-flat timeout, ATR noise band, and consecutive-breach confirmation. Replays deterministically over a price series and persists a JSON tracker snapshot.
---

# Dynamic Exit Engine

## Overview

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

## When to Use

- User asks where to move a stop on a winning position, or how to trail it.
- User asks "should I hold or exit this position?"
- User wants to lock in profit / avoid giving back an open gain.
- User mentions breakeven stops, ATR trailing stops, or round-trip protection.
- Running a daily "manage my open positions" pass over held equities.

Do **not** use for entry timing, position sizing, or short positions.

## Prerequisites

- Python 3.9+ (standard library only for the calculation and fixture paths).
- A price series covering entry → now: an FMP daily OHLCV pull (needs
  `FMP_API_KEY`) or a local fixture JSON. Alpaca/Robinhood bar exports in the
  same OHLCV shape also work as a fixture.
- No API key is required when using `--bars-source fixture`.

## Workflow

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

## Output Format

### JSON snapshot / report

```json
{
  "schema_version": "1.0",
  "skill": "dynamic-exit-engine",
  "ticker": "AAPL",
  "side": "long",
  "entry_price": 150.0,
  "qty": 100,
  "atr": 3.2,
  "reconcile": {"decision": "match", "note": "tracking the same entry"},
  "state": {
    "status": "holding",
    "phase": "phase2",
    "effective_stop_pct": 6.4,
    "stop_source": "atr",
    "hard_stop_px": 140.4,
    "peak_px": 172.0,
    "peak_profit_pct": 14.67,
    "floor_px": 163.2,
    "last_mark": 168.0,
    "unrealized_pct": 12.0,
    "consecutive_breaches": 0,
    "bars_processed": 9,
    "exit_reason": null
  },
  "action": {
    "action": "raise_stop",
    "recommended_stop": 163.2,
    "reason": "phase2 trailing floor ratcheted to 163.2000"
  },
  "stop_raised_since_last_run": true
}
```

The `floor_trace` array (per-bar floor levels) is included for auditing. The
snapshot persists to `--state-dir` keyed by `ticker_side`; a dated JSON + Markdown
report is written to `--output-dir` (default `reports/`).

## Resources

- `references/exit_engine.md`: FSM mechanics, all policy knobs, scalp-vs-trend
  tuning profiles, worked ratchet/breakeven/noise-band examples, and the
  replay-determinism / reconciliation contract.
- `scripts/manage_exit.py`: the FSM, stop math, snapshot persistence, and CLI.

## Key Principles

1. **The floor only moves up.** Once profit is locked, the trail never loosens.
2. **The hard stop is never suppressed.** Noise-band and consecutive-breach logic
   govern only the trailing give-back, not the Phase-1 max-loss exit.
3. **Replay-deterministic.** Provide bars from entry forward; the same inputs
   always produce the same action. The snapshot is for reconciliation, not state.
4. **Breakeven beats round-trips.** A medium winner should not become a loss —
   arm the breakeven lock on positions that peak between flat and `protect_pct`.
5. **Flat is a cost.** A position that never arms Phase 2 ties up a slot; the
   stale-flat timeout recycles it.
