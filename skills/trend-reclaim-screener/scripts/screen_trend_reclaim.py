#!/usr/bin/env python3
"""Trend Reclaim screener (Phase 1) — long-side SMA50 reclaim after a reset.

Finds US stocks that recently reclaimed their SMA50 after a genuine pullback
below it, scores the reclaim on 5 weighted factors, grades A-D, and emits a
ranked watchlist (JSON + Markdown) with top picks and per-name plan levels.

Data: keyless Yahoo Finance (yfinance) — coarse server-side EquityQuery for
the universe, one batched history download for bars. Offline `--fixture`
JSON supported for tests and dry runs. No paid API required.

After-hours scanner: run after the close; picks assume entry at the next
session's open.
"""

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

logger = logging.getLogger("trend_reclaim")

FACTOR_WEIGHTS = {
    "reclaim_quality": 0.30,
    "momentum": 0.25,
    "structure": 0.20,
    "volume": 0.15,
    "trend_alignment": 0.10,
}

GRADE_THRESHOLDS = [
    (85, "A", "High-quality trend restoration with strong confirmation."),
    (70, "B", "Solid reclaim; one factor less mature. Standard sizing on a clean entry."),
    (50, "C", "Early or mixed reclaim attempt. Watchlist only."),
    (0, "D", "Weak or messy reclaim. Skip."),
]

DEFAULTS = {
    "reclaim_window": 5,  # sessions since cross-above to count as "recent"
    "min_days_below": 5,  # of the lookback window, to prove a real reset
    "lookback_below": 30,  # sessions before the cross inspected for the reset
    "max_failed_reclaims": 2,  # prior cross-aboves in lookback -> choppy, reject
    "min_history": 200,  # bars needed (SMA200 trend alignment)
    "min_price": 5.0,
    "min_adv_usd": 10_000_000,
    "max_ext_pct": 10.0,  # close more than this % above SMA50 -> chasing
    "fade_rvol": 0.8,  # cross-day RVOL below this = fade risk (doc's volume diagram)
    "exclude_earnings_within_days": 7,  # reject when next earnings is this close; 0 disables
    "top": 3,
    "watch_min_grade": "C",
}

GRADE_ORDER = ["A", "B", "C", "D"]


# ---------------------------------------------------------------- indicators
def sma(values, period, end=None):
    """Simple moving average of values[:end][-period:]; None if not enough data."""
    vals = values if end is None else values[:end]
    if len(vals) < period:
        return None
    window = vals[-period:]
    return sum(window) / period


def roc(values, period, end=None):
    vals = values if end is None else values[:end]
    if len(vals) < period + 1 or vals[-period - 1] == 0:
        return None
    return (vals[-1] / vals[-period - 1] - 1.0) * 100.0


def atr(bars, period=14, end=None):
    """Average true range over bars[:end][-period:]."""
    sub = bars if end is None else bars[:end]
    if len(sub) < period + 1:
        return None
    trs = []
    for i in range(len(sub) - period, len(sub)):
        prev_close = sub[i - 1]["close"]
        trs.append(
            max(
                sub[i]["high"] - sub[i]["low"],
                abs(sub[i]["high"] - prev_close),
                abs(sub[i]["low"] - prev_close),
            )
        )
    return sum(trs) / period


# ---------------------------------------------------------------- detection
def detect_reclaim(bars, cfg):
    """Return reclaim metadata if the last close sits above SMA50 after a
    recent below->above cross that followed a genuine reset; else None."""
    closes = [b["close"] for b in bars]
    n = len(closes)
    if n < cfg["min_history"]:
        return None

    sma50 = [None] * n
    for i in range(49, n):
        sma50[i] = sum(closes[i - 49 : i + 1]) / 50

    def above(i):
        return sma50[i] is not None and closes[i] > sma50[i]

    if not above(n - 1):
        return None

    cross_i = None
    for i in range(n - 1, 50, -1):
        if above(i) and not above(i - 1):
            cross_i = i
            break
        if not above(i):  # dipped back below since — cross_i search over
            break
    if cross_i is None:
        return None

    days_since_cross = (n - 1) - cross_i
    if days_since_cross >= cfg["reclaim_window"]:
        return None

    lo = max(50, cross_i - cfg["lookback_below"])
    below_days = sum(1 for i in range(lo, cross_i) if not above(i))
    if below_days < cfg["min_days_below"]:
        return None

    reset_low_i = min(range(lo, cross_i), key=lambda i: closes[i])
    prior_crosses = sum(1 for i in range(lo + 1, cross_i) if above(i) and not above(i - 1))
    closes_above = days_since_cross + 1

    return {
        "cross_index": cross_i,
        "days_since_cross": days_since_cross,
        "closes_above": closes_above,
        "reset_low_index": reset_low_i,
        "prior_failed_reclaims": prior_crosses,
        "sma50": sma50,
        "phase": "reclaimed_trend" if closes_above >= 3 else "reclaim_attempt",
    }


# ---------------------------------------------------------------- invalidation
def invalidation_reasons(bars, reclaim, cfg):
    """Hard-reject reasons. Works with reclaim=None (history/liquidity only)."""
    reasons = []
    if len(bars) < cfg["min_history"]:
        reasons.append("insufficient_history")
        return reasons

    last = bars[-1]
    if last["close"] < cfg["min_price"]:
        reasons.append("price_below_minimum")

    adv = sum(b["volume"] for b in bars[-20:]) / 20 * last["close"]
    if adv < cfg["min_adv_usd"]:
        reasons.append("insufficient_dollar_volume")

    if reclaim:
        s50 = reclaim["sma50"][-1]
        ext_pct = (last["close"] / s50 - 1.0) * 100.0
        if ext_pct > cfg["max_ext_pct"]:
            reasons.append(f"extended_{ext_pct:.1f}pct_above_sma50")
        if reclaim["prior_failed_reclaims"] >= cfg["max_failed_reclaims"]:
            reasons.append("repeated_failed_reclaims")
    return reasons


# ---------------------------------------------------------------- scoring
def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def score_candidate(bars, reclaim, cfg):
    """Score five factors 0-100 each; composite is the weighted sum."""
    closes = [b["close"] for b in bars]
    n = len(closes)
    last = closes[-1]
    sma50_series = reclaim["sma50"]
    s50 = sma50_series[-1]
    reset_i = reclaim["reset_low_index"]

    # --- reclaim quality: persistence + healthy distance + SMA50 slope
    held = min(reclaim["closes_above"], 5) / 5 * 40
    dist_pct = (last / s50 - 1.0) * 100.0
    if dist_pct < 1.0:
        distance = 30 * max(0.0, dist_pct)
    elif dist_pct <= 6.0:
        distance = 30.0
    else:
        distance = 30 * max(0.0, (cfg["max_ext_pct"] - dist_pct) / (cfg["max_ext_pct"] - 6.0))
    s50_prev = sma50_series[-11] if n >= 11 and sma50_series[-11] else s50
    slope_pct = (s50 / s50_prev - 1.0) * 100.0
    slope = 30 if slope_pct > 0 else (15 if slope_pct > -1.0 else 0)
    reclaim_quality = _clamp(held + distance + slope)

    # --- momentum: positive + improving ROC, rising SMA20
    roc_now = roc(closes, 10) or 0.0
    roc_at_low = roc(closes, 10, end=reset_i + 1) or 0.0
    momentum = 0.0
    if roc_now > 0:
        momentum += 30
    if roc_now > roc_at_low:
        momentum += 40
    sma20_now, sma20_prev = sma(closes, 20), sma(closes, 20, end=n - 5)
    if sma20_now and sma20_prev and sma20_now > sma20_prev:
        momentum += 30
    momentum = _clamp(momentum)

    # --- structure: higher lows + volatility contraction since the reset low
    lows = [b["low"] for b in bars[-15:]]
    block_mins = [min(lows[0:5]), min(lows[5:10]), min(lows[10:15])]
    if block_mins[0] < block_mins[1] < block_mins[2]:
        structure = 50.0
    elif block_mins[1] < block_mins[2]:
        structure = 25.0
    else:
        structure = 0.0
    atr_now, atr_then = atr(bars, 14), atr(bars, 14, end=reset_i + 1)
    if atr_now and atr_then and closes[reset_i]:
        ratio = (atr_now / last) / (atr_then / closes[reset_i])
        structure += 50 if ratio <= 0.9 else (25 if ratio <= 1.0 else 0)
    structure = _clamp(structure)

    # --- volume: participation on the cross + up/down volume since the low
    vols = [b["volume"] for b in bars]
    avg50 = sum(vols[-50:]) / 50
    cross_rvol = vols[reclaim["cross_index"]] / avg50 if avg50 else 0.0
    # Bands per the doc's volume diagram: ~3x RVOL = strong break, <0.8x = fade risk.
    if cross_rvol >= 3.0:
        volume = 50.0
    elif cross_rvol >= 1.5:
        volume = 40.0
    elif cross_rvol >= 1.0:
        volume = 25.0
    elif cross_rvol >= cfg["fade_rvol"]:
        volume = 10.0
    else:
        volume = 0.0
    up_vol = down_vol = 0.0
    for i in range(reset_i + 1, n):
        if closes[i] > closes[i - 1]:
            up_vol += vols[i]
        elif closes[i] < closes[i - 1]:
            down_vol += vols[i]
    ud_ratio = up_vol / down_vol if down_vol else 2.0
    volume += 50 if ud_ratio >= 1.2 else (25 if ud_ratio >= 1.0 else 0)
    volume = _clamp(volume)

    # --- trend alignment: SMA200 posture
    s200 = sma(closes, 200)
    trend = 0.0
    if s200:
        if last > s200:
            trend += 50
        if s50 >= s200:
            trend += 30
        elif s50 >= s200 * 0.97:
            trend += 15
        s200_prev = sma(closes, 200, end=n - 20)
        if s200_prev and s200 >= s200_prev:
            trend += 20
    trend = _clamp(trend)

    factors = {
        "reclaim_quality": round(reclaim_quality, 1),
        "momentum": round(momentum, 1),
        "structure": round(structure, 1),
        "volume": round(volume, 1),
        "trend_alignment": round(trend, 1),
    }
    composite = sum(factors[k] * w for k, w in FACTOR_WEIGHTS.items())

    warnings = []
    if cross_rvol < cfg["fade_rvol"]:
        warnings.append("fade_risk_volume")
    elif cross_rvol < 1.0:
        warnings.append("low_participation_reclaim")
    if dist_pct < 1.0:
        warnings.append("marginal_reclaim_distance")
    if slope_pct <= -1.0:
        warnings.append("sma50_still_declining")

    return {
        "factors": factors,
        "composite": round(composite, 1),
        "warnings": warnings,
        "cross_rvol": round(cross_rvol, 2),
    }


def grade_for(composite):
    for threshold, g, _ in GRADE_THRESHOLDS:
        if composite >= threshold:
            return g
    return "D"


def grade_at_or_above(grade, threshold):
    return GRADE_ORDER.index(grade) <= GRADE_ORDER.index(threshold)


def capped_grade(grade, cross_rvol, cfg):
    """Doc's volume diagram: a sub-0.8x RVOL cross is fade risk — never tradeable-grade."""
    if cross_rvol is not None and cross_rvol < cfg["fade_rvol"] and grade_at_or_above(grade, "B"):
        return "C"
    return grade


def market_regime(bars):
    """Benchmark (SPY) posture vs its SMA50/SMA200 — the don't-fight-the-tape check."""
    if not bars:
        return None
    closes = [b["close"] for b in bars]
    s50, s200 = sma(closes, 50), sma(closes, 200)
    if not (s50 and s200):
        return None
    last = closes[-1]
    if last > s50 and last > s200:
        label = "risk_on"
    elif last < s50 and last < s200:
        label = "risk_off"
    else:
        label = "mixed"
    return {
        "benchmark": "SPY",
        "label": label,
        "close": round(last, 2),
        "sma50": round(s50, 2),
        "sma200": round(s200, 2),
    }


# ---------------------------------------------------------------- plan levels
def build_plan(bars, reclaim):
    """Informational levels per the reclaim playbook: entry next open, stop
    below the restoration level, T1 at pre-reset resistance."""
    last = bars[-1]["close"]
    s50 = reclaim["sma50"][-1]
    cross_low = bars[reclaim["cross_index"]]["low"]
    stop = min(cross_low, s50)
    reset_i = reclaim["reset_low_index"]
    t1 = max(b["high"] for b in bars[max(0, reset_i - 60) : reset_i + 1])
    t2 = s50 + (t1 - bars[reset_i]["low"])  # measured move: base depth off the reclaim level
    return {
        "entry": "next_session_open",
        "reclaim_level": round(s50, 2),
        "stop": round(stop, 2),
        "t1": round(t1, 2),
        "t2": round(t2, 2),
        "risk_pct": round((last - stop) / last * 100.0, 2),
        "reward_risk_at_t1": round((t1 - last) / max(last - stop, 1e-9), 2),
    }


# ---------------------------------------------------------------- screening
def screen(bars_by_ticker, cfg, as_of, earnings_by_ticker=None):
    candidates, rejected, no_signal = [], {}, []
    for ticker, bars in sorted(bars_by_ticker.items()):
        if not bars:
            no_signal.append(ticker)
            continue
        reclaim = detect_reclaim(bars, cfg)
        if reclaim is None:
            no_signal.append(ticker)
            continue
        reasons = invalidation_reasons(bars, reclaim, cfg)
        if reasons:
            rejected[ticker] = reasons
            continue
        edate = (earnings_by_ticker or {}).get(ticker)
        window = cfg["exclude_earnings_within_days"]
        if edate and window:
            days_to = (date.fromisoformat(edate) - date.fromisoformat(as_of)).days
            if 0 <= days_to <= window:
                rejected[ticker] = [f"earnings_{edate}_within_{window}d"]
                continue
        scored = score_candidate(bars, reclaim, cfg)
        grade = grade_for(scored["composite"])
        cap = capped_grade(grade, scored["cross_rvol"], cfg)
        if cap != grade:
            scored["warnings"].append("grade_capped_fade_risk")
            grade = cap
        if not grade_at_or_above(grade, cfg["watch_min_grade"]):
            rejected[ticker] = [f"below_watch_grade_{grade}"]
            continue
        candidates.append(
            {
                "ticker": ticker,
                "grade": grade,
                "phase": reclaim["phase"],
                "composite": scored["composite"],
                "factors": scored["factors"],
                "cross_rvol": scored["cross_rvol"],
                "warnings": scored["warnings"],
                "next_earnings": edate,
                "last_close": round(bars[-1]["close"], 2),
                "days_since_cross": reclaim["days_since_cross"],
                "closes_above_sma50": reclaim["closes_above"],
                "plan": build_plan(bars, reclaim),
            }
        )
    candidates.sort(key=lambda c: -c["composite"])
    return {
        "as_of": as_of,
        "params": {k: v for k, v in cfg.items()},
        "scanned": len(bars_by_ticker),
        "candidates": candidates,
        "top_picks": candidates[: cfg["top"]],
        "rejected": rejected,
        "no_signal_count": len(no_signal),
    }


# ---------------------------------------------------------------- reports
PRE_ENTRY_CHECKLIST = [
    "Reset/base complete — stabilization visible, chop declining",
    "Momentum improving over multiple sessions, not a one-bar bounce",
    "Volume supportive of the reclaim (participation rising)",
    "Higher lows forming; structure cleaner than the reset phase",
    "Broader market / sector not fighting the reclaim direction (see Market regime above)",
    "No binary catalyst within the holding window — earnings auto-rejected when Yahoo"
    " publishes a date; verify other catalysts (FDA, M&A, guidance) manually",
]


def render_markdown(result):
    lines = [
        "# Trend Reclaim Screener",
        "",
        f"**As of:** {result['as_of']} (after-hours scan; entries assume next open)",
        f"**Scanned:** {result['scanned']} | **Candidates:** {len(result['candidates'])}"
        f" | **Rejected:** {len(result['rejected'])}",
    ]
    regime = result.get("market_regime")
    if regime:
        note = {
            "risk_on": "tape supportive",
            "mixed": "mixed tape — be selective",
            "risk_off": "hostile tape — reclaims fight the major trend",
        }[regime["label"]]
        lines.append(
            f"**Market regime:** {regime['label']} ({note}) — {regime['benchmark']}"
            f" {regime['close']} vs SMA50 {regime['sma50']} / SMA200 {regime['sma200']}"
        )
    lines += [
        "",
        "## Top Picks",
        "",
    ]
    if not result["top_picks"]:
        lines.append("_No qualifying reclaim setups today._")
    else:
        lines.append("| # | Ticker | Grade | Score | Phase | Close | Stop | T1 | T2 | R:R |")
        lines.append("|---|--------|-------|-------|-------|-------|------|----|----|-----|")
        for i, c in enumerate(result["top_picks"], 1):
            p = c["plan"]
            lines.append(
                f"| {i} | {c['ticker']} | {c['grade']} | {c['composite']} | {c['phase']}"
                f" | {c['last_close']} | {p['stop']} | {p['t1']} | {p['t2']}"
                f" | {p['reward_risk_at_t1']} |"
            )
    lines += ["", "## Watchlist", ""]
    for c in result["candidates"]:
        f = c["factors"]
        lines += [
            f"### {c['ticker']} — {c['grade']} ({c['composite']}) · {c['phase']}",
            f"- Close {c['last_close']}, crossed above SMA50 {c['days_since_cross']}"
            f" session(s) ago, {c['closes_above_sma50']} close(s) above",
            f"- Factors: reclaim {f['reclaim_quality']} / momentum {f['momentum']}"
            f" / structure {f['structure']} / volume {f['volume']}"
            f" / trend {f['trend_alignment']}",
            f"- Plan: entry next open · reclaim level {c['plan']['reclaim_level']}"
            f" · stop {c['plan']['stop']} ({c['plan']['risk_pct']}% risk)"
            f" · T1 {c['plan']['t1']} · T2 {c['plan']['t2']}",
        ]
        if c.get("next_earnings"):
            lines.append(f"- Next earnings: {c['next_earnings']}")
        if c["warnings"]:
            lines.append(f"- ⚠️ {', '.join(c['warnings'])}")
        lines.append("")
    lines += ["## Pre-Entry Checklist", ""]
    lines += [f"- [ ] {item}" for item in PRE_ENTRY_CHECKLIST]
    if result["rejected"]:
        lines += ["", "## Rejected", ""]
        lines += [f"- {t}: {', '.join(r)}" for t, r in sorted(result["rejected"].items())]
    lines.append("")
    return "\n".join(lines)


def write_reports(result, output_dir, prefix):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{prefix}_{result['as_of']}.json"
    md_path = out / f"{prefix}_{result['as_of']}.md"
    json_path.write_text(json.dumps(result, indent=2) + "\n")
    md_path.write_text(render_markdown(result))
    return str(json_path), str(md_path)


# ---------------------------------------------------------------- data fetch
def bars_from_fixture(path):
    data = json.loads(Path(path).read_text())
    return data["bars"], data.get("as_of"), data.get("earnings", {})


def fetch_earnings_dates(tickers):
    """Next scheduled earnings date per ticker via the keyless yfinance calendar."""
    import yfinance as yf

    out = {}
    today = date.today().isoformat()
    for t in tickers:
        try:
            cal = yf.Ticker(t).calendar or {}
            dates = cal.get("Earnings Date") or []
            # Yahoo mixes past and future dates; keep the earliest upcoming one.
            upcoming = sorted(d.isoformat()[:10] for d in dates)
            upcoming = [d for d in upcoming if d >= today]
            if upcoming:
                out[t] = upcoming[0]
        except Exception as exc:  # missing calendar must not kill the run
            logger.warning("Earnings lookup failed for %s: %s", t, exc)
    return out


def yahoo_universe(cfg, size):
    """Keyless coarse universe: liquid US names via Yahoo EquityQuery."""
    import yfinance as yf

    Q = yf.EquityQuery
    query = Q(
        "and",
        [
            Q("eq", ["region", "us"]),
            Q("gt", ["intradayprice", cfg["min_price"]]),
            Q("gt", ["intradaymarketcap", 2_000_000_000]),
            Q("gt", ["avgdailyvol3m", 500_000]),
        ],
    )
    res = yf.screen(query, sortField="avgdailyvol3m", sortAsc=False, size=size)
    quotes = res.get("quotes", [])
    total = res.get("total", len(quotes))
    if total > len(quotes):
        logger.warning("Yahoo universe truncated to %d of %d by liquidity.", len(quotes), total)
    return [q["symbol"] for q in quotes if q.get("symbol")]


def fetch_bars(tickers):
    """Batched ~1y daily OHLCV via yfinance -> {ticker: [bar, ...]} oldest first."""
    import yfinance as yf

    # auto_adjust=False: raw (split-adjusted, dividend-unadjusted) prices so
    # plan levels match broker/TradingView charts; dividend adjustment shifts
    # older levels like T1 below what a chart shows.
    df = yf.download(
        tickers,
        period="1y",
        auto_adjust=False,
        group_by="ticker",
        threads=True,
        progress=False,
    )
    bars_by_ticker = {}
    multi = hasattr(df.columns, "levels")  # MultiIndex even for a single ticker
    for t in tickers:
        try:
            sub = df[t].dropna() if multi else df.dropna()
        except KeyError:
            logger.warning("No Yahoo data for %s — skipped.", t)
            continue
        bars = [
            {
                "date": idx.date().isoformat(),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]),
            }
            for idx, row in sub.iterrows()
        ]
        if bars:
            bars_by_ticker[t] = bars
    return bars_by_ticker


def tickers_from_csv(path):
    import csv

    with Path(path).open() as fh:
        rows = list(csv.DictReader(fh))
    col = next((c for c in rows[0] if c.strip().lower() in ("ticker", "symbol")), None)
    if not col:
        raise SystemExit("ERROR: CSV needs a Ticker/Symbol column.")
    return [r[col].strip().upper() for r in rows if r.get(col, "").strip()]


# ---------------------------------------------------------------- CLI
def build_arg_parser():
    p = argparse.ArgumentParser(description="Trend Reclaim — after-hours SMA50 reclaim screener")
    p.add_argument("--tickers", help="Comma-separated tickers (skips the Yahoo universe screen)")
    p.add_argument("--universe-csv", help="CSV with a Ticker/Symbol column (e.g. Finviz export)")
    p.add_argument("--universe-size", type=int, default=250, help="Yahoo screen size (default 250)")
    p.add_argument("--fixture", help="Offline JSON fixture: {as_of, bars:{TICKER:[...]}}")
    p.add_argument("--reclaim-window", type=int, default=DEFAULTS["reclaim_window"])
    p.add_argument("--min-days-below", type=int, default=DEFAULTS["min_days_below"])
    p.add_argument("--min-price", type=float, default=DEFAULTS["min_price"])
    p.add_argument("--min-adv-usd", type=float, default=DEFAULTS["min_adv_usd"])
    p.add_argument("--max-ext-pct", type=float, default=DEFAULTS["max_ext_pct"])
    p.add_argument(
        "--exclude-earnings-within-days",
        type=int,
        default=DEFAULTS["exclude_earnings_within_days"],
        help="Reject candidates with earnings within this many calendar days (0 disables)",
    )
    p.add_argument("--top", type=int, default=DEFAULTS["top"])
    p.add_argument("--watch-min-grade", choices=GRADE_ORDER, default=DEFAULTS["watch_min_grade"])
    p.add_argument("--output-dir", default="reports/")
    p.add_argument("--output-prefix", default="trend_reclaim")
    p.add_argument("--as-of", default=None, help="YYYY-MM-DD; default: today")
    return p


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    args = build_arg_parser().parse_args(argv)
    cfg = dict(
        DEFAULTS,
        reclaim_window=args.reclaim_window,
        min_days_below=args.min_days_below,
        min_price=args.min_price,
        min_adv_usd=args.min_adv_usd,
        max_ext_pct=args.max_ext_pct,
        exclude_earnings_within_days=args.exclude_earnings_within_days,
        top=args.top,
        watch_min_grade=args.watch_min_grade,
    )

    as_of = args.as_of
    earnings = {}
    if args.fixture:
        bars_by_ticker, fixture_as_of, earnings = bars_from_fixture(args.fixture)
        as_of = as_of or fixture_as_of
        regime = market_regime(bars_by_ticker.get("SPY"))
    else:
        if args.tickers:
            tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        elif args.universe_csv:
            tickers = tickers_from_csv(args.universe_csv)
        else:
            tickers = yahoo_universe(cfg, args.universe_size)
        fetch_list = tickers if "SPY" in tickers else tickers + ["SPY"]
        logger.info("Fetching daily bars for %d tickers...", len(fetch_list))
        bars_by_ticker = fetch_bars(fetch_list)
        regime = market_regime(bars_by_ticker.get("SPY"))
        if "SPY" not in tickers:  # benchmark only — don't screen it
            bars_by_ticker.pop("SPY", None)
    as_of = as_of or date.today().isoformat()

    result = screen(bars_by_ticker, cfg, as_of, earnings_by_ticker=earnings)
    # Second pass: earnings dates only for survivors (a handful of lookups, not 250).
    if not args.fixture and cfg["exclude_earnings_within_days"] and result["candidates"]:
        survivors = [c["ticker"] for c in result["candidates"]]
        logger.info("Checking earnings dates for %d candidates...", len(survivors))
        earnings = fetch_earnings_dates(survivors)
        if earnings:
            result = screen(bars_by_ticker, cfg, as_of, earnings_by_ticker=earnings)
    result["market_regime"] = regime
    json_path, md_path = write_reports(result, args.output_dir, args.output_prefix)
    logger.info(
        "Scanned %d | candidates %d | top picks %d",
        result["scanned"],
        len(result["candidates"]),
        len(result["top_picks"]),
    )
    print(json_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
