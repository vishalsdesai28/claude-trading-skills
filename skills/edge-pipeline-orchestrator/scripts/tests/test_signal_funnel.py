"""Tests for signal_funnel.py — composite normalization, TA verdict thresholds, gate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_funnel import (  # noqa: E402
    composite_score,
    default_config,
    funnel_candidate,
    funnel_candidates,
    main,
    momentum_burst,
    normalize_ohlcv,
    render_markdown,
    run_triggers,
    sustained_trend,
    ta_filter,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _hit(name: str, score: float, fired: bool = True) -> dict:
    return {"name": name, "score": score, "reason": "", "fired": fired}


# ─── Composite normalization ────────────────────────────────────────────────


class TestCompositeNormalization:
    def test_no_fired_hits_scores_zero(self):
        weights = {"a": 0.5, "b": 0.5}
        assert composite_score([_hit("a", 10, fired=False)], weights) == 0.0
        assert composite_score([], weights) == 0.0

    def test_cofiring_beats_lone_max(self):
        """Two co-firing triggers must outscore a single max-score trigger."""
        weights = {"a": 0.5, "b": 0.5}
        lone = composite_score([_hit("a", 10)], weights)
        cofire = composite_score([_hit("a", 10), _hit("b", 10)], weights)
        assert cofire > lone
        assert cofire == pytest.approx(100.0)
        assert lone == pytest.approx(50.0)

    def test_lone_max_cannot_reach_100_with_real_weights(self):
        """Normalizing against the sum of ALL weights caps a lone trigger well below 100."""
        weights = default_config()["weights"]
        lone = composite_score([_hit("adx_trend", 10)], weights)
        assert 0 < lone < 100
        # adx_trend weight 0.55, total weight 2.25 -> (10*0.55/2.25)*10 ~= 24.4
        assert lone == pytest.approx(24.44, abs=0.1)

    def test_zero_weight_lane_does_not_change_composite(self):
        """A fired zero-weight surfacing lane must not move the composite score."""
        weights = default_config()["weights"]
        base = composite_score([_hit("adx_trend", 10)], weights)
        with_lane = composite_score([_hit("adx_trend", 10), _hit("sustained_trend", 10)], weights)
        assert with_lane == pytest.approx(base)

    def test_score_clamped_0_100(self):
        weights = {"a": 0.1}
        # Even an absurd score is clamped to 100.
        assert composite_score([_hit("a", 10_000)], weights) == 100.0


# ─── Trigger library ─────────────────────────────────────────────────────────


class TestTriggerLibrary:
    def test_run_triggers_shape(self):
        candles = _load("confirmed_candidate.json")["ohlcv"]["1d"]
        hits = run_triggers(candles)
        assert len(hits) == 13
        for h in hits:
            assert set(h) >= {"name", "score", "reason", "fired"}
            assert 0.0 <= h["score"] <= 10.0
            assert isinstance(h["fired"], bool)

    def test_momentum_burst_fires_on_pop_not_on_flat(self):
        burst_candles = _load("burst_candidate.json")["ohlcv"]["1d"]
        flat_candles = _load("rejected_candidate.json")["ohlcv"]["1d"]
        assert momentum_burst(burst_candles)["fired"] is True
        assert momentum_burst(flat_candles)["fired"] is False

    def test_sustained_trend_fires_on_uptrend(self):
        up = _load("confirmed_candidate.json")["ohlcv"]["1d"]
        hit = sustained_trend(up)
        assert hit["fired"] is True
        assert 0.0 <= hit["score"] <= 10.0

    def test_triggers_handle_short_series(self):
        # Fewer bars than any lookback: nothing should fire, nothing should raise.
        tiny = [{"o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]
        for h in run_triggers(tiny):
            assert h["fired"] is False


# ─── Multi-timeframe TA filter thresholds ────────────────────────────────────


class TestTAFilter:
    def test_confirmed_on_clean_uptrend(self):
        tf = _load("confirmed_candidate.json")["ohlcv"]
        res = ta_filter(tf, composite=33.0)
        assert res["verdict"] == "CONFIRMED"
        assert res["trend_direction"] == "bullish"
        assert res["score"] >= 22.0

    def test_insufficient_data_is_rejected(self):
        tf = {"1d": _load("confirmed_candidate.json")["ohlcv"]["1d"][:10]}
        res = ta_filter(tf)
        assert res["verdict"] == "REJECTED"
        assert res["reason"] == "insufficient candle data"

    def test_verdict_threshold_mapping(self):
        """Same candles, moving only the thresholds, must walk CONFIRMED->WEAK->REJECTED."""
        tf = _load("confirmed_candidate.json")["ohlcv"]
        score = ta_filter(tf, composite=33.0)["score"]
        assert score >= 22.0  # baseline is CONFIRMED under defaults

        weak = ta_filter(
            tf,
            composite=33.0,
            config={"confirmed_threshold": score + 5, "weak_threshold": score - 5},
        )
        assert weak["verdict"] == "WEAK"

        rejected = ta_filter(
            tf,
            composite=33.0,
            config={"confirmed_threshold": score + 20, "weak_threshold": score + 10},
        )
        assert rejected["verdict"] == "REJECTED"

    def test_directional_weighting_downtrend_scores_lower(self):
        """A clean downtrend earns half the trend bonus of the mirror uptrend."""
        up_candles = _load("confirmed_candidate.json")["ohlcv"]["1d"]
        down_candles = [
            {"o": c["o"], "h": c["h"], "l": c["l"], "c": (300 - c["c"]), "v": c["v"]}
            for c in up_candles
        ]
        # rebuild h/l around the inverted close so ranges stay valid
        fixed = []
        for c in down_candles:
            mid = c["c"]
            fixed.append({"o": c["o"], "h": mid + 2, "l": mid - 2, "c": mid, "v": c["v"]})
        up = ta_filter({"1d": up_candles}, composite=0.0)
        down = ta_filter({"1d": fixed}, composite=0.0)
        assert up["trend_direction"] == "bullish"
        assert down["trend_direction"] == "bearish"


# ─── Escalation gate / funnel_candidate ──────────────────────────────────────


class TestEscalationGate:
    def test_confirmed_candidate_escalates(self):
        r = funnel_candidate(_load("confirmed_candidate.json"))
        assert r["tier"] == "escalated"
        assert r["escalate"] is True
        assert r["ta"]["verdict"] == "CONFIRMED"
        assert r["bypass_lane"] is None

    def test_rejected_candidate_is_dropped_before_ta(self):
        r = funnel_candidate(_load("rejected_candidate.json"))
        assert r["tier"] == "dropped"
        assert r["surfaced"] is False
        assert r["escalate"] is False
        assert r["ta"] is None  # TA never runs on a dropped candidate

    def test_burst_bypass_escalates_when_gate_and_ta_would_reject(self):
        """Raise every threshold out of reach; only the burst lane can escalate."""
        strict = {"min_composite": 999, "confirmed_threshold": 999, "weak_threshold": 999}
        r = funnel_candidate(_load("burst_candidate.json"), strict)
        assert r["tier"] == "escalated"
        assert r["bypass_lane"] == "burst"
        assert r["ta"]["verdict"] == "REJECTED"

        off = {**strict, "enable_burst_bypass": False, "enable_surfacing_bypass": False}
        r_off = funnel_candidate(_load("burst_candidate.json"), off)
        assert r_off["tier"] == "dropped"

    def test_whale_bypass_escalates_and_toggles(self):
        strict = {"min_composite": 999, "confirmed_threshold": 999, "weak_threshold": 999}
        cand = _load("whale_candidate.json")
        r = funnel_candidate(cand, strict)
        assert r["tier"] == "escalated"
        assert r["bypass_lane"] == "whale"

        no_whale = {k: v for k, v in cand.items() if k != "whale_signal"}
        r_off = funnel_candidate(no_whale, strict)
        assert r_off["tier"] == "dropped"

    def test_surfacing_lane_surfaces_but_does_not_escalate(self):
        """A zero-weight surfacing lane clears the composite gate but still needs CONFIRMED."""
        strict = {
            "min_composite": 999,
            "confirmed_threshold": 999,
            "weak_threshold": 999,
            "enable_burst_bypass": False,
            "enable_whale_bypass": False,
        }
        r = funnel_candidate(_load("confirmed_candidate.json"), strict)
        assert r["surfaced"] is True
        assert "surfacing bypass" in r["surface_reason"]
        assert r["escalate"] is False
        assert r["tier"] == "surfaced"
        assert r["bypass_lane"] is None


# ─── Batch funnel + reporting + CLI ──────────────────────────────────────────


class TestBatchAndCLI:
    def test_batch_summary_counts(self):
        candidates = _load("funnel_candidates.json")
        report = funnel_candidates(candidates)
        s = report["summary"]
        assert s["total_candidates"] == 4
        assert s["escalated_to_llm"] == 3  # confirmed + burst + whale
        assert s["dropped"] == 1  # flat noise
        assert s["llm_call_reduction_pct"] == pytest.approx(25.0)
        # escalated candidates sort ahead of dropped ones
        assert report["results"][0]["tier"] == "escalated"

    def test_render_markdown_contains_header_and_ids(self):
        report = funnel_candidates(_load("funnel_candidates.json"))
        md = render_markdown(report)
        assert "# Signal Funnel Report" in md
        assert "LLM-call reduction" in md
        assert "CONF-UP" in md

    def test_normalize_ohlcv_accepts_list_and_dict(self):
        assert list(normalize_ohlcv([{"c": 1}]).keys()) == ["1d"]
        assert set(normalize_ohlcv({"1h": [{"c": 1}], "4h": []}).keys()) == {"1h"}

    def test_cli_writes_reports_to_output_dir(self, tmp_path, capsys):
        rc = main(
            [
                "--candidates",
                str(FIXTURES / "funnel_candidates.json"),
                "--output-dir",
                str(tmp_path),
            ]
        )
        assert rc == 0
        jsons = list(tmp_path.glob("signal_funnel_*.json"))
        mds = list(tmp_path.glob("signal_funnel_*.md"))
        assert len(jsons) == 1
        assert len(mds) == 1
        report = json.loads(jsons[0].read_text())
        assert report["summary"]["total_candidates"] == 4

    def test_cli_stdout_mode_writes_no_files(self, tmp_path, capsys):
        rc = main(
            [
                "--candidates",
                str(FIXTURES / "confirmed_candidate.json"),
                "--stdout",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        report = json.loads(out)
        assert report["summary"]["total_candidates"] == 1

    def test_cli_missing_file_errors(self, capsys):
        rc = main(["--candidates", "/nonexistent/does_not_exist.json"])
        assert rc == 1
