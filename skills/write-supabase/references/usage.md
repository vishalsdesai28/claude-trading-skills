# write-supabase — CLI & records contract

## Records file

A JSON file that is either a bare array of row objects, or an object with a `records` key:

```json
{ "records": [ { "ticker": "SOFI", "current_price": 18.0, "gain_loss_pct": 12.5 } ] }
```

`--records` accepts a path or a glob (e.g. `reports/enriched_records_*.json`); all matching files
are concatenated. Each row object's keys must be columns of the target table. Any non-column keys
(e.g. a producer's `table_hint` / `conflict_hint` live at the top level, not inside rows) are ignored.

## CLI

| Flag | Required | Meaning |
|---|---|---|
| `--table` | yes | Target Supabase table |
| `--records` | yes | Path or glob to the records file(s) |
| `--conflict` | no | Comma-separated `on_conflict` columns (upsert) |
| `--mode` | no | `upsert` (default) or `insert` |

## Env

`SUPABASE_URL` + a secret key (first present of `SUPABASE_SERVICE_KEY`, `SUPABASE_SECRET_KEY`,
`SUPABASE_SECRETS_KEY`, `SUPABASE_SERVICE_ROLE_KEY`). Resolution order: existing environment →
repo-root `.env` (auto-parsed, never overrides an already-set var). Examples of providing them:

```bash
# already exported / cloud vault — nothing extra needed
python3 .../write_supabase.py --table recommendations --records "reports/enriched_records_*.json" \
  --conflict "ticker,recommendation_source,date_recommended"

# or rely on the repo-root .env auto-load (same command, no `source` needed)
```

## Behavior

- `upsert` (default): POST with `Prefer: resolution=merge-duplicates` and `?on_conflict=<cols>`.
  Existing rows (by the conflict key) are merged; new rows inserted.
- `insert`: plain POST, no conflict handling (fails on duplicates).
- Uses the service key (bypasses RLS). Prints `{table, written, mode}`.
