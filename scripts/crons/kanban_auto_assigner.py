#!/usr/bin/env python3
"""kanban_auto_assigner.py — CANONICAL auto-assigner (Phase 1, t_bd4d83c1).

Consolidates three divergent copies that previously lived at:
  * ~/.hermes/profiles/manager/scripts/kanban_auto_assigner.py (849 lines)
  * ~/.hermes/scripts/kanban_auto_assigner.py                     (382 lines)
  * ~/.hermes/bot/scripts/crons/kanban_auto_assigner.py           (339 lines)

Routing decisions now DELEGATE to the single canonical module offload_router.py
(same directory): classify() -> probe_targets() -> route(). No divergent
keyword/kalman copies anywhere. This file keeps only the CLI/board/scan glue.

Behavior:
  * Scans every board DB directly for ready+unassigned tasks (fast path,
    no per-board CLI spawns — avoids 86+ subprocess under load).
  * For each task: classify via offload_router. Heavy + hardware -> local
    board worker; heavy + target green + local stressed -> worker-dq05
    (board-preferred-over-stress FIX, scope item e); else board-preferred
    profile -> keyword local route -> worker-base -> any idle worker.
  * Honours per-board routing.json {"offload":"off|auto|force"} escape
    hatches via offload_router.load_board_rule.
  * Idempotent: only touches ready+unassigned; consumes an idle worker per
    assignment; respects Kalman-smoothed pool cap.

Externally-required module API (used by kanban-assigner-gate.py):
    scan_all_boards_fast(), get_busy_profiles(), get_profile_status(),
    assign_task(), recommend_profile(), main().

Run:
    python3 kanban_auto_assigner.py [--auto] [--dry-run] [--board <b>]
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

try:
    import offload_router
except ImportError:  # canonical module missing -> degrade to local-only routing
    offload_router = None

# Board → preferred worker profile mapping
BOARD_PROFILE_MAP = {
    "plebeian": "worker-plebeian",
    "tollgate": "worker-tollgate",
    "admin": "worker-admin",
    "market": "worker-plebeian",
    "fips": "worker-admin",
    "vps-infra": "worker-admin",
}

# Worker profile → description (for reporting)
WORKER_DESCRIPTIONS = {
    "worker-plebeian": "Plebeian Market tasks (React, NDK, e2e, CI)",
    "worker-tollgate": "TollGate/IoT tasks (ESP32, RP2040, LoRa, firmware)",
    "worker-admin": "Admin/ops tasks (Hermes, proxy, kanban, monitoring)",
    "worker-base": "General fallback worker (any task type)",
    "worker-dq05": "Remote-compute worker (SSH delegation to DQ05)",
}


def run_hermes(cmd, timeout=15):
    """Run a hermes CLI command and return (stdout, returncode)."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, shell=True
        )
        return r.stdout, r.returncode
    except subprocess.TimeoutExpired:
        return "", -1


def get_all_boards():
    """Discover all boards from ~/.hermes/kanban/boards/*/kanban.db."""
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


def scan_all_boards_fast():
    """Direct-SQLite scan of every board. Returns list of task dicts with
    keys id, title, body, board, status, assignee, priority. ~100x faster
    than one `hermes kanban ls` per board."""
    boards_dir = Path.home() / ".hermes" / "kanban" / "boards"
    tasks = []
    if not boards_dir.exists():
        return tasks
    for db_path in sorted(boards_dir.glob("*/kanban.db")):
        board = db_path.parent.name
        if board in ("default", "archive", "archived"):
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT id, title, body, status, assignee, priority "
                "FROM tasks"
            ).fetchall()
            for tid, title, body, status, assignee, priority in rows:
                tasks.append({
                    "id": tid, "title": title or "", "body": body or "",
                    "board": board, "status": status,
                    "assignee": assignee or "", "priority": priority,
                })
            conn.close()
        except Exception:
            continue
    return tasks


def get_busy_profiles():
    """Scan ALL board DBs for tasks in 'running' status.

    Definitive source of truth for profile availability (unlike per-board
    assignees output which only reflects one board).
    """
    busy = set()
    boards_dir = Path.home() / ".hermes" / "kanban" / "boards"
    if not boards_dir.exists():
        return busy
    for db_path in boards_dir.glob("*/kanban.db"):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            for row in conn.execute(
                "SELECT DISTINCT assignee FROM tasks WHERE status='running'"
            ).fetchall():
                if row[0]:
                    busy.add(row[0])
            conn.close()
        except Exception:
            continue
    return busy


def get_profile_status():
    """Get profile status from `hermes kanban --board admin assignees`."""
    out, rc = run_hermes("hermes kanban --board admin assignees 2>/dev/null")
    if rc != 0:
        return {}
    profiles = {}
    for line in out.split("\n"):
        line = line.strip()
        if not line or line.startswith("NAME"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            name = parts[0]
            disk_state = parts[1]  # "yes" / "no"
            rest = " ".join(parts[2:])
            profiles[name] = {
                "on_disk": disk_state == "yes",
                "running": "running" in rest,
                "idle": "idle" in rest,
            }
    return profiles


def _pool_cap(profiles):
    """Kalman-smoothed worker pool cap (mirrors daemon state)."""
    pool_state_path = os.path.expanduser("~/.hermes/state/pool_kalman.json")
    try:
        if os.path.exists(pool_state_path):
            with open(pool_state_path) as f:
                ps = json.load(f)
            smoothed = int(round(ps["x"][0]))
            return max(1, min(smoothed, len(profiles)))
    except (KeyError, ValueError, json.JSONDecodeError):
        pass
    return len(profiles)


def _keyword_route(board, title):
    """Local board/keyword profile routing (never DQ05)."""
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
    title_lower = title.lower()
    fw_score = sum(1 for kw in fw_keywords if kw in title_lower)
    market_score = sum(1 for kw in market_keywords if kw in title_lower)
    admin_score = sum(1 for kw in admin_keywords if kw in title_lower)
    scores = {
        "worker-tollgate": fw_score * 3 + (1 if board == "tollgate" else 0),
        "worker-plebeian": market_score * 3 + (1 if board == "plebeian" else 0),
        "worker-admin": admin_score * 3 + (1 if board == "admin" else 0),
        "worker-base": 0,
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "worker-base"


def recommend_profile(board, title, task_id="", body="", idle_profiles=None,
                      probe_facts=None, local_load_per_core=None):
    """Recommend the best worker profile for a task.

    Delegates the OFFLOAD decision to canonical offload_router.route():
        hardware/off-board/light -> local
        heavy + green target + (auto: local stressed | force) -> worker-dq05
        else local board/keyword route.
    When offload_router is unavailable (degraded install), falls back to
    board-preferred/keyword routing ONLY (no remote dispatch).
    """
    preferred = BOARD_PROFILE_MAP.get(board, "worker-base")

    if offload_router is not None:
        try:
            facts = probe_facts
            if facts is None:
                facts = offload_router.probe.probe_targets(cache_ttl=30)
            if local_load_per_core is None:
                local_load_per_core = offload_router._local_load_per_core()
            route_result = offload_router.route(
                board, title, body,
                board_rule=offload_router.load_board_rule(board),
                facts=facts,
                local_load_per_core=local_load_per_core,
            )
            if idle_profiles is not None:
                # (e) FIX board-preferred-over-stress: offload verdict beats an
                # idle board-preferred profile whenever DQ05 is green.
                target, why = offload_router.pick_assignment_target(
                    route_result, board_preferred=preferred,
                    idle_profiles=idle_profiles)
                if target is not None and target != preferred:
                    return target, f"{why} (offload_router)"
                if target == preferred:
                    return target, why
            if route_result["decision"] == "offload":
                return (route_result["profile"],
                        f"offload_route ({route_result['reason']})")
        except Exception:
            pass  # probe/routing error -> fall through to local keyword route

    local = _keyword_route(board, title)
    return local, f"keyword_route (board={board})"


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
    parser = argparse.ArgumentParser(description="Kanban auto-assigner (canonical)")
    parser.add_argument("--auto", action="store_true", help="Actually assign tasks")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be assigned")
    parser.add_argument("--min-idle-hours", type=float, default=1.0,
                        help="Minimum idle age threshold (kept for CLI compat)")
    parser.add_argument("--board", type=str, default="",
                        help="Only process this board")
    args = parser.parse_args()

    all_tasks = scan_all_boards_fast()
    if args.board:
        all_tasks = [t for t in all_tasks if t["board"] == args.board]

    ready_unassigned = [
        t for t in all_tasks
        if t["status"] == "ready" and not t["assignee"]
    ]

    profiles = get_profile_status()
    busy_profiles_global = get_busy_profiles()
    running_total = len([n for n in busy_profiles_global
                         if n.startswith("worker-")])
    pool_cap = _pool_cap(profiles)
    remaining_slots = max(0, pool_cap - running_total)

    idle_profiles = {
        name: info
        for name, info in profiles.items()
        if name not in busy_profiles_global
        and name.startswith("worker-")
        and info.get("on_disk", False)
    }

    if not ready_unassigned:
        print("NO_ACTION: no ready+unassigned tasks found")
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"=== Auto-Assigner Scan: {now} ===")
    print(f"Ready+unassigned: {len(ready_unassigned)}")
    print(f"Available workers (idle): {len(idle_profiles)} "
          f"({', '.join(idle_profiles.keys())})")
    if running_total:
        print(f"Busy workers: {running_total}")
    print(f"Pool cap: {pool_cap}, remaining_slots={remaining_slots}")
    print()

    # Probe the remote pool ONCE for the whole pass (30s TTL cache).
    probe_facts = None
    local_load_per_core = None
    if offload_router is not None:
        try:
            probe_facts = offload_router.probe.probe_targets(cache_ttl=30)
            local_load_per_core = offload_router._local_load_per_core()
        except Exception:
            probe_facts = None

    assigned = 0
    skipped_no_worker = 0

    for task in ready_unassigned:
        board = task["board"]
        preferred = BOARD_PROFILE_MAP.get(board, "worker-base")
        recommended, rec_why = recommend_profile(
            board, task["title"], task["id"], task.get("body", ""),
            idle_profiles=set(idle_profiles.keys()),
            probe_facts=probe_facts,
            local_load_per_core=local_load_per_core,
        )

        if recommended in idle_profiles:
            target = recommended
        elif preferred in idle_profiles:
            target = preferred
        elif "worker-base" in idle_profiles:
            target = "worker-base"
        elif idle_profiles:
            target = sorted(idle_profiles.keys())[0]
        else:
            target = None

        if target and args.auto:
            success, msg = assign_task(board, task["id"], target, args.dry_run)
            prefix = "[DRY-RUN]" if args.dry_run else "[ASSIGNED]"
            print(f"{prefix} {board}/{task['id']}: {task['title']}")
            print(f"       recommended={recommended} ({rec_why}) "
                  f"→ assigned={target}")
            if not args.dry_run and success:
                idle_profiles.pop(target, None)
                remaining_slots -= 1
            assigned += 1
            if remaining_slots <= 0:
                print(f"       (pool at capacity — {pool_cap} workers)")
                break
        elif target:
            print(f"[SUGGEST] {board}/{task['id']}: {task['title']}")
            print(f"          recommended={recommended} ({rec_why}), "
                  f"available={target}")
            print(f"          → hermes kanban --board {board} reassign "
                  f"{task['id']} {target}")
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
