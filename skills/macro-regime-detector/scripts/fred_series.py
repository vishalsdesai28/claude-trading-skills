#!/usr/bin/env python3
"""
FRED macro grounding for the Macro Regime Detector.

Fetches macroeconomic time series -- policy rates, Treasury yields, inflation,
labor -- from the St. Louis Fed's free FRED API and turns them into current
"prints + trends" that anchor the detector's named regime states (Concentration,
Broadening, Contraction, Inflationary, Transitional) in real yield-curve and
inflation numbers rather than cross-asset ratios alone.

Two layers, cleanly separated so the pure-calc path imports with stdlib only and
tests run fully offline against saved fixtures:

- Network layer: ``fetch_series_payload`` (lazily imports ``requests``).
- Pure layer: ``resolve_series_id``, ``parse_series``, ``build_macro_grounding``
  (via an injectable ``fetch_fn``), ``anchor_regime``, and the markdown renderers.

A free API key (https://fred.stlouisfed.org/docs/api/api_key.html) is read from
``FRED_API_KEY`` or ``--api-key``. When it is missing the module degrades
gracefully: ``build_macro_grounding`` returns ``{"available": False, ...}`` and
``anchor_regime`` leaves the regime untouched except for an "unavailable" marker,
so a detector run never crashes just because FRED is not configured.

Usage:
    export FRED_API_KEY=YOUR_KEY
    python3 fred_series.py --output-dir reports/
    python3 fred_series.py --series yield_curve cpi core_pce --look-back-days 540
    python3 fred_series.py --api-key YOUR_KEY --as-of 2026-06-30 --format json
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

FRED_API_BASE = "https://api.stlouisfed.org/fred"

# Network timeout (seconds) so a stalled request can't hang a detector run.
REQUEST_TIMEOUT = 30

# Default trailing window. A year captures the trend and the year-over-year base
# for the monthly inflation/labor series that anchor the regime.
DEFAULT_LOOKBACK_DAYS = 365

# Row cap for rendered/serialized observation tables: recent values matter most,
# and a daily series (e.g. the yield-curve spread) over a year would otherwise
# flood the report/context. Only the most-recent MAX_ROWS rows are emitted.
MAX_ROWS = 40

# Curated human-friendly aliases -> FRED series IDs. Anything not listed is used
# verbatim as a raw FRED series ID, so power users are never limited to this set.
MACRO_SERIES = {
    # Policy rate
    "fed_funds_rate": "FEDFUNDS",
    "federal_funds_rate": "FEDFUNDS",
    "fed_funds": "FEDFUNDS",
    # Treasury yields (constant maturity)
    "ust2y": "DGS2",
    "ust10y": "DGS10",
    "ust30y": "DGS30",
    "2y_treasury": "DGS2",
    "10y_treasury": "DGS10",
    "30y_treasury": "DGS30",
    "10y_2y_spread": "T10Y2Y",
    "yield_curve": "T10Y2Y",
    # Inflation
    "cpi": "CPIAUCSL",
    "core_cpi": "CPILFESL",
    "pce": "PCEPI",
    "core_pce": "PCEPILFE",
    "inflation_expectations": "T10YIE",
    # Growth & output
    "real_gdp": "GDPC1",
    "gdp": "GDP",
    "industrial_production": "INDPRO",
    # Labor
    "unemployment": "UNRATE",
    "unemployment_rate": "UNRATE",
    "nonfarm_payrolls": "PAYEMS",
    "payrolls": "PAYEMS",
    "initial_claims": "ICSA",
    # Money & markets
    "m2": "M2SL",
    "money_supply": "M2SL",
    "vix": "VIXCLS",
    "dollar_index": "DTWEXBGS",
}

# Aliases whose FRED series is a percentage/rate level (or spread) rather than an
# index. For these the meaningful trend is the absolute change in the same units
# (percentage points); percent-of-base change is unstable when the base is small
# or crosses zero (e.g. the 10Y-2Y spread), so it is not used for their headline.
_LEVEL_SERIES_IDS = {
    "FEDFUNDS",
    "DGS2",
    "DGS10",
    "DGS30",
    "T10Y2Y",
    "T10YIE",
    "UNRATE",
    "VIXCLS",
}

# Default series pulled for regime grounding: the yield curve, headline & core
# inflation, the policy rate, and unemployment -- the numbers that name the
# Contraction / Inflationary / Broadening states.
GROUNDING_SERIES = [
    "yield_curve",
    "cpi",
    "core_pce",
    "fed_funds_rate",
    "unemployment",
    "ust2y",
    "ust10y",
]


class FredNotConfiguredError(ValueError):
    """Raised when FRED is selected but no API key is configured.

    Subclasses ``ValueError`` so callers that already degrade on a missing FMP
    key (the detector's ``except ValueError``) treat FRED the same way.
    """


def get_api_key(api_key=None):
    """Return the FRED API key from the argument or ``FRED_API_KEY`` env var."""
    key = api_key or os.getenv("FRED_API_KEY")
    if not key:
        raise FredNotConfiguredError(
            "FRED_API_KEY is not set. Get a free key at "
            "https://fred.stlouisfed.org/docs/api/api_key.html, then export it "
            "or pass --api-key."
        )
    return key


def resolve_series_id(indicator):
    """Map a friendly alias to a FRED series ID, or pass a raw ID through.

    Raises ``ValueError`` when the input is neither a known alias nor a plausible
    FRED series ID (short, no whitespace), so a descriptive phrase is rejected up
    front rather than 400ing the API.
    """
    key = indicator.strip().lower().replace(" ", "_").replace("-", "_")
    if key in MACRO_SERIES:
        return MACRO_SERIES[key]
    candidate = indicator.strip().upper()
    if not candidate or len(candidate) > 30 or any(c.isspace() for c in candidate):
        raise ValueError(
            f"'{indicator}' is not a known macro alias or a valid FRED series ID. "
            f"Use an alias (e.g. 'yield_curve', 'cpi', 'fed_funds_rate') or a raw "
            f"FRED series ID (e.g. 'CPIAUCSL')."
        )
    return candidate


# --------------------------------------------------------------------------- #
# Network layer (lazily imports requests; never touched by offline tests)
# --------------------------------------------------------------------------- #


def _http_get_json(url, params):
    """GET a FRED endpoint, surfacing FRED's JSON error body on a bad request."""
    import requests  # lazy: keeps module import stdlib-only for offline tests

    response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    if response.status_code == 400:
        try:
            message = response.json().get("error_message", response.text)
        except ValueError:
            message = response.text
        raise ValueError(f"FRED request failed: {message}")
    response.raise_for_status()
    return response.json()


def fetch_series_payload(series_id, start_date, end_date, api_key=None):
    """Fetch a series' metadata + observations from FRED.

    Returns ``{"meta": {...}, "observations": [{"date","value"}, ...]}`` -- the
    shape ``parse_series`` consumes and the shape the saved test fixtures use.
    Raises ``FredNotConfiguredError`` when no API key is available.
    """
    key = get_api_key(api_key)
    base = {"api_key": key, "file_type": "json"}

    meta_resp = _http_get_json(f"{FRED_API_BASE}/series", {**base, "series_id": series_id})
    meta_list = meta_resp.get("seriess") or []

    obs_resp = _http_get_json(
        f"{FRED_API_BASE}/series/observations",
        {
            **base,
            "series_id": series_id,
            "observation_start": start_date,
            "observation_end": end_date,
            "sort_order": "asc",
        },
    )
    return {
        "meta": meta_list[0] if meta_list else {},
        "observations": obs_resp.get("observations", []),
    }


# --------------------------------------------------------------------------- #
# Pure layer (stdlib only)
# --------------------------------------------------------------------------- #


def _clean_points(observations):
    """Extract ``[(date, float_value), ...]`` (ascending), dropping FRED's "."."""
    points = []
    for obs in observations or []:
        value = obs.get("value")
        if value in (".", None, ""):
            continue
        try:
            points.append((obs["date"], float(value)))
        except (ValueError, TypeError, KeyError):
            continue
    return points


def parse_series(series_id, payload, max_rows=MAX_ROWS):
    """Turn a raw FRED payload into a structured series summary (pure).

    Computes the latest print, the change over the window (absolute, and percent
    when the base is a positive index), a coarse rising/falling/flat trend, and a
    row-capped observation table so daily series don't flood the report.
    """
    meta = payload.get("meta") or {}
    points = _clean_points(payload.get("observations"))
    is_level = series_id.upper() in _LEVEL_SERIES_IDS

    summary = {
        "series_id": series_id,
        "title": meta.get("title", series_id),
        "units": meta.get("units_short") or meta.get("units", ""),
        "frequency": meta.get("frequency", ""),
        "seasonal_adjustment": meta.get("seasonal_adjustment_short", ""),
        "is_level": is_level,
        "observations": len(points),
        "latest": None,
        "latest_date": None,
        "first": None,
        "first_date": None,
        "change_abs": None,
        "change_pct": None,
        "trend": "unknown",
        "rows_truncated": False,
        "rows": [],
    }

    if not points:
        return summary

    first_date, first_val = points[0]
    latest_date, latest_val = points[-1]
    change_abs = round(latest_val - first_val, 4)

    # Percent change is meaningful only for a positive index base (inflation,
    # payrolls, M2); for rate/spread levels it is left None -- the absolute
    # change in percentage points is the interpretable number there.
    change_pct = None
    if not is_level and first_val > 0:
        change_pct = round((latest_val / first_val - 1.0) * 100.0, 2)

    if abs(change_abs) < 1e-9:
        trend = "flat"
    elif change_abs > 0:
        trend = "rising"
    else:
        trend = "falling"

    shown = points[-max_rows:] if len(points) > max_rows else points

    summary.update(
        {
            "latest": round(latest_val, 4),
            "latest_date": latest_date,
            "first": round(first_val, 4),
            "first_date": first_date,
            "change_abs": change_abs,
            "change_pct": change_pct,
            "trend": trend,
            "rows_truncated": len(points) > max_rows,
            "rows": [{"date": d, "value": v} for d, v in shown],
        }
    )
    return summary


def get_macro_series(
    indicator,
    curr_date,
    look_back_days=None,
    api_key=None,
    max_rows=MAX_ROWS,
    fetch_fn=None,
):
    """Fetch and summarize a single macro series (alias or raw FRED ID).

    ``fetch_fn`` is injectable so tests can supply saved fixtures without any
    network access; it defaults to ``fetch_series_payload`` resolved at call time
    (so a module-level monkeypatch takes effect). ``curr_date`` (yyyy-mm-dd) is
    the end of the trailing window.
    """
    if fetch_fn is None:
        fetch_fn = fetch_series_payload
    if look_back_days is None:
        look_back_days = DEFAULT_LOOKBACK_DAYS
    series_id = resolve_series_id(indicator)
    end_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_date = (end_dt - timedelta(days=look_back_days)).strftime("%Y-%m-%d")
    payload = fetch_fn(series_id, start_date, curr_date, api_key)
    return parse_series(series_id, payload, max_rows=max_rows)


def build_macro_grounding(
    curr_date=None,
    indicators=None,
    look_back_days=None,
    api_key=None,
    max_rows=MAX_ROWS,
    fetch_fn=None,
):
    """Fetch the curated grounding set and return current prints + trends.

    Returns ``{"available": True, "series": {alias: summary, ...}, ...}`` on
    success, or ``{"available": False, "reason": ...}`` when FRED is not
    configured -- graceful degradation so a detector run continues without it.
    A per-series fetch/parse failure is recorded under ``errors`` and skipped
    rather than aborting the whole grounding.
    """
    if curr_date is None:
        curr_date = datetime.now().strftime("%Y-%m-%d")
    if indicators is None:
        indicators = list(GROUNDING_SERIES)
    if look_back_days is None:
        look_back_days = DEFAULT_LOOKBACK_DAYS

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Check configuration once so a missing key degrades cleanly instead of
    # raising per series.
    try:
        get_api_key(api_key)
    except FredNotConfiguredError as exc:
        return {
            "available": False,
            "reason": str(exc),
            "curr_date": curr_date,
            "generated_at": generated_at,
            "series": {},
            "errors": {},
        }

    series = {}
    errors = {}
    for indicator in indicators:
        try:
            series[indicator] = get_macro_series(
                indicator,
                curr_date,
                look_back_days=look_back_days,
                api_key=api_key,
                max_rows=max_rows,
                fetch_fn=fetch_fn,
            )
        except FredNotConfiguredError as exc:
            # Key vanished mid-run: degrade to unavailable rather than partial.
            return {
                "available": False,
                "reason": str(exc),
                "curr_date": curr_date,
                "generated_at": generated_at,
                "series": {},
                "errors": {},
            }
        except Exception as exc:  # noqa: BLE001 - one bad series shouldn't abort
            errors[indicator] = str(exc)

    return {
        "available": True,
        "curr_date": curr_date,
        "look_back_days": look_back_days,
        "generated_at": generated_at,
        "series": series,
        "errors": errors,
    }


# --------------------------------------------------------------------------- #
# Regime anchoring (pure) -- feeds real prints into the classification output
# --------------------------------------------------------------------------- #

# Inflation above this YoY % is treated as "elevated" for regime consistency.
_ELEVATED_INFLATION_PCT = 3.0

# Keys surfaced as headline prints when anchoring a regime.
_ANCHOR_KEYS = ("yield_curve", "cpi", "core_pce", "fed_funds_rate", "unemployment")


def _fmt_print(key, summary):
    """One human-readable print line for a grounding series."""
    latest = summary.get("latest")
    if latest is None:
        return None
    trend = summary.get("trend", "unknown")
    if key in ("cpi", "core_pce"):
        pct = summary.get("change_pct")
        label = "CPI" if key == "cpi" else "core PCE"
        if pct is not None:
            return f"{label} {pct:+.1f}% YoY"
        return f"{label} index {latest} ({trend})"
    if key == "yield_curve":
        return f"10Y-2Y spread {latest:+.2f}pp ({trend})"
    if key == "fed_funds_rate":
        return f"Fed funds {latest:.2f}% ({trend})"
    if key == "unemployment":
        return f"Unemployment {latest:.1f}% ({trend})"
    return f"{summary.get('title', key)} {latest} ({trend})"


def _macro_consistency_notes(regime_name, series):
    """Cross-check the named regime against the real macro prints.

    Returns short strings that either support or flag the classification, so the
    named state is defensible against actual yield-curve/inflation numbers.
    """
    notes = []
    yc = series.get("yield_curve") or {}
    cpi = series.get("cpi") or {}
    core = series.get("core_pce") or {}
    unemp = series.get("unemployment") or {}
    ff = series.get("fed_funds_rate") or {}

    yc_latest = yc.get("latest")
    if yc_latest is not None:
        if yc_latest < 0:
            note = f"Yield curve inverted ({yc_latest:+.2f}pp) -- classic late-cycle / recession-risk signal"
            if regime_name in ("contraction", "inflationary"):
                note += "; consistent with the classified regime"
            elif regime_name == "broadening":
                note += "; tension with a Broadening call"
            notes.append(note)
        elif yc.get("trend") == "rising":
            notes.append(
                f"Yield curve steepening ({yc_latest:+.2f}pp, rising) -- normalization / early-cycle signal"
            )

    infl_pct = core.get("change_pct")
    infl_label = "core PCE"
    if infl_pct is None:
        infl_pct = cpi.get("change_pct")
        infl_label = "CPI"
    if infl_pct is not None:
        if infl_pct >= _ELEVATED_INFLATION_PCT:
            note = f"Inflation elevated ({infl_label} {infl_pct:+.1f}% YoY)"
            if regime_name == "inflationary":
                note += "; supports the Inflationary regime"
            notes.append(note)
        else:
            notes.append(f"Inflation contained ({infl_label} {infl_pct:+.1f}% YoY)")

    if unemp.get("latest") is not None and unemp.get("trend") == "rising":
        note = f"Unemployment rising ({unemp['latest']:.1f}%) -- softening labor market"
        if regime_name == "contraction":
            note += "; supports the Contraction regime"
        notes.append(note)

    if ff.get("latest") is not None and ff.get("trend") == "falling":
        note = f"Policy rate easing (Fed funds {ff['latest']:.2f}%)"
        if regime_name == "broadening":
            note += "; tailwind for a Broadening rotation"
        notes.append(note)

    return notes


def anchor_regime(regime, grounding):
    """Return a copy of ``regime`` augmented with real FRED prints + trends.

    Never mutates the input. Attaches a ``macro_grounding`` block with the
    headline prints, a one-line summary, and consistency notes that tie the named
    regime to the actual yield-curve/inflation numbers. When grounding is
    unavailable the block just records that, leaving the regime otherwise intact.
    """
    anchored = dict(regime) if regime else {}

    if not grounding or not grounding.get("available"):
        anchored["macro_grounding"] = {
            "available": False,
            "reason": (grounding or {}).get("reason", "FRED grounding unavailable"),
        }
        return anchored

    series = grounding.get("series", {})
    prints = {}
    lines = []
    for key in _ANCHOR_KEYS:
        summary = series.get(key)
        if not summary or summary.get("latest") is None:
            continue
        prints[key] = {
            "latest": summary.get("latest"),
            "latest_date": summary.get("latest_date"),
            "units": summary.get("units"),
            "change_abs": summary.get("change_abs"),
            "change_pct": summary.get("change_pct"),
            "trend": summary.get("trend"),
        }
        line = _fmt_print(key, summary)
        if line:
            lines.append(line)

    anchored["macro_grounding"] = {
        "available": True,
        "curr_date": grounding.get("curr_date"),
        "prints": prints,
        "summary": "; ".join(lines) if lines else "No macro prints available.",
        "consistency_notes": _macro_consistency_notes(
            regime.get("current_regime") if regime else None, series
        ),
    }
    return anchored


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render_series_markdown(summary):
    """Render one series summary as a markdown block (pure)."""
    sid = summary.get("series_id", "")
    title = summary.get("title", sid)
    units = summary.get("units", "")
    freq = summary.get("frequency", "")
    seasonal = summary.get("seasonal_adjustment", "")
    header = (
        f"### {title} ({sid})\n"
        f"- Units: {units}\n"
        f"- Frequency: {freq}{f' ({seasonal})' if seasonal else ''}\n"
    )
    if summary.get("latest") is None:
        return header + "\n_No observations in window._\n"

    change_abs = summary.get("change_abs")
    change_pct = summary.get("change_pct")
    pct_txt = f" ({change_pct:+.2f}%)" if change_pct is not None else ""
    summary_line = (
        f"\n**Latest:** {summary['latest']} ({summary['latest_date']}) | "
        f"**Trend:** {summary.get('trend')} | "
        f"**Change over window:** {change_abs:+.4f}{pct_txt} "
        f"from {summary.get('first')} ({summary.get('first_date')})\n"
    )
    note = ""
    rows = summary.get("rows", [])
    if summary.get("rows_truncated"):
        note = (
            f"\n_(showing the most recent {len(rows)} of "
            f"{summary.get('observations')} observations)_\n"
        )
    table = (
        "\n| Date | Value |\n| --- | --- |\n"
        + "\n".join(f"| {r['date']} | {r['value']} |" for r in rows)
        + "\n"
    )
    return header + summary_line + note + table


def render_grounding_markdown(grounding):
    """Render a full grounding report as markdown (pure)."""
    lines = ["# FRED Macro Grounding", ""]
    lines.append(f"**Generated:** {grounding.get('generated_at', 'N/A')}")
    lines.append(f"**As of:** {grounding.get('curr_date', 'N/A')}")
    lines.append(f"**Window:** trailing {grounding.get('look_back_days', 'N/A')} days")
    lines.append("**Data Source:** FRED (Federal Reserve Economic Data)")
    lines.append("")

    if not grounding.get("available"):
        lines.append(f"> FRED grounding unavailable: {grounding.get('reason', 'unknown')}")
        lines.append("")
        return "\n".join(lines)

    series = grounding.get("series", {})
    lines.append("## Current Prints")
    lines.append("")
    lines.append("| Indicator | Series | Latest | Date | Trend | Change (window) |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for alias, summary in series.items():
        latest = summary.get("latest")
        if latest is None:
            lines.append(f"| {alias} | {summary.get('series_id')} | n/a | -- | -- | -- |")
            continue
        change_abs = summary.get("change_abs")
        change_pct = summary.get("change_pct")
        change_txt = f"{change_abs:+.4f}"
        if change_pct is not None:
            change_txt += f" ({change_pct:+.2f}%)"
        lines.append(
            f"| {alias} | {summary.get('series_id')} | {latest} | "
            f"{summary.get('latest_date')} | {summary.get('trend')} | {change_txt} |"
        )
    lines.append("")

    errors = grounding.get("errors", {})
    if errors:
        lines.append("## Fetch Errors")
        lines.append("")
        for alias, msg in errors.items():
            lines.append(f"- **{alias}**: {msg}")
        lines.append("")

    lines.append("## Series Detail")
    lines.append("")
    for summary in series.values():
        lines.append(render_series_markdown(summary))
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="FRED macro grounding for the Macro Regime Detector"
    )
    parser.add_argument(
        "--series",
        nargs="+",
        default=list(GROUNDING_SERIES),
        help="Aliases or raw FRED series IDs to fetch (default: grounding set)",
    )
    parser.add_argument(
        "--api-key", help="FRED API key (defaults to FRED_API_KEY environment variable)"
    )
    parser.add_argument(
        "--as-of",
        default=datetime.now().strftime("%Y-%m-%d"),
        help="End of the trailing window, yyyy-mm-dd (default: today)",
    )
    parser.add_argument(
        "--look-back-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"Trailing window length in days (default: {DEFAULT_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=MAX_ROWS,
        help=f"Max observation rows per series (default: {MAX_ROWS})",
    )
    parser.add_argument(
        "--output-dir",
        default="reports",
        help="Output directory for reports (default: reports/)",
    )
    parser.add_argument(
        "--format",
        choices=["md", "json", "both"],
        default="both",
        help="Output format (default: both)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_arguments(argv)

    grounding = build_macro_grounding(
        curr_date=args.as_of,
        indicators=args.series,
        look_back_days=args.look_back_days,
        api_key=args.api_key,
        max_rows=args.max_rows,
    )

    if not grounding.get("available"):
        print(f"ERROR: {grounding.get('reason')}", file=sys.stderr)
        return 1

    os.makedirs(args.output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    written = []

    if args.format in ("json", "both"):
        json_file = os.path.join(args.output_dir, f"fred_macro_{timestamp}.json")
        with open(json_file, "w", encoding="utf-8") as handle:
            json.dump(grounding, handle, indent=2, default=str)
        written.append(json_file)

    if args.format in ("md", "both"):
        md_file = os.path.join(args.output_dir, f"fred_macro_{timestamp}.md")
        with open(md_file, "w", encoding="utf-8") as handle:
            handle.write(render_grounding_markdown(grounding))
        written.append(md_file)

    print("FRED macro grounding complete.")
    for alias, summary in grounding.get("series", {}).items():
        line = _fmt_print(alias, summary) or f"{alias}: {summary.get('latest')}"
        print(f"  {line}")
    for path in written:
        print(f"  Report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
