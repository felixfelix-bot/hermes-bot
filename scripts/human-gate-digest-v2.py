#!/usr/bin/env python3
"""
human-gate-digest-v2.py — ICS-formatted Liaison digest.

THE single funnel for human notifications. Collects from:
1. Human-gate board (blocked tasks requiring decisions)
2. Anomaly buffer (system issues)
3. Kanban board summaries (activity counts)
4. Recently resolved items

Formats everything in ICS message format:
  [STATUS] [PROJECT] task_id — Brief description

Silent when healthy. Only outputs when there are NEW items requiring attention.
"""
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

HOME = os.path.expanduser("~")
BOARDS_DIR = os.path.join(HOME, ".hermes", "kanban", "boards")
STATE_FILE = os.path.join(HOME, ".hermes", "state", "human-gate-digest-v2.json")

# Map board slugs to ICS project codes
PROJECT_CODES = {
    "tollgate": "TOLLGATE",
    "tollgate-software": "TOLLGATE",
    "tollgate-module-basic-go": "TOLLGATE",
    "tollgate-rs": "TOLLGATE",
    "fips": "FIPS",
    "fips2": "FIPS",
    "net4sats-mvp": "NET4SATS",
    "net4sats-mvp-v2": "NET4SATS",
    "plebeian": "PLEBEIAN",
    "infrastructure": "INFRA",
    "esp32-balloon-integration": "BALLOON",
    "esp32-tollgate": "BALLOON",
    "sovereign-shops": "SOVEREIGN",
    "human-gate": "HUMAN-GATE",
    "hermes-orchestration": "INFRA",
}


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"seen_ids": set(), "last_digest": 0, "last_reminder": 0}
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
            return {
                "seen_ids": set(data.get("seen_ids", [])),
                "last_digest": data.get("last_digest", 0),
                "last_reminder": data.get("last_reminder", 0),
            }
    except (json.JSONDecodeError, OSError):
        return {"seen_ids": set(), "last_digest": 0, "last_reminder": 0}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(
            {
                "seen_ids": sorted(state["seen_ids"]),
                "last_digest": state["last_digest"],
                "last_reminder": state["last_reminder"],
            },
            f,
            indent=2,
        )


def get_project_code(board_slug):
    """Map board slug to ICS project code."""
    if board_slug in PROJECT_CODES:
        return PROJECT_CODES[board_slug]
    # Try partial match
    for key, code in PROJECT_CODES.items():
        if key in board_slug or board_slug in key:
            return code
    return board_slug.upper()[:12]


def parse_source_board(body):
    """Extract source board from shadow task body."""
    if not body:
        return "?"
    m = re.search(r'[Ss]ource[_\s][Bb]oard["\s:]+([\w-]+)', body)
    if m:
        return m.group(1)
    return "?"


def collect_human_gate_items():
    """Read pending items from human-gate board."""
    db_path = os.path.join(BOARDS_DIR, "human-gate", "kanban.db")
    if not os.path.exists(db_path):
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    items = []
    try:
        rows = conn.execute(
            """
            SELECT id, title, body, status, created_at
            FROM tasks
            WHERE status NOT IN ('done', 'archived')
            ORDER BY created_at ASC
            """
        ).fetchall()
        for row in rows:
            board = parse_source_board(row["body"])
            project = get_project_code(board)
            # Determine status code
            status_code = "BLOCKER"
            title = row["title"] or "untitled"
            if "approve" in title.lower() or "approval" in title.lower():
                status_code = "APPROVE"

            items.append(
                {
                    "id": row["id"],
                    "status_code": status_code,
                    "project": project,
                    "title": title[:80],
                    "board": board,
                }
            )
    finally:
        conn.close()
    return items


def collect_anomalies():
    """Check anomaly-notify output for critical system issues."""
    anomaly_file = os.path.join(HOME, ".hermes", "state", "anomaly-current.json")
    if not os.path.exists(anomaly_file):
        return []
    try:
        with open(anomaly_file) as f:
            data = json.load(f)
        anomalies = []
        for a in data if isinstance(data, list) else [data]:
            anomalies.append(
                {
                    "id": f"anomaly_{a.get('check', 'unknown')}",
                    "status_code": "CRITICAL",
                    "project": "INFRA",
                    "title": a.get("message", a.get("check", "system issue"))[:80],
                    "board": "infrastructure",
                }
            )
        return anomalies
    except (json.JSONDecodeError, OSError):
        return []


def collect_board_summaries():
    """Quick activity summary across main project boards."""
    summaries = []
    main_boards = [
        "tollgate",
        "fips",
        "net4sats-mvp-v2",
        "plebeian",
        "infrastructure",
        "sovereign-shops",
    ]
    for board in main_boards:
        db_path = os.path.join(BOARDS_DIR, board, "kanban.db")
        if not os.path.exists(db_path):
            continue
        try:
            conn = sqlite3.connect(db_path)
            counts = {}
            for status in ["blocked", "todo", "ready", "in_progress", "done"]:
                row = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status = ?", (status,)
                ).fetchone()
                counts[status] = row[0] if row else 0
            conn.close()

            project = get_project_code(board)
            blocked = counts.get("blocked", 0)
            ready = counts.get("ready", 0) + counts.get("in_progress", 0)
            if blocked > 0:
                summaries.append(
                    {
                        "project": project,
                        "board": board,
                        "blocked": blocked,
                        "ready": ready,
                    }
                )
        except sqlite3.Error:
            continue
    return summaries


def format_ics_digest(new_items, all_items, anomalies, summaries, state):
    """Format all collected data into ICS-style digest."""
    lines = []
    now = int(time.time())
    hours_since_reminder = (now - state["last_reminder"]) / 3600

    # Section 1: CRITICAL anomalies (always shown first)
    if anomalies:
        lines.append("🔴 CRITICAL:")
        for a in anomalies:
            lines.append(f"  [CRITICAL] [{a['project']}] {a['title']}")

    # Section 2: NEW blocked items requiring decisions
    if new_items:
        lines.append("")
        lines.append(f"⛔ {len(new_items)} NEW item(s) need attention:")
        for item in new_items:
            lines.append(
                f"  [{item['status_code']}] [{item['project']}] {item['id']} — {item['title']}"
            )

    # Section 3: Board health summary (every digest)
    if summaries:
        lines.append("")
        lines.append("📊 Board health:")
        for s in summaries:
            status_icon = "🔴" if s["blocked"] > 3 else ("🟡" if s["blocked"] > 0 else "🟢")
            lines.append(
                f"  {status_icon} [{s['project']}] {s['blocked']} blocked, {s['ready']} active"
            )

    # Section 4: Daily stale reminder (once per 24h)
    if hours_since_reminder >= 24 and all_items and not new_items:
        lines.append("")
        lines.append(
            f"⏰ {len(all_items)} item(s) still pending on human-gate board"
        )

    return "\n".join(lines).strip() if lines else None


def main():
    state = load_state()
    now = int(time.time())

    # Collect from all sources
    all_items = collect_human_gate_items()
    anomalies = collect_anomalies()
    summaries = collect_board_summaries()

    # Determine NEW items (not previously seen)
    current_ids = {item["id"] for item in all_items}
    new_items = [item for item in all_items if item["id"] not in state["seen_ids"]]

    # Format the digest
    output = format_ics_digest(new_items, all_items, anomalies, summaries, state)

    # Update state
    state["seen_ids"] = current_ids | state["seen_ids"]
    # Clean up resolved items from seen set
    state["seen_ids"] = {sid for sid in state["seen_ids"] if sid in current_ids}
    state["last_digest"] = now
    if output:
        state["last_reminder"] = now
    save_state(state)

    if output:
        print(output)
    # Silent if nothing to report — exit 0 with no output
    sys.exit(0)


if __name__ == "__main__":
    main()
