#!/usr/bin/env python3
"""db_health_check — nightly SQLite integrity check + auto-recovery.

Scans all state.db files under ~/.hermes/profiles/*/, checks integrity,
and repairs corrupted databases using SQLite's .recover command.

Usage:
  python3 db_health_check.py              # check + repair
  python3 db_health_check.py --check-only # check without repairing
  python3 db_health_check.py --profile manager  # check one profile

Schedule: nightly via nightly_sweep.sh (02:00 local)
"""
from __future__ import annotations
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
PROFILES_DIR = HOME / ".hermes" / "profiles"
LARGE_DB_THRESHOLD_MB = 50       # vacuum DBs larger than this
OLD_SESSION_DAYS = 30            # delete sessions older than this
INTEGRITY_TIMEOUT_S = 10         # timeout per integrity check


def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(msg: str):
    print(f"[{_ts()}] {msg}")


def find_state_dbs() -> list[tuple[str, Path]]:
    """Find all state.db files under profiles/."""
    results = []
    if not PROFILES_DIR.exists():
        return results
    for profile_dir in sorted(PROFILES_DIR.iterdir()):
        if not profile_dir.is_dir():
            continue
        db = profile_dir / "state.db"
        if db.exists():
            results.append((profile_dir.name, db))
    # Also check the default profile
    default_db = HOME / ".hermes" / "state.db"
    if default_db.exists():
        results.append(("default", default_db))
    return results


def check_integrity(db_path: Path) -> tuple[str, str]:
    """Check SQLite integrity. Returns (status, detail).
    status: 'ok', 'corrupted', 'timeout', 'error'
    """
    try:
        result = subprocess.run(
            ["sqlite3", f"file:{db_path}?mode=ro", "PRAGMA quick_check;"],
            capture_output=True, text=True, timeout=INTEGRITY_TIMEOUT_S,
        )
        if result.returncode == 0:
            output = result.stdout.strip()
            if output == "ok":
                return "ok", output
            else:
                return "corrupted", output
        else:
            stderr = result.stderr.strip()
            if "malformed" in stderr.lower() or "database disk image" in stderr.lower():
                return "corrupted", stderr
            return "error", stderr
    except subprocess.TimeoutExpired:
        return "timeout", f"integrity check exceeded {INTEGRITY_TIMEOUT_S}s"
    except FileNotFoundError:
        # sqlite3 CLI not available — try Python sqlite3
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = conn.execute("PRAGMA quick_check")
            row = cur.fetchone()
            conn.close()
            output = row[0] if row else "empty"
            if output == "ok":
                return "ok", output
            else:
                return "corrupted", output
        except Exception as e:
            return "error", str(e)
    except Exception as e:
        return "error", str(e)


def recover_database(db_path: Path) -> bool:
    """Attempt to recover a corrupted SQLite database.
    
    Uses sqlite3 .recover to extract readable data, then rebuilds.
    Returns True on success.
    """
    backup = db_path.with_suffix(f".db.corrupted-{int(time.time())}")
    recovered_sql = db_path.with_suffix(f".recovered-{int(time.time())}.sql")
    
    _log(f"  Backing up: {db_path} → {backup.name}")
    shutil.copy2(str(db_path), str(backup))
    
    # Method 1: sqlite3 .recover (best — handles corruption gracefully)
    try:
        with open(recovered_sql, "w") as f:
            result = subprocess.run(
                ["sqlite3", str(backup), ".recover"],
                stdout=f, stderr=subprocess.PIPE, text=True, timeout=300,
            )
        if result.returncode == 0 and recovered_sql.stat().st_size > 0:
            _log(f"  Recovered SQL: {recovered_sql.stat().st_size // 1024}KB")
            # Remove corrupted DB and rebuild from recovered SQL
            db_path.unlink()
            # Also remove WAL/SHM files
            for suffix in ["-wal", "-shm"]:
                wal = Path(str(db_path) + suffix)
                if wal.exists():
                    wal.unlink()
            # Rebuild
            result = subprocess.run(
                ["sqlite3", str(db_path), f".read {recovered_sql}"],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode == 0:
                # Vacuum to compact
                conn = sqlite3.connect(str(db_path))
                conn.execute("VACUUM")
                conn.close()
                recovered_sql.unlink()
                new_size = db_path.stat().st_size / 1048576
                old_size = backup.stat().st_size / 1048576
                _log(f"  RECOVERED: {old_size:.0f}MB → {new_size:.0f}MB")
                return True
            else:
                _log(f"  Rebuild failed: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        _log(f"  .recover timed out (300s) — DB may be too corrupted")
    except FileNotFoundError:
        _log(f"  sqlite3 CLI not available — trying Python recovery")
        return _recover_python(db_path, backup)
    except Exception as e:
        _log(f"  Recovery error: {e}")
    
    # Restore backup if recovery failed
    if not db_path.exists() and backup.exists():
        shutil.copy2(str(backup), str(db_path))
        _log(f"  Restored backup (recovery failed)")
    
    if recovered_sql.exists():
        recovered_sql.unlink()
    return False


def _recover_python(db_path: Path, backup: Path) -> bool:
    """Fallback recovery using Python sqlite3."""
    try:
        # Try to dump what we can
        conn = sqlite3.connect(str(backup))
        recovered_sql = db_path.with_suffix(f".recovered-{int(time.time())}.sql")
        with open(recovered_sql, "w") as f:
            for line in conn.iterdump():
                f.write(line + "\n")
        conn.close()
        
        if recovered_sql.stat().st_size > 0:
            db_path.unlink()
            for suffix in ["-wal", "-shm"]:
                wal = Path(str(db_path) + suffix)
                if wal.exists():
                    wal.unlink()
            new_conn = sqlite3.connect(str(db_path))
            new_conn.executescript(recovered_sql.read_text())
            new_conn.execute("VACUUM")
            new_conn.close()
            recovered_sql.unlink()
            new_size = db_path.stat().st_size / 1048576
            _log(f"  RECOVERED (Python): {new_size:.0f}MB")
            return True
    except Exception as e:
        _log(f"  Python recovery failed: {e}")
    return False


def compact_database(db_path: Path, profile_name: str) -> bool:
    """Compact a large database by removing old sessions and vacuuming."""
    try:
        conn = sqlite3.connect(str(db_path))
        old_size = db_path.stat().st_size / 1048576
        
        # Delete old ended sessions
        cutoff = time.time() - (OLD_SESSION_DAYS * 86400)
        cur = conn.execute(
            "DELETE FROM sessions WHERE ended_at IS NOT NULL AND ended_at < ?",
            (cutoff,)
        )
        deleted = cur.rowcount
        conn.commit()
        
        # Also clean up orphaned messages from deleted sessions
        conn.execute(
            "DELETE FROM messages WHERE session_id NOT IN "
            "(SELECT id FROM sessions)"
        )
        conn.commit()
        
        # Vacuum
        conn.execute("VACUUM")
        conn.close()
        
        new_size = db_path.stat().st_size / 1048576
        _log(f"  COMPACTED {profile_name}: {old_size:.0f}MB → {new_size:.0f}MB "
             f"(deleted {deleted} old sessions)")
        return True
    except Exception as e:
        _log(f"  Compact failed for {profile_name}: {e}")
        return False


def run(check_only: bool = False, profile_filter: str | None = None):
    """Run the health check across all profiles."""
    dbs = find_state_dbs()
    if profile_filter:
        dbs = [(p, d) for p, d in dbs if profile_filter in p]
    
    _log(f"Scanning {len(dbs)} state.db files...")
    
    results = {"ok": 0, "corrupted": 0, "recovered": 0, "compacted": 0, "failed": 0}
    corrupted_profiles = []
    
    for profile_name, db_path in dbs:
        size_mb = db_path.stat().st_size / 1048576
        status, detail = check_integrity(db_path)
        
        if status == "ok":
            results["ok"] += 1
            if size_mb > LARGE_DB_THRESHOLD_MB:
                _log(f"  {profile_name}: OK ({size_mb:.0f}MB) — compacting...")
                if not check_only:
                    if compact_database(db_path, profile_name):
                        results["compacted"] += 1
            elif size_mb > 10:
                _log(f"  {profile_name}: OK ({size_mb:.0f}MB)")
        elif status in ("corrupted", "timeout"):
            results["corrupted"] += 1
            corrupted_profiles.append(profile_name)
            _log(f"  {profile_name}: {status.upper()} ({size_mb:.0f}MB) — {detail[:80]}")
            if not check_only:
                _log(f"  Attempting recovery for {profile_name}...")
                if recover_database(db_path):
                    results["recovered"] += 1
                    results["corrupted"] -= 1
                else:
                    results["failed"] += 1
        else:
            _log(f"  {profile_name}: ERROR — {detail[:80]}")
            results["failed"] += 1
    
    _log(f"\nSummary: {results['ok']} OK, {results['compacted']} compacted, "
         f"{results['recovered']} recovered, {results['corrupted']} corrupted, "
         f"{results['failed']} failed")
    
    if corrupted_profiles and check_only:
        _log(f"Corrupted profiles need repair: {', '.join(corrupted_profiles)}")
    
    return results


if __name__ == "__main__":
    check_only = "--check-only" in sys.argv
    profile = None
    for arg in sys.argv[1:]:
        if arg.startswith("--profile="):
            profile = arg.split("=", 1)[1]
        elif arg == "--profile" and sys.argv.index(arg) + 1 < len(sys.argv):
            profile = sys.argv[sys.argv.index(arg) + 1]
    
    run(check_only=check_only, profile_filter=profile)
