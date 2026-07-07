#!/usr/bin/env python3
"""session_registry — zero-token session tracking + curation flagging.

Scans opencode.db, groups sessions by worktree, checks llms.txt + HANDOVER.md
freshness, writes a JSON registry. Only flags sessions that need LLM curation.

The llms-sweep cron reads this registry and only burns tokens on sessions
where needs_curation=true.

Usage:
  session_registry.py sync    # full rebuild + staleness check
  session_registry.py show    # print registry summary
  session_registry.py stale   # print only sessions needing curation
"""
from __future__ import annotations
import json, os, sqlite3, sys, time
from pathlib import Path
from datetime import datetime, timezone

OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
REGISTRY_PATH = Path.home() / ".hermes" / "bot" / "session_registry.json"

SKIP_DIRS = {".hermes", ".config", ".local", ".cache", ".cargo", ".bun",
             ".opencode", "Downloads", "Desktop", "Documents", "Pictures",
             "Videos", "Music", "Public", "snap", ".gnupg", ".ssh",
             ".mozilla", ".thunderbird"}


def ts_to_iso(ts_ms: int) -> str:
    if not ts_ms:
        return ""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def ts_to_epoch(ts_ms: int) -> float:
    return ts_ms / 1000 if ts_ms else 0


def file_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except Exception:
        return 0


def sync():
    if not OPENCODE_DB.exists():
        print("opencode.db not found", file=sys.stderr)
        return 1

    db = sqlite3.connect(str(OPENCODE_DB))
    db.row_factory = sqlite3.Row

    rows = db.execute("""
        SELECT id, directory, title, time_created, time_updated, time_archived,
               tokens_input, tokens_output, model, slug
        FROM session
        WHERE time_archived IS NULL
        ORDER BY time_updated DESC
    """).fetchall()
    db.close()

    worktrees: dict[str, dict] = {}

    for row in rows:
        directory = row["directory"] or ""
        if not directory:
            continue

        path = Path(directory)
        if path.name in SKIP_DIRS:
            continue

        wt_key = str(path)

        if wt_key not in worktrees:
            handover_path = None
            for name in ("HANDOVER.md", "HANDOVER-plebeian.md", "HANDOVER-esp32.md"):
                candidate = path / name
                if candidate.exists():
                    handover_path = str(candidate)
                    break
            if not handover_path:
                handover_path = str(path / "HANDOVER.md")

            llms_path = path / "llms.txt"

            repo_name = path.name

            is_git = (path / ".git").exists()
            branch = ""
            if is_git:
                try:
                    import subprocess
                    result = subprocess.run(
                        ["git", "branch", "--show-current"],
                        capture_output=True, text=True, timeout=3,
                        cwd=str(path),
                    )
                    branch = result.stdout.strip()
                except Exception:
                    pass

            worktrees[wt_key] = {
                "description": row["title"] or repo_name,
                "worktree": wt_key,
                "repo": repo_name,
                "branch": branch,
                "is_git": is_git,
                "session_ids": [],
                "last_activity": ts_to_iso(row["time_updated"]),
                "last_activity_epoch": ts_to_epoch(row["time_updated"]),
                "total_sessions": 0,
                "total_tokens_in": 0,
                "total_tokens_out": 0,
                "handover": {
                    "path": handover_path,
                    "exists": Path(handover_path).exists(),
                    "mtime_epoch": file_mtime(Path(handover_path)),
                    "mtime_iso": ts_to_iso(int(file_mtime(Path(handover_path)) * 1000)) if file_mtime(Path(handover_path)) else "",
                },
                "llms_txt": {
                    "path": str(llms_path),
                    "exists": llms_path.exists(),
                    "mtime_epoch": file_mtime(llms_path),
                    "mtime_iso": ts_to_iso(int(file_mtime(llms_path) * 1000)) if file_mtime(llms_path) else "",
                },
                "needs_curation": False,
                "curation_reasons": [],
                "status": "active" if ts_to_epoch(row["time_updated"]) > (time.time() - 14 * 86400) else "stale",
            }

        wt = worktrees[wt_key]
        wt["session_ids"].append(row["id"])
        wt["total_sessions"] += 1
        wt["total_tokens_in"] += row["tokens_input"] or 0
        wt["total_tokens_out"] += row["tokens_output"] or 0
        if ts_to_epoch(row["time_updated"]) > wt["last_activity_epoch"]:
            wt["last_activity"] = ts_to_iso(row["time_updated"])
            wt["last_activity_epoch"] = ts_to_epoch(row["time_updated"])
            wt["description"] = row["title"] or wt["description"]

    stale_count = 0
    for wt in worktrees.values():
        reasons = []
        last_act = wt["last_activity_epoch"]

        if not wt["llms_txt"]["exists"]:
            reasons.append("llms_txt missing")
        elif wt["llms_txt"]["mtime_epoch"] < last_act:
            reasons.append("llms_txt stale")

        if not wt["handover"]["exists"]:
            if wt["is_git"]:
                reasons.append("handover missing")
        elif wt["handover"]["mtime_epoch"] < last_act:
            reasons.append("handover stale")

        if reasons:
            wt["needs_curation"] = True
            wt["curation_reasons"] = reasons
            stale_count += 1

    registry = {
        "last_sync": datetime.now(timezone.utc).isoformat(),
        "total_worktrees": len(worktrees),
        "total_sessions": sum(wt["total_sessions"] for wt in worktrees.values()),
        "needs_curation": stale_count,
        "sessions": worktrees,
    }

    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2))

    print(f"Registry: {len(worktrees)} worktrees, {registry['total_sessions']} sessions")
    print(f"Needs curation: {stale_count}")
    if stale_count > 0:
        print("\nSessions needing curation:")
        for key, wt in sorted(worktrees.items(), key=lambda x: x[1]["last_activity_epoch"], reverse=True):
            if wt["needs_curation"]:
                print(f"  {wt['repo']:30s} {', '.join(wt['curation_reasons'])}")

    return 0


def show():
    if not REGISTRY_PATH.exists():
        print("No registry. Run: session_registry.py sync")
        return 1
    data = json.loads(REGISTRY_PATH.read_text())
    print(f"Last sync: {data['last_sync']}")
    print(f"Worktrees: {data['total_worktrees']}, Sessions: {data['total_sessions']}")
    print(f"Needs curation: {data['needs_curation']}\n")
    for key, wt in sorted(data["sessions"].items(), key=lambda x: x[1]["last_activity_epoch"], reverse=True):
        status = "CURATE" if wt["needs_curation"] else "ok"
        print(f"  [{status:6s}] {wt['repo']:30s} {wt['description'][:50]}")


def stale():
    if not REGISTRY_PATH.exists():
        print("No registry. Run: session_registry.py sync")
        return 1
    data = json.loads(REGISTRY_PATH.read_text())
    for key, wt in data["sessions"].items():
        if wt["needs_curation"]:
            print(json.dumps({
                "worktree": wt["worktree"],
                "repo": wt["repo"],
                "reasons": wt["curation_reasons"],
                "last_activity": wt["last_activity"],
            }))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "sync":
        sys.exit(sync())
    elif cmd == "show":
        sys.exit(show())
    elif cmd == "stale":
        sys.exit(stale())
    else:
        print(f"Unknown: {cmd}", file=sys.stderr)
        sys.exit(1)
