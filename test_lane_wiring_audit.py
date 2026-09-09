"""Tests for lane_wiring_audit.py — the routability/configuration-gap auditor.

Runs against the pure functions with mocked HTTP + DB + subprocess. No live
network or kanban board access. These tests document the four finding classes
and the ok→broken transition-dedup behaviour that the hourly cron relies on.
"""
from __future__ import annotations
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "lane_wiring_audit", Path.home() / ".hermes/bot/scripts/lane_wiring_audit.py")
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["lane_wiring_audit"] = _MOD
_SPEC.loader.exec_module(_MOD)


# ── helpers ─────────────────────────────────────────────────────────────────

def _quota(*lanes):
    q = {}
    for lane, headroom in lanes:
        if lane in ("ours", "friend"):
            q[lane] = {"windows": [{"name": "weekly", "used_pct": 0 if headroom else 90}]}
        elif lane == "opencode_go":
            q[lane] = {"regime": "included" if headroom else "exhausted",
                       "probe_exhausted": not headroom}
        else:
            q[lane] = {"regime": "included" if headroom else "exhausted",
                       "probe_exhausted": not headroom,
                       "server_used_pct": 0.0 if headroom else 100.0}
    return q


def _health(**lanes):
    d = {}
    for lane, spec in lanes.items():
        d[lane] = {"healthy": spec.get("healthy", 0),
                   "failure_count": spec.get("failure_count", 0),
                   "last_error_type": spec.get("last_error_type"),
                   "backoff_seconds": spec.get("backoff_seconds", 0),
                   "disabled_manually": 0}
    return d


# ── quota headroom ──────────────────────────────────────────────────────────

def test_quota_has_headroom_ours_below_60():
    assert _MOD._quota_has_headroom(
        _quota(("ours", True)), "ours") is True


def test_quota_has_headroom_ours_above_60():
    assert _MOD._quota_has_headroom(
        {"ours": {"windows": [{"name": "weekly", "used_pct": 76}]}}, "ours") is False


def test_quota_has_headroom_ollama_exhausted():
    assert _MOD._quota_has_headroom(
        _quota(("ollama_cloud", False)), "ollama_cloud") is False


def test_quota_has_headroom_ollama_ok():
    assert _MOD._quota_has_headroom(
        _quota(("ollama_cloud_4", True)), "ollama_cloud_4") is True


# ── LANE_WIRING_GAP (the empty _OLLAMA_CLOUD_KEYS class) ─────────────────────

def test_wiring_gap_when_paygo_wins_despite_healthy_quota():
    quota = _quota(("ollama_cloud", True), ("ollama_cloud_4", True))
    health = _health(ollama_cloud={"healthy": 1},
                     ollama_cloud_4={"healthy": 1})
    state = {}
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value="deepseek"), \
         mock.patch.object(_MOD, "probe_lane", return_value=200), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_save_state"):
        rc = _MOD.audit(dry_run=True)
    emit.assert_called()
    args = emit.call_args[0]
    assert args[0] == "LANE_WIRING_GAP"
    assert args[1] in ("ollama_cloud", "ollama_cloud_4")


def test_no_wiring_gap_when_quota_lane_already_wins():
    quota = _quota(("ollama_cloud_4", True))
    health = _health(ollama_cloud_4={"healthy": 1})
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value="ollama_cloud_4"), \
         mock.patch.object(_MOD, "probe_lane", return_value=200), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True)
    emit.assert_not_called()


# ── SUSTAINED_DISPATCH_FAIL ─────────────────────────────────────────────────

def test_sustained_dispatch_fail_when_climbing_and_probe_ok():
    quota = _quota(("ollama_cloud", True))
    health = _health(ollama_cloud={"failure_count": 12, "last_error_type": "dispatch_fail"})
    state = {"failure_counts": {"ollama_cloud": 8}}  # climbing from 8 -> 12
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value="ollama_cloud_4"), \
         mock.patch.object(_MOD, "probe_lane", return_value=200), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True, state=state)
    assert any(c[0][0] == "SUSTAINED_DISPATCH_FAIL" for c in emit.call_args_list)


def test_no_sustained_dispatch_fail_for_stale_count():
    # failure_count NOT climbing (stale pre-fix history) → no finding.
    quota = _quota(("ollama_cloud", True))
    health = _health(ollama_cloud={"failure_count": 87, "last_error_type": "dispatch_fail"})
    state = {"failure_counts": {"ollama_cloud": 87}}
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value="ollama_cloud_4"), \
         mock.patch.object(_MOD, "probe_lane", return_value=200), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True, state=state)
    assert not any(c[0][0] == "SUSTAINED_DISPATCH_FAIL" for c in emit.call_args_list)


# ── QUOTA_MODEL_DRIFT ───────────────────────────────────────────────────────

def test_drift_when_probe_limited_but_quota_says_headroom():
    quota = _quota(("opencode_go", True))  # /quota believes headroom
    health = _health(opencode_go={"last_error_type": None})
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value="ollama_cloud_4"), \
         mock.patch.object(_MOD, "probe_lane", return_value=429), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True)
    assert any(c[0][0] == "QUOTA_MODEL_DRIFT" and c[0][1] == "opencode_go"
               for c in emit.call_args_list)


def test_no_drift_when_exhausted_lane_skipped():
    quota = _quota(("ollama_cloud_3", False))  # genuinely exhausted
    health = _health(ollama_cloud_3={"last_error_type": "exhausted"})
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value="ollama_cloud_4"), \
         mock.patch.object(_MOD, "probe_lane", return_value=429), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True)
    assert not any(c[0][0] == "QUOTA_MODEL_DRIFT" for c in emit.call_args_list)


# ── COST_LEAK ───────────────────────────────────────────────────────────────

def test_cost_leak_when_paygo_spends_while_quota_idle():
    quota = _quota(("ollama_cloud_4", True))
    health = _health(ollama_cloud_4={"healthy": 1})
    spend = {"deepseek": {"tokens": 5_000_000, "cost": 2.0, "calls": 100},
             "ollama_cloud_4": {"tokens": 0, "cost": 0, "calls": 0}}
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value=spend), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value="ollama_cloud_4"), \
         mock.patch.object(_MOD, "probe_lane", return_value=200), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True)
    assert any(c[0][0] == "COST_LEAK" for c in emit.call_args_list)


def test_no_cost_leak_when_quota_lane_serving():
    quota = _quota(("ollama_cloud_4", True))
    health = _health(ollama_cloud_4={"healthy": 1})
    spend = {"deepseek": {"tokens": 100_000, "cost": 0.1, "calls": 5},
             "ollama_cloud_4": {"tokens": 6_000_000, "cost": 0, "calls": 500}}
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value=spend), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value="ollama_cloud_4"), \
         mock.patch.object(_MOD, "probe_lane", return_value=200), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True)
    assert not any(c[0][0] == "COST_LEAK" for c in emit.call_args_list)


# ── operator flags respected ────────────────────────────────────────────────

def test_operator_disabled_lane_is_skipped():
    quota = _quota(("ollama_cloud", True), ("ollama_cloud_4", True))
    health = _health(ollama_cloud={"healthy": 1},
                     ollama_cloud_4={"healthy": 1})
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value={"ollama_cloud"}), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value="deepseek"), \
         mock.patch.object(_MOD, "probe_lane", return_value=200), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True)
    # ollama_cloud is operator-disabled → must NOT be blamed; but oc4 (healthy,
    # not flagged) still can be. Assert ollama_cloud is never the culprit.
    for c in emit.call_args_list:
        if c[0][0] == "LANE_WIRING_GAP":
            assert c[0][1] != "ollama_cloud"


# ── transition dedup ────────────────────────────────────────────────────────

def test_emit_finding_creates_only_on_transition():
    state = {"findings": {}}
    with mock.patch.object(_MOD, "_save_state"), \
         mock.patch.object(_MOD, "_db_conn"), \
         mock.patch.object(_MOD, "_create_fix_task", return_value="t_deadbeef") as create:
        _MOD._emit_finding("COST_LEAK", "ollama_cloud", "t", "d", state, dry_run=False)
        # second call with same key → status open → should NOT create again
        _MOD._emit_finding("COST_LEAK", "ollama_cloud", "t", "d", state, dry_run=False)
    assert create.call_count == 1


def test_resolve_finding_marks_resolved():
    state = {"findings": {"COST_LEAK:ollama_cloud": {"status": "open", "task_id": "t_x"}}}
    with mock.patch.object(_MOD, "_save_state"):
        _MOD._resolve_finding("COST_LEAK", "ollama_cloud", state)
    assert state["findings"]["COST_LEAK:ollama_cloud"]["status"] == "resolved"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
