"""Short-squeeze radar — rank US equities by short-squeeze potential from FREE FINRA data.

Two free, no-auth FINRA inputs drive the scan:

1. Daily Reg SHO consolidated short-sale volume files (the raw input behind every
   "short volume %" product). Published every trading day, pipe-delimited, no auth:
       https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt
   Columns: Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market
   short_volume_ratio = (ShortVolume + ShortExemptVolume) / TotalVolume. This is a
   daily EXECUTED short-flow proxy — a rising ratio means shorts are piling in.

2. Bi-monthly Consolidated Short Interest (shares reported short). Fields:
   short_interest, avg_daily_volume, days_to_cover (= short_interest / avg_daily_volume).
   Days-to-cover is the classic squeeze pressure gauge. Supplied via a local
   CSV/JSON file (exported from FINRA / NASDAQ / a broker) so the skill stays free
   and offline-testable; no short-interest URL is hardcoded.

The module is split into PURE compute/parse functions (unit-tested against saved
FINRA fixtures, no network) and thin, lazily-imported, TTL-cached fetch helpers.
Nothing here trades; it produces a ranked squeeze watchlist (markdown + JSON).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── FINRA source + tunable thresholds ────────────────────────────────────────

FINRA_DAILY_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{date}.txt"

# FINRA consolidated short volume routinely runs ~40-50% market-wide, so the
# squeeze-relevant band is the UPPER tail of the short-volume ratio.
RATIO_CROWDED = 0.60  # >= this = heavy short-side flow (crowded)
RATIO_LIGHT = 0.35  # <= this = little short pressure
# Days-to-cover: shares short / average daily volume. Higher = harder to cover.
DTC_HIGH = 5.0  # >= this = a lot of buying required to unwind shorts
DTC_LOW = 2.0  # < this = shorts can exit in a day or two
TREND_EPS = 0.03  # ratio move over the window needed to call a trend

# Fetch cache: the daily file is immutable once published, so a long TTL is safe.
CACHE_TTL_SECONDS = 6 * 3600.0
_USER_AGENT = "short-squeeze-radar/1.0 (+claude-trading-skills)"


# ── data model ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ShortVolDay:
    """One symbol's row from one FINRA daily Reg SHO file."""

    date: str  # YYYYMMDD
    symbol: str
    short_vol: float
    short_exempt: float
    total_vol: float

    @property
    def ratio(self) -> float:
        if self.total_vol <= 0:
            return 0.0
        return (self.short_vol + self.short_exempt) / self.total_vol


@dataclass(frozen=True)
class ShortInterest:
    """One symbol's bi-monthly short-interest snapshot."""

    symbol: str
    settlement_date: str
    short_interest: float
    avg_daily_volume: float
    days_to_cover: float | None


@dataclass
class SqueezeCandidate:
    """A ranked short-squeeze candidate."""

    symbol: str
    latest_date: str
    latest_ratio: float
    ratio_series: list[float]
    ratio_trend: str  # rising | falling | flat | n/a
    rising_inflection: bool
    days_to_cover: float | None
    short_interest: float | None
    avg_daily_volume: float | None
    classification: str  # crowded_short | neutral | low_pressure
    squeeze_score: float
    squeeze_primed: bool
    fallback_used: bool
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "latest_date": self.latest_date,
            "latest_ratio": self.latest_ratio,
            "ratio_series": self.ratio_series,
            "ratio_trend": self.ratio_trend,
            "rising_inflection": self.rising_inflection,
            "days_to_cover": self.days_to_cover,
            "short_interest": self.short_interest,
            "avg_daily_volume": self.avg_daily_volume,
            "classification": self.classification,
            "squeeze_score": self.squeeze_score,
            "squeeze_primed": self.squeeze_primed,
            "fallback_used": self.fallback_used,
            "notes": self.notes,
        }


# ── PURE parsing ─────────────────────────────────────────────────────────────


def parse_finra_shvol(text: str, want_symbols: set[str] | None = None) -> list[ShortVolDay]:
    """Parse one FINRA CNMS daily short-volume file (pipe-delimited).

    Skips the header line, blank lines, and the trailer ("Records: N"). If
    ``want_symbols`` is given, keep only those tickers (case-insensitive).
    """
    want = {s.upper() for s in want_symbols} if want_symbols else None
    rows: list[ShortVolDay] = []
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) < 5:
            continue  # blank / trailer ("Records: N")
        date, sym = parts[0].strip(), parts[1].strip().upper()
        if not date.isdigit():
            continue  # header row or footer text
        if want is not None and sym not in want:
            continue
        try:
            rows.append(
                ShortVolDay(
                    date=date,
                    symbol=sym,
                    short_vol=float(parts[2] or 0),
                    short_exempt=float(parts[3] or 0),
                    total_vol=float(parts[4] or 0),
                )
            )
        except (ValueError, IndexError):
            continue
    return rows


def parse_short_interest(records: list[dict]) -> dict[str, ShortInterest]:
    """Build a {SYMBOL: ShortInterest} map from short-interest record dicts.

    Accepts the FINRA/vendor field names (ticker/symbol, short_interest,
    avg_daily_volume, days_to_cover, settlement_date). ``days_to_cover`` is
    computed as short_interest / avg_daily_volume when not supplied. The most
    recent settlement_date per symbol wins.
    """
    out: dict[str, ShortInterest] = {}
    for rec in records:
        sym = str(rec.get("ticker") or rec.get("symbol") or "").strip().upper()
        if not sym:
            continue
        si = _to_float(rec.get("short_interest"))
        adv = _to_float(rec.get("avg_daily_volume"))
        if si is None or adv is None:
            continue
        dtc = _to_float(rec.get("days_to_cover"))
        if dtc is None and adv > 0:
            dtc = round(si / adv, 2)
        settlement = str(rec.get("settlement_date") or "").strip()
        entry = ShortInterest(
            symbol=sym,
            settlement_date=settlement,
            short_interest=si,
            avg_daily_volume=adv,
            days_to_cover=dtc,
        )
        prev = out.get(sym)
        if prev is None or settlement >= prev.settlement_date:
            out[sym] = entry
    return out


def parse_short_interest_csv(text: str) -> dict[str, ShortInterest]:
    """Parse a short-interest CSV (header row required) into a ShortInterest map."""
    reader = csv.DictReader(io.StringIO(text))
    return parse_short_interest(list(reader))


def _to_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── PURE analytics ───────────────────────────────────────────────────────────


def index_by_symbol(days: list[ShortVolDay]) -> dict[str, list[ShortVolDay]]:
    """Group parsed rows by symbol, each list sorted oldest -> newest by date."""
    grouped: dict[str, list[ShortVolDay]] = {}
    for d in days:
        grouped.setdefault(d.symbol, []).append(d)
    for sym in grouped:
        grouped[sym].sort(key=lambda d: d.date)
    return grouped


def latest_date_in(days: list[ShortVolDay]) -> str:
    """Return the most recent trading date present across all rows."""
    return max((d.date for d in days), default="")


def classify_ratio_trend(values: list[float]) -> tuple[str, bool]:
    """Classify the multi-day short-volume-ratio series.

    Returns (trend, rising_inflection) where trend is rising/falling/flat/n/a.
    rising_inflection = the series is rising AND the latest value is a new high
    above the prior day (shorts freshly piling in).
    """
    if len(values) < 2:
        return "n/a", False
    delta = round(values[-1] - values[0], 6)  # kill float noise at the EPS boundary
    if delta > TREND_EPS:
        trend = "rising"
    elif delta < -TREND_EPS:
        trend = "falling"
    else:
        trend = "flat"
    inflection = trend == "rising" and values[-1] >= max(values) and values[-1] > values[-2]
    return trend, inflection


def classify_squeeze(ratio: float, days_to_cover: float | None) -> str:
    """Classify short-side pressure into crowded_short / neutral / low_pressure."""
    if ratio >= RATIO_CROWDED or (days_to_cover is not None and days_to_cover >= DTC_HIGH):
        return "crowded_short"
    if ratio <= RATIO_LIGHT and (days_to_cover is None or days_to_cover < DTC_LOW):
        return "low_pressure"
    return "neutral"


def is_squeeze_primed(
    classification: str, trend: str, days_to_cover: float | None, ratio: float
) -> bool:
    """A crowded short that is still building (rising flow) and hard to unwind."""
    if classification != "crowded_short" or trend != "rising":
        return False
    return (days_to_cover is not None and days_to_cover >= 3.0) or ratio >= 0.65


def compute_squeeze_score(ratio: float, days_to_cover: float | None, trend: str) -> float:
    """Composite 0-100 squeeze score: ratio (40) + days-to-cover (40) + trend (20).

    With no short-interest data the days-to-cover component is 0, so the score
    tops out at 60 and the candidate is flagged accordingly.
    """
    ratio_pts = min(ratio / 0.75, 1.0) * 40.0
    dtc_pts = min(days_to_cover / 10.0, 1.0) * 40.0 if days_to_cover is not None else 0.0
    trend_pts = {"rising": 20.0, "flat": 8.0, "falling": 2.0, "n/a": 4.0}.get(trend, 4.0)
    return round(ratio_pts + dtc_pts + trend_pts, 1)


def build_candidate(
    symbol: str,
    sv_days: list[ShortVolDay],
    short_interest: ShortInterest | None,
    window_latest_date: str,
) -> SqueezeCandidate:
    """Assemble a ranked candidate from one symbol's short-volume history + SI."""
    ordered = sorted(sv_days, key=lambda d: d.date)
    series = [round(d.ratio, 4) for d in ordered]
    latest = ordered[-1]
    trend, inflection = classify_ratio_trend(series)

    dtc = short_interest.days_to_cover if short_interest else None
    si_shares = short_interest.short_interest if short_interest else None
    adv = short_interest.avg_daily_volume if short_interest else None

    classification = classify_squeeze(latest.ratio, dtc)
    primed = is_squeeze_primed(classification, trend, dtc, latest.ratio)
    score = compute_squeeze_score(latest.ratio, dtc, trend)
    fallback_used = bool(window_latest_date) and latest.date < window_latest_date

    notes: list[str] = []
    if classification == "crowded_short":
        notes.append("crowded short — squeeze fuel for a long")
    elif classification == "low_pressure":
        notes.append("little short pressure")
    if dtc is not None:
        notes.append(f"days-to-cover {dtc}")
    else:
        notes.append("no short-interest data (score capped at 60)")
    if trend == "rising":
        notes.append("short-volume ratio rising (shorts piling in)")
    if fallback_used:
        notes.append(
            f"latest file {window_latest_date} missing this symbol; "
            f"using prior trading day {latest.date}"
        )

    return SqueezeCandidate(
        symbol=symbol,
        latest_date=latest.date,
        latest_ratio=round(latest.ratio, 4),
        ratio_series=series,
        ratio_trend=trend,
        rising_inflection=inflection,
        days_to_cover=dtc,
        short_interest=si_shares,
        avg_daily_volume=adv,
        classification=classification,
        squeeze_score=score,
        squeeze_primed=primed,
        fallback_used=fallback_used,
        notes=notes,
    )


def rank_candidates(candidates: list[SqueezeCandidate]) -> list[SqueezeCandidate]:
    """Rank by days-to-cover (desc, unknown last), then squeeze score, then ratio."""
    return sorted(
        candidates,
        key=lambda c: (
            c.days_to_cover if c.days_to_cover is not None else -1.0,
            c.squeeze_score,
            c.latest_ratio,
        ),
        reverse=True,
    )


def scan(
    symbols: list[str],
    days: list[ShortVolDay],
    short_interest: dict[str, ShortInterest],
) -> list[SqueezeCandidate]:
    """Build and rank squeeze candidates for the requested symbols.

    Symbols with no short-volume data anywhere in the window are skipped (their
    absence is surfaced separately by the caller).
    """
    grouped = index_by_symbol(days)
    window_latest = latest_date_in(days)
    candidates: list[SqueezeCandidate] = []
    for sym in symbols:
        sym_u = sym.upper()
        sym_days = grouped.get(sym_u)
        if not sym_days:
            continue
        candidates.append(
            build_candidate(sym_u, sym_days, short_interest.get(sym_u), window_latest)
        )
    return rank_candidates(candidates)


# ── input loading (local files — no network) ─────────────────────────────────


def load_symbols(watchlist_path: str | None, tickers: str | None) -> list[str]:
    """Load the target symbol list from a watchlist file and/or a --tickers string.

    Watchlist file may be a JSON array, a JSON object with a "tickers"/"symbols"
    key, or a plain newline/comma-delimited text file (# comments allowed).
    """
    out: list[str] = []
    if watchlist_path:
        raw = Path(watchlist_path).read_text(encoding="utf-8")
        out.extend(_parse_symbol_text(raw))
    if tickers:
        out.extend(_split_tickers(tickers))
    # De-dupe preserving order, uppercased.
    seen: set[str] = set()
    result: list[str] = []
    for s in out:
        s = s.strip().upper()
        if s and s not in seen:
            seen.add(s)
            result.append(s)
    return result


def _parse_symbol_text(raw: str) -> list[str]:
    stripped = raw.strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        data = json.loads(stripped)
        if isinstance(data, dict):
            data = data.get("tickers") or data.get("symbols") or []
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
        return []
    out: list[str] = []
    for line in stripped.splitlines():
        line = line.split("#", 1)[0]
        out.extend(_split_tickers(line))
    return out


def _split_tickers(text: str) -> list[str]:
    return [t.strip() for t in text.replace(",", " ").split() if t.strip()]


def load_short_interest_file(path: str) -> dict[str, ShortInterest]:
    """Load a local short-interest file (.json or .csv) into a ShortInterest map."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".csv":
        return parse_short_interest_csv(text)
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("results") or data.get("data") or []
    if not isinstance(data, list):
        raise ValueError("short-interest JSON must be a list or have a 'results' list")
    return parse_short_interest(data)


# ── thin, lazily-imported, TTL-cached fetch ──────────────────────────────────


def _cache_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "short_squeeze_radar_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_read(key: str, ttl: float) -> str | None:
    p = _cache_dir() / key
    if p.exists() and (time.time() - p.stat().st_mtime) < ttl:
        return p.read_text(encoding="utf-8")
    return None


def _cache_write(key: str, text: str) -> None:
    (_cache_dir() / key).write_text(text, encoding="utf-8")


def fetch_finra_day(
    date_str: str,
    timeout: float = 15.0,
    ttl: float = CACHE_TTL_SECONDS,
    allow_fetch: bool = True,
) -> str | None:
    """Fetch one FINRA daily short-volume file (TTL-cached). Returns text or None.

    Network access is lazily imported and never exercised by the unit tests.
    A missing file (weekend/holiday/not-yet-published) 404s and returns None.
    """
    key = f"CNMSshvol{date_str}.txt"
    cached = _cache_read(key, ttl)
    if cached is not None:
        return cached
    if not allow_fetch:
        return None
    import urllib.request  # lazy: keep pure functions stdlib-only importable

    url = FINRA_DAILY_URL.format(date=date_str)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception as e:  # network error / 404 for a non-trading day
        print(f"Warning: could not fetch {url}: {e}", file=sys.stderr)
        return None
    _cache_write(key, text)
    return text


def fetch_short_volume_window(
    symbols: list[str],
    lookback_days: int = 5,
    as_of: datetime | None = None,
    timeout: float = 15.0,
    ttl: float = CACHE_TTL_SECONDS,
) -> list[ShortVolDay]:
    """Fetch and parse the last ``lookback_days`` trading days of short volume.

    Walks back from ``as_of`` (default: today UTC), skipping weekends; missing
    files just 404 and are skipped. Filters to the requested symbols on parse.
    """
    want = {s.upper() for s in symbols}
    rows: list[ShortVolDay] = []
    d = (as_of or datetime.now(timezone.utc)).date()
    collected_days = 0
    checked = 0
    while collected_days < lookback_days and checked < lookback_days + 7:
        if d.weekday() < 5:  # Mon-Fri only
            text = fetch_finra_day(d.strftime("%Y%m%d"), timeout=timeout, ttl=ttl)
            if text:
                rows.extend(parse_finra_shvol(text, want_symbols=want))
                collected_days += 1
        d -= timedelta(days=1)
        checked += 1
    return rows


# ── reporting ────────────────────────────────────────────────────────────────


def build_result(
    candidates: list[SqueezeCandidate],
    requested_symbols: list[str],
    lookback_days: int,
    short_interest_source: str | None,
) -> dict:
    """Assemble the JSON result payload."""
    found = {c.symbol for c in candidates}
    missing = [s for s in requested_symbols if s.upper() not in found]
    primed = [c.symbol for c in candidates if c.squeeze_primed]
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "FINRA Reg SHO daily short-volume (free, no auth)",
        "lookback_days": lookback_days,
        "short_interest_source": short_interest_source,
        "requested_symbols": [s.upper() for s in requested_symbols],
        "symbols_without_data": missing,
        "squeeze_primed": primed,
        "candidates": [c.to_dict() for c in candidates],
    }


def generate_markdown_report(result: dict) -> str:
    """Render the ranked squeeze watchlist as markdown."""
    lines = [
        "# Short-Squeeze Radar",
        f"**Generated:** {result['generated_at']}",
        f"**Source:** {result['source']}",
        f"**Lookback:** {result['lookback_days']} trading days",
    ]
    if result.get("short_interest_source"):
        lines.append(f"**Short interest:** {result['short_interest_source']}")
    else:
        lines.append("**Short interest:** not provided (scores capped at 60)")
    primed = result.get("squeeze_primed") or []
    lines.append(f"**Squeeze-primed names:** {', '.join(primed) if primed else 'none'}")
    lines.append("")

    lines.append("## Ranked Candidates")
    lines.append("")
    lines.append(
        "| Rank | Symbol | Class | Score | Days-to-Cover | Short-Vol Ratio | Trend | Primed |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for i, c in enumerate(result["candidates"], start=1):
        dtc = "n/a" if c["days_to_cover"] is None else f"{c['days_to_cover']}"
        primed_flag = "YES" if c["squeeze_primed"] else ""
        lines.append(
            "| {} | {} | {} | {} | {} | {:.2f} | {} | {} |".format(
                i,
                c["symbol"],
                c["classification"],
                c["squeeze_score"],
                dtc,
                c["latest_ratio"],
                c["ratio_trend"],
                primed_flag,
            )
        )
    lines.append("")

    lines.append("## Detail")
    lines.append("")
    for c in result["candidates"]:
        lines.append(f"### {c['symbol']} — {c['classification']}")
        series = ", ".join(f"{v:.2f}" for v in c["ratio_series"])
        lines.append(f"- Short-volume ratio series (oldest→newest): {series}")
        lines.append(f"- Latest ratio: {c['latest_ratio']:.2f} (as of {c['latest_date']})")
        if c["days_to_cover"] is not None:
            lines.append(
                f"- Short interest: {int(c['short_interest']):,} shares; "
                f"days-to-cover {c['days_to_cover']}"
            )
        for note in c["notes"]:
            lines.append(f"- {note}")
        lines.append("")

    if result.get("symbols_without_data"):
        lines.append("## No FINRA Data In Window")
        lines.append("")
        lines.append(", ".join(result["symbols_without_data"]))
        lines.append("")

    lines.append(
        "> Short volume is off-exchange EXECUTED short flow (a proxy), not reported "
        "short interest. High ratio = crowded short = squeeze fuel for a long, but "
        "confirm with price/catalyst before acting."
    )
    return "\n".join(lines) + "\n"


def write_reports(result: dict, output_dir: str) -> tuple[str, str]:
    """Write JSON + markdown reports; return (json_path, md_path)."""
    os.makedirs(output_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    json_path = os.path.join(output_dir, f"short_squeeze_{stamp}.json")
    md_path = os.path.join(output_dir, f"short_squeeze_{stamp}.md")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(generate_markdown_report(result))
    return json_path, md_path


# ── CLI ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rank US equities by short-squeeze potential from FREE FINRA data."
    )
    parser.add_argument("--watchlist", type=str, help="Watchlist file (JSON array or text)")
    parser.add_argument("--tickers", type=str, help="Comma/space-separated tickers")
    parser.add_argument(
        "--short-interest-file",
        type=str,
        help="Local short-interest file (.json or .csv) for days-to-cover ranking",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=5,
        help="Trading days of short-volume history to fetch (default: 5)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="reports/",
        help="Output directory for reports (default: reports/)",
    )
    parser.add_argument(
        "--cache-ttl",
        type=float,
        default=CACHE_TTL_SECONDS,
        help="Fetch cache TTL in seconds (default: 21600)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.watchlist and not args.tickers:
        print("Error: provide --watchlist FILE and/or --tickers SYM,SYM", file=sys.stderr)
        return 1
    if args.lookback_days < 1:
        print("Error: --lookback-days must be >= 1", file=sys.stderr)
        return 1

    try:
        symbols = load_symbols(args.watchlist, args.tickers)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"Error: could not read watchlist: {e}", file=sys.stderr)
        return 1
    if not symbols:
        print("Error: no symbols resolved from watchlist/tickers", file=sys.stderr)
        return 1

    si_map: dict[str, ShortInterest] = {}
    si_source: str | None = None
    if args.short_interest_file:
        try:
            si_map = load_short_interest_file(args.short_interest_file)
            si_source = os.path.basename(args.short_interest_file)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"Error: could not read short-interest file: {e}", file=sys.stderr)
            return 1

    days = fetch_short_volume_window(symbols, lookback_days=args.lookback_days, ttl=args.cache_ttl)
    if not days:
        print(
            "Error: no FINRA short-volume data fetched. Check connectivity or try again "
            "after ~18:00 ET when the daily file publishes.",
            file=sys.stderr,
        )
        return 1

    candidates = scan(symbols, days, si_map)
    result = build_result(candidates, symbols, args.lookback_days, si_source)

    json_path, md_path = write_reports(result, args.output_dir)
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")

    primed = result["squeeze_primed"]
    print(f"\nScanned {len(symbols)} symbols; {len(candidates)} with FINRA data.")
    if primed:
        print(f"Squeeze-primed: {', '.join(primed)}")
    for c in result["candidates"][:10]:
        dtc = "n/a" if c["days_to_cover"] is None else f"{c['days_to_cover']}"
        print(
            f"  {c['symbol']:<6} {c['classification']:<14} "
            f"score={c['squeeze_score']:<5} dtc={dtc:<6} ratio={c['latest_ratio']:.2f} "
            f"{c['ratio_trend']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
