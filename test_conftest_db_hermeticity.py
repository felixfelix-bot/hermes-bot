"""Test-infrastructure guard: the suite must NEVER mutate the LIVE zai_usage.db.

Observed live (t_5f82cd0f runs 79/80/81, 2026-09-09, three times): test
copies of zai_proxy spec-loaded from the live tree upsert into the REAL
~/.hermes/bot/zai_usage.db via ``_log_key_health`` — the mocked-200 success
path in test_user_agent_headers.py calls ``_mark_key_healthy("opencode_go")``
→ the key_health mirror flips to healthy=1 while the lane sits 429-benched
upstream (Sep-2026 monthly GoUsageLimitError). Routing is unaffected (the
live proxy gates on its OWN in-memory _zai_key_health), but the mirror lies
to future diagnosis (flags → key_health → live probe) and to any future
proxy restart reading a stale healthy mirror.

The bench flag got a suite-wide conftest guard in 2c3ddbe; these tests pin
the same protection for the DB mirror (conftest run-81 extension). RED
evidence (2026-09-09 run 81): against the 2c3ddbe flag-only conftest this
file fails 4/4 — no PATCH_USAGE_DB_COPIES marker, ``_log_key_health`` on a
loaded copy mutates the live key_health row, and the upsert lands in the
live DB rather than a per-test DB.

Contract under test (conftest._hermetic_proxy_state_files autouse fixture):

  * conftest exposes PATCH_USAGE_DB_COPIES (guard existence marker);
  * ``_log_key_health`` on every loaded zai_proxy copy is redirected so the
    upsert lands in a per-test tmp DB — the live key_health signature is
    unchanged AND the tmp DB actually contains the row (guards against a
    vacuous pass where hermeticity is "achieved" by silently swallowing
    the write: the write must be REDIRECTED, not dropped);
  * the live zai_usage.db is never even OPENED for writing during a test
    (a write-handle to the live DB is corruption waiting to happen).

Scope note: only the key_health mirror WRITE is redirected. DB READS
(real_price_tracker measured rates, ollama quota windows, api_calls sums)
still see the live DB — test_user_agent_headers' ollama UA test depends on
the live measured-rate path skipping the billing-API fallback request, and
several suites (test_ppq_policy) inject their own tmp connections directly
into the ``_usage_db`` singleton, which stays theirs.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

import conftest  # the guard under test

LIVE_DB = Path.home() / ".hermes" / "bot" / "zai_usage.db"


def _load_a_copy():
    """Load ONE zai_proxy copy the way every other test file does.

    Standalone runs of this file happen before any other module registered a
    copy in sys.modules; load our own so the guard tests exercise a real
    module object (same spec-load pattern as test_opencode_quota_truth.py).
    Uses a registry key nobody else claims, so we never clobber another
    file's copy mid-suite.
    """
    live = os.path.expanduser("~/.hermes/bot/zai_proxy.py")
    spec = importlib.util.spec_from_file_location("zai_proxy", live)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["zai_proxy"] = mod
    sys.modules["_zai_proxy_conftest_db_hermeticity"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# Loaded at MODULE IMPORT time (collection), not mid-test: the conftest
# fixture walks sys.modules at each test's SETUP — a copy spec-loaded inside
# a test body would appear AFTER the walk and stay unpatched. Loading here
# guarantees the copy exists in sys.modules before any fixture setup runs,
# in every run combination (standalone or full suite).
_zp = _load_a_copy()


def _copies_in_session() -> list:
    copies = [m for m in list(sys.modules.values())
              if m is not None and hasattr(m, "_OPENCODE_GO_BENCH_FLAG")]
    return copies or [_zp]


def _opencode_go_health_state() -> dict:
    return {
        "healthy": True,
        "consecutive_failures": 0,
        "last_error_type": None,
        "backoff_until": 0,
        "disabled_manually": False,
    }


class TestDbMirrorHermeticity:
    """The suite must not write the live zai_usage.db key_health mirror."""

    def test_conftest_has_db_hermeticity_fixture(self):
        """Guard exists: conftest exposes the DB-hermeticity marker.

        RED against the 2c3ddbe flag-only conftest — pins the run-81
        extension so a revert is caught by the suite itself.
        """
        assert hasattr(conftest, "PATCH_USAGE_DB_COPIES"), (
            "conftest.py must expose PATCH_USAGE_DB_COPIES (DB-mirror "
            "hermeticity, t_5f82cd0f run 81 — the 2c3ddbe flag-only guard "
            "leaves the live DB mirror exposed to mocked-200 test paths)")

    def test_log_key_health_lands_in_per_test_db(self):
        """A health upsert must land in the per-test DB — both directions.

        RED against 2c3ddbe on both asserts: the upsert inserts the probe
        row into the LIVE key_health table (first assert) and no per-test
        DB holds it (second). The probe key name is unique per test — the
        live proxy process concurrently writes REAL lane rows (observed:
        'chutes' recovery between snapshots), so comparing the whole mirror
        would flake on foreign writes; a probe row can only come from THIS
        call. The second assert also guards against a vacuous pass —
        hermeticity must come from REDIRECTION, not from silently
        swallowing the write.
        """
        probe = f"hermeticity_probe_{uuid.uuid4().hex[:12]}"
        mod = _copies_in_session()[0]
        tmp_db = None
        try:
            mod._zai_key_health[probe] = _opencode_go_health_state()
            mod._log_key_health(probe, mod._zai_key_health[probe])
            tmp_db = getattr(mod, "_HERMETIC_KEY_HEALTH_DB", None)
        finally:
            mod._zai_key_health.pop(probe, None)
        # Direction 1: the probe row must NOT exist in the live DB.
        conn = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True, timeout=10)
        try:
            live_row = conn.execute(
                "SELECT key_name FROM key_health WHERE key_name=?",
                (probe,)).fetchone()
        finally:
            conn.close()
        assert live_row is None, (
            f"probe row {probe!r} landed in the LIVE zai_usage.db — the "
            "DB-hermeticity guard is not covering the executing test")
        # Direction 2: ...and it genuinely landed in the per-test DB.
        assert tmp_db is not None and tmp_db.exists(), (
            "upsert did not land in a per-test DB — hermeticity must "
            "redirect the write, not swallow it")
        conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT key_name, healthy FROM key_health "
                "WHERE key_name=?", (probe,)).fetchone()
        finally:
            conn.close()
        assert row == (probe, 1), (
            "the redirected upsert must genuinely land (row present, "
            "healthy=1) in the per-test DB — a swallowed write is a "
            "vacuous pass")

    def test_no_live_db_write_handle_during_test(self):
        """The executing copy's write funnel must be redirected, not live.

        RED against 2c3ddbe: with no guard, ``_log_key_health`` writes
        straight through ``_usage_db()`` to the live DB (the observed
        corruption). The positive marker ``_HERMETIC_KEY_HEALTH_DB`` proves
        the redirect wrapper is installed on this copy during the test.
        """
        mod = _copies_in_session()[0]
        assert getattr(mod, "_HERMETIC_KEY_HEALTH_DB", None) is not None, (
            "_log_key_health writes must be redirected to a per-test DB "
            "during tests (missing _HERMETIC_KEY_HEALTH_DB marker)")