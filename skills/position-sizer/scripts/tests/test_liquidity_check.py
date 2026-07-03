"""Tests for liquidity_check.py — pre-trade liquidity / execution-cost model."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from liquidity_check import (
    analyze_ticker,
    build_report,
    compare_tickers,
    compute_amihud,
    compute_spread,
    compute_turnover,
    daily_volatility,
    estimate_slippage_bps,
    generate_markdown_report,
    grade_liquidity,
    grade_slippage,
    max_shares_under_slippage,
    volume_cv,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = "skills/position-sizer/scripts/liquidity_check.py"


def load_bars():
    with open(FIXTURES / "bars_sample.json") as f:
        return json.load(f)


# ─── Spread ──────────────────────────────────────────────────────────────────


class TestSpread:
    def test_basic_spread(self):
        r = compute_spread(99.9, 100.1)
        assert r["spread"] == 0.2
        assert r["spread_pct"] == 0.2
        assert r["spread_bps"] == 20.0

    def test_missing_quote(self):
        r = compute_spread(None, None)
        assert r["spread"] is None and r["spread_bps"] is None

    def test_crossed_or_zero_quote(self):
        assert compute_spread(0, 100)["spread"] is None
        assert compute_spread(101, 100)["spread"] is None  # ask < bid


# ─── Square-root market impact ───────────────────────────────────────────────


class TestSlippage:
    def test_known_impact(self):
        # sigma=0.02, order=10k, ADV=1M -> 0.02 * sqrt(0.01) * 1e4 = 20 bps
        assert estimate_slippage_bps(10_000, 1_000_000, 0.02) == pytest.approx(20.0)

    def test_impact_zero_guards(self):
        assert estimate_slippage_bps(0, 1_000_000, 0.02) == 0.0
        assert estimate_slippage_bps(10_000, 0, 0.02) == 0.0
        assert estimate_slippage_bps(10_000, 1_000_000, 0) == 0.0

    def test_inverse_matches(self):
        # max_shares for a 20 bps budget should reproduce the 10k order above
        assert max_shares_under_slippage(1_000_000, 0.02, 20.0) == 10_000

    def test_inverse_guards(self):
        assert max_shares_under_slippage(0, 0.02, 20.0) is None
        assert max_shares_under_slippage(1_000_000, 0, 20.0) is None
        assert max_shares_under_slippage(1_000_000, 0.02, 0) is None

    def test_impact_concavity(self):
        # doubling order size raises impact by ~sqrt(2), not 2x
        small = estimate_slippage_bps(10_000, 1_000_000, 0.02)
        big = estimate_slippage_bps(20_000, 1_000_000, 0.02)
        assert big == pytest.approx(small * (2**0.5))


# ─── Amihud / volatility / CV ────────────────────────────────────────────────


class TestSeriesMetrics:
    def test_amihud_positive(self):
        amihud = compute_amihud([100.0, 101.0, 100.0], [1000.0, 1000.0, 1000.0])
        assert amihud is not None and amihud > 0

    def test_amihud_needs_two_points(self):
        assert compute_amihud([100.0], [1000.0]) is None

    def test_amihud_higher_for_thin_volume(self):
        thick = compute_amihud([100.0, 101.0, 100.0], [1_000_000.0] * 3)
        thin = compute_amihud([100.0, 101.0, 100.0], [1_000.0] * 3)
        assert thin > thick

    def test_daily_volatility(self):
        assert daily_volatility([100.0]) is None
        assert daily_volatility([100.0, 102.0, 101.0, 103.0]) > 0

    def test_volume_cv(self):
        assert volume_cv([100.0, 100.0]) == 0.0
        assert volume_cv([50.0, 150.0]) > 0
        assert volume_cv([100.0]) is None


# ─── Turnover ────────────────────────────────────────────────────────────────


class TestTurnover:
    def test_float_preferred(self):
        r = compute_turnover(1000, float_shares=100_000, shares_outstanding=500_000)
        assert r["turnover_base"] == "float"
        assert r["turnover_ratio_daily"] == 0.01
        assert r["turnover_annualized_pct"] == 252.0
        assert r["days_to_trade_float"] == 100.0

    def test_falls_back_to_shares_outstanding(self):
        r = compute_turnover(1000, float_shares=None, shares_outstanding=200_000)
        assert r["turnover_base"] == "shares_outstanding"

    def test_no_base(self):
        r = compute_turnover(1000, None, None)
        assert r["turnover_ratio_daily"] is None


# ─── Grading ─────────────────────────────────────────────────────────────────


class TestGrading:
    def test_slippage_grades(self):
        assert grade_slippage(5) == "minimal"
        assert grade_slippage(25) == "low"
        assert grade_slippage(50) == "moderate"
        assert grade_slippage(100) == "high"
        assert grade_slippage(150) == "severe"
        assert grade_slippage(None) is None

    def test_liquidity_grades(self):
        assert grade_liquidity(6e8, 0.02, 0.005) == "very_high"
        assert grade_liquidity(1e8, 0.05, 0.05) == "high"
        assert grade_liquidity(1e7, 0.2, 0.5) == "moderate"
        assert grade_liquidity(1e6, 0.5, 0.5) == "low"
        assert grade_liquidity(1e5, 3.0, 50.0) == "very_low"
        assert grade_liquidity(None, None, None) == "unknown"

    def test_amihud_downgrade(self):
        # moderate ($1e7 dollar volume) downgraded to low by illiquid Amihud
        assert grade_liquidity(1e7, 0.1, 5.0) == "low"

    def test_wide_spread_downgrade(self):
        assert grade_liquidity(1e7, 3.0, 0.1) == "low"


# ─── analyze_ticker (fixtures) ───────────────────────────────────────────────


class TestAnalyzeTicker:
    def test_liquid_name(self):
        data = load_bars()
        r = analyze_ticker(
            "BIGCAP", data["BIGCAP"]["bars"], quote=data["BIGCAP"]["quote"], order_shares=10_000
        )
        assert r["liquidity_grade"] == "very_high"
        assert r["slippage_grade"] == "minimal"
        assert r["warnings"] == []
        assert r["turnover_base"] == "float"

    def test_illiquid_name(self):
        data = load_bars()
        r = analyze_ticker(
            "SMALLCAP",
            data["SMALLCAP"]["bars"],
            quote=data["SMALLCAP"]["quote"],
            order_shares=10_000,
        )
        assert r["liquidity_grade"] == "very_low"
        assert any("micro-cap" in w for w in r["warnings"])
        assert any("wide spread" in w for w in r["warnings"])

    def test_liquid_has_lower_slippage_than_illiquid(self):
        data = load_bars()
        liquid = analyze_ticker(
            "BIGCAP", data["BIGCAP"]["bars"], quote=data["BIGCAP"]["quote"], order_shares=10_000
        )
        illiquid = analyze_ticker(
            "SMALLCAP",
            data["SMALLCAP"]["bars"],
            quote=data["SMALLCAP"]["quote"],
            order_shares=10_000,
        )
        assert illiquid["estimated_slippage_bps"] > liquid["estimated_slippage_bps"]

    def test_order_dollars_conversion(self):
        data = load_bars()
        r = analyze_ticker(
            "BIGCAP", data["BIGCAP"]["bars"], quote=data["BIGCAP"]["quote"], order_dollars=100_000
        )
        # $100k at ~$100 current price -> ~1000 shares
        assert r["order_shares"] == pytest.approx(1000, abs=5)

    def test_no_order_size_no_slippage(self):
        data = load_bars()
        r = analyze_ticker("BIGCAP", data["BIGCAP"]["bars"], quote=data["BIGCAP"]["quote"])
        assert r["estimated_slippage_bps"] is None

    def test_empty_bars_error(self):
        r = analyze_ticker("EMPTY", [], quote={})
        assert "error" in r


# ─── Comparison / report ─────────────────────────────────────────────────────


class TestReport:
    def test_comparison_ranking(self):
        data = load_bars()
        results = [
            analyze_ticker(tk, e["bars"], quote=e["quote"], order_shares=10_000)
            for tk, e in data.items()
        ]
        c = compare_tickers(results)
        assert c["most_liquid"] == "BIGCAP"
        assert c["least_liquid"] == "SMALLCAP"

    def test_comparison_needs_two(self):
        data = load_bars()
        single = [analyze_ticker("BIGCAP", data["BIGCAP"]["bars"], quote=data["BIGCAP"]["quote"])]
        assert compare_tickers(single) is None

    def test_build_report_serializable(self):
        data = load_bars()
        results = [analyze_ticker(tk, e["bars"], quote=e["quote"]) for tk, e in data.items()]
        report = build_report(results, None, None)
        assert report["schema_version"] == "1.0"
        json.dumps(report)  # must not raise

    def test_markdown_sections(self):
        data = load_bars()
        results = [
            analyze_ticker(tk, e["bars"], quote=e["quote"], order_shares=10_000)
            for tk, e in data.items()
        ]
        md = generate_markdown_report(build_report(results, 10_000, None))
        assert "# Pre-Trade Liquidity Check" in md
        assert "## BIGCAP" in md
        assert "## Comparison" in md


# ─── CLI (offline via --bars-json) ───────────────────────────────────────────


class TestCLI:
    def test_cli_bars_json(self, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                SCRIPT,
                "BIGCAP",
                "SMALLCAP",
                "--bars-json",
                str(FIXTURES / "bars_sample.json"),
                "--order-shares",
                "10000",
                "--output-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, result.stderr
        json_files = list(tmp_path.glob("liquidity_check_*.json"))
        assert json_files, "no JSON report written"
        report = json.loads(json_files[0].read_text())
        grades = {t["ticker"]: t["liquidity_grade"] for t in report["tickers"]}
        assert grades["BIGCAP"] == "very_high"
        assert grades["SMALLCAP"] == "very_low"

    def test_cli_requires_ticker(self, tmp_path):
        result = subprocess.run(
            [sys.executable, SCRIPT, "--output-dir", str(tmp_path)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0
