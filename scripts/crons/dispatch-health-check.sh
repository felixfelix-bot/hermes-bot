#!/usr/bin/env bash
# dispatch-health-check.sh — Cron safety net for the adaptive dispatch daemon.
# If the daemon PID file is missing or process dead, restart via systemd.
#
# Dedup (alert_dedup.py): a persistently-dead OR persistently-alive daemon is
# reported on an exponential backoff so we're not pinged every 10m with the
# same status. The restart ACTION always runs; only the notification is gated.
# Fail-open: if the dedup module is missing/errors, we report normally.
set -u
PID_FILE="/tmp/adaptive-dispatch.pid"
DEDUP="$HOME/.hermes/profiles/manager/scripts/alert_dedup.py"
SRC="dispatch-health-check"

PID=""
[ -f "$PID_FILE" ] && PID=$(cat "$PID_FILE" 2>/dev/null)

if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
    MSG="DAEMON_DEAD at $(date) — restarting via systemd"
    KEY="daemon-dead"          # stable: same every cycle while dead
    systemctl --user restart hermes-dispatch 2>/dev/null
else
    MSG="Daemon alive (PID $PID)"
    KEY="daemon-alive"         # stable: same every cycle while alive (PID varies)
fi

# Dedup gate on the stable status key. exit 0=report, 1=suppress, 2+=error.
if [ -x "$DEDUP" ]; then
    printf '%s' "$KEY" | python3 "$DEDUP" gate --source "$SRC" >/dev/null 2>&1
    rc=$?
    [ "$rc" -eq 1 ] && exit 0   # same status, backoff active -> silent
    # rc 0 (notify) or rc 2+ (error -> fail-open) -> fall through to echo
fi

echo "$MSG"
exit 0
