#!/usr/bin/env python3
"""kanban_setup - Initialize kanban database with retry tracking.

Creates tables:
- tasks: main task table with retry tracking
- task_history: audit log for task changes
"""
from __future__ import annotations
import sqlite3
from pathlib import Path
from datetime import datetime, UTC

DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"

def setup():
    """Create kanban database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'backlog',
            priority TEXT DEFAULT 'medium',
            assigned_to TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            retry_count INTEGER DEFAULT 0,
            last_error TEXT,
            escalated_at TIMESTAMP,
            ppq_cost_usd REAL
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            field TEXT,
            old_value TEXT,
            new_value TEXT,
            changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks (id)
        )
    """)
    
    conn.commit()
    print(f"Created kanban database at {DB_PATH}")
    
    cursor.execute("PRAGMA table_info(tasks)")
    print("\nTasks schema:")
    for row in cursor.fetchall():
        print(f"  {row[1]}: {row[2]}")

if __name__ == "__main__":
    setup()