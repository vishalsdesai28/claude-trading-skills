# Structured Output Schemas

The two judges (Research Manager and Portfolio Manager) and the Trader emit
**structured output**. When the runtime supports typed/JSON output, produce the
JSON object directly. When it does not, fall back to the **deterministic-header
markdown** shown for each schema — `scripts/debate_kit.py parse-decision`
recovers the same fields from those headers, so downstream wiring never breaks.

The field descriptions below are the model's output instructions. Fill every
required field; leave an optional numeric field out (or write `null`) rather
than inventing a number.

---

## `ResearchPlan` — Research Manager (Stage 1 judge)

Turns the bull-vs-bear debate into a directional call and concrete instructions
for the trader.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `recommendation` | enum: `Buy` / `Overweight` / `Hold` / `Underweight` / `Sell` | yes | The forced 5-tier rating. Reserve `Hold` for a genuinely balanced debate; otherwise commit to the stronger side. |
| `rationale` | string | yes | Conversational summary of both sides, ending with which arguments carried the decision. |
| `strategic_actions` | string | yes | Concrete steps to implement the recommendation, including sizing guidance consistent with the rating. |

JSON:

```json
{
  "recommendation": "Overweight",
  "rationale": "The bull's margin-expansion and 30% valuation-gap case outweighed the bear's terminal-value-dominance concern, though the retail over-extension flag tempers conviction.",
  "strategic_actions": "Scale in over two tranches on a pullback toward the 30-week SMA; cap total open risk at the 1% rule; revisit if a weekly close breaks 92."
}
```

Deterministic-header fallback (parseable):

```
**Recommendation**: Overweight

**Rationale**: The bull's margin-expansion and 30% valuation-gap case ...

**Strategic Actions**: Scale in over two tranches ...
```

---

## `TraderProposal` — concrete trade proposal (bridges Stage 1 → Stage 2)

The trader reads the `ResearchPlan` and turns it into an executable proposal.
This is the object the risk debate (Stage 2) argues over.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `action` | enum: `Buy` / `Hold` / `Sell` | yes | Transaction direction. Sizing nuance (Overweight/Underweight) is decided later by the Portfolio Manager. |
| `reasoning` | string | yes | 2–4 sentences anchoring the action in the analyst reports and the research plan. |
| `entry_price` | number \| null | no | Entry target in the quote currency. |
| `stop_loss` | number \| null | no | Stop-loss price. For a long, must be below `entry_price`. |
| `position_sizing` | string \| null | no | Sizing guidance, e.g. `"~5% of portfolio"`. |

Deterministic-header fallback:

```
**Action**: Buy

**Reasoning**: ...

**Entry Price**: 101.50
**Stop Loss**: 92.00
**Position Sizing**: ~5% of portfolio
```

The trailing `FINAL TRANSACTION PROPOSAL: **BUY**` line from TradingAgents is
optional here; the parser keys off the `**Action**` header.

---

## `PortfolioDecision` — Portfolio Manager (Stage 2 judge, final)

Synthesizes the aggressive/conservative/neutral risk debate into the final,
possibly-resized decision.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `rating` | enum: `Buy` / `Overweight` / `Hold` / `Underweight` / `Sell` | yes | The final position rating. May differ from the `ResearchPlan.recommendation` if the risk debate justified a change (e.g. Buy → Overweight to respect risk). |
| `executive_summary` | string | yes | Concise action plan: entry strategy, position sizing, key risk levels, time horizon. 2–4 sentences. |
| `investment_thesis` | string | yes | Detailed reasoning anchored in the risk debate. Incorporate prior lessons if they were supplied in context. |
| `price_target` | number \| null | no | Target price in the quote currency. |
| `time_horizon` | string \| null | no | Holding period, e.g. `"3-6 months"`. |

JSON:

```json
{
  "rating": "Overweight",
  "executive_summary": "Enter on a pullback to 101-102, stop at the weekly swing low near 92, target the 130 blended fair value over 2-3 quarters; size at ~5% given the retail over-extension flag.",
  "investment_thesis": "The conservative analyst's over-extension concern was valid but did not override the base-case upside; the neutral view's staged entry resolved it. Sizing is capped one notch below full Buy.",
  "price_target": 130,
  "time_horizon": "6-12 months"
}
```

Deterministic-header fallback:

```
**Rating**: Overweight

**Executive Summary**: Enter on a pullback to 101-102 ...

**Investment Thesis**: The conservative analyst's over-extension concern ...

**Price Target**: 130
**Time Horizon**: 6-12 months
```

---

## Parsing rules (what `parse-decision` enforces)

- `**Recommendation**` and `**Rating**` both map to the single `rating` field;
  the **first** header seen wins, so print the Research Manager block before the
  Portfolio Manager block if both appear in one document.
- `rating` is validated against the forced 5-tier scale; `action` against the
  3-tier scale. An unrecognized value (e.g. `Strong Buy`, `Accumulate`) is kept
  as `rating_raw` / `action_raw` and a warning is emitted — never silently
  coerced.
- `entry_price`, `stop_loss`, `price_target` are coerced to float; currency
  symbols, commas, percent signs, and nullish placeholders (`N/A`, `TBD`, `—`)
  are handled.
- A long entry whose `stop_loss >= entry_price` is flagged as contradictory.
- A long-side decision (`rating` in {Buy, Overweight} or `action` = Buy) with a
  valid entry + stop produces a ready-to-run `position-sizer` command; anything
  else returns `eligible: false` with a reason.
