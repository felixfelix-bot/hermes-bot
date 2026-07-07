#!/usr/bin/env python3
"""kanban_migration - Add retry tracking columns to kanban database.

Adds:
- retry_count: integer, default 0
- last_error: text, nullable
- escalated_at: timestamp, nullable
- ppq_cost_usd: real, nullable

Run once to apply migration.
"""
from __future__ import annotations
import sqlite3
from pathlib import Path
from datetime import datetime, UTC

DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"

def migrate():
    """Apply migration to add retry tracking columns."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT retry_count FROM tasks LIMIT 1")
        print("Migration already applied (retry_count exists)")
        return
    except sqlite3.OperationalError:
        pass
    
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN retry_count INTEGER DEFAULT 0")
        print("Added retry_count column")
    except sqlite3.OperationalError as e:
        print(f"Failed to add retry_count: {e}")
    
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN last_error TEXT")
        print("Added last_error column")
    except sqlite3.OperationalError as e:
        print(f"Failed to add last_error: {e}")
    
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN escalated_at TIMESTAMP")
        print("Added escalated_at column")
    except sqlite3.OperationalError as e:
        print(f"Failed to add escalated_at: {e}")
    
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN ppq_cost_usd REAL")
        print("Added ppq_cost_usd column")
    except sqlite3.OperationalError as e:
        print(f"Failed to add ppq_cost_usd: {e}")
    
    conn.commit()
    print("\nMigration complete!")
    print("\nSchema check:")
    cursor.execute("PRAGMA table_info(tasks)")
    for row in cursor.fetchall():
        print(f"  {row[1]}: {row[2]}")

if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        exit(1)
    
    migrate()