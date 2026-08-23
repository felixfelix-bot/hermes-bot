#!/usr/bin/env python3
"""completion_watch — surface completed worker tasks to the operator via Matrix.

Checks the kanban DB for tasks completed since the last run. Sends a formatted
summary to Matrix so the operator never misses worker output. Runs every 10 min.

Usage (via hermes cron):
  hermes cron create --no-agent --script completion_watch.py \
    --name completion-watch --deliver local "*/10 * * * *"
"""
from __future__ import annotations
import json, os, sqlite3, subprocess, sys, time
from pathlib import Path

HOME = Path.home()
KANBAN_BOARDS_DIR = HOME / ".hermes" / "kanban" / "boards"
STATE_FILE = HOME / ".hermes" / "bot" / "completion_watch_state.json"
MATRIX_ROOM = os.environ.get("MATRIX_HOME_ROOM", "!cqgmiHeQtATPAJwJZg:matrix.org")
HERMES = os.environ.get("HERMES_BIN", str(HOME / ".local" / "bin" / "hermes"))


def load_last_check() -> float:
    try:
        return json.loads(STATE_FILE.read_text()).get("last_check_ts", 0)
    except Exception:
        return 0


def save_last_check(ts: float):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"last_check_ts": ts}))


def query_completed(since_ts: float) -> list[dict]:
    completed = []
    if not KANBAN_BOARDS_DIR.exists():
        return completed
    for db_path in KANBAN_BOARDS_DIR.glob("*/kanban.db"):
        board = db_path.parent.name
        try:
            db = sqlite3.connect(str(db_path))
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """SELECT id, title, assignee, completed_at, result, body
                   FROM tasks
                   WHERE status = 'done' AND completed_at IS NOT NULL AND completed_at > ?
                   ORDER BY completed_at ASC""",
                (since_ts,),
            ).fetchall()
            for r in rows:
                d = dict(r)
                d["board"] = board
                completed.append(d)
            db.close()
        except Exception:
            continue
    return completed


def query_failed() -> list[dict]:
    failed = []
    if not KANBAN_BOARDS_DIR.exists():
        return failed
    for db_path in KANBAN_BOARDS_DIR.glob("*/kanban.db"):
        board = db_path.parent.name
        try:
            db = sqlite3.connect(str(db_path))
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """SELECT id, title, assignee, last_failure_error
                   FROM tasks
                   WHERE status = 'blocked' AND consecutive_failures >= 2
                   LIMIT 5""",
            ).fetchall()
            for r in rows:
                d = dict(r)
                d["board"] = board
                failed.append(d)
            db.close()
        except Exception:
            continue
    return failed


def format_summary(completed: list[dict], failed: list[dict]) -> str:
    if not completed and not failed:
        return ""

    lines = []
    if completed:
        lines.append(f"✅ {len(completed)} task(s) completed since last check:\n")
        for t in completed:
            title = t["title"][:80]
            assignee = t.get("assignee") or "unassigned"
            result = (t.get("result") or "")[:300]
            lines.append(f"  ▸ {title}")
            lines.append(f"    worker: {assignee}")
            if result:
                lines.append(f"    result: {result}")
            lines.append("")

    if failed:
        lines.append(f"⚠️ {len(failed)} task(s) blocked (worker failures):")
        for t in failed:
            title = t["title"][:80]
            err = (t.get("last_failure_error") or "")[:150]
            lines.append(f"  ▸ {title}")
            if err:
                lines.append(f"    error: {err}")
        lines.append("")

    lines.append("💡 Run `hermes kanban --board <name> show <task_id>` for full details.")
    return "\n".join(lines)


def send_to_matrix(message: str):
    try:
        subprocess.run(
            [HERMES, "send", f"-t", f"matrix:{MATRIX_ROOM}", message],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        print(f"Failed to send Matrix message: {e}", file=sys.stderr)


def main() -> int:
    now = time.time()
    since = load_last_check()

    completed = query_completed(since)
    failed = query_failed()

    summary = format_summary(completed, failed)
    if summary:
        print(f"[completion-watch] {len(completed)} new completions, {len(failed)} blocked")
        print(summary)
        send_to_matrix(summary)
    else:
        print("[completion-watch] no new completions since last check")

    save_last_check(now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
