"""Offline tests for the multi-style idea-screen runner.

All recipe/spec/command/parse/format helpers run with the stdlib only. The
network path (``run_screener``) is exercised against a stubbed ``subprocess``
and the sibling screener is never imported or called.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import run_style_screens as rss

FIXTURES = Path(__file__).parent / "fixtures"

EXPECTED_RECIPES = {"value", "growth", "quality", "short", "special-situation"}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class TestRegistry:
    def test_list_recipes(self) -> None:
        assert set(rss.list_recipes()) == EXPECTED_RECIPES

    def test_get_recipe_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown recipe"):
            rss.get_recipe("momentum")

    @pytest.mark.parametrize("name", sorted(EXPECTED_RECIPES))
    def test_every_recipe_has_required_metadata(self, name: str) -> None:
        recipe = rss.get_recipe(name)
        for key in (
            "style",
            "direction",
            "summary",
            "screen_spec",
            "qualitative_factors",
            "peer_metrics",
            "mispricing_prompts",
            "catalysts",
            "disconfirming_risks",
        ):
            assert recipe.get(key), f"{name} missing {key}"

    @pytest.mark.parametrize("name", sorted(EXPECTED_RECIPES))
    def test_screen_spec_is_well_formed_boolean_tree(self, name: str) -> None:
        spec = rss.get_recipe(name)["screen_spec"]
        assert spec["operator"] in {"and", "or"}
        assert len(spec["operands"]) >= 2
        for leaf in spec["operands"]:
            assert "operator" in leaf and "operands" in leaf
            assert isinstance(leaf["operands"][0], str)  # field name

    def test_short_recipe_is_a_short(self) -> None:
        assert rss.get_recipe("short")["direction"] == "short"


# ---------------------------------------------------------------------------
# build_screen_spec
# ---------------------------------------------------------------------------
class TestBuildScreenSpec:
    def test_region_appended_as_eq_leaf(self) -> None:
        spec = rss.build_screen_spec("value", region="us")
        assert spec["operator"] == "and"
        region_leaves = [o for o in spec["operands"] if o["operands"][:1] == ["region"]]
        assert region_leaves == [{"operator": "eq", "operands": ["region", "us"]}]

    def test_region_none_leaves_spec_untouched(self) -> None:
        spec = rss.build_screen_spec("value", region=None)
        assert all(o["operands"][0] != "region" for o in spec["operands"])

    def test_does_not_mutate_registry(self) -> None:
        original = json.dumps(rss.STYLE_RECIPES["value"]["screen_spec"], sort_keys=True)
        rss.build_screen_spec("value", region="ca")
        assert json.dumps(rss.STYLE_RECIPES["value"]["screen_spec"], sort_keys=True) == original


# ---------------------------------------------------------------------------
# build_screener_command
# ---------------------------------------------------------------------------
class TestBuildCommand:
    def test_command_shape(self) -> None:
        spec = rss.build_screen_spec("growth")
        cmd = rss.build_screener_command(
            spec, script_path=Path("/x/yf_boolean_screen.py"), count=10, sort_field="percentchange"
        )
        assert "--query-json" in cmd
        assert json.loads(cmd[cmd.index("--query-json") + 1]) == spec
        assert "--no-report" in cmd
        assert cmd[cmd.index("--count") + 1] == "10"
        assert cmd[cmd.index("--sort-field") + 1] == "percentchange"
        assert "--sort-asc" not in cmd

    def test_sort_asc_flag(self) -> None:
        cmd = rss.build_screener_command(
            {"operator": "gt", "operands": ["beta", 1]},
            script_path=Path("/x/yf.py"),
            sort_asc=True,
        )
        assert "--sort-asc" in cmd


# ---------------------------------------------------------------------------
# parse_screener_output
# ---------------------------------------------------------------------------
class TestParseOutput:
    def test_parses_fixture_and_cleans_symbols(self) -> None:
        stdout = (FIXTURES / "screener_summary.json").read_text()
        parsed = rss.parse_screener_output(stdout)
        assert parsed["symbols"] == ["FAKE1", "FAKE2", "FAKE3"]  # drops "" and non-str
        assert parsed["total_matches"] == 42
        assert parsed["result_count"] == 3

    def test_missing_symbols_defaults_to_empty(self) -> None:
        parsed = rss.parse_screener_output('{"result_count": 0}')
        assert parsed["symbols"] == []

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ValueError, match="not valid JSON"):
            rss.parse_screener_output("not json")

    def test_non_object_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a JSON object"):
            rss.parse_screener_output("[1, 2, 3]")


# ---------------------------------------------------------------------------
# run_screener (network path, stubbed subprocess)
# ---------------------------------------------------------------------------
class TestRunScreener:
    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Proc:
            returncode = 0
            stdout = '{"result_count": 1, "symbols": ["ABC"], "total_matches": 5}'
            stderr = ""

        monkeypatch.setattr(rss.subprocess, "run", lambda *a, **k: _Proc())
        out = rss.run_screener(["python", "yf.py"])
        assert out["symbols"] == ["ABC"]

    def test_nonzero_exit_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "boom"

        monkeypatch.setattr(rss.subprocess, "run", lambda *a, **k: _Proc())
        with pytest.raises(RuntimeError, match="boom"):
            rss.run_screener(["python", "yf.py"])


# ---------------------------------------------------------------------------
# build_idea_card
# ---------------------------------------------------------------------------
class TestIdeaCard:
    def test_contains_four_required_sections(self) -> None:
        card = rss.build_idea_card("value", "TEST")
        assert "TEST" in card
        assert "Peer-relative metrics" in card
        assert "Mispricing bullets" in card
        assert "Catalyst" in card
        assert "Disconfirming risks" in card

    def test_peer_table_has_one_row_per_metric(self) -> None:
        recipe = rss.get_recipe("quality")
        card = rss.build_idea_card("quality", "QLT")
        for metric in recipe["peer_metrics"]:
            assert f"| {metric} |" in card


# ---------------------------------------------------------------------------
# build_value_chain_map
# ---------------------------------------------------------------------------
class TestValueChain:
    def _beneficiaries(self) -> list[dict]:
        return [
            {"ticker": "PURE", "name": "Pure Play", "layer": "direct", "priced_in": True},
            {
                "ticker": "SUPP",
                "name": "Supplier",
                "layer": "indirect",
                "priced_in": False,
                "mechanism": "sells tooling",
            },
            {
                "ticker": "PWR",
                "name": "Grid Co",
                "layer": "second_order",
                "priced_in": False,
                "mechanism": "powers data centers",
            },
            {"ticker": "KNOWN", "name": "Known Co", "layer": "second_order", "priced_in": True},
        ]

    def test_hunt_list_surfaces_not_priced_in_non_direct(self) -> None:
        md = rss.build_value_chain_map("AI capex", self._beneficiaries())
        assert "Hunt list" in md
        assert "SUPP" in md.split("Hunt list")[1]
        assert "PWR" in md.split("Hunt list")[1]

    def test_direct_and_priced_in_excluded_from_hunt(self) -> None:
        md = rss.build_value_chain_map("AI capex", self._beneficiaries())
        hunt = md.split("Hunt list")[1]
        assert "PURE" not in hunt  # direct, priced-in
        assert "KNOWN" not in hunt  # second-order but priced-in

    def test_empty_hunt_when_all_priced_in(self) -> None:
        b = [{"ticker": "A", "layer": "direct", "priced_in": True}]
        md = rss.build_value_chain_map("theme", b)
        assert "priced-in" in md.lower()


# ---------------------------------------------------------------------------
# build_recipe_report
# ---------------------------------------------------------------------------
class TestRecipeReport:
    def _dt(self):
        import datetime as dt

        return dt.datetime(2026, 7, 3, tzinfo=dt.timezone.utc)

    def test_dry_run_shows_spec_and_command(self) -> None:
        spec = rss.build_screen_spec("value")
        cmd = rss.build_screener_command(spec, script_path=Path("/x/yf.py"))
        md = rss.build_recipe_report(
            [{"recipe": "value", "spec": spec, "command": cmd}], executed=False, now=self._dt()
        )
        assert "dry-run" in md
        assert "**Command**" in md
        assert "yf.py" in md

    def test_executed_shows_candidates_and_cards(self) -> None:
        spec = rss.build_screen_spec("value")
        md = rss.build_recipe_report(
            [{"recipe": "value", "spec": spec, "command": [], "symbols": ["ABC", "XYZ"]}],
            executed=True,
            now=self._dt(),
        )
        assert "Candidates (2)" in md
        assert "ABC" in md and "XYZ" in md
        assert "Peer-relative metrics" in md  # idea card rendered


# ---------------------------------------------------------------------------
# CLI (main) — offline
# ---------------------------------------------------------------------------
class TestCli:
    def test_list(self, capsys: pytest.CaptureFixture) -> None:
        assert rss.main(["--list"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert set(out["recipes"]) == EXPECTED_RECIPES

    def test_recipe_dry_run_writes_report(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        rc = rss.main(["--recipe", "value", "--output-dir", str(tmp_path)])
        assert rc == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["mode"] == "dry-run"
        report = Path(summary["report_markdown"])
        assert report.exists()
        assert report.parent == tmp_path

    def test_all_dry_run_no_report(self, capsys: pytest.CaptureFixture) -> None:
        rc = rss.main(["--all", "--no-report"])
        assert rc == 0
        summary = json.loads(capsys.readouterr().out)
        assert set(summary["recipes"]) == EXPECTED_RECIPES
        assert "report_markdown" not in summary

    def test_execute_uses_stubbed_run_screener(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(
            rss, "run_screener", lambda cmd, **k: {"symbols": ["ZZZ"], "result_count": 1}
        )
        rc = rss.main(["--recipe", "growth", "--execute", "--output-dir", str(tmp_path)])
        assert rc == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["mode"] == "execute"
        assert summary["candidates"]["growth"] == ["ZZZ"]

    def test_value_chain_file(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        vc = tmp_path / "thesis.json"
        vc.write_text(
            json.dumps(
                {
                    "thesis": "reshoring",
                    "beneficiaries": [{"ticker": "X", "layer": "second_order", "priced_in": False}],
                }
            )
        )
        rc = rss.main(["--value-chain-file", str(vc), "--output-dir", str(tmp_path)])
        assert rc == 0
        summary = json.loads(capsys.readouterr().out)
        report = Path(summary["report_markdown"])
        assert report.exists()
        assert "reshoring" in report.read_text()

    def test_unknown_recipe_returns_error(self, capsys: pytest.CaptureFixture) -> None:
        rc = rss.main(["--recipe", "nope", "--no-report"])
        assert rc == 1
