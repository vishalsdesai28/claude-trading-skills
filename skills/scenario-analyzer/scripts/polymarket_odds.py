#!/usr/bin/env python3
"""Polymarket forward base rates for the Scenario Analyzer.

Surfaces live, market-implied probabilities for forward-looking macro and
geopolitical events (Fed rate decisions, recession, elections, government
shutdowns, crypto/sector events) so the scenario analysis can anchor its
Base/Bull/Bear probabilities to a *quantified* base rate the crowd is actually
pricing -- a complement to news (what happened) and macro data (where things
stand).

Data source: Polymarket's public Gamma API (https://gamma-api.polymarket.com)
-- FREE, keyless, no auth. Each market's ``outcomePrices`` are the implied
probabilities of its outcomes (a "Yes" at 0.76 means a 76% priced chance).

Usage:
    # Curated default macro/geopolitical topics
    python3 polymarket_odds.py

    # One or more explicit topics
    python3 polymarket_odds.py "Fed rate cut" "US recession 2026"

    # Offline / reproducible run against a saved Gamma search payload
    python3 polymarket_odds.py "Fed rate cut" --fixture path/to/search.json --stdout

Output (written to <repo>/reports by default, override with --output-dir):
    - polymarket_odds_YYYYMMDD.json  (machine-readable base rates)
    - polymarket_odds_YYYYMMDD.md    (markdown for the scenario report)

The JSON is the documented hand-off shape consumed by the scenario analysis:
    {
      "generated_at": "...", "source": "...", "topics": [...],
      "results": [
        {"topic": "Fed rate cut", "available": true, "error": null,
         "markets": [
           {"question": "...", "outcome": "Yes",
            "implied_probability": 0.76, "implied_probability_pct": 76.0,
            "outcomes": [{"label": "Yes", "probability": 0.76}, ...],
            "volume_usd": 5200000.0, "resolves": "2030-12-31",
            "one_week_change_pp": -4.5, "closed": false}
         ]}
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

GAMMA_BASE = "https://gamma-api.polymarket.com"
SEARCH_PATH = "public-search"

# Network timeout (seconds) and default markets returned per topic.
REQUEST_TIMEOUT = 30
DEFAULT_LIMIT = 6

# Curated forward macro/geopolitical topics used when the caller passes none.
DEFAULT_TOPICS = [
    "Fed rate cut",
    "US recession 2026",
    "US government shutdown",
    "US presidential election",
    "Bitcoin price",
]


# --------------------------------------------------------------------------- #
# Pure parsing / ranking helpers (stdlib only -- import without `requests`)    #
# --------------------------------------------------------------------------- #
def parse_json_list(value):
    """Gamma encodes ``outcomes``/``outcomePrices`` as JSON-string arrays.

    Accept an already-decoded list, a JSON-string array, or garbage (-> []).
    """
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_iso(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def implied_probabilities(market):
    """Return ``[{"label", "probability"}, ...]`` from a market's outcomes.

    A probability is the crowd's priced odds for that outcome (0.76 -> 76%).
    """
    outcomes = parse_json_list(market.get("outcomes"))
    prices = parse_json_list(market.get("outcomePrices"))
    result = []
    for i, price in enumerate(prices):
        prob = _to_float(price)
        if prob is None:
            continue
        label = str(outcomes[i]) if i < len(outcomes) else f"Outcome {i + 1}"
        result.append({"label": label, "probability": round(prob, 4)})
    return result


def is_forward_looking(market, now):
    """Keep only open markets that resolve in the future.

    ``closed`` is the reliable resolved flag (``active`` stays True even for
    settled markets); a past ``endDate`` means the event already resolved.
    Either way it is no longer a forward-looking signal.
    """
    if market.get("closed"):
        return False
    end = _parse_iso(market.get("endDate"))
    if end is not None and end < now:
        return False
    return bool(implied_probabilities(market))


def normalize_market(market):
    """Flatten a raw Gamma market into the documented base-rate record."""
    probs = implied_probabilities(market)
    if not probs:
        return None
    top = probs[0]
    wk = _to_float(market.get("oneWeekPriceChange"))
    end_date = market.get("endDate") or ""
    return {
        "question": market.get("question") or market.get("title") or "(untitled market)",
        "outcome": top["label"],
        "implied_probability": top["probability"],
        "implied_probability_pct": round(top["probability"] * 100, 1),
        "outcomes": probs,
        "volume_usd": round(_to_float(market.get("volumeNum")) or 0.0, 2),
        "resolves": end_date[:10],
        "one_week_change_pp": round(wk * 100, 1) if wk is not None else None,
        "closed": bool(market.get("closed")),
    }


def rank_markets(search_payload, now, limit=DEFAULT_LIMIT):
    """Extract forward-looking markets from a Gamma search payload, volume-ranked."""
    raw = [
        m
        for event in (search_payload.get("events") or [])
        for m in (event.get("markets") or [])
        if is_forward_looking(m, now)
    ]
    normalized = [nm for m in raw if (nm := normalize_market(m)) is not None]
    normalized.sort(key=lambda m: m["volume_usd"], reverse=True)
    return normalized[:limit]


# --------------------------------------------------------------------------- #
# Network fetch (lazy `requests` import) + graceful degradation                #
# --------------------------------------------------------------------------- #
def search_gamma(topic, timeout=REQUEST_TIMEOUT):
    """Query the keyless Gamma public-search endpoint for a topic."""
    import requests  # lazy: keep the pure helpers importable without the dep

    resp = requests.get(
        f"{GAMMA_BASE}/{SEARCH_PATH}",
        params={"q": topic, "limit_per_type": 20},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def get_base_rates(
    topic,
    limit=DEFAULT_LIMIT,
    timeout=REQUEST_TIMEOUT,
    now=None,
    search_payload=None,
):
    """Return volume-ranked base rates for one topic; degrades gracefully.

    Pass ``search_payload`` to run fully offline against a saved Gamma response
    (used by tests and ``--fixture``); otherwise the keyless Gamma API is hit.
    A network/parse hiccup returns ``available=False`` rather than raising, so a
    single flaky topic never aborts the scenario run.
    """
    now = now or datetime.now(timezone.utc)
    if search_payload is None:
        try:
            search_payload = search_gamma(topic, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 -- any fetch failure is non-fatal
            return {"topic": topic, "available": False, "error": str(exc), "markets": []}
    markets = rank_markets(search_payload, now, limit=limit)
    return {"topic": topic, "available": True, "error": None, "markets": markets}


def build_report(topics, limit=DEFAULT_LIMIT, timeout=REQUEST_TIMEOUT, now=None, fixture=None):
    """Assemble the full base-rate report across topics."""
    now = now or datetime.now(timezone.utc)
    results = [
        get_base_rates(topic, limit=limit, timeout=timeout, now=now, search_payload=fixture)
        for topic in topics
    ]
    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "source": "Polymarket Gamma API (free, keyless)",
        "topics": list(topics),
        "results": results,
    }


# --------------------------------------------------------------------------- #
# Markdown rendering                                                           #
# --------------------------------------------------------------------------- #
def render_markdown(report):
    lines = [
        "# Polymarket Forward Base Rates",
        "",
        f"**Generated:** {report['generated_at']}",
        f"**Source:** {report['source']}",
        "",
        "> Market-implied probabilities are the crowd's *priced odds* of a forward",
        "> event, not a guaranteed forecast. Higher traded volume means a deeper,",
        "> more reliable market. Feed these as a quantified base rate alongside news",
        "> and macro when allocating Base/Bull/Bear scenario probabilities.",
        "",
    ]
    for res in report["results"]:
        lines.append(f"## {res['topic']}")
        lines.append("")
        if not res["available"]:
            lines.append(
                f"_Polymarket unavailable ({res['error']}). Proceed without a "
                "prediction-market base rate for this topic._"
            )
            lines.append("")
            continue
        if not res["markets"]:
            lines.append(
                "_No open prediction markets matched. Coverage concentrates in "
                "macro, political, geopolitical, and crypto events._"
            )
            lines.append("")
            continue
        lines.append("| Market | Outcome | Implied prob | Volume (USD) | Resolves | 1-wk move |")
        lines.append("|---|---|---:|---:|---|---:|")
        for m in res["markets"]:
            wk = f"{m['one_week_change_pp']:+.1f}pp" if m["one_week_change_pp"] is not None else "—"
            lines.append(
                f"| {m['question']} | {m['outcome']} | {m['implied_probability_pct']:.1f}% "
                f"| ${m['volume_usd']:,.0f} | {m['resolves'] or '—'} | {wk} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def _default_output_dir():
    # skills/scenario-analyzer/scripts/polymarket_odds.py -> parents[3] == repo root
    return Path(__file__).resolve().parents[3] / "reports"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Fetch Polymarket forward base rates (keyless Gamma API) for scenario analysis."
    )
    parser.add_argument(
        "topics",
        nargs="*",
        help="Event keyword(s) to search (default: curated macro/geopolitical set).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Max markets per topic, ranked by volume (default: {DEFAULT_LIMIT}).",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for reports (default: <repo>/reports).",
    )
    parser.add_argument(
        "--format",
        choices=["md", "json", "both"],
        default="both",
        help="Output format(s) to write (default: both).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=REQUEST_TIMEOUT,
        help=f"Network timeout in seconds (default: {REQUEST_TIMEOUT}).",
    )
    parser.add_argument(
        "--fixture",
        help="Path to a saved Gamma search JSON; runs offline, applied to every topic.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print markdown to stdout instead of writing report files.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    topics = args.topics or DEFAULT_TOPICS

    fixture = None
    if args.fixture:
        try:
            fixture = json.loads(Path(args.fixture).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: could not read fixture {args.fixture!r}: {exc}", file=sys.stderr)
            return 1

    report = build_report(topics, limit=args.limit, timeout=args.timeout, fixture=fixture)
    markdown = render_markdown(report)

    if args.stdout:
        print(markdown)
        return 0

    out_dir = Path(args.output_dir) if args.output_dir else _default_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    stem = f"polymarket_odds_{stamp}"

    written = []
    if args.format in ("md", "both"):
        md_path = out_dir / f"{stem}.md"
        md_path.write_text(markdown)
        written.append(md_path)
    if args.format in ("json", "both"):
        json_path = out_dir / f"{stem}.json"
        json_path.write_text(json.dumps(report, indent=2))
        written.append(json_path)

    for path in written:
        print(f"Wrote {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
