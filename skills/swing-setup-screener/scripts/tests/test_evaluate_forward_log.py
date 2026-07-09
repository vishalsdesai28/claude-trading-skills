"""Tests for the forward-log evaluator."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluate_forward_log import (  # noqa: E402
    evaluate_reports,
    load_screener_reports,
    render_markdown,
    write_reports,
)
from test_screen_swing_setups import make_bars, uptrend  # noqa: E402


def nightly_report(session, ticker="PB", grade="A", screen_name="swing-long"):
    return {
        "screen": screen_name,
        "session": session,
        "candidates": [
            {
                "ticker": ticker,
                "grade": grade,
                "label": "pullback_zone",
                "composite": 90.0,
                "plan": {"stop": 90.0, "t1": 200.0},
            }
        ],
    }


def universe(n=300):
    return {"PB": make_bars(uptrend(n)), "SPY": make_bars(uptrend(n, step=0.3))}


def test_matured_report_is_scored():
    bars = universe()
    session = bars["SPY"][-30]["date"]  # 29 forward sessions available
    result = evaluate_reports([nightly_report(session)], bars, horizon=20)
    assert result["reports_matured"] == 1
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["ticker"] == "PB" and row["grade"] == "A"
    assert row["dir_ret20"] is not None and row["mae20"] is not None
    assert row["exit_reason"] is not None  # plan screen -> R-model applied


def test_immature_report_is_excluded_not_part_scored():
    bars = universe()
    recent = bars["SPY"][-5]["date"]  # only 4 forward sessions
    result = evaluate_reports([nightly_report(recent)], bars, horizon=20)
    assert result["reports_matured"] == 0
    assert result["reports_immature"] == 1
    assert result["rows"] == []


def test_missing_ticker_data_skipped_and_counted():
    bars = universe()
    session = bars["SPY"][-30]["date"]
    report = nightly_report(session, ticker="GONE")  # no bars for GONE
    result = evaluate_reports([report], bars, horizon=20)
    assert result["rows"] == []
    assert result["rows_skipped_incomplete_data"] == 1


def test_load_ignores_backtest_and_eval_files(tmp_path):
    (tmp_path / "swing_setups_swing_long_2026-07-08.json").write_text(
        json.dumps(nightly_report("2026-07-08"))
    )
    (tmp_path / "swing_setups_backtest_2026-07-08.json").write_text(
        json.dumps({"rows": [], "aggregate": {}})
    )
    (tmp_path / "swing_setups_forward_eval_2026-07-08.json").write_text(
        json.dumps({"kind": "forward_log_evaluation", "rows": []})
    )
    reports = load_screener_reports(tmp_path)
    assert len(reports) == 1
    assert reports[0]["screen"] == "swing-long"


def test_report_written_with_disclosures(tmp_path):
    bars = universe()
    session = bars["SPY"][-30]["date"]
    result = evaluate_reports([nightly_report(session)], bars, horizon=20)
    json_path, md_path = write_reports(result, tmp_path, "swing_setups_forward_eval")
    md = Path(md_path).read_text()
    assert "Zero pick-list bias" in md
    assert "proxy" in md
    assert json.loads(Path(json_path).read_text())["kind"] == "forward_log_evaluation"


def test_empty_maturity_renders_waiting_note():
    bars = universe()
    recent = bars["SPY"][-2]["date"]
    result = evaluate_reports([nightly_report(recent)], bars, horizon=20)
    md = render_markdown(result)
    assert "No matured picks yet" in md
