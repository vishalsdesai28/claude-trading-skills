#!/usr/bin/env python3
"""edge-social-aggregator: social-specific scoring of YouTube/X/Reddit signals.

Reads data/<agent>/vault/current/signals/index.json (produced by
social-signal-ingestor's build_signal_index.py), applies objective social
scoring — recency × corroboration (how many independent sources name the
ticker) — deduplicates the same ticker across multiple videos, and emits
reports/edge_social_aggregator_<ts>.json in the shape edge-signal-aggregator's
--social-signals parser consumes.

Deliberately does NOT redo cross-source merge / contradiction / ranking — that
stays in edge-signal-aggregator, which blends this in as ONE low-weight source.
Channels are pre-vetted before they enter channels.yaml, so there is no
per-channel credibility weighting and no per-signal confidence — conviction is
driven purely by recency and how many sources independently surface a ticker.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any


def resolve_index_path(agent: str, data_dir: str | None) -> Path:
    root = Path(data_dir).expanduser() if data_dir else Path(__file__).resolve().parents[3] / "data"
    return root / agent / "vault" / "current" / "signals" / "index.json"


def recency_factor(claim_date: Any, now: dt.datetime) -> float:
    """Mirror edge-signal-aggregator's recency buckets. Conservative on unknown dates."""
    if not isinstance(claim_date, str):
        return 0.85
    try:
        d = dt.datetime.strptime(claim_date[:10], "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return 0.85
    days = (now - d).days
    if days <= 1:
        return 1.00
    if days <= 3:
        return 0.95
    if days <= 7:
        return 0.90
    return 0.85


def source_factor(n_sources: int) -> float:
    """Corroboration: more independent sources → higher, capped at 1.0.
    1→0.6, 2→0.7, 3→0.8, 5+→1.0. This is the main driver now that there is no
    per-signal confidence — a ticker named by several sources outranks a lone one."""
    return min(1.0, 0.5 + 0.1 * max(n_sources, 1))


def score_one(sig: dict[str, Any], now: dt.datetime) -> float:
    """social_conviction (0-1) = recency_factor × source_factor."""
    n_sources = len(sig.get("sources") or []) or 1
    conv = recency_factor(sig.get("claim_date") or sig.get("updated"), now) * source_factor(
        n_sources
    )
    return round(max(0.0, min(1.0, conv)), 4)


def aggregate(index: dict[str, Any], now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    by_ticker: dict[str, dict[str, Any]] = {}
    skipped = 0
    for sig in index.get("signals", []):
        ticker = sig.get("ticker")
        if not isinstance(ticker, str) or not ticker.strip() or "/" in ticker or "," in ticker:
            skipped += 1  # no real single instrument — skip (mirrors ingestor's ticker_warning)
            continue
        ticker = ticker.strip().upper()
        conv = score_one(sig, now)
        sources = sig.get("sources") or []
        rec = by_ticker.setdefault(
            ticker,
            {
                "ticker": ticker,
                "title": sig.get("title"),
                "direction": sig.get("direction"),
                "social_conviction": 0.0,
                "_source_set": set(),
                "time_horizon": sig.get("time_horizon"),
                "timestamp": sig.get("claim_date") or sig.get("updated"),
            },
        )
        rec["_source_set"].update(sources)
        if conv > rec["social_conviction"]:  # keep the strongest mention's framing
            rec["social_conviction"] = conv
            rec["title"] = sig.get("title")
            rec["direction"] = sig.get("direction")
            rec["time_horizon"] = sig.get("time_horizon")

    signals = []
    for rec in by_ticker.values():
        srcs = sorted(rec.pop("_source_set"))
        rec["n_sources"] = len(srcs)
        rec["top_sources"] = srcs[:5]
        signals.append(rec)
    signals.sort(key=lambda s: s["social_conviction"], reverse=True)

    return {
        "schema_version": "1.0",
        "generated_at": now.isoformat(),
        "source_skill": "edge_social_aggregator",
        "week": index.get("week"),
        "signal_count": len(signals),
        "skipped_invalid_ticker": skipped,
        "signals": signals,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score social signals into a feed for edge-signal-aggregator."
    )
    parser.add_argument(
        "--agent", default="social", help="Agent name → data/<agent>/ (default: social)"
    )
    parser.add_argument(
        "--data-dir", default=None, help="Override base data dir (default: <repo>/data)"
    )
    parser.add_argument(
        "--index", default=None, help="Explicit path to signals/index.json (overrides --agent)"
    )
    parser.add_argument(
        "--output-dir", default="reports/", help="Output directory (default: reports/)"
    )
    parser.add_argument(
        "--output-prefix", default="edge_social_aggregator", help="Output filename prefix"
    )
    args = parser.parse_args()

    index_path = (
        Path(args.index).expanduser()
        if args.index
        else resolve_index_path(args.agent, args.data_dir)
    )
    if not index_path.exists():
        raise SystemExit(
            f"Signal index not found: {index_path} (run social-signal-ingestor build_signal_index.py first)"
        )
    index = json.loads(index_path.read_text())
    result = aggregate(index)
    result["agent"] = args.agent

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_path = out_dir / f"{args.output_prefix}_{ts}.json"
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True, default=str))
    print(
        json.dumps(
            {
                "message": "social signals aggregated",
                "path": str(out_path),
                "signal_count": result["signal_count"],
                "skipped_invalid_ticker": result["skipped_invalid_ticker"],
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
