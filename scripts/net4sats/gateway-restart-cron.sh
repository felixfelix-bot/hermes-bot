#!/bin/bash
# gateway-restart-cron.sh — called by hermes cron --no-agent --script
# Runs hermes gateway restart from OUTSIDE the gateway process.
# This script IS the job — its stdout is delivered verbatim.
# Empty stdout = silent (nothing happens visible to user).
# Non-empty = alert delivered.

set -eu

LOG="/tmp/gateway-restart-$(date +%s).log"

# Capture output
OUTPUT=$(hermes gateway restart 2>&1) || true
echo "$OUTPUT" > "$LOG"

# Only report if something went wrong (empty = silent on success)
if echo "$OUTPUT" | grep -qi "error\|fail\|refused\|cannot"; then
    echo "⚠️ Gateway restart may have failed. Log: $LOG"
    echo "Output: $OUTPUT"
else
    # Silent on success (empty stdout = no notification)
    :
fi
