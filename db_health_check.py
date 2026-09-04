#!/usr/bin/env python3
"""db_health_check — nightly SQLite integrity check + safe auto-recovery.

Snapshot-based health check (consultant rewrite, 2026-09-04).

The pre-rewrite scanner opened every state.db *live* through the sqlite3 CLI
with a 10s wall-clock timeout, then treated a timeout on large/live WAL DBs as
corruption, full-copied the live DB (dropping WAL), unlinked the *live* file
under the gateway writer, and never deleted the copies — a ~21 GiB/night leak
plus divergent/ghost-inode rebuilds. This rewrite makes classification
*snapshot-based* so a live database is never the thing we poke at under load,
and it only ever quarantines a database after a *confirmed* corruption verdict.

Design (see consultant spec):
  1. classify: <100 MB -> live quick_check (10s). A timeout is now 'unknown',
     NOT ok — it falls through to the snapshot path. >=100 MB (or any
     'unknown') -> SQLite backup-API snapshot to state.db.snap-<epoch>, run
     quick_check *offline* on the snapshot with a 600s hang-guard. The backup
     API reads WAL correctly (no checkpoint needed); a timeout on a static
     snapshot is corruption evidence, not a "still running" guess.
  2. compact (only when 'ok'): session DELETE kept; add
     idx_sessions_ended(ended_at) once per DB; orphan-message DELETE only when
     that night's session DELETE removed >0 rows; VACUUM dropped entirely;
     optional best-effort wal_checkpoint(TRUNCATE) in a retry loop.
  3. quarantine (only snapshot-confirmed 'corrupted'): gateway ALIVE =>
     atomic rename to state.db.corrupted-<epoch>, keep -wal/-shm, NO rebuild,
     urgent alert + loud log ('paged'). gateway DEAD => best-effort
     checkpoint(TRUNCATE) => rename aside => rebuild from .recover => VACUUM
     the (unowned) fresh DB => verify => delete aside + recovered.sql; on
     failure restore via rename-back and keep artifacts.
  4. housekeeping: at start, sweep state.db.snap-* / *.recovered-*.sql /
     state.db.corrupted-* older than 7 days (never a young .corrupted — may be
     a forensic/restore image).
  5. Preserves 8d5f5b7 semantics: the snapshot path never copies the live DB,
     so a false-positive can never produce a .corrupted copy.

Usage:
  python3 db_health_check.py              # check + (safe) recovery
  python3 db_health_check.py --check-only # check without any mutation
  python3 db_health_check.py --profile manager  # one profile (substr match)

Schedule: nightly via nightly_sweep.sh (02:00 local)
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
PROFILES_DIR = HOME / ".hermes" / "profiles"

SNAPSHOT_THRESHOLD_BYTES = 100 * 1024 * 1024  # >= this -> snapshot path
LARGE_DB_THRESHOLD_MB = 50                   # compact DBs larger than this
OLD_SESSION_DAYS = 30                        # delete sessions older than this
INTEGRITY_TIMEOUT_S = 10                     # live quick_check timeout
SNAPSHOT_TIMEOUT_S = 600                     # snapshot quick_check hang-guard
STALE_ARTIFACT_DAYS = 7                      # housekeeping sweep age


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(msg: str) -> None:
    print(f"[{_ts()}] {msg}")


def RESULTS_DEFAULTS() -> dict:
    """Fresh result counters. Kept as a callable so tests don't share state."""
    return {"ok": 0, "corrupted": 0, "recovered": 0, "compacted": 0,
            "failed": 0, "paged": 0, "unknown": 0}


def find_state_dbs() -> list[tuple[str, Path]]:
    """Find all state.db files under profiles/ (plus the default profile)."""
    results = []
    if PROFILES_DIR.exists():
        for profile_dir in sorted(PROFILES_DIR.iterdir()):
            if not profile_dir.is_dir():
                continue
            db = profile_dir / "state.db"
            if db.exists():
                results.append((profile_dir.name, db))
    default_db = HOME / ".hermes" / "state.db"
    if default_db.exists():
        results.append(("default", default_db))
    return results


# ---------------------------------------------------------------------------
# quick_check — the single sqlite3 CLI integrity primitive (live + snapshot)
# ---------------------------------------------------------------------------

def quick_check(path: Path, timeout_s: int = INTEGRITY_TIMEOUT_S) -> tuple[str, str]:
    """Run `sqlite3 <path> PRAGMA quick_check`. Returns (status, detail).

    status in {'ok', 'corrupted', 'timeout', 'error'}.
    """
    try:
        result = subprocess.run(
            ["sqlite3", f"file:{path}?mode=ro", "PRAGMA quick_check;"],
            capture_output=True, text=True, timeout=timeout_s,
        )
        if result.returncode == 0:
            output = result.stdout.strip()
            if output == "ok":
                return "ok", output
            return "corrupted", output
        stderr = result.stderr.strip()
        low = stderr.lower()
        if "malformed" in low or "database disk image" in low \
                or "file is not a database" in low:
            return "corrupted", stderr
        return "error", stderr
    except subprocess.TimeoutExpired:
        return "timeout", f"quick_check exceeded {timeout_s}s"
    except FileNotFoundError:
        return "error", "sqlite3 CLI not available"
    except Exception as e:
        return "error", str(e)


# ---------------------------------------------------------------------------
# snapshot path — backup-API copy, then offline quick_check
# ---------------------------------------------------------------------------

def snapshot_check(db_path: Path) -> tuple[str, str]:
    """Backup db_path to state.db.snap-<epoch> and quick_check it offline.

    Uses the SQLite backup API (reads WAL correctly, no checkpoint needed) and
    *always* deletes the snap temp in a finally block. A timeout here is
    corruption evidence — the snapshot is static, so "hanging" means the image
    itself cannot be read.
    """
    epoch = int(time.time())
    snap = Path(str(db_path) + f".snap-{epoch}")
    try:
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(str(snap))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        status, detail = quick_check(snap, timeout_s=SNAPSHOT_TIMEOUT_S)
        if status == "timeout":
            # static snapshot cannot legitimately "still be running"
            return "corrupted", detail
        return status, detail
    except sqlite3.DatabaseError as e:
        # a corrupt source can make backup() itself fail
        return "corrupted", str(e)
    except Exception as e:
        return "error", str(e)
    finally:
        try:
            if snap.exists():
                snap.unlink()
        except OSError:
            pass


def classify(db_path: Path) -> tuple[str, str, str]:
    """Classify db_path. Returns (status, detail, method).

    method in {'live', 'snapshot', 'timeout-snapshot'}.
    """
    try:
        size = db_path.stat().st_size
    except OSError as e:
        return "error", str(e), "live"

    if size < SNAPSHOT_THRESHOLD_BYTES:
        status, detail = quick_check(db_path)
        if status == "timeout":
            # NOT ok — the 8d5f5b7 blind spot. Fall through to snapshot.
            status, detail = snapshot_check(db_path)
            return status, detail, "timeout-snapshot"
        return status, detail, "live"

    # large DB — never poke it live; always snapshot
    status, detail = snapshot_check(db_path)
    return status, detail, "snapshot"


# ---------------------------------------------------------------------------
# gateway liveness
# ---------------------------------------------------------------------------

def profile_gateway_alive(profile_dir: Path) -> bool:
    """True if <profile>/gateway.pid names a process that is currently alive."""
    pid_file = profile_dir / "gateway.pid"
    if not pid_file.exists():
        return False
    try:
        raw = pid_file.read_text().strip()
        if not raw:
            return False
        try:
            data = json.loads(raw)
            pid = int(data["pid"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pid = int(raw)
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # exists but not ours to signal — still alive
        return True
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# quarantine (only for snapshot-/live-confirmed 'corrupted')
# ---------------------------------------------------------------------------

def _alert_path(profile_name: str, epoch: int) -> Path:
    return HOME / ".hermes" / "logs" / f"CORRUPTION-{profile_name}-{epoch}.alert"


def _write_alert(profile_name: str, epoch: int, profile_dir: Path) -> None:
    try:
        log_dir = HOME / ".hermes" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        alert = _alert_path(profile_name, epoch)
        alert.write_text(
            f"[{_ts()}] URGENT: confirmed SQLite corruption for profile "
            f"'{profile_name}'.\n"
            f"  gateway: ALIVE\n"
            f"  live db quarantined (atomic rename, same fs) to:\n"
            f"    {profile_dir / f'state.db.corrupted-{epoch}'}\n"
            f"  -wal/-shm preserved for forensic integrity (NOT unlinked).\n"
            f"  DB was NOT rebuilt (a live rebuild would lose sessions).\n"
            f"  Manual recovery required. Old image retained under 7d sweep.\n"
        )
    except OSError as e:
        _log(f"  WARN: could not write alert marker: {e}")


def _checkpoint_truncate_best_effort(db_path: Path) -> None:
    """Flush WAL into main db (best-effort; ignore busy). No writer is alive."""
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        pass


def _run_recover(aside: Path, recovered_sql: Path) -> bool:
    """sqlite3 <aside> .recover -> recovered_sql. True on success."""
    try:
        with open(recovered_sql, "w") as f:
            result = subprocess.run(
                ["sqlite3", str(aside), ".recover"],
                stdout=f, stderr=subprocess.PIPE, text=True, timeout=300,
            )
        return result.returncode == 0 and recovered_sql.stat().st_size > 0
    except Exception as e:
        _log(f"  .recover failed: {e}")
        return False


def _run_rebuild(db_path: Path, recovered_sql: Path) -> bool:
    """sqlite3 <fresh db_path> '.read recovered_sql'. True on success."""
    try:
        result = subprocess.run(
            ["sqlite3", str(db_path), f".read {recovered_sql}"],
            capture_output=True, text=True, timeout=300,
        )
        return result.returncode == 0
    except Exception as e:
        _log(f"  rebuild failed: {e}")
        return False


def _vacuum(db_path: Path) -> bool:
    """VACUUM a fresh, unowned DB (safe — nobody holds it)."""
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
        return True
    except sqlite3.Error as e:
        _log(f"  post-rebuild VACUUM failed: {e}")
        return False


def _unlink_best_effort(p: Path) -> None:
    try:
        if p.exists():
            p.unlink()
    except OSError:
        pass


def quarantine(db_path: Path, profile_name: str, profile_dir: Path) -> str:
    """Act on a *confirmed* corrupted DB. Returns 'paged'|'recovered'|'failed'.

    Gateway ALIVE -> atomic rename aside, keep -wal/-shm, alert, page (no
    rebuild: unlinking a live writer's file loses sessions / ghosts inodes).
    Gateway DEAD  -> checkpoint, rename aside, rebuild from .recover, VACUUM,
                     verify, then delete aside + recovered.sql; on any
                     rebuild/verify failure restore by renaming back and keep
                     the aside/recovered artifacts for forensics.
    """
    epoch = int(time.time())
    aside = Path(str(db_path) + f".corrupted-{epoch}")
    recovered_sql = db_path.with_suffix(f".recovered-{epoch}.sql")

    if profile_gateway_alive(profile_dir):
        try:
            os.rename(db_path, aside)  # atomic, same fs; -wal/-shm left alone
        except OSError as e:
            _log(f"  !! rename-to-quarantine failed: {e}")
            return "failed"
        _log(f"  !!! URGENT: confirmed corruption in '{profile_name}' "
             f"(gateway ALIVE). Live DB quarantined to {aside.name}; "
             f"NOT rebuilt; -wal/-shm preserved; paging for manual recovery.")
        _write_alert(profile_name, epoch, profile_dir)
        return "paged"

    # gateway DEAD: no live writer, rebuild is safe
    _log(f"  Confirmed corruption in '{profile_name}' (gateway DEAD) — rebuilding.")
    _checkpoint_truncate_best_effort(db_path)

    try:
        os.rename(db_path, aside)  # aside = the corrupt (now checkpointed) image
    except OSError as e:
        _log(f"  !! rename-aside failed: {e}")
        return "failed"
    # stale -wal/-shm belong to the OLD db (moved aside); checkpoint flushed them
    for suffix in ("-wal", "-shm"):
        _unlink_best_effort(Path(str(db_path) + suffix))

    ok = _run_recover(aside, recovered_sql)
    if ok:
        ok = _run_rebuild(db_path, recovered_sql)
    if ok:
        ok = _vacuum(db_path)
    if ok:
        ok = quick_check(db_path, timeout_s=SNAPSHOT_TIMEOUT_S)[0] == "ok"

    if ok:
        _unlink_best_effort(aside)
        _unlink_best_effort(recovered_sql)
        old_size = db_path.stat().st_size / 1048576
        _log(f"  RECOVERED '{profile_name}': rebuilt {old_size:.0f}MB "
             f"(aside-copy + recovered.sql deleted)")
        return "recovered"

    # failure: restore original by renaming aside back; keep artifacts
    _unlink_best_effort(db_path)  # drop the bad fresh rebuild if present
    try:
        if aside.exists():
            os.rename(aside, db_path)
            _log(f"  Rebuild failed — restored original '{profile_name}' "
                 f"(aside-copy + recovered.sql kept for forensics)")
    except OSError as e:
        _log(f"  !! restore failed too: {e} — aside kept at {aside.name}")
    return "failed"


# ---------------------------------------------------------------------------
# compact (only when check 'ok')
# ---------------------------------------------------------------------------

def _checkpoint_best_effort(db_path: Path) -> None:
    """After-compact wal_checkpoint(TRUNCATE) from our own connection.

    Best-effort with a short retry loop; busy is expected on live DBs and is
    ignored (WAL compaction is an optimization, never required for safety).
    """
    for _ in range(3):
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                return
            finally:
                conn.close()
        except sqlite3.OperationalError:
            time.sleep(0.5)
        except (sqlite3.Error, OSError):
            return


def compact_database(db_path: Path, profile_name: str) -> bool:
    """Maintenance on a *healthy* DB: prune old sessions, ensure index.

    VACUUM is intentionally gone: on a live gateway it silently failed with
    SQLITE_BUSY every night (zero COMPACTED log lines ever) and when idle it
    was a multi-GB exclusive-lock storm. Session DELETE works under WAL; the
    orphan-message DELETE only runs when old sessions were actually removed;
    wal_checkpoint(TRUNCATE) is best-effort.
    """
    try:
        conn = sqlite3.connect(str(db_path))
        old_size = db_path.stat().st_size / 1048576

        # Delete old ended sessions (works under WAL against a live gateway).
        cutoff = time.time() - (OLD_SESSION_DAYS * 86400)
        cur = conn.execute(
            "DELETE FROM sessions WHERE ended_at IS NOT NULL AND ended_at < ?",
            (cutoff,),
        )
        deleted = cur.rowcount
        conn.commit()

        # Ensure the ended_at index once per DB (idempotent; safe under WAL).
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_ended "
                "ON sessions(ended_at)"
            )
            conn.commit()
        except sqlite3.Error as e:
            _log(f"  index create skipped for {profile_name}: {e}")

        # Orphan cleanup ONLY when this night actually removed closed sessions.
        if deleted > 0:
            conn.execute(
                "DELETE FROM messages WHERE session_id NOT IN "
                "(SELECT id FROM sessions)"
            )
            conn.commit()

        conn.close()

        _checkpoint_best_effort(db_path)

        new_size = db_path.stat().st_size / 1048576
        _log(f"  COMPACTED {profile_name}: {old_size:.0f}MB → {new_size:.0f}MB "
             f"(deleted {deleted} old sessions)")
        return True
    except Exception as e:
        _log(f"  Compact failed for {profile_name}: {e}")
        return False


# ---------------------------------------------------------------------------
# housekeeping — stale artifact sweep
# ---------------------------------------------------------------------------

_SWEEP_PATTERNS = ("state.db.snap-*", "*.recovered-*.sql", "state.db.corrupted-*")


def _sweep_dir(d: Path, cutoff: float) -> int:
    removed = 0
    for pattern in _SWEEP_PATTERNS:
        for p in d.glob(pattern):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


def sweep_stale_artifacts(root: Path | None = None) -> int:
    """Delete snap/recovered/corrupted temp artifacts older than 7 days.

    A .corrupted-* younger than 7 days is never deleted — it may be a
    forensic/restore image awaiting manual action.
    """
    cutoff = time.time() - (STALE_ARTIFACT_DAYS * 86400)
    if root is not None:
        dirs = [root]
    else:
        profile_dirs = [d for d in PROFILES_DIR.iterdir() if d.is_dir()] \
            if PROFILES_DIR.exists() else []
        dirs = profile_dirs + [HOME / ".hermes"]  # default-profile artifacts
    removed = 0
    for d in dirs:
        removed += _sweep_dir(d, cutoff)
    if removed:
        _log(f"  housekeeping: swept {removed} stale artifact(s) (>"
             f"{STALE_ARTIFACT_DAYS}d)")
    return removed


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------

def run(check_only: bool = False, profile_filter: str | None = None) -> dict:
    """Scan + classify all state.db files; quarantine only confirmed corruption."""
    sweep_stale_artifacts()

    dbs = find_state_dbs()
    if profile_filter:
        dbs = [(p, d) for p, d in dbs if profile_filter in p]

    _log(f"Scanning {len(dbs)} state.db files...")

    results = RESULTS_DEFAULTS()

    for profile_name, db_path in dbs:
        profile_dir = db_path.parent
        try:
            size_mb = db_path.stat().st_size / 1048576
        except OSError as e:
            results["failed"] += 1
            _log(f"  {profile_name}: ERROR — {e}")
            continue

        status, detail, method = classify(db_path)

        if method == "timeout-snapshot":
            results["unknown"] += 1  # was 'unknown', resolved via snapshot

        if status == "ok":
            results["ok"] += 1
            if size_mb > LARGE_DB_THRESHOLD_MB:
                _log(f"  {profile_name}: OK ({size_mb:.0f}MB) — compacting...")
                if not check_only and compact_database(db_path, profile_name):
                    results["compacted"] += 1
            elif size_mb > 10:
                _log(f"  {profile_name}: OK ({size_mb:.0f}MB)")
        elif status == "corrupted":
            results["corrupted"] += 1
            _log(f"  {profile_name}: CORRUPTED ({size_mb:.0f}MB) — {detail[:80]}")
            if not check_only:
                outcome = quarantine(db_path, profile_name, profile_dir)
                if outcome == "paged":
                    results["paged"] += 1
                elif outcome == "recovered":
                    results["recovered"] += 1
                    results["corrupted"] -= 1
                else:  # failed
                    results["failed"] += 1
        else:  # 'error'
            results["failed"] += 1
            _log(f"  {profile_name}: ERROR — {detail[:80]}")

    parts = [f"{results['ok']} OK", f"{results['compacted']} compacted",
             f"{results['recovered']} recovered", f"{results['corrupted']} corrupted",
             f"{results['unknown']} snapshot-resolved", f"{results['paged']} paged",
             f"{results['failed']} failed"]
    _log("Summary: " + ", ".join(parts))
    return results


def main(argv: list[str]) -> int:
    check_only = "--check-only" in argv
    profile = None
    for i, arg in enumerate(argv):
        if arg.startswith("--profile="):
            profile = arg.split("=", 1)[1]
        elif arg == "--profile" and i + 1 < len(argv):
            profile = argv[i + 1]

    results = run(check_only=check_only, profile_filter=profile)
    if results["corrupted"] or results["paged"] or results["failed"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
