"""Tests for the weekly backtest mechanics (no network)."""

import backtest_weekly as bt
import pytest


def bar(date, o, h, lo, c):
    return {"date": date, "open": o, "high": h, "low": lo, "close": c}


def uptrend_history(weeks=14, start=5000.0):
    """Weeks of 5 daily bars, steadily rising: establishes an uptrend regime."""
    bars = []
    price = start
    d = 0
    for _ in range(weeks):
        for _ in range(5):
            bars.append(
                bar(
                    f"2026-01-{d + 1:02d}" if d < 28 else f"2026-02-{d - 27:02d}",
                    price,
                    price + 8,
                    price - 8,
                    price + 5,
                )
            )
            price += 5
            d += 1
    # give bars real sequential ISO dates instead
    import datetime

    day = datetime.date(2026, 1, 5)  # a Monday
    for b in bars:
        while day.weekday() >= 5:
            day += datetime.timedelta(days=1)
        b["date"] = day.isoformat()
        day += datetime.timedelta(days=1)
    return bars


def test_weekly_groups():
    bars = uptrend_history(weeks=3)
    weeks = bt.weekly_groups(bars)
    assert len(weeks) == 3
    assert all(len(w) == 5 for w in weeks)


def test_breakout_target_hit_is_2r():
    bars = uptrend_history()
    # decision from last full week; craft the "next week" to trigger and run to target
    hist = bars[:-5]
    prior_week = bt.weekly_groups(hist)[-1]
    prior_high = max(b["high"] for b in prior_week)
    a = bt.atr_at(hist, 14)
    entry = prior_high + bt.BUFFER_ATR * a
    target = entry + bt.TARGET_ATR * a
    next_week = [
        bar("2026-06-01", entry - 5, entry + 1, entry - 10, entry),  # triggers
        bar("2026-06-02", entry, target + 5, entry - a * 0.5, target + 1),  # runs to target
        bar("2026-06-03", target, target + 2, target - 2, target),
        bar("2026-06-04", target, target + 2, target - 2, target),
        bar("2026-06-05", target, target + 2, target - 2, target),
    ]
    trades = bt.backtest_breakout(hist + next_week, min_history=60)
    assert trades, "expected a triggered trade"
    t = trades[-1]
    assert t["outcome"] == "target"
    assert t["r"] == pytest.approx(bt.TARGET_ATR / bt.STOP_ATR, rel=1e-6)


def test_breakout_same_bar_stop_and_target_counts_stop():
    bars = uptrend_history()
    hist = bars[:-5]
    prior_week = bt.weekly_groups(hist)[-1]
    prior_high = max(b["high"] for b in prior_week)
    a = bt.atr_at(hist, 14)
    entry = prior_high + bt.BUFFER_ATR * a
    stop = entry - bt.STOP_ATR * a
    target = entry + bt.TARGET_ATR * a
    # one violent bar touches entry, target AND stop -> conservative: stop
    next_week = [bar("2026-06-01", entry - 5, target + 5, stop - 5, entry)] + [
        bar(f"2026-06-0{i}", entry, entry + 1, entry - 1, entry) for i in range(2, 6)
    ]
    trades = bt.backtest_breakout(hist + next_week, min_history=60)
    t = trades[-1]
    assert t["outcome"] == "stop"
    assert t["r"] == pytest.approx(-1.0)


def test_breakout_no_trigger_no_trade():
    bars = uptrend_history()
    hist = bars[:-5]
    quiet = [bar(f"2026-06-0{i}", 1000, 1001, 999, 1000) for i in range(1, 6)]
    trades = bt.backtest_breakout(hist + quiet, min_history=60)
    dates = {t["entry_date"] for t in trades}
    assert not any(d.startswith("2026-06") for d in dates)


def test_put_spread_expires_worthless_keeps_credit():
    bars = uptrend_history()
    vix = {b["date"]: 15.0 for b in bars}
    trades = bt.backtest_put_spread(bars, vix, min_history=60)
    assert trades
    # market kept rising, all spreads expire worthless -> pnl == credit
    for t in trades:
        assert t["outcome"] == "expired_worthless"
        assert t["pnl_points"] == pytest.approx(t["credit"])
        assert 0 < t["r"] < 1.0


def test_summarize():
    trades = [{"r": 2.0}, {"r": -1.0}, {"r": 2.0}, {"r": -1.0}]
    s = bt.summarize(trades)
    assert s["trades"] == 4
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["avg_r"] == pytest.approx(0.5)
    assert s["profit_factor"] == pytest.approx(2.0)
    assert s["max_drawdown_r"] == pytest.approx(-1.0)
    assert bt.summarize([]) == {"trades": 0}


def test_core_position_rides_uptrend():
    bars = uptrend_history(weeks=60)  # 300 bars, steady rise (warmup needs 262)
    cp = bt.backtest_core_position(bars, lev=1.0)
    assert cp["cagr"] > 0
    assert cp["time_in_market"] > 0.9  # always above 200d SMA once warmed up
    assert cp["final_multiple"] > 1.0
    # leverage scales returns
    cp2 = bt.backtest_core_position(bars, lev=2.0)
    assert cp2["final_multiple"] > cp["final_multiple"]


def test_core_position_stays_flat_in_downtrend():
    bars = uptrend_history(weeks=60)
    for b in bars:  # mirror into a decline
        b["open"], b["high"], b["low"], b["close"] = (
            10000 - b["open"],
            10000 - b["low"],
            10000 - b["high"],
            10000 - b["close"],
        )
    cp = bt.backtest_core_position(bars, lev=1.0)
    assert cp["time_in_market"] < 0.1
    assert cp["final_multiple"] >= 0.99  # flat = capital preserved


def test_monthly_spread_profitable_in_rising_market():
    bars = uptrend_history(weeks=80)  # ~18 months rising
    vix = {b["date"]: 15.0 for b in bars}
    trades = bt.backtest_monthly_spread(bars, vix, min_history=262)
    assert trades
    wins = [t for t in trades if t["pnl_points"] > 0]
    assert len(wins) == len(trades)  # every month up -> every spread wins
    for t in trades:
        assert t["outcome"] == "expired_worthless"
        assert t["pnl_points"] == pytest.approx(t["credit"])


def test_monthly_spread_managed_redeploys_in_rising_market():
    bars = uptrend_history(weeks=80)
    vix = {b["date"]: 15.0 for b in bars}
    held = bt.backtest_monthly_spread(bars, vix, min_history=262)
    managed = bt.backtest_monthly_spread(
        bars, vix, min_history=262, take_profit=0.5, stop_mult=2.0, redeploy=True
    )
    assert len(managed) == len(held)
    # steadily rising market: profit-takes fire and chain into new spreads
    assert any(t["entries"] > 1 for t in managed)
    assert all(t["pnl_points"] > 0 for t in managed)
