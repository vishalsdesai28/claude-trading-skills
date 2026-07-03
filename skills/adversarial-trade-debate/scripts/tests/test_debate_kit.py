"""Offline tests for debate_kit — the deterministic layer of adversarial-trade-debate.

All tests run against committed fixtures with no network. Report writes are
directed to pytest's tmp_path, never the repo reports/ directory.
"""

import json
import os

import debate_kit as dk

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fx(name):
    return os.path.join(FIXTURES, name)


# --------------------------------------------------------------------------- #
# coercion helpers
# --------------------------------------------------------------------------- #


def test_coerce_float_variants():
    assert dk.coerce_float("$101.50") == 101.5
    assert dk.coerce_float("1,234.5") == 1234.5
    assert dk.coerce_float("30%") == 30.0
    assert dk.coerce_float(130) == 130.0
    assert dk.coerce_float("**92.00**") == 92.0


def test_coerce_float_nullish_and_junk():
    for junk in ("N/A", "none", "", "TBD", "—", "unknown"):
        assert dk.coerce_float(junk) is None
    assert dk.coerce_float(True) is None


def test_match_enum():
    assert dk.match_enum("Buy", dk.RATING_VALUES) == "Buy"
    assert dk.match_enum("overweight", dk.RATING_VALUES) == "Overweight"
    assert dk.match_enum("Sell — strong conviction", dk.RATING_VALUES) == "Sell"
    assert dk.match_enum("Strong Buy", dk.RATING_VALUES) is None
    assert dk.match_enum(None, dk.RATING_VALUES) is None


# --------------------------------------------------------------------------- #
# summarizers (documented sibling output shapes)
# --------------------------------------------------------------------------- #


def test_summarize_dcf():
    summary = dk.summarize_dcf(dk.load_json(_fx("dcf_sample.json")))
    assert summary["current_price"] == 100.0
    assert summary["fair_value"] == 130.0
    assert summary["upside_pct"] == 0.30
    assert summary["sector"] == "Technology"
    assert len(summary["guardrail_warnings"]) == 1


def test_summarize_dcf_tolerates_missing_blocks():
    summary = dk.summarize_dcf({})
    assert summary["current_price"] is None
    assert summary["fair_value"] is None
    assert summary["guardrail_warnings"] == []


def test_summarize_sentiment_found_and_missing():
    report = dk.load_json(_fx("sentiment_run.json"))
    acme = dk.summarize_sentiment(report, "acme")  # case-insensitive
    assert acme["band"] == "Bullish"
    assert acme["score"] == 7.4
    assert acme["contrarian_overextension"] is True
    assert dk.summarize_sentiment(report, "NOPE") is None


def test_summarize_technical_markdown_and_json():
    md = dk.summarize_technical("# TA\n- trend up", is_json=False)
    assert md["format"] == "markdown"
    assert "trend up" in md["text"]
    js = dk.summarize_technical('{"trend": "up"}', is_json=True)
    assert js["format"] == "json"
    assert js["data"]["trend"] == "up"


# --------------------------------------------------------------------------- #
# brief assembly
# --------------------------------------------------------------------------- #


def test_build_debate_brief_lanes_tracking():
    val = dk.summarize_dcf(dk.load_json(_fx("dcf_sample.json")))
    brief = dk.build_debate_brief("acme", valuation=val)
    assert brief["ticker"] == "ACME"
    assert "valuation" in brief["lanes_present"]
    # missing lanes are flagged so the debate cannot fabricate them
    assert set(brief["lanes_missing"]) == {"technical", "sentiment", "news"}


def test_render_brief_markdown_flags_missing_and_shows_values():
    val = dk.summarize_dcf(dk.load_json(_fx("dcf_sample.json")))
    sent = dk.summarize_sentiment(dk.load_json(_fx("sentiment_run.json")), "ACME")
    tech = dk.summarize_technical(open(_fx("technical_sample.md")).read())
    brief = dk.build_debate_brief("ACME", valuation=val, sentiment=sent, technical=tech)
    md = dk.render_brief_markdown(brief)
    assert "Blended fair value: 130.0 (upside 30.0%)" in md
    assert "Bullish" in md
    assert "Bull continuation (55%)" in md
    assert "Lanes MISSING" in md  # news lane absent
    assert "Guardrail:" in md


# --------------------------------------------------------------------------- #
# deterministic-header parser (free-text fallback recovery)
# --------------------------------------------------------------------------- #


def test_parse_deterministic_headers_full():
    rec = dk.parse_deterministic_headers(open(_fx("decision_headers.md")).read())
    assert rec["parse_source"] == "deterministic_headers"
    assert rec["rating"] == "Overweight"  # from **Rating** / **Recommendation**
    assert rec["action"] == "Buy"
    assert rec["entry_price"] == 101.5
    assert rec["stop_loss"] == 92.0
    assert rec["price_target"] == 130.0
    assert rec["time_horizon"] == "6-12 months"
    assert rec["position_sizing"] == "~5% of portfolio"
    assert rec["warnings"] == []


def test_parse_first_rating_header_wins():
    # ResearchPlan Recommendation appears before PM Rating; both are Overweight
    # here, but the parser must map both labels to the single rating field.
    text = "**Recommendation**: Buy\n\n**Rating**: Sell\n"
    rec = dk.parse_deterministic_headers(text)
    assert rec["rating"] == "Buy"  # first occurrence wins


def test_parse_bad_rating_and_action_warns():
    rec = dk.parse_deterministic_headers(open(_fx("decision_bad_rating.md")).read())
    assert rec["rating"] is None
    assert rec["rating_raw"] == "Strong Buy"
    assert rec["action"] is None
    assert rec["action_raw"] == "Accumulate"
    assert rec["entry_price"] is None  # "N/A" coerces to None
    assert any("unrecognized rating" in w for w in rec["warnings"])


def test_parse_flags_stop_above_entry():
    text = "**Rating**: Buy\n**Entry Price**: 90\n**Stop Loss**: 95\n"
    rec = dk.parse_deterministic_headers(text)
    assert any("stop_loss" in w for w in rec["warnings"])


def test_parse_no_rating_header_warns():
    rec = dk.parse_deterministic_headers("just some prose, no headers")
    assert rec["rating"] is None
    assert any("no rating" in w for w in rec["warnings"])


# --------------------------------------------------------------------------- #
# position-sizer hand-off
# --------------------------------------------------------------------------- #


def test_handoff_eligible_long():
    rec = dk.parse_deterministic_headers(open(_fx("decision_headers.md")).read())
    handoff = dk.position_sizer_handoff(rec, account_size=100000, risk_pct=1.0)
    assert handoff["eligible"] is True
    assert "--entry 101.5" in handoff["suggested_command"]
    assert "--stop 92" in handoff["suggested_command"]
    assert "--account-size 100000" in handoff["suggested_command"]


def test_handoff_placeholder_account():
    rec = {"rating": "Buy", "action": "Buy", "entry_price": 50.0, "stop_loss": 45.0}
    handoff = dk.position_sizer_handoff(rec)
    assert handoff["eligible"] is True
    assert "<ACCOUNT_SIZE>" in handoff["suggested_command"]


def test_handoff_not_long_side():
    rec = {"rating": "Sell", "action": "Sell", "entry_price": 50.0, "stop_loss": 55.0}
    handoff = dk.position_sizer_handoff(rec)
    assert handoff["eligible"] is False
    assert "not long-side" in handoff["reason"]


def test_handoff_missing_levels():
    rec = {"rating": "Buy", "action": "Buy", "entry_price": None, "stop_loss": None}
    handoff = dk.position_sizer_handoff(rec)
    assert handoff["eligible"] is False
    assert "missing entry" in handoff["reason"]


def test_handoff_bad_stop():
    rec = {"rating": "Overweight", "entry_price": 50.0, "stop_loss": 55.0}
    handoff = dk.position_sizer_handoff(rec)
    assert handoff["eligible"] is False
    assert "not below entry_price" in handoff["reason"]


# --------------------------------------------------------------------------- #
# CLI (writes only to tmp_path)
# --------------------------------------------------------------------------- #


def test_cli_assemble_writes_reports(tmp_path):
    rc = dk.main(
        [
            "assemble",
            "--ticker",
            "ACME",
            "--dcf",
            _fx("dcf_sample.json"),
            "--sentiment",
            _fx("sentiment_run.json"),
            "--technical",
            _fx("technical_sample.md"),
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert rc == 0
    jsons = list(tmp_path.glob("debate_brief_ACME_*.json"))
    mds = list(tmp_path.glob("debate_brief_ACME_*.md"))
    assert jsons and mds
    payload = json.loads(jsons[0].read_text())
    assert payload["ticker"] == "ACME"
    assert payload["valuation"]["fair_value"] == 130.0
    assert payload["sentiment"]["band"] == "Bullish"


def test_cli_assemble_requires_an_input(tmp_path):
    rc = dk.main(["assemble", "--ticker", "ACME", "--output-dir", str(tmp_path)])
    assert rc == 1
    assert not list(tmp_path.glob("*.json"))


def test_cli_parse_decision_writes_reports(tmp_path):
    rc = dk.main(
        [
            "parse-decision",
            "--input",
            _fx("decision_headers.md"),
            "--account-size",
            "100000",
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert rc == 0
    jsons = list(tmp_path.glob("debate_decision_*.json"))
    assert jsons
    payload = json.loads(jsons[0].read_text())
    assert payload["decision"]["rating"] == "Overweight"
    assert payload["position_sizer_handoff"]["eligible"] is True
