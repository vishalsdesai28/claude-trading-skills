# Development Ranking

Every input is decomposed into **developments** (a `category`, `headline`,
`priority` 0-100, and an optional `direction` and `ticker`). The note is led by
the single highest-priority development; the Top Call is the highest-priority
development that also carries a long/short direction. Ranking is deterministic
so the same inputs always produce the same note (fully testable offline).

## Priority bands

| Category | Condition | Priority |
|----------|-----------|----------|
| News (market-news-analyst / gdelt) | breaking (blackout) or severity high/critical | 90 |
| News | elevated coverage | 55 |
| News | baseline coverage | 35 |
| Earnings | `|EPS surprise| >= 10%` or guidance raised/lowered | 80 |
| Earnings | `|EPS surprise| >= 3%` | 60 |
| Earnings | reported, small surprise | 45 |
| Earnings | estimate only (not yet reported) | 30 |
| Macro | High impact + actual deviates from estimate | 85 |
| Macro | High impact (scheduled, no actual) | 65 |
| Macro | Medium impact | 40 |
| Macro | Low / other impact | 20 |
| Mover | `|move| >= 7%` | 75 |
| Mover | `|move| >= 4%` | 55 |
| Mover | smaller move | 35 |
| Sector | overbought/oversold present or risk-off/defensive regime | 55 |
| Sector | otherwise | 40 |

Ties are broken by insertion order (news → earnings → macro → movers → sector),
so a breaking-news catalyst outranks an equally scored item added later.

## Direction

- **News is directionless** by design — a coverage surge flags *attention*, not
  price direction — so it can lead the note but never becomes the Top Call.
- **Earnings**: guidance dominates (`lowered` → SHORT, `raised` → LONG);
  otherwise a material EPS surprise sets the sign (`>= +3%` LONG, `<= -3%` SHORT).
- **Movers**: positive move → LONG candidate, negative move → SHORT candidate.
- **Macro / sector**: directionless context, never a Top Call on their own.

## Actionable ideas

Ideas are the directional developments (long/short) that carry a ticker,
de-duplicated by ticker keeping the highest-priority instance, capped at four.
Each idea gets a generic risk line appropriate to its direction (gap-fade risk
for longs, squeeze risk for shorts) that the analyst can refine.

## Design principle

Missing inputs degrade gracefully: absent files are recorded in
`inputs_missing` and simply contribute no developments. An empty note is a valid
"nothing material overnight — maintain positioning" briefing rather than an error.
