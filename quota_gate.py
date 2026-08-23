#!/usr/bin/env python3
"""quota_gate — preflight check before dispatching kanban workers.

Queries the proxy's /quota endpoint and determines whether dispatch
is safe based on quota headroom. Returns exit code 0 = OK, 1 = BLOCKED.

Usage:
    python3 quota_gate.py                           # check quota
    python3 quota_gate.py --task-tier heavy          # check for specific tier
    python3 quota_gate.py --verbose                  # detailed output
    python3 quota_gate.py --deferred                 # list deferred tiers
"""

from __future__ import annotations
import json, sys, urllib.request

QUOTA_URL = "http://localhost:9099/quota"
PROXY_HEALTH_URL = "http://localhost:9099/health"

# Minimum hours_left thresholds per quota state
STATE_THRESHOLDS = {
    "PLENTYFUL": 48,
    "MODERATE": 12,
    "TIGHT": 2,
    "CRITICAL": 0,
}

# Allowed tiers per quota state
ALLOWED_TIERS = {
    "PLENTYFUL": {"flash", "air", "mid", "heavy"},
    "MODERATE":  {"flash", "air", "mid"},
    "TIGHT":     {"flash", "air"},
    "CRITICAL":  {"flash"},
}


def _fetch_json(url: str, timeout: int = 5) -> dict:
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def check_quota() -> dict:
    """Return dict with quota gate verdict.

    Returns:
        {
            "ok": bool,          # True if dispatch should proceed
            "quota_state": str,  # PLENTYFUL|MODERATE|TIGHT|CRITICAL
            "peak_hours": bool,
            "reason": str,       # human-readable
            "min_hours_left": float|None,
            "keys": {...}        # per-key data
        }
    """
    data = _fetch_json(QUOTA_URL)
    if "error" in data:
        return {"ok": True, "reason": f"proxy unreachable: {data['error']} — dispatching anyway",
                "quota_state": "UNKNOWN", "peak_hours": False}

    # Check proxy health
    try:
        health_resp = urllib.request.urlopen(PROXY_HEALTH_URL, timeout=3)
        health_text = health_resp.read().decode().strip()
        proxy_dead = health_text != "ok"
    except Exception:
        proxy_dead = True

    # Compute min estimated hours_left across both keys
    min_hours = float("inf")
    keys_info = {}
    for key_name in ("ours", "friend"):
        key_data = data.get(key_name, {})
        windows = key_data.get("windows", [])
        locked = key_data.get("locked", True)
        max_pct = max((w.get("used_pct", 0) for w in windows), default=0)

        # Find min estimated hours_left for this key
        key_min_hours = float("inf")
        for w in windows:
            wh = w.get("window_hours")
            pct = w.get("used_pct", 0)
            if wh and pct is not None:
                # Estimate hours remaining from percentage used
                elapsed = wh * pct / 100.0
                remaining = wh - elapsed
                key_min_hours = min(key_min_hours, remaining)
            # Also check explicit hours_left if provided
            hl = w.get("hours_left")
            if hl is not None:
                key_min_hours = min(key_min_hours, hl)

        keys_info[key_name] = {
            "locked": locked,
            "max_pct": max_pct,
            "min_hours_left": None if key_min_hours == float("inf") else key_min_hours,
            "windows": windows,
        }
        if key_min_hours != float("inf"):
            min_hours = min(min_hours, key_min_hours)

    # Determine quota state
    if min_hours == float("inf") or proxy_dead:
        quota_state = "MODERATE"  # conservative default when no data
    elif min_hours > 48:
        quota_state = "PLENTYFUL"
    elif min_hours > 12:
        quota_state = "MODERATE"
    elif min_hours > 2:
        quota_state = "TIGHT"
    else:
        quota_state = "CRITICAL"

    # Check peak hours
    import datetime
    h = datetime.datetime.now(datetime.timezone.utc).hour
    peak = 6 <= h < 10

    # Determine if dispatch is viable.
    # The proxy marks a key "locked" when ANY window hits its threshold.
    # But the proxy still serves traffic via the least-bad key even when
    # both are "locked". We only block dispatch when both keys are truly
    # exhausted (max_pct >= 95 on their critical windows).
    def truly_exhausted(info: dict) -> bool:
        """A key is truly exhausted if its limiting window is near 100%."""
        for w in info.get("windows", []):
            pct = w.get("used_pct", 0)
            name = w.get("name", "")
            # For friend key, 5-hour at 80%+ = throttled; weekly at 80%+ = tight
            if info.get("name", "") == "friend" and name == "5-hour":
                if pct >= 95:
                    return True
            elif pct >= 95:
                return True
        return False

    both_truly_exhausted = all(
        truly_exhausted(keys_info.get(k, {}))
        for k in ("ours", "friend")
    ) if keys_info else False

    ok = not both_truly_exhausted and not proxy_dead

    reasons = []
    if both_truly_exhausted:
        reasons.append("both keys exhausted")
    if peak:
        reasons.append("peak hours")
    if quota_state == "CRITICAL":
        reasons.append("CRITICAL quota")
    if proxy_dead:
        reasons.append("proxy dead")

    return {
        "ok": ok,
        "quota_state": quota_state,
        "peak_hours": peak,
        "min_hours_left": None if min_hours == float("inf") else min_hours,
        "keys": keys_info,
        "reason": "; ".join(reasons) if reasons else "OK",
    }


def task_tier_is_viable(tier: str, quota_state: str, peak: bool) -> tuple[bool, str]:
    """Check if a task with given model_tier can be dispatched."""
    tier = tier.lower()
    if tier not in {"flash", "air", "mid", "heavy"}:
        return False, f"unknown tier: {tier}"

    allowed = ALLOWED_TIERS.get(quota_state, {"flash"})
    if peak:
        # Peak hours: cap at air
        peak_allowed = {"flash", "air"}
        allowed = allowed & peak_allowed

    if tier not in allowed:
        # Task tier not available — but maybe a cheaper tier can serve it?
        # Check if any tier >= task_tier is allowed
        tier_order = ["flash", "air", "mid", "heavy"]
        tier_idx = tier_order.index(tier)
        viable = [t for t in tier_order[tier_idx:] if t in allowed]
        if viable:
            return True, f"upgrade available: {tier}→{viable[0]} (cheaper model)"
        return False, f"{tier} not available in {quota_state}{' (peak)' if peak else ''}"
    return True, "OK"


def main():
    args = sys.argv[1:]
    verbose = "--verbose" in args or "-v" in args
    show_deferred = "--deferred" in args

    result = check_quota()

    if verbose or show_deferred:
        print(f"quota_state={result['quota_state']}")
        print(f"peak_hours={result['peak_hours']}")
        print(f"min_hours_left={result['min_hours_left']}")
        print(f"reason={result['reason']}")
        for key_name, info in result.get("keys", {}).items():
            print(f"  {key_name}: locked={info['locked']} max_pct={info['max_pct']}% "
                  f"hours_left={info['min_hours_left']}")
        if show_deferred:
            print(f"\nDeferred tiers in {result['quota_state']}:")
            for t in ["mid", "heavy"]:
                viable, msg = task_tier_is_viable(t, result["quota_state"], result["peak_hours"])
                if not viable:
                    print(f"  {t}: {msg}")
        print()
    elif not result["ok"]:
        print(result["reason"])
    else:
        # Quiet mode: just print quota_state for scripting
        print(result["quota_state"])

    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
