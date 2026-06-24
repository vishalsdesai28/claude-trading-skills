---
name: ticker-enricher
description: Enrich ticker signals with company metadata (name, sector, industry) and prices (recommendation-date price, current price) from Yahoo Finance. Reads the social-signal vault index and emits a records file ready for a generic writer (e.g. write-supabase); the dashboard derives gain % and days held from the stored prices/dates. Reusable by any producer that has tickers.
---

# Ticker Enricher

## Overview

Turns ticker signals into enriched, dashboard-ready records. For each ticker in the social-signal
vault index it looks up company name / sector / industry and the recommendation-date + current
price (Yahoo Finance), resolves the recommendation source (the YouTube channel from the source
note), and emits `reports/enriched_records_*.json`. Gain % and age are derived later by the UI
from `price_at_recommendation`, `current_price`, and `date_recommended` — not stored here.

It does **only** enrichment + record shaping — persisting the records is a separate, generic
concern (`write-supabase`). That separation makes both halves reusable: any producer with tickers
can enrich them here, and the records can be written to any table.

## When to Use

- After social-signal-ingestor has produced/refreshed the signal index, to attach company +
  price data to each ticker.
- Before `write-supabase` (which uploads the emitted records file to a table).
- NOT for trade lifecycle / MAE-MFE (that's `trader-memory-core`).

## Prerequisites

- A populated `data/<agent>/vault/current/signals/index.json`.
- `yfinance` (already a repo dependency) for company metadata + prices.

## Workflow

```bash
python3 skills/ticker-enricher/scripts/enrich_tickers.py --agent social --output-dir reports/
```

Groups index signals by ticker, sets `date_recommended` = earliest `claim_date`,
`price_at_recommendation` = Yahoo close on that date, `current_price` = latest,
resolves `recommendation_source`, and writes `reports/enriched_records_<ts>.json`:

```json
{ "table_hint": "recommendations",
  "conflict_hint": "ticker,recommendation_source,date_recommended",
  "records": [ { "ticker": "SOFI", "company_name": "...", "sector": "...", "current_price": 17.1, ... } ] }
```

Then hand the file to `write-supabase` (the `*_hint` keys are documentation; the writer takes
table + conflict from its own CLI). See `references/ledger-schema.md` for the full record shape.

## Output

`reports/enriched_records_<ts>.json` — one record per ticker with: ticker, company_name, sector,
industry, date_recommended, price_at_recommendation, current_price, recommendation_source,
source_skill, direction, status. (Gain % and days held are computed downstream by the UI.)

## Resources

- `references/ledger-schema.md` — record shape + the `recommendations` table it typically lands in.

## Notes & Risk

- `price_at_recommendation` is the Yahoo close on a past date (immutable), so re-running is
  idempotent at upsert time. yfinance `.info` (sector/industry) can be flaky; FMP `/profile` is a
  later alternative.
