"""End-to-end CLI check via offline fixture (no network)."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]


def test_cli_fixture_mode(tmp_path):
    bars = []
    price = 5000.0
    import datetime

    day = datetime.date(2026, 1, 5)
    for _ in range(80):
        while day.weekday() >= 5:
            day += datetime.timedelta(days=1)
        bars.append(
            {
                "date": day.isoformat(),
                "open": price - 1,
                "high": price + 8,
                "low": price - 8,
                "close": price,
            }
        )
        price += 5
        day += datetime.timedelta(days=1)
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"ES": {"bars": bars, "vix": 15.0}}))

    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "futures_signals.py"),
            "--symbols",
            "ES",
            "--fixture",
            str(fixture),
            "--output-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr

    json_files = list(tmp_path.glob("index_futures_signals_*.json"))
    md_files = list(tmp_path.glob("index_futures_signals_*.md"))
    assert json_files and md_files

    payload = json.loads(json_files[0].read_text())
    assert payload["skill"] == "index-futures-weekly-income"
    (market,) = payload["markets"]
    assert market["regime"]["trend"] == "uptrend"
    setups = {s["setup"]: s for s in market["signals"]}
    assert "weekly_breakout" in setups
    bo = setups["weekly_breakout"]
    assert {"position_sizer", "technical_analyst", "trader_memory_core"} <= set(bo["handoff"])


def test_nearest_friday_targets_30dte_not_third_friday():
    import datetime

    sys.path.insert(0, str(SCRIPTS))
    from futures_signals import nearest_friday

    # 2026-07-06 (Mon) + 30d = 2026-08-05 (Wed) -> nearest Friday is 2026-08-07,
    # NOT the August monthly expiry 2026-08-21
    assert nearest_friday(datetime.date(2026, 8, 5)) == datetime.date(2026, 8, 7)
    assert nearest_friday(datetime.date(2026, 8, 3)) == datetime.date(2026, 7, 31)
    assert nearest_friday(datetime.date(2026, 8, 7)) == datetime.date(2026, 8, 7)
