#!/usr/bin/env python3
"""
net4sats_kanbanstr_sync.py — Full bidirectional sync between local Hermes kanban
and Nostr kanbanstr board.

OUTBOUND (local → Nostr):
  - Read ALL tasks from net4sats-mvp board
  - Map Hermes status → kanbanstr column ID
  - Publish each task as kind 30302 card (replaceable via d-tag)
  - Only re-publish if status or title changed since last sync

INBOUND (Nostr → local):
  - Query kind 30302 events from external maintainers (not our pubkey)
  - Detect status changes and new cards
  - Update local task status if the card moved columns
  - Create shadow tasks for new external cards

Column mapping:
  Hermes todo    → kanbanstr backlog
  Hermes ready   → kanbanstr backlog
  Hermes running → kanbanstr inprogress
  Hermes blocked → kanbanstr blocked (or humanreview if human-gate)
  Hermes done    → kanbanstr done

Usage: python3 net4sats_kanbanstr_sync.py [--dry-run]
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
BOARD_SLUG = "net4sats-mvp"
KANBANSTR_BOARD_ID = "net4sats-mvp-board"
RELAYS = ["wss://relay.damus.io", "wss://nos.lol"]
NAK = str(HOME / ".local" / "bin" / "nak")

# Status mapping: Hermes → kanbanstr column ID
STATUS_MAP = {
    "todo": "backlog",
    "ready": "backlog",
    "running": "inprogress",
    "blocked": "blocked",
    "done": "done",
    "archived": "done",
}

# Reverse mapping: kanbanstr column ID → Hermes status
REVERSE_STATUS_MAP = {
    "backlog": "todo",
    "inprogress": "running",
    "humanreview": "blocked",
    "blocked": "blocked",
    "done": "done",
}


def get_secret_key():
    env_file = HOME / "nostr-glasses" / "secrets" / ".env"
    try:
        with open(env_file) as f:
            for line in f:
                if "NOSTR_SECRET_KEY" in line and "=" in line:
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
    except Exception:
        pass
    return None


def get_my_pubkey(secret_key):
    r = subprocess.run([NAK, "key", "public", secret_key], capture_output=True, text=True, timeout=10)
    return r.stdout.strip()


def read_board_tasks():
    """Read all tasks from the local net4sats-mvp board."""
    db_path = BOARDS_DIR / BOARD_SLUG / "kanban.db"
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, title, body, status, assignee, created_at FROM tasks WHERE status != 'archived' ORDER BY created_at"
    ).fetchall()
    conn.close()

    tasks = []
    for r in rows:
        # Determine if this is a human-gate blocked task
        is_human_gate = False
        if r["status"] == "blocked" and r["body"]:
            is_human_gate = "human-gate" in r["body"].lower() or "review" in r["body"].lower()

        kanbanstr_status = STATUS_MAP.get(r["status"], "backlog")
        if is_human_gate:
            kanbanstr_status = "humanreview"

        tasks.append({
            "id": r["id"],
            "title": r["title"] or "Untitled",
            "status": r["status"],
            "assignee": r["assignee"] or "(unassigned)",
            "kanbanstr_status": kanbanstr_status,
            "body": (r["body"] or "")[:200],
        })
    return tasks


def load_sync_state():
    """Load the last-synced state."""
    state_file = STATE_DIR / "kanbanstr_sync_state.json"
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return {"outbound": {}, "inbound": {}}


def save_sync_state(state):
    """Save sync state."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / "kanbanstr_sync_state.json"
    state_file.write_text(json.dumps(state, indent=2))


def publish_card(secret_key, board_pubkey, task, dry_run=False):
    """Publish or update a task as a kanbanstr 30302 card."""
    d_tag = f"n4s-{task['id']}"

    # Truncate title for Nostr (keep it readable)
    title = task["title"][:120]
    desc = f"Source: {BOARD_SLUG}/{task['id']}\nStatus: {task['status']}\nAssignee: {task['assignee']}"

    cmd = [
        NAK, "event", "--sec", secret_key,
        "-k", "30302",
        "-d", d_tag,
        "-t", f"title={title}",
        "-t", f"description={desc}",
        "-t", f"a=30301:{board_pubkey}:{KANBANSTR_BOARD_ID}",
        "-t", f"s={task['kanbanstr_status']}",
        "-t", f"rank=0",
    ] + RELAYS

    if dry_run:
        print(f"  [DRY RUN] Would publish: s={task['kanbanstr_status']:12s} {title[:60]}")
        return True

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        combined = (r.stdout + r.stderr).lower()
        return r.returncode == 0 and "success" in combined
    except Exception:
        return False


def publish_board(secret_key, board_pubkey, dry_run=False):
    """Publish the board event (kind 30301) with correct columns.

    nak -t supports multi-element tags via semicolons:
    -t 'col=backlog;Backlog;1' → ['col', 'backlog', 'Backlog', '1']
    """
    cmd = [
        NAK, "event", "--sec", secret_key,
        "-k", "30301",
        "-d", KANBANSTR_BOARD_ID,
        "-t", "title=net4sats MVP Board",
        "-t", "description=Full mirror of Hermes kanban. Changes sync both ways.",
        "-t", "col=backlog;Backlog;1",
        "-t", "col=inprogress;In Progress;2",
        "-t", "col=humanreview;Human Review;3",
        "-t", "col=blocked;Blocked;4",
        "-t", "col=done;Done;5",
        "-t", f"p={board_pubkey}",
    ] + RELAYS

    if dry_run:
        print(f"  [DRY RUN] Would publish board event: {KANBANSTR_BOARD_ID}")
        return True

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        combined = (r.stdout + r.stderr).lower()
        return r.returncode == 0 and "success" in combined
    except Exception:
        return False


def query_external_cards(board_pubkey, my_pubkey):
    """Query Nostr for cards from external maintainers (not our pubkey)."""
    a_tag = f"30301:{board_pubkey}:{KANBANSTR_BOARD_ID}"
    cmd = [NAK, "req", "-k", "30302", "-t", f"a={a_tag}"] + RELAYS

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except Exception:
        return []

    cards = []
    for line in r.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
            if ev.get("pubkey") == my_pubkey:
                continue  # Skip our own cards

            title = desc = status = dtag = ""
            for tag in ev.get("tags", []):
                if len(tag) >= 2:
                    if tag[0] == "title": title = tag[1]
                    elif tag[0] == "description": desc = tag[1]
                    elif tag[0] == "s": status = tag[1]
                    elif tag[0] == "d": dtag = tag[1]

            cards.append({
                "event_id": ev.get("id", ""),
                "pubkey": ev.get("pubkey", ""),
                "dtag": dtag,
                "title": title or "Untitled",
                "status": status,
                "description": desc,
            })
        except json.JSONDecodeError:
            continue

    return cards


def update_local_task_status(dtag, new_kanbanstr_status):
    """Update a local task's status based on a kanbanstr column change."""
    # Parse task ID from d-tag (format: n4s-t_xxxxx)
    if not dtag.startswith("n4s-"):
        return False

    task_id = dtag[4:]
    hermes_status = REVERSE_STATUS_MAP.get(new_kanbanstr_status)
    if not hermes_status:
        return False

    db_path = BOARDS_DIR / BOARD_SLUG / "kanban.db"
    if not db_path.exists():
        return False

    try:
        conn = sqlite3.connect(str(db_path))
        # Check current status
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            conn.close()
            return False

        if row[0] != hermes_status:
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (hermes_status, task_id))
            conn.commit()
            conn.close()
            print(f"  📥 Updated {task_id}: {row[0]} → {hermes_status} (from Nostr)")
            return True
        conn.close()
    except Exception:
        pass
    return False


def outbound_sync(secret_key, board_pubkey, tasks, state, dry_run=False):
    """Publish all local tasks to Nostr. Only re-publish changed items."""
    prev = state.get("outbound", {})
    published = 0
    unchanged = 0

    for task in tasks:
        key = task["id"]
        current_sig = f"{task['kanbanstr_status']}|{task['title']}"
        prev_sig = prev.get(key, {}).get("sig", "")

        if current_sig == prev_sig:
            unchanged += 1
            continue

        if publish_card(secret_key, board_pubkey, task, dry_run):
            prev[key] = {"sig": current_sig, "status": task["kanbanstr_status"]}
            published += 1

    state["outbound"] = prev
    return published, unchanged


def inbound_sync(board_pubkey, my_pubkey, state, dry_run=False):
    """Check for external changes and apply them locally."""
    external_cards = query_external_cards(board_pubkey, my_pubkey)
    if not external_cards:
        return 0

    prev_inbound = state.get("inbound", {})
    updated = 0
    new_external = 0

    for card in external_cards:
        key = card["dtag"] or card["event_id"]
        prev_status = prev_inbound.get(key, {}).get("status", "")

        if card["status"] != prev_status:
            # Status changed externally
            if key.startswith("n4s-") and not dry_run:
                if update_local_task_status(key, card["status"]):
                    updated += 1
            elif not prev_status:
                # New external card
                new_external += 1
                print(f"  📥 New external card: [{card['status']}] {card['title'][:60]}")
                print(f"     From: {card['pubkey'][:16]}...")
                print(f"     Desc: {card['description'][:80]}")

        prev_inbound[key] = {"status": card["status"], "title": card["title"]}

    state["inbound"] = prev_inbound
    return updated + new_external


def main():
    dry_run = "--dry-run" in sys.argv

    secret_key = get_secret_key()
    if not secret_key:
        print("ERROR: No NOSTR_SECRET_KEY found in ~/nostr-glasses/secrets/.env")
        sys.exit(1)

    my_pubkey = get_my_pubkey(secret_key)
    if not my_pubkey:
        print("ERROR: Could not derive pubkey from secret key")
        sys.exit(1)

    print(f"Board: {KANBANSTR_BOARD_ID}")
    print(f"Pubkey: {my_pubkey}")
    print(f"Relays: {', '.join(RELAYS)}")
    print()

    # Step 1: Publish board event (ensure columns exist)
    print("── Publishing board event ──")
    if publish_board(secret_key, my_pubkey, dry_run):
        print(f"  ✅ Board event published ({KANBANSTR_BOARD_ID})")
    else:
        print(f"  ⚠️  Board event publish failed (may already exist)")

    # Step 2: Read local tasks
    tasks = read_board_tasks()
    print(f"\n── Local tasks: {len(tasks)} ──")
    by_status = {}
    for t in tasks:
        by_status.setdefault(t["kanbanstr_status"], []).append(t)
    for status, items in sorted(by_status.items()):
        print(f"  {status:15s}: {len(items)}")

    # Step 3: Load state
    state = load_sync_state()

    # Step 4: Outbound sync
    print(f"\n── Outbound sync ──")
    published, unchanged = outbound_sync(secret_key, my_pubkey, tasks, state, dry_run)
    print(f"  Published/updated: {published}")
    print(f"  Unchanged: {unchanged}")

    # Step 5: Inbound sync
    print(f"\n── Inbound sync ──")
    inbound_count = inbound_sync(my_pubkey, my_pubkey, state, dry_run)
    print(f"  External changes: {inbound_count}")

    # Step 6: Save state
    if not dry_run:
        save_sync_state(state)

    # Summary
    print(f"\n{'─' * 40}")
    if published > 0 or inbound_count > 0:
        print(f"✅ Synced: {published} outbound, {inbound_count} inbound")
    else:
        print("✅ All in sync — no changes")


if __name__ == "__main__":
    main()
