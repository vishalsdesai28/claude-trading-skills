#!/usr/bin/env python3
"""Weekly reset for the social-signal vault (Hermes-style).

Archives data/<agent>/vault/current → vault/archive/weeks/YYYY-Www, starts a fresh
empty current week, and prunes old week-archives + raw video artifacts. Keeps the
working set (and everything downstream) bounded instead of growing forever.

state/youtube_state.json is intentionally NOT touched: the ingestor's seen-video
dedup must survive a reset so archived videos are never re-fetched or re-extracted.

Pure script, no LLM. Idempotent per ISO week via the vault/current/_reset.json marker.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
from pathlib import Path
from typing import Any

MARKER = "_reset.json"


def week_id(day: dt.datetime) -> str:
    """ISO week label, e.g. 2026-W26 (%G/%V = ISO year/week)."""
    return day.strftime("%G-W%V")


def resolve_paths(agent: str, data_dir: str | None) -> dict[str, Path]:
    root = Path(data_dir).expanduser() if data_dir else Path(__file__).resolve().parents[3] / "data"
    vault = root / agent / "vault"
    return {
        "current": vault / "current",
        "archive_weeks": vault / "archive" / "weeks",
        "raw_youtube": root / agent / "raw" / "youtube",
    }


def marker_week(current: Path) -> str | None:
    """The ISO week the current vault was initialized for, or None if unmarked (legacy)."""
    p = current / MARKER
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text()).get("week")
    except (OSError, json.JSONDecodeError):
        return None


def init_current(current: Path, week: str) -> None:
    """Create an empty fresh week: just the dirs the ingestor writes into + the marker."""
    for rel in ("sources/youtube", "signals"):
        (current / rel).mkdir(parents=True, exist_ok=True)
    (current / MARKER).write_text(json.dumps({"week": week}, indent=2) + "\n")


def prune_week_archives(archive_weeks: Path, keep: int, dry_run: bool = False) -> list[str]:
    """Remove all but the newest `keep` week-archive dirs (lexical order = chronological)."""
    if keep <= 0 or not archive_weeks.exists():
        return []
    dirs = sorted((p for p in archive_weeks.iterdir() if p.is_dir()), key=lambda p: p.name)
    removed = []
    for p in dirs[:-keep]:  # keep>=1 → newest `keep` retained
        removed.append(str(p))
        if not dry_run:
            shutil.rmtree(p)
    return removed


def prune_raw(raw_youtube: Path, days: int, now: dt.datetime, dry_run: bool = False) -> list[str]:
    """Remove raw/youtube/<channel>/<video> dirs older than `days` by mtime."""
    if days <= 0 or not raw_youtube.exists():
        return []
    cutoff = now.timestamp() - days * 86400
    removed = []
    for channel_dir in sorted(p for p in raw_youtube.iterdir() if p.is_dir()):
        for video_dir in sorted(p for p in channel_dir.iterdir() if p.is_dir()):
            try:
                if video_dir.stat().st_mtime < cutoff:
                    removed.append(str(video_dir))
                    if not dry_run:
                        shutil.rmtree(video_dir)
            except FileNotFoundError:  # concurrent removal — tolerate
                pass
    return removed


def run_reset(
    paths: dict[str, Path],
    now: dt.datetime,
    *,
    keep_archives: int,
    raw_days: int,
    force: bool,
    dry_run: bool,
) -> dict[str, Any]:
    current, archive_weeks = paths["current"], paths["archive_weeks"]
    new_week = week_id(now)
    mk = marker_week(current)
    has_content = current.exists() and any(current.iterdir())

    # No-op only when this week's reset already ran (marker present and matches).
    if has_content and mk == new_week and not force:
        return {
            "message": "vault already reset for this week; no-op",
            "week": new_week,
            "archived_to": None,
            "pruned_archives": [],
            "pruned_raw_dirs": [],
            "dry_run": dry_run,
        }

    archived_to = None
    if has_content:
        source_week = mk or new_week  # unmarked legacy content → label with the run week
        dest = archive_weeks / source_week
        if dest.exists():  # collision (e.g. --force same week) → timestamp suffix
            dest = archive_weeks / f"{source_week}-{now.strftime('%Y%m%dT%H%M%SZ')}"
        archived_to = str(dest)
        if not dry_run:
            archive_weeks.mkdir(parents=True, exist_ok=True)
            shutil.move(str(current), str(dest))

    if not dry_run:
        init_current(current, new_week)
    # ponytail: in --dry-run the prune lists reflect existing archives only (this run's
    # not-yet-created archive can't push an old one over the limit) — close enough for a preview.
    pruned_archives = prune_week_archives(archive_weeks, keep_archives, dry_run)
    pruned_raw = prune_raw(paths["raw_youtube"], raw_days, now, dry_run)

    return {
        "message": "weekly vault reset complete" + (" (dry-run)" if dry_run else ""),
        "week": new_week,
        "current": str(current),
        "archived_to": archived_to,
        "pruned_archives": pruned_archives,
        "pruned_raw_dirs": pruned_raw,
        "dry_run": dry_run,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Weekly archive+reset of the social-signal vault (data/<agent>/vault)."
    )
    ap.add_argument(
        "--agent", default="social", help="Agent name → data/<agent>/ (default: social)"
    )
    ap.add_argument(
        "--data-dir", default=None, help="Override base data dir (default: <repo>/data)"
    )
    ap.add_argument(
        "--keep-weeks", type=int, default=8, help="Week-archives to retain (default: 8)"
    )
    ap.add_argument(
        "--raw-days", type=int, default=60, help="Prune raw videos older than N days (default: 60)"
    )
    ap.add_argument("--force", action="store_true", help="Reset even if already done this ISO week")
    ap.add_argument("--dry-run", action="store_true", help="Report only; make no changes")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    report = run_reset(
        resolve_paths(args.agent, args.data_dir),
        now,
        keep_archives=args.keep_weeks,
        raw_days=args.raw_days,
        force=args.force,
        dry_run=args.dry_run,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
