#!/usr/bin/env python3
"""priority_gc — classify each worker profile's state + write profile_states.json.

States: pending (no priorities.yaml entry; never dispatches) | active | parked
(dormant/stale/user-parked; kept, 0 tokens) | archived (branch merged to default).
Also computes activity_score for ranking. Reads ~/.hermes/bot/worktree_manifest.json
+ priorities.yaml + opencode.db + git. No LLM tokens. Output: profile_states.json +
prints the pending list (for the daily "Needs your input" nudge).
"""
from __future__ import annotations
import json, os, subprocess, sys, sqlite3, time
from pathlib import Path

HOME = Path.home()
BOT = HOME / ".hermes" / "bot"
MANIFEST = BOT / "worktree_manifest.json"
PRIORITIES = BOT / "priorities.yaml"
STATES = BOT / "profile_states.json"
OPENCODE_DB = HOME / ".local/share/opencode/opencode.db"
PARK_AFTER_DAYS = int(os.environ.get("PARK_AFTER_DAYS", "14"))
STALE_BEHIND = int(os.environ.get("STALE_BEHIND", "50"))


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def load_priorities() -> dict:
    """Minimal resilient parse of priorities.yaml (no pyyaml dependency)."""
    pri = {"repos": {}, "profiles": {}}
    if not PRIORITIES.exists():
        return pri
    section = None
    for raw in PRIORITIES.read_text().splitlines():
        line = raw.rstrip()
        s = line.strip()
        if s.startswith("#") or not s:
            continue
        if line.startswith("repos:"):
            section = "repos"; continue
        if line.startswith("profiles:"):
            section = "profiles"; continue
        if section and s.endswith(":") and not s.startswith("-"):
            cur = s[:-1]
            if section == "repos" and line.startswith("  ") and not line.startswith("    "):
                pri["repos"][cur] = {}
        if section and "priority:" in s:
            # form: "key: { priority: 1, pinned: true }" or "priority: 2"
            import re
            m = re.search(r"priority:\s*(\d+)", s)
            name = None
            if line.startswith("    ") or line.startswith("\t"):  # nested under a repo/profile key
                pass
            # inline dict on a repo line: "<repo>: { priority: N ... }"
            if "{" in raw and "}" in raw and section in ("repos", "profiles"):
                key = raw.split(":", 1)[0].strip()
                if m:
                    pri[section][key] = {"priority": int(m.group(1)),
                                         "parked": "parked: true" in s or "parked:True" in s,
                                         "pinned": "pinned: true" in s}
    return pri


def last_session(directory: str) -> float:
    """Last opencode session time (unix s) for a worktree dir, or 0."""
    if not OPENCODE_DB.exists():
        return 0.0
    try:
        con = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True)
        row = con.execute("SELECT MAX(time_updated) FROM session WHERE directory=?", (directory,)).fetchone()
        con.close()
        return (row[0] or 0) / 1000.0
    except Exception:
        return 0.0


def git_int(repo, *args):
    r = run(["git", "-C", repo] + list(args))
    try:
        return int(r.stdout.strip()) if r.stdout.strip().isdigit() else 0
    except Exception:
        return 0


def classify(entry, pri):
    repo, branch, default = entry["repo"], entry["branch"], entry["default_branch"]
    name = Path(repo).name
    slug = entry["slug"]
    # priority resolution: profile override > repo
    rp = pri["repos"].get(name, {})
    pp = pri["profiles"].get(slug, {})
    priority = pp.get("priority", rp.get("priority"))
    parked_user = pp.get("parked", rp.get("parked", False))

    state = "pending" if priority is None else "active"

    # archived: branch merged into default
    if state != "pending":
        r = run(["git", "-C", repo, "branch", "--merged", default])
        merged = any(b.strip().lstrip("* ").strip() == branch for b in r.stdout.splitlines())
        if merged and branch != default:
            state = "archived"

    # activity signals
    ls = last_session(entry["worktree"])
    lc = float(git_int(repo, "log", "-1", "--format=%ct", branch) or 0)
    now = time.time()
    dormant = (now - max(ls, lc)) > PARK_AFTER_DAYS * 86400 if max(ls, lc) else True
    behind = git_int(repo, "rev-list", "--count", f"{branch}..{default}") if branch != default else 0
    stale = behind > STALE_BEHIND

    if state == "active" and (parked_user or dormant or stale):
        state = "parked"

    activity = max(ls, lc)
    return {"state": state, "priority": priority, "harness": entry["harness"],
            "last_session": ls, "last_commit": lc, "behind": behind,
            "activity_score": activity, "parked_user": parked_user, "stale": stale}


def main() -> int:
    if not MANIFEST.exists():
        print(f"missing {MANIFEST}; run worktree_sync first", file=sys.stderr)
        return 1
    manifest = json.loads(MANIFEST.read_text())
    pri = load_priorities()
    states = {}
    pending = []
    for e in manifest:
        st = classify(e, pri)
        states[e["slug"]] = {**st, "repo": e["repo"], "branch": e["branch"], "worktree": e["worktree"]}
        if st["state"] == "pending":
            pending.append(f"{Path(e['repo']).name} ({e['branch']})")
    STATES.write_text(json.dumps(states, indent=2))
    print(f"classified {len(states)} profiles -> {STATES}")
    counts = {}
    for s in states.values():
        counts[s["state"]] = counts.get(s["state"], 0) + 1
    print("  " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if pending:
        print("PENDING (needs priority input):")
        for p in pending:
            print(f"  - {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
