"""Tests for manage_exit.py — the dynamic exit engine FSM.

Covers the four contract behaviours the skill exists for:
  * ratchet monotonicity   (the profit floor only moves up)
  * noise-band suppression  (sub-tier give-back inside ATR is held, not exited)
  * breakeven lock          (a peak past the trigger locks >= entry+fees)
  * stale-flat timeout       (a drifter is cut after N bars)

plus the Phase-1 hard stop (spot + ATR-scaled min), reconciliation, replay
determinism, and the CLI. All tests read committed JSON fixtures and never hit
the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from manage_exit import (
    ExitPolicy,
    RetraceTier,
    active_retrace,
    compute_stop,
    derive_action,
    load_bars_fixture,
    main,
    normalize_bars,
    reconcile,
    replay,
    run,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _bars(name: str, ticker: str) -> list[dict]:
    return normalize_bars(load_bars_fixture(str(FIXTURES / name), ticker))


# ─── Ratchet monotonicity ────────────────────────────────────────────────────


class TestRatchetMonotonicity:
    def test_floor_trace_is_monotonic_non_decreasing(self):
        bars = _bars("ratchet_rise_fall.json", "AAPL")
        state = replay(100.0, bars, ExitPolicy())
        trace = state["floor_trace"]
        assert trace == sorted(trace), f"floor dropped somewhere: {trace}"

    def test_floor_holds_through_pullback(self):
        """Peak 130 sets floor 115; later dips to 122 must not lower it."""
        bars = _bars("ratchet_rise_fall.json", "AAPL")
        state = replay(100.0, bars, ExitPolicy())
        # peak 130, tier at 20% -> retrace 0.50 -> floor = 100 + 30*0.5 = 115
        assert state["peak_px"] == 130.0
        assert state["floor_px"] == pytest.approx(115.0)
        assert state["status"] == "holding"  # no dip breaches 115

    def test_holding_ratchet_yields_raise_stop_action(self):
        bars = _bars("ratchet_rise_fall.json", "AAPL")
        state = replay(100.0, bars, ExitPolicy())
        action = derive_action(state)
        assert action["action"] == "raise_stop"
        assert action["recommended_stop"] == pytest.approx(115.0)


# ─── Noise-band suppression ──────────────────────────────────────────────────


class TestNoiseBand:
    def test_pullback_inside_band_holds(self):
        bars = _bars("noise_band_pullback.json", "NVDA")
        policy = ExitPolicy(noise_band_enabled=True, noise_band_atr_mult=1.0)
        state = replay(100.0, bars, policy, atr=3.0)
        assert state["status"] == "holding"
        assert state["consecutive_breaches"] == 0

    def test_same_pullback_without_band_exits(self):
        bars = _bars("noise_band_pullback.json", "NVDA")
        policy = ExitPolicy(noise_band_enabled=False)
        state = replay(100.0, bars, policy, atr=3.0)
        assert state["status"] == "exited"
        assert "floor_breach" in state["exit_reason"]

    def test_band_does_not_suppress_beyond_first_tier(self):
        """A pull-back that is inside the ATR band but from a peak ABOVE the
        first tier is a real give-back and must still be eligible to exit."""
        # Peak 108 (+8% > 5% first tier); give-back to 105 (3% pull-back == band).
        bars = normalize_bars(
            [
                {"date": "2026-01-02", "close": 100.0},
                {"date": "2026-01-05", "close": 108.0},
                {"date": "2026-01-06", "close": 105.0},
            ]
        )
        policy = ExitPolicy(noise_band_enabled=True, noise_band_atr_mult=1.0)
        state = replay(100.0, bars, policy, atr=3.0)
        # floor at peak 108 = 100 + 8*(1-0.30)=105.6; mark 105 < 105.6 -> exits,
        # NOT suppressed because peak_profit 8% >= first tier 5%.
        assert state["status"] == "exited"


# ─── Breakeven lock ──────────────────────────────────────────────────────────


class TestBreakevenLock:
    def test_lock_arms_and_exits_at_locked_gain(self):
        bars = _bars("breakeven_lock.json", "MSFT")
        policy = ExitPolicy(
            protect_pct=5.0,
            breakeven_trigger_pct=2.0,
            breakeven_lock_pct=0.5,
        )
        state = replay(100.0, bars, policy)
        assert state["status"] == "exited"
        assert state["floor_px"] == pytest.approx(100.5)
        assert "floor_breach" in state["exit_reason"]

    def test_without_breakeven_no_lock_just_holds(self):
        bars = _bars("breakeven_lock.json", "MSFT")
        policy = ExitPolicy(protect_pct=5.0, breakeven_trigger_pct=0.0)
        state = replay(100.0, bars, policy)
        # Floor stays at the 92 hard stop; the round-trip to 100 never breaches.
        assert state["status"] == "holding"
        assert state["floor_px"] == pytest.approx(92.0)

    def test_lock_never_below_entry_plus_fees(self):
        bars = _bars("breakeven_lock.json", "MSFT")
        policy = ExitPolicy(protect_pct=5.0, breakeven_trigger_pct=2.0, breakeven_lock_pct=0.5)
        state = replay(100.0, bars, policy)
        assert state["floor_px"] >= 100.0 * (1 + 0.5 / 100)


# ─── Stale-flat timeout ──────────────────────────────────────────────────────


class TestStaleFlat:
    def test_drifter_cut_after_n_bars(self):
        bars = _bars("stale_flat.json", "F")
        policy = ExitPolicy(stale_flat_bars=5, protect_pct=3.0)
        state = replay(100.0, bars, policy)
        assert state["status"] == "exited"
        assert state["phase"] == "timeout"
        assert "stale_flat" in state["exit_reason"]
        assert state["exit_bar_index"] == 4  # 5th bar (bars_elapsed == 5)

    def test_disabled_stale_flat_holds(self):
        bars = _bars("stale_flat.json", "F")
        policy = ExitPolicy(stale_flat_bars=0, protect_pct=3.0)
        state = replay(100.0, bars, policy)
        assert state["status"] == "holding"

    def test_position_that_armed_protect_is_exempt(self):
        """A position that reached protect is never stale-flat cut."""
        bars = normalize_bars(
            [
                {"date": "2026-01-02", "close": 100.0},
                {"date": "2026-01-05", "close": 104.0},  # +4% >= protect 3%
                {"date": "2026-01-06", "close": 103.5},
                {"date": "2026-01-07", "close": 103.6},
                {"date": "2026-01-08", "close": 103.7},
            ]
        )
        policy = ExitPolicy(stale_flat_bars=3, protect_pct=3.0)
        state = replay(100.0, bars, policy)
        assert "stale_flat" not in (state["exit_reason"] or "")


# ─── Phase-1 hard stop ───────────────────────────────────────────────────────


class TestHardStop:
    def test_spot_stop_fires_immediately(self):
        bars = _bars("hard_stop_hit.json", "GE")
        state = replay(100.0, bars, ExitPolicy(spot_stop_pct=8.0))
        assert state["status"] == "exited"
        assert "max_loss" in state["exit_reason"]
        assert state["exit_bar_index"] == 2

    def test_atr_stop_min_takes_tighter_floor(self):
        """min(spot_stop_pct, clamped ATR%) -- ATR path is tighter here."""
        # entry 100, atr 2 -> atr% 2, mult 2 -> 4% clamped to [3,15] -> 4% < 8%.
        pct, source = compute_stop(
            ExitPolicy(spot_stop_pct=8.0, atr_stop_enabled=True, atr_stop_mult=2.0),
            entry=100.0,
            atr=2.0,
        )
        assert pct == pytest.approx(4.0)
        assert source == "atr"

    def test_atr_stop_clamp_floor(self):
        """A tiny ATR is clamped up to the floor so the stop isn't noise-tight."""
        pct, source = compute_stop(
            ExitPolicy(
                spot_stop_pct=8.0,
                atr_stop_enabled=True,
                atr_stop_mult=2.0,
                atr_stop_floor_pct=3.0,
            ),
            entry=100.0,
            atr=0.5,  # atr% 0.5 * 2 = 1% -> clamped up to 3%
        )
        assert pct == pytest.approx(3.0)
        assert source == "atr"

    def test_spot_wins_when_tighter_than_atr(self):
        pct, source = compute_stop(
            ExitPolicy(spot_stop_pct=2.0, atr_stop_enabled=True, atr_stop_mult=2.0),
            entry=100.0,
            atr=3.0,  # atr% 3 * 2 = 6% clamped, but spot 2% is tighter
        )
        assert pct == pytest.approx(2.0)
        assert source == "spot"

    def test_no_atr_falls_back_to_spot(self):
        pct, source = compute_stop(
            ExitPolicy(spot_stop_pct=8.0, atr_stop_enabled=True), entry=100.0, atr=None
        )
        assert pct == pytest.approx(8.0)
        assert source == "spot"


# ─── Consecutive-breach confirmation ─────────────────────────────────────────


class TestConsecutiveBreach:
    def test_two_consecutive_required_holds_on_single_breach(self):
        # Peak 110 -> floor 106; single dip to 105 (below floor) then back to 108.
        bars = normalize_bars(
            [
                {"date": "2026-01-02", "close": 100.0},
                {"date": "2026-01-05", "close": 110.0},
                {"date": "2026-01-06", "close": 105.0},  # 1st breach of 106 floor
                {"date": "2026-01-07", "close": 108.0},  # reclaim -> resets
            ]
        )
        policy = ExitPolicy(consecutive_breaches_required=2)
        state = replay(100.0, bars, policy)
        assert state["status"] == "holding"

    def test_two_consecutive_breaches_exit(self):
        bars = normalize_bars(
            [
                {"date": "2026-01-02", "close": 100.0},
                {"date": "2026-01-05", "close": 110.0},
                {"date": "2026-01-06", "close": 105.0},  # 1st breach
                {"date": "2026-01-07", "close": 104.0},  # 2nd consecutive breach
            ]
        )
        policy = ExitPolicy(consecutive_breaches_required=2)
        state = replay(100.0, bars, policy)
        assert state["status"] == "exited"
        assert "floor_breach" in state["exit_reason"]


# ─── active_retrace tiers ────────────────────────────────────────────────────


class TestRetraceTiers:
    def test_default_below_first_tier(self):
        assert active_retrace(ExitPolicy(), 4.0) == 0.30  # below 5% tier

    def test_picks_highest_active_tier(self):
        assert active_retrace(ExitPolicy(), 25.0) == 0.50  # 20% tier

    def test_custom_tiers(self):
        pol = ExitPolicy(phase2_tiers=[RetraceTier(8.0, 0.35), RetraceTier(15.0, 0.40)])
        assert active_retrace(pol, 20.0) == 0.40


# ─── Reconciliation ──────────────────────────────────────────────────────────


class TestReconcile:
    def test_fresh_when_no_prior(self):
        assert reconcile(None, 100.0, 100)["decision"] == "fresh"

    def test_match_same_entry(self):
        prior = {"entry_price": 100.0}
        assert reconcile(prior, 100.0, 100)["decision"] == "match"

    def test_reset_on_changed_entry(self):
        prior = {"entry_price": 100.0}
        assert reconcile(prior, 142.0, 50)["decision"] == "reset"

    def test_no_position_when_flat(self):
        prior = {"entry_price": 100.0}
        assert reconcile(prior, 100.0, 0)["decision"] == "no_position"


# ─── Replay determinism ──────────────────────────────────────────────────────


class TestReplayDeterminism:
    def test_two_runs_identical_state(self):
        bars = _bars("ratchet_rise_fall.json", "AAPL")
        s1 = replay(100.0, bars, ExitPolicy(), atr=3.0)
        s2 = replay(100.0, bars, ExitPolicy(), atr=3.0)
        assert s1 == s2

    def test_prior_snapshot_does_not_change_state(self, tmp_path):
        """run() folds the FSM purely; a stale prior snapshot on disk must not
        change the computed FSM state between two runs."""
        raw = load_bars_fixture(str(FIXTURES / "ratchet_rise_fall.json"), "AAPL")
        state_dir = str(tmp_path / "state")
        args = dict(
            ticker="AAPL",
            side="long",
            entry=100.0,
            qty=100,
            atr=3.0,
            entry_date=None,
            raw_bars=raw,
            policy=ExitPolicy(),
            state_dir=state_dir,
            as_of="2026-01-14",
        )
        out1 = run(**args)
        # Persist a snapshot so run 2 has a prior on disk.
        from manage_exit import save_snapshot, snapshot_path

        save_snapshot(snapshot_path(state_dir, "AAPL", "long"), out1)
        out2 = run(**args)
        assert out1["state"] == out2["state"]
        assert out1["action"] == out2["action"]


# ─── Input validation ────────────────────────────────────────────────────────


class TestValidation:
    def test_bad_entry_raises(self):
        with pytest.raises(ValueError, match="entry must be positive"):
            replay(0.0, [{"ts": None, "close": 1.0}], ExitPolicy())

    def test_bad_retrace_raises(self):
        with pytest.raises(ValueError, match="retrace_threshold"):
            ExitPolicy(retrace_threshold=1.5).validate()

    def test_short_side_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="long positions only"):
            run(
                ticker="AAPL",
                side="short",
                entry=100.0,
                qty=100,
                atr=None,
                entry_date=None,
                raw_bars=[{"date": "2026-01-02", "close": 100.0}],
                policy=ExitPolicy(),
                state_dir=str(tmp_path),
            )

    def test_policy_roundtrip(self):
        pol = ExitPolicy(atr_stop_enabled=True, breakeven_trigger_pct=3.0)
        restored = ExitPolicy.from_dict(pol.to_dict())
        assert restored.atr_stop_enabled is True
        assert restored.breakeven_trigger_pct == 3.0
        assert len(restored.phase2_tiers) == len(pol.phase2_tiers)


# ─── No-position path ────────────────────────────────────────────────────────


class TestNoPosition:
    def test_flat_position_returns_no_position(self, tmp_path):
        out = run(
            ticker="AAPL",
            side="long",
            entry=100.0,
            qty=0,
            atr=None,
            entry_date=None,
            raw_bars=[],
            policy=ExitPolicy(),
            state_dir=str(tmp_path),
        )
        assert out["state"]["status"] == "no_position"
        assert out["action"]["action"] == "no_position"


# ─── CLI ─────────────────────────────────────────────────────────────────────


class TestCLI:
    def test_cli_writes_reports_and_snapshot(self, tmp_path):
        rc = main(
            [
                "--ticker",
                "AAPL",
                "--entry",
                "100",
                "--qty",
                "100",
                "--atr",
                "3.0",
                "--bars-source",
                "fixture",
                "--bars-fixture",
                str(FIXTURES / "ratchet_rise_fall.json"),
                "--state-dir",
                str(tmp_path / "state"),
                "--output-dir",
                str(tmp_path / "reports"),
                "--as-of",
                "2026-01-14",
            ]
        )
        assert rc == 0
        report = tmp_path / "reports" / "dynamic_exit_AAPL_2026-01-14.json"
        assert report.exists()
        data = json.loads(report.read_text())
        assert data["action"]["action"] == "raise_stop"
        snap = tmp_path / "state" / "dynamic_exit_AAPL_long.json"
        assert snap.exists()

    def test_cli_missing_entry_errors(self, tmp_path):
        rc = main(
            [
                "--ticker",
                "AAPL",
                "--bars-source",
                "fixture",
                "--bars-fixture",
                str(FIXTURES / "ratchet_rise_fall.json"),
                "--state-dir",
                str(tmp_path / "state"),
                "--output-dir",
                str(tmp_path / "reports"),
            ]
        )
        assert rc == 1

    def test_cli_fmp_missing_key_errors(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FMP_API_KEY", raising=False)
        rc = main(
            [
                "--ticker",
                "AAPL",
                "--entry",
                "100",
                "--qty",
                "100",
                "--bars-source",
                "fmp",
                "--state-dir",
                str(tmp_path / "state"),
                "--output-dir",
                str(tmp_path / "reports"),
            ]
        )
        assert rc == 1
