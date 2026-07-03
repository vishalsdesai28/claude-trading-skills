#!/usr/bin/env python3
"""
Composite Scorer for Earnings Trade Analyzer

Combines factor scores with fixed weights into a composite score (0-100)
and assigns letter grades (A/B/C/D).

Two weighting modes are supported for backward compatibility:

5-factor (default, when no estimate-revision score is supplied):
  gap_size:            25%
  pre_earnings_trend:  30%
  volume_trend:        20%
  ma200_position:      15%
  ma50_position:       10%

6-factor (when an estimate-revision momentum score is supplied): the
analyst estimate-revision factor is added at 15% weight, with the other
five factors reduced pro-rata so the weights still sum to exactly 1.0.
This lets candidates facing quiet analyst downgrades be penalized even
when their price/volume factors look strong.

Grade Thresholds:
  A: 85+   "Strong earnings reaction with institutional accumulation"
  B: 70-84 "Good earnings reaction worth monitoring"
  C: 55-69 "Mixed signals, use caution"
  D: <55   "Weak setup, avoid"
"""

from __future__ import annotations

COMPONENT_WEIGHTS = {
    "gap_size": 0.25,
    "pre_earnings_trend": 0.30,
    "volume_trend": 0.20,
    "ma200_position": 0.15,
    "ma50_position": 0.10,
}

# 6-factor weights, used when an estimate-revision score is provided.
# Sums to exactly 1.0; the estimate-revision factor claims 15%, taken
# pro-rata from the original five factors.
COMPONENT_WEIGHTS_6 = {
    "gap_size": 0.22,
    "pre_earnings_trend": 0.26,
    "volume_trend": 0.17,
    "ma200_position": 0.12,
    "ma50_position": 0.08,
    "estimate_revision": 0.15,
}

GRADE_THRESHOLDS = [
    (85, "A", "Strong earnings reaction with institutional accumulation"),
    (70, "B", "Good earnings reaction worth monitoring"),
    (55, "C", "Mixed signals, use caution"),
    (0, "D", "Weak setup, avoid"),
]

GRADE_GUIDANCE = {
    "A": "Consider entry on pullback to gap support or breakout continuation. High conviction setup.",
    "B": "Monitor for follow-through buying. Wait for pullback to key support or volume confirmation.",
    "C": "Additional analysis needed. Consider waiting for clearer price action or catalyst.",
    "D": "Avoid trading. Weak setup with poor risk/reward profile.",
}


def calculate_composite_score(
    gap_score: float,
    trend_score: float,
    volume_score: float,
    ma200_score: float,
    ma50_score: float,
    revision_score: float | None = None,
) -> dict:
    """
    Calculate weighted composite score and assign grade.

    Args:
        gap_score: Gap size score (0-100)
        trend_score: Pre-earnings trend score (0-100)
        volume_score: Volume trend score (0-100)
        ma200_score: MA200 position score (0-100)
        ma50_score: MA50 position score (0-100)
        revision_score: Optional analyst estimate-revision momentum score
            (0-100). When None (default), the original 5-factor weighting
            is used and the result is identical to prior behavior. When
            supplied, the 6-factor weighting (COMPONENT_WEIGHTS_6) is used,
            adding the estimate-revision factor at 15% weight.

    Returns:
        dict with:
          - composite_score: float (0-100)
          - grade: str ('A', 'B', 'C', or 'D')
          - grade_description: str
          - guidance: str
          - weakest_component: str
          - weakest_score: float
          - strongest_component: str
          - strongest_score: float
          - component_breakdown: dict
    """
    if revision_score is None:
        weights = COMPONENT_WEIGHTS
        components = {
            "Gap Size": {"score": gap_score, "weight": weights["gap_size"]},
            "Pre-Earnings Trend": {
                "score": trend_score,
                "weight": weights["pre_earnings_trend"],
            },
            "Volume Trend": {"score": volume_score, "weight": weights["volume_trend"]},
            "MA200 Position": {"score": ma200_score, "weight": weights["ma200_position"]},
            "MA50 Position": {"score": ma50_score, "weight": weights["ma50_position"]},
        }
    else:
        weights = COMPONENT_WEIGHTS_6
        components = {
            "Gap Size": {"score": gap_score, "weight": weights["gap_size"]},
            "Pre-Earnings Trend": {
                "score": trend_score,
                "weight": weights["pre_earnings_trend"],
            },
            "Volume Trend": {"score": volume_score, "weight": weights["volume_trend"]},
            "MA200 Position": {"score": ma200_score, "weight": weights["ma200_position"]},
            "MA50 Position": {"score": ma50_score, "weight": weights["ma50_position"]},
            "Estimate Revision": {
                "score": revision_score,
                "weight": weights["estimate_revision"],
            },
        }

    composite_score = sum(comp["score"] * comp["weight"] for comp in components.values())
    composite_score = round(composite_score, 1)

    # Determine grade
    grade = "D"
    grade_description = "Weak setup, avoid"
    for threshold, g, desc in GRADE_THRESHOLDS:
        if composite_score >= threshold:
            grade = g
            grade_description = desc
            break

    guidance = GRADE_GUIDANCE.get(grade, "")

    # Find weakest and strongest components
    weakest_name = min(components, key=lambda k: components[k]["score"])
    strongest_name = max(components, key=lambda k: components[k]["score"])

    component_breakdown = {
        name: {
            "score": comp["score"],
            "weight": comp["weight"],
            "weighted_score": round(comp["score"] * comp["weight"], 1),
        }
        for name, comp in components.items()
    }

    return {
        "composite_score": composite_score,
        "grade": grade,
        "grade_description": grade_description,
        "guidance": guidance,
        "weakest_component": weakest_name,
        "weakest_score": components[weakest_name]["score"],
        "strongest_component": strongest_name,
        "strongest_score": components[strongest_name]["score"],
        "component_breakdown": component_breakdown,
    }
