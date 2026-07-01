#!/usr/bin/env python3
"""Build a machine-readable index of this week's social signal notes.

Deterministically scans data/<agent>/vault/current/signals/*.md frontmatter and
writes two files, both AFTER the extraction step has written/updated signal notes:

- signals/index.json — the full week's signals (cumulative, since notes accumulate
  in the vault until the weekly reset). Consumed by edge-social-aggregator and for tracking.
- signals/index_last.json — only the records new since the previous index.json (this run's
  delta), overwritten each run. Consumed by robinhood-trade-executor so it buys only the
  fresh picks, not the whole week's accumulation.

`direction` is included so the aggregator can read it without re-parsing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any


def resolve_signals_dir(agent: str, data_dir: str | None) -> Path:
    root = Path(data_dir).expanduser() if data_dir else Path(__file__).resolve().parents[3] / "data"
    return root / agent / "vault" / "current" / "signals"


class FrontmatterError(Exception):
    pass


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse a note's YAML frontmatter. Raises FrontmatterError on malformed input
    so one bad note doesn't break the whole index build."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(block)
    except ImportError:
        return {}
    except yaml.YAMLError as exc:
        raise FrontmatterError(str(exc)) from exc
    return data if isinstance(data, dict) else {}


def week_id(now: dt.datetime) -> str:
    return now.strftime("%G-W%V")


def normalize_sources(value: Any) -> list[str]:
    """Flatten the Obsidian [[wikilink]] YAML nesting quirk into plain strings."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        while isinstance(item, list) and len(item) == 1:
            item = item[0]
        if item is not None:
            out.append(item if isinstance(item, str) else str(item))
    return out


def ticker_warning(ticker: Any) -> str | None:
    """Flag tickers that a downstream live-quote call would choke on."""
    if not ticker:
        return None
    if not isinstance(ticker, str):
        return f"ticker is not a string: {ticker!r}"
    if "/" in ticker or "," in ticker:
        return f"ticker {ticker!r} looks like multiple symbols joined; split into one signal note per ticker"
    return None


_LEG_SIDES = {"buy", "sell"}
_LEG_RIGHTS = {"call", "put"}


def option_warning(fm: dict[str, Any]) -> str | None:
    """Flag option notes whose legs a downstream consumer can't use. Warnings, not
    failures — the signal still indexes, the operator just gets told it's malformed."""
    legs = fm.get("option_legs")
    if fm.get("instrument") != "option":
        # Option detail on a note not marked as an option → likely mislabeled.
        if legs or fm.get("option_strategy") or fm.get("net_premium") is not None:
            return "option fields set but instrument is not 'option'"
        return None
    if not isinstance(legs, list) or not legs:
        return "instrument is 'option' but option_legs is missing or empty"
    for i, leg in enumerate(legs):
        if not isinstance(leg, dict):
            return f"option_legs[{i}] is not a mapping"
        if leg.get("side") not in _LEG_SIDES:
            return f"option_legs[{i}].side must be buy/sell, got {leg.get('side')!r}"
        if leg.get("right") not in _LEG_RIGHTS:
            return f"option_legs[{i}].right must be call/put, got {leg.get('right')!r}"
        if not isinstance(leg.get("strike"), (int, float)) or isinstance(leg.get("strike"), bool):
            return f"option_legs[{i}].strike must be numeric, got {leg.get('strike')!r}"
        if not leg.get("expiry"):
            return f"option_legs[{i}] missing expiry"
    return None


def compact_signal(fm: dict[str, Any], path: Path, signals_dir: Path) -> dict[str, Any]:
    signal = {
        "path": str(path.relative_to(signals_dir)),
        "title": fm.get("title"),
        "ticker": fm.get("ticker"),
        "direction": fm.get("direction"),
        "status": fm.get("status"),
        "probability": fm.get("probability"),
        "time_horizon": fm.get("time_horizon"),
        "claim_date": fm.get("claim_date"),
        "updated": fm.get("updated"),
        "watch": fm.get("watch"),
        "instrument": fm.get("instrument"),          # stock | option (default stock downstream)
        "option_strategy": fm.get("option_strategy"),  # e.g. long_call, covered_call (option only)
        "option_legs": fm.get("option_legs"),         # list of {side,right,strike,expiry,ratio} (option only)
        "net_premium": fm.get("net_premium"),         # net debit(+)/credit(-) at recommendation (option only)
        "sources": normalize_sources(fm.get("sources")),
    }
    return signal


def build_index(signals_dir: Path, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    signals_dir.mkdir(parents=True, exist_ok=True)
    signals: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    ticker_warnings: list[dict[str, Any]] = []
    option_warnings: list[dict[str, Any]] = []
    for path in sorted(signals_dir.glob("*.md")):
        try:
            fm = parse_frontmatter(path.read_text())
        except FrontmatterError as exc:
            parse_errors.append({"path": path.name, "error": str(exc)})
            continue
        if not fm or fm.get("type") not in (None, "signal"):
            continue
        warning = ticker_warning(fm.get("ticker"))
        if warning:
            ticker_warnings.append({"path": path.name, "warning": warning})
        ow = option_warning(fm)
        if ow:
            option_warnings.append({"path": path.name, "warning": ow})
        signals.append(compact_signal(fm, path, signals_dir))
    return {
        "generated_at": now.isoformat(),
        "week": week_id(now),
        "signal_count": len(signals),
        "signals": signals,
        "parse_errors": parse_errors,
        "ticker_warnings": ticker_warnings,
        "option_warnings": option_warnings,
    }


def load_prev_signal_paths(index_path: Path) -> set[str]:
    """Note paths recorded in an existing index.json (empty if absent/unreadable)."""
    if not index_path.exists():
        return set()
    try:
        prev = json.loads(index_path.read_text())
    except (json.JSONDecodeError, OSError):
        return set()  # ponytail: unreadable prior index → treat everything as new this run
    return {s.get("path") for s in prev.get("signals", []) if isinstance(s, dict)}


def build_last_index(index: dict[str, Any], prev_paths: set[str]) -> dict[str, Any]:
    """Subset of `index` holding only records whose note wasn't in the previous index —
    i.e. what this run newly added. Same schema; overwritten each run for the executor."""

    def is_new(entry: dict[str, Any]) -> bool:
        return entry.get("path") not in prev_paths

    signals = [s for s in index["signals"] if is_new(s)]
    return {
        **index,
        "signal_count": len(signals),
        "signals": signals,
        "parse_errors": [e for e in index["parse_errors"] if is_new(e)],
        "ticker_warnings": [w for w in index["ticker_warnings"] if is_new(w)],
        "option_warnings": [w for w in index["option_warnings"] if is_new(w)],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build signals/index.json from vault signal notes."
    )
    parser.add_argument(
        "--agent", default="social", help="Agent name → data/<agent>/ (default: social)"
    )
    parser.add_argument(
        "--data-dir", default=None, help="Override base data dir (default: <repo>/data)"
    )
    args = parser.parse_args()

    signals_dir = resolve_signals_dir(args.agent, args.data_dir)
    out_path = signals_dir / "index.json"
    # Snapshot the prior index before overwriting, so index_last.json can carry just this run's delta.
    prev_paths = load_prev_signal_paths(out_path)

    index = build_index(signals_dir)
    out_path.write_text(json.dumps(index, indent=2, sort_keys=True, default=str))

    last = build_last_index(index, prev_paths)
    last_path = signals_dir / "index_last.json"
    last_path.write_text(json.dumps(last, indent=2, sort_keys=True, default=str))

    print(
        json.dumps(
            {
                "message": "signal index built",
                "path": str(out_path),
                "last_path": str(last_path),
                "signal_count": index["signal_count"],
                "added_count": last["signal_count"],
                "parse_error_count": len(index["parse_errors"]),
                "parse_errors": index["parse_errors"],
                "ticker_warning_count": len(index["ticker_warnings"]),
                "ticker_warnings": index["ticker_warnings"],
                "option_warning_count": len(index["option_warnings"]),
                "option_warnings": index["option_warnings"],
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
