#!/usr/bin/env python3
"""
test_worker_track_fixes_phase2_helpers.py
Helper functions for Phase 2 worker track fixes:
  Q1: Quota gate depth check (viable provider count warning)
  Q3: Human-gate timeout (3-tier taxonomy)

This module contains the pure-Python logic that the bash scripts call into,
so it can be unit-tested without live proxy or kanban state.
"""

import glob
import json
import os
import re
import sqlite3
import time
from pathlib import Path

# ============================================================================
# Q1: Quota Gate Depth — viable provider count parsing
# ============================================================================

def parse_viable_count(gate_json: str) -> int:
    """Parse the /v1/dispatch_gate JSON response and count viable providers
    in the downgrade_chain.

    This is the Q1 fix: the dispatcher already checks can_dispatch (boolean),
    but doesn't expose how many providers are healthy. This function counts
    providers with viable=True in the downgrade_chain.

    Returns 0 if:
    - JSON is malformed
    - downgrade_chain is missing or empty
    - No providers have viable=True

    WARNING ONLY: The caller should use this to log a warning when
    viable_count < 2, but must NOT block dispatch. Felix wants no caps.
    """
    try:
        d = json.loads(gate_json)
    except (json.JSONDecodeError, TypeError):
        return 0

    chain = d.get("downgrade_chain", [])
    if not isinstance(chain, list):
        return 0

    viable = sum(1 for c in chain if isinstance(c, dict) and c.get("viable", False))
    return viable


def should_warn_depth(gate_json: str) -> bool:
    """Determine if a depth warning should be emitted.

    Returns True when:
    - can_dispatch is True (dispatch is allowed)
    - AND viable_count < 2 (only 1 or 0 viable providers)

    This is a WARNING only — never blocks dispatch.
    The caller (kalman_dispatch_gate in adaptive-dispatch-daemon.sh) should
    log "WARNING: Only N viable provider(s), dispatching is fragile" but
    still return 0 (allow dispatch).
    """
    try:
        d = json.loads(gate_json)
    except (json.JSONDecodeError, TypeError):
        return False

    if not d.get("can_dispatch", False):
        return False  # Not dispatching anyway — no depth warning needed

    viable_count = parse_viable_count(gate_json)
    return viable_count < 2


# ============================================================================
# Q3: Human-Gate Timeout — 3-tier taxonomy
# ============================================================================

# Timeout values (in seconds) for each tier
TIER_TIMEOUTS = {
    "stand_down": 4 * 3600,        # 4 hours — safety-critical
    "proceed_isolated": 2 * 3600,  # 2 hours — methodology
    "escalate": 4 * 3600,          # 4 hours — unknown impact
}

# Classification keywords (lowercase matching)
SAFETY_KEYWORDS = [
    "corrupt", "interference", "external", "mutated", "deleted",
    "damage", "irreversible", "data loss", "safety",
]
METHODOLOGY_KEYWORDS = [
    "isolated", "clone", "verify", "re-run", "proceed",
    "approach", "methodology", "fresh",
]


def classify_gate(reason: str) -> str:
    """Classify a human-gate block reason into a timeout tier.

    Tier 1 (stand_down): Safety-critical — corruption, interference, data loss.
    Tier 2 (proceed_isolated): Methodology — verification, isolated clone.
    Tier 3 (escalate): Unknown/complex — default when no keywords match.

    The classification is conservative: when uncertain, default to 'escalate'
    (the safest tier — keeps the task blocked and alerts the operator).
    """
    reason_lower = (reason or "").lower()

    # Tier 1: Safety-critical
    if any(kw in reason_lower for kw in SAFETY_KEYWORDS):
        return "stand_down"

    # Tier 2: Methodology
    if any(kw in reason_lower for kw in METHODOLOGY_KEYWORDS):
        return "proceed_isolated"

    # Tier 3: Unknown/complex — safest default
    return "escalate"


def format_timeout_alert(tier: str, task_id: str, elapsed_seconds: int) -> str:
    """Format the alert message for a human-gate timeout.

    Each tier has its own emoji and message:
    - stand_down:    ⏸️ HUMAN-GATE TIMEOUT: Task {id} stood down after 4h — safety-critical gate unanswered
    - proceed_isolated: ⏭️ HUMAN-GATE TIMEOUT: Task {id} proceeding with isolated approach after 2h
    - escalate:      🚨 HUMAN-GATE TIMEOUT: Task {id} needs escalation — gate unanswered for 4h
    """
    hours = elapsed_seconds // 3600

    if tier == "stand_down":
        return (
            f"⏸️ HUMAN-GATE TIMEOUT: Task {task_id} stood down after {hours}h "
            f"— safety-critical gate unanswered"
        )
    elif tier == "proceed_isolated":
        return (
            f"⏭️ HUMAN-GATE TIMEOUT: Task {task_id} proceeding with isolated "
            f"approach after {hours}h"
        )
    else:  # escalate
        return (
            f"🚨 HUMAN-GATE TIMEOUT: Task {task_id} needs escalation "
            f"— gate unanswered for {hours}h"
        )


class DedupChecker:
    """Once-per-24h dedup for human-gate timeout alerts.

    Same pattern as unified-system-alert.sh:
    - First alert for a task passes through
    - Same task within 24h is suppressed
    - After 24h, alert fires again (daily reminder)
    - Different tasks are independent
    """

    DAY_SECONDS = 86400

    def __init__(self, state_file: str):
        self.state_file = state_file
        self.state = self._load_state()

    def _load_state(self) -> dict:
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"alerts": {}}

    def _save_state(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump(self.state, f, indent=2, sort_keys=True)
        except (IOError, OSError):
            pass  # Non-fatal — dedup is best-effort

    def should_alert(self, task_id: str, tier: str) -> bool:
        """Check if an alert should be emitted for this task."""
        alerts = self.state.get("alerts", {})
        if task_id not in alerts:
            return True  # New task — alert

        prev = alerts[task_id]
        last_alerted = prev.get("last_alerted", 0)
        now = time.time()

        if now - last_alerted >= self.DAY_SECONDS:
            return True  # 24h elapsed — daily reminder

        return False  # Within 24h — suppress

    def record_alert(self, task_id: str, tier: str):
        """Record that an alert was emitted for this task."""
        if "alerts" not in self.state:
            self.state["alerts"] = {}
        self.state["alerts"][task_id] = {
            "tier": tier,
            "last_alerted": time.time(),
        }
        self._save_state()


def scan_blocked_tasks(boards_dir: str) -> list:
    """Scan all kanban boards for blocked tasks with human-gate in the block reason.

    For each blocked task, checks the most recent 'blocked' event payload
    for 'human-gate' or 'operator-gate' in the reason.

    Returns a list of dicts with keys:
    - board: board slug name
    - task_id: task ID
    - title: task title
    - block_reason: the reason from the blocked event payload
    - blocked_at: timestamp of the blocked event (epoch seconds)
    """
    tasks = []
    boards_path = Path(boards_dir)

    if not boards_path.exists():
        return tasks

    for db_path in sorted(boards_path.glob("*/kanban.db")):
        board_slug = db_path.parent.name
        if board_slug in ("human-gate", "archive", "archived"):
            continue

        try:
            conn = sqlite3.connect(str(db_path))
            # Find all blocked tasks
            rows = conn.execute(
                "SELECT id, title FROM tasks WHERE status = 'blocked'"
            ).fetchall()

            for tid, title in rows:
                # Check the most recent 'blocked' event for human-gate reason
                event_row = conn.execute(
                    "SELECT payload, created_at FROM task_events "
                    "WHERE task_id = ? AND kind = 'blocked' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (tid,)
                ).fetchone()

                if not event_row:
                    continue

                payload, blocked_at = event_row
                if not payload:
                    continue

                payload_str = str(payload).lower()
                if "human-gate" not in payload_str and "operator-gate" not in payload_str:
                    continue

                # Extract reason from payload (JSON or plain text)
                reason = payload
                try:
                    p = json.loads(payload)
                    if isinstance(p, dict):
                        reason = p.get("reason", p.get("description", payload))
                except (json.JSONDecodeError, TypeError):
                    pass

                tasks.append({
                    "board": board_slug,
                    "task_id": tid,
                    "title": title or "",
                    "block_reason": reason,
                    "blocked_at": blocked_at or 0,
                })

            conn.close()
        except sqlite3.Error:
            continue  # Skip corrupt/inaccessible databases

    return tasks