---
name: retail-sentiment-ingestor
description: Ingest real-time retail sentiment for a list of tickers from StockTwits cashtag streams and Reddit (r/wallstreetbets, r/stocks, r/investing) with no API key, plus an optional X path, and emit one scored signal note per ticker into the shared social vault. Use when gauging retail crowd sentiment, spotting cross-source divergence, or flagging a >=90/10 StockTwits over-extension to feed the edge pipeline. Scoring is fully deterministic (data-in-prompt, no model tool-calling) so nothing is fabricated.
---

# Retail Sentiment Ingestor

## Overview

Score real-time retail crowd sentiment per ticker and write it into the SAME
vault schema `social-signal-ingestor` uses, so `build_signal_index.py` and
`edge-social-aggregator` consume it with no change. `ingest_retail_sentiment.py`
fetches StockTwits cashtag messages (with their user-applied Bullish/Bearish
labels) and Reddit posts keylessly, optionally sweeps X, then runs **pure,
deterministic scoring** — sentiment band + 0-10 score + confidence, a StockTwits
message-count base rate, engagement-weighted Reddit, cross-source divergence
detection, and a contrarian over-extension flag at a >=90/10 StockTwits lean.

Unlike the YouTube ingestor, there is **no LLM extraction step**: StockTwits
carries explicit labels and Reddit/X carry numeric engagement, so the whole
pipeline is fetch → score → write. This is the anti-fabrication win — the model
never free-forms over raw feeds, so it cannot invent tickers, levels, or
sentiment (the failure mode that forced TradingAgents to redesign its sentiment
analyst around data-in-prompt).

Storage is namespaced by agent: `data/<agent>/{raw,vault,state}` (default agent
`social`, shared with `social-signal-ingestor` so corroboration spans platforms).

## When to Use

- Gauging retail sentiment on a watchlist of tickers before or alongside the
  edge pipeline.
- Detecting cross-source divergence (retail leaning one way, another crowd the
  other) or a crowded >=90/10 StockTwits over-extension.
- NOT for institutional/news framing (that is `market-news-analyst`), not for
  fundamentals/technicals on a known ticker (`us-stock-analysis`), and not for
  delivery/messaging (Hermes territory).

## Prerequisites

- Python 3.9+ and `pyyaml` (already a repo dependency). No API key for the
  StockTwits + Reddit path.
- Network egress to `api.stocktwits.com` and `reddit.com`.
- Optional X path: an X API bearer token in `X_API_KEY` (or `--x-api-key`).
  Historical event-window search needs a paid full-archive X tier.

## Workflow

### Step 1 — Ingest + score (deterministic, one command)

```bash
python3 skills/retail-sentiment-ingestor/scripts/ingest_retail_sentiment.py \
  --tickers "NVDA,AMD,TSLA" --agent social
```

For each ticker the script fetches StockTwits + Reddit, scores every source,
combines them, and (unless `--dry-run`) writes raw artifacts, one source note
per platform, and one signal note per ticker into
`data/social/vault/current/`. It prints a JSON run report and also saves a
markdown+JSON summary to `reports/`.

Seed tickers from StockTwits trending instead of (or in addition to) a list:

```bash
python3 skills/retail-sentiment-ingestor/scripts/ingest_retail_sentiment.py --trending
python3 skills/retail-sentiment-ingestor/scripts/ingest_retail_sentiment.py \
  --watchlist watchlists/swing.txt --tickers "NVDA" --dry-run
```

Add the optional impression-ranked X sweep (needs `X_API_KEY`), or a historical
event-window sweep via full-archive search:

```bash
export X_API_KEY=...   # bearer token
python3 skills/retail-sentiment-ingestor/scripts/ingest_retail_sentiment.py \
  --tickers "NVDA" --use-x
python3 skills/retail-sentiment-ingestor/scripts/ingest_retail_sentiment.py \
  --tickers "TSLA" --use-x --x-start 2026-01-27T00:00:00Z --x-end 2026-01-31T00:00:00Z
```

### Step 2 — Rebuild the machine index (deterministic)

The index builder lives in `social-signal-ingestor` and scans the shared vault,
so it picks up these notes with no change:

```bash
python3 skills/social-signal-ingestor/scripts/build_signal_index.py --agent social
```

### Step 3 — Score into the edge pipeline

```bash
python3 skills/edge-social-aggregator/scripts/aggregate_social.py \
  --agent social --output-dir reports/
```

`edge-social-aggregator` deduplicates each ticker across platforms and scores
`recency × corroboration`; a retail-sentiment ticker that a YouTube video also
named unions to a higher `n_sources`.

## Output

- `data/<agent>/raw/{stocktwits,reddit,x}/<date>/<TICKER>.json` — immutable raw payloads.
- `data/<agent>/vault/current/sources/{stocktwits,reddit,x}/<date>_<TICKER>.md` — one
  source note per platform with aggregate stats (no raw bodies/usernames).
- `data/<agent>/vault/current/signals/<date>_<TICKER>_retail-sentiment.md` — one signal
  note per ticker, in the exact schema `build_signal_index.py` reads.
- `reports/retail_sentiment_<ts>.{json,md}` — a run summary table.

The `data/` tree is git-ignored. Notes carry `direction`, `time_horizon: swing`,
`claim_date` (the snapshot date), and wikilinked `sources`. No `watch` block and
no `probability` are written — retail sentiment has neither clean numeric levels
nor a calibrated probability, and inventing them would be fabrication.

## Scoring (summary)

- **StockTwits:** Beta(2,2)-shrunk bullish fraction → 0-10 (small samples
  regress to the 5.0 base rate). `>=90/10` on `>=10` labeled messages fires the
  contrarian over-extension flag.
- **Reddit / X:** engagement-weighted keyword polarity (Reddit by score+comments,
  X by impressions), shrunk by directional-post count. Reddit's keyless RSS path
  has no engagement data → equal weight, marked `via_rss`.
- **Combine:** reliability-weighted blend (StockTwits > Reddit > X). One source
  bullish + another bearish → Mixed / watch. Confidence from directional volume.

Full derivation and worked examples: `references/sentiment_scoring.md`.

## Weekly reset

The signal vault is bounded by `social-signal-ingestor`'s
`reset_weekly_vault.py --agent social` (archives `vault/current`, prunes raw).
Retail-sentiment notes live in the same vault, so no separate reset is needed.

## Resources

- `scripts/ingest_retail_sentiment.py` — fetch + deterministic scoring + vault writer.
- `references/sentiment_scoring.md` — the scoring model and its rationale.

## Notes & Risk

- StockTwits and Reddit rate-limit anonymous clients; keep ticker lists modest.
  Reddit's JSON search is often WAF-blocked (HTTP 403) for keyless clients, so
  the fetcher falls back to the Atom RSS feed (no engagement data).
- Retail sentiment is a fast-moving, low-conviction signal — `edge-signal-
  aggregator` already weights the social source lowest (0.10). Treat it as one
  input to weigh alongside fundamentals and technicals, never a price call.
