"""Tests for the social source added to edge-signal-aggregator."""

from copy import deepcopy

from aggregate_signals import (
    DEFAULT_CONFIG,
    DEFAULT_WEIGHTS,
    aggregate_signals,
    extract_signals_from_social,
)

SOCIAL_DOC = {
    "_source_file": "reports/edge_social_aggregator_x.json",
    "signals": [
        {
            "ticker": "NVDA",
            "direction": "long",
            "social_conviction": 0.7,
            "title": "NVDA social",
            "time_horizon": "weekly",
            "timestamp": "2026-06-22",
        },
    ],
}


def test_social_carries_lowest_weight():
    assert DEFAULT_WEIGHTS["edge_social_aggregator"] == 0.10
    assert DEFAULT_WEIGHTS["edge_social_aggregator"] <= min(DEFAULT_WEIGHTS.values())


def test_extract_signals_from_social():
    sigs = extract_signals_from_social([SOCIAL_DOC])
    assert len(sigs) == 1
    s = sigs[0]
    assert s["skill"] == "edge_social_aggregator"
    assert s["tickers"] == ["NVDA"]
    assert s["direction"] == "LONG"
    assert abs(s["raw_score"] - 0.7) < 1e-9


def test_aggregate_includes_social_source():
    res = aggregate_signals(
        edge_candidates=[],
        edge_concepts=[],
        themes=[],
        sectors=[],
        institutional=[],
        hints=[],
        config=deepcopy(DEFAULT_CONFIG),
        social=[SOCIAL_DOC],
    )
    assert res["summary"]["total_input_signals"] == 1
    assert any("NVDA" in s["tickers"] for s in res["ranked_signals"])
