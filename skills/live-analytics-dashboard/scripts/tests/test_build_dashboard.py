"""Tests for build_dashboard.py -- view model, CSP-safe rendering, serve tiers."""

import json
import subprocess
import sys
from pathlib import Path

import build_dashboard as bd
import csp_check
import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
WATCHLIST = FIXTURES / "watchlist_report.json"
BREADTH = FIXTURES / "breadth_report.json"
PORTFOLIO = FIXTURES / "portfolio_report.json"
SCRIPT = Path(__file__).resolve().parents[1] / "build_dashboard.py"
REPO_ROOT = Path(__file__).resolve().parents[4]


# ── Layout detection ─────────────────────────────────────────────────────────
class TestDetectLayout:
    def test_watchlist(self):
        assert bd.detect_layout(bd.load_report(WATCHLIST)) == "watchlist"

    def test_breadth(self):
        assert bd.detect_layout(bd.load_report(BREADTH)) == "breadth"

    def test_portfolio(self):
        assert bd.detect_layout(bd.load_report(PORTFOLIO)) == "portfolio"

    def test_bare_list_is_generic(self):
        assert bd.detect_layout([{"x": 1}, {"x": 2}]) == "generic"

    def test_scalar_is_generic(self):
        assert bd.detect_layout("nope") == "generic"


# ── View model ───────────────────────────────────────────────────────────────
class TestViewModel:
    def test_watchlist_shape(self):
        vm = bd.build_view_model(bd.load_report(WATCHLIST))
        assert vm["layout"] == "watchlist"
        assert vm["record_count"] == 3
        assert vm["generated_at"] == "2026-07-03"
        cols = {c["key"]: c["type"] for c in vm["table"]["columns"]}
        assert cols["change_pct"] == "percent"
        assert cols["price"] == "currency"
        assert cols["grade"] == "string"
        # ticker sorts to the front of the columns
        assert vm["table"]["columns"][0]["key"] == "ticker"

    def test_watchlist_kpis_exclude_records(self):
        vm = bd.build_view_model(bd.load_report(WATCHLIST))
        labels = {k["label"] for k in vm["kpis"]}
        assert "Universe Size" in labels
        assert "Avg Score" in labels
        # the record array key and metadata must not become KPIs
        assert "Candidates" not in labels
        assert "Source" not in labels

    def test_portfolio_percent_kpi_direction(self):
        vm = bd.build_view_model(bd.load_report(PORTFOLIO))
        assert vm["layout"] == "portfolio"
        pnl = next(k for k in vm["kpis"] if k["label"] == "Day Pnl Pct")
        assert pnl["direction"] == "positive"
        assert pnl["value"] == "+0.83%"

    def test_breadth_has_no_table_rows(self):
        vm = bd.build_view_model(bd.load_report(BREADTH))
        assert vm["layout"] == "breadth"
        assert vm["record_count"] == 0
        assert vm["table"]["rows"] == []
        assert vm["kpis"], "breadth scalars should surface as KPIs"

    def test_explicit_layout_and_title_override(self):
        vm = bd.build_view_model(bd.load_report(WATCHLIST), layout="generic", title="Custom")
        assert vm["layout"] == "generic"
        assert vm["title"] == "Custom"

    def test_kpi_cap(self):
        big = {f"metric_{i}": i for i in range(20)}
        vm = bd.build_view_model(big)
        assert len(vm["kpis"]) <= bd.MAX_KPIS


# ── Static render + CSP gate ─────────────────────────────────────────────────
class TestRenderStatic:
    def test_static_html_is_csp_clean(self):
        vm = bd.build_view_model(bd.load_report(WATCHLIST))
        html = bd.render_static_html(vm)
        assert csp_check.scan_text(html) == []

    def test_static_html_is_self_contained(self):
        vm = bd.build_view_model(bd.load_report(PORTFOLIO))
        html = bd.render_static_html(vm)
        assert "<script src=" not in html
        assert "const DATA =" in html
        assert "renderDashboard(DATA)" in html

    def test_static_html_escapes_title(self):
        vm = bd.build_view_model(bd.load_report(WATCHLIST), title="<script>x</script>")
        html = bd.render_static_html(vm)
        assert "<title><script>x</script></title>" not in html
        assert "&lt;script&gt;" in html

    def test_fastapi_index_polls(self):
        vm = bd.build_view_model(bd.load_report(WATCHLIST))
        html = bd.render_fastapi_index_html(vm, poll_seconds=15, api_path="/api/data")
        assert csp_check.scan_text(html) == []
        assert "fetch(API" in html
        assert "setInterval(refresh, POLL_MS)" in html
        assert "15000" in html  # 15s -> ms


# ── build_static ─────────────────────────────────────────────────────────────
class TestBuildStatic:
    def test_writes_and_passes_gate(self, tmp_path):
        manifest = bd.build_static(WATCHLIST, tmp_path)
        index = tmp_path / "index.html"
        assert index.exists()
        assert csp_check.scan_file(index) == []
        assert manifest["tier"] == "static"
        assert manifest["artifact_ready"] is True
        assert (tmp_path / "manifest.json").exists()
        assert (tmp_path / "SERVE.md").exists()

    def test_serve_command_uses_convention_port(self, tmp_path):
        manifest = bd.build_static(BREADTH, tmp_path)
        assert str(bd.DEFAULT_PORT) in manifest["serve_command"]


# ── build_fastapi ────────────────────────────────────────────────────────────
class TestBuildFastapi:
    def test_writes_full_bundle(self, tmp_path):
        manifest = bd.build_fastapi(PORTFOLIO, tmp_path, port=8771, poll_seconds=20)
        assert (tmp_path / "static" / "index.html").exists()
        assert (tmp_path / "server" / "main.py").exists()
        assert (tmp_path / "server" / "requirements.txt").exists()
        assert (tmp_path / "server" / "build_dashboard.py").exists()
        assert (tmp_path / "server" / "csp_check.py").exists()
        assert (tmp_path / "start.sh").exists()
        assert manifest["port"] == 8771
        assert manifest["poll_seconds"] == 20

    def test_index_passes_gate(self, tmp_path):
        bd.build_fastapi(WATCHLIST, tmp_path)
        assert csp_check.scan_file(tmp_path / "static" / "index.html") == []

    def test_server_binds_all_interfaces(self, tmp_path):
        bd.build_fastapi(WATCHLIST, tmp_path, port=8772)
        main_py = (tmp_path / "server" / "main.py").read_text()
        assert 'host="0.0.0.0"' in main_py
        assert "/healthz" in main_py
        assert '"/api/data"' in main_py
        assert "8772" in main_py

    def test_server_main_is_valid_python(self, tmp_path):
        bd.build_fastapi(WATCHLIST, tmp_path)
        src = (tmp_path / "server" / "main.py").read_text()
        compile(src, "main.py", "exec")  # syntax-only; does not import fastapi

    def test_start_sh_is_idempotent_and_binds(self, tmp_path):
        bd.build_fastapi(WATCHLIST, tmp_path, port=8773)
        sh = (tmp_path / "start.sh").read_text()
        assert "8773" in sh
        assert "0.0.0.0" in sh
        assert "uvicorn" in sh
        assert "fuser" in sh  # frees the port -> safe to re-run


# ── CSP gate actually fails a build ──────────────────────────────────────────
class TestCspGateFailsBuild:
    def test_build_raises_when_html_is_unsafe(self, tmp_path, monkeypatch):
        # Force the static renderer to emit an inline handler; the gate must reject it.
        def _unsafe(_vm):
            return '<!DOCTYPE html><html><body><button onclick="x()">go</button></body></html>'

        monkeypatch.setattr(bd, "render_static_html", _unsafe)
        with pytest.raises(csp_check.CspViolationError):
            bd.build_static(WATCHLIST, tmp_path)


# ── CLI ──────────────────────────────────────────────────────────────────────
class TestCli:
    def test_static_cli_build(self, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--report",
                str(WATCHLIST),
                "--tier",
                "static",
                "--output-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "index.html").exists()
        assert json.loads(result.stdout)["tier"] == "static"

    def test_spa_tier_refused(self, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--report",
                str(WATCHLIST),
                "--tier",
                "spa",
                "--output-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 2
        assert "SPA" in result.stderr

    def test_missing_report(self, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--report",
                str(tmp_path / "nope.json"),
                "--output-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 1
