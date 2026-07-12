#!/usr/bin/env python3
"""off_peak_dispatch — dispatch queued heavy tasks after peak hours or when quota recovers.

Called by cron at 10:01 UTC (off-peak start) and whenever the quota gate flips to safe.
Scans kanban boards for deferred tasks (blocked with quota_* reason) and dispatches them.

Usage:
  python3 off_peak_dispatch.py                        # dry-run
  python3 off_peak_dispatch.py --board fips --execute  # actually dispatch
  python3 off_peak_dispatch.py --all-boards --execute
"""

from __future__ import annotations
import json
import subprocess
import sys
import datetime

QUOTA_GATE = "python3 ~/.hermes/bot/dispatch_quota_gate.py"


def check_quota() -> dict:
    """Run quota gate and parse result."""
    try:
        result = subprocess.run(
            QUOTA_GATE, shell=True, capture_output=True, text=True, timeout=10
        )
        return json.loads(result.stdout)
    except Exception as e:
        return {"safe": False, "reason": f"gate_error_{e}"}


def get_deferred_tasks(board: str) -> list[dict]:
    """Get tasks deferred due to quota on a specific board."""
    try:
        result = subprocess.run(
            f"hermes kanban --board {board} list --status blocked",
            shell=True, capture_output=True, text=True, timeout=30
        )
        tasks = []
        for line in result.stdout.splitlines():
            if "quota_" in line.lower():
                # Parse task ID and title from kanban output
                # Format: "⊘ t_task_id  blocked   assignee       title (details)"
                parts = line.strip().split()
                if len(parts) >= 4:
                    task_id = parts[1]  # t_task_id
                    title = ' '.join(parts[4:]) if len(parts) > 4 else ""
                    tasks.append({"id": task_id, "title": title})
        return tasks
    except Exception:
        return []


def dispatch_task(board: str, task_id: str):
    """Unblock and dispatch a single task."""
    try:
        subprocess.run(
            f"hermes kanban --board {board} unblock {task_id}",
            shell=True, capture_output=True, text=True, timeout=15
        )
        print(f"  Unblocked {task_id} on {board}")
        return True
    except Exception as e:
        print(f"  Failed to unblock {task_id}: {e}")
        return False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Off-peak heavy task dispatcher")
    parser.add_argument("--board", help="Specific board to scan")
    parser.add_argument("--all-boards", action="store_true", help="Scan all boards")
    parser.add_argument("--execute", action="store_true", help="Actually dispatch (default: dry-run)")
    args = parser.parse_args()
    
    # Check quota safety first
    quota = check_quota()
    hour = datetime.datetime.utcnow().hour
    peak = 6 <= hour < 10
    
    print(f"Quota: {'SAFE' if quota.get('safe') else 'BLOCKED'}")
    print(f"  state={quota.get('quota_state')} peak={quota.get('peak_hours')} tier={quota.get('recommended_tier')}")
    
    if peak:
        print("Peak hours — not dispatching heavy tasks")
        sys.exit(0)
    
    if not quota.get("safe"):
        print("Quota unsafe — not dispatching")
        sys.exit(0)
    
    # Determine which boards to scan
    boards = [args.board] if args.board else []
    if args.all_boards or not boards:
        # Get all boards
        try:
            result = subprocess.run(
                "ls ~/.hermes/kanban/boards/", shell=True,
                capture_output=True, text=True, timeout=5
            )
            boards = result.stdout.strip().splitlines()
        except Exception:
            boards = ["fips", "infrastructure", "admin", "tollgate"]
    
    if not boards:
        print("No boards found")
        sys.exit(1)
    
    for board in boards:
        tasks = get_deferred_tasks(board)
        if not tasks:
            print(f"No deferred tasks on {board}")
            continue
        
        print(f"\n{board}: {len(tasks)} deferred tasks")
        for t in tasks:
            print(f"  {t['id']}: {t['title']}")
            if args.execute:
                dispatch_task(board, t["id"])
            else:
                print(f"    (dry-run — use --execute to dispatch)")


if __name__ == "__main__":
    main()
