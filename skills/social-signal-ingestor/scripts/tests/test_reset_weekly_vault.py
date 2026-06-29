"""Tests for reset_weekly_vault.py (no network)."""

import datetime as dt
import json
import os

import reset_weekly_vault as rv

NOW = dt.datetime(2026, 6, 29, 12, 0, tzinfo=dt.timezone.utc)  # ISO 2026-W27
WEEK = rv.week_id(NOW)


def _build_vault(tmp_path):
    """A populated data/social tree: signals + sources + raw videos + state."""
    base = tmp_path / "social"
    cur = base / "vault" / "current"
    (cur / "signals").mkdir(parents=True)
    (cur / "sources" / "youtube").mkdir(parents=True)
    (cur / "signals" / "2026-06-22_AUPH.md").write_text("---\nticker: AUPH\n---\n")
    (cur / "sources" / "youtube" / "2026-06-22_VID.md").write_text("---\nchannel: X\n---\n")
    state = base / "state"
    state.mkdir(parents=True)
    (state / "youtube_state.json").write_text('{"seen_video_ids": {"x": {"VID": 1}}}')
    raw = base / "raw" / "youtube" / "chan"
    for vid in ("old", "new"):
        (raw / vid).mkdir(parents=True)
        (raw / vid / "metadata.json").write_text("{}")
    return base


def _paths(base):
    return rv.resolve_paths("social", str(base.parent))


def test_first_reset_archives_reinits_and_preserves_state(tmp_path):
    base = _build_vault(tmp_path)
    paths = _paths(base)
    state_before = (base / "state" / "youtube_state.json").read_bytes()

    rep = rv.run_reset(paths, NOW, keep_archives=8, raw_days=0, force=False, dry_run=False)

    # old week archived with its signals
    assert (paths["archive_weeks"] / WEEK / "signals" / "2026-06-22_AUPH.md").exists()
    # fresh empty current + marker
    assert list((paths["current"] / "signals").iterdir()) == []
    assert (paths["current"] / "sources" / "youtube").is_dir()
    assert json.loads((paths["current"] / rv.MARKER).read_text())["week"] == WEEK
    # state untouched
    assert (base / "state" / "youtube_state.json").read_bytes() == state_before
    assert rep["archived_to"].endswith(WEEK)


def test_idempotent_same_week_is_noop(tmp_path):
    base = _build_vault(tmp_path)
    paths = _paths(base)
    rv.run_reset(paths, NOW, keep_archives=8, raw_days=0, force=False, dry_run=False)
    rep2 = rv.run_reset(paths, NOW, keep_archives=8, raw_days=0, force=False, dry_run=False)
    assert "no-op" in rep2["message"]
    assert rep2["archived_to"] is None
    # only one archive dir for this week
    assert [p.name for p in paths["archive_weeks"].iterdir()] == [WEEK]


def test_force_rearchives_same_week(tmp_path):
    base = _build_vault(tmp_path)
    paths = _paths(base)
    rv.run_reset(paths, NOW, keep_archives=8, raw_days=0, force=False, dry_run=False)
    rep = rv.run_reset(paths, NOW, keep_archives=8, raw_days=0, force=True, dry_run=False)
    assert rep["archived_to"] is not None
    assert len(list(paths["archive_weeks"].iterdir())) == 2  # WEEK + WEEK-<ts>


def test_prune_raw_by_mtime(tmp_path):
    base = _build_vault(tmp_path)
    paths = _paths(base)
    old_dir = paths["raw_youtube"] / "chan" / "old"
    old_ts = NOW.timestamp() - 100 * 86400
    os.utime(old_dir, (old_ts, old_ts))

    rep = rv.run_reset(paths, NOW, keep_archives=8, raw_days=60, force=False, dry_run=False)

    assert not old_dir.exists()
    assert (paths["raw_youtube"] / "chan" / "new").exists()
    assert any(p.endswith("old") for p in rep["pruned_raw_dirs"])


def test_prune_archives_by_count(tmp_path):
    base = _build_vault(tmp_path)
    paths = _paths(base)
    for wk in ("2026-W10", "2026-W11", "2026-W12"):
        (paths["archive_weeks"] / wk).mkdir(parents=True)

    rep = rv.run_reset(paths, NOW, keep_archives=2, raw_days=0, force=False, dry_run=False)

    remaining = sorted(p.name for p in paths["archive_weeks"].iterdir())
    assert len(remaining) == 2  # newest 2 of {W10,W11,W12,WEEK(this run)}
    assert WEEK in remaining
    assert "2026-W10" not in remaining
    assert any("2026-W10" in p for p in rep["pruned_archives"])


def test_dry_run_changes_nothing(tmp_path):
    base = _build_vault(tmp_path)
    paths = _paths(base)
    rep = rv.run_reset(paths, NOW, keep_archives=8, raw_days=1, force=False, dry_run=True)

    # current still holds the original signal; nothing archived; raw intact
    assert (paths["current"] / "signals" / "2026-06-22_AUPH.md").exists()
    assert not paths["archive_weeks"].exists() or list(paths["archive_weeks"].iterdir()) == []
    assert (paths["raw_youtube"] / "chan" / "old").exists()
    assert rep["dry_run"] is True
