#!/usr/bin/env python3
"""session_health — watchdog that prevents silent-gateway-from-expired-session.

Checks the gateway's session token counts. If any session exceeds 150K tokens
( approaching the expiry threshold ), auto-resets it BEFORE it goes silent.

Also detects sessions marked expiry_finalized=True and resets them so the
gateway stays responsive even after context overflow events.

Runs every 5 minutes via hermes cron. Zero tokens — pure inspection + JSON edit.

Usage:
  hermes cron create --no-agent --script session_health.py
    --name session-health --deliver local "*/5 * * * *"
"""
from __future__ import annotations
import json, os, sys, time
from pathlib import Path

SESSIONS_PATH = Path.home() / ".hermes" / "profiles" / "manager" / "sessions" / "sessions.json"
WARN_TOKENS = 120000
RESET_TOKENS = 150000


def main() -> int:
    if not SESSIONS_PATH.exists():
        return 0

    data = json.loads(SESSIONS_PATH.read_text())
    reset_count = 0
    warnings = []

    for key, session in data.items():
        tokens = session.get("last_prompt_tokens", 0)
        expired = session.get("expiry_finalized", False)
        room = session.get("display_name", key)[:50]

        if expired:
            session["expiry_finalized"] = False
            session["is_fresh_reset"] = True
            session["was_auto_reset"] = True
            session["auto_reset_reason"] = "auto-reset by session_health (expiry detected)"
            session["last_prompt_tokens"] = 0
            reset_count += 1
            print(f"RESET expired session: {room} (was {tokens} tokens)")
        elif tokens > RESET_TOKENS:
            session["expiry_finalized"] = False
            session["is_fresh_reset"] = True
            session["was_auto_reset"] = True
            session["auto_reset_reason"] = f"auto-reset by session_health (token overflow: {tokens})"
            session["last_prompt_tokens"] = 0
            reset_count += 1
            print(f"RESET overflowing session: {room} ({tokens} tokens > {RESET_TOKENS} threshold)")
        elif tokens > WARN_TOKENS:
            warnings.append(f"{room}: {tokens} tokens (approaching limit)")

    if reset_count > 0:
        SESSIONS_PATH.write_text(json.dumps(data, indent=4))
        print(f"\nReset {reset_count} session(s). Session reset only — no gateway restart (gateway rotates sessions natively).")
    elif warnings:
        for w in warnings:
            print(f"WARN: {w}")
    else:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
