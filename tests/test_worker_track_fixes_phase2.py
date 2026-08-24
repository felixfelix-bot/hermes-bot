#!/usr/bin/env python3
"""
Tests for Phase 2 worker track fixes:
  Q1: Quota gate depth check (viable provider count warning)
  Q3: Human-gate timeout (3-tier taxonomy: stand_down, proceed_isolated, escalate)

These tests validate the core logic without requiring live proxy or kanban state.
Run: python3 -m pytest tests/test_worker_track_fixes_phase2.py -v
"""

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ============================================================================
# Q1: Quota Gate Depth — viable provider count parsing
# ============================================================================

class TestQuotaGateDepth:
    """Test parsing of dispatch_gate JSON for viable provider count."""

    def test_parse_viable_count_from_downgrade_chain(self):
        """Viable count is correctly extracted from downgrade_chain."""
        from test_worker_track_fixes_phase2_helpers import parse_viable_count
        gate_response = {
            "can_dispatch": True,
            "recommended_model": "glm-5.2",
            "downgrade_chain": [
                {"provider": "zai", "model": "glm-5.2", "viable": True},
                {"provider": "ollama_cloud", "model": "deepseek-v4", "viable": True},
                {"provider": "neuralwatt", "model": "kimi-k3", "viable": False},
            ]
        }
        count = parse_viable_count(json.dumps(gate_response))
        assert count == 2

    def test_viable_count_zero_when_all_degraded(self):
        """Viable count is 0 when no providers are viable."""
        from test_worker_track_fixes_phase2_helpers import parse_viable_count
        gate_response = {
            "can_dispatch": False,
            "downgrade_chain": [
                {"provider": "zai", "viable": False},
                {"provider": "ollama_cloud", "viable": False},
            ]
        }
        count = parse_viable_count(json.dumps(gate_response))
        assert count == 0

    def test_viable_count_single_provider(self):
        """Viable count is 1 when only one provider is viable."""
        from test_worker_track_fixes_phase2_helpers import parse_viable_count
        gate_response = {
            "can_dispatch": True,
            "downgrade_chain": [
                {"provider": "ollama_cloud", "viable": True},
                {"provider": "zai", "viable": False},
            ]
        }
        count = parse_viable_count(json.dumps(gate_response))
        assert count == 1

    def test_viable_count_missing_downgrade_chain(self):
        """Viable count defaults to 0 when downgrade_chain is missing."""
        from test_worker_track_fixes_phase2_helpers import parse_viable_count
        gate_response = {"can_dispatch": True}
        count = parse_viable_count(json.dumps(gate_response))
        assert count == 0

    def test_viable_count_malformed_json(self):
        """Viable count defaults to 0 on malformed JSON."""
        from test_worker_track_fixes_phase2_helpers import parse_viable_count
        count = parse_viable_count("not valid json")
        assert count == 0

    def test_viable_count_empty_chain(self):
        """Viable count is 0 when downgrade_chain is empty list."""
        from test_worker_track_fixes_phase2_helpers import parse_viable_count
        gate_response = {"can_dispatch": True, "downgrade_chain": []}
        count = parse_viable_count(json.dumps(gate_response))
        assert count == 0

    def test_should_warn_when_single_viable_provider(self):
        """When can_dispatch=True and viable_count==1, should warn (not block)."""
        from test_worker_track_fixes_phase2_helpers import should_warn_depth
        gate_response = {
            "can_dispatch": True,
            "downgrade_chain": [{"provider": "ollama_cloud", "viable": True}]
        }
        assert should_warn_depth(json.dumps(gate_response)) is True

    def test_should_not_warn_when_multiple_viable_providers(self):
        """When can_dispatch=True and viable_count>=2, should not warn."""
        from test_worker_track_fixes_phase2_helpers import should_warn_depth
        gate_response = {
            "can_dispatch": True,
            "downgrade_chain": [
                {"provider": "zai", "viable": True},
                {"provider": "ollama_cloud", "viable": True},
            ]
        }
        assert should_warn_depth(json.dumps(gate_response)) is False

    def test_should_not_warn_when_cannot_dispatch(self):
        """When can_dispatch=False, depth warning is irrelevant."""
        from test_worker_track_fixes_phase2_helpers import should_warn_depth
        gate_response = {
            "can_dispatch": False,
            "downgrade_chain": [{"provider": "ollama_cloud", "viable": True}]
        }
        assert should_warn_depth(json.dumps(gate_response)) is False

    def test_q1_is_warning_only_never_blocks(self):
        """Q1 design constraint: WARNING only, never blocks dispatch.
        Even with 1 viable provider, the gate still returns can_dispatch=True.
        """
        from test_worker_track_fixes_phase2_helpers import should_warn_depth, parse_viable_count
        gate_response = {
            "can_dispatch": True,
            "downgrade_chain": [{"provider": "only_one", "viable": True}]
        }
        raw = json.dumps(gate_response)
        # The warning fires
        assert should_warn_depth(raw) is True
        # But can_dispatch is still True — no blocking
        d = json.loads(raw)
        assert d["can_dispatch"] is True
        # And viable_count is 1
        assert parse_viable_count(raw) == 1


# ============================================================================
# Q3: Human-Gate Timeout — 3-tier taxonomy
# ============================================================================

class TestHumanGateClassification:
    """Test classification of human-gate reasons into timeout tiers."""

    def test_classify_safety_critical(self):
        """Reasons with safety keywords → stand_down tier."""
        from test_worker_track_fixes_phase2_helpers import classify_gate
        assert classify_gate("human-gate: repo corruption detected") == "stand_down"
        assert classify_gate("human-gate: external interference on workspace") == "stand_down"
        assert classify_gate("human-gate: files were mutated by external tool") == "stand_down"
        assert classify_gate("human-gate: potential data loss, deleted files") == "stand_down"
        assert classify_gate("human-gate: irreversible operation needed") == "stand_down"

    def test_classify_methodology(self):
        """Reasons with methodology keywords → proceed_isolated tier."""
        from test_worker_track_fixes_phase2_helpers import classify_gate
        assert classify_gate("human-gate: should I proceed with isolated clone?") == "proceed_isolated"
        assert classify_gate("human-gate: verify in fresh environment?") == "proceed_isolated"
        assert classify_gate("human-gate: re-run tests in clean workspace?") == "proceed_isolated"
        assert classify_gate("human-gate: clone and verify approach") == "proceed_isolated"

    def test_classify_unknown_escalate(self):
        """Reasons without specific keywords → escalate tier."""
        from test_worker_track_fixes_phase2_helpers import classify_gate
        assert classify_gate("human-gate: unclear requirements") == "escalate"
        assert classify_gate("human-gate: dependency conflict between modules") == "escalate"
        assert classify_gate("human-gate: which API should we use?") == "escalate"
        assert classify_gate("human-gate: worker stuck on ambiguous spec") == "escalate"

    def test_classify_empty_reason(self):
        """Empty reason → escalate (safest default)."""
        from test_worker_track_fixes_phase2_helpers import classify_gate
        assert classify_gate("") == "escalate"
        assert classify_gate("human-gate:") == "escalate"


class TestHumanGateTimeouts:
    """Test timeout values for each tier."""

    def test_stand_down_timeout_is_4h(self):
        """stand_down tier: 4 hour timeout."""
        from test_worker_track_fixes_phase2_helpers import TIER_TIMEOUTS
        assert TIER_TIMEOUTS["stand_down"] == 4 * 3600  # 14400 seconds

    def test_proceed_isolated_timeout_is_2h(self):
        """proceed_isolated tier: 2 hour timeout."""
        from test_worker_track_fixes_phase2_helpers import TIER_TIMEOUTS
        assert TIER_TIMEOUTS["proceed_isolated"] == 2 * 3600  # 7200 seconds

    def test_escalate_timeout_is_4h(self):
        """escalate tier: 4 hour timeout."""
        from test_worker_track_fixes_phase2_helpers import TIER_TIMEOUTS
        assert TIER_TIMEOUTS["escalate"] == 4 * 3600  # 14400 seconds


class TestHumanGateAlertMessages:
    """Test alert message generation for each tier."""

    def test_stand_down_alert_message(self):
        """stand_down: '⏸️ HUMAN-GATE TIMEOUT: Task {id} stood down after 4h'."""
        from test_worker_track_fixes_phase2_helpers import format_timeout_alert
        msg = format_timeout_alert("stand_down", "t_abc123", 14400)
        assert "⏸️" in msg
        assert "t_abc123" in msg
        assert "stood down" in msg.lower()
        assert "4h" in msg or "safety" in msg.lower()

    def test_proceed_isolated_alert_message(self):
        """proceed_isolated: '⏭️ HUMAN-GATE TIMEOUT: Task {id} proceeding with isolated approach after 2h'."""
        from test_worker_track_fixes_phase2_helpers import format_timeout_alert
        msg = format_timeout_alert("proceed_isolated", "t_def456", 7200)
        assert "⏭️" in msg
        assert "t_def456" in msg
        assert "proceeding" in msg.lower() or "isolated" in msg.lower()
        assert "2h" in msg or "methodology" in msg.lower()

    def test_escalate_alert_message(self):
        """escalate: '🚨 HUMAN-GATE TIMEOUT: Task {id} needs escalation after 4h'."""
        from test_worker_track_fixes_phase2_helpers import format_timeout_alert
        msg = format_timeout_alert("escalate", "t_xyz789", 14400)
        assert "🚨" in msg
        assert "t_xyz789" in msg
        assert "escalat" in msg.lower()
        assert "4h" in msg or "gate unanswered" in msg.lower()


class TestHumanGateDedup:
    """Test 24h dedup pattern for human-gate alerts."""

    def test_dedup_first_alert_passes(self):
        """First alert for a task should pass through."""
        from test_worker_track_fixes_phase2_helpers import DedupChecker
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write('{"alerts":{}}')
            state_file = f.name
        try:
            checker = DedupChecker(state_file)
            assert checker.should_alert("t_abc123", "stand_down") is True
        finally:
            os.unlink(state_file)

    def test_dedup_same_task_within_24h_suppressed(self):
        """Same task within 24h should be suppressed."""
        from test_worker_track_fixes_phase2_helpers import DedupChecker
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write('{"alerts":{}}')
            state_file = f.name
        try:
            checker = DedupChecker(state_file)
            # First alert passes
            assert checker.should_alert("t_abc123", "stand_down") is True
            checker.record_alert("t_abc123", "stand_down")
            # Second alert within 24h suppressed
            assert checker.should_alert("t_abc123", "stand_down") is False
        finally:
            os.unlink(state_file)

    def test_dedup_different_tasks_both_pass(self):
        """Different tasks should both get their first alert."""
        from test_worker_track_fixes_phase2_helpers import DedupChecker
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write('{"alerts":{}}')
            state_file = f.name
        try:
            checker = DedupChecker(state_file)
            assert checker.should_alert("t_task1", "stand_down") is True
            checker.record_alert("t_task1", "stand_down")
            assert checker.should_alert("t_task2", "escalate") is True
        finally:
            os.unlink(state_file)

    def test_dedup_after_24h_passes(self):
        """After 24h, same task should alert again."""
        from test_worker_track_fixes_phase2_helpers import DedupChecker
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            # Pre-populate with an old alert (>24h ago)
            old_time = time.time() - 25 * 3600  # 25 hours ago
            f.write(json.dumps({"alerts": {"t_abc123": {"tier": "stand_down", "last_alerted": old_time}}}))
            state_file = f.name
        try:
            checker = DedupChecker(state_file)
            assert checker.should_alert("t_abc123", "stand_down") is True
        finally:
            os.unlink(state_file)


class TestHumanGateKanbanScan:
    """Test scanning kanban boards for blocked tasks with human-gate metadata."""

    def test_scan_finds_blocked_human_gate_tasks(self):
        """Scan correctly identifies blocked tasks with human-gate in block reason."""
        from test_worker_track_fixes_phase2_helpers import scan_blocked_tasks
        with tempfile.TemporaryDirectory() as tmpdir:
            boards_dir = Path(tmpdir) / "boards"
            board_dir = boards_dir / "testboard"
            board_dir.mkdir(parents=True)
            db_path = board_dir / "kanban.db"

            conn = sqlite3.connect(str(db_path))
            # Create schema matching live kanban
            conn.execute("""CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT,
                assignee TEXT, status TEXT NOT NULL, priority INTEGER DEFAULT 0,
                created_by TEXT, created_at INTEGER NOT NULL, started_at INTEGER,
                completed_at INTEGER, workspace_kind TEXT DEFAULT 'scratch',
                workspace_path TEXT, branch_name TEXT, claim_lock TEXT,
                claim_expires INTEGER, tenant TEXT, result TEXT,
                idempotency_key TEXT, consecutive_failures INTEGER DEFAULT 0,
                worker_pid INTEGER, last_failure_error TEXT,
                max_runtime_seconds INTEGER, last_heartbeat_at INTEGER,
                current_run_id INTEGER, workflow_template_id TEXT,
                current_step_key TEXT, skills TEXT, model_override TEXT,
                max_retries INTEGER, goal_mode INTEGER DEFAULT 0,
                goal_max_turns INTEGER, session_id TEXT,
                urgency TEXT, urgency_deadline INTEGER,
                urgency_set_at INTEGER, urgency_source TEXT
            )""")
            conn.execute("""CREATE TABLE task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL, run_id INTEGER, kind TEXT NOT NULL,
                payload TEXT, created_at INTEGER NOT NULL
            )""")

            now = int(time.time())

            # Task 1: blocked with human-gate
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, 'blocked', ?)",
                ("t_001", "Test task with human gate", now)
            )
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'blocked', ?, ?)",
                ("t_001", json.dumps({"reason": "human-gate: repo corruption detected"}), now)
            )

            # Task 2: blocked without human-gate
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, 'blocked', ?)",
                ("t_002", "Task blocked for other reason", now)
            )
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'blocked', ?, ?)",
                ("t_002", json.dumps({"reason": "dependency not ready"}), now)
            )

            # Task 3: not blocked
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, 'running', ?)",
                ("t_003", "Running task", now)
            )

            conn.commit()
            conn.close()

            tasks = scan_blocked_tasks(str(boards_dir))
            # Should find only t_001
            assert len(tasks) == 1
            assert tasks[0]["task_id"] == "t_001"
            assert tasks[0]["board"] == "testboard"
            assert "human-gate" in tasks[0]["block_reason"].lower()

    def test_scan_handles_empty_boards_dir(self):
        """Scan returns empty list for non-existent boards directory."""
        from test_worker_track_fixes_phase2_helpers import scan_blocked_tasks
        tasks = scan_blocked_tasks("/nonexistent/path/boards")
        assert tasks == []

    def test_scan_handles_corrupt_db(self):
        """Scan skips corrupt databases gracefully."""
        from test_worker_track_fixes_phase2_helpers import scan_blocked_tasks
        with tempfile.TemporaryDirectory() as tmpdir:
            boards_dir = Path(tmpdir) / "boards"
            board_dir = boards_dir / "corruptboard"
            board_dir.mkdir(parents=True)
            # Write a non-database file
            (board_dir / "kanban.db").write_text("not a database")
            # Should not raise
            tasks = scan_blocked_tasks(str(boards_dir))
            assert tasks == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])