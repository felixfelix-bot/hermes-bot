#!/usr/bin/env python3
"""Tests for compression_growth_governor.py.

Tests cover:
- GrowthRateKalman convergence (feed known data, verify convergence)
- measure_growth_rate with a mock SQLite DB
- compute_threshold at various growth rates (sparse, normal, dense)
- Hysteresis (threshold unchanged when delta < 0.02)
- Dynamic context_length (verify floor changes with different values)
- Fallback when zai_usage.db doesn't exist or is empty
"""
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Import the module under test. We need to set up paths first.
import sys
sys.path.insert(0, str(Path.home() / ".hermes" / "bot"))

from compression_growth_governor import (
    GrowthRateKalman,
    measure_growth_rate,
    compute_threshold,
    apply_threshold,
    read_config,
    load_state,
    save_state,
    main,
    _write_audit,
    G_BASELINE,
    G_MIN,
    G_MAX,
    K_SENSITIVITY,
    FALLBACK_THRESHOLD,
    MAX_THRESHOLD,
    MINIMUM_CONTEXT_LENGTH,
    WINDOW_HOURS,
)


# ---------------------------------------------------------------------------
# 1. Kalman filter convergence
# ---------------------------------------------------------------------------

class TestGrowthRateKalmanConvergence:
    """Feed synthetic measurements and verify the estimate converges."""

    def test_converges_to_low_growth(self):
        """Feed 500 tokens/call repeatedly; estimate should approach 500."""
        kf = GrowthRateKalman()
        assert kf.x == 1800, "Initial state should be G_BASELINE (1800)"
        assert kf.p == 500000.0, "Initial uncertainty should be 500000"
        assert kf.q == 50000.0, "Process noise should be 50000"
        assert kf.r == 300000.0, "Measurement noise should be 300000"

        for _ in range(30):
            kf.predict()
            kf.update(500)

        assert kf.x < 600, f"Estimate {kf.x} should converge below 600"
        assert kf.x > 400, f"Estimate {kf.x} should stay above 400"

    def test_converges_to_high_growth(self):
        """Feed 10000 tokens/call repeatedly; estimate should approach 10000."""
        kf = GrowthRateKalman()
        for _ in range(30):
            kf.predict()
            kf.update(10000)

        assert kf.x > 8000, f"Estimate {kf.x} should converge above 8000"
        assert kf.x < 12000, f"Estimate {kf.x} should stay below 12000"

    def test_clamped_to_g_min(self):
        """Filter should clamp to G_MIN when fed very low measurements."""
        kf = GrowthRateKalman()
        for _ in range(30):
            kf.predict()
            kf.update(0)

        assert kf.x >= G_MIN, f"Estimate {kf.x} should be clamped at G_MIN ({G_MIN})"

    def test_clamped_to_g_max(self):
        """Filter should clamp to G_MAX when fed very high measurements."""
        kf = GrowthRateKalman()
        for _ in range(30):
            kf.predict()
            kf.update(100000)

        assert kf.x <= G_MAX, f"Estimate {kf.x} should be clamped at G_MAX ({G_MAX})"

    def test_update_count_increments(self):
        """n should increment with each update call."""
        kf = GrowthRateKalman()
        assert kf.n == 0
        kf.predict()
        kf.update(1000)
        assert kf.n == 1
        kf.predict()
        kf.update(2000)
        assert kf.n == 2

    def test_predict_increases_uncertainty(self):
        """predict() should increase p by q (process noise)."""
        kf = GrowthRateKalman()
        p_before = kf.p
        kf.predict()
        assert kf.p == p_before + kf.q, "predict() should add q to p"

    def test_to_dict_and_from_dict_roundtrip(self):
        """Kalman state should survive serialization/deserialization."""
        kf = GrowthRateKalman()
        for _ in range(5):
            kf.predict()
            kf.update(2500)

        d = kf.to_dict()
        kf2 = GrowthRateKalman.from_dict(d)
        assert kf2.x == kf.x
        assert kf2.p == kf.p
        assert kf2.q == kf.q
        assert kf2.r == kf.r
        assert kf2.n == kf.n


# ---------------------------------------------------------------------------
# 2. measure_growth_rate with mock SQLite DB
# ---------------------------------------------------------------------------

class TestMeasureGrowthRate:
    """Test measure_growth_rate with a temporary SQLite DB."""

    def _create_test_db(self, db_path, sessions):
        """Create a test DB with the given session data.

        Args:
            db_path: Path to the DB file
            sessions: dict of {session_id: [(prompt_tokens, ts_offset), ...]}
        """
        now = time.time()
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                key_name TEXT,
                key_suffix TEXT,
                model TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                tier TEXT,
                cache_hit INTEGER DEFAULT 0,
                ollama_hit INTEGER DEFAULT 0,
                ppq_hit INTEGER DEFAULT 0,
                status_code INTEGER,
                error TEXT,
                duration_ms INTEGER,
                cost_usd REAL,
                cost_source TEXT,
                session_id TEXT,
                task_type TEXT
            )
        """)

        for sid, calls in sessions.items():
            for i, (pt, ts_offset) in enumerate(calls):
                conn.execute(
                    "INSERT INTO api_calls (ts, prompt_tokens, status_code, session_id, task_type) VALUES (?, ?, ?, ?, ?)",
                    (now - ts_offset, pt, 200, sid, None),
                )
        conn.commit()
        conn.close()

    def test_normal_growth_rate(self):
        """DB with consistent ~1000 token growth should return ~1000."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            sessions = {
                "s1": [(50000, 3600), (51000, 3500), (52000, 3400), (53000, 3300), (54000, 3200), (55000, 3100)],
                "s2": [(30000, 3600), (31000, 3500), (32000, 3400), (33000, 3300), (34000, 3200), (35000, 3100)],
            }
            self._create_test_db(db_path, sessions)

            result = measure_growth_rate(db_path, hours=WINDOW_HOURS)

            assert 500 < result < 1500, f"Expected growth ~1000, got {result}"

    def test_skips_compression_rows(self):
        """Rows with task_type='compression' should be excluded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            now = time.time()
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE api_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL, prompt_tokens INTEGER, status_code INTEGER,
                    session_id TEXT, task_type TEXT
                )
            """)
            # Session with growth, plus compression calls
            for i in range(6):
                conn.execute(
                    "INSERT INTO api_calls (ts, prompt_tokens, status_code, session_id, task_type) VALUES (?, ?, ?, ?, ?)",
                    (now - 3600 + i, 50000 + i * 1000, 200, "s1", None),
                )
            # Insert compression rows (should be excluded)
            conn.execute(
                "INSERT INTO api_calls (ts, prompt_tokens, status_code, session_id, task_type) VALUES (?, ?, ?, ?, ?)",
                (now - 3300, 10000, 200, "s1", "compression"),
            )
            conn.commit()
            conn.close()

            result = measure_growth_rate(db_path, hours=WINDOW_HOURS)
            assert result > 0, "Should still measure growth from non-compression rows"

    def test_excludes_post_compression_drops(self):
        """Negative deltas (post-compression token drops) should be excluded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            sessions = {
                "s1": [
                    (50000, 3600), (51000, 3500),  # growth: +1000
                    (52000, 3400),
                    (10000, 3300),  # compression: -42000 (should be excluded as drop)
                    (11000, 3200),  # growth: +1000
                    (12000, 3100),
                ],
                "s2": [
                    (30000, 3600), (31000, 3500),  # growth: +1000
                    (32000, 3400),
                    (5000, 3300),   # compression: -27000 (should be excluded)
                    (6000, 3200),   # growth: +1000
                    (7000, 3100),
                ],
            }
            self._create_test_db(db_path, sessions)

            result = measure_growth_rate(db_path, hours=WINDOW_HOURS)

            # Only positive deltas should be counted: +1000, +1000, +1000, +1000
            # (negative drops are excluded)
            assert 500 < result < 1500, f"Expected ~1000 (median of positive deltas), got {result}"

    def test_db_missing_returns_baseline(self):
        """Missing DB should return G_BASELINE."""
        result = measure_growth_rate(Path("/nonexistent/db.db"))
        assert result == G_BASELINE, f"Missing DB should return G_BASELINE ({G_BASELINE}), got {result}"

    def test_empty_db_returns_baseline(self):
        """Empty DB (no rows) should return G_BASELINE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE api_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL, prompt_tokens INTEGER, status_code INTEGER,
                    session_id TEXT, task_type TEXT
                )
            """)
            conn.commit()
            conn.close()

            result = measure_growth_rate(db_path, hours=WINDOW_HOURS)
            assert result == G_BASELINE, f"Empty DB should return G_BASELINE, got {result}"

    def test_insufficient_rows_returns_baseline(self):
        """DB with < 10 rows should return G_BASELINE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            now = time.time()
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE api_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL, prompt_tokens INTEGER, status_code INTEGER,
                    session_id TEXT, task_type TEXT
                )
            """)
            for i in range(5):
                conn.execute(
                    "INSERT INTO api_calls (ts, prompt_tokens, status_code, session_id, task_type) VALUES (?, ?, ?, ?, ?)",
                    (now - 3600 + i, 50000 + i * 1000, 200, "s1", None),
                )
            conn.commit()
            conn.close()

            result = measure_growth_rate(db_path, hours=WINDOW_HOURS)
            assert result == G_BASELINE, f"Insufficient rows should return G_BASELINE, got {result}"


# ---------------------------------------------------------------------------
# 3. compute_threshold at various growth rates
# ---------------------------------------------------------------------------

class TestComputeThreshold:
    """Test the control law at sparse, normal, and dense growth rates."""

    CONTEXT_LENGTH = 200000

    def test_sparse_growth_raises_threshold(self):
        """Sparse session (g=200) should raise threshold above FALLBACK."""
        t = compute_threshold(200, self.CONTEXT_LENGTH)
        min_t = 64000 / self.CONTEXT_LENGTH
        assert t > FALLBACK_THRESHOLD, f"Sparse (g=200) should raise threshold above {FALLBACK_THRESHOLD}, got {t}"
        assert t <= MAX_THRESHOLD, f"Threshold should not exceed MAX_THRESHOLD ({MAX_THRESHOLD}), got {t}"

    def test_normal_growth_near_fallback(self):
        """Normal session (g=1800) should produce threshold near FALLBACK."""
        t = compute_threshold(1800, self.CONTEXT_LENGTH)
        delta = abs(t - FALLBACK_THRESHOLD)
        assert delta < 0.01, f"Normal (g=1800) should produce threshold near FALLBACK ({FALLBACK_THRESHOLD}), got {t}"

    def test_dense_growth_lowers_threshold(self):
        """Dense session (g=10000) should lower threshold below FALLBACK."""
        t = compute_threshold(10000, self.CONTEXT_LENGTH)
        assert t < FALLBACK_THRESHOLD, f"Dense (g=10000) should lower threshold below {FALLBACK_THRESHOLD}, got {t}"

    def test_very_dense_hits_floor(self):
        """Very dense session (g=20000) should hit the MIN_THRESHOLD floor."""
        t = compute_threshold(20000, self.CONTEXT_LENGTH)
        min_t = 64000 / self.CONTEXT_LENGTH
        assert abs(t - min_t) < 0.01, f"Very dense (g=20000) should hit MIN_THRESHOLD ({min_t:.4f}), got {t:.4f}"

    def test_never_exceeds_max(self):
        """Threshold should never exceed MAX_THRESHOLD."""
        t = compute_threshold(G_MIN, self.CONTEXT_LENGTH)  # Sparsest possible
        assert t <= MAX_THRESHOLD, f"Threshold should not exceed MAX_THRESHOLD ({MAX_THRESHOLD}), got {t}"

    def test_never_goes_below_min(self):
        """Threshold should never go below MIN_THRESHOLD."""
        t = compute_threshold(G_MAX, self.CONTEXT_LENGTH)  # Densest possible
        min_t = 64000 / self.CONTEXT_LENGTH
        assert t >= min_t, f"Threshold should not go below MIN_THRESHOLD ({min_t:.4f}), got {t:.4f}"

    def test_clamped_growth_rate(self):
        """Growth rate outside [G_MIN, G_MAX] should still be handled."""
        t_neg = compute_threshold(-100, self.CONTEXT_LENGTH)  # Below G_MIN
        t_huge = compute_threshold(100000, self.CONTEXT_LENGTH)  # Above G_MAX
        min_t = 64000 / self.CONTEXT_LENGTH
        assert min_t <= t_neg <= MAX_THRESHOLD
        assert min_t <= t_huge <= MAX_THRESHOLD


# ---------------------------------------------------------------------------
# 4. Hysteresis
# ---------------------------------------------------------------------------

class TestHysteresis:
    """Test that apply_threshold respects the hysteresis threshold."""

    def test_small_delta_not_applied(self):
        """Threshold change < 0.02 should not be applied."""
        with patch('compression_growth_governor.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

            with tempfile.TemporaryDirectory() as tmpdir:
                config_path = Path(tmpdir) / "config.yaml"
                config_path.write_text(
                    f"model:\n  context_length: 200000\ncompression:\n  threshold: 0.4000\n"
                )
                audit_path = Path(tmpdir) / "audit.json"
                with patch('compression_growth_governor.AUDIT_FILE', audit_path):
                    with patch('compression_growth_governor.CONFIG_PATH', config_path):
                        result = apply_threshold(0.4100, config_path)  # delta=0.01 < 0.02

        assert result is False, "Small delta should not be applied"
        mock_run.assert_not_called()

    def test_large_delta_applied(self):
        """Threshold change > 0.02 should be applied via hermes config set."""
        with patch('compression_growth_governor.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="ok")

            with tempfile.TemporaryDirectory() as tmpdir:
                config_path = Path(tmpdir) / "config.yaml"
                config_path.write_text(
                    f"model:\n  context_length: 200000\ncompression:\n  threshold: 0.4000\n"
                )
                audit_path = Path(tmpdir) / "audit.json"
                with patch('compression_growth_governor.AUDIT_FILE', audit_path):
                    with patch('compression_growth_governor.CONFIG_PATH', config_path):
                        result = apply_threshold(0.4500, config_path)  # delta=0.05 > 0.02

        assert result is True, "Large delta should be applied"
        mock_run.assert_called_once()

    def test_config_set_failure_returns_false(self):
        """If hermes config set fails, should return False."""
        with patch('compression_growth_governor.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="error", stdout="")

            with tempfile.TemporaryDirectory() as tmpdir:
                config_path = Path(tmpdir) / "config.yaml"
                config_path.write_text(
                    f"model:\n  context_length: 200000\ncompression:\n  threshold: 0.4000\n"
                )
                audit_path = Path(tmpdir) / "audit.json"
                with patch('compression_growth_governor.AUDIT_FILE', audit_path):
                    with patch('compression_growth_governor.CONFIG_PATH', config_path):
                        result = apply_threshold(0.5000, config_path)

        assert result is False, "Config set failure should return False"


# ---------------------------------------------------------------------------
# 5. Dynamic context_length
# ---------------------------------------------------------------------------

class TestDynamicContextLength:
    """Test that compute_threshold's floor changes with context_length."""

    def test_floor_changes_with_context_length(self):
        """MIN_THRESHOLD = 64000/context_length should vary with context_length."""
        t_200k = compute_threshold(20000, 200000)
        t_128k = compute_threshold(20000, 131072)
        t_1m = compute_threshold(20000, 1000000)

        min_200k = 64000 / 200000  # 0.32
        min_128k = 64000 / 131072  # 0.488
        min_1m = 64000 / 1000000   # 0.064

        assert abs(t_200k - min_200k) < 0.01, f"At 200K, dense should hit floor {min_200k:.4f}, got {t_200k:.4f}"
        assert abs(t_128k - min_128k) < 0.01, f"At 128K, dense should hit floor {min_128k:.4f}, got {t_128k:.4f}"
        assert abs(t_1m - min_1m) < 0.01 or t_1m <= min_1m + 0.01, (
            f"At 1M, dense should hit floor {min_1m:.4f}, got {t_1m:.4f}"
        )

    def test_floor_varies(self):
        """Floors should be different for different context lengths."""
        floor_200k = 64000 / 200000
        floor_128k = 64000 / 131072
        assert floor_200k != floor_128k, "Floors should differ for different context lengths"

    def test_read_config_extracts_context_length(self):
        """read_config should return context_length from config.yaml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "model:\n  default: glm-5.3\n  context_length: 200000\n"
                "compression:\n  threshold: 0.6228\n"
            )
            ctx_len, threshold = read_config(config_path)
            assert ctx_len == 200000, f"Expected context_length 200000, got {ctx_len}"
            assert threshold == 0.6228, f"Expected threshold 0.6228, got {threshold}"

    def test_read_config_missing_returns_defaults(self):
        """read_config should return defaults when config is missing."""
        ctx_len, threshold = read_config(Path("/nonexistent/config.yaml"))
        assert ctx_len == 131072, f"Expected default 131072, got {ctx_len}"
        assert threshold == FALLBACK_THRESHOLD


# ---------------------------------------------------------------------------
# 6. Fallback behavior
# ---------------------------------------------------------------------------

class TestFallback:
    """Test fallback when zai_usage.db is missing or empty."""

    def test_missing_db_returns_baseline(self):
        """Missing DB path should return G_BASELINE."""
        result = measure_growth_rate(Path("/nonexistent/path.db"))
        assert result == G_BASELINE

    def test_empty_db_returns_baseline(self):
        """Empty DB should return G_BASELINE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "empty.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE api_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL, prompt_tokens INTEGER, status_code INTEGER,
                    session_id TEXT, task_type TEXT
                )
            """)
            conn.commit()
            conn.close()

            result = measure_growth_rate(db_path)
            assert result == G_BASELINE

    def test_corrupt_db_returns_baseline(self):
        """Corrupt DB file (not SQLite) should return G_BASELINE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "corrupt.db"
            db_path.write_text("not a database")
            result = measure_growth_rate(db_path)
            assert result == G_BASELINE

    def test_no_sessions_returns_baseline(self):
        """DB with only non-session rows should return G_BASELINE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            now = time.time()
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE api_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL, prompt_tokens INTEGER, status_code INTEGER,
                    session_id TEXT, task_type TEXT
                )
            """)
            for i in range(20):
                conn.execute(
                    "INSERT INTO api_calls (ts, prompt_tokens, status_code, session_id, task_type) VALUES (?, ?, ?, ?, ?)",
                    (now - 3600 + i, 50000 + i * 100, 200, None, None),
                )
            conn.commit()
            conn.close()

            result = measure_growth_rate(db_path)
            assert result == G_BASELINE


# ---------------------------------------------------------------------------
# 7. State persistence, audit, and main()
# ---------------------------------------------------------------------------

class TestStatePersistence:
    """Test load_state / save_state roundtrip and edge cases."""

    def test_save_load_roundtrip(self):
        """State should survive save -> load cycle."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            audit_path = Path(tmpdir) / "audit.json"
            with patch("compression_growth_governor.STATE_FILE", state_path):
                with patch("compression_growth_governor.AUDIT_FILE", audit_path):
                    state = {
                        "kalman": GrowthRateKalman(2500).to_dict(),
                        "last_measurement": 2400.0,
                        "last_ts": 1234567890,
                        "current_threshold": 0.45,
                        "threshold_history": [],
                    }
                    save_state(state)
                    loaded = load_state()
                    assert loaded["kalman"]["x"] == 2500
                    assert loaded["last_measurement"] == 2400.0
                    assert loaded["current_threshold"] == 0.45

    def test_load_corrupt_state(self):
        """Corrupt state file should fall back to defaults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text("not json{{{")
            with patch("compression_growth_governor.STATE_FILE", state_path):
                loaded = load_state()
                assert loaded["kalman"]["x"] == G_BASELINE
                assert loaded["current_threshold"] == FALLBACK_THRESHOLD

    def test_load_missing_state(self):
        """Missing state file should fall back to defaults."""
        with patch("compression_growth_governor.STATE_FILE", Path("/nonexistent/state.json")):
            loaded = load_state()
            assert loaded["kalman"]["x"] == G_BASELINE
            assert loaded["current_threshold"] == FALLBACK_THRESHOLD


class TestAuditFile:
    """Test _write_audit writes proper JSON."""

    def test_audit_written(self):
        """_write_audit should write a valid JSON file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "audit.json"
            with patch("compression_growth_governor.AUDIT_FILE", audit_path):
                _write_audit(0.4500, 0.4000, 200000, True, growth_rate=5000.0, kalman_estimate=4800.0)
                assert audit_path.exists()
                data = json.loads(audit_path.read_text())
                assert data["threshold"] == 0.45
                assert data["old_threshold"] == 0.4
                assert data["applied"] is True
                assert data["context_length"] == 200000
                assert data["growth_rate"] == 5000.0


class TestReadConfigException:
    """Test read_config error handling."""

    def test_corrupt_yaml(self):
        """Corrupt YAML should fall back to defaults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("model: [invalid\n  - broken")
            ctx_len, threshold = read_config(config_path)
            assert ctx_len == 131072
            assert threshold == FALLBACK_THRESHOLD

    def test_missing_keys(self):
        """Config without model.context_length should use default."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("other: stuff\n")
            ctx_len, threshold = read_config(config_path)
            assert ctx_len == 131072
            assert threshold == FALLBACK_THRESHOLD


class TestMain:
    """Test the main() entry point."""

    def test_main_with_mock_db(self):
        """main() should produce JSON output and update state."""
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            audit_path = Path(tmpdir) / "audit.json"
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "model:\n  context_length: 200000\ncompression:\n  threshold: 0.6228\n"
            )

            # Create a test DB with growth data
            db_path = Path(tmpdir) / "test.db"
            now = time.time()
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE api_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL, prompt_tokens INTEGER, status_code INTEGER,
                    session_id TEXT, task_type TEXT
                )
            """)
            for i in range(12):
                conn.execute(
                    "INSERT INTO api_calls (ts, prompt_tokens, status_code, session_id, task_type) VALUES (?, ?, ?, ?, ?)",
                    (now - 3600 + i, 50000 + i * 1000, 200, "sess1", None),
                )
            conn.commit()
            conn.close()

            with patch("compression_growth_governor.STATE_FILE", state_path), \
                 patch("compression_growth_governor.AUDIT_FILE", audit_path), \
                 patch("compression_growth_governor.CONFIG_PATH", config_path), \
                 patch("compression_growth_governor.DB_PATH", db_path), \
                 patch("compression_growth_governor.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="ok")

                f = io.StringIO()
                with redirect_stdout(f):
                    main()

            output = f.getvalue()
            summary = json.loads(output)
            assert "growth_rate" in summary
            assert "kalman_estimate" in summary
            assert "old_threshold" in summary
            assert "new_threshold" in summary
            assert "context_length" in summary
            assert "applied" in summary
            assert summary["context_length"] == 200000
            assert state_path.exists()

    def test_main_db_missing_no_crash(self):
        """main() should not crash when DB is missing (fallback path)."""
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            audit_path = Path(tmpdir) / "audit.json"
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "model:\n  context_length: 200000\ncompression:\n  threshold: 0.6228\n"
            )

            with patch("compression_growth_governor.STATE_FILE", state_path), \
                 patch("compression_growth_governor.AUDIT_FILE", audit_path), \
                 patch("compression_growth_governor.CONFIG_PATH", config_path), \
                 patch("compression_growth_governor.DB_PATH", Path("/nonexistent/db")), \
                 patch("compression_growth_governor.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="ok")

                f = io.StringIO()
                with redirect_stdout(f):
                    main()

            summary = json.loads(f.getvalue())
            assert summary["growth_rate"] == G_BASELINE
            assert summary["context_length"] == 200000
            assert state_path.exists()
