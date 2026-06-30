-- ============================================================================
--  recommendations — social-signal recommendation ledger
-- ----------------------------------------------------------------------------
--  One row per (ticker, recommendation_source, date_recommended), written by
--  ticker-enricher -> write-supabase from the social-signal pipeline.
--
--  The recommendation baseline (price + date) is immutable once set; gain % and
--  days held are derived live by the dashboard and intentionally not stored.
-- ============================================================================

create table if not exists public.recommendations (
    -- Identity
    id                      uuid        primary key default gen_random_uuid(),

    -- Instrument
    ticker                  text        not null,
    company_name            text,
    sector                  text,
    industry                text,

    -- Provenance
    recommendation_source   text        not null,
    source_type             text        not null default 'youtube',   -- youtube | twitter | reddit
    source_skill            text,

    -- Recommendation
    date_recommended        date        not null,
    direction               text,                                      -- long | short | watch
    instrument_type         text        not null default 'stock',      -- stock | option
    option_strategy         text,                                      -- long_call, covered_call, … (option only)
    option_legs             jsonb,                                     -- [{side, right, strike, expiry, ratio}, …]
    net_premium             numeric,                                   -- net debit (+) / credit (-) at recommendation

    -- Pricing (baseline frozen by trigger below)
    price_at_recommendation numeric,
    current_price           numeric,

    -- Lifecycle
    status                  text        not null default 'active',
    last_updated            timestamptz not null default now(),

    constraint recommendations_unique
        unique (ticker, recommendation_source, date_recommended),
    constraint recommendations_instrument_type_check
        check (instrument_type in ('stock', 'option'))
);

alter table public.recommendations enable row level security;

comment on table  public.recommendations is
    'Social-signal recommendation ledger; one row per (ticker, source, date).';
comment on column public.recommendations.option_legs is
    'Option structure: array of {side, right, strike, expiry, ratio}; null for stock.';
comment on column public.recommendations.net_premium is
    'Net debit (+) / credit (-) at recommendation; option P&L baseline. Null for stock.';
comment on column public.recommendations.price_at_recommendation is
    'Underlying close on date_recommended; immutable once set (see trigger).';

-- Immutable baseline: an UPDATE may backfill a null price/date but can never
-- change an established one, so historical gain stays correct even if the data
-- vendor later back-adjusts closes for splits / dividends.
create or replace function public.freeze_recommendation_baseline()
returns trigger as $$
begin
    if old.price_at_recommendation is not null then
        new.price_at_recommendation := old.price_at_recommendation;
    end if;
    new.date_recommended := old.date_recommended;
    return new;
end;
$$ language plpgsql;

drop trigger if exists trg_freeze_recommendation_baseline on public.recommendations;
create trigger trg_freeze_recommendation_baseline
    before update on public.recommendations
    for each row execute function public.freeze_recommendation_baseline();
