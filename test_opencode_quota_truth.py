"""Tests for the opencode_go /quota truth entry — QUOTA_MODEL_DRIFT fix (t_5f82cd0f).

The /quota snapshot previously served a HARDCODED
{"used_pct": 0.0, "remaining": inf, "regime": "included"} entry for
opencode_go ("Per-token providers — effectively unlimited") while the circuit
breaker had the lane benched for a real 429 GoUsageLimitError ("Monthly usage
limit reached. Resets in 15 days"). The lane-wiring-audit compares /quota
against a live probe and (correctly) flagged QUOTA_MODEL_DRIFT.

These tests pin the fixed contract for `_opencode_go_quota_entry()`:

  * ACTIVE exhausted/dead bench (backoff_until in the future) → the entry
    reports regime="exhausted", used_pct=100, probe_exhausted=True and the
    bench expiry as resets_at (epoch seconds) — the t_30dde4c7 probe-truth
    marker pattern applied to opencode_go.
  * EXPIRED bench (backoff_until in the past) → back to "included" with
    remaining Infinity (the breaker is no longer active; the routing gate
    honours retry_after, so an expired bench must not show as exhausted).
  * No breaker entry at all (fresh process / healthy lane) → legacy shape
    preserved, optionally enriched with the real allowance when known.
  * Allowance enrichment: when `_opencode_go_allowance` knows a remaining_usd,
    used_pct reflects the depletion fraction of the $10/mo initial allowance
    and remaining carries the USD figure (total = initial allowance).
  * Fail-open: any internal error yields the legacy dict — routing never
    breaks because of this builder.

Import strategy: load the LIVE ~/.hermes/bot/zai_proxy.py by explicit path
(same pattern as test_flat_router.py) — the module runs no servers on import
and all state under test is plain module-level dicts we set directly.
"""
from __future__ import annotations
import importlib.util
import math
import os
import sys
import time
from pathlib import Path

BOT = os.path.expanduser("~/.hermes/bot")
_SPEC = importlib.util.spec_from_file_location("zai_proxy", os.path.join(BOT, "zai_proxy.py"))
zp = importlib.util.module_from_spec(_SPEC)
sys.modules["zai_proxy"] = zp
_SPEC.loader.exec_module(zp)

LEGACY_SHAPE = {"used_pct": 0.0, "remaining": float("inf"), "total": float("inf"),
                "regime": "included"}


def _set_breaker(healthy: bool, err: str | None, backoff_until: float) -> None:
    zp._zai_key_health["opencode_go"] = {
        "healthy": healthy,
        "consecutive_failures": 2,
        "last_error_type": err,
        "retry_after": backoff_until,
        "backoff_until": backoff_until,
        "backoff_seconds": int(backoff_until - time.time()),
        "last_failure_ts": time.time() - 10,
        "disabled_manually": False,
    }


def _clear_state() -> None:
    zp._zai_key_health.pop("opencode_go", None)
    zp._opencode_go_allowance["remaining_usd"] = None
    zp._opencode_go_allowance["ts"] = 0.0


def test_active_exhausted_bench_reports_exhausted():
    """RED→GREEN: an ACTIVE exhausted bench must surface as exhausted in /quota.

    Live shape (2026-09-09): upstream 429 GoUsageLimitError, backoff_until
    2026-09-23 (14-day cap) — /quota used to say included/Infinity the whole
    time (lane-wiring-audit QUOTA_MODEL_DRIFT).
    """
    _clear_state()
    _set_breaker(False, "exhausted", time.time() + 14 * 86400)
    entry = zp._opencode_go_quota_entry()
    assert entry["regime"] == "exhausted", f"regime={entry.get('regime')!r}"
    assert entry["used_pct"] == 100.0
    assert entry["probe_exhausted"] is True
    assert entry["remaining"] == 0.0
    # resets_at must carry the bench expiry so operators see WHEN it clears
    assert entry["resets_at"] == zp._zai_key_health["opencode_go"]["backoff_until"]


def test_active_dead_bench_also_reports_exhausted():
    """A dead (401/403) bench is equally unusable — must not claim headroom."""
    _clear_state()
    _set_breaker(False, "dead", time.time() + 3600)
    entry = zp._opencode_go_quota_entry()
    assert entry["regime"] == "exhausted"
    assert entry["used_pct"] == 100.0
    assert entry["probe_exhausted"] is True


def test_expired_bench_falls_back_to_included():
    """EXPIRED bench (backoff_until in the past) = lane eligible again.

    The routing gate (_is_key_healthy) honours retry_after; once it expires
    the lane is eligible. /quota must not keep claiming exhausted forever
    (the sticky-mirror false-positive class, 2026-09-09 oc2 incident).
    """
    _clear_state()
    _set_breaker(False, "exhausted", time.time() - 60)
    entry = zp._opencode_go_quota_entry()
    assert entry["regime"] == "included"
    assert entry["used_pct"] == 0.0
    assert entry["remaining"] == float("inf")
    assert entry.get("probe_exhausted") is False


def test_healthy_lane_preserves_legacy_shape():
    """No active bench → legacy included/Infinity shape preserved (backwards
    compat for every consumer that treats missing fields as full headroom)."""
    _clear_state()
    entry = zp._opencode_go_quota_entry()
    assert entry["regime"] == "included"
    assert entry["used_pct"] == 0.0
    assert entry["remaining"] == float("inf")
    assert entry["total"] == float("inf")
    assert entry.get("probe_exhausted") is False


def test_allowance_enrichment_reports_depletion():
    """When the 200-path fed _opencode_go_allowance, used_pct must reflect it."""
    _clear_state()
    zp._opencode_go_allowance["remaining_usd"] = 2.5
    zp._opencode_go_allowance["ts"] = time.time()
    entry = zp._opencode_go_quota_entry()
    assert entry["regime"] == "included"
    assert math.isclose(entry["used_pct"], 75.0, rel_tol=1e-6), entry["used_pct"]
    assert math.isclose(entry["remaining"], 2.5, rel_tol=1e-9)
    assert math.isclose(entry["total"], 10.0, rel_tol=1e-9)


def test_allowance_zero_reads_as_exhausted():
    """Allowance at $0 = depleted (pay-per-use threshold) — report exhausted so
    scarcity steering and the audit see the same truth the API returns."""
    _clear_state()
    zp._opencode_go_allowance["remaining_usd"] = 0.0
    zp._opencode_go_allowance["ts"] = time.time()
    entry = zp._opencode_go_quota_entry()
    assert entry["regime"] == "exhausted"
    assert entry["used_pct"] == 100.0
    assert entry["probe_exhausted"] is True


def test_breaker_beats_allowance():
    """Precedence: an active bench wins over a stale optimistic allowance."""
    _clear_state()
    zp._opencode_go_allowance["remaining_usd"] = 9.0  # stale pre-429 sample
    zp._opencode_go_allowance["ts"] = time.time() - 7200
    _set_breaker(False, "exhausted", time.time() + 86400)
    entry = zp._opencode_go_quota_entry()
    assert entry["regime"] == "exhausted"
    assert entry["used_pct"] == 100.0


def test_snapshot_quota_uses_builder():
    """_snapshot_quota() must route opencode_go through the truth builder —
    the integration point the audit reads via GET /quota."""
    _clear_state()
    _set_breaker(False, "exhausted", time.time() + 1209600)
    snap = zp._snapshot_quota()
    assert snap["opencode_go"]["regime"] == "exhausted"
    assert snap["opencode_go"]["probe_exhausted"] is True
    assert snap["opencode_go"]["used_pct"] == 100.0
    # sibling lanes untouched by this change
    assert snap["neuralwatt"]["used_pct"] in (0.0,) or isinstance(snap["neuralwatt"], dict)


def test_builder_fails_open():
    """Any internal error must yield the legacy dict — never break /quota."""
    _clear_state()
    original = zp._opencode_go_allowance
    zp._opencode_go_allowance = None  # force the internal error path
    try:
        entry = zp._opencode_go_quota_entry()
        assert entry["regime"] == "included"
        assert entry["remaining"] == float("inf")
    finally:
        zp._opencode_go_allowance = original


# ── persisted bench flag (restart survival) ─────────────────────────────────
# The routing gate is the in-memory _zai_key_health dict (write-only-mirror
# architecture: restart = all keys healthy). Without persistence, /quota would
# flip back to included/Infinity after every proxy restart until a live
# dispatch re-arms the 429 bench — the lane-wiring-audit probes upstream
# DIRECTLY, so it would see /quota=available vs probe=429 and re-fire drift.
# Same fix shape as the ollama paywall _ollama_exhausted_until flag.

def _flag_path():
    return Path.home() / ".hermes" / "bot" / ".opencode_go_exhausted_until"


def _clear_flag():
    _flag_path().unlink(missing_ok=True)


def test_persisted_bench_flag_survives_missing_memory():
    """RED: a fresh persisted flag must keep /quota truthful even when the
    in-memory breaker is empty (post-restart shape)."""
    _clear_state()
    _clear_flag()
    zp._opencode_go_persist_bench(15 * 86400)
    try:
        entry = zp._opencode_go_quota_entry()
        assert entry["regime"] == "exhausted", entry
        assert entry["used_pct"] == 100.0
        assert entry["probe_exhausted"] is True
        assert entry["resets_at"] == float(_flag_path().read_text().strip())
    finally:
        _clear_flag()


def test_expired_persisted_flag_reads_included():
    """A stale flag (expiry in the past) is historical — no longer a bench."""
    _clear_state()
    _clear_flag()
    _flag_path().write_text(str(time.time() - 3600))
    try:
        entry = zp._opencode_go_quota_entry()
        assert entry["regime"] == "included"
        assert entry["remaining"] == float("inf")
    finally:
        _clear_flag()


def test_persist_none_writes_nothing():
    """A 429 WITHOUT a reset hint (transient throttle) must not persist a
    long bench — the in-memory short backoff governs alone."""
    _clear_state()
    _clear_flag()
    zp._opencode_go_persist_bench(None)
    assert not _flag_path().exists()
    entry = zp._opencode_go_quota_entry()
    assert entry["regime"] == "included"


def test_clear_persisted_bench_on_recovery():
    """The 200-success path clears the flag — a probe-confirmed recovery
    immediately restores headroom in /quota."""
    _clear_state()
    _clear_flag()
    zp._opencode_go_persist_bench(15 * 86400)
    zp._opencode_go_clear_persisted_bench()
    assert not _flag_path().exists()
    entry = zp._opencode_go_quota_entry()
    assert entry["regime"] == "included"


def test_memory_bench_extends_flag_window():
    """Precedence when both exist: the LONGER window wins (max), so a fresh
    in-memory bench never shortens a persisted upstream window and vice versa."""
    _clear_state()
    _clear_flag()
    zp._opencode_go_persist_bench(2 * 86400)
    try:
        _set_breaker(False, "exhausted", time.time() + 14 * 86400)
        entry = zp._opencode_go_quota_entry()
        assert entry["regime"] == "exhausted"
        # in-memory 14d > flag 2d → resets_at must follow the in-memory bench
        assert entry["resets_at"] == zp._zai_key_health["opencode_go"]["backoff_until"]
    finally:
        _clear_flag()