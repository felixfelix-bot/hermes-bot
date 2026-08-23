#!/usr/bin/env bash
# anomaly-notify.sh — Deliver unresolved anomaly alerts to the user.
# Runs via cron every 5m (no_agent=true). Silent when nothing to report.
#
# Dedup (alert_dedup.py): an unchanged set of alerts is suppressed on an
# exponential backoff (15m..24h); new/changed alerts notify immediately.
# We gate on the RAW alert payload (most stable: severity+title, no timestamps).
# Fail-open: if the dedup module is missing/errors, alerts are reported.
set -u
METRICS="$HOME/.hermes/profiles/manager/scripts/daemon_metrics.py"
DEDUP="$HOME/.hermes/profiles/manager/scripts/alert_dedup.py"
SRC="anomaly-notify"

OUTPUT=$(/usr/bin/python3 "$METRICS" pending-alerts 2>/dev/null)

if [ "$OUTPUT" != "OK" ] && [ -n "$OUTPUT" ]; then
    # Dedup gate on the raw alert payload. exit 0=report, 1=suppress, 2+=error.
    if [ -x "$DEDUP" ]; then
        printf '%s' "$OUTPUT" | python3 "$DEDUP" gate --source "$SRC" >/dev/null 2>&1
        rc=$?
        [ "$rc" -eq 1 ] && exit 0   # same alerts, backoff active -> silent
        # rc 0 (notify) or rc 2+ (error -> fail-open) -> fall through
    fi

    # Alerts found — format as human-readable summary
    echo "$OUTPUT" | /usr/bin/python3 -c "
import json, sys
data = json.load(sys.stdin)
for a in data:
    icon = {'warning': '⚠️ ', 'critical': '🚨', 'info': 'ℹ️'}.get(a.get('severity','info'), 'ℹ️')
    print(f'{icon} **{a[\"severity\"].upper()}** — {a[\"title\"]}')
    print(f'   {a[\"detail\"]}')
    print()
" 2>/dev/null
fi
exit 0
