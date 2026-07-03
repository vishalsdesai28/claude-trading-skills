#!/usr/bin/env python3
"""
Tests for the analyst estimate-revision momentum factor (estimate_revision.py)
and its integration as the 6th input to the composite scorer.

All tests run offline against saved yfinance-shaped fixtures in
scripts/tests/fixtures/. No network, no yfinance import.
"""

import json
import os

import estimate_revision as er
from scorer import COMPONENT_WEIGHTS, COMPONENT_WEIGHTS_6, calculate_composite_score

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name):
    with open(os.path.join(FIXTURE_DIR, name)) as f:
        return json.load(f)


# ===========================================================================
# Sub-computation tests
# ===========================================================================


class TestConsensusSummary:
    def test_spread_computed(self):
        data = _load("upco_estimates.json")
        cons = er.summarize_consensus(data["earnings_estimate"])
        q = cons["0q"]
        assert q["avg"] == 1.42
        assert q["spread"] == round(1.50 - 1.35, 4)
        # spread_pct = 0.15 / 1.42 * 100 ~= 10.56
        assert abs(q["spread_pct"] - 10.56) < 0.1
        assert q["num_analysts"] == 28

    def test_empty_estimate(self):
        assert er.summarize_consensus({}) == {}

    def test_zero_avg_no_spread_pct(self):
        cons = er.summarize_consensus({"0q": {"avg": 0, "low": -0.1, "high": 0.1}})
        assert cons["0q"]["spread"] == 0.2
        assert cons["0q"]["spread_pct"] is None


class TestEpsTrendDrift:
    def test_positive_drift(self):
        data = _load("upco_estimates.json")
        drift = er.compute_eps_trend_drift(data["eps_trend"])
        d0q = drift["0q"]["drift"]
        # 30d: (1.42 - 1.38) / 1.38 * 100 = 2.90
        assert abs(d0q["30d"]["pct"] - 2.90) < 0.05
        # 90d: (1.42 - 1.33) / 1.33 * 100 = 6.77
        assert abs(d0q["90d"]["pct"] - 6.77) < 0.05
        assert d0q["30d"]["abs"] == round(1.42 - 1.38, 4)

    def test_negative_drift(self):
        data = _load("dnco_estimates.json")
        drift = er.compute_eps_trend_drift(data["eps_trend"])
        d0q = drift["0q"]["drift"]
        assert d0q["30d"]["pct"] < 0
        assert d0q["90d"]["pct"] < 0

    def test_missing_base_gives_none(self):
        drift = er.compute_eps_trend_drift({"0q": {"current": 1.0}})
        assert drift["0q"]["drift"]["30d"]["pct"] is None


class TestRevisionBreadth:
    def test_upgrade_breadth(self):
        data = _load("upco_estimates.json")
        b = er.compute_revision_breadth(data["eps_revisions"])
        assert b["total_up30"] == 12 + 9 + 14 + 10
        assert b["total_down30"] == 3 + 2 + 2 + 3
        assert b["net30"] > 0
        assert b["breadth_ratio"] > 0.7

    def test_downgrade_breadth(self):
        data = _load("dnco_estimates.json")
        b = er.compute_revision_breadth(data["eps_revisions"])
        assert b["net30"] < 0
        assert b["breadth_ratio"] < 0.3

    def test_empty_breadth_ratio_none(self):
        b = er.compute_revision_breadth({})
        assert b["breadth_ratio"] is None
        assert b["net30"] == 0


class TestCalibration:
    def test_consistent_beats(self):
        data = _load("upco_estimates.json")
        cal = er.compute_calibration(data["earnings_history"])
        assert cal["n"] == 4
        assert cal["beat_rate"] == 1.0
        assert 0.8 <= cal["calibration_score"] <= 1.0

    def test_no_history_neutral(self):
        cal = er.compute_calibration([])
        assert cal["n"] == 0
        assert cal["calibration_score"] == 0.5
        assert cal["beat_rate"] is None

    def test_surprise_from_est_actual(self):
        cal = er.compute_calibration(
            [{"epsEstimate": 1.0, "epsActual": 1.1}]  # +10% surprise, ignores surprisePercent
        )
        assert cal["quarters"][0]["surprise_pct"] == 10.0
        assert cal["quarters"][0]["beat"] is True


# ===========================================================================
# Top-level factor tests (the key behavior)
# ===========================================================================


class TestRevisionFactor:
    def test_upgrade_scores_above_neutral(self):
        factor = er.compute_revision_factor(_load("upco_estimates.json"))
        assert factor["score"] > 58
        assert factor["direction"] == "up"
        assert factor["label"] in ("upgrade", "strong_upgrade")
        assert factor["signed_signal"] > 0

    def test_quiet_downgrade_penalized(self):
        factor = er.compute_revision_factor(_load("dnco_estimates.json"))
        assert factor["score"] < 42
        assert factor["direction"] == "down"
        assert factor["label"] in ("downgrade", "strong_downgrade")
        assert factor["signed_signal"] < 0

    def test_no_coverage_is_neutral_not_penalized(self):
        factor = er.compute_revision_factor(_load("nocv_estimates.json"))
        assert factor["score"] == 50.0
        assert factor["direction"] == "flat"
        assert factor["label"] == "neutral"
        assert "warning" in factor

    def test_factor_has_score_for_scorer(self):
        factor = er.compute_revision_factor(_load("upco_estimates.json"))
        assert isinstance(factor["score"], (int, float))
        assert 0 <= factor["score"] <= 100

    def test_upgrade_outscores_downgrade(self):
        up = er.compute_revision_factor(_load("upco_estimates.json"))
        down = er.compute_revision_factor(_load("dnco_estimates.json"))
        assert up["score"] > down["score"]


# ===========================================================================
# Report builder (pure, offline)
# ===========================================================================


class TestBuildReport:
    def test_report_contains_key_fields(self):
        factor = er.compute_revision_factor(_load("upco_estimates.json"))
        json_obj, md = er.build_report(factor, "2026-07-03T00:00:00+00:00")
        assert json_obj["schema_version"] == "1.0"
        assert json_obj["ticker"] == "UPCO"
        assert "UPCO" in md
        assert "Revision score" in md
        assert "EPS-Trend Drift" in md

    def test_report_warns_on_no_coverage(self):
        factor = er.compute_revision_factor(_load("nocv_estimates.json"))
        _, md = er.build_report(factor, "2026-07-03T00:00:00+00:00")
        assert "No analyst estimate" in md


# ===========================================================================
# 6-factor composite scorer integration
# ===========================================================================


class TestSixFactorScorer:
    def test_weights_6_sum_to_one(self):
        assert abs(sum(COMPONENT_WEIGHTS_6.values()) - 1.0) < 1e-9

    def test_five_factor_path_unchanged(self):
        """Omitting revision_score reproduces the original 5-factor result."""
        result = calculate_composite_score(70, 70, 70, 70, 70)
        assert result["composite_score"] == 70.0
        assert len(result["component_breakdown"]) == 5
        assert "Estimate Revision" not in result["component_breakdown"]
        assert len(COMPONENT_WEIGHTS) == 5

    def test_six_factor_breakdown_has_revision(self):
        result = calculate_composite_score(80, 80, 80, 80, 80, revision_score=80)
        assert len(result["component_breakdown"]) == 6
        assert "Estimate Revision" in result["component_breakdown"]
        assert result["composite_score"] == 80.0  # uniform 80 across weights summing to 1.0

    def test_quiet_downgrade_lowers_composite(self):
        """Same price/volume factors: a downgrade revision must score lower
        than an upgrade revision, and lower than a neutral one."""
        base = dict(gap_score=90, trend_score=80, volume_score=70, ma200_score=60, ma50_score=50)
        upgrade = calculate_composite_score(**base, revision_score=80)
        neutral = calculate_composite_score(**base, revision_score=50)
        downgrade = calculate_composite_score(**base, revision_score=20)
        assert downgrade["composite_score"] < neutral["composite_score"]
        assert downgrade["composite_score"] < upgrade["composite_score"]

    def test_downgrade_can_flip_grade(self):
        """A strong price setup that would grade A can be pulled to B by a
        real analyst downgrade fed through the fixture pipeline."""
        down = er.compute_revision_factor(_load("dnco_estimates.json"))
        strong = calculate_composite_score(100, 100, 100, 100, 100)
        with_downgrade = calculate_composite_score(
            100, 100, 100, 100, 100, revision_score=down["score"]
        )
        assert with_downgrade["composite_score"] < strong["composite_score"]

    def test_monotonic_in_revision_score(self):
        base = dict(gap_score=60, trend_score=60, volume_score=60, ma200_score=60, ma50_score=60)
        low = calculate_composite_score(**base, revision_score=10)
        high = calculate_composite_score(**base, revision_score=90)
        assert high["composite_score"] > low["composite_score"]
