#!/usr/bin/env python3
"""Fetch new YouTube videos for the social-signal-ingestor skill.

Uses the `yt-dlp` binary (via subprocess) for channel listing, metadata, and
subtitles; stores immutable raw artifacts under data/<agent>/raw/youtube/,
writes vault source stubs, and emits a JSON report. No video/audio is downloaded.

The platform fetch lives in fetch_youtube() — the pluggable seam where future
X / Reddit backends (Agent-Reach: twitter-cli, rdt-cli) slot in without
restructuring the rest of the pipeline.

# ponytail: yt-dlp is a system binary, called by subprocess — no `import yt_dlp`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from clean_transcript import clean_vtt_file  # noqa: E402

VIDEO_URL = "https://www.youtube.com/watch?v={video_id}"
# Written verbatim into the stub's Executive summary. Its presence means the
# extraction step never enriched the stub (crash/rate limit after the video was
# marked seen), so the video is still pending and should be re-offered next run.
ENRICHMENT_PENDING_MARKER = (
    "TODO: read the transcript/raw artifacts and summarize this source, then extract signal notes."
)
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "assets" / "channels.yaml"


def resolve_paths(agent: str, data_dir: str | None) -> dict[str, Path]:
    """data/<agent>/{raw, vault/current, state}. Repo-root-relative by default."""
    root = Path(data_dir).expanduser() if data_dir else Path(__file__).resolve().parents[3] / "data"
    base = root / agent
    return {
        "raw": base / "raw",
        "vault_current": base / "vault" / "current",
        "state": base / "state",
    }


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Config file not found: {path}")
    text = path.read_text()
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError("top-level config must be a mapping")
        return data
    except ImportError:
        # JSON is valid YAML, so this fallback supports JSON syntax too.
        return json.loads(text)


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-._")
    return value or "channel"


def run(cmd: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)


def ensure_yt_dlp() -> None:
    if not shutil.which("yt-dlp"):
        raise SystemExit("yt-dlp is not on PATH (install: pip install yt-dlp)")


def load_state(state_path: Path) -> dict[str, Any]:
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {"seen_video_ids": {}, "runs": []}


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(state_path)


def resolve_channel_id(channel_url: str) -> str | None:
    """Resolve a channel/handle/videos URL to a channel id via one flat yt-dlp entry."""
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-json",
        "--playlist-end",
        "1",
        "--no-warnings",
        channel_url,
    ]
    proc = run(cmd, timeout=60)
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        channel_id = (
            obj.get("playlist_channel_id") or obj.get("channel_id") or obj.get("uploader_id")
        )
        if channel_id:
            return str(channel_id)
    return None


def list_channel_entries_rss(channel_id: str, playlist_items: int) -> list[dict[str, Any]]:
    """Fast path: YouTube RSS has title, video id, and published timestamp."""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (trusted host)
        body = resp.read()
    root = ET.fromstring(body)
    ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    entries: list[dict[str, Any]] = []
    for node in root.findall("atom:entry", ns)[:playlist_items]:
        video_id = (node.findtext("yt:videoId", default="", namespaces=ns) or "").strip()
        title = (node.findtext("atom:title", default="", namespaces=ns) or "").strip()
        published = (node.findtext("atom:published", default="", namespaces=ns) or "").strip()
        ts = None
        upload_date = None
        if published:
            try:
                parsed = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
                ts = int(parsed.timestamp())
                upload_date = parsed.strftime("%Y%m%d")
            except ValueError:
                pass
        if video_id:
            entries.append(
                {
                    "id": video_id,
                    "url": VIDEO_URL.format(video_id=video_id),
                    "title": title,
                    "timestamp": ts,
                    "upload_date": upload_date,
                    "published": published,
                    "source": "youtube_rss",
                }
            )
    return entries


def list_channel_entries(channel_url: str, playlist_items: int) -> list[dict[str, Any]]:
    channel_id = resolve_channel_id(channel_url)
    if channel_id:
        try:
            entries = list_channel_entries_rss(channel_id, playlist_items)
            if entries:
                return entries
        except Exception:  # noqa: BLE001
            pass
    # Fallback: flat playlist has no publish date for many channels, but keeps compatibility.
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-json",
        "--playlist-end",
        str(playlist_items),
        "--no-warnings",
        channel_url,
    ]
    proc = run(cmd, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.strip() or proc.stdout.strip() or "yt-dlp channel listing failed"
        )
    entries = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        video_id = obj.get("id") or obj.get("url")
        if video_id:
            obj["id"] = str(video_id)
            entries.append(obj)
    return entries


def fetch_metadata(video_id: str) -> dict[str, Any]:
    proc = run(
        [
            "yt-dlp",
            "--dump-json",
            "--skip-download",
            "--no-warnings",
            VIDEO_URL.format(video_id=video_id),
        ],
        timeout=240,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.strip() or proc.stdout.strip() or "yt-dlp metadata fetch failed"
        )
    return json.loads(proc.stdout)


def metadata_timestamp(metadata: dict[str, Any]) -> int | None:
    """Best-effort UTC publish timestamp from yt-dlp metadata."""
    for key in ("timestamp", "release_timestamp", "modified_timestamp"):
        value = metadata.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    upload_date = metadata.get("upload_date")
    if isinstance(upload_date, str) and re.fullmatch(r"\d{8}", upload_date):
        try:
            parsed = dt.datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=dt.timezone.utc)
            return int(parsed.timestamp())
        except ValueError:
            return None
    return None


def fetch_subtitles(video_id: str, out_base: Path, subtitle_languages: str) -> list[str]:
    outtmpl = str(out_base) + ".%(ext)s"
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        subtitle_languages,
        "--sub-format",
        "vtt/best",
        "--no-warnings",
        "-o",
        outtmpl,
        VIDEO_URL.format(video_id=video_id),
    ]
    before = set(out_base.parent.glob(out_base.name + ".*"))
    proc = run(cmd, timeout=300)
    after = set(out_base.parent.glob(out_base.name + ".*"))
    created = sorted(
        str(p)
        for p in (after - before)
        if p.suffix.lower() in {".vtt", ".json3", ".srv1", ".srv2", ".srv3", ".ttml"}
    )
    if proc.returncode != 0 and not created:
        return []
    return created


def clean_subtitles(subtitle_paths: list[str]) -> list[str]:
    """Collapse each raw auto-caption VTT into a plain-text sibling file."""
    clean_paths: list[str] = []
    for raw_path in subtitle_paths:
        path = Path(raw_path)
        if path.suffix.lower() != ".vtt":
            continue
        try:
            cleaned = clean_vtt_file(path)
        except Exception:  # noqa: BLE001
            continue
        if not cleaned:
            continue
        clean_path = path.parent / (path.name[: -len(".vtt")] + ".clean.txt")
        clean_path.write_text(cleaned)
        clean_paths.append(str(clean_path))
    return clean_paths


def write_source_stub(
    vault_current_dir: Path,
    channel_slug: str,
    video_id: str,
    metadata: dict[str, Any],
    raw_paths: dict[str, Any],
) -> Path:
    upload_date = metadata.get("upload_date") or "unknown-date"
    if re.fullmatch(r"\d{8}", str(upload_date)):
        date_s = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    else:
        date_s = str(upload_date)
    title = metadata.get("title") or video_id
    source_dir = vault_current_dir / "sources" / "youtube"
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"{date_s}_{video_id}.md"
    channel = metadata.get("channel") or metadata.get("uploader") or channel_slug
    webpage_url = metadata.get("webpage_url") or VIDEO_URL.format(video_id=video_id)
    now = dt.datetime.now(dt.timezone.utc)
    today = now.strftime("%Y-%m-%d")
    week = now.strftime("%G-W%V")
    raw_source_list: list[str] = []
    for value in raw_paths.values():
        if isinstance(value, list):
            raw_source_list.extend(str(item) for item in value)
        else:
            raw_source_list.append(str(value))
    source_yaml = "\n".join(f"  - {item}" for item in raw_source_list) or "  []"
    lines = [
        "---",
        f"title: {title}",
        f"created: {today}",
        f"updated: {today}",
        f"week: {week}",
        "type: source",
        "source_type: youtube",
        "status: active",
        "time_horizon: weekly",
        f"channel: {channel}",
        f"video_id: {video_id}",
        f"source_url: {webpage_url}",
        "sources:",
        source_yaml,
        "tags: [social-signal, source, youtube]",
        "---",
        "",
        f"# {title}",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Channel | {channel} |",
        f"| Upload date | {date_s} |",
        f"| Video ID | {video_id} |",
        f"| URL | {webpage_url} |",
        f"| Duration | {metadata.get('duration_string') or metadata.get('duration') or ''} |",
        "",
        "## Executive summary",
        "",
        ENRICHMENT_PENDING_MARKER,
        "",
        "## Trading-relevant claims",
        "",
        "TODO: extract tickers, catalysts, levels, time horizons, invalidations, sentiment, contradictions.",
        "",
        "## Signal candidates",
        "",
        "TODO: link any created signal notes, e.g. [[signals/YYYY-MM-DD_TICKER_short-slug]].",
        "",
        "## Raw artifacts",
        "",
    ]
    for key, value in raw_paths.items():
        if isinstance(value, list):
            for item in value:
                lines.append(f"- {key}: `{item}`")
        else:
            lines.append(f"- {key}: `{value}`")
    lines.append("")
    if not path.exists():
        path.write_text("\n".join(lines))
    return path


def collect_pending_retries(state: dict[str, Any], channels: list[Any]) -> list[dict[str, Any]]:
    """Re-offer previously-fetched videos whose stub was never enriched.

    A video is marked seen as soon as its raw stub is written, before extraction
    runs. If extraction fails (crash, rate limit), the unfilled TODO marker in
    the stub makes retry detection deterministic and self-healing.
    """
    name_by_slug: dict[str, str] = {}
    for channel in channels:
        if isinstance(channel, str):
            channel = {"name": channel, "url": channel}
        if not isinstance(channel, dict):
            continue
        name = str(channel.get("name") or channel.get("url") or "channel")
        name_by_slug[slugify(name)] = name

    retries: list[dict[str, Any]] = []
    seen = state.get("seen_video_ids", {}) or {}
    for channel_slug, videos in seen.items():
        if not isinstance(videos, dict):
            continue
        for video_id, info in videos.items():
            if not isinstance(info, dict):
                continue
            source_stub = info.get("source_stub")
            if not source_stub:
                continue
            try:
                text = Path(source_stub).read_text()
            except OSError:
                continue
            if ENRICHMENT_PENDING_MARKER not in text:
                continue
            retries.append(
                {
                    "channel": name_by_slug.get(channel_slug, channel_slug),
                    "channel_slug": channel_slug,
                    "video_id": video_id,
                    "url": info.get("url") or VIDEO_URL.format(video_id=video_id),
                    "title": info.get("title"),
                    "source_stub": source_stub,
                    "metadata_path": info.get("metadata_path"),
                    "clean_transcript_paths": info.get("clean_transcript_paths"),
                    "retry": True,
                    "retry_reason": "source stub still has unfilled TODO placeholders from a previous failed extraction",
                }
            )
    return retries


def fetch_youtube(
    args: argparse.Namespace, config: dict[str, Any], paths: dict[str, Path]
) -> dict[str, Any]:
    """YouTube backend. The pluggable seam: fetch_x() / fetch_reddit() mirror this later."""
    raw_dir = paths["raw"]
    vault_current_dir = paths["vault_current"]
    youtube_cfg = config.get("youtube", {}) or {}
    channels = youtube_cfg.get("channels", []) or []
    playlist_items = int(args.max_videos or youtube_cfg.get("playlist_items", 25))
    subtitle_languages = str(youtube_cfg.get("subtitle_languages", "en.*,en"))
    default_initial_since_hours = int(youtube_cfg.get("initial_since_hours", 24))

    state_path = paths["state"] / "youtube_state.json"
    state = load_state(state_path)
    seen = state.setdefault("seen_video_ids", {})
    channel_cutoffs = state.setdefault("channel_cutoffs", {})

    run_started_dt = dt.datetime.now(dt.timezone.utc)
    run_started = run_started_dt.isoformat()
    report: dict[str, Any] = {
        "run_started": run_started,
        "channels_configured": len(channels),
        "dry_run": args.dry_run,
        "new_videos": [],
        "errors": [],
    }
    if not channels:
        report["message"] = (
            "No YouTube channels configured. Add youtube.channels entries to the config."
        )
        return report

    report["new_videos"] = collect_pending_retries(state, channels)

    for channel in channels:
        if isinstance(channel, str):
            channel = {"name": channel, "url": channel, "enabled": True}
        if not channel or channel.get("enabled", True) is False:
            continue
        name = str(channel.get("name") or channel.get("url") or "channel")
        url = str(channel.get("url") or "").strip()
        if not url:
            report["errors"].append({"channel": name, "error": "missing url"})
            continue
        channel_slug = slugify(name)
        channel_seen = seen.setdefault(channel_slug, {})
        cutoff_info = channel_cutoffs.get(channel_slug)
        initial_cutoff_ts: int | None = None
        if isinstance(cutoff_info, dict) and isinstance(cutoff_info.get("initial_cutoff_ts"), int):
            initial_cutoff_ts = int(cutoff_info["initial_cutoff_ts"])
        elif not channel_seen:
            initial_cutoff_dt = run_started_dt - dt.timedelta(hours=default_initial_since_hours)
            initial_cutoff_ts = int(initial_cutoff_dt.timestamp())
            if not args.dry_run:
                channel_cutoffs[channel_slug] = {
                    "initial_cutoff_ts": initial_cutoff_ts,
                    "initial_cutoff_iso": initial_cutoff_dt.isoformat(),
                    "initial_since_hours": default_initial_since_hours,
                    "created_at": run_started,
                }
        try:
            entries = list_channel_entries(url, playlist_items)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append({"channel": name, "url": url, "error": str(exc)})
            continue

        for entry in reversed(entries):  # oldest first → natural chronology
            video_id = str(entry.get("id") or "").strip()
            if not video_id or video_id in channel_seen:
                continue
            item = {
                "channel": name,
                "channel_slug": channel_slug,
                "video_id": video_id,
                "url": VIDEO_URL.format(video_id=video_id),
                "listed_title": entry.get("title"),
            }
            try:
                entry_ts = (
                    entry.get("timestamp") if isinstance(entry.get("timestamp"), int) else None
                )
                if (
                    initial_cutoff_ts is not None
                    and entry_ts is not None
                    and entry_ts < initial_cutoff_ts
                ):
                    item.update(
                        {
                            "skipped": True,
                            "skip_reason": f"older than initial_since_hours={default_initial_since_hours}",
                        }
                    )
                    report.setdefault("skipped_videos", []).append(item)
                    continue
                metadata = fetch_metadata(video_id)
                published_ts = metadata_timestamp(metadata) or entry_ts
                if initial_cutoff_ts is not None and (
                    published_ts is None or published_ts < initial_cutoff_ts
                ):
                    item.update(
                        {
                            "skipped": True,
                            "skip_reason": "older than cutoff or missing publish timestamp",
                        }
                    )
                    report.setdefault("skipped_videos", []).append(item)
                    continue
                if args.dry_run:
                    item.update(
                        {
                            "title": metadata.get("title") or entry.get("title"),
                            "published_ts": published_ts,
                        }
                    )
                    report["new_videos"].append(item)
                    continue
                video_dir = raw_dir / "youtube" / channel_slug / video_id
                video_dir.mkdir(parents=True, exist_ok=True)
                metadata_path = video_dir / "metadata.json"
                metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True))
                entry_path = video_dir / "playlist_entry.json"
                entry_path.write_text(json.dumps(entry, indent=2, sort_keys=True))
                subtitle_paths = fetch_subtitles(
                    video_id, video_dir / "subtitles", subtitle_languages
                )
                clean_transcript_paths = clean_subtitles(subtitle_paths)
                raw_paths = {
                    "metadata": str(metadata_path),
                    "playlist_entry": str(entry_path),
                    "subtitles": subtitle_paths,
                }
                if clean_transcript_paths:
                    raw_paths["clean_transcripts"] = clean_transcript_paths
                source_stub = write_source_stub(
                    vault_current_dir, channel_slug, video_id, metadata, raw_paths
                )
                channel_seen[video_id] = {
                    "first_seen_at": run_started,
                    "title": metadata.get("title") or entry.get("title"),
                    "url": metadata.get("webpage_url") or VIDEO_URL.format(video_id=video_id),
                    "metadata_path": str(metadata_path),
                    "clean_transcript_paths": clean_transcript_paths,
                    "source_stub": str(source_stub),
                }
                item.update(channel_seen[video_id])
                report["new_videos"].append(item)
            except Exception as exc:  # noqa: BLE001
                report["errors"].append({"channel": name, "video_id": video_id, "error": str(exc)})

    if not args.dry_run:
        state.setdefault("runs", []).append(
            {
                "started_at": run_started,
                "new_count": len(report["new_videos"]),
                "skipped_count": len(report.get("skipped_videos", [])),
                "error_count": len(report["errors"]),
            }
        )
        state["runs"] = state["runs"][-200:]
        save_state(state_path, state)

    fresh = sum(1 for v in report["new_videos"] if not v.get("retry"))
    retry = sum(1 for v in report["new_videos"] if v.get("retry"))
    if report["new_videos"]:
        report["message"] = (
            f"Extract signal notes from raw artifacts: {fresh} new video(s)"
            + (f", {retry} pending retry" if retry else "")
            + "."
        )
    elif report["errors"]:
        report["message"] = "No new videos fetched; errors occurred."
    else:
        report["message"] = "No new YouTube videos since last fetch."
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest YouTube trading videos into data/<agent>/{raw,vault}."
    )
    parser.add_argument(
        "--agent", default="social", help="Agent name → data/<agent>/ (default: social)"
    )
    parser.add_argument(
        "--data-dir", default=None, help="Override base data dir (default: <repo>/data)"
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Channels config YAML")
    parser.add_argument(
        "--max-videos", type=int, default=None, help="Override playlist_items per channel"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="List new videos without fetching/storing"
    )
    args = parser.parse_args()

    ensure_yt_dlp()
    config = load_config(Path(args.config).expanduser())
    paths = resolve_paths(args.agent, args.data_dir)
    report = fetch_youtube(args, config, paths)
    report["agent"] = args.agent
    report["config"] = str(Path(args.config).expanduser())
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
