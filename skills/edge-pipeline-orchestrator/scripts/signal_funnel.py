#!/usr/bin/env python3
"""Tiered, cost-controlled signal funnel for the edge pipeline.

Three cheap-to-expensive tiers filter candidates before any paid LLM/debate call:

  Tier 0 (triggers)  — a library of pure statistical detectors over OHLCV, each
                       scored 0-10 with a fired flag, combined into a weighted
                       composite normalized against the sum of ALL weights so
                       co-firing beats a lone max. Zero-weight "surfacing bypass"
                       lanes surface a candidate without polluting the denominator.
  Tier 1 (TA filter) — a multi-timeframe (1h/4h/1d) EMA/RSI/ATR/ADX/volume additive
                       filter emitting CONFIRMED / WEAK / REJECTED with directional
                       weighting (a clean uptrend outscores a clean downtrend).
  Tier 2 (gate)      — a candidate is escalated to the expensive LLM verdict ONLY
                       when the TA filter says CONFIRMED, plus two named bypass
                       lanes (momentum burst, whale accumulation).

The orchestrator runs Tier 0 over all candidates, Tier 1 over survivors, and only
escalates the CONFIRMED (or burst/whale-bypassed) ones — cutting LLM spend to the
fraction of candidates that actually pass a statistical bar.

No network access: candles are supplied as input (from candidate JSON), so every
function imports with the standard library only and tests run fully offline.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ─── Candle access ────────────────────────────────────────────────────────────

_LONG_KEYS = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}


def _cv(candle: Any, key: str) -> float:
    """Read an OHLCV field (o/h/l/c/v) from a dict, tolerating long key names."""
    if isinstance(candle, dict):
        if key in candle:
            return float(candle[key] or 0)
        long = _LONG_KEYS.get(key)
        if long and long in candle:
            return float(candle[long] or 0)
        return 0.0
    return float(getattr(candle, key, 0) or 0)


# ─── Math helpers (re-implemented standard indicator formulas) ─────────────────


def ema(values: list[float], period: int) -> list[float]:
    """Exponential moving average; out[0] seeds with the first value."""
    out = [float("nan")] * len(values)
    if not values:
        return out
    k = 2 / (period + 1)
    e = values[0]
    out[0] = e
    for i in range(1, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def sma(values: list[float], period: int) -> list[float]:
    """Simple moving average; NaN until `period` samples are available."""
    out = [float("nan")] * len(values)
    acc = 0.0
    for i in range(len(values)):
        acc += values[i]
        if i >= period:
            acc -= values[i - period]
        if i >= period - 1:
            out[i] = acc / period
    return out


def atr(candles: list[Any], period: int = 14) -> list[float]:
    """Average true range (Wilder smoothing)."""
    n = len(candles)
    out = [float("nan")] * n
    if n <= period:
        return out
    tr = [0.0] * n
    for i in range(1, n):
        h, low = _cv(candles[i], "h"), _cv(candles[i], "l")
        pc = _cv(candles[i - 1], "c")
        tr[i] = max(h - low, abs(h - pc), abs(low - pc))
    out[period] = sum(tr[1 : period + 1]) / period
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def rsi(candles: list[Any], period: int = 14) -> list[float]:
    """Relative strength index (Wilder smoothing)."""
    n = len(candles)
    out = [float("nan")] * n
    if n <= period:
        return out
    gain = loss = 0.0
    for i in range(1, period + 1):
        d = _cv(candles[i], "c") - _cv(candles[i - 1], "c")
        if d >= 0:
            gain += d
        else:
            loss -= d
    avg_g = gain / period
    avg_l = loss / period
    out[period] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(period + 1, n):
        d = _cv(candles[i], "c") - _cv(candles[i - 1], "c")
        avg_g = (avg_g * (period - 1) + (d if d > 0 else 0)) / period
        avg_l = (avg_l * (period - 1) + (-d if d < 0 else 0)) / period
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def adx(candles: list[Any], period: int = 14) -> list[float]:
    """Average directional index."""
    n = len(candles)
    out = [float("nan")] * n
    if n <= period * 2:
        return out
    tr = [0.0] * n
    p_dm = [0.0] * n
    m_dm = [0.0] * n
    for i in range(1, n):
        h, low = _cv(candles[i], "h"), _cv(candles[i], "l")
        pc = _cv(candles[i - 1], "c")
        ph, pl = _cv(candles[i - 1], "h"), _cv(candles[i - 1], "l")
        tr[i] = max(h - low, abs(h - pc), abs(low - pc))
        up = h - ph
        dn = pl - low
        p_dm[i] = up if (up > dn and up > 0) else 0
        m_dm[i] = dn if (dn > up and dn > 0) else 0
    tr_s = sum(tr[1 : period + 1])
    p_s = sum(p_dm[1 : period + 1])
    m_s = sum(m_dm[1 : period + 1])

    def _dx() -> float:
        pdi = 0 if tr_s == 0 else 100 * p_s / tr_s
        mdi = 0 if tr_s == 0 else 100 * m_s / tr_s
        total = pdi + mdi
        return 0 if total == 0 else 100 * abs(pdi - mdi) / total

    dx = [float("nan")] * n
    dx[period] = _dx()
    for i in range(period + 1, n):
        tr_s = tr_s - tr_s / period + tr[i]
        p_s = p_s - p_s / period + p_dm[i]
        m_s = m_s - m_s / period + m_dm[i]
        dx[i] = _dx()
    out[period * 2 - 1] = sum(dx[period : period * 2]) / period
    for i in range(period * 2, n):
        out[i] = (out[i - 1] * (period - 1) + dx[i]) / period
    return out


# ─── Trigger detector plumbing ─────────────────────────────────────────────────


def _hit(name: str, score: float, reason: str, fired: bool) -> dict[str, Any]:
    return {
        "name": name,
        "score": round(float(score), 4),
        "reason": reason,
        "fired": bool(fired),
    }


def _flat(name: str, reason: str = "flat") -> dict[str, Any]:
    return {"name": name, "score": 0.0, "reason": reason, "fired": False}


def _pct_move(candles: list[Any], lookback: int) -> float | None:
    """Close-to-close % change over `lookback` bars, or None if too short."""
    if len(candles) < lookback + 1:
        return None
    start = _cv(candles[-lookback - 1], "c")
    end = _cv(candles[-1], "c")
    if start == 0:
        return None
    return (end - start) / start * 100


# ─── Trigger detectors (each returns a hit dict; score 0-10) ───────────────────


def return_zscore(candles: list[Any], sigma: float = 2.0, window: int = 60) -> dict[str, Any]:
    """Current-bar return z-score vs the trailing return distribution."""
    name = "return_zscore"
    if len(candles) < 3:
        return _flat(name)
    rets = []
    for i in range(1, len(candles)):
        pc = _cv(candles[i - 1], "c")
        rets.append((_cv(candles[i], "c") - pc) / pc if pc else 0.0)
    cur = rets[-1]
    prior = rets[:-1][-window:]
    if len(prior) < 2:
        return _flat(name)
    mean = sum(prior) / len(prior)
    std = (sum((v - mean) ** 2 for v in prior) / len(prior)) ** 0.5
    if std == 0:
        return _flat(name)
    z = abs(cur - mean) / std
    fired = z >= sigma
    direction = "up" if cur > mean else "down"
    return _hit(name, min(10.0, z), f"{z:.1f} sigma return {direction}" if fired else "flat", fired)


def volume_zscore(candles: list[Any], sigma: float = 2.0, window: int = 20) -> dict[str, Any]:
    """Current volume z-score vs the trailing `window`-bar distribution."""
    name = "volume_zscore"
    vols = [_cv(c, "v") for c in candles]
    if len(vols) < window + 1:
        return _flat(name)
    hist = vols[-window - 1 : -1]
    cur = vols[-1]
    if sum(1 for v in hist if v == 0) > len(hist) * 0.5:
        return _flat(name, "sparse")
    mean = sum(hist) / len(hist)
    std = (sum((v - mean) ** 2 for v in hist) / len(hist)) ** 0.5
    if std == 0:
        return _flat(name)
    z = abs(cur - mean) / std
    fired = z >= sigma
    return _hit(name, min(10.0, z), f"{z:.1f} sigma volume spike" if fired else "flat", fired)


def range_breakout(candles: list[Any], lookback: int = 20) -> dict[str, Any]:
    """Breakout of the prior `lookback`-bar range high (up) or low (down)."""
    name = "range_breakout"
    if len(candles) < lookback + 2:
        return _flat(name)
    cur = _cv(candles[-1], "c")
    prior = candles[-lookback - 1 : -1]
    hi = max(_cv(c, "h") for c in prior)
    lo = min(_cv(c, "l") for c in prior)
    if hi > 0 and cur > hi:
        return _hit(
            name, min(10.0, (cur - hi) / hi * 100), f"breakout above {lookback}-bar high", True
        )
    if lo > 0 and cur < lo:
        return _hit(
            name, min(10.0, (lo - cur) / lo * 100), f"breakdown below {lookback}-bar low", True
        )
    return _hit(name, 0.0, "inside range", False)


def bollinger_squeeze(
    candles: list[Any],
    length: int = 20,
    std_dev: float = 2.0,
    history: int = 100,
    pct_gate: float = 10.0,
) -> dict[str, Any]:
    """Bollinger bandwidth percentile: fires when current width is in the tightest decile."""
    name = "bollinger_squeeze"
    closes = [_cv(c, "c") for c in candles]
    if len(closes) < length + 1:
        return _flat(name)
    mid = sma(closes, length)
    widths = []
    for i in range(len(closes)):
        if not math.isfinite(mid[i]) or mid[i] == 0:
            continue
        window = closes[max(0, i - length + 1) : i + 1]
        if len(window) < length:
            continue
        sd = (sum((v - mid[i]) ** 2 for v in window) / length) ** 0.5
        widths.append((2 * sd * std_dev) / abs(mid[i]))
    if len(widths) < 2:
        return _flat(name)
    cur = widths[-1]
    hist = widths[-history:]
    percentile = sum(1 for v in hist if v < cur) / len(hist) * 100
    fired = percentile <= pct_gate
    score = min(10.0, 10 * (1 - percentile / 100))
    return _hit(
        name,
        score,
        f"BB squeeze P{percentile:.0f}" if fired else f"BB normal P{percentile:.0f}",
        fired,
    )


def adx_trend(candles: list[Any], period: int = 14, gate: float = 25.0) -> dict[str, Any]:
    """Trend strength via ADX; fires above `gate`."""
    name = "adx_trend"
    if len(candles) < period * 2 + 1:
        return _flat(name)
    last = adx(candles, period)[-1]
    if not math.isfinite(last):
        return _flat(name)
    fired = last >= gate
    return _hit(
        name,
        min(10.0, max(0.0, last / 4)),
        f"ADX {last:.1f} trending" if fired else f"ADX {last:.1f} weak",
        fired,
    )


def momentum_burst(candles: list[Any], lookback: int = 2, pct: float = 8.0) -> dict[str, Any]:
    """Explosive raw % move over the last `lookback` bars (a named bypass lane)."""
    name = "momentum_burst"
    move = _pct_move(candles, lookback)
    if move is None:
        return _flat(name)
    fired = abs(move) >= pct
    score = min(10.0, abs(move) / pct * 5) if pct > 0 else 0.0
    direction = "up" if move > 0 else "down"
    return _hit(
        name, score, f"{move:+.1f}% over {lookback} bars {direction}" if fired else "flat", fired
    )


def sustained_trend(candles: list[Any], lookback: int = 20, pct: float = 12.0) -> dict[str, Any]:
    """Sustained directional move over `lookback` bars (zero-weight surfacing lane).

    Fires on either direction so a steady downtrend surfaces for SHORT research the
    same way an uptrend surfaces for LONG — the bullish-structured weighted triggers
    alone leave down-movers unscored.
    """
    name = "sustained_trend"
    move = _pct_move(candles, lookback)
    if move is None:
        return _flat(name, "insufficient_history")
    fired = abs(move) >= pct
    score = min(10.0, abs(move) / pct * 5) if pct > 0 else 0.0
    direction = "uptrend" if move > 0 else "downtrend"
    return _hit(
        name, score, f"{move:+.1f}% over {lookback} bars ({direction})" if fired else "flat", fired
    )


def volume_buildup(
    candles: list[Any],
    recent: int = 4,
    baseline: int = 20,
    ratio_threshold: float = 2.5,
) -> dict[str, Any]:
    """Notional-volume surge in the last `recent` bars vs the prior `baseline` average."""
    name = "volume_buildup"
    need = recent + baseline
    if len(candles) < need:
        return _flat(name, "insufficient_history")
    recent_ntl = sum(_cv(c, "v") * _cv(c, "c") for c in candles[-recent:])
    base_avg = sum(_cv(c, "v") * _cv(c, "c") for c in candles[-need:-recent]) / baseline
    if base_avg <= 0:
        return _flat(name, "no_baseline")
    ratio = (recent_ntl / recent) / base_avg
    fired = ratio >= ratio_threshold
    score = min(10.0, ratio / ratio_threshold * 5) if ratio_threshold > 0 else 0.0
    reason = (
        f"{ratio:.1f}x prior baseline" if fired else f"{ratio:.1f}x (need {ratio_threshold:.1f}x)"
    )
    return _hit(name, score, reason, fired)


def ema_cross(
    candles: list[Any], fast: int = 8, slow: int = 21, lookback: int = 3
) -> dict[str, Any]:
    """EMA fast/slow cross (either direction) within the last `lookback` bars."""
    name = "ema_cross"
    if len(candles) < slow + lookback + 1:
        return _flat(name, "insufficient_history")
    closes = [_cv(c, "c") for c in candles]
    ef = ema(closes, fast)
    es = ema(closes, slow)
    n = len(closes)
    for i in range(n - 1, max(0, n - 1 - lookback), -1):
        p_f, p_s, c_f, c_s = ef[i - 1], es[i - 1], ef[i], es[i]
        if not all(math.isfinite(x) for x in (p_f, p_s, c_f, c_s)):
            continue
        bars = n - 1 - i
        if p_f <= p_s and c_f > c_s:
            return _hit(
                name, max(4.0, 8 - bars * 2), f"EMA{fast}/{slow} cross up {bars} bars ago", True
            )
        if p_f >= p_s and c_f < c_s:
            return _hit(
                name, max(4.0, 8 - bars * 2), f"EMA{fast}/{slow} cross down {bars} bars ago", True
            )
    return _flat(name, "no recent cross")


def higher_lows(candles: list[Any], window: int = 6, required: int = 4) -> dict[str, Any]:
    """Structure: at least `required` of the last `window` bars printed a higher low."""
    name = "higher_lows"
    if len(candles) < window + 1:
        return _flat(name, "insufficient_history")
    lows = [_cv(c, "l") for c in candles[-(window + 1) :]]
    count = sum(1 for i in range(1, len(lows)) if lows[i] > lows[i - 1])
    fired = count >= required
    score = min(10.0, count / window * 10)
    return _hit(
        name,
        score,
        f"{count}/{window} higher lows" if fired else f"{count}/{window} (need {required})",
        fired,
    )


def momentum_continuation(
    candles: list[Any],
    min_trend_pct: float = 12.0,
    max_pullback_pct: float = 8.0,
    window: int = 12,
) -> dict[str, Any]:
    """Extended uptrend now consolidating (EMA-stacked, orderly pullback from the window high)."""
    name = "momentum_continuation"
    if len(candles) < max(window, 22):
        return _flat(name, "insufficient_history")
    closes = [_cv(c, "c") for c in candles]
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    cur = closes[-1]
    win = closes[-window:]
    base, hi = win[0], max(win)
    if base <= 0 or hi <= 0:
        return _flat(name)
    trend = (cur - base) / base * 100
    pullback = (hi - cur) / hi * 100
    stacked = e9[-1] > e21[-1] and cur > e21[-1]
    fired = stacked and trend >= min_trend_pct and 0 <= pullback <= max_pullback_pct
    score = min(10.0, max(0.0, trend / 3)) if fired else 0.0
    reason = (
        f"+{trend:.1f}% trend, {pullback:.1f}% pullback, EMA-stacked"
        if fired
        else f"trend {trend:+.1f}% / pullback {pullback:.1f}% / stacked={stacked}"
    )
    return _hit(name, score, reason, fired)


def bearish_reversal_candle(
    candles: list[Any],
    wick_body_ratio: float = 2.0,
    context_lookback: int = 6,
    context_pct: float = 4.0,
) -> dict[str, Any]:
    """Shooting-star / bearish-engulfing at the top of an advance (zero-weight surfacing lane)."""
    name = "bearish_reversal_candle"
    if len(candles) < context_lookback + 2:
        return _flat(name, "insufficient_history")
    o, h, low, c = (_cv(candles[-1], k) for k in ("o", "h", "l", "c"))
    po, pc = _cv(candles[-2], "o"), _cv(candles[-2], "c")
    rng = h - low
    if rng <= 0:
        return _flat(name)
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - low
    ctx = _pct_move(candles[:-1], context_lookback)
    advanced = ctx is not None and ctx >= context_pct
    shooting = (
        body > 0
        and upper_wick >= wick_body_ratio * body
        and lower_wick <= body
        and body <= 0.4 * rng
    )
    engulf = pc > po and c < o and o >= pc and c <= po
    if shooting:
        pattern, strength = "shooting_star", min(10.0, upper_wick / body * 2.5)
    elif engulf:
        prior_body = abs(po - pc)
        pattern, strength = (
            "bearish_engulfing",
            (min(10.0, abs(o - c) / prior_body * 5) if prior_body > 0 else 5.0),
        )
    else:
        pattern, strength = None, 0.0
    fired = bool(pattern) and advanced
    return _hit(
        name, strength if fired else 0.0, f"{pattern} after +{ctx:.1f}%" if fired else "flat", fired
    )


def bullish_reversal_candle(
    candles: list[Any],
    wick_body_ratio: float = 2.0,
    context_lookback: int = 6,
    context_pct: float = 4.0,
) -> dict[str, Any]:
    """Hammer / bullish-engulfing at the bottom of a decline (zero-weight surfacing lane)."""
    name = "bullish_reversal_candle"
    if len(candles) < context_lookback + 2:
        return _flat(name, "insufficient_history")
    o, h, low, c = (_cv(candles[-1], k) for k in ("o", "h", "l", "c"))
    po, pc = _cv(candles[-2], "o"), _cv(candles[-2], "c")
    rng = h - low
    if rng <= 0:
        return _flat(name)
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - low
    ctx = _pct_move(candles[:-1], context_lookback)
    declined = ctx is not None and ctx <= -context_pct
    hammer = (
        body > 0
        and lower_wick >= wick_body_ratio * body
        and upper_wick <= body
        and body <= 0.4 * rng
    )
    engulf = pc < po and c > o and o <= pc and c >= po
    if hammer:
        pattern, strength = "hammer", min(10.0, lower_wick / body * 2.5)
    elif engulf:
        prior_body = abs(po - pc)
        pattern, strength = (
            "bullish_engulfing",
            (min(10.0, abs(c - o) / prior_body * 5) if prior_body > 0 else 5.0),
        )
    else:
        pattern, strength = None, 0.0
    fired = bool(pattern) and declined
    return _hit(
        name, strength if fired else 0.0, f"{pattern} after {ctx:.1f}%" if fired else "flat", fired
    )


# ─── Configuration ─────────────────────────────────────────────────────────────

# Weights track marginal edge; net-negative or flat-price structural signals are
# kept at zero and act purely as surfacing lanes (they never enter the denominator).
DEFAULT_WEIGHTS: dict[str, float] = {
    "adx_trend": 0.55,
    "return_zscore": 0.40,
    "range_breakout": 0.30,
    "volume_zscore": 0.25,
    "momentum_burst": 0.20,
    "volume_buildup": 0.15,
    "bollinger_squeeze": 0.10,
    "ema_cross": 0.10,
    "higher_lows": 0.10,
    "momentum_continuation": 0.10,
    # Zero-weight surfacing bypass lanes — surface a candidate without polluting
    # the composite denominator.
    "sustained_trend": 0.0,
    "bullish_reversal_candle": 0.0,
    "bearish_reversal_candle": 0.0,
}

# Detectors whose fire surfaces a candidate past the composite gate but does NOT by
# itself escalate to the LLM (they must still clear the TA CONFIRMED bar).
SURFACING_LANES = {"sustained_trend", "bullish_reversal_candle", "bearish_reversal_candle"}

DEFAULT_THRESHOLDS: dict[str, float] = {
    "return_sigma": 2.0,
    "volume_sigma": 2.0,
    "breakout_lookback": 20,
    "bb_length": 20,
    "bb_std": 2.0,
    "adx_period": 14,
    "burst_lookback": 2,
    "burst_pct": 8.0,
    "sustained_lookback": 20,
    "sustained_pct": 12.0,
    "vol_buildup_recent": 4,
    "vol_buildup_baseline": 20,
    "vol_buildup_ratio": 2.5,
    "ema_fast": 8,
    "ema_slow": 21,
    "ema_cross_lookback": 3,
    "higher_lows_window": 6,
    "higher_lows_required": 4,
    "mc_min_trend_pct": 12.0,
    "mc_max_pullback_pct": 8.0,
    "mc_window": 12,
    "reversal_wick_body_ratio": 2.0,
    "reversal_context_lookback": 6,
    "reversal_context_pct": 4.0,
}


def default_config() -> dict[str, Any]:
    """A fresh, fully-populated funnel configuration."""
    return {
        "weights": dict(DEFAULT_WEIGHTS),
        "thresholds": dict(DEFAULT_THRESHOLDS),
        "min_composite": 30.0,
        "confirmed_threshold": 22.0,
        "weak_threshold": 12.0,
        "trigger_timeframe_priority": ["1d", "4h", "1h"],
        "signal_timeframe_priority": ["4h", "1d", "1h"],
        "enable_burst_bypass": True,
        "enable_whale_bypass": True,
        "enable_surfacing_bypass": True,
    }


def _merge_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Overlay a (possibly partial) user config on the defaults. Idempotent."""
    cfg = default_config()
    if not config:
        return cfg
    for key, val in config.items():
        if key in ("weights", "thresholds") and isinstance(val, dict):
            merged = dict(cfg[key])
            merged.update(val)
            cfg[key] = merged
        else:
            cfg[key] = val
    return cfg


# ─── Composite scoring ─────────────────────────────────────────────────────────


def composite_score(hits: list[dict[str, Any]], weights: dict[str, float]) -> float:
    """Weighted composite of fired triggers, normalized against the sum of ALL weights.

    The denominator is the total weight of every trigger, not just the fired ones,
    so a single max-score trigger cannot alone reach 100 — co-firing triggers score
    proportionally higher. Zero-weight lanes contribute nothing to either the
    numerator or the denominator, so surfacing them never dilutes the score.
    """
    fired = [h for h in hits if h.get("fired")]
    if not fired:
        return 0.0
    total_weight = sum(weights.values()) or 1.0
    weighted_sum = sum(h["score"] * weights.get(h["name"], 0.0) for h in fired)
    raw = (weighted_sum / total_weight) * 10.0
    return max(0.0, min(100.0, raw))


def run_triggers(candles: list[Any], config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run the full trigger library over a single candle series (the cheap Tier 0)."""
    cfg = _merge_config(config)
    t = cfg["thresholds"]
    return [
        return_zscore(candles, t["return_sigma"]),
        volume_zscore(candles, t["volume_sigma"]),
        range_breakout(candles, int(t["breakout_lookback"])),
        bollinger_squeeze(candles, int(t["bb_length"]), t["bb_std"]),
        adx_trend(candles, int(t["adx_period"])),
        momentum_burst(candles, int(t["burst_lookback"]), t["burst_pct"]),
        sustained_trend(candles, int(t["sustained_lookback"]), t["sustained_pct"]),
        volume_buildup(
            candles,
            int(t["vol_buildup_recent"]),
            int(t["vol_buildup_baseline"]),
            t["vol_buildup_ratio"],
        ),
        ema_cross(candles, int(t["ema_fast"]), int(t["ema_slow"]), int(t["ema_cross_lookback"])),
        higher_lows(candles, int(t["higher_lows_window"]), int(t["higher_lows_required"])),
        momentum_continuation(
            candles, t["mc_min_trend_pct"], t["mc_max_pullback_pct"], int(t["mc_window"])
        ),
        bullish_reversal_candle(
            candles,
            t["reversal_wick_body_ratio"],
            int(t["reversal_context_lookback"]),
            t["reversal_context_pct"],
        ),
        bearish_reversal_candle(
            candles,
            t["reversal_wick_body_ratio"],
            int(t["reversal_context_lookback"]),
            t["reversal_context_pct"],
        ),
    ]


# ─── Multi-timeframe TA filter (Tier 1) ────────────────────────────────────────


def _assess_trend(candles: list[Any]) -> str:
    """Bullish / bearish / flat from an EMA8/21 cross and its slope."""
    if len(candles) < 30:
        return "flat"
    closes = [_cv(c, "c") for c in candles]
    e8 = ema(closes, 8)
    e21 = ema(closes, 21)
    i = len(closes) - 1
    a, b = e8[i], e21[i]
    if not (math.isfinite(a) and math.isfinite(b)):
        return "flat"
    prev = e8[max(0, i - 3)]
    cross_up = a > b
    rising = a > prev
    if cross_up and rising:
        return "bullish"
    if (not cross_up) and (not rising):
        return "bearish"
    return "flat"


def _ema_crossed_recently(
    candles: list[Any], fast: int = 8, slow: int = 21, lookback: int = 3
) -> bool:
    if len(candles) < slow + lookback + 1:
        return False
    closes = [_cv(c, "c") for c in candles]
    ef = ema(closes, fast)
    es = ema(closes, slow)
    n = len(closes)
    for i in range(max(1, n - lookback), n):
        p_f, p_s, c_f, c_s = ef[i - 1], es[i - 1], ef[i], es[i]
        if not all(math.isfinite(x) for x in (p_f, p_s, c_f, c_s)):
            continue
        if (p_f <= p_s and c_f > c_s) or (p_f >= p_s and c_f < c_s):
            return True
    return False


def _volume_confirm(candles: list[Any]) -> bool:
    if len(candles) < 21:
        return False
    last = _cv(candles[-1], "v")
    avg = sum(_cv(c, "v") for c in candles[-21:-1]) / 20
    return avg == 0 or last >= avg * 0.8


def _atr_pct(candles: list[Any], period: int = 14) -> float | None:
    if len(candles) < period + 6:
        return None
    last = atr(candles, period)[-1]
    close = _cv(candles[-1], "c")
    if not math.isfinite(last) or close == 0:
        return None
    return last / close * 100


def _last_finite(arr: list[float]) -> float | None:
    for v in reversed(arr):
        if math.isfinite(v):
            return v
    return None


def _pick_timeframe(tf_candles: dict[str, list[Any]], priority: list[str]) -> str | None:
    for key in priority:
        if tf_candles.get(key):
            return key
    for key, val in tf_candles.items():
        if val:
            return key
    return None


def ta_filter(
    tf_candles: dict[str, list[Any]],
    composite: float = 0.0,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Multi-timeframe additive TA validation -> CONFIRMED / WEAK / REJECTED.

    Directional weighting: a clean higher-timeframe uptrend earns the full trend
    bonus, a clean downtrend earns half (a short is tradeable but lower-edge), and a
    conflicting/flat trend earns none.
    """
    cfg = _merge_config(config)
    rejected = {
        "verdict": "REJECTED",
        "score": 0.0,
        "trend_direction": "flat",
        "trend_aligned": False,
        "trends": {},
        "rsi": None,
        "atr_pct": None,
        "adx": None,
        "ema_cross": False,
        "volume_confirm": False,
        "signal_timeframe": None,
        "reason": "insufficient candle data",
    }
    signal_tf = _pick_timeframe(tf_candles, cfg["signal_timeframe_priority"])
    if signal_tf is None:
        return {**rejected, "reason": "no candle data"}
    sc = tf_candles[signal_tf]
    if len(sc) < 30:
        return {**rejected, "signal_timeframe": signal_tf}

    trends = {tf: _assess_trend(c) for tf, c in tf_candles.items() if c}
    higher = [trends.get(tf) for tf in ("4h", "1d") if trends.get(tf)]
    if not higher:
        higher = [trends.get(signal_tf)]
    is_bullish = any(t == "bullish" for t in higher)
    is_bearish = any(t == "bearish" for t in higher)
    if is_bullish and not is_bearish:
        direction = "bullish"
    elif is_bearish and not is_bullish:
        direction = "bearish"
    else:
        direction = "flat"
    trend_aligned = direction in ("bullish", "bearish")

    rsi_v = _last_finite(rsi(sc, 14))
    atr_pct = _atr_pct(sc, 14)
    adx_v = _last_finite(adx(sc, 14))
    ema_x = _ema_crossed_recently(sc)
    vol_ok = _volume_confirm(sc)

    score = 0.0
    reasons: list[str] = []
    if direction == "bullish":
        score += 20
        reasons.append("trend aligned (bullish)")
    elif direction == "bearish":
        score += 10
        reasons.append("trend aligned (bearish)")
    if rsi_v is not None and 30 < rsi_v < 70:
        score += 15
        reasons.append(f"RSI {rsi_v:.0f}")
    if atr_pct is not None and atr_pct >= 0.5:
        score += 15
        reasons.append(f"ATR {atr_pct:.1f}%")
    if adx_v is not None and adx_v >= 25:
        score += 15
        reasons.append(f"ADX {adx_v:.0f}")
    if ema_x:
        score += 10
        reasons.append("EMA cross")
    if vol_ok:
        score += 10
        reasons.append("volume confirmed")
    score += min(15.0, composite / 100 * 15)

    if score >= cfg["confirmed_threshold"]:
        verdict = "CONFIRMED"
    elif score >= cfg["weak_threshold"]:
        verdict = "WEAK"
    else:
        verdict = "REJECTED"

    return {
        "verdict": verdict,
        "score": round(min(100.0, score), 2),
        "trend_direction": direction,
        "trend_aligned": trend_aligned,
        "trends": trends,
        "rsi": round(rsi_v, 2) if rsi_v is not None else None,
        "atr_pct": round(atr_pct, 3) if atr_pct is not None else None,
        "adx": round(adx_v, 2) if adx_v is not None else None,
        "ema_cross": ema_x,
        "volume_confirm": vol_ok,
        "signal_timeframe": signal_tf,
        "reason": ", ".join(reasons) if reasons else "no signals",
    }


# ─── Per-candidate funnel (Tier 0 -> Tier 1 -> Tier 2 gate) ────────────────────


def normalize_ohlcv(ohlcv: Any) -> dict[str, list[Any]]:
    """Accept a {timeframe: [candles]} dict or a bare candle list (treated as '1d')."""
    if isinstance(ohlcv, dict):
        return {k: list(v) for k, v in ohlcv.items() if v}
    if isinstance(ohlcv, list):
        return {"1d": list(ohlcv)}
    return {}


def funnel_candidate(
    candidate: dict[str, Any], config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Run one candidate through all three tiers and return the full decision record."""
    cfg = _merge_config(config)
    cid = (
        candidate.get("candidate_id") or candidate.get("id") or candidate.get("symbol") or "unknown"
    )
    symbol = candidate.get("symbol") or cid
    tf = normalize_ohlcv(candidate.get("ohlcv") or candidate.get("candles") or {})

    result: dict[str, Any] = {
        "candidate_id": cid,
        "symbol": symbol,
        "direction": candidate.get("direction"),
        "trigger_timeframe": None,
        "triggers": [],
        "composite_score": 0.0,
        "surfaced": False,
        "surface_reason": "",
        "bypass_lane": None,
        "ta": None,
        "escalate": False,
        "escalation_reason": "",
        "tier": "dropped",
    }

    trig_key = _pick_timeframe(tf, cfg["trigger_timeframe_priority"])
    if trig_key is None:
        result["surface_reason"] = "no OHLCV data"
        return result
    result["trigger_timeframe"] = trig_key

    hits = run_triggers(tf[trig_key], cfg)
    comp = composite_score(hits, cfg["weights"])
    result["triggers"] = hits
    result["composite_score"] = round(comp, 2)

    fired = {h["name"] for h in hits if h["fired"]}
    burst = cfg["enable_burst_bypass"] and "momentum_burst" in fired
    whale = cfg["enable_whale_bypass"] and bool(candidate.get("whale_signal"))
    surfacing_fired = sorted(n for n in fired if n in SURFACING_LANES)
    surfacing = cfg["enable_surfacing_bypass"] and bool(surfacing_fired)

    if comp >= cfg["min_composite"]:
        result["surfaced"] = True
        result["surface_reason"] = f"composite {comp:.1f} >= {cfg['min_composite']:.0f}"
    elif burst:
        result["surfaced"] = True
        result["surface_reason"] = "momentum_burst bypass"
    elif whale:
        result["surfaced"] = True
        result["surface_reason"] = "whale bypass"
    elif surfacing:
        result["surfaced"] = True
        result["surface_reason"] = f"surfacing bypass ({', '.join(surfacing_fired)})"
    else:
        result["surface_reason"] = f"composite {comp:.1f} < {cfg['min_composite']:.0f}, no bypass"
        return result

    ta = ta_filter(tf, comp, cfg)
    result["ta"] = ta

    # Tier-2 gate: escalate to the paid LLM ONLY on TA CONFIRMED, plus the two
    # named bypass lanes (momentum burst, whale accumulation).
    if burst:
        result["escalate"] = True
        result["bypass_lane"] = "burst"
        result["escalation_reason"] = "momentum_burst bypass to LLM"
    elif whale:
        result["escalate"] = True
        result["bypass_lane"] = "whale"
        result["escalation_reason"] = "whale bypass to LLM"
    elif ta["verdict"] == "CONFIRMED":
        result["escalate"] = True
        result["escalation_reason"] = f"TA CONFIRMED (score {ta['score']:.0f})"
    else:
        result["escalation_reason"] = (
            f"TA {ta['verdict']} (score {ta['score']:.0f}) — not escalated"
        )

    result["tier"] = "escalated" if result["escalate"] else "surfaced"
    return result


def _tier_rank(tier: str) -> int:
    return {"escalated": 0, "surfaced": 1, "dropped": 2}.get(tier, 3)


def funnel_candidates(
    candidates: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a batch of candidates through the funnel and summarize LLM-call savings."""
    cfg = _merge_config(config)
    results = [funnel_candidate(c, cfg) for c in candidates]
    total = len(results)
    escalated = [r for r in results if r["tier"] == "escalated"]
    surfaced = [r for r in results if r["tier"] == "surfaced"]
    dropped = [r for r in results if r["tier"] == "dropped"]
    results.sort(key=lambda r: (_tier_rank(r["tier"]), -r["composite_score"]))
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": {
            "min_composite": cfg["min_composite"],
            "confirmed_threshold": cfg["confirmed_threshold"],
            "weak_threshold": cfg["weak_threshold"],
            "weights": cfg["weights"],
        },
        "summary": {
            "total_candidates": total,
            "dropped": len(dropped),
            "surfaced_ta_only": len(surfaced),
            "escalated_to_llm": len(escalated),
            "llm_call_reduction_pct": round((1 - len(escalated) / total) * 100, 1)
            if total
            else 0.0,
        },
        "escalated_candidate_ids": [r["candidate_id"] for r in escalated],
        "results": results,
    }


# ─── Reporting ─────────────────────────────────────────────────────────────────


def render_markdown(report: dict[str, Any]) -> str:
    """Human-readable funnel dashboard."""
    s = report["summary"]
    lines = [
        "# Signal Funnel Report",
        f"**Generated:** {report['generated_at']}",
        "",
        "## Summary",
        f"- Total candidates: {s['total_candidates']}",
        f"- Dropped (no cost): {s['dropped']}",
        f"- Surfaced, TA only (no LLM): {s['surfaced_ta_only']}",
        f"- Escalated to LLM: {s['escalated_to_llm']}",
        f"- LLM-call reduction: {s['llm_call_reduction_pct']}%",
        "",
        "## Candidates",
        "",
        "| Candidate | Tier | Composite | TA verdict | Bypass | Reason |",
        "|-----------|------|-----------|------------|--------|--------|",
    ]
    for r in report["results"]:
        ta = r.get("ta") or {}
        lines.append(
            f"| {r['candidate_id']} | {r['tier']} | {r['composite_score']:.1f} | "
            f"{ta.get('verdict', '-')} | {r.get('bypass_lane') or '-'} | "
            f"{r.get('escalation_reason') or r.get('surface_reason', '')} |"
        )
    lines.append("")
    if report["escalated_candidate_ids"]:
        lines.append("## Escalated to LLM / debate tier")
        for cid in report["escalated_candidate_ids"]:
            lines.append(f"- {cid}")
    else:
        lines.append("_No candidates cleared the escalation gate._")
    lines.append("")
    return "\n".join(lines)


# ─── CLI ───────────────────────────────────────────────────────────────────────


def _load_candidates(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        if "candidates" in data:
            data = data["candidates"]
        else:
            data = [data]  # a single candidate object
    if not isinstance(data, list):
        raise ValueError(
            "candidates file must be a JSON list or an object with a 'candidates' list"
        )
    return data


def _load_weights_config(path: Path) -> dict[str, Any]:
    text = path.read_text()
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml  # lazy: keep pure-calc paths stdlib-only

        return yaml.safe_load(text) or {}
    return json.loads(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Tiered cost-controlled signal funnel over OHLCV candidates."
    )
    parser.add_argument(
        "--candidates", required=True, help="JSON file: list of candidates with per-timeframe OHLCV"
    )
    parser.add_argument(
        "--config", help="YAML/JSON funnel config (weights, thresholds, gate levels)"
    )
    parser.add_argument("--min-composite", type=float, help="Override the composite surfacing gate")
    parser.add_argument(
        "--confirmed-threshold", type=float, help="Override the TA CONFIRMED score threshold"
    )
    parser.add_argument("--weak-threshold", type=float, help="Override the TA WEAK score threshold")
    parser.add_argument(
        "--output-dir", default="reports", help="Directory for report files (default: reports)"
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the JSON report to stdout instead of writing files",
    )
    args = parser.parse_args(argv)

    candidates_path = Path(args.candidates)
    if not candidates_path.exists():
        print(f"error: candidates file not found: {candidates_path}", file=sys.stderr)
        return 1

    config: dict[str, Any] = {}
    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.exists():
            print(f"error: config file not found: {cfg_path}", file=sys.stderr)
            return 1
        config = _load_weights_config(cfg_path)
    if args.min_composite is not None:
        config["min_composite"] = args.min_composite
    if args.confirmed_threshold is not None:
        config["confirmed_threshold"] = args.confirmed_threshold
    if args.weak_threshold is not None:
        config["weak_threshold"] = args.weak_threshold

    try:
        candidates = _load_candidates(candidates_path)
    except (ValueError, json.JSONDecodeError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    report = funnel_candidates(candidates, config)

    if args.stdout:
        print(json.dumps(report, indent=2))
        return 0

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    json_path = out_dir / f"signal_funnel_{stamp}.json"
    md_path = out_dir / f"signal_funnel_{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2))
    md_path.write_text(render_markdown(report))

    s = report["summary"]
    print(
        f"Funnel: {s['total_candidates']} candidates -> {s['escalated_to_llm']} escalated "
        f"({s['llm_call_reduction_pct']}% LLM-call reduction)"
    )
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
