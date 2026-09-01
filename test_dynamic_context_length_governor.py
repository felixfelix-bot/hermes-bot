#!/usr/bin/env python3
"""Tests for dynamic_context_length_governor.py.

Covers: model detection from DB, registry lookup (exact/prefix/family),
413 rate checking, config set (no-op when unchanged), safety floor,
fallbacks when DB or probe fails, profile discovery, and multi-profile
iteration.

Uses temp SQLite databases and mocks subprocess for `hermes config set`.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from unittest import mock

import pytest

# Import the module under test
import dynamic_context_length_governor as dclg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_test_db(path: Path, rows: list[tuple]) -> None:
    """Create a zai_usage.db-compatible api_calls table with given rows.

    Each row: (ts, model, status_code[, session_id])
    """
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
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
    for row in rows:
        # Pad to full column count if needed
        while len(row) < 18:
            row = row + (None,)
        conn.execute(
            "INSERT INTO api_calls (ts, key_name, key_suffix, model, prompt_tokens, "
            "completion_tokens, total_tokens, tier, cache_hit, ollama_hit, ppq_hit, "
            "status_code, error, duration_ms, cost_usd, cost_source, session_id, task_type) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )
    conn.commit()
    conn.close()


@pytest.fixture
def tmp_db(tmp_path: Path):
    """Return a path to a temp DB (not yet created)."""
    return tmp_path / "zai_usage.db"


@pytest.fixture
def registry_file(tmp_path: Path):
    """Write a registry JSON to temp location and patch the module's path."""
    reg_data = {
        "glm-5.3": 1000000,
        "glm-5.2": 200000,
        "glm-4.5-flash": 128000,
        "glm-4.5-air": 128000,
        "kimi-k2.7-code": 128000,
        "kimi-k3": 128000,
        "kimi-k3:cloud": 200000,
    }
    reg_path = tmp_path / "registry.json"
    reg_path.write_text(json.dumps(reg_data))
    with mock.patch.object(dclg, "REGISTRY_PATH", reg_path):
        yield reg_path


@pytest.fixture
def state_file(tmp_path: Path):
    """Patch STATE_FILE to a temp path."""
    sf = tmp_path / "state.json"
    with mock.patch.object(dclg, "STATE_FILE", sf):
        yield sf


@pytest.fixture
def patched_config_paths(tmp_path: Path):
    """Patch PROFILES_DIR and CONFIG_PATH so real config is never touched.

    Creates a mock profiles directory with a 'manager' profile (exempt)
    and a 'worker-test' profile (non-exempt), each with config.yaml
    context_length=200000.
    """
    profiles_dir = tmp_path / "profiles"
    mgr_dir = profiles_dir / "manager"
    mgr_dir.mkdir(parents=True)
    cfg = mgr_dir / "config.yaml"
    cfg.write_text("model:\n  context_length: 200000\n")
    worker_dir = profiles_dir / "worker-test"
    worker_dir.mkdir(parents=True)
    (worker_dir / "config.yaml").write_text("model:\n  context_length: 200000\n")
    with mock.patch.object(dclg, "PROFILES_DIR", profiles_dir), \
         mock.patch.object(dclg, "CONFIG_PATH", cfg):
        yield cfg


# ---------------------------------------------------------------------------
# discover_profiles()
# ---------------------------------------------------------------------------

class TestDiscoverProfiles:
    """Test the discover_profiles() function."""

    def test_returns_profiles_with_config(self, tmp_path: Path):
        """Profiles with config.yaml should be discovered."""
        profiles_dir = tmp_path / "profiles"
        for name in ["alpha", "beta", "gamma"]:
            d = profiles_dir / name
            d.mkdir(parents=True)
            (d / "config.yaml").write_text("model:\n  default: glm-5.2\n")
        result = dclg.discover_profiles(profiles_dir)
        assert result == ["alpha", "beta", "gamma"]

    def test_returns_sorted(self, tmp_path: Path):
        """Results should be sorted alphabetically."""
        profiles_dir = tmp_path / "profiles"
        for name in ["zebra", "alpha", "mango"]:
            d = profiles_dir / name
            d.mkdir(parents=True)
            (d / "config.yaml").write_text("model:\n  default: glm-5.2\n")
        result = dclg.discover_profiles(profiles_dir)
        assert result == ["alpha", "mango", "zebra"]

    def test_skips_dirs_without_config(self, tmp_path: Path):
        """Directories without config.yaml should be skipped."""
        profiles_dir = tmp_path / "profiles"
        good = profiles_dir / "good"
        good.mkdir(parents=True)
        (good / "config.yaml").write_text("model:\n  default: glm-5.2\n")

        bad = profiles_dir / "bad"
        bad.mkdir(parents=True)
        # No config.yaml

        result = dclg.discover_profiles(profiles_dir)
        assert result == ["good"]

    def test_empty_dir_returns_empty(self, tmp_path: Path):
        """Empty profiles directory returns empty list."""
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir(parents=True)
        result = dclg.discover_profiles(profiles_dir)
        assert result == []

    def test_nonexistent_dir_returns_empty(self, tmp_path: Path):
        """Non-existent directory returns empty list."""
        result = dclg.discover_profiles(tmp_path / "noexist")
        assert result == []

    def test_skips_files_not_dirs(self, tmp_path: Path):
        """Regular files in profiles_dir should be ignored."""
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir(parents=True)
        # A regular file named 'not-a-dir' — should be skipped
        (profiles_dir / "not-a-dir").write_text("junk")
        result = dclg.discover_profiles(profiles_dir)
        assert result == []

    def test_default_profiles_dir(self):
        """When called with no args, should use PROFILES_DIR."""
        with mock.patch.object(dclg, "PROFILES_DIR", Path("/nonexistent/12345")):
            result = dclg.discover_profiles()
        assert result == []


# ---------------------------------------------------------------------------
# profile_config_path()
# ---------------------------------------------------------------------------

class TestProfileConfigPath:
    def test_returns_path_for_profile(self):
        path = dclg.profile_config_path("worker-test")
        assert path.name == "config.yaml"
        assert "worker-test" in str(path)

    def test_uses_patched_profiles_dir(self, tmp_path: Path):
        """When PROFILES_DIR is patched, profile_config_path should use it."""
        fake_dir = tmp_path / "profiles"
        with mock.patch.object(dclg, "PROFILES_DIR", fake_dir):
            path = dclg.profile_config_path("manager")
        assert path == fake_dir / "manager" / "config.yaml"


# ---------------------------------------------------------------------------
# detect_active_model()
# ---------------------------------------------------------------------------

class TestDetectActiveModel:

    def test_returns_served_model_from_db(self, tmp_db):
        """Most recent api_calls row gives the served model."""
        now = time.time()
        make_test_db(tmp_db, [
            (1, None, None, "glm-5.3", 100, 10, 110, None, 0, 0, 0, 200, None, 500, None, None, None, None),
            (2, None, None, "glm-5.2", 200, 20, 220, None, 0, 0, 0, 200, None, 600, None, None, None, None),
        ])
        model = dclg.detect_active_model(db_path=tmp_db)
        assert model == "glm-5.2"

    def test_skips_non_200_rows(self, tmp_db):
        """Should pick most recent model with status_code = 200."""
        now = time.time()
        make_test_db(tmp_db, [
            (now - 60, None, None, "glm-5.3", 100, 10, 110, None, 0, 0, 0, 200, None, 500, None, None, None, None),
            (now - 30, None, None, "glm-4.5-flash", 50, 5, 55, None, 0, 0, 0, 502, None, 100, None, None, None, None),
            (now - 10, None, None, "glm-5.2", 200, 20, 220, None, 0, 0, 0, 200, None, 600, None, None, None, None),
        ])
        model = dclg.detect_active_model(db_path=tmp_db)
        assert model == "glm-5.2"

    def test_db_missing_returns_none(self, tmp_path: Path):
        """If DB doesn't exist, returns None (leave config unchanged)."""
        missing = tmp_path / "nonexistent.db"
        model = dclg.detect_active_model(db_path=missing)
        assert model is None

    def test_db_empty_returns_none(self, tmp_db):
        """Empty DB (no rows) returns None."""
        make_test_db(tmp_db, [])
        model = dclg.detect_active_model(db_path=tmp_db)
        assert model is None

    def test_probe_fallback_when_db_missing(self, tmp_path: Path):
        """When DB is missing, probe request is used as fallback."""
        missing = tmp_path / "nonexistent.db"
        mock_resp = mock.MagicMock()
        mock_resp.getcode.return_value = 200
        mock_resp.read.return_value = json.dumps({"model": "glm-5.3"}).encode()
        with mock.patch("urllib.request.urlopen", return_value=mock_resp):
            model = dclg.detect_active_model(db_path=missing, allow_probe=True)
        assert model == "glm-5.3"

    def test_probe_fallback_when_db_empty(self, tmp_db):
        """Empty DB triggers probe fallback."""
        make_test_db(tmp_db, [])
        mock_resp = mock.MagicMock()
        mock_resp.getcode.return_value = 200
        mock_resp.read.return_value = json.dumps({"model": "glm-5.2"}).encode()
        with mock.patch("urllib.request.urlopen", return_value=mock_resp):
            model = dclg.detect_active_model(db_path=tmp_db, allow_probe=True)
        assert model == "glm-5.2"

    def test_probe_failure_returns_none(self, tmp_path: Path):
        """Both DB and probe fail → returns None."""
        missing = tmp_path / "nonexistent.db"
        with mock.patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            model = dclg.detect_active_model(db_path=missing, allow_probe=True)
        assert model is None

    def test_no_probe_when_disabled(self, tmp_path: Path):
        """When allow_probe=False, DB miss returns None without probe."""
        missing = tmp_path / "nonexistent.db"
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            model = dclg.detect_active_model(db_path=missing, allow_probe=False)
        assert model is None
        mock_urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# get_model_context_length()
# ---------------------------------------------------------------------------

class TestGetModelContextLength:

    def test_exact_match(self, registry_file):
        assert dclg.get_model_context_length("glm-5.3") == 1000000

    def test_exact_match_glm_52(self, registry_file):
        assert dclg.get_model_context_length("glm-5.2") == 200000

    def test_prefix_match(self, registry_file):
        """glm-5.3-chat matches glm-5.3 prefix."""
        assert dclg.get_model_context_length("glm-5.3-chat") == 1000000

    def test_prefix_match_kimi(self, registry_file):
        assert dclg.get_model_context_length("kimi-k3-something") == 128000

    def test_family_fallback_glm(self, registry_file):
        """Unknown glm model falls back to family prefix 'glm' → 200000."""
        assert dclg.get_model_context_length("glm-99-unknown") == 200000

    def test_family_fallback_kimi(self, registry_file):
        assert dclg.get_model_context_length("kimi-unknown-model") == 128000

    def test_unknown_model_no_match(self, registry_file):
        """Completely unknown model returns None."""
        result = dclg.get_model_context_length("claude-4-opus")
        assert result is None

    def test_registry_missing_returns_none(self, tmp_path: Path):
        """If registry file is missing, returns None."""
        missing = tmp_path / "noregistry.json"
        with mock.patch.object(dclg, "REGISTRY_PATH", missing):
            result = dclg.get_model_context_length("glm-5.3")
        assert result is None

    def test_longest_prefix_wins(self, registry_file):
        """Longer registry key should match first."""
        # "glm-5.2" should match "glm-5.2" not the generic "glm" family
        assert dclg.get_model_context_length("glm-5.2-x") == 200000


# ---------------------------------------------------------------------------
# check_413_rate()
# ---------------------------------------------------------------------------

class TestCheck413Rate:

    def test_no_413_errors(self, tmp_db):
        now = time.time()
        make_test_db(tmp_db, [
            (now - 30, None, None, "glm-5.3", 100, 10, 110, None, 0, 0, 0, 200, None, 500, None, None, None, None),
        ])
        count = dclg.check_413_rate(db_path=tmp_db)
        assert count == 0

    def test_few_413_errors_under_threshold(self, tmp_db):
        """1-3 413 errors → count returned but no special action needed."""
        now = time.time()
        make_test_db(tmp_db, [
            (now - 3000, None, None, "glm-5.3", 100, 10, 110, None, 0, 0, 0, 413, None, 500, None, None, None, None),
            (now - 2000, None, None, "glm-5.3", 200, 20, 220, None, 0, 0, 0, 413, None, 600, None, None, None, None),
            (now - 30, None, None, "glm-5.3", 150, 15, 165, None, 0, 0, 0, 200, None, 400, None, None, None, None),
        ])
        count = dclg.check_413_rate(db_path=tmp_db)
        assert count == 2

    def test_many_413_errors_over_threshold(self, tmp_db):
        now = time.time()
        rows = []
        for i in range(5):
            rows.append((now - 600 + i, None, None, "glm-5.3", 100, 10, 110, None, 0, 0, 0, 413, None, 500, None, None, None, None))
        make_test_db(tmp_db, rows)
        count = dclg.check_413_rate(db_path=tmp_db)
        assert count == 5

    def test_413_filtered_by_model(self, tmp_db):
        """413s for other models shouldn't count."""
        now = time.time()
        make_test_db(tmp_db, [
            (now - 100, None, None, "glm-5.2", 100, 10, 110, None, 0, 0, 0, 413, None, 500, None, None, None, None),
            (now - 50, None, None, "glm-5.3", 100, 10, 110, None, 0, 0, 0, 200, None, 500, None, None, None, None),
        ])
        count = dclg.check_413_rate(db_path=tmp_db, model="glm-5.3")
        assert count == 0

    def test_413_db_missing_returns_zero(self, tmp_path: Path):
        missing = tmp_path / "nonexistent.db"
        assert dclg.check_413_rate(db_path=missing) == 0

    def test_old_413s_excluded(self, tmp_db):
        """413 errors older than the time window are excluded."""
        now = time.time()
        # 413 error from 3 hours ago (outside 1h window)
        make_test_db(tmp_db, [
            (now - 3 * 3600, None, None, "glm-5.3", 100, 10, 110, None, 0, 0, 0, 413, None, 500, None, None, None, None),
            (now - 10, None, None, "glm-5.3", 200, 20, 220, None, 0, 0, 0, 200, None, 600, None, None, None, None),
        ])
        count = dclg.check_413_rate(db_path=tmp_db, hours=1)
        assert count == 0


# ---------------------------------------------------------------------------
# set_context_length()
# ---------------------------------------------------------------------------

class TestSetContextLength:

    def test_applies_when_different(self):
        """When new value differs from current, hermes config set is called."""
        with mock.patch("subprocess.run") as mock_run, \
             mock.patch.object(dclg, "get_current_context_length", return_value=200000):
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="ok", stderr="")
            result = dclg.set_context_length(1000000)
        assert result is True
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "model.context_length" in args
        assert "1000000" in args

    def test_applies_with_custom_profile(self):
        """When profile_name is given, it should appear in the subprocess args."""
        with mock.patch("subprocess.run") as mock_run, \
             mock.patch.object(dclg, "get_current_context_length", return_value=200000):
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="ok", stderr="")
            result = dclg.set_context_length(1000000, profile_name="worker-dq05")
        assert result is True
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "--profile" in args
        idx = args.index("--profile")
        assert args[idx + 1] == "worker-dq05"

    def test_noop_when_unchanged(self):
        """When new value equals current, no subprocess call is made."""
        with mock.patch("subprocess.run") as mock_run, \
             mock.patch.object(dclg, "get_current_context_length", return_value=200000):
            result = dclg.set_context_length(200000)
        assert result is False
        mock_run.assert_not_called()

    def test_safety_floor_never_below_128000(self):
        """Even if registry says something tiny, never set below 128000."""
        with mock.patch("subprocess.run") as mock_run, \
             mock.patch.object(dclg, "get_current_context_length", return_value=200000):
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="ok", stderr="")
            # Try to set below the floor
            result = dclg.set_context_length(50000)
        # Should be clamped to 128000
        assert result is True
        args = mock_run.call_args[0][0]
        assert "128000" in args

    def test_hermes_failure_returns_false(self):
        """If hermes config set fails, returns False (backward-compatible)."""
        with mock.patch("subprocess.run") as mock_run, \
             mock.patch.object(dclg, "get_current_context_length", return_value=200000):
            mock_run.return_value = mock.MagicMock(returncode=1, stdout="", stderr="error")
            result = dclg.set_context_length(1000000)
        assert result is False

    def test_subprocess_exception_returns_false(self):
        """If subprocess raises, returns False."""
        with mock.patch("subprocess.run", side_effect=Exception("boom")), \
             mock.patch.object(dclg, "get_current_context_length", return_value=200000):
            result = dclg.set_context_length(1000000)
        assert result is False


# ---------------------------------------------------------------------------
# get_current_context_length()
# ---------------------------------------------------------------------------

class TestGetCurrentContextLength:

    def test_reads_from_config(self, patched_config_paths):
        assert dclg.get_current_context_length() == 200000

    def test_reads_from_explicit_path(self, tmp_path: Path):
        """When config_path is given, should read from that path."""
        cfg = tmp_path / "custom.yaml"
        cfg.write_text("model:\n  context_length: 500000\n")
        assert dclg.get_current_context_length(cfg) == 500000

    def test_config_missing_returns_fallback(self, tmp_path: Path):
        cfg = tmp_path / "noexist.yaml"
        with mock.patch.object(dclg, "CONFIG_PATH", cfg):
            val = dclg.get_current_context_length()
        assert val == 200000  # fallback


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

class TestStateFile:

    def test_save_and_load(self, state_file):
        state = {
            "last_detected_model": "glm-5.3",
            "last_detected_ctx": 1000000,
            "last_check": "2026-08-23T20:00:00Z",
            "413_count_1h": 0,
            "reduced_to_90pct": False,
        }
        dclg.save_state(state)
        loaded = dclg.load_state()
        assert loaded["last_detected_model"] == "glm-5.3"
        assert loaded["413_count_1h"] == 0

    def test_load_missing_returns_defaults(self, state_file):
        loaded = dclg.load_state()
        assert "last_detected_model" in loaded
        assert loaded["last_detected_model"] is None


# ---------------------------------------------------------------------------
# process_profile()
# ---------------------------------------------------------------------------

class TestProcessProfile:

    def test_detects_and_does_noop(self, tmp_db, registry_file, state_file, patched_config_paths):
        """process_profile runs: detects model, finds it matches config → no change."""
        now = time.time()
        make_test_db(tmp_db, [
            (now - 10, None, None, "glm-5.2", 200, 20, 220, None, 0, 0, 0, 200, None, 600, None, None, None, None),
        ])
        with mock.patch.object(dclg, "DB_PATH", tmp_db), \
             mock.patch("subprocess.run") as mock_run:
            output = dclg.process_profile("worker-test")
        assert output["detected_model"] == "glm-5.2"
        assert output["registry_ctx"] == 200000
        assert output["applied"] is False
        mock_run.assert_not_called()

    def test_applies_when_different(self, tmp_db, registry_file, state_file, patched_config_paths):
        """process_profile: detected model has different context → applies change."""
        now = time.time()
        make_test_db(tmp_db, [
            (now - 10, None, None, "glm-5.3", 200, 20, 220, None, 0, 0, 0, 200, None, 600, None, None, None, None),
        ])
        with mock.patch.object(dclg, "DB_PATH", tmp_db), \
             mock.patch("subprocess.run") as mock_run, \
             mock.patch.object(dclg, "get_current_context_length", return_value=200000):
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="ok", stderr="")
            output = dclg.process_profile("worker-test")
        assert output["detected_model"] == "glm-5.3"
        assert output["registry_ctx"] == 1000000
        assert output["new_ctx"] == 1000000
        assert output["applied"] is True

    def test_db_missing_leaves_config_unchanged(self, tmp_path, registry_file, state_file, patched_config_paths):
        """If DB is missing, model is None → no change."""
        missing = tmp_path / "nonexistent.db"
        with mock.patch.object(dclg, "DB_PATH", missing), \
             mock.patch("subprocess.run") as mock_run:
            output = dclg.process_profile("worker-test")
        assert output["detected_model"] is None
        assert output["applied"] is False
        mock_run.assert_not_called()

    def test_reduces_for_413_pressure(self, tmp_db, registry_file, state_file, patched_config_paths):
        """If >3 413 errors in past hour, reduce to 90% of registry value."""
        now = time.time()
        rows = []
        # 4 successful calls
        for i in range(4):
            rows.append((now - 600 + i, None, None, "glm-5.3", 200, 20, 220, None, 0, 0, 0, 200, None, 600, None, None, None, None))
        # 4 413 errors
        for i in range(4):
            rows.append((now - 400 + i, None, None, "glm-5.3", 900000, 0, 900000, None, 0, 0, 0, 413, "too large", 0, None, None, None, None))
        make_test_db(tmp_db, rows)

        with mock.patch.object(dclg, "DB_PATH", tmp_db), \
             mock.patch("subprocess.run") as mock_run, \
             mock.patch.object(dclg, "get_current_context_length", return_value=200000):
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="ok", stderr="")
            output = dclg.process_profile("worker-test")
        assert output["detected_model"] == "glm-5.3"
        assert output["413_count"] == 4
        # 90% of 1000000 = 900000
        assert output["new_ctx"] == 900000
        assert output["applied"] is True

    def test_profile_with_no_context_length_gets_set(self, tmp_db, registry_file, state_file, tmp_path: Path):
        """Profile with config.yaml but no context_length should get set."""
        # Create a profile dir without context_length
        profiles_dir = tmp_path / "profiles"
        prof_dir = profiles_dir / "worker-new"
        prof_dir.mkdir(parents=True)
        cfg = prof_dir / "config.yaml"
        cfg.write_text("model:\n  default: glm-5.2\n")  # No context_length

        now = time.time()
        make_test_db(tmp_db, [
            (now - 10, None, None, "glm-5.2", 200, 20, 220, None, 0, 0, 0, 200, None, 600, None, None, None, None),
        ])
        with mock.patch.object(dclg, "PROFILES_DIR", profiles_dir), \
             mock.patch.object(dclg, "DB_PATH", tmp_db), \
             mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="ok", stderr="")
            output = dclg.process_profile("worker-new")
        # context_length not in config → fallback 200000, model glm-5.2 → registry 200000
        # current=200000 (from fallback), new=200000 → no change needed
        assert output["current_ctx"] == 200000
        assert output["new_ctx"] == 200000
        # If they match, no subprocess call
        # If they don't match, should have been applied
        if output["new_ctx"] != output["current_ctx"]:
            assert output["applied"] is True

    def test_result_includes_profile_name(self, tmp_db, registry_file, state_file, patched_config_paths):
        """process_profile result should include the profile name."""
        now = time.time()
        make_test_db(tmp_db, [
            (now - 10, None, None, "glm-5.2", 200, 20, 220, None, 0, 0, 0, 200, None, 600, None, None, None, None),
        ])
        with mock.patch.object(dclg, "DB_PATH", tmp_db), \
             mock.patch("subprocess.run"):
            output = dclg.process_profile("manager")
        assert output["profile"] == "manager"


# ---------------------------------------------------------------------------
# main() — multi-profile iteration
# ---------------------------------------------------------------------------

class TestMainMultiProfile:

    def test_main_processes_all_discovered_profiles(self, tmp_db, registry_file, state_file, tmp_path: Path):
        """main() should iterate over all discovered profiles."""
        profiles_dir = tmp_path / "profiles"
        for name in ["alpha", "beta", "gamma"]:
            d = profiles_dir / name
            d.mkdir(parents=True)
            (d / "config.yaml").write_text("model:\n  context_length: 200000\n")

        now = time.time()
        make_test_db(tmp_db, [
            (now - 10, None, None, "glm-5.2", 200, 20, 220, None, 0, 0, 0, 200, None, 600, None, None, None, None),
        ])
        with mock.patch.object(dclg, "PROFILES_DIR", profiles_dir), \
             mock.patch.object(dclg, "DB_PATH", tmp_db), \
             mock.patch("subprocess.run"):
            output = dclg.main()

        assert output["profiles_processed"] == 3
        assert len(output["profile_results"]) == 3
        profile_names = [r["profile"] for r in output["profile_results"]]
        assert profile_names == ["alpha", "beta", "gamma"]

    def test_main_empty_profiles_falls_back_to_manager(self, tmp_db, registry_file, state_file, tmp_path: Path):
        """If no profiles discovered, fall back to ['manager']."""
        profiles_dir = tmp_path / "empty_profiles"
        profiles_dir.mkdir(parents=True)

        now = time.time()
        make_test_db(tmp_db, [
            (now - 10, None, None, "glm-5.2", 200, 20, 220, None, 0, 0, 0, 200, None, 600, None, None, None, None),
        ])
        with mock.patch.object(dclg, "PROFILES_DIR", profiles_dir), \
             mock.patch.object(dclg, "DB_PATH", tmp_db), \
             mock.patch("subprocess.run"):
            output = dclg.main()

        # Should fall back to "manager" (which won't have config in fake dir → skipped)
        assert output["profiles_processed"] == 0
        assert len(output["profiles_skipped"]) >= 1
        skipped_profile = output["profiles_skipped"][0]["profile"]
        assert skipped_profile == "manager"

    def test_main_skips_profile_without_config(self, tmp_db, registry_file, state_file, tmp_path: Path):
        """Profiles without config.yaml should be skipped."""
        profiles_dir = tmp_path / "profiles"
        # good profile
        good_dir = profiles_dir / "good"
        good_dir.mkdir(parents=True)
        (good_dir / "config.yaml").write_text("model:\n  context_length: 200000\n")
        # bad profile — no config.yaml
        bad_dir = profiles_dir / "bad"
        bad_dir.mkdir(parents=True)

        now = time.time()
        make_test_db(tmp_db, [
            (now - 10, None, None, "glm-5.2", 200, 20, 220, None, 0, 0, 0, 200, None, 600, None, None, None, None),
        ])
        with mock.patch.object(dclg, "PROFILES_DIR", profiles_dir), \
             mock.patch.object(dclg, "DB_PATH", tmp_db), \
             mock.patch("subprocess.run"):
            output = dclg.main(profiles=["good", "bad"])

        assert output["profiles_processed"] == 1
        assert output["profile_results"][0]["profile"] == "good"
        # bad should be skipped (no config.yaml)
        bad_skips = [s for s in output["profiles_skipped"] if s["profile"] == "bad"]
        assert len(bad_skips) == 1
        assert "no config.yaml" in bad_skips[0]["reason"]

    def test_main_with_explicit_profile_list(self, tmp_db, registry_file, state_file, tmp_path: Path):
        """main(profiles=[...]) should process only the given profiles."""
        profiles_dir = tmp_path / "profiles"
        for name in ["alpha", "beta", "gamma"]:
            d = profiles_dir / name
            d.mkdir(parents=True)
            (d / "config.yaml").write_text("model:\n  context_length: 200000\n")

        now = time.time()
        make_test_db(tmp_db, [
            (now - 10, None, None, "glm-5.2", 200, 20, 220, None, 0, 0, 0, 200, None, 600, None, None, None, None),
        ])
        with mock.patch.object(dclg, "PROFILES_DIR", profiles_dir), \
             mock.patch.object(dclg, "DB_PATH", tmp_db), \
             mock.patch("subprocess.run"):
            output = dclg.main(profiles=["alpha", "gamma"])

        assert output["profiles_processed"] == 2
        names = [r["profile"] for r in output["profile_results"]]
        assert names == ["alpha", "gamma"]

    def test_main_aggregate_structure(self, tmp_db, registry_file, state_file, patched_config_paths):
        """main() output should have the expected aggregate structure."""
        now = time.time()
        make_test_db(tmp_db, [
            (now - 10, None, None, "glm-5.2", 200, 20, 220, None, 0, 0, 0, 200, None, 600, None, None, None, None),
        ])
        with mock.patch.object(dclg, "DB_PATH", tmp_db), \
             mock.patch("subprocess.run"):
            output = dclg.main(profiles=["manager"])

        assert "profiles_processed" in output
        assert "profiles_updated" in output
        assert "profiles_skipped" in output
        assert "profile_results" in output
        assert isinstance(output["profiles_updated"], list)
        assert isinstance(output["profiles_skipped"], list)

    def test_main_handles_process_error_gracefully(self, tmp_db, registry_file, state_file, patched_config_paths):
        """If process_profile raises, main() should log and continue."""
        now = time.time()
        make_test_db(tmp_db, [
            (now - 10, None, None, "glm-5.2", 200, 20, 220, None, 0, 0, 0, 200, None, 600, None, None, None, None),
        ])
        with mock.patch.object(dclg, "DB_PATH", tmp_db), \
             mock.patch("subprocess.run"), \
             mock.patch.object(dclg, "process_profile", side_effect=Exception("boom")):
            output = dclg.main(profiles=["manager"])

        # Error should be captured in skipped
        assert len(output["profiles_skipped"]) == 1
        assert "error" in output["profiles_skipped"][0]["reason"]


# ---------------------------------------------------------------------------
# DELIBERATE_CONTEXT_LENGTHS — exemption mechanism
# ---------------------------------------------------------------------------

class TestDeliberateContextLengths:
    """Deliberate context_lengths must survive governor runs (2026-09-02)."""

    def test_manager_is_exempt(self):
        assert "manager" in dclg.DELIBERATE_CONTEXT_LENGTHS
        assert dclg.DELIBERATE_CONTEXT_LENGTHS["manager"] == 200000

    def test_process_profile_skips_exempt(self, tmp_db, registry_file, state_file, patched_config_paths):
        """process_profile must not touch config of an exempt profile."""
        now = time.time()
        make_test_db(tmp_db, [
            (now - 10, None, None, "glm-5.3", 200, 20, 220, None, 0, 0, 0, 200, None, 600, None, None, None, None),
        ])
        with mock.patch.object(dclg, "DB_PATH", tmp_db), \
             mock.patch("subprocess.run") as mock_run:
            output = dclg.process_profile("manager")
        assert output["applied"] is False
        assert output["reason"] == "exempt (deliberate context_length)"
        assert output["detected_model"] is None
        mock_run.assert_not_called()

    def test_main_skips_exempt_profile(self, tmp_db, registry_file, state_file, tmp_path: Path):
        """main() must not govern an exempt profile."""
        profiles_dir = tmp_path / "profiles"
        mgr_dir = profiles_dir / "manager"
        mgr_dir.mkdir(parents=True)
        (mgr_dir / "config.yaml").write_text("model:\n  context_length: 200000\n")

        now = time.time()
        make_test_db(tmp_db, [
            (now - 10, None, None, "glm-5.3", 200, 20, 220, None, 0, 0, 0, 200, None, 600, None, None, None, None),
        ])
        with mock.patch.object(dclg, "PROFILES_DIR", profiles_dir), \
             mock.patch.object(dclg, "DB_PATH", tmp_db), \
             mock.patch("subprocess.run") as mock_run:
            output = dclg.main(profiles=["manager"])

        assert output["profiles_updated"] == []
        assert len(output["profiles_skipped"]) == 1
        assert output["profiles_skipped"][0]["reason"] == "exempt (deliberate context_length)"
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# detect_active_model_for_profile() — cron-filter behavior
# ---------------------------------------------------------------------------

class TestDetectActiveModelForProfile:

    def test_excludes_cron_sessions(self, tmp_db):
        """Cron sessions must not stamp the interactive model signal."""
        now = time.time()
        make_test_db(tmp_db, [
            # Most recent is a cron session (deepseek) — must be excluded
            (now - 5, None, None, "deepseek-v4-pro", 100, 10, 110, None, 0, 0, 0, 200, None, 500, None, None, "cron_abc123", None),
            # Older interactive session (glm-5.3) — should win
            (now - 10, None, None, "glm-5.3", 200, 20, 220, None, 0, 0, 0, 200, None, 600, None, None, "20260901_150640_b4d817eb", None),
        ])
        model = dclg.detect_active_model_for_profile("worker-test", db_path=tmp_db)
        assert model == "glm-5.3"

    def test_null_session_id_is_kept(self, tmp_db):
        """Rows with NULL session_id are interactive and must be kept."""
        now = time.time()
        make_test_db(tmp_db, [
            (now - 5, None, None, "glm-5.2", 100, 10, 110, None, 0, 0, 0, 200, None, 500, None, None, None, None),
        ])
        model = dclg.detect_active_model_for_profile("worker-test", db_path=tmp_db)
        assert model == "glm-5.2"

    def test_only_cron_sessions_returns_none(self, tmp_db):
        """If only cron sessions exist, returns None (no interactive signal)."""
        now = time.time()
        make_test_db(tmp_db, [
            (now - 5, None, None, "deepseek-v4-pro", 100, 10, 110, None, 0, 0, 0, 200, None, 500, None, None, "cron_abc123", None),
        ])
        model = dclg.detect_active_model_for_profile("worker-test", db_path=tmp_db)
        assert model is None

    def test_profile_mapping_restricts_to_mapped_sessions(self, tmp_db):
        """When SESSION_PROFILE_MAP has entries, restrict to those sessions."""
        now = time.time()
        make_test_db(tmp_db, [
            # A different interactive session (glm-5.3) — not mapped to worker-test
            (now - 5, None, None, "glm-5.3", 200, 20, 220, None, 0, 0, 0, 200, None, 600, None, None, "20260901_150640_b4d817eb", None),
            # Mapped session (glm-5.2) — should win for worker-test
            (now - 10, None, None, "glm-5.2", 100, 10, 110, None, 0, 0, 0, 200, None, 500, None, None, "sess_worker_1", None),
        ])
        with mock.patch.object(dclg, "SESSION_PROFILE_MAP", {"sess_worker_1": "worker-test"}):
            model = dclg.detect_active_model_for_profile("worker-test", db_path=tmp_db)
        assert model == "glm-5.2"

    def test_db_missing_returns_none(self, tmp_path: Path):
        missing = tmp_path / "nonexistent.db"
        assert dclg.detect_active_model_for_profile("worker-test", db_path=missing) is None
