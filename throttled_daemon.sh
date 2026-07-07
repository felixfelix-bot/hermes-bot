#!/usr/bin/env bash
# throttled_daemon — one dispatch tick. systemd user timer calls this every 60s.
# Gate: skip entirely while z.ai throttle==true. Then dispatch active profiles
# (ranked, capped at 3). Logs a usage row. SAFE while all profiles are `pending`
# (zero dispatch). See docs/DISPATCHER.md.
set -u
BOT="$HOME/.hermes/bot"
LOG="$BOT/usage.csv"
PY="$(command -v python3 || echo "$HOME/.hermes/hermes-agent/venv/bin/python")"
mkdir -p "$BOT"
[ -f "$LOG" ] || printf 'ts,zai_session_pct,zai_token_pct,throttle,active_profiles,dispatch_rc\n' >> "$LOG"

# 0) post-sleep recovery: detect gap > 10 min (was 3 min — caused restart loop)
# Only clean stale workers. DO NOT restart the gateway — that creates a
# self-reinforcing loop (restart → gap → restart → gap → ...).
ts=$(date -u +%FT%TZ)
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 1 ]; then
  last_ts=$(tail -1 "$LOG" | cut -d, -f1)
  last_epoch=$(date -d "$last_ts" +%s 2>/dev/null || echo 0)
  now_epoch=$(date -u +%s)
  gap=$((now_epoch - last_epoch))
  if [ "$gap" -gt 600 ]; then
    echo "[$ts] POST-SLEEP RECOVERY: ${gap}s gap detected — cleaning stale workers (gateway NOT restarted)"
    for board_dir in "$HOME"/.hermes/kanban/boards/*/; do
      db="${board_dir}kanban.db"
      [ -f "$db" ] || continue
      $PY - "$db" <<'PY' 2>/dev/null
import sqlite3, os, signal, sys, time
db = sys.argv[1]
try:
    conn = sqlite3.connect(db)
    now = int(time.time())
    for task_id, pid, exp in conn.execute(
        "SELECT id, worker_pid, claim_expires FROM tasks WHERE status='running' AND worker_pid IS NOT NULL"
    ).fetchall():
        if exp and now > exp:
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"  killed stale pid={pid} (task={task_id})")
            except (ProcessLookupError, PermissionError):
                pass
    conn.close()
except Exception:
    pass
PY
    done
    ts=$(date -u +%FT%TZ)
    echo "[$ts] recovery complete — stale workers cleaned (gateway left untouched)"
  fi
fi

# 1) throttle gate
throttle=$($PY - <<'PY' 2>/dev/null
import json,os
try:
    s=json.load(open(os.path.expanduser("~/.hermes/bot/zai_state.json")))
    print("1" if s.get("throttle") else "0")
except Exception:
    print("0")
PY
)
# pull pcts if available
read spct tpct < <($PY - <<'PY' 2>/dev/null
import json,os
try:
    s=json.load(open(os.path.expanduser("~/.hermes/bot/zai_state.json")))
    print(s.get("session_pct",0), s.get("token_pct",0))
except Exception:
    print(0,0)
PY
)

# 2) count active profiles
active=$($PY - <<'PY' 2>/dev/null
import json,os
try:
    s=json.load(open(os.path.expanduser("~/.hermes/bot/profile_states.json")))
    print(sum(1 for v in s.values() if v.get("state")=="active"))
except Exception:
    print(0)
PY
)

ts=$(date -u +%FT%TZ)
# quota_pause = TOKENS_LIMIT >= 85% (D-062): pause z.ai, do NOT auto-failover to PPQ (ask-first)
qpause=$($PY - <<'PY' 2>/dev/null
import json,os
try:
    s=json.load(open(os.path.expanduser("~/.hermes/bot/zai_state.json")))
    print("1" if s.get("quota_pause") else "0")
except Exception:
    print("0")
PY
)
# friend's fallback key state (read BEFORE quota gate so we can fall through)
fpause=$($PY -c "import json,os; s=json.load(open(os.path.expanduser('~/.hermes/bot/zai_state.json'))); print('1' if s.get('friend_pause') else '0')" 2>/dev/null)
fpct=$($PY -c "import json,os; s=json.load(open(os.path.expanduser('~/.hermes/bot/zai_state.json'))); print(s.get('friend_token_pct',0))" 2>/dev/null)

# quota gate: pause only if BOTH keys are blocked (D-069 proxy rotation)
if { [ "$throttle" = "1" ] || [ "$qpause" = "1" ]; } && [ "$fpause" = "1" ]; then
  printf '%s,%s,%s,1,%s,-\n' "$ts" "$spct" "$tpct" "$active" >> "$LOG"
  echo "[$ts] ALL keys paused — ours ${tpct}%, friend's ${fpct}%. ($active active queued)"
  exit 0
elif [ "$throttle" = "1" ] || [ "$qpause" = "1" ]; then
  echo "[$ts] our key ${tpct}% — delegating to friend's key (${fpct}% available, D-069 proxy rotation)"
fi

# peak hours (config-driven via peak_hours.json, updated weekly by peak_hours_check.py)
ph_s=$($PY -c "import json;d=json.load(open('$HOME/.hermes/bot/peak_hours.json'));print(d.get('peak_start_utc',6))" 2>/dev/null||echo 6)
ph_e=$($PY -c "import json;d=json.load(open('$HOME/.hermes/bot/peak_hours.json'));print(d.get('peak_end_utc',10))" 2>/dev/null||echo 10)
hour=$(date -u +%H)
if [ "$hour" -ge "$ph_s" ] && [ "$hour" -lt "$ph_e" ]; then
  echo "[$ts] PEAK HOURS (${ph_s}:00-${ph_e}:00 UTC, 3× burn) — pausing dispatch"
  exit 0
fi

if [ "$active" -eq 0 ]; then
  printf '%s,%s,%s,0,0,-\n' "$ts" "$spct" "$tpct" >> "$LOG"
  echo "[$ts] no active profiles — nothing to dispatch (pending/ parked/ archived only)"
  exit 0
fi

# 3) resource gate — check load + RAM before dispatching.
# Feedback loop: dispatch AT MOST 1 worker per tick. Next tick dispatches
# another if resources are still available. The system self-limits: a heavy
# worker drops available RAM, next tick sees it and skips. Light workers
# barely register, so they keep accumulating until RAM or load is consumed.
GATE_SCRIPT="$HOME/.hermes/profiles/manager/scripts/dispatch_resource_gate.sh"
if [ -f "$GATE_SCRIPT" ]; then
  gate_msg=$(bash "$GATE_SCRIPT" 2>&1) || {
    printf '%s,%s,%s,2,%s,-\n' "$ts" "$spct" "$tpct" "$active" >> "$LOG"
    echo "[$ts] $gate_msg — dispatch paused"
    exit 0
  }
  gate_info="$gate_msg"
else
  gate_info="GATE MISSING (no resource check)"
fi

# 4) dispatch — at most 1 per tick (feedback-loop ramp-up)
export PATH="$HOME/.local/bin:$PATH"
hermes kanban dispatch --max 1 >/tmp/hermes-dispatch.out 2>&1
rc=$?
printf '%s,%s,%s,0,%s,%s\n' "$ts" "$spct" "$tpct" "$active" "$rc" >> "$LOG"
echo "[$ts] $gate_info — dispatch 1 of $active active (rc=$rc)"
[ -s /tmp/hermes-dispatch.out ] && tail -3 /tmp/hermes-dispatch.out
exit 0
