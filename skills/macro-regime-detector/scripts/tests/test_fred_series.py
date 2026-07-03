"""Offline tests for fred_series.py (FRED macro grounding).

All network is avoided by injecting a fixture-loading ``fetch_fn`` or by
monkeypatching ``fred_series.fetch_series_payload``; the saved payloads live in
tests/fixtures/<SERIES_ID>.json and mirror ``fetch_series_payload``'s shape.
"""

import json
import os

import fred_series
import pytest

FIX_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

GROUNDING_INDICATORS = [
    "yield_curve",
    "cpi",
    "core_pce",
    "fed_funds_rate",
    "unemployment",
]


def load_fixture(series_id, start_date=None, end_date=None, api_key=None):
    """A fetch_fn stand-in: return the saved payload for a series ID."""
    path = os.path.join(FIX_DIR, f"{series_id}.json")
    if not os.path.exists(path):
        raise ValueError(f"FRED series '{series_id}' not found (no fixture)")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------------------- #
# resolve_series_id
# --------------------------------------------------------------------------- #


class TestResolveSeriesId:
    def test_task_required_aliases(self):
        assert fred_series.resolve_series_id("fed_funds_rate") == "FEDFUNDS"
        assert fred_series.resolve_series_id("yield_curve") == "T10Y2Y"
        assert fred_series.resolve_series_id("cpi") == "CPIAUCSL"
        assert fred_series.resolve_series_id("core_pce") == "PCEPILFE"
        assert fred_series.resolve_series_id("unemployment") == "UNRATE"
        assert fred_series.resolve_series_id("ust2y") == "DGS2"
        assert fred_series.resolve_series_id("ust10y") == "DGS10"
        assert fred_series.resolve_series_id("ust30y") == "DGS30"

    def test_alias_is_case_and_separator_insensitive(self):
        assert fred_series.resolve_series_id("Yield-Curve") == "T10Y2Y"
        assert fred_series.resolve_series_id(" FED FUNDS RATE ") == "FEDFUNDS"

    def test_raw_series_id_passthrough(self):
        assert fred_series.resolve_series_id("CPIAUCSL") == "CPIAUCSL"
        assert fred_series.resolve_series_id("dgs10") == "DGS10"

    def test_descriptive_phrase_rejected(self):
        with pytest.raises(ValueError):
            fred_series.resolve_series_id("bank of japan policy rate outlook")


# --------------------------------------------------------------------------- #
# get_api_key / graceful degradation
# --------------------------------------------------------------------------- #


class TestApiKey:
    def test_missing_key_raises_and_is_valueerror(self, monkeypatch):
        monkeypatch.delenv("FRED_API_KEY", raising=False)
        with pytest.raises(fred_series.FredNotConfiguredError):
            fred_series.get_api_key()
        # Must remain a ValueError so the detector's `except ValueError` degrades.
        assert issubclass(fred_series.FredNotConfiguredError, ValueError)

    def test_explicit_key_wins(self, monkeypatch):
        monkeypatch.delenv("FRED_API_KEY", raising=False)
        assert fred_series.get_api_key("abc123") == "abc123"

    def test_env_key_used(self, monkeypatch):
        monkeypatch.setenv("FRED_API_KEY", "env_key")
        assert fred_series.get_api_key() == "env_key"


# --------------------------------------------------------------------------- #
# parse_series
# --------------------------------------------------------------------------- #


class TestParseSeries:
    def test_level_series_row_cap_and_gap_cleaning(self):
        payload = load_fixture("T10Y2Y")
        summary = fred_series.parse_series("T10Y2Y", payload)  # default max_rows=40
        # 50 daily obs, one is "." -> 49 clean points.
        assert summary["observations"] == 49
        assert summary["rows_truncated"] is True
        assert len(summary["rows"]) == 40  # row cap keeps context bounded
        assert summary["is_level"] is True
        # Spread level: percent-of-base change is left None (base crosses zero).
        assert summary["change_pct"] is None
        assert summary["latest"] == 0.22
        assert summary["first"] == -0.30
        assert summary["change_abs"] == pytest.approx(0.52)
        assert summary["trend"] == "rising"
        assert summary["units"] == "%"

    def test_max_rows_configurable(self):
        payload = load_fixture("T10Y2Y")
        summary = fred_series.parse_series("T10Y2Y", payload, max_rows=5)
        assert len(summary["rows"]) == 5
        # Row cap keeps the most-recent observations (ascending -> tail).
        assert summary["rows"][-1]["value"] == pytest.approx(0.22)

    def test_index_series_yoy_percent(self):
        payload = load_fixture("CPIAUCSL")
        summary = fred_series.parse_series("CPIAUCSL", payload)
        assert summary["observations"] == 13
        assert summary["rows_truncated"] is False
        assert summary["is_level"] is False
        assert summary["change_pct"] == pytest.approx(2.9, abs=0.05)
        assert summary["trend"] == "rising"

    def test_falling_level_series(self):
        summary = fred_series.parse_series("FEDFUNDS", load_fixture("FEDFUNDS"))
        assert summary["change_abs"] == pytest.approx(-1.0)
        assert summary["trend"] == "falling"
        assert summary["change_pct"] is None

    def test_empty_observations(self):
        summary = fred_series.parse_series("XYZ", {"meta": {}, "observations": []})
        assert summary["observations"] == 0
        assert summary["latest"] is None
        assert summary["trend"] == "unknown"
        assert summary["rows"] == []


# --------------------------------------------------------------------------- #
# get_macro_series (window + fetch injection)
# --------------------------------------------------------------------------- #


class TestGetMacroSeries:
    def test_uses_alias_and_window(self):
        captured = {}

        def spy_fetch(series_id, start_date, end_date, api_key=None):
            captured["series_id"] = series_id
            captured["start"] = start_date
            captured["end"] = end_date
            return load_fixture(series_id)

        summary = fred_series.get_macro_series(
            "yield_curve",
            "2026-06-30",
            look_back_days=365,
            api_key="k",
            fetch_fn=spy_fetch,
        )
        assert captured["series_id"] == "T10Y2Y"
        assert captured["end"] == "2026-06-30"
        assert captured["start"] == "2025-06-30"  # 365d trailing window
        assert summary["series_id"] == "T10Y2Y"


# --------------------------------------------------------------------------- #
# build_macro_grounding
# --------------------------------------------------------------------------- #


class TestBuildMacroGrounding:
    def test_available_with_fixtures(self):
        grounding = fred_series.build_macro_grounding(
            curr_date="2026-06-30",
            indicators=GROUNDING_INDICATORS,
            api_key="k",
            fetch_fn=load_fixture,
        )
        assert grounding["available"] is True
        assert set(grounding["series"]) == set(GROUNDING_INDICATORS)
        assert grounding["errors"] == {}
        assert grounding["series"]["yield_curve"]["latest"] == 0.22

    def test_missing_key_degrades_gracefully(self, monkeypatch):
        monkeypatch.delenv("FRED_API_KEY", raising=False)
        grounding = fred_series.build_macro_grounding(
            curr_date="2026-06-30",
            indicators=GROUNDING_INDICATORS,
            fetch_fn=load_fixture,  # never reached: key check happens first
        )
        assert grounding["available"] is False
        assert "FRED_API_KEY" in grounding["reason"]
        assert grounding["series"] == {}

    def test_bad_indicator_recorded_not_fatal(self):
        grounding = fred_series.build_macro_grounding(
            curr_date="2026-06-30",
            indicators=["cpi", "no_such_series_id"],
            api_key="k",
            fetch_fn=load_fixture,
        )
        assert grounding["available"] is True
        assert "cpi" in grounding["series"]
        assert "no_such_series_id" in grounding["errors"]


# --------------------------------------------------------------------------- #
# anchor_regime (feeding prints into the classification output)
# --------------------------------------------------------------------------- #


class TestAnchorRegime:
    def _grounding(self):
        return fred_series.build_macro_grounding(
            curr_date="2026-06-30",
            indicators=GROUNDING_INDICATORS,
            api_key="k",
            fetch_fn=load_fixture,
        )

    def test_anchors_without_mutating_input(self):
        regime = {"current_regime": "contraction", "regime_label": "Contraction"}
        anchored = fred_series.anchor_regime(regime, self._grounding())
        # Original untouched.
        assert "macro_grounding" not in regime
        block = anchored["macro_grounding"]
        assert block["available"] is True
        assert "yield_curve" in block["prints"]
        assert block["prints"]["fed_funds_rate"]["latest"] == pytest.approx(4.33)
        assert "10Y-2Y spread" in block["summary"]

    def test_contraction_consistency_notes(self):
        regime = {"current_regime": "contraction"}
        anchored = fred_series.anchor_regime(regime, self._grounding())
        notes = " ".join(anchored["macro_grounding"]["consistency_notes"])
        # Rising unemployment + easing policy rate are surfaced.
        assert "Unemployment rising" in notes
        assert "Policy rate easing" in notes

    def test_inverted_curve_and_elevated_inflation_synthetic(self):
        # Synthetic grounding: inverted curve + hot core PCE, Inflationary regime.
        grounding = {
            "available": True,
            "curr_date": "2026-06-30",
            "series": {
                "yield_curve": {
                    "latest": -0.45,
                    "latest_date": "2026-06-30",
                    "units": "%",
                    "change_abs": -0.10,
                    "change_pct": None,
                    "trend": "falling",
                },
                "core_pce": {
                    "latest": 130.0,
                    "latest_date": "2026-06-01",
                    "units": "Index",
                    "change_abs": 4.2,
                    "change_pct": 3.5,
                    "trend": "rising",
                },
            },
        }
        anchored = fred_series.anchor_regime({"current_regime": "inflationary"}, grounding)
        notes = " ".join(anchored["macro_grounding"]["consistency_notes"])
        assert "inverted" in notes.lower()
        assert "Inflation elevated" in notes
        assert "supports the Inflationary regime" in notes

    def test_unavailable_grounding_leaves_regime_intact(self):
        regime = {"current_regime": "broadening", "regime_label": "Broadening"}
        anchored = fred_series.anchor_regime(
            regime, {"available": False, "reason": "FRED_API_KEY is not set."}
        )
        assert anchored["current_regime"] == "broadening"
        assert anchored["macro_grounding"]["available"] is False
        assert "FRED_API_KEY" in anchored["macro_grounding"]["reason"]

    def test_none_grounding_is_safe(self):
        anchored = fred_series.anchor_regime({"current_regime": "transitional"}, None)
        assert anchored["macro_grounding"]["available"] is False


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


class TestRendering:
    def test_series_markdown_has_truncation_note(self):
        summary = fred_series.parse_series("T10Y2Y", load_fixture("T10Y2Y"))
        md = fred_series.render_series_markdown(summary)
        assert "T10Y2Y" in md
        assert "showing the most recent 40 of 49" in md
        assert "| Date | Value |" in md

    def test_grounding_markdown_available(self):
        grounding = fred_series.build_macro_grounding(
            curr_date="2026-06-30",
            indicators=GROUNDING_INDICATORS,
            api_key="k",
            fetch_fn=load_fixture,
        )
        md = fred_series.render_grounding_markdown(grounding)
        assert "# FRED Macro Grounding" in md
        assert "Current Prints" in md
        assert "T10Y2Y" in md

    def test_grounding_markdown_unavailable(self):
        md = fred_series.render_grounding_markdown(
            {"available": False, "reason": "FRED_API_KEY is not set."}
        )
        assert "unavailable" in md.lower()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


class TestCli:
    def test_main_writes_reports(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRED_API_KEY", "test_key")
        monkeypatch.setattr(fred_series, "fetch_series_payload", load_fixture)
        rc = fred_series.main(
            [
                "--series",
                *GROUNDING_INDICATORS,
                "--as-of",
                "2026-06-30",
                "--output-dir",
                str(tmp_path),
            ]
        )
        assert rc == 0
        written = list(tmp_path.iterdir())
        assert any(p.suffix == ".json" for p in written)
        assert any(p.suffix == ".md" for p in written)
        json_file = next(p for p in written if p.suffix == ".json")
        data = json.loads(json_file.read_text())
        assert data["available"] is True
        assert data["series"]["cpi"]["change_pct"] == pytest.approx(2.9, abs=0.05)

    def test_main_missing_key_returns_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("FRED_API_KEY", raising=False)
        rc = fred_series.main(["--output-dir", str(tmp_path)])
        assert rc == 1
        assert "FRED_API_KEY" in capsys.readouterr().err
        # Nothing written on failure.
        assert list(tmp_path.iterdir()) == []
