---
name: social-signal-ingestor
description: Ingest trading signals from YouTube channels — fetch video transcripts via yt-dlp, then extract one structured signal note per ticker (direction, price levels, sources) into a local vault. Use when mining social/video commentary for tradeable tickers, sentiment, and catalysts to feed the edge pipeline. YouTube is v1; X/Reddit plug in later via Agent-Reach.
---

# Social Signal Ingestor

## Overview

Mine trading YouTube channels for tradeable signals. A deterministic fetch step
(`ingest_youtube.py`, wrapping the `yt-dlp` binary) stores immutable raw artifacts
and writes a source stub per new video; this skill then reads each transcript and
extracts **one signal note per ticker** into a vault; a deterministic index build
(`build_signal_index.py`) emits `signals/index.json`, which `edge-social-aggregator`
consumes. No video/audio is downloaded — metadata + subtitles only.

Storage is namespaced by agent: `data/<agent>/{raw,vault,state}` (default agent `social`).

## When to Use

- Mining YouTube trading commentary for tickers, sentiment, price levels, and catalysts.
- Producing social signals to merge (low-weight) into `edge-signal-aggregator`.
- NOT for fundamentals/technicals on a known ticker (use `us-stock-analysis`), and not
  for delivery/messaging (that is Hermes territory).

## Prerequisites

- `yt-dlp` on PATH (`pip install yt-dlp`). Public subtitles only — no API key.
- `pyyaml` (already a repo dependency).
- A channel list (`assets/channels.yaml` by default; override with `--config`).

## Workflow

### Step 1 — Fetch new videos (deterministic)

```bash
python3 skills/social-signal-ingestor/scripts/ingest_youtube.py \
  --agent social --config skills/social-signal-ingestor/assets/channels.yaml
```

Writes raw artifacts to `data/social/raw/youtube/<channel>/<video_id>/`
(`metadata.json`, `transcript .clean.txt`), a source stub in
`data/social/vault/current/sources/youtube/`, updates `state/youtube_state.json`
(idempotent — seen videos are skipped), and prints a JSON report listing
`new_videos` (each with `clean_transcript_paths` and `source_stub`).

### Step 2 — Extract signal notes (the analysis step)

For every entry in the report's `new_videos`:

1. Read the `clean_transcript_paths` and the `source_stub`.
2. Fill in the source stub's summary + trading-relevant claims.
3. Create **one signal note per real, quotable ticker** in
   `data/<agent>/vault/current/signals/` named `YYYY-MM-DD_TICKER_short-slug.md`,
   following `references/signal-schema.md`. Required frontmatter: `title`, `type: signal`,
   `ticker` (one symbol), `direction` (long/short/watch), `time_horizon`,
   `claim_date`, `sources` (wikilinks to the source note).
4. Add a `watch` block only when the source gives clean numeric trigger/invalidation
   levels. Conviction is computed downstream from recency and corroboration, so there
   is no confidence field to set — just be accurate about `claim_date` and `sources`.
5. Never invent a ticker for a theme with no instrument; never join symbols
   (`STRL/POWL`) into one note.

### Step 3 — Rebuild the machine index (deterministic)

```bash
python3 skills/social-signal-ingestor/scripts/build_signal_index.py --agent social
```

Scans the signal notes and writes `data/social/vault/current/signals/index.json`,
reporting any parse errors or multi-symbol ticker warnings. Run this AFTER Step 2.

## Output

- `data/<agent>/raw/youtube/...` — immutable transcripts + metadata.
- `data/<agent>/vault/current/sources/youtube/*.md` — one source note per video.
- `data/<agent>/vault/current/signals/*.md` — one signal note per ticker.
- `data/<agent>/vault/current/signals/index.json` — the machine contract consumed by
  `edge-social-aggregator`.

The `data/` tree is git-ignored — raw transcripts and signals never enter the repo.

## Resources

- `references/signal-schema.md` — signal-note frontmatter spec.
- `assets/channels.yaml` — the (pre-vetted) channel list.

## Notes & Risk

- `yt-dlp` scrapes public YouTube subtitles; YouTube rate-limits (HTTP 429). Keep
  `playlist_items` small. For higher volume, move to the YouTube Data API (key in 1Password).
- X / Reddit are deferred: add `fetch_x()` / `fetch_reddit()` backends that shell out to
  Agent-Reach (`twitter-cli`, `rdt-cli`), gated by `agent-reach doctor`.
