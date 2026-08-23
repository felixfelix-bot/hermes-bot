#!/bin/bash
# Schedule a gateway restart with configurable delay (default: 1 minute)
# This must run OUTSIDE the gateway process tree — uses one-shot cron
# Usage: gateway-restart.sh [delay_minutes] [--reason "some reason"]

set -eu

DELAY="${1:-1}"
shift || true
REASON=""
if [[ "${1:-}" == "--reason" ]]; then
    REASON="$2"
    shift 2
fi

LABEL="gateway-restart-$(date +%s)"
TIMESTAMP=$(date -d "+${DELAY} minutes" '+%M %H %d %m *')

# Create the restart command (runs from a fresh shell, not gateway child)
RESTART_CMD="hermes gateway restart > /tmp/gateway-restart-${LABEL}.log 2>&1"

# Write one-shot cron entry
CRON_LINE="${TIMESTAMP} ${RESTART_CMD} # ${LABEL}"

(crontab -l 2>/dev/null || true; echo "$CRON_LINE") | crontab -

echo "✅ Gateway restart scheduled in ${DELAY} minute(s)."
echo "   Label: ${LABEL}"
if [ -n "$REASON" ]; then
    echo "   Reason: ${REASON}"
fi
echo "   The gateway will restart at ~$(date -d "+${DELAY} minutes" '+%H:%M')."
echo "   To cancel: ~/scripts/gateway-restart-cancel.sh"
