"""Tests for the swing-setup-screener (7-screen EOD suite)."""

import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from screen_swing_setups import (  # noqa: E402
    DEFAULTS,
    SCREENS,
    atr,
    close_location,
    detect_in_play,
    detect_leaders,
    detect_swing_long,
    detect_swing_short,
    detect_unusual_volume,
    detect_volatility,
    detect_weak,
    grade_for,
    invalidation_reasons,
    rvol_last,
    screen,
    sma,
    strip_partial_bar,
    write_reports,
)

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------- fixtures
def make_bars(closes, volumes=None, start="2026-01-02", wick=0.005, override_last=None):
    """Synthetic OHLCV bars (oldest first) from a close series."""
    d0 = dt.date.fromisoformat(start)
    volumes = volumes or [1_000_000] * len(closes)
    bars = []
    prev = closes[0]
    for i, (c, v) in enumerate(zip(closes, volumes)):
        o = prev
        bars.append(
            {
                "date": (d0 + dt.timedelta(days=i)).isoformat(),
                "open": round(o, 4),
                "high": round(max(o, c) * (1 + wick), 4),
                "low": round(min(o, c) * (1 - wick), 4),
                "close": round(c, 4),
                "volume": v,
            }
        )
        prev = c
    if override_last:
        bars[-1].update(override_last)
    return bars


def uptrend(n=260, step=0.5, start_price=100.0):
    return [start_price + step * i for i in range(n)]


def downtrend(n=260, step=0.5, start_price=260.0):
    return [start_price - step * i for i in range(n)]


def vol_spike(closes, mult, last_n=1):
    vols = [1_000_000] * len(closes)
    for i in range(len(closes) - last_n, len(closes)):
        vols[i] = int(1_000_000 * mult)
    return vols


# ---------------------------------------------------------------- indicators
def test_sma_and_atr_basics():
    assert sma([1, 2, 3, 4, 5], 5) == 3
    assert sma([1, 2], 5) is None
    bars = make_bars(uptrend(60))
    assert atr(bars, 14) is not None


def test_rvol_excludes_last_bar_from_baseline():
    closes = uptrend(60)
    bars = make_bars(closes, vol_spike(closes, 3.0))
    r = rvol_last(bars, 20)
    assert 2.5 < r <= 3.5  # baseline is the 20 bars BEFORE the last


def test_close_location():
    bar = {"open": 10, "high": 12, "low": 10, "close": 11.5, "volume": 1}
    assert 0.7 < close_location(bar) <= 0.8
    flat = {"open": 10, "high": 10, "low": 10, "close": 10, "volume": 1}
    assert close_location(flat) == 0.5  # zero-range guard


# ---------------------------------------------------------------- timing guard
def test_strip_partial_bar_drops_intraday_today():
    bars = make_bars(uptrend(30), start="2026-07-01")
    last_date = bars[-1]["date"]
    y, m, d = (int(x) for x in last_date.split("-"))
    during = dt.datetime(y, m, d, 11, 0, tzinfo=ET)
    after = dt.datetime(y, m, d, 16, 30, tzinfo=ET)
    next_day = dt.datetime(y, m, d, 9, 0, tzinfo=ET) + dt.timedelta(days=1)
    assert strip_partial_bar(bars, during)[-1]["date"] != last_date
    assert strip_partial_bar(bars, after)[-1]["date"] == last_date
    assert strip_partial_bar(bars, next_day)[-1]["date"] == last_date


# ---------------------------------------------------------------- swing-long
def pullback_closes():
    closes = uptrend(255)
    top = closes[-1]
    closes += [top - 1.2 * i for i in range(1, 6)]  # ~3% dip to/below SMA20
    return closes


def test_swing_long_pullback_zone():
    bars = make_bars(pullback_closes())
    sig = detect_swing_long(bars, DEFAULTS)
    assert sig is not None
    assert sig["trigger"] == "pullback_zone"


def test_swing_long_extended_capped():
    closes = uptrend(250)
    for _ in range(5):
        closes.append(closes[-1] * 1.04)  # blast >10% above SMA50
    bars = make_bars(closes)
    sig = detect_swing_long(bars, DEFAULTS)
    assert sig is not None
    assert sig["trigger"] == "extended"
    res = screen({"EXT": bars}, "swing-long", DEFAULTS, as_of="2026-07-08")
    cand = next(c for c in res["candidates"] if c["ticker"] == "EXT")
    assert cand["grade"] in ("C", "D")  # doc: Extended trigger is watchlist-grade


def test_swing_long_breakout_ready():
    closes = uptrend(250)
    closes += [closes[-1] + 0.05 * ((i % 2) or -1) for i in range(6)]  # tight flag at highs
    sig = detect_swing_long(make_bars(closes), DEFAULTS)
    assert sig is not None
    assert sig["trigger"] == "breakout_ready"


def test_swing_long_rejects_downtrend():
    assert detect_swing_long(make_bars(downtrend()), DEFAULTS) is None


# ---------------------------------------------------------------- swing-short
def test_swing_short_detects_downtrend():
    closes = downtrend(255)
    bottom = closes[-1]
    closes += [bottom + 0.3 * i for i in range(1, 6)]  # weak drift up = bear flag zone
    sig = detect_swing_short(make_bars(closes), DEFAULTS)
    assert sig is not None
    assert sig["trigger"] in ("bear_flag", "breakdown_ready", "oversold", "none")


def test_swing_short_oversold_capped():
    closes = downtrend(250)
    for _ in range(5):
        closes.append(closes[-1] * 0.955)  # stretch far below SMA50
    bars = make_bars(closes)
    sig = detect_swing_short(bars, DEFAULTS)
    assert sig is not None and sig["trigger"] == "oversold"
    res = screen({"OSD": bars}, "swing-short", DEFAULTS, as_of="2026-07-08")
    if res["candidates"]:
        assert res["candidates"][0]["grade"] in ("C", "D")


def test_swing_short_rejects_uptrend():
    assert detect_swing_short(make_bars(uptrend()), DEFAULTS) is None


def test_regime_gate_caps_shorts_in_risk_on():
    closes = downtrend(255)
    closes += [closes[-1] + 0.2 * i for i in range(1, 6)]
    bars = make_bars(closes)
    cfg = dict(DEFAULTS, watch_min_grade="D")  # keep every grade visible
    spy_on = make_bars(uptrend(260, step=0.3))  # SPY above SMA50/200 -> risk_on

    res = screen({"S": bars}, "swing-short", cfg, as_of="2026-07-08", bench=spy_on)
    assert res["market_regime"]["label"] == "risk_on"
    assert res["candidates"], "expected a short candidate"
    for c in res["candidates"]:
        assert c["grade"] in ("C", "D")
        assert any(w.startswith("regime_risk_on") for w in c["warnings"])

    spy_off = make_bars(downtrend(260, step=0.3))  # risk_off -> gate inactive
    res2 = screen({"S": bars}, "swing-short", cfg, as_of="2026-07-08", bench=spy_off)
    assert not any(
        w.startswith("regime_risk_on") for c in res2["candidates"] for w in c["warnings"]
    )

    no_gate = dict(cfg, regime_gate=False)
    res3 = screen({"S": bars}, "swing-short", no_gate, as_of="2026-07-08", bench=spy_on)
    assert not any(
        w.startswith("regime_risk_on") for c in res3["candidates"] for w in c["warnings"]
    )

    # weak is gated the same way; swing-long is NOT gated by risk_on
    resw = screen({"W": weak_bars()}, "weak", cfg, as_of="2026-07-08", bench=spy_on)
    assert all(c["grade"] in ("C", "D") for c in resw["candidates"])
    resl = screen(
        {"PB": make_bars(pullback_closes())}, "swing-long", cfg, "2026-07-08", bench=spy_on
    )
    assert not any(
        w.startswith("regime_risk_on") for c in resl["candidates"] for w in c["warnings"]
    )


def test_swing_short_warns_squeeze_data_unknown():
    closes = downtrend(255)
    closes += [closes[-1] + 0.2 * i for i in range(1, 6)]
    bars = make_bars(closes)
    res = screen({"SHRT": bars}, "swing-short", DEFAULTS, as_of="2026-07-08")
    if res["candidates"]:
        assert any("short_interest_unknown" in w for w in res["candidates"][0]["warnings"])


# ---------------------------------------------------------------- leaders
def test_leaders_detects_near_52w_high():
    bars = make_bars(uptrend())
    sig = detect_leaders(bars, DEFAULTS)
    assert sig is not None
    assert sig["pullback_plan"] in ("at_sma20", "sma20_dip_buy")


def test_leaders_pullback_plan_bands():
    steep = [100 * 1.02**i for i in range(260)]  # ~20% above SMA20 -> wait_deeper
    sig = detect_leaders(make_bars(steep), DEFAULTS)
    assert sig is not None and sig["pullback_plan"] == "wait_deeper"


def test_leaders_rejects_far_from_high():
    closes = uptrend(200)
    closes += [closes[-1] - 0.6 * i for i in range(1, 61)]  # ~14% off highs but > SMA200
    assert detect_leaders(make_bars(closes), DEFAULTS) is None


# ---------------------------------------------------------------- volatility
def choppy_closes(n=260, base=100.0, pct=4.0):
    closes = [base]
    for i in range(1, n):
        closes.append(closes[-1] * (1 + (pct / 100) * (1 if i % 2 else -1)))
    return closes


def test_volatility_detects_high_atr():
    sig = detect_volatility(make_bars(choppy_closes()), DEFAULTS)
    assert sig is not None
    assert sig["atr_pct"] >= DEFAULTS["min_atr_pct"]


def test_volatility_rejects_quiet_tape():
    assert detect_volatility(make_bars(uptrend(260, step=0.05)), DEFAULTS) is None


def test_volatility_chaotic_capped():
    closes = [100 + 0.01 * (i % 3) for i in range(260)]  # tiny bodies
    bars = make_bars(closes, wick=0.04)  # huge wicks -> chaotic
    sig = detect_volatility(bars, DEFAULTS)
    assert sig is not None and sig["body_ratio"] < DEFAULTS["chaotic_body_ratio"]
    res = screen({"CHAOS": bars}, "volatility", DEFAULTS, as_of="2026-07-08")
    if res["candidates"]:
        assert res["candidates"][0]["grade"] in ("C", "D")


# ---------------------------------------------------------------- in-play
def in_play_bars(day_chg=5.0, mult=3.0, faded=False):
    closes = uptrend(255, step=0.2)
    last = closes[-1] * (1 + day_chg / 100)
    closes.append(last)
    bars = make_bars(closes, vol_spike(closes, mult))
    if faded:
        prev = closes[-2]
        bars[-1].update(
            {
                "open": prev,
                "high": round(prev * 1.09, 4),
                "low": prev,
                "close": round(prev * 1.03, 4),
            }
        )
    return bars


def test_in_play_detects_rvol_and_move():
    sig = detect_in_play(in_play_bars(), DEFAULTS)
    assert sig is not None
    assert sig["rvol"] >= DEFAULTS["inplay_min_rvol"]
    assert sig["day_change_pct"] >= DEFAULTS["inplay_min_day_chg"]


def test_in_play_rejects_normal_volume():
    assert detect_in_play(in_play_bars(mult=1.1), DEFAULTS) is None


def test_in_play_faded_close_capped():
    bars = in_play_bars(faded=True)
    res = screen({"FADE": bars}, "in-play", DEFAULTS, as_of="2026-07-08")
    if res["candidates"]:
        c = res["candidates"][0]
        assert c["grade"] in ("C", "D")
        assert any("faded_close" in w for w in c["warnings"])


# ---------------------------------------------------------------- unusual volume
def quadrant_bars(kind):
    closes = uptrend(255, step=0.2)
    prev = closes[-1]
    atr_abs = prev * 0.012  # builder wick=0.005 -> ATR ~1.2% of price
    shapes = {
        "accumulation": {
            "open": prev,
            "low": prev,
            "high": prev + 2 * atr_abs,
            "close": prev + 1.9 * atr_abs,
        },
        "distribution": {
            "open": prev,
            "high": prev,
            "low": prev - 2 * atr_abs,
            "close": prev - 1.9 * atr_abs,
        },
        "absorption": {
            "open": prev,
            "high": prev + 0.2 * atr_abs,
            "low": prev - 0.2 * atr_abs,
            "close": prev,
        },
        "chop": {
            "open": prev,
            "high": prev + 2 * atr_abs,
            "low": prev - 2 * atr_abs,
            "close": prev + 0.05 * atr_abs,
        },
    }
    o = {k: round(v, 4) for k, v in shapes[kind].items()}
    closes.append(o["close"])
    return make_bars(closes, vol_spike(closes, 4.0), override_last=o)


def test_unusual_volume_quadrants():
    for kind in ("accumulation", "distribution", "absorption", "chop"):
        sig = detect_unusual_volume(quadrant_bars(kind), DEFAULTS)
        assert sig is not None, kind
        assert sig["quadrant"] == kind


def test_unusual_volume_chop_is_skip_grade():
    res = screen({"CHOP": quadrant_bars("chop")}, "unusual-volume", DEFAULTS, as_of="2026-07-08")
    assert not res["candidates"] or res["candidates"][0]["grade"] == "D"


def test_unusual_volume_needs_3x():
    closes = uptrend(256, step=0.2)
    assert detect_unusual_volume(make_bars(closes, vol_spike(closes, 2.0)), DEFAULTS) is None


# ---------------------------------------------------------------- weak
def weak_bars(day_chg=-5.0, mult=2.0, aligned=True, bought_back=False):
    closes = downtrend(255, step=0.4) if aligned else uptrend(255, step=0.4)
    closes.append(closes[-1] * (1 + day_chg / 100))
    bars = make_bars(closes, vol_spike(closes, mult))
    if bought_back:
        prev = closes[-2]
        bars[-1].update(  # gapped -10%, recovered most of it, still -3.5% on the day
            {
                "open": round(prev * 0.90, 4),
                "low": round(prev * 0.89, 4),
                "high": round(prev * 0.97, 4),
                "close": round(prev * 0.965, 4),
            }
        )
    return bars


def test_weak_detects_decline_on_volume():
    sig = detect_weak(weak_bars(), DEFAULTS)
    assert sig is not None
    assert sig["downtrend_aligned"] is True


def test_weak_rejects_small_decline():
    assert detect_weak(weak_bars(day_chg=-1.0), DEFAULTS) is None


def test_weak_gap_bought_back_capped():
    res = screen({"GAP": weak_bars(bought_back=True)}, "weak", DEFAULTS, as_of="2026-07-08")
    if res["candidates"]:
        c = res["candidates"][0]
        assert c["grade"] in ("C", "D")
        assert any("gap_down_bought_back" in w for w in c["warnings"])


# ---------------------------------------------------------------- invalidation
def test_thin_liquidity_and_history_invalidated():
    bars = make_bars(uptrend())
    for b in bars:
        b["volume"] = 1_000
    assert "insufficient_dollar_volume" in invalidation_reasons(bars, DEFAULTS)
    assert "insufficient_history" in invalidation_reasons(bars[:100], DEFAULTS)


# ---------------------------------------------------------------- earnings gate
def test_earnings_rejects_swing_but_warns_in_play():
    swing = make_bars(pullback_closes())
    res = screen(
        {"E": swing},
        "swing-long",
        DEFAULTS,
        as_of="2026-07-08",
        earnings_by_ticker={"E": "2026-07-10"},
    )
    assert not res["candidates"]
    assert res["rejected"]["E"][0].startswith("earnings")

    ip = in_play_bars()
    res2 = screen(
        {"E": ip}, "in-play", DEFAULTS, as_of="2026-07-08", earnings_by_ticker={"E": "2026-07-10"}
    )
    if res2["candidates"]:
        assert any(w.startswith("earnings_within") for w in res2["candidates"][0]["warnings"])


def test_missing_earnings_marked_unknown():
    res = screen({"U": make_bars(pullback_closes())}, "swing-long", DEFAULTS, as_of="2026-07-08")
    if res["candidates"]:
        assert any("earnings_date_unknown" in w for w in res["candidates"][0]["warnings"])


# ---------------------------------------------------------------- stale data
def test_stale_ticker_flagged():
    fresh = make_bars(pullback_closes())
    stale = make_bars(pullback_closes())[:-3]  # data stopped 3 sessions early
    res = screen({"FRESH": fresh, "STALE": stale}, "swing-long", DEFAULTS, as_of="2026-07-08")
    by_ticker = {c["ticker"]: c for c in res["candidates"]}
    if "STALE" in by_ticker:
        assert any("stale_data" in w for w in by_ticker["STALE"]["warnings"])
    assert res["session"] == fresh[-1]["date"]


# ---------------------------------------------------------------- grades & report
def test_grade_thresholds():
    assert grade_for(90) == "A"
    assert grade_for(72) == "B"
    assert grade_for(55) == "C"
    assert grade_for(10) == "D"


def test_all_screens_registered():
    assert set(SCREENS) == {
        "swing-long",
        "swing-short",
        "leaders",
        "volatility",
        "in-play",
        "unusual-volume",
        "weak",
    }


def test_reports_written_with_timing_banner(tmp_path):
    res = screen({"HOT": in_play_bars()}, "in-play", DEFAULTS, as_of="2026-07-08")
    json_path, md_path = write_reports(res, tmp_path, "swing_setups_in_play")
    md = Path(md_path).read_text()
    assert "next-session watchlist" in md  # the Day-1 timing contract, printed
    data = json.loads(Path(json_path).read_text())
    assert data["screen"] == "in-play"
    assert data["session"] == in_play_bars()[-1]["date"]
    assert "params" in data


def test_swing_report_has_plan_levels(tmp_path):
    res = screen({"PB": make_bars(pullback_closes())}, "swing-long", DEFAULTS, as_of="2026-07-08")
    assert res["candidates"], "expected a pullback candidate"
    plan = res["candidates"][0]["plan"]
    last = res["candidates"][0]["last_close"]
    assert plan["stop"] < last < plan["t1"]
    _, md_path = write_reports(res, tmp_path, "swing_setups_swing_long")
    assert "Top Picks" in Path(md_path).read_text()


def test_partial_universe_coverage_disclosed(tmp_path):
    bars = {"PB": make_bars(pullback_closes())}
    res = screen(bars, "swing-long", DEFAULTS, as_of="2026-07-08", universe_total=1776)
    assert res["universe_total"] == 1776
    _, md_path = write_reports(res, tmp_path, "swing_setups_swing_long")
    md = Path(md_path).read_text()
    assert "1 of 1776 tickers" in md  # coverage banner when scanned < total

    full = screen(bars, "swing-long", DEFAULTS, as_of="2026-07-08", universe_total=1)
    _, md_path2 = write_reports(full, tmp_path, "swing_setups_full")
    assert "Coverage:" not in Path(md_path2).read_text()  # no banner at full coverage
