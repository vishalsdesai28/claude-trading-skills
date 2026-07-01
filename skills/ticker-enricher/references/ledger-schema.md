# Enriched record shape (→ Supabase `recommendations`)

`ticker-enricher` emits one record per **(ticker, channel)** — if two channels recommend the
same ticker, that's two rows, each with its own `recommendation_source`. These map 1:1 to the
`public.recommendations` table (created via migration; written by `write-supabase`). The table
stores **facts only** — `gain_loss_pct` and `days_held` are NOT stored; the UI/dashboard computes
them live (see below).

Columns in table order (see the migration for the authoritative DDL):

| Field | Type | Source |
|---|---|---|
| id | uuid | generated |
| ticker | text | index (uppercased) |
| company_name | text | yfinance `.info` longName |
| sector | text | yfinance sector |
| industry | text | yfinance industry |
| recommendation_source | text | A **single** channel name (e.g. `MarketBeat`, `Ross Givens`) — one row per channel that cited the ticker; keeps the conflict key stable |
| source_type | text | Platform: `youtube` (future: `twitter`, `reddit`) |
| source_skill | text | provenance, e.g. `social-signal-ingestor` |
| date_recommended | date | earliest `claim_date` for the ticker (**immutable** — frozen by trigger) |
| direction | text | long / short / watch |
| instrument_type | text | `stock` (default) or `option` — from the note's `instrument` field |
| option_strategy | text | option play when `instrument_type = option` (e.g. `long_call`, `covered_call`); null for stock |
| option_legs | jsonb | one object per leg `{side, right, strike, expiry, ratio}`; multi-leg spreads stored in full; null for stock |
| net_premium | numeric | net debit (+) / credit (−) at recommendation — the option's P&L baseline (`price_at_recommendation` tracks the underlying, not the option); null for stock |
| price_at_recommendation | numeric | Yahoo close on date_recommended; **falls back to the current close** if that date's close isn't available yet (future-dated upload / non-trading day) so it's never null. **Immutable once set** (frozen by trigger) |
| current_price | numeric | latest close (the only field a refresh changes) |
| status | text | `active` |
| last_updated | timestamptz | run time |

Upsert conflict key: `(ticker, recommendation_source, date_recommended)` — pass to `write-supabase --conflict`.

## Computed by the UI (not stored)

```sql
gain_loss_pct = round((current_price - price_at_recommendation) / price_at_recommendation * 100, 2)
days_held     = current_date - date_recommended
```

A new (ticker, channel) pair inserts a full row; a later run only refreshes `current_price` + `last_updated`.
`price_at_recommendation` / `date_recommended` can never be overwritten — a `BEFORE UPDATE`
trigger forces them back to their original values, so the baseline (and therefore any gain
computed from it) stays correct even if yfinance later back-adjusts historical closes for
splits/dividends.

## Schema SQL

Authoritative DDL (table, constraints, RLS, immutability trigger) lives in
[`supabase/migrations/20260630010000_recommendations_schema.sql`](../../../supabase/migrations/20260630010000_recommendations_schema.sql).
Kept there only, so the schema can't drift between two copies.
