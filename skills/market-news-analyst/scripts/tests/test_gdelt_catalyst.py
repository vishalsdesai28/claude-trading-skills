"""Tests for gdelt_catalyst.py — GDELT/RSS parsing, surge detection, blackout signal.

All tests run offline against saved fixtures:
  fixtures/gdelt_artlist.json          — 4 articles (3 fresh, 1 stale @ 2026-06-15)
  fixtures/gdelt_timeline_breaking.json — coverage [10,8,12,10,40] -> surge 4.0x (BREAKING)
  fixtures/gdelt_timeline_quiet.json    — coverage [10,11,9,10,10] -> surge 1.0x (quiet)
  fixtures/yahoo_rss.xml                — 3 items (NVDA hit fresh, Fed miss, NVDA hit stale)

The freshness "now" is pinned to 2026-07-03T15:00:00Z so timestamp-relative
assertions are deterministic.
"""

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from gdelt_catalyst import (
    Article,
    blackout_signal,
    build_catalyst_report,
    build_gdelt_query,
    detect_surge,
    filter_fresh,
    filter_keywords,
    generate_markdown_report,
    parse_gdelt_artlist,
    parse_gdelt_date,
    parse_gdelt_timeline,
    parse_rss,
    parse_rss_date,
    query_keywords,
    report_to_dict,
    surge_severity,
)

FIX = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 7, 3, 15, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def artlist():
    with open(FIX / "gdelt_artlist.json") as f:
        return json.load(f)


@pytest.fixture
def timeline_breaking():
    with open(FIX / "gdelt_timeline_breaking.json") as f:
        return json.load(f)


@pytest.fixture
def timeline_quiet():
    with open(FIX / "gdelt_timeline_quiet.json") as f:
        return json.load(f)


@pytest.fixture
def rss_articles():
    return parse_rss((FIX / "yahoo_rss.xml").read_text(), source="finance.yahoo.com")


# ─── Query building ──────────────────────────────────────────────────────────


class TestQueryBuilding:
    def test_keyword_takes_precedence(self):
        assert build_gdelt_query(ticker="NVDA", keyword="Nvidia earnings") == "Nvidia earnings"

    def test_ticker_is_scoped(self):
        assert build_gdelt_query(ticker="F") == '"F" (stock OR shares OR earnings OR SEC)'

    def test_empty(self):
        assert build_gdelt_query() == ""

    def test_query_keywords(self):
        assert query_keywords("NVDA", "Nvidia") == ["NVDA", "Nvidia"]
        assert query_keywords(None, "  Apple  ") == ["Apple"]
        assert query_keywords(None, None) == []


# ─── GDELT date + artlist parsing ────────────────────────────────────────────


class TestGdeltParse:
    def test_parse_date(self):
        dt = parse_gdelt_date("20260703T143000Z")
        assert dt == datetime(2026, 7, 3, 14, 30, 0, tzinfo=timezone.utc)

    def test_parse_date_bad(self):
        assert parse_gdelt_date("not-a-date") is None
        assert parse_gdelt_date("") is None
        assert parse_gdelt_date(None) is None

    def test_artlist_count_and_sort(self, artlist):
        arts = parse_gdelt_artlist(artlist)
        assert len(arts) == 4
        # newest first: 07-03 14:30 reuters at top
        assert arts[0].domain == "reuters.com"
        assert "export curbs" in arts[0].title
        assert all(a.source == "gdelt" for a in arts)

    def test_artlist_empty(self):
        assert parse_gdelt_artlist({}) == []
        assert parse_gdelt_artlist({"articles": None}) == []


# ─── Timeline + surge detection ──────────────────────────────────────────────


class TestSurge:
    def test_timeline_extract(self, timeline_breaking):
        assert parse_gdelt_timeline(timeline_breaking) == [10.0, 8.0, 12.0, 10.0, 40.0]

    def test_breaking(self, timeline_breaking):
        breaking, x = detect_surge(parse_gdelt_timeline(timeline_breaking))
        # baseline median([10,8,12,10]) = 10 ; 40/10 = 4.0
        assert x == 4.0
        assert breaking is True

    def test_quiet(self, timeline_quiet):
        breaking, x = detect_surge(parse_gdelt_timeline(timeline_quiet))
        assert x == 1.0
        assert breaking is False

    def test_too_few_points(self):
        assert detect_surge([10.0, 40.0]) == (False, 1.0)

    def test_elevated_not_breaking(self):
        # baseline 10, latest 16 -> 1.6x : elevated but under the 2.5 breaking bar
        breaking, x = detect_surge([10.0, 10.0, 10.0, 10.0, 16.0])
        assert x == 1.6
        assert breaking is False

    def test_zero_latest_not_breaking(self):
        breaking, x = detect_surge([10.0, 10.0, 10.0, 0.0])
        assert breaking is False

    def test_custom_threshold(self):
        breaking, x = detect_surge([10.0, 10.0, 10.0, 18.0], breaking_threshold=1.5)
        assert x == 1.8
        assert breaking is True


class TestSeverity:
    def test_high(self):
        assert surge_severity(4.0, True) == "high"

    def test_elevated(self):
        assert surge_severity(1.8, False) == "elevated"

    def test_none(self):
        assert surge_severity(1.0, False) == "none"


# ─── RSS parsing + filters ───────────────────────────────────────────────────


class TestRss:
    def test_parse_rss_date_rfc822(self):
        dt = parse_rss_date("Fri, 03 Jul 2026 13:00:00 +0000")
        assert dt == datetime(2026, 7, 3, 13, 0, 0, tzinfo=timezone.utc)

    def test_parse_rss_date_bad(self):
        assert parse_rss_date("garbage") is None

    def test_parse_rss_items(self, rss_articles):
        assert len(rss_articles) == 3
        titles = [a.title for a in rss_articles]
        assert any("data-center deal" in t for t in titles)
        assert all(a.source == "finance.yahoo.com" for a in rss_articles)

    def test_parse_rss_malformed(self):
        assert parse_rss("<rss><broken") == []

    def test_filter_keywords(self, rss_articles):
        kept = filter_keywords(rss_articles, ["NVDA", "Nvidia"])
        # 2 NVDA items (fresh + stale) match; the Fed item is dropped
        assert len(kept) == 2
        assert all("nvda" in a.title.lower() or "nvidia" in a.title.lower() for a in kept)

    def test_filter_keywords_empty_passthrough(self, rss_articles):
        assert filter_keywords(rss_articles, []) == rss_articles


class TestFreshness:
    def test_drops_stale_keeps_fresh(self, artlist):
        arts = parse_gdelt_artlist(artlist)
        fresh = filter_fresh(arts, 48, NOW)
        # the 2026-06-15 retrospective is > 48h old -> dropped; 3 remain
        assert len(fresh) == 3
        assert all("retrospective" not in a.title for a in fresh)

    def test_keeps_undated(self):
        a = Article(title="no date", url="u", domain="d", seen=None, source="rss")
        assert filter_fresh([a], 48, NOW) == [a]

    def test_window_disabled(self, artlist):
        arts = parse_gdelt_artlist(artlist)
        assert len(filter_fresh(arts, 0, NOW)) == 4


# ─── End-to-end report builder ───────────────────────────────────────────────


class TestBuildReport:
    def _report(self, artlist, timeline, rss_articles):
        return build_catalyst_report(
            query="Nvidia",
            ticker="NVDA",
            keyword="Nvidia",
            artlist_payload=artlist,
            timeline_payload=timeline,
            rss_articles=filter_keywords(rss_articles, ["NVDA", "Nvidia"]),
            fresh_window_hours=48,
            now=NOW,
        )

    def test_breaking_report(self, artlist, timeline_breaking, rss_articles):
        rep = self._report(artlist, timeline_breaking, rss_articles)
        assert rep.breaking is True
        assert rep.surge_x == 4.0
        assert rep.severity == "high"
        # 3 fresh GDELT + 1 fresh RSS (the stale NVDA RSS item dropped) = 4
        assert rep.n_recent == 4
        assert rep.sources == {"gdelt": 3, "rss": 1}
        # newest first -> reuters 14:30 leads
        assert rep.headlines[0].domain == "reuters.com"

    def test_quiet_report(self, artlist, timeline_quiet, rss_articles):
        rep = self._report(artlist, timeline_quiet, rss_articles)
        assert rep.breaking is False
        assert rep.elevated is False
        assert rep.severity == "none"
        assert rep.n_recent == 4  # freshness unaffected by the timeline

    def test_dedup_across_sources(self, timeline_quiet):
        # same URL from GDELT and RSS collapses to one
        dupe_art = {
            "articles": [
                {
                    "url": "http://x/1",
                    "title": "Dup story",
                    "seendate": "20260703T140000Z",
                    "domain": "x",
                }
            ]
        }
        rss = [Article(title="Dup story", url="http://x/1", domain="x", seen=NOW, source="rss")]
        rep = build_catalyst_report(
            "q", "T", None, dupe_art, timeline_quiet, rss_articles=rss, now=NOW
        )
        assert rep.n_recent == 1


# ─── Blackout signal ─────────────────────────────────────────────────────────


class TestBlackoutSignal:
    def test_breaking_blackout(self, artlist, timeline_breaking, rss_articles):
        rep = build_catalyst_report(
            "Nvidia",
            "NVDA",
            "Nvidia",
            artlist,
            timeline_breaking,
            rss_articles=filter_keywords(rss_articles, ["NVDA", "Nvidia"]),
            fresh_window_hours=48,
            now=NOW,
        )
        sig = blackout_signal(rep)
        assert sig["schema_version"] == "1.0"
        assert sig["signal"] == "news_blackout"
        assert sig["blackout"] is True
        assert sig["severity"] == "high"
        assert sig["ticker"] == "NVDA"
        assert sig["surge_x"] == 4.0
        assert sig["top_headline"]  # non-empty
        assert "suppress" in sig["recommended_action"].lower()

    def test_quiet_no_blackout(self, artlist, timeline_quiet, rss_articles):
        rep = build_catalyst_report(
            "Nvidia",
            "NVDA",
            "Nvidia",
            artlist,
            timeline_quiet,
            rss_articles=[],
            fresh_window_hours=48,
            now=NOW,
        )
        sig = blackout_signal(rep)
        assert sig["blackout"] is False
        assert sig["severity"] == "none"


# ─── Serialization + markdown ────────────────────────────────────────────────


class TestSerialization:
    def test_report_dict_json_roundtrip(self, artlist, timeline_breaking):
        rep = build_catalyst_report("Nvidia", "NVDA", "Nvidia", artlist, timeline_breaking, now=NOW)
        d = report_to_dict(rep)
        parsed = json.loads(json.dumps(d))
        assert parsed["schema_version"] == "1.0"
        assert parsed["coverage"]["breaking"] is True
        assert parsed["blackout_signal"]["blackout"] is True
        assert isinstance(parsed["headlines"], list)
        # headline seen serializes to a Z-suffixed ISO string
        assert parsed["headlines"][0]["seen"].endswith("Z")

    def test_markdown_contents(self, artlist, timeline_breaking, rss_articles):
        rep = build_catalyst_report(
            "Nvidia",
            "NVDA",
            "Nvidia",
            artlist,
            timeline_breaking,
            rss_articles=filter_keywords(rss_articles, ["NVDA", "Nvidia"]),
            now=NOW,
        )
        md = generate_markdown_report(rep)
        assert "# News Catalyst Scan — Nvidia" in md
        assert "BREAKING" in md
        assert "BLACKOUT" in md
        assert "Latest Headlines" in md
        assert "Risk-Gate Signal" in md
        assert "export curbs" in md


# ─── CLI (offline via saved fixtures) ────────────────────────────────────────


class TestCli:
    SCRIPT = "skills/market-news-analyst/scripts/gdelt_catalyst.py"

    def _repo_root(self):
        return Path(__file__).resolve().parents[4]

    def test_cli_breaking_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    self.SCRIPT,
                    "--ticker",
                    "NVDA",
                    "--keyword",
                    "Nvidia",
                    "--gdelt-json",
                    str(FIX / "gdelt_artlist.json"),
                    "--gdelt-timeline-json",
                    str(FIX / "gdelt_timeline_breaking.json"),
                    "--rss-xml",
                    str(FIX / "yahoo_rss.xml"),
                    "--now",
                    "2026-07-03T15:00:00Z",
                    "--output-dir",
                    tmp,
                ],
                capture_output=True,
                text=True,
                cwd=str(self._repo_root()),
            )
            assert result.returncode == 0, result.stderr
            assert "BLACKOUT" in result.stdout
            assert "BREAKING" in result.stdout
            outputs = list(Path(tmp).glob("news_catalyst_Nvidia_*"))
            assert any(p.suffix == ".json" for p in outputs)
            assert any(p.suffix == ".md" for p in outputs)
            # verify the JSON blackout signal is present and correct
            jpath = next(p for p in outputs if p.suffix == ".json")
            data = json.loads(jpath.read_text())
            assert data["blackout_signal"]["blackout"] is True
            assert data["coverage"]["n_recent"] == 4

    def test_cli_quiet_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    self.SCRIPT,
                    "--ticker",
                    "NVDA",
                    "--gdelt-json",
                    str(FIX / "gdelt_artlist.json"),
                    "--gdelt-timeline-json",
                    str(FIX / "gdelt_timeline_quiet.json"),
                    "--now",
                    "2026-07-03T15:00:00Z",
                    "--output-dir",
                    tmp,
                ],
                capture_output=True,
                text=True,
                cwd=str(self._repo_root()),
            )
            assert result.returncode == 0, result.stderr
            assert "clear" in result.stdout
            assert "BLACKOUT" not in result.stdout

    def test_cli_requires_ticker_or_keyword(self):
        result = subprocess.run(
            [sys.executable, self.SCRIPT, "--output-dir", "reports/"],
            capture_output=True,
            text=True,
            cwd=str(self._repo_root()),
        )
        assert result.returncode == 1
        assert "ticker" in result.stderr.lower()

    def test_cli_bad_gdelt_path(self):
        result = subprocess.run(
            [sys.executable, self.SCRIPT, "--ticker", "NVDA", "--gdelt-json", "/no/such/file.json"],
            capture_output=True,
            text=True,
            cwd=str(self._repo_root()),
        )
        assert result.returncode == 1
        assert "Error" in result.stderr
