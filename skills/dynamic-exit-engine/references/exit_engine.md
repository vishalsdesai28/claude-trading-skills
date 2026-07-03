# Dynamic Exit Engine — mechanics, knobs, and tuning

Adaptive trailing-stop state machine for a single open **long equity** position.
It replays a two-phase FSM over a price series from entry to now and emits one
action: `hold`, `raise_stop` (to a specific level), or `exit`. The design targets
the classic failure mode of a plain stop order — **"we had the gain and gave it
all back"** — while avoiding the opposite failure of being shaken out of a live
trend by ordinary volatility.

This is an equities adaptation of a leveraged-perp exit engine. The **leverage-
aware ROE cap is intentionally removed**: cash equities have no margin-multiplied
loss, so only the spot-% and ATR-scaled stop paths remain.

## The two phases

Each bar's **close is treated as the mark price** (the bar-granularity analogue of
a live tick). Peak tracking, floor computation, and breach detection all use the
close. Provide bars from entry forward; the engine folds the whole series each run.

### Phase 1 — loss protection

Active until unrealized profit first clears `protect_pct`. The floor is a **hard
stop**:

```
effective_stop_pct = min(spot_stop_pct, clamp(ATR% * atr_stop_mult))   # if ATR scaling on
hard_stop_px       = entry * (1 - effective_stop_pct / 100)
```

- `spot_stop_pct` is a fixed drawdown (default 8%).
- With `--atr-stop`, the stop width becomes the coin's ATR as a percent of entry,
  times `atr_stop_mult`, **clamped** to `[atr_stop_floor_pct, atr_stop_ceiling_pct]`
  so an ATR spike at entry cannot set an unbounded stop and a quiet name cannot get
  a noise-tight one.
- The `min()` takes the **tighter** of the two (the smaller percent = higher stop
  price for a long). The output records which one bound (`stop_source`).
- A hard-stop breach (`loss_pct >= effective_stop_pct`) fires an **immediate**
  `max_loss` exit. It is never noise-suppressed and never needs consecutive
  confirmation — it is the fast invalidation stop.

### Phase 2 — profit lock

Arms the first bar unrealized profit reaches `protect_pct`. The floor trails the
peak:

```
retrace = active tier's give-back for the current PEAK profit
floor   = entry + (peak - entry) * (1 - retrace)
```

- The floor is computed from the **peak** run-up, not the current price, so a dip
  after a high does not loosen it.
- `phase2_tiers` is the give-back ladder: tighter give-back as the winner grows,
  so a big move banks more of its gain. Default ladder:

  | peak profit | give-back | keeps |
  |-------------|-----------|-------|
  | >= 5%       | 30%       | 70%   |
  | >= 10%      | 40%       | 60%   |
  | >= 20%      | 50%       | 50%   |
  | >= 50%      | 60%       | 40%   |

  Below the first tier, `retrace_threshold` (default 30%) applies.
- **One-way ratchet:** the floor is clamped to never fall below the previous bar's
  floor. For a long it only ever rises.

## Layered protections

### Breakeven ratchet (`breakeven_trigger_pct`, `breakeven_lock_pct`)

Once **peak** profit clears `breakeven_trigger_pct`, the floor may never fall below
`entry * (1 + breakeven_lock_pct / 100)` — a guaranteed small gain that covers
fees. Disabled when the trigger is 0.

This plugs the specific leak where a **medium winner peaks between flat and
`protect_pct`** and then round-trips to a loss. Because Phase 2 only arms when
*current* profit reaches `protect_pct`, a peak of +2–4% under a 5% protect never
engages the trailing floor — without the breakeven lock the position rides the
hard stop all the way back down. Set `breakeven_trigger_pct` below `protect_pct`
to catch these.

### Stale-flat timeout (`stale_flat_bars`)

Cut a position that has **never armed Phase 2** (peak profit < `protect_pct`) after
`stale_flat_bars` bars. Such a position is a drifter tying up a scarce slot at
roughly zero expectancy. Positions that ever reached `protect_pct` are permanently
exempt (peak profit is monotonic non-decreasing). Disabled when 0. The reason is
`stale_flat` and the phase is `timeout`.

### ATR noise band (`noise_band_enabled`, `noise_band_atr_mult`)

**Below the first Phase-2 tier only**, if a floor breach would fire but the
pull-back from peak is inside the name's volatility band
(`noise_band_atr_mult * ATR%`), the engine **holds** instead of exiting and resets
the consecutive-breach counter. This stops churning out of a barely-green position
at, say, +0.8% when the 30% give-back applied to a small run-up sits just under the
mark. The hard max-loss stop (checked earlier) is **not** suppressed, and pull-backs
from a peak already past the first tier are real give-backs and remain eligible to
exit. Requires an ATR at entry; degrades to no suppression without one.

### Consecutive-breach confirmation (`consecutive_breaches_required`)

A trailing exit fires only after N consecutive bars close below the floor. A single
breach that the next bar reclaims resets the counter — whipsaw suppression. The
hard max-loss stop ignores this and fires on the first breach.

## Order of checks per bar

1. Update peak.
2. **Hard max-loss** (`loss_pct >= effective_stop_pct`) → immediate `max_loss` exit.
3. **Stale-flat** (elapsed bars >= N and peak never reached protect) → `stale_flat` exit.
4. Compute floor (Phase 2 if current profit >= protect, else Phase 1 hard stop).
5. **Breakeven ratchet** clamp (if armed).
6. **One-way ratchet** clamp against the previous floor.
7. Breach = close < floor.
8. **Noise-band** suppression (sub-first-tier only).
9. **Consecutive-breach** count → `floor_breach` exit when the threshold is met.

Once any exit fires the FSM is terminal (later bars are ignored in the fold).

## Actions

- `exit` — the FSM fired during the replay. The report carries the exit reason and
  the bar timestamp it fired on.
- `raise_stop` — still holding and the floor has ratcheted above the initial hard
  stop. Move the resting broker stop to `recommended_stop` (= `floor_px`).
- `hold` — still holding in Phase 1 at the initial hard stop.

`recommended_stop` is always the current floor, so the trader always knows where
the protective stop belongs.

## Tuning profiles

Choose by how much give-back you tolerate vs. how much you want to ride:

| Profile     | protect_pct | retrace | stale_flat_bars | Notes |
|-------------|-------------|---------|-----------------|-------|
| Scalp       | 1.5         | 0.20    | 8               | Banks fast; best in chop. Can amputate fat-tail winners. |
| Swing (def) | 3.0         | 0.30    | 0 (off)         | Daily-bar default. Balanced. |
| Trend-ride  | 5.0         | 0.55    | 0 (off)         | Rides rippers; bleeds more in chop. |

General guidance:
- **Tight beats loose in chop** — a loose trail lets winners give it all back. Only
  widen to trend-ride once a sustained-trend sample justifies it.
- Turn on `--atr-stop` when the universe spans very different volatilities; a fixed
  `spot_stop_pct` is noise-tight on volatile names and slack on quiet ones.
- Pair a tight profile with a broker-side take-profit scale-out (outside this skill)
  to keep the right tail while banking the core.

## Worked examples

**Ratchet (default policy), entry 100.** Series `100 → 105 → 110 → 120 → 130`,
then dips `125 → 122 → 128 → 124`. Floors: `92, 103.5, 106, 110, 115, 115, 115,
115, 115`. Peak 130 sets the 20%-tier floor at `100 + 30*0.50 = 115`; every later
dip stays above 115, so the position holds with `raise_stop` to 115. The floor
never drops.

**Breakeven lock, entry 100, `protect_pct=5`, `breakeven_trigger_pct=2`,
`breakeven_lock_pct=0.5`.** Series `100 → 101 → 103 → 101.5 → 100`. Phase 2 never
arms (current profit never hits 5%), but peak +3% arms the breakeven lock at 100.5.
The drop to 100 breaches 100.5 → `exit` at a locked +0.5%. Without the lock the
floor stays at the 92 hard stop and the round-trip simply holds.

**Noise-band hold, entry 100, ATR 3 (3%).** Series `100 → 104 → 102`. Peak +4% is
below the 5% first tier; the default floor is `100 + 4*0.70 = 102.8`, so mark 102
would breach and exit. The 2% give-back is inside the 3% band while sub-first-tier,
so the engine **holds**. Disable the band and it exits with `floor_breach`.

**Stale-flat, entry 100, `protect_pct=3`, `stale_flat_bars=5`.** Series drifts
`100, 100.5, 99.8, 100.2, 100.1` (peak only +0.5%). On the 5th bar the position is
cut with `stale_flat`. With the timeout off it just holds.

## Replay determinism & reconciliation contract

- The FSM (`replay`) is a **pure function** of `(entry, bars, policy, atr)` — no
  wall clock, no prior snapshot. Two runs with the same inputs produce byte-
  identical `state` and `action`.
- The on-disk snapshot (in `--state-dir`, keyed `dynamic_exit_<ticker>_<side>.json`)
  is rehydrated **only** to:
  - **reconcile** against the current broker position — `no_position` when qty is 0,
    `reset` when the entry price changed (re-entry / averaged in), `match` otherwise;
  - **diff** the recommended stop (`stop_raised_since_last_run`) for notification.
  The snapshot never feeds the FSM transitions, so persisted state cannot change the
  computed action.
- Because the fold starts at entry, supply bars covering entry → now. When bars
  carry timestamps, `--entry-date` filters out anything before entry so the trail
  anchors correctly.
