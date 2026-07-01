"""Tests for aggregate_social.py — recency × corroboration scoring + per-ticker dedup."""

import datetime as dt

from aggregate_social import aggregate, score_one

NOW = dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc)

INDEX = {
    "week": "2026-W26",
    "signals": [
        {
            "ticker": "NVDA",
            "direction": "long",
            "claim_date": "2026-06-22",
            "sources": ["a", "b"],
            "title": "NVDA strong",
            "time_horizon": "weekly",
        },
        {
            "ticker": "NVDA",
            "direction": "long",
            "claim_date": "2026-06-01",
            "sources": ["c"],
            "title": "NVDA old",
        },
        {
            "ticker": "AMD",
            "direction": "short",
            "claim_date": "2026-06-21",
            "sources": ["d"],
            "title": "AMD weak",
        },
        {
            "ticker": "FOO/BAR",
            "direction": "long",
            "claim_date": "2026-06-22",
            "sources": ["e"],
        },  # multi-symbol → skipped
    ],
}


def test_dedup_skip_and_ordering():
    res = aggregate(INDEX, now=NOW)
    assert res["signal_count"] == 2  # NVDA deduped, AMD; FOO/BAR dropped
    assert res["skipped_invalid_ticker"] == 1
    tickers = [s["ticker"] for s in res["signals"]]
    assert tickers == ["NVDA", "AMD"]  # sorted by conviction desc


def test_nvda_keeps_strongest_mention_and_unions_sources():
    res = aggregate(INDEX, now=NOW)
    nvda = next(s for s in res["signals"] if s["ticker"] == "NVDA")
    # strongest = same-day (recency 1.0), 2 sources (source_factor 0.7) → 0.70
    assert abs(nvda["social_conviction"] - 0.70) < 1e-6
    assert nvda["n_sources"] == 3  # {a,b} ∪ {c}
    assert nvda["direction"] == "long"


def test_all_convictions_bounded():
    res = aggregate(INDEX, now=NOW)
    assert all(0.0 <= s["social_conviction"] <= 1.0 for s in res["signals"])


def test_score_one_recency_and_corroboration():
    recent_corroborated = {"claim_date": "2026-06-22", "sources": ["a", "b", "c", "d"]}
    stale_single = {"claim_date": "2026-01-01", "sources": ["a"]}
    assert score_one(recent_corroborated, NOW) > score_one(stale_single, NOW)
