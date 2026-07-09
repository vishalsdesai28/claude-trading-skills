"""Tests for the pure signal engine (no network)."""

import math

import pytest
import signal_engine as se


def make_bars(closes, spread=5.0):
    """Build synthetic OHLC bars from a close series."""
    bars = []
    for i, c in enumerate(closes):
        bars.append(
            {
                "date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                "open": c - 1.0,
                "high": c + spread,
                "low": c - spread,
                "close": c,
            }
        )
    return bars


def trending_up(n=80, start=5000.0, step=10.0):
    return [start + i * step for i in range(n)]


def trending_down(n=80, start=5000.0, step=10.0):
    return [start - i * step for i in range(n)]


# --- indicators ---


def test_sma():
    assert se.sma([1, 2, 3, 4], 2) == 3.5
    assert se.sma([1, 2], 5) is None


def test_ema_converges_toward_recent_values():
    vals = [10.0] * 20 + [20.0] * 40
    ema = se.ema(vals, 20)
    assert 19.0 < ema <= 20.0


def test_atr_constant_range():
    bars = make_bars([100.0] * 20, spread=2.0)
    # every bar: high-low = 4, no gaps beyond that
    assert se.atr(bars, 14) == pytest.approx(4.0)


# --- regime ---


def test_classify_trend_up_down_range():
    assert se.classify_trend(trending_up()) == "uptrend"
    assert se.classify_trend(trending_down()) == "downtrend"
    flat = [5000.0 + (5 if i % 2 else -5) for i in range(80)]
    assert se.classify_trend(flat) == "range"
    assert se.classify_trend([5000.0] * 10) == "range"  # insufficient history


def test_vol_regime():
    assert se.vol_regime(12.0) == "calm"
    assert se.vol_regime(20.0) == "normal"
    assert se.vol_regime(30.0) == "stressed"


def test_expected_move():
    em = se.expected_move(5000.0, 20.0, 5)
    assert em == pytest.approx(5000 * 0.20 * math.sqrt(5 / 365), rel=1e-9)


def test_bs_put_sanity():
    # ATM put, S=K=100, sigma=0.2, T=1y, r=0.04 -> ~5.99 (Black-Scholes)
    p = se.bs_price(100, 100, 1.0, 0.20, r=0.04, kind="put")
    assert p == pytest.approx(5.99, abs=0.05)
    # deep OTM put worth ~0
    assert se.bs_price(100, 50, 5 / 365, 0.20, kind="put") < 0.01


# --- signal construction ---


def test_build_signals_uptrend_calm():
    bars = make_bars(trending_up())
    out = se.build_signals("ES", bars, vix=15.0)
    setups = {s["setup"]: s for s in out["signals"]}

    assert out["regime"]["trend"] == "uptrend"
    assert out["regime"]["vol"] == "calm"

    bo = setups["weekly_breakout"]
    assert bo["direction"] == "long"
    prior_week_high = max(b["high"] for b in bars[-5:])
    assert bo["entry"] > prior_week_high
    assert bo["stop"] < bo["entry"] < bo["target"]
    assert bo["rr"] >= 1.5
    # risk per contract uses point values (ES=50, MES=5)
    pts = bo["entry"] - bo["stop"]
    assert bo["risk_per_contract"]["ES"] == pytest.approx(pts * 50)
    assert bo["risk_per_contract"]["MES"] == pytest.approx(pts * 5)

    pcs = setups["put_credit_spread"]
    spot = bars[-1]["close"]
    assert pcs["direction"] == "neutral_bullish"
    assert pcs["short_strike"] < spot
    assert pcs["long_strike"] < pcs["short_strike"]
    assert 0 < pcs["est_credit"] < pcs["max_loss"]  # income = low RR, stated honestly
    assert pcs["rr"] < 1.0


def test_build_signals_downtrend_stands_aside():
    # Long-only skill: downtrends produce no trades (shorts + call spreads
    # both backtested to negative expectancy 2010-2026).
    bars = make_bars(trending_down())
    out = se.build_signals("NQ", bars, vix=20.0)
    setups = {s["setup"]: s for s in out["signals"]}
    assert "weekly_breakout" not in setups
    assert "put_credit_spread" not in setups
    assert "call_credit_spread" not in setups
    assert "stand_aside" in setups


def test_stressed_vol_stands_aside_entirely():
    bars = make_bars(trending_up())
    out = se.build_signals("ES", bars, vix=32.0)
    setups = {s["setup"] for s in out["signals"]}
    assert setups == {"stand_aside_premium"}


def test_range_regime_sells_put_spread_no_breakout():
    closes = [5000.0 + (20 if i % 2 else -20) for i in range(80)]
    bars = make_bars(closes)
    out = se.build_signals("ES", bars, vix=15.0)
    setups = {s["setup"]: s for s in out["signals"]}
    assert "weekly_breakout" not in setups
    assert setups["put_credit_spread"]["short_strike"] < bars[-1]["close"]


def test_strikes_rounded_to_step():
    bars = make_bars(trending_up())
    out = se.build_signals("ES", bars, vix=15.0)
    pcs = next(s for s in out["signals"] if s["setup"] == "put_credit_spread")
    assert pcs["short_strike"] % se.CONTRACTS["ES"]["strike_step"] == 0


# --- core trend position ---


def test_core_position_long_above_200sma():
    bars = make_bars(trending_up(n=220))
    out = se.build_signals("ES", bars, vix=15.0)
    core = next(s for s in out["signals"] if s["setup"] == "core_trend_position")
    assert core["state"] == "LONG"
    assert core["entry_trigger"] < bars[-1]["close"]
    assert (
        core["exit_trigger"] <= core["entry_trigger"] or core["exit_trigger"] == core["crash_line"]
    )
    assert core["distance_pct"] > 0


def test_core_position_flat_below_200sma():
    bars = make_bars(trending_down(n=220))
    out = se.build_signals("ES", bars, vix=15.0)
    core = next(s for s in out["signals"] if s["setup"] == "core_trend_position")
    assert core["state"] == "FLAT"
    assert core["direction"] == "none"


def test_core_position_omitted_without_200_bars():
    bars = make_bars(trending_up(n=80))
    out = se.build_signals("ES", bars, vix=15.0)
    assert not any(s["setup"] == "core_trend_position" for s in out["signals"])


def test_core_position_present_even_when_vol_stressed():
    # deliberately NOT vol-gated (the vix gate hurt the hold in backtest)
    bars = make_bars(trending_up(n=220))
    out = se.build_signals("ES", bars, vix=32.0)
    assert any(s["setup"] == "core_trend_position" for s in out["signals"])


def test_core_position_hold_zone_between_channel_floor_and_sma():
    # flat at 100 for 210 bars, then a small dip to just under the SMA
    closes = [100.0] * 215 + [99.0] * 5
    bars = make_bars(closes, spread=0.5)
    out = se.build_signals("ES", bars, vix=15.0)
    core = next(s for s in out["signals"] if s["setup"] == "core_trend_position")
    assert core["state"] == "HOLD_IF_LONG"  # 98% of SMA < 99 < SMA, above crash line


def test_core_position_crash_brake_flat_despite_sma():
    # long rise to 200, then a fast 16% waterfall: still above the lagging SMA
    # but below 88% of the 52-week high -> FLAT
    closes = [100 + i * 0.5 for i in range(200)] + [168.0] * 5
    bars = make_bars(closes, spread=1.0)
    out = se.build_signals("ES", bars, vix=15.0)
    core = next(s for s in out["signals"] if s["setup"] == "core_trend_position")
    assert bars[-1]["close"] > core["entry_trigger"]  # above the SMA...
    assert core["state"] == "FLAT"  # ...but the crash brake fired


# --- monthly income leg ---


def test_monthly_bull_put_spread_atm_in_uptrend():
    bars = make_bars(trending_up(n=220))
    out = se.build_signals("ES", bars, vix=15.0)
    m = next(s for s in out["signals"] if s["setup"] == "monthly_bull_put_spread")
    spot = bars[-1]["close"]
    assert abs(m["short_strike"] - spot) <= se.CONTRACTS["ES"]["strike_step"]  # at the money
    assert m["long_strike"] < m["short_strike"]
    assert 0 < m["est_credit"] < m["max_loss"] + m["est_credit"]
    # ATM credit is rich: a large fraction of the wing width
    assert m["est_credit"] > 0.25 * (m["short_strike"] - m["long_strike"])


def test_monthly_spread_absent_when_core_flat():
    bars = make_bars(trending_down(n=220))
    out = se.build_signals("ES", bars, vix=20.0)
    assert not any(s["setup"] == "monthly_bull_put_spread" for s in out["signals"])
