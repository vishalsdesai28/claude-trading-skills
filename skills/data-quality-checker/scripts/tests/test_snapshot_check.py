"""Tests for the verified-snapshot cross-check in check_data_quality.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from check_data_quality import check_snapshot, run_checks

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..")


def _snapshot(
    latest_close: float = 150.0,
    recent_low: float = 140.0,
    recent_high: float = 160.0,
    atr: float | None = 3.0,
    latest_date: str = "2026-01-14",
) -> dict:
    return {
        "symbol": "TEST",
        "analysis_date": latest_date,
        "latest_row": {
            "date": latest_date,
            "open": latest_close,
            "high": recent_high,
            "low": recent_low,
            "close": latest_close,
            "volume": 1_000_000,
        },
        "indicators": {"atr": atr},
        "recent_closes": [{"date": latest_date, "close": latest_close}],
        "recent_high": recent_high,
        "recent_low": recent_low,
        "guardrail": "Treat this snapshot as the single source of truth.",
    }


class TestCheckSnapshot:
    def test_no_snapshot_is_noop(self):
        assert check_snapshot("support at $50", None) == []
        assert check_snapshot("support at $50", {}) == []

    def test_support_level_out_of_band_flagged(self):
        # Band = [140 - 3*3, 160 + 3*3] = [131, 169]. $50 is far outside.
        content = "We expect support at $50 to hold."
        findings = check_snapshot(content, _snapshot())
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "WARNING"
        assert f.category == "snapshot"
        assert "support" in f.message.lower()
        assert "$50" in f.message

    def test_resistance_level_within_band_ok(self):
        content = "Resistance at $158 caps the move."
        findings = check_snapshot(content, _snapshot())
        assert findings == []

    def test_price_target_out_of_band_flagged(self):
        content = "Our price target is $900."
        findings = check_snapshot(content, _snapshot())
        assert len(findings) == 1
        assert findings[0].category == "snapshot"

    def test_current_price_gross_mismatch_is_error(self):
        # $300 vs verified close $150 -> 100% off -> ERROR.
        content = "The stock is trading at $300."
        findings = check_snapshot(content, _snapshot())
        assert len(findings) == 1
        assert findings[0].severity == "ERROR"
        assert "current price" in findings[0].message.lower()

    def test_current_price_moderate_mismatch_is_warning(self):
        # $165 vs $150 -> 10% off -> WARNING.
        content = "It closed at $165 yesterday."
        findings = check_snapshot(content, _snapshot())
        assert len(findings) == 1
        assert findings[0].severity == "WARNING"

    def test_current_price_within_tolerance_ok(self):
        content = "The stock is trading at $151."
        findings = check_snapshot(content, _snapshot())
        assert findings == []

    def test_band_falls_back_to_pct_when_atr_missing(self):
        snap = _snapshot(atr=None)
        # pad = 0.15 * 150 = 22.5 -> band [117.5, 182.5]. $50 outside.
        findings = check_snapshot("support at $50", snap)
        assert len(findings) == 1

    def test_japanese_support_level(self):
        content = "サポートは $40 付近。"
        findings = check_snapshot(content, _snapshot())
        assert len(findings) == 1
        assert findings[0].category == "snapshot"


class TestRunChecksWiring:
    def test_snapshot_check_noop_without_snapshot(self):
        """Default run_checks (no snapshot) must not emit snapshot findings."""
        findings = run_checks("Our price target is $900.")
        assert not any(f.category == "snapshot" for f in findings)

    def test_snapshot_check_runs_when_provided(self):
        findings = run_checks(
            "Our price target is $900.",
            checks=["snapshot"],
            snapshot=_snapshot(),
        )
        assert any(f.category == "snapshot" for f in findings)


class TestCliSnapshot:
    def test_cli_snapshot_flag(self):
        report = "The stock is trading at $300 with support at $50.\n"
        snap = _snapshot()
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = os.path.join(tmpdir, "report.md")
            snap_path = os.path.join(tmpdir, "snap.json")
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(report)
            with open(snap_path, "w", encoding="utf-8") as f:
                json.dump(snap, f)

            result = subprocess.run(
                [
                    sys.executable,
                    os.path.join(SCRIPTS_DIR, "check_data_quality.py"),
                    "--file",
                    report_path,
                    "--snapshot",
                    snap_path,
                    "--output-dir",
                    tmpdir,
                ],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0
            # JSON report should contain snapshot-category findings.
            json_reports = [
                p
                for p in os.listdir(tmpdir)
                if p.startswith("data_quality_") and p.endswith(".json")
            ]
            assert json_reports
            with open(os.path.join(tmpdir, json_reports[0]), encoding="utf-8") as f:
                findings = json.load(f)
            assert any(fi["category"] == "snapshot" for fi in findings)

    def test_cli_missing_snapshot_file_exits_1(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = os.path.join(tmpdir, "report.md")
            with open(report_path, "w", encoding="utf-8") as f:
                f.write("clean report\n")
            result = subprocess.run(
                [
                    sys.executable,
                    os.path.join(SCRIPTS_DIR, "check_data_quality.py"),
                    "--file",
                    report_path,
                    "--snapshot",
                    os.path.join(tmpdir, "does_not_exist.json"),
                    "--output-dir",
                    tmpdir,
                ],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 1
