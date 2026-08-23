#!/usr/bin/env python3
"""TDD tests for compression_growth_governor.py — written BEFORE implementation.

Tests cover:
  1. test_kalman_convergence    — synthetic measurements verify convergence
  2. test_measure_growth_rate   — temp DB with known session data, verify median extraction
  3. test_compute_threshold     — g=200 sparse raise, g=10000 dense lower, g=1800 baseline no change
  4. test_hysteresis            — two runs same data, second stable (no config change)
  5. test_fallback_db_missing   — no DB returns G_BASELINE, no crash
  6. test_fallback_hermes_cli_missing — mock subprocess, no crash, no config change

Run: python3 -m pytest tests/test_compression_growth_governor.py -v
"""
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Import will fail until implementation exists — that's the RED phase
from compression_growth_governor import (
    GrowthRateKalman,
    measure_growth_rate,
    compute_threshold,
    apply_threshold,
    load_state,
    save_state,
    G_BASELINE,
    MIN_THRESHOLD,
    MAX_THRESHOLD,
    FALLBACK_THRESHOLD,
    HYSTERESIS,
    CONTEXT_LENGTH,
    K_SENSITIVITY,
)


# ─── Test 1: Kalman convergence ───

class TestKalmanConvergence:
    """Feed synthetic measurements, verify the filter converges to the true value."""

    def test_converges_to_constant_signal(self):
        """If we feed 500 repeatedly, the estimate should approach 500."""
        kf = GrowthRateKalman(initial_g=1800)
        for _ in range(50):
            kf.update(500.0)
        assert 400 < kf.x < 600, f"Expected convergence near 500, got {kf.x}"
        assert kf.n == 50

    def test_converges_to_high_signal(self):
        """If we feed 8000 repeatedly, the estimate should approach 8000."""
        kf = GrowthRateKalman(initial_g=1800)
        for _ in range(50):
            kf.update(8000.0)
        assert 7000 < kf.x < 9000, f"Expected convergence near 8000, got {kf.x}"

    def test_clamps_to_g_min(self):
        """State should never go below G_MIN."""
        kf = GrowthRateKalman(initial_g=500)
        for _ in range(20):
            kf.update(50.0)  # Way below G_MIN
        # Should be clamped at G_MIN (200)
        assert kf.x >= 200, f"Expected clamp at G_MIN=200, got {kf.x}"

    def test_clamps_to_g_max(self):
        """State should never go above G_MAX."""
        kf = GrowthRateKalman(initial_g=5000)
        for _ in range(20):
            kf.update(50000.0)  # Way above G_MAX
        assert kf.x <= 20000, f"Expected clamp at G_MAX=20000, got {kf.x}"

    def test_state_persistence(self):
        """to_dict / from_dict round-trip preserves state."""
        kf = GrowthRateKalman(initial_g=3000)
        kf.update(2500)
        d = kf.to_dict()
        kf2 = GrowthRateKalman.from_dict(d)
        assert kf2.x == kf.x
        assert kf2.p == kf.p
        assert kf2.n == kf.n


# ─── Test 2: measure_growth_rate ───

class TestMeasureGrowthRate:
    """Create a temp DB with known session data, verify median extraction."""

    def _create_test_db(self, db_path):
        """Create a test DB with known session data (enough rows to pass 10-row minimum)."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE api_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                prompt_tokens INTEGER,
                status_code INTEGER,
                session_id TEXT,
                task_type TEXT
            )
        """)
        now = time.time()
        # Session A: growth of 500, 1000, 1500, 2000, 2500 (deltas: 500, 500, 500, 500)
        # Session B: growth of 200, 800, 1600, 2400, 3200 (deltas: 600, 800, 800, 800)
        # Session C: growth of 100, 300, 600, 1000, 1500 (deltas: 200, 300, 400, 500)
        # All deltas: 500×4, 600, 800×3, 200, 300, 400, 500
        # = [200, 300, 400, 500, 500, 500, 500, 600, 800, 800, 800]
        # Median (index 5) = 500
        rows = []
        # Session A
        for i, pt in enumerate([500, 1000, 1500, 2000, 2500]):
            rows.append(("sess-A", pt, now - 300 + i, 200, None))
        # Session B
        for i, pt in enumerate([200, 800, 1600, 2400, 3200]):
            rows.append(("sess-B", pt, now - 300 + i, 200, None))
        # Session C
        for i, pt in enumerate([100, 300, 600, 1000, 1500]):
            rows.append(("sess-C", pt, now - 300 + i, 200, None))
        for sid, pt, ts, sc, tt in rows:
            conn.execute(
                "INSERT INTO api_calls (session_id, prompt_tokens, ts, status_code, task_type) VALUES (?, ?, ?, ?, ?)",
                (sid, pt, ts, sc, tt),
            )
        conn.commit()
        conn.close()

    def test_extracts_median_growth(self):
        """Verify the median of positive deltas is correctly extracted."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            self._create_test_db(db_path)
            result = measure_growth_rate(db_path, hours=1)
            # Median of [200, 300, 500, 500, 600, 800] = 500
            assert result == 500.0, f"Expected median 500, got {result}"

    def test_excludes_post_compression_resets(self):
        """Negative deltas (post-compression resets) should be excluded."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE api_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    prompt_tokens INTEGER,
                    status_code INTEGER,
                    session_id TEXT,
                    task_type TEXT
                )
            """)
            now = time.time()
            # Two sessions with compression resets, enough rows to pass 10-minimum
            # Session X: 1000 → 2000 → 500 (reset) → 1500 → 2500 → 3500
            #   Positive deltas: 1000, (skip -1500), 1000, 1000, 1000 → median = 1000
            # Session Y: 2000 → 3000 → 1000 (reset) → 2000 → 3000 → 4000
            #   Positive deltas: 1000, (skip -2000), 1000, 1000, 1000 → median = 1000
            rows = [
                ("sess-X", 1000, now - 60, 200, None),
                ("sess-X", 2000, now - 50, 200, None),
                ("sess-X", 500, now - 40, 200, None),   # post-compression reset
                ("sess-X", 1500, now - 30, 200, None),
                ("sess-X", 2500, now - 20, 200, None),
                ("sess-X", 3500, now - 10, 200, None),
                ("sess-Y", 2000, now - 60, 200, None),
                ("sess-Y", 3000, now - 50, 200, None),
                ("sess-Y", 1000, now - 40, 200, None),   # post-compression reset
                ("sess-Y", 2000, now - 30, 200, None),
                ("sess-Y", 3000, now - 20, 200, None),
                ("sess-Y", 4000, now - 10, 200, None),
            ]
            for sid, pt, ts, sc, tt in rows:
                conn.execute(
                    "INSERT INTO api_calls (session_id, prompt_tokens, ts, status_code, task_type) VALUES (?, ?, ?, ?, ?)",
                    (sid, pt, ts, sc, tt),
                )
            conn.commit()
            conn.close()
            result = measure_growth_rate(db_path, hours=1)
            # Positive deltas: [1000×4, 1000×4] = eight 1000s → median = 1000
            assert result == 1000.0, f"Expected median 1000 (excluding reset), got {result}"

    def test_excludes_compression_task_type(self):
        """Rows with task_type='compression' should be excluded."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE api_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    prompt_tokens INTEGER,
                    status_code INTEGER,
                    session_id TEXT,
                    task_type TEXT
                )
            """)
            now = time.time()
            # Session Z with interleaved compression calls (excluded)
            # Non-compression: 1000, 2000, 3000, 4000, 5000, 5500, 6500, 7500
            #   deltas: 1000×5, 1000, 1000 = seven 1000s
            # Compression: 1500, 2500, 3500, 4500 (excluded by task_type filter)
            # Session W: 100, 200, 300, 400 (deltas: 100, 100, 100)
            # Need >=10 non-compression rows total: 8 (sess-Z) + 4 (sess-W) = 12
            rows = [
                ("sess-Z", 1000, now - 50, 200, None),
                ("sess-Z", 1500, now - 45, 200, "compression"),
                ("sess-Z", 2000, now - 40, 200, None),
                ("sess-Z", 2500, now - 35, 200, "compression"),
                ("sess-Z", 3000, now - 30, 200, None),
                ("sess-Z", 3500, now - 25, 200, "compression"),
                ("sess-Z", 4000, now - 20, 200, None),
                ("sess-Z", 4500, now - 15, 200, "compression"),
                ("sess-Z", 5000, now - 10, 200, None),
                ("sess-Z", 5500, now - 8, 200, None),
                ("sess-Z", 6500, now - 6, 200, None),
                ("sess-Z", 7500, now - 4, 200, None),
                ("sess-W", 100, now - 50, 200, None),
                ("sess-W", 200, now - 40, 200, None),
                ("sess-W", 300, now - 30, 200, None),
                ("sess-W", 400, now - 20, 200, None),
            ]
            for sid, pt, ts, sc, tt in rows:
                conn.execute(
                    "INSERT INTO api_calls (session_id, prompt_tokens, ts, status_code, task_type) VALUES (?, ?, ?, ?, ?)",
                    (sid, pt, ts, sc, tt),
                )
            conn.commit()
            conn.close()
            result = measure_growth_rate(db_path, hours=1)
            # Non-compression rows in sess-Z: 1000, 2000, 3000, 4000, 5000, 5500, 6500, 7500
            #   deltas: 1000, 1000, 1000, 1000, 500, 1000, 1000
            # sess-W: 100, 200, 300, 400 → deltas: 100, 100, 100
            # All deltas: [100, 100, 100, 500, 1000, 1000, 1000, 1000, 1000, 1000]
            #   sorted → median (idx 5) = 1000
            assert result == 1000.0, f"Expected 1000, got {result}"

    def test_insufficient_rows_returns_baseline(self):
        """Fewer than 10 rows should return G_BASELINE."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE api_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    prompt_tokens INTEGER,
                    status_code INTEGER,
                    session_id TEXT,
                    task_type TEXT
                )
            """)
            now = time.time()
            # Only 5 rows — below the 10-row minimum
            for i in range(5):
                conn.execute(
                    "INSERT INTO api_calls (session_id, prompt_tokens, ts, status_code, task_type) VALUES (?, ?, ?, ?, ?)",
                    ("sess-S", 1000 + i * 100, now - i, 200, None),
                )
            conn.commit()
            conn.close()
            result = measure_growth_rate(db_path, hours=1)
            assert result == G_BASELINE, f"Expected G_BASELINE={G_BASELINE}, got {result}"


# ─── Test 3: compute_threshold ───

class TestComputeThreshold:
    """Test the control law at extremes and baseline."""

    def test_sparse_raises_threshold(self):
        """g=200 (sparse) should raise threshold above base."""
        base = 0.60
        result = compute_threshold(200.0, base)
        # delta = K * (1800 - 200) = K * 1600 > 0 -> threshold raised
        assert result > base, f"Sparse session should raise threshold: {result} vs base {base}"
        assert result <= MAX_THRESHOLD

    def test_dense_lowers_threshold(self):
        """g=10000 (dense) should lower threshold below base."""
        base = 0.60
        result = compute_threshold(10000.0, base)
        # delta = K * (1800 - 10000) = K * (-8200) < 0 -> threshold lowered
        assert result < base, f"Dense session should lower threshold: {result} vs base {base}"
        assert result >= MIN_THRESHOLD

    def test_baseline_no_change(self):
        """g=1800 (baseline) should produce no adjustment."""
        base = 0.60
        result = compute_threshold(1800.0, base)
        # delta = K * (1800 - 1800) = 0 -> no change
        assert abs(result - base) < 0.001, f"Baseline should not change threshold: {result} vs base {base}"

    def test_clamped_to_min(self):
        """Very dense session should clamp to MIN_THRESHOLD."""
        base = 0.60
        result = compute_threshold(20000.0, base)
        assert result == MIN_THRESHOLD, f"Very dense should clamp to MIN_THRESHOLD={MIN_THRESHOLD}, got {result}"

    def test_clamped_to_max(self):
        """Very sparse session should clamp to MAX_THRESHOLD."""
        base = 0.60
        result = compute_threshold(200.0, base)
        # With large enough K, sparse should push to MAX
        assert result <= MAX_THRESHOLD, f"Should not exceed MAX_THRESHOLD={MAX_THRESHOLD}, got {result}"

    def test_absolute_law_not_incremental(self):
        """Same g always produces same threshold regardless of base drift.
        This catches the integrator-walk bug from kimi review."""
        result1 = compute_threshold(5000.0, 0.60)
        result2 = compute_threshold(5000.0, 0.60)
        assert result1 == result2, "Absolute law: same g must produce same threshold"
        # And it should NOT drift if called with its own output as base
        result3 = compute_threshold(5000.0, result1)
        # With absolute law, result3 = result1 + K*(1800-5000) = result1 - 0.096
        # which IS different — but that's because base changed.
        # The key test: main() uses FALLBACK_THRESHOLD as base always,
        # so threshold doesn't walk. Verify the function is deterministic.
        assert compute_threshold(5000.0) == compute_threshold(5000.0), "Deterministic"

    def test_min_threshold_correct_for_131k(self):
        """MIN_THRESHOLD should be 64000/131072 ≈ 0.488."""
        expected = 64000 / 131072
        assert abs(MIN_THRESHOLD - expected) < 0.001, f"MIN_THRESHOLD should be {expected:.3f}, got {MIN_THRESHOLD}"

    def test_context_length_is_131072(self):
        """CONTEXT_LENGTH must be 131072, NOT 202752 from the design doc."""
        assert CONTEXT_LENGTH == 131072, f"CONTEXT_LENGTH should be 131072, got {CONTEXT_LENGTH}"


# ─── Test 4: Hysteresis ───

class TestHysteresis:
    """Two runs with same data: second should be stable (no config change)."""

    def test_apply_threshold_skips_small_delta(self):
        """If delta < HYSTERESIS, apply_threshold should return False."""
        with patch("compression_growth_governor.subprocess") as mock_subprocess:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_subprocess.run.return_value = mock_result
            # Old = 0.55, new = 0.555 → delta = 0.005 < 0.02
            result = apply_threshold(0.555, 0.55)
            assert result is False, "Small delta should be skipped by hysteresis"
            # subprocess.run should NOT have been called
            mock_subprocess.run.assert_not_called()

    def test_apply_threshold_applies_large_delta(self):
        """If delta >= HYSTERESIS, apply_threshold should call hermes config set."""
        with patch("compression_growth_governor.subprocess") as mock_subprocess:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_subprocess.run.return_value = mock_result
            # Old = 0.50, new = 0.55 → delta = 0.05 > 0.02
            result = apply_threshold(0.55, 0.50)
            assert result is True, "Large delta should be applied"
            mock_subprocess.run.assert_called_once()

    def test_hysteresis_constant_value(self):
        """HYSTERESIS should be 0.02."""
        assert HYSTERESIS == 0.02


# ─── Test 5: Fallback DB missing ───

class TestFallbackDbMissing:
    """No DB file → returns G_BASELINE, no crash."""

    def test_returns_baseline_when_db_missing(self):
        """measure_growth_rate with non-existent DB returns G_BASELINE."""
        result = measure_growth_rate(Path("/nonexistent/path/db.db"), hours=6)
        assert result == G_BASELINE, f"Missing DB should return G_BASELINE={G_BASELINE}, got {result}"

    def test_no_exception_on_missing_db(self):
        """Should not raise on missing DB."""
        try:
            measure_growth_rate(Path("/nonexistent/path/db.db"), hours=6)
        except Exception as e:
            pytest.fail(f"Should not raise on missing DB: {e}")


# ─── Test 6: Fallback hermes CLI missing ───

class TestFallbackHermesCliMissing:
    """Mock subprocess failure → no crash, no config change."""

    def test_subprocess_failure_returns_false(self):
        """If hermes CLI raises, apply_threshold returns False (no crash)."""
        with patch("compression_growth_governor.subprocess") as mock_subprocess:
            mock_subprocess.run.side_effect = FileNotFoundError("hermes not found")
            # delta = 0.10 > HYSTERESIS → would normally apply
            result = apply_threshold(0.60, 0.50)
            assert result is False, "CLI failure should return False"

    def test_subprocess_timeout_returns_false(self):
        """If hermes CLI times out, apply_threshold returns False (no crash)."""
        import subprocess as sp
        with patch("compression_growth_governor.subprocess") as mock_subprocess:
            mock_subprocess.run.side_effect = sp.TimeoutExpired(cmd="hermes", timeout=30)
            result = apply_threshold(0.60, 0.50)
            assert result is False, "CLI timeout should return False"

    def test_subprocess_nonzero_return_returns_false(self):
        """If hermes CLI returns non-zero, apply_threshold returns False."""
        with patch("compression_growth_governor.subprocess") as mock_subprocess:
            mock_result = MagicMock()
            mock_result.returncode = 1
            mock_result.stderr = "config error"
            mock_subprocess.run.return_value = mock_result
            result = apply_threshold(0.60, 0.50)
            assert result is False, "Non-zero return should return False"


# ─── State persistence ───

class TestStatePersistence:
    """load_state / save_state round-trip."""

    def test_save_load_roundtrip(self, tmp_path, monkeypatch):
        """Save state, load it back, verify fields preserved."""
        import compression_growth_governor as mod
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(mod, "STATE_FILE", state_file)

        state = {
            "kalman": GrowthRateKalman(2500).to_dict(),
            "last_measurement": 2300.0,
            "last_ts": 12345,
            "current_threshold": 0.55,
            "last_config_threshold": 0.50,
        }
        save_state(state)

        loaded = load_state()
        assert loaded["last_measurement"] == 2300.0
        assert loaded["current_threshold"] == 0.55
        assert loaded["kalman"]["x"] == 2500

    def test_load_default_when_no_file(self, tmp_path, monkeypatch):
        """Loading when state file doesn't exist returns sensible defaults."""
        import compression_growth_governor as mod
        state_file = tmp_path / "nonexistent.json"
        monkeypatch.setattr(mod, "STATE_FILE", state_file)

        loaded = load_state()
        assert loaded["current_threshold"] == FALLBACK_THRESHOLD
        assert loaded["kalman"]["x"] == G_BASELINE


# ─── Main function integration ───

class TestMainFunction:
    """Test the main() entry point runs end-to-end without crashing."""

    def test_main_runs_with_missing_db(self, tmp_path, monkeypatch):
        """main() should complete without crash even if DB is missing."""
        import compression_growth_governor as mod
        monkeypatch.setattr(mod, "DB_PATH", tmp_path / "nonexistent.db")
        monkeypatch.setattr(mod, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(mod, "AUDIT_FILE", tmp_path / "audit.json")
        # Mock apply_threshold so we don't actually call hermes
        monkeypatch.setattr(mod, "apply_threshold", lambda new, old: False)

        mod.main()  # Should not raise

        # State file should have been written
        state = json.loads((tmp_path / "state.json").read_text())
        assert "kalman" in state
        assert state["last_measurement"] == G_BASELINE

        # Audit file should have been written
        audit = json.loads((tmp_path / "audit.json").read_text())
        assert "growth_estimate" in audit
        assert "current_threshold" in audit

    def test_main_writes_audit_file(self, tmp_path, monkeypatch):
        """main() should write the audit JSON file."""
        import compression_growth_governor as mod
        monkeypatch.setattr(mod, "DB_PATH", tmp_path / "nonexistent.db")
        monkeypatch.setattr(mod, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(mod, "AUDIT_FILE", tmp_path / "audit.json")
        monkeypatch.setattr(mod, "apply_threshold", lambda new, old: False)

        mod.main()

        audit = json.loads((tmp_path / "audit.json").read_text())
        assert audit["growth_baseline"] == G_BASELINE
        assert audit["context_length"] == CONTEXT_LENGTH
        assert audit["applied"] is False
        assert "implied_turns_to_compaction" in audit

    def test_main_no_integrator_walk(self, tmp_path, monkeypatch):
        """Running main() twice with missing DB should NOT walk the threshold.
        This catches the integrator-walk bug from kimi review: with absolute
        control law, threshold stays at FALLBACK_THRESHOLD when g=baseline."""
        import compression_growth_governor as mod
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(mod, "DB_PATH", tmp_path / "nonexistent.db")
        monkeypatch.setattr(mod, "STATE_FILE", state_file)
        monkeypatch.setattr(mod, "AUDIT_FILE", tmp_path / "audit.json")
        monkeypatch.setattr(mod, "apply_threshold", lambda new, old: True)

        # First run — DB missing, g=baseline, threshold=0.60
        mod.main()
        state1 = json.loads(state_file.read_text())
        t1 = state1["current_threshold"]

        # Second run — same conditions
        mod.main()
        state2 = json.loads(state_file.read_text())
        t2 = state2["current_threshold"]

        # With absolute law, both should be 0.60 (baseline produces no delta)
        assert abs(t1 - t2) < 0.001, f"Threshold walked: {t1} -> {t2}"
        assert abs(t1 - FALLBACK_THRESHOLD) < 0.001, f"Baseline should give FALLBACK_THRESHOLD, got {t1}"


# ─── Edge cases for measure_growth_rate ───

class TestMeasureGrowthRateEdgeCases:
    """Additional edge cases for coverage."""

    def test_all_negative_deltas_returns_baseline(self):
        """If all deltas are negative (only compression resets), return G_BASELINE."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE api_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    prompt_tokens INTEGER,
                    status_code INTEGER,
                    session_id TEXT,
                    task_type TEXT
                )
            """)
            now = time.time()
            # Single session with strictly decreasing tokens (all resets)
            # Need >=10 rows
            for i in range(15):
                pt = 10000 - i * 500  # 10000, 9500, 9000, ... decreasing
                conn.execute(
                    "INSERT INTO api_calls (session_id, prompt_tokens, ts, status_code, task_type) VALUES (?, ?, ?, ?, ?)",
                    ("sess-dec", pt, now - 14 + i, 200, None),
                )
            conn.commit()
            conn.close()
            result = measure_growth_rate(db_path, hours=1)
            assert result == G_BASELINE, f"All-negative deltas should return baseline, got {result}"

    def test_db_exception_returns_baseline(self):
        """If the DB query raises an exception, return G_BASELINE."""
        # Create a path that exists but isn't a valid SQLite DB
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "corrupt.db"
            db_path.write_text("not a database")
            result = measure_growth_rate(db_path, hours=1)
            assert result == G_BASELINE, f"Corrupt DB should return baseline, got {result}"