"""Offline tests for ingest_retail_sentiment.py.

Pure scoring, parsing, combine, and vault-note writing exercised against saved
StockTwits / Reddit / X JSON fixtures. No network, no model, no repo reports/.
"""

import json
from pathlib import Path

import ingest_retail_sentiment as m
import yaml

FIX = Path(__file__).resolve().parent / "fixtures"


def _load(name):
    return json.loads((FIX / name).read_text())


# --------------------------------------------------------------------------- #
# Ticker normalization
# --------------------------------------------------------------------------- #


def test_normalize_ticker():
    assert m.normalize_ticker("$nvda") == "NVDA"
    assert m.normalize_ticker("  AMD ") == "AMD"
    assert m.normalize_ticker("BRK.B") == "BRK.B"
    assert m.normalize_ticker("STRL/POWL") is None  # multi-symbol junk
    assert m.normalize_ticker("A,B") is None
    assert m.normalize_ticker("") is None
    assert m.normalize_ticker(None) is None


# --------------------------------------------------------------------------- #
# Text polarity
# --------------------------------------------------------------------------- #


def test_polarity_from_text():
    assert m.polarity_from_text("buying calls, bullish breakout") > 0
    assert m.polarity_from_text("shorting puts, bearish dump") < 0
    assert m.polarity_from_text("what is the dividend schedule") == 0


def test_band_and_direction():
    assert m.band_from_score(9.0) == "Bullish"
    assert m.band_from_score(6.0) == "Mildly Bullish"
    assert m.band_from_score(5.0) == "Neutral"
    assert m.band_from_score(4.0) == "Mildly Bearish"
    assert m.band_from_score(1.0) == "Bearish"
    assert m.direction_from_band("Bullish") == "long"
    assert m.direction_from_band("Bearish") == "short"
    assert m.direction_from_band("Mixed") == "watch"


# --------------------------------------------------------------------------- #
# StockTwits
# --------------------------------------------------------------------------- #


def test_parse_stocktwits_labels():
    msgs = m.parse_stocktwits(_load("stocktwits_nvda.json"))
    assert len(msgs) == 12
    assert sum(1 for x in msgs if x["sentiment"] == "Bullish") == 10
    assert sum(1 for x in msgs if x["sentiment"] == "Bearish") == 1
    assert sum(1 for x in msgs if x["sentiment"] is None) == 1


def test_score_stocktwits_overextension_flag():
    s = m.score_stocktwits(m.parse_stocktwits(_load("stocktwits_nvda.json")))
    assert s["labeled"] == 11 and s["bullish"] == 10 and s["bearish"] == 1
    assert s["bull_pct"] == 91
    assert s["score"] == 8.0 and s["band"] == "Bullish"
    # >=90/10 bullish on a real sample fires the contrarian over-extension flag.
    assert s["contrarian_overextension"] is True
    assert s["overextension_side"] == "bullish"


def test_stocktwits_small_sample_regresses_to_neutral():
    # Two bullish messages must NOT read 10/10 — the base rate pulls it toward 5.
    two_bull = [{"sentiment": "Bullish"}, {"sentiment": "Bullish"}]
    small = m.score_stocktwits(two_bull)
    assert small["score"] < 8.0  # (2+2)/(2+4) = 0.667 -> 6.67
    assert small["contrarian_overextension"] is False  # below OVEREXT_MIN_LABELED


def test_stocktwits_all_unlabeled_is_neutral_base_rate():
    s = m.score_stocktwits([{"sentiment": None}, {"sentiment": None}])
    assert s["labeled"] == 0 and s["score"] == 5.0 and s["band"] == "Neutral"
    assert s["has_data"] is True


# --------------------------------------------------------------------------- #
# Reddit (engagement-weighted)
# --------------------------------------------------------------------------- #


def test_score_reddit_engagement_weighted():
    s = m.score_reddit(m.parse_reddit_json(_load("reddit_nvda.json")))
    assert s["n_posts"] == 4
    assert s["n_directional"] == 3 and s["bull_posts"] == 2 and s["bear_posts"] == 1
    assert 5.5 <= s["score"] < 6.5 and s["band"] == "Mildly Bullish"
    assert s["engagement_total"] == 1492.0
    assert s["via_rss"] is False


def test_reddit_engagement_outweighs_a_low_score_post():
    # A high-engagement bullish post should dominate a low-engagement bearish one.
    posts = [
        {"title": "buying calls breakout", "selftext": "", "score": 900, "num_comments": 300},
        {"title": "puts bearish dump", "selftext": "", "score": 2, "num_comments": 1},
    ]
    s = m.score_reddit(posts)
    assert s["score"] > 5.0 and s["band"] in ("Mildly Bullish", "Bullish")


def test_reddit_rss_posts_equal_weighted():
    posts = m.parse_reddit_rss(
        """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><title>NVDA calls breakout bullish</title>
            <content>&lt;!-- SC_OFF --&gt;&lt;div&gt;buying more&lt;/div&gt;&lt;!-- SC_ON --&gt;</content>
          </entry>
          <entry><title>NVDA puts bearish short</title><content>dump incoming</content></entry>
        </feed>"""
    )
    assert len(posts) == 2 and all(p["score"] is None for p in posts)
    s = m.score_reddit(posts)
    assert s["via_rss"] is True and s["n_directional"] == 2


# --------------------------------------------------------------------------- #
# X (optional)
# --------------------------------------------------------------------------- #


def test_parse_x_impression_ranked():
    posts = m.parse_x(_load("x_nvda.json"))
    assert len(posts) == 5
    # Sorted by impressions descending (loudest first).
    assert posts[0]["impressions"] == 82000
    assert posts[0]["author"] == "macro_finch"


def test_score_x_impression_weighted():
    s = m.score_x(m.parse_x(_load("x_nvda.json")))
    assert s["n_directional"] == 4 and s["bull_posts"] == 3 and s["bear_posts"] == 1
    assert s["score"] >= 6.5 and s["band"] == "Bullish"
    assert s["impression_total"] == 141900.0


# --------------------------------------------------------------------------- #
# Cross-source combine
# --------------------------------------------------------------------------- #


def test_combine_aligned_bullish():
    st = m.score_stocktwits(m.parse_stocktwits(_load("stocktwits_nvda.json")))
    rd = m.score_reddit(m.parse_reddit_json(_load("reddit_nvda.json")))
    c = m.combine_sentiment(st, rd, None)
    assert c["overall_band"] == "Bullish" and c["direction"] == "long"
    assert c["divergence"] is False
    assert c["contrarian_overextension"] is True  # inherited from StockTwits
    assert c["n_sources_with_data"] == 2


def test_combine_divergence_forces_mixed_watch():
    st = m.score_stocktwits(m.parse_stocktwits(_load("stocktwits_nvda.json")))  # bullish
    rd = m.score_reddit(m.parse_reddit_json(_load("reddit_bearish.json")))  # bearish
    c = m.combine_sentiment(st, rd, None)
    assert c["divergence"] is True
    assert c["overall_band"] == "Mixed" and c["direction"] == "watch"
    assert c["divergence_sources"] == ["reddit", "stocktwits"]


def test_combine_three_sources():
    st = m.score_stocktwits(m.parse_stocktwits(_load("stocktwits_nvda.json")))
    rd = m.score_reddit(m.parse_reddit_json(_load("reddit_nvda.json")))
    x = m.score_x(m.parse_x(_load("x_nvda.json")))
    c = m.combine_sentiment(st, rd, x)
    assert c["n_sources_with_data"] == 3
    assert c["sources_present"] == ["stocktwits", "reddit", "x"]
    assert c["direction"] == "long"


def test_combine_no_data_returns_none():
    empty_st = m.score_stocktwits([])
    empty_rd = m.score_reddit([])
    assert m.combine_sentiment(empty_st, empty_rd, None) is None


def test_analyze_ticker_end_to_end():
    a = m.analyze_ticker(
        m.parse_stocktwits(_load("stocktwits_nvda.json")),
        m.parse_reddit_json(_load("reddit_nvda.json")),
        None,
    )
    assert a["combined"]["direction"] == "long"
    assert a["stocktwits"]["has_data"] and a["reddit"]["has_data"]
    assert a["x"] is None


# --------------------------------------------------------------------------- #
# Vault writers (schema compatibility) — tmp dir only, never repo reports/
# --------------------------------------------------------------------------- #


def _frontmatter(text):
    assert text.startswith("---")
    end = text.find("\n---", 3)
    return yaml.safe_load(text[3:end])


def test_write_ticker_notes_matches_vault_schema(tmp_path):
    paths = {
        "vault_current": tmp_path / "vault" / "current",
        "raw": tmp_path / "raw",
        "state": tmp_path / "state",
    }
    a = m.analyze_ticker(
        m.parse_stocktwits(_load("stocktwits_nvda.json")),
        m.parse_reddit_json(_load("reddit_nvda.json")),
        None,
    )
    raw = {"stocktwits": _load("stocktwits_nvda.json"), "reddit": _load("reddit_nvda.json")}
    manifest = m.write_ticker_notes("NVDA", a, paths, "2026-07-03", "2026-W27", raw)

    # Signal note frontmatter matches the fields build_signal_index.py reads.
    sig = Path(manifest["signal_note"])
    assert sig.name == "2026-07-03_NVDA_retail-sentiment.md"
    fm = _frontmatter(sig.read_text())
    assert fm["type"] == "signal"
    assert fm["ticker"] == "NVDA"
    assert fm["direction"] == "long"
    # claim_date is an unquoted YAML date (same as social-signal-ingestor notes);
    # build_signal_index serializes it to a string via json default=str downstream.
    assert str(fm["claim_date"]) == "2026-07-03"
    assert fm["instrument"] == "stock"
    assert "watch" not in fm  # no clean numeric levels -> no watch block
    assert "probability" not in fm  # no real basis -> omitted

    # Sources are Obsidian wikilinks pointing at the two source notes. YAML
    # double-nests `- [[x]]`; build_signal_index.normalize_sources flattens it
    # with the same while-loop, so this confirms schema compatibility.
    def _flatten(v):
        while isinstance(v, list) and len(v) == 1:
            v = v[0]
        return v

    srcs = [_flatten(s) for s in fm["sources"]]
    assert "sources/stocktwits/2026-07-03_NVDA" in srcs
    assert "sources/reddit/2026-07-03_NVDA" in srcs
    assert "contrarian-overextension" in fm["tags"]

    # Source notes written per platform with type: source.
    assert len(manifest["source_notes"]) == 2
    st_note = paths["vault_current"] / "sources" / "stocktwits" / "2026-07-03_NVDA.md"
    st_fm = _frontmatter(st_note.read_text())
    assert st_fm["type"] == "source" and st_fm["source_type"] == "stocktwits"

    # Raw artifact saved under raw/, not echoed into the notes.
    assert (paths["raw"] / "stocktwits" / "2026-07-03" / "NVDA.json").exists()
    assert "trader_alpha" not in sig.read_text()  # usernames never leak into the signal note


def test_run_report_render(tmp_path):
    import datetime as dt

    a = m.analyze_ticker(
        m.parse_stocktwits(_load("stocktwits_nvda.json")),
        m.parse_reddit_json(_load("reddit_nvda.json")),
        None,
    )
    paths = {"vault_current": tmp_path / "v", "raw": tmp_path / "r", "state": tmp_path / "s"}
    manifest = m.write_ticker_notes("NVDA", a, paths, "2026-07-03", "2026-W27", {})
    report = m.build_run_report(
        [manifest], ["ZZZZ"], dt.datetime(2026, 7, 3, tzinfo=dt.timezone.utc)
    )
    md = m.render_run_markdown(report)
    assert "Retail Sentiment Ingest" in md
    assert "NVDA" in md and "Bullish" in md
    assert "ZZZZ" in md  # skipped ticker reported
