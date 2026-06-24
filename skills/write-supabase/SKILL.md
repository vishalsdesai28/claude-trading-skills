---
name: write-supabase
description: Generic Supabase table writer. Reads a records JSON file (array or {records:[...]}) and upserts or inserts the rows into a Supabase table named on the command line, with a caller-supplied conflict key. Knows nothing about any domain — every workflow points it at its own table and details, so it is reusable across the whole repo. Use as the final "persist to Supabase" step after a producer emits a records file.
---

# Write Supabase

## Overview

A small, generic transport skill: take a records JSON file and write the rows into a Supabase
table. The table, conflict key, and mode are all supplied by the caller, so any workflow can
reuse it — `ticker-enricher → write-supabase --table recommendations`, a screener →
`--table screener_hits`, etc. It holds no domain logic; building the records is the producer's job.

## When to Use

- As the last step of any workflow that has produced a records file and needs it persisted to Supabase.
- NOT for building/enriching records (that's the producer, e.g. `ticker-enricher`).

## Prerequisites

- `SUPABASE_URL` + a Supabase secret key — any of `SUPABASE_SERVICE_KEY`, `SUPABASE_SECRET_KEY`,
  `SUPABASE_SECRETS_KEY`, `SUPABASE_SERVICE_ROLE_KEY` (Supabase's newer "secret" key naming is
  accepted). Auto-loaded from a repo-root `.env` if present (so `python3 …` works without
  `source .env`); also honors a real exported env / cloud vault.
- The target table must already exist (create it via a Supabase migration).
- `requests` (already a repo dependency).

## Workflow

```bash
python3 skills/write-supabase/scripts/write_supabase.py \
  --table recommendations \
  --records "reports/enriched_records_*.json" \
  --conflict "ticker,recommendation_source,date_recommended"
```

- `--table` — target Supabase table (required).
- `--records` — path or glob to the records file(s); each is a JSON array or `{ "records": [...] }`.
- `--conflict` — comma-separated columns for `on_conflict` (upsert only).
- `--mode upsert` (default; `resolution=merge-duplicates`) or `--mode insert`.

## Output

Rows written to the named table via the Supabase REST API. Prints `{table, written, mode}`.

## Resources

- `references/usage.md` — CLI + records-file contract and examples.

## Notes & Risk

- Uses the **service/secret key** (bypasses RLS). Never expose it client-side; for dashboards add a
  read-only policy or key. Keep the key in a gitignored `.env` / 1Password / cloud vault — never committed.
- Upsert is idempotent on the conflict key, so re-running a workflow is safe.
- Retries transient 5xx with exponential backoff (Supabase intermittently 500s), and dedups the
  batch by the conflict key (keeping newest) so a glob matching several record files won't trip
  PostgREST's "ON CONFLICT cannot affect row a second time".
