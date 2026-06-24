"""Tests for ingest_youtube.py pure helpers (no network / no yt-dlp)."""

import datetime as dt

import ingest_youtube as iy


def test_slugify():
    assert iy.slugify("Stocks with Josh") == "stocks-with-josh"
    assert iy.slugify("@Ross/Givens") == "ross-givens"
    assert iy.slugify("   ") == "channel"


def test_metadata_timestamp_prefers_epoch():
    assert iy.metadata_timestamp({"timestamp": 1700000000}) == 1700000000


def test_metadata_timestamp_falls_back_to_upload_date():
    ts = iy.metadata_timestamp({"timestamp": 0, "upload_date": "20260101"})
    expected = int(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp())
    assert ts == expected


def test_metadata_timestamp_missing_returns_none():
    assert iy.metadata_timestamp({}) is None


def test_resolve_paths_layout():
    p = iy.resolve_paths("myagent", "/tmp/somewhere")
    assert p["raw"].as_posix().endswith("/tmp/somewhere/myagent/raw")
    assert p["vault_current"].as_posix().endswith("myagent/vault/current")
    assert p["state"].as_posix().endswith("myagent/state")


def test_write_source_stub_content_and_idempotent(tmp_path):
    meta = {
        "upload_date": "20260622",
        "title": "My Video",
        "channel": "Chan",
        "webpage_url": "http://x",
    }
    stub = iy.write_source_stub(tmp_path, "chan", "VID123", meta, {"metadata": "m.json"})

    assert stub.name == "2026-06-22_VID123.md"
    text = stub.read_text()
    assert "type: source" in text
    assert "video_id: VID123" in text
    assert iy.ENRICHMENT_PENDING_MARKER in text  # extraction still pending

    # Idempotent: a second call must NOT overwrite an existing stub.
    stub.write_text("ENRICHED — do not clobber")
    iy.write_source_stub(tmp_path, "chan", "VID123", meta, {"metadata": "m.json"})
    assert stub.read_text() == "ENRICHED — do not clobber"


def test_collect_pending_retries_detects_unfilled_stub(tmp_path):
    meta = {"upload_date": "20260622", "title": "V", "channel": "Chan"}
    stub = iy.write_source_stub(tmp_path, "chan", "VID123", meta, {"metadata": "m.json"})
    state = {"seen_video_ids": {"chan": {"VID123": {"source_stub": str(stub)}}}}
    channels = [{"name": "Chan", "url": "http://x"}]

    retries = iy.collect_pending_retries(state, channels)
    assert len(retries) == 1
    assert retries[0]["video_id"] == "VID123"
    assert retries[0]["retry"] is True

    # Once enriched (marker gone), it is no longer re-offered.
    stub.write_text("fully enriched, no marker here")
    assert iy.collect_pending_retries(state, channels) == []
