#!/usr/bin/env python3
"""tests/test_routstr_delist.py — T-D delist script (ADR-007 Gate 4).

Covers all trigger/decision/state/backend helpers. Pure functions are
fully unit-tested; the remote delist action is tested against a mock
backend so the suite is hermetic.

Run: python3 -m pytest tests/test_routstr_delist.py -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time

# Load the delivered script by absolute path.
_SCRIPT_PATH = os.path.expanduser("~/.hermes/bot/scripts/routstr_delist.py")
_spec = importlib.util.spec_from_file_location("routstr_delist", _SCRIPT_PATH)
assert _spec and _spec.loader, f"cannot build spec for {_SCRIPT_PATH}"
rd = importlib.util.module_from_spec(_spec)
sys.modules["routstr_delist"] = rd
_spec.loader.exec_module(rd)


# ── Fixtures ──────────────────────────────────────────────────────────────
def _pred(key="routstr", window="weekly", exhaust_hours=40.0,
          will_exhaust=False, uncertainty=0.05, burn_tph=1000.0, vel=0.0):
    return {
        "key": key,
        "window": window,
        "exhausts_in_hours": exhaust_hours,
        "will_exhaust": will_exhaust,
        "uncertainty": uncertainty,
        "burn_rate_tph": burn_tph,
        "velocity_tph2": vel,
    }


# ── exhaustion_accelerated ────────────────────────────────────────────────
class TestExhaustionAccelerated:
    def test_no_acceleration_returns_empty(self):
        now = _pred(exhaust_hours=40.0)
        prev = _pred(exhaust_hours=40.0)
        assert rd.exhaustion_accelerated([now], [prev]) == []

    def test_moved_forward_by_threshold_fires(self):
        now = _pred(exhaust_hours=38.0)
        prev = _pred(exhaust_hours=40.0)
        res = rd.exhaustion_accelerated([now], [prev], move_hours=2.0)
        assert len(res) == 1
        assert res[0]["prev_hours"] == 40.0
        assert res[0]["now_hours"] == 38.0

    def test_moved_forward_below_threshold_does_not_fire(self):
        now = _pred(exhaust_hours=39.0)
        prev = _pred(exhaust_hours=40.0)
        assert rd.exhaustion_accelerated([now], [prev], move_hours=2.0) == []

    def test_multiple_windows_some_fire(self):
        now = [_pred(key="a", exhaust_hours=10.0), _pred(key="b", exhaust_hours=20.0)]
        prev = [_pred(key="a", exhaust_hours=15.0), _pred(key="b", exhaust_hours=20.0)]
        res = rd.exhaustion_accelerated(now, prev, move_hours=2.0)
        assert len(res) == 1
        assert res[0]["key"] == "a"

    def test_no_baseline_returns_empty(self):
        now = [_pred(exhaust_hours=10.0)]
        assert rd.exhaustion_accelerated(now, []) == []
        assert rd.exhaustion_accelerated(now, None) == []

    def test_none_hours_skipped(self):
        now = [{"key": "a", "window": "w", "exhausts_in_hours": None}]
        prev = [{"key": "a", "window": "w", "exhausts_in_hours": 40.0}]
        assert rd.exhaustion_accelerated(now, prev) == []


# ── variance_jumped ───────────────────────────────────────────────────────
class TestVarianceJumped:
    def test_below_threshold_returns_empty(self):
        assert rd.variance_jumped([_pred(uncertainty=0.05)], threshold=0.30) == []

    def test_above_threshold_fires(self):
        res = rd.variance_jumped([_pred(uncertainty=0.50)], threshold=0.30)
        assert len(res) == 1
        assert res[0]["uncertainty"] == 0.50

    def test_key_filter(self):
        preds = [_pred(key="a", uncertainty=0.50), _pred(key="b", uncertainty=0.05)]
        assert len(rd.variance_jumped(preds, threshold=0.30, key="a")) == 1
        assert len(rd.variance_jumped(preds, threshold=0.30, key="b")) == 0

    def test_none_uncertainty_skipped(self):
        assert rd.variance_jumped([{"uncertainty": None}], threshold=0.30) == []

    def test_empty_preds_returns_empty(self):
        assert rd.variance_jumped([], threshold=0.30) == []


# ── manual_override ───────────────────────────────────────────────────────
class TestManualOverride:
    def test_no_path_returns_false(self):
        assert rd.manual_override(None) is False

    def test_missing_file_returns_false(self):
        assert rd.manual_override("/nonexistent/override") is False

    def test_existing_file_returns_true(self):
        with tempfile.NamedTemporaryFile() as f:
            assert rd.manual_override(f.name) is True


# ── evaluate_triggers ─────────────────────────────────────────────────────
class TestEvaluateTriggers:
    def test_no_triggers_and_no_baseline(self):
        dec = rd.evaluate_triggers([_pred(exhaust_hours=40.0)], None)
        assert dec["fire"] is False
        assert dec["reasons"] == []

    def test_acceleration_with_baseline(self):
        prev_state = {"snapshot": {"routstr|weekly": 42.0}}
        dec = rd.evaluate_triggers(
            [_pred(exhaust_hours=38.0)], prev_state, move_hours=2.0)
        assert dec["fire"] is True
        assert any("exhaustion" in r for r in dec["reasons"])

    def test_variance_jump(self):
        dec = rd.evaluate_triggers([_pred(uncertainty=0.50)], None, variance_threshold=0.30)
        assert dec["fire"] is True
        assert any("variance" in r for r in dec["reasons"])

    def test_manual_override_fires(self):
        dec = rd.evaluate_triggers([], None, override=True)
        assert dec["fire"] is True
        assert any("manual" in r for r in dec["reasons"])

    def test_combined_triggers(self):
        dec = rd.evaluate_triggers(
            [_pred(exhaust_hours=35.0, uncertainty=0.50)],
            {"snapshot": {"routstr|weekly": 40.0}},
            override=True, move_hours=2.0, variance_threshold=0.30)
        assert dec["fire"] is True
        assert len(dec["reasons"]) >= 2


# ── state persistence ─────────────────────────────────────────────────────
class TestStatePersistence:
    def test_load_state_missing_returns_empty(self):
        assert rd.load_state("/nonexistent/state.json") == {}

    def test_load_state_invalid_json_returns_empty(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as f:
            f.write("not json")
            f.flush()
            assert rd.load_state(f.name) == {}

    def test_save_and_load_roundtrip(self):
        state = {"ts": 1234.5, "snapshot": {"a|w": 40.0}, "delisted": True, "last_reason": "test"}
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            assert rd.save_state(path, state) is True
            loaded = rd.load_state(path)
            assert loaded["ts"] == 1234.5
            assert loaded["delisted"] is True
            assert loaded["snapshot"]["a|w"] == 40.0
        finally:
            os.unlink(path)

    def test_update_state_keys(self):
        state = rd.update_state({}, ts=100.0, preds=[_pred(exhaust_hours=40.0)],
                                delisted=True, last_reason="burn_accel")
        assert state["ts"] == 100.0
        assert state["delisted"] is True
        assert state["last_reason"] == "burn_accel"
        assert "routstr|weekly" in state.get("snapshot", {})


# ── SQL helpers ───────────────────────────────────────────────────────────
class TestSqlHelpers:
    def test_pull_listing_sql_updates_enabled_only(self):
        sql = rd.pull_listing_sql()
        assert "models" in sql
        assert "enabled=0" in sql
        assert "upstream_providers" not in sql

    def test_relist_sql_restores_enabled(self):
        sql = rd.relist_sql()
        assert "models" in sql
        assert "enabled=1" in sql


# ── Mock backend ──────────────────────────────────────────────────────────
class _MockBackend:
    def __init__(self):
        self.pull_calls = []
        self.relist_calls = []

    def pull(self, dry_run=False):
        self.pull_calls.append({"dry_run": dry_run})
        return {"ok": True, "action": "pull", "mock": True}

    def relist(self, dry_run=False):
        self.relist_calls.append({"dry_run": dry_run})
        return {"ok": True, "action": "relist", "mock": True}


class TestListingAction:
    def test_pull_delegates_to_backend(self):
        backend = _MockBackend()
        res = rd.pull_listing(backend)
        assert res["ok"] is True
        assert res["action"] == "pull"
        assert len(backend.pull_calls) == 1

    def test_relist_delegates_to_backend(self):
        backend = _MockBackend()
        res = rd.relist(backend)
        assert res["ok"] is True
        assert res["action"] == "relist"
        assert len(backend.relist_calls) == 1


# ── CLI integration ───────────────────────────────────────────────────────
class TestCli:
    def test_status_no_state(self):
        with tempfile.NamedTemporaryFile(suffix=".json") as f:
            rc = rd.main(["--status", "--state", f.name])
        assert rc == 0

    def test_dry_run_pull(self):
        with tempfile.NamedTemporaryFile(suffix=".json") as f:
            rc = rd.main(["--delist", "--dry-run", "--state", f.name,
                          "--ssh-target", "nonexistent", "--container", "test"])
        assert rc == 0

    def test_dry_run_relist(self):
        with tempfile.NamedTemporaryFile(suffix=".json") as f:
            rc = rd.main(["--relist", "--dry-run", "--state", f.name,
                          "--ssh-target", "nonexistent", "--container", "test"])
        assert rc == 0

    def test_manual_override_flag(self):
        with tempfile.NamedTemporaryFile(suffix=".json") as f:
            rc = rd.main(["--manual", "--dry-run", "--state", f.name,
                          "--ssh-target", "nonexistent", "--container", "test"])
        assert rc == 0