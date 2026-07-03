"""Trader Memory Core — review, postmortem, and MAE/MFE calculation."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import reflection_log  # noqa: E402
import thesis_store  # noqa: E402

logger = logging.getLogger(__name__)

JOURNAL_DIR_NAME = "journal"
REFLECTION_LOG_NAME = "reflection_log.md"
TERMINAL_STATUSES = {"CLOSED", "INVALIDATED"}
DEFAULT_BENCHMARK = "SPY"


# -- MAE / MFE ----------------------------------------------------------------


def compute_mae_mfe(thesis: dict, price_adapter: Any | None = None) -> dict[str, float | None]:
    """Compute Maximum Adverse Excursion and Maximum Favorable Excursion.

    Args:
        thesis: Thesis dict (must be CLOSED or ACTIVE with entry data).
        price_adapter: Object with get_daily_closes(ticker, from_date, to_date).
                       If None, returns nulls.

    Returns:
        {"mae_pct": float|None, "mfe_pct": float|None, "mae_mfe_source": str|None}
    """
    result = {"mae_pct": None, "mfe_pct": None, "mae_mfe_source": None}

    if price_adapter is None:
        return result

    entry_price = thesis.get("entry", {}).get("actual_price")
    entry_date = thesis.get("entry", {}).get("actual_date")
    if not entry_price or not entry_date:
        return result

    # Determine end date
    exit_date = thesis.get("exit", {}).get("actual_date")
    if not exit_date:
        # Use today for active theses
        exit_date = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00")

    # Normalize dates to YYYY-MM-DD
    from_date = entry_date[:10]
    to_date = exit_date[:10]

    try:
        prices = price_adapter.get_daily_closes(thesis["ticker"], from_date, to_date)
    except Exception as e:
        logger.warning("Failed to fetch prices for %s: %s", thesis["ticker"], e)
        return result

    if not prices:
        return result

    closes = [p["close"] for p in prices]
    min_close = min(closes)
    max_close = max(closes)

    mae_pct = ((min_close - entry_price) / entry_price) * 100
    mfe_pct = ((max_close - entry_price) / entry_price) * 100

    result["mae_pct"] = round(mae_pct, 2)
    result["mfe_pct"] = round(mfe_pct, 2)
    result["mae_mfe_source"] = "fmp_eod"

    return result


# -- Alpha attribution ---------------------------------------------------------


def compute_alpha(
    thesis: dict,
    benchmark_adapter: Any | None = None,
    benchmark: str = DEFAULT_BENCHMARK,
) -> dict:
    """Compute the trade's alpha vs a benchmark over the holding window.

    ``alpha = trade return - benchmark return`` measured across the same
    entry-to-exit dates. The trade return prefers the recorded
    ``outcome.pnl_pct`` (trim-aware, cumulative) and falls back to the raw
    entry→exit price move. The benchmark return uses the first and last daily
    close returned by ``benchmark_adapter.get_daily_closes(benchmark, ...)``.

    Args:
        thesis: A CLOSED / INVALIDATED thesis dict.
        benchmark_adapter: Object with ``get_daily_closes(ticker, from, to)``
            (e.g. FMPPriceAdapter, or a yfinance-backed adapter). ``None`` ⇒
            raw return only, alpha stays ``None``.
        benchmark: Benchmark symbol (default ``SPY``).

    Returns:
        {"raw_return_pct", "benchmark_return_pct", "alpha_pct",
         "benchmark", "holding_days", "alpha_source"} — any value may be None.
    """
    outcome = thesis.get("outcome", {})
    result = {
        "raw_return_pct": None,
        "benchmark_return_pct": None,
        "alpha_pct": None,
        "benchmark": benchmark,
        "holding_days": outcome.get("holding_days"),
        "alpha_source": None,
    }

    entry = thesis.get("entry", {})
    exit_data = thesis.get("exit", {})
    entry_price = entry.get("actual_price")
    entry_date = entry.get("actual_date")
    exit_price = exit_data.get("actual_price")
    exit_date = exit_data.get("actual_date")

    raw_return = outcome.get("pnl_pct")
    if raw_return is None and entry_price and exit_price:
        raw_return = (exit_price - entry_price) / entry_price * 100
    if raw_return is not None:
        result["raw_return_pct"] = round(raw_return, 2)

    if benchmark_adapter is None or not (entry_date and exit_date):
        return result

    try:
        prices = benchmark_adapter.get_daily_closes(benchmark, entry_date[:10], exit_date[:10])
    except Exception as e:  # network / API errors are non-fatal for the postmortem
        logger.warning("Failed to fetch benchmark %s: %s", benchmark, e)
        return result

    closes = [p["close"] for p in (prices or []) if p.get("close")]
    if len(closes) < 2 or not closes[0]:
        return result

    bench_return = (closes[-1] - closes[0]) / closes[0] * 100
    result["benchmark_return_pct"] = round(bench_return, 2)
    if result["raw_return_pct"] is not None:
        result["alpha_pct"] = round(result["raw_return_pct"] - bench_return, 2)
        result["alpha_source"] = "benchmark_eod"

    return result


def _thesis_rating(thesis: dict) -> str:
    """Decision-time label for the log tag (confidence → grade → type)."""
    conf = thesis.get("confidence")
    if conf:
        return str(conf)
    grade = (thesis.get("origin") or {}).get("screening_grade")
    if grade:
        return str(grade)
    return thesis.get("thesis_type", "long")


def _primary_pillar(thesis: dict) -> str:
    """The headline thesis pillar to judge held/failed (evidence → statement)."""
    evidence = thesis.get("evidence") or []
    if evidence:
        pillar = str(evidence[0]).strip()
    else:
        pillar = (thesis.get("thesis_statement") or "the core thesis").strip()
    return pillar[:100] + ("…" if len(pillar) > 100 else "")


def compose_reflection(thesis: dict, alpha_info: dict) -> str:
    """Build a terse 2-4 sentence reflection judging the closed trade.

    Deterministic template (no LLM) covering, in order: (1) whether the
    directional call was correct — citing the *alpha* figure, not the raw
    return; (2) which thesis pillar held or failed; (3) one concrete lesson.
    Kept compact so it can be re-injected into future analysis prompts.
    """
    ticker = thesis.get("ticker", "the position")
    alpha = alpha_info.get("alpha_pct")
    raw = alpha_info.get("raw_return_pct")
    bench = alpha_info.get("benchmark", DEFAULT_BENCHMARK)
    bench_ret = alpha_info.get("benchmark_return_pct")
    days = alpha_info.get("holding_days")
    exit_reason = (thesis.get("exit") or {}).get("exit_reason")
    pillar = _primary_pillar(thesis)
    window = f" over {days} days" if days else ""

    # 1. Directional call — cite alpha, not raw return.
    if alpha is not None:
        beat = alpha > 0
        verb = "beat" if beat else "lagged"
        s1 = (
            f"The long call on {ticker} {verb} {bench} by {abs(alpha):.1f}pp{window} "
            f"({raw:+.1f}% vs {bench} {bench_ret:+.1f}%), so the directional call was "
            f"{'correct' if beat else 'not worth the risk versus a passive hold'}."
        )
    elif raw is not None:
        beat = raw > 0
        s1 = (
            f"Alpha vs {bench} could not be computed (no benchmark data){window}; the raw "
            f"return was {raw:+.1f}%, so judge the call on absolute terms with caution."
        )
    else:
        beat = None
        s1 = "Neither the raw return nor the alpha could be computed from the recorded prices."

    # 2. Which pillar held / failed.
    if beat:
        s2 = f"The core pillar — {pillar} — held through the hold."
    elif beat is False:
        if exit_reason == "stop_hit":
            s2 = f"The stop fired before the core pillar ({pillar}) could play out."
        elif thesis.get("status") == "INVALIDATED":
            s2 = f"The thesis was invalidated: {pillar} broke down."
        else:
            s2 = f"The core pillar — {pillar} — did not carry the trade."
    else:
        s2 = f"With no return recorded, the fate of the core pillar ({pillar}) is unresolved."

    # 3. One concrete lesson.
    lesson = (thesis.get("outcome") or {}).get("lessons_learned")
    if lesson:
        s3 = f"Lesson: {str(lesson).strip()}"
    elif beat:
        s3 = "Lesson: this setup earned its alpha — repeat the entry discipline that produced it."
    elif beat is False and exit_reason == "stop_hit":
        s3 = "Lesson: size to the stop so a single invalidation stays within the risk budget."
    elif beat is False:
        s3 = "Lesson: define a sharper kill-criterion up front so a stalling pillar exits sooner."
    else:
        s3 = "Lesson: record entry/exit prices at close so the next postmortem can measure alpha."

    return " ".join([s1, s2, s3])


# -- Postmortem ----------------------------------------------------------------


def generate_postmortem(
    thesis_id: str,
    state_dir: str,
    price_adapter: Any | None = None,
    journal_dir: str | None = None,
    *,
    benchmark_adapter: Any | None = None,
    benchmark: str = DEFAULT_BENCHMARK,
    reflection_log_path: str | None = None,
    reflection_text: str | None = None,
    with_reflection: bool = True,
) -> str:
    """Generate a postmortem markdown report for a closed thesis.

    Also computes the trade's alpha vs ``benchmark`` and appends an
    alpha-attribution reflection to the reflection log (pending → resolved,
    idempotent). Re-running the postmortem never duplicates a log entry.

    Args:
        thesis_id: Thesis ID to generate postmortem for.
        state_dir: Path to state/theses/ directory.
        price_adapter: Optional adapter for MAE/MFE; also reused as the
            benchmark adapter when ``benchmark_adapter`` is not supplied.
        journal_dir: Path to journal directory (default: state/journal/).
        benchmark_adapter: Optional adapter for the benchmark return.
        benchmark: Benchmark symbol for the alpha figure (default SPY).
        reflection_log_path: Override for the reflection log file
            (default: ``<journal_dir>/reflection_log.md``).
        reflection_text: Caller-supplied reflection prose; when omitted a
            deterministic template reflection is composed.
        with_reflection: Set False to skip alpha + reflection entirely.

    Returns:
        Path to the generated postmortem file.
    """
    state_path = Path(state_dir)
    thesis = thesis_store.get(state_path, thesis_id)

    if thesis["status"] not in ("CLOSED", "INVALIDATED"):
        raise ValueError(
            f"Postmortem requires CLOSED or INVALIDATED thesis, got status={thesis['status']}"
        )

    # Compute MAE/MFE if possible
    mae_mfe = compute_mae_mfe(thesis, price_adapter)
    thesis["outcome"]["mae_pct"] = mae_mfe["mae_pct"]
    thesis["outcome"]["mfe_pct"] = mae_mfe["mfe_pct"]
    thesis["outcome"]["mae_mfe_source"] = mae_mfe["mae_mfe_source"]

    # Resolve journal dir / reflection log path early (needed for the log).
    if journal_dir:
        j_dir = Path(journal_dir)
    else:
        j_dir = state_path.parent / JOURNAL_DIR_NAME
    j_dir.mkdir(parents=True, exist_ok=True)
    log_path = Path(reflection_log_path) if reflection_log_path else j_dir / REFLECTION_LOG_NAME

    # Compute alpha + reflection (benchmark adapter falls back to price adapter).
    reflection = None
    if with_reflection:
        bench_adapter = benchmark_adapter if benchmark_adapter is not None else price_adapter
        alpha_info = compute_alpha(thesis, bench_adapter, benchmark)
        thesis["outcome"]["raw_return_pct"] = alpha_info["raw_return_pct"]
        thesis["outcome"]["benchmark"] = alpha_info["benchmark"]
        thesis["outcome"]["benchmark_return_pct"] = alpha_info["benchmark_return_pct"]
        thesis["outcome"]["alpha_pct"] = alpha_info["alpha_pct"]
        thesis["outcome"]["alpha_source"] = alpha_info["alpha_source"]
        reflection = reflection_text or compose_reflection(thesis, alpha_info)
        thesis["outcome"]["reflection"] = reflection

    # Single outcome update (MAE/MFE + alpha + reflection).
    thesis_store.update(state_path, thesis_id, {"outcome": thesis["outcome"]})

    content = _render_postmortem(thesis)
    pm_path = j_dir / f"pm_{thesis_id}.md"
    pm_path.write_text(content)

    # Reflection log: pending → resolved lifecycle, both steps idempotent.
    if with_reflection and reflection is not None:
        reflection_log.store_pending(
            log_path,
            thesis_id,
            thesis["ticker"],
            _thesis_rating(thesis),
            thesis.get("thesis_statement") or "",
        )
        reflection_log.resolve(
            log_path,
            thesis_id,
            raw_return=alpha_info["raw_return_pct"],
            alpha=alpha_info["alpha_pct"],
            holding_days=alpha_info["holding_days"],
            reflection=reflection,
        )

    logger.info("Generated postmortem: %s", pm_path)
    return str(pm_path)


def _render_postmortem(thesis: dict) -> str:
    """Render postmortem markdown from thesis data."""
    entry = thesis.get("entry", {})
    exit_data = thesis.get("exit", {})
    outcome = thesis.get("outcome", {})
    position = thesis.get("position") or {}

    evidence_list = "\n".join(f"- {e}" for e in thesis.get("evidence", [])) or "- (none recorded)"

    kill_list = "\n".join(f"- {k}" for k in thesis.get("kill_criteria", [])) or "- (none recorded)"

    def _fmt(val, suffix=""):
        if val is None:
            return "—"
        return f"{val}{suffix}"

    benchmark = outcome.get("benchmark") or DEFAULT_BENCHMARK

    return f"""# Postmortem: {thesis["thesis_id"]}

**Ticker:** {thesis["ticker"]}
**Type:** {thesis["thesis_type"]}
**Status:** {thesis["status"]}

## Thesis

{thesis.get("thesis_statement", "(no statement)")}

## Timeline

| Event | Date | Price |
|-------|------|-------|
| Created | {thesis.get("created_at", "—")} | — |
| Entry | {_fmt(entry.get("actual_date"))} | {_fmt(entry.get("actual_price"))} |
| Exit | {_fmt(exit_data.get("actual_date"))} | {_fmt(exit_data.get("actual_price"))} |

## Outcome

| Metric | Value |
|--------|-------|
| P&L ($) | {_fmt(outcome.get("pnl_dollars"))} |
| P&L (%) | {_fmt(outcome.get("pnl_pct"), "%")} |
| Holding Days | {_fmt(outcome.get("holding_days"))} |
| Exit Reason | {_fmt(exit_data.get("exit_reason"))} |
| MAE (%) | {_fmt(outcome.get("mae_pct"), "%")} |
| MFE (%) | {_fmt(outcome.get("mfe_pct"), "%")} |
| {benchmark} Return (%) | {_fmt(outcome.get("benchmark_return_pct"), "%")} |
| Alpha vs {benchmark} (pp) | {_fmt(outcome.get("alpha_pct"))} |

## Position

| Metric | Value |
|--------|-------|
| Shares | {_fmt(position.get("shares"))} |
| Position Value | {_fmt(position.get("position_value"))} |
| Risk ($) | {_fmt(position.get("risk_dollars"))} |

## Evidence at Entry

{evidence_list}

## Kill Criteria

{kill_list}

## Lessons Learned

{outcome.get("lessons_learned") or "(not yet recorded)"}

## Reflection

{outcome.get("reflection") or "(not generated)"}
"""


# -- Summary Stats -------------------------------------------------------------


def summary_stats(state_dir: str) -> dict:
    """Compute summary statistics across all terminal theses with P&L.

    Includes CLOSED theses and INVALIDATED theses that have recorded P&L.

    Returns:
        Dict with win_rate, avg_pnl_pct, count, and per-type breakdown.
    """
    state_path = Path(state_dir)
    closed = thesis_store.query(state_path, status="CLOSED")
    invalidated = thesis_store.query(state_path, status="INVALIDATED")
    all_terminal = closed + invalidated

    if not all_terminal:
        return {"count": 0, "win_rate": None, "avg_pnl_pct": None, "by_type": {}}

    stats = {
        "count": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl_pct": 0.0,
        "by_type": {},
    }

    for entry in all_terminal:
        thesis = thesis_store.get(state_path, entry["thesis_id"])
        pnl_pct = thesis.get("outcome", {}).get("pnl_pct")
        if pnl_pct is None:
            continue

        stats["count"] += 1
        stats["total_pnl_pct"] += pnl_pct
        if pnl_pct >= 0:
            stats["wins"] += 1
        else:
            stats["losses"] += 1

        ttype = thesis.get("thesis_type", "unknown")
        if ttype not in stats["by_type"]:
            stats["by_type"][ttype] = {"count": 0, "wins": 0, "total_pnl_pct": 0.0}
        stats["by_type"][ttype]["count"] += 1
        stats["by_type"][ttype]["total_pnl_pct"] += pnl_pct
        if pnl_pct >= 0:
            stats["by_type"][ttype]["wins"] += 1

    result = {
        "count": stats["count"],
        "win_rate": round(stats["wins"] / stats["count"], 4) if stats["count"] else None,
        "avg_pnl_pct": round(stats["total_pnl_pct"] / stats["count"], 2)
        if stats["count"]
        else None,
        "by_type": {},
    }

    for ttype, ts in stats["by_type"].items():
        result["by_type"][ttype] = {
            "count": ts["count"],
            "win_rate": round(ts["wins"] / ts["count"], 4) if ts["count"] else None,
            "avg_pnl_pct": round(ts["total_pnl_pct"] / ts["count"], 2) if ts["count"] else None,
        }

    return result


def _matches_as_of(entry: dict, as_of: str | None) -> bool:
    if not as_of:
        return True
    if entry.get("status") in TERMINAL_STATUSES:
        return True
    next_review = entry.get("next_review_date")
    return bool(next_review and next_review <= as_of)


def summary_entries(
    state_dir: str,
    *,
    ticker: str | None = None,
    status: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
    by: str | None = None,
) -> dict:
    """Build filtered review-summary data from the lightweight index."""
    state_path = Path(state_dir)
    entries = thesis_store.query(
        state_path,
        ticker=ticker,
        status=status,
        date_from=since,
    )
    entries = [e for e in entries if _matches_as_of(e, as_of)]

    result = {
        "count": len(entries),
        "filters": {
            "ticker": ticker,
            "status": status,
            "since": since,
            "as_of": as_of,
        },
        "entries": entries,
    }
    if by:
        grouped: dict[str, int] = {}
        for entry in entries:
            key = str(entry.get(by) or "unknown")
            grouped[key] = grouped.get(key, 0) + 1
        result["by"] = by
        result["groups"] = grouped
    return result


def format_compact_summary(summary: dict) -> str:
    """Render one line per thesis for CLI scanning."""
    lines = []
    for entry in summary["entries"]:
        parts = [
            entry["thesis_id"],
            entry.get("ticker", "?"),
            entry.get("status", "?"),
            entry.get("thesis_type", "?"),
            f"created={entry.get('created_at', '—')}",
        ]
        next_review = entry.get("next_review_date")
        if next_review:
            parts.append(f"next_review={next_review}")
        lines.append(" | ".join(parts))
    return "\n".join(lines) if lines else "(no theses matched)"


def _terminal_event_date(thesis: dict) -> str | None:
    exit_date = thesis.get("exit", {}).get("actual_date")
    if exit_date:
        return exit_date[:10]
    for event in reversed(thesis.get("status_history", [])):
        if event.get("status") in TERMINAL_STATUSES:
            at = event.get("at")
            if at:
                return at[:10]
    return None


def _month_bounds(month: str) -> tuple[str, str]:
    try:
        start = datetime.strptime(month, "%Y-%m").date()
    except ValueError as e:
        raise ValueError("--month must be YYYY-MM") from e
    if start.month == 12:
        next_month = start.replace(year=start.year + 1, month=1)
    else:
        next_month = start.replace(month=start.month + 1)
    end = next_month - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def monthly_report(
    state_dir: str,
    month: str,
    *,
    journal_dir: str | None = None,
    output: str | None = None,
) -> str:
    """Generate a monthly review markdown report for terminal theses."""
    start, end = _month_bounds(month)
    state_path = Path(state_dir)

    terminal_entries = thesis_store.query(state_path, status="CLOSED") + thesis_store.query(
        state_path, status="INVALIDATED"
    )
    theses = []
    for entry in terminal_entries:
        thesis = thesis_store.get(state_path, entry["thesis_id"])
        event_date = _terminal_event_date(thesis)
        if event_date and start <= event_date <= end:
            theses.append((event_date, thesis))
    theses.sort(key=lambda item: (item[0], item[1]["ticker"]))

    content = _render_monthly_report(month, start, end, theses)
    if output:
        out_path = Path(output)
    else:
        j_dir = Path(journal_dir) if journal_dir else state_path.parent / JOURNAL_DIR_NAME
        out_path = j_dir / f"monthly-review-{month}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content)
    return str(out_path)


def _render_monthly_report(month: str, start: str, end: str, theses: list[tuple[str, dict]]) -> str:
    pnl_values = [
        t.get("outcome", {}).get("pnl_pct")
        for _, t in theses
        if t.get("outcome", {}).get("pnl_pct") is not None
    ]
    wins = sum(1 for p in pnl_values if p >= 0)
    avg_pnl = round(sum(pnl_values) / len(pnl_values), 2) if pnl_values else None

    distribution: dict[str, int] = {}
    lessons = []
    rows = []
    for event_date, thesis in theses:
        outcome = thesis.get("outcome", {})
        exit_data = thesis.get("exit", {})
        reason = exit_data.get("exit_reason") or thesis.get("status")
        distribution[reason] = distribution.get(reason, 0) + 1
        lesson = outcome.get("lessons_learned")
        if lesson:
            lessons.append(f"- {thesis['ticker']}: {lesson}")
        rows.append(
            "| {date} | {ticker} | {status} | {ttype} | {pnl} | {reason} |".format(
                date=event_date,
                ticker=thesis["ticker"],
                status=thesis["status"],
                ttype=thesis["thesis_type"],
                pnl=outcome.get("pnl_pct", "—"),
                reason=reason,
            )
        )

    if not rows:
        rows.append("| — | — | — | — | — | — |")
    distribution_lines = [f"- {key}: {count}" for key, count in sorted(distribution.items())]
    if not distribution_lines:
        distribution_lines = ["- (none)"]
    if not lessons:
        lessons = ["- (none recorded)"]

    win_rate = round(wins / len(pnl_values), 4) if pnl_values else None
    return f"""# Monthly Review: {month}

**Window:** {start} to {end}

## P&L Summary

- Closed/invalidated theses: {len(theses)}
- Theses with P&L: {len(pnl_values)}
- Win rate: {win_rate}
- Average P&L (%): {avg_pnl}

## Closed Trade Roster

| Date | Ticker | Status | Type | P&L (%) | Outcome |
|------|--------|--------|------|---------|---------|
{chr(10).join(rows)}

## Postmortem Outcome Distribution

{chr(10).join(distribution_lines)}

## Top Lessons

{chr(10).join(lessons)}
"""


# -- CLI -----------------------------------------------------------------------


def _build_price_adapter(api_key: str | None) -> Any | None:
    """Construct an FMP price adapter if a key is available, else None.

    Used for both MAE/MFE (ticker prices) and the alpha benchmark (SPY).
    Import is deferred so the module stays importable with the stdlib alone.
    """
    if not (api_key or os.environ.get("FMP_API_KEY")):
        logger.info("No FMP API key — postmortem will omit MAE/MFE and alpha.")
        return None
    try:
        from fmp_price_adapter import FMPPriceAdapter

        return FMPPriceAdapter(api_key=api_key)
    except Exception as e:  # missing key / import issue → graceful degrade
        logger.warning("Could not build price adapter: %s", e)
        return None


def main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Trader Memory Core — review tools")
    parser.add_argument("--state-dir", default="state/theses")
    sub = parser.add_subparsers(dest="command")

    # review-due
    due_p = sub.add_parser("review-due", help="List theses due for review")
    due_p.add_argument("--as-of", default=None)

    # postmortem
    pm_p = sub.add_parser("postmortem", help="Generate postmortem for a thesis")
    pm_p.add_argument("thesis_id")
    pm_p.add_argument("--journal-dir", default=None)
    pm_p.add_argument(
        "--benchmark", default=DEFAULT_BENCHMARK, help="Alpha benchmark (default SPY)"
    )
    pm_p.add_argument("--api-key", default=None, help="FMP API key (else $FMP_API_KEY)")
    pm_p.add_argument("--reflection-log", default=None, help="Override reflection log path")
    pm_p.add_argument("--no-reflection", action="store_true", help="Skip alpha + reflection")

    # past-context (inject prior reflections into an analysis prompt)
    pc_p = sub.add_parser("past-context", help="Print past-context block for a ticker")
    pc_p.add_argument("--ticker", required=True)
    pc_p.add_argument("--reflection-log", default=None, help="Reflection log path")
    pc_p.add_argument("--journal-dir", default=None)
    pc_p.add_argument("--n-same", type=int, default=3)
    pc_p.add_argument("--n-cross", type=int, default=3)

    # summary
    summary_p = sub.add_parser("summary", help="Show summary statistics")
    summary_p.add_argument("--ticker", default=None)
    summary_p.add_argument("--status", default=None)
    summary_p.add_argument("--since", default=None, help="Filter by created_at >= YYYY-MM-DD")
    summary_p.add_argument("--as-of", default=None, help="Review-due snapshot date YYYY-MM-DD")
    summary_p.add_argument("--by", choices=["status", "thesis_type"], default=None)
    summary_p.add_argument("--compact", action="store_true")

    monthly_p = sub.add_parser("monthly-report", help="Generate monthly review markdown")
    monthly_p.add_argument("--month", required=True, help="Month in YYYY-MM")
    monthly_p.add_argument("--journal-dir", default=None)
    monthly_p.add_argument("--output", default=None)

    args = parser.parse_args(argv)

    if args.command == "review-due":
        as_of = args.as_of or datetime.utcnow().strftime("%Y-%m-%d")
        results = thesis_store.list_review_due(Path(args.state_dir), as_of)
        print(json.dumps(results, indent=2))
    elif args.command == "postmortem":
        adapter = None
        if not args.no_reflection:
            adapter = _build_price_adapter(args.api_key)
        path = generate_postmortem(
            args.thesis_id,
            args.state_dir,
            price_adapter=adapter,
            journal_dir=args.journal_dir,
            benchmark=args.benchmark,
            reflection_log_path=args.reflection_log,
            with_reflection=not args.no_reflection,
        )
        print(f"Postmortem generated: {path}")
    elif args.command == "past-context":
        if args.reflection_log:
            log_path = args.reflection_log
        else:
            j_dir = (
                Path(args.journal_dir)
                if args.journal_dir
                else (Path(args.state_dir).parent / JOURNAL_DIR_NAME)
            )
            log_path = j_dir / REFLECTION_LOG_NAME
        block = reflection_log.get_past_context(
            log_path, args.ticker, n_same=args.n_same, n_cross=args.n_cross
        )
        print(block or "(no past context)")
    elif args.command == "summary":
        if not any([args.ticker, args.status, args.since, args.as_of, args.by, args.compact]):
            s = summary_stats(args.state_dir)
        else:
            s = summary_entries(
                args.state_dir,
                ticker=args.ticker,
                status=args.status,
                since=args.since,
                as_of=args.as_of,
                by=args.by,
            )
        if args.compact:
            print(format_compact_summary(s))
        else:
            print(json.dumps(s, indent=2))
    elif args.command == "monthly-report":
        path = monthly_report(
            args.state_dir,
            args.month,
            journal_dir=args.journal_dir,
            output=args.output,
        )
        print(f"Monthly report generated: {path}")
    else:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)
