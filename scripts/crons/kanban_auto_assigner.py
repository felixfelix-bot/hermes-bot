#!/usr/bin/env python3
"""
Kanban Auto-Assigner — scans all boards for ready+unassigned tasks and
intelligently assigns them to available worker profiles.

Runs as a cron job (LLM-driven, with --auto flag) or standalone (report only).

Assignment logic (deterministic, no LLM needed for this part):
  - Tasks on 'plebeian' board → worker-plebeian
  - Tasks on 'tollgate' board → worker-tollgate
  - Tasks on 'admin' board → worker-admin
  - If a board's dedicated worker is busy → fall back to worker-base
  - If all workers busy → report and defer

Usage:
  # Report mode (zero tokens) — scan and print what needs assigning
  python3 ~/.hermes/profiles/manager/scripts/kanban_auto_assigner.py

  # Auto-assign mode — actually assign profiles to unassigned tasks
  python3 ~/.hermes/profiles/manager/scripts/kanban_auto_assigner.py --auto

  # Dry-run mode — show what would be assigned without doing it
  python3 ~/.hermes/profiles/manager/scripts/kanban_auto_assigner.py --auto --dry-run

  # Custom idle threshold (default: 1h)
  python3 ~/.hermes/profiles/manager/scripts/kanban_auto_assigner.py --min-idle-hours 2
"""

import subprocess
import json
import re
import sys
import time
import os
import argparse
from datetime import datetime, timezone
from pathlib import Path


def run_hermes(cmd, timeout=15):
    """Run a hermes CLI command and return stdout."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, shell=True
        )
        return r.stdout, r.returncode
    except subprocess.TimeoutExpired:
        return "", -1


def get_all_boards():
    """Discover all boards from filesystem — always accurate, no CLI parsing needed.
    
    Scans ~/.hermes/kanban/boards/ for SQLite databases. Skips 'default' and
    'archive' boards. This adapts automatically when new boards are created
    without needing to update any hardcoded lists.
    """
    boards_dir = Path.home() / ".hermes" / "kanban" / "boards"
    skip = {"default", "archive", "archived"}
    try:
        boards = sorted([
            d.name for d in boards_dir.iterdir()
            if d.is_dir() and d.name not in skip and (d / "kanban.db").exists()
        ])
    except Exception:
        boards = ["admin", "plebeian", "tollgate", "market"]
    return boards if boards else ["admin", "plebeian", "tollgate", "market"]


def parse_task_line(line):
    """Parse a kanban ls output line into structured data."""
    # Format: ✓ t_abc123  done      profile-name      Task title
    #         ▶ t_def456  ready     (unassigned)      Task title
    #         ⊘ t_ghi789  blocked   profile-name      Task title
    #         ◻ t_jkl012  todo      profile-name      Task title
    m = re.match(
        r"[▶✓⊘◻●]\s+(t_\w+)\s+(\w+)\s+(\(?[\w-]+\)?)\s+(.*)",
        line,
    )
    if m:
        tid = m.group(1)
        status = m.group(2)
        assignee = m.group(3)
        if assignee == "(unassigned)":
            assignee = ""
        title = m.group(4).strip()
        return {"id": tid, "status": status, "assignee": assignee, "title": title}
    return None


def scan_board(board):
    """Scan a single board for tasks."""
    out, rc = run_hermes(f"hermes kanban --board {board} ls 2>/dev/null")
    if rc != 0 or not out:
        return []
    tasks = []
    for line in out.split("\n"):
        parsed = parse_task_line(line)
        if parsed:
            parsed["board"] = board
            tasks.append(parsed)
    return tasks


def get_profile_status():
    """Get profile status from assignees output."""
    out, rc = run_hermes("hermes kanban --board admin assignees 2>/dev/null")
    if rc != 0:
        return {}
    profiles = {}
    for line in out.split("\n"):
        line = line.strip()
        if not line or line.startswith("NAME"):
            continue
        m = re.match(r"(\S+)\s+(\w+)\s+\((.+)\)", line)
        if not m:
            # Fallback: try no parens (header-like or simple format)
            m = re.match(r"(\S+)\s+(\w+)\s*$", line)
        if m:
            name = m.group(1)
            if name == "NAME":
                continue
            disk_state = m.group(2)  # "yes" or "no"
            counts_str = m.group(3) if len(m.groups()) >= 3 else ""
            is_running = "running" in counts_str if counts_str else False
            is_idle = "idle" in counts_str if counts_str else False
            profiles[name] = {
                "on_disk": disk_state == "yes",
                "running": is_running,
                "idle": is_idle,
            }
    return profiles


# Board → preferred worker profile mapping
BOARD_PROFILE_MAP = {
    "plebeian": "worker-plebeian",
    "tollgate": "worker-tollgate",
    "admin": "worker-admin",
    "market": "worker-plebeian",
    "fips": "worker-admin",
}

# Worker profile → description (for reporting)
WORKER_DESCRIPTIONS = {
    "worker-plebeian": "Plebeian Market tasks (React, NDK, e2e, CI)",
    "worker-tollgate": "TollGate/IoT tasks (ESP32, RP2040, LoRa, firmware)",
    "worker-admin": "Admin/ops tasks (Hermes, proxy, kanban, monitoring)",
    "worker-base": "General fallback worker (any task type)",
}


def recommend_profile(board, title, task_id):
    """Recommend the best worker profile for a task."""
    # Board-based routing is the primary strategy
    preferred = BOARD_PROFILE_MAP.get(board, "worker-base")

    # Check for firmware/hardware keywords to route to tollgate
    fw_keywords = [
        "esp32", "rp2040", "lora", "firmware", "balloon", "tollgate",
        "spi", "dma", "pio", "flrc", "meshcore", "sx1280", "radio",
        "uart", "serial", "i2c", "gps", "nmea",
    ]
    market_keywords = [
        "market", "plebeian", "nostr", "nip", "applesauce", "ndk",
        "e2e", "test", "ci", "pr", "ui", "react", "typescript",
    ]
    admin_keywords = [
        "hermes", "proxy", "kanban", "gateway", "cron", "ngit",
        "deploy", "monitor", "ctx", "backup", "ansible",
    ]

    title_lower = title.lower() + " " + task_id.lower()

    # Count keyword matches
    fw_score = sum(1 for kw in fw_keywords if kw in title_lower)
    market_score = sum(1 for kw in market_keywords if kw in title_lower)
    admin_score = sum(1 for kw in admin_keywords if kw in title_lower)

    # Board preference is a tiebreaker
    scores = {
        "worker-tollgate": fw_score * 3 + (1 if board == "tollgate" else 0),
        "worker-plebeian": market_score * 3 + (1 if board == "plebeian" else 0),
        "worker-admin": admin_score * 3 + (1 if board == "admin" else 0),
        "worker-base": 0,
    }

    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "worker-base"


def assign_task(board, task_id, profile, dry_run=False):
    """Assign a task to a profile."""
    if dry_run:
        return True, f"WOULD assign {task_id} on {board} → {profile}"
    out, rc = run_hermes(
        f"hermes kanban --board {board} reassign {task_id} {profile} 2>&1",
        timeout=10,
    )
    success = rc == 0 and "error" not in out.lower()
    return success, out.strip() if not success else f"Assigned {task_id} → {profile}"


def main():
    parser = argparse.ArgumentParser(description="Kanban auto-assigner")
    parser.add_argument("--auto", action="store_true", help="Actually assign tasks")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be assigned"
    )
    parser.add_argument(
        "--min-idle-hours", type=float, default=1.0, help="Minimum idle age threshold"
    )
    parser.add_argument(
        "--board", type=str, default="", help="Only process this board"
    )
    args = parser.parse_args()

    # Scan all boards
    boards = [args.board] if args.board else get_all_boards()
    all_tasks = []
    for board in boards:
        all_tasks.extend(scan_board(board))

    # Filter to ready+unassigned
    ready_unassigned = [
        t
        for t in all_tasks
        if t["status"] == "ready" and not t["assignee"]
    ]

    # Get profile status
    profiles = get_profile_status()
    running_profiles = {
        name: info
        for name, info in profiles.items()
        if not info.get("idle") and name.startswith("worker-")
    }

    # Read Kalman-smoothed pool size from the daemon's state
    pool_smoothed = None
    pool_cap = len(profiles)  # default: all profiles
    pool_state_path = os.path.expanduser("~/.hermes/state/pool_kalman.json")
    try:
        if os.path.exists(pool_state_path):
            with open(pool_state_path) as f:
                ps = json.load(f)
            pool_smoothed = int(round(ps["x"][0]))
            pool_velocity = ps["x"][1]
            # Pool cap = smoothed workers, but at least 1 and at most all profiles
            pool_cap = max(1, min(pool_smoothed, len(profiles)))
    except (KeyError, ValueError, json.JSONDecodeError):
        pass

    # How many additional workers can we assign?
    running_total = len(running_profiles)
    remaining_slots = max(0, pool_cap - running_total)

    idle_profiles = {
        name: info
        for name, info in profiles.items()
        if info.get("idle") and name.startswith("worker-")
    }

    if not ready_unassigned:
        print("NO_ACTION: no ready+unassigned tasks found")
        return

    # Print structured output
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"=== Auto-Assigner Scan: {now} ===")
    print(f"Ready+unassigned: {len(ready_unassigned)}")
    print(f"Available workers (idle): {len(idle_profiles)} ({', '.join(idle_profiles.keys())})")
    if running_profiles:
        print(f"Busy workers: {len(running_profiles)} ({', '.join(running_profiles.keys())})")
    if pool_smoothed:
        print(f"Pool Kalman: smoothed={pool_smoothed}, running={running_total}, "
              f"remaining_slots={remaining_slots}")
    print()

    # Assignments
    assigned = 0
    skipped_no_worker = 0

    for task in ready_unassigned:
        board = task["board"]
        preferred = BOARD_PROFILE_MAP.get(board, "worker-base")
        recommended = recommend_profile(board, task["title"], task["id"])

        # Check if preferred profile is idle
        if preferred in idle_profiles:
            target = preferred
        elif recommended in idle_profiles:
            target = recommended
        elif "worker-base" in idle_profiles:
            target = "worker-base"
        else:
            target = None

        idle_str = ""
        if task.get("board"):
            pass  # already available

        if target and args.auto:
            success, msg = assign_task(board, task["id"], target, args.dry_run)
            prefix = "[DRY-RUN]" if args.dry_run else "[ASSIGNED]"
            print(f"{prefix} {board}/{task['id']}: {task['title']}")
            print(f"       recommended={recommended} → assigned={target}")
            if not args.dry_run:
                idle_profiles.pop(target, None)  # consume it
                remaining_slots -= 1
            assigned += 1
            # Stop assigning if we've filled the pool
            if remaining_slots <= 0:
                print(f"       (pool at capacity — {pool_cap} workers)")
                break
        elif target:
            print(f"[SUGGEST] {board}/{task['id']}: {task['title']}")
            print(f"          recommended={recommended}, available={target}")
            print(f"          → hermes kanban --board {board} reassign {task['id']} {target}")
            assigned += 1
        else:
            print(f"[STALLED] {board}/{task['id']}: {task['title']}")
            print(f"          recommended={recommended}, but ALL workers busy")
            skipped_no_worker += 1

    print()
    summary_parts = []
    if args.auto:
        action = "dry-run" if args.dry_run else "assigned"
        summary_parts.append(f"{assigned} {action}")
    else:
        summary_parts.append(f"{assigned} suggestions")
    if skipped_no_worker:
        summary_parts.append(f"{skipped_no_worker} skipped (no free workers)")
    print(f"Summary: {', '.join(summary_parts)}")


if __name__ == "__main__":
    main()
