#!/usr/bin/env python3
"""offload-sweep.py — no_agent 5-min offload sweep (Phase 1, t_bd4d83c1).

Reassigns READY (never running/claimed) CPU-heavy tasks to worker-dq05 when:
    * task classify() is heavy (build/test/crunch)   AND
    * board routing.json offload != "off"             AND
    * local load/core > 2.0 (stressed)                AND
    * a green dispatchable remote target exists (probe, 30s TTL cache)
The delegation snippet is appended to the task body (idempotent) so the
worker knows to ssh to the target for the heavy step.

ZERO TOKENS: this is a no_agent cron script ($0). It never blocks, never
touches running/claimed tasks, and fails-soft to a no-op on any error.

Run:
    python3 offload-sweep.py [--board <b>] [--dry-run] [--once]
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import offload_router
except ImportError:
    offload_router = None

BOARDS_ROOT = Path.home() / ".hermes" / "kanban" / "boards"
SWEEP_MARKER = "OFFLOAD EXECUTION (auto-appended by offload_router)"


def run_hermes(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, shell=True)
        return r.stdout, r.returncode
    except subprocess.TimeoutExpired:
        return "", -1


def ready_tasks(board=None):
    """All READY tasks (assigned OR unassigned) — the sweep must see tasks the
    auto-assigner never would (they arrive pre-assigned)."""
    tasks = []
    boards = [board] if board else [d.name for d in sorted(BOARDS_ROOT.iterdir())
                                    if (d / "kanban.db").exists()]
    for b in boards:
        db = BOARDS_ROOT / b / "kanban.db"
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT id, title, body, assignee FROM tasks WHERE status='ready'"
            ).fetchall()
            for tid, title, body, assignee in rows:
                tasks.append({"id": tid, "title": title or "", "body": body or "",
                              "board": b, "assignee": assignee or ""})
            conn.close()
        except Exception:
            continue
    return tasks


def append_body(board, task_id, body, alias, target):
    """Idempotently append delegation snippet to a task body via sqlite.
    Only ever touches the body field of a READY task we're about to reassign."""
    db = BOARDS_ROOT / board / "kanban.db"
    new_body = offload_router.append_delegation_body(body, alias=alias,
                                                     target=target)
    if new_body == body:
        return False
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE tasks SET body=? WHERE id=? AND status='ready'",
                     (new_body, task_id))
        conn.commit()
        return True
    finally:
        conn.close()


def reassign(board, task_id, profile):
    out, rc = run_hermes(
        f"hermes kanban --board {board} reassign {task_id} {profile} 2>&1",
        timeout=10)
    return rc == 0 and "error" not in out.lower()


def main():
    parser = argparse.ArgumentParser(description="Offload sweep (no_agent)")
    parser.add_argument("--board", type=str, default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true",
                        help="Single pass (cron sets its own cadence)")
    args = parser.parse_args()

    if offload_router is None:
        print("OFFLOAD-SWEEP: offload_router.py missing — no-op")
        return 0

    # Gate 1: quota not needed (zero-token). Resource gate: local stressed?
    local_lpc = offload_router._local_load_per_core()
    local_stressed = local_lpc > offload_router.LOCAL_STRESS_LOAD_PER_CORE
    if not local_stressed:
        print(f"OFFLOAD-SWEEP: local load/core {local_lpc:.2f} not stressed — no-op")
        return 0

    facts = offload_router.probe.probe_targets(cache_ttl=30)
    green = offload_router.first_green(facts)
    if green is None:
        print("OFFLOAD-SWEEP: no green dispatchable remote target — no-op")
        return 0

    changed = 0
    for t in ready_tasks(args.board):
        if t["assignee"] == "worker-dq05":
            continue  # already routed
        board_rule = offload_router.load_board_rule(t["board"])
        if board_rule == "off":
            continue
        cls = offload_router.classify(t["title"], t["body"])
        if not cls["heavy"]:
            continue
        if t["body"] and SWEEP_MARKER in t["body"]:
            pass  # already has delegation body
        elif not args.dry_run:
            append_body(t["board"], t["id"], t["body"],
                        green["ssh_alias"], green["name"].upper())
        if args.dry_run:
            print(f"[DRY-RUN] would reassign {t['board']}/{t['id']} "
                  f"({t['title'][:60]}) → {green['profile']} "
                  f"(green={green['name']})")
            changed += 1
            continue
        ok = reassign(t["board"], t["id"], green["profile"])
        if ok:
            print(f"[REASSIGNED] {t['board']}/{t['id']} "
                  f"({t['title'][:60]}) → {green['profile']} "
                  f"(green={green['name']})")
            changed += 1
        else:
            print(f"[FAIL] {t['board']}/{t['id']} reassign failed")

    print(f"OFFLOAD-SWEEP: {changed} heavy tasks routed to "
          f"{green['profile'] if green else 'none'} "
          f"(local_lpc={local_lpc:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
