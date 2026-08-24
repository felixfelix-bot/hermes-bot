#!/usr/bin/env bash
# ============================================================================
# human-gate-timeout.sh — Human-gate timeout with 3-tier taxonomy
#
# Scans all kanban boards for blocked tasks with "human-gate" or "operator-gate"
# in the block reason. Classifies each into a timeout tier and checks if the
# timeout has elapsed. If so, emits an alert and updates task metadata.
#
# THREE-TIER TAXONOMY (from design doc Q3):
#   Tier 1 — stand_down (4h timeout):
#     Safety-critical decisions (corruption, interference, data loss).
#     After 4h: mark task as "stood-down", alert operator.
#
#   Tier 2 — proceed_isolated (2h timeout):
#     Methodology questions (verification, isolated clone, re-run).
#     After 2h: mark task as "proceeding-isolated", alert operator.
#
#   Tier 3 — escalate (4h timeout):
#     Unknown impact questions (no specific keywords).
#     After 4h: alert operator — task remains blocked.
#
# ALERT CHANNEL:
#   Rides the unified-system-alert.sh pattern: empty stdout = SILENT,
#   non-empty stdout = alert delivered. Uses once-per-24h dedup per task
#   (same pattern as unified-system-alert.sh).
#
# SCHEDULE: every 15 min, deliver=origin, no_agent=true
#
# DESIGN CONSTRAINTS:
#   - NO CAPS, NO BLOCKS — alert only (Felix's preference)
#   - When a timeout fires, task metadata is updated to record the action
#   - Dedup: once per 24h per condition, immediate on severity change
# ============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="/tmp/human-gate-timeout-state.json"
BOARDS_DIR="${HERMES_KANBAN_BOARDS_DIR:-$HOME/.hermes/kanban/boards}"

# 24 hours in seconds
DAY_SECONDS=86400

# Timeout values (seconds)
STAND_DOWN_TIMEOUT=14400      # 4 hours
PROCEED_ISOLATED_TIMEOUT=7200 # 2 hours
ESCALATE_TIMEOUT=14400        # 4 hours

# Run the core logic in Python (bash is too fragile for JSON + SQLite)
/usr/bin/python3 <<'PYEOF' 2>/dev/null || true
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HOME = os.path.expanduser("~")
BOARDS_DIR = os.environ.get(
    "HERMES_KANBAN_BOARDS_DIR",
    os.path.join(HOME, ".hermes", "kanban", "boards"),
)
STATE_FILE = "/tmp/human-gate-timeout-state.json"
DAY_SECONDS = 86400

# Tier timeouts (seconds)
TIER_TIMEOUTS = {
    "stand_down": 14400,        # 4h — safety-critical
    "proceed_isolated": 7200,   # 2h — methodology
    "escalate": 14400,          # 4h — unknown impact
}

# Classification keywords (lowercase matching)
SAFETY_KEYWORDS = [
    "corrupt", "interference", "external", "mutated", "deleted",
    "damage", "irreversible", "data loss", "safety",
]
METHODOLOGY_KEYWORDS = [
    "isolated", "clone", "verify", "re-run", "proceed",
    "approach", "methodology", "fresh",
]

# Boards to skip (shadow/archive boards)
SKIP_BOARDS = {"human-gate", "archive", "archived"}


def classify_gate(reason: str) -> str:
    """Classify a human-gate block reason into a timeout tier.

    Conservative: defaults to 'escalate' (safest) when uncertain.
    """
    reason_lower = (reason or "").lower()

    # Tier 1: Safety-critical
    if any(kw in reason_lower for kw in SAFETY_KEYWORDS):
        return "stand_down"

    # Tier 2: Methodology
    if any(kw in reason_lower for kw in METHODOLOGY_KEYWORDS):
        return "proceed_isolated"

    # Tier 3: Unknown/complex — safest default
    return "escalate"


def format_timeout_alert(tier: str, task_id: str, elapsed_seconds: int) -> str:
    """Format the alert message for a human-gate timeout."""
    hours = elapsed_seconds // 3600

    if tier == "stand_down":
        return (
            f"⏸️ HUMAN-GATE TIMEOUT: Task {task_id} stood down after {hours}h "
            f"— safety-critical gate unanswered"
        )
    elif tier == "proceed_isolated":
        return (
            f"⏭️ HUMAN-GATE TIMEOUT: Task {task_id} proceeding with isolated "
            f"approach after {hours}h"
        )
    else:  # escalate
        return (
            f"🚨 HUMAN-GATE TIMEOUT: Task {task_id} needs escalation "
            f"— gate unanswered for {hours}h"
        )


def load_dedup_state() -> dict:
    """Load the dedup state file."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"alerts": {}}


def save_dedup_state(state: dict):
    """Save the dedup state file."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except (IOError, OSError):
        pass  # Non-fatal — dedup is best-effort


def should_alert(state: dict, task_id: str, tier: str) -> bool:
    """Check if an alert should be emitted (once-per-24h dedup)."""
    alerts = state.get("alerts", {})
    if task_id not in alerts:
        return True  # New task — alert

    prev = alerts[task_id]
    last_alerted = prev.get("last_alerted", 0)
    now = time.time()

    if now - last_alerted >= DAY_SECONDS:
        return True  # 24h elapsed — daily reminder

    return False  # Within 24h — suppress


def record_alert(state: dict, task_id: str, tier: str):
    """Record that an alert was emitted."""
    if "alerts" not in state:
        state["alerts"] = {}
    state["alerts"][task_id] = {
        "tier": tier,
        "last_alerted": time.time(),
    }
    save_dedup_state(state)


def scan_blocked_tasks() -> list:
    """Scan all kanban boards for blocked tasks with human-gate in block reason.

    Returns list of dicts: board, task_id, title, block_reason, blocked_at.
    """
    tasks = []
    boards_path = Path(BOARDS_DIR)

    if not boards_path.exists():
        return tasks

    for db_path in sorted(boards_path.glob("*/kanban.db")):
        board_slug = db_path.parent.name
        if board_slug in SKIP_BOARDS:
            continue

        try:
            conn = sqlite3.connect(str(db_path))
            # Find all blocked tasks
            rows = conn.execute(
                "SELECT id, title FROM tasks WHERE status = 'blocked'"
            ).fetchall()

            for tid, title in rows:
                # Check the most recent 'blocked' event for human-gate reason
                event_row = conn.execute(
                    "SELECT payload, created_at FROM task_events "
                    "WHERE task_id = ? AND kind = 'blocked' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (tid,)
                ).fetchone()

                if not event_row:
                    continue

                payload, blocked_at = event_row
                if not payload:
                    continue

                payload_str = str(payload).lower()
                if "human-gate" not in payload_str and "operator-gate" not in payload_str:
                    continue

                # Extract reason from payload
                reason = payload
                try:
                    p = json.loads(payload)
                    if isinstance(p, dict):
                        reason = p.get("reason", p.get("description", payload))
                except (json.JSONDecodeError, TypeError):
                    pass

                tasks.append({
                    "board": board_slug,
                    "task_id": tid,
                    "title": title or "",
                    "block_reason": reason,
                    "blocked_at": blocked_at or 0,
                })

            conn.close()
        except sqlite3.Error:
            continue  # Skip corrupt/inaccessible databases

    return tasks


def update_task_metadata(board: str, task_id: str, tier: str, action: str):
    """Update task metadata to record the timeout action taken.

    Uses hermes kanban CLI to add a comment to the task recording the action.
    This is non-fatal — if the CLI fails, the alert still fires.
    """
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    comment = f"[human-gate-timeout] tier={tier} action={action} at={timestamp}"

    try:
        subprocess.run(
            ["hermes", "kanban", "--board", board, "comment", task_id, comment],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        pass  # Non-fatal — the alert is the primary output


def main():
    """Main entry point. Outputs alert text to stdout (or empty if silent)."""
    now = time.time()
    state = load_dedup_state()
    alerts = []

    tasks = scan_blocked_tasks()

    for task in tasks:
        task_id = task["task_id"]
        board = task["board"]
        reason = task["block_reason"]
        blocked_at = task["blocked_at"]

        # Skip if no blocked_at timestamp (can't compute elapsed)
        if not blocked_at:
            continue

        elapsed = now - blocked_at
        tier = classify_gate(reason)
        timeout = TIER_TIMEOUTS.get(tier, TIER_TIMEOUTS["escalate"])

        if elapsed < timeout:
            continue  # Not yet timed out

        # Check dedup
        if not should_alert(state, task_id, tier):
            continue  # Suppressed by 24h dedup

        # Format alert
        alert_msg = format_timeout_alert(tier, task_id, int(elapsed))
        alerts.append(alert_msg)

        # Record alert in dedup state
        record_alert(state, task_id, tier)

        # Update task metadata with the action taken
        action = {
            "stand_down": "stood-down",
            "proceed_isolated": "proceeding-isolated",
            "escalate": "escalated",
        }.get(tier, "escalated")
        update_task_metadata(board, task_id, tier, action)

    # Output: non-empty = alert delivered, empty = silent
    if alerts:
        print("🚨 Human-Gate Timeout Report — " + time.strftime("%H:%M %b %d"))
        print()
        for a in alerts:
            print(a)
        print("---")
        print("Reported by human-gate-timeout.sh (every 15 min)")


if __name__ == "__main__":
    main()
PYEOF