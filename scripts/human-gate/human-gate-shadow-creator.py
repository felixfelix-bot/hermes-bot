#!/usr/bin/env python3
"""
human-gate-shadow-creator.py — Auto-create shadow tasks on human-gate board.

Scans ALL boards for blocked tasks with "human-gate" in the block reason.
Creates a shadow task on the human-gate board if one doesn't already exist.
Outputs alerts for NEW shadows only (stdout = new items, silent otherwise).

Run as: hermes cron --no-agent --script scripts/human-gate-shadow-creator.py
"""
import glob
import hashlib
import json
import os
import re
import sqlite3
import sys

HOME = os.path.expanduser("~")
BOARDS_DIR = os.path.join(HOME, ".hermes", "kanban", "boards")
HUMAN_GATE_DB = os.path.join(BOARDS_DIR, "human-gate", "kanban.db")
NOTIFIED_STATE = os.path.join(HOME, ".hermes", "state", "human-gate-notified.json")


def load_notified() -> set[str]:
    """Load previously notified shadow task IDs."""
    if not os.path.exists(NOTIFIED_STATE):
        return set()
    try:
        with open(NOTIFIED_STATE) as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError):
        return set()


def save_notified(ids: set[str]) -> None:
    """Save notified shadow task IDs."""
    os.makedirs(os.path.dirname(NOTIFIED_STATE), exist_ok=True)
    with open(NOTIFIED_STATE, "w") as f:
        json.dump(sorted(ids), f, indent=2)


def load_existing_bodies() -> set[str]:
    """Load all existing shadow task bodies from the human-gate board."""
    bodies = set()
    if not os.path.exists(HUMAN_GATE_DB):
        return bodies
    try:
        conn = sqlite3.connect(HUMAN_GATE_DB)
        for (body,) in conn.execute(
            "SELECT body FROM tasks WHERE body IS NOT NULL"
        ).fetchall():
            bodies.add(body)
        conn.close()
    except Exception:
        pass
    return bodies


def shadow_exists_in_db(board_slug: str, task_id: str) -> bool:
    """Check if a shadow for this board/task already exists in the DB."""
    if not os.path.exists(HUMAN_GATE_DB):
        return False
    try:
        conn = sqlite3.connect(HUMAN_GATE_DB)
        rows = conn.execute(
            "SELECT 1 FROM tasks WHERE body LIKE ?",
            (f"%{board_slug}%{task_id}%",),
        ).fetchall()
        conn.close()
        return len(rows) > 0
    except Exception:
        return False


def create_shadow(board_slug: str, task_id: str, reason: str, title: str) -> str | None:
    """Create a shadow task on the human-gate board. Returns shadow_id or None."""
    shadow_id = "s_" + hashlib.sha256(f"{board_slug}:{task_id}".encode()).hexdigest()[:16]
    shadow_title = f"[{board_slug}] Human review: {reason[:80]}".strip()

    full_body = json.dumps({
        "source_board": board_slug,
        "source_task": task_id,
        "reason": reason,
        "title": title,
    }, indent=2)

    try:
        conn = sqlite3.connect(HUMAN_GATE_DB)
        conn.execute(
            """INSERT OR IGNORE INTO tasks
               (id, title, body, assignee, status, priority, created_by, created_at, workspace_kind)
               VALUES (?, ?, ?, 'human-gate', 'todo', 5, 'shadow-creator', strftime('%s','now'), 'scratch')""",
            (shadow_id, shadow_title, full_body),
        )
        conn.commit()

        # Verify it was actually inserted (not a duplicate)
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (shadow_id,)
        ).fetchone()
        conn.close()

        if row:
            return shadow_id
        return None
    except Exception as e:
        print(f"ERROR creating shadow for {board_slug}/{task_id}: {e}", file=sys.stderr)
        return None


def scan_all_boards() -> list[dict]:
    """Scan all boards for blocked tasks with human-gate reason."""
    new_shadows = []
    existing_bodies = load_existing_bodies()
    notified = load_notified()

    for db_path in sorted(glob.glob(os.path.join(BOARDS_DIR, "*/kanban.db"))):
        board_slug = os.path.basename(os.path.dirname(db_path))
        if board_slug == "human-gate":
            continue

        try:
            conn = sqlite3.connect(db_path)
            blocked = conn.execute(
                """SELECT id, title, body FROM tasks
                   WHERE status = 'blocked'
                   ORDER BY created_at DESC LIMIT 1000"""
            ).fetchall()

            for tid, title, body in blocked:
                # Get the latest block event
                ev = conn.execute(
                    """SELECT payload FROM task_events
                       WHERE task_id = ? AND kind = 'blocked'
                       ORDER BY created_at DESC LIMIT 1""",
                    (tid,),
                ).fetchone()

                if not ev or not ev[0]:
                    continue

                payload_str = str(ev[0]).lower()
                if "human-gate" not in payload_str:
                    continue

                # Extract reason from payload
                reason = ""
                try:
                    p = json.loads(ev[0])
                    reason = p.get("reason", str(p))
                except (json.JSONDecodeError, TypeError):
                    reason = str(ev[0])

                # Build the body format the resolver expects
                shadow_body = json.dumps({
                    "source_board": board_slug,
                    "source_task": tid,
                    "action": "human-review",
                    "reason": reason,
                }, indent=2)

                # Check if a shadow already exists (by checking body content)
                already_exists = any(
                    f'"source_board": "{board_slug}"' in b
                    and f'"source_task": "{tid}"' in b
                    for b in existing_bodies
                )

                if already_exists:
                    continue

                # Also check by SQL LIKE (for old-format bodies)
                if shadow_exists_in_db(board_slug, tid):
                    existing_bodies.add(shadow_body)
                    continue

                # Create the shadow
                shadow_id = create_shadow(board_slug, tid, reason, title)
                if shadow_id:
                    existing_bodies.add(shadow_body)
                    new_shadows.append({
                        "shadow_id": shadow_id,
                        "board": board_slug,
                        "task": tid,
                        "title": f"[{board_slug}] Human review: {reason[:60]}",
                        "reason": reason[:100],
                    })

            conn.close()
        except Exception as e:
            print(f"ERROR scanning {board_slug}: {e}", file=sys.stderr)

    # Track newly notified IDs
    newly_notified = set(notified)
    for s in new_shadows:
        newly_notified.add(s["shadow_id"])
    save_notified(newly_notified)

    return new_shadows


def main():
    new_shadows = scan_all_boards()

    if new_shadows:
        print(f"📋 {len(new_shadows)} NEW human-gate shadow(s) created:")
        for s in new_shadows:
            print(f"  [{s['board']}] {s['title'][len(s['board'])+3:] if s['title'].startswith('['+s['board']+']') else s['title']}")
            print(f"    Shadow: {s['shadow_id']}  |  Source: {s['task']}")
            print(f"    Reason: {s['reason']}")


if __name__ == "__main__":
    main()
