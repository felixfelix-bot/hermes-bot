#!/usr/bin/env python3
"""key_rotate — auto-rotate between z.ai API keys based on quota health.

Runs as a standalone systemd user timer (NOT inside the gateway). Checks both keys'
quotas every 10 min, writes the healthiest as ZAI_API_KEY in the manager .env,
and restarts the gateway ONLY when a swap is needed. Hysteresis prevents thrashing.

State: ~/.hermes/bot/key_rotation.json (which key is active + both quotas).
"""
from __future__ import annotations
import json, os, subprocess, time, urllib.request
from pathlib import Path

KEYS = {
    "ours": os.environ.get("ZAI_OUR_KEY", "943020a0b95a46b2b0d9a43a59a2b38c.eVibSHrRt9SHV0N7"),
    "friend": os.environ.get("ZAI_FALLBACK_API_KEY", "038e51301df14dee85d85d82027ade69.oljMmlmipcnrdnoX"),
}
THRESHOLDS = {"ours": 80, "friend": 40}  # swap away when above; friend is more conservative
QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
ENV = Path.home() / ".hermes" / "profiles" / "manager" / ".env"
STATE = Path.home() / ".hermes" / "bot" / "key_rotation.json"
HYSTERESIS = 20  # only swap if other key is >20pp better


def quota(key: str) -> int:
    try:
        req = urllib.request.Request(QUOTA_URL, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        pcts = [L.get("percentage", 0) for L in data["data"]["limits"] if L.get("type") == "TOKENS_LIMIT"]
        return max(pcts) if pcts else 0
    except Exception:
        return 999


def current_key() -> str | None:
    if not ENV.exists():
        return None
    for line in ENV.read_text().splitlines():
        if line.startswith("ZAI_API_KEY=") and "ZAI_OUR_KEY" not in line:
            return line.split("=", 1)[1].split("#")[0].strip()
    return None


def main() -> int:
    quotas = {n: quota(k) for n, k in KEYS.items()}
    cur = current_key()
    cur_name = next((n for n, k in KEYS.items() if k == cur), "unknown")
    best_name = min(quotas, key=quotas.get)
    best_key = KEYS[best_name]

    # write state
    STATE.write_text(json.dumps({"ts": int(time.time()), "active": cur_name,
                                 "quotas": quotas, "active_key_pct": quotas.get(cur_name, -1)}, indent=2))

    # should we swap?
    should_swap = False
    reason = ""
    if cur_name in THRESHOLDS and quotas[cur_name] >= THRESHOLDS[cur_name]:
        # current key is above threshold — consider swapping
        if quotas[best_name] < quotas[cur_name] - HYSTERESIS:
            should_swap = True
            reason = f"{cur_name} at {quotas[cur_name]}% (≥{THRESHOLDS[cur_name]}%), {best_name} at {quotas[best_name]}%"
        else:
            reason = f"{cur_name} at {quotas[cur_name]}% but {best_name} not significantly better ({quotas[best_name]}%)"
    else:
        reason = f"{cur_name} healthy at {quotas.get(cur_name, -1)}%"

    if not should_swap:
        return 0  # silent, no swap

    # swap the key in .env
    lines = ENV.read_text().splitlines()
    new = [f"ZAI_API_KEY={best_key}  # auto-rotated to {best_name} ({quotas[best_name]}%)" 
           if (l.startswith("ZAI_API_KEY=") and "ZAI_OUR_KEY" not in l) else l 
           for l in lines]
    ENV.write_text("\n".join(new) + "\n")

    # restart gateway (safe: we run OUTSIDE the gateway process)
    subprocess.run(["systemctl", "--user", "restart", "hermes-gateway.service"], timeout=30)

    print(f"🔄 KEY ROTATION: {reason} → switched to '{best_name}' key ({quotas[best_name]}% quota)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
