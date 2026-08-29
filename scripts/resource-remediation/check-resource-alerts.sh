#!/bin/bash
# check-resource-alerts.sh — Gate script for resource-remediation-consultant cron
#
# ── Cron Integration ──────────────────────────────────────────────────────────
# This script is the gate for the resource-remediation-consultant cron job
# (cron ID: 8461da7a7fb1, profile: manager). It runs before the LLM consultant
# is invoked. Its stdout is injected into the agent's prompt as script context.
#
# Flow:
#   1. Cron fires → runs this script
#   2. Script checks /tmp/unified-system-alert-state.json for active alerts
#   3. If NO_ALERTS → agent prompt gets "NO_ALERTS" → agent stays silent
#   4. If alerts → script gathers diagnostics (ps, du, vmstat, kalman, proxy)
#      → agent prompt gets real data → agent produces remediation advice
#
# State file is written by the unified-system-monitor (separate process).
# This script only READS it — never writes or modifies it.
#
# Deployed at: ~/.hermes/profiles/manager/scripts/check-resource-alerts.sh
# Version controlled at: bot repo scripts/resource-remediation/
# ─────────────────────────────────────────────────────────────────────────────
#
# Reads unified-system-alert state. If no alerts → output "NO_ALERTS" (agent stays silent).
# If alerts exist → pre-runs diagnostics and outputs JSON context for the LLM consultant.
# The LLM then has real data to reason about, not just "RAM is high".
#
# Output goes to the cron's script field → injected into agent prompt.

set -uo pipefail

# Resource throttle (Felix 2026-08-29): never compete with urgent work.
# nice -n 19 = LOWEST CPU priority (positive = nicer). Inherited by all children.
# ionice -c3 = idle I/O class: disk access only when nothing else wants it.
renice -n 19 -p $$ >/dev/null 2>&1
ionice -c3 -p $$ >/dev/null 2>&1

STATE_FILE="/tmp/unified-system-alert-state.json"

if [ ! -f "$STATE_FILE" ]; then
  echo "NO_ALERTS"
  exit 0
fi

# Extract active alerts from state file
ALERTS_JSON=$(python3 -c "
import json
try:
    state = json.load(open('$STATE_FILE'))
    alerts = state.get('alerts', {})
    active = {k: v for k, v in alerts.items() if v.get('severity')}
    if active:
        print(json.dumps(active, indent=2))
    else:
        print('NO_ALERTS')
except:
    print('NO_ALERTS')
" 2>/dev/null)

if [ "$ALERTS_JSON" = "NO_ALERTS" ]; then
  echo "NO_ALERTS"
  exit 0
fi

# Alerts exist — gather diagnostic data for each category
echo "ACTIVE_ALERTS:"
echo "$ALERTS_JSON"
echo ""
echo "DIAGNOSTICS:"

# Memory pressure: top 10 memory hogs
echo "--- TOP_MEMORY (ps aux --sort=-%mem | head -10) ---"
ps aux --sort=-%mem 2>/dev/null | head -10

# CPU pressure: top 10 CPU hogs
echo "--- TOP_CPU (ps aux --sort=-%cpu | head -10) ---"
ps aux --sort=-%cpu 2>/dev/null | head -10

# Disk pressure: largest dirs + largest files in /tmp
echo "--- DISK_USAGE (du -sh /var/log /tmp /home 2>/dev/null) ---"
du -sh /var/log /tmp /home 2>/dev/null | sort -rh | head -5
echo "--- LARGE_TMP_FILES (find /tmp -type f -size +100M 2>/dev/null | head -10) ---"
find /tmp -type f -size +100M 2>/dev/null | head -10
echo "--- DISK_FREE (df -h / /home 2>/dev/null) ---"
df -h / /home 2>/dev/null

# Swap thrashing: vmstat
echo "--- VMSTAT (vmstat 1 3) ---"
vmstat 1 3 2>/dev/null | tail -1

# Kalman predictions (if available)
PREDICT_SCRIPT="$HOME/.hermes/profiles/manager/scripts/kalman-resource-predict.sh"
if [ -x "$PREDICT_SCRIPT" ]; then
  echo "--- KALMAN_PREDICTIONS ---"
  bash "$PREDICT_SCRIPT" 2>/dev/null
fi

# Proxy health
echo "--- PROXY (curl -sf localhost:9099/health 2>/dev/null) ---"
curl -sf localhost:9099/health 2>/dev/null || echo "proxy unreachable"

# Dispatch daemon
echo "--- DISPATCH (systemctl --user is-active hermes-dispatch 2>/dev/null) ---"
systemctl --user is-active hermes-dispatch 2>/dev/null || echo "dispatch not active"

echo "--- END_DIAGNOSTICS ---"
exit 0
