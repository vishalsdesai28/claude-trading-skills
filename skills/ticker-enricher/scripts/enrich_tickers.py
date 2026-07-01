#!/usr/bin/env python3
"""ticker-enricher: enrich ticker signals with company metadata + prices.

Reads data/<agent>/vault/current/signals/index.json (from social-signal-ingestor),
and for each ticker looks up company name / sector / industry and the recommendation-date
+ current price (Yahoo Finance), and resolves the recommendation source (the YouTube channel
from the source note). Emits a records file (reports/enriched_records_<ts>.json) for a
downstream writer (e.g. write-supabase). Gain % and days held are derived by the UI from the
stored prices/dates — not computed or stored here.

Pure transport is intentionally NOT here — this skill only finds the metadata/price
details for tickers and shapes records. Persisting them is write-supabase's job.

# ponytail: yfinance is imported lazily inside the fetch fns so unit tests can
# monkeypatch them without the package installed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

SOURCE_SKILL = "social-signal-ingestor"
TABLE_HINT = "recommendations"
CONFLICT_HINT = "ticker,recommendation_source,date_recommended"


# ----------------------------- paths / loading -----------------------------


def resolve_paths(agent: str, data_dir: str | None) -> dict[str, Path]:
    root = Path(data_dir).expanduser() if data_dir else Path(__file__).resolve().parents[3] / "data"
    vault_current = root / agent / "vault" / "current"
    return {"vault_current": vault_current, "index": vault_current / "signals" / "index.json"}


def load_index(index_path: Path) -> dict[str, Any]:
    return json.loads(index_path.read_text())


# ----------------------------- pure transforms -----------------------------


def group_by_ticker_channel(
    index: dict[str, Any], vault_current: Path
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group signals by (ticker, channel) so each channel that recommends a ticker
    becomes its own row. Keys the stable Supabase conflict tuple
    (ticker, recommendation_source, date_recommended)."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for sig in index.get("signals", []):
        ticker = sig.get("ticker")
        if not isinstance(ticker, str) or not ticker.strip() or "/" in ticker or "," in ticker:
            continue  # skip blanks / multi-symbol (mirrors the ingestor's ticker_warning)
        t = ticker.strip().upper()
        for channel in channels_for_signal(sig, vault_current):
            groups.setdefault((t, channel), []).append(sig)
    return groups


def earliest_claim_date(sigs: list[dict[str, Any]]) -> str | None:
    dates = [s.get("claim_date") for s in sigs if isinstance(s.get("claim_date"), str)]
    return min(dates) if dates else None


def first_value(sigs: list[dict[str, Any]], key: str) -> str | None:
    """First non-empty value of `key` across the grouped signals."""
    for s in sigs:
        if s.get(key):
            return s[key]
    return None


def _channel_from_note(note_path: Path) -> str | None:
    """Pull `channel: <name>` from a source note's frontmatter (simple line scan).

    Falls back to glob by video_id when the exact date-prefixed path doesn't exist
    (signal notes hand-created with a different date than the yt-dlp upload_date).
    """
    candidates = [note_path]
    if not note_path.exists():
        # e.g. ref = "sources/youtube/2026-06-29_VID123" but stub is "2026-06-27_VID123"
        video_id = note_path.stem.split("_", 1)[-1]
        candidates = list(note_path.parent.glob(f"*_{video_id}.md"))
    for p in candidates:
        try:
            text = p.read_text()
        except OSError:
            continue
        in_fm = False
        for line in text.splitlines():
            if line.strip() == "---":
                if in_fm:
                    break
                in_fm = True
                continue
            if in_fm and line.startswith("channel:"):
                return line.split(":", 1)[1].strip() or None
    return None


def channels_for_signal(sig: dict[str, Any], vault_current: Path) -> list[str]:
    """Distinct channel names a single signal cites, via its source notes.
    Falls back to ["YouTube"] when none resolve."""
    chans: list[str] = []
    for ref in sig.get("sources", []) or []:
        ch = _channel_from_note(vault_current / f"{ref}.md")
        if ch and ch not in chans:
            chans.append(ch)
    return chans or ["YouTube"]


def map_to_record(
    ticker: str,
    channel: str,
    sigs: list[dict[str, Any]],
    profile: dict[str, Any],
    price_at_rec: float | None,
    current_price: float | None,
    now: dt.datetime,
) -> dict[str, Any]:
    date_rec = earliest_claim_date(sigs)
    return {
        "ticker": ticker,
        "company_name": profile.get("company_name"),
        "sector": profile.get("sector"),
        "industry": profile.get("industry"),
        "date_recommended": date_rec,
        "price_at_recommendation": price_at_rec,
        "current_price": current_price,
        "recommendation_source": channel,
        "source_type": "youtube",
        "source_skill": SOURCE_SKILL,
        "direction": first_value(sigs, "direction"),
        "instrument_type": first_value(sigs, "instrument") or "stock",
        "option_strategy": first_value(sigs, "option_strategy"),
        "option_legs": first_value(sigs, "option_legs"),  # jsonb list of legs; null for stock
        "net_premium": first_value(sigs, "net_premium"),  # net debit(+)/credit(-); null for stock
        "status": "active",
        "last_updated": now.isoformat(),
    }


# ----------------------------- enrichment I/O (monkeypatched in tests) -----------------------------


def fetch_profile(ticker: str) -> dict[str, Any]:
    import yfinance as yf  # lazy

    info = yf.Ticker(ticker).info or {}
    return {
        "company_name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
    }


def fetch_price_on(ticker: str, date_iso: str) -> float | None:
    import yfinance as yf  # lazy

    start = dt.date.fromisoformat(date_iso)
    end = start + dt.timedelta(days=5)  # window to catch the next trading day
    hist = yf.Ticker(ticker).history(start=start.isoformat(), end=end.isoformat(), auto_adjust=True)
    if hist is None or hist.empty:
        return None
    return round(float(hist["Close"].iloc[0]), 4)


def fetch_current_price(ticker: str) -> float | None:
    import yfinance as yf  # lazy

    hist = yf.Ticker(ticker).history(period="1d", auto_adjust=True)
    if hist is None or hist.empty:
        return None
    return round(float(hist["Close"].iloc[-1]), 4)


# ----------------------------- orchestration -----------------------------


def build_records(
    index: dict[str, Any], vault_current: Path, now: dt.datetime
) -> list[dict[str, Any]]:
    """One enriched record per (ticker, channel) (pure except the yfinance fetch fns)."""
    records = []
    profile_cache: dict[str, dict[str, Any]] = {}
    current_cache: dict[str, float | None] = {}
    for (ticker, channel), sigs in group_by_ticker_channel(index, vault_current).items():
        date_rec = earliest_claim_date(sigs)
        if ticker not in profile_cache:  # a ticker spans multiple channel rows — fetch once
            profile_cache[ticker] = fetch_profile(ticker)
            current_cache[ticker] = fetch_current_price(ticker)
        profile, current = profile_cache[ticker], current_cache[ticker]
        # Fall back to the current close when the recommendation-date close isn't available
        # (e.g. a future-dated upload_date from yt-dlp timezone skew, or a non-trading day),
        # so the baseline is never null — a just-posted call's baseline ≈ today's price.
        price_at_rec = (fetch_price_on(ticker, date_rec) if date_rec else None) or current
        records.append(map_to_record(ticker, channel, sigs, profile, price_at_rec, current, now))
    return records


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enrich ticker signals with metadata + prices into a records file."
    )
    parser.add_argument(
        "--agent", default="social", help="Agent name → data/<agent>/ (default: social)"
    )
    parser.add_argument(
        "--data-dir", default=None, help="Override base data dir (default: <repo>/data)"
    )
    parser.add_argument(
        "--output-dir", default="reports/", help="Output directory (default: reports/)"
    )
    parser.add_argument(
        "--output-prefix", default="enriched_records", help="Output filename prefix"
    )
    args = parser.parse_args()

    paths = resolve_paths(args.agent, args.data_dir)
    if not paths["index"].exists():
        raise SystemExit(
            f"Signal index not found: {paths['index']} (run social-signal-ingestor first)"
        )
    now = dt.datetime.now(dt.timezone.utc)
    records = build_records(load_index(paths["index"]), paths["vault_current"], now)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.output_prefix}_{now.strftime('%Y-%m-%d_%H%M%S')}.json"
    out_path.write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "table_hint": TABLE_HINT,
                "conflict_hint": CONFLICT_HINT,
                "records": records,
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    print(
        json.dumps(
            {
                "message": "tickers enriched",
                "path": str(out_path),
                "record_count": len(records),
                "tickers": [r["ticker"] for r in records],
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
