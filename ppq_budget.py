#!/usr/bin/env python3
"""ppq_budget - Track PPQ daily spend and manage budget.

Tracks daily spend, handles budget resets, stores corrections, and
provides budget status queries.
"""
from __future__ import annotations
import json
from datetime import datetime, UTC
from pathlib import Path
from typing import Optional

HOME = Path.home()
USAGE_FILE = HOME / ".hermes" / "bot" / "ppq_usage.json"
CORRECTIONS_FILE = HOME / ".hermes" / "bot" / "ppq_corrections.json"
DAILY_LIMIT = 1.00  # USD per day

def _load_usage() -> dict:
    """Load usage data."""
    if not USAGE_FILE.exists():
        return {
            "daily_spend": 0.0,
            "daily_limit": DAILY_LIMIT,
            "reset_at": None,
            "usage_history": []
        }
    
    return json.loads(USAGE_FILE.read_text())

def _save_usage(data: dict):
    """Save usage data."""
    USAGE_FILE.write_text(json.dumps(data, indent=2))

def _check_and_reset():
    """Check if reset needed (midnight UTC)."""
    data = _load_usage()
    
    if not data.get("reset_at"):
        today_midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_midnight = today_midnight.replace(day=today_midnight.day + 1)
        data["reset_at"] = tomorrow_midnight.isoformat()
        data["daily_spend"] = 0.0
        data["usage_history"] = []
        _save_usage(data)
        return
    
    reset_time = datetime.fromisoformat(data["reset_at"])
    now = datetime.now(UTC)
    
    if now > reset_time:
        next_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        data["reset_at"] = next_day.replace(day=next_day.day + 1).isoformat()
        data["daily_spend"] = 0.0
        data["usage_history"] = []
        _save_usage(data)

def record_usage(cost_usd: float, model: str, tokens: int = 0):
    """Record PPQ usage."""
    _check_and_reset()
    data = _load_usage()
    
    data["daily_spend"] += cost_usd
    data["usage_history"].append({
        "timestamp": datetime.now(UTC).isoformat(),
        "model": model,
        "cost_usd": round(cost_usd, 6),
        "tokens": tokens
    })
    
    _save_usage(data)

def get_budget_status() -> dict:
    """Get current budget status."""
    _check_and_reset()
    data = _load_usage()
    
    return {
        "daily_spend": data.get("daily_spend", 0.0),
        "daily_limit": data.get("daily_limit", DAILY_LIMIT),
        "remaining": max(0.0, data.get("daily_limit", DAILY_LIMIT) - data.get("daily_spend", 0.0)),
        "reset_at": data.get("reset_at")
    }

def can_afford(cost_usd: float) -> bool:
    """Check if we can afford this cost."""
    status = get_budget_status()
    return status["remaining"] >= cost_usd

def record_correction(timestamp: str, actual_cost: float, predicted_cost: float):
    """Record a cost correction (actual != predicted)."""
    if not CORRECTIONS_FILE.exists():
        CORRECTIONS_FILE.write_text(json.dumps({"corrections": [], "last_updated": None}, indent=2))
    
    data = json.loads(CORRECTIONS_FILE.read_text())
    data["corrections"].append({
        "timestamp": timestamp,
        "actual_cost": actual_cost,
        "predicted_cost": predicted_cost,
        "delta": actual_cost - predicted_cost
    })
    data["last_updated"] = datetime.now(UTC).isoformat()
    
    CORRECTIONS_FILE.write_text(json.dumps(data, indent=2))

def get_corrections() -> list:
    """Get all corrections."""
    if not CORRECTIONS_FILE.exists():
        return []
    
    data = json.loads(CORRECTIONS_FILE.read_text())
    return data.get("corrections", [])

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="PPQ Budget Tracker")
    parser.add_argument("--status", action="store_true", help="Show budget status")
    parser.add_argument("--record", type=float, metavar="COST", help="Record usage cost")
    parser.add_argument("--model", type=str, metavar="MODEL", help="Model name (for recording)")
    parser.add_argument("--tokens", type=int, metavar="TOKENS", default=0, help="Token count (for recording)")
    
    args = parser.parse_args()
    
    if args.status:
        status = get_budget_status()
        print(f"Daily Limit: ${status['daily_limit']:.2f}")
        print(f"Daily Spend: ${status['daily_spend']:.6f}")
        print(f"Remaining:   ${status['remaining']:.6f}")
        print(f"Resets at:   {status['reset_at']}")
    elif args.record:
        record_usage(args.record, args.model or "unknown", args.tokens)
        print(f"Recorded ${args.record:.6f} for {args.model or 'unknown'}")
    else:
        parser.print_help()