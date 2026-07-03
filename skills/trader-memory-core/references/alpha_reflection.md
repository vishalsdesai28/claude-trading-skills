# Alpha Attribution & Reflection Log

When a thesis reaches a terminal status (CLOSED or INVALIDATED) the postmortem
path computes the trade's **alpha** and writes a terse reflection so future
analysis can learn from the outcome instead of re-deriving it.

## Why alpha, not raw return

A +10% trade in a +25% market was a *bad* directional call; a -3% trade in a
-15% market was a *good* one. Judging the call on the raw return rewards beta
and punishes hedges. Alpha isolates the decision from the regime:

```
alpha (pp) = trade_return_pct − benchmark_return_pct
```

- **trade_return_pct** — prefers the recorded `outcome.pnl_pct` (trim-aware,
  cumulative across partial closes); falls back to the raw entry→exit price
  move when no P&L was recorded.
- **benchmark_return_pct** — first-to-last daily close of the benchmark (SPY by
  default) over the *same* entry→exit dates. US-listed names use SPY; adapt the
  benchmark symbol for other venues (e.g. a Nikkei proxy for `.T` listings).

If no price data is available (no adapter / no key / thin history) alpha is
omitted and the reflection reports the raw return with an explicit caveat.

## The reflection (2-4 sentences, no LLM required)

`compose_reflection()` builds deterministic prose covering, in order:

1. **Directional call** — correct or not, *citing the alpha figure*.
2. **Thesis pillar** — which pillar (first `evidence` item, else the statement)
   held or failed, keyed off the alpha sign and the exit reason.
3. **One concrete lesson** — the recorded `outcome.lessons_learned` if present,
   else a template keyed to the outcome (e.g. size-to-stop after a stop-out).

A caller (e.g. Claude) may supply richer prose via `reflection_text=` /
`--no-reflection`-aware flows; the template is the offline default.

## Reflection log lifecycle

`reflection_log.md` is append-only markdown with an HTML-comment separator.
Each thesis produces exactly one entry with a two-phase lifecycle:

| Phase | Tag | Body |
|-------|-----|------|
| pending | `[<id> \| <ticker> \| <rating> \| pending]` | DECISION only |
| resolved | `[<id> \| <ticker> \| <rating> \| <raw> \| <alpha> \| <days>d]` | DECISION + REFLECTION |

- `rating` is a decision-time label: `confidence` → `origin.screening_grade`
  → `thesis_type`.
- **Atomic**: every write goes through tempfile + `os.replace`.
- **Idempotent**: `store_pending` skips a thesis already logged; `resolve` is a
  no-op once resolved, so re-running the postmortem is byte-stable.

## Past-context injection

`get_past_context(log_path, ticker, n_same, n_cross)` returns the N most-recent
resolved same-ticker entries **in full** (decision + reflection) plus N
cross-ticker **reflection-only** lessons. Pending entries are never surfaced.
The block is compact by design — intended to be pasted into a downstream
analysis prompt (e.g. an adversarial-trade-debate) so the model reasons with
what actually happened last time.
