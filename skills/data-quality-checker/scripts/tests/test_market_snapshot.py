"""Tests for the verified market snapshot builder.

Fully offline: indicator helpers are exercised on tiny hand-verifiable inputs
and the snapshot builder/renderer run against a committed OHLCV fixture.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest
from market_snapshot import (
    SNAPSHOT_INDICATORS,
    _atr,
    _bollinger,
    _ema,
    _macd,
    _rsi,
    _sma,
    build_snapshot,
    compute_indicators,
    render_snapshot_text,
)

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..")
FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load_fixture_rows() -> list[dict]:
    with open(os.path.join(FIXTURES_DIR, "ohlcv_TEST.json"), encoding="utf-8") as f:
        return json.load(f)["rows"]


# ──────────────────────────────────────────────
# Pure indicator helpers
# ──────────────────────────────────────────────


class TestIndicatorHelpers:
    def test_sma_basic(self):
        assert _sma([10, 20, 30, 40, 50], 5) == 30

    def test_sma_too_few(self):
        assert _sma([1, 2], 5) is None

    def test_ema_constant_series(self):
        """EMA of a constant series equals the constant."""
        assert _ema([5.0] * 20, 10) == pytest.approx(5.0)

    def test_ema_adjust_false_seed(self):
        """adjust=False EMA is seeded at the first value."""
        # alpha = 2/3 for n=2: y1 = 2/3*2 + 1/3*1 = 1.6667
        assert _ema([1.0, 2.0], 2) == pytest.approx(1.0 + (2.0 / 3.0))

    def test_rsi_all_gains_is_100(self):
        rising = [float(i) for i in range(1, 30)]
        assert _rsi(rising, 14) == pytest.approx(100.0)

    def test_rsi_all_losses_is_zero(self):
        falling = [float(i) for i in range(30, 1, -1)]
        assert _rsi(falling, 14) == pytest.approx(0.0)

    def test_rsi_too_few(self):
        assert _rsi([1.0, 2.0, 3.0], 14) is None

    def test_bollinger_constant_series(self):
        mid, ub, lb = _bollinger([100.0] * 20, 20, 2.0)
        assert mid == pytest.approx(100.0)
        assert ub == pytest.approx(100.0)
        assert lb == pytest.approx(100.0)

    def test_bollinger_too_few(self):
        assert _bollinger([1.0] * 5, 20) == (None, None, None)

    def test_macd_constant_series_is_zero(self):
        macd, sig, hist = _macd([50.0] * 40)
        assert macd == pytest.approx(0.0)
        assert sig == pytest.approx(0.0)
        assert hist == pytest.approx(0.0)

    def test_macd_too_few(self):
        assert _macd([1.0] * 10) == (None, None, None)

    def test_atr_constant_range(self):
        """Constant close with high/low +/-1 gives a true range (and ATR) of 2."""
        highs = [101.0] * 20
        lows = [99.0] * 20
        closes = [100.0] * 20
        assert _atr(highs, lows, closes, 14) == pytest.approx(2.0)

    def test_atr_too_few(self):
        assert _atr([1.0] * 5, [1.0] * 5, [1.0] * 5, 14) is None


class TestComputeIndicators:
    def test_returns_fixed_indicator_set(self):
        rows = _load_fixture_rows()
        ind = compute_indicators(rows)
        assert set(ind.keys()) == set(SNAPSHOT_INDICATORS)
        assert len(SNAPSHOT_INDICATORS) == 11

    def test_200_sma_na_with_60_rows(self):
        rows = _load_fixture_rows()
        ind = compute_indicators(rows)
        # Only 60 rows -> 200 SMA cannot be computed.
        assert ind["close_200_sma"] is None
        # But 50 SMA can.
        assert ind["close_50_sma"] is not None


# ──────────────────────────────────────────────
# Snapshot builder + renderer
# ──────────────────────────────────────────────


class TestBuildSnapshot:
    def test_structure_and_latest_row(self):
        rows = _load_fixture_rows()
        snap = build_snapshot("test", "2026-01-14", rows)
        assert snap["symbol"] == "TEST"
        assert snap["analysis_date"] == "2026-01-14"
        assert snap["latest_row"]["date"] == "2026-01-14"
        assert snap["latest_row"]["close"] == 128.51
        assert snap["recent_high"] >= snap["recent_low"]
        assert "single source of truth" in snap["guardrail"]

    def test_lookahead_cutoff_reapplied(self):
        """A date before the last fixture row excludes look-ahead rows."""
        rows = _load_fixture_rows()
        snap = build_snapshot("TEST", "2025-11-03", rows)
        assert snap["latest_row"]["date"] <= "2025-11-03"
        for rc in snap["recent_closes"]:
            assert rc["date"] <= "2025-11-03"

    def test_lookback_window_capped_at_30(self):
        rows = _load_fixture_rows()
        snap = build_snapshot("TEST", "2026-01-14", rows, look_back_days=999)
        assert len(snap["recent_closes"]) <= 30

    def test_no_rows_raises(self):
        rows = _load_fixture_rows()
        with pytest.raises(ValueError):
            build_snapshot("TEST", "2000-01-01", rows)

    def test_unsorted_input_is_handled(self):
        rows = list(reversed(_load_fixture_rows()))
        snap = build_snapshot("TEST", "2026-01-14", rows)
        assert snap["latest_row"]["date"] == "2026-01-14"


class TestRenderSnapshotText:
    def test_fixed_shape_and_guardrail(self):
        rows = _load_fixture_rows()
        snap = build_snapshot("TEST", "2026-01-14", rows)
        text = render_snapshot_text(snap)
        assert "Verified market data snapshot for TEST" in text
        assert "Latest verified OHLCV row" in text
        assert "Verified technical indicators" in text
        assert "Recent verified closes" in text
        assert "single source of truth" in text
        # Every fixed indicator name appears as a table row.
        for name in SNAPSHOT_INDICATORS:
            assert f"| {name} |" in text
        # N/A rendered for the unavailable 200 SMA.
        assert "N/A" in text


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────


class TestCli:
    def test_invalid_date_exits_1(self):
        result = subprocess.run(
            [
                sys.executable,
                os.path.join(SCRIPTS_DIR, "market_snapshot.py"),
                "--ticker",
                "TEST",
                "--date",
                "not-a-date",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1

    def test_fmp_without_key_exits_1(self):
        """FMP source with no key present must fail cleanly (no network)."""
        env = dict(os.environ)
        env.pop("FMP_API_KEY", None)
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                [
                    sys.executable,
                    os.path.join(SCRIPTS_DIR, "market_snapshot.py"),
                    "--ticker",
                    "TEST",
                    "--date",
                    "2026-01-14",
                    "--source",
                    "fmp",
                    "--output-dir",
                    tmpdir,
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            assert result.returncode == 1
            assert "API key" in result.stderr
