"""Offline tests for polymarket_odds.py.

All Gamma API access is either replaced by a saved fixture payload or a patched
``search_gamma`` -- these run without a network connection.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import polymarket_odds as pm

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "gamma_search_fed.json"
NOW = datetime(2026, 7, 3, tzinfo=timezone.utc)


def _payload():
    return json.loads(FIXTURE.read_text())


# --------------------------------------------------------------------------- #
# parse_json_list                                                             #
# --------------------------------------------------------------------------- #
def test_parse_json_list_handles_list_string_and_garbage():
    assert pm.parse_json_list(["Yes", "No"]) == ["Yes", "No"]
    assert pm.parse_json_list('["0.76", "0.24"]') == ["0.76", "0.24"]
    assert pm.parse_json_list("not json") == []
    assert pm.parse_json_list(None) == []
    assert pm.parse_json_list('{"a": 1}') == []  # decodes to dict, not a list


# --------------------------------------------------------------------------- #
# implied_probabilities                                                       #
# --------------------------------------------------------------------------- #
def test_implied_probabilities_binary_and_multi_outcome():
    binary = {"outcomes": '["Yes", "No"]', "outcomePrices": '["0.76", "0.24"]'}
    probs = pm.implied_probabilities(binary)
    assert probs == [
        {"label": "Yes", "probability": 0.76},
        {"label": "No", "probability": 0.24},
    ]

    multi = {
        "outcomes": '["0", "1", "2", "3+"]',
        "outcomePrices": '["0.10", "0.30", "0.40", "0.20"]',
    }
    labels = [p["label"] for p in pm.implied_probabilities(multi)]
    assert labels == ["0", "1", "2", "3+"]


def test_implied_probabilities_skips_unparseable_price():
    m = {"outcomes": '["Yes", "No"]', "outcomePrices": '["0.9", "junk"]'}
    probs = pm.implied_probabilities(m)
    assert probs == [{"label": "Yes", "probability": 0.9}]


# --------------------------------------------------------------------------- #
# is_forward_looking                                                          #
# --------------------------------------------------------------------------- #
def test_is_forward_looking_excludes_closed_and_past():
    open_future = {
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.5", "0.5"]',
        "endDate": "2030-12-31T00:00:00Z",
        "closed": False,
    }
    closed = dict(open_future, closed=True)
    past = dict(open_future, endDate="2019-01-01T00:00:00Z")
    assert pm.is_forward_looking(open_future, NOW) is True
    assert pm.is_forward_looking(closed, NOW) is False
    assert pm.is_forward_looking(past, NOW) is False


# --------------------------------------------------------------------------- #
# rank_markets                                                                #
# --------------------------------------------------------------------------- #
def test_rank_markets_filters_and_orders_by_volume():
    markets = pm.rank_markets(_payload(), NOW, limit=10)
    questions = [m["question"] for m in markets]
    # closed + past-dated markets dropped
    assert "Did the Fed hike in Jan 2020? (resolved)" not in questions
    assert "Past-dated recession call?" not in questions
    # ranked by traded volume descending: 5.2M > 3.0M > 1.5k
    assert questions == [
        "Will the Fed cut rates in 2026?",
        "How many Fed cuts in 2026?",
        "Will there be an emergency 50bp cut?",
    ]


def test_rank_markets_respects_limit():
    markets = pm.rank_markets(_payload(), NOW, limit=1)
    assert len(markets) == 1
    assert markets[0]["question"] == "Will the Fed cut rates in 2026?"


def test_top_market_implied_probability_and_weekly_move():
    top = pm.rank_markets(_payload(), NOW, limit=10)[0]
    assert top["outcome"] == "Yes"
    assert top["implied_probability"] == 0.76
    assert top["implied_probability_pct"] == 76.0
    assert top["volume_usd"] == 5200000.0
    assert top["resolves"] == "2030-12-31"
    assert top["one_week_change_pp"] == -4.5  # -0.045 -> -4.5pp


def test_multi_outcome_market_keeps_all_outcomes():
    markets = pm.rank_markets(_payload(), NOW, limit=10)
    multi = next(m for m in markets if m["question"] == "How many Fed cuts in 2026?")
    assert len(multi["outcomes"]) == 4
    assert multi["one_week_change_pp"] is None or isinstance(multi["one_week_change_pp"], float)


# --------------------------------------------------------------------------- #
# get_base_rates                                                              #
# --------------------------------------------------------------------------- #
def test_get_base_rates_offline_with_payload():
    res = pm.get_base_rates("Fed rate cut", now=NOW, search_payload=_payload())
    assert res["available"] is True
    assert res["error"] is None
    assert res["topic"] == "Fed rate cut"
    assert res["markets"][0]["implied_probability_pct"] == 76.0


def test_get_base_rates_degrades_on_network_error(monkeypatch):
    def _boom(topic, timeout=pm.REQUEST_TIMEOUT):
        raise RuntimeError("network down")

    monkeypatch.setattr(pm, "search_gamma", _boom)
    res = pm.get_base_rates("Fed rate cut", now=NOW)  # no payload -> hits patched fetch
    assert res["available"] is False
    assert "network down" in res["error"]
    assert res["markets"] == []


# --------------------------------------------------------------------------- #
# build_report + render_markdown                                              #
# --------------------------------------------------------------------------- #
def test_build_report_applies_fixture_to_all_topics():
    report = pm.build_report(["Fed rate cut", "recession"], now=NOW, fixture=_payload())
    assert report["topics"] == ["Fed rate cut", "recession"]
    assert report["source"] == "Polymarket Gamma API (free, keyless)"
    assert len(report["results"]) == 2
    assert all(r["available"] for r in report["results"])


def test_render_markdown_contents():
    report = pm.build_report(["Fed rate cut"], now=NOW, fixture=_payload())
    md = pm.render_markdown(report)
    assert "# Polymarket Forward Base Rates" in md
    assert "76.0%" in md
    assert "$5,200,000" in md
    assert "-4.5pp" in md
    assert "How many Fed cuts in 2026?" in md
    # resolved / past markets must not leak into the report
    assert "resolved" not in md.lower()


# --------------------------------------------------------------------------- #
# CLI main                                                                    #
# --------------------------------------------------------------------------- #
def test_main_writes_json_and_md_to_output_dir(tmp_path):
    rc = pm.main(
        [
            "Fed rate cut",
            "--fixture",
            str(FIXTURE),
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert rc == 0
    json_files = list(tmp_path.glob("polymarket_odds_*.json"))
    md_files = list(tmp_path.glob("polymarket_odds_*.md"))
    assert len(json_files) == 1
    assert len(md_files) == 1

    data = json.loads(json_files[0].read_text())
    assert data["results"][0]["markets"][0]["implied_probability_pct"] == 76.0
    assert "76.0%" in md_files[0].read_text()


def test_main_stdout_mode_writes_no_files(tmp_path, capsys):
    rc = pm.main(["Fed rate cut", "--fixture", str(FIXTURE), "--stdout"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Polymarket Forward Base Rates" in out
    assert list(tmp_path.iterdir()) == []


def test_main_bad_fixture_path_returns_error(tmp_path):
    missing = tmp_path / "nope.json"
    rc = pm.main(["Fed rate cut", "--fixture", str(missing), "--output-dir", str(tmp_path)])
    assert rc == 1
