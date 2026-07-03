#!/usr/bin/env python3
"""
Analyst Estimate-Revision Momentum factor for the Earnings Trade Analyzer.

Pulls analyst estimate data (keyless, via Yahoo Finance / yfinance) for a
ticker and turns it into a single signed revision-momentum score (0-100)
that the composite scorer consumes as its 6th factor. The goal is to
penalize post-earnings candidates that are quietly being downgraded by
analysts even when their price/volume factors look strong.

Data sources (all from ``yfinance.Ticker``):
  - earnings_estimate  -> current EPS consensus + high/low spread per period
  - revenue_estimate   -> current revenue consensus + high/low spread per period
  - eps_trend          -> EPS-estimate drift over 7 / 30 / 60 / 90 days
  - eps_revisions      -> up-vs-down revision breadth counts (7d / 30d)
  - growth_estimates   -> company vs industry/sector/index growth (context only)
  - earnings_history   -> estimate-vs-actual calibration (confidence weight)

Design notes:
  - ``fetch_estimate_data`` is the only function that touches the network;
    yfinance is imported lazily inside it so the pure compute functions and
    the offline tests import with the standard library alone.
  - Missing analyst coverage yields a neutral score of 50 (a candidate is
    never penalized for the *absence* of estimate data, only for actual
    downward revisions).

Not financial advice. yfinance is not affiliated with Yahoo, Inc.

Usage:
    python3 estimate_revision.py --ticker AAPL --output-dir reports/
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Near-term first: current quarter, next quarter, current year, next year.
PERIOD_ORDER = ["0q", "+1q", "0y", "+1y"]

# EPS-trend look-back windows: label -> yfinance column name.
TREND_WINDOWS = [
    ("7d", "7daysAgo"),
    ("30d", "30daysAgo"),
    ("60d", "60daysAgo"),
    ("90d", "90daysAgo"),
]

# Score bands for the signed momentum score (0-100, 50 = neutral).
MOMENTUM_BANDS = [
    (75.0, "strong_upgrade"),
    (58.0, "upgrade"),
    (42.0, "neutral"),
    (25.0, "downgrade"),
    (0.0, "strong_downgrade"),
]


# --------------------------------------------------------------------------
# Small pure helpers
# --------------------------------------------------------------------------


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _num(v):
    """Coerce a value (incl. numpy scalars / NaN) to a plain float or None."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    if f != f:  # NaN
        return None
    return f


def _pick_near_term(mapping: dict) -> str | None:
    """Return the most near-term period key present in a period-keyed dict."""
    for p in PERIOD_ORDER:
        if p in mapping:
            return p
    return next(iter(mapping), None)


def _label_for_score(score: float) -> str:
    for threshold, label in MOMENTUM_BANDS:
        if score >= threshold:
            return label
    return "strong_downgrade"


# --------------------------------------------------------------------------
# Pure computation functions (operate on normalized dicts, stdlib only)
# --------------------------------------------------------------------------


def summarize_consensus(estimate: dict) -> dict:
    """Current consensus with high/low spread per period.

    Args:
        estimate: period -> {avg, low, high, numberOfAnalysts, growth, ...}

    Returns:
        period -> {avg, low, high, num_analysts, growth, spread, spread_pct}
    """
    out = {}
    for period, row in (estimate or {}).items():
        avg = row.get("avg")
        low = row.get("low")
        high = row.get("high")
        spread = round(high - low, 4) if (high is not None and low is not None) else None
        spread_pct = (
            round(spread / abs(avg) * 100.0, 2)
            if (spread is not None and avg not in (None, 0))
            else None
        )
        out[period] = {
            "avg": avg,
            "low": low,
            "high": high,
            "num_analysts": row.get("numberOfAnalysts"),
            "growth": row.get("growth"),
            "spread": spread,
            "spread_pct": spread_pct,
        }
    return out


def compute_eps_trend_drift(eps_trend: dict) -> dict:
    """EPS-estimate drift over 7 / 30 / 60 / 90 days, per period.

    Args:
        eps_trend: period -> {current, 7daysAgo, 30daysAgo, 60daysAgo, 90daysAgo}

    Returns:
        period -> {current, drift: {window -> {abs, pct}}}
    """
    out = {}
    for period, row in (eps_trend or {}).items():
        cur = row.get("current")
        drift = {}
        for label, key in TREND_WINDOWS:
            base = row.get(key)
            if cur is None or base is None:
                drift[label] = {"abs": None, "pct": None}
            else:
                abs_ch = round(cur - base, 4)
                pct = round((cur - base) / abs(base) * 100.0, 2) if base != 0 else None
                drift[label] = {"abs": abs_ch, "pct": pct}
        out[period] = {"current": cur, "drift": drift}
    return out


def compute_revision_breadth(eps_revisions: dict) -> dict:
    """Up-vs-down analyst revision breadth counts, per period and aggregate.

    Args:
        eps_revisions: period -> {upLast7days, downLast7days,
                                  upLast30days, downLast30days}

    Returns:
        dict with per-period counts/ratios plus aggregate totals, net30,
        and a breadth_ratio (up30 / (up30 + down30)).
    """
    periods = {}
    tot_up7 = tot_dn7 = tot_up30 = tot_dn30 = 0
    for period, row in (eps_revisions or {}).items():
        up7 = int(row.get("upLast7days") or 0)
        dn7 = int(row.get("downLast7days") or 0)
        up30 = int(row.get("upLast30days") or 0)
        dn30 = int(row.get("downLast30days") or 0)
        tot30 = up30 + dn30
        periods[period] = {
            "up7": up7,
            "down7": dn7,
            "up30": up30,
            "down30": dn30,
            "net30": up30 - dn30,
            "ratio30": round(up30 / tot30, 3) if tot30 else None,
        }
        tot_up7 += up7
        tot_dn7 += dn7
        tot_up30 += up30
        tot_dn30 += dn30
    agg30 = tot_up30 + tot_dn30
    return {
        "periods": periods,
        "total_up7": tot_up7,
        "total_down7": tot_dn7,
        "total_up30": tot_up30,
        "total_down30": tot_dn30,
        "net30": tot_up30 - tot_dn30,
        "breadth_ratio": round(tot_up30 / agg30, 3) if agg30 else None,
    }


def compute_calibration(history: list) -> dict:
    """Historical estimate-vs-actual calibration, used as a confidence weight.

    A well-calibrated track record (directionally consistent beats/misses and
    tight surprise magnitudes) makes the current revision signal more
    trustworthy; an erratic or absent history damps the signal toward neutral.

    Args:
        history: list of {epsEstimate, epsActual, ...} records.

    Returns:
        dict with per-quarter surprises, beat_rate, avg surprise stats, and a
        calibration_score in [0, 1] (0.5 when no history is available).
    """
    quarters = []
    for rec in history or []:
        est = rec.get("epsEstimate")
        act = rec.get("epsActual")
        if est is None or act is None:
            continue
        surprise_pct = (act - est) / abs(est) * 100.0 if est != 0 else 0.0
        quarters.append(
            {
                "estimate": est,
                "actual": act,
                "surprise_pct": round(surprise_pct, 2),
                "beat": act >= est,
            }
        )
    n = len(quarters)
    if n == 0:
        return {
            "quarters": [],
            "n": 0,
            "beat_rate": None,
            "avg_surprise_pct": None,
            "avg_abs_surprise_pct": None,
            "calibration_score": 0.5,
        }
    beats = sum(1 for q in quarters if q["beat"])
    beat_rate = beats / n
    avg_surprise = sum(q["surprise_pct"] for q in quarters) / n
    avg_abs = sum(abs(q["surprise_pct"]) for q in quarters) / n
    consistency = max(beat_rate, 1 - beat_rate)  # 0.5..1.0 directional predictability
    tightness = max(0.0, 1.0 - avg_abs / 15.0)  # surprises within ~15% -> tight
    calibration = round(0.5 * consistency + 0.5 * tightness, 3)
    return {
        "quarters": quarters,
        "n": n,
        "beat_rate": round(beat_rate, 3),
        "avg_surprise_pct": round(avg_surprise, 2),
        "avg_abs_surprise_pct": round(avg_abs, 2),
        "calibration_score": calibration,
    }


def compute_revision_momentum(trend_drift: dict, breadth: dict, calibration: dict) -> dict:
    """Blend drift + breadth into a signed momentum score (0-100, 50 neutral).

    - Drift signal: near-term (0q preferred) EPS drift over 30d and 90d,
      each saturating at +/-1 (a +10% 30d or +20% 90d drift maxes out).
    - Breadth signal: aggregate net 30-day revisions / total, in [-1, 1].
    - Raw signal: 0.6 * drift + 0.4 * breadth, in [-1, 1].
    - Calibration confidence (0.5..1.0) damps the raw signal toward neutral
      when the analyst track record is weak/unknown.
    """
    period = _pick_near_term(trend_drift)
    drift_signal = 0.0
    drift_used = {"period": period, "pct30": None, "pct90": None}
    if period:
        drift = trend_drift[period]["drift"]
        pct30 = drift.get("30d", {}).get("pct")
        pct90 = drift.get("90d", {}).get("pct")
        parts = []
        if pct30 is not None:
            parts.append(_clamp(pct30 / 10.0, -1.0, 1.0))
        if pct90 is not None:
            parts.append(_clamp(pct90 / 20.0, -1.0, 1.0))
        if parts:
            drift_signal = sum(parts) / len(parts)
        drift_used = {"period": period, "pct30": pct30, "pct90": pct90}

    total30 = breadth.get("total_up30", 0) + breadth.get("total_down30", 0)
    breadth_signal = _clamp(breadth.get("net30", 0) / total30, -1.0, 1.0) if total30 else 0.0

    raw = 0.6 * drift_signal + 0.4 * breadth_signal
    cal = calibration.get("calibration_score", 0.5)
    confidence = round(0.5 + 0.5 * cal, 3)  # 0.5..1.0
    adj = raw * confidence
    score = round(_clamp(50.0 + 50.0 * adj, 0.0, 100.0), 1)

    if score > 52:
        direction = "up"
    elif score < 48:
        direction = "down"
    else:
        direction = "flat"

    return {
        "score": score,
        "raw": round(raw, 4),
        "drift_signal": round(drift_signal, 4),
        "breadth_signal": round(breadth_signal, 4),
        "confidence": confidence,
        "direction": direction,
        "label": _label_for_score(score),
        "drift_used": drift_used,
    }


def compute_revision_factor(data: dict) -> dict:
    """Top-level factor: normalized estimate data -> scorer-ready factor dict.

    Args:
        data: output of ``fetch_estimate_data`` (or a saved fixture of same shape).

    Returns:
        dict with a ``score`` field (0-100) for the composite scorer plus the
        full breakdown (consensus, drift, breadth, calibration, momentum).
        Missing coverage yields a neutral score of 50 with a ``warning``.
    """
    eps_est = data.get("earnings_estimate") or {}
    rev_est = data.get("revenue_estimate") or {}
    eps_trend = data.get("eps_trend") or {}
    eps_rev = data.get("eps_revisions") or {}
    history = data.get("earnings_history") or []

    consensus_eps = summarize_consensus(eps_est)
    consensus_rev = summarize_consensus(rev_est)
    trend_drift = compute_eps_trend_drift(eps_trend)
    breadth = compute_revision_breadth(eps_rev)
    calibration = compute_calibration(history)
    momentum = compute_revision_momentum(trend_drift, breadth, calibration)

    has_signal = bool(trend_drift) or bool(breadth["periods"])

    result = {
        "ticker": data.get("ticker"),
        "score": momentum["score"] if has_signal else 50.0,
        "signed_signal": momentum["raw"] if has_signal else 0.0,
        "direction": momentum["direction"] if has_signal else "flat",
        "label": momentum["label"] if has_signal else "neutral",
        "confidence_weight": momentum["confidence"],
        "calibration_score": calibration["calibration_score"],
        "drift_detail": momentum["drift_used"],
        "eps_consensus": consensus_eps,
        "revenue_consensus": consensus_rev,
        "eps_trend_drift": trend_drift,
        "revision_breadth": breadth,
        "calibration": calibration,
    }
    if not has_signal:
        result["warning"] = "No analyst estimate/revision data available; neutral score applied."
    return result


# --------------------------------------------------------------------------
# Network fetch (only place that touches yfinance; imported lazily)
# --------------------------------------------------------------------------


def _safe(fn):
    """Call fn(), returning None on any failure (a frame may be absent)."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 - any yfinance/parse error -> treat as no data
        return None


def fetch_estimate_data(ticker: str) -> dict:
    """Pull all estimate frames for a ticker (keyless via yfinance).

    Normalizes each pandas DataFrame into plain Python dicts so the rest of
    the pipeline (and the tests) never depend on pandas.
    """
    import yfinance as yf  # lazy: keeps pure functions / tests stdlib-only

    t = yf.Ticker(ticker)

    def frame_to_index_dict(df):
        if df is None or getattr(df, "empty", True):
            return {}
        return {str(idx): {str(c): _num(row[c]) for c in df.columns} for idx, row in df.iterrows()}

    def history_records(df):
        if df is None or getattr(df, "empty", True):
            return []
        recs = []
        for idx, row in df.iterrows():
            rec = {str(c): _num(row[c]) for c in df.columns}
            rec["date"] = str(idx.date()) if hasattr(idx, "date") else str(idx)
            recs.append(rec)
        return recs

    return {
        "ticker": ticker.upper(),
        "earnings_estimate": frame_to_index_dict(_safe(lambda: t.earnings_estimate)),
        "revenue_estimate": frame_to_index_dict(_safe(lambda: t.revenue_estimate)),
        "eps_trend": frame_to_index_dict(_safe(lambda: t.eps_trend)),
        "eps_revisions": frame_to_index_dict(_safe(lambda: t.eps_revisions)),
        "growth_estimates": frame_to_index_dict(_safe(lambda: t.growth_estimates)),
        "earnings_history": history_records(_safe(lambda: t.earnings_history)),
    }


# --------------------------------------------------------------------------
# Reporting (pure)
# --------------------------------------------------------------------------


def build_report(factor: dict, generated_at: str) -> tuple[dict, str]:
    """Return (json_obj, markdown_str) for a single-ticker revision factor."""
    ticker = factor.get("ticker", "???")
    json_obj = {
        "schema_version": "1.0",
        "generated_at": generated_at,
        "generator": "earnings-trade-analyzer/estimate-revision",
        "ticker": ticker,
        "factor": factor,
    }

    lines = [
        f"# Estimate-Revision Momentum - {ticker}",
        "",
        f"**Generated:** {generated_at}",
        f"**Revision score:** {factor.get('score')}/100 "
        f"({factor.get('label')}, direction: {factor.get('direction')})",
        f"**Signed signal:** {factor.get('signed_signal')}  "
        f"**Confidence weight:** {factor.get('confidence_weight')}  "
        f"**Calibration:** {factor.get('calibration_score')}",
        "",
    ]
    if factor.get("warning"):
        lines.append(f"> {factor['warning']}")
        lines.append("")

    # EPS consensus + spread
    eps = factor.get("eps_consensus") or {}
    if eps:
        lines.append("## EPS Consensus (avg / low-high spread)")
        lines.append("")
        lines.append("| Period | Consensus | Low | High | Spread % | # Analysts |")
        lines.append("|--------|-----------|-----|------|----------|------------|")
        for period in [p for p in PERIOD_ORDER if p in eps] + [
            p for p in eps if p not in PERIOD_ORDER
        ]:
            r = eps[period]
            lines.append(
                f"| {period} | {r.get('avg')} | {r.get('low')} | {r.get('high')} | "
                f"{r.get('spread_pct')} | {r.get('num_analysts')} |"
            )
        lines.append("")

    # EPS-trend drift
    drift = factor.get("eps_trend_drift") or {}
    if drift:
        lines.append("## EPS-Trend Drift (% change)")
        lines.append("")
        lines.append("| Period | 7d | 30d | 60d | 90d |")
        lines.append("|--------|----|-----|-----|-----|")
        for period in [p for p in PERIOD_ORDER if p in drift] + [
            p for p in drift if p not in PERIOD_ORDER
        ]:
            d = drift[period]["drift"]
            lines.append(
                f"| {period} | {d['7d']['pct']} | {d['30d']['pct']} | "
                f"{d['60d']['pct']} | {d['90d']['pct']} |"
            )
        lines.append("")

    # Revision breadth
    breadth = factor.get("revision_breadth") or {}
    lines.append("## Revision Breadth (30-day)")
    lines.append("")
    lines.append(
        f"- Up: {breadth.get('total_up30')}  Down: {breadth.get('total_down30')}  "
        f"Net: {breadth.get('net30')}  Breadth ratio: {breadth.get('breadth_ratio')}"
    )
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "*Analyst estimates from Yahoo Finance (yfinance). Research/education only, "
        "not financial advice.*"
    )
    lines.append("")
    return json_obj, "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyst estimate-revision momentum factor (keyless, via yfinance)."
    )
    parser.add_argument("--ticker", required=True, help="Ticker symbol, e.g. AAPL")
    parser.add_argument(
        "--output-dir", default="reports/", help="Output directory (default: reports/)"
    )
    args = parser.parse_args()

    ticker = args.ticker.strip().upper()
    try:
        data = fetch_estimate_data(ticker)
    except ImportError:
        print(
            "ERROR: yfinance is required for estimate-revision fetching. "
            "Install it with: pip install yfinance",
            file=sys.stderr,
        )
        return 1
    except Exception as e:  # noqa: BLE001 - surface any fetch failure cleanly
        print(f"ERROR: could not fetch estimate data for {ticker}: {e}", file=sys.stderr)
        return 1

    factor = compute_revision_factor(data)

    generated_at = datetime.now(timezone.utc).isoformat()
    json_obj, md = build_report(factor, generated_at)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    json_path = out_dir / f"estimate_revision_{ticker}_{stamp}.json"
    md_path = out_dir / f"estimate_revision_{ticker}_{stamp}.md"
    json_path.write_text(json.dumps(json_obj, indent=2, default=str))
    md_path.write_text(md)

    print(
        f"{ticker}: revision score {factor['score']}/100 "
        f"({factor['label']}, {factor['direction']})",
        file=sys.stderr,
    )
    print(f"JSON report: {json_path}", file=sys.stderr)
    print(f"Markdown report: {md_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
