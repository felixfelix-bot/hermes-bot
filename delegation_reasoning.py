#!/usr/bin/env python3
"""delegation_reasoning - Value-per-token reasoning for delegation.

Estimates tokens, detects blocking tasks, determines if PPQ should be used,
and manages approval requests.
"""
from __future__ import annotations
import json
import re
from datetime import datetime, UTC
from pathlib import Path
from typing import Optional, Dict, Any
import subprocess

from ppq_budget import get_budget_status, can_afford, record_correction

HOME = Path.home()
CORRECTIONS_FILE = HOME / ".hermes" / "bot" / "ppq_corrections.json"
PPQ_COST_PER_1M_TOKENS = 0.15  # DeepSeek V4 Flash on PPQ

TOKEN_ESTIMATION_RATIOS = {
    "coding": 0.75,
    "reasoning": 1.2,
    "math": 1.1,
    "research": 1.3,
    "general": 1.0,
}

BLOCKING_KEYWORDS = [
    "urgent", "blocking", "blocker", "critical", "asap", "immediately",
    "today", "now", "stuck", "error", "failed", "broken", "fix",
    "deadline", "production", "deploy", "release", "hotfix"
]

def estimate_tokens(message: str, task_type: str = "general") -> int:
    """Estimate token count from message."""
    word_count = len(message.split())
    char_count = len(message)
    
    base_tokens = max(word_count, char_count // 4)
    multiplier = TOKEN_ESTIMATION_RATIOS.get(task_type.lower(), 1.0)
    
    estimated = int(base_tokens * multiplier)
    
    if estimated < 100:
        estimated = 100
    elif estimated > 500000:
        estimated = 500000
    
    return estimated

def detect_blocking(message: str, explicit_flag: bool = False) -> tuple[bool, str]:
    """Detect if task is blocking."""
    if explicit_flag:
        return True, "explicit flag"
    
    message_lower = message.lower()
    for keyword in BLOCKING_KEYWORDS:
        if keyword in message_lower:
            return True, f"keyword: {keyword}"
    
    if "error" in message_lower or "failed" in message_lower or "broken" in message_lower:
        return True, "error detected"
    
    return False, ""

def should_use_ppq(
    message: str,
    task_type: str = "general",
    explicit_blocking: bool = False,
    zai_quota_available: bool = True,
    priority: str = "balanced"
) -> dict:
    """Determine if PPQ should be used for this task."""
    blocking_detected, blocking_reason = detect_blocking(message, explicit_blocking)
    
    estimated_tokens = estimate_tokens(message, task_type)
    estimated_cost = (estimated_tokens / 1_000_000) * PPQ_COST_PER_1M_TOKENS
    
    budget_status = get_budget_status()
    can_afford_cost = can_afford(estimated_cost)
    
    task_value = "unknown"
    if blocking_detected:
        task_value = "high"
    elif priority == "speed":
        task_value = "high"
    elif priority == "economy":
        task_value = "low"
    else:
        task_value = "medium"
    
    if not zai_quota_available:
        if blocking_detected and can_afford_cost:
            return {
                "use_ppq": True,
                "reason": f"Blocking task ({blocking_reason}) + no zai quota",
                "estimated_tokens": estimated_tokens,
                "estimated_cost_usd": round(estimated_cost, 6),
                "task_value": task_value,
                "requires_approval": False
            }
        elif task_value == "high" and estimated_tokens < 50000 and can_afford_cost:
            return {
                "use_ppq": True,
                "reason": "High value low token task + no zai quota",
                "estimated_tokens": estimated_tokens,
                "estimated_cost_usd": round(estimated_cost, 6),
                "task_value": task_value,
                "requires_approval": False
            }
        else:
            return {
                "use_ppq": False,
                "reason": "Use free quota or queue (low value or high cost)",
                "estimated_tokens": estimated_tokens,
                "estimated_cost_usd": round(estimated_cost, 6),
                "task_value": task_value,
                "requires_approval": False
            }
    
    if blocking_detected:
        if can_afford_cost:
            return {
                "use_ppq": True,
                "reason": f"Blocking task ({blocking_reason})",
                "estimated_tokens": estimated_tokens,
                "estimated_cost_usd": round(estimated_cost, 6),
                "task_value": task_value,
                "requires_approval": False
            }
        else:
            return {
                "use_ppq": True,
                "reason": f"Blocking task ({blocking_reason}) - budget exceeded",
                "estimated_tokens": estimated_tokens,
                "estimated_cost_usd": round(estimated_cost, 6),
                "task_value": task_value,
                "requires_approval": True
            }
    
    if task_value == "high" and estimated_tokens < 50000:
        if can_afford_cost:
            return {
                "use_ppq": True,
                "reason": "High value low token task",
                "estimated_tokens": estimated_tokens,
                "estimated_cost_usd": round(estimated_cost, 6),
                "task_value": task_value,
                "requires_approval": False
            }
        else:
            return {
                "use_ppq": False,
                "reason": "Budget exceeded, use zai quota",
                "estimated_tokens": estimated_tokens,
                "estimated_cost_usd": round(estimated_cost, 6),
                "task_value": task_value,
                "requires_approval": False
            }
    
    if priority == "speed" and can_afford_cost:
        return {
            "use_ppq": True,
            "reason": "Speed priority",
            "estimated_tokens": estimated_tokens,
            "estimated_cost_usd": round(estimated_cost, 6),
            "task_value": task_value,
            "requires_approval": False
        }
    
    if priority == "economy" and zai_quota_available:
        return {
            "use_ppq": False,
            "reason": "Economy priority + zai quota available",
            "estimated_tokens": estimated_tokens,
            "estimated_cost_usd": round(estimated_cost, 6),
            "task_value": task_value,
            "requires_approval": False
        }
    
    if estimated_cost > budget_status["remaining"] * 0.5:
        if can_afford_cost:
            return {
                "use_ppq": True,
                "reason": "High cost task (50%+ of budget)",
                "estimated_tokens": estimated_tokens,
                "estimated_cost_usd": round(estimated_cost, 6),
                "task_value": task_value,
                "requires_approval": True
            }
    
    if zai_quota_available:
        return {
            "use_ppq": False,
            "reason": "Use zai quota (free)",
            "estimated_tokens": estimated_tokens,
            "estimated_cost_usd": round(estimated_cost, 6),
            "task_value": task_value,
            "requires_approval": False
        }
    
    if can_afford_cost:
        return {
            "use_ppq": True,
            "reason": "Use PPQ (budget available)",
            "estimated_tokens": estimated_tokens,
            "estimated_cost_usd": round(estimated_cost, 6),
            "task_value": task_value,
            "requires_approval": False
        }
    
    return {
        "use_ppq": False,
        "reason": "Budget exceeded, queue for free quota",
        "estimated_tokens": estimated_tokens,
        "estimated_cost_usd": round(estimated_cost, 6),
        "task_value": task_value,
        "requires_approval": False
    }

def record_user_decision(
    timestamp: str,
    use_ppq: bool,
    predicted_cost: float,
    actual_cost: float,
    user_approved: bool
):
    """Record a user decision for learning."""
    if actual_cost != predicted_cost:
        record_correction(timestamp, actual_cost, predicted_cost)
    
    if not CORRECTIONS_FILE.exists():
        CORRECTIONS_FILE.write_text(json.dumps({"corrections": [], "last_updated": None}, indent=2))
    
    data = json.loads(CORRECTIONS_FILE.read_text())
    data["corrections"].append({
        "timestamp": timestamp,
        "predicted_use_ppq": use_ppq,
        "predicted_cost": predicted_cost,
        "actual_cost": actual_cost,
        "user_approved": user_approved
    })
    data["last_updated"] = datetime.now(UTC).isoformat()
    
    CORRECTIONS_FILE.write_text(json.dumps(data, indent=2))

def send_matrix_notification(message: str) -> bool:
    """Send Matrix notification for approval requests."""
    try:
        result = subprocess.run(
            ["python3", str(HOME / ".hermes" / "bot" / "matrix_notify.py"), message],
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.returncode == 0
    except Exception as e:
        print(f"Failed to send Matrix notification: {e}")
        return False

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Delegation Reasoning")
    parser.add_argument("--estimate", type=str, metavar="MESSAGE", help="Estimate tokens")
    parser.add_argument("--blocking", type=str, metavar="MESSAGE", help="Detect blocking")
    parser.add_argument("--should-ppq", type=str, metavar="MESSAGE", help="Should use PPQ?")
    parser.add_argument("--task-type", type=str, default="general", help="Task type")
    parser.add_argument("--explicit-blocking", action="store_true", help="Explicit blocking flag")
    parser.add_argument("--no-zai", action="store_true", help="No zai quota available")
    parser.add_argument("--priority", type=str, default="balanced", help="Priority (speed/balanced/economy)")
    
    args = parser.parse_args()
    
    if args.estimate:
        tokens = estimate_tokens(args.estimate, args.task_type)
        print(f"Estimated tokens: {tokens}")
    elif args.blocking:
        blocking, reason = detect_blocking(args.blocking, args.explicit_blocking)
        print(f"Blocking: {blocking}")
        if blocking:
            print(f"Reason: {reason}")
    elif args.should_ppq:
        decision = should_use_ppq(
            args.should_ppq,
            args.task_type,
            args.explicit_blocking,
            not args.no_zai,
            args.priority
        )
        print(json.dumps(decision, indent=2))
    else:
        parser.print_help()