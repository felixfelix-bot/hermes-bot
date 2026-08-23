#!/usr/bin/env python3
"""retry_tracking - Track task retries and escalations.

Uses Hermes' native kanban schema:
- consecutive_failures (not retry_count)
- last_failure_error (not last_error)
- max_retries
- ppq_cost_usd (new column)

Tracks tasks in per-profile kanban.db (~/.hermes/profiles/*/kanban.db).
"""
from __future__ import annotations
import json, sqlite3
from pathlib import Path
from datetime import datetime, UTC
from typing import Optional

# Default to manager profile (most common case)
DEFAULT_PROFILE = "manager"
KANBAN_DB_BASE = Path.home() / ".hermes" / "profiles"

def get_db(profile: str = DEFAULT_PROFILE) -> sqlite3.Connection:
    """Get database connection for a specific profile."""
    db_path = KANBAN_DB_BASE / profile / "kanban.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Kanban DB not found: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def record_failure(task_id: str, error: str, profile: str = DEFAULT_PROFILE) -> bool:
    """Record a failed attempt (increments consecutive_failures)."""
    conn = get_db(profile)
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT consecutive_failures FROM tasks WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        
        if not row:
            print(f"Task {task_id} not found")
            return False
        
        consecutive = row["consecutive_failures"] or 0
        
        cursor.execute("""
            UPDATE tasks 
            SET consecutive_failures = consecutive_failures + 1,
                last_failure_error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (error[:1000], task_id))
        
        conn.commit()
        return True
    except Exception as e:
        print(f"Failed to record failure: {e}")
        return False
    finally:
        conn.close()

def check_escalation(task_id: str, profile: str = DEFAULT_PROFILE) -> bool:
    """Check if task should be escalated (reached max_retries)."""
    conn = get_db(profile)
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            "SELECT consecutive_failures, max_retries, escalated_at FROM tasks WHERE id = ?",
            (task_id,)
        )
        row = cursor.fetchone()
        
        if not row:
            return False
        
        if row["escalated_at"]:
            return False
        
        consecutive = row["consecutive_failures"] or 0
        max_retries = row["max_retries"] or 3
        return consecutive >= max_retries
    except Exception as e:
        print(f"Failed to check escalation: {e}")
        return False
    finally:
        conn.close()

def escalate_task(task_id: int, profile: str = DEFAULT_PROFILE) -> bool:
    """Mark task as escalated."""
    conn = get_db(profile)
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            UPDATE tasks 
            SET escalated_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (task_id,))
        
        conn.commit()
        return True
    except Exception as e:
        print(f"Failed to escalate task: {e}")
        return False
    finally:
        conn.close()

def record_ppq_cost(task_id: int, cost_usd: float, profile: str = DEFAULT_PROFILE) -> bool:
    """Record PPQ cost for a task."""
    conn = get_db(profile)
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            UPDATE tasks 
            SET ppq_cost_usd = COALESCE(ppq_cost_usd, 0) + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (cost_usd, task_id))
        
        conn.commit()
        return True
    except Exception as e:
        print(f"Failed to record PPQ cost: {e}")
        return False
    finally:
        conn.close()

def get_task_status(task_id: int, profile: str = DEFAULT_PROFILE) -> Optional[dict]:
    """Get task status including retry info."""
    conn = get_db(profile)
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        
        if not row:
            return None
        
        return dict(row)
    except Exception as e:
        print(f"Failed to get task status: {e}")
        return None
    finally:
        conn.close()

def add_ppq_cost_column(profile: str = DEFAULT_PROFILE) -> bool:
    """Add ppq_cost_usd column to tasks table (idempotent)."""
    conn = get_db(profile)
    cursor = conn.cursor()
    
    try:
        cols = [r[1] for r in cursor.execute("PRAGMA table_info(tasks)")]
        if "ppq_cost_usd" not in cols:
            cursor.execute("ALTER TABLE tasks ADD COLUMN ppq_cost_usd REAL DEFAULT 0")
            conn.commit()
            return True
        return True
    except Exception as e:
        print(f"Failed to add ppq_cost_usd: {e}")
        return False
    finally:
        conn.close()

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Retry Tracking (Hermes Native Schema)")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="Profile name (default: manager)")
    parser.add_argument("--add-column", action="store_true", help="Add ppq_cost_usd column")
    parser.add_argument("--record", type=int, metavar="ID", help="Record failure")
    parser.add_argument("--error", type=str, metavar="ERROR", help="Error message")
    parser.add_argument("--check", type=int, metavar="ID", help="Check escalation")
    parser.add_argument("--escalate", type=int, metavar="ID", help="Escalate task")
    parser.add_argument("--cost", type=float, metavar="COST", help="Record PPQ cost")
    parser.add_argument("--status", type=int, metavar="ID", help="Get task status")
    
    args = parser.parse_args()
    
    if args.add_column:
        success = add_ppq_cost_column(args.profile)
        print(f"Column added: {success}")
    elif args.record:
        success = record_failure(args.record, args.error or "Unknown error", args.profile)
        print(f"Recorded failure: {success}")
    elif args.check:
        should = check_escalation(args.check, args.profile)
        print(f"Should escalate: {should}")
    elif args.escalate:
        success = escalate_task(args.escalate, args.profile)
        print(f"Escalated task: {success}")
    elif args.cost and args.status:
        success = record_ppq_cost(args.status, args.cost, args.profile)
        print(f"Recorded cost: {success}")
    elif args.status:
        status = get_task_status(args.status, args.profile)
        print(json.dumps(status, indent=2, default=str))
    else:
        parser.print_help()