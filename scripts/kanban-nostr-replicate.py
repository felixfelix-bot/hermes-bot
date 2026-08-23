#!/usr/bin/env python3
"""
kanban-nostr-replicate.py — Bidirectional Nostr replication for Hermes Kanban.

Replicates ALL kanban board state between machines via Nostr relays (kind 38010).
Each machine runs this script in two modes:

  --outbound  : detect local kanban changes, publish them as Nostr events
  --inbound   : subscribe to events from other machines, apply to local kanban.db

DESIGN
======
Event-sourced change detection: the outbound mode uses the task_events table as a
change feed. When new events appear for a task, it publishes a fresh snapshot of
that task's logical state.

Nostr event format (kind 38010, parameterized replaceable per NIP-33):
  d-tag   = "<hostname>:<board>:<entity>"     (unique per machine+entity)
  tags    = b=<board> t=<task_id> src=<hostname> op=<task|comment|link> ts=<unix>
  content = JSON snapshot of the logical columns

The d-tag includes the source hostname so events from different machines don't
clobber each other on relays (NIP-33 replacement only matches same pubkey+d-tag).
The inbound consumer applies events in ts order (last-write-wins) and skips
self-published events (src == local hostname).

Conflict resolution: timestamp-based last-write-wins (per the DQ05 plan, M2d).
"""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time

import sqlite3

# ─── Configuration ────────────────────────────────────────────────────────────

HERMES_HOME = os.path.expanduser("~/.hermes")
BOARDS_DIR = os.path.join(HERMES_HOME, "kanban", "boards")
STATE_DIR = os.path.join(HERMES_HOME, "state")

# Nostr relays — same as existing kanbanstr scripts
DEFAULT_RELAYS = ["wss://relay.damus.io", "wss://nos.lol"]
DEFAULT_TIMEOUT = 30

# Event kind for Hermes kanban sync (parameterized replaceable, NIP-33 range)
KANBAN_KIND = 38010

# Logical task columns that define reproducible state.
# Machine-local runtime columns (worker_pid, claim_lock, current_run_id, etc.)
# are deliberately EXCLUDED — they're local operational state, not task state.
TASK_SYNC_COLUMNS = [
    "id", "title", "body", "assignee", "status", "priority",
    "created_by", "created_at", "started_at", "completed_at",
    "workspace_kind", "workspace_path", "branch_name", "tenant",
    "result", "idempotency_key", "max_runtime_seconds",
    "skills", "model_override", "max_retries", "goal_mode",
    "goal_max_turns", "session_id",
]

# ─── Helpers ──────────────────────────────────────────────────────────────────


def hostname():
    return socket.gethostname()


def get_secret_key():
    """Load Nostr secret key from the standard location."""
    env_path = os.path.expanduser("~/nostr-glasses/secrets/.env")
    if not os.path.isfile(env_path):
        return None
    key_export = "export NOSTR_SECRET_KEY"
    key_plain = "NOSTR_SECRET_KEY"
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            val = None
            if line.startswith(key_export):
                val = line.split("=", 1)[1].strip()
            elif line.startswith(key_plain):
                val = line.split("=", 1)[1].strip()
            if val:
                return val.strip("'\"")
    return None


def board_db_path(board):
    return os.path.join(BOARDS_DIR, board, "kanban.db")


def list_boards():
    """Return all board slugs that have a kanban.db."""
    boards = []
    if not os.path.isdir(BOARDS_DIR):
        return boards
    for name in sorted(os.listdir(BOARDS_DIR)):
        if os.path.isfile(board_db_path(name)):
            boards.append(name)
    return boards


def task_hash(row_dict):
    """Stable hash of a task's logical columns for change detection."""
    parts = []
    for col in TASK_SYNC_COLUMNS:
        parts.append(str(row_dict.get(col, "")))
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]


def load_state(name):
    path = os.path.join(STATE_DIR, name)
    if os.path.isfile(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_state(name, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, name)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.rename(tmp, path)


# ─── Nostr I/O (via nak CLI) ──────────────────────────────────────────────────


NAK_TIMEOUT = DEFAULT_TIMEOUT


def nak_publish(secret_key, kind, content, tags, relays):
    """Publish a Nostr event via nak. Returns True on success."""
    import signal
    template = json.dumps({"content": content, "tags": tags})
    cmd = ["nak", "event", "--sec", secret_key, "-k", str(kind)] + relays
    try:
        p = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True
        )
        try:
            out, _ = p.communicate(input=template.encode(), timeout=NAK_TIMEOUT)
            return p.returncode == 0
        except subprocess.TimeoutExpired:
            # kill the entire process group (nak spawns WebSocket children)
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass
            p.communicate(timeout=5)
            return False
    except (FileNotFoundError, OSError):
        return False


def nak_fetch(kind, tag_filters, relays, timeout=45):
    """Fetch events from relays via nak req. Returns list of event dicts."""
    cmd = ["nak", "req", "-k", str(kind)]
    for tk, tv in tag_filters.items():
        cmd.extend(["-t", f"{tk}={tv}"])
    cmd.extend(relays)
    events = []
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        for line in r.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return events


# ─── Seed: initialize state file without publishing ──────────────────────────


def seed():
    """Initialize the outbound state file so the first outbound run only
    publishes deltas. Call this once after copying the baseline kanban.db
    (M2b) so both machines start with matching watermarks."""
    hn = hostname()
    state = load_state("kanban-nostr-outbound.json")
    seeded = 0

    for board in list_boards():
        db_path = board_db_path(board)
        bstate = state.setdefault(board, {
            "last_event_id": 0,
            "task_hashes": {},
            "last_comment_id": 0,
            "links": [],
        })

        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
        except sqlite3.Error:
            continue

        try:
            max_ev = c.execute(
                "SELECT COALESCE(MAX(id), 0) FROM task_events"
            ).fetchone()[0]
            max_cmt = c.execute(
                "SELECT COALESCE(MAX(id), 0) FROM task_comments"
            ).fetchone()[0]
            tasks = c.execute(
                f"SELECT {', '.join(TASK_SYNC_COLUMNS)} FROM tasks"
            ).fetchall()
            links = c.execute(
                "SELECT parent_id, child_id FROM task_links"
            ).fetchall()
        except sqlite3.Error:
            conn.close()
            continue

        bstate["last_event_id"] = max_ev
        bstate["last_comment_id"] = max_cmt
        bstate["task_hashes"] = {
            t["id"]: task_hash({col: t[col] for col in TASK_SYNC_COLUMNS})
            for t in tasks
        }
        bstate["links"] = [[r["parent_id"], r["child_id"]] for r in links]
        seeded += len(tasks)
        conn.close()

    save_state("kanban-nostr-outbound.json", state)
    print(f"🌱 Seeded outbound state: {seeded} tasks across {len(state)} boards")


def publish_single_task(secret_key, relays, board, task_id):
    """Publish a single task snapshot — for testing the Nostr round-trip."""
    hn = hostname()
    db_path = board_db_path(board)
    if not os.path.isfile(db_path):
        print(f"Board '{board}' not found", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    row = c.execute(
        f"SELECT {', '.join(TASK_SYNC_COLUMNS)} FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    conn.close()

    if row is None:
        print(f"Task '{task_id}' not found on board '{board}'", file=sys.stderr)
        sys.exit(1)

    row_dict = {col: row[col] for col in TASK_SYNC_COLUMNS}
    eff_ts = max(
        row_dict.get("created_at") or 0,
        row_dict.get("started_at") or 0,
        row_dict.get("completed_at") or 0,
        int(time.time()),
    )

    d_tag = f"{hn}:{board}:{task_id}"
    tags = [
        ["d", d_tag], ["b", board], ["t", task_id],
        ["src", hn], ["op", "task"], ["ts", str(eff_ts)],
    ]

    ok = nak_publish(secret_key, KANBAN_KIND, json.dumps(row_dict), tags, relays)
    if ok:
        print(f"✅ Published {board}/{task_id} → Nostr kind {KANBAN_KIND} (d={d_tag})")
    else:
        print(f"❌ Failed to publish {board}/{task_id}", file=sys.stderr)
        sys.exit(1)


# ─── Outbound: detect + publish local changes ────────────────────────────────


def outbound(secret_key, relays, dry_run=False):
    """Detect local kanban changes and publish them to Nostr."""
    hn = hostname()
    state = load_state("kanban-nostr-outbound.json")
    published = 0

    for board in list_boards():
        db_path = board_db_path(board)
        bstate = state.setdefault(board, {
            "last_event_id": 0,
            "task_hashes": {},
            "last_comment_id": 0,
            "links": [],
        })

        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
        except sqlite3.Error:
            continue

        # ── Detect changed tasks via task_events watermark ──
        try:
            max_ev = c.execute(
                "SELECT COALESCE(MAX(id), 0) FROM task_events"
            ).fetchone()[0]
        except sqlite3.Error:
            conn.close()
            continue

        last_ev = bstate.get("last_event_id", 0)
        changed_task_ids = set()

        if max_ev > last_ev:
            rows = c.execute(
                "SELECT DISTINCT task_id FROM task_events WHERE id > ?",
                (last_ev,),
            ).fetchall()
            changed_task_ids = {r[0] for r in rows}

        # ── Publish changed task snapshots ──
        for tid in sorted(changed_task_ids):
            row = c.execute(
                f"SELECT {', '.join(TASK_SYNC_COLUMNS)} FROM tasks WHERE id = ?",
                (tid,),
            ).fetchone()
            if row is None:
                # Task was deleted/archived — skip (we don't sync deletions yet)
                continue

            row_dict = {col: row[col] for col in TASK_SYNC_COLUMNS}
            h = task_hash(row_dict)

            if bstate["task_hashes"].get(tid) == h:
                continue  # No logical change (e.g., just a heartbeat event)

            # Effective timestamp = max of created_at, completed_at, started_at, now
            def safe_int(v, default=0):
                try:
                    return int(v) if v else default
                except (ValueError, TypeError):
                    return default

            eff_ts = max(
                safe_int(row_dict.get("created_at")),
                safe_int(row_dict.get("started_at")),
                safe_int(row_dict.get("completed_at")),
                int(time.time()),
            )

            d_tag = f"{hn}:{board}:{tid}"
            tags = [
                ["d", d_tag],
                ["b", board],
                ["t", tid],
                ["src", hn],
                ["op", "task"],
                ["ts", str(eff_ts)],
            ]

            if nak_publish(secret_key, KANBAN_KIND, json.dumps(row_dict), tags, relays):
                bstate["task_hashes"][tid] = h
                published += 1

        bstate["last_event_id"] = max_ev

        # ── Publish new comments ──
        try:
            max_cmt = c.execute(
                "SELECT COALESCE(MAX(id), 0) FROM task_comments"
            ).fetchone()[0]
        except sqlite3.Error:
            max_cmt = bstate.get("last_comment_id", 0)

        last_cmt = bstate.get("last_comment_id", 0)
        if max_cmt > last_cmt:
            comments = c.execute(
                "SELECT id, task_id, author, body, created_at "
                "FROM task_comments WHERE id > ?",
                (last_cmt,),
            ).fetchall()
            for cmt in comments:
                d_tag = f"{hn}:{board}:{cmt['task_id']}:c:{cmt['id']}"
                tags = [
                    ["d", d_tag],
                    ["b", board],
                    ["t", cmt["task_id"]],
                    ["src", hn],
                    ["op", "comment"],
                    ["ts", str(cmt["created_at"])],
                ]
                content = json.dumps({
                    "id": cmt["id"],
                    "task_id": cmt["task_id"],
                    "author": cmt["author"],
                    "body": cmt["body"],
                    "created_at": cmt["created_at"],
                })
                if nak_publish(secret_key, KANBAN_KIND, content, tags, relays):
                    published += 1
            bstate["last_comment_id"] = max_cmt

        # ── Publish new links ──
        try:
            links = c.execute(
                "SELECT parent_id, child_id FROM task_links"
            ).fetchall()
            current_links = [[r["parent_id"], r["child_id"]] for r in links]
            prev_set = {tuple(l) for l in bstate.get("links", [])}
            for link in current_links:
                if tuple(link) not in prev_set:
                    d_tag = f"{hn}:{board}:link:{link[0]}:{link[1]}"
                    tags = [
                        ["d", d_tag],
                        ["b", board],
                        ["src", hn],
                        ["op", "link"],
                        ["ts", str(int(time.time()))],
                    ]
                    content = json.dumps({
                        "parent_id": link[0],
                        "child_id": link[1],
                    })
                    if nak_publish(secret_key, KANBAN_KIND, content, tags, relays):
                        published += 1
            bstate["links"] = current_links
        except sqlite3.Error:
            pass

        conn.close()

    if published > 0:
        print(f"📤 Published {published} kanban change(s) to Nostr (kind {KANBAN_KIND})")
    # Silent on zero changes (cron-friendly)

    # Persist watermarks — skip in dry-run so --dry-run is side-effect-free
    if not dry_run:
        save_state("kanban-nostr-outbound.json", state)


# ─── Inbound: subscribe + apply foreign changes ──────────────────────────────


def apply_task_snapshot(board, row_dict, db_path):
    """UPSERT a task snapshot into the local DB. Returns True if applied."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # Check if task exists and compare timestamps (last-write-wins)
    existing = c.execute(
        "SELECT created_at, started_at, completed_at FROM tasks WHERE id = ?",
        (row_dict["id"],),
    ).fetchone()

    if existing:
        # Last-write-wins: compare effective timestamps
        local_ts = max(existing[0] or 0, existing[1] or 0, existing[2] or 0)
        remote_ts = max(
            row_dict.get("created_at") or 0,
            row_dict.get("started_at") or 0,
            row_dict.get("completed_at") or 0,
        )
        if remote_ts <= local_ts:
            conn.close()
            return False  # Local is newer or same

    # Build UPSERT — only set logical columns, preserve local runtime columns
    cols = TASK_SYNC_COLUMNS
    placeholders = ", ".join(["?"] * len(cols))
    col_list = ", ".join(cols)
    update_set = ", ".join([f"{col} = excluded.{col}" for col in cols])

    values = [row_dict.get(col) for col in cols]

    try:
        c.execute(
            f"INSERT INTO tasks ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {update_set}",
            values,
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.Error as e:
        conn.close()
        print(f"  ⚠️ task upsert error for {row_dict['id']}: {e}", file=sys.stderr)
        return False


def apply_comment(board, comment, db_path):
    """INSERT a synced comment if not already present."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # Check by (task_id, author, created_at, body prefix) to avoid dupes
    existing = c.execute(
        "SELECT 1 FROM task_comments WHERE task_id = ? AND author = ? "
        "AND created_at = ? AND substr(body, 1, 200) = substr(?, 1, 200)",
        (comment["task_id"], comment["author"], comment["created_at"],
         comment["body"]),
    ).fetchone()

    if existing:
        conn.close()
        return False

    try:
        c.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (comment["task_id"], comment["author"], comment["body"],
             comment["created_at"]),
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.Error as e:
        conn.close()
        return False


def apply_link(board, link, db_path):
    """INSERT a parent→child link if not present."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    try:
        c.execute(
            "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (link["parent_id"], link["child_id"]),
        )
        conn.commit()
        changed = c.rowcount > 0
    except sqlite3.Error:
        changed = False
    conn.close()
    return changed


def update_outbound_hashes(board, task_id, row_dict):
    """Update outbound state so we don't re-publish what we just received."""
    state = load_state("kanban-nostr-outbound.json")
    bstate = state.setdefault(board, {"task_hashes": {}})
    bstate.setdefault("task_hashes", {})[task_id] = task_hash(row_dict)
    save_state("kanban-nostr-outbound.json", state)


def inbound(secret_key, relays, dry_run=False):
    """Subscribe to Nostr events and apply foreign kanban changes locally."""
    hn = hostname()
    applied = 0

    # Fetch ALL kind 38010 events (we filter by src in Python)
    events = nak_fetch(KANBAN_KIND, {}, relays, timeout=45)

    if not events:
        return  # Silent

    # Track per (board, d-tag) the highest ts we've applied
    istate = load_state("kanban-nostr-inbound.json")

    # Group events by board
    by_board = {}
    for ev in events:
        tags = {t[0]: t[1] for t in ev.get("tags", []) if len(t) >= 2}
        src = tags.get("src", "")
        if src == hn:
            continue  # Skip self-published
        board = tags.get("b", "")
        if not board:
            continue
        by_board.setdefault(board, []).append(ev)

    for board, evs in by_board.items():
        db_path = board_db_path(board)
        if not os.path.isfile(db_path):
            # Board doesn't exist locally — skip (or create?)
            continue

        board_state = istate.setdefault(board, {})

        # Sort by ts for deterministic application
        def get_ts(ev):
            tags = {t[0]: t[1] for t in ev.get("tags", []) if len(t) >= 2}
            try:
                return int(tags.get("ts", "0"))
            except ValueError:
                return 0

        evs.sort(key=get_ts)

        for ev in evs:
            tags = {t[0]: t[1] for t in ev.get("tags", []) if len(t) >= 2}
            d_tag = tags.get("d", "")
            op = tags.get("op", "")
            ts_val = get_ts(ev)

            # Skip if we've already applied this d-tag at >= ts
            if board_state.get(d_tag, 0) >= ts_val and ts_val > 0:
                continue

            try:
                content = json.loads(ev.get("content", "{}"))
            except json.JSONDecodeError:
                continue

            if op == "task":
                if apply_task_snapshot(board, content, db_path):
                    applied += 1
                    update_outbound_hashes(board, content.get("id", ""), content)
            elif op == "comment":
                if apply_comment(board, content, db_path):
                    applied += 1
            elif op == "link":
                if apply_link(board, content, db_path):
                    applied += 1

            board_state[d_tag] = max(board_state.get(d_tag, 0), ts_val)

    # Persist applied-watermarks — skip in dry-run so --dry-run is side-effect-free
    if not dry_run:
        save_state("kanban-nostr-inbound.json", istate)

    if applied > 0:
        print(f"📥 Applied {applied} inbound kanban change(s) from Nostr")
    # Silent on zero changes (cron-friendly)


# ─── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Bidirectional Nostr replication for Hermes Kanban"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--outbound", action="store_true",
                      help="Publish local kanban changes to Nostr")
    mode.add_argument("--inbound", action="store_true",
                      help="Apply incoming Nostr kanban changes locally")
    mode.add_argument("--seed", action="store_true",
                      help="Initialize state file without publishing (run once "
                           "after baseline copy)")
    mode.add_argument("--publish-task", nargs=2, metavar=("BOARD", "TASK_ID"),
                      help="Publish a single task snapshot (testing)")
    parser.add_argument("--relays", nargs="*", default=DEFAULT_RELAYS,
                        help="Nostr relay URLs")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't publish/apply, just report")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"Timeout in seconds per nak call (default: {DEFAULT_TIMEOUT})")
    args = parser.parse_args()

    # Apply timeout to nak functions
    global NAK_TIMEOUT
    NAK_TIMEOUT = args.timeout

    sk = get_secret_key()
    if not sk and not args.seed:
        # No key configured — exit silently (cron-safe)
        sys.exit(0)

    if args.dry_run:
        # In dry-run mode, skip actual Nostr I/O
        global nak_publish, nak_fetch
        def fake_publish(*a, **kw):
            d_tag = ""
            for tag in a[3]:
                if tag and tag[0] == "d":
                    d_tag = tag[1]
                    break
            print(f"  [dry-run] would publish kind={a[1]} d={d_tag}")
            return True
        def fake_fetch(*a, **kw):
            print(f"  [dry-run] would fetch kind={a[0]}")
            return []
        nak_publish = fake_publish
        nak_fetch = fake_fetch

    if args.seed:
        seed()
    elif args.publish_task:
        board, task_id = args.publish_task
        publish_single_task(sk, args.relays, board, task_id)
    elif args.outbound:
        outbound(sk, args.relays, dry_run=args.dry_run)
    elif args.inbound:
        inbound(sk, args.relays, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
