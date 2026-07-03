#!/usr/bin/env python3
"""
human-gate-digest.py — Smart digest of pending human-gate items.

Only notifies on NEW items (never repeats the same list).
Once per 24h, outputs a brief stale-reminder count.
Silent otherwise.

Run as: hermes cron --no-agent --script scripts/human-gate-digest.py
"""
import json
import os
import re
import sqlite3
import sys

HOME = os.path.expanduser("~")
DB_PATH = os.path.join(HOME, ".hermes", "kanban", "boards", "human-gate", "kanban.db")
STATE_FILE = os.path.join(HOME, ".hermes", "state", "human-gate-digest-seen.json")


def load_state() -> tuple[set[str], int]:
    """Return (seen_ids, last_reminder_timestamp)."""
    if not os.path.exists(STATE_FILE):
        return set(), 0
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
            return set(data.get("seen_ids", [])), data.get("last_reminder", 0)
    except (json.JSONDecodeError, OSError):
        return set(), 0


def save_state(seen_ids: set[str], last_reminder: int) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(
            {"seen_ids": sorted(seen_ids), "last_reminder": last_reminder},
            f,
            indent=2,
        )


def main():
    if not os.path.exists(DB_PATH):
        return

    import time
    now = int(time.time())

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT id, title, body, status, created_at
           FROM tasks
           WHERE status NOT IN ('done', 'archived')
           ORDER BY created_at ASC"""
    ).fetchall()
    conn.close()

    seen, last_reminder = load_state()

    all_items = []
    new_items = []
    for tid, title, body, status, created_at in rows:
        src_board = "?"
        if body:
            m = re.search(r'[Ss]ource[_\s][Bb]oard[":\s]+([\w-]+)', body)
            if m:
                src_board = m.group(1)
        item = {"id": tid, "title": (title or "untitled")[:60], "board": src_board}
        all_items.append(item)
        if tid not in seen:
            new_items.append(item)

    output_lines = []

    # 1. NEW items — always notify immediately
    if new_items:
        output_lines.append(f"📋 {len(new_items)} NEW human-gate item(s):")
        for it in new_items:
            output_lines.append(f'  [{it["board"]}] {it["title"]} ({it["id"]})')

    # 2. Daily stale reminder — once per 24h, brief count only
    hours_since = (now - last_reminder) / 3600
    if hours_since >= 24 and all_items and not new_items:
        output_lines.append(
            f"⏰ Reminder: {len(all_items)} human-gate item(s) still pending."
        )

    # 3. Save state
    current_ids = {it["id"] for it in all_items}
    # Keep only IDs still pending (prune resolved ones)
    cleaned_seen = (seen | current_ids) & current_ids
    new_reminder = now if output_lines else last_reminder
    save_state(cleaned_seen, new_reminder)

    if output_lines:
        print("\n".join(output_lines))


if __name__ == "__main__":
    main()
