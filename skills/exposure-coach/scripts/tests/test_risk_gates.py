"""Tests for risk_gates.py — one pass + one fail per gate, plus wiring."""

import json
import subprocess
import sys
from pathlib import Path

from risk_gates import (
    GateContext,
    build_context,
    confidence_gate,
    cooldown_gate,
    correlation_cap,
    daily_giveback_gate,
    daily_loss_kill_switch,
    eval_all_gates,
    extract_dollar_volume,
    extract_news_blackout,
    generate_gate_markdown,
    is_trend_aligned,
    liquidity_floor,
    max_concurrent_positions_gate,
    news_blackout_gate,
    opposite_direction_guard,
    per_trade_notional_cap_gate,
    short_liquidity_floor,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCRIPT = Path(__file__).resolve().parents[1] / "risk_gates.py"


def _ctx(**overrides):
    """GateContext with sensible defaults, overridable per test."""
    base = dict(
        ticker="AAPL",
        side="long",
        confidence=0.80,
        trade_notional_usd=8000.0,
        sector="Technology",
        current_positions=[],
        equity=100000.0,
        daily_pnl=0.0,
        peak_daily_pnl=0.0,
        dollar_volume_usd=8_500_000_000.0,
        trend_aligned=False,
        has_news_blackout=False,
        news_reason="",
        last_trade_epoch_ms=None,
    )
    base.update(overrides)
    return GateContext(**base)


def _load(name):
    return json.loads((FIXTURES / name).read_text())


# --------------------------------------------------------------------------- #
# confidence gate                                                             #
# --------------------------------------------------------------------------- #


class TestConfidenceGate:
    def test_pass(self):
        assert confidence_gate(_ctx(confidence=0.70), 0.65)["pass"] is True

    def test_fail(self):
        result = confidence_gate(_ctx(confidence=0.50), 0.65)
        assert result["pass"] is False
        assert "0.50" in result["reason"]

    def test_regime_aware_lower_bar_for_aligned(self):
        # 0.58 confidence fails the 0.65 default but passes the aligned 0.55 bar.
        config = {"min_confidence": 0.65, "aligned_min_confidence": 0.55}
        aligned = eval_all_gates(_ctx(confidence=0.58, trend_aligned=True), config)
        counter = eval_all_gates(_ctx(confidence=0.58, trend_aligned=False), config)
        assert aligned["results"]["confidence"]["pass"] is True
        assert counter["results"]["confidence"]["pass"] is False


# --------------------------------------------------------------------------- #
# max concurrent                                                              #
# --------------------------------------------------------------------------- #


class TestMaxConcurrent:
    def test_pass(self):
        positions = [{"ticker": "MSFT", "side": "long"}]
        assert max_concurrent_positions_gate(_ctx(current_positions=positions), 5)["pass"] is True

    def test_fail(self):
        positions = [{"ticker": t, "side": "long"} for t in ("A", "B", "C")]
        result = max_concurrent_positions_gate(_ctx(current_positions=positions), 3)
        assert result["pass"] is False
        assert "3/3" in result["reason"]

    def test_zero_disables(self):
        positions = [{"ticker": t} for t in ("A", "B")]
        assert max_concurrent_positions_gate(_ctx(current_positions=positions), 0)["pass"] is True


# --------------------------------------------------------------------------- #
# per-trade notional cap                                                      #
# --------------------------------------------------------------------------- #


class TestNotionalCap:
    def test_pass(self):
        assert per_trade_notional_cap_gate(_ctx(trade_notional_usd=8000), 10000)["pass"] is True

    def test_fail(self):
        result = per_trade_notional_cap_gate(_ctx(trade_notional_usd=12000), 10000)
        assert result["pass"] is False

    def test_precision_tolerance(self):
        # Slight rounding overshoot still passes.
        assert per_trade_notional_cap_gate(_ctx(trade_notional_usd=10040), 10000)["pass"] is True

    def test_zero_disables(self):
        assert per_trade_notional_cap_gate(_ctx(trade_notional_usd=99999), 0)["pass"] is True


# --------------------------------------------------------------------------- #
# daily-loss kill switch                                                      #
# --------------------------------------------------------------------------- #


class TestDailyLossKillSwitch:
    def test_pass_when_above_limit(self):
        assert daily_loss_kill_switch(_ctx(daily_pnl=-500), -3000)["pass"] is True

    def test_fail_when_breached(self):
        result = daily_loss_kill_switch(_ctx(daily_pnl=-3200), -3000)
        assert result["pass"] is False
        assert "kill switch" in result["reason"]

    def test_non_negative_threshold_disables(self):
        # A flat day must never block just because the limit was left at 0.
        assert daily_loss_kill_switch(_ctx(daily_pnl=0.0), 0.0)["pass"] is True


# --------------------------------------------------------------------------- #
# daily give-back halt                                                        #
# --------------------------------------------------------------------------- #


class TestDailyGiveback:
    def test_pass_when_holding_gains(self):
        result = daily_giveback_gate(_ctx(daily_pnl=800, peak_daily_pnl=900), 0.5, 500)
        assert result["pass"] is True

    def test_fail_when_retraced(self):
        # peak 900, halt 50% -> floor 450; pnl 400 <= 450 blocks.
        result = daily_giveback_gate(_ctx(daily_pnl=400, peak_daily_pnl=900), 0.5, 500)
        assert result["pass"] is False
        assert "give-back" in result["reason"]

    def test_disabled_below_min_peak(self):
        # Peak never reached the arming threshold -> gate inactive.
        result = daily_giveback_gate(_ctx(daily_pnl=-100, peak_daily_pnl=200), 0.5, 500)
        assert result["pass"] is True

    def test_zero_pct_disables(self):
        assert (
            daily_giveback_gate(_ctx(daily_pnl=-500, peak_daily_pnl=900), 0.0, 500)["pass"] is True
        )


# --------------------------------------------------------------------------- #
# liquidity floor + separate short floor                                      #
# --------------------------------------------------------------------------- #


class TestLiquidityFloor:
    def test_pass(self):
        assert liquidity_floor(_ctx(dollar_volume_usd=8.5e9), 20e6)["pass"] is True

    def test_fail(self):
        result = liquidity_floor(_ctx(dollar_volume_usd=2.5e6), 20e6)
        assert result["pass"] is False
        assert "below floor" in result["reason"]

    def test_missing_data_not_enforced(self):
        result = liquidity_floor(_ctx(dollar_volume_usd=None), 20e6)
        assert result["pass"] is True
        assert "unavailable" in result["note"]

    def test_zero_disables(self):
        assert liquidity_floor(_ctx(dollar_volume_usd=1.0), 0)["pass"] is True


class TestShortLiquidityFloor:
    def test_long_is_exempt(self):
        # A long name that would fail the deep short floor still passes.
        assert (
            short_liquidity_floor(_ctx(side="long", dollar_volume_usd=50e6), 100e6)["pass"] is True
        )

    def test_short_passes_deep_floor(self):
        assert (
            short_liquidity_floor(_ctx(side="short", dollar_volume_usd=150e6), 100e6)["pass"]
            is True
        )

    def test_short_fails_deep_floor(self):
        # 50M clears the 20M long floor but not the 100M short floor.
        result = short_liquidity_floor(_ctx(side="short", dollar_volume_usd=50e6), 100e6)
        assert result["pass"] is False
        assert "squeeze risk" in result["reason"]

    def test_zero_disables(self):
        assert short_liquidity_floor(_ctx(side="short", dollar_volume_usd=1.0), 0)["pass"] is True


# --------------------------------------------------------------------------- #
# correlation cap                                                             #
# --------------------------------------------------------------------------- #


class TestCorrelationCap:
    def test_pass(self):
        positions = [{"ticker": "MSFT", "side": "long", "sector": "Technology"}]
        assert (
            correlation_cap(_ctx(sector="Technology", current_positions=positions), 3)["pass"]
            is True
        )

    def test_fail(self):
        positions = [
            {"ticker": t, "side": "long", "sector": "Technology"} for t in ("MSFT", "NVDA", "GOOG")
        ]
        result = correlation_cap(_ctx(sector="Technology", current_positions=positions), 3)
        assert result["pass"] is False
        assert "correlation cap" in result["reason"]

    def test_opposite_side_not_counted(self):
        positions = [
            {"ticker": t, "side": "short", "sector": "Technology"} for t in ("MSFT", "NVDA", "GOOG")
        ]
        assert (
            correlation_cap(_ctx(sector="Technology", side="long", current_positions=positions), 3)[
                "pass"
            ]
            is True
        )

    def test_unknown_sector_skips(self):
        positions = [{"ticker": "MSFT", "side": "long", "sector": "Technology"}]
        assert correlation_cap(_ctx(sector="", current_positions=positions), 1)["pass"] is True


# --------------------------------------------------------------------------- #
# cooldown                                                                    #
# --------------------------------------------------------------------------- #


class TestCooldown:
    def test_pass_after_cooldown(self):
        now = 1_000_000 * 60_000  # arbitrary epoch-ms base
        last = now - 45 * 60_000  # 45 min ago
        assert cooldown_gate(_ctx(last_trade_epoch_ms=last), 30, now_epoch_ms=now)["pass"] is True

    def test_fail_within_cooldown(self):
        now = 1_000_000 * 60_000
        last = now - 10 * 60_000  # 10 min ago
        result = cooldown_gate(_ctx(last_trade_epoch_ms=last), 30, now_epoch_ms=now)
        assert result["pass"] is False
        assert "remaining" in result["reason"]

    def test_no_prior_trade_passes(self):
        assert cooldown_gate(_ctx(last_trade_epoch_ms=None), 30, now_epoch_ms=0)["pass"] is True


# --------------------------------------------------------------------------- #
# opposite-direction / pyramid guard                                          #
# --------------------------------------------------------------------------- #


class TestOppositeGuard:
    def test_pass_when_not_held(self):
        positions = [{"ticker": "MSFT", "side": "long"}]
        assert (
            opposite_direction_guard(_ctx(ticker="AAPL", current_positions=positions))["pass"]
            is True
        )

    def test_block_opposite_flip(self):
        positions = [{"ticker": "AAPL", "side": "short"}]
        result = opposite_direction_guard(
            _ctx(ticker="AAPL", side="long", current_positions=positions)
        )
        assert result["pass"] is False
        assert "no auto-flip" in result["reason"]

    def test_block_same_side_pyramid(self):
        positions = [{"ticker": "AAPL", "side": "long"}]
        result = opposite_direction_guard(
            _ctx(ticker="AAPL", side="long", current_positions=positions)
        )
        assert result["pass"] is False
        assert "pyramid" in result["reason"]

    def test_allow_pyramid_flag(self):
        positions = [{"ticker": "AAPL", "side": "long"}]
        assert (
            opposite_direction_guard(
                _ctx(ticker="AAPL", side="long", current_positions=positions), allow_pyramid=True
            )["pass"]
            is True
        )

    def test_allow_pyramid_still_blocks_flip(self):
        positions = [{"ticker": "AAPL", "side": "short"}]
        result = opposite_direction_guard(
            _ctx(ticker="AAPL", side="long", current_positions=positions), allow_pyramid=True
        )
        assert result["pass"] is False


# --------------------------------------------------------------------------- #
# news blackout                                                               #
# --------------------------------------------------------------------------- #


class TestNewsBlackout:
    def test_pass_when_clear(self):
        assert news_blackout_gate(_ctx(has_news_blackout=False))["pass"] is True

    def test_fail_when_blackout(self):
        result = news_blackout_gate(_ctx(has_news_blackout=True, news_reason="earnings in 2 days"))
        assert result["pass"] is False
        assert "earnings in 2 days" in result["reason"]


# --------------------------------------------------------------------------- #
# config tolerance + extractors + trend alignment                            #
# --------------------------------------------------------------------------- #


class TestConfigTolerance:
    def test_camelcase_config_honored(self):
        # camelCase keys must be read identically to snake_case.
        camel = {"minConfidence": 0.9, "maxConcurrent": 1}
        result = eval_all_gates(_ctx(confidence=0.8), camel)
        assert result["results"]["confidence"]["pass"] is False  # 0.8 < 0.9

    def test_snakecase_equivalent(self):
        snake = {"min_confidence": 0.9}
        result = eval_all_gates(_ctx(confidence=0.8), snake)
        assert result["results"]["confidence"]["pass"] is False


class TestExtractors:
    def test_dollar_volume_field_variants(self):
        assert extract_dollar_volume({"dollar_volume_usd": 5e6}) == 5e6
        assert extract_dollar_volume({"adv_usd": 3e6}) == 3e6
        assert extract_dollar_volume({"liquidity": {"dollar_volume": 7e6}}) == 7e6
        assert extract_dollar_volume(None) is None
        assert extract_dollar_volume({}) is None

    def test_news_blackout_explicit_flag(self):
        assert extract_news_blackout({"blackout": True, "headline": "X"}) == (True, "X")
        assert extract_news_blackout({"blackout": False}) == (False, "")

    def test_news_blackout_inferred_from_severity(self):
        blackout, reason = extract_news_blackout({"severity": "critical", "catalyst": "hack"})
        assert blackout is True
        assert reason == "hack"

    def test_trend_alignment(self):
        assert is_trend_aligned("NEW_ENTRY_ALLOWED", "long") is True
        assert is_trend_aligned("NEW_ENTRY_ALLOWED", "short") is False
        assert is_trend_aligned("CASH_PRIORITY", "short") is True
        assert is_trend_aligned("REDUCE_ONLY", "short") is True
        assert is_trend_aligned(None, "long") is False


# --------------------------------------------------------------------------- #
# build_context + eval_all_gates integration from fixtures                    #
# --------------------------------------------------------------------------- #


class TestBuildContextAndEval:
    def test_clean_candidate_approved(self):
        ctx = build_context(
            _load("candidate.json"),
            portfolio=_load("portfolio.json"),
            liquidity=_load("liquidity_high.json"),
            news=_load("news_clear.json"),
            posture=_load("posture_risk_on.json"),
        )
        assert ctx.trend_aligned is True  # long into NEW_ENTRY_ALLOWED
        assert ctx.dollar_volume_usd == 8.5e9
        assert ctx.has_news_blackout is False
        decision = eval_all_gates(ctx, _load("config.json"), now_epoch_ms=1751500000000)
        # last_trade_epoch_ms equals now -> 0 elapsed -> cooldown blocks; drop it.
        assert decision["results"]["cooldown"]["pass"] is False

    def test_clean_candidate_no_cooldown_conflict(self):
        portfolio = _load("portfolio.json")
        # 60 min after last trade clears the 30-min cooldown.
        now = portfolio["last_trade_epoch_ms"] + 60 * 60_000
        ctx = build_context(
            _load("candidate.json"),
            portfolio=portfolio,
            liquidity=_load("liquidity_high.json"),
            news=_load("news_clear.json"),
            posture=_load("posture_risk_on.json"),
        )
        decision = eval_all_gates(ctx, _load("config.json"), now_epoch_ms=now)
        assert decision["approved"] is True
        assert decision["blocked"] is False
        assert decision["block_reasons"] == []

    def test_all_gates_evaluated_no_short_circuit(self):
        # A candidate that trips MANY gates should report ALL of them.
        portfolio = {
            "equity": 100000,
            "daily_pnl": -5000,  # breaches daily loss
            "positions": [{"ticker": "AAPL", "side": "short", "sector": "Technology"}],
        }
        candidate = {
            "ticker": "AAPL",  # already held -> opposite guard
            "side": "long",
            "confidence": 0.10,  # confidence floor
            "trade_notional_usd": 50000,  # notional cap
            "sector": "Technology",
        }
        ctx = build_context(
            candidate,
            portfolio=portfolio,
            liquidity=_load("liquidity_thin.json"),  # liquidity floor
            news=_load("news_blackout.json"),  # news blackout
            posture=_load("posture_risk_on.json"),
        )
        decision = eval_all_gates(ctx, _load("config.json"))
        assert decision["blocked"] is True
        # Every one of these independent blocks must be captured.
        for gate in (
            "confidence",
            "notional_cap",
            "daily_loss",
            "liquidity",
            "opposite_guard",
            "news",
        ):
            assert decision["results"][gate]["pass"] is False, gate
        assert len(decision["block_reasons"]) >= 6

    def test_short_deep_liquidity_from_fixture(self):
        # A short into a $50M name clears the 20M long floor but not 100M short.
        candidate = {
            "ticker": "MIDCO",
            "side": "short",
            "confidence": 0.9,
            "trade_notional_usd": 5000,
        }
        liquidity = {"ticker": "MIDCO", "dollar_volume_usd": 50_000_000}
        ctx = build_context(candidate, liquidity=liquidity, posture=_load("posture_risk_off.json"))
        decision = eval_all_gates(ctx, _load("config.json"))
        assert decision["results"]["liquidity"]["pass"] is True
        assert decision["results"]["short_liquidity"]["pass"] is False


# --------------------------------------------------------------------------- #
# markdown + CLI                                                              #
# --------------------------------------------------------------------------- #


class TestMarkdown:
    def test_markdown_contains_verdict_and_gates(self):
        ctx = _ctx(has_news_blackout=True, news_reason="earnings soon")
        decision = eval_all_gates(ctx, {"min_confidence": 0.65})
        decision["generated_at"] = "2026-07-03T12:00:00+00:00"
        md = generate_gate_markdown(decision, decision["generated_at"])
        assert "Risk Gate Decision" in md
        assert "BLOCKED" in md
        assert "news" in md


class TestCli:
    def test_cli_writes_reports_to_tempdir(self, tmp_path):
        out = tmp_path / "reports"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--candidate",
                str(FIXTURES / "candidate.json"),
                "--portfolio",
                str(FIXTURES / "portfolio.json"),
                "--liquidity",
                str(FIXTURES / "liquidity_high.json"),
                "--news",
                str(FIXTURES / "news_clear.json"),
                "--posture",
                str(FIXTURES / "posture_risk_on.json"),
                "--config",
                str(FIXTURES / "config.json"),
                "--now-ms",
                "1751503600000",
                "--output-dir",
                str(out),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        json_files = list(out.glob("risk_gate_decision_AAPL_*.json"))
        md_files = list(out.glob("risk_gate_decision_AAPL_*.md"))
        assert len(json_files) == 1
        assert len(md_files) == 1
        payload = json.loads(json_files[0].read_text())
        assert payload["ticker"] == "AAPL"
        assert "results" in payload and "approved" in payload
