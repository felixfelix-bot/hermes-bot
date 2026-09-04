#!/usr/bin/env python3
"""test_db_health_check.py — snapshot-based SQLite health check.

TDD suite for the db_health_check.py rewrite (consultant spec 2026-09-04).
Covers:
  * classify: live quick_check (small) vs snapshot (large / timeout-fallback)
  * snapshot backup API reads WAL correctly; snap temp always cleaned up
  * quarantine: gateway-ALIVE -> rename + page (never rebuild, keep -wal/-shm)
                gateway-DEAD  -> checkpoint -> rename aside -> rebuild -> verify
                                 -> delete aside+recovered.sql (or restore on fail)
  * compact: session DELETE kept, index added, orphan DELETE gated on deleted>0,
             VACUUM dropped entirely, best-effort checkpoint
  * housekeeping: stale artifact sweep (7-day age rule)

Run: python3 -m pytest tests/test_db_health_check.py -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Bootstrap import path (db_health_check lives in ~/.hermes/bot)
sys.path.insert(0, os.path.expanduser("~/.hermes/bot"))

import db_health_check as dbh  # noqa: E402


# ============================================================================
# Fixtures / helpers
# ============================================================================

def _make_db(path: Path, rows: int = 200) -> None:
    """Create a small valid SQLite DB with a sessions + messages table."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions ("
        "id TEXT PRIMARY KEY, source TEXT, user_id TEXT, "
        "started_at REAL, ended_at REAL, end_reason TEXT)"
    )
    conn.execute(
        "CREATE TABLE messages ("
        "id TEXT PRIMARY KEY, session_id TEXT, content TEXT)"
    )
    conn.execute("CREATE INDEX idx_sessions_started ON sessions(started_at)")
    conn.execute("CREATE INDEX idx_messages_session ON messages(session_id)")
    for i in range(rows):
        ended = float(i * 100) if i % 3 == 0 else None
        conn.execute(
            "INSERT INTO sessions (id, source, user_id, started_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"s{i}", "src", f"u{i}", float(i), ended),
        )
    conn.commit()
    conn.close()


def _corrupt_mid_file(path: Path, offset: int = 4096) -> None:
    """Flip the page-type byte of page 2 (an in-use leaf page) => corruption."""
    data = bytearray(path.read_bytes())
    if offset >= len(data):
        # DB smaller than 2 pages: corrupt the header type instead
        offset = 100
    data[offset] = 0x00 if data[offset] != 0x00 else 0xFF
    path.write_bytes(bytes(data))


def _make_sessions_db(path: Path) -> sqlite3.Connection:
    """Minimal sessions + messages tables for compact tests (no other indexes)."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions ("
        "id TEXT PRIMARY KEY, ended_at REAL, started_at REAL)"
    )
    conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT)")
    conn.commit()
    return conn


def _drop_indexes(path: Path) -> None:
    """Ensure idx_sessions_ended does not pre-exist."""
    conn = sqlite3.connect(str(path))
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_sessions_ended'"
    ):
        conn.execute("DROP INDEX IF EXISTS idx_sessions_ended")
    conn.commit()
    conn.close()


@pytest.fixture
def tmp_db(tmp_path):
    """A fresh small valid DB path."""
    db = tmp_path / "state.db"
    _make_db(db)
    return db


@pytest.fixture
def tmp_profile(tmp_path):
    """A temp profile dir with a valid state.db."""
    db = tmp_path / "state.db"
    _make_db(db)
    return tmp_path


# ============================================================================
# 1. classify: live quick_check (small)
# ============================================================================

def test_live_quick_check_small_ok(tmp_db):
    """A small healthy DB is classified 'ok' via live quick_check, no snapshot."""
    status, detail, method = dbh.classify(tmp_db)
    assert status == "ok", f"{status} {detail}"
    assert method == "live", method
    # no snapshot artifact may be left behind
    assert list(tmp_db.parent.glob("state.db.snap-*")) == []


# ============================================================================
# 2. classify: snapshot path (large / threshold-forced)
# ============================================================================

def test_snapshot_path_ok_and_no_snap_left(monkeypatch, tmp_db):
    """Snapshot path produces 'ok' and always cleans up the snap temp."""
    monkeypatch.setattr(dbh, "SNAPSHOT_THRESHOLD_BYTES", 1)  # force snapshot
    status, detail, method = dbh.classify(tmp_db)
    assert status == "ok", f"{status} {detail}"
    assert method == "snapshot", method
    assert list(tmp_db.parent.glob("state.db.snap-*")) == []


def test_snapshot_detects_corruption(monkeypatch, tmp_db):
    """A corrupt DB is classified 'corrupted' (not ok) via snapshot path."""
    monkeypatch.setattr(dbh, "SNAPSHOT_THRESHOLD_BYTES", 1)
    _corrupt_mid_file(tmp_db)
    status, detail, method = dbh.classify(tmp_db)
    assert status == "corrupted", f"{status} {detail}"
    # never silently 'ok' on a corrupt DB
    assert status != "ok"
    assert list(tmp_db.parent.glob("state.db.snap-*")) == []


# ============================================================================
# 3. classify: timeout -> snapshot fallback (the 8d5f5b7 blind-spot fix)
# ============================================================================

def test_timeout_falls_through_to_snapshot_ok(monkeypatch, tmp_db):
    """Live timeout is NOT 'ok'; snapshot resolves it (snapshot-resolved)."""
    real = dbh.quick_check

    def fake_quick_check(path, timeout_s=dbh.INTEGRITY_TIMEOUT_S):
        if ".snap-" in str(path):
            return ("ok", "ok")
        return ("timeout", f"exceeded {timeout_s}s")

    monkeypatch.setattr(dbh, "quick_check", fake_quick_check)
    status, detail, method = dbh.classify(tmp_db)
    assert status == "ok", f"{status} {detail}"
    assert method == "timeout-snapshot", method
    assert list(tmp_db.parent.glob("state.db.snap-*")) == []


def test_timeout_then_snapshot_confirms_corruption(monkeypatch, tmp_db):
    """Genuinely corrupt large DB times out live, snapshot confirms corrupt."""
    _corrupt_mid_file(tmp_db)

    def fake_quick_check(path, timeout_s=dbh.INTEGRITY_TIMEOUT_S):
        if ".snap-" in str(path):
            return ("corrupted", "database disk image is malformed")
        return ("timeout", f"exceeded {timeout_s}s")

    monkeypatch.setattr(dbh, "quick_check", fake_quick_check)
    status, detail, method = dbh.classify(tmp_db)
    assert status == "corrupted", status  # NOT silently ok
    assert method == "timeout-snapshot", method


def test_snapshot_timeout_is_corruption_evidence(monkeypatch, tmp_db):
    """Timeout on a STATIC snapshot is corruption evidence (never 'ok')."""
    # force snapshot path, and make quick_check on the snapshot time out
    monkeypatch.setattr(dbh, "SNAPSHOT_THRESHOLD_BYTES", 0)

    def fake_quick_check(path, timeout_s=dbh.INTEGRITY_TIMEOUT_S):
        return ("timeout", f"exceeded {timeout_s}s")

    monkeypatch.setattr(dbh, "quick_check", fake_quick_check)
    status, detail, method = dbh.classify(tmp_db)
    assert status == "corrupted", f"{status} {detail}"
    assert list(tmp_db.parent.glob("state.db.snap-*")) == []


# ============================================================================
# 4. quarantine: gateway ALIVE -> rename + page (never rebuild)
# ============================================================================

def test_quarantine_alive_renames_and_pages(monkeypatch, tmp_profile):
    """Alive gateway => rename to .corrupted-*, write alert, NO rebuild."""
    monkeypatch.setattr(dbh, "HOME", tmp_profile.parent)  # alert under tmp
    db = tmp_profile / "state.db"
    # fake live gateway: pid == this process's pid (alive)
    (tmp_profile / "gateway.pid").write_text(
        json.dumps({"pid": os.getpid(), "kind": "hermes-gateway"})
    )

    outcome = dbh.quarantine(db, "testprof", tmp_profile)

    assert outcome == "paged", outcome
    # live db moved aside, not rebuilt in place
    assert not db.exists(), "live db must be renamed away, not rebuilt"
    corrupted = list(tmp_profile.glob("state.db.corrupted-*"))
    assert len(corrupted) == 1, corrupted
    # alert marker written
    alerts = list((tmp_profile.parent / ".hermes" / "logs").glob(
        "CORRUPTION-testprof-*.alert"))
    assert len(alerts) == 1, alerts


def test_quarantine_alive_keeps_wal_shm(monkeypatch, tmp_profile):
    """Alive path must NOT unlink or move -wal/-shm (forensic integrity)."""
    monkeypatch.setattr(dbh, "HOME", tmp_profile.parent)
    db = tmp_profile / "state.db"
    wal = tmp_profile / "state.db-wal"
    shm = tmp_profile / "state.db-shm"
    wal.write_bytes(b"WALDATA")
    shm.write_bytes(b"SHMDATA")
    (tmp_profile / "gateway.pid").write_text(
        json.dumps({"pid": os.getpid(), "kind": "hermes-gateway"})
    )

    dbh.quarantine(db, "testprof", tmp_profile)

    assert wal.exists() and wal.read_bytes() == b"WALDATA"
    assert shm.exists() and shm.read_bytes() == b"SHMDATA"
    assert not db.exists()


# ============================================================================
# 5. quarantine: gateway DEAD -> rebuild
# ============================================================================

def test_quarantine_dead_rebuild_success_deletes_copies(tmp_profile):
    """Dead gateway => rebuild fresh, delete aside-copy + recovered.sql."""
    db = tmp_profile / "state.db"
    # no gateway.pid => dead
    assert not (tmp_profile / "gateway.pid").exists()

    outcome = dbh.quarantine(db, "testprof", tmp_profile)

    assert outcome == "recovered", outcome
    # fresh db rebuilt and healthy
    assert db.exists()
    conn = sqlite3.connect(str(db))
    n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    conn.close()
    assert n > 0, "rebuilt db must retain session rows"
    # aside-copy and recovered.sql removed
    assert list(tmp_profile.glob("state.db.corrupted-*")) == []
    assert list(tmp_profile.glob("*.recovered-*.sql")) == []


def test_quarantine_dead_rebuild_failure_restores(monkeypatch, tmp_profile):
    """Rebuild failure restores original db (no data loss), keeps artifacts."""
    db = tmp_profile / "state.db"
    # force rebuild to fail
    monkeypatch.setattr(dbh, "_run_rebuild", lambda p, s: False)

    outcome = dbh.quarantine(db, "testprof", tmp_profile)

    assert outcome == "failed", outcome
    # original restored (valid, retains rows)
    assert db.exists()
    conn = sqlite3.connect(str(db))
    n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    conn.close()
    assert n > 0, "restored original must retain rows"
    # recovered.sql is kept for forensics (not deleted on failure)
    assert list(tmp_profile.glob("*.recovered-*.sql")) != [], "recovered.sql kept"


# ============================================================================
# 6. compact: VACUUM dropped, index added, orphan DELETE gated
# ============================================================================

class _RecordingConn:
    """Wraps a real sqlite3 connection, recording executed SQL."""

    def __init__(self, real, log):
        self._real = real
        self._log = log

    def execute(self, sql, *args):
        s = sql if isinstance(sql, str) else str(sql)
        self._log.append(s)
        return self._real.execute(sql, *args)

    def commit(self):
        return self._real.commit()

    def close(self):
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _spy_connect(monkeypatch, executed):
    real_connect = dbh.sqlite3.connect

    def spy(*a, **k):
        return _RecordingConn(real_connect(*a, **k), executed)

    monkeypatch.setattr(dbh.sqlite3, "connect", spy)
    return executed


def test_compact_drops_vacuum(monkeypatch, tmp_path):
    """compact_database must never run VACUUM (SQLITE_BUSY storm / lock)."""
    db = tmp_path / "state.db"
    _make_sessions_db(db).close()
    executed = []
    _spy_connect(monkeypatch, executed)

    ok = dbh.compact_database(db, "prof")

    assert ok
    vac = [s for s in executed if s.strip().upper().startswith("VACUUM")]
    assert vac == [], f"VACUUM must be dropped, got {vac}"


def test_compact_creates_index(monkeypatch, tmp_path):
    """compact_database creates idx_sessions_ended."""
    db = tmp_path / "state.db"
    _make_sessions_db(db).close()
    executed = []
    _spy_connect(monkeypatch, executed)

    dbh.compact_database(db, "prof")

    idx = [s for s in executed if "idx_sessions_ended" in s]
    assert idx, "idx_sessions_ended must be created"
    # verify it actually exists in the db now
    conn = sqlite3.connect(str(db))
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")]
    conn.close()
    assert "idx_sessions_ended" in names


def test_compact_orphan_delete_gated_when_none_deleted(monkeypatch, tmp_path):
    """No old sessions deleted => orphan-messages DELETE is skipped."""
    import time
    db = tmp_path / "state.db"
    conn = _make_sessions_db(db)
    # session whose ended_at is recent (within OLD_SESSION_DAYS => NOT deleted)
    conn.execute(
        "INSERT INTO sessions (id, started_at, ended_at) VALUES (?, 0.0, ?)",
        ("srecent", time.time()),
    )
    conn.commit()
    conn.close()
    executed = []
    _spy_connect(monkeypatch, executed)

    dbh.compact_database(db, "prof")

    msgs = [s for s in executed if s.strip().upper().startswith(
        "DELETE FROM MESSAGES")]
    assert msgs == [], f"orphan DELETE must be gated, got {msgs}"


def test_compact_orphan_delete_runs_when_sessions_deleted(monkeypatch, tmp_path):
    """Old session deleted => orphan-messages DELETE runs this night."""
    import time
    db = tmp_path / "state.db"
    conn = _make_sessions_db(db)
    old_cutoff = time.time() - (dbh.OLD_SESSION_DAYS + 10) * 86400
    conn.execute(
        "INSERT INTO sessions (id, started_at, ended_at) VALUES (?, 0.0, ?)",
        ("sold", old_cutoff),
    )
    conn.commit()
    conn.close()
    executed = []
    _spy_connect(monkeypatch, executed)

    dbh.compact_database(db, "prof")

    msgs = [s for s in executed if s.strip().upper().startswith(
        "DELETE FROM MESSAGES")]
    assert msgs, "orphan DELETE must run when old sessions were deleted"


# ============================================================================
# 7. housekeeping: stale artifact sweep
# ============================================================================

def test_sweep_stale_artifacts_age_rule(tmp_path):
    """Delete snap/recovered/corrupted artifacts older than 7d; keep young."""
    now = dbh.time.time()
    old = now - (dbh.STALE_ARTIFACT_DAYS + 2) * 86400
    young = now - 1 * 86400

    artifacts = [
        ("state.db.snap-111", old),
        ("state.db.snap-222", young),
        ("state.recovered-111.sql", old),
        ("state.recovered-222.sql", young),
        ("state.db.corrupted-111", old),
        ("state.db.corrupted-222", young),
    ]
    for name, mtime in artifacts:
        p = tmp_path / name
        p.write_bytes(b"x")
        os.utime(p, (mtime, mtime))

    removed = dbh.sweep_stale_artifacts(root=tmp_path)

    assert removed == 3, removed  # the three 'old' artifacts
    # old gone, young kept
    assert not (tmp_path / "state.db.snap-111").exists()
    assert (tmp_path / "state.db.snap-222").exists()
    assert not (tmp_path / "state.recovered-111.sql").exists()
    assert (tmp_path / "state.recovered-222.sql").exists()
    assert not (tmp_path / "state.db.corrupted-111").exists()
    # young .corrupted (potential forensic image) is NEVER deleted
    assert (tmp_path / "state.db.corrupted-222").exists()


def test_profile_gateway_alive_detection(tmp_path):
    """gateway.pid pointing at a live pid => alive; absent/dead => dead."""
    # live pid (this process)
    (tmp_path / "gateway.pid").write_text(json.dumps({"pid": os.getpid()}))
    assert dbh.profile_gateway_alive(tmp_path) is True
    # no pid file
    (tmp_path / "gateway.pid").unlink()
    assert dbh.profile_gateway_alive(tmp_path) is False
    # dead pid (unlikely to exist)
    (tmp_path / "gateway.pid").write_text(json.dumps({"pid": 99999999}))
    assert dbh.profile_gateway_alive(tmp_path) is False


# ============================================================================
# 8. exit code: corrupted/paged/failed => non-zero
# ============================================================================

def test_results_dict_has_new_counters():
    """results dict carries ok/corrupted/recovered/compacted/failed/paged/unknown."""
    keys = {"ok", "corrupted", "recovered", "compacted", "failed",
            "paged", "unknown"}
    assert keys <= set(dbh.RESULTS_DEFAULTS())
