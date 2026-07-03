"""Offline tests for yf_boolean_screen.py.

Pure functions (parser, validator, table formatter) run with the stdlib only.
The yfinance-backed builder/fetchers are exercised against a committed fixture
via an injected stub module — no network, no real yfinance dependency.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yf_boolean_screen as ybs

FIXTURES = Path(__file__).parent / "fixtures"


def _screen_fixture() -> dict:
    return json.loads((FIXTURES / "yf_screen_result.json").read_text())


def _search_fixture() -> dict:
    return json.loads((FIXTURES / "yf_search_result.json").read_text())


# ---------------------------------------------------------------------------
# normalize_operator / coerce_value
# ---------------------------------------------------------------------------
class TestOperators:
    def test_canonical_passthrough(self):
        assert ybs.normalize_operator("gt") == "gt"
        assert ybs.normalize_operator("IS-IN") == "is-in"

    def test_aliases(self):
        assert ybs.normalize_operator(">") == "gt"
        assert ybs.normalize_operator(">=") == "gte"
        assert ybs.normalize_operator("==") == "eq"
        assert ybs.normalize_operator("in") == "is-in"
        assert ybs.normalize_operator("between") == "btwn"

    def test_unknown_operator(self):
        with pytest.raises(ValueError):
            ybs.normalize_operator("approx")

    def test_non_string_operator(self):
        with pytest.raises(ValueError):
            ybs.normalize_operator(5)

    def test_coerce_value(self):
        assert ybs.coerce_value("5") == 5
        assert ybs.coerce_value("-3") == -3
        assert ybs.coerce_value("0.03") == 0.03
        assert ybs.coerce_value("2e9") == 2e9
        assert ybs.coerce_value("Technology") == "Technology"


# ---------------------------------------------------------------------------
# parse_dsl
# ---------------------------------------------------------------------------
class TestParseDsl:
    def test_single_leaf(self):
        assert ybs.parse_dsl("percentchange gt 3") == {
            "operator": "gt",
            "operands": ["percentchange", 3],
        }

    def test_and_flattening(self):
        spec = ybs.parse_dsl("marketcap gt 1e9 and percentchange gt 3 and beta lt 1.5")
        assert spec["operator"] == "and"
        assert len(spec["operands"]) == 3

    def test_or_precedence_lower_than_and(self):
        # a AND b OR c  ==  (a AND b) OR c
        spec = ybs.parse_dsl("a gt 1 and b gt 2 or c gt 3")
        assert spec["operator"] == "or"
        assert len(spec["operands"]) == 2
        assert spec["operands"][0]["operator"] == "and"
        assert spec["operands"][1] == {"operator": "gt", "operands": ["c", 3]}

    def test_parentheses_override_precedence(self):
        spec = ybs.parse_dsl(
            "marketcap gt 1e9 and (percentchange gt 3 or forward_dividend_yield gte 0.03)"
        )
        assert spec["operator"] == "and"
        inner = spec["operands"][1]
        assert inner["operator"] == "or"
        assert inner["operands"][1] == {
            "operator": "gte",
            "operands": ["forward_dividend_yield", 0.03],
        }

    def test_btwn(self):
        assert ybs.parse_dsl("percentchange btwn 1 5") == {
            "operator": "btwn",
            "operands": ["percentchange", 1, 5],
        }

    def test_is_in_comma_list(self):
        assert ybs.parse_dsl("sector is-in Technology,Healthcare") == {
            "operator": "is-in",
            "operands": ["sector", "Technology", "Healthcare"],
        }

    def test_alias_symbols(self):
        assert ybs.parse_dsl("price >= 10") == {"operator": "gte", "operands": ["price", 10]}

    def test_empty_query(self):
        with pytest.raises(ValueError):
            ybs.parse_dsl("   ")

    def test_unbalanced_parens(self):
        with pytest.raises(ValueError):
            ybs.parse_dsl("(marketcap gt 1e9")

    def test_trailing_token(self):
        with pytest.raises(ValueError):
            ybs.parse_dsl("marketcap gt 1e9 percentchange gt 3")

    def test_field_where_operator_expected(self):
        with pytest.raises(ValueError):
            ybs.parse_dsl("and gt 3")


# ---------------------------------------------------------------------------
# normalize_spec
# ---------------------------------------------------------------------------
class TestNormalizeSpec:
    def test_valid_nested(self):
        spec = {
            "operator": "AND",
            "operands": [
                {"operator": ">", "operands": ["marketcap", 1000000000]},
                {"operator": "is-in", "operands": ["sector", "Technology"]},
            ],
        }
        out = ybs.normalize_spec(spec)
        assert out["operator"] == "and"
        assert out["operands"][0]["operator"] == "gt"

    def test_bool_needs_two(self):
        with pytest.raises(ValueError):
            ybs.normalize_spec(
                {"operator": "and", "operands": [{"operator": "gt", "operands": ["x", 1]}]}
            )

    def test_gt_arity(self):
        with pytest.raises(ValueError):
            ybs.normalize_spec({"operator": "gt", "operands": ["x", 1, 2]})

    def test_gt_needs_numeric(self):
        with pytest.raises(ValueError):
            ybs.normalize_spec({"operator": "gt", "operands": ["x", "abc"]})

    def test_gt_rejects_bool(self):
        with pytest.raises(ValueError):
            ybs.normalize_spec({"operator": "gt", "operands": ["x", True]})

    def test_btwn_arity_and_types(self):
        with pytest.raises(ValueError):
            ybs.normalize_spec({"operator": "btwn", "operands": ["x", 1]})
        with pytest.raises(ValueError):
            ybs.normalize_spec({"operator": "btwn", "operands": ["x", 1, "z"]})

    def test_is_in_needs_value(self):
        with pytest.raises(ValueError):
            ybs.normalize_spec({"operator": "is-in", "operands": ["sector"]})

    def test_missing_keys(self):
        with pytest.raises(ValueError):
            ybs.normalize_spec({"operator": "gt"})

    def test_empty_field(self):
        with pytest.raises(ValueError):
            ybs.normalize_spec({"operator": "gt", "operands": ["", 1]})


# ---------------------------------------------------------------------------
# screen_result_to_rows / markdown
# ---------------------------------------------------------------------------
class TestResultShaping:
    def test_rows_from_fixture(self):
        rows = ybs.screen_result_to_rows(_screen_fixture())
        assert len(rows) == 6
        assert rows[0]["symbol"] == "LOBLY"
        assert all("marketCap" in r for r in rows)

    def test_shortname_fallback_to_longname(self):
        result = {"quotes": [{"symbol": "X", "longName": "Ex Corp", "regularMarketPrice": 1.0}]}
        rows = ybs.screen_result_to_rows(result)
        assert rows[0]["shortName"] == "Ex Corp"

    def test_custom_columns(self):
        rows = ybs.screen_result_to_rows(_screen_fixture(), ["symbol", "currency"])
        assert set(rows[0].keys()) == {"symbol", "currency"}

    def test_empty_result(self):
        assert ybs.screen_result_to_rows(None) == []
        assert ybs.screen_result_to_rows({"quotes": []}) == []


class TestMarkdown:
    def test_table_and_humanization(self):
        rows = ybs.screen_result_to_rows(_screen_fixture())
        md = ybs.rows_to_markdown(rows, title="T", total=324)
        assert "# T" in md
        assert "**Total matches:** 324" in md
        assert "62.20B" in md  # marketCap humanized
        assert "%" in md  # percent columns
        # header + separator + 6 data rows present
        assert md.count("| symbol |") == 1

    def test_empty_rows(self):
        md = ybs.rows_to_markdown([])
        assert "_No matching stocks._" in md

    def test_pipe_escaping(self):
        result = {"quotes": [{"symbol": "A|B", "shortName": "x", "regularMarketPrice": 1.0}]}
        rows = ybs.screen_result_to_rows(result)
        md = ybs.rows_to_markdown(rows)
        assert "A\\|B" in md

    def test_volume_and_percent_formatting(self):
        assert ybs._fmt_cell("averageDailyVolume3Month", 1234567) == "1,234,567"
        assert ybs._fmt_cell("regularMarketChangePercent", 3.14159) == "3.14%"
        assert ybs._fmt_cell("marketCap", 1.5e12) == "1.50T"
        assert ybs._fmt_cell("anything", None) == "-"


# ---------------------------------------------------------------------------
# build_payload / write_reports
# ---------------------------------------------------------------------------
class TestReports:
    def test_build_payload(self):
        import datetime as dt

        rows = ybs.screen_result_to_rows(_screen_fixture())
        payload = ybs.build_payload(
            mode="boolean",
            query={"operator": "gt", "operands": ["percentchange", 1]},
            rows=rows,
            columns=ybs.DEFAULT_COLUMNS,
            total=324,
            sort_field="percentchange",
            sort_asc=False,
            now=dt.datetime(2026, 7, 3, tzinfo=dt.timezone.utc),
        )
        assert payload["mode"] == "boolean"
        assert payload["result_count"] == 6
        assert payload["total_matches"] == 324
        assert payload["source"] == "yfinance"

    def test_write_reports_to_tmp(self, tmp_path):
        import datetime as dt

        now = dt.datetime(2026, 7, 3, 12, 0, 0, tzinfo=dt.timezone.utc)
        md_path, json_path = ybs.write_reports(tmp_path, "yf_screen", now, "# hi\n", {"a": 1})
        assert md_path.exists() and json_path.exists()
        assert md_path.read_text() == "# hi\n"
        assert json.loads(json_path.read_text()) == {"a": 1}
        assert md_path.parent == tmp_path


# ---------------------------------------------------------------------------
# yfinance-backed builder/fetchers — injected stub, offline
# ---------------------------------------------------------------------------
class _FakeEquityQuery:
    def __init__(self, operator, operands):
        self.operator = operator
        self.operands = operands


class _FakeSearch:
    def __init__(self, query, max_results, news_count):
        self.query = query
        payload = json.loads((FIXTURES / "yf_search_result.json").read_text())
        self.quotes = payload["quotes"]
        self.news = payload["news"]


class _FakeYF:
    """Minimal stand-in for the yfinance module used by the lazy fetchers."""

    def __init__(self, screen_result):
        self.EquityQuery = _FakeEquityQuery
        self.Search = _FakeSearch
        self.PREDEFINED_SCREENER_QUERIES = {"day_gainers": object(), "most_actives": object()}
        self._screen_result = screen_result
        self.screen_calls: list = []

    def screen(self, query, **kwargs):
        self.screen_calls.append((query, kwargs))
        return self._screen_result


@pytest.fixture()
def fake_yf(monkeypatch):
    fake = _FakeYF(_screen_fixture())
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    return fake


class TestBuilder:
    def test_build_equity_query_recursion(self, fake_yf):
        spec = {
            "operator": "and",
            "operands": [
                {"operator": "gt", "operands": ["marketcap", 1000000000]},
                {
                    "operator": "or",
                    "operands": [
                        {"operator": "lt", "operands": ["percentchange", -3]},
                        {"operator": "gt", "operands": ["percentchange", 3]},
                    ],
                },
            ],
        }
        q = ybs.build_equity_query(spec)
        assert q.operator == "AND"
        assert len(q.operands) == 2
        assert q.operands[0].operator == "GT"
        assert q.operands[0].operands == ["marketcap", 1000000000]
        assert q.operands[1].operator == "OR"
        assert q.operands[1].operands[0].operator == "LT"

    def test_run_boolean_screen(self, fake_yf):
        spec = {"operator": "gt", "operands": ["percentchange", 1]}
        result = ybs.run_boolean_screen(spec, "percentchange", False, 10)
        assert result["total"] == 324
        # verify the sort/size kwargs were forwarded
        _query, kwargs = fake_yf.screen_calls[0]
        assert kwargs == {"sortField": "percentchange", "sortAsc": False, "size": 10}

    def test_run_predefined_screen_valid(self, fake_yf):
        result = ybs.run_predefined_screen("day_gainers", 5)
        assert result["total"] == 324
        _query, kwargs = fake_yf.screen_calls[0]
        assert kwargs == {"count": 5}

    def test_run_predefined_screen_invalid(self, fake_yf):
        with pytest.raises(ValueError):
            ybs.run_predefined_screen("nope", 5)

    def test_list_predefined(self, fake_yf):
        assert ybs.list_predefined() == ["day_gainers", "most_actives"]

    def test_search_tickers(self, fake_yf):
        out = ybs.search_tickers("electric vehicles", max_results=4, news_count=2)
        assert out["quotes"][0]["symbol"] == "EXMP"
        assert out["news"][0]["publisher"] == "Example Newswire"


# ---------------------------------------------------------------------------
# CLI (main) — patch lazy fetchers, offline
# ---------------------------------------------------------------------------
class TestCli:
    def test_list_predefined_cli(self, monkeypatch, capsys):
        monkeypatch.setattr(ybs, "list_predefined", lambda: ["day_gainers"])
        rc = ybs.main(["--list-predefined"])
        assert rc == 0
        assert "day_gainers" in capsys.readouterr().out

    def test_boolean_dsl_writes_report(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(ybs, "run_boolean_screen", lambda *a, **k: _screen_fixture())
        rc = ybs.main(
            [
                "--dsl",
                "intradaymarketcap gte 5e10 and percentchange gt 1",
                "--output-dir",
                str(tmp_path),
            ]
        )
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["mode"] == "boolean"
        assert out["result_count"] == 6
        assert Path(out["report_markdown"]).exists()
        assert Path(out["report_json"]).exists()

    def test_boolean_bad_dsl_returns_1(self, capsys):
        rc = ybs.main(["--dsl", "(marketcap gt 1e9"])
        assert rc == 1
        assert "Error" in capsys.readouterr().err

    def test_predefined_cli_no_report(self, monkeypatch, capsys):
        monkeypatch.setattr(ybs, "run_predefined_screen", lambda name, count: _screen_fixture())
        rc = ybs.main(["--predefined", "day_gainers", "--no-report"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["mode"] == "predefined"
        assert "report_markdown" not in out

    def test_search_cli(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(ybs, "search_tickers", lambda *a, **k: _search_fixture())
        rc = ybs.main(["--search", "electric vehicles", "--output-dir", str(tmp_path)])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["mode"] == "search"
        assert out["quotes"][0]["symbol"] == "EXMP"
        assert Path(out["report_markdown"]).exists()

    def test_count_capped(self, monkeypatch, capsys):
        captured = {}

        def fake_screen(spec, sort_field, sort_asc, count):
            captured["count"] = count
            return _screen_fixture()

        monkeypatch.setattr(ybs, "run_boolean_screen", fake_screen)
        ybs.main(["--dsl", "percentchange gt 1", "--count", "9999", "--no-report"])
        assert captured["count"] == ybs.MAX_COUNT
