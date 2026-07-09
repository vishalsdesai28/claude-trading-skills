"""Tests for the swing-setup-screener backtest harness."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest_swing_setups import (  # noqa: E402
    aggregate,
    cutoff_positions,
    forward_returns,
    long_trade_outcome,
    run_backtest,
    short_trade_outcome,
    slice_bars,
    write_reports,
)
from test_screen_swing_setups import make_bars, pullback_closes, uptrend  # noqa: E402


def bar(o, h, lo, c, v=1_000_000, d="2026-01-02"):
    return {"date": d, "open": o, "high": h, "low": lo, "close": c, "volume": v}


# ---------------------------------------------------------------- slicing
def test_slice_bars_inclusive():
    bars = make_bars(uptrend(30), start="2026-03-02")
    cut = bars[9]["date"]
    sliced = slice_bars(bars, cut)
    assert len(sliced) == 10
    assert sliced[-1]["date"] == cut
    assert slice_bars(bars, "2020-01-01") == []


def test_cutoff_positions_respect_warmup_horizon_cadence():
    dates = [b["date"] for b in make_bars(uptrend(300))]
    pos = cutoff_positions(dates, warmup=210, horizon=20, cadence=10)
    assert pos, "expected at least one cutoff"
    assert min(pos) >= 209  # 210 bars of history available (index >= 209)
    assert max(pos) <= len(dates) - 21  # 20 forward bars available
    assert all(b - a == 10 for a, b in zip(pos, pos[1:]))


# ---------------------------------------------------------------- long outcomes
def test_long_target_hit():
    fwd = [
        bar(100, 101, 99.5, 100.5),
        bar(100.5, 106, 100, 105.5),  # tags t1=105
    ]
    out = long_trade_outcome(fwd, stop=95.0, t1=105.0, horizon=20)
    assert out["exit_reason"] == "target"
    assert out["exit_price"] == 105.0
    assert abs(out["r_multiple"] - 1.0) < 0.01  # entry 100, risk 5, gain 5


def test_long_stop_first_on_ambiguous_bar():
    fwd = [bar(100, 106, 94, 100)]  # touches both stop 95 and t1 105
    out = long_trade_outcome(fwd, stop=95.0, t1=105.0, horizon=20)
    assert out["exit_reason"] == "stop"
    assert out["exit_price"] == 95.0
    assert out["r_multiple"] == -1.0


def test_long_gap_through_stop_exits_at_open():
    fwd = [
        bar(100, 101, 99, 99.5),
        bar(90, 91, 89, 90.5),  # gaps far below stop 95
    ]
    out = long_trade_outcome(fwd, stop=95.0, t1=110.0, horizon=20)
    assert out["exit_reason"] == "gap_stop"
    assert out["exit_price"] == 90
    assert out["r_multiple"] < -1.0  # worse than -1R, honestly recorded


def test_long_time_stop():
    fwd = [bar(100, 101, 99.5, 100.2) for _ in range(25)]
    out = long_trade_outcome(fwd, stop=95.0, t1=110.0, horizon=5)
    assert out["exit_reason"] == "time"
    assert out["days_held"] == 5


def test_long_invalidated_at_open():
    fwd = [bar(94, 95, 93, 94.5)]  # opens below the stop
    out = long_trade_outcome(fwd, stop=95.0, t1=110.0, horizon=20)
    assert out["exit_reason"] == "invalidated_at_open"
    assert out["r_multiple"] is None


# ---------------------------------------------------------------- short outcomes
def test_short_target_and_stop_first():
    fwd = [bar(100, 101, 94, 95.5)]  # tags t1=95 (low), no stop touch (105)
    out = short_trade_outcome(fwd, stop=105.0, t1=95.0, horizon=20)
    assert out["exit_reason"] == "target"
    assert abs(out["r_multiple"] - 1.0) < 0.01

    ambiguous = [bar(100, 106, 94, 100)]
    out2 = short_trade_outcome(ambiguous, stop=105.0, t1=95.0, horizon=20)
    assert out2["exit_reason"] == "stop"
    assert out2["r_multiple"] == -1.0


# ---------------------------------------------------------------- excursions
def test_excursions_direction_signed():
    from backtest_swing_setups import excursions

    fwd = [bar(100, 104, 95, 103), bar(103, 110, 102, 109)]
    mae, mfe = excursions(fwd, 20, "long")
    assert mae == -5.0 and mfe == 10.0  # adverse always <= 0, favorable >= 0
    mae_s, mfe_s = excursions(fwd, 20, "short")
    assert mae_s == -10.0 and mfe_s == 5.0


# ---------------------------------------------------------------- forward returns
def test_forward_returns_from_next_open():
    fwd = [bar(100, 102, 99, 101)] + [bar(101, 103, 100, 102) for _ in range(20)]
    rets = forward_returns(fwd, horizons=(5, 20))
    assert abs(rets[5] - 2.0) < 0.01  # entry open 100 -> close 102
    assert abs(rets[20] - 2.0) < 0.01


# ---------------------------------------------------------------- end to end
def engineered_universe():
    """PB sets up a swing-long pullback at the LAST possible cutoff, then runs to T1."""
    closes = pullback_closes()  # 260 bars, candidate at the end
    tail = [closes[-1] * (1 + 0.01 * i) for i in range(1, 26)]  # +25% grind up
    return {
        "PB": make_bars(closes + tail),
        "SPY": make_bars(uptrend(len(closes) + 25, step=0.3)),
    }


def test_run_backtest_end_to_end(tmp_path):
    bars = engineered_universe()
    result = run_backtest(bars, screens=["swing-long"], cadence=5, horizon=20, universe_total=None)
    assert result["cutoffs"] >= 1
    rows = result["rows"]
    assert any(r["ticker"] == "PB" for r in rows), "engineered candidate not found"
    pb = [r for r in rows if r["ticker"] == "PB"][-1]
    assert pb["screen"] == "swing-long"
    assert pb["grade"] in "ABCD"
    assert pb["dir_ret20"] is not None

    agg = result["aggregate"]
    assert ("swing-long" in agg) and agg["swing-long"]
    json_path, md_path = write_reports(result, tmp_path, "swing_setups_backtest")
    md = Path(md_path).read_text()
    assert "Survivorship" in md  # bias disclosure is mandatory
    assert "stop-first" in md  # execution model disclosed
    data = json.loads(Path(json_path).read_text())
    assert data["params"]["horizon"] == 20


def test_aggregate_grades_and_monotonicity():
    rows = []
    for grade, ret in (("A", 10.0), ("A", 8.0), ("B", 4.0), ("B", 2.0), ("C", -1.0), ("C", -3.0)):
        rows.append(
            {
                "screen": "swing-long",
                "grade": grade,
                "label": "pullback_zone",
                "ticker": "X",
                "cutoff": "2026-01-01",
                "dir_ret20": ret,
                "rel20": ret - 1.0,
                "r_multiple": ret / 5.0,
                "exit_reason": "time",
            }
        )
    agg = aggregate(rows)
    stats = agg["swing-long"]["by_grade"]
    assert stats["A"]["n"] == 2 and stats["A"]["mean_dir_ret20"] == 9.0
    assert stats["A"]["median_r"] == 1.8  # median, not mean — robust to tiny-risk outliers
    mono = agg["swing-long"]["monotonic_dir_ret20"]
    assert mono["ordering_holds"] is True
    assert mono["verdict"] == "INSUFFICIENT_DATA"  # n < 30 per grade — honest verdict
