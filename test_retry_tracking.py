#!/usr/bin/env python3
"""Test retry tracking with native kanban schema."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / ".hermes" / "bot"))

from retry_tracking import (
    record_failure,
    check_escalation,
    escalate_task,
    record_ppq_cost,
    get_task_status,
    get_db
)

def test_retry_tracking():
    """Test retry tracking with native schema."""
    print("Creating test task in manager kanban...")
    
    conn = get_db("manager")
    cursor = conn.cursor()
    
    # Create test task
    cursor.execute("""
        INSERT INTO tasks (title, body, status, priority, consecutive_failures, max_retries, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, ("Retry test (native)", "Testing retry logic with native schema", "backlog", "medium", 0, 3, "test_user"))
    task_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    print(f"Created task {task_id}")
    
    print("\nRecording failures...")
    for i in range(3):
        success = record_failure(task_id, f"Error {i+1} (native)")
        print(f"  Retry {i+1}: {success}")
        status = get_task_status(task_id)
        print(f"    consecutive_failures: {status['consecutive_failures']}")
    
    print("\nChecking escalation...")
    should = check_escalation(task_id)
    print(f"  Should escalate: {should}")
    
    print("\nEscalating task...")
    success = escalate_task(task_id)
    print(f"  Escalated: {success}")
    
    print("\nRecording PPQ cost...")
    success = record_ppq_cost(task_id, 0.001234)
    print(f"  Cost recorded: {success}")
    
    print("\nFinal status:")
    status = get_task_status(task_id)
    for key in ["id", "title", "status", "consecutive_failures", "last_failure_error", "escalated_at", "ppq_cost_usd"]:
        print(f"  {key}: {status.get(key)}")
    
    print("\nTest passed!")

if __name__ == "__main__":
    test_retry_tracking()