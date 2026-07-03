"""Systematic coverage-surge catalyst detector for the Market News Analyst.

A market-moving headline often shows up first as a SURGE in how many outlets are
suddenly covering a topic — before the price fully reflects it. This module turns
that intuition into a deterministic, free, no-API-key signal by combining two
public sources:

  1. GDELT 2.0 DOC API (https://api.gdeltproject.org/api/v2/doc/doc) — indexes
     global news every ~15 min, full-text searchable, free, no key. We pull:
       * an ArtList (latest matching articles: headline + domain + timestamp), and
       * a TimelineVol coverage-volume series, from which a SURGE multiplier
         (surge_x = latest bin vs the median of the earlier bins) and a BREAKING
         flag are computed.
  2. Public RSS wires (Yahoo Finance, CNBC) — lowest-latency major headlines,
     keyword-filtered for the ticker/keyword under study.

The output is (a) a `blackout_signal` block a news-blackout risk gate can consume
(stand down / suppress new entries while a ticker is mid-catalyst), and (b) a
markdown context block the Market News Analyst can paste into its prompt.

Design mirrors the rest of the repo: PURE parse/compute functions import with the
standard library only and are unit-tested against saved GDELT/RSS fixtures. The
network calls (fetch_gdelt / fetch_rss) lazily import urllib INSIDE the function,
so nothing here touches the network on import and the tests run fully offline.
Fetches are TTL-cached (news moves fast; short cache) and headlines are filtered
to a freshness window so a stale article never trips the blackout gate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median

_GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"

# Free, no-auth RSS wires. Equity/macro focused. Add/remove freely.
_RSS_FEEDS = [
    "https://finance.yahoo.com/news/rssindex",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
]

# News moves fast — a short cache keeps repeated intraday scans from hammering
# the feeds while still surfacing a developing catalyst within minutes.
_CACHE_TTL_S = 300.0
# Only articles seen within this window count toward the blackout gate; a stale
# headline (e.g. a year-old retrospective) is noise to the gate and the analyst.
_FRESH_WINDOW_HOURS = 48
# Coverage-surge thresholds (latest bin / baseline median).
_BREAKING_X = 2.5
_ELEVATED_X = 1.5

_UTC_MIN = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    domain: str
    seen: datetime | None  # UTC
    source: str = ""  # "gdelt" | RSS feed host


@dataclass
class CatalystReport:
    query: str
    ticker: str | None
    keyword: str | None
    timespan: str
    fresh_window_hours: int
    n_recent: int
    surge_x: float
    breaking: bool
    elevated: bool
    severity: str  # "high" | "elevated" | "none"
    headlines: list = field(default_factory=list)  # list[Article], newest first
    sources: dict = field(default_factory=dict)  # {"gdelt": n, "rss": n}
    note: str = ""
    as_of: str = ""  # ISO UTC of the freshness "now" used


# ── query building (pure) ────────────────────────────────────────────────────


def build_gdelt_query(ticker: str | None = None, keyword: str | None = None) -> str:
    """Build a GDELT DOC query for a ticker and/or keyword.

    A bare ticker is noisy in a global news index (``F`` = Ford but also the
    word "false"), so a ticker-only query is scoped to market vocabulary. An
    explicit keyword/company-name phrase takes precedence and is used verbatim.
    """
    kw = (keyword or "").strip()
    if kw:
        return kw
    t = (ticker or "").strip()
    if not t:
        return ""
    return f'"{t}" (stock OR shares OR earnings OR SEC)'


def query_keywords(ticker: str | None = None, keyword: str | None = None) -> list:
    """Keyword list used to filter the RSS wires down to the topic."""
    return [k.strip() for k in (ticker, keyword) if k and k.strip()]


# ── GDELT parsing (pure) ─────────────────────────────────────────────────────


def parse_gdelt_date(s: str) -> datetime | None:
    """Parse a GDELT seendate ("20260703T143000Z") to an aware UTC datetime."""
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def parse_gdelt_artlist(payload: dict) -> list:
    """Parse a GDELT ArtList payload into Articles, sorted newest first."""
    out: list = []
    for a in (payload or {}).get("articles", []) or []:
        out.append(
            Article(
                title=(a.get("title") or "").strip(),
                url=a.get("url") or "",
                domain=a.get("domain") or "",
                seen=parse_gdelt_date(a.get("seendate") or ""),
                source="gdelt",
            )
        )
    out.sort(key=lambda x: x.seen or _UTC_MIN, reverse=True)
    return out


def parse_gdelt_timeline(payload: dict) -> list:
    """Extract the coverage-volume series (oldest->newest) from a TimelineVol payload."""
    tl = (payload or {}).get("timeline") or []
    if not tl:
        return []
    pts = tl[0].get("data") or []
    return [float(p.get("value") or 0) for p in pts]


def detect_surge(
    volume_points: list,
    breaking_threshold: float = _BREAKING_X,
    min_baseline: float = 1e-9,
) -> tuple:
    """Is the latest coverage bin a SURGE vs its recent baseline?

    Baseline = median of the earlier bins. Returns ``(breaking, surge_x)`` where
    ``breaking`` is True when the latest bin is at least ``breaking_threshold`` x
    the baseline AND nonzero. Needs >=3 points to have a baseline to compare to.
    """
    if len(volume_points) < 3:
        return (False, 1.0)
    latest = volume_points[-1]
    base = median(volume_points[:-1]) or min_baseline
    x = latest / base if base > 0 else 0.0
    return (x >= breaking_threshold and latest > 0, round(x, 2))


def surge_severity(
    surge_x: float,
    breaking: bool,
    elevated_threshold: float = _ELEVATED_X,
) -> str:
    """Map a surge multiplier to a severity label: high | elevated | none."""
    if breaking:
        return "high"
    if surge_x >= elevated_threshold:
        return "elevated"
    return "none"


# ── RSS parsing (pure) ───────────────────────────────────────────────────────


def parse_rss_date(s: str) -> datetime | None:
    """Parse an RSS/Atom date to an aware UTC datetime (tolerant of formats)."""
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
    ):
        try:
            dt = datetime.strptime((s or "").strip(), fmt)
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def parse_rss(xml_text: str, source: str = "") -> list:
    """Parse an RSS/Atom feed into Articles. Tolerant of malformed feeds."""
    out: list = []
    try:
        root = ET.fromstring(xml_text or "")
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
        dom = urllib.parse.urlparse(link).netloc
        if title:
            out.append(
                Article(
                    title=title,
                    url=link,
                    domain=dom,
                    seen=parse_rss_date(pub),
                    source=source or dom,
                )
            )
    return out


def filter_keywords(articles: list, keywords: list) -> list:
    """Keep articles whose title contains ANY keyword (case-insensitive)."""
    if not keywords:
        return list(articles)
    kw = [k.lower() for k in keywords if k]
    if not kw:
        return list(articles)
    return [a for a in articles if any(k in a.title.lower() for k in kw)]


def filter_fresh(articles: list, window_hours: int, now: datetime) -> list:
    """Keep articles seen within ``window_hours`` of ``now``.

    Articles whose date could not be parsed (``seen is None``) are KEPT — we
    cannot prove they are stale, and dropping them risks missing a live catalyst.
    """
    if not window_hours or window_hours <= 0:
        return list(articles)
    cutoff = now - timedelta(hours=window_hours)
    return [a for a in articles if a.seen is None or a.seen >= cutoff]


def _dedup(articles: list) -> list:
    """Drop duplicate articles by URL, then by normalized title, keeping order."""
    seen_url: set = set()
    seen_title: set = set()
    out: list = []
    for a in articles:
        u = (a.url or "").strip().lower()
        t = a.title.strip().lower()
        if u and u in seen_url:
            continue
        if t and t in seen_title:
            continue
        if u:
            seen_url.add(u)
        if t:
            seen_title.add(t)
        out.append(a)
    return out


# ── pure report builder ──────────────────────────────────────────────────────


def build_catalyst_report(
    query: str,
    ticker: str | None,
    keyword: str | None,
    artlist_payload: dict,
    timeline_payload: dict,
    rss_articles: list | None = None,
    timespan: str = "1d",
    fresh_window_hours: int = _FRESH_WINDOW_HOURS,
    breaking_threshold: float = _BREAKING_X,
    elevated_threshold: float = _ELEVATED_X,
    max_headlines: int = 30,
    now: datetime | None = None,
) -> CatalystReport:
    """Combine a GDELT ArtList + TimelineVol (+ keyword-filtered RSS) into a report.

    All inputs are already-parsed payloads/lists, so this is a pure function the
    tests drive directly from fixtures.
    """
    now = now or datetime.now(timezone.utc)

    gdelt_arts = parse_gdelt_artlist(artlist_payload)
    rss = list(rss_articles or [])
    merged = _dedup(gdelt_arts + rss)
    fresh = filter_fresh(merged, fresh_window_hours, now)
    fresh.sort(key=lambda x: x.seen or _UTC_MIN, reverse=True)
    fresh = fresh[:max_headlines]

    breaking, surge_x = detect_surge(
        parse_gdelt_timeline(timeline_payload), breaking_threshold=breaking_threshold
    )
    severity = surge_severity(surge_x, breaking, elevated_threshold=elevated_threshold)
    elevated = severity in ("high", "elevated")

    if breaking:
        note = f"BREAKING — coverage surging {surge_x}x its recent baseline"
    elif elevated:
        note = f"elevated coverage ({surge_x}x baseline)"
    else:
        note = "no coverage surge detected"

    sources = {
        "gdelt": sum(1 for a in fresh if a.source == "gdelt"),
        "rss": sum(1 for a in fresh if a.source != "gdelt"),
    }

    return CatalystReport(
        query=query,
        ticker=ticker,
        keyword=keyword,
        timespan=timespan,
        fresh_window_hours=fresh_window_hours,
        n_recent=len(fresh),
        surge_x=surge_x,
        breaking=breaking,
        elevated=elevated,
        severity=severity,
        headlines=fresh,
        sources=sources,
        note=note,
        as_of=now.isoformat().replace("+00:00", "Z"),
    )


def blackout_signal(rep: CatalystReport) -> dict:
    """Derive the news-blackout risk-gate signal from a catalyst report.

    Consumed as INPUT by the risk-gate-framework skill (documented JSON shape):
    when ``blackout`` is True the gate should stand new entries down for the
    ticker until the catalyst window clears, since price is mid-repricing on a
    developing story rather than trading a clean setup.
    """
    top = rep.headlines[0].title if rep.headlines else ""
    if rep.breaking:
        action = (
            "Suppress new entries for this ticker until the catalyst window clears "
            "(coverage surge indicates active repricing / event risk)."
        )
        reason = (
            f"coverage surge {rep.surge_x}x baseline at/above the {_BREAKING_X}x "
            f"breaking threshold across {rep.n_recent} recent articles"
        )
    elif rep.elevated:
        action = "Tighten sizing / require confirmation; coverage is elevated but not breaking."
        reason = f"coverage {rep.surge_x}x baseline (elevated, below the {_BREAKING_X}x breaking threshold)"
    else:
        action = "No news-blackout constraint; coverage is at baseline."
        reason = f"coverage {rep.surge_x}x baseline (no surge)"

    return {
        "schema_version": "1.0",
        "signal": "news_blackout",
        "ticker": rep.ticker,
        "keyword": rep.keyword,
        "blackout": bool(rep.breaking),
        "severity": rep.severity,
        "surge_x": rep.surge_x,
        "n_recent": rep.n_recent,
        "reason": reason,
        "recommended_action": action,
        "top_headline": top,
        "as_of": rep.as_of,
    }


def _article_to_dict(a: Article) -> dict:
    return {
        "title": a.title,
        "url": a.url,
        "domain": a.domain,
        "seen": a.seen.isoformat().replace("+00:00", "Z") if a.seen else None,
        "source": a.source,
    }


def report_to_dict(rep: CatalystReport) -> dict:
    """Serialize a CatalystReport to a JSON-friendly dict."""
    return {
        "schema_version": "1.0",
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "query": rep.query,
        "ticker": rep.ticker,
        "keyword": rep.keyword,
        "timespan": rep.timespan,
        "fresh_window_hours": rep.fresh_window_hours,
        "coverage": {
            "n_recent": rep.n_recent,
            "surge_x": rep.surge_x,
            "breaking": rep.breaking,
            "elevated": rep.elevated,
            "severity": rep.severity,
            "baseline": "median of the earlier GDELT coverage-volume bins",
            "note": rep.note,
        },
        "blackout_signal": blackout_signal(rep),
        "headlines": [_article_to_dict(a) for a in rep.headlines],
        "sources": rep.sources,
        "caveats": [
            "GDELT indexes coverage VOLUME, not price; a surge flags attention, not direction.",
            "Coverage baseline is the median of earlier timeline bins — a proxy, not a fitted model.",
            "RSS/GDELT timestamps can lag or be missing; unknown-date articles are kept, not dropped.",
            "Free feeds are best-effort and rate-limited; a fetch failure degrades to no signal.",
            "Descriptive, not predictive — a catalyst detector for the analyst, never an auto-trade.",
        ],
    }


def _fmt_seen(a: Article) -> str:
    return a.seen.strftime("%m-%d %H:%MZ") if a.seen else "  undated  "


def generate_markdown_report(rep: CatalystReport) -> str:
    """Render a CatalystReport to a markdown context block for the analyst prompt."""
    d = report_to_dict(rep)
    subject = rep.keyword or rep.ticker or rep.query
    flag = "BREAKING" if rep.breaking else "ELEVATED" if rep.elevated else "quiet"
    black = "BLACKOUT" if rep.breaking else "clear"
    lines = [
        f"# News Catalyst Scan — {subject}",
        f"**Generated:** {d['generated']}",
        f"**Query:** `{rep.query}`",
        f"**Window:** last {rep.fresh_window_hours}h (as of {rep.as_of})",
        f"**Coverage surge:** {rep.surge_x}x baseline — {flag}",
        f"**Articles in window:** {rep.n_recent} "
        f"(GDELT {rep.sources.get('gdelt', 0)} / RSS {rep.sources.get('rss', 0)})",
        f"**News-blackout signal:** {black} (severity: {rep.severity})",
        "",
        "## Latest Headlines (newest first)",
    ]
    if rep.headlines:
        for a in rep.headlines:
            src = a.domain or a.source
            url = f" — {a.url}" if a.url else ""
            lines.append(f"- `[{_fmt_seen(a)}]` {a.title} ({src}){url}")
    else:
        lines.append("- none in the freshness window")
    lines += [
        "",
        "## Risk-Gate Signal (JSON — consumed by risk-gate-framework)",
        "```json",
        json.dumps(d["blackout_signal"], indent=2),
        "```",
        "",
        "## Analyst Context Block",
        f"> Coverage of **{subject}** is {rep.note}. "
        + (
            "Treat any setup on this name as event-driven: confirm the catalyst and "
            "widen risk assumptions before acting."
            if rep.elevated
            else "No unusual news flow detected for this name in the window."
        ),
        "",
        "## Caveats",
    ]
    lines += [f"- {c}" for c in d["caveats"]]
    return "\n".join(lines) + "\n"


# ── thin cached network fetch (lazy urllib import) ───────────────────────────
_cache: dict = {}
_lock = threading.Lock()


def _get_json(url: str, timeout: float = 12.0) -> dict | None:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _get_text(url: str, timeout: float = 12.0) -> str | None:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def fetch_gdelt(
    query: str,
    timespan: str = "1d",
    max_records: int = 30,
    ttl: float = _CACHE_TTL_S,
    allow_fetch: bool = True,
) -> tuple:
    """Fetch (ArtList payload, TimelineVol payload) for a query. TTL-cached.

    ``allow_fetch=False`` = CACHE-ONLY (return the last cached value or ({}, {})
    without any network access).
    """
    key = f"gdelt::{query}::{timespan}::{max_records}"
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < ttl:
            return hit[1]
    if not allow_fetch:
        return hit[1] if hit else ({}, {})

    q = urllib.parse.quote(query)
    art = _get_json(
        f"{_GDELT}?query={q}&mode=ArtList&maxrecords={max_records}"
        f"&format=json&sortby=datedesc&timespan={timespan}"
    )
    vol = _get_json(f"{_GDELT}?query={q}&mode=TimelineVol&format=json&timespan={timespan}")
    result = (art or {}, vol or {})
    with _lock:
        _cache[key] = (now, result)
    return result


def fetch_rss(
    keywords: list | None = None,
    feeds: list | None = None,
    limit: int = 25,
    ttl: float = _CACHE_TTL_S,
) -> list:
    """Fetch keyword-filtered headlines from the public RSS wires. TTL-cached."""
    feeds = feeds or _RSS_FEEDS
    key = "rss::" + ",".join(sorted(feeds)) + "::" + ",".join(sorted(keywords or []))
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < ttl:
            return hit[1]
    arts: list = []
    for f in feeds:
        txt = _get_text(f)
        if txt:
            arts.extend(parse_rss(txt, source=urllib.parse.urlparse(f).netloc))
    if keywords:
        arts = filter_keywords(arts, keywords)
    arts.sort(key=lambda x: x.seen or _UTC_MIN, reverse=True)
    arts = arts[:limit]
    with _lock:
        _cache[key] = (now, arts)
    return arts


# ── CLI ──────────────────────────────────────────────────────────────────────


def _load_json(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Detect news coverage-surge catalysts via the FREE GDELT 2.0 DOC API "
            "plus public RSS wires (no API key). Emits a news-blackout risk-gate "
            "signal and an analyst context block."
        )
    )
    parser.add_argument(
        "--ticker", help="Ticker to scan (e.g. NVDA). Ticker or --keyword required."
    )
    parser.add_argument(
        "--keyword", help="Topic/company phrase (takes precedence over --ticker in the query)."
    )
    parser.add_argument(
        "--timespan", default="1d", help="GDELT lookback (e.g. 1h, 1d, 3d). Default: 1d."
    )
    parser.add_argument(
        "--fresh-hours",
        type=int,
        default=_FRESH_WINDOW_HOURS,
        help=f"Freshness window in hours for kept headlines (default: {_FRESH_WINDOW_HOURS}).",
    )
    parser.add_argument(
        "--max-records", type=int, default=30, help="Max GDELT articles to request (default: 30)."
    )
    parser.add_argument(
        "--breaking-threshold",
        type=float,
        default=_BREAKING_X,
        help=f"Surge multiplier flagged as BREAKING (default: {_BREAKING_X}).",
    )
    parser.add_argument(
        "--elevated-threshold",
        type=float,
        default=_ELEVATED_X,
        help=f"Surge multiplier flagged as elevated (default: {_ELEVATED_X}).",
    )
    parser.add_argument(
        "--no-rss", action="store_true", help="Skip the RSS wire supplement (GDELT only)."
    )
    parser.add_argument(
        "--no-fetch", action="store_true", help="Cache-only: never touch the network."
    )
    parser.add_argument(
        "--now", help="Override 'now' as an ISO-8601 UTC time (for reproducible runs/tests)."
    )
    parser.add_argument(
        "--gdelt-json", help="Path to a saved GDELT ArtList JSON (offline mode; skips network)."
    )
    parser.add_argument(
        "--gdelt-timeline-json", help="Path to a saved GDELT TimelineVol JSON (offline mode)."
    )
    parser.add_argument(
        "--rss-xml",
        action="append",
        default=[],
        help="Path to a saved RSS XML file (offline mode; repeatable).",
    )
    parser.add_argument(
        "--output-dir", default="reports/", help="Output directory for reports (default: reports/)."
    )
    return parser


def _parse_now(s: str | None) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    txt = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        print(
            f"Error: could not parse --now '{s}' (expected ISO-8601, e.g. 2026-07-03T15:00:00Z).",
            file=sys.stderr,
        )
        sys.exit(1)
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not (args.ticker or args.keyword):
        print("Error: provide --ticker and/or --keyword.", file=sys.stderr)
        sys.exit(1)

    now = _parse_now(args.now)
    query = build_gdelt_query(args.ticker, args.keyword)
    keywords = query_keywords(args.ticker, args.keyword)

    offline = bool(args.gdelt_json or args.gdelt_timeline_json or args.rss_xml)
    if offline:
        try:
            artlist = _load_json(args.gdelt_json) if args.gdelt_json else {}
            timeline = _load_json(args.gdelt_timeline_json) if args.gdelt_timeline_json else {}
        except (OSError, json.JSONDecodeError) as e:
            print(f"Error: could not read GDELT JSON: {e}", file=sys.stderr)
            sys.exit(1)
        rss_articles: list = []
        for p in args.rss_xml:
            try:
                with open(p) as f:
                    txt = f.read()
            except OSError as e:
                print(f"Error: could not read RSS XML '{p}': {e}", file=sys.stderr)
                sys.exit(1)
            rss_articles.extend(
                parse_rss(txt, source=urllib.parse.urlparse(p).netloc or os.path.basename(p))
            )
        rss_articles = filter_keywords(rss_articles, keywords)
    else:
        artlist, timeline = fetch_gdelt(
            query,
            timespan=args.timespan,
            max_records=args.max_records,
            allow_fetch=not args.no_fetch,
        )
        rss_articles = [] if args.no_rss else fetch_rss(keywords=keywords)
        if not artlist and not timeline and not rss_articles:
            print(
                "Error: no data from GDELT or RSS. The feeds may be unavailable or "
                "rate-limited. Retry, or pass --gdelt-json/--rss-xml with saved payloads.",
                file=sys.stderr,
            )
            sys.exit(1)

    rep = build_catalyst_report(
        query=query,
        ticker=args.ticker,
        keyword=args.keyword,
        artlist_payload=artlist,
        timeline_payload=timeline,
        rss_articles=rss_articles,
        timespan=args.timespan,
        fresh_window_hours=args.fresh_hours,
        breaking_threshold=args.breaking_threshold,
        elevated_threshold=args.elevated_threshold,
        max_headlines=args.max_records,
        now=now,
    )

    os.makedirs(args.output_dir, exist_ok=True)
    subject = (args.keyword or args.ticker or "topic").strip().replace(" ", "_")
    subject = "".join(c for c in subject if c.isalnum() or c in ("_", "-")) or "topic"
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    base = f"news_catalyst_{subject}_{stamp}"

    json_path = os.path.join(args.output_dir, f"{base}.json")
    with open(json_path, "w") as f:
        json.dump(report_to_dict(rep), f, indent=2)
    print(f"JSON report: {json_path}")

    md_path = os.path.join(args.output_dir, f"{base}.md")
    with open(md_path, "w") as f:
        f.write(generate_markdown_report(rep))
    print(f"Markdown report: {md_path}")

    flag = "BREAKING" if rep.breaking else "elevated" if rep.elevated else "quiet"
    black = "BLACKOUT" if rep.breaking else "clear"
    print(
        f"\n{subject}: coverage {rep.surge_x}x baseline ({flag}) | "
        f"{rep.n_recent} articles in window | news-blackout {black} (severity {rep.severity})"
    )


if __name__ == "__main__":
    main()
