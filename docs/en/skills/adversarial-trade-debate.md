---
layout: default
title: "Adversarial Trade Debate"
grand_parent: English
parent: Skill Guides
nav_order: 11
lang_peer: /ja/skills/adversarial-trade-debate/
permalink: /en/skills/adversarial-trade-debate/
generated: true
---

# Adversarial Trade Debate
{: .no_toc }

Turn a screener candidate into a decisive conviction rating and a sized, risk-vetted trade decision through two staged adversarial debates. Use when the user has a candidate ticker (with some mix of valuation, technical, sentiment, and news inputs) and wants a forced Buy/Overweight/Hold/Underweight/Sell call plus a final action/entry/stop/size decision — i.e. "should I take this trade", "debate this idea", "bull vs bear on X", "stress-test this setup", "give me a conviction rating", "red-team this trade". Runs a bull-vs-bear debate judged into a 5-tier rating (anti-fence-sitting), then an aggressive/conservative/neutral risk debate judged by a Portfolio Manager into a final, possibly-resized decision. LLM-orchestrated; consumes intrinsic-value-dcf, technical-analyst, and retail-sentiment-ingestor outputs and hands off to position-sizer. No paid data of its own.
{: .fs-6 .fw-300 }

<span class="badge badge-free">No API</span>

[View Source on GitHub](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/adversarial-trade-debate){: .btn .fs-5 .mb-4 .mb-md-0 }

<details open markdown="block">
  <summary>Table of Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## 1. Overview

Act as an adversarial gate between a screener candidate and a committed trade.
Two staged debates convert assembled analyst inputs into a decision:

1. **Stage 1 — Bull vs. Bear.** A two-sided ping-pong over the analyst inputs
   (valuation, technicals, sentiment, news). A **Research Manager** judges the
   debate into a **forced 5-tier rating**: Buy / Overweight / Hold / Underweight
   / Sell.
2. **Stage 2 — Risk round-robin.** Given a concrete proposal (action, entry,
   stop, size), an **Aggressive / Conservative / Neutral** trio argues the risk,
   and a **Portfolio Manager** judges into the **final decision** — which may
   adjust sizing and may splice in **lessons from prior decisions**.

The adversarial structure exists to defeat one-sided confirmation: every turn
must rebut the opponent's last point, and both judges are forbidden from
fence-sitting on `Hold` unless the debate is genuinely balanced. Output is
research/educational, not financial advice.

---

## 2. When to Use

- The user has a candidate ticker and wants a decisive conviction rating rather
  than a hedged summary.
- The user asks to "debate", "red-team", "stress-test", or run "bull vs bear" on
  a trade idea.
- A screener (VCP, CANSLIM, kanchi, earnings, PEAD, edge pipeline) surfaced a
  name and the next step is a go/no-go with sizing.
- NOT for gathering the raw inputs themselves — run the analyst skills first
  (see below). NOT a price prediction engine; it weighs evidence that already
  exists.

---

## 3. Prerequisites

- No API keys and no paid data of its own — this is an LLM-orchestration skill.
- Analyst inputs come from sibling skills' **outputs** (their own prerequisites
  apply): `intrinsic-value-dcf` JSON, a `technical-analyst` report,
  `retail-sentiment-ingestor` run report. Any subset works; missing lanes are
  flagged so the debate cannot fabricate them.
- Python 3.9+ standard library only for `scripts/debate_kit.py` (no third-party
  deps, no network).

---

## 4. Quick Start

```bash
python3 skills/adversarial-trade-debate/scripts/debate_kit.py assemble \
  --ticker ACME \
  --dcf reports/intrinsic_value_ACME_2026-07-01_101500.json \
  --technical reports/ACME_technical_analysis_2026-07-01.md \
  --sentiment reports/retail_sentiment_1751371200.json \
  --news reports/acme_news.txt \
  --output-dir reports/
```

---

## 5. Workflow

### Step 1 — Assemble the analyst inputs

Gather whatever analyst lanes exist for the ticker and fold them into one brief.
Consume the **documented output shapes** of the upstream skills — do not re-run
their analysis here:

- **Valuation** ← `intrinsic-value-dcf` JSON (`snapshot.current_price`,
  `blended.fair_value` / `upside_pct`, `sector_routing`, `guardrail_warnings`).
- **Technicals** ← a `technical-analyst` report (its markdown, or a JSON summary).
- **Sentiment** ← `retail-sentiment-ingestor` run report (`results[]` row:
  band, score, direction, confidence, divergence, contrarian over-extension).
- **News/catalysts** ← optional text (e.g. from `market-news-analyst`).

```bash
python3 skills/adversarial-trade-debate/scripts/debate_kit.py assemble \
  --ticker ACME \
  --dcf reports/intrinsic_value_ACME_2026-07-01_101500.json \
  --technical reports/ACME_technical_analysis_2026-07-01.md \
  --sentiment reports/retail_sentiment_1751371200.json \
  --news reports/acme_news.txt \
  --output-dir reports/
```

The brief's markdown lists each lane and explicitly names the **MISSING** lanes.
Read it before debating: the debate may only reason from lanes that are present.

### Step 2 — Run Stage 1 (Bull vs. Bear)

Follow `references/debate_protocol.md` for the turn-count gates. Alternate
**Bull → Bear** until the turn cap (`2 × max_debate_rounds`, default 2 turns),
then judge. Each turn must:

- open by **directly rebutting** the opponent's last point with specific
  evidence from a named lane, then
- advance its own strongest lane-grounded argument.

The Bull emphasizes upside vs. fair value, trend/structure, and constructive
catalysts; the Bear emphasizes valuation guardrails, downside technicals,
crowd over-extension, and adverse catalysts.

### Step 3 — Judge Stage 1 into a forced 5-tier rating

Act as the **Research Manager**. Read the whole debate and emit a `ResearchPlan`
(schema in `references/schemas.md`) with exactly one `recommendation`:

> **Buy / Overweight / Hold / Underweight / Sell.**

**Anti-fence-sitting rule:** reserve `Hold` *only* when the evidence on both
sides is genuinely balanced. If either side landed a modestly stronger,
better-evidenced case, commit — `Overweight`/`Underweight` for a lean,
`Buy`/`Sell` for strong conviction. When choosing `Hold`, state explicitly why
the debate was balanced.

Produce the structured JSON when the runtime supports it; otherwise print the
deterministic-header markdown from `schemas.md`.

### Step 4 — Form the concrete proposal

Translate the `ResearchPlan` into a `TraderProposal`: `action` (Buy/Hold/Sell),
`reasoning`, and — for an actionable long — `entry_price`, `stop_loss`, and
`position_sizing`. This is the object Stage 2 argues over. If the rating is
`Sell`/`Underweight` with no position to open, Stage 2 debates the exit/avoid
instead.

### Step 5 — (Optional) splice in prior lessons

If `trader-memory-core` postmortems exist for this ticker or setup, pass their
lessons text so the Portfolio Manager can weigh what went wrong/right before:

```bash
python3 skills/adversarial-trade-debate/scripts/debate_kit.py assemble \
  --ticker ACME --dcf ... --technical ... --sentiment ... \
  --proposal reports/acme_proposal.json \
  --lessons reports/acme_prior_lessons.txt \
  --output-dir reports/
```

Only splice lessons that genuinely exist — never invent a prior-trade history.

### Step 6 — Run Stage 2 (risk round-robin)

Rotate **Aggressive → Conservative → Neutral** until the turn cap
(`3 × max_risk_discuss_rounds`, default 3 turns), then judge. Aggressive pushes
for more size/upside; Conservative protects capital and may push to trim or
stage; Neutral balances and challenges whichever side overreaches. Same
rebut-first discipline as Stage 1.

### Step 7 — Judge Stage 2 into the final decision

Act as the **Portfolio Manager**. Emit a `PortfolioDecision` with a final
`rating`, an `executive_summary` (entry, sizing, key risk levels, horizon), and
an `investment_thesis` anchored in the risk debate. The Portfolio Manager **may
adjust sizing** relative to the proposal (e.g. Buy → Overweight, or a smaller
share count) and must justify any change with a specific risk-debate point. The
same anti-fence-sitting rule applies to the final `rating`.

### Step 8 — Hand off to position-sizer

Recover the structured decision from the judge's output and build the
`position-sizer` hand-off:

```bash
python3 skills/adversarial-trade-debate/scripts/debate_kit.py parse-decision \
  --input reports/portfolio_manager_decision.md \
  --account-size 100000 --risk-pct 1.0 \
  --output-dir reports/
```

For a long-side decision with a valid entry + stop, this emits a ready-to-run
`position-sizer` command. Then run `position-sizer` to convert the entry/stop
into a risk-based share count:

```bash
python3 skills/position-sizer/scripts/position_sizer.py \
  --account-size 100000 --entry 101.50 --stop 92.00 --risk-pct 1.0 \
  --output-dir reports/
```

---

## 6. Resources

**References:**

- `skills/adversarial-trade-debate/references/debate_protocol.md`
- `skills/adversarial-trade-debate/references/schemas.md`

**Scripts:**

- `skills/adversarial-trade-debate/scripts/debate_kit.py`
