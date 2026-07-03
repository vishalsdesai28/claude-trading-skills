# Debate Protocol — Turn-Count Gates and Rules

Two staged debates run in sequence. Each has a fixed turn budget so the process
terminates deterministically and cannot loop. The gates below mirror
TradingAgents' `ConditionalLogic` (`should_continue_debate`,
`should_continue_risk_analysis`), re-expressed as an orchestration protocol.

---

## Stage 1 — Bull vs. Bear (ping-pong)

Two speakers alternate: **Bull → Bear → Bull → Bear …**, then the **Research
Manager** judges.

**Turn budget.** With `max_debate_rounds = R` (default `R = 1`):

- The debate runs until the turn `count` reaches `2 × R` (2 turns per round: one
  Bull, one Bear). Default `R = 1` ⇒ **2 turns** (one Bull, one Bear), then the
  judge. `R = 2` ⇒ 4 turns, etc.
- **Who speaks next:** if the last speaker was the Bull, the Bear goes next;
  otherwise the Bull goes. The Bull opens.
- When `count ≥ 2 × R`, stop and route to the **Research Manager**.

**Turn rules (every turn):**

1. **Rebut first.** Open by directly answering the opponent's *last* point — name
   it, then counter it with specific evidence. A turn that ignores the prior
   argument and only re-lists its own thesis is a wasted turn.
2. **Cite the lane.** Ground each claim in a specific analyst lane from the brief
   (valuation, technicals, sentiment, news). Do not invent a lane the brief
   marks as MISSING.
3. **No new personas.** Only Bull and Bear speak in Stage 1.
4. **Conversational, not a data dump.** Argue; don't recite.

**Judge (Research Manager):** reads the full debate `history` and emits a
`ResearchPlan` (see `schemas.md`) with a **forced 5-tier** `recommendation`.

---

## Stage 2 — Aggressive / Conservative / Neutral (round-robin)

Three speakers rotate: **Aggressive → Conservative → Neutral → Aggressive …**,
then the **Portfolio Manager** judges. Requires a concrete `TraderProposal`
(action/entry/stop/size) as the thing under debate.

**Turn budget.** With `max_risk_discuss_rounds = K` (default `K = 1`):

- The debate runs until the turn `count` reaches `3 × K` (3 turns per round, one
  per persona). Default `K = 1` ⇒ **3 turns** (one each), then the judge.
- **Rotation:** after Aggressive → Conservative; after Conservative → Neutral;
  otherwise → Aggressive. Aggressive opens.
- When `count ≥ 3 × K`, stop and route to the **Portfolio Manager**.

**Persona stances:**

- **Aggressive** — champions the high-reward, high-risk read; argues the caution
  of the other two misses upside; may push to *increase* size.
- **Conservative** — protects capital; surfaces downside, volatility, and the
  worst-case; may push to *reduce* size, widen the stop's implied risk budget, or
  stage entries.
- **Neutral** — weighs both; challenges whichever side overreaches; typically
  lands on a balanced/staged posture.

**Turn rules:** same rebut-first discipline as Stage 1 — each persona must
directly address the *last* points from the other two before adding its own.

**Judge (Portfolio Manager):** reads the risk debate `history`, the
`ResearchPlan`, the `TraderProposal`, and (if supplied) the **Lessons from prior
decisions** block, then emits the final `PortfolioDecision`. It **may adjust
sizing** relative to the proposal (e.g. Buy → Overweight, or trim the share
count) and must justify any change with a specific point from the risk debate.

---

## Anti-fence-sitting rule (applies to both judges)

`Hold` (Stage 1) / `Hold` (Stage 2 final) is reserved for a debate where the
evidence is **genuinely balanced** — comparable-strength arguments on both sides
that a reasonable analyst could not decisively separate. It is **not** a default
for uncertainty or thin data. If one side landed even a modestly stronger,
better-evidenced blow, commit to that side (`Overweight`/`Underweight` for a lean,
`Buy`/`Sell` for strong conviction). State explicitly *why* the debate was
balanced whenever `Hold` is chosen.

---

## Tuning the budgets

- Raise `R` / `K` for higher-stakes or larger positions where more back-and-forth
  is worth the tokens; keep them at 1 for routine screener triage.
- The gates are counts, not wall-clock — the debate always terminates at the
  turn cap regardless of content.
