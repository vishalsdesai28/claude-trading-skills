#!/usr/bin/env python3
"""Forward-log evaluator — score the nightly screener picks that have matured.

This is the ZERO-BIAS half of the validation loop: each nightly
`reports/swing_setups_<screen>_<date>.json` recorded its picks before the
outcome existed, with the live earnings gate and regime gate active and no
survivorship bias in the pick list. Once a report's session is at least
`--horizon` trading sessions old, this script computes realized outcomes
under the SAME execution model as the backtest (shared code:
`candidate_outcome_row` — next-open entry, stop-first, gap-through-stop at
the open, horizon time-stop) and emits the same grade/label tables, so live
results are directly comparable to `swing_setups_backtest_*` reports.

Remaining honesty limits, printed on every report: the execution model is
still the mechanical proxy (discretionary playbook entries are not what is
being scored), outcomes of picks from consecutive nights overlap, and a few
weeks of cohorts is one market regime.
"""

import argparse
import json
import logging
import sys
from bisect import bisect_right
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_swing_setups import (  # noqa: E402
    aggregate,
    candidate_outcome_row,
    forward_returns,
)
from screen_swing_setups import fetch_bars, strip_partial_bar  # noqa: E402

logger = logging.getLogger("swing_forward_eval")

FORWARD_DISCLOSURES = [
    "**Zero pick-list bias:** picks were recorded before outcomes existed, with the"
    " live earnings and regime gates active — no survivorship, no look-ahead.",
    "**Execution model is still the proxy:** next-open entry, stop-first, horizon"
    " time-stop. Discretionary playbook entries are not what is being scored.",
    "**Overlap:** consecutive nightly picks share outcome windows; counts overstate"
    " independent evidence.",
    "**Sample:** early cohorts cover one regime; compare grade separation against the"
    " backtest tables before trusting levels.",
]


def load_screener_reports(reports_dir):
    """Nightly screener JSONs only — backtest and forward-eval files are excluded
    by shape (they carry 'rows', not 'candidates')."""
    reports = []
    for path in sorted(Path(reports_dir).glob("swing_setups_*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping unreadable %s: %s", path.name, exc)
            continue
        if isinstance(data, dict) and "candidates" in data and "screen" in data:
            reports.append(data)
    return reports


def evaluate_reports(reports, bars_by_ticker, horizon):
    """Score every report whose session has >= horizon forward calendar sessions;
    immature reports/rows are counted, never part-scored."""
    bench = bars_by_ticker.get("SPY")
    if not bench:
        raise ValueError("SPY bars required as the trading calendar")
    spy_dates = [b["date"] for b in bench]
    date_index = {
        t: [b["date"] for b in bars] for t, bars in bars_by_ticker.items() if t != "SPY" and bars
    }

    rows, immature_reports, immature_rows, sessions = [], 0, 0, set()
    for report in reports:
        session = report["session"]
        spy_cut = bisect_right(spy_dates, session)
        spy_fwd = bench[spy_cut:]
        if len(spy_fwd) < horizon:
            immature_reports += 1
            continue
        spy_rets = forward_returns(spy_fwd)
        sessions.add(session)
        for c in report["candidates"]:
            t = c["ticker"]
            if t not in date_index:
                immature_rows += 1
                continue
            cut = bisect_right(date_index[t], session)
            fwd = bars_by_ticker[t][cut : cut + horizon + 1]
            if len(fwd) < horizon:  # thin/stale ticker data — do not part-score
                immature_rows += 1
                continue
            rows.append(candidate_outcome_row(report["screen"], c, fwd, spy_rets, horizon, session))

    result = {
        "kind": "forward_log_evaluation",
        "sessions_evaluated": sorted(sessions),
        "reports_matured": len(sessions),
        "reports_immature": immature_reports,
        "rows_skipped_incomplete_data": immature_rows,
        "params": {"horizon": horizon},
        "rows": rows,
        "aggregate": aggregate(rows),
    }
    return result


def render_markdown(result):
    lines = [
        "# Swing Setup Screener — Forward-Log Evaluation",
        "",
        f"**Matured report sessions:** {result['reports_matured']}"
        f" ({', '.join(result['sessions_evaluated']) or 'none yet'})",
        f"**Immature (still inside the {result['params']['horizon']}-session window):**"
        f" {result['reports_immature']} report(s)"
        f" | rows skipped for incomplete data: {result['rows_skipped_incomplete_data']}"
        f" | scored rows: {len(result['rows'])}",
        "",
        "## Read This First",
        "",
    ]
    lines += [f"- {d}" for d in FORWARD_DISCLOSURES]
    if not result["rows"]:
        lines += [
            "",
            "_No matured picks yet — outcomes appear once nightly reports are"
            f" {result['params']['horizon']} trading sessions old._",
            "",
        ]
        return "\n".join(lines)
    for name, agg in result["aggregate"].items():
        mono = agg["monotonic_dir_ret20"]
        lines += ["", f"## {name}", ""]
        lines.append(
            f"**Grade monotonicity: {mono['verdict']}** (ordering holds: {mono['ordering_holds']})"
        )
        lines += [
            "",
            "| Grade | n | Win rate | Mean dir 20d % | Median dir 20d % | Median R |"
            " Median MAE % | Median MFE % |",
            "|-------|---|----------|----------------|------------------|----------|"
            "--------------|--------------|",
        ]
        for g, s in agg["by_grade"].items():
            lines.append(
                f"| {g} | {s['n']} | {s['win_rate']} | {s['mean_dir_ret20']} |"
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


def build_arg_parser():
    p = argparse.ArgumentParser(description="Score matured nightly screener picks")
    p.add_argument("--reports-dir", default="reports/", help="Where the nightly JSONs live")
    p.add_argument(
        "--horizon", type=int, default=20, help="Forward sessions per outcome (default 20)"
    )
    p.add_argument("--period", default="1y", help="Bars history to fetch (default 1y)")
    p.add_argument("--output-dir", default="reports/")
    p.add_argument("--output-prefix", default="swing_setups_forward_eval")
    return p


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    args = build_arg_parser().parse_args(argv)
    reports = load_screener_reports(args.reports_dir)
    if not reports:
        logger.info(
            "No nightly screener reports found in %s — nothing to evaluate.", args.reports_dir
        )
        return 0
    tickers = sorted({c["ticker"] for r in reports for c in r["candidates"]})
    logger.info(
        "Found %d nightly reports (%d unique tickers); fetching bars...",
        len(reports),
        len(tickers),
    )
    bars_by_ticker = fetch_bars([*tickers, "SPY"], period=args.period)
    bars_by_ticker = {t: strip_partial_bar(b) for t, b in bars_by_ticker.items()}
    result = evaluate_reports(reports, bars_by_ticker, args.horizon)
    json_path, md_path = write_reports(result, args.output_dir, args.output_prefix)
    logger.info(
        "Matured sessions: %d | scored rows: %d | immature reports: %d",
        result["reports_matured"],
        len(result["rows"]),
        result["reports_immature"],
    )
    print(json_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
