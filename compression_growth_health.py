#!/usr/bin/env python3
"""Health check for the compression growth governor.

Prints:
- Current Kalman state (x, p, q, r, n)
- Last measurement and timestamp
- Threshold history (last 10 changes)
- State file existence/validity
- Audit file contents

Usage:
    python3 compression_growth_health.py
"""
from __future__ import annotations

import json
import time
from pathlib import Path

BOT_DIR = Path.home() / ".hermes" / "bot"
STATE_FILE = BOT_DIR / "compression_growth_state.json"
AUDIT_FILE = BOT_DIR / "compression_growth_override.json"


def main():
    print("=" * 60)
    print("  Compression Growth Governor — Health Check")
    print("=" * 60)

    # --- State file ---
    print("\n--- State File ---")
    print(f"  Path: {STATE_FILE}")
    if not STATE_FILE.exists():
        print("  ❌ State file MISSING")
        return

    try:
        state = json.loads(STATE_FILE.read_text())
        print("  ✅ State file is valid JSON")
    except Exception as e:
        print(f"  ❌ State file is corrupt: {e}")
        return

    kalman = state.get("kalman", {})
    print(f"\n  Kalman State:")
    print(f"    x (estimate):    {kalman.get('x', 'N/A')}")
    print(f"    p (uncertainty): {kalman.get('p', 'N/A')}")
    print(f"    q (proc noise):  {kalman.get('q', 'N/A')}")
    print(f"    r (meas noise):  {kalman.get('r', 'N/A')}")
    print(f"    n (updates):     {kalman.get('n', 'N/A')}")

    print(f"\n  Last measurement:  {state.get('last_measurement', 'N/A')}")
    last_ts = state.get("last_ts", 0)
    if last_ts:
        age_min = (time.time() - last_ts) / 60
        print(f"  Last run:          {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_ts))} ({age_min:.0f} min ago)")
    else:
        print(f"  Last run:          Never")

    print(f"  Current threshold: {state.get('current_threshold', 'N/A')}")
    print(f"  Context length:    {state.get('context_length', 'N/A')}")

    # --- Threshold history ---
    history = state.get("threshold_history", [])
    print(f"\n--- Threshold History (last 10) ---")
    if not history:
        print("  (no history yet)")
    else:
        for entry in history[-10:]:
            ts_str = time.strftime("%H:%M:%S", time.localtime(entry.get("ts", 0)))
            old = entry.get("old_threshold", "?")
            new = entry.get("new_threshold", "?")
            applied = "APPLIED" if entry.get("applied") else "skipped"
            print(f"  {ts_str}  {old} → {new}  [{applied}]")

    # --- Audit file ---
    print(f"\n--- Audit File ---")
    print(f"  Path: {AUDIT_FILE}")
    if not AUDIT_FILE.exists():
        print("  ⚠️  Audit file MISSING (governor may not have run)")
    else:
        try:
            audit = json.loads(AUDIT_FILE.read_text())
            print("  ✅ Audit file is valid JSON")
            for k, v in audit.items():
                if k == "updated_at":
                    print(f"    {k}: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(v))}")
                else:
                    print(f"    {k}: {v}")
        except Exception as e:
            print(f"  ❌ Audit file is corrupt: {e}")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
