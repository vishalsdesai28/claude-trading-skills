# Enriched record shape (→ Supabase `recommendations`)

`ticker-enricher` emits one record per **(ticker, channel)** — if two channels recommend the
same ticker, that's two rows, each with its own `recommendation_source`. These map 1:1 to the
`public.recommendations` table (created via migration; written by `write-supabase`). The table
stores **facts only** — `gain_loss_pct` and `days_held` are NOT stored; the UI/dashboard computes
them live (see below).

| Field | Type | Source |
|---|---|---|
| ticker | text | index (uppercased) |
| company_name | text | yfinance `.info` longName |
| sector | text | yfinance sector |
| industry | text | yfinance industry |
| date_recommended | date | earliest `claim_date` for the ticker (**immutable** — frozen by trigger) |
| price_at_recommendation | numeric | Yahoo close on date_recommended; **falls back to the current close** if that date's close isn't available yet (future-dated upload / non-trading day) so it's never null. **Immutable once set** (frozen by trigger) |
| current_price | numeric | latest close (the only field a refresh changes) |
| recommendation_source | text | A **single** channel name (e.g. `MarketBeat`, `Ross Givens`) — one row per channel that cited the ticker; keeps the conflict key stable |
| source_type | text | Platform: `youtube` (future: `twitter`, `reddit`) |
| source_skill | text | provenance, e.g. `social-signal-ingestor` |
| direction | text | long / short / watch |
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

## Schema SQL (migrations; already applied)

```sql
create table if not exists public.recommendations (
  id uuid primary key default gen_random_uuid(),
  ticker text not null,
  company_name text,
  sector text,
  industry text,
  date_recommended date not null,
  price_at_recommendation numeric,
  current_price numeric,
  recommendation_source text not null,
  source_type text not null default 'youtube',
  source_skill text,
  direction text,
  status text not null default 'active',
  last_updated timestamptz not null default now(),
  constraint recommendations_unique unique (ticker, recommendation_source, date_recommended)
);
alter table public.recommendations enable row level security;

-- Baseline is immutable: any UPDATE keeps the original recommendation price + date.
create or replace function public.freeze_recommendation_baseline() returns trigger as $$
begin
  -- Freeze the baseline only once set: allow null -> value (backfill), block value -> change.
  if old.price_at_recommendation is not null then
    new.price_at_recommendation := old.price_at_recommendation;
  end if;
  new.date_recommended := old.date_recommended;
  return new;
end;
$$ language plpgsql;

create trigger trg_freeze_recommendation_baseline
  before update on public.recommendations
  for each row execute function public.freeze_recommendation_baseline();
```
