#!/bin/bash
# Cancel any pending scheduled gateway restart
set -eu

BEFORE=$(crontab -l 2>/dev/null | grep -c "gateway-restart-" || true)

crontab -l 2>/dev/null | grep -v "gateway-restart-" | crontab -

AFTER=$(crontab -l 2>/dev/null | grep -c "gateway-restart-" || true)

echo "✅ Cancelled ${BEFORE} pending gateway restart(s). ${AFTER} remaining."
