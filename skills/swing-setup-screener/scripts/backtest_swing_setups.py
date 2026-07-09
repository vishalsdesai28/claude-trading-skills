#!/usr/bin/env python3
"""Backtest harness for the swing-setup-screener — grade-monotonicity study.

Replays each screen at historical cutoffs (bars truncated at date T, exactly
what the screener would have seen after that close) and measures forward
outcomes. The defensible question this answers is CROSS-SECTIONAL: do A
grades beat B beat C beat D, and do the doc-mandated caps (extended,
oversold, faded close, chop) underperform as claimed? Absolute performance
claims are NOT defensible here — see the disclosure block below, which is
also printed on every report.

MANDATORY DISCLOSURES (baked into every report):
1. Survivorship bias: the universe is TODAY's listing replayed into the
   past. Delisted/acquired/shrunken names are missing — long results are
   biased UP, short results biased DOWN, by an unknown amount. Compare
   grades against each other, not against zero.
2. Earnings gate disabled: historical earnings dates are not available
   keylessly; the live screener's 5-day earnings reject is OFF here.
3. Execution model (a proxy, not the discretionary playbooks): enter at the
   next session's open; stop-first on any bar touching both stop and
   target; gaps through the stop exit at the open (worse than -1R);
   time-stop at the horizon close. Human steps (catalyst check, chart
   confirmation, live VWAP) are not modeled.
4. Data: Yahoo split-adjusted daily bars, dividends excluded from returns.
5. Overlapping windows: cutoffs closer than the horizon produce correlated
   outcomes — counts overstate independent evidence; no t-stats are shown.
6. Grades A-D are all kept (watch_min_grade=D) so D acts as the control.
7. Thresholds are the shipped defaults, untuned on this data. Any future
   tuning must use a train/validation split (see backtest-expert).
"""

import argparse
import json
import logging
import sys
from bisect import bisect_right
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from screen_swing_setups import (  # noqa: E402
    DEFAULTS,
    SCREENS,
    atr,
    fetch_bars,
    screen,
    strip_partial_bar,
    tickers_from_csv,
    yahoo_universe,
)

logger = logging.getLogger("swing_backtest")

LONG_PLAN_SCREENS = {"swing-long", "leaders"}
SHORT_PLAN_SCREENS = {"swing-short"}
LONG_RETURN_SCREENS = {"in-play"}
SHORT_RETURN_SCREENS = {"weak"}
QUADRANT_DIRECTION = {"accumulation": "long", "distribution": "short"}

DISCLOSURES = [
    "**Survivorship bias:** universe is TODAY's listing replayed into the past —"
    " delisted/acquired names are missing. Long results biased UP, shorts biased DOWN."
    " Judge grades against each other, never against zero.",
    "**Earnings gate disabled:** historical earnings dates unavailable keylessly;"
    " the live 5-day earnings reject is OFF in this study.",
    "**Execution model (proxy):** next-open entry; **stop-first** on bars touching both"
    " stop and target; gaps through the stop exit at the open (worse than -1R);"
    " time-stop at the horizon close. Discretionary playbook steps are not modeled.",
    "**Data:** Yahoo split-adjusted daily bars; dividends excluded from returns.",
    "**Overlapping windows:** correlated outcomes inflate counts; no t-stats shown.",
    "**All grades kept** (watch_min_grade=D) so D serves as the control group.",
    "**Untuned:** shipped default thresholds; tune only with a train/validation split.",
]


# ---------------------------------------------------------------- replay plumbing
def slice_bars(bars, cutoff_date):
    """Bars with date <= cutoff_date (ISO strings compare chronologically)."""
    dates = [b["date"] for b in bars]
    return bars[: bisect_right(dates, cutoff_date)]


def cutoff_positions(dates, warmup, horizon, cadence):
    """Calendar index positions with `warmup` bars of history and `horizon` ahead."""
    first = warmup - 1
    last = len(dates) - horizon - 1
    return list(range(first, last + 1, cadence)) if last >= first else []


# ---------------------------------------------------------------- outcomes
def long_trade_outcome(fwd_bars, stop, t1, horizon):
    entry = fwd_bars[0]["open"]
    if entry <= stop:
        return {
            "exit_reason": "invalidated_at_open",
            "r_multiple": None,
            "exit_price": None,
            "days_held": 0,
        }
    risk = entry - stop
    for i, b in enumerate(fwd_bars[:horizon]):
        if i > 0 and b["open"] <= stop:
            exit_price, reason = b["open"], "gap_stop"
        elif b["low"] <= stop:
            exit_price, reason = stop, "stop"  # stop-first on ambiguous bars
        elif b["high"] >= t1:
            exit_price, reason = t1, "target"
        else:
            continue
        return {
            "exit_reason": reason,
            "exit_price": exit_price,
            "r_multiple": round((exit_price - entry) / risk, 3),
            "days_held": i + 1,
        }
    last = fwd_bars[:horizon][-1]
    return {
        "exit_reason": "time",
        "exit_price": last["close"],
        "r_multiple": round((last["close"] - entry) / risk, 3),
        "days_held": min(horizon, len(fwd_bars)),
    }


def short_trade_outcome(fwd_bars, stop, t1, horizon):
    entry = fwd_bars[0]["open"]
    if entry >= stop:
        return {
            "exit_reason": "invalidated_at_open",
            "r_multiple": None,
            "exit_price": None,
            "days_held": 0,
        }
    risk = stop - entry
    for i, b in enumerate(fwd_bars[:horizon]):
        if i > 0 and b["open"] >= stop:
            exit_price, reason = b["open"], "gap_stop"
        elif b["high"] >= stop:
            exit_price, reason = stop, "stop"
        elif b["low"] <= t1:
            exit_price, reason = t1, "target"
        else:
            continue
        return {
            "exit_reason": reason,
            "exit_price": exit_price,
            "r_multiple": round((entry - exit_price) / risk, 3),
            "days_held": i + 1,
        }
    last = fwd_bars[:horizon][-1]
    return {
        "exit_reason": "time",
        "exit_price": last["close"],
        "r_multiple": round((entry - last["close"]) / risk, 3),
        "days_held": min(horizon, len(fwd_bars)),
    }


def excursions(fwd_bars, horizon, direction):
    """Direction-signed max adverse / favorable excursion % from next-open entry.

    MAE <= 0 (how far the trade went against you within the horizon), MFE >= 0
    (how far it went in your favor). This is what tests risk claims like the
    doc's "extended pullbacks are violent" — 20-day returns alone cannot.
    """
    entry = fwd_bars[0]["open"]
    window = fwd_bars[:horizon]
    if not entry or not window:
        return None, None
    lo = min(b["low"] for b in window)
    hi = max(b["high"] for b in window)
    if direction == "long":
        return round((lo / entry - 1) * 100, 2), round((hi / entry - 1) * 100, 2)
    return round((entry - hi) / entry * 100, 2), round((entry - lo) / entry * 100, 2)


def forward_returns(fwd_bars, horizons=(5, 10, 20)):
    """% return from the next session's open to the close of each horizon bar."""
    entry = fwd_bars[0]["open"]
    out = {}
    for h in horizons:
        idx = min(h, len(fwd_bars)) - 1
        out[h] = round((fwd_bars[idx]["close"] / entry - 1.0) * 100.0, 2) if entry else None
    return out


# ---------------------------------------------------------------- backtest core
def candidate_outcome_row(screen_name, cand, fwd, spy_rets, horizon, cutoff):
    """One outcome row for a screened candidate — shared verbatim by the
    historical backtest and the live forward-log evaluator so both use the
    exact same execution model."""
    rets = forward_returns(fwd)
    direction = _direction_for(screen_name, cand["label"])
    row = {
        "screen": screen_name,
        "cutoff": cutoff,
        "ticker": cand["ticker"],
        "grade": cand["grade"],
        "label": cand["label"],
        "composite": cand["composite"],
        "direction": direction,
        "ret5": rets.get(5),
        "ret10": rets.get(10),
        "ret20": rets.get(20),
        "rel20": round(rets[20] - spy_rets[20], 2)
        if rets.get(20) is not None and spy_rets.get(20) is not None
        else None,
        "dir_ret20": None,
        "r_multiple": None,
        "exit_reason": None,
        "days_held": None,
    }
    if direction and rets.get(20) is not None:
        row["dir_ret20"] = rets[20] if direction == "long" else round(-rets[20], 2)
    if direction:
        row["mae20"], row["mfe20"] = excursions(fwd, horizon, direction)
    plan = cand["plan"]
    if screen_name in LONG_PLAN_SCREENS:
        out = long_trade_outcome(fwd, plan["stop"], plan["t1"], horizon)
        row.update({k: out[k] for k in ("r_multiple", "exit_reason", "days_held")})
    elif screen_name in SHORT_PLAN_SCREENS:
        out = short_trade_outcome(fwd, plan["stop"], plan["t1"], horizon)
        row.update({k: out[k] for k in ("r_multiple", "exit_reason", "days_held")})
    return row


def _direction_for(screen_name, label):
    if screen_name in LONG_PLAN_SCREENS or screen_name in LONG_RETURN_SCREENS:
        return "long"
    if screen_name in SHORT_PLAN_SCREENS or screen_name in SHORT_RETURN_SCREENS:
        return "short"
    if screen_name == "unusual-volume":
        return QUADRANT_DIRECTION.get(label)
    return None  # volatility: candidate pool, not a directional call


def run_backtest(bars_by_ticker, screens, cadence, horizon, universe_total=None, max_cutoffs=None):
    cfg = dict(DEFAULTS, watch_min_grade="D", exclude_earnings_within_days=0)
    bench_full = bars_by_ticker.get("SPY")
    tickers = {t: b for t, b in bars_by_ticker.items() if t != "SPY" and b}
    calendar_src = bench_full or max(tickers.values(), key=len)
    calendar = [b["date"] for b in calendar_src]
    positions = cutoff_positions(calendar, cfg["min_history"], horizon, cadence)
    if max_cutoffs:
        positions = positions[-max_cutoffs:]

    date_index = {t: [b["date"] for b in b_] for t, b_ in tickers.items()}
    rows = []
    for n_done, pos in enumerate(positions, 1):
        cutoff = calendar[pos]
        sliced = {t: b[: bisect_right(date_index[t], cutoff)] for t, b in tickers.items()}
        bench_sliced = slice_bars(bench_full, cutoff) if bench_full else None
        spy_fwd = bench_full[len(bench_sliced) :] if bench_full else []
        spy_rets = forward_returns(spy_fwd) if spy_fwd else {}
        for name in screens:
            result = screen(sliced, name, cfg, as_of=cutoff, bench=bench_sliced)
            for c in result["candidates"]:
                t = c["ticker"]
                cut_len = len(sliced[t])
                fwd = tickers[t][cut_len : cut_len + horizon + 1]
                if not fwd:
                    continue
                row = candidate_outcome_row(name, c, fwd, spy_rets, horizon, cutoff)
                if name == "volatility":
                    atr_fwd = atr(tickers[t][: cut_len + horizon])
                    close_fwd = tickers[t][min(cut_len + horizon, len(tickers[t])) - 1]["close"]
                    if atr_fwd and close_fwd:
                        row["atr_pct_t"] = c["signal"]["atr_pct"]
                        row["atr_pct_fwd"] = round(atr_fwd / close_fwd * 100.0, 2)
                rows.append(row)
        if n_done % 10 == 0 or n_done == len(positions):
            logger.info(
                "cutoff %d/%d (%s) — %d rows so far", n_done, len(positions), cutoff, len(rows)
            )

    return {
        "cutoffs": len(positions),
        "date_range": [calendar[positions[0]], calendar[positions[-1]]] if positions else None,
        "params": {
            "cadence": cadence,
            "horizon": horizon,
            "screens": screens,
            "universe_total": universe_total,
            "scanned": len(tickers),
            **{k: cfg[k] for k in ("min_history", "min_price", "min_adv_usd", "watch_min_grade")},
        },
        "rows": rows,
        "aggregate": aggregate(rows),
    }


# ---------------------------------------------------------------- aggregation
def _mean(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return round(vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2, 3)


def _bucket_stats(rows):
    dir_rets = [r.get("dir_ret20") for r in rows]
    # Median R, not mean: a stop sitting a fraction of a percent below entry
    # makes the R denominator near-zero, and a single such trade can push a
    # mean into the hundreds. The median is robust to those degenerate stops.
    r_mults = [r.get("r_multiple") for r in rows if r.get("r_multiple") is not None]
    wins = [v for v in dir_rets if v is not None]
    exits = {}
    for r in rows:
        if r.get("exit_reason"):
            exits[r["exit_reason"]] = exits.get(r["exit_reason"], 0) + 1
    stats = {
        "n": len(rows),
        "win_rate": round(sum(1 for v in wins if v > 0) / len(wins), 3) if wins else None,
        "mean_dir_ret20": _mean(dir_rets),
        "median_dir_ret20": _median(dir_rets),
        "mean_rel20": _mean([r.get("rel20") for r in rows]),
        "median_r": _median(r_mults),
        "median_mae20": _median([r.get("mae20") for r in rows]),
        "median_mfe20": _median([r.get("mfe20") for r in rows]),
        "exits": exits or None,
    }
    persist = [
        r["atr_pct_fwd"] / r["atr_pct_t"]
        for r in rows
        if r.get("atr_pct_fwd") and r.get("atr_pct_t")
    ]
    if persist:
        stats["mean_atr_persistence"] = round(sum(persist) / len(persist), 2)
    return stats


def aggregate(rows, min_n=30):
    out = {}
    for name in sorted({r["screen"] for r in rows}):
        srows = [r for r in rows if r["screen"] == name]
        by_grade = {
            g: _bucket_stats([r for r in srows if r["grade"] == g])
            for g in "ABCD"
            if any(r["grade"] == g for r in srows)
        }
        labels = sorted({r["label"] for r in srows})
        # Continuous labels (rvol_2.91x, atr_8.09pct) would make one bucket per
        # unique value — only break down genuinely categorical labels.
        by_label = (
            {lbl: _bucket_stats([r for r in srows if r["label"] == lbl]) for lbl in labels}
            if len(labels) <= 12
            else {"_note": f"{len(labels)} distinct continuous labels — breakdown skipped"}
        )
        means = [
            (g, by_grade[g]["mean_dir_ret20"])
            for g in "ABCD"
            if g in by_grade and by_grade[g]["mean_dir_ret20"] is not None
        ]
        ordering = all(a[1] >= b[1] for a, b in zip(means, means[1:])) if len(means) >= 2 else None
        enough = bool(means) and all(by_grade[g]["n"] >= min_n for g, _ in means)
        if ordering is None:
            verdict = "NOT_APPLICABLE"
        elif not enough:
            verdict = "INSUFFICIENT_DATA"
        else:
            verdict = "PASS" if ordering else "FAIL"
        out[name] = {
            "by_grade": by_grade,
            "by_label": by_label,
            "monotonic_dir_ret20": {
                "ordering_holds": ordering,
                "verdict": verdict,
                "min_n_per_grade": min_n,
            },
        }
    return out


# ---------------------------------------------------------------- reports
def render_markdown(result):
    p = result["params"]
    lines = [
        "# Swing Setup Screener — Backtest (grade-monotonicity study)",
        "",
        f"**Cutoffs:** {result['cutoffs']} ({result['date_range'][0]} → {result['date_range'][1]})"
        if result.get("date_range")
        else "**Cutoffs:** 0",
        f"**Universe:** {p['scanned']} tickers"
        + (f" of {p['universe_total']} matching today" if p.get("universe_total") else "")
        + f" | cadence {p['cadence']} sessions | horizon {p['horizon']} sessions"
        f" | rows {len(result['rows'])}",
        "",
        "## Read This First — What This Can and Cannot Prove",
        "",
    ]
    lines += [f"- {d}" for d in DISCLOSURES]
    for name, agg in result["aggregate"].items():
        mono = agg["monotonic_dir_ret20"]
        lines += ["", f"## {name}", ""]
        lines.append(
            f"**Grade monotonicity (mean direction-signed 20-session return): "
            f"{mono['verdict']}** (ordering holds: {mono['ordering_holds']},"
            f" min n per grade for a verdict: {mono['min_n_per_grade']})"
        )
        lines += [
            "",
            "| Grade | n | Win rate | Mean dir 20d % | Median dir 20d % | Mean vs SPY 20d % |"
            " Median R | Median MAE % | Median MFE % | Exits |",
            "|-------|---|----------|----------------|------------------|-------------------|"
            "----------|--------------|--------------|-------|",
        ]
        for g, s in agg["by_grade"].items():
            exits = (
                " ".join(f"{k}:{v}" for k, v in sorted(s["exits"].items())) if s["exits"] else "—"
            )
            extra = (
                f" atr_persist:{s['mean_atr_persistence']}" if "mean_atr_persistence" in s else ""
            )
            lines.append(
                f"| {g} | {s['n']} | {s['win_rate']} | {s['mean_dir_ret20']} |"
                f" {s['median_dir_ret20']} | {s['mean_rel20']} | {s['median_r']} |"
                f" {s['median_mae20']} | {s['median_mfe20']} | {exits}{extra} |"
            )
        if "_note" in agg["by_label"]:
            lines += ["", f"_By label: {agg['by_label']['_note']}_"]
        else:
            lines += ["", "By label:", ""]
            lines += [
                "| Label | n | Win rate | Mean dir 20d % | Median dir 20d % | Median R |"
                " Median MAE % | Median MFE % |",
                "|-------|---|----------|----------------|------------------|----------|"
                "--------------|--------------|",
            ]
            for lbl, s in agg["by_label"].items():
                lines.append(
                    f"| {lbl} | {s['n']} | {s['win_rate']} | {s['mean_dir_ret20']} |"
                    f" {s['median_dir_ret20']} | {s['median_r']} |"
                    f" {s['median_mae20']} | {s['median_mfe20']} |"
                )
    lines.append("")
    return "\n".join(lines)


def write_reports(result, output_dir, prefix):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    json_path = out / f"{prefix}_{stamp}.json"
    md_path = out / f"{prefix}_{stamp}.md"
    json_path.write_text(json.dumps(result, indent=2) + "\n")
    md_path.write_text(render_markdown(result))
    return str(json_path), str(md_path)


# ---------------------------------------------------------------- data / CLI
def bars_from_fixture(path):
    data = json.loads(Path(path).read_text())
    return data["bars"]


def build_arg_parser():
    p = argparse.ArgumentParser(
        description="Backtest the swing-setup-screener at historical cutoffs"
    )
    p.add_argument(
        "--screens",
        default="swing-long,swing-short,leaders,volatility,in-play,unusual-volume,weak",
        help="Comma-separated screen names (default: all seven)",
    )
    p.add_argument(
        "--period", default="3y", help="Bars history to fetch (yfinance period, default 3y)"
    )
    p.add_argument("--cadence", type=int, default=10, help="Sessions between cutoffs (default 10)")
    p.add_argument(
        "--horizon", type=int, default=20, help="Forward sessions per outcome (default 20)"
    )
    p.add_argument(
        "--max-cutoffs", type=int, default=None, help="Keep only the most recent N cutoffs"
    )
    p.add_argument("--tickers", help="Comma-separated tickers (skips the Yahoo universe screen)")
    p.add_argument("--universe-csv", help="CSV with a Ticker/Symbol column")
    p.add_argument(
        "--universe-size", type=int, default=2000, help="Yahoo universe cap (default 2000 = full)"
    )
    p.add_argument("--fixture", help="Offline JSON fixture: {bars:{TICKER:[...]}}")
    p.add_argument("--output-dir", default="reports/")
    p.add_argument("--output-prefix", default="swing_setups_backtest")
    return p


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    args = build_arg_parser().parse_args(argv)
    screens = [s.strip() for s in args.screens.split(",") if s.strip()]
    unknown = [s for s in screens if s not in SCREENS]
    if unknown:
        raise SystemExit(f"ERROR: unknown screens: {unknown}")

    universe_total = None
    if args.fixture:
        bars_by_ticker = bars_from_fixture(args.fixture)
    else:
        if args.tickers:
            tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        elif args.universe_csv:
            tickers = tickers_from_csv(args.universe_csv)
        else:
            tickers, universe_total = yahoo_universe(DEFAULTS, args.universe_size)
        fetch_list = tickers if "SPY" in tickers else [*tickers, "SPY"]
        logger.info("Fetching %s of daily bars for %d tickers...", args.period, len(fetch_list))
        bars_by_ticker = fetch_bars(fetch_list, period=args.period)
        bars_by_ticker = {t: strip_partial_bar(b) for t, b in bars_by_ticker.items()}

    result = run_backtest(
        bars_by_ticker,
        screens=screens,
        cadence=args.cadence,
        horizon=args.horizon,
        universe_total=universe_total,
        max_cutoffs=args.max_cutoffs,
    )
    json_path, md_path = write_reports(result, args.output_dir, args.output_prefix)
    for name, agg in result["aggregate"].items():
        logger.info("[%s] monotonicity: %s", name, agg["monotonic_dir_ret20"]["verdict"])
    print(json_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
