#!/usr/bin/env python3
"""human-gate-mcp.py — MCP server for human-gate task management.

Exposes structured methods for requesting/completing human actions
across all Hermes kanban boards. Uses subprocess to call hermes CLI.

Methods:
    request_review(board, task_id, pr_url, description) -> shadow_task_id
    request_merge(board, task_id, pr_url, branch) -> shadow_task_id
    request_approval(board, task_id, description, artifact_url) -> shadow_task_id
    list_pending(action_type=None) -> [items]
    complete_action(shadow_task_id, summary, decision) -> success
    get_digest() -> formatted string
    get_stats() -> {total_pending, by_action_type, by_board}
"""
import json
import subprocess
import sys
import sqlite3
import os
import re
from pathlib import Path

HUMAN_GATE_DB = os.path.expanduser("~/.hermes/kanban/boards/human-gate/kanban.db")
BOARDS_ROOT = os.path.expanduser("~/.hermes/kanban/boards")


def _run(cmd: list[str], timeout: int = 15) -> str:
    """Run a command and return stdout."""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


def _create_shadow(board: str, task_id: str, action_type: str, title: str, description: str, url: str = "") -> str:
    """Create a shadow task on human-gate board."""
    body = f"Source board: {board}\nSource task: {task_id}\nAction type: {action_type}\nDescription: {description}"
    if url:
        body += f"\nURL: {url}"
    out = _run([
        "hermes", "kanban", "--board", "human-gate", "create",
        "--body", body,
        "--json",
        f"[{board}] {title}"
    ])
    if "Created t_" in out:
        return out.split("Created ")[1].split(" ")[0].strip()
    return f"error: {out[:100]}"


def request_review(board: str, task_id: str, pr_url: str, description: str) -> str:
    """Request human review of a PR."""
    _run(["hermes", "kanban", "--board", board, "block", task_id, "--reason", f"human-gate: review PR {pr_url}"])
    return _create_shadow(board, task_id, "review", f"Review PR: {description}", description, pr_url)


def request_merge(board: str, task_id: str, pr_url: str, branch: str) -> str:
    """Request human merge approval."""
    _run(["hermes", "kanban", "--board", board, "block", task_id, "--reason", f"human-gate: merge {branch}"])
    return _create_shadow(board, task_id, "merge", f"Merge {branch}: {pr_url}", f"Branch: {branch}", pr_url)


def request_approval(board: str, task_id: str, description: str, artifact_url: str = "") -> str:
    """Request human sign-off on an artifact."""
    _run(["hermes", "kanban", "--board", board, "block", task_id, "--reason", f"human-gate: approve {description}"])
    return _create_shadow(board, task_id, "approval", f"Approve: {description}", description, artifact_url)


def list_pending(action_type: str = "") -> list[dict]:
    """List all pending human-gate items."""
    items = []
    if not os.path.exists(HUMAN_GATE_DB):
        return items
    conn = sqlite3.connect(HUMAN_GATE_DB)
    c = conn.cursor()
    c.execute("SELECT id, title, body, status FROM tasks WHERE status IN ('ready','running','todo')")
    for tid, title, body, status in c.fetchall():
        # Parse action type from body
        at = ""
        if body and "Action type:" in body:
            m = re.search(r"Action type:\s*(\w+)", body)
            if m:
                at = m.group(1)
        if action_type and at != action_type:
            continue
        items.append({"id": tid, "title": title, "action_type": at, "status": status})
    conn.close()
    return items


def complete_action(shadow_task_id: str, summary: str, decision: str = "approved") -> str:
    """Complete a human-gate shadow task."""
    out = _run(["hermes", "kanban", "--board", "human-gate", "complete", shadow_task_id, "--summary", f"{decision}: {summary}"])
    return out if "completed" in out.lower() or "done" in out.lower() else f"completed: {shadow_task_id}"


def get_digest() -> str:
    """Get a formatted digest of all pending items."""
    items = list_pending()
    if not items:
        return ""
    lines = [f"📋 {len(items)} human-gate item(s) pending:"]
    for item in items:
        lines.append(f"  [{item['action_type']}] {item['title']} ({item['id']})")
    return "\n".join(lines)


def get_stats() -> dict:
    """Get statistics."""
    items = list_pending()
    by_type = {}
    for item in items:
        by_type[item["action_type"]] = by_type.get(item["action_type"], 0) + 1
    return {"total_pending": len(items), "by_action_type": by_type}


# Simple JSON-RPC-like dispatcher for stdin/stdout MCP protocol
if __name__ == "__main__":
    for line in sys.stdin:
        try:
            req = json.loads(line)
            method = req.get("method", "")
            params = req.get("params", {})
            id = req.get("id")

            dispatch = {
                "request_review": request_review,
                "request_merge": request_merge,
                "request_approval": request_approval,
                "list_pending": list_pending,
                "complete_action": complete_action,
                "get_digest": get_digest,
                "get_stats": get_stats,
            }

            if method in dispatch:
                result = dispatch[method](**params)
                print(json.dumps({"jsonrpc": "2.0", "id": id, "result": result}))
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}))
        except Exception as e:
            print(json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -32603, "message": str(e)}}))
        sys.stdout.flush()
