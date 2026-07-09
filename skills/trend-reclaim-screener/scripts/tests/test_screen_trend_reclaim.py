"""Tests for the Trend Reclaim screener (Phase 1)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from screen_trend_reclaim import (  # noqa: E402
    DEFAULTS,
    build_plan,
    capped_grade,
    detect_reclaim,
    grade_for,
    invalidation_reasons,
    market_regime,
    score_candidate,
    screen,
    sma,
    write_reports,
)


# ---------------------------------------------------------------- fixtures
def make_bars(closes, volumes=None, start="2026-01-01"):
    """Synthetic OHLCV bars (oldest first) from a close series."""
    import datetime as dt

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
                "high": round(max(o, c) * 1.005, 4),
                "low": round(min(o, c) * 0.995, 4),
                "close": round(c, 4),
                "volume": v,
            }
        )
        prev = c
    return bars


def reclaim_closes(reclaim_days=4, reclaim_step=1.5):
    """Uptrend -> pullback below SMA50 -> base -> reclaim. Ends just after cross."""
    closes = [100 + 0.5 * i for i in range(200)]  # steady uptrend to ~199.5
    top = closes[-1]
    closes += [top - 0.9 * i for i in range(1, 26)]  # -11% pullback, below SMA50
    low = closes[-1]
    closes += [low + 0.02 * i for i in range(30)]  # flat base while SMA50 decays
    closes += [closes[-1] + reclaim_step * i for i in range(1, reclaim_days + 1)]
    return closes


def strong_reclaim_bars(**kw):
    closes = reclaim_closes(**kw)
    n = len(closes)
    volumes = [1_000_000] * n
    for i in range(n - 6, n):  # heavy participation on the reclaim leg
        volumes[i] = 2_200_000
    return make_bars(closes, volumes)


# ---------------------------------------------------------------- indicators
def test_sma_basic():
    assert sma([1, 2, 3, 4, 5], 5) == 3
    assert sma([1, 2, 3], 5) is None


# ---------------------------------------------------------------- detection
def test_detects_fresh_reclaim():
    r = detect_reclaim(strong_reclaim_bars(), DEFAULTS)
    assert r is not None
    assert r["days_since_cross"] <= DEFAULTS["reclaim_window"]
    assert r["closes_above"] >= 1


def test_no_signal_without_pullback():
    bars = make_bars([100 + 0.5 * i for i in range(260)])  # never below SMA50
    assert detect_reclaim(bars, DEFAULTS) is None


def test_no_signal_when_still_below():
    closes = [100 + 0.5 * i for i in range(200)]
    closes += [closes[-1] - 1.2 * i for i in range(1, 40)]  # still falling
    assert detect_reclaim(make_bars(closes), DEFAULTS) is None


def test_choppy_repeated_crosses_rejected():
    # Flat tape oscillating across a flat SMA50 -> multiple failed reclaims.
    closes = [100.0] * 200
    for blk in range(6):
        step = 3.0 if blk % 2 == 0 else -3.0
        closes += [closes[-1] + step] * 5
    closes += [closes[-1] + 2.0]  # end with a cross-above
    bars = make_bars(closes)
    r = detect_reclaim(bars, DEFAULTS)
    if r is not None:
        assert "repeated_failed_reclaims" in invalidation_reasons(bars, r, DEFAULTS)


# ---------------------------------------------------------------- invalidation
def test_extended_reclaim_invalidated():
    closes = reclaim_closes(reclaim_days=10, reclaim_step=4.0)  # blasts >10% above SMA50
    bars = make_bars(closes)
    r = detect_reclaim(bars, DEFAULTS)
    reasons = invalidation_reasons(bars, r, DEFAULTS) if r else ["no_signal"]
    assert r is None or any(x.startswith("extended") for x in reasons)


def test_thin_liquidity_invalidated():
    bars = strong_reclaim_bars()
    for b in bars:
        b["volume"] = 1_000  # ~ $200k ADV
    r = detect_reclaim(bars, DEFAULTS)
    assert r is not None
    assert "insufficient_dollar_volume" in invalidation_reasons(bars, r, DEFAULTS)


def test_insufficient_history_invalidated():
    bars = strong_reclaim_bars()[-150:]
    assert "insufficient_history" in invalidation_reasons(bars, None, DEFAULTS)


# ---------------------------------------------------------------- scoring
def test_strong_reclaim_scores_tradeable():
    bars = strong_reclaim_bars()
    r = detect_reclaim(bars, DEFAULTS)
    s = score_candidate(bars, r, DEFAULTS)
    assert set(s["factors"]) == {
        "reclaim_quality",
        "momentum",
        "structure",
        "volume",
        "trend_alignment",
    }
    assert 0 <= s["composite"] <= 100
    assert s["composite"] >= 50  # at least C on a clean synthetic reclaim


def test_weak_volume_scores_lower():
    strong = strong_reclaim_bars()
    weak = make_bars(reclaim_closes(), [400_000] * len(reclaim_closes()))
    rs, rw = detect_reclaim(strong, DEFAULTS), detect_reclaim(weak, DEFAULTS)
    ss = score_candidate(strong, rs, DEFAULTS)
    sw = score_candidate(weak, rw, DEFAULTS)
    assert ss["factors"]["volume"] > sw["factors"]["volume"]
    assert ss["composite"] > sw["composite"]


def test_grade_thresholds():
    assert grade_for(90) == "A"
    assert grade_for(70) == "B"
    assert grade_for(55) == "C"
    assert grade_for(10) == "D"


def test_phase_label():
    r = detect_reclaim(strong_reclaim_bars(reclaim_days=5), DEFAULTS)
    assert r["phase"] in ("reclaim_attempt", "reclaimed_trend")
    r2 = detect_reclaim(strong_reclaim_bars(reclaim_days=2), DEFAULTS)
    assert r2["phase"] == "reclaim_attempt"


# ---------------------------------------------------------------- plan
def test_plan_levels_sane():
    bars = strong_reclaim_bars()
    r = detect_reclaim(bars, DEFAULTS)
    plan = build_plan(bars, r)
    last_close = bars[-1]["close"]
    assert plan["stop"] < last_close
    assert plan["t1"] > last_close
    assert plan["reclaim_level"] < last_close
    assert plan["risk_pct"] > 0


# ---------------------------------------------------------------- end to end
def test_screen_and_reports(tmp_path):
    bars_by_ticker = {
        "GOOD": strong_reclaim_bars(),
        "FLAT": make_bars([100 + 0.5 * i for i in range(260)]),
        "THIN": make_bars(reclaim_closes(), [1_000] * len(reclaim_closes())),
    }
    result = screen(bars_by_ticker, DEFAULTS, as_of="2026-07-06")
    tickers = [c["ticker"] for c in result["candidates"]]
    assert "GOOD" in tickers and "FLAT" not in tickers and "THIN" not in tickers
    assert len(result["top_picks"]) <= DEFAULTS["top"]
    assert result["rejected"]["THIN"] == ["insufficient_dollar_volume"]

    json_path, md_path = write_reports(result, tmp_path, "trend_reclaim")
    data = json.loads(Path(json_path).read_text())
    assert data["as_of"] == "2026-07-06"
    md = Path(md_path).read_text()
    assert "GOOD" in md and "Top Picks" in md


# ---------------------------------------------------------------- earnings gate
def test_earnings_gate():
    bars_by_ticker = {"GOOD": strong_reclaim_bars()}
    near = screen(
        bars_by_ticker, DEFAULTS, as_of="2026-07-06", earnings_by_ticker={"GOOD": "2026-07-09"}
    )
    assert near["candidates"] == []
    assert near["rejected"]["GOOD"][0].startswith("earnings")
    far = screen(
        bars_by_ticker, DEFAULTS, as_of="2026-07-06", earnings_by_ticker={"GOOD": "2026-08-20"}
    )
    assert [c["ticker"] for c in far["candidates"]] == ["GOOD"]
    past = screen(  # already reported — not a forward binary event
        bars_by_ticker, DEFAULTS, as_of="2026-07-06", earnings_by_ticker={"GOOD": "2026-07-01"}
    )
    assert [c["ticker"] for c in past["candidates"]] == ["GOOD"]


# ---------------------------------------------------------------- volume bands
def _scored_with_reclaim_vol(mult):
    closes = reclaim_closes()
    volumes = [1_000_000] * len(closes)
    for i in range(len(closes) - 6, len(closes)):
        volumes[i] = int(1_000_000 * mult)
    bars = make_bars(closes, volumes)
    return score_candidate(bars, detect_reclaim(bars, DEFAULTS), DEFAULTS)


def test_volume_bands_match_doc():
    strong, weak = _scored_with_reclaim_vol(5.0), _scored_with_reclaim_vol(0.7)
    assert strong["cross_rvol"] >= 3.0
    assert weak["cross_rvol"] < DEFAULTS["fade_rvol"]
    assert strong["factors"]["volume"] > weak["factors"]["volume"]
    assert "fade_risk_volume" in weak["warnings"]


def test_fade_risk_grade_cap():
    assert capped_grade("A", 0.7, DEFAULTS) == "C"
    assert capped_grade("B", 0.79, DEFAULTS) == "C"
    assert capped_grade("B", 1.2, DEFAULTS) == "B"
    assert capped_grade("D", 0.7, DEFAULTS) == "D"


# ---------------------------------------------------------------- market regime
def test_market_regime_labels():
    up = make_bars([100 + 0.5 * i for i in range(260)])
    down = make_bars([200 - 0.3 * i for i in range(260)])
    assert market_regime(up)["label"] == "risk_on"
    assert market_regime(down)["label"] == "risk_off"
    assert market_regime(up[:100]) is None  # not enough history
    assert market_regime(None) is None


def test_regime_rendered_in_report(tmp_path):
    result = screen({"GOOD": strong_reclaim_bars()}, DEFAULTS, as_of="2026-07-06")
    result["market_regime"] = market_regime(make_bars([100 + 0.5 * i for i in range(260)]))
    _, md_path = write_reports(result, tmp_path, "trend_reclaim")
    assert "Market regime" in Path(md_path).read_text()


# ---------------------------------------------------------------- T2 target
def test_plan_t2_measured_move():
    bars = strong_reclaim_bars()
    plan = build_plan(bars, detect_reclaim(bars, DEFAULTS))
    assert plan["t2"] > plan["t1"]  # base depth projected from the reclaim level
