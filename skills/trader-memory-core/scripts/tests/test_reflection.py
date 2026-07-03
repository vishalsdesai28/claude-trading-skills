"""Tests for alpha attribution + the reflection log (thesis_review + reflection_log)."""

from pathlib import Path

import pytest
import reflection_log
import thesis_review
import thesis_store

# -- Helpers -------------------------------------------------------------------

_COUNTER = 0


def _make_thesis_data(**overrides):
    global _COUNTER
    _COUNTER += 1
    data = {
        "ticker": "AAPL",
        "thesis_type": "growth_momentum",
        "thesis_statement": f"AAPL reflection test thesis #{_COUNTER}",
        "evidence": ["Accelerating revenue growth"],
        "origin": {"skill": "test", "output_file": "test.json"},
    }
    data.update(overrides)
    return data


def _closed_thesis(state_dir: Path, entry_price=150.0, exit_price=165.0, **data_overrides):
    """Register → ENTRY_READY → ACTIVE → CLOSED, return thesis_id."""
    tid = thesis_store.register(state_dir, _make_thesis_data(**data_overrides))
    thesis_store.transition(state_dir, tid, "ENTRY_READY", "ok")
    thesis_store.open_position(state_dir, tid, entry_price, "2026-03-01T10:00:00+00:00")
    thesis_store.close(state_dir, tid, "target_hit", exit_price, "2026-04-01T10:00:00+00:00")
    return tid


class MockBenchmarkAdapter:
    """Return fixed closes for any symbol requested."""

    def __init__(self, closes):
        self.closes = closes
        self.calls = []

    def get_daily_closes(self, ticker, from_date, to_date):
        self.calls.append((ticker, from_date, to_date))
        return list(self.closes)


# -- compute_alpha ------------------------------------------------------------


def test_compute_alpha_beats_benchmark(tmp_path: Path):
    """raw +10% vs SPY +5% → alpha +5pp."""
    state_dir = tmp_path / "theses"
    tid = _closed_thesis(state_dir, entry_price=150.0, exit_price=165.0)  # +10%
    thesis = thesis_store.get(state_dir, tid)

    bench = MockBenchmarkAdapter(
        [{"date": "2026-03-01", "close": 400.0}, {"date": "2026-04-01", "close": 420.0}]  # +5%
    )
    result = thesis_review.compute_alpha(thesis, bench, benchmark="SPY")

    assert result["raw_return_pct"] == pytest.approx(10.0, abs=0.01)
    assert result["benchmark_return_pct"] == pytest.approx(5.0, abs=0.01)
    assert result["alpha_pct"] == pytest.approx(5.0, abs=0.01)
    assert result["benchmark"] == "SPY"
    assert result["holding_days"] == 31
    # Benchmark symbol (not the ticker) is what gets fetched.
    assert bench.calls[0][0] == "SPY"


def test_compute_alpha_lags_benchmark(tmp_path: Path):
    """raw +10% vs SPY +25% → negative alpha."""
    state_dir = tmp_path / "theses"
    tid = _closed_thesis(state_dir, entry_price=100.0, exit_price=110.0)  # +10%
    thesis = thesis_store.get(state_dir, tid)

    bench = MockBenchmarkAdapter(
        [{"date": "2026-03-01", "close": 400.0}, {"date": "2026-04-01", "close": 500.0}]  # +25%
    )
    result = thesis_review.compute_alpha(thesis, bench)
    assert result["alpha_pct"] == pytest.approx(-15.0, abs=0.01)


def test_compute_alpha_no_adapter_returns_raw_only(tmp_path: Path):
    """No benchmark adapter → raw return set, alpha stays None."""
    state_dir = tmp_path / "theses"
    tid = _closed_thesis(state_dir, entry_price=100.0, exit_price=112.0)
    thesis = thesis_store.get(state_dir, tid)

    result = thesis_review.compute_alpha(thesis, None)
    assert result["raw_return_pct"] == pytest.approx(12.0, abs=0.01)
    assert result["benchmark_return_pct"] is None
    assert result["alpha_pct"] is None


def test_compute_alpha_insufficient_benchmark_data(tmp_path: Path):
    """A single benchmark close cannot form a return → alpha None."""
    state_dir = tmp_path / "theses"
    tid = _closed_thesis(state_dir)
    thesis = thesis_store.get(state_dir, tid)

    bench = MockBenchmarkAdapter([{"date": "2026-03-01", "close": 400.0}])
    result = thesis_review.compute_alpha(thesis, bench)
    assert result["benchmark_return_pct"] is None
    assert result["alpha_pct"] is None
    assert result["raw_return_pct"] is not None


# -- compose_reflection -------------------------------------------------------


def test_compose_reflection_cites_alpha_not_raw(tmp_path: Path):
    state_dir = tmp_path / "theses"
    tid = _closed_thesis(state_dir, entry_price=150.0, exit_price=165.0)
    thesis = thesis_store.get(state_dir, tid)
    alpha_info = {
        "raw_return_pct": 10.0,
        "benchmark_return_pct": 5.0,
        "alpha_pct": 5.0,
        "benchmark": "SPY",
        "holding_days": 31,
    }
    text = thesis_review.compose_reflection(thesis, alpha_info)

    assert "5.0pp" in text  # alpha figure cited
    assert "SPY" in text
    assert "correct" in text
    assert "Accelerating revenue growth" in text  # pillar surfaced
    assert "Lesson:" in text
    # 2-4 sentences → 2 to 4 period-terminated segments.
    assert 2 <= text.count(". ") + text.count(".\n") + text.endswith(".") <= 6


def test_compose_reflection_handles_missing_alpha(tmp_path: Path):
    state_dir = tmp_path / "theses"
    tid = _closed_thesis(state_dir)
    thesis = thesis_store.get(state_dir, tid)
    alpha_info = {
        "raw_return_pct": None,
        "benchmark_return_pct": None,
        "alpha_pct": None,
        "benchmark": "SPY",
        "holding_days": None,
    }
    text = thesis_review.compose_reflection(thesis, alpha_info)
    assert "neither the raw return nor the alpha could be computed" in text.lower()


# -- reflection_log lifecycle -------------------------------------------------


def test_store_pending_then_resolve(tmp_path: Path):
    log = tmp_path / "reflection_log.md"
    assert reflection_log.store_pending(log, "th_x", "AAPL", "high", "buy the dip") is True

    entries = reflection_log.load_entries(log)
    assert len(entries) == 1
    assert entries[0]["pending"] is True

    assert (
        reflection_log.resolve(
            log, "th_x", raw_return=10.0, alpha=5.0, holding_days=31, reflection="Nice trade."
        )
        is True
    )
    entries = reflection_log.load_entries(log)
    assert len(entries) == 1
    e = entries[0]
    assert e["pending"] is False
    assert e["raw_return"] == "+10.0%"
    assert e["alpha"] == "+5.0%"
    assert e["holding"] == "31d"
    assert e["reflection"] == "Nice trade."


def test_store_pending_idempotent(tmp_path: Path):
    log = tmp_path / "reflection_log.md"
    assert reflection_log.store_pending(log, "th_x", "AAPL", "high", "d1") is True
    # Second call for the same thesis is a no-op.
    assert reflection_log.store_pending(log, "th_x", "AAPL", "high", "d1") is False
    assert len(reflection_log.load_entries(log)) == 1


def test_resolve_idempotent_and_byte_stable(tmp_path: Path):
    log = tmp_path / "reflection_log.md"
    reflection_log.store_pending(log, "th_x", "AAPL", "high", "d1")
    assert (
        reflection_log.resolve(
            log, "th_x", raw_return=10.0, alpha=5.0, holding_days=31, reflection="r"
        )
        is True
    )
    first = log.read_text()
    # Re-resolving an already-resolved entry does nothing and leaves the file intact.
    assert (
        reflection_log.resolve(
            log, "th_x", raw_return=99.0, alpha=99.0, holding_days=99, reflection="different"
        )
        is False
    )
    assert log.read_text() == first


def test_resolve_missing_pending_returns_false(tmp_path: Path):
    log = tmp_path / "reflection_log.md"
    assert (
        reflection_log.resolve(
            log, "th_missing", raw_return=1.0, alpha=1.0, holding_days=1, reflection="x"
        )
        is False
    )


# -- get_past_context ----------------------------------------------------------


def _log_resolved(log, tid, ticker, reflection):
    reflection_log.store_pending(log, tid, ticker, "high", f"decision for {tid}")
    reflection_log.resolve(
        log, tid, raw_return=1.0, alpha=2.0, holding_days=10, reflection=reflection
    )


def test_get_past_context_same_full_and_cross_reflection_only(tmp_path: Path):
    log = tmp_path / "reflection_log.md"
    _log_resolved(log, "th_a1", "AAPL", "AAPL lesson one")
    _log_resolved(log, "th_a2", "AAPL", "AAPL lesson two")
    _log_resolved(log, "th_m1", "MSFT", "MSFT lesson")

    block = reflection_log.get_past_context(log, "aapl", n_same=2, n_cross=2)

    assert "Past theses on AAPL" in block
    assert "DECISION: decision for th_a2" in block  # same-ticker full includes decision
    assert "AAPL lesson two" in block
    assert "Recent cross-ticker lessons:" in block
    assert "MSFT lesson" in block
    # Cross-ticker entries are reflection-only: no DECISION line for MSFT.
    assert "decision for th_m1" not in block


def test_get_past_context_skips_pending_and_empty(tmp_path: Path):
    log = tmp_path / "reflection_log.md"
    reflection_log.store_pending(log, "th_p", "AAPL", "high", "still open")  # pending only
    assert reflection_log.get_past_context(log, "AAPL") == ""
    # Missing file → empty string.
    assert reflection_log.get_past_context(tmp_path / "nope.md", "AAPL") == ""


# -- generate_postmortem integration ------------------------------------------


def test_generate_postmortem_computes_alpha_and_logs(tmp_path: Path):
    state_dir = tmp_path / "theses"
    journal_dir = tmp_path / "journal"
    tid = _closed_thesis(state_dir, entry_price=150.0, exit_price=165.0)  # +10%

    bench = MockBenchmarkAdapter(
        [{"date": "2026-03-01", "close": 400.0}, {"date": "2026-04-01", "close": 420.0}]  # +5%
    )
    pm_path = thesis_review.generate_postmortem(
        tid, str(state_dir), benchmark_adapter=bench, journal_dir=str(journal_dir)
    )

    content = Path(pm_path).read_text()
    assert "Alpha vs SPY" in content
    assert "## Reflection" in content

    # Outcome persisted on the thesis.
    thesis = thesis_store.get(state_dir, tid)
    assert thesis["outcome"]["alpha_pct"] == pytest.approx(5.0, abs=0.01)
    assert thesis["outcome"]["benchmark"] == "SPY"

    # Reflection log resolved.
    log = journal_dir / "reflection_log.md"
    entries = reflection_log.load_entries(log)
    assert len(entries) == 1
    assert entries[0]["pending"] is False
    assert entries[0]["alpha"] == "+5.0%"


def test_generate_postmortem_reflection_log_idempotent(tmp_path: Path):
    state_dir = tmp_path / "theses"
    journal_dir = tmp_path / "journal"
    tid = _closed_thesis(state_dir, entry_price=150.0, exit_price=165.0)

    bench = MockBenchmarkAdapter(
        [{"date": "2026-03-01", "close": 400.0}, {"date": "2026-04-01", "close": 420.0}]
    )
    thesis_review.generate_postmortem(
        tid, str(state_dir), benchmark_adapter=bench, journal_dir=str(journal_dir)
    )
    log = journal_dir / "reflection_log.md"
    first = log.read_text()

    # Re-running the postmortem must not duplicate or corrupt the log entry.
    thesis_review.generate_postmortem(
        tid, str(state_dir), benchmark_adapter=bench, journal_dir=str(journal_dir)
    )
    assert log.read_text() == first
    assert len(reflection_log.load_entries(log)) == 1


def test_generate_postmortem_with_reflection_disabled(tmp_path: Path):
    state_dir = tmp_path / "theses"
    journal_dir = tmp_path / "journal"
    tid = _closed_thesis(state_dir)

    thesis_review.generate_postmortem(
        tid, str(state_dir), journal_dir=str(journal_dir), with_reflection=False
    )
    assert not (journal_dir / "reflection_log.md").exists()
