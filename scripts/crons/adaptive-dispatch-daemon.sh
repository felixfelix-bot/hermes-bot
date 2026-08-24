#!/usr/bin/env bash
# adaptive-dispatch-daemon.sh — Long-lived Kalman-adaptive dispatch loop
#
# Runs continuously with an adaptive sleep interval based on Kalman-smoothed CPU.
# Started by systemd (hermes-dispatch.service).
set -u
SCRIPT_DIR="$HOME/.hermes/profiles/manager/scripts"
PID_FILE="/tmp/adaptive-dispatch.pid"
STATE_FILE="/tmp/adaptive-dispatch.state"
KALMAN_STATE="$HOME/.hermes/state/dispatch_kalman.json"

# --- Hysteresis state tracking ---
HYSTERESIS_STATE_FILE="/tmp/adaptive-dispatch-hysteresis.state"
LAST_SCALING_TS_FILE="/tmp/adaptive-dispatch-last-scaling.ts"

# --- Source worker watchdog helper functions ---
WATCHDOG="$SCRIPT_DIR/worker-watchdog.sh"
if [ ! -f "$WATCHDOG" ]; then
    echo "FATAL: worker-watchdog.sh not found at $WATCHDOG"
    exit 1
fi

# Extract helper functions by sourcing a subset (load_per_core, mem_pct, etc.)
# We redefine them here to avoid sourcing the whole watchdog (which has side-effects)
load_per_core() {
    local load1 nproc
    load1=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null || echo 0)
    nproc=$(nproc 2>/dev/null || echo 1)
    awk "BEGIN {printf \"%.2f\", ${load1} / ${nproc}}"
}

mem_pct() {
    local total avail used
    total=$(awk '/MemTotal:/ {print $2}' /proc/meminfo)
    avail=$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)
    if [ -n "$total" ] && [ -n "$avail" ] && [ "$total" -gt 0 ]; then
        awk "BEGIN {printf \"%.0f\", (${total} - ${avail}) * 100 / ${total}}"
    else
        echo 0
    fi
}

swap_used_kb() {
    local total free used
    total=$(awk '/SwapTotal:/ {print $2}' /proc/meminfo)
    free=$(awk '/SwapFree:/ {print $2}' /proc/meminfo)
    if [ -n "$total" ] && [ -n "$free" ]; then
        echo $(( total - free ))
    else
        echo 0
    fi
}

# --- Proxy liveness check: returns 0 if proxy is alive and serving models ---
proxy_is_alive() {
    local model_count
    model_count=$(curl -sf --max-time 5 http://localhost:9099/v1/models 2>/dev/null | \
        python3 -c "import sys,json; print(len(json.load(sys.stdin).get('data',[])))" 2>/dev/null || echo 0)
    [ "$model_count" -gt 0 ]
}

# --- Legacy API quota gate (kept as fallback for kalman_dispatch_gate) ---
api_quota_ok_legacy() {
    local state_file="$HOME/.hermes/bot/zai_state.json"

    # PROXY-AWARE GATE: If the proxy is alive and serving models,
    # allow dispatch even when z.ai quota is exhausted.
    # Alternative providers (NeuralWatt, Ollama Cloud, OpenCode Go) can serve.
    # Block ONLY when proxy is unreachable AND z.ai quota is exhausted.
    if proxy_is_alive; then
        echo "YES:proxy_alive"
        return 0
    fi

    # Proxy is down — fall through to z.ai-only quota check
    if [ -f "$state_file" ]; then
        local result
        result=$(/usr/bin/python3 -c "
import json
d = json.load(open('$state_file'))
# quota_pause=True means OUR key is exhausted (token >= 85%).
# The proxy handles key rotation internally — it will route to the
# friend key when ours is exhausted. So only block dispatch when
# BOTH keys are exhausted.
# Also check model_tier_router for tier-aware gate.
if d.get('quota_pause', False):
    friend_pct = int(d.get('friend_token_pct', 100))
    if friend_pct < 80:
        print('YES:friend_key')  # Friend key has room, dispatch on friend key
    else:
        # Last resort: check model_tier_router for flash-only
        try:
            import subprocess, sys
            r = subprocess.run(['$HOME/.hermes/bot/model_tier_router.py'],
                capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                import json as j2
                data = j2.loads(r.stdout.strip())
                if data.get('quota_state') == 'CRITICAL':
                    print('NO:quota_pause_critical')
                else:
                    print('YES:quota_pause_but_noncritical')
            else:
                print('NO:quota_pause')
        except:
            print('NO:quota_pause')
elif d.get('ok', True) is False:
    print('NO:proxy_unhealthy')
elif 'quota_pause' not in d:
    # Fallback for older state files without quota_pause field
    our_ok = (not d.get('critical', False) and
              not d.get('throttle', False) and
              int(d.get('token_pct', 0)) < 80 and
              int(d.get('session_pct', 0)) < 85)
    friend_ok = int(d.get('friend_token_pct', 0)) < 80
    if our_ok:
        print('YES')
    elif friend_ok:
        print('YES:friend_key')
    else:
        print('NO:both_keys_exhausted')
else:
    print('YES')
" 2>/dev/null || echo "YES")
        case "$result" in
            YES*) return 0 ;;
            *)    return 1 ;;
        esac
    fi
    return 0  # assume ok if no state file
}

# --- Task type inference (keyword matching from task title) ---
infer_task_type() {
    local title="$1"
    local title_lower
    title_lower=$(echo "$title" | tr '[:upper:]' '[:lower:]')
    
    if echo "$title_lower" | grep -qE 'drc|gerber|pcb|hw.config|kicad.config|wire.*up|yaml'; then
        echo "mechanical"
    elif echo "$title_lower" | grep -qE 'review|cold.review|audit'; then
        echo "review"
    elif echo "$title_lower" | grep -qE 'doc|readme|handover|plan'; then
        echo "docs"
    elif echo "$title_lower" | grep -qE 'research|investigate|analyze|analysis'; then
        echo "research"
    else
        echo "coding"
    fi
}

# --- Hardware requirement inference (keyword matching from task title) ---
infer_hardware_req() {
    local title="$1"
    local title_lower
    title_lower=$(echo "$title" | tr '[:upper:]' '[:lower:]')
    
    # Two-board tasks
    if echo "$title_lower" | grep -qE 'handshake|two.node|2.node|phy.exchange|dual'; then
        echo "dual_board"
    # Single-board tasks
    elif echo "$title_lower" | grep -qE 'flash|pio.upload|serial|capture|throughput|f242d|bootsel'; then
        echo "board"
    # DQ05-dependent tasks
    elif echo "$title_lower" | grep -qE 'dq05|remote.build|ssh.*compile'; then
        echo "dq05"
    else
        echo "none"
    fi
}

# --- Kalman dispatch gate (calls /v1/dispatch_gate endpoint) ---
# Returns 0 (YES) if dispatch allowed, 1 (HOLD) if not.
# Fails open: if endpoint unreachable, falls back to api_quota_ok_legacy().
kalman_dispatch_gate() {
    local task_type="${1:-coding}"
    local estimated_tokens="${2:-200000}"
    local hardware_req="${3:-none}"
    local queue_depth="${4:-0}"
    
    local result
    result=$(curl -s --max-time 5 \
        "http://127.0.0.1:9099/v1/dispatch_gate?estimated_tokens=${estimated_tokens}&task_type=${task_type}&hardware_req=${hardware_req}&queue_depth=${queue_depth}" \
        2>/dev/null)
    
    if [ $? -ne 0 ] || [ -z "$result" ]; then
        # FALLBACK: use old binary gate if endpoint unreachable
        echo "FALLBACK:legacy"
        api_quota_ok_legacy
        return $?
    fi
    
    local can_dispatch
    can_dispatch=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print('1' if d.get('can_dispatch') else '0')" 2>/dev/null)
    
    # Fail-open: empty can_dispatch means JSON parse failed (502/HTML/etc).
    # Fall through to legacy gate instead of holding indefinitely.
    if [ -z "$can_dispatch" ]; then
        echo "FALLBACK:legacy"
        api_quota_ok_legacy
        return $?
    fi
    
    if [ "$can_dispatch" = "1" ]; then
        local model
        model=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('recommended_model','unknown'))" 2>/dev/null)
        # Q1: Provider depth check — WARNING only, never blocks (Felix: no caps).
        # If can_dispatch=true but only 1 viable provider in downgrade_chain,
        # dispatch is fragile. Log a warning but still allow dispatch.
        local viable_count
        viable_count=$(echo "$result" | python3 -c "
import sys, json
d = json.load(sys.stdin)
chain = d.get('downgrade_chain', [])
print(sum(1 for c in chain if isinstance(c, dict) and c.get('viable', False)))
" 2>/dev/null || echo 0)
        if [ "${viable_count:-0}" -lt 2 ]; then
            echo "WARNING: Only ${viable_count} viable provider(s), dispatching is fragile" >&2
        fi
        echo "YES:${model}"
        return 0
    else
        local reason
        reason=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('reason','unknown'))" 2>/dev/null)
        echo "HOLD:${reason}"
        return 1
    fi
}

# --- Hardware queue depth: count running + ready tasks with same hardware_req ---
# Reads task titles from all kanban boards, infers hardware_req, counts matches.
hardware_queue_depth() {
    local target_hw="${1:-none}"
    
    # Don't bother counting for software tasks
    if [ "$target_hw" = "none" ]; then
        echo 0
        return
    fi
    
    /usr/bin/python3 <<PYEOF 2>/dev/null || echo 0
import sqlite3, re
from pathlib import Path

target_hw = "${target_hw}"

def infer_hw(title):
    if not title:
        return "none"
    t = title.lower()
    if re.search(r'handshake|two.node|2.node|phy.exchange|dual', t):
        return "dual_board"
    elif re.search(r'flash|pio.upload|serial|capture|throughput|f242d|bootsel', t):
        return "board"
    elif re.search(r'dq05|remote.build|ssh.*compile', t):
        return "dq05"
    else:
        return "none"

boards_dir = Path.home() / ".hermes" / "kanban" / "boards"
total = 0
for d in sorted(boards_dir.iterdir()):
    if not d.is_dir():
        continue
    db = d / "kanban.db"
    if not db.exists():
        continue
    try:
        conn = sqlite3.connect(str(db))
        # Count running + ready tasks whose title infers to the same hardware_req
        rows = conn.execute(
            "SELECT title FROM tasks WHERE status IN ('running','ready')"
        ).fetchall()
        conn.close()
        for (title,) in rows:
            if infer_hw(title or "") == target_hw:
                total += 1
    except:
        pass
print(max(0, total - 1))  # subtract the task being gated (always in 'ready')
PYEOF
}

# --- Get the title of the next ready task on a given board ---
get_next_ready_task_title() {
    local board="$1"
    /usr/bin/python3 <<PYEOF 2>/dev/null || echo ""
import sqlite3
from pathlib import Path
db = Path.home() / ".hermes" / "kanban" / "boards" / "${board}" / "kanban.db"
if not db.exists():
    print("")
    exit()
try:
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT title FROM tasks WHERE status='ready' ORDER BY priority DESC, created_at ASC LIMIT 1"
    ).fetchone()
    conn.close()
    print(row[0] if row else "")
except:
    print("")
PYEOF
}

# --- Per-spawn dispatch gate: check kalman_dispatch_gate for a specific board ---
# Gets the next ready task title on the board, infers task_type + hardware_req,
# computes queue_depth, and calls the Kalman gate.
# Returns 0 if dispatch allowed, 1 if held.
# Fails open to api_quota_ok_legacy() if endpoint unreachable.
board_dispatch_gate() {
    local board="$1"
    local title task_type hw_req queue_depth
    
    title=$(get_next_ready_task_title "$board")
    
    if [ -z "$title" ]; then
        # No ready tasks on this board — nothing to gate, allow (dispatch will no-op)
        return 0
    fi
    
    task_type=$(infer_task_type "$title")
    hw_req=$(infer_hardware_req "$title")
    queue_depth=$(hardware_queue_depth "$hw_req")
    
    local gate_msg
    gate_msg=$(kalman_dispatch_gate "$task_type" 200000 "$hw_req" "$queue_depth")
    local gate_rc=$?
    
    if [ $gate_rc -eq 0 ]; then
        echo "GATE OK: $board task_type=$task_type hw=$hw_req qd=$queue_depth — $gate_msg"
        return 0
    else
        echo "GATE HOLD: $board task_type=$task_type hw=$hw_req qd=$queue_depth — $gate_msg"
        return 1
    fi
}

compute_max_concurrent() {
    timeout 5 /usr/bin/python3 "$SCRIPT_DIR/compute_max_workers.py" 2>/dev/null || echo 2
}

# --- Memory-hog kill helper: kill non-worker processes eating swap ---
# Called by the resource-check section inside the dispatch loop.
kill_memory_hogs() {
    local threshold_kb="${1:-500000}"  # default: kill anything above 500MB swap
    local count=0
    # Kill LSP servers (largest first)
    for proc in "pyright" "typescript-language-server" "rust-analyzer" "gopls" "lua-language-server"; do
        local pids
        pids=$(pgrep -f "$proc" 2>/dev/null | head -5)
        for pid in $pids; do
            local swap_kb
            swap_kb=$(grep -i "VmSwap" /proc/"$pid"/status 2>/dev/null | awk '{print $2}' | grep -oE '^[0-9]+' || echo 0)
            if [ "${swap_kb:-0}" -gt "$threshold_kb" ]; then
                kill "$pid" 2>/dev/null && count=$((count + 1)) && echo "  killed $proc (PID ${pid}, swap=${swap_kb}KB)"
            fi
        done
    done
    # Kill headless browsers
    for proc in "chromium" "chrome" "firefox" "playwright"; do
        local pids
        pids=$(pgrep -f "$proc" 2>/dev/null | head -3)
        for pid in $pids; do
            local swap_kb
            swap_kb=$(grep -i "VmSwap" /proc/"$pid"/status 2>/dev/null | awk '{print $2}' | grep -oE '^[0-9]+' || echo 0)
            if [ "${swap_kb:-0}" -gt "$threshold_kb" ]; then
                kill "$pid" 2>/dev/null && count=$((count + 1)) && echo "  killed $proc (PID ${pid}, swap=${swap_kb}KB)"
            fi
        done
    done
    # Output count to stdout for command substitution capture
    echo "$count"
}

# --- Hysteresis helper functions ---
# Check if enough time has passed since last scaling decision (30-second cooldown)
can_scale() {
    local last_scaling_ts now cooldown_period
    cooldown_period=30  # 30 seconds minimum between scaling decisions
    
    if [ -f "$LAST_SCALING_TS_FILE" ]; then
        last_scaling_ts=$(cat "$LAST_SCALING_TS_FILE" 2>/dev/null || echo 0)
        now=$(date +%s)
        if [ $((now - last_scaling_ts)) -lt $cooldown_period ]; then
            return 1  # Cannot scale yet
        fi
    fi
    return 0  # Can scale
}

# Record scaling decision timestamp
record_scaling_decision() {
    date +%s > "$LAST_SCALING_TS_FILE"
}

# Get current hysteresis state (emergency_mode, last_worker_count)
get_hysteresis_state() {
    if [ -f "$HYSTERESIS_STATE_FILE" ]; then
        cat "$HYSTERESIS_STATE_FILE" 2>/dev/null || echo "normal:0"
    else
        echo "normal:0"
    fi
}

# Set hysteresis state
set_hysteresis_state() {
    local state worker_count
    state="$1"
    worker_count="$2"
    echo "${state}:${worker_count}" > "$HYSTERESIS_STATE_FILE"
}

# --- Peak hours for failure-limit tuning (peak_hours.json, updated weekly) ---
PEAK_START=$(/usr/bin/python3 -c "import json; d=json.load(open('$HOME/.hermes/bot/peak_hours.json')); print(d.get('peak_start_utc',6))" 2>/dev/null || echo 6)
PEAK_END=$(/usr/bin/python3 -c "import json; d=json.load(open('$HOME/.hermes/bot/peak_hours.json')); print(d.get('peak_end_utc',10))" 2>/dev/null || echo 10)

# --- Cleanup on exit ---
cleanup() {
    echo "Daemon shutting down" >&2
    rm -f "$PID_FILE"
    exit 0
}
trap cleanup SIGTERM SIGINT

# --- Write PID ---
echo $$ > "$PID_FILE"
chmod 644 "$PID_FILE"

ITERATION=0
LAST_TS=$(date +%s)
SINCE_LAST=30

# --- Main loop ---
while true; do
    ITERATION=$((ITERATION + 1))
    NOW=$(date +%s)
    SINCE_LAST=$((NOW - LAST_TS))
    if [ "$SINCE_LAST" -lt 1 ]; then SINCE_LAST=1; fi
    LAST_TS=$NOW

    # 1. Read system signals
    CPU_PCT=$(load_per_core | awk '{printf "%.0f", $1 * 100}')
    MEM=$(mem_pct)
    RAW_SWAP=$(swap_used_kb)
    RAW_MAX_CONCURRENT=$(compute_max_concurrent)

    # 1b. Kalman smooth swap for predictive throttling
    SWAP_KALMAN_OUT=$(/usr/bin/python3 "$SCRIPT_DIR/swap_kalman.py" --measure "$RAW_SWAP" --interval "$SINCE_LAST" 2>/dev/null)
    SMOOTHED_SWAP=$(echo "$SWAP_KALMAN_OUT" | grep "^SMOOTHED=" | cut -d= -f2)
    SWAP_VELOCITY=$(echo "$SWAP_KALMAN_OUT" | grep "^VELOCITY=" | cut -d= -f2)
    PREDICTED_SWAP_3=$(echo "$SWAP_KALMAN_OUT" | grep "^PREDICTED_3=" | cut -d= -f2)
    SMOOTHED_SWAP=${SMOOTHED_SWAP:-$RAW_SWAP}
    SWAP_VELOCITY=${SWAP_VELOCITY:-0}
    PREDICTED_SWAP_3=${PREDICTED_SWAP_3:-$RAW_SWAP}
    # Use smoothed swap as authoritative (less noisy than RAW_SWAP)
    SWAP=$SMOOTHED_SWAP

    # 1a. Kalman smooth the pool size (follows same pattern as CPU Kalman)
    POOL_KALMAN_OUT=$(/usr/bin/python3 "$SCRIPT_DIR/pool_kalman.py" --measure "$RAW_MAX_CONCURRENT" --interval "$SINCE_LAST" 2>/dev/null)
    SMOOTHED_POOL=$(echo "$POOL_KALMAN_OUT" | grep "^SMOOTHED=" | cut -d= -f2)
    POOL_VELOCITY=$(echo "$POOL_KALMAN_OUT" | grep "^VELOCITY=" | cut -d= -f2)
    SMOOTHED_POOL=${SMOOTHED_POOL:-$RAW_MAX_CONCURRENT}
    POOL_VELOCITY=${POOL_VELOCITY:-0}

    # Round to integer and clamp: at least 1 worker, at most the raw cap
    SMOOTHED_POOL_INT=$(printf "%.0f" "$SMOOTHED_POOL" 2>/dev/null || echo "$RAW_MAX_CONCURRENT")
    [ "$SMOOTHED_POOL_INT" -lt 1 ] && SMOOTHED_POOL_INT=1
    [ "$SMOOTHED_POOL_INT" -gt "$RAW_MAX_CONCURRENT" ] && SMOOTHED_POOL_INT="$RAW_MAX_CONCURRENT"
    MAX_CONCURRENT=$SMOOTHED_POOL_INT

    # DYNAMIC board discovery — auto-discovers all active boards from filesystem
    # Round-robin: rotate starting board each tick so `admin` doesn't starve others
    ACTIVE_BOARDS=$(/usr/bin/python3 <<'PYEOF' 2>/dev/null || echo "admin")
import sqlite3
from pathlib import Path
boards_dir = Path.home() / ".hermes" / "kanban" / "boards"
skip = {"default", "archive", "archived"}
for d in sorted(boards_dir.iterdir()):
    if d.name in skip or not d.is_dir(): continue
    db = d / "kanban.db"
    if not db.exists(): continue
    try:
        conn = sqlite3.connect(str(db))
        count = conn.execute("SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done','archived')").fetchone()[0]
        conn.close()
        if count > 0: print(d.name)
    except: pass
PYEOF
    # Rotate board list so each tick starts at a different board (round-robin fairness)
    BOARD_COUNT=$(echo "$ACTIVE_BOARDS" | wc -l)
    if [ "$BOARD_COUNT" -gt 1 ]; then
        OFFSET=$((ITERATION % BOARD_COUNT))
        if [ "$OFFSET" -gt 0 ]; then
            ACTIVE_BOARDS=$(echo "$ACTIVE_BOARDS" | tail -n +$((OFFSET + 1)) && echo "$ACTIVE_BOARDS" | head -n $OFFSET)
        fi
    fi
    # Count running tasks via SQLite (accurate, not global CLI view)
    RUNNING=$(/usr/bin/python3 <<'PYEOF' 2>/dev/null || echo 0)
import sqlite3
from pathlib import Path
boards_dir = Path.home() / ".hermes" / "kanban" / "boards"
total = 0
for d in sorted(boards_dir.iterdir()):
    if not d.is_dir(): continue
    db = d / "kanban.db"
    if not db.exists(): continue
    try:
        conn = sqlite3.connect(str(db))
        total += conn.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0]
        conn.close()
    except: pass
print(total)
PYEOF
    PENDING=$(/usr/bin/python3 <<'PYEOF' 2>/dev/null || echo 0)
import sqlite3
from pathlib import Path
boards_dir = Path.home() / ".hermes" / "kanban" / "boards"
total = 0
for d in sorted(boards_dir.iterdir()):
    if not d.is_dir(): continue
    db = d / "kanban.db"
    if not db.exists(): continue
    try:
        conn = sqlite3.connect(str(db))
        total += conn.execute("SELECT COUNT(*) FROM tasks WHERE status='ready'").fetchone()[0]
        conn.close()
    except: pass
print(total)
PYEOF

    # 2. Kalman smooth CPU
    KALMAN_OUT=$(/usr/bin/python3 "$SCRIPT_DIR/adaptive_dispatch_kalman.py" --measure "$CPU_PCT" --interval "$SINCE_LAST" 2>/dev/null)
    SMOOTHED_CPU=$(echo "$KALMAN_OUT" | grep "^SMOOTHED=" | cut -d= -f2)
    VELOCITY=$(echo "$KALMAN_OUT" | grep "^VELOCITY=" | cut -d= -f2)
    SMOOTHED_CPU=${SMOOTHED_CPU:-$CPU_PCT}
    VELOCITY=${VELOCITY:-0}

    # 3. Compute adaptive interval
    SMOOTHED_INT=$(printf "%.0f" "$SMOOTHED_CPU" 2>/dev/null || echo 50)
    if [ "$SMOOTHED_INT" -lt 25 ]; then
        BASE=15
    elif [ "$SMOOTHED_INT" -lt 50 ]; then
        BASE=30
    elif [ "$SMOOTHED_INT" -lt 75 ]; then
        BASE=60
    else
        BASE=120
    fi

    # Trend bonus: CPU dropping fast → halve interval (worker just finished)
    VEL_INT=$(printf "%.0f" "$VELOCITY" 2>/dev/null || echo 0)
    if [ "$VEL_INT" -lt -5 ]; then
        BASE=$((BASE / 2))
        [ "$BASE" -lt 10 ] && BASE=10
    fi

    # Idle bonus: no room for more workers or no pending tasks
    if [ "$RUNNING" -ge "$MAX_CONCURRENT" ] || [ "$PENDING" -eq 0 ]; then
        MIN_INTERVAL=30
    else
        MIN_INTERVAL=10
    fi

    [ "$BASE" -lt "$MIN_INTERVAL" ] && BASE=$MIN_INTERVAL
    [ "$BASE" -gt 300 ] && BASE=300
    INTERVAL=$BASE

    # 4. Write state file
    {
        echo "INTERVAL=$INTERVAL"
        echo "SMOOTHED_CPU=$SMOOTHED_CPU"
        echo "VELOCITY=$VELOCITY"
        echo "SMOOTHED_POOL=$SMOOTHED_POOL"
        echo "POOL_VELOCITY=$POOL_VELOCITY"
        echo "POOL_RAW=$RAW_MAX_CONCURRENT"
        echo "MAX_CONCURRENT=$MAX_CONCURRENT"
        echo "SWAP_SMOOTHED=$SMOOTHED_SWAP"
        echo "SWAP_VELOCITY=$SWAP_VELOCITY"
        echo "SWAP_PREDICTED_3=$PREDICTED_SWAP_3"
        echo "SWAP_RAW=$RAW_SWAP"
        echo "TS=$NOW"
    } > "$STATE_FILE"

    # 5. Resource check + act (with hysteresis)
    LAST_ACTION="hold"
    
    # Get current hysteresis state
    hysteresis_state=$(get_hysteresis_state)
    current_mode=$(echo "$hysteresis_state" | cut -d: -f1)
    last_worker_count=$(echo "$hysteresis_state" | cut -d: -f2)
    
    # EMERGENCY (80% threshold): RAM > 80% OR raw swap > 14GB
    # Lowered from 90% to 80% with hysteresis recovery at 65%
    if [ "$MEM" -gt 80 ] || [ "${RAW_SWAP:-0}" -gt 14000000 ]; then
        if [ "$current_mode" != "emergency" ]; then
            echo "EMERGENCY TRIGGERED: mem=${MEM}% swap=$((SWAP/1024))MB raw=$((RAW_SWAP/1024))MB"
            # Entering emergency mode - implement gradual scaling
            if can_scale; then
                # Calculate gradual reduction: reduce by 1 worker, but not below 1
                target_workers=$((MAX_CONCURRENT - 1))
                if [ "$target_workers" -lt 1 ]; then
                    target_workers=1
                fi
                
                # Only reduce if we're not already at minimum
                if [ "$RUNNING" -gt "$target_workers" ]; then
                    echo "  gradual scaling: $RUNNING → $target_workers workers (-1)"
                    # Find and reclaim youngest running task (gradual)
                    youngest_id=""
                    youngest_age=999999
                    for board in $ACTIVE_BOARDS; do
                        while IFS='|' read -r tid status rest; do
                            [ "$status" != "running" ] && continue
                            pid=$(pgrep -f "kanban task ${tid}" 2>/dev/null | head -1)
                            if [ -n "$pid" ]; then
                                age=$(ps -o etimes= -p "$pid" 2>/dev/null || echo 999999)
                                if [ "$age" -lt "$youngest_age" ]; then
                                    youngest_age=$age
                                    youngest_id="$board:$tid"
                                fi
                            fi
                        done < <(hermes kanban --board "$board" ls 2>/dev/null | grep "running")
                    done
                    
                    if [ -n "$youngest_id" ]; then
                        b=$(echo "$youngest_id" | cut -d: -f1)
                        tid=$(echo "$youngest_id" | cut -d: -f2)
                        echo "  reclaiming youngest task $tid on $b (age=${youngest_age}s)"
                        timeout 10 hermes kanban --board "$b" reclaim "$tid" 2>/dev/null
                        record_scaling_decision
                    fi
                fi
                set_hysteresis_state "emergency" "$RUNNING"
            else
                echo "  scaling cooldown active - skipping"
            fi
        else
            echo "EMERGENCY PERSISTENT: mem=${MEM}% swap=$((SWAP/1024))MB (already in emergency mode)"
        fi
        
        # First: kill non-worker memory hogs (>300MB swap each)
        hog_count=$(kill_memory_hogs 300000)
        echo "  killed $hog_count memory hogs"
        
        # Clean kanban-sandbox dirs older than 1 hour
        find /tmp -maxdepth 1 -name "kanban-sandbox-*" -type d -mmin +60 -exec rm -rf {} + 2>/dev/null
        
        LAST_ACTION="emergency"

    # RECOVERY: RAM < 65% AND we were in emergency mode
    elif [ "$MEM" -lt 65 ] && [ "$current_mode" = "emergency" ]; then
        echo "EMERGENCY RECOVERY: mem=${MEM}% - resuming normal scaling"
        if can_scale; then
            # Implement gradual scaling back to normal: increase by 1 worker
            target_workers=$((last_worker_count + 1))
            if [ "$target_workers" -gt "$RAW_MAX_CONCURRENT" ]; then
                target_workers="$RAW_MAX_CONCURRENT"
            fi
            
            echo "  gradual recovery: scaling up to $target_workers workers (+1)"
            set_hysteresis_state "normal" "$target_workers"
            record_scaling_decision
        else
            echo "  scaling cooldown active - staying in emergency mode"
        fi
        LAST_ACTION="recovery"

    # TRIM: raw swap > 10GB AND RAM > 75% (middle hysteresis band)
    # This is the middle band between emergency (80%) and recovery (65%)
    elif [ "${RAW_SWAP:-0}" -gt 10000000 ] && [ "$MEM" -gt 75 ]; then
        echo "TRIM: mem=${MEM}% swap=$((SWAP/1024))MB - middle hysteresis band"
        # First: kill non-worker memory hogs (>500MB swap each) before reclaiming workers
        hog_count=$(kill_memory_hogs 500000)
        [ "$hog_count" -gt 0 ] && echo "  killed $hog_count memory hogs before trim"
        # Then find and reclaim youngest running task (gradual - only 1)
        if can_scale; then
            youngest_id=""
            youngest_age=999999
            for board in $ACTIVE_BOARDS; do
                while IFS='|' read -r tid status rest; do
                    [ "$status" != "running" ] && continue
                    pid=$(pgrep -f "kanban task ${tid}" 2>/dev/null | head -1)
                    if [ -n "$pid" ]; then
                        age=$(ps -o etimes= -p "$pid" 2>/dev/null || echo 999999)
                        if [ "$age" -lt "$youngest_age" ]; then
                            youngest_age=$age
                            youngest_id="$board:$tid"
                        fi
                    fi
                done < <(hermes kanban --board "$board" ls 2>/dev/null | grep "running")
            done
            if [ -n "$youngest_id" ]; then
                b=$(echo "$youngest_id" | cut -d: -f1)
                tid=$(echo "$youngest_id" | cut -d: -f2)
                echo "  reclaiming youngest task $tid on $b (age=${youngest_age}s) - gradual trim"
                timeout 10 hermes kanban --board "$b" reclaim "$tid" 2>/dev/null
                record_scaling_decision
                LAST_ACTION="trim"
            else
                echo "  no workers to reclaim"
                LAST_ACTION="warn"
            fi
        else
            echo "  scaling cooldown active - skipping trim"
            LAST_ACTION="warn"
        fi

    # WARN: raw swap > 7GB AND RAM > 70% (lower hysteresis band)
    # Stop dispatch but DON'T kill anything - just pause
    elif [ "${RAW_SWAP:-0}" -gt 7000000 ] && [ "$MEM" -gt 70 ]; then
        echo "WARN: mem=${MEM}% swap=$((SWAP/1024))MB - lower hysteresis band — pausing dispatch, no killing"
        LAST_ACTION="warn"

    # QUOTA RECOVERY: if previous tick was paused on quota and now quota is OK,
    # dispatch ALL ready tasks immediately regardless of running count.
    # This fires when friend key resets (e.g. 19:41 IST daily).
    elif [ "$LAST_ACTION" = "api_pause" ]; then
        if kalman_dispatch_gate coding 200000 none 0 >/dev/null 2>&1; then
            echo "QUOTA RECOVERED: dispatching all boards (Kalman gate)"
            for board in $ACTIVE_BOARDS; do
                # Per-spawn gate: check each board's next task before dispatching
                gate_msg=$(board_dispatch_gate "$board")
                if [ $? -eq 0 ]; then
                    dispatched=$(timeout 15 hermes kanban --board "$board" dispatch --failure-limit 3 2>/dev/null)
                    if echo "$dispatched" | grep -qiE "spawned|reclaimed|started"; then
                        echo "  $board — $dispatched"
                    fi
                else
                    echo "  $board — HELD by gate: $gate_msg"
                fi
            done
            LAST_ACTION="dispatch"
        else
            echo "HOLD: API quota still exhausted (Kalman gate)"
            LAST_ACTION="api_pause"
        fi

    # API QUOTA STANDALONE CHECK: if quota is exhausted, set state regardless of running count.
    # This ensures LAST_ACTION=api_pause even when RUNNING >= MAX_CONCURRENT.
    elif ! kalman_dispatch_gate coding 200000 none 0 >/dev/null 2>&1; then
        echo "HOLD: API quota exhausted (Kalman gate, standalone)"
        LAST_ACTION="api_pause"

    # DISPATCH: under limit and resources OK
    elif [ "$RUNNING" -lt "$MAX_CONCURRENT" ]; then
        # Rate-limit gate (Kalman-backed) — check BEFORE API quota.
        # Reads ~/.hermes/state/rate_limit_gate.json (kept fresh by 5-min cron).
        # Fail-open: broken gate → CLEAR, dispatch proceeds.
        GATE_MSG=$(bash "$SCRIPT_DIR/rate_limit_gate_check.sh" 2>/dev/null)
        GATE_RC=$?
        if [ "$GATE_RC" -ne 0 ]; then
            echo "GATE PAUSE: $GATE_MSG"
            LAST_ACTION="rate_gate_pause"
            # Re-check sooner: cap sleep at 60s while paused so we resume fast
            [ "$INTERVAL" -gt 60 ] && INTERVAL=60
        else
            # Pre-flight: validate all worker profiles have supported models.
            # Catches kimi-k2.7-code style misconfig before spawning workers that
            # will immediately 401 and crash.  Fail-open: if validator missing, proceed.
            if /usr/bin/python3 "$SCRIPT_DIR/validate-profile-models.py" 2>/dev/null; then
                :  # all profiles OK
            else
                echo "MODEL VALIDATION FAILED — some worker profiles have unsupported models"
                /usr/bin/python3 "$SCRIPT_DIR/validate-profile-models.py" 2>&1 || true
                LAST_ACTION="model_validation_fail"
                [ "$INTERVAL" -gt 60 ] && INTERVAL=60
            fi
            # Peak/off-peak failure limit (peak_hours.json)
            # Peak:     --failure-limit 3  (stricter, 3× burn rate)
            # Off-peak: --failure-limit 3  (was 10, reduced to prevent waste)
            HOUR=$(date -u +%H)
            if [ "$HOUR" -ge "$PEAK_START" ] && [ "$HOUR" -lt "$PEAK_END" ]; then
                FAIL_LIMIT=3
            else
                FAIL_LIMIT=3
            fi
            for board in $ACTIVE_BOARDS; do
                # Per-spawn Kalman gate: check each board's next task type + hardware before dispatching
                gate_msg=$(board_dispatch_gate "$board")
                if [ $? -eq 0 ]; then
                    dispatched=$(timeout 12 hermes kanban --board "$board" dispatch --failure-limit "$FAIL_LIMIT" 2>/dev/null)
                    if echo "$dispatched" | grep -qiE "spawned: *[1-9]|reclaimed: *[1-9]|started: *[1-9]"; then
                        echo "DISPATCH: $board — $dispatched (fail_limit=$FAIL_LIMIT) [$gate_msg]"
                        LAST_ACTION="dispatch"
                        # Continue checking other boards — each may have independent tasks
                    fi
                else
                    echo "GATE HOLD: $board — $gate_msg"
                    LAST_ACTION="gate_hold"
                    # Backoff: cap sleep at 60s to avoid busy-looping the gate
                    [ "$INTERVAL" -gt 60 ] && INTERVAL=60
                fi
            done
        fi
    else
        echo "HOLD: running=$RUNNING max=$MAX_CONCURRENT mem=${MEM}%%"
    fi

    # Update state with LAST_ACTION
    echo "LAST_ACTION=$LAST_ACTION" >> "$STATE_FILE"

    # --- DB logging + anomaly detection ---
    API_QUOTA_PCT=$(/usr/bin/python3 -c "
import json
try:
    d = json.load(open('$HOME/.hermes/bot/zai_state.json'))
    print(d.get('session_pct', 0))
except: print(0)
" 2>/dev/null || echo 0)

    /usr/bin/python3 "$SCRIPT_DIR/daemon_metrics.py" insert \
        "$CPU_PCT" "$MEM" "$SWAP" "$API_QUOTA_PCT" \
        "$SMOOTHED_CPU" "$VELOCITY" "$RUNNING" "$PENDING" \
        "$MAX_CONCURRENT" "$SMOOTHED_POOL" "$POOL_VELOCITY" \
        "$INTERVAL" "$LAST_ACTION" \
        2>/dev/null || true

    /usr/bin/python3 "$SCRIPT_DIR/daemon_metrics.py" anomalies \
        "$SMOOTHED_CPU" "$VELOCITY" "$MEM" "$RUNNING" "$PENDING" \
        "$MAX_CONCURRENT" "$SMOOTHED_POOL" "$POOL_VELOCITY" \
        2>/dev/null || true

    # Periodic self-exec to prevent bash memory leaks
    # Self-exec frequently to pick up script changes (round-robin rotation, etc.)
    if [ "$ITERATION" -ge 50 ]; then
        echo "Self-exec after $ITERATION iterations"
        exec "$0"
    fi

    sleep "$INTERVAL"
done
