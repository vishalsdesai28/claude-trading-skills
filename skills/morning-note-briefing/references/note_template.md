# Morning Note — Fixed Template

A morning note is a **two-minute, one-screen** pre-market briefing. It is
opinionated, led by the single most important development, and always ends with
a clear directional call and any actionable ideas. `scripts/build_morning_note.py`
renders exactly this layout from the assembled JSON.

Rules of the format:

- **Lead with the one thing that matters.** The highest-priority development
  (see `development_ranking.md`) is the headline. Never bury it.
- **The Top Call is directional.** It is the highest-conviction long/short idea,
  which may differ from the lead when the lead is a directionless print (a macro
  release, a coverage surge). If nothing is directional, say so.
- **Every idea carries a catalyst and a risk.** An idea without "why now" and
  "what makes this wrong" is noise.
- **"Nothing material overnight" is a valid note.** Say it and maintain
  positioning rather than manufacturing a call.
- **Time-stamp and hedge.** Pre-market moves may change by the open.

## Layout

```markdown
# {YYYY-MM-DD} Morning Note
**Prepared:** {generated_at} | **Coverage:** {coverage} | **Analyst:** {analyst}

## Lead: {single most important development headline}
{one or two lines on why it matters}

## Top Call
**LONG|SHORT {TICKER}** — {thesis in one line}
Catalyst: {the catalyst}.

## Actionable Ideas
- **LONG {TICKER}**: {thesis} — catalyst: {catalyst} | risk: {what makes it wrong}
- **SHORT {TICKER}**: {thesis} — catalyst: {catalyst} | risk: {what makes it wrong}

## Overnight & Pre-Market Developments
- [{TICKER}] {earnings / news / mover one-liner} — {detail}

## Macro Calendar — Today
- {event} ({country}) — Impact {level}. est {x}, prev {y}, actual {z}

## Sector Read
{sector-analyst regime, leaders, overbought/oversold in one line}

---
*Inputs: earnings-calendar, sector-analyst, economic-calendar-fetcher,
market-news-analyst. Estimates only — pre-market moves may change by open.*
```

## Section sourcing

| Section | Upstream skill | JSON consumed |
|---------|----------------|---------------|
| Lead / Top Call | (derived) | highest-priority development across all inputs |
| Actionable Ideas | earnings-calendar, movers, market-news-analyst | directional developments with a ticker |
| Overnight & Pre-Market Developments | earnings-calendar, market-news-analyst, movers | news / earnings / mover developments |
| Macro Calendar — Today | economic-calendar-fetcher | events dated `as_of` |
| Sector Read | sector-analyst | `groups`, `ranking`, `overbought`, `oversold`, `cycle_phase` |
