"""Tests for write_supabase.py (no network — requests monkeypatched via sys.modules)."""

import json
import sys
import types

import write_supabase as ws


def test_parse_dotenv():
    text = '# comment\nSUPABASE_URL=https://x.supabase.co\nKEY="quoted-val"\n\nBAD LINE\n'
    parsed = ws._parse_dotenv(text)
    assert parsed["SUPABASE_URL"] == "https://x.supabase.co"
    assert parsed["KEY"] == "quoted-val"
    assert "BAD LINE" not in parsed


def test_load_records_array(tmp_path):
    f = tmp_path / "a.json"
    f.write_text(json.dumps([{"ticker": "AAA"}, {"ticker": "BBB"}]))
    assert [r["ticker"] for r in ws.load_records(str(f))] == ["AAA", "BBB"]


def test_load_records_object_and_glob(tmp_path):
    (tmp_path / "r1.json").write_text(json.dumps({"records": [{"ticker": "AAA"}]}))
    (tmp_path / "r2.json").write_text(json.dumps({"records": [{"ticker": "CCC"}]}))
    rows = ws.load_records(str(tmp_path / "r*.json"))
    assert sorted(r["ticker"] for r in rows) == ["AAA", "CCC"]


def test_load_records_no_match_errors(tmp_path):
    try:
        ws.load_records(str(tmp_path / "nope_*.json"))
        raise AssertionError("expected SystemExit")
    except SystemExit:
        pass


class _FakeResp:
    status_code = 200

    def raise_for_status(self):
        pass


def _install_fake_requests(monkeypatch):
    calls = {}
    fake = types.ModuleType("requests")
    fake.RequestException = Exception

    def post(endpoint, headers=None, data=None, timeout=None):
        calls["endpoint"] = endpoint
        calls["headers"] = headers
        calls["data"] = data
        return _FakeResp()

    fake.post = post
    monkeypatch.setitem(sys.modules, "requests", fake)
    return calls


def test_upsert_sets_conflict_and_merge(monkeypatch):
    calls = _install_fake_requests(monkeypatch)
    n = ws.supabase_write(
        "https://x.supabase.co/",
        "k",
        "recommendations",
        [{"ticker": "AAA"}],
        "ticker,recommendation_source,date_recommended",
        "upsert",
    )
    assert n == 1
    assert calls["endpoint"].endswith(
        "/rest/v1/recommendations?on_conflict=ticker,recommendation_source,date_recommended"
    )
    assert "merge-duplicates" in calls["headers"]["Prefer"]


def test_insert_omits_conflict(monkeypatch):
    calls = _install_fake_requests(monkeypatch)
    ws.supabase_write(
        "https://x.supabase.co", "k", "recommendations", [{"ticker": "AAA"}], None, "insert"
    )
    assert calls["endpoint"].endswith("/rest/v1/recommendations")
    assert "merge-duplicates" not in calls["headers"]["Prefer"]


def test_dedup_by_conflict_keeps_last():
    rows = [
        {"ticker": "MU", "src": "a", "current_price": 1},
        {"ticker": "NVDA", "src": "a", "current_price": 2},
        {"ticker": "MU", "src": "a", "current_price": 9},  # newer dup → wins
    ]
    out = ws.dedup_by_conflict(rows, "ticker,src")
    assert len(out) == 2
    mu = next(r for r in out if r["ticker"] == "MU")
    assert mu["current_price"] == 9


def test_dedup_noop_without_conflict():
    rows = [{"ticker": "MU"}, {"ticker": "MU"}]
    assert ws.dedup_by_conflict(rows, None) == rows


def test_pick_key_order_and_alt_names():
    assert (
        ws._pick_key({"SUPABASE_SERVICE_KEY": "a", "SUPABASE_SECRETS_KEY": "b"}) == "a"
    )  # preference order
    assert ws._pick_key({"SUPABASE_SECRETS_KEY": "b"}) == "b"  # newer Supabase naming
    assert ws._pick_key({}) is None
