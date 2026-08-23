#!/usr/bin/env python3
"""
net4sats_autoheal.py — net4sats-mvp board auto-healer. Runs as no_agent=True (zero tokens).

BACKOFF GATE: checks if board state changed before doing any work.
- Same board state (blocked/ready tasks unchanged) -> exponential backoff:
  15m -> 30m -> 1h -> 2h -> 4h -> 8h -> 24h (cap)
- Board state changed -> runs immediately, resets backoff counter
- During backoff: exits 0 silently (no work done, no output)

When it DOES run:
  1. Run stale resetter — reclaim zombie tasks (30min threshold)
  2. Dispatch ready tasks on net4sats-mvp board
  3. Check for human-review blockers
  4. Signal AI fallback if analysis needed
  5. Report what happened
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
BOARDS_DIR = HOME / ".hermes" / "kanban" / "boards"
STATE_DIR = HOME / ".hermes" / "state"
TARGET_BOARD = "net4sats-mvp"
HUMAN_GATE_BOARD = "human-gate"


def run(cmd: str, timeout: int = 15) -> tuple[str, int]:
    """Run a shell command, return (stdout, exit_code)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "(timeout)", -1
    except Exception as e:
        return str(e), -1


def run_stale_resetter() -> int:
    """Run the stale resetter script. Returns number of tasks reclaimed."""
    out, rc = run("python3 " + str(HOME / ".hermes/profiles/manager/scripts/kanban_stale_resetter.py"), timeout=10)
    if rc != 0:
        return 0
    if not out or not out.startswith("ZOMBIE:"):
        return 0
    try:
        return int(out.split()[1])
    except (IndexError, ValueError):
        return 0


def dispatch_board() -> int:
    """Dispatch ready tasks on net4sats-mvp. Returns spawned count."""
    out, rc = run(f"hermes kanban --board {TARGET_BOARD} dispatch --failure-limit 3", timeout=15)
    if rc != 0:
        return 0
    count = 0
    for line in out.split("\n"):
        if "Spawned:" in line:
            try:
                count += int(line.split(":")[1].strip())
            except (IndexError, ValueError):
                pass
    return count


def get_board_state() -> dict:
    """Get detailed state of the net4sats-mvp board."""
    db_path = BOARDS_DIR / TARGET_BOARD / "kanban.db"
    state = {
        "todo": 0, "ready": 0, "running": 0,
        "blocked": 0, "done": 0, "archived": 0,
        "blocked_tasks": [],
        "ready_tasks": [],
        "human_gate_pending": 0,
        "human_review_blockers": [],
    }
    if not db_path.exists():
        return state

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        # Count by status
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
        ).fetchall()
        for r in rows:
            s = r["status"]
            if s in state:
                state[s] = r["cnt"]

        # Detailed blocked tasks
        blocked = conn.execute(
            "SELECT id, title, assignee FROM tasks WHERE status = 'blocked' ORDER BY created_at"
        ).fetchall()
        for r in blocked:
            state["blocked_tasks"].append({
                "id": r["id"],
                "title": (r["title"] or "")[:80],
                "assignee": r["assignee"] or "(unassigned)",
            })

        # Detailed ready tasks
        ready = conn.execute(
            "SELECT id, title, assignee FROM tasks WHERE status = 'ready' ORDER BY created_at"
        ).fetchall()
        for r in ready:
            state["ready_tasks"].append({
                "id": r["id"],
                "title": (r["title"] or "")[:80],
                "assignee": r["assignee"] or "(unassigned)",
            })

        conn.close()
    except Exception:
        pass

    # Check human-gate board for pending shadows
    hg_db = BOARDS_DIR / HUMAN_GATE_BOARD / "kanban.db"
    if hg_db.exists():
        try:
            hg_conn = sqlite3.connect(str(hg_db))
            hg_conn.row_factory = sqlite3.Row
            pending = hg_conn.execute(
                "SELECT id, title FROM tasks WHERE status IN ('ready', 'running') ORDER BY created_at"
            ).fetchall()
            state["human_gate_pending"] = len(pending)
            state["human_review_blockers"] = [
                {"id": r["id"], "title": (r["title"] or "")[:100]}
                for r in pending
            ]
            hg_conn.close()
        except Exception:
            pass

    return state


def _compute_board_hash(state: dict) -> str:
    """Compute a deterministic hash of the board state for backoff."""
    key_fields = (
        state["todo"], state["ready"], state["running"],
        state["blocked"], state["done"],
        state["human_gate_pending"],
        [(t["id"], t["assignee"]) for t in state["blocked_tasks"]],
        [(t["id"],) for t in state["ready_tasks"]],
    )
    return str(key_fields)


def _check_backoff() -> bool:
    """
    Check if we should skip this run due to exponential backoff.
    Returns True if we should RUN. Returns False if SKIP.
    """
    BACKOFF_MINUTES = [15, 30, 60, 120, 240, 480, 1440]
    backoff_path = STATE_DIR / "net4sats_run_backoff.json"
    now = time.time()

    state_obj = get_board_state()
    current_hash = _compute_board_hash(state_obj)

    persisted = {"hash": None, "consecutive": 0, "last_run": 0}
    if backoff_path.exists():
        try:
            persisted = json.loads(backoff_path.read_text())
        except (json.JSONDecodeError, IOError):
            pass

    # If board state changed — run immediately, reset backoff
    if persisted.get("hash") != current_hash:
        backoff_path.write_text(json.dumps({
            "hash": current_hash, "consecutive": 0, "last_run": now
        }))
        # Also cache the current state for the signal file
        _cache_state(state_obj)
        return True

    # Same hash — check if backoff expired
    consecutive = persisted.get("consecutive", 0)
    last_run = persisted.get("last_run", 0)
    idx = min(consecutive, len(BACKOFF_MINUTES) - 1)
    required_gap = BACKOFF_MINUTES[idx] * 60

    if now - last_run >= required_gap:
        backoff_path.write_text(json.dumps({
            "hash": current_hash, "consecutive": consecutive + 1, "last_run": now
        }))
        _cache_state(state_obj)
        return True

    return False


def _cache_state(state: dict):
    """Cache the current board state for the AI fallback cron to read."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = STATE_DIR / "net4sats_state.json"
        cache_path.write_text(json.dumps({
            "timestamp": time.time(),
            "iso_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "board": state,
        }, indent=2))
    except Exception:
        pass


def _signal_if_needed(state: dict, reclaimed: int, spawned: int):
    """Write a signal file if the AI fallback cron should do analysis."""
    needs_analysis = False
    reasons = []

    # Check for human-review blockers
    if state["human_gate_pending"] > 0:
        needs_analysis = True
        reasons.append(f"{state['human_gate_pending']} human-gate item(s) pending")

    # Check for long-running blocked tasks (more than just human-gate)
    non_hg_blocked = [t for t in state["blocked_tasks"]]
    if non_hg_blocked:
        needs_analysis = True
        reasons.append(f"{len(non_hg_blocked)} blocked task(s)")

    # Check for stalled workers
    if state["running"] > 0:
        needs_analysis = True
        reasons.append(f"{state['running']} running worker(s)")

    # Check for ready tasks not yet dispatched
    if state["ready"] > 0:
        needs_analysis = True
        reasons.append(f"{state['ready']} ready task(s)")

    signal_path = STATE_DIR / "net4sats_needs_ai.json"
    if needs_analysis:
        signal = {
            "timestamp": time.time(),
            "iso_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "type": "analysis_needed",
            "reasons": reasons,
            "state": state,
            "reclaimed": reclaimed,
            "spawned": spawned,
        }
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        signal_path.write_text(json.dumps(signal, indent=2))
        return True
    else:
        # No analysis needed — remove signal file
        try:
            if signal_path.exists():
                signal_path.unlink()
        except Exception:
            pass
        return False


def main():
    # BACKOFF GATE
    if not _check_backoff():
        sys.exit(0)

    # Step 1: Reclaim zombie tasks
    reclaimed = run_stale_resetter()

    # Step 2: Dispatch ready tasks
    spawned = dispatch_board()

    # Step 3: Get fresh state after dispatch
    state = get_board_state()

    # Step 4: Signal if AI analysis needed
    needs_ai = _signal_if_needed(state, reclaimed, spawned)

    # Step 5: Build report
    now = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    lines = []

    if reclaimed > 0 or spawned > 0:
        parts = []
        if reclaimed > 0:
            parts.append(f"reclaimed {reclaimed}")
        if spawned > 0:
            parts.append(f"spawned {spawned}")
        lines.append(f"🔄 net4sats auto-heal [{now}]: {' + '.join(parts)}")

    if needs_ai:
        lines.append("\n📋 net4sats-mvp state:")
        lines.append(f"  {state['todo']} todo | {state['ready']} ready | {state['running']} running | {state['blocked']} blocked | {state['done']} done")

        if state["human_review_blockers"]:
            lines.append("\n  ⏳ Needs HUMAN REVIEW (human-gate board):")
            for b in state["human_review_blockers"]:
                lines.append(f"    {b['id']} — {b['title']}")

        if state["blocked_tasks"]:
            lines.append(f"\n  🚧 Other blocked:")
            for b in state["blocked_tasks"][:5]:
                lines.append(f"    {b['id']} — {b['title']} ({b['assignee']})")

        if state["ready_tasks"]:
            lines.append(f"\n  ▶ Ready to dispatch:")
            for r in state["ready_tasks"][:3]:
                lines.append(f"    {r['id']} — {r['title']}")

    if lines:
        print("\n".join(lines))
    # else: silent


if __name__ == "__main__":
    main()
