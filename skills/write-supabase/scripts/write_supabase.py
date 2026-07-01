#!/usr/bin/env python3
"""write-supabase: generic Supabase table writer.

Reads a records JSON file (a list, or {"records": [...]}) and UPSERTs/INSERTs the rows
into a Supabase table named on the CLI. Knows nothing about any domain — every workflow
points it at its own table with its own conflict key, so it is reusable everywhere.

Env: SUPABASE_URL + a secret key (SUPABASE_SERVICE_KEY / SUPABASE_SECRET_KEY /
SUPABASE_SECRETS_KEY / SUPABASE_SERVICE_ROLE_KEY), auto-loaded from a repo-root .env if present.

# ponytail: writes via the already-present `requests` dep against the Supabase REST API
# (PostgREST) — no supabase-py. The .env loader is a ~10-line parser, no python-dotenv.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path
from typing import Any

# Accepted env var names for the Supabase secret/write key, newest naming first.
# (Supabase renamed `service_role` → "secret" keys; we accept both conventions.)
SECRET_KEY_NAMES = (
    "SUPABASE_SERVICE_KEY",
    "SUPABASE_SECRET_KEY",
    "SUPABASE_SECRETS_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
)


def _pick_key(env: dict[str, str]) -> str | None:
    """First present Supabase secret-key value from the accepted names."""
    return next((env[n] for n in SECRET_KEY_NAMES if env.get(n)), None)


def _parse_dotenv(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines (skip blanks/comments, strip surrounding quotes)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def _load_dotenv() -> None:
    """Populate SUPABASE_* from a repo-root .env when not already in the environment."""
    if os.environ.get("SUPABASE_URL") and _pick_key(os.environ):
        return
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if not env_path.exists():
        return
    for key, val in _parse_dotenv(env_path.read_text()).items():
        os.environ.setdefault(key, val)


def load_records(pattern: str) -> list[dict[str, Any]]:
    """Load records from a path or glob. Each file is a list or {"records": [...]}."""
    files = sorted(glob.glob(pattern))
    if not files:
        raise SystemExit(f"No records files match: {pattern}")
    records: list[dict[str, Any]] = []
    for f in files:
        data = json.loads(Path(f).read_text())
        rows = data.get("records") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise SystemExit(f"{f}: expected a JSON array or {{'records': [...]}}")
        records.extend(rows)
    return records


def dedup_by_conflict(rows: list[dict[str, Any]], conflict: str | None) -> list[dict[str, Any]]:
    """Collapse rows sharing the same conflict-key tuple, keeping the LAST (newest, since
    a sorted glob appends newest files last). PostgREST upsert rejects a batch that contains
    duplicate constrained values (error 21000)."""
    if not conflict:
        return rows
    cols = [c.strip() for c in conflict.split(",")]
    seen: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        seen[tuple(r.get(c) for c in cols)] = r
    return list(seen.values())


def _headers(key: str) -> dict[str, str]:
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def supabase_write(
    url: str,
    key: str,
    table: str,
    rows: list[dict[str, Any]],
    conflict: str | None,
    mode: str,
    retries: int = 4,
) -> int:
    """UPSERT (default) or INSERT rows into a table via the Supabase REST API.

    Retries on transient 5xx / connection errors with exponential backoff — Supabase
    intermittently returns 500 on otherwise-valid requests. 4xx raise immediately.
    """
    import time

    import requests

    endpoint = f"{url.rstrip('/')}/rest/v1/{table}"
    headers = {**_headers(key), "Prefer": "return=minimal"}
    if mode == "upsert":
        rows = dedup_by_conflict(rows, conflict)  # PostgREST rejects intra-batch dup conflict keys
        if conflict:
            endpoint += f"?on_conflict={conflict}"
        headers["Prefer"] = "resolution=merge-duplicates,return=minimal"

    last = ""
    for attempt in range(retries):
        try:
            resp = requests.post(endpoint, headers=headers, data=json.dumps(rows), timeout=30)
            if resp.status_code < 500:
                resp.raise_for_status()  # 4xx → our bug, surface it
                return len(rows)
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except requests.RequestException as exc:  # noqa: PERF203
            last = str(exc)
        if attempt < retries - 1:
            time.sleep(2**attempt)  # 1s, 2s, 4s
    raise SystemExit(f"Supabase write failed after {retries} attempts — {last}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a records file to a Supabase table.")
    parser.add_argument("--table", required=True, help="Target Supabase table")
    parser.add_argument("--records", required=True, help="Path or glob to the records JSON file(s)")
    parser.add_argument(
        "--conflict", default=None, help="Comma-separated on_conflict columns (upsert)"
    )
    parser.add_argument(
        "--mode", choices=["upsert", "insert"], default="upsert", help="Default: upsert"
    )
    args = parser.parse_args()

    _load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = _pick_key(os.environ)
    if not url or not key:
        print(
            "Error: set SUPABASE_URL and a secret key "
            f"({' / '.join(SECRET_KEY_NAMES)}) via env / .env / vault.",
            file=sys.stderr,
        )
        return 1

    rows = load_records(args.records)
    if not rows:
        print(json.dumps({"message": "no records to write", "table": args.table, "written": 0}))
        return 0
    written = supabase_write(url, key, args.table, rows, args.conflict, args.mode)
    print(
        json.dumps(
            {"message": "rows written", "table": args.table, "written": written, "mode": args.mode}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
