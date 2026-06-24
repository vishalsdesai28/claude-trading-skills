"""Tests for enrich_tickers.py pure logic (no network / no yfinance)."""

import datetime as dt

import enrich_tickers as et

NOW = dt.datetime(2026, 6, 27, tzinfo=dt.timezone.utc)


def test_group_by_ticker_skips_multisymbol_and_uppercases():
    idx = {"signals": [{"ticker": "sofi"}, {"ticker": "A/B"}, {"ticker": "SOFI"}, {"ticker": ""}]}
    g = et.group_by_ticker(idx)
    assert set(g) == {"SOFI"}
    assert len(g["SOFI"]) == 2


def test_earliest_claim_date():
    assert (
        et.earliest_claim_date([{"claim_date": "2026-06-22"}, {"claim_date": "2026-06-19"}])
        == "2026-06-19"
    )
    assert et.earliest_claim_date([{}]) is None


def test_resolve_source_reads_channel(tmp_path):
    note_dir = tmp_path / "sources" / "youtube"
    note_dir.mkdir(parents=True)
    (note_dir / "2026-06-22_VID.md").write_text(
        "---\ntitle: x\ntype: source\nchannel: Stocks with Josh\nvideo_id: VID\n---\nbody"
    )
    sigs = [{"sources": ["sources/youtube/2026-06-22_VID"]}]
    assert et.resolve_source(sigs, tmp_path) == "YouTube — Stocks with Josh"


def test_resolve_source_fallback_when_no_channel(tmp_path):
    assert et.resolve_source([{"sources": []}], tmp_path) == "YouTube"


def test_build_records_full_row(tmp_path, monkeypatch):
    note_dir = tmp_path / "sources" / "youtube"
    note_dir.mkdir(parents=True)
    (note_dir / "2026-06-22_VID.md").write_text("---\nchannel: Stocks with Josh\n---\n")
    idx = {
        "signals": [
            {
                "ticker": "SOFI",
                "direction": "long",
                "claim_date": "2026-06-22",
                "sources": ["sources/youtube/2026-06-22_VID"],
            }
        ]
    }
    monkeypatch.setattr(
        et,
        "fetch_profile",
        lambda t: {
            "company_name": "SoFi Technologies",
            "sector": "Financial Services",
            "industry": "Credit Services",
        },
    )
    monkeypatch.setattr(et, "fetch_price_on", lambda t, d: 16.0)
    monkeypatch.setattr(et, "fetch_current_price", lambda t: 18.0)

    recs = et.build_records(idx, tmp_path, NOW)
    assert len(recs) == 1
    r = recs[0]
    assert r["ticker"] == "SOFI"
    assert r["company_name"] == "SoFi Technologies"
    assert r["sector"] == "Financial Services"
    assert r["industry"] == "Credit Services"
    assert r["date_recommended"] == "2026-06-22"
    assert r["price_at_recommendation"] == 16.0
    assert r["current_price"] == 18.0
    assert "gain_loss_pct" not in r  # derived → computed by the UI
    assert "days_held" not in r
    assert r["recommendation_source"] == "YouTube — Stocks with Josh"
    assert r["source_skill"] == "social-signal-ingestor"
    assert r["direction"] == "long"
    assert r["status"] == "active"
