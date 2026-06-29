"""Tests for enrich_tickers.py pure logic (no network / no yfinance)."""

import datetime as dt

import enrich_tickers as et

NOW = dt.datetime(2026, 6, 27, tzinfo=dt.timezone.utc)


def test_group_by_ticker_channel_skips_multisymbol_and_uppercases(tmp_path):
    idx = {"signals": [{"ticker": "sofi"}, {"ticker": "A/B"}, {"ticker": "SOFI"}, {"ticker": ""}]}
    g = et.group_by_ticker_channel(idx, tmp_path)  # no sources → channel "YouTube"
    assert set(g) == {("SOFI", "YouTube")}
    assert len(g[("SOFI", "YouTube")]) == 2


def test_earliest_claim_date():
    assert (
        et.earliest_claim_date([{"claim_date": "2026-06-22"}, {"claim_date": "2026-06-19"}])
        == "2026-06-19"
    )
    assert et.earliest_claim_date([{}]) is None


def test_channels_for_signal_reads_channel(tmp_path):
    note_dir = tmp_path / "sources" / "youtube"
    note_dir.mkdir(parents=True)
    (note_dir / "2026-06-22_VID.md").write_text(
        "---\ntitle: x\ntype: source\nchannel: Stocks with Josh\nvideo_id: VID\n---\nbody"
    )
    sig = {"sources": ["sources/youtube/2026-06-22_VID"]}
    assert et.channels_for_signal(sig, tmp_path) == ["Stocks with Josh"]


def test_channels_for_signal_fallback_when_no_channel(tmp_path):
    assert et.channels_for_signal({"sources": []}, tmp_path) == ["YouTube"]


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
    assert r["price_at_recommendation"] == 16.0  # historical close used when available
    assert r["company_name"] == "SoFi Technologies"
    assert r["sector"] == "Financial Services"
    assert r["industry"] == "Credit Services"
    assert r["date_recommended"] == "2026-06-22"
    assert r["price_at_recommendation"] == 16.0
    assert r["current_price"] == 18.0
    assert "gain_loss_pct" not in r  # derived → computed by the UI
    assert "days_held" not in r
    assert r["recommendation_source"] == "Stocks with Josh"
    assert r["source_skill"] == "social-signal-ingestor"
    assert r["direction"] == "long"
    assert r["status"] == "active"


def test_build_records_splits_by_channel(tmp_path, monkeypatch):
    """One ticker cited by two channels → two rows, each with its own clean source."""
    note_dir = tmp_path / "sources" / "youtube"
    note_dir.mkdir(parents=True)
    (note_dir / "2026-06-22_A.md").write_text("---\nchannel: MarketBeat\n---\n")
    (note_dir / "2026-06-22_B.md").write_text("---\nchannel: Stocks with Josh\n---\n")
    idx = {
        "signals": [
            {
                "ticker": "MU",
                "direction": "long",
                "claim_date": "2026-06-22",
                "sources": ["sources/youtube/2026-06-22_A"],
            },
            {
                "ticker": "MU",
                "direction": "long",
                "claim_date": "2026-06-22",
                "sources": ["sources/youtube/2026-06-22_B"],
            },
        ]
    }
    monkeypatch.setattr(
        et, "fetch_profile", lambda t: {"company_name": "Micron", "sector": "x", "industry": "y"}
    )
    monkeypatch.setattr(et, "fetch_price_on", lambda t, d: 100.0)
    monkeypatch.setattr(et, "fetch_current_price", lambda t: 110.0)

    recs = et.build_records(idx, tmp_path, NOW)
    assert len(recs) == 2
    assert all(r["ticker"] == "MU" and r["date_recommended"] == "2026-06-22" for r in recs)
    assert {r["recommendation_source"] for r in recs} == {"MarketBeat", "Stocks with Josh"}


def test_build_records_falls_back_to_current_when_no_historical_close(tmp_path, monkeypatch):
    note_dir = tmp_path / "sources" / "youtube"
    note_dir.mkdir(parents=True)
    (note_dir / "2026-06-27_VID.md").write_text("---\nchannel: X\n---\n")
    idx = {
        "signals": [
            {
                "ticker": "F",
                "direction": "long",
                "claim_date": "2026-06-27",
                "sources": ["sources/youtube/2026-06-27_VID"],
            }
        ]
    }
    monkeypatch.setattr(
        et, "fetch_profile", lambda t: {"company_name": "Ford", "sector": "x", "industry": "y"}
    )
    monkeypatch.setattr(et, "fetch_price_on", lambda t, d: None)  # future/no-trade date → no close
    monkeypatch.setattr(et, "fetch_current_price", lambda t: 14.13)

    r = et.build_records(idx, tmp_path, NOW)[0]
    assert r["price_at_recommendation"] == 14.13  # fell back to current → never null
    assert r["current_price"] == 14.13
