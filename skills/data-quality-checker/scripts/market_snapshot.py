"""Verified market-data snapshot for the data-quality-checker guardrail.

An LLM writing a market report can confabulate exact numbers -- citing a
Bollinger band, a "historically validated bounce", or a support level the
underlying data does not support. This module computes a deterministic
ground-truth snapshot for a ticker as of an analysis date:

    * the latest OHLCV row on or before the analysis date (look-ahead cutoff
      re-applied defensively, never trusting the caller to pre-filter),
    * a FIXED set of ~11 common indicators (EMA/SMA/RSI/Bollinger/MACD/ATR),
    * the recent verified closes and the recent high/low range.

It renders a fixed-shape text block with guardrail language instructing the
reader to treat the snapshot as the single source of truth and to flag
conflicts rather than inventing reconciled numbers. The companion
``check_data_quality.py`` ``snapshot`` check consumes the JSON form of this
snapshot to flag fabricated or inconsistent price levels in a report.

Indicators are computed in pure Python (stdlib only) so this module imports
without pandas/network libraries and the tests run fully offline against
committed fixtures. Network fetching (yfinance / FMP) is imported lazily
inside ``load_ohlcv`` only.

Indicator conventions (documented so callers know what "verified" means):
  * SMA(n)        -- simple mean of the last n closes; N/A if < n rows.
  * EMA(n)        -- recursive, adjust=False, alpha = 2/(n+1), seeded at the
                     first close (matches pandas ``ewm(span=n, adjust=False)``).
  * RSI(14)       -- Wilder's smoothing; 100 when there are only gains.
  * Bollinger(20) -- middle = SMA(20); bands = middle +/- 2 * population std.
  * MACD(12,26,9) -- EMA(12) - EMA(26); signal = EMA(9) of the MACD line.
  * ATR(14)       -- Wilder's smoothing of the true range; N/A if < 15 rows.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date, datetime

# Fixed indicator set -- the snapshot is the same shape every run.
SNAPSHOT_INDICATORS: tuple[str, ...] = (
    "close_10_ema",
    "close_50_sma",
    "close_200_sma",
    "rsi",
    "boll",
    "boll_ub",
    "boll_lb",
    "macd",
    "macds",
    "macdh",
    "atr",
)

OHLCV_FIELDS: tuple[str, ...] = ("open", "high", "low", "close", "volume")


# ---------------------------------------------------------------------------
# Pure indicator helpers (stdlib only)
# ---------------------------------------------------------------------------


def _sma(values: list[float], n: int) -> float | None:
    """Simple moving average of the last n values, or None if too few."""
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def _ema_series(values: list[float], n: int) -> list[float]:
    """Recursive EMA (adjust=False), seeded at the first value."""
    if not values:
        return []
    alpha = 2.0 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def _ema(values: list[float], n: int) -> float | None:
    """Latest EMA(n) value, or None if no data."""
    series = _ema_series(values, n)
    return series[-1] if series else None


def _rsi(closes: list[float], n: int = 14) -> float | None:
    """Wilder's RSI. Returns None with fewer than n+1 closes."""
    if len(closes) < n + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for prev, cur in zip(closes, closes[1:]):
        delta = cur - prev
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _pop_std(values: list[float]) -> float:
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(var)


def _bollinger(
    closes: list[float], n: int = 20, k: float = 2.0
) -> tuple[float | None, float | None, float | None]:
    """(middle, upper, lower) Bollinger bands using population std."""
    if len(closes) < n:
        return None, None, None
    window = closes[-n:]
    mid = sum(window) / n
    std = _pop_std(window)
    return mid, mid + k * std, mid - k * std


def _macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[float | None, float | None, float | None]:
    """(macd, signal, histogram). Returns None triple with < slow closes."""
    if len(closes) < slow:
        return None, None, None
    ema_fast = _ema_series(closes, fast)
    ema_slow = _ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_series = _ema_series(macd_line, signal)
    macd_val = macd_line[-1]
    signal_val = signal_series[-1]
    return macd_val, signal_val, macd_val - signal_val


def _atr(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> float | None:
    """Wilder's ATR. Returns None with fewer than n+1 rows."""
    count = len(closes)
    if count < n + 1 or len(highs) != count or len(lows) != count:
        return None
    trs: list[float] = []
    for i in range(1, count):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    # trs has count-1 entries; seed ATR with the mean of the first n.
    atr = sum(trs[:n]) / n
    for i in range(n, len(trs)):
        atr = (atr * (n - 1) + trs[i]) / n
    return atr


def compute_indicators(rows: list[dict]) -> dict[str, float | None]:
    """Compute the fixed SNAPSHOT_INDICATORS set from date-ascending rows."""
    closes = [float(r["close"]) for r in rows]
    highs = [float(r["high"]) for r in rows]
    lows = [float(r["low"]) for r in rows]

    boll_mid, boll_ub, boll_lb = _bollinger(closes)
    macd_val, macd_sig, macd_hist = _macd(closes)

    return {
        "close_10_ema": _ema(closes, 10),
        "close_50_sma": _sma(closes, 50),
        "close_200_sma": _sma(closes, 200),
        "rsi": _rsi(closes, 14),
        "boll": boll_mid,
        "boll_ub": boll_ub,
        "boll_lb": boll_lb,
        "macd": macd_val,
        "macds": macd_sig,
        "macdh": macd_hist,
        "atr": _atr(highs, lows, closes, 14),
    }


# ---------------------------------------------------------------------------
# Snapshot assembly + rendering (pure calc)
# ---------------------------------------------------------------------------

GUARDRAIL_TEXT = (
    "Treat this snapshot as the single source of truth for exact OHLCV, "
    "price-level, and indicator-value claims. If another tool output or a "
    "draft report conflicts with it, flag the discrepancy rather than "
    "inventing a reconciled number. Do not claim historical validation, "
    "support/resistance bounces, or exact percentage moves unless directly "
    "supported by the values below with concrete dates and prices."
)


def _cutoff_rows(rows: list[dict], analysis_date: str) -> list[dict]:
    """Keep rows with ISO date on or before analysis_date, sorted ascending.

    The look-ahead cutoff is re-applied here defensively -- this is a
    verification path, so it must not trust its input to be pre-filtered.
    """
    filtered = [r for r in rows if str(r.get("date", "")) <= analysis_date]
    return sorted(filtered, key=lambda r: str(r.get("date", "")))


def build_snapshot(
    symbol: str,
    analysis_date: str,
    rows: list[dict],
    look_back_days: int = 30,
) -> dict:
    """Build a structured ground-truth snapshot dict from OHLCV rows."""
    valid = _cutoff_rows(rows, analysis_date)
    if not valid:
        raise ValueError(f"No OHLCV rows on or before {analysis_date} for {symbol}.")

    indicators = compute_indicators(valid)
    latest = valid[-1]

    window_n = max(1, min(int(look_back_days), 30))
    window = valid[-window_n:]
    recent_closes = [{"date": str(r["date"]), "close": float(r["close"])} for r in window]
    recent_high = max(float(r["high"]) for r in window)
    recent_low = min(float(r["low"]) for r in window)

    return {
        "symbol": symbol.upper(),
        "analysis_date": analysis_date,
        "latest_row": {
            "date": str(latest["date"]),
            "open": float(latest["open"]),
            "high": float(latest["high"]),
            "low": float(latest["low"]),
            "close": float(latest["close"]),
            "volume": float(latest["volume"]),
        },
        "indicators": indicators,
        "recent_closes": recent_closes,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "guardrail": GUARDRAIL_TEXT,
    }


def _fmt(value: float | int | None) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def render_snapshot_text(snapshot: dict) -> str:
    """Render the fixed-shape ground-truth text block with guardrail language."""
    latest = snapshot["latest_row"]
    lines = [
        f"## Verified market data snapshot for {snapshot['symbol']}",
        "",
        f"- Requested analysis date: {snapshot['analysis_date']}",
        f"- Latest trading row used: {latest['date']}",
        "- Rows after the requested analysis date are excluded before verification.",
        "",
        "### Latest verified OHLCV row",
        "",
        "| Field | Value |",
        "|---|---:|",
    ]
    for field in OHLCV_FIELDS:
        lines.append(f"| {field.capitalize()} | {_fmt(latest.get(field))} |")

    lines += [
        "",
        "### Verified technical indicators (latest row)",
        "",
        "| Indicator | Value |",
        "|---|---:|",
    ]
    for name in SNAPSHOT_INDICATORS:
        lines.append(f"| {name} | {_fmt(snapshot['indicators'].get(name))} |")

    recent = snapshot["recent_closes"]
    lines += [
        "",
        f"### Recent verified closes (last {len(recent)} rows)",
        "",
        f"- Recent verified range: low {_fmt(snapshot['recent_low'])} / "
        f"high {_fmt(snapshot['recent_high'])}",
        "",
        "| Date | Close |",
        "|---|---:|",
    ]
    for row in recent:
        lines.append(f"| {row['date']} | {_fmt(row['close'])} |")

    lines += ["", GUARDRAIL_TEXT]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# OHLCV loading (network -- lazy imports inside)
# ---------------------------------------------------------------------------


def load_ohlcv(
    symbol: str,
    analysis_date: str,
    source: str = "yfinance",
    api_key: str | None = None,
    history_days: int = 420,
) -> list[dict]:
    """Load OHLCV rows for a symbol, then re-apply the look-ahead cutoff.

    ``history_days`` calendar days are fetched before the analysis date so the
    long moving averages (up to 200 sessions) have enough data. Network
    libraries are imported lazily so the module imports with stdlib only.
    """
    from datetime import timedelta

    end = date.fromisoformat(analysis_date)
    start = end - timedelta(days=history_days)

    if source == "yfinance":
        rows = _load_yfinance(symbol, start, end)
    elif source == "fmp":
        rows = _load_fmp(symbol, start, end, api_key)
    else:
        raise ValueError(f"Unknown source: {source!r} (use 'yfinance' or 'fmp').")

    return _cutoff_rows(rows, analysis_date)


def _load_yfinance(symbol: str, start: date, end: date) -> list[dict]:
    from datetime import timedelta

    import yfinance as yf

    # yfinance end is exclusive; add a day so analysis_date is included.
    df = yf.download(
        symbol,
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        progress=False,
        auto_adjust=False,
    )
    if df is None or df.empty:
        return []

    def _pick(row, col: str) -> float:
        # Columns may be a MultiIndex when a single ticker is requested.
        val = row[col]
        return float(val.iloc[0]) if hasattr(val, "iloc") else float(val)

    rows: list[dict] = []
    for idx, r in df.iterrows():
        rows.append(
            {
                "date": idx.strftime("%Y-%m-%d"),
                "open": _pick(r, "Open"),
                "high": _pick(r, "High"),
                "low": _pick(r, "Low"),
                "close": _pick(r, "Close"),
                "volume": _pick(r, "Volume"),
            }
        )
    return rows


def _load_fmp(symbol: str, start: date, end: date, api_key: str | None) -> list[dict]:
    import urllib.request

    key = api_key or os.environ.get("FMP_API_KEY")
    if not key:
        raise SystemExit(
            "Error: FMP source requires an API key. Set FMP_API_KEY or pass "
            "--api-key. Get one at https://site.financialmodelingprep.com/"
        )
    url = (
        f"https://financialmodelingprep.com/api/v3/historical-price-full/{symbol}"
        f"?from={start.isoformat()}&to={end.isoformat()}&apikey={key}"
    )
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
        payload = json.loads(resp.read().decode("utf-8"))
    historical = payload.get("historical", []) if isinstance(payload, dict) else []
    rows: list[dict] = []
    for r in historical:
        rows.append(
            {
                "date": str(r["date"]),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r.get("volume", 0) or 0),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a verified market-data snapshot for a ticker/date."
    )
    parser.add_argument("--ticker", required=True, help="Ticker symbol")
    parser.add_argument(
        "--date",
        required=True,
        help="Analysis date (YYYY-MM-DD); look-ahead rows are excluded",
    )
    parser.add_argument(
        "--source",
        default="yfinance",
        choices=["yfinance", "fmp"],
        help="OHLCV data source (default: yfinance)",
    )
    parser.add_argument("--api-key", help="FMP API key (or set FMP_API_KEY)")
    parser.add_argument(
        "--look-back-days",
        type=int,
        default=30,
        help="Recent closes to include in the snapshot (max 30)",
    )
    parser.add_argument(
        "--output-dir",
        default="reports/",
        help="Output directory for snapshot files",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        date.fromisoformat(args.date)
    except ValueError:
        print(f"Error: Invalid date format: {args.date}", file=sys.stderr)
        sys.exit(1)

    try:
        rows = load_ohlcv(args.ticker, args.date, source=args.source, api_key=args.api_key)
        snapshot = build_snapshot(args.ticker, args.date, rows, look_back_days=args.look_back_days)
    except (ValueError, SystemExit) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    stem = f"market_snapshot_{args.ticker.upper()}_{timestamp}"

    json_path = os.path.join(args.output_dir, f"{stem}.json")
    with open(json_path, "w", encoding="utf-8") as jf:
        json.dump(snapshot, jf, indent=2, ensure_ascii=False)
    print(f"JSON snapshot: {json_path}")

    md_path = os.path.join(args.output_dir, f"{stem}.md")
    with open(md_path, "w", encoding="utf-8") as mf:
        mf.write(render_snapshot_text(snapshot))
    print(f"Markdown snapshot: {md_path}")


if __name__ == "__main__":
    main()
