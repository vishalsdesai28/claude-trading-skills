#!/usr/bin/env python3
"""retail-sentiment-ingestor: keyless real-time retail sentiment -> social vault.

Fetches StockTwits cashtag messages (with their user-labeled Bullish/Bearish
tags) and Reddit posts (r/wallstreetbets, r/stocks, r/investing), scores each
ticker deterministically -- sentiment band + 0-10 score + confidence, a
StockTwits message-count base rate, and engagement-weighted Reddit -- detects
cross-source divergence, and fires a contrarian over-extension flag when the
StockTwits bullish/bearish ratio is >= 90/10. An OPTIONAL X path (env
X_API_KEY) adds impression-ranked cashtag sweeps and historical event-window
search.

Design: data-in-prompt / no model tool-calling. The fetch layer pulls
already-structured data (StockTwits carries explicit sentiment labels;
Reddit/X carry numeric engagement), and PURE scoring functions consume it --
the model never free-forms over raw feeds, so nothing is fabricated. Output is
written into the SAME vault schema social-signal-ingestor uses
(data/<agent>/vault/current/{sources,signals}), so build_signal_index.py and
edge-social-aggregator consume it with no change.

Network libs are imported lazily inside the fetch_* functions; the pure
scoring / parsing / note-writing functions import with only the stdlib, so the
test suite exercises them against saved fixtures without touching the network.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

USER_AGENT = "retail-sentiment-ingestor/1.0 (+https://github.com/tradermonty/claude-trading-skills)"

STOCKTWITS_STREAM = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
STOCKTWITS_TRENDING = "https://api.stocktwits.com/api/2/trending/symbols.json"
REDDIT_JSON = "https://www.reddit.com/r/{sub}/search.json?{qs}"
REDDIT_RSS = "https://www.reddit.com/r/{sub}/search.rss?{qs}"
X_API_BASE = "https://api.x.com/2"

DEFAULT_SUBREDDITS = ("wallstreetbets", "stocks", "investing")
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# StockTwits Beta(2,2)-style prior: small labeled samples regress toward the
# 0.5 base rate instead of screaming 0/10 or 10/10 off two messages.
ST_PRIOR = 2.0
# The contrarian over-extension flag: a >=90/10 lean on a real sample size.
OVEREXT_MIN_LABELED = 10
OVEREXT_RATIO = 0.90

# Directional-post pseudo-counts: few directional posts pull the engagement-
# weighted fraction toward neutral (one bullish post != a 10/10 read).
RD_SHRINK_K = 3.0
X_SHRINK_K = 3.0

# How many data points each source needs before it is fully trusted in the
# blend, and its base weight (StockTwits carries explicit labels -> most
# reliable retail read; Reddit next; X last).
ST_FULL_SAMPLE = 20.0
RD_FULL_SAMPLE = 8.0
X_FULL_SAMPLE = 8.0
SOURCE_BASE_WEIGHT = {"stocktwits": 1.0, "reddit": 0.8, "x": 0.7}

_WORD_RE = re.compile(r"[a-z]+")
_TICKER_RE = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")

# Deterministic directional lexicons. A crude, transparent keyword signal for
# platforms that (unlike StockTwits) carry no explicit Bullish/Bearish tag.
# Kept narrow to clearly directional slang; ambiguous words ("hold", "green",
# "support") are deliberately excluded to reduce noise.
BULL_TERMS = frozenset(
    {
        "buy",
        "buying",
        "bought",
        "long",
        "longs",
        "calls",
        "moon",
        "mooning",
        "bullish",
        "bull",
        "breakout",
        "rocket",
        "undervalued",
        "accumulate",
        "accumulating",
        "squeeze",
        "rally",
        "upside",
        "beat",
        "beats",
        "pump",
        "hodl",
        "ath",
    }
)
BEAR_TERMS = frozenset(
    {
        "sell",
        "selling",
        "sold",
        "short",
        "shorting",
        "shorts",
        "puts",
        "bearish",
        "bear",
        "crash",
        "dump",
        "dumping",
        "overvalued",
        "bagholder",
        "tank",
        "tanking",
        "weak",
        "miss",
        "misses",
        "fade",
        "rug",
        "rekt",
        "downtrend",
    }
)


# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #


def resolve_paths(agent: str, data_dir: str | None) -> dict[str, Path]:
    """data/<agent>/{raw, vault/current, state}. Repo-root-relative by default,
    matching social-signal-ingestor so both write into one shared vault."""
    root = Path(data_dir).expanduser() if data_dir else Path(__file__).resolve().parents[3] / "data"
    base = root / agent
    return {
        "raw": base / "raw",
        "vault_current": base / "vault" / "current",
        "state": base / "state",
    }


def today_and_week(now: dt.datetime | None = None) -> tuple[str, str]:
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y-%m-%d"), now.strftime("%G-W%V")


def normalize_ticker(raw: Any) -> str | None:
    """Uppercase, strip a leading '$', and reject multi-symbol / junk strings."""
    if not isinstance(raw, str):
        return None
    t = raw.strip().lstrip("$").upper()
    if not t or "/" in t or "," in t:
        return None
    return t if _TICKER_RE.match(t) else None


# --------------------------------------------------------------------------- #
# Pure text sentiment
# --------------------------------------------------------------------------- #


def polarity_from_text(text: str) -> int:
    """Directional keyword score for one post: distinct bull terms - bear terms.
    >0 bullish, <0 bearish, 0 neutral/off-topic."""
    tokens = set(_WORD_RE.findall((text or "").lower()))
    return len(tokens & BULL_TERMS) - len(tokens & BEAR_TERMS)


def band_from_score(score: float) -> str:
    """0-10 score -> six-tier band (mirrors TradingAgents' SentimentReport scale)."""
    if score >= 6.5:
        return "Bullish"
    if score >= 5.5:
        return "Mildly Bullish"
    if score >= 4.5:
        return "Neutral"
    if score >= 3.5:
        return "Mildly Bearish"
    return "Bearish"


def direction_from_band(band: str) -> str:
    """Map a band to the vault's long/short/watch direction field."""
    return {
        "Bullish": "long",
        "Mildly Bullish": "long",
        "Neutral": "watch",
        "Mixed": "watch",
        "Mildly Bearish": "short",
        "Bearish": "short",
    }.get(band, "watch")


def _sign_of_band(band: str) -> int:
    if band in ("Bullish", "Mildly Bullish"):
        return 1
    if band in ("Bearish", "Mildly Bearish"):
        return -1
    return 0


# --------------------------------------------------------------------------- #
# StockTwits: parse + score
# --------------------------------------------------------------------------- #


def parse_stocktwits(payload: dict) -> list[dict]:
    """Extract (created_at, user, sentiment, body) from a StockTwits stream payload.
    Only Bullish/Bearish survive as a label; anything else becomes None."""
    messages = payload.get("messages", []) if isinstance(payload, dict) else []
    out: list[dict] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        entities = m.get("entities") or {}
        sent_obj = entities.get("sentiment") or {}
        sentiment = sent_obj.get("basic") if isinstance(sent_obj, dict) else None
        out.append(
            {
                "created_at": m.get("created_at"),
                "user": (m.get("user") or {}).get("username"),
                "sentiment": sentiment if sentiment in ("Bullish", "Bearish") else None,
                "body": (m.get("body") or "").strip(),
            }
        )
    return out


def score_stocktwits(messages: list[dict]) -> dict:
    """Score a StockTwits stream from its user-labeled Bullish/Bearish tags.

    The 0-10 score is a Beta(ST_PRIOR, ST_PRIOR)-shrunk bullish fraction, so a
    thin sample regresses toward 5.0 (the message-count base rate). Fires the
    contrarian over-extension flag once a real sample (>= OVEREXT_MIN_LABELED)
    leans >= OVEREXT_RATIO one way.
    """
    bullish = sum(1 for m in messages if m.get("sentiment") == "Bullish")
    bearish = sum(1 for m in messages if m.get("sentiment") == "Bearish")
    total = len(messages)
    labeled = bullish + bearish
    bull_ratio = (bullish / labeled) if labeled else None

    posterior = (bullish + ST_PRIOR) / (labeled + 2 * ST_PRIOR)
    score = round(10.0 * posterior, 2)

    overext_side: str | None = None
    if labeled >= OVEREXT_MIN_LABELED and bull_ratio is not None:
        if bull_ratio >= OVEREXT_RATIO:
            overext_side = "bullish"
        elif (1 - bull_ratio) >= OVEREXT_RATIO:
            overext_side = "bearish"

    return {
        "bullish": bullish,
        "bearish": bearish,
        "unlabeled": total - labeled,
        "total": total,
        "labeled": labeled,
        "bull_ratio": round(bull_ratio, 4) if bull_ratio is not None else None,
        "bull_pct": round(100 * bull_ratio) if bull_ratio is not None else None,
        "bear_pct": round(100 * (1 - bull_ratio)) if bull_ratio is not None else None,
        "score": score,
        "band": band_from_score(score),
        "contrarian_overextension": overext_side is not None,
        "overextension_side": overext_side,
        "has_data": total > 0,
    }


# --------------------------------------------------------------------------- #
# Reddit / X: parse + engagement-weighted score
# --------------------------------------------------------------------------- #


def parse_reddit_json(payload: dict) -> list[dict]:
    """Rich JSON listing -> posts carrying score + num_comments for weighting."""
    children = (
        (payload.get("data") or {}).get("children") or [] if isinstance(payload, dict) else []
    )
    out: list[dict] = []
    for c in children:
        d = c.get("data", {}) if isinstance(c, dict) else {}
        out.append(
            {
                "title": d.get("title") or "",
                "selftext": d.get("selftext") or "",
                "score": d.get("score"),
                "num_comments": d.get("num_comments"),
                "created_utc": d.get("created_utc"),
                "subreddit": d.get("subreddit"),
                "source": "json",
            }
        )
    return out


def parse_reddit_rss(xml_text: str) -> list[dict]:
    """Public Atom search feed -> posts with no score/comments (equal-weighted)."""
    import html
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out: list[dict] = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        title_el = entry.find("atom:title", _ATOM_NS)
        content_el = entry.find("atom:content", _ATOM_NS)
        content = content_el.text if content_el is not None else ""
        if content and "<!-- SC_OFF -->" in content and "<!-- SC_ON -->" in content:
            content = content.split("<!-- SC_OFF -->")[1].split("<!-- SC_ON -->")[0]
        selftext = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", content or "")).split())
        out.append(
            {
                "title": (title_el.text if title_el is not None else "") or "",
                "selftext": selftext,
                "score": None,
                "num_comments": None,
                "created_utc": None,
                "source": "rss",
            }
        )
    return out


def post_engagement(post: dict) -> float:
    """Reddit engagement = upvote score + comment count (0 on the RSS path)."""
    try:
        score = max(0.0, float(post.get("score") or 0))
        comments = max(0.0, float(post.get("num_comments") or 0))
    except (TypeError, ValueError):
        return 0.0
    return score + comments


def reddit_post_weight(post: dict) -> float:
    """Log-damped engagement weight; RSS posts (engagement 0) fall to 1.0."""
    return 1.0 + math.log1p(post_engagement(post))


def x_engagement(post: dict) -> float:
    """X engagement = impressions, falling back to likes."""
    for key in ("impressions", "likes"):
        val = post.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            return float(val)
    return 0.0


def x_post_weight(post: dict) -> float:
    return 1.0 + math.log1p(x_engagement(post))


def _directional_score(weighted_polarities: list[tuple[float, int]], shrink_k: float) -> dict:
    """Core engagement-weighted 0-10 score shared by Reddit and X.

    Each item is (weight, polarity). Neutral (polarity 0) items count toward
    volume but not direction. The weighted directional fraction is shrunk by
    the count of directional items so a lone loud post can't peg the score.
    """
    num = den = 0.0
    n_dir = bull = bear = 0
    for weight, pol in weighted_polarities:
        if pol == 0:
            continue
        n_dir += 1
        sign = 1.0 if pol > 0 else -1.0
        if sign > 0:
            bull += 1
        else:
            bear += 1
        num += weight * sign
        den += weight
    if den > 0:
        frac = num / den
        shrink = n_dir / (n_dir + shrink_k)
        score = max(0.0, min(10.0, 5.0 + 5.0 * frac * shrink))
    else:
        frac = 0.0
        score = 5.0
    return {
        "weighted_fraction": round(frac, 4),
        "score": round(score, 2),
        "n_directional": n_dir,
        "bull_posts": bull,
        "bear_posts": bear,
    }


def score_reddit(posts: list[dict]) -> dict:
    pairs = [
        (reddit_post_weight(p), polarity_from_text(f"{p.get('title', '')} {p.get('selftext', '')}"))
        for p in posts
    ]
    core = _directional_score(pairs, RD_SHRINK_K)
    return {
        **core,
        "band": band_from_score(core["score"]),
        "n_posts": len(posts),
        "engagement_total": round(sum(post_engagement(p) for p in posts), 2),
        "via_rss": any(p.get("source") == "rss" for p in posts) if posts else False,
        "has_data": len(posts) > 0,
    }


def score_x(posts: list[dict]) -> dict:
    pairs = [(x_post_weight(p), polarity_from_text(p.get("text", ""))) for p in posts]
    core = _directional_score(pairs, X_SHRINK_K)
    return {
        **core,
        "band": band_from_score(core["score"]),
        "n_posts": len(posts),
        "impression_total": round(sum(x_engagement(p) for p in posts), 2),
        "has_data": len(posts) > 0,
    }


def parse_x(payload: dict) -> list[dict]:
    """X v2 search payload -> posts with text + public_metrics for weighting.

    Resolves the author username from the ``includes.users`` expansion (raw v2
    shape) or from an already-enriched ``author`` object (MCP shape)."""
    data = payload.get("data") or [] if isinstance(payload, dict) else []
    includes = payload.get("includes") or {} if isinstance(payload, dict) else {}
    users_by_id = {
        u["id"]: u for u in (includes.get("users") or []) if isinstance(u, dict) and "id" in u
    }
    out: list[dict] = []
    for p in data:
        if not isinstance(p, dict):
            continue
        pm = p.get("public_metrics") or {}
        author = p.get("author")
        if isinstance(author, dict):
            username = author.get("username")
        else:
            u = users_by_id.get(p.get("author_id"))
            username = u.get("username") if isinstance(u, dict) else None
        out.append(
            {
                "text": p.get("text") or "",
                "created_at": p.get("created_at"),
                "impressions": pm.get("impression_count"),
                "likes": pm.get("like_count"),
                "author": username,
                "source": "x",
            }
        )
    # Impression-ranked: loudest posts first (scoring uses all; this orders display).
    out.sort(key=lambda p: x_engagement(p), reverse=True)
    return out


# --------------------------------------------------------------------------- #
# Cross-source combine
# --------------------------------------------------------------------------- #


def combine_sentiment(
    st: dict | None = None,
    rd: dict | None = None,
    x: dict | None = None,
) -> dict | None:
    """Blend the per-source scores into one reliability-weighted read.

    Weight = base_weight(source) x reliability(sample size). Divergence (one
    source bullish, another bearish) forces a Mixed / watch read and caps
    confidence. Returns None only when no source returned any data.
    """
    contributions: list[dict] = []
    if st and st.get("has_data"):
        contributions.append(
            {
                "name": "stocktwits",
                "score": st["score"],
                "reliability": min(1.0, st["labeled"] / ST_FULL_SAMPLE),
                "base_weight": SOURCE_BASE_WEIGHT["stocktwits"],
                "sign": _sign_of_band(st["band"]),
                "volume": st["labeled"],
            }
        )
    if rd and rd.get("has_data"):
        contributions.append(
            {
                "name": "reddit",
                "score": rd["score"],
                "reliability": min(1.0, rd["n_directional"] / RD_FULL_SAMPLE),
                "base_weight": SOURCE_BASE_WEIGHT["reddit"],
                "sign": _sign_of_band(rd["band"]),
                "volume": rd["n_directional"],
            }
        )
    if x and x.get("has_data"):
        contributions.append(
            {
                "name": "x",
                "score": x["score"],
                "reliability": min(1.0, x["n_directional"] / X_FULL_SAMPLE),
                "base_weight": SOURCE_BASE_WEIGHT["x"],
                "sign": _sign_of_band(x["band"]),
                "volume": x["n_directional"],
            }
        )
    if not contributions:
        return None

    wsum = sum(c["reliability"] * c["base_weight"] for c in contributions)
    if wsum > 0:
        overall = (
            sum(c["score"] * c["reliability"] * c["base_weight"] for c in contributions) / wsum
        )
    else:
        # Data present but no directional signal anywhere -> equal-weight the
        # (neutral) scores rather than divide by zero.
        overall = sum(c["score"] for c in contributions) / len(contributions)
    overall = round(max(0.0, min(10.0, overall)), 2)

    dir_signs = [c["sign"] for c in contributions if c["sign"] != 0]
    divergence = (1 in dir_signs) and (-1 in dir_signs)
    if divergence:
        band = "Mixed"
        direction = "watch"
    else:
        band = band_from_score(overall)
        direction = direction_from_band(band)

    volume = sum(c["volume"] for c in contributions)
    if volume >= 25 and len(contributions) >= 2:
        confidence = "high"
    elif volume >= 8:
        confidence = "medium"
    else:
        confidence = "low"
    if divergence and confidence == "high":
        confidence = "medium"

    return {
        "overall_score": overall,
        "overall_band": band,
        "direction": direction,
        "confidence": confidence,
        "divergence": divergence,
        "divergence_sources": sorted(c["name"] for c in contributions if c["sign"] != 0)
        if divergence
        else [],
        "contrarian_overextension": bool(st and st.get("contrarian_overextension")),
        "overextension_side": st.get("overextension_side") if st else None,
        "n_sources_with_data": len(contributions),
        "sources_present": [c["name"] for c in contributions],
    }


def analyze_ticker(
    st_messages: list[dict] | None,
    reddit_posts: list[dict] | None,
    x_posts: list[dict] | None = None,
) -> dict:
    """Pure end-to-end scoring for one ticker from already-fetched data.

    This is the anti-fabrication core: it consumes structured data and returns
    the full scored blocks; no network, no model. Returns per-source blocks and
    the combined read (`combined` is None when every source is empty)."""
    st = score_stocktwits(st_messages) if st_messages is not None else None
    rd = score_reddit(reddit_posts) if reddit_posts is not None else None
    xb = score_x(x_posts) if x_posts is not None else None
    return {
        "stocktwits": st,
        "reddit": rd,
        "x": xb,
        "combined": combine_sentiment(st, rd, xb),
    }


# --------------------------------------------------------------------------- #
# Vault writers (filesystem only; tests target a tmp dir)
# --------------------------------------------------------------------------- #

_SOURCE_URL = {
    "stocktwits": "https://stocktwits.com/symbol/{ticker}",
    "reddit": "https://www.reddit.com/search/?q={ticker}",
    "x": "https://x.com/search?q=%24{ticker}",
}


def _source_note_rows(source_type: str, block: dict) -> list[str]:
    """Aggregate-only stats table (never echoes raw post bodies or usernames)."""
    if source_type == "stocktwits":
        rows = [
            f"| Messages | {block['total']} |",
            f"| Bullish | {block['bullish']}"
            + (f" ({block['bull_pct']}%)" if block["bull_pct"] is not None else "")
            + " |",
            f"| Bearish | {block['bearish']}"
            + (f" ({block['bear_pct']}%)" if block["bear_pct"] is not None else "")
            + " |",
            f"| Unlabeled | {block['unlabeled']} |",
            f"| Sentiment score | {block['score']}/10 ({block['band']}) |",
            f"| Contrarian over-extension | {block['overextension_side'] or 'no'} |",
        ]
    elif source_type == "reddit":
        rows = [
            f"| Posts | {block['n_posts']} |",
            f"| Directional posts | {block['n_directional']} "
            f"(bull {block['bull_posts']} / bear {block['bear_posts']}) |",
            f"| Engagement (score+comments) | {block['engagement_total']} |",
            f"| Weighted fraction | {block['weighted_fraction']:+.2f} |",
            f"| Sentiment score | {block['score']}/10 ({block['band']}) |",
            f"| Via RSS (no engagement data) | {'yes' if block['via_rss'] else 'no'} |",
        ]
    else:  # x
        rows = [
            f"| Posts | {block['n_posts']} |",
            f"| Directional posts | {block['n_directional']} "
            f"(bull {block['bull_posts']} / bear {block['bear_posts']}) |",
            f"| Impressions | {block['impression_total']} |",
            f"| Weighted fraction | {block['weighted_fraction']:+.2f} |",
            f"| Sentiment score | {block['score']}/10 ({block['band']}) |",
        ]
    return rows


def write_source_note(
    vault_current_dir: Path,
    source_type: str,
    ticker: str,
    block: dict,
    date_s: str,
    week: str,
    raw_path: str | None = None,
) -> Path:
    """One source note per (ticker, platform), carrying deterministic stats.

    Returns the path. Idempotent per day: re-running overwrites the day's note
    with the freshest snapshot (the claim is 'as of today')."""
    source_dir = vault_current_dir / "sources" / source_type
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"{date_s}_{ticker}.md"
    url = _SOURCE_URL.get(source_type, "").format(ticker=ticker)
    lines = [
        "---",
        f"title: {ticker} retail sentiment - {source_type} ({date_s})",
        f"created: {date_s}",
        f"updated: {date_s}",
        f"week: {week}",
        "type: source",
        f"source_type: {source_type}",
        "status: active",
        "time_horizon: swing",
        f"ticker: {ticker}",
        f"source_url: {url}",
        "sources:",
        (f"  - {raw_path}" if raw_path else "  []"),
        f"tags: [social-signal, source, {source_type}]",
        "---",
        "",
        f"# {ticker} - {source_type} sentiment ({date_s})",
        "",
        "| Metric | Value |",
        "|---|---|",
        *_source_note_rows(source_type, block),
        "",
        "Deterministic aggregate of public retail chatter; raw message bodies and",
        "usernames are kept only in the git-ignored raw artifact, not reproduced here.",
        "",
    ]
    path.write_text("\n".join(lines))
    return path


def write_signal_note(
    vault_current_dir: Path,
    ticker: str,
    combined: dict,
    per_source: dict,
    source_links: list[str],
    date_s: str,
) -> Path:
    """One signal note per ticker, matching social-signal-ingestor's schema.

    No `watch` block (retail sentiment carries no clean numeric levels) and no
    `probability` (no real basis) -- honest to the schema rules."""
    sig_dir = vault_current_dir / "signals"
    sig_dir.mkdir(parents=True, exist_ok=True)
    path = sig_dir / f"{date_s}_{ticker}_retail-sentiment.md"

    band = combined["overall_band"]
    score = combined["overall_score"]
    title = f"{ticker} retail sentiment {band} ({score:.1f}/10)"
    tags = ["social-signal", "signal", "retail-sentiment"]
    if combined["contrarian_overextension"]:
        tags.append("contrarian-overextension")
    if combined["divergence"]:
        tags.append("cross-source-divergence")

    lines = [
        "---",
        f"title: {title}",
        "type: signal",
        "status: watching",
        f"ticker: {ticker}",
        f"direction: {combined['direction']}",
        "time_horizon: swing",
        f"claim_date: {date_s}",
        f"updated: {date_s}",
        "instrument: stock",
        "sources:",
    ]
    lines.extend(f"  - [[{link}]]" for link in source_links)
    lines.append(f"tags: [{', '.join(tags)}]")
    lines.append("---")
    lines += [
        "",
        f"# {title}",
        "",
        f"- **Overall:** {band} - {score:.1f}/10 - direction `{combined['direction']}`",
        f"- **Confidence:** {combined['confidence']} "
        f"({combined['n_sources_with_data']} source(s) with data)",
    ]
    if combined["divergence"]:
        lines.append(
            "- **Cross-source divergence:** "
            + ", ".join(combined["divergence_sources"])
            + " point in opposite directions -> treated as Mixed / watch."
        )
    if combined["contrarian_overextension"]:
        lines.append(
            f"- **Contrarian over-extension:** StockTwits is >= 90/10 "
            f"{combined['overextension_side']} - crowded, elevated reversal risk."
        )
    lines += [
        "",
        "## Per-source breakdown",
        "",
        "| Source | Score | Band | Volume |",
        "|---|---|---|---|",
    ]
    st, rd, xb = per_source.get("stocktwits"), per_source.get("reddit"), per_source.get("x")
    if st and st.get("has_data"):
        lines.append(f"| StockTwits | {st['score']}/10 | {st['band']} | {st['labeled']} labeled |")
    if rd and rd.get("has_data"):
        lines.append(
            f"| Reddit | {rd['score']}/10 | {rd['band']} | {rd['n_directional']} directional |"
        )
    if xb and xb.get("has_data"):
        lines.append(f"| X | {xb['score']}/10 | {xb['band']} | {xb['n_directional']} directional |")
    lines += [
        "",
        "_Deterministic retail-sentiment snapshot (data-in-prompt scoring; no model",
        "interpretation of raw posts). Signal for the trader to weigh alongside",
        "fundamentals and technicals, not a price call._",
        "",
    ]
    path.write_text("\n".join(lines))
    return path


def save_raw(raw_dir: Path, source_type: str, date_s: str, ticker: str, payload: Any) -> str:
    d = raw_dir / source_type / date_s
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{ticker}.json"
    p.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return str(p)


def write_ticker_notes(
    ticker: str,
    analysis: dict,
    paths: dict[str, Path],
    date_s: str,
    week: str,
    raw_payloads: dict[str, Any] | None = None,
) -> dict:
    """Write the raw artifacts + per-source notes + signal note for one ticker.

    Returns a small manifest (paths written). `analysis` is the output of
    analyze_ticker; `combined` must be non-None (caller skips empty tickers)."""
    vault = paths["vault_current"]
    raw_dir = paths["raw"]
    raw_payloads = raw_payloads or {}
    combined = analysis["combined"]
    source_links: list[str] = []
    source_notes: list[str] = []
    for source_type in ("stocktwits", "reddit", "x"):
        block = analysis.get(source_type)
        if not block or not block.get("has_data"):
            continue
        raw_path = None
        if source_type in raw_payloads:
            raw_path = save_raw(raw_dir, source_type, date_s, ticker, raw_payloads[source_type])
        note = write_source_note(vault, source_type, ticker, block, date_s, week, raw_path)
        source_notes.append(str(note))
        source_links.append(f"sources/{source_type}/{date_s}_{ticker}")

    signal_note = write_signal_note(vault, ticker, combined, analysis, source_links, date_s)
    return {
        "ticker": ticker,
        "signal_note": str(signal_note),
        "source_notes": source_notes,
        "direction": combined["direction"],
        "overall_score": combined["overall_score"],
        "overall_band": combined["overall_band"],
        "confidence": combined["confidence"],
        "divergence": combined["divergence"],
        "contrarian_overextension": combined["contrarian_overextension"],
    }


# --------------------------------------------------------------------------- #
# Network fetchers (lazy imports; never exercised by the offline test suite)
# --------------------------------------------------------------------------- #


def _http_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes | None]:
    """GET a URL. Returns (status_code, body) or (-1, None) on transport error."""
    import http.client
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted hosts)
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (OSError, http.client.HTTPException):
        return -1, None


def fetch_stocktwits(ticker: str, timeout: float = 10.0) -> dict | None:
    status, body = _http_get(
        STOCKTWITS_STREAM.format(ticker=ticker),
        {"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout,
    )
    if status != 200 or not body:
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None


def fetch_trending_symbols(timeout: float = 10.0) -> list[str]:
    status, body = _http_get(
        STOCKTWITS_TRENDING,
        {"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout,
    )
    if status != 200 or not body:
        return []
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return []
    syms = payload.get("symbols", []) if isinstance(payload, dict) else []
    return [s["symbol"] for s in syms if isinstance(s, dict) and s.get("symbol")]


def _reddit_qs(ticker: str, limit: int) -> str:
    from urllib.parse import urlencode

    return urlencode(
        {
            "q": ticker,
            "restrict_sr": "on",
            "sort": "new",
            "t": "week",
            "limit": limit,
        }
    )


def fetch_reddit(ticker: str, sub: str, limit: int = 5, timeout: float = 10.0) -> list[dict]:
    """JSON search first (carries engagement); fall back to the Atom RSS feed,
    which is the reliable keyless path when Reddit's WAF 403s the JSON endpoint."""
    qs = _reddit_qs(ticker, limit)
    status, body = _http_get(
        REDDIT_JSON.format(sub=sub, qs=qs),
        {"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout,
    )
    if status == 200 and body:
        try:
            return parse_reddit_json(json.loads(body))
        except (json.JSONDecodeError, ValueError):
            pass
    status, body = _http_get(REDDIT_RSS.format(sub=sub, qs=qs), {"User-Agent": USER_AGENT}, timeout)
    if status == 200 and body:
        return parse_reddit_rss(body.decode("utf-8", "replace"))
    return []


def fetch_x(
    ticker: str,
    token: str,
    max_results: int = 50,
    start_time: str | None = None,
    end_time: str | None = None,
    timeout: float = 30.0,
) -> dict | None:
    """Optional X v2 search. Recent search by default; the full-archive endpoint
    (paid tier) is used automatically when an event window is given."""
    from urllib.parse import urlencode

    endpoint = "/tweets/search/all" if (start_time or end_time) else "/tweets/search/recent"
    params = {
        "query": f"${ticker} -is:retweet lang:en",
        "max_results": max(10, min(max_results, 100 if endpoint.endswith("recent") else 500)),
        "tweet.fields": "created_at,public_metrics,lang,author_id",
    }
    if start_time:
        params["start_time"] = start_time
    if end_time:
        params["end_time"] = end_time
    url = f"{X_API_BASE}{endpoint}?{urlencode(params)}"
    status, body = _http_get(
        url,
        {
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        timeout,
    )
    if status != 200 or not body:
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Run summary report (reports/)
# --------------------------------------------------------------------------- #


def build_run_report(results: list[dict], skipped: list[dict], now: dt.datetime) -> dict:
    return {
        "schema_version": "1.0",
        "source_skill": "retail_sentiment_ingestor",
        "generated_at": now.isoformat(),
        "week": now.strftime("%G-W%V"),
        "ticker_count": len(results),
        "skipped_no_data": skipped,
        "results": sorted(results, key=lambda r: r.get("overall_score", 5.0), reverse=True),
    }


def render_run_markdown(report: dict) -> str:
    lines = [
        "# Retail Sentiment Ingest",
        f"**Generated:** {report['generated_at']}",
        f"**Week:** {report['week']} | **Tickers scored:** {report['ticker_count']}",
        "",
        "| Ticker | Band | Score | Direction | Confidence | Divergence | Over-ext |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in report["results"]:
        lines.append(
            f"| {r['ticker']} | {r['overall_band']} | {r['overall_score']:.1f} | "
            f"{r['direction']} | {r['confidence']} | "
            f"{'yes' if r['divergence'] else 'no'} | "
            f"{'yes' if r['contrarian_overextension'] else 'no'} |"
        )
    if report["skipped_no_data"]:
        lines += ["", "Skipped (no data): " + ", ".join(report["skipped_no_data"])]
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def resolve_tickers(args: argparse.Namespace) -> list[str]:
    seen: list[str] = []
    raw: list[str] = []
    if args.tickers:
        raw += re.split(r"[,\s]+", args.tickers)
    if args.watchlist:
        wl = Path(args.watchlist).expanduser()
        if wl.exists():
            raw += re.split(r"[,\s]+", wl.read_text())
    if args.trending:
        raw += fetch_trending_symbols()
    for item in raw:
        t = normalize_ticker(item)
        if t and t not in seen:
            seen.append(t)
    return seen


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Ingest keyless retail sentiment (StockTwits + Reddit, optional X) into the social vault."
    )
    p.add_argument("--tickers", help="Comma/space-separated tickers, e.g. 'NVDA,AMD,TSLA'")
    p.add_argument("--watchlist", help="Path to a file of tickers (comma/newline separated)")
    p.add_argument(
        "--trending", action="store_true", help="Seed tickers from StockTwits trending (keyless)"
    )
    p.add_argument(
        "--agent", default="social", help="Agent name -> data/<agent>/ (default: social)"
    )
    p.add_argument("--data-dir", default=None, help="Override base data dir (default: <repo>/data)")
    p.add_argument(
        "--subreddits",
        default=",".join(DEFAULT_SUBREDDITS),
        help="Comma-separated subreddits (default: wallstreetbets,stocks,investing)",
    )
    p.add_argument("--st-limit", type=int, default=30, help="StockTwits messages per ticker")
    p.add_argument("--reddit-limit", type=int, default=5, help="Reddit posts per subreddit")
    p.add_argument(
        "--use-x", action="store_true", help="Include the optional X path (needs X_API_KEY)"
    )
    p.add_argument("--x-api-key", default=None, help="X bearer token (else env X_API_KEY)")
    p.add_argument(
        "--x-start", default=None, help="Event-window start (ISO8601, full-archive search)"
    )
    p.add_argument("--x-end", default=None, help="Event-window end (ISO8601, full-archive search)")
    p.add_argument("--x-max-results", type=int, default=50, help="X posts per ticker")
    p.add_argument(
        "--output-dir", default="reports/", help="Run-summary output dir (default: reports/)"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="Fetch + score, print summary, write nothing"
    )
    return p


def main() -> int:
    args = build_parser().parse_args()

    x_token: str | None = None
    if args.use_x:
        x_token = args.x_api_key or os.environ.get("X_API_KEY")
        if not x_token:
            print(
                "Error: --use-x requires an X bearer token (set X_API_KEY or pass --x-api-key)",
                file=sys.stderr,
            )
            return 1

    tickers = resolve_tickers(args)
    if not tickers:
        print(
            "Error: no valid tickers. Pass --tickers, --watchlist, and/or --trending.",
            file=sys.stderr,
        )
        return 1

    subreddits = [s for s in re.split(r"[,\s]+", args.subreddits) if s]
    paths = resolve_paths(args.agent, args.data_dir)
    now = dt.datetime.now(dt.timezone.utc)
    date_s, week = today_and_week(now)

    results: list[dict] = []
    skipped: list[str] = []
    for ticker in tickers:
        st_payload = fetch_stocktwits(ticker, timeout=10.0)
        st_messages = parse_stocktwits(st_payload) if st_payload else []

        reddit_posts: list[dict] = []
        for sub in subreddits:
            reddit_posts += fetch_reddit(ticker, sub, limit=args.reddit_limit)

        x_payload = None
        x_posts = None
        if x_token:
            x_payload = fetch_x(
                ticker,
                x_token,
                max_results=args.x_max_results,
                start_time=args.x_start,
                end_time=args.x_end,
            )
            x_posts = parse_x(x_payload) if x_payload else []

        analysis = analyze_ticker(st_messages, reddit_posts, x_posts)
        if analysis["combined"] is None:
            skipped.append(ticker)
            continue

        if args.dry_run:
            results.append(
                {
                    "ticker": ticker,
                    "overall_band": analysis["combined"]["overall_band"],
                    "overall_score": analysis["combined"]["overall_score"],
                    "direction": analysis["combined"]["direction"],
                    "confidence": analysis["combined"]["confidence"],
                    "divergence": analysis["combined"]["divergence"],
                    "contrarian_overextension": analysis["combined"]["contrarian_overextension"],
                }
            )
            continue

        raw_payloads: dict[str, Any] = {}
        if st_payload:
            raw_payloads["stocktwits"] = st_payload
        if reddit_posts:
            raw_payloads["reddit"] = reddit_posts
        if x_payload:
            raw_payloads["x"] = x_payload
        results.append(write_ticker_notes(ticker, analysis, paths, date_s, week, raw_payloads))

    report = build_run_report(results, skipped, now)
    report["agent"] = args.agent
    report["dry_run"] = args.dry_run

    if not args.dry_run:
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = now.strftime("%Y-%m-%d_%H%M%S")
        (out_dir / f"retail_sentiment_{ts}.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str)
        )
        (out_dir / f"retail_sentiment_{ts}.md").write_text(render_run_markdown(report))
        report["message"] = (
            f"Scored {len(results)} ticker(s) into data/{args.agent}/vault/current/signals/. "
            "Run social-signal-ingestor build_signal_index.py, then edge-social-aggregator."
        )
    else:
        report["message"] = f"Dry run: scored {len(results)} ticker(s); wrote nothing."

    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
