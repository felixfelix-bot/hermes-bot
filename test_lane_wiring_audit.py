"""Tests for lane_wiring_audit.py — the routability/configuration-gap auditor.

Runs against the pure functions with mocked HTTP + DB + subprocess. No live
network or kanban board access. These tests document the four finding classes
and the ok→broken transition-dedup behaviour that the hourly cron relies on.
"""
from __future__ import annotations
import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
import urllib.error
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
                   "disabled_manually": 0,
                   # epoch seconds the lane stays benched until; 0 = not benched
                   "backoff_until": spec.get("backoff_until", 0.0)}
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


# ── QUOTA_MODEL_DRIFT: active-bench discrimination (2026-09-09 oc2 incident) ──
# Live case: oc2 took ONE transient 429 at 09:59:03 (exhausted #1, backoff 2s,
# expired 09:59:05). The proxy's server-truth recovery heuristic cleared the
# breaker at 10:02:53. The audit read the stale mirror at 10:00:39 — 94s after
# the 2s backoff expired — and flagged "stale backoff" for a lane that was
# already back in rotation. The detector must only fire while the breaker is
# STILL ACTIVE (backoff_until in the future); an expired backoff is a
# historical note, not a bench.

def test_no_drift_when_backoff_expired():
    # EXACT oc2 incident shape: mirror says exhausted, probe 200, but the
    # backoff_until is in the PAST — the lane is not benched anymore.
    quota = _quota(("ollama_cloud_2", True))
    health = _health(ollama_cloud_2={"last_error_type": "exhausted",
                                     "backoff_seconds": 2,
                                     "backoff_until": time.time() - 60})
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value="ollama_cloud_4"), \
         mock.patch.object(_MOD, "probe_lane", return_value=200), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True)
    assert not any(c[0][0] == "QUOTA_MODEL_DRIFT"
                   and c[0][1] == "ollama_cloud_2" for c in emit.call_args_list)


def test_drift_fires_when_backoff_still_active():
    # Genuine stale bench: mirror says exhausted, probe 200, backoff_until
    # still in the FUTURE (e.g. opencode_go 14-day backoff, 2026-09-04 class).
    quota = _quota(("ollama_cloud_2", True))
    health = _health(ollama_cloud_2={"last_error_type": "exhausted",
                                     "backoff_seconds": 900,
                                     "backoff_until": time.time() + 600})
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value="ollama_cloud_4"), \
         mock.patch.object(_MOD, "probe_lane", return_value=200), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True)
    assert any(c[0][0] == "QUOTA_MODEL_DRIFT"
               and c[0][1] == "ollama_cloud_2" for c in emit.call_args_list)


def test_drift_resolved_when_no_longer_stale():
    # Latch-open bug: once a QUOTA_MODEL_DRIFT finding opens, nothing ever
    # resolves it — state latches open forever even after the lane recovers.
    # A healthy mirror + probe 200 must resolve the finding.
    quota = _quota(("ollama_cloud_2", True))
    health = _health(ollama_cloud_2={"healthy": 1,
                                     "last_error_type": None})
    state = {"findings": {"QUOTA_MODEL_DRIFT:ollama_cloud_2": {
        "status": "open", "task_id": "t_b77b3aba", "first_seen": 1}}}
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value="ollama_cloud_4"), \
         mock.patch.object(_MOD, "probe_lane", return_value=200), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_resolve_finding") as resolve, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True, state=state)
    resolve.assert_called_once_with("QUOTA_MODEL_DRIFT", "ollama_cloud_2", state,
                                    True)


def test_no_drift_when_backoff_null_legacy_row():
    # Legacy-row edge (kimi cold review 2026-09-09): backoff_until is NULL in
    # rows never benched (or written by older code). float(None or 0) = 0 →
    # NOT benched: must not fire drift, and an open drift finding resolves
    # because the lane provably serves (mirror clean, probe 200, headroom).

    quota = _quota(("ollama_cloud_2", True))
    health = _health(ollama_cloud_2={"last_error_type": "exhausted",
                                     "backoff_seconds": 2,
                                     "backoff_until": None})
    state = {"findings": {"QUOTA_MODEL_DRIFT:ollama_cloud_2": {
        "status": "open", "task_id": "t_x", "first_seen": 1}}}
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value="ollama_cloud_4"), \
         mock.patch.object(_MOD, "probe_lane", return_value=200), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_resolve_finding") as resolve, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True, state=state)
    assert not any(c[0][0] == "QUOTA_MODEL_DRIFT" for c in emit.call_args_list)
    resolve.assert_called_once_with("QUOTA_MODEL_DRIFT", "ollama_cloud_2", state,
                                    True)


# ── QUOTA_MODEL_DRIFT: stale-probe confirmation (2026-09-10 t_cb9de508) ─────
# Live case: the 15:00 probe caught a GENUINE session-limit 429 on
# ollama_cloud. The 5h session window rolled at 16:02 (first 200 dispatch at
# ts 1789038135); 777 successful dispatches followed. The 17:00 audit run then
# compared FRESH /quota headroom (session 29%) against the 2h-stale cached
# 429 (probe cache TTL is 6h — it outlives the 5h window it measures) and
# fired a false QUOTA_MODEL_DRIFT task for a lane that was actively serving.
# The drift arms must CONFIRM a cached-probe contradiction with ONE fresh
# probe before firing (or resolving) — the probe is the only live signal in
# that decision, and everything else (/quota, key_health, spend) is fresh.

def _stale_probe_state(age_s=7200, code=429):
    """State exactly like lane_audit_state.json at 17:00: a cached probe
    taken `age_s` ago whose code contradicts the current fresh /quota."""
    return {"probes": {"ollama_cloud": {"ts": time.time() - age_s,
                                        "code": code, "body": ""}}}


def test_drift_not_fired_when_fresh_probe_recovered_after_cached_429():
    # EXACT t_cb9de508 incident shape: cached 429 (2h old, within the 6h
    # TTL), fresh /quota headroom, live lane. A confirmation re-probe
    # returns 200 → the cached 429 was stale → NO finding, cache refreshed.
    quota = _quota(("ollama_cloud", True))
    health = _health(ollama_cloud={"healthy": 1})
    state = _stale_probe_state()
    env = {"OLLAMA_CLOUD_API_KEY": "k"}
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end",
                           return_value="ollama_cloud"), \
         mock.patch.object(_MOD, "_load_env", return_value=env), \
         mock.patch.object(_MOD, "_probe_chat", return_value=(200, "{}")) as pc, \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True, state=state)
    assert not any(c[0][0] == "QUOTA_MODEL_DRIFT"
                   for c in emit.call_args_list), \
        "stale cached 429 + fresh 200 re-probe must not fire drift"
    # the confirmation probe actually ran, bypassing the cache…
    assert pc.call_count == 1
    assert pc.call_args[0][1] == "k"
    # …and the stale 429 entry was refreshed with the live truth
    assert state["probes"]["ollama_cloud"]["code"] == 200


def test_drift_fires_when_fresh_probe_confirms_cached_429():
    # A cached 429 that a fresh probe CONFIRMS is genuine drift (/quota
    # server-truth stale, upstream still limited) → fires, with the cache
    # refreshed so the evidence is provably live.
    quota = _quota(("ollama_cloud", True))
    health = _health(ollama_cloud={"healthy": 1})
    state = _stale_probe_state()
    env = {"OLLAMA_CLOUD_API_KEY": "k"}
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end",
                           return_value="ollama_cloud"), \
         mock.patch.object(_MOD, "_load_env", return_value=env), \
         mock.patch.object(_MOD, "_probe_chat", return_value=(429, "{}")), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True, state=state)
    assert any(c[0][0] == "QUOTA_MODEL_DRIFT" and c[0][1] == "ollama_cloud"
               for c in emit.call_args_list), \
        "fresh-confirmed 429 + headroom is genuine drift and must fire"
    assert state["probes"]["ollama_cloud"]["code"] == 429


def test_drift_skipped_when_confirmation_probe_impossible():
    # If the confirmation probe cannot run at all (env key vanished since the
    # cached probe was taken), the cached contradiction is unconfirmable —
    # do NOT schedule a fix task on possibly-stale evidence. The lane's other
    # signals (mirror, /quota server truth, spend) are still audited hourly.
    quota = _quota(("ollama_cloud", True))
    health = _health(ollama_cloud={"healthy": 1})
    state = _stale_probe_state()
    env = {}  # key present at probe time, gone now
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end",
                           return_value="ollama_cloud"), \
         mock.patch.object(_MOD, "_load_env", return_value=env), \
         mock.patch.object(_MOD, "_probe_chat") as pc, \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True, state=state)
    assert not any(c[0][0] == "QUOTA_MODEL_DRIFT"
                   for c in emit.call_args_list), \
        "unconfirmable cached evidence must not fire drift"
    pc.assert_not_called()


def test_stale_backoff_arm_also_requires_fresh_confirmation():
    # Mirror says benched (active backoff) + cached 200 → before firing the
    # "stale backoff" drift arm, confirm with a fresh probe. Here the fresh
    # probe returns 429 → the bench is REAL (upstream limited again): do not
    # fire the stale-backoff arm (and, no headroom change, nothing fires —
    # expected-exhaustion state).
    quota = _quota(("ollama_cloud_2", True))
    health = _health(ollama_cloud_2={"last_error_type": "exhausted",
                                     "backoff_seconds": 900,
                                     "backoff_until": time.time() + 600})
    state = {"probes": {"ollama_cloud_2": {"ts": time.time() - 7200,
                                           "code": 200, "body": ""}}}
    env = {"OLLAMA_CLOUD_API_KEY_2": "k"}
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end",
                           return_value="ollama_cloud"), \
         mock.patch.object(_MOD, "_load_env", return_value=env), \
         mock.patch.object(_MOD, "_probe_chat", return_value=(429, "{}")), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True, state=state)
    assert not any(c[0][1] == "ollama_cloud_2" and "stale backoff"
                   in c[0][2] for c in emit.call_args_list), \
        "fresh 429 must not be read as a stale bench"
    # the fresh 429 + headroom IS a genuine drift, though — that arm fires
    assert any(c[0][0] == "QUOTA_MODEL_DRIFT" and c[0][1] == "ollama_cloud_2"
               for c in emit.call_args_list)
    assert state["probes"]["ollama_cloud_2"]["code"] == 429


def test_probe_lane_force_bypasses_cache_and_refreshes():
    # Unit: force=True skips the TTL cache-hit branch and rewrites the
    # cached entry with the fresh result.
    env = {"OLLAMA_CLOUD_API_KEY": "k"}
    state = {"probes": {"ollama_cloud": {"ts": time.time(), "code": 429,
                                         "body": ""}}}
    with mock.patch.object(_MOD, "_probe_chat",
                           return_value=(200, "{}")) as pc:
        assert _MOD.probe_lane("ollama_cloud", env, state, force=True) == 200
    pc.assert_called_once()
    assert state["probes"]["ollama_cloud"]["code"] == 200
    # force=False honours the cache: no probe, cached code returned
    with mock.patch.object(_MOD, "_probe_chat") as pc2:
        assert _MOD.probe_lane("ollama_cloud", env, state) == 200
    pc2.assert_not_called()


def test_no_resolve_when_probe_limited():
    # Negative-resolve (kimi cold review): probe 429 while an open drift
    # finding exists must NOT resolve — the lane is not provably serving.
    quota = _quota(("ollama_cloud_2", True))
    health = _health(ollama_cloud_2={"healthy": 1})
    state = {"findings": {"QUOTA_MODEL_DRIFT:ollama_cloud_2": {
        "status": "open", "task_id": "t_x", "first_seen": 1}}}
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value="ollama_cloud_4"), \
         mock.patch.object(_MOD, "probe_lane", return_value=429), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_resolve_finding") as resolve, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True, state=state)
    resolve.assert_not_called()
    # probe 429 + headroom is itself drift — fires (deduped silently: already open)
    assert any(c[0][0] == "QUOTA_MODEL_DRIFT" for c in emit.call_args_list)


def test_no_resolve_when_no_headroom():
    # Negative-resolve: no quota headroom → the lane cannot serve; an open
    # drift finding must stay open even though the probe returns 200.
    quota = _quota(("ollama_cloud_2", False))
    health = _health(ollama_cloud_2={"healthy": 1})
    state = {"findings": {"QUOTA_MODEL_DRIFT:ollama_cloud_2": {
        "status": "open", "task_id": "t_x", "first_seen": 1}}}
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value="ollama_cloud_4"), \
         mock.patch.object(_MOD, "probe_lane", return_value=200), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_resolve_finding") as resolve, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True, state=state)
    resolve.assert_not_called()
    assert not any(c[0][0] == "QUOTA_MODEL_DRIFT" for c in emit.call_args_list)


def test_no_resolve_without_open_finding():
    # Negative-resolve: nothing latched open → resolve must not be called
    # (recurrence detection re-arms cleanly without spurious resolutions).
    quota = _quota(("ollama_cloud_2", True))
    health = _health(ollama_cloud_2={"healthy": 1})
    state = {"findings": {}}
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value="ollama_cloud_4"), \
         mock.patch.object(_MOD, "probe_lane", return_value=200), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_resolve_finding") as resolve, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True, state=state)
    resolve.assert_not_called()
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


def test_cost_leak_resolved_when_paygo_spend_clears():
    # Latch-open bug: a COST_LEAK finding never resolves after the leak stops.
    # When PAYGO spend drops below threshold, open COST_LEAK findings resolve.
    quota = _quota(("ollama_cloud_4", True))
    health = _health(ollama_cloud_4={"healthy": 1})
    spend = {"ollama_cloud_4": {"tokens": 6_000_000, "cost": 0, "calls": 500}}
    state = {"findings": {"COST_LEAK:ollama_cloud_2": {
        "status": "open", "task_id": "t_efc68b73", "first_seen": 1}}}
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value=spend), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value="ollama_cloud_4"), \
         mock.patch.object(_MOD, "probe_lane", return_value=200), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_resolve_finding") as resolve, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True, state=state)
    assert not any(c[0][0] == "COST_LEAK" for c in emit.call_args_list)
    resolve.assert_called_once_with("COST_LEAK", "ollama_cloud_2", state, True)


def test_resolve_finding_comments_on_open_task():
    state = {"findings": {"COST_LEAK:ollama_cloud_2": {
        "status": "open", "task_id": "t_efc68b73", "first_seen": 1}}}
    with mock.patch.object(_MOD, "_save_state"), \
         mock.patch.object(_MOD, "subprocess") as sp:
        _MOD._resolve_finding("COST_LEAK", "ollama_cloud_2", state,
                              dry_run=False)
    assert state["findings"]["COST_LEAK:ollama_cloud_2"]["status"] == "resolved"
    # comment on the task (kanban comment <id> <text>)
    assert sp.run.called
    assert "t_efc68b73" in sp.run.call_args[0][0]


def test_resolve_finding_dry_run_skips_kanban_comment():
    # Dry-run side-effect leak (t_efc68b73 incident): _resolve_finding fired
    # the `hermes kanban comment` subprocess even under dry_run, while
    # _save_state no-ops. Every pytest run (audit(dry_run=True) with no state=
    # reloads the REAL state file where the finding was still open) posted a
    # live "condition cleared" comment on the board — ~40 duplicates in an
    # hour. Dry-run resolve must be fully inert: no kanban subprocess.
    state = {"findings": {"COST_LEAK:ollama_cloud_2": {
        "status": "open", "task_id": "t_efc68b73", "first_seen": 1}}}
    with mock.patch.object(_MOD, "_save_state") as save, \
         mock.patch.object(_MOD, "subprocess") as sp:
        _MOD._resolve_finding("COST_LEAK", "ollama_cloud_2", state,
                              dry_run=True)
    # The in-memory state transition is still fine (callers inspect it), but
    # NO board write may happen while dry-running.
    assert not sp.run.called
    save.assert_not_called()


def test_audit_resolves_cost_leak_dry_run_without_comment():
    # End-to-end arm: audit(dry_run=True) reaching the COST_LEAK resolve
    # branch must not touch the board. Regression guard for the exact leak:
    # bare audit(dry_run=True) loads the live state file, so an unguarded
    # subprocess posted real comments from inside the test suite.
    quota = _quota(("ollama_cloud_4", True))
    health = _health(ollama_cloud_4={"healthy": 1})
    spend = {"ollama_cloud_4": {"tokens": 6_000_000, "cost": 0, "calls": 500}}
    state = {"findings": {"COST_LEAK:ollama_cloud_2": {
        "status": "open", "task_id": "t_efc68b73", "first_seen": 1}}}
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health", return_value=health), \
         mock.patch.object(_MOD, "read_1h_spend", return_value=spend), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value="ollama_cloud_4"), \
         mock.patch.object(_MOD, "probe_lane", return_value=200), \
         mock.patch.object(_MOD, "_emit_finding"), \
         mock.patch.object(_MOD, "subprocess") as sp, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True, state=state)
    assert not sp.run.called
    assert state["findings"]["COST_LEAK:ollama_cloud_2"]["status"] == "resolved"


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
         mock.patch.object(_MOD, "subprocess"), \
         mock.patch.object(_MOD, "_create_fix_task", return_value="t_deadbeef") as create:
        _MOD._emit_finding("COST_LEAK", "ollama_cloud", "t", "d", state, dry_run=False)
        # second call with same key → status open → should NOT create again
        _MOD._emit_finding("COST_LEAK", "ollama_cloud", "t", "d", state, dry_run=False)
    assert create.call_count == 1


def test_resolve_finding_marks_resolved():
    state = {"findings": {"COST_LEAK:ollama_cloud": {"status": "open", "task_id": "t_x"}}}
    with mock.patch.object(_MOD, "_save_state") as save, \
         mock.patch.object(_MOD, "subprocess") as sp:
        _MOD._resolve_finding("COST_LEAK", "ollama_cloud", state,
                              dry_run=True)
    assert state["findings"]["COST_LEAK:ollama_cloud"]["status"] == "resolved"
    # dry-run resolve is inert: no persist, no board comment
    save.assert_not_called()
    assert not sp.run.called


# ── backoff (novelty-reset binary exponential, 24h cap) ──────────────────────

def test_backoff_interval_ladder():
    assert _MOD._backoff_interval(0) == 3600
    assert _MOD._backoff_interval(1) == 7200
    assert _MOD._backoff_interval(2) == 14400
    assert _MOD._backoff_interval(3) == 28800
    assert _MOD._backoff_interval(4) == 57600


def test_backoff_interval_caps_at_24h():
    assert _MOD._backoff_interval(5) == 86400
    assert _MOD._backoff_interval(100) == 86400


def test_update_backoff_advances_on_clean_same_signature():
    state = {"backoff": {"clean_streak": 1, "last_signature": "sigA"}}
    with mock.patch.object(_MOD, "_save_state"):
        _MOD._update_backoff(state, "sigA", found=0)
    b = state["backoff"]
    assert b["clean_streak"] == 2
    assert b["next_run_at"] > b["last_run_at"]
    assert b["last_signature"] == "sigA"


def test_update_backoff_resets_on_finding():
    state = {"backoff": {"clean_streak": 3, "last_signature": "sigA"}}
    with mock.patch.object(_MOD, "_save_state"):
        _MOD._update_backoff(state, "sigA", found=1)
    assert state["backoff"]["clean_streak"] == 0


def test_update_backoff_resets_on_signature_change():
    state = {"backoff": {"clean_streak": 3, "last_signature": "sigA"}}
    with mock.patch.object(_MOD, "_save_state"):
        _MOD._update_backoff(state, "sigB", found=0)
    assert state["backoff"]["clean_streak"] == 0


def test_update_backoff_holds_on_open_finding():
    state = {"backoff": {"clean_streak": 3, "last_signature": "sigA"},
             "findings": {"QUOTA_MODEL_DRIFT:opencode_go": {"status": "open"}}}
    with mock.patch.object(_MOD, "_save_state"):
        _MOD._update_backoff(state, "sigA", found=0)
    assert state["backoff"]["clean_streak"] == 0


def test_should_run_now():
    assert _MOD._should_run_now({"backoff": {"next_run_at": 0}}) is True
    assert _MOD._should_run_now({"backoff": {"next_run_at": time.time() + 10000}}) is False
    assert _MOD._should_run_now({"backoff": {"next_run_at": time.time() + 10000}}, force=True) is True


def test_state_signature_changes_with_failure_count():
    quota = _quota(("ollama_cloud", True))
    h1 = _health(ollama_cloud={"failure_count": 0, "healthy": 1})
    h2 = _health(ollama_cloud={"failure_count": 5, "last_error_type": "dispatch_fail"})
    assert _MOD._state_signature(quota, h1) != _MOD._state_signature(quota, h2)


def test_state_signature_stable_for_same_state():
    quota = _quota(("ollama_cloud_4", True))
    h = _health(ollama_cloud_4={"healthy": 1})
    assert _MOD._state_signature(quota, h) == _MOD._state_signature(quota, h)


# ── IO helpers (mocked boundaries — no live network/DB/filesystem) ──────────

def test_load_env_parses_quotes_comments_and_export():
    # _load_env walks HOME env files first (real paths, read-only), then
    # BOT_DIR/.env — synthetic keys cannot collide with real ones, and the
    # first-definition-wins rule is asserted via the dup KEY_A line.
    with tempfile.TemporaryDirectory() as td:
        bot = Path(td) / ".hermes" / "bot"
        bot.mkdir(parents=True)
        (bot / ".env").write_text(
            'export KEY_A="quoted value"\n'
            'KEY_B=bare # stripped\n'
            'EMPTY=\n'
            'KEY_A=ignored-dup\n')
        with mock.patch.object(_MOD, "BOT_DIR", bot):
            vals = _MOD._load_env()
        assert vals["KEY_A"] == "quoted value"   # first wins, quotes stripped
        assert vals["KEY_B"] == "bare"          # trailing comment stripped
        assert "EMPTY" not in vals              # empty value dropped


def test_load_state_and_save_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        state_path = Path(td) / "state.json"
        # _DRY_RUN is a module global set by audit() and never restored —
        # earlier dry-run tests leak True into this test. Pin the real-run
        # value explicitly so the roundtrip is deterministic.
        prev_dry = _MOD._DRY_RUN
        _MOD._DRY_RUN = False
        try:
            with mock.patch.object(_MOD, "STATE_PATH", state_path):
                assert _MOD._load_state() == {}      # missing file → empty dict
                st = {"findings": {"X:y": {"status": "open"}}}
                _MOD._save_state(st)
                assert _MOD._load_state() == st
        finally:
            _MOD._DRY_RUN = prev_dry


def test_save_state_dry_run_never_writes():
    with tempfile.TemporaryDirectory() as td:
        state_path = Path(td) / "state.json"
        with mock.patch.object(_MOD, "STATE_PATH", state_path):
            _MOD._DRY_RUN = True
            try:
                _MOD._save_state({"a": 1})
            finally:
                _MOD._DRY_RUN = False
        assert not state_path.exists()           # dry-run writes nothing


def test_fetch_quota_error_returns_empty():
    with mock.patch.object(_MOD.urllib.request, "urlopen",
                           side_effect=RuntimeError("boom")):
        assert _MOD.fetch_quota() == {}


def test_read_1h_spend_groups_by_key():
    class _FakeExec:
        def __init__(self, results):
            self.results = results
        def __call__(self, *a, **k):
            return iter(self.results)
        def close(self):
            pass

    conn = mock.MagicMock()
    conn.execute = _FakeExec([("deepseek", 5, 1.5, 2), (None, 3, 0.0, 1)])
    with mock.patch.object(_MOD, "_db_conn", return_value=conn):
        out = _MOD.read_1h_spend()
    assert out["deepseek"] == {"tokens": 5, "cost": 1.5, "calls": 2}
    assert out[""]["tokens"] == 3                 # None key coerced to ""


def test_read_1h_spend_db_error_returns_empty():
    with mock.patch.object(_MOD, "_db_conn",
                          side_effect=sqlite3.OperationalError("locked")):
        assert _MOD.read_1h_spend() == {}


def test_probe_chat_http_error_and_timeout():
    class _Err(urllib.error.HTTPError):
        def __init__(self, code):
            super().__init__("http://x", code, "msg", None, None)
            self._body = b"payload"
        def read(self, n=-1):
            return self._body

    with mock.patch.object(_MOD.urllib.request, "urlopen",
                           side_effect=_Err(429)):
        code, body = _MOD._probe_chat("https://x/v1", "k")
    assert (code, body) == (429, "payload")
    with mock.patch.object(_MOD.urllib.request, "urlopen",
                           side_effect=OSError("timeout")):
        assert _MOD._probe_chat("https://x/v1", "k") == (0, "")


def test_probe_lane_skips_missing_key_and_uncached_lanes():
    env = {}
    state = {"probes": {}}
    assert _MOD.probe_lane("deepseek", env, state) is None   # not in PROBE_KEYS
    assert _MOD.probe_lane("ollama_cloud", env, state) is None  # empty key
    # cached probe within 6h window returns cached code without re-probing
    state = {"probes": {"ollama_cloud": {"ts": time.time(), "code": 200}}}
    with mock.patch.object(_MOD, "_probe_chat") as pc:
        assert _MOD.probe_lane("ollama_cloud", {"OLLAMA_CLOUD_API_KEY": "k"},
                              state) == 200
    pc.assert_not_called()


def test_probe_end_to_end_returns_provider_header():
    class _Resp:
        headers = {"X-Provider": "ollama_cloud_4"}
        def read(self):
            return b"{}"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    with mock.patch.object(_MOD.urllib.request, "urlopen",
                           return_value=_Resp()):
        assert _MOD.probe_end_to_end() == "ollama_cloud_4"
    # HTTP error still extracts the header (e2e result is provider-or-None)
    class _HErr(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("http://x", 503, "msg", None, None)
            # instance attr — HTTPError.__init__ overwrites any class attr
            self.headers = {"X-Provider": "deepseek"}
        def read(self, n=-1):
            return b""
    with mock.patch.object(_MOD.urllib.request, "urlopen", side_effect=_HErr()):
        assert _MOD.probe_end_to_end() == "deepseek"
    with mock.patch.object(_MOD.urllib.request, "urlopen",
                           side_effect=OSError("net")):
        assert _MOD.probe_end_to_end() is None


def test_emit_finding_recurrence_comments_on_open_task():
    # Recurrence path (prev status open): comment on the existing task,
    # no second anomaly/task creation.
    state = {"findings": {"COST_LEAK:ollama_cloud_2": {
        "status": "open", "task_id": "t_efc68b73", "first_seen": 1}}}
    with mock.patch.object(_MOD, "_save_state"), \
         mock.patch.object(_MOD, "subprocess") as sp, \
         mock.patch.object(_MOD, "_create_fix_task") as create:
        _MOD._emit_finding("COST_LEAK", "ollama_cloud_2", "t", "d", state,
                           dry_run=False)
    create.assert_not_called()
    assert sp.run.called
    assert "still broken" in sp.run.call_args[0][0][6]


def test_sustained_dispatch_fail_resolves_with_dry_run_threaded():
    # Mirror of the QUOTA_MODEL_DRIFT/COST_LEAK resolve-arg tests: if someone
    # drops the dry_run arg at the SUSTAINED_DISPATCH_FAIL call site, the
    # resolve mock sees the old 3-arg signature and this goes red.
    quota = _quota(("ollama_cloud_2", True))
    open_finding = {"SUSTAINED_DISPATCH_FAIL:ollama_cloud_2": {
        "status": "open", "task_id": "t_efc68b73", "first_seen": 1}}
    # failure_count NOT climbing (fc_prev seeded from state) → emit's climbing
    # guard fails → falls to the resolve elif, which resolves the open finding
    # with dry_run threaded (True) — this is the branch whose args no test
    # previously asserted.
    state = {"findings": dict(open_finding), "failure_counts": {"ollama_cloud_2": 9}}
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health",
                           return_value=_health(ollama_cloud_2={
                               "failure_count": 9,
                               "last_error_type": "dispatch_fail"})), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value=None), \
         mock.patch.object(_MOD, "probe_lane", return_value=200), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_resolve_finding") as resolve, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True, state=state)
    emit.assert_not_called()
    resolve.assert_called_once_with("SUSTAINED_DISPATCH_FAIL", "ollama_cloud_2",
                                    state, True)
    # failure_count >= threshold AND climbing (fc_prev < fc_now) → emit fires,
    # resolve does not (the if-branch wins the elif)
    state2 = {"findings": dict(open_finding),
              "failure_counts": {"ollama_cloud_2": 29}}
    with mock.patch.object(_MOD, "fetch_quota", return_value=quota), \
         mock.patch.object(_MOD, "read_key_health",
                           return_value=_health(ollama_cloud_2={
                               "failure_count": 30,
                               "last_error_type": "dispatch_fail"})), \
         mock.patch.object(_MOD, "read_1h_spend", return_value={}), \
         mock.patch.object(_MOD, "disabled_flags", return_value=set()), \
         mock.patch.object(_MOD, "probe_end_to_end", return_value=None), \
         mock.patch.object(_MOD, "probe_lane", return_value=200), \
         mock.patch.object(_MOD, "_emit_finding") as emit, \
         mock.patch.object(_MOD, "_resolve_finding") as resolve, \
         mock.patch.object(_MOD, "_save_state"):
        _MOD.audit(dry_run=True, state=state2)
    emit.assert_called_once()
    resolve.assert_not_called()


def test_emit_finding_dry_run_no_persist_no_task():
    state = {"findings": {}}
    with mock.patch.object(_MOD, "_save_state") as save, \
         mock.patch.object(_MOD, "_create_fix_task") as create:
        _MOD._emit_finding("COST_LEAK", "ollama_cloud_2", "t", "d", state,
                           dry_run=True)
    create.assert_not_called()
    save.assert_not_called()
    # in-memory record still opened (dry-run callers inspect state)
    assert state["findings"]["COST_LEAK:ollama_cloud_2"]["status"] == "open"


def test_should_run_now_backoff_and_force():
    assert _MOD._should_run_now({"backoff": {"next_run_at": 0}}) is True
    assert _MOD._should_run_now(
        {"backoff": {"next_run_at": time.time() + 10000}}) is False
    assert _MOD._should_run_now(
        {"backoff": {"next_run_at": time.time() + 10000}}, force=True) is True


# ── more IO helpers (read_key_health / disabled_flags / _create_fix_task) ────

def test_load_state_corrupt_file_returns_empty():
    with tempfile.TemporaryDirectory() as td:
        state_path = Path(td) / "state.json"
        state_path.write_text("{not json")
        with mock.patch.object(_MOD, "STATE_PATH", state_path):
            assert _MOD._load_state() == {}


def test_read_key_health_maps_rows_and_swallows_db_errors():
    class _FakeExec:
        def __init__(self, results):
            self.results = results
        def __call__(self, *a, **k):
            return iter(self.results)
        def close(self):
            pass

    conn = mock.MagicMock()
    conn.execute = _FakeExec([
        ("ollama_cloud_2", 1, 3, None, 0, 0, 1788950000.0),
        ("neuralwatt", 0, 817, "dispatch_fail", 30, 1, None),
    ])
    with mock.patch.object(_MOD, "_db_conn", return_value=conn):
        out = _MOD.read_key_health()
    assert out["ollama_cloud_2"] == {
        "healthy": 1, "failure_count": 3, "last_error_type": None,
        "backoff_seconds": 0, "disabled_manually": 0,
        "backoff_until": 1788950000.0}
    assert out["neuralwatt"]["healthy"] == 0
    assert out["neuralwatt"]["backoff_until"] is None
    # DB failure → empty dict, never raises
    with mock.patch.object(_MOD, "_db_conn",
                           side_effect=sqlite3.OperationalError("busy")):
        assert _MOD.read_key_health() == {}


def test_disabled_flags_reads_key_disabled_marker_files():
    with tempfile.TemporaryDirectory() as td:
        bot = Path(td)
        (bot / ".key_disabled_ollama_cloud_2").touch()
        (bot / ".key_disabled_telnyx").touch()
        with mock.patch.object(_MOD, "BOT_DIR", bot):
            flags = _MOD.disabled_flags()
        assert "ollama_cloud_2" in flags and "telnyx" in flags
        assert "ollama_cloud" not in flags      # prefix must match exactly


def test_create_fix_task_parses_json_and_unblocks():
    # Successful creation: hermes kanban create returns {"id": "t_new"};
    # the helper then unblocks the task. Returns the task id.
    r_ok = mock.MagicMock(returncode=0, stdout='{"id": "t_new"}')
    with mock.patch.object(_MOD.subprocess, "run",
                           side_effect=[r_ok, mock.MagicMock()]) as run:
        tid = _MOD._create_fix_task("COST_LEAK", "ollama_cloud_2",
                                    "title", "detail")
    assert tid == "t_new"
    argv0 = run.call_args_list[0][0][0]
    assert argv0[:3] == ["hermes", "kanban", "--board"]
    assert argv0[3] == "llm-routing"
    # idempotency key is week-scoped per category+lane
    assert any("lane-audit-COST_LEAK-ollama_cloud_2" in str(a) for a in argv0)
    # second call = the unblock
    argv1 = run.call_args_list[1][0][0]
    assert "unblock" in argv1 and "t_new" in argv1


def test_create_fix_task_failure_returns_none():
    r_bad = mock.MagicMock(returncode=1, stdout="")
    with mock.patch.object(_MOD.subprocess, "run", return_value=r_bad):
        assert _MOD._create_fix_task("COST_LEAK", "x", "t", "d") is None
    # subprocess exception → None, never raises
    with mock.patch.object(_MOD.subprocess, "run",
                           side_effect=OSError("no hermes")):
        assert _MOD._create_fix_task("COST_LEAK", "x", "t", "d") is None
    # returncode 0 but non-JSON stdout → None
    r_weird = mock.MagicMock(returncode=0, stdout="not json")
    with mock.patch.object(_MOD.subprocess, "run", return_value=r_weird):
        assert _MOD._create_fix_task("COST_LEAK", "x", "t", "d") is None


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
