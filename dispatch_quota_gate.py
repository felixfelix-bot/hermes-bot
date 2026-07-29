#!/usr/bin/env python3
"""dispatch_quota_gate — preflight check before spawning kanban workers.

Queries proxy /quota endpoint, checks quota state + peak hours,
returns whether dispatch is safe and which model tier to recommend.

Used by kanban dispatch daemon as a preflight gate.
Can also be used by cron jobs to decide whether to dispatch.

Usage:
  python3 dispatch_quota_gate.py
  python3 dispatch_quota_gate.py --json  # default

Returns JSON:
  {"safe": true/false, "reason": "...", "quota_state": "MODERATE",
   "recommended_tier": "air", "peak_hours": true, "hours_left_best": 24.5}
"""

from __future__ import annotations
import json
import sys
import urllib.request
import datetime

QUOTA_ENDPOINT = "http://localhost:9099/quota"


def check() -> dict:
    """Check if it's safe to dispatch workers right now."""
    try:
        resp = urllib.request.urlopen(QUOTA_ENDPOINT, timeout=5)
        data = json.loads(resp.read())
    except Exception as e:
        return {
            "safe": True,  # Default safe — don't block dispatch on proxy failure
            "reason": f"quota_unreachable_{e}",
            "quota_state": "UNKNOWN",
            "recommended_tier": "flash",
            "peak_hours": False,
            "hours_left_best": 0,
        }

    hour = datetime.datetime.utcnow().hour
    peak = 6 <= hour < 10

    # Check which keys are available (not locked)
    available_keys = []
    for name in ["ours", "friend"]:
        k = data.get(name, {})
        if not k.get("locked", True):
            available_keys.append(name)

    if not available_keys:
        return {
            "safe": False,
            "reason": "all_keys_locked",
            "quota_state": "CRITICAL",
            "recommended_tier": "flash",
            "peak_hours": peak,
            "hours_left_best": 0,
        }

    # Find best hours_left across all available keys
    best_hours = 0
    best_key = None
    lowest_used_pct = 100  # fallback when hours_left unavailable
    
    for name in available_keys:
        k = data.get(name, {})
        for w in k.get("windows", []):
            hl = w.get("hours_left")
            up = w.get("used_pct", 100)
            if up < lowest_used_pct:
                lowest_used_pct = up
            if hl is not None and hl > best_hours:
                best_hours = hl
                best_key = name
    
    # If hours_left unavailable from proxy, estimate from used_pct
    if best_hours == 0 and available_keys:
        # Friend 5-hour: ~5h window. At 1% used ≈ 4.95h left
        key_name = available_keys[0]
        best_key = key_name
        k = data.get(key_name, {})
        for w in k.get("windows", []):
            name = w.get("name", "")
            up = w.get("used_pct", 100)
            if name == "5-hour":
                best_hours = max(0.01, 5 * (1 - up / 100))
            elif name == "weekly":
                best_hours = max(best_hours, 168 * (1 - up / 100))
            elif name == "monthly":
                best_hours = max(best_hours, 720 * (1 - up / 100))
    
    # Determine quota state
    will_exhaust = False
    for name in available_keys:
        k = data.get(name, {})
        for w in k.get("windows", []):
            if w.get("will_exhaust", False):
                will_exhaust = True
                break

    if best_hours > 48 and not will_exhaust:
        state = "PLENTYFUL"
    elif best_hours > 12:
        state = "MODERATE"
    elif best_hours > 2:
        state = "TIGHT"
    else:
        state = "CRITICAL"

    # Recommended tier
    if state == "CRITICAL":
        rec = "flash"
    elif peak:
        rec = "air"
    elif state == "TIGHT":
        rec = "air"
    else:
        rec = "heavy"

    return {
        "safe": len(available_keys) > 0,
        "reason": f"key={best_key}_state={state}" if best_key else "no_available_key",
        "quota_state": state,
        "recommended_tier": rec,
        "peak_hours": peak,
        "hours_left_best": best_hours,
        "available_keys": available_keys,
    }


def main():
    result = check()
    if "--json" in sys.argv or len(sys.argv) == 1:
        print(json.dumps(result))
    else:
        # Human-readable output
        status = "SAFE" if result["safe"] else "BLOCKED"
        print(f"[{status}] quota={result['quota_state']} peak={result['peak_hours']}")
        print(f"  recommended_tier={result['recommended_tier']} hours_left={result['hours_left_best']:.1f}")
        print(f"  reason={result['reason']}")
        if result.get("available_keys"):
            print(f"  available_keys={','.join(result['available_keys'])}")


if __name__ == "__main__":
    main()
