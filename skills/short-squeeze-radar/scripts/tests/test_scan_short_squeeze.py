"""Tests for scan_short_squeeze.py — FINRA short-volume parsing, days-to-cover
ranking, rising-inflection detection, and squeeze classification.

All tests exercise PURE functions against saved FINRA-file fixtures. No network.
"""

import json
import tempfile
from pathlib import Path

from scan_short_squeeze import (
    ShortInterest,
    ShortVolDay,
    build_candidate,
    build_result,
    classify_ratio_trend,
    classify_squeeze,
    compute_squeeze_score,
    generate_markdown_report,
    index_by_symbol,
    is_squeeze_primed,
    latest_date_in,
    load_short_interest_file,
    load_symbols,
    parse_finra_shvol,
    parse_short_interest,
    parse_short_interest_csv,
    rank_candidates,
    scan,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FINRA_DIR = FIXTURES / "finra_daily"


def _load_all_days(symbols=None):
    """Parse all three daily FINRA fixtures into one flat list of ShortVolDay."""
    want = set(symbols) if symbols else None
    rows = []
    for f in sorted(FINRA_DIR.glob("CNMSshvol*.txt")):
        rows.extend(parse_finra_shvol(f.read_text(), want_symbols=want))
    return rows


# ─── Parsing FINRA daily files ───────────────────────────────────────────────


class TestParseFinra:
    def test_parses_rows_and_skips_header_and_trailer(self):
        text = (FINRA_DIR / "CNMSshvol20260703.txt").read_text()
        rows = parse_finra_shvol(text)
        assert len(rows) == 3  # header + trailer skipped
        syms = {r.symbol for r in rows}
        assert syms == {"GME", "AMC", "XYZ"}

    def test_ratio_includes_short_exempt(self):
        text = (FINRA_DIR / "CNMSshvol20260703.txt").read_text()
        gme = next(r for r in parse_finra_shvol(text) if r.symbol == "GME")
        # (650000 + 30000) / 1000000
        assert gme.ratio == 0.68

    def test_zero_total_volume_ratio_is_zero(self):
        text = (FINRA_DIR / "CNMSshvol20260701.txt").read_text()
        zero = next(r for r in parse_finra_shvol(text) if r.symbol == "ZERO")
        assert zero.ratio == 0.0

    def test_want_symbols_filter_case_insensitive(self):
        text = (FINRA_DIR / "CNMSshvol20260702.txt").read_text()
        rows = parse_finra_shvol(text, want_symbols={"gme"})
        assert len(rows) == 1
        assert rows[0].symbol == "GME"

    def test_empty_and_garbage_lines_ignored(self):
        rows = parse_finra_shvol("Date|Symbol|A|B|C\n\nRecords: 0\ngarbage\n")
        assert rows == []


# ─── Short-interest parsing + days-to-cover ──────────────────────────────────


class TestShortInterest:
    def test_computes_days_to_cover_when_missing(self):
        recs = [{"ticker": "GME", "short_interest": 25_000_000, "avg_daily_volume": 3_000_000}]
        si = parse_short_interest(recs)
        assert si["GME"].days_to_cover == 8.33  # 25M / 3M

    def test_uses_provided_days_to_cover(self):
        recs = [
            {
                "ticker": "AMC",
                "short_interest": 20_000_000,
                "avg_daily_volume": 40_000_000,
                "days_to_cover": 0.5,
            }
        ]
        si = parse_short_interest(recs)
        assert si["AMC"].days_to_cover == 0.5

    def test_skips_records_missing_core_fields(self):
        recs = [{"ticker": "BAD"}, {"symbol": "OK", "short_interest": 1, "avg_daily_volume": 1}]
        si = parse_short_interest(recs)
        assert "BAD" not in si
        assert "OK" in si

    def test_json_file_loader(self):
        si = load_short_interest_file(str(FIXTURES / "short_interest.json"))
        assert si["AAPL"].days_to_cover == 2.17  # 130M / 60M
        assert si["GME"].days_to_cover == 8.33

    def test_csv_file_loader(self):
        si = parse_short_interest_csv((FIXTURES / "short_interest.csv").read_text())
        assert si["GME"].days_to_cover == 8.33
        assert si["AMC"].days_to_cover == 0.5


# ─── Trend / rising-inflection detection ─────────────────────────────────────


class TestTrend:
    def test_rising_series_flags_inflection(self):
        trend, inflection = classify_ratio_trend([0.48, 0.56, 0.68])
        assert trend == "rising"
        assert inflection is True

    def test_flat_series(self):
        trend, inflection = classify_ratio_trend([0.30, 0.32, 0.31])
        assert trend == "flat"
        assert inflection is False

    def test_falling_series(self):
        trend, inflection = classify_ratio_trend([0.70, 0.55, 0.40])
        assert trend == "falling"
        assert inflection is False

    def test_single_day_is_na(self):
        assert classify_ratio_trend([0.62]) == ("n/a", False)

    def test_exactly_eps_move_is_flat_not_rising(self):
        # 0.45 - 0.42 == 0.0300000000000...27 in float; the EPS boundary must
        # resolve to flat, not rising.
        trend, inflection = classify_ratio_trend([0.42, 0.45])
        assert trend == "flat"
        assert inflection is False

    def test_rising_but_not_new_high_no_inflection(self):
        # ends up rising overall but the last step ticks down from the peak
        trend, inflection = classify_ratio_trend([0.40, 0.60, 0.50])
        assert trend == "rising"
        assert inflection is False


# ─── Classification + scoring ────────────────────────────────────────────────


class TestClassification:
    def test_crowded_by_ratio(self):
        assert classify_squeeze(0.68, None) == "crowded_short"

    def test_crowded_by_days_to_cover(self):
        assert classify_squeeze(0.50, 6.0) == "crowded_short"

    def test_low_pressure(self):
        assert classify_squeeze(0.31, 0.5) == "low_pressure"

    def test_neutral(self):
        assert classify_squeeze(0.45, 2.17) == "neutral"

    def test_primed_requires_crowded_rising_and_pressure(self):
        assert is_squeeze_primed("crowded_short", "rising", 8.33, 0.68) is True
        assert is_squeeze_primed("crowded_short", "flat", 8.33, 0.68) is False
        assert is_squeeze_primed("neutral", "rising", 8.33, 0.68) is False
        assert is_squeeze_primed("crowded_short", "rising", 1.0, 0.55) is False

    def test_score_clean_numbers(self):
        assert compute_squeeze_score(0.75, 10.0, "rising") == 100.0
        assert compute_squeeze_score(0.375, 5.0, "flat") == 48.0

    def test_score_without_short_interest_caps_at_60(self):
        assert compute_squeeze_score(0.75, None, "rising") == 60.0


# ─── Indexing + ranking integration ──────────────────────────────────────────


class TestScanIntegration:
    def test_index_by_symbol_chronological(self):
        days = _load_all_days()
        grouped = index_by_symbol(days)
        gme_dates = [d.date for d in grouped["GME"]]
        assert gme_dates == ["20260701", "20260702", "20260703"]

    def test_latest_date_in(self):
        assert latest_date_in(_load_all_days()) == "20260703"

    def test_prior_day_fallback_for_missing_symbol(self):
        # AAPL is present on 0701 and 0702 but MISSING on the latest file (0703).
        days = _load_all_days()
        si = load_short_interest_file(str(FIXTURES / "short_interest.json"))
        grouped = index_by_symbol(days)
        cand = build_candidate("AAPL", grouped["AAPL"], si["AAPL"], latest_date_in(days))
        assert cand.latest_date == "20260702"
        assert cand.fallback_used is True
        assert cand.latest_ratio == 0.45

    def test_gme_is_squeeze_primed(self):
        days = _load_all_days()
        si = load_short_interest_file(str(FIXTURES / "short_interest.json"))
        cands = scan(["GME", "AMC", "AAPL", "XYZ"], days, si)
        gme = next(c for c in cands if c.symbol == "GME")
        assert gme.classification == "crowded_short"
        assert gme.ratio_trend == "rising"
        assert gme.rising_inflection is True
        assert gme.squeeze_primed is True
        assert gme.days_to_cover == 8.33

    def test_amc_low_pressure_not_primed(self):
        days = _load_all_days()
        si = load_short_interest_file(str(FIXTURES / "short_interest.json"))
        cands = scan(["AMC"], days, si)
        amc = cands[0]
        assert amc.classification == "low_pressure"
        assert amc.squeeze_primed is False

    def test_ranking_by_days_to_cover_desc_none_last(self):
        days = _load_all_days()
        si = load_short_interest_file(str(FIXTURES / "short_interest.json"))
        cands = scan(["GME", "AMC", "AAPL", "XYZ"], days, si)
        order = [c.symbol for c in cands]
        # DTC: GME 8.33 > AAPL 2.17 > AMC 0.5 > XYZ (none)
        assert order == ["GME", "AAPL", "AMC", "XYZ"]

    def test_xyz_crowded_but_not_primed_no_short_interest(self):
        days = _load_all_days()
        cands = scan(["XYZ"], days, {})  # no short-interest map
        xyz = cands[0]
        assert xyz.classification == "crowded_short"  # ratio 0.625 >= 0.60
        assert xyz.ratio_trend == "n/a"  # only appears once
        assert xyz.squeeze_primed is False
        assert xyz.days_to_cover is None
        assert xyz.squeeze_score <= 60.0  # no SI component

    def test_rank_candidates_is_stable_sort_helper(self):
        a = build_candidate("A", [ShortVolDay("20260703", "A", 6, 0, 10)], None, "20260703")
        b = build_candidate(
            "B",
            [ShortVolDay("20260703", "B", 7, 0, 10)],
            ShortInterest("B", "2026-06-30", 100, 10, 10.0),
            "20260703",
        )
        ranked = rank_candidates([a, b])
        assert ranked[0].symbol == "B"  # has days-to-cover, ranks above None


# ─── Symbol loading ──────────────────────────────────────────────────────────


class TestLoadSymbols:
    def test_tickers_string(self):
        assert load_symbols(None, "gme, amc  aapl") == ["GME", "AMC", "AAPL"]

    def test_dedupes_and_uppercases(self):
        assert load_symbols(None, "gme,GME,amc") == ["GME", "AMC"]

    def test_json_watchlist(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "wl.json"
            p.write_text(json.dumps(["GME", "amc"]))
            assert load_symbols(str(p), None) == ["GME", "AMC"]

    def test_text_watchlist_with_comments(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "wl.txt"
            p.write_text("# my list\nGME\nAMC  # meme\n\nAAPL\n")
            assert load_symbols(str(p), None) == ["GME", "AMC", "AAPL"]


# ─── Reporting ───────────────────────────────────────────────────────────────


class TestReporting:
    def test_build_result_and_markdown(self):
        days = _load_all_days()
        si = load_short_interest_file(str(FIXTURES / "short_interest.json"))
        symbols = ["GME", "AMC", "AAPL", "XYZ", "NODATA"]
        cands = scan(symbols, days, si)
        result = build_result(cands, symbols, lookback_days=5, short_interest_source="si.json")
        assert result["squeeze_primed"] == ["GME"]
        assert result["symbols_without_data"] == ["NODATA"]
        md = generate_markdown_report(result)
        assert "# Short-Squeeze Radar" in md
        assert "GME" in md
        assert "NODATA" in md  # surfaced in "No FINRA Data" section

    def test_write_reports_to_tmp(self):
        from scan_short_squeeze import write_reports

        days = _load_all_days()
        si = load_short_interest_file(str(FIXTURES / "short_interest.json"))
        cands = scan(["GME"], days, si)
        result = build_result(cands, ["GME"], 5, "si.json")
        with tempfile.TemporaryDirectory() as td:
            json_path, md_path = write_reports(result, td)
            assert Path(json_path).exists()
            assert Path(md_path).exists()
            loaded = json.loads(Path(json_path).read_text())
            assert loaded["candidates"][0]["symbol"] == "GME"
