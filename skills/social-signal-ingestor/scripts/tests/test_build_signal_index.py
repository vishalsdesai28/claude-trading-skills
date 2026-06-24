"""Tests for build_signal_index.py — frontmatter → machine index."""

import datetime as dt

from build_signal_index import build_index

NOW = dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc)

SIGNAL_NVDA = """---
title: NVDA AI memory breakout
type: signal
status: watching
ticker: NVDA
direction: long
time_horizon: weekly
claim_date: 2026-06-21
sources:
  - [[sources/youtube/2026-06-21_abc]]
  - [[sources/youtube/2026-06-22_def]]
---

# body
"""

SIGNAL_MULTI = """---
title: basket idea
type: signal
ticker: STRL/POWL
direction: long
---
"""

SOURCE_NOTE = """---
title: some video
type: source
source_type: youtube
---
"""


def _write(d, name, text):
    p = d / name
    p.write_text(text)
    return p


def test_index_collects_signals_and_skips_source_notes(tmp_path):
    sig_dir = tmp_path / "signals"
    sig_dir.mkdir()
    _write(sig_dir, "2026-06-21_NVDA_ai.md", SIGNAL_NVDA)
    _write(sig_dir, "2026-06-22_BASKET.md", SIGNAL_MULTI)
    _write(sig_dir, "2026-06-22_video.md", SOURCE_NOTE)  # type: source → excluded

    index = build_index(sig_dir, now=NOW)

    assert index["signal_count"] == 2
    nvda = next(s for s in index["signals"] if s["ticker"] == "NVDA")
    assert nvda["direction"] == "long"
    assert "confidence" not in nvda  # confidence dropped from the schema
    assert nvda["sources"] == ["sources/youtube/2026-06-21_abc", "sources/youtube/2026-06-22_def"]
    assert index["week"] == "2026-W26"  # 2026-06-22 is a Monday → ISO week 26


def test_multi_symbol_ticker_is_flagged(tmp_path):
    sig_dir = tmp_path / "signals"
    sig_dir.mkdir()
    _write(sig_dir, "2026-06-22_BASKET.md", SIGNAL_MULTI)

    index = build_index(sig_dir, now=NOW)

    assert len(index["ticker_warnings"]) == 1
    assert "STRL/POWL" in index["ticker_warnings"][0]["warning"]
