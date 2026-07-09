# Trend Reclaim Framework

Knowledge base for interpreting Trend Reclaim screener output. The screener
finds stocks regaining directional control after a reset; this document
covers the reclaim lifecycle, grade usage, playbooks, traps, and risk rules.

## The Reclaim Lifecycle

Some of the best multi-week movers don't offer their cleanest entry on the
first breakout or first MA cross — they reset, stabilize, and then reclaim
trend control. Four stages:

1. **Reset / Base** — the stock pulls back, shakes out, or consolidates
   after a prior move. Volatility may spike during the reset, then declines
   as the stock stabilizes. Watchlist material only; define the key reclaim
   level (the SMA50).
2. **Reclaim Attempt** — early signs of regaining directional control:
   structure improving, momentum shifting, first constructive price action.
   Screener phase label: `reclaim_attempt` (fewer than 3 closes above the
   SMA50). Plan the entry, define risk, size smaller.
3. **Reclaimed Trend** — trend footing restored with confirmed structure,
   improving momentum, and supportive participation. Screener phase label:
   `reclaimed_trend` (3+ consecutive closes above). Highest-conviction
   entry zone — enter on pullbacks toward the reclaim level.
4. **Continuation** — the restored trend develops over weeks to months.
   Manage with trailing stops like any confirmed trend.

True reclaims show improving control, cleaner structure, and follow-through
after the reset — not a one-bar bounce that quickly reverses. Reclaim
signals develop over multiple sessions: re-run the screener daily and watch
whether a name's grade is maturing or deteriorating.

## Using the Grades

- **A (composite ≥ 85)** — high-quality trend restoration with strong
  confirmation across all five factors. Highest-conviction second-chance
  entries; the reclaim is sticking.
- **B (≥ 70)** — solid reclaim, one factor slightly less mature (momentum
  improving but not established, or volume supportive but not exceptional).
  Tradeable with standard position sizing on a clean setup.
- **C (≥ 50)** — early or mixed attempt. Structure improving but unproven.
  Watchlist and monitor for continued improvement before committing capital.
- **D (< 50)** — weak or messy. Skip; the screener excludes these from the
  watchlist by default.

## Playbooks

### Reclaimed Continuation (primary)

- **Conditions:** phase `reclaimed_trend`, clean structure, improving
  momentum, supportive volume; the reclaim has held multiple sessions.
- **Entry:** first constructive pullback after the reclaim confirms — hold
  of the restored structure on lighter volume, then resumption with
  improving participation. The screener's assumed entry is the next open;
  a pullback entry toward the `reclaim_level` improves risk/reward.
- **Invalidation:** loss of the reclaimed footing — close back below the
  restoration level (the report's `stop`). A reclaim that gives back its
  gains quickly was never a true restoration.
- **Targets:** T1 = prior swing high / pre-reset resistance (the report's
  `t1`). Scale ~50% at T1, trail the rest with a stop below the reclaim
  level.
- **Avoid when:** the reclaim happened on thin volume
  (`low_participation_reclaim` warning), or price is already extended above
  the reclaim zone (the screener hard-rejects > max-ext, but re-check at
  entry time).

### Reclaim Attempt Entry (secondary)

- **Conditions:** phase `reclaim_attempt`, reset complete, momentum
  shifting, early structural improvement.
- **Entry:** smaller initial position on early reclaim behavior, add on
  confirmation (3rd close above / grade upgrade on the next scan).
- **Invalidation:** the attempt fails and prior weakness resumes — exit at
  the planned smaller loss. Failed attempts often precede continuation of
  the decline.
- **Targets:** T1 = confirmation of the reclaim itself; take profits there
  if it stalls, add if it confirms.
- **Avoid when:** the reset is incomplete (still in active decline), or
  multiple prior attempts failed at the same level (the screener rejects
  ≥ 2 prior crosses in 30 sessions as `repeated_failed_reclaims`).

### Unconfirmed Reclaim / Avoid (defensive)

Do not enter when a bounce lacks improving structure, momentum, and volume.
That is a dead-cat bounce, not a restoration. Reassess only if structure
genuinely improves over subsequent sessions. Feeling compelled to buy
because a stock bounced after a decline is the trap this playbook exists to
block.

## Common Traps

- **False reclaim / dead-cat bounce** — bounce without volume, flat
  momentum, messy structure. Genuine restoration shows follow-through.
- **Chasing an extended reclaim** — the signal fired days ago and price has
  run. The cleanest entry is the first pullback after confirmation, not the
  extended move. (Hard-rejected above `--max-ext-pct`.)
- **Ignoring volume** — a reclaim without participation is a drift.
  Above-average volume during the reclaim is what separates institutional
  restoration from noise. The doc's volume diagram sets the bands: ~3× RVOL
  is a strong tradeable break, below 0.8× is fade risk — the screener caps
  fade-risk names at grade C (`grade_capped_fade_risk`) and warns with
  `fade_risk_volume` / `low_participation_reclaim`.
- **Fighting the major trend** — local reclaims against a hostile market or
  sector have materially lower success rates. The trend-alignment factor
  penalizes names below their SMA200, and the report's market-regime line
  (SPY vs. its SMA50/SMA200) covers the tape itself: in `risk_off` treat
  every reclaim as guilty until proven innocent. Cross-check
  market-breadth-analyzer / exposure-coach for depth.

## Risk Management

1. Stop below the reclaim restoration level — if the regained footing is
   lost, the thesis is invalidated. Accept the loss and move on.
2. Size for the stop distance: the report's `risk_pct` feeds position-sizer
   directly. Wider stops mean fewer shares for the same dollar risk.
3. Don't chase extended reclaims; wait for the pullback to the reclaim zone.
4. Take partial profits at the first resistance (T1); trail the remainder
   below the reclaim level. T2 (measured move — base depth projected from
   the reclaim level) is the stretch target for the trailed portion.
5. The screener auto-rejects candidates with Yahoo-published earnings inside
   `--exclude-earnings-within-days` (default 7) and prints each candidate's
   next earnings date. Non-earnings binary events (FDA decisions, M&A,
   analyst days) are NOT detected — verify those manually
   (earnings-calendar / market-news-analyst skills).

## Screener Method Notes

- Timing: after-hours (4:00–8:00 PM ET) on the completed session's data;
  entries are assumed at the next open. Reference win metric for review:
  open-to-high on entry day, win = +5% from open intraday.
- Reset requirement (≥ 5 of the prior 30 sessions below the SMA50) is what
  distinguishes a reclaim from ordinary chop along a rising average.
- Selection: top 3 by composite each session; the full watchlist (grade ≥ C)
  is retained for maturation tracking.
