"""Tests for the --max-slippage-bps liquidity gate wired into position_sizer.py."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from position_sizer import (
    SizingParameters,
    calculate_position,
    generate_markdown_report,
    load_liquidity_json,
    validate_parameters,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = "skills/position-sizer/scripts/position_sizer.py"


# ─── Slippage cap logic ──────────────────────────────────────────────────────


class TestSlippageGate:
    def test_slippage_caps_position(self):
        """Risk size (153) exceeds the 9 bps budget -> capped to 90 shares."""
        params = SizingParameters(
            account_size=100_000,
            entry_price=155.0,
            stop_price=148.50,
            risk_pct=1.0,
            max_slippage_bps=9.0,
            adv=100_000,
            daily_volatility=0.03,
        )
        result = calculate_position(params)
        assert result["final_recommended_shares"] == 90
        assert result["binding_constraint"] == "max_slippage_bps"
        assert result["slippage"]["capped"] is True
        # final slippage must respect the budget
        assert result["slippage"]["final_shares_slippage_bps"] <= 9.0

    def test_generous_budget_not_binding(self):
        """A 50 bps budget leaves the 153-share risk size untouched."""
        params = SizingParameters(
            account_size=100_000,
            entry_price=155.0,
            stop_price=148.50,
            risk_pct=1.0,
            max_slippage_bps=50.0,
            adv=100_000,
            daily_volatility=0.03,
        )
        result = calculate_position(params)
        assert result["final_recommended_shares"] == 153
        assert result["slippage"]["capped"] is False
        assert result["binding_constraint"] != "max_slippage_bps"

    def test_slippage_block_absent_without_inputs(self):
        """No adv/volatility supplied -> no slippage block, no cap."""
        params = SizingParameters(
            account_size=100_000,
            entry_price=155.0,
            stop_price=148.50,
            risk_pct=1.0,
            max_slippage_bps=9.0,  # budget alone is insufficient
        )
        result = calculate_position(params)
        assert "slippage" not in result
        assert result["final_recommended_shares"] == 153

    def test_slippage_reported_when_not_capping(self):
        """Slippage detail is still computed for reporting when budget is loose."""
        params = SizingParameters(
            account_size=100_000,
            entry_price=155.0,
            stop_price=148.50,
            risk_pct=1.0,
            max_slippage_bps=50.0,
            adv=100_000,
            daily_volatility=0.03,
        )
        result = calculate_position(params)
        assert result["slippage"]["risk_shares"] == 153
        assert result["slippage"]["risk_shares_slippage_bps"] > 0

    def test_slippage_and_other_constraints_strictest_wins(self):
        """max_position_pct (64) is tighter than the slippage cap (90) here."""
        params = SizingParameters(
            account_size=100_000,
            entry_price=155.0,
            stop_price=148.50,
            risk_pct=1.0,
            max_position_pct=10.0,
            max_slippage_bps=9.0,
            adv=100_000,
            daily_volatility=0.03,
        )
        result = calculate_position(params)
        assert result["final_recommended_shares"] == 64
        assert result["binding_constraint"] == "max_position_pct"


# ─── Validation ──────────────────────────────────────────────────────────────


class TestSlippageValidation:
    def test_negative_slippage_budget(self):
        params = SizingParameters(
            account_size=100_000,
            entry_price=155.0,
            stop_price=148.50,
            risk_pct=1.0,
            max_slippage_bps=-1.0,
        )
        with pytest.raises(ValueError, match="max_slippage_bps must be positive"):
            validate_parameters(params)

    def test_negative_adv(self):
        params = SizingParameters(
            account_size=100_000, entry_price=155.0, stop_price=148.50, risk_pct=1.0, adv=-5.0
        )
        with pytest.raises(ValueError, match="adv must be positive"):
            validate_parameters(params)

    def test_negative_daily_volatility(self):
        params = SizingParameters(
            account_size=100_000,
            entry_price=155.0,
            stop_price=148.50,
            risk_pct=1.0,
            daily_volatility=-0.1,
        )
        with pytest.raises(ValueError, match="daily_volatility must be positive"):
            validate_parameters(params)


# ─── liquidity_check JSON consumption ────────────────────────────────────────


class TestLiquidityJson:
    def test_first_entry_default(self):
        adv, vol = load_liquidity_json(str(FIXTURES / "liquidity_output.json"))
        assert adv == 100_000
        assert vol == 0.03

    def test_ticker_match(self):
        adv, vol = load_liquidity_json(str(FIXTURES / "liquidity_output.json"), ticker="OTHER")
        assert adv == 5_000_000
        assert vol == 0.015


# ─── Markdown ────────────────────────────────────────────────────────────────


class TestMarkdown:
    def test_slippage_section_rendered(self):
        params = SizingParameters(
            account_size=100_000,
            entry_price=155.0,
            stop_price=148.50,
            risk_pct=1.0,
            max_slippage_bps=9.0,
            adv=100_000,
            daily_volatility=0.03,
        )
        md = generate_markdown_report(calculate_position(params))
        assert "## Liquidity / Slippage" in md
        assert "Position capped by slippage budget" in md


# ─── CLI end-to-end ──────────────────────────────────────────────────────────


class TestCLI:
    def test_cli_slippage_flags(self, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                SCRIPT,
                "--account-size",
                "100000",
                "--entry",
                "155",
                "--stop",
                "148.50",
                "--risk-pct",
                "1.0",
                "--max-slippage-bps",
                "9.0",
                "--adv",
                "100000",
                "--daily-volatility",
                "0.03",
                "--output-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, result.stderr
        assert "CAPPED" in result.stdout
        json_files = list(tmp_path.glob("position_sizer_*.json"))
        report = json.loads(json_files[0].read_text())
        assert report["final_recommended_shares"] == 90

    def test_cli_liquidity_json(self, tmp_path):
        """--liquidity-json supplies adv/volatility from a liquidity_check report."""
        result = subprocess.run(
            [
                sys.executable,
                SCRIPT,
                "--account-size",
                "100000",
                "--entry",
                "155",
                "--stop",
                "148.50",
                "--risk-pct",
                "1.0",
                "--max-slippage-bps",
                "9.0",
                "--liquidity-json",
                str(FIXTURES / "liquidity_output.json"),
                "--ticker",
                "TESTCO",
                "--output-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, result.stderr
        json_files = list(tmp_path.glob("position_sizer_*.json"))
        report = json.loads(json_files[0].read_text())
        # TESTCO adv=100k, vol=0.03, budget 9 bps -> capped to 90
        assert report["final_recommended_shares"] == 90
        assert report["slippage"]["capped"] is True
