"""Tests for value_company.py — DCF engine, WACC, relative/SOTP, sensitivity, guardrails.

All tests are network-free: pure functions use hand-calculable inputs, and the integration
tests drive the committed ACME fixture through normalize_fmp + value_company.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from value_company import (
    DEFAULT_ERP,
    after_tax_cost_of_debt,
    blend_fair_value,
    build_growth_path,
    clamp_tax_rate,
    compute_wacc,
    cost_of_equity,
    default_beta_for,
    discount_cash_flows,
    discount_terminal,
    evaluate_guardrails,
    generate_markdown_report,
    normalize_fmp,
    peer_median_multiples,
    project_fcff,
    relative_valuation,
    route_sector_method,
    rule_of_40,
    run_dcf,
    sensitivity_grid,
    size_premium_for,
    sotp_valuation,
    terminal_value_exit,
    terminal_value_gordon,
    value_company,
)

REPO_ROOT = Path(__file__).resolve().parents[4]


# ─── WACC / CAPM ─────────────────────────────────────────────────────────────


class TestWACC:
    def test_cost_of_equity(self):
        # 0.045 + 1.0 * 0.055 + 0 = 0.10
        assert cost_of_equity(0.045, 1.0, 0.055) == pytest.approx(0.10)

    def test_cost_of_equity_with_size_premium(self):
        assert cost_of_equity(0.04, 1.2, 0.05, 0.02) == pytest.approx(0.04 + 0.06 + 0.02)

    def test_after_tax_cost_of_debt(self):
        assert after_tax_cost_of_debt(0.05, 0.25) == pytest.approx(0.0375)

    def test_compute_wacc(self):
        # E=80, D=20, ke=0.10, kd_at=0.04 -> 0.8*0.10 + 0.2*0.04 = 0.088
        w = compute_wacc(80, 20, 0.10, 0.04)
        assert w["wacc"] == pytest.approx(0.088)
        assert w["equity_weight"] == pytest.approx(0.8)
        assert w["debt_weight"] == pytest.approx(0.2)

    def test_compute_wacc_zero_value_raises(self):
        with pytest.raises(ValueError):
            compute_wacc(0, 0, 0.10, 0.04)

    def test_size_premium_tiers(self):
        assert size_premium_for(50_000_000_000) == 0.0
        assert size_premium_for(5_000_000_000) == 0.0075
        assert size_premium_for(1_000_000_000) == 0.02
        assert size_premium_for(300_000_000) == 0.03
        assert size_premium_for(50_000_000) == 0.04

    def test_default_beta_for_sector(self):
        assert default_beta_for("Utilities") == 0.55
        assert default_beta_for("Technology") == 1.15
        assert default_beta_for("Unknown Sector") == 1.0
        assert default_beta_for(None) == 1.0

    def test_clamp_tax_rate(self):
        assert clamp_tax_rate(0.05) == 0.15  # floored
        assert clamp_tax_rate(0.40) == 0.30  # capped
        assert clamp_tax_rate(0.21) == 0.21
        assert clamp_tax_rate(None) == pytest.approx(0.21)


# ─── Growth path & FCFF projection ───────────────────────────────────────────


class TestProjection:
    def test_growth_path_endpoints(self):
        path = build_growth_path(0.10, 0.02, years=5)
        assert path[0] == pytest.approx(0.10)
        assert path[-1] == pytest.approx(0.02)
        assert len(path) == 5
        # Linear fade — equal steps.
        diffs = [path[i] - path[i + 1] for i in range(4)]
        assert all(d == pytest.approx(diffs[0]) for d in diffs)

    def test_growth_path_single_year(self):
        assert build_growth_path(0.10, 0.02, years=1) == [0.02]

    def test_project_fcff_single_year(self):
        # base 1000, g 0.10 -> rev 1100; ebit 220; nopat 165; da 55; capex 66; dnwc 10 -> fcff 144
        revenues, fcff = project_fcff(1000, [0.10], 0.20, 0.25, 0.05, 0.06, 0.10)
        assert revenues == [pytest.approx(1100.0)]
        assert fcff == [pytest.approx(144.0)]

    def test_project_fcff_multi_year_growing_revenue(self):
        revenues, fcff = project_fcff(1000, [0.10, 0.05], 0.20, 0.25, 0.05, 0.06, 0.10)
        assert revenues[1] > revenues[0]
        assert len(fcff) == 2


# ─── Terminal value & discounting ────────────────────────────────────────────


class TestTerminalAndDiscount:
    def test_terminal_value_gordon(self):
        # 100 * 1.02 / (0.10 - 0.02) = 102 / 0.08 = 1275
        assert terminal_value_gordon(100, 0.02, 0.10) == pytest.approx(1275.0)

    def test_terminal_value_gordon_gate(self):
        with pytest.raises(ValueError):
            terminal_value_gordon(100, 0.10, 0.10)
        with pytest.raises(ValueError):
            terminal_value_gordon(100, 0.12, 0.10)

    def test_terminal_value_exit(self):
        assert terminal_value_exit(200, 10) == pytest.approx(2000.0)

    def test_discount_cash_flows(self):
        assert discount_cash_flows([110], 0.10) == pytest.approx(100.0)
        assert discount_cash_flows([100, 100], 0.10) == pytest.approx(90.9090909 + 82.6446281)

    def test_discount_terminal(self):
        assert discount_terminal(1210, 0.10, 2) == pytest.approx(1000.0)


# ─── run_dcf engine ──────────────────────────────────────────────────────────


class TestRunDCF:
    def test_run_dcf_gate_raises(self):
        with pytest.raises(ValueError):
            run_dcf(
                1000,
                [0.03],
                0.2,
                0.25,
                0.05,
                0.06,
                0.1,
                wacc=0.02,
                terminal_g=0.03,
                exit_multiple=10,
                cash=0,
                total_debt=0,
                shares=100,
            )

    def test_run_dcf_zero_shares_raises(self):
        with pytest.raises(ValueError):
            run_dcf(
                1000,
                [0.03],
                0.2,
                0.25,
                0.05,
                0.06,
                0.1,
                wacc=0.10,
                terminal_g=0.02,
                exit_multiple=10,
                cash=0,
                total_debt=0,
                shares=0,
            )

    def test_run_dcf_basic_shape(self):
        res = run_dcf(
            1000,
            [0.05, 0.04, 0.03],
            0.20,
            0.25,
            0.05,
            0.06,
            0.10,
            wacc=0.10,
            terminal_g=0.02,
            exit_multiple=10,
            cash=100,
            total_debt=200,
            shares=100,
        )
        assert res["implied_price"] > 0
        assert res["enterprise_value"] > 0
        assert 0 < res["tv_share_of_ev"] < 1
        # equity = ev + cash - debt
        assert res["equity_value"] == pytest.approx(res["enterprise_value"] + 100 - 200)

    def test_run_dcf_higher_wacc_lowers_price(self):
        base = dict(
            base_revenue=1000,
            growth_path=[0.05, 0.04, 0.03],
            ebit_margin=0.20,
            tax_rate=0.25,
            da_pct=0.05,
            capex_pct=0.06,
            nwc_intensity=0.10,
            terminal_g=0.02,
            exit_multiple=10,
            cash=0,
            total_debt=0,
            shares=100,
        )
        low = run_dcf(**base, wacc=0.09)
        high = run_dcf(**base, wacc=0.12)
        assert low["implied_price"] > high["implied_price"]


# ─── Relative valuation & SOTP ───────────────────────────────────────────────


class TestRelative:
    def test_peer_median_multiples(self):
        peers = [
            {"pe_fwd": 17, "ev_rev": 2.4, "ev_ebitda": 11.5},
            {"pe_fwd": 19, "ev_rev": 2.6, "ev_ebitda": 12.5},
            {"pe_fwd": 18, "ev_rev": 2.5, "ev_ebitda": 12.0},
            {"pe_fwd": 20, "ev_rev": 2.7, "ev_ebitda": 13.0},
        ]
        m = peer_median_multiples(peers)
        assert m["pe"] == pytest.approx(18.5)
        assert m["ev_rev"] == pytest.approx(2.55)
        assert m["ev_ebitda"] == pytest.approx(12.25)
        assert m["peer_count"] == 4

    def test_relative_valuation(self):
        # pe: 18.5*6=111; ev_rev: (2.55*20000-5000)/500=92; ev_ebitda:(12.25*5000-5000)/500=112.5
        r = relative_valuation(18.5, 2.55, 12.25, 6.0, 20000, 5000, 5000, 500)
        assert r["implied"]["pe"] == pytest.approx(111.0)
        assert r["implied"]["ev_rev"] == pytest.approx(92.0)
        assert r["implied"]["ev_ebitda"] == pytest.approx(112.5)
        assert r["implied_price"] == pytest.approx(111.0)  # median of the three

    def test_relative_skips_negative_eps(self):
        r = relative_valuation(18.5, 2.55, 12.25, -1.0, 20000, 5000, 5000, 500)
        assert r["implied"]["pe"] is None
        assert r["implied"]["ev_rev"] is not None

    def test_relative_all_missing(self):
        r = relative_valuation(None, None, None, None, None, None, 0, 100)
        assert r["implied_price"] is None

    def test_sotp_valuation(self):
        segments = [
            {"name": "Cloud", "ebitda": 750, "ev_ebitda": 20},
            {"name": "Hardware", "ebitda": 750, "ev_ebitda": 8},
        ]
        # seg EV = 15000 + 6000 = 21000; -corp 2000 = 19000; -net debt 2000 = 17000; /250 = 68
        s = sotp_valuation(
            segments, net_debt=2000, shares=250, corporate_ev_deduction=2000, current_price=42
        )
        assert s["total_segment_ev"] == pytest.approx(21000)
        assert s["enterprise_value"] == pytest.approx(19000)
        assert s["equity_value"] == pytest.approx(17000)
        assert s["implied_price"] == pytest.approx(68.0)
        # discount = (68 - 42)/68
        assert s["conglomerate_discount"] == pytest.approx((68 - 42) / 68, abs=1e-3)

    def test_sotp_revenue_basis(self):
        segments = [{"name": "Growth", "revenue": 1000, "ev_rev": 5}]
        s = sotp_valuation(segments, net_debt=0, shares=100)
        assert s["segments"][0]["basis"] == "EV/Revenue"
        assert s["implied_price"] == pytest.approx(50.0)


# ─── Blending ────────────────────────────────────────────────────────────────


class TestBlend:
    def test_blend_dcf_and_relative(self):
        b = blend_fair_value(100, 110, None, current_price=100)
        assert b["weights"] == {"dcf": 0.5, "rel": 0.5}
        assert b["fair_value"] == pytest.approx(105.0)
        assert b["upside_pct"] == pytest.approx(0.05)

    def test_blend_with_sotp(self):
        b = blend_fair_value(100, 110, 120, current_price=100)
        # 0.4*100 + 0.3*110 + 0.3*120 = 109
        assert b["fair_value"] == pytest.approx(109.0)
        assert set(b["weights"]) == {"dcf", "rel", "sotp"}

    def test_blend_renormalizes_when_dcf_missing(self):
        b = blend_fair_value(None, 110, None, current_price=100)
        assert b["fair_value"] == pytest.approx(110.0)
        assert b["weights"] == {"rel": 1.0}

    def test_blend_all_missing(self):
        b = blend_fair_value(None, None, None, current_price=100)
        assert b["fair_value"] is None


# ─── Sector routing & Rule of 40 ─────────────────────────────────────────────


class TestSectorRouting:
    def test_bank_suppresses_dcf(self):
        r = route_sector_method("Financial Services", "Banks - Regional")
        assert r["dcf_appropriate"] is False
        assert "P/TBV" in r["relative_multiples"]

    def test_reit_routing(self):
        r = route_sector_method("Real Estate", "REIT - Retail")
        assert r["dcf_appropriate"] is False
        assert "P/FFO" in r["relative_multiples"]

    def test_saas_routing(self):
        r = route_sector_method("Technology", "Software - Application")
        assert r["dcf_appropriate"] is True
        assert "Rule of 40" in r["relative_multiples"]

    def test_mature_routing(self):
        r = route_sector_method("Consumer Defensive", "Household & Personal Products")
        assert r["primary"] == "dcf"
        assert r["dcf_appropriate"] is True

    def test_rule_of_40(self):
        assert rule_of_40(30, 15)["classification"] == "healthy"
        assert rule_of_40(10, 5)["classification"] == "bottom-quartile"
        assert rule_of_40(40, 20)["classification"] == "top-quartile"
        assert rule_of_40(None, 5)["classification"] == "insufficient-data"


# ─── Guardrails ──────────────────────────────────────────────────────────────


class TestGuardrails:
    def test_gate_wacc_le_g(self):
        w = evaluate_guardrails(0.03, 0.04, 0.6, "Technology")
        assert any("GATE" in msg for msg in w)

    def test_tv_share_high(self):
        w = evaluate_guardrails(0.10, 0.02, 0.90, None)
        assert any("multiple-expansion" in msg for msg in w)

    def test_tv_share_low(self):
        w = evaluate_guardrails(0.10, 0.02, 0.30, None)
        assert any("too conservative" in msg for msg in w)

    def test_wacc_outside_sector_band(self):
        w = evaluate_guardrails(0.20, 0.02, 0.6, "Technology")
        assert any("sanity band" in msg for msg in w)

    def test_no_warnings_when_healthy(self):
        w = evaluate_guardrails(0.09, 0.025, 0.65, "Consumer Defensive")
        assert w == []


# ─── Sensitivity grid ────────────────────────────────────────────────────────


class TestSensitivity:
    def _base_args(self):
        return dict(
            base_revenue=1000,
            growth_path=[0.05, 0.04, 0.03, 0.03, 0.025],
            ebit_margin=0.20,
            tax_rate=0.25,
            da_pct=0.05,
            capex_pct=0.06,
            nwc_intensity=0.10,
            exit_multiple=10,
            cash=0,
            total_debt=0,
            shares=100,
        )

    def test_center_cell_matches_base_dcf(self):
        args = self._base_args()
        base_wacc = 0.10
        base = run_dcf(**args, wacc=base_wacc, terminal_g=0.025)
        grid = sensitivity_grid(args, base_wacc)
        # center of 5x5: wacc step 0.0 (row 2), g 0.025 (col 2)
        assert grid["rows"][2]["prices"][2] == pytest.approx(base["implied_price"])

    def test_gate_cells_are_none(self):
        args = self._base_args()
        # low WACC row combined with high g will trip the WACC<=g gate for some cells
        grid = sensitivity_grid(args, 0.02)
        flat = [p for row in grid["rows"] for p in row["prices"]]
        assert any(p is None for p in flat)

    def test_monotonic_decreasing_in_wacc(self):
        args = self._base_args()
        grid = sensitivity_grid(args, 0.10)
        # For the middle g column, higher WACC -> lower implied price
        col = [row["prices"][2] for row in grid["rows"]]
        assert all(col[i] > col[i + 1] for i in range(len(col) - 1))


# ─── Normalization (ACME fixture) ────────────────────────────────────────────


class TestNormalize:
    def test_basic_fields(self, acme_financials):
        fin = acme_financials
        assert fin.ticker == "ACME"
        assert fin.current_price == 100.0
        assert fin.shares_outstanding == 500_000_000
        assert fin.base_revenue == 20_000_000_000
        assert fin.beta == pytest.approx(0.9)
        assert fin.sector == "Consumer Defensive"

    def test_derived_margins(self, acme_financials):
        fin = acme_financials
        assert fin.ebit_margin == pytest.approx(0.20, abs=1e-3)
        assert fin.da_pct == pytest.approx(0.05, abs=1e-3)
        assert fin.capex_pct == pytest.approx(0.06, abs=1e-3)
        assert fin.tax_rate == pytest.approx(0.21, abs=1e-3)

    def test_nwc_intensity(self, acme_financials):
        # (6000-3000) - (3500-1000) = 500; /20000 = 0.025
        assert acme_financials.nwc_intensity == pytest.approx(0.025, abs=1e-3)

    def test_ttm_proxies(self, acme_financials):
        assert acme_financials.eps_ttm == pytest.approx(6.0)
        assert acme_financials.ebitda_ttm == pytest.approx(5_000_000_000)
        assert acme_financials.net_debt() == pytest.approx(5_000_000_000)

    def test_hist_cagr(self, acme_financials):
        # (20000/17000)^(1/2) - 1 ~ 0.0846
        assert acme_financials.hist_revenue_cagr == pytest.approx(0.0846, abs=2e-3)

    def test_peers_loaded(self, acme_financials):
        assert len(acme_financials.peer_multiples) == 4

    def test_missing_price_raises(self):
        raw = {"profile": [{"symbol": "X"}], "income": [{"revenue": 100}]}
        with pytest.raises(ValueError, match="price"):
            normalize_fmp(raw, "X")


# ─── Full valuation (ACME fixture) ───────────────────────────────────────────


class TestValueCompany:
    def test_full_run_structure(self, acme_financials):
        res = value_company(acme_financials, rf=0.045)
        assert res["ticker"] == "ACME"
        assert res["dcf"] is not None
        assert res["relative"] is not None
        assert res["blended"]["fair_value"] is not None
        assert res["sensitivity"] is not None
        assert res["scenarios"] is not None
        # JSON-serializable
        json.dumps(res)

    def test_wacc_in_expected_band(self, acme_financials):
        res = value_company(acme_financials, rf=0.045)
        # ke = 0.045 + 0.9*0.055 = 0.0945; wacc ~ 8.7%
        assert res["assumptions"]["wacc"] == pytest.approx(0.087, abs=3e-3)

    def test_dcf_price_reasonable(self, acme_financials):
        res = value_company(acme_financials, rf=0.045)
        price = res["dcf"]["implied_price"]
        assert 95 < price < 130
        assert 0.45 <= res["dcf"]["tv_share_of_ev"] <= 0.85

    def test_relative_price(self, acme_financials):
        res = value_company(acme_financials, rf=0.045)
        assert res["relative"]["implied_price"] == pytest.approx(111.0)

    def test_blended_between_methods(self, acme_financials):
        res = value_company(acme_financials, rf=0.045)
        dcf = res["dcf"]["implied_price"]
        rel = res["relative"]["implied_price"]
        blended = res["blended"]["fair_value"]
        assert min(dcf, rel) - 0.01 <= blended <= max(dcf, rel) + 0.01

    def test_no_gate_warning(self, acme_financials):
        res = value_company(acme_financials, rf=0.045)
        assert not any("GATE" in w for w in res["guardrail_warnings"])

    def test_base_scenario_matches_dcf(self, acme_financials):
        res = value_company(acme_financials, rf=0.045)
        assert res["scenarios"]["base"]["implied_price"] == pytest.approx(
            res["dcf"]["implied_price"]
        )

    def test_scenario_ordering(self, acme_financials):
        res = value_company(acme_financials, rf=0.045)
        sc = res["scenarios"]
        assert (
            sc["bull"]["implied_price"] > sc["base"]["implied_price"] > sc["bear"]["implied_price"]
        )

    def test_sensitivity_center_matches_dcf(self, acme_financials):
        res = value_company(acme_financials, rf=0.045)
        center = res["sensitivity"]["rows"][2]["prices"][2]
        assert center == pytest.approx(res["dcf"]["implied_price"])

    def test_bank_suppresses_dcf_in_full_run(self, acme_raw):
        acme_raw["profile"][0]["sector"] = "Financial Services"
        acme_raw["profile"][0]["industry"] = "Banks - Regional"
        fin = normalize_fmp(acme_raw, "ACME")
        res = value_company(fin, rf=0.045)
        assert res["dcf"] is None
        assert res["sensitivity"] is None
        # Blended still available from relative multiples
        assert res["blended"]["fair_value"] is not None
        assert res["blended"]["weights"] == {"rel": 1.0}

    def test_terminal_growth_capped_when_exceeding_wacc(self, acme_financials):
        # Force a terminal growth above WACC -> capped below WACC, gate warning present
        res = value_company(acme_financials, rf=0.045, terminal_g=0.20)
        assert res["assumptions"]["terminal_growth"] < res["assumptions"]["wacc"]
        assert any("GATE" in w for w in res["guardrail_warnings"])

    def test_markdown_report(self, acme_financials):
        res = value_company(acme_financials, rf=0.045)
        md = generate_markdown_report(res)
        assert "# Intrinsic Value — ACME" in md
        assert "## DCF Build" in md
        assert "## Sensitivity" in md
        assert "## Scenarios" in md


# ─── CLI end-to-end (offline via --input-json) ───────────────────────────────


class TestCLI:
    def _script(self):
        return str(REPO_ROOT / "skills" / "intrinsic-value-dcf" / "scripts" / "value_company.py")

    def _fixture(self):
        return str(Path(__file__).resolve().parent / "fixtures" / "acme_fmp.json")

    def test_cli_input_json(self, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                self._script(),
                "ACME",
                "--input-json",
                self._fixture(),
                "--output-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, result.stderr
        assert "blended fair value" in result.stdout
        # Reports written to the tmp dir, not the repo reports/.
        jsons = list(tmp_path.glob("intrinsic_value_ACME_*.json"))
        mds = list(tmp_path.glob("intrinsic_value_ACME_*.md"))
        assert len(jsons) == 1
        assert len(mds) == 1
        payload = json.loads(jsons[0].read_text())
        assert payload["ticker"] == "ACME"
        assert payload["dcf"]["implied_price"] > 0

    def test_cli_rf_override(self, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                self._script(),
                "ACME",
                "--input-json",
                self._fixture(),
                "--rf",
                "0.05",
                "--output-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(next(tmp_path.glob("*.json")).read_text())
        assert payload["assumptions"]["risk_free_rate"] == pytest.approx(0.05)

    def test_cli_missing_data_source(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FMP_API_KEY", raising=False)
        result = subprocess.run(
            [sys.executable, self._script(), "ACME", "--output-dir", str(tmp_path)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env={"PATH": __import__("os").environ.get("PATH", "")},
        )
        assert result.returncode == 1
        assert "no data source" in result.stderr

    def test_default_erp_constant(self):
        assert DEFAULT_ERP == 0.055
