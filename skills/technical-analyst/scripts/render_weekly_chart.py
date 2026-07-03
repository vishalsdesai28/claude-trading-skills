#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mplfinance>=0.12.9b0", "pandas>=2.0"]
# ///
"""Render a deterministic TradingView-style weekly candlestick chart.

The visual STYLE is pinned here (colors, dark background, volume panel, legend)
so every chart looks identical run to run. The INDICATOR SET varies per run via
--sma / --ema flags. Color is keyed by indicator period, not draw order, so
SMA20 is always yellow whether it is the only line or one of six.

Moving averages are computed on MORE history than is displayed, then trimmed to
the requested window, so a long MA (e.g. SMA200) is fully drawn from the left
edge instead of only appearing partway across. --range is the DISPLAY window.

Data source: `yahoo-finance-pp-cli chart <SYMBOL> --interval 1wk --range <r> --json`
Run with uv so deps resolve without touching system Python:

    uv run render_weekly_chart.py MSFT --sma 20,30,50,200 --range 5y --out reports/

ponytail: single script = the consistency contract. Do not inline matplotlib
in the skill; call this instead.
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless render
import matplotlib.pyplot as plt  # noqa: E402
import mplfinance as mpf  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

# --- pinned style: the "consistent look" contract (never varies per run) ---
UP, DOWN = "#26a69a", "#ef5350"
FACE, GRID = "#131722", "#2a2e39"
# indicator color keyed by PERIOD, not draw order -> SMA20 always yellow, etc.
SMA_COLORS = {20: "#f5d020", 30: "#ffffff", 50: "#26a69a", 200: "#ef5350"}
# periods not in the map get a fixed palette, assigned deterministically by order
EXT_PALETTE = ["#ff9800", "#00bcd4", "#e040fb", "#7e57c2", "#8d6e63"]

# Approx weekly bars per CLI range bucket. NOTE: this source's "max" caps low
# (~163 wk for MSFT) — smaller than 10y — so we never use it to pad; 10y is the
# largest reliable bucket. ponytail: hardcoded because the source is quirky.
FETCH_ORDER = [("1y", 52), ("2y", 104), ("5y", 260), ("10y", 523)]
DISPLAY_WEEKS = {"6mo": 26, "ytd": 52, "1y": 52, "2y": 104, "5y": 260, "10y": 520, "max": 523}
DISPLAY_OFFSET = {
    "6mo": pd.DateOffset(months=6), "1y": pd.DateOffset(years=1),
    "2y": pd.DateOffset(years=2), "5y": pd.DateOffset(years=5),
    "10y": pd.DateOffset(years=10),
}


def color_for(period, ordinal):
    """Return the pinned color for a period, or a deterministic extension color."""
    return SMA_COLORS.get(period, EXT_PALETTE[ordinal % len(EXT_PALETTE)])


def parse_periods(spec):
    return [int(x) for x in spec.split(",") if x.strip()] if spec else []


def choose_fetch_range(display_range, periods):
    """Smallest bucket covering the display window + longest MA lookback."""
    need = DISPLAY_WEEKS.get(display_range, 260) + (max(periods) if periods else 0)
    for name, weeks in FETCH_ORDER:
        if weeks >= need:
            return name
    return "10y"  # largest reliable bucket; long MAs may stay partial


def fetch(symbol, rng, interval):
    """Fetch OHLCV from the CLI (stdout only; warnings go to stderr)."""
    out = subprocess.run(
        ["yahoo-finance-pp-cli", "chart", symbol,
         "--interval", interval, "--range", rng, "--json"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        sys.exit(f"chart fetch failed for {symbol}: {out.stderr.strip()}")
    result = json.loads(out.stdout)["results"]["chart"]["result"][0]
    q = result["indicators"]["quote"][0]
    df = pd.DataFrame(
        {"Open": q["open"], "High": q["high"], "Low": q["low"],
         "Close": q["close"], "Volume": q["volume"]},
        index=pd.to_datetime(result["timestamp"], unit="s"),
    ).dropna()
    if df.empty:
        sys.exit(f"no bars returned for {symbol} (check ticker / range)")
    return df


def compute_overlays(df, sma, ema):
    """MA overlays computed on FULL history: (label, series, color, linestyle)."""
    overlays = []
    for i, p in enumerate(sma):
        overlays.append((f"SMA{p}", df["Close"].rolling(p).mean(), color_for(p, i), "-"))
    for i, p in enumerate(ema):
        c = color_for(p, len(sma) + i)
        overlays.append((f"EMA{p}", df["Close"].ewm(span=p, adjust=False).mean(), c, "--"))
    return overlays


def trim(df, overlays, display_range):
    """Trim to the display window; MAs keep values computed on full history."""
    if display_range == "max":
        return df, overlays
    last = df.index[-1]
    if display_range == "ytd":
        start = pd.Timestamp(year=last.year, month=1, day=1)
    else:
        start = last - DISPLAY_OFFSET.get(display_range, pd.DateOffset(years=5))
    mask = df.index >= start
    df2 = df[mask]
    overlays2 = [(n, s[mask], c, st) for n, s, c, st in overlays]
    for n, s, _, _ in overlays2:
        if len(s) and bool(pd.isna(s.iloc[0])):
            print(f"note: {n} not fully covered at left edge (insufficient history)",
                  file=sys.stderr)
    return df2, overlays2


def render(df, overlays, symbol, interval, out_dir):
    addplots = [mpf.make_addplot(s, color=c, width=1.0, linestyle=st)
                for _, s, c, st in overlays]
    legend = [(n, c) for n, _, c, _ in overlays]

    mc = mpf.make_marketcolors(
        up=UP, down=DOWN, edge="inherit", wick="inherit",
        volume={"up": UP, "down": DOWN})
    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds", marketcolors=mc,
        facecolor=FACE, figcolor=FACE, edgecolor=GRID, gridcolor=GRID,
        gridstyle="-", y_on_right=True)

    fig, axes = mpf.plot(
        df, type="candle", style=style, volume=True, addplot=addplots,
        returnfig=True, figsize=(16, 9), datetime_format="%Y", xrotation=0,
        title=f"\n{symbol.upper()} · {interval.upper()}", tight_layout=True)

    if legend:
        handles = [Line2D([0], [0], color=c, lw=2, label=n) for n, c in legend]
        axes[0].legend(handles=handles, loc="upper left", ncol=len(handles),
                       facecolor=FACE, edgecolor=FACE, labelcolor="linecolor",
                       fontsize=9, framealpha=0)

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    png = Path(out_dir) / f"{symbol.upper()}_weekly_{date}.png"
    fig.savefig(png, dpi=110, facecolor=FACE)
    plt.close(fig)
    return png


def self_check():
    """Offline check: color map, fetch-range padding, full-MA trim, render."""
    assert parse_periods("20,30,50,200") == [20, 30, 50, 200]
    assert parse_periods("") == []
    assert color_for(20, 0) == SMA_COLORS[20]           # pinned period
    assert color_for(9, 0) == EXT_PALETTE[0]            # unmapped -> palette
    assert color_for(9, 5) == EXT_PALETTE[0]            # 5 % len(palette) wraps to [0]
    # 5y display + SMA200 must pad the fetch beyond 5y so SMA200 is full at left edge
    assert choose_fetch_range("5y", [20, 30, 50, 200]) == "10y"
    assert choose_fetch_range("1y", [200]) == "5y"
    # synthetic upward-drifting OHLCV, 460 weekly bars (>= 5y display + SMA200)
    idx = pd.date_range("2017-01-02", periods=460, freq="W-MON")
    base = pd.Series(range(460), index=idx, dtype=float) + 100
    df = pd.DataFrame({
        "Open": base, "High": base + 3, "Low": base - 3,
        "Close": base + 1, "Volume": [1_000_000] * 460}, index=idx)
    overlays = compute_overlays(df, [20, 30, 50, 200], [])
    df_disp, ov_disp = trim(df, overlays, "5y")
    sma200 = next(s for n, s, _, _ in ov_disp if n == "SMA200")
    assert not bool(pd.isna(sma200.iloc[0])), "SMA200 should be full at the left edge after padding"
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        png = render(df_disp, ov_disp, "TEST", "1wk", td)
        assert png.exists() and png.stat().st_size > 5000, "render produced no image"
    print("self-check OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("symbol", nargs="?", help="ticker, e.g. MSFT")
    ap.add_argument("--sma", default="20,30,50,200", help="comma-separated SMA periods")
    ap.add_argument("--ema", default="", help="comma-separated EMA periods (dashed)")
    ap.add_argument("--interval", default="1wk")
    ap.add_argument("--range", dest="rng", default="5y",
                    help="DISPLAY window (6mo/ytd/1y/2y/5y/10y/max); more history is "
                         "fetched behind the scenes so long MAs are fully drawn")
    ap.add_argument("--out", default="reports")
    ap.add_argument("--self-check", action="store_true", help="offline render test, no network")
    a = ap.parse_args()

    if a.self_check:
        self_check()
        return
    if not a.symbol:
        ap.error("symbol is required (or pass --self-check)")

    sma, ema = parse_periods(a.sma), parse_periods(a.ema)
    fetch_range = choose_fetch_range(a.rng, sma + ema)
    df_full = fetch(a.symbol, fetch_range, a.interval)
    overlays = compute_overlays(df_full, sma, ema)
    df_disp, ov_disp = trim(df_full, overlays, a.rng)
    print(render(df_disp, ov_disp, a.symbol, a.interval, a.out))


if __name__ == "__main__":
    main()
