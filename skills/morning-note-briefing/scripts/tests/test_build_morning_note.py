"""Tests for the morning-note-briefing assembler.

All tests run offline against committed fixtures. Report writes go to a
tmp_path, never the repo reports/ directory.
"""

import json
from pathlib import Path

from build_morning_note import (
    NoteInputs,
    assemble_note,
    build_actionable_ideas,
    earnings_surprise_pct,
    extract_earnings_developments,
    extract_macro_developments,
    extract_mover_developments,
    extract_news_developments,
    extract_sector_development,
    load_json_file,
    load_news_inputs,
    main,
    rank_developments,
    render_markdown,
    select_lead,
    select_top_call,
)

FIX = Path(__file__).resolve().parent / "fixtures"


def _load(name):
    return load_json_file(FIX / name)


# --------------------------------------------------------------------------
# News extraction
# --------------------------------------------------------------------------
class TestNews:
    def test_breaking_full_shape(self):
        devs = extract_news_developments([_load("news_breaking.json")])
        assert len(devs) == 1
        d = devs[0]
        assert d.category == "news"
        assert d.ticker == "FCTA"
        assert d.priority == 90.0  # breaking
        assert "takeover" in d.headline
        assert d.direction == "none"  # news is attention, not direction

    def test_calm_compact_shape(self):
        devs = extract_news_developments([_load("news_calm.json")])
        assert len(devs) == 1
        assert devs[0].priority == 35.0  # baseline
        assert devs[0].ticker == "FRTL"

    def test_elevated_severity_scores_between(self):
        item = {"ticker": "X", "severity": "elevated", "blackout": False}
        devs = extract_news_developments([item])
        assert devs[0].priority == 55.0


# --------------------------------------------------------------------------
# Earnings extraction
# --------------------------------------------------------------------------
class TestEarnings:
    def test_surprise_pct_beat(self):
        rec = {"epsEstimated": 1.20, "epsActual": 1.55}
        assert earnings_surprise_pct(rec) == 29.2

    def test_surprise_pct_missing(self):
        assert earnings_surprise_pct({"epsEstimated": 1.0}) is None

    def test_surprise_fallback_field(self):
        assert earnings_surprise_pct({"epsSurprisePct": 4.2}) == 4.2

    def test_beat_raised_is_long_major(self):
        devs = extract_earnings_developments(_load("earnings.json"))
        by_sym = {d.ticker: d for d in devs}
        beat = by_sym["FCTA"]
        assert beat.direction == "long"
        assert beat.priority == 80.0  # major (raised guidance)
        assert "beat" in beat.headline

    def test_miss_lowered_is_short(self):
        devs = extract_earnings_developments(_load("earnings.json"))
        miss = {d.ticker: d for d in devs}["FRTL"]
        assert miss.direction == "short"
        assert miss.priority == 80.0

    def test_scheduled_only_is_low_priority_neutral(self):
        devs = extract_earnings_developments(_load("earnings.json"))
        sched = {d.ticker: d for d in devs}["FSKD"]
        assert sched.direction == "none"
        assert sched.priority == 30.0


# --------------------------------------------------------------------------
# Macro extraction
# --------------------------------------------------------------------------
class TestMacro:
    def test_filters_to_as_of_date(self):
        devs = extract_macro_developments(_load("economic.json"), "2026-07-03")
        headlines = [d.headline for d in devs]
        assert any("Nonfarm Payrolls" in h for h in headlines)
        # German event is dated 2026-07-04 and must be excluded.
        assert not any("German" in h for h in headlines)

    def test_high_impact_surprise_is_top_priority(self):
        devs = extract_macro_developments(_load("economic.json"), "2026-07-03")
        nfp = next(d for d in devs if "Nonfarm" in d.headline)
        assert nfp.priority == 85.0  # high impact + actual present

    def test_medium_impact_no_actual(self):
        devs = extract_macro_developments(_load("economic.json"), "2026-07-03")
        ism = next(d for d in devs if "ISM" in d.headline)
        assert ism.priority == 40.0


# --------------------------------------------------------------------------
# Movers extraction
# --------------------------------------------------------------------------
class TestMovers:
    def test_large_gap_up_is_long(self):
        devs = extract_mover_developments(_load("movers.json"))
        up = {d.ticker: d for d in devs}["FGAP"]
        assert up.direction == "long"
        assert up.priority == 75.0

    def test_negative_move_is_short(self):
        devs = extract_mover_developments(_load("movers.json"))
        down = {d.ticker: d for d in devs}["FDWN"]
        assert down.direction == "short"
        assert down.priority == 55.0  # 5.1% -> notable band

    def test_missing_pct_skipped(self):
        devs = extract_mover_developments([{"ticker": "NOPCT"}])
        assert devs == []


# --------------------------------------------------------------------------
# Sector extraction
# --------------------------------------------------------------------------
class TestSector:
    def test_sector_read_from_fixture(self):
        dev = extract_sector_development(_load("sector.json"))
        assert dev is not None
        assert dev.category == "sector"
        assert "risk off" in dev.headline
        assert dev.priority == 55.0  # overbought/oversold present -> extreme
        assert "Utilities" in dev.detail

    def test_none_on_empty(self):
        assert extract_sector_development({}) is None
        assert extract_sector_development(None) is None


# --------------------------------------------------------------------------
# Ranking + selection
# --------------------------------------------------------------------------
class TestRanking:
    def test_rank_descending_stable(self):
        from build_morning_note import Development

        a = Development("news", "a", priority=50)
        b = Development("news", "b", priority=90)
        c = Development("news", "c", priority=90)
        ranked = rank_developments([a, b, c])
        # b and c tie at 90 -> stable insertion order preserved.
        assert [d.headline for d in ranked] == ["b", "c", "a"]

    def test_lead_is_top_priority(self):
        from build_morning_note import Development

        ranked = rank_developments(
            [Development("macro", "m", priority=40), Development("news", "n", priority=90)]
        )
        assert select_lead(ranked).headline == "n"

    def test_top_call_skips_directionless_lead(self):
        from build_morning_note import Development

        # Highest priority is a directionless macro print; top call must be the
        # highest-priority *directional* development instead.
        ranked = rank_developments(
            [
                Development("macro", "cpi", priority=85, direction="none"),
                Development("mover", "gap", priority=75, direction="long", ticker="ZZZ"),
            ]
        )
        assert select_lead(ranked).headline == "cpi"
        assert select_top_call(ranked).ticker == "ZZZ"

    def test_top_call_none_when_no_direction(self):
        from build_morning_note import Development

        ranked = rank_developments([Development("macro", "cpi", priority=85)])
        assert select_top_call(ranked) is None


# --------------------------------------------------------------------------
# Actionable ideas
# --------------------------------------------------------------------------
class TestActionableIdeas:
    def test_dedup_by_ticker_keeps_highest(self):
        from build_morning_note import Development

        ranked = rank_developments(
            [
                Development("earnings", "FCTA beat", priority=80, direction="long", ticker="FCTA"),
                Development("mover", "FCTA gap", priority=75, direction="long", ticker="FCTA"),
            ]
        )
        ideas = build_actionable_ideas(ranked)
        assert len(ideas) == 1
        assert ideas[0]["thesis"] == "FCTA beat"

    def test_respects_max_ideas(self):
        from build_morning_note import Development

        devs = [
            Development("mover", f"t{i}", priority=60, direction="long", ticker=f"T{i}")
            for i in range(6)
        ]
        assert len(build_actionable_ideas(rank_developments(devs), max_ideas=3)) == 3

    def test_skips_directionless_and_tickerless(self):
        from build_morning_note import Development

        ranked = [
            Development("macro", "cpi", priority=85, direction="none"),
            Development("news", "surge", priority=90, direction="long", ticker=None),
        ]
        assert build_actionable_ideas(ranked) == []


# --------------------------------------------------------------------------
# Full assembly
# --------------------------------------------------------------------------
class TestAssemble:
    def _full_inputs(self):
        return NoteInputs(
            earnings=_load("earnings.json"),
            sector=_load("sector.json"),
            economic=_load("economic.json"),
            news=[_load("news_breaking.json"), _load("news_calm.json")],
            movers=_load("movers.json"),
        )

    def test_lead_is_breaking_news(self):
        note = assemble_note(self._full_inputs(), as_of="2026-07-03")
        assert note["lead"]["category"] == "news"
        assert "takeover" in note["lead"]["headline"]

    def test_top_call_is_directional(self):
        note = assemble_note(self._full_inputs(), as_of="2026-07-03")
        # Breaking news leads but is directionless; top call must be a
        # directional idea (earnings beat / mover) with a ticker.
        assert note["top_call"]["direction"] in ("long", "short")
        assert note["top_call"]["ticker"] is not None

    def test_inputs_provided_and_missing_tracked(self):
        inputs = NoteInputs(earnings=_load("earnings.json"))
        note = assemble_note(inputs, as_of="2026-07-03")
        assert "earnings" in note["inputs_provided"]
        assert "news" in note["inputs_missing"]
        assert "sector" in note["inputs_missing"]

    def test_macro_today_excludes_other_dates(self):
        note = assemble_note(self._full_inputs(), as_of="2026-07-03")
        macro_heads = [d["headline"] for d in note["macro_today"]]
        assert not any("German" in h for h in macro_heads)

    def test_empty_inputs_no_news_note(self):
        note = assemble_note(NoteInputs(), as_of="2026-07-03")
        assert note["lead"] is None
        assert note["top_call"] is None
        assert note["actionable_ideas"] == []
        md = render_markdown(note)
        assert "Nothing material overnight" in md

    def test_stable_generated_at_override(self):
        note = assemble_note(
            self._full_inputs(), as_of="2026-07-03", generated_at="2026-07-03T06:00:00+00:00"
        )
        assert note["generated_at"] == "2026-07-03T06:00:00+00:00"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
class TestRender:
    def test_markdown_has_fixed_sections(self):
        note = assemble_note(
            NoteInputs(
                earnings=_load("earnings.json"),
                sector=_load("sector.json"),
                economic=_load("economic.json"),
                news=[_load("news_breaking.json")],
                movers=_load("movers.json"),
            ),
            as_of="2026-07-03",
            analyst="Test Desk",
        )
        md = render_markdown(note)
        assert "# 2026-07-03 Morning Note" in md
        assert "## Lead:" in md
        assert "## Top Call" in md
        assert "## Actionable Ideas" in md
        assert "## Macro Calendar — Today" in md
        assert "## Sector Read" in md
        assert "Test Desk" in md

    def test_markdown_ends_with_newline(self):
        note = assemble_note(NoteInputs(), as_of="2026-07-03")
        assert render_markdown(note).endswith("\n")


# --------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------
class TestLoaders:
    def test_load_missing_returns_none(self, tmp_path):
        assert load_json_file(tmp_path / "nope.json") is None

    def test_load_news_inputs_multiple(self):
        items = load_news_inputs([FIX / "news_breaking.json", FIX / "news_calm.json"])
        assert len(items) == 2

    def test_load_news_inputs_none(self):
        assert load_news_inputs(None) == []


# --------------------------------------------------------------------------
# CLI (writes to tmp_path only)
# --------------------------------------------------------------------------
class TestCLI:
    def test_main_writes_json_and_md(self, tmp_path):
        rc = main(
            [
                "--earnings",
                str(FIX / "earnings.json"),
                "--sector",
                str(FIX / "sector.json"),
                "--economic",
                str(FIX / "economic.json"),
                "--news",
                str(FIX / "news_breaking.json"),
                str(FIX / "news_calm.json"),
                "--movers",
                str(FIX / "movers.json"),
                "--as-of",
                "2026-07-03",
                "--output-dir",
                str(tmp_path),
            ]
        )
        assert rc == 0
        json_path = tmp_path / "morning_note_2026-07-03.json"
        md_path = tmp_path / "morning_note_2026-07-03.md"
        assert json_path.exists() and md_path.exists()
        data = json.loads(json_path.read_text())
        assert data["schema_version"] == "1.0"
        assert data["lead"]["category"] == "news"

    def test_main_json_only(self, tmp_path):
        rc = main(
            [
                "--earnings",
                str(FIX / "earnings.json"),
                "--as-of",
                "2026-07-03",
                "--output-dir",
                str(tmp_path),
                "--json-only",
            ]
        )
        assert rc == 0
        assert (tmp_path / "morning_note_2026-07-03.json").exists()
        assert not (tmp_path / "morning_note_2026-07-03.md").exists()
