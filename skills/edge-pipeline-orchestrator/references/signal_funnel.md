# Signal Funnel — Tiered Cost Control

`scripts/signal_funnel.py` sits in front of the expensive stages of the pipeline
(LLM verdict, multi-agent debate) and answers one question per candidate: *is this
worth a paid model call?* It does so with three cheap-to-expensive tiers, so the
LLM only ever sees candidates that have already cleared a statistical bar.

## The three tiers

| Tier | Cost | What runs | Output |
|------|------|-----------|--------|
| 0 — triggers | free (pure math) | full trigger library over OHLCV, weighted composite | `composite_score`, per-trigger hits |
| 1 — TA filter | free (pure math) | multi-timeframe EMA/RSI/ATR/ADX/volume filter | `CONFIRMED` / `WEAK` / `REJECTED` |
| 2 — gate | free | escalation decision | `escalate: true/false`, `tier`, `bypass_lane` |

A candidate lands in exactly one final tier:

- **dropped** — did not clear the composite gate and hit no bypass lane. No TA, no LLM.
- **surfaced** — cleared Tier 0 but TA was `WEAK`/`REJECTED`. TA ran; the LLM does not.
- **escalated** — TA said `CONFIRMED`, or a named burst/whale bypass lane fired. This
  is the only tier that spends an LLM/debate call.

## Tier 0: trigger library and composite

Thirteen pure detectors run over a single candle series (the highest-priority
timeframe present, default daily). Each returns `{name, score (0-10), reason, fired}`:

- `return_zscore`, `volume_zscore` — current-bar return / volume z-score vs the trailing window
- `range_breakout` — close beyond the prior N-bar range high/low
- `bollinger_squeeze` — bandwidth percentile in the tightest decile
- `adx_trend` — ADX(14) trend strength
- `momentum_burst` — explosive raw % move (also a named bypass lane)
- `sustained_trend` — steady directional move (zero-weight surfacing lane)
- `volume_buildup` — notional-volume surge vs a longer baseline (accumulation)
- `ema_cross` — recent EMA8/21 cross
- `higher_lows` — higher-low structure count
- `momentum_continuation` — extended uptrend now consolidating (EMA-stacked, orderly pullback)
- `bullish_reversal_candle`, `bearish_reversal_candle` — hammer/shooting-star & engulfing (zero-weight surfacing lanes)

**Composite normalization.** The composite is a weighted sum of the *fired* triggers,
divided by the sum of **all** trigger weights (not just the fired ones), times ten and
clamped to 0-100. Two consequences:

- **Co-firing beats a lone max.** A single max-score trigger cannot reach 100 on its
  own; two triggers firing together score proportionally higher.
- **Zero-weight surfacing lanes never pollute the denominator.** `sustained_trend` and
  the reversal-candle detectors carry weight 0, so they contribute nothing to the score
  but can still *surface* a candidate past the composite gate via the bypass path — this
  is what lets a steady downtrend reach research even though every weighted trigger is
  bullish-structured.

A candidate surfaces past Tier 0 when `composite >= min_composite` **or** any bypass
lane fires (burst, whale, or a surfacing lane).

## Tier 1: multi-timeframe TA filter

`ta_filter` scores a candidate additively across 1h/4h/1d:

- Higher-timeframe trend direction — **bullish = +20, bearish = +10, flat/conflicting = 0**
  (directional weighting: our measured edge is long/trend-aligned, so a clean short is
  tradeable but lower-edge and must not be rubber-stamped as equal to a clean long).
- RSI in (30, 70): +15 · ATR% ≥ 0.5: +15 · ADX ≥ 25: +15 · recent EMA cross: +10 ·
  volume confirmed: +10 · plus a small composite bonus (≤ 15).

Verdict mapping: `score ≥ confirmed_threshold` → **CONFIRMED**, `≥ weak_threshold` →
**WEAK**, else **REJECTED**. Insufficient candle history is **REJECTED** outright.

## Tier 2: the escalation gate

Escalate to the paid LLM verdict **only** when:

1. TA verdict is `CONFIRMED`, **or**
2. the **burst** bypass lane fired (`momentum_burst`) — a large, fast move must never be
   filtered out, **or**
3. the **whale** bypass lane fired (`whale_signal` present on the candidate) —
   flat-price accumulation scores ~0 on momentum triggers, so it needs its own door.

Surfacing lanes (`sustained_trend`, reversal candles) get a candidate *past the composite
gate* for TA scoring, but they do **not** by themselves escalate — the candidate still has
to earn a `CONFIRMED`.

## Candidate input shape

```json
[
  {
    "candidate_id": "AAPL-2026-07-01",
    "symbol": "AAPL",
    "direction": "long",
    "ohlcv": {
      "1h": [{"o": 1, "h": 2, "l": 0.9, "c": 1.8, "v": 1000}, "..."],
      "4h": ["..."],
      "1d": ["..."]
    },
    "whale_signal": {"type": "oi_surge_accumulation"}
  }
]
```

`ohlcv` may be a `{timeframe: [candles]}` map or a bare candle list (treated as `1d`).
Candle fields accept short (`o/h/l/c/v`) or long (`open/high/...`) keys. `whale_signal`
is optional and only used to open the whale bypass lane.

## Output

`funnel_candidates()` returns a report with a `summary` (counts per tier plus
`llm_call_reduction_pct`), an `escalated_candidate_ids` list, and a per-candidate
`results` array sorted escalated-first. The CLI writes `signal_funnel_<stamp>.json`
and `.md` to `--output-dir` (default `reports/`).

## How the orchestrator wires it

The funnel is the pre-LLM cost-control stage: run Tier 0 over **all** detected
candidates, feed survivors into the TA filter, and hand **only** the escalated set to
the LLM verdict / debate stage. Everything else is either dropped for free or parked at
the surfaced tier for observation. Because the module has no network dependencies and
takes candles as input, it can be invoked on any candidate JSON that carries per-timeframe
OHLCV — including the tickets produced by `edge-candidate-agent`.
