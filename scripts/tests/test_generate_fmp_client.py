"""Tests for the fmp_client generator (Issue #115)."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def gen():
    path = REPO_ROOT / "scripts" / "generate_fmp_client.py"
    spec = importlib.util.spec_from_file_location("generate_fmp_client", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _skills(gen):
    return gen._load_registry()


def test_registry_has_both_families(gen):
    skills = _skills(gen)
    assert set(skills) == {
        "pead-screener",
        "earnings-trade-analyzer",
        "ibd-distribution-day-monitor",
        "vcp-screener",
        "parabolic-short-trade-planner",
        "ftd-detector",
    }
    for cfg in skills.values():
        # budget (family B) and quote (family A) are mutually exclusive.
        assert cfg.budget == (cfg.family == "B")
        assert cfg.has_quote == (cfg.family == "A")


def test_render_is_idempotent(gen):
    for cfg in _skills(gen).values():
        assert gen.render_fmp_client(cfg) == gen.render_fmp_client(cfg)


def test_no_unsubstituted_tokens(gen):
    for cfg in _skills(gen).values():
        assert "@@" not in gen.render_fmp_client(cfg)


def test_family_b_no_quote_has_budget(gen):
    for cfg in _skills(gen).values():
        if cfg.family != "B":
            continue
        out = gen.render_fmp_client(cfg)
        assert "def get_quote" not in out
        assert '"quote"' not in out  # no quote entry in _FMP_ENDPOINTS
        assert "class ApiCallBudgetExceeded(Exception):" in out
        assert "max_api_calls: int = 200" in out
        assert '"budget_remaining"' in out


def test_family_a_has_quote_no_budget(gen):
    for cfg in _skills(gen).values():
        if cfg.family != "A":
            continue
        out = gen.render_fmp_client(cfg)
        assert "def get_quote(self, symbols: str)" in out
        assert '"quote": [' in out  # quote entry in _FMP_ENDPOINTS
        assert "ApiCallBudgetExceeded" not in out
        assert "max_api_calls" not in out
        assert "budget_remaining" not in out
        assert (
            "def get_historical_prices(self, symbol: str, days: int = 365) -> Optional[dict]" in out
        )


def test_core_upgrade_present(gen):
    # The canonical core is the evolved vcp client; family B inherits its diagnostics.
    for cfg in _skills(gen).values():
        out = gen.render_fmp_client(cfg)
        assert "_warn_fallback" in out
        assert "self._last_error" in out


def test_hist_return_type_per_skill(gen):
    skills = _skills(gen)
    earnings = gen.render_fmp_client(skills["earnings-trade-analyzer"])
    assert (
        "def get_historical_prices(self, symbol: str, days: int = 250) -> Optional[list[dict]]"
        in earnings
    )
    pead = gen.render_fmp_client(skills["pead-screener"])
    assert "def get_historical_prices(self, symbol: str, days: int = 90) -> Optional[dict]" in pead


def test_us_exchanges_only_for_earnings(gen):
    skills = _skills(gen)
    assert "US_EXCHANGES = [" in gen.render_fmp_client(skills["earnings-trade-analyzer"])
    assert "US_EXCHANGES" not in gen.render_fmp_client(skills["pead-screener"])


def test_compat_template_matches_vendored(gen):
    # The byte-match invariant: the canonical compat == each skill's vendored copy.
    compat = gen.render_compat()
    for cfg in _skills(gen).values():
        if not cfg.has_compat:
            continue  # ftd-detector vendors no _fmp_compat.py
        vendored = (REPO_ROOT / "skills" / cfg.skill / "scripts" / "_fmp_compat.py").read_text(
            encoding="utf-8"
        )
        assert vendored == compat


def test_check_passes_against_committed(gen):
    # The committed vendored files must match the generator output (no drift).
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "generate_fmp_client.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_check_detects_drift(gen, tmp_path):
    skills = _skills(gen)
    cfg = next(iter(skills.values()))
    target = REPO_ROOT / "skills" / cfg.skill / "scripts" / "fmp_client.py"
    backup = tmp_path / "fmp_client.py.bak"
    shutil.copy(target, backup)
    try:
        target.write_text(target.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "generate_fmp_client.py"), "--check"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "DRIFT:" in result.stderr
    finally:
        shutil.copy(backup, target)


def test_generated_output_is_ruff_clean(gen):
    """Generated files must already match ruff 0.9.6 formatting (CI pins 0.9.6)."""
    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff not installed")
    version = subprocess.run([ruff, "--version"], capture_output=True, text=True).stdout
    if "0.9.6" not in version:
        pytest.skip(f"ruff 0.9.6 required for a meaningful format check, found: {version.strip()}")
    files = [
        str(REPO_ROOT / "skills" / cfg.skill / "scripts" / "fmp_client.py")
        for cfg in _skills(gen).values()
    ]
    fmt = subprocess.run([ruff, "format", "--check", *files], capture_output=True, text=True)
    assert fmt.returncode == 0, fmt.stdout + fmt.stderr
    chk = subprocess.run([ruff, "check", *files], capture_output=True, text=True)
    assert chk.returncode == 0, chk.stdout + chk.stderr
