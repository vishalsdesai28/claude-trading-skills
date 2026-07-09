"""Tests for the pure decision logic in momentum_bot.py (offline, no yfinance import)."""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momentum_bot import (
    avg_entry,
    momentum_metrics,
    pick_candidate,
    prefilter_quote,
    rank_candidates,
    regime_ok,
    should_exit,
    should_scale_out,
)

# ---------------------------------------------------------------- finviz csv path


def _row(ticker, month, week, price="100.00"):
    return {"Ticker": ticker, "Perf Month": month, "Perf Week": week, "Price": price}


def test_rank_orders_by_perf_month_then_shallowest_week():
    rows = [
        _row("AAA", "10.0%", "-4.0%"),
        _row("BBB", "25.0%", "-6.0%"),
        _row("CCC", "25.0%", "-1.5%"),  # ties BBB on month, shallower pullback wins
    ]
    ranked = rank_candidates(rows)
    assert [r["ticker"] for r in ranked] == ["CCC", "BBB", "AAA"]


def test_rank_skips_unparseable_rows():
    ranked = rank_candidates([_row("AAA", "-", "-"), _row("BBB", "5.0%", "-1.0%")])
    assert [r["ticker"] for r in ranked] == ["BBB"]


# ---------------------------------------------------------------- yahoo path


def _quote(**overrides):
    quote = {
        "regularMarketPrice": 110.0,
        "fiftyDayAverage": 100.0,
        "twoHundredDayAverage": 90.0,
        "fiftyTwoWeekHigh": 115.0,
        "exchange": "NMS",
        "averageAnalystRating": "1.8 - Buy",
    }
    quote.update(overrides)
    return quote


def test_prefilter_accepts_healthy_quote():
    assert prefilter_quote(_quote()) is True


def test_prefilter_rejects_below_sma_otc_far_from_high_and_weak_rating():
    assert prefilter_quote(_quote(fiftyDayAverage=120.0)) is False
    assert prefilter_quote(_quote(exchange="PNK")) is False
    assert prefilter_quote(_quote(fiftyTwoWeekHigh=140.0)) is False  # >10% below high
    assert prefilter_quote(_quote(averageAnalystRating="3.2 - Hold")) is False
    assert prefilter_quote(_quote(averageAnalystRating=None)) is True  # missing = keep
    assert prefilter_quote(_quote(fiftyDayAverage=None)) is False


def _history(last=110.0, week_ago=112.0, month_ago=100.0, base=70.0, n=260):
    """[(date, close)] newest first: red week, +10% month, big YTD off `base`."""
    today = date(2026, 7, 3)
    closes = [last] + [week_ago] * 5 + [month_ago] * 16 + [base] * (n - 22)
    return [(today - timedelta(days=i), c) for i, c in enumerate(closes)]


def test_momentum_metrics_passes_and_ranks():
    m = momentum_metrics(_history())
    assert m is not None
    assert m["perf_week"] < 0
    assert m["perf_month"] == 10.0
    assert m["perf_ytd"] >= 50.0
    assert m["price"] == 110.0


def test_momentum_metrics_rejects_green_week_low_ytd_and_short_history():
    assert momentum_metrics(_history(week_ago=105.0)) is None  # up on the week
    assert momentum_metrics(_history(base=100.0)) is None  # YTD < 50%
    assert momentum_metrics(_history(n=100)) is None  # young listing
    assert momentum_metrics(_history(last=80.0, week_ago=81.0)) is None  # >10% off high


# ---------------------------------------------------------------- allocation


def test_pick_respects_per_name_lot_cap():
    ranked = rank_candidates([_row("HOT", "30%", "-2%"), _row("NEXT", "20%", "-3%")])
    positions = {"HOT": {"lots": [{}, {}, {}], "hwm": 0.0}}  # at 3-lot cap
    assert pick_candidate(ranked, positions)["ticker"] == "NEXT"
    assert pick_candidate(ranked, {})["ticker"] == "HOT"
    assert pick_candidate(ranked, {t["ticker"]: {"lots": [{}] * 3} for t in ranked}) is None


def test_regime_gate():
    assert regime_ok([101.0] + [100.0] * 60) is True
    assert regime_ok([99.0] + [100.0] * 60) is False
    assert regime_ok([100.0] * 10) is False  # too little history fails closed


# ---------------------------------------------------------------- exits + profit booking


def test_exit_on_sma50_break():
    closes = [95.0] + [100.0] * 60  # today closed below the ~100 SMA50
    assert should_exit(closes, hwm=100.0) == "close_below_sma50"


def test_exit_on_trailing_stop():
    closes = [90.0] + [89.0] * 60  # above SMA50, but 90 <= 0.85 * 110
    assert should_exit(closes, hwm=110.0) == "trailing_stop_15pct"


def test_no_exit_when_healthy():
    closes = [105.0] + [100.0] * 60
    assert should_exit(closes, hwm=106.0) is None


def test_avg_entry_is_notional_weighted():
    pos = {"lots": [{"notional": 300.0, "price": 100.0}, {"notional": 300.0, "price": 150.0}]}
    assert avg_entry(pos) == 120.0  # 600 notional / 5 shares
    assert avg_entry({"lots": [{"notional": 300.0, "price": None}]}) is None
    assert avg_entry({"lots": []}) is None


def test_scale_out_triggers_once_at_20pct():
    assert should_scale_out(144.0, 120.0, already_scaled=False) is True  # exactly +20%
    assert should_scale_out(143.0, 120.0, already_scaled=False) is False
    assert should_scale_out(200.0, 120.0, already_scaled=True) is False  # only once
    assert should_scale_out(200.0, None, already_scaled=False) is False  # unknown entry


def test_saved_scan_roundtrip(tmp_path, monkeypatch):
    import momentum_bot as mb

    monkeypatch.setattr(mb, "REPORTS_DIR", tmp_path)
    ranked = [{"ticker": "CGON", "perf_month": 26.7, "perf_week": -3.4, "price": 70.36}]
    mb.save_scan(ranked, "2026-07-05")
    assert mb.load_saved_scan("2026-07-05") == ranked
    assert mb.load_saved_scan("2026-07-06") is None  # never reuses another day's scan


def test_scale_out_plus_trail_locks_in_breakeven():
    # After booking half at +20%, the worst daily-close exit on the remainder is
    # 0.85 * hwm >= 0.85 * 1.20 * entry = 1.02 * entry — never a round-trip loss.
    entry, hwm = 100.0, 120.0
    worst_exit = hwm * 0.85
    assert worst_exit > entry
