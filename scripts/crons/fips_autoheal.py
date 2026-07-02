#!/usr/bin/env python3
"""
fips_autoheal.py — FIPS board auto-healer. Runs as no_agent=True (zero tokens).

BACKOFF GATE: checks if board state changed before doing any work.
- Same board state (blocked/ready tasks unchanged) -> exponential backoff:
  15m -> 30m -> 1h -> 2h -> 4h -> 8h -> 24h (cap)
- Board state changed -> runs immediately, resets backoff counter
- During backoff: exits 0 silently (no work done, no output)

When it DOES run:
  1. Run stale resetter — reclaim zombie tasks (30min threshold)
  2. Dispatch ready tasks — one per board per cycle
  3. Signal AI fallback if human review needed
  4. Report what happened
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# Shared alert de-duplication (optional; fail-open if absent).
try:
    import alert_dedup
except Exception:
    alert_dedup = None

HOME = Path.home()
BOARDS_DIR = HOME / ".hermes" / "kanban" / "boards"
STATE_DIR = HOME / ".hermes" / "state"


def run(cmd: str, timeout: int = 15) -> tuple[str, int]:
    """Run a shell command, return (stdout, exit_code)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "(timeout)", -1
    except Exception as e:
        return str(e), -1


def get_active_boards() -> list[str]:
    """Discover boards with non-done, non-archived tasks."""
    skip = {"default", "archive", "archived"}
    boards = []
    try:
        for d in sorted(BOARDS_DIR.iterdir()):
            if d.name in skip or not d.is_dir():
                continue
            db = d / "kanban.db"
            if not db.exists():
                continue
            try:
                conn = sqlite3.connect(str(db))
                count = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done','archived')"
                ).fetchone()[0]
                conn.close()
                if count > 0:
                    boards.append(d.name)
            except Exception:
                pass
    except Exception:
        pass
    return boards if boards else ["admin"]


def run_stale_resetter() -> int:
    """Run the stale resetter script. Returns number of tasks reclaimed."""
    out, rc = run("python3 " + str(HOME / ".hermes/profiles/manager/scripts/kanban_stale_resetter.py"), timeout=10)
    if rc != 0:
        return 0
    # Output format: "ZOMBIE: N stale tasks recovered on [boards] | M boards re-dispatched"
    if not out:
        return 0
    if out.startswith("ZOMBIE:"):
        try:
            count = int(out.split()[1])
            return count
        except (IndexError, ValueError):
            return 0
    return 0


def dispatch_boards(boards: list[str]) -> int:
    """Dispatch each board. Returns total tasks spawned."""
    total = 0
    for board in boards:
        out, rc = run(f"hermes kanban --board {board} dispatch --failure-limit 3", timeout=15)
        if rc == 0 and ("Spawned:" in out or "spawned" in out.lower()):
            # Count spawned tasks
            for line in out.split("\n"):
                if "Spawned:" in line:
                    try:
                        total += int(line.split(":")[1].strip())
                    except (IndexError, ValueError):
                        total += 0
    return total


def find_blocked_needing_human() -> list[dict]:
    """Find blocked tasks that need human review — FIPS board only.
    
    Other boards have their own auto-healers and per-board reporting.
    FIPS is the one the user monitors here.
    """
    issues = []
    fips_db = BOARDS_DIR / "fips" / "kanban.db"
    if not fips_db.exists():
        return issues
    
    try:
        conn = sqlite3.connect(str(fips_db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, status, assignee FROM tasks WHERE status IN ('blocked', 'ready')"
        ).fetchall()
        conn.close()
        for r in rows:
            tid = r["id"]
            title = r["title"] or ""
            status = r["status"]
            assignee = r["assignee"] or "(unassigned)"

            # External mentions always need human
            if "[MENTION]" in title or "[EXTERNAL]" in title:
                issues.append({
                    "id": tid,
                    "title": title,
                    "type": "external_mention",
                    "assignee": assignee,
                })
                continue

            # Blocked tasks need human review
            if status == "blocked":
                issues.append({
                    "id": tid,
                    "title": title,
                    "type": "blocked",
                    "assignee": assignee,
                })
                continue

    except Exception:
        pass
    return issues


def _compute_board_hash() -> str:
    """Compute a hash of the current FIPS board state (blocked + ready task IDs)."""
    fips_db = BOARDS_DIR / "fips" / "kanban.db"
    if not fips_db.exists():
        return ""
    try:
        conn = sqlite3.connect(str(fips_db))
        rows = conn.execute(
            "SELECT id, status, assignee, title FROM tasks WHERE status IN ('blocked', 'ready')"
        ).fetchall()
        conn.close()
        # Deterministic: sort by id
        rows.sort(key=lambda r: r[0])
        return str([(r[0], r[1], r[2] or "", (r[3] or "")[:40]) for r in rows])
    except Exception:
        return ""


def _check_backoff() -> bool:
    """
    Check if we should skip this run entirely due to exponential backoff.
    Returns True if we should RUN (either state changed or backoff expired).
    Returns False if we should SKIP (same state, backoff still active).
    """
    BACKOFF_MINUTES = [15, 30, 60, 120, 240, 480, 1440]  # 15m -> 30m -> 1h -> 2h -> 4h -> 8h -> 24h
    backoff_path = STATE_DIR / "fips_run_backoff.json"
    current_hash = _compute_board_hash()
    now = time.time()
    
    state = {"hash": None, "consecutive": 0, "last_run": 0}
    if backoff_path.exists():
        try:
            state = json.loads(backoff_path.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    
    # If hash changed (board state changed) -> RUN immediately, RESET backoff
    if state.get("hash") != current_hash:
        backoff_path.write_text(json.dumps({
            "hash": current_hash, "consecutive": 0, "last_run": now
        }))
        return True  # run
    
    # Same hash — check if backoff has expired
    consecutive = state.get("consecutive", 0)
    last_run = state.get("last_run", 0)
    idx = min(consecutive, len(BACKOFF_MINUTES) - 1)
    required_gap = BACKOFF_MINUTES[idx] * 60
    
    if now - last_run >= required_gap:
        # Backoff expired — run and increment
        backoff_path.write_text(json.dumps({
            "hash": current_hash, "consecutive": consecutive + 1, "last_run": now
        }))
        return True  # run
    
    return False  # skip — still in backoff


def main():
    # BACKOFF GATE — check BEFORE doing any work
    if not _check_backoff():
        # Same board state, backoff still active — skip entirely (silent)
        sys.exit(0)
    
    boards = get_active_boards()
    
    # Step 1: Reclaim zombie tasks
    reclaimed = run_stale_resetter()
    
    # Step 2: Dispatch ready tasks
    spawned = dispatch_boards(boards)
    
    # Step 3: Find what needs human review
    human_issues = find_blocked_needing_human()
    
    # Step 4: Write signal file if human review needed (for AI fallback cron)
    signal_path = STATE_DIR / "fips_needs_ai.json"
    if human_issues:
        signal = {
            "timestamp": time.time(),
            "iso_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "type": "human_review_needed",
            "issues": human_issues,
            "reclaimed": reclaimed,
            "spawned": spawned,
        }
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        signal_path.write_text(json.dumps(signal, indent=2))
    else:
        try:
            if signal_path.exists():
                signal_path.unlink()
        except Exception:
            pass
    
    # Step 5: Build report (only if something meaningful changed)
    now = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    lines = []
    
    # Auto-recovery events
    if reclaimed > 0 or spawned > 0:
        parts = []
        if reclaimed > 0:
            parts.append(f"reclaimed {reclaimed} zombie{'s' if reclaimed != 1 else ''}")
        if spawned > 0:
            parts.append(f"spawned {spawned} worker{'s' if spawned != 1 else ''}")
        lines.append(f"🔄 FIPS auto-heal [{now}]: {' + '.join(parts)}")
    
    # Human-review items
    if human_issues:
        lines.append("")
        lines.append("NEEDS HUMAN REVIEW (FIPS):")
        for issue in human_issues:
            icon = "🔗" if issue["type"] == "external_mention" else "🚧"
            lines.append(f"  {icon} {issue['id']} — {issue['title']}")
    
    if lines:
        print("\n".join(lines))
    # else: silent


if __name__ == "__main__":
    main()
