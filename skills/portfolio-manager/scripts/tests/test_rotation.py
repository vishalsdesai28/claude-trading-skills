"""Tests for rotation.py — capital-rotation decision engine.

All tests run OFFLINE against in-memory data / committed fixtures. The CLI test
writes to a pytest tmp_path, never the repo reports/ directory.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from rotation import (
    RotationDecision,
    decide_rotation,
    generate_markdown_report,
    normalize_alpaca_positions,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# A book with two evictable non-winners (KO +2.5%, PG -3%) and one protected
# winner (AAPL +22%). PG is the weakest non-winner.
BOOK = [
    {"symbol": "KO", "roe_pct": 2.5, "age_days": 40},
    {"symbol": "PG", "roe_pct": -3.0, "age_days": 30},
    {"symbol": "AAPL", "roe_pct": 22.0, "age_days": 120},
]

BASE_THRESHOLDS = dict(
    min_candidate_composite=70.0,
    min_hold_days=5.0,
    protect_winner_roe_pct=15.0,
)


# ─── Capital-only guard ──────────────────────────────────────────────────────


class TestCapitalOnlyGuard:
    def test_capital_block_rotates(self):
        """Blocked purely by notional/concurrency -> rotation fires."""
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=82.0,
            blocked_reasons=["notional cap reached", "max positions reached"],
            open_positions=BOOK,
            **BASE_THRESHOLDS,
        )
        assert d.should_rotate is True
        assert d.action == "evict_and_enter"
        assert d.evict_symbol == "PG"  # weakest non-winner

    def test_risk_veto_blocks_rotation(self):
        """Any non-capital reason (a real risk veto) prevents rotation."""
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=95.0,
            blocked_reasons=["notional cap reached", "regime risk-off"],
            open_positions=BOOK,
            **BASE_THRESHOLDS,
        )
        assert d.should_rotate is False
        assert d.action == "hold"
        assert "non-capital" in d.reason

    def test_pure_risk_veto_blocks_rotation(self):
        """A lone risk veto (no capital marker) never rotates, even at max score."""
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=100.0,
            blocked_reasons=["failed liquidity floor"],
            open_positions=BOOK,
            **BASE_THRESHOLDS,
        )
        assert d.should_rotate is False
        assert "non-capital" in d.reason

    def test_no_block_no_rotation(self):
        """Candidate that was not blocked at all needs no rotation."""
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=90.0,
            blocked_reasons=[],
            open_positions=BOOK,
            **BASE_THRESHOLDS,
        )
        assert d.should_rotate is False
        assert "not blocked" in d.reason

    def test_case_insensitive_and_buying_power_marker(self):
        """Markers match case-insensitively; 'buying power' is a capital marker."""
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=82.0,
            blocked_reasons=["Insufficient BUYING POWER"],
            open_positions=BOOK,
            **BASE_THRESHOLDS,
        )
        assert d.should_rotate is True


# ─── Winner protection ───────────────────────────────────────────────────────


class TestWinnerProtection:
    def test_never_evicts_winner(self):
        """The only non-candidate holding is a winner -> no eviction."""
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=99.0,
            blocked_reasons=["max positions reached"],
            open_positions=[{"symbol": "AAPL", "roe_pct": 22.0, "age_days": 200}],
            **BASE_THRESHOLDS,
        )
        assert d.should_rotate is False
        assert "no eligible evictee" in d.reason

    def test_boundary_roe_is_protected(self):
        """ROE exactly at the protect threshold counts as a winner (>=)."""
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=99.0,
            blocked_reasons=["notional cap reached"],
            open_positions=[{"symbol": "MSFT", "roe_pct": 15.0, "age_days": 60}],
            **BASE_THRESHOLDS,
        )
        assert d.should_rotate is False

    def test_evicts_weakest_non_winner_not_winner(self):
        """With a winner and two laggards, evict the weakest laggard only."""
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=80.0,
            blocked_reasons=["notional cap reached"],
            open_positions=BOOK,
            **BASE_THRESHOLDS,
        )
        assert d.evict_symbol == "PG"
        assert d.evict_symbol != "AAPL"


# ─── Minimum hold ────────────────────────────────────────────────────────────


class TestMinHold:
    def test_too_young_not_evictable(self):
        """Weakest holding is younger than min hold -> skip to next eligible."""
        book = [
            {"symbol": "PG", "roe_pct": -10.0, "age_days": 2},  # weakest but young
            {"symbol": "KO", "roe_pct": 1.0, "age_days": 40},  # eligible
        ]
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=80.0,
            blocked_reasons=["notional cap reached"],
            open_positions=book,
            **BASE_THRESHOLDS,
        )
        assert d.should_rotate is True
        assert d.evict_symbol == "KO"  # PG protected by min-hold

    def test_all_too_young_no_rotation(self):
        """Every laggard is below min hold -> hold, no eviction."""
        book = [
            {"symbol": "PG", "roe_pct": -10.0, "age_days": 1},
            {"symbol": "KO", "roe_pct": 1.0, "age_days": 3},
        ]
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=80.0,
            blocked_reasons=["notional cap reached"],
            open_positions=book,
            **BASE_THRESHOLDS,
        )
        assert d.should_rotate is False
        assert "no eligible evictee" in d.reason

    def test_age_exactly_min_hold_is_eligible(self):
        """age_days == min_hold_days is eligible (>=)."""
        book = [{"symbol": "PG", "roe_pct": -5.0, "age_days": 5}]
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=80.0,
            blocked_reasons=["notional cap reached"],
            open_positions=book,
            **BASE_THRESHOLDS,
        )
        assert d.should_rotate is True
        assert d.evict_symbol == "PG"


# ─── Composite threshold ─────────────────────────────────────────────────────


class TestCompositeThreshold:
    def test_weak_candidate_no_rotation(self):
        """Candidate below the composite floor is not worth a round-trip."""
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=55.0,
            blocked_reasons=["notional cap reached"],
            open_positions=BOOK,
            **BASE_THRESHOLDS,
        )
        assert d.should_rotate is False
        assert "not worth a rotation" in d.reason


# ─── Determinism / edge cases ────────────────────────────────────────────────


class TestDeterminismAndEdges:
    def test_empty_book_no_rotation(self):
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=90.0,
            blocked_reasons=["notional cap reached"],
            open_positions=[],
            **BASE_THRESHOLDS,
        )
        assert d.should_rotate is False

    def test_does_not_evict_same_symbol(self):
        """A held position with the same ticker as the candidate is skipped."""
        book = [{"symbol": "NVDA", "roe_pct": -20.0, "age_days": 50}]
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=90.0,
            blocked_reasons=["notional cap reached"],
            open_positions=book,
            **BASE_THRESHOLDS,
        )
        assert d.should_rotate is False

    def test_tie_break_prefers_staler_position(self):
        """Equal ROE -> evict the older (staler) position deterministically."""
        book = [
            {"symbol": "PG", "roe_pct": -5.0, "age_days": 10},
            {"symbol": "KO", "roe_pct": -5.0, "age_days": 90},
        ]
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=90.0,
            blocked_reasons=["notional cap reached"],
            open_positions=book,
            **BASE_THRESHOLDS,
        )
        assert d.evict_symbol == "KO"

    def test_ticker_key_and_missing_fields_default_safely(self):
        """'ticker' alias works; missing roe/age default to 0 (age 0 < min hold)."""
        book = [{"ticker": "XYZ"}]  # no roe_pct, no age_days
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=90.0,
            blocked_reasons=["notional cap reached"],
            open_positions=book,
            **BASE_THRESHOLDS,
        )
        # age defaults to 0 < min_hold_days=5 -> not eligible
        assert d.should_rotate is False

    def test_empty_candidate_symbol_raises(self):
        with pytest.raises(ValueError, match="candidate_symbol"):
            decide_rotation(
                candidate_symbol="",
                candidate_composite=90.0,
                blocked_reasons=["notional cap reached"],
                open_positions=BOOK,
                **BASE_THRESHOLDS,
            )

    def test_negative_min_hold_raises(self):
        with pytest.raises(ValueError, match="min_hold_days"):
            decide_rotation(
                candidate_symbol="NVDA",
                candidate_composite=90.0,
                blocked_reasons=["notional cap reached"],
                open_positions=BOOK,
                min_candidate_composite=70.0,
                min_hold_days=-1.0,
                protect_winner_roe_pct=15.0,
            )

    def test_decision_is_shadow_by_default_and_serializable(self):
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=82.0,
            blocked_reasons=["notional cap reached"],
            open_positions=BOOK,
            **BASE_THRESHOLDS,
        )
        assert isinstance(d, RotationDecision)
        assert d.shadow_mode is True
        parsed = json.loads(json.dumps(d.to_dict()))
        assert parsed["schema_version"] == "1.0"
        assert parsed["evict_symbol"] == "PG"
        assert parsed["considered"]  # audit trail present


# ─── Alpaca normalizer ───────────────────────────────────────────────────────


class TestNormalizer:
    def test_normalize_maps_plpc_to_roe(self):
        raw = json.loads((FIXTURES / "positions_alpaca.json").read_text())
        norm = normalize_alpaca_positions(raw, age_days_by_symbol={"KO": 40, "PG": 30})
        by_sym = {p["symbol"]: p for p in norm}
        assert by_sym["KO"]["roe_pct"] == pytest.approx(2.5)
        assert by_sym["PG"]["roe_pct"] == pytest.approx(-3.0)
        assert by_sym["KO"]["age_days"] == 40
        # AAPL not in age map -> defaults to 0 (conservative)
        assert by_sym["AAPL"]["age_days"] == 0.0

    def test_normalized_feeds_decision(self):
        raw = json.loads((FIXTURES / "positions_alpaca.json").read_text())
        norm = normalize_alpaca_positions(raw, age_days_by_symbol={"KO": 40, "PG": 30, "AAPL": 120})
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=82.0,
            blocked_reasons=["notional cap reached"],
            open_positions=norm,
            **BASE_THRESHOLDS,
        )
        assert d.evict_symbol == "PG"


# ─── Report generation ───────────────────────────────────────────────────────


class TestReport:
    def test_markdown_has_sections(self):
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=82.0,
            blocked_reasons=["notional cap reached"],
            open_positions=BOOK,
            **BASE_THRESHOLDS,
        )
        md = generate_markdown_report(d)
        assert "# Capital Rotation Decision" in md
        assert "## Verdict" in md
        assert "EVICT_AND_ENTER" in md
        assert "PG" in md
        assert "SHADOW" in md

    def test_markdown_hold_case(self):
        d = decide_rotation(
            candidate_symbol="NVDA",
            candidate_composite=82.0,
            blocked_reasons=["regime risk-off"],
            open_positions=BOOK,
            **BASE_THRESHOLDS,
        )
        md = generate_markdown_report(d)
        assert "HOLD" in md


# ─── CLI ─────────────────────────────────────────────────────────────────────


class TestCLI:
    SCRIPT = "skills/portfolio-manager/scripts/rotation.py"
    REPO_ROOT = Path(__file__).resolve().parents[4]

    def test_cli_writes_reports(self, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                self.SCRIPT,
                "--candidate",
                str(FIXTURES / "candidate_capital_blocked.json"),
                "--positions",
                str(FIXTURES / "positions_rotation.json"),
                "--output-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(self.REPO_ROOT),
        )
        assert result.returncode == 0, result.stderr
        jsons = list(tmp_path.glob("rotation_decision_*.json"))
        mds = list(tmp_path.glob("rotation_decision_*.md"))
        assert jsons and mds
        payload = json.loads(jsons[0].read_text())
        assert payload["should_rotate"] is True
        assert payload["evict_symbol"] == "PG"
        assert payload["shadow_mode"] is True

    def test_cli_alpaca_normalization(self, tmp_path):
        age_map = tmp_path / "ages.json"
        age_map.write_text(json.dumps({"KO": 40, "PG": 30, "AAPL": 120}))
        result = subprocess.run(
            [
                sys.executable,
                self.SCRIPT,
                "--candidate",
                str(FIXTURES / "candidate_capital_blocked.json"),
                "--positions",
                str(FIXTURES / "positions_alpaca.json"),
                "--alpaca",
                "--age-days-json",
                str(age_map),
                "--output-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(self.REPO_ROOT),
        )
        assert result.returncode == 0, result.stderr
        jsons = list(tmp_path.glob("rotation_decision_*.json"))
        payload = json.loads(jsons[0].read_text())
        assert payload["evict_symbol"] == "PG"

    def test_cli_missing_candidate_arg(self):
        result = subprocess.run(
            [sys.executable, self.SCRIPT, "--positions", "x.json"],
            capture_output=True,
            text=True,
            cwd=str(self.REPO_ROOT),
        )
        assert result.returncode != 0
