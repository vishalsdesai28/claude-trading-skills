#!/usr/bin/env python3
"""Swing Setup Screener — seven EOD screens over keyless Yahoo daily bars.

Screens: swing-long, swing-short, leaders, volatility, in-play,
unusual-volume, weak. Shared universe fetch, indicators, A-D grading, and
JSON+Markdown reports; each screen contributes a detector, a documented
factor score, a grade cap, and plan levels.

Data-fidelity contract (real money depends on this):
- Only the last COMPLETED session is evaluated. A same-day bar fetched
  before ~16:15 ET is dropped (override: --allow-partial-today).
- in-play / unusual-volume / weak are NEXT-SESSION WATCHLIST screens.
  The live product computes them intraday (time-of-day RVOL, VWAP,
  premarket levels); daily bars cannot — reports say so explicitly.
- Missing data is reported UNKNOWN (warning), never assumed safe.
- No synthesized fields: nothing derived from VWAP, float, spreads, or
  premarket data appears anywhere.

Thresholds sourced from the product docs are marked "doc"; the rest are
this repo's documented defaults ("ours") — all CLI-overridable and echoed
into every report's params block.
"""

import argparse
import json
import logging
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("swing_setups")

ET = ZoneInfo("America/New_York")
SESSION_CLOSE_ET = time(16, 15)  # consider a same-day bar complete after this

GRADE_THRESHOLDS = [(85, "A"), (70, "B"), (50, "C"), (0, "D")]
GRADE_ORDER = ["A", "B", "C", "D"]

DEFAULTS = {
    # shared hard gates
    "min_history": 210,  # bars; SMA200 + slope lookback; excludes recent IPOs
    "min_price": 5.0,
    "min_adv_usd": 10_000_000,  # 20-day average dollar volume
    "exclude_earnings_within_days": 5,  # doc: "avoid entirely if earnings within 5 days"
    "top": 3,
    "watch_min_grade": "C",
    # Backtest-validated (3y, 53 cutoffs): every short grade lost money in a
    # risk_on tape. Shorts are capped at C while SPY > SMA50/SMA200.
    "regime_gate": True,
    "rvol_baseline": 20,  # sessions in the RVOL denominator (last bar excluded)
    "slope_lookback": 10,  # sessions for SMA slope
    # swing-long / swing-short triggers
    "extended_dist50_pct": 10.0,  # doc: ">10% above SMA50 = extended, reduce/avoid"
    "breakout_near_high_pct": 3.0,  # ours: close within 3% of 20d high
    "breakout_range5_pct": 5.0,  # doc (leaders): "range < 5% for 5+ days"
    "pullback_dist20_max": 1.0,  # doc (leaders): At-SMA20 band tops out at +1%
    "oversold_dist50_pct": -10.0,  # mirror of extended, ours
    "bearflag_leg_pct": -8.0,  # ours: flagpole decline over the prior 10 sessions
    "bearflag_drift_max": 4.0,  # ours: flag drift-up ceiling over the last 5
    # leaders (doc-numbered bands)
    "leader_prox_pct": 5.0,  # doc: "within 5% of 52-week high"
    "at_sma20_lo": -3.0,  # doc band: -3%..+1% = At SMA20
    "dip_buy_max": 6.0,  # doc band: +1%..+6% = SMA20 Dip Buy
    "parabolic_dist20_pct": 10.0,  # doc: "avoid chasing if >10% above SMA20"
    # volatility
    "min_atr_pct": 4.0,  # ours (doc visuals: normal 2-3%, in-play 5-10%)
    "chaotic_body_ratio": 0.35,  # ours: bodies/range below this = chaotic tape
    # in-play
    "inplay_min_rvol": 2.0,  # doc: "IN-PLAY RVOL 2x+"
    "inplay_min_day_chg": 3.0,  # ours: "meaningful day move" floor
    "faded_close_loc": 0.5,  # ours: up-day closing in lower half = faded
    # unusual-volume
    "unusual_min_rvol": 3.0,  # doc: RVOL gauge "scanner triggers here" at 3x+
    "acc_close_loc": 0.7,  # doc quadrant: close near highs = accumulation
    "dist_close_loc": 0.3,  # doc quadrant: close near lows = distribution
    "absorption_range_atr": 0.8,  # ours: day range under 0.8x ATR = absorption
    # weak
    "weak_max_day_chg": -3.0,  # ours: "meaningful decline" floor
    "weak_min_rvol": 1.5,  # doc checklist: "RVOL above normal (1.5x+)"
    "gap_down_min_pct": 2.0,  # ours: open this far below prior close = gap down
    "gap_recovered_loc": 0.6,  # ours: gap-down closing in top 40% = bought back
}

WEIGHTS = {
    "swing-long": {
        "trend_structure": 0.25,
        "ma_alignment": 0.25,
        "entry_timing": 0.25,
        "rel_strength": 0.15,
        "volume_character": 0.10,
    },
    "swing-short": {
        "trend_structure": 0.25,
        "ma_alignment": 0.25,
        "entry_timing": 0.25,
        "rel_weakness": 0.15,
        "volume_character": 0.10,
    },
    "leaders": {
        "proximity_to_high": 0.25,
        "trend_quality": 0.25,
        "accumulation": 0.20,
        "rel_strength": 0.20,
        "entry_timing": 0.10,
    },
    "volatility": {
        "expansion": 0.35,
        "structure_quality": 0.30,
        "liquidity": 0.20,
        "participation": 0.15,
    },
    "in-play": {
        "rvol_strength": 0.30,
        "day_move": 0.20,
        "close_strength": 0.20,
        "range_expansion": 0.15,
        "liquidity": 0.15,
    },
    "unusual-volume": {
        "rvol_magnitude": 0.35,
        "quadrant_clarity": 0.25,
        "range_participation": 0.20,
        "liquidity": 0.20,
    },
    "weak": {
        "decline_magnitude": 0.25,
        "rvol_strength": 0.25,
        "weakness_structure": 0.20,
        "trend_alignment": 0.15,
        "close_weakness": 0.15,
    },
}

# Screens whose live-product counterpart is intraday: reports carry the
# next-session-watchlist banner and earnings proximity warns instead of rejects.
NEXT_SESSION_SCREENS = {"in-play", "unusual-volume", "weak"}
SHORT_BIAS_SCREENS = {"swing-short", "weak"}

TIMING_NOTE = (
    "Signals reflect the last completed session. This is a next-session "
    "watchlist, NOT a live Day-1 entry signal — intraday RVOL pace, VWAP, "
    "spreads, and premarket levels are not available from daily bars; mark "
    "those live before trading."
)


# ---------------------------------------------------------------- indicators
def sma(values, period, end=None):
    vals = values if end is None else values[:end]
    if len(vals) < period:
        return None
    return sum(vals[-period:]) / period


def roc(values, period, end=None):
    vals = values if end is None else values[:end]
    if len(vals) < period + 1 or vals[-period - 1] == 0:
        return None
    return (vals[-1] / vals[-period - 1] - 1.0) * 100.0


def atr(bars, period=14, end=None):
    sub = bars if end is None else bars[:end]
    if len(sub) < period + 1:
        return None
    trs = []
    for i in range(len(sub) - period, len(sub)):
        prev_close = sub[i - 1]["close"]
        trs.append(
            max(
                sub[i]["high"] - sub[i]["low"],
                abs(sub[i]["high"] - prev_close),
                abs(sub[i]["low"] - prev_close),
            )
        )
    return sum(trs) / period


def rvol_last(bars, baseline=20):
    """Last completed bar's volume vs. the average of the `baseline` bars before it."""
    if len(bars) < baseline + 1:
        return None
    base = sum(b["volume"] for b in bars[-baseline - 1 : -1]) / baseline
    return bars[-1]["volume"] / base if base else None


def close_location(bar):
    """(close - low) / (high - low); 0.5 on a zero-range bar."""
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return 0.5
    return (bar["close"] - bar["low"]) / rng


def adv_usd(bars, period=20):
    sub = bars[-period:]
    return sum(b["volume"] for b in sub) / len(sub) * bars[-1]["close"]


def block_extremes(bars, field, blocks=3, size=5):
    """min/max of `field` over `blocks` consecutive trailing windows, oldest first."""
    vals = [b[field] for b in bars[-blocks * size :]]
    if len(vals) < blocks * size:
        return None
    fn = min if field == "low" else max
    return [fn(vals[i * size : (i + 1) * size]) for i in range(blocks)]


def rel_return_pct(bars, period=63):
    closes = [b["close"] for b in bars]
    return roc(closes, min(period, len(closes) - 1)) if len(closes) > 1 else None


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _band(value, bands, default=0.0):
    """First score whose threshold `value` meets. bands: [(threshold, score), ...]."""
    for threshold, score in bands:
        if value >= threshold:
            return score
    return default


# ---------------------------------------------------------------- timing guard
def strip_partial_bar(bars, now_et=None):
    """Drop the last bar when it is today's still-open session (before ~16:15 ET)."""
    if not bars:
        return bars
    now_et = now_et or datetime.now(ET)
    last_date = date.fromisoformat(bars[-1]["date"])
    if last_date == now_et.date() and now_et.time() < SESSION_CLOSE_ET:
        return bars[:-1]
    return bars


# ---------------------------------------------------------------- shared scores
def _rel_strength_factor(bars, bench, invert=False):
    """63-day return vs. benchmark, banded. Returns (score, warning_or_None)."""
    own = rel_return_pct(bars)
    ref = rel_return_pct(bench) if bench else None
    if own is None or ref is None:
        return 50.0, "benchmark_unavailable_rel_strength_neutral"
    edge = (ref - own) if invert else (own - ref)
    return _band(edge, [(15, 100), (5, 75), (0, 55), (-5, 30)], 0.0), None


def _liquidity_factor(bars):
    return _band(adv_usd(bars), [(100e6, 100), (50e6, 80), (20e6, 60), (10e6, 40)], 0.0)


def _ma_alignment_factor(closes, s50, s200, cfg, bearish=False):
    n = len(closes)
    lb = cfg["slope_lookback"]
    s50_prev = sma(closes, 50, end=n - lb) or s50
    s200_prev = sma(closes, 200, end=n - lb * 2) or s200
    slope50 = (s50 / s50_prev - 1.0) * 100.0
    slope200 = (s200 / s200_prev - 1.0) * 100.0
    gap = (s50 / s200 - 1.0) * 100.0
    if bearish:
        slope50, slope200, gap = -slope50, -slope200, -gap
    score = min(40.0, max(0.0, slope50 * 20))
    score += min(30.0, max(0.0, gap * 6))
    score += 30 if slope200 >= 0 else 0
    return _clamp(score)


def _volume_character_factor(bars, bullish=True, period=10):
    """Healthy tape: with-trend days on heavier volume than counter-trend days."""
    up, dn = [], []
    for i in range(len(bars) - period, len(bars)):
        if bars[i]["close"] > bars[i - 1]["close"]:
            up.append(bars[i]["volume"])
        elif bars[i]["close"] < bars[i - 1]["close"]:
            dn.append(bars[i]["volume"])
    if not up or not dn:
        return 60.0
    ratio = (sum(up) / len(up)) / (sum(dn) / len(dn))
    if not bullish:
        ratio = 1.0 / ratio if ratio else 99.0
    return 100.0 if ratio >= 1.15 else (60.0 if ratio >= 0.87 else 20.0)


# ---------------------------------------------------------------- swing-long
def detect_swing_long(bars, cfg):
    closes = [b["close"] for b in bars]
    n = len(closes)
    if n < cfg["min_history"]:
        return None
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    if not (s20 and s50 and s200):
        return None
    last = closes[-1]
    s50_prev = sma(closes, 50, end=n - cfg["slope_lookback"]) or s50
    slope50_pct = (s50 / s50_prev - 1.0) * 100.0
    if not (last > s50 > s200 and slope50_pct > 0):
        return None
    dist50 = (last / s50 - 1.0) * 100.0
    dist20 = (last / s20 - 1.0) * 100.0
    high20 = max(b["high"] for b in bars[-20:])
    lows5 = [b["low"] for b in bars[-5:]]
    highs5 = [b["high"] for b in bars[-5:]]
    range5_pct = (max(highs5) - min(lows5)) / last * 100.0
    if dist50 > cfg["extended_dist50_pct"]:
        trigger = "extended"
    elif (
        last >= high20 * (1 - cfg["breakout_near_high_pct"] / 100.0)
        and range5_pct < cfg["breakout_range5_pct"]
    ):
        trigger = "breakout_ready"
    elif dist20 <= cfg["pullback_dist20_max"]:
        trigger = "pullback_zone"
    else:
        trigger = "none"
    return {
        "trigger": trigger,
        "sma20": s20,
        "sma50": s50,
        "sma200": s200,
        "slope50_pct": round(slope50_pct, 2),
        "dist50_pct": round(dist50, 2),
        "dist20_pct": round(dist20, 2),
    }


def score_swing_long(bars, sig, cfg, bench):
    closes = [b["close"] for b in bars]
    lows = block_extremes(bars, "low")
    if lows and lows[0] < lows[1] < lows[2]:
        structure = 100.0
    elif lows and lows[1] < lows[2]:
        structure = 50.0
    else:
        structure = 0.0
    alignment = _ma_alignment_factor(closes, sig["sma50"], sig["sma200"], cfg)
    timing = {"pullback_zone": 100.0, "breakout_ready": 85.0, "none": 40.0, "extended": 15.0}[
        sig["trigger"]
    ]
    rel, rel_warn = _rel_strength_factor(bars, bench)
    vol_char = _volume_character_factor(bars, bullish=True)
    factors = {
        "trend_structure": structure,
        "ma_alignment": alignment,
        "entry_timing": timing,
        "rel_strength": rel,
        "volume_character": vol_char,
    }
    warnings = [w for w in [rel_warn] if w]
    if sig["slope50_pct"] < 0.3:
        warnings.append("sma50_slope_flattening")
    return factors, warnings


def plan_swing_long(bars, sig, cfg):
    last = bars[-1]["close"]
    low10 = min(b["low"] for b in bars[-10:])
    below = [v for v in (low10, sig["sma50"]) if v < last]
    stop = max(below) if below else min(low10, sig["sma50"])
    prior_high = (
        max(b["high"] for b in bars[-60:-5])
        if len(bars) >= 65
        else max(b["high"] for b in bars[:-1])
    )
    if prior_high <= last * 1.005:  # already at highs -> measured move
        high20 = max(b["high"] for b in bars[-20:])
        low20 = min(b["low"] for b in bars[-20:])
        t1, t1_basis = last + (high20 - low20), "measured_move"
    else:
        t1, t1_basis = prior_high, "prior_swing_high"
    risk = last - stop
    return {
        "entry": "next_session_open",
        "stop": round(stop, 2),
        "t1": round(t1, 2),
        "t1_basis": t1_basis,
        "risk_pct": round(risk / last * 100.0, 2),
        "reward_risk_at_t1": round((t1 - last) / max(risk, 1e-9), 2),
    }


def cap_swing_long(grade, sig, cfg):
    if sig["trigger"] in ("extended", "none"):
        return _worst_grade(grade, "C"), [f"watchlist_only_trigger_{sig['trigger']}"]
    return grade, []


# ---------------------------------------------------------------- swing-short
def detect_swing_short(bars, cfg):
    closes = [b["close"] for b in bars]
    n = len(closes)
    if n < cfg["min_history"]:
        return None
    s50, s200 = sma(closes, 50), sma(closes, 200)
    if not (s50 and s200):
        return None
    last = closes[-1]
    s50_prev = sma(closes, 50, end=n - cfg["slope_lookback"]) or s50
    slope50_pct = (s50 / s50_prev - 1.0) * 100.0
    if not (last < s50 < s200 and slope50_pct < 0):
        return None
    dist50 = (last / s50 - 1.0) * 100.0
    leg_pct = (closes[-6] / closes[-16] - 1.0) * 100.0 if n >= 16 else 0.0
    drift5_pct = (last / closes[-6] - 1.0) * 100.0 if n >= 6 else 0.0
    vol5 = sum(b["volume"] for b in bars[-5:]) / 5
    vol_prior10 = sum(b["volume"] for b in bars[-15:-5]) / 10
    low20 = min(b["low"] for b in bars[-20:])
    highs = block_extremes(bars, "high")
    lower_highs = bool(highs and highs[0] > highs[1] > highs[2])
    if dist50 < cfg["oversold_dist50_pct"]:
        trigger = "oversold"
    elif (
        leg_pct <= cfg["bearflag_leg_pct"]
        and 0 <= drift5_pct <= cfg["bearflag_drift_max"]
        and vol5 < vol_prior10
    ):
        trigger = "bear_flag"
    elif last <= low20 * 1.02 and lower_highs:
        trigger = "breakdown_ready"
    else:
        trigger = "none"
    return {
        "trigger": trigger,
        "sma50": s50,
        "sma200": s200,
        "slope50_pct": round(slope50_pct, 2),
        "dist50_pct": round(dist50, 2),
        "lower_highs": lower_highs,
    }


def score_swing_short(bars, sig, cfg, bench):
    closes = [b["close"] for b in bars]
    highs = block_extremes(bars, "high")
    if highs and highs[0] > highs[1] > highs[2]:
        structure = 100.0
    elif highs and highs[1] > highs[2]:
        structure = 50.0
    else:
        structure = 0.0
    alignment = _ma_alignment_factor(closes, sig["sma50"], sig["sma200"], cfg, bearish=True)
    timing = {"bear_flag": 100.0, "breakdown_ready": 85.0, "none": 40.0, "oversold": 15.0}[
        sig["trigger"]
    ]
    rel, rel_warn = _rel_strength_factor(bars, bench, invert=True)
    vol_char = _volume_character_factor(bars, bullish=False)
    factors = {
        "trend_structure": structure,
        "ma_alignment": alignment,
        "entry_timing": timing,
        "rel_weakness": rel,
        "volume_character": vol_char,
    }
    return factors, [w for w in [rel_warn] if w]


def plan_swing_short(bars, sig, cfg):
    last = bars[-1]["close"]
    high10 = max(b["high"] for b in bars[-10:])
    above = [v for v in (high10, sig["sma50"]) if v > last]
    stop = min(above) if above else max(high10, sig["sma50"])
    prior_low = (
        min(b["low"] for b in bars[-60:-5]) if len(bars) >= 65 else min(b["low"] for b in bars[:-1])
    )
    if prior_low >= last * 0.995:
        high20 = max(b["high"] for b in bars[-20:])
        low20 = min(b["low"] for b in bars[-20:])
        t1, t1_basis = last - (high20 - low20), "measured_move"
    else:
        t1, t1_basis = prior_low, "prior_swing_low"
    risk = stop - last
    return {
        "entry": "next_session_open_short",
        "stop": round(stop, 2),
        "t1": round(t1, 2),
        "t1_basis": t1_basis,
        "risk_pct": round(risk / last * 100.0, 2),
        "reward_risk_at_t1": round((last - t1) / max(risk, 1e-9), 2),
    }


def cap_swing_short(grade, sig, cfg):
    if sig["trigger"] in ("oversold", "none"):
        return _worst_grade(grade, "C"), [f"watchlist_only_trigger_{sig['trigger']}"]
    return grade, []


# ---------------------------------------------------------------- leaders
def detect_leaders(bars, cfg):
    closes = [b["close"] for b in bars]
    n = len(closes)
    if n < cfg["min_history"]:
        return None
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    if not (s20 and s50 and s200):
        return None
    last = closes[-1]
    high52w = max(b["high"] for b in bars[-252:])
    prox_pct = (last / high52w - 1.0) * 100.0
    if prox_pct < -cfg["leader_prox_pct"] or not (last > s50 > s200):
        return None
    dist20 = (last / s20 - 1.0) * 100.0
    if cfg["at_sma20_lo"] <= dist20 <= cfg["pullback_dist20_max"]:
        plan = "at_sma20"
    elif cfg["pullback_dist20_max"] < dist20 <= cfg["dip_buy_max"]:
        plan = "sma20_dip_buy"
    elif dist20 > cfg["dip_buy_max"]:
        plan = "wait_deeper"
    else:
        plan = "below_sma20"
    return {
        "pullback_plan": plan,
        "high52w": high52w,
        "prox_pct": round(prox_pct, 2),
        "dist20_pct": round(dist20, 2),
        "sma20": s20,
        "sma50": s50,
        "sma200": s200,
    }


def score_leaders(bars, sig, cfg, bench):
    closes = [b["close"] for b in bars]
    proximity = _clamp(100.0 + sig["prox_pct"] * 12.0)  # 0% off = 100, -5% = 40
    trend = _ma_alignment_factor(closes, sig["sma50"], sig["sma200"], cfg)
    up, dn = [], []
    for i in range(len(bars) - 20, len(bars)):
        if bars[i]["close"] > bars[i - 1]["close"]:
            up.append(bars[i]["volume"])
        elif bars[i]["close"] < bars[i - 1]["close"]:
            dn.append(bars[i]["volume"])
    ratio = (sum(up) / max(len(up), 1)) / max(sum(dn) / max(len(dn), 1), 1e-9) if dn else 1.5
    accumulation = _band(ratio, [(1.3, 100), (1.1, 70), (0.95, 45)], 15.0)
    rel, rel_warn = _rel_strength_factor(bars, bench)
    timing = {"at_sma20": 100.0, "sma20_dip_buy": 80.0, "wait_deeper": 35.0, "below_sma20": 20.0}[
        sig["pullback_plan"]
    ]
    factors = {
        "proximity_to_high": proximity,
        "trend_quality": trend,
        "accumulation": accumulation,
        "rel_strength": rel,
        "entry_timing": timing,
    }
    warnings = [w for w in [rel_warn] if w]
    if sig["dist20_pct"] > cfg["parabolic_dist20_pct"]:
        warnings.append("parabolic_extension_above_sma20")
    return factors, warnings


def plan_leaders(bars, sig, cfg):
    last = bars[-1]["close"]
    low20 = min(b["low"] for b in bars[-20:])
    below = [v for v in (low20, sig["sma50"]) if v < last]
    stop = max(below) if below else min(low20, sig["sma50"])
    if sig["high52w"] <= last * 1.005:
        high20 = max(b["high"] for b in bars[-20:])
        t1, t1_basis = last + (high20 - low20), "measured_move_new_highs"
    else:
        t1, t1_basis = sig["high52w"], "52w_high"
    risk = last - stop
    return {
        "entry": sig["pullback_plan"],
        "stop": round(stop, 2),
        "t1": round(t1, 2),
        "t1_basis": t1_basis,
        "risk_pct": round(risk / last * 100.0, 2),
        "reward_risk_at_t1": round((t1 - last) / max(risk, 1e-9), 2),
    }


def cap_leaders(grade, sig, cfg):
    if sig["pullback_plan"] in ("wait_deeper", "below_sma20"):
        return _worst_grade(grade, "C"), [f"watchlist_only_plan_{sig['pullback_plan']}"]
    return grade, []


# ---------------------------------------------------------------- volatility
def detect_volatility(bars, cfg):
    if len(bars) < cfg["min_history"]:
        return None
    last = bars[-1]["close"]
    a = atr(bars, 14)
    if not a or not last:
        return None
    atr_pct = a / last * 100.0
    if atr_pct < cfg["min_atr_pct"]:
        return None
    a_prev = atr(bars, 14, end=len(bars) - 60)
    prev_close = bars[-61]["close"] if len(bars) >= 61 else last
    expansion_ratio = (atr_pct / (a_prev / prev_close * 100.0)) if a_prev and prev_close else None
    bodies = []
    for b in bars[-10:]:
        rng = b["high"] - b["low"]
        if rng > 0:
            bodies.append(abs(b["close"] - b["open"]) / rng)
    body_ratio = sum(bodies) / len(bodies) if bodies else 0.0
    return {
        "atr_pct": round(atr_pct, 2),
        "expansion_ratio": round(expansion_ratio, 2) if expansion_ratio else None,
        "body_ratio": round(body_ratio, 2),
    }


def score_volatility(bars, sig, cfg, bench):
    expansion = _clamp((sig["atr_pct"] - cfg["min_atr_pct"]) * 8.3 + 50.0)
    structure = _band(sig["body_ratio"], [(0.55, 100), (0.45, 70), (0.35, 45)], 10.0)
    liquidity = _liquidity_factor(bars)
    rv = rvol_last(bars, cfg["rvol_baseline"]) or 0.0
    participation = _band(rv, [(2.0, 100), (1.3, 70), (0.9, 50)], 25.0)
    factors = {
        "expansion": expansion,
        "structure_quality": structure,
        "liquidity": liquidity,
        "participation": participation,
    }
    warnings = []
    if sig["expansion_ratio"] is None:
        warnings.append("atr_baseline_unavailable")
    warnings.append("catalyst_unknown_identify_before_trading")
    return factors, warnings


def plan_volatility(bars, sig, cfg):
    high20 = max(b["high"] for b in bars[-20:])
    low20 = min(b["low"] for b in bars[-20:])
    return {
        "breakout_trigger": round(high20, 2),
        "range_support": round(low20, 2),
        "size_note": "reduce_position_30_50pct_per_doc",
    }


def cap_volatility(grade, sig, cfg):
    if sig["body_ratio"] < cfg["chaotic_body_ratio"]:
        return _worst_grade(grade, "C"), ["chaotic_tape_watchlist_only"]
    return grade, []


# ---------------------------------------------------------------- in-play
def detect_in_play(bars, cfg):
    if len(bars) < cfg["min_history"]:
        return None
    rv = rvol_last(bars, cfg["rvol_baseline"])
    prev_close = bars[-2]["close"]
    day_chg = (bars[-1]["close"] / prev_close - 1.0) * 100.0 if prev_close else 0.0
    if rv is None or rv < cfg["inplay_min_rvol"] or day_chg < cfg["inplay_min_day_chg"]:
        return None
    a_prior = atr(bars, 14, end=len(bars) - 1)
    day_range = bars[-1]["high"] - bars[-1]["low"]
    return {
        "rvol": round(rv, 2),
        "day_change_pct": round(day_chg, 2),
        "close_loc": round(close_location(bars[-1]), 2),
        "range_vs_atr": round(day_range / a_prior, 2) if a_prior else None,
    }


def score_in_play(bars, sig, cfg, bench):
    factors = {
        "rvol_strength": _band(sig["rvol"], [(5, 100), (3, 85), (2.5, 70), (2, 55)], 40.0),
        "day_move": _band(sig["day_change_pct"], [(10, 100), (7, 85), (5, 70), (3, 50)], 30.0),
        "close_strength": _band(sig["close_loc"], [(0.8, 100), (0.65, 75), (0.5, 50)], 20.0),
        "range_expansion": _band(
            sig["range_vs_atr"] or 0.0, [(2.5, 100), (1.8, 75), (1.2, 55)], 30.0
        ),
        "liquidity": _liquidity_factor(bars),
    }
    return factors, ["catalyst_unknown_identify_before_trading"]


def plan_in_play(bars, sig, cfg):
    return {
        "day1_high": round(bars[-1]["high"], 2),
        "day1_low": round(bars[-1]["low"], 2),
        "playbook": "day2_3_pullback_to_support_per_doc",
        "note": "VWAP_and_premarket_levels_not_in_daily_bars_mark_live",
    }


def cap_in_play(grade, sig, cfg):
    if sig["close_loc"] < cfg["faded_close_loc"]:
        return _worst_grade(grade, "C"), ["faded_close_lower_half_of_range"]
    return grade, []


# ---------------------------------------------------------------- unusual volume
def detect_unusual_volume(bars, cfg):
    if len(bars) < cfg["min_history"]:
        return None
    rv = rvol_last(bars, cfg["rvol_baseline"])
    if rv is None or rv < cfg["unusual_min_rvol"]:
        return None
    a_prior = atr(bars, 14, end=len(bars) - 1)
    day_range = bars[-1]["high"] - bars[-1]["low"]
    range_atr = day_range / a_prior if a_prior else None
    loc = close_location(bars[-1])
    if range_atr is not None and range_atr < cfg["absorption_range_atr"]:
        quadrant, direction = "absorption", "wait_for_break"
    elif loc >= cfg["acc_close_loc"]:
        quadrant, direction = "accumulation", "bullish"
    elif loc <= cfg["dist_close_loc"]:
        quadrant, direction = "distribution", "bearish"
    else:
        quadrant, direction = "chop", "skip"
    return {
        "rvol": round(rv, 2),
        "quadrant": quadrant,
        "direction": direction,
        "close_loc": round(loc, 2),
        "range_vs_atr": round(range_atr, 2) if range_atr is not None else None,
    }


def score_unusual_volume(bars, sig, cfg, bench):
    if sig["quadrant"] == "accumulation":
        clarity = _clamp(
            (sig["close_loc"] - cfg["acc_close_loc"]) / (1 - cfg["acc_close_loc"]) * 100.0
        )
    elif sig["quadrant"] == "distribution":
        clarity = _clamp((cfg["dist_close_loc"] - sig["close_loc"]) / cfg["dist_close_loc"] * 100.0)
    elif sig["quadrant"] == "absorption":
        r = sig["range_vs_atr"] or 0.0
        clarity = _clamp((cfg["absorption_range_atr"] - r) / cfg["absorption_range_atr"] * 100.0)
    else:
        clarity = 20.0
    factors = {
        "rvol_magnitude": _band(sig["rvol"], [(6, 100), (4, 85), (3, 65)], 50.0),
        "quadrant_clarity": clarity,
        "range_participation": _band(
            sig["range_vs_atr"] or 0.0, [(2, 100), (1.2, 70), (0.8, 50)], 40.0
        ),
        "liquidity": _liquidity_factor(bars),
    }
    return factors, [
        "signal_not_direction_confirm_price_action",
        "catalyst_unknown_identify_before_trading",
    ]


def plan_unusual_volume(bars, sig, cfg):
    return {
        "bias": sig["direction"],
        "day_high": round(bars[-1]["high"], 2),
        "day_low": round(bars[-1]["low"], 2),
        "note": "quadrant_read_from_daily_OHLC_verify_intraday_tape_live",
    }


def cap_unusual_volume(grade, sig, cfg):
    if sig["quadrant"] == "chop":
        return "D", ["quadrant_chop_skip_per_doc"]
    if sig["quadrant"] == "absorption":
        return _worst_grade(grade, "C"), ["absorption_wait_for_break"]
    return grade, []


# ---------------------------------------------------------------- weak
def detect_weak(bars, cfg):
    if len(bars) < cfg["min_history"]:
        return None
    closes = [b["close"] for b in bars]
    prev_close = closes[-2]
    day_chg = (closes[-1] / prev_close - 1.0) * 100.0 if prev_close else 0.0
    rv = rvol_last(bars, cfg["rvol_baseline"])
    if day_chg > cfg["weak_max_day_chg"] or rv is None or rv < cfg["weak_min_rvol"]:
        return None
    s50, s200 = sma(closes, 50), sma(closes, 200)
    aligned = bool(s50 and s200 and closes[-1] < s50 < s200)
    highs = block_extremes(bars, "high")
    gap_down = bars[-1]["open"] <= prev_close * (1 - cfg["gap_down_min_pct"] / 100.0)
    loc = close_location(bars[-1])
    return {
        "day_change_pct": round(day_chg, 2),
        "rvol": round(rv, 2),
        "downtrend_aligned": aligned,
        "lower_highs": bool(highs and highs[0] > highs[1] > highs[2]),
        "close_loc": round(loc, 2),
        "gap_down_bought_back": bool(gap_down and loc >= cfg["gap_recovered_loc"]),
    }


def score_weak(bars, sig, cfg, bench):
    decline = _band(-sig["day_change_pct"], [(8, 100), (6, 85), (4.5, 70), (3, 55)], 40.0)
    structure = 100.0 if sig["lower_highs"] else 40.0
    closes = [b["close"] for b in bars]
    s50 = sma(closes, 50)
    if sig["downtrend_aligned"]:
        trend = 100.0
    elif s50 and closes[-1] < s50:
        trend = 50.0
    else:
        trend = 0.0
    factors = {
        "decline_magnitude": decline,
        "rvol_strength": _band(sig["rvol"], [(3, 100), (2, 80), (1.5, 55)], 40.0),
        "weakness_structure": structure,
        "trend_alignment": trend,
        "close_weakness": _band(1 - sig["close_loc"], [(0.8, 100), (0.65, 75), (0.5, 50)], 20.0),
    }
    warnings = []
    if not sig["downtrend_aligned"]:
        warnings.append("counter_trend_weakness_cross_check_swing_short")
    return factors, warnings


def plan_weak(bars, sig, cfg):
    return {
        "breakdown_trigger": round(bars[-1]["low"], 2),
        "lower_high_ref": round(max(b["high"] for b in bars[-5:]), 2),
        "playbook": "weak_bounce_short_or_breakdown_continuation_per_doc",
        "note": "VWAP_not_in_daily_bars_mark_live",
    }


def cap_weak(grade, sig, cfg):
    if sig["gap_down_bought_back"]:
        return _worst_grade(grade, "C"), ["gap_down_bought_back_avoid_per_doc"]
    return grade, []


# ---------------------------------------------------------------- registry
SCREENS = {
    "swing-long": {
        "title": "Swing Trend: Longs",
        "detect": detect_swing_long,
        "score": score_swing_long,
        "plan": plan_swing_long,
        "cap": cap_swing_long,
        "label": lambda sig: sig["trigger"],
    },
    "swing-short": {
        "title": "Swing Trend: Shorts",
        "detect": detect_swing_short,
        "score": score_swing_short,
        "plan": plan_swing_short,
        "cap": cap_swing_short,
        "label": lambda sig: sig["trigger"],
    },
    "leaders": {
        "title": "Strength: Leaders",
        "detect": detect_leaders,
        "score": score_leaders,
        "plan": plan_leaders,
        "cap": cap_leaders,
        "label": lambda sig: sig["pullback_plan"],
    },
    "volatility": {
        "title": "Volatility: High",
        "detect": detect_volatility,
        "score": score_volatility,
        "plan": plan_volatility,
        "cap": cap_volatility,
        "label": lambda sig: f"atr_{sig['atr_pct']}pct",
    },
    "in-play": {
        "title": "Breakout: In-Play (next-session watchlist)",
        "detect": detect_in_play,
        "score": score_in_play,
        "plan": plan_in_play,
        "cap": cap_in_play,
        "label": lambda sig: f"rvol_{sig['rvol']}x",
    },
    "unusual-volume": {
        "title": "Volume: Unusual (next-session watchlist)",
        "detect": detect_unusual_volume,
        "score": score_unusual_volume,
        "plan": plan_unusual_volume,
        "cap": cap_unusual_volume,
        "label": lambda sig: sig["quadrant"],
    },
    "weak": {
        "title": "Breakdown: Weak (next-session watchlist)",
        "detect": detect_weak,
        "score": score_weak,
        "plan": plan_weak,
        "cap": cap_weak,
        "label": lambda sig: "downtrend_aligned" if sig["downtrend_aligned"] else "counter_trend",
    },
}

CHECKLISTS = {
    "swing-long": [
        "Price above a rising SMA50 with SMA200 confirming — trend intact",
        "Pullbacks on lighter volume than rallies (digestion, not distribution)",
        "No earnings within 5 sessions (auto-rejected when a date is known — verify UNKNOWNs manually)",
        "Prefer Pullback Zone / Breakout Ready triggers; Extended is capped to watchlist",
    ],
    "swing-short": [
        "Price below a falling SMA50 with SMA200 confirming — downtrend intact",
        "Never short into Oversold — wait for the bounce to form a lower high / bear flag",
        "Check locate/HTB and short interest first (short-squeeze-radar) — this screen cannot",
        "No earnings within 5 sessions (auto-rejected when a date is known)",
    ],
    "leaders": [
        "Within 5% of the 52-week high with SMA50/SMA200 uptrend confirmed",
        "Buy the pullback per the Pullback Plan label — never chase Wait Deeper names",
        "Up-day volume heavier than down-day volume (accumulation)",
        "Note earnings/catalyst proximity before sizing up",
    ],
    "volatility": [
        "Confirm the volatility is structured (clear levels), not chaotic — chaotic is capped",
        "Reduce position size 30-50% versus normal — wider stops, same dollar risk",
        "Check live spreads before entry; spread data is not in daily bars",
        "Identify what is driving the expansion — catalyst is UNKNOWN to this screen",
    ],
    "in-play": [
        "This is a Day-2/3 watchlist — do NOT treat it as a live Day-1 entry signal",
        "Identify the catalyst before trading; the screen cannot (marked UNKNOWN)",
        "Mark VWAP, premarket levels, and spreads live at the next open",
        "Best entries: pullback to support on Day 2-3 with volume still elevated",
    ],
    "unusual-volume": [
        "Unusual volume is a SIGNAL, not a direction — trade the quadrant read, not the RVOL",
        "Accumulation = bullish bias, Distribution = bearish, Absorption = wait, Chop = skip",
        "Re-check participation at the next open — a one-day spike may be stale",
        "Identify the catalyst before trading; the screen cannot (marked UNKNOWN)",
    ],
    "weak": [
        "Highest conviction when weakness aligns with a confirmed downtrend (label says which)",
        "Short weak bounces, not fresh lows — wait for a lower high to form",
        "Check short interest / float first (short-squeeze-radar); squeeze data is UNKNOWN here",
        "Skip gap-downs that were bought back (auto-capped) — that is demand, not weakness",
    ],
}


# ---------------------------------------------------------------- grading
def grade_for(composite):
    for threshold, g in GRADE_THRESHOLDS:
        if composite >= threshold:
            return g
    return "D"


def _worst_grade(a, b):
    """The worse of two grades (D worst)."""
    return a if GRADE_ORDER.index(a) >= GRADE_ORDER.index(b) else b


def grade_at_or_above(grade, threshold):
    return GRADE_ORDER.index(grade) <= GRADE_ORDER.index(threshold)


# ---------------------------------------------------------------- invalidation
def invalidation_reasons(bars, cfg):
    reasons = []
    if len(bars) < cfg["min_history"]:
        reasons.append("insufficient_history")
        return reasons
    last = bars[-1]
    if last["close"] < cfg["min_price"]:
        reasons.append("price_below_minimum")
    if adv_usd(bars) < cfg["min_adv_usd"]:
        reasons.append("insufficient_dollar_volume")
    return reasons


def market_regime(bars):
    if not bars:
        return None
    closes = [b["close"] for b in bars]
    s50, s200 = sma(closes, 50), sma(closes, 200)
    if not (s50 and s200):
        return None
    last = closes[-1]
    if last > s50 and last > s200:
        label = "risk_on"
    elif last < s50 and last < s200:
        label = "risk_off"
    else:
        label = "mixed"
    return {
        "benchmark": "SPY",
        "label": label,
        "close": round(last, 2),
        "sma50": round(s50, 2),
        "sma200": round(s200, 2),
    }


# ---------------------------------------------------------------- screening
def screen(
    bars_by_ticker,
    screen_name,
    cfg,
    as_of,
    bench=None,
    earnings_by_ticker=None,
    universe_total=None,
):
    spec = SCREENS[screen_name]
    weights = WEIGHTS[screen_name]
    earnings_by_ticker = earnings_by_ticker or {}
    window = cfg["exclude_earnings_within_days"]
    session = max((bars[-1]["date"] for bars in bars_by_ticker.values() if bars), default=as_of)
    regime = market_regime(bench) if bench else None
    gate_shorts = (
        cfg.get("regime_gate", True)
        and screen_name in SHORT_BIAS_SCREENS
        and regime is not None
        and regime["label"] == "risk_on"
    )

    candidates, rejected, no_signal = [], {}, []
    for ticker, bars in sorted(bars_by_ticker.items()):
        if not bars or len(bars) < 2:
            no_signal.append(ticker)
            continue
        sig = spec["detect"](bars, cfg)
        if sig is None:
            no_signal.append(ticker)
            continue
        reasons = invalidation_reasons(bars, cfg)
        if reasons:
            rejected[ticker] = reasons
            continue

        edate = earnings_by_ticker.get(ticker)
        earnings_warning = None
        if edate and window:
            days_to = (date.fromisoformat(edate) - date.fromisoformat(as_of)).days
            if 0 <= days_to <= window:
                if screen_name in NEXT_SESSION_SCREENS:
                    earnings_warning = f"earnings_within_{window}d_{edate}"
                else:
                    rejected[ticker] = [f"earnings_{edate}_within_{window}d"]
                    continue

        factors, warnings = spec["score"](bars, sig, cfg, bench)
        factors = {k: round(v, 1) for k, v in factors.items()}
        composite = round(sum(factors[k] * w for k, w in weights.items()), 1)
        grade = grade_for(composite)
        grade, cap_warnings = spec["cap"](grade, sig, cfg)
        warnings += cap_warnings
        if earnings_warning:
            warnings.append(earnings_warning)
        if not edate:
            warnings.append("earnings_date_unknown_verify_manually")
        if bars[-1]["date"] != session:
            warnings.append(f"stale_data_last_bar_{bars[-1]['date']}")
        if screen_name in SHORT_BIAS_SCREENS:
            warnings.append("short_interest_unknown_check_short_squeeze_radar")
        if gate_shorts:
            grade = _worst_grade(grade, "C")
            warnings.append("regime_risk_on_short_capped_watchlist_only")

        if not grade_at_or_above(grade, cfg["watch_min_grade"]):
            rejected[ticker] = [f"below_watch_grade_{grade}"]
            continue
        candidates.append(
            {
                "ticker": ticker,
                "grade": grade,
                "label": spec["label"](sig),
                "composite": composite,
                "factors": factors,
                "signal": sig,
                "warnings": warnings,
                "next_earnings": edate,
                "last_close": round(bars[-1]["close"], 2),
                "last_bar_date": bars[-1]["date"],
                "plan": spec["plan"](bars, sig, cfg),
            }
        )
    candidates.sort(key=lambda c: -c["composite"])
    return {
        "screen": screen_name,
        "title": spec["title"],
        "as_of": as_of,
        "session": session,
        "timing_note": TIMING_NOTE if screen_name in NEXT_SESSION_SCREENS else None,
        "params": {**cfg, "weights": weights},
        "scanned": len(bars_by_ticker),
        "universe_total": universe_total,
        "market_regime": regime,
        "candidates": candidates,
        "top_picks": candidates[: cfg["top"]],
        "rejected": rejected,
        "no_signal_count": len(no_signal),
    }


# ---------------------------------------------------------------- reports
def render_markdown(result):
    lines = [
        f"# Swing Setup Screener — {result['title']}",
        "",
        f"**As of:** {result['as_of']} · **Session evaluated:** {result['session']}"
        " (last completed session only)",
        f"**Scanned:** {result['scanned']} | **Candidates:** {len(result['candidates'])}"
        f" | **Rejected:** {len(result['rejected'])}",
    ]
    total = result.get("universe_total")
    if total and total > result["scanned"]:
        lines.append(
            f"> ⚠️ **Coverage:** {result['scanned']} of {total} tickers matching the universe"
            " filters were scanned (top by 3-month average share volume). Raise"
            " `--universe-size` for full coverage."
        )
    if result.get("timing_note"):
        note = TIMING_NOTE.replace(
            "last completed session", f"last completed session ({result['session']})", 1
        )
        lines += ["", f"> ⚠️ **Timing contract:** {note}"]
        lines += ["> In short: **next-session watchlist**, not a live entry feed."]
    regime = result.get("market_regime")
    if regime:
        note = {
            "risk_on": "tape supportive",
            "mixed": "mixed tape — be selective",
            "risk_off": "hostile tape for longs",
        }[regime["label"]]
        lines.append(
            f"**Market regime:** {regime['label']} ({note}) — {regime['benchmark']}"
            f" {regime['close']} vs SMA50 {regime['sma50']} / SMA200 {regime['sma200']}"
        )
    lines += ["", "## Top Picks", ""]
    if not result["top_picks"]:
        lines.append("_No qualifying setups for this screen today._")
    else:
        lines.append("| # | Ticker | Grade | Score | Label | Close | Key levels |")
        lines.append("|---|--------|-------|-------|-------|-------|------------|")
        for i, c in enumerate(result["top_picks"], 1):
            levels = " · ".join(
                f"{k} {v}" for k, v in c["plan"].items() if isinstance(v, (int, float))
            )
            lines.append(
                f"| {i} | {c['ticker']} | {c['grade']} | {c['composite']} | {c['label']}"
                f" | {c['last_close']} | {levels} |"
            )
    lines += ["", "## Watchlist", ""]
    for c in result["candidates"]:
        factor_str = " / ".join(f"{k} {v}" for k, v in c["factors"].items())
        plan_str = " · ".join(f"{k}={v}" for k, v in c["plan"].items())
        lines += [
            f"### {c['ticker']} — {c['grade']} ({c['composite']}) · {c['label']}",
            f"- Close {c['last_close']} (bar {c['last_bar_date']})",
            f"- Factors: {factor_str}",
            f"- Plan: {plan_str}",
        ]
        if c.get("next_earnings"):
            lines.append(f"- Next earnings: {c['next_earnings']}")
        if c["warnings"]:
            lines.append(f"- ⚠️ {', '.join(c['warnings'])}")
        lines.append("")
    lines += ["## Pre-Entry Checklist", ""]
    lines += [f"- [ ] {item}" for item in CHECKLISTS[result["screen"]]]
    if result["rejected"]:
        lines += ["", "## Rejected", ""]
        lines += [f"- {t}: {', '.join(r)}" for t, r in sorted(result["rejected"].items())]
    lines.append("")
    return "\n".join(lines)


def write_reports(result, output_dir, prefix):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{prefix}_{result['as_of']}.json"
    md_path = out / f"{prefix}_{result['as_of']}.md"
    json_path.write_text(json.dumps(result, indent=2) + "\n")
    md_path.write_text(render_markdown(result))
    return str(json_path), str(md_path)


# ---------------------------------------------------------------- data fetch
def bars_from_fixture(path):
    data = json.loads(Path(path).read_text())
    return data["bars"], data.get("as_of"), data.get("earnings", {})


def fetch_earnings_dates(tickers):
    import yfinance as yf

    out = {}
    today = date.today().isoformat()
    for t in tickers:
        try:
            cal = yf.Ticker(t).calendar or {}
            dates = cal.get("Earnings Date") or []
            upcoming = sorted(d.isoformat()[:10] for d in dates)
            upcoming = [d for d in upcoming if d >= today]
            if upcoming:
                out[t] = upcoming[0]
        except Exception as exc:  # missing calendar must not kill the run
            logger.warning("Earnings lookup failed for %s: %s", t, exc)
    return out


def yahoo_universe(cfg, size):
    """Keyless universe via Yahoo EquityQuery, paginated (Yahoo caps each
    request at 250 rows). Returns (symbols, total_matching)."""
    import yfinance as yf

    Q = yf.EquityQuery
    query = Q(
        "and",
        [
            Q("eq", ["region", "us"]),
            Q("gt", ["intradayprice", cfg["min_price"]]),
            Q("gt", ["intradaymarketcap", 2_000_000_000]),
            Q("gt", ["avgdailyvol3m", 500_000]),
        ],
    )
    symbols, seen, total, offset = [], set(), None, 0
    while len(symbols) < size:
        chunk = min(250, size - len(symbols))
        res = yf.screen(query, sortField="avgdailyvol3m", sortAsc=False, size=chunk, offset=offset)
        quotes = res.get("quotes", [])
        total = res.get("total", total)
        for q in quotes:
            sym = q.get("symbol")
            if sym and sym not in seen:
                seen.add(sym)
                symbols.append(sym)
        offset += len(quotes)
        if not quotes or (total is not None and offset >= total):
            break
    if total and total > len(symbols):
        logger.warning(
            "Universe covers %d of %d matching tickers — raise --universe-size for full coverage.",
            len(symbols),
            total,
        )
    return symbols, total


def fetch_bars(tickers, period="1y"):
    import yfinance as yf

    # auto_adjust=False: raw prices so plan levels match broker/TradingView charts.
    df = yf.download(
        tickers,
        period=period,
        auto_adjust=False,
        group_by="ticker",
        threads=True,
        progress=False,
    )
    bars_by_ticker = {}
    multi = hasattr(df.columns, "levels")
    for t in tickers:
        try:
            sub = df[t].dropna() if multi else df.dropna()
        except KeyError:
            logger.warning("No Yahoo data for %s — skipped.", t)
            continue
        bars = [
            {
                "date": idx.date().isoformat(),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]),
            }
            for idx, row in sub.iterrows()
        ]
        if bars:
            bars_by_ticker[t] = bars
    return bars_by_ticker


def tickers_from_csv(path):
    import csv

    with Path(path).open() as fh:
        rows = list(csv.DictReader(fh))
    col = next((c for c in rows[0] if c.strip().lower() in ("ticker", "symbol")), None)
    if not col:
        raise SystemExit("ERROR: CSV needs a Ticker/Symbol column.")
    return [r[col].strip().upper() for r in rows if r.get(col, "").strip()]


# ---------------------------------------------------------------- CLI
def build_arg_parser():
    p = argparse.ArgumentParser(
        description="Swing Setup Screener — 7 EOD screens (last completed session only)"
    )
    p.add_argument(
        "--screen",
        required=True,
        choices=[*SCREENS, "all"],
        help="Which screen to run ('all' runs all seven, one report pair each)",
    )
    p.add_argument("--tickers", help="Comma-separated tickers (skips the Yahoo universe screen)")
    p.add_argument("--universe-csv", help="CSV with a Ticker/Symbol column (e.g. Finviz export)")
    p.add_argument(
        "--universe-size",
        type=int,
        default=2000,
        help="Max tickers from the Yahoo universe screen, paginated 250/request"
        " (default 2000 = full ~1.8k universe; use 250 for a quick most-liquid scan)",
    )
    p.add_argument(
        "--fixture", help="Offline JSON fixture: {as_of, bars:{TICKER:[...]}, earnings:{}}"
    )
    p.add_argument("--min-price", type=float, default=DEFAULTS["min_price"])
    p.add_argument("--min-adv-usd", type=float, default=DEFAULTS["min_adv_usd"])
    p.add_argument(
        "--exclude-earnings-within-days",
        type=int,
        default=DEFAULTS["exclude_earnings_within_days"],
        help="Swing screens reject, watchlist screens warn, within this window (0 disables)",
    )
    p.add_argument("--top", type=int, default=DEFAULTS["top"])
    p.add_argument("--watch-min-grade", choices=GRADE_ORDER, default=DEFAULTS["watch_min_grade"])
    p.add_argument(
        "--allow-partial-today",
        action="store_true",
        help="DANGEROUS: score today's still-open bar instead of dropping it",
    )
    p.add_argument(
        "--no-regime-gate",
        action="store_true",
        help="Disable the risk_on short cap (backtest-validated; disabling is on you)",
    )
    p.add_argument("--output-dir", default="reports/")
    p.add_argument("--output-prefix", default=None, help="Default: swing_setups_<screen>")
    p.add_argument("--as-of", default=None, help="YYYY-MM-DD; default: today")
    return p


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    args = build_arg_parser().parse_args(argv)
    cfg = dict(
        DEFAULTS,
        min_price=args.min_price,
        min_adv_usd=args.min_adv_usd,
        exclude_earnings_within_days=args.exclude_earnings_within_days,
        top=args.top,
        watch_min_grade=args.watch_min_grade,
        regime_gate=not args.no_regime_gate,
    )
    screen_names = list(SCREENS) if args.screen == "all" else [args.screen]

    as_of = args.as_of
    earnings = {}
    universe_total = None
    if args.fixture:
        bars_by_ticker, fixture_as_of, earnings = bars_from_fixture(args.fixture)
        as_of = as_of or fixture_as_of
    else:
        if args.tickers:
            tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        elif args.universe_csv:
            tickers = tickers_from_csv(args.universe_csv)
        else:
            tickers, universe_total = yahoo_universe(cfg, args.universe_size)
        fetch_list = tickers if "SPY" in tickers else [*tickers, "SPY"]
        logger.info("Fetching daily bars for %d tickers...", len(fetch_list))
        bars_by_ticker = fetch_bars(fetch_list)
        if not args.allow_partial_today:
            now_et = datetime.now(ET)
            before = sum(len(b) for b in bars_by_ticker.values())
            bars_by_ticker = {t: strip_partial_bar(b, now_et) for t, b in bars_by_ticker.items()}
            dropped = before - sum(len(b) for b in bars_by_ticker.values())
            if dropped:
                logger.info(
                    "Dropped %d in-progress bars dated today (market not closed at %s ET).",
                    dropped,
                    now_et.strftime("%H:%M"),
                )
    bench = bars_by_ticker.get("SPY")  # screen() derives the regime from this
    if not args.fixture and "SPY" not in (args.tickers or ""):
        bars_by_ticker = {t: b for t, b in bars_by_ticker.items() if t != "SPY"}
    as_of = as_of or date.today().isoformat()

    exit_paths = []
    for name in screen_names:
        result = screen(
            bars_by_ticker,
            name,
            cfg,
            as_of,
            bench=bench,
            earnings_by_ticker=earnings,
            universe_total=universe_total,
        )
        # Earnings second pass: only for survivors, only on a live run.
        if not args.fixture and cfg["exclude_earnings_within_days"] and result["candidates"]:
            survivors = [c["ticker"] for c in result["candidates"] if c["ticker"] not in earnings]
            if survivors:
                logger.info(
                    "[%s] checking earnings dates for %d candidates...", name, len(survivors)
                )
                earnings.update(fetch_earnings_dates(survivors))
                result = screen(
                    bars_by_ticker,
                    name,
                    cfg,
                    as_of,
                    bench=bench,
                    earnings_by_ticker=earnings,
                    universe_total=universe_total,
                )
        prefix = args.output_prefix or f"swing_setups_{name.replace('-', '_')}"
        json_path, md_path = write_reports(result, args.output_dir, prefix)
        logger.info(
            "[%s] scanned %d | candidates %d | top %d",
            name,
            result["scanned"],
            len(result["candidates"]),
            len(result["top_picks"]),
        )
        exit_paths += [json_path, md_path]
    print("\n".join(exit_paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
