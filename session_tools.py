#!/usr/bin/env python3
"""session_tools - Visibility tools for opencode/pi sessions.

Provides programmatic access to session state without burning LLM tokens.
Can be called via:
  - Python: import session_tools; session_tools.session_status(...)
  - CLI: python3 ~/.hermes/bot/session_tools.py status --session <id>
  - HTTP: curl http://localhost:7779/sessions/status?session_id=<id>
  - MCP: via hermes-cli MCP server

Usage:
  python3 ~/.hermes/bot/session_tools.py <command> [options]
  
Commands:
  status     - Get session status (active/paused/stuck/expired)
  tokens     - Get token usage for session
  list       - List active sessions
  context    - Get context window usage %
  errors     - Get recent errors from session
  kanban     - Get kanban board status for repo
  profiles   - List worker profiles
  quota      - Get model quota status
  git        - Get git status for worktree
  tools      - Get last tool calls from session
"""
from __future__ import annotations
import argparse, datetime, json, sqlite3, subprocess, sys, urllib.request
from pathlib import Path
from typing import Optional

# Config
HOME = Path.home()
OPENCODE_DB = HOME / ".local" / "share" / "opencode" / "opencode.db"
WORKTREE_MANIFEST = HOME / ".hermes" / "bot" / "worktree_manifest.json"
ZAI_PROXY_URL = "http://localhost:9099"
MODEL_SELECTOR_URL = "http://localhost:7779"

def get_db():
    """Connect to opencode database."""
    if not OPENCODE_DB.exists():
        return None
    return sqlite3.connect(str(OPENCODE_DB))

def session_status(session_id: Optional[str] = None, worktree: Optional[str] = None) -> dict:
    """Get opencode session status."""
    db = get_db()
    if not db:
        return {"error": "opencode.db not found"}
    
    try:
        if session_id:
            # Get specific session
            row = db.execute("""
                SELECT id, time_created, time_updated, title, model, tokens_input, tokens_output, cost
                FROM session 
                WHERE id = ?
            """, (session_id,)).fetchone()
            
            if not row:
                return {"error": f"Session not found: {session_id}"}
            
            id, time_created, time_updated, title, model, tokens_input, tokens_output, cost = row
            
            # Determine status based on activity
            last_activity = datetime.datetime.fromtimestamp(time_updated)
            age_hours = (datetime.datetime.now() - last_activity).total_seconds() / 3600
            
            if age_hours < 1:
                status = "active"
            elif age_hours < 24:
                status = "paused"
            else:
                status = "stuck"
            
            return {
                "session_id": id,
                "status": status,
                "title": title,
                "model": model,
                "tokens_input": tokens_input,
                "tokens_output": tokens_output,
                "cost": cost,
                "last_activity": time_updated,
                "age_hours": round(age_hours, 1)
            }
        
        elif worktree:
            # Get sessions for worktree
            rows = db.execute("""
                SELECT s.id, s.time_created, s.time_updated, s.title,
                       s.tokens_input, s.tokens_output
                FROM session s
                WHERE s.directory = ? OR s.path = ?
                ORDER BY s.time_updated DESC
                LIMIT 10
            """, (worktree, worktree)).fetchall()
            
            return {
                "worktree": worktree,
                "sessions": [
                    {
                        "session_id": r[0],
                        "created_at": r[1],
                        "updated_at": r[2],
                        "title": r[3],
                        "tokens_input": r[4],
                        "tokens_output": r[5]
                    }
                    for r in rows
                ]
            }
        
        else:
            # List all recent sessions
            rows = db.execute("""
                SELECT id, time_created, time_updated, title, model, 
                       tokens_input, tokens_output, cost
                FROM session
                ORDER BY time_updated DESC
                LIMIT 20
            """).fetchall()
            
            return {
                "sessions": [
                    {
                        "session_id": r[0],
                        "created_at": r[1],
                        "updated_at": r[2],
                        "title": r[3],
                        "model": r[4],
                        "tokens_input": r[5],
                        "tokens_output": r[6],
                        "cost": r[7]
                    }
                    for r in rows
                ]
            }
    except Exception as e:
        return {"error": str(e)}
    finally:
        db.close()

def session_tokens(session_id: str) -> dict:
    """Get token usage for session (approximate)."""
    db = get_db()
    if not db:
        return {"error": "opencode.db not found"}
    
    try:
        # Approximate tokens from message content length
        result = db.execute("""
            SELECT 
                SUM(CASE WHEN role = 'user' THEN LENGTH(content) ELSE 0 END) as input_chars,
                SUM(CASE WHEN role = 'assistant' THEN LENGTH(content) ELSE 0 END) as output_chars,
                COUNT(*) as message_count
            FROM message 
            WHERE session_id = ?
        """, (session_id,)).fetchone()
        
        input_chars = result[0] or 0
        output_chars = result[1] or 0
        message_count = result[2] or 0
        
        # Approximate tokens (1 token ≈ 4 chars)
        return {
            "session_id": session_id,
            "input_tokens_approx": input_chars // 4,
            "output_tokens_approx": output_chars // 4,
            "total_tokens_approx": (input_chars + output_chars) // 4,
            "message_count": message_count
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        db.close()

def session_context_pct(session_id: str, model_limit: int = 200000) -> dict:
    """Calculate context window usage percentage."""
    tokens = session_tokens(session_id)
    if "error" in tokens:
        return tokens
    
    total = tokens.get("total_tokens_approx", 0)
    pct = min(total / model_limit, 1.0)
    
    return {
        "session_id": session_id,
        "tokens": total,
        "model_limit": model_limit,
        "percentage": round(pct * 100, 1),
        "needs_handover": pct >= 0.70
    }

def session_errors(session_id: str, limit: int = 10) -> dict:
    """Get recent errors from session."""
    db = get_db()
    if not db:
        return {"error": "opencode.db not found"}
    
    try:
        rows = db.execute("""
            SELECT content, created_at
            FROM message
            WHERE session_id = ? AND role = 'assistant'
            ORDER BY created_at DESC
            LIMIT ?
        """, (session_id, limit * 2)).fetchall()
        
        errors = []
        for content, created_at in rows:
            content_lower = content.lower() if content else ""
            error_keywords = ["error", "exception", "failed", "traceback", "assertionerror"]
            
            if any(kw in content_lower for kw in error_keywords):
                errors.append({
                    "timestamp": created_at,
                    "snippet": content[:500] if content else ""
                })
                
            if len(errors) >= limit:
                break
        
        return {
            "session_id": session_id,
            "error_count": len(errors),
            "errors": errors
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        db.close()

def session_list(worktree: Optional[str] = None) -> dict:
    """List active sessions."""
    return session_status(worktree=worktree)

def kanban_status(repo: str) -> dict:
    """Get kanban board status for repo."""
    kanban_dir = HOME / ".hermes" / "kanban" / "boards"
    
    # Find matching board
    board_dir = None
    for d in kanban_dir.iterdir():
        if d.is_dir() and repo.lower() in d.name.lower():
            board_dir = d
            break
    
    if not board_dir:
        return {"error": f"No kanban board found for {repo}"}
    
    db_path = board_dir / "kanban.db"
    if not db_path.exists():
        return {"error": f"kanban.db not found in {board_dir}"}
    
    db = sqlite3.connect(str(db_path))
    try:
        pending = db.execute("SELECT COUNT(*) FROM tasks WHERE status = 'pending'").fetchone()[0]
        active = db.execute("SELECT COUNT(*) FROM tasks WHERE status = 'active'").fetchone()[0]
        completed = db.execute("SELECT COUNT(*) FROM tasks WHERE status = 'completed'").fetchone()[0]
        blocked = db.execute("SELECT COUNT(*) FROM tasks WHERE status = 'blocked'").fetchone()[0]
        
        return {
            "repo": repo,
            "board": board_dir.name,
            "pending": pending,
            "active": active,
            "completed": completed,
            "blocked": blocked
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        db.close()

def worker_profiles() -> dict:
    """List all worker profiles."""
    if not WORKTREE_MANIFEST.exists():
        return {"error": "worktree_manifest.json not found"}
    
    try:
        manifest = json.loads(WORKTREE_MANIFEST.read_text())
        
        profiles = []
        for entry in manifest:
            slug = entry.get("slug", "")
            worktree = entry.get("worktree", "")
            harness = entry.get("harness", "unknown")
            branch = entry.get("branch", "")
            
            # Check if profile exists
            profile_dir = HOME / ".hermes" / "profiles" / slug
            exists = profile_dir.exists()
            
            profiles.append({
                "slug": slug,
                "worktree": worktree,
                "harness": harness,
                "branch": branch,
                "exists": exists
            })
        
        return {
            "total": len(profiles),
            "profiles": profiles
        }
    except Exception as e:
        return {"error": str(e)}

def model_quota() -> dict:
    """Get current model quota status."""
    try:
        # Try zai_proxy first
        req = urllib.request.Request(f"{ZAI_PROXY_URL}/quota")
        with urllib.request.urlopen(req, timeout=5) as resp:
            zai_quota = json.loads(resp.read())
        
        # Try model_selector for PPQ prices
        req = urllib.request.Request(f"{MODEL_SELECTOR_URL}/models/prices")
        with urllib.request.urlopen(req, timeout=5) as resp:
            ppq_prices = json.loads(resp.read())
        
        return {
            "zai": zai_quota,
            "ppq_models": len(ppq_prices)
        }
    except Exception as e:
        return {"error": str(e)}

def git_status(worktree: str) -> dict:
    """Get git status for worktree."""
    try:
        result = subprocess.run(
            ["git", "-C", worktree, "status", "--porcelain"],
            capture_output=True, text=True, timeout=10
        )
        
        modified = []
        untracked = []
        
        for line in result.stdout.splitlines():
            if line.startswith(" M") or line.startswith("M "):
                modified.append(line[3:])
            elif line.startswith("??"):
                untracked.append(line[3:])
        
        return {
            "worktree": worktree,
            "clean": len(modified) == 0 and len(untracked) == 0,
            "modified": modified,
            "untracked": untracked
        }
    except Exception as e:
        return {"error": str(e)}

def last_tool_calls(session_id: str, limit: int = 5) -> dict:
    """Get last tool calls from session."""
    db = get_db()
    if not db:
        return {"error": "opencode.db not found"}
    
    try:
        rows = db.execute("""
            SELECT content, created_at
            FROM message
            WHERE session_id = ? AND role = 'assistant'
            ORDER BY created_at DESC
            LIMIT ?
        """, (session_id, limit * 3)).fetchall()
        
        tools = []
        for content, created_at in rows:
            # Look for tool call patterns
            if content and ("tool" in content.lower() or "<tool>" in content.lower()):
                tools.append({
                    "timestamp": created_at,
                    "snippet": content[:300]
                })
            
            if len(tools) >= limit:
                break
        
        return {
            "session_id": session_id,
            "tool_calls": tools
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        db.close()

def main():
    parser = argparse.ArgumentParser(description="Session visibility tools")
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    
    # status
    status_parser = subparsers.add_parser("status", help="Get session status")
    status_parser.add_argument("--session", help="Session ID")
    status_parser.add_argument("--worktree", help="Worktree path")
    
    # tokens
    tokens_parser = subparsers.add_parser("tokens", help="Get token usage")
    tokens_parser.add_argument("--session", required=True, help="Session ID")
    
    # context
    context_parser = subparsers.add_parser("context", help="Get context for session")
    context_parser.add_argument("--session", required=True, help="Session ID")
    context_parser.add_argument("--limit", type=int, default=200000, help="Model context limit")
    
    # errors
    errors_parser = subparsers.add_parser("errors", help="Get session errors")
    errors_parser.add_argument("--session", required=True, help="Session ID")
    errors_parser.add_argument("--limit", type=int, default=10, help="Max errors")
    
    # list
    list_parser = subparsers.add_parser("list", help="List sessions")
    list_parser.add_argument("--worktree", help="Filter by worktree")
    
    # kanban
    kanban_parser = subparsers.add_parser("kanban", help="Get kanban status")
    kanban_parser.add_argument("--repo", required=True, help="Repo name")
    
    # profiles
    subparsers.add_parser("profiles", help="List worker profiles")
    
    # quota
    subparsers.add_parser("quota", help="Get model quota")
    
    # git
    git_parser = subparsers.add_parser("git", help="Get git status")
    git_parser.add_argument("--worktree", required=True, help="Worktree path")
    
    # tools
    tools_parser = subparsers.add_parser("tools", help="Get last tool calls")
    tools_parser.add_argument("--session", required=True, help="Session ID")
    tools_parser.add_argument("--limit", type=int, default=5, help="Max tools")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    result = {}
    
    if args.command == "status":
        result = session_status(args.session, args.worktree)
    elif args.command == "tokens":
        result = session_tokens(args.session)
    elif args.command == "context":
        result = session_context_pct(args.session, args.limit)
    elif args.command == "errors":
        result = session_errors(args.session, args.limit)
    elif args.command == "list":
        result = session_list(args.worktree)
    elif args.command == "kanban":
        result = kanban_status(args.repo)
    elif args.command == "profiles":
        result = worker_profiles()
    elif args.command == "quota":
        result = model_quota()
    elif args.command == "git":
        result = git_status(args.worktree)
    elif args.command == "tools":
        result = last_tool_calls(args.session, args.limit)
    
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
