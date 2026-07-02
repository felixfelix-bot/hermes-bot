#!/usr/bin/env python3
"""
Kanban Stale Task Resetter — reclaims running tasks with dead workers.

Runs as a cron job (no_agent=True, zero tokens). Scans all board DBs directly,
finds tasks stuck in 'running' with dead workers + no heartbeat, and resets
them to 'ready' so the dispatcher can re-pick them.

This is a SAFETY NET for the dispatcher's built-in reclaim, which has two
known gaps:
  1. `hermes kanban dispatch` CLI doesn't pass stale_timeout_seconds (Bug 1)
  2. CLI dispatch only processes the current board, not all boards (Bug 2)

Criteria for reset:
  - status = 'running'
  - worker_pid is NULL or the PID is not alive (/proc/PID doesn't exist)
  - age > 30 minutes (tasks with dead PIDs — fast reclaim)
  - last_heartbeat_at is NULL or > 1h ago (keep 2h threshold for heartbeat-only)

FIXES APPLIED (2026-07-01):
  - Lowered dead-PID threshold from 2h to 30 min
  - After resetting zombies, calls `hermes kanban --board X dispatch`
  - Writes a structured alert file at ~/.hermes/state/zombie_alert.json so the
    unified anomaly cron can pick it up

Silent (empty stdout) when nothing needs resetting — designed for cron.
Emits one-line alert via anomaly file when zombies found + recovered.

Usage:
  /usr/bin/python3 kanban_stale_resetter.py [--dry-run]

============================================================================
PERMANENT FIX (upstream Hermes — when time allows):
============================================================================

This script is a SAFETY NET. The proper fix is two patches in the Hermes
codebase:

  FIX 1 — kanban.py:_cmd_dispatch (~line 2141)
  -----------------------------------------------
  The CLI dispatch command calls dispatch_once() but does NOT pass
  stale_timeout_seconds, so it defaults to 0 (disabled). The fix is to
  read it from config and pass it through, just like the gateway watcher does:

      # In _cmd_dispatch, after reading _kanban_cfg:
      raw_stale = _kanban_cfg.get("dispatch_stale_timeout_seconds", 0)
      try:
          stale_timeout_seconds = int(raw_stale or 0)
      except (TypeError, ValueError):
          stale_timeout_seconds = 0

      res = kb.dispatch_once(
          conn,
          ...,
          stale_timeout_seconds=stale_timeout_seconds,  # <-- ADD THIS
      )

  FIX 2 — kanban.py:_cmd_dispatch (~line 2140)
  -----------------------------------------------
  dispatch_once is called via `connect_closing()` with no board argument,
  which resolves to the "current" board pointer only. Tasks on other boards
  are never reclaimed or dispatched. The fix is to iterate all boards:

      # Instead of:
      #   with kb.connect_closing() as conn:
      #       res = kb.dispatch_once(conn, ...)
      #
      # Do:
      for slug in _all_board_slugs():
          with kb.connect_closing(board=slug) as conn:
              res = kb.dispatch_once(conn, board=slug, ...)
              # aggregate results across boards

  The gateway watcher (_kanban_dispatcher_watcher in kanban_watchers.py)
  already does this correctly — it iterates all boards and passes
  stale_timeout_seconds. The issue is ONLY in the CLI path, which is
  what our systemd timer (throttled_daemon.sh) uses because
  dispatch_in_gateway=false in our config.

  ALTERNATIVE FIX 3 — enable dispatch_in_gateway=true
  ------------------------------------------------------
  If the gateway's internal watcher is reliable enough, simply set
  dispatch_in_gateway=true in config.yaml and REMOVE the systemd timer.
  The gateway watcher already iterates all boards AND passes
  stale_timeout_seconds correctly. This is the path of least resistance
  but depends on the gateway staying alive.

  Until one of these fixes is applied upstream, this cron script runs
  every 5 minutes as a safety net.
============================================================================
"""
import argparse
import glob
import json
import os
import sqlite3
import subprocess  # nosec — trusted cron context
import sys
import time
from pathlib import Path

BOARDS_DIR = Path.home() / ".hermes" / "kanban" / "boards"
STATE_DIR = Path.home() / ".hermes" / "state"
ALERT_FILE = STATE_DIR / "zombie_alert.json"

# Fast reclaim for tasks with dead PIDs
DEAD_PID_THRESHOLD_SECONDS = 1800  # 30 minutes

# Slower heartbeat-only check (worker alive but no heartbeat = 1h)
HEARTBEAT_STALE_SECONDS = 3600  # 1h — matches _STALE_HEARTBEAT_GAP_SECONDS


def find_stale_running(db_path: str, now: int) -> list[dict]:
    """Find stale running tasks in a single board DB."""
    stale = []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, started_at, last_heartbeat_at, worker_pid, assignee "
            "FROM tasks WHERE status = 'running'"
        ).fetchall()

        for r in rows:
            started = r["started_at"] or 0
            age_s = now - started

            # Check PID liveness
            pid = r["worker_pid"]
            pid_alive = False
            if pid:
                try:
                    pid_alive = os.path.exists(f"/proc/{pid}")
                except (OSError, ValueError):
                    pass

            if pid_alive:
                # Worker PID is alive — use heartbeat-only staleness (1h)
                hb = r["last_heartbeat_at"]
                if hb and (now - hb) < HEARTBEAT_STALE_SECONDS:
                    continue  # Recent heartbeat — probably still working
                # No recent heartbeat but PID alive — could be hanging
                # Use longer threshold (keep the original 2h behavior)
                if age_s < 7200:  # 2 hours for heartbeat-only
                    continue
            else:
                # PID dead or missing — use fast reclaim (30 min)
                if age_s < DEAD_PID_THRESHOLD_SECONDS:
                    continue

            # Check heartbeat as final gate
            hb = r["last_heartbeat_at"]
            if hb and (now - hb) < HEARTBEAT_STALE_SECONDS:
                continue  # Recent heartbeat — give benefit of doubt

            stale.append({
                "id": r["id"],
                "title": r["title"],
                "started_at": started,
                "age_hours": age_s / 3600,
                "worker_pid": pid,
                "assignee": r["assignee"],
            })
        conn.close()
    except Exception:
        pass
    return stale


def reset_task(db_path: str, task_id: str, now: int) -> bool:
    """Reset a single task to ready. Returns True on success."""
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE tasks SET "
            "  status = 'ready', "
            "  claim_lock = NULL, "
            "  claim_expires = NULL, "
            "  worker_pid = NULL, "
            "  last_heartbeat_at = NULL, "
            "  current_run_id = NULL "
            "WHERE id = ? AND status = 'running'",
            (task_id,),
        )
        # Close orphaned runs
        conn.execute(
            "UPDATE task_runs SET "
            "  status = 'reclaimed', "
            "  outcome = 'reclaimed_by_cron', "
            "  ended_at = ? "
            "WHERE task_id = ? AND status = 'running'",
            (now, task_id),
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def dispatch_board(board: str) -> bool:
    """Call hermes kanban dispatch for a board. Returns True on success."""
    try:
        result = subprocess.run(
            ["hermes", "kanban", "--board", board, "dispatch", "--failure-limit", "3"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def write_alert(affected_boards: list[str], reset_count: int):
    """Write structured alert for the unified anomaly cron."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    alert = {
        "type": "zombie_recovery",
        "timestamp": time.time(),
        "iso_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "boards": affected_boards,
        "tasks_recovered": reset_count,
        "dispatched": True,
    }
    try:
        ALERT_FILE.write_text(json.dumps(alert))
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Reset stale kanban running tasks")
    parser.add_argument("--dry-run", action="store_true", help="Report only, don't reset")
    args = parser.parse_args()

    now = int(time.time())

    all_stale = []
    for db_path in sorted(glob.glob(str(BOARDS_DIR / "*/kanban.db"))):
        board = db_path.split("/boards/")[1].split("/")[0]
        stale = find_stale_running(db_path, now)
        for s in stale:
            s["board"] = board
            s["db_path"] = db_path
            all_stale.append(s)

    if not all_stale:
        # Clean up stale alert file if it exists (no longer recovering)
        try:
            if ALERT_FILE.exists():
                ALERT_FILE.unlink()
        except Exception:
            pass
        # Silent — nothing to report
        return

    reset_count = 0
    failed_count = 0
    affected_boards = set()

    for s in sorted(all_stale, key=lambda x: -x["age_hours"]):
        affected_boards.add(s["board"])
        if not args.dry_run:
            ok = reset_task(s["db_path"], s["id"], now)
            if ok:
                reset_count += 1
            else:
                failed_count += 1

    # Write anomaly alert
    if not args.dry_run:
        write_alert(list(affected_boards), reset_count)

    # Re-dispatch affected boards
    dispatched_ok = 0
    dispatched_fail = 0
    if not args.dry_run:
        for board in sorted(affected_boards):
            if dispatch_board(board):
                dispatched_ok += 1
            else:
                dispatched_fail += 1

    # Print one-line alert for cron delivery
    boards_str = ", ".join(sorted(affected_boards))
    print(
        f"ZOMBIE: {reset_count} stale tasks recovered on [{boards_str}] "
        f"| {dispatched_ok}/{dispatched_fail + dispatched_ok} boards re-dispatched"
    )


if __name__ == "__main__":
    main()
