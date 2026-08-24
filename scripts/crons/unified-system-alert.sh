#!/usr/bin/env bash
# ============================================================================
# unified-system-alert.sh — Comprehensive system + hardware anomaly monitor
#
# Checks ALL system and hardware health indicators in one pass.
#   empty stdout = silent (everything healthy), non-empty = alert text
#
# SYSTEM CHECKS:
#   1. System memory pressure (RAM % > 70% OR swap > 5GB)
#   2. Swap thrashing (vmstat si/so > 1000 sustained)
#   3. Disk space (any mount > 90% used OR < 15GB free)
#   4. CPU load (load_per_core > 2.0)
#   5. API quota (z.ai weekly >= 85%)
#   6. Dispatch daemon (paused or not running)
#   7. Proxy health (localhost:9099/health != "ok")
#   8. Worker crashes (from crash-monitor state if available)
#   9. VPS health (from recent vps-health-check output if available)
#
# HARDWARE CHECKS (always run, no API gate):
#  10. USB peripheral changes (boards connecting/disconnecting)
#  11. Serial port availability (RP2040, ESP32, STM32)
#  12. Board access violations (port conflicts)
#  13. FIPS daemon health
#  14. Balloon board status
#  15. microFIPS infrastructure
#
# DEDUP: Once-per-day max for same condition+severity.
#   Severity change (improve/worsen) alerts immediately.
#   Condition resolution alerts once "✅ RESOLVED" then clears state.
#
# Schedule: every 30 min, deliver=origin, no_agent=true
# ============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="/tmp/unified-system-alert-state.json"
PERIPH_STATE="/tmp/periph_status_prev.json"
SERIAL_STATE="/tmp/serial_ports_prev.txt"
ALERTS=()

# Severity ordering for comparison (higher = worse)
SEVERITY_ORDER="low medium high critical"

# 24 hours in seconds
DAY_SECONDS=86400

# ----------------------------------------------------------------------------
# Helper: add alert with severity
# ----------------------------------------------------------------------------
add_alert() {
  local category="$1"
  local description="$2"
  local severity="${3:-medium}"
  ALERTS+=("${category}|${severity}|${description}")
}

# ----------------------------------------------------------------------------
# SYSTEM CHECKS (1-9)
# ----------------------------------------------------------------------------

# 1. System memory pressure (RAM + swap)
check_memory() {
  local mem_info ram_pct swap_used swap_total
  mem_info=$(free -m 2>/dev/null)
  if [ -z "$mem_info" ]; then
    return
  fi

  ram_pct=$(echo "$mem_info" | awk '/^Mem:/ {printf "%.0f", $3*100/$2}')
  swap_used=$(echo "$mem_info" | awk '/^Swap:/ {print $3}')
  swap_total=$(echo "$mem_info" | awk '/^Swap:/ {print $2}')

  ram_pct=${ram_pct:-0}
  swap_used=${swap_used:-0}

  if [ "$ram_pct" -gt 90 ] 2>/dev/null; then
    add_alert "RAM" "${ram_pct}% used — critical, system may OOM" "critical"
  elif [ "$ram_pct" -gt 70 ] 2>/dev/null; then
    add_alert "RAM" "${ram_pct}% used — high (consider: pkill -f opencode to free sessions)" "high"
  fi

  if [ "$swap_used" -gt 5000 ] 2>/dev/null; then
    local swap_gb
    swap_gb=$(echo "scale=1; $swap_used / 1024" | bc 2>/dev/null || echo "$((swap_used / 1024))")
    if [ "$swap_used" -gt 10000 ] 2>/dev/null; then
      add_alert "SWAP" "${swap_gb}GB in use — critical thrashing risk" "critical"
    else
      add_alert "SWAP" "${swap_gb}GB in use — high, thrashing risk" "high"
    fi
  fi
}

# 2. Swap thrashing (vmstat si/so > 1000 sustained)
check_swap_thrashing() {
  local vmstat_line si so
  vmstat_line=$(vmstat 1 2 2>/dev/null | tail -1)
  if [ -z "$vmstat_line" ]; then
    return
  fi

  si=$(echo "$vmstat_line" | awk '{print $7}')
  so=$(echo "$vmstat_line" | awk '{print $8}')
  si=${si:-0}
  so=${so:-0}

  if [ "$si" -gt 1000 ] 2>/dev/null || [ "$so" -gt 1000 ] 2>/dev/null; then
    add_alert "SWAP-THRASH" "vmstat si=${si} so=${so} — active thrashing detected" "critical"
  fi
}

# 3. Disk space (any mount > 90% used OR < 15GB free)
check_disk() {
  local df_output
  df_output=$(df -B1 --output=target,pcent,avail 2>/dev/null | grep -v '^Mounted' || df -h 2>/dev/null | grep -v '^Filesystem')

  if [ -z "$df_output" ]; then
    return
  fi

  while IFS= read -r line; do
    local mount pct avail_bytes avail_gb
    mount=$(echo "$line" | awk '{print $1}')
    pct=$(echo "$line" | awk '{print $2}' | tr -d ' %')
    avail_bytes=$(echo "$line" | awk '{print $3}')

    [ -z "$pct" ] && continue
    [[ ! "$pct" =~ ^[0-9]+$ ]] && continue

    avail_gb=0
    if [ -n "$avail_bytes" ] && [[ "$avail_bytes" =~ ^[0-9]+$ ]]; then
      avail_gb=$((avail_bytes / 1073741824))
    fi

    case "$mount" in
      /dev/*|/proc/*|/sys/*|/run|/run/*|tmpfs|devtmpfs|none|/dev/shm|/boot/efi|/tmp|/var/tmp) continue ;;
    esac

    local mount_size_gb
    mount_size_gb=$(df -B1 --output=size "$mount" 2>/dev/null | tail -1 | awk '{print int($1/1073741824)}')
    [ -z "$mount_size_gb" ] && mount_size_gb=0
    [ "$mount_size_gb" -lt 20 ] 2>/dev/null && continue

    if [ "$pct" -ge 90 ] 2>/dev/null; then
      add_alert "DISK" "${mount} ${pct}% used, ${avail_gb}GB free — critical" "critical"
    elif [ "$avail_gb" -lt 15 ] 2>/dev/null && [ "$avail_gb" -ge 0 ] 2>/dev/null; then
      add_alert "DISK" "${mount} only ${avail_gb}GB free (${pct}% used) — low space" "high"
    fi
  done <<< "$df_output"
}

# 4. CPU load (load_per_core > 2.0)
check_cpu_load() {
  local load_1m cpu_cores load_per_core
  load_1m=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null)
  cpu_cores=$(nproc 2>/dev/null)
  if [ -z "$load_1m" ] || [ -z "$cpu_cores" ] || [ "$cpu_cores" -eq 0 ]; then
    return
  fi

  load_per_core=$(echo "$load_1m $cpu_cores" | awk '{printf "%.1f", $1/$2}')

  local over_threshold
  over_threshold=$(echo "$load_per_core 2.0" | awk '{if ($1 > $2) print 1; else print 0}')
  if [ "$over_threshold" -eq 1 ]; then
    if [ "$(echo "$load_per_core 4.0" | awk '{if ($1 > $2) print 1; else print 0}')" -eq 1 ]; then
      add_alert "CPU" "load ${load_per_core}/core (1m: ${load_1m}, ${cpu_cores} cores) — critical overload" "critical"
    else
      add_alert "CPU" "load ${load_per_core}/core (1m: ${load_1m}, ${cpu_cores} cores) — high" "high"
    fi
  fi
}

# 5. API quota (z.ai weekly >= 85%)
check_api_quota() {
  local quota_json weekly_pct
  quota_json=$(curl -sf --connect-timeout 5 http://localhost:9099/quota 2>/dev/null)
  if [ -z "$quota_json" ]; then
    return
  fi

  weekly_pct=$(echo "$quota_json" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    ours = d.get('ours', {})
    max_pct = ours.get('max_pct', 0)
    for w in ours.get('windows', []):
        if w.get('name') == 'weekly':
            max_pct = max(max_pct, w.get('used_pct', 0))
    print(max_pct)
except:
    print(0)
" 2>/dev/null)
  weekly_pct=${weekly_pct:-0}

  if [ "$weekly_pct" -ge 95 ] 2>/dev/null; then
    add_alert "QUOTA" "z.ai weekly quota at ${weekly_pct}% — critical, workers blocked" "critical"
  elif [ "$weekly_pct" -ge 85 ] 2>/dev/null; then
    add_alert "QUOTA" "z.ai weekly quota at ${weekly_pct}% — high, workers may need alternative providers" "high"
  fi
}

# 6. Dispatch daemon (paused or not running)
check_dispatch() {
  local daemon_active pause_log
  daemon_active=$(systemctl --user is-active hermes-dispatch 2>/dev/null)

  if [ "$daemon_active" != "active" ]; then
    add_alert "DISPATCH" "daemon not active (status: ${daemon_active:-unknown}) — tasks not dispatching" "critical"
    return
  fi

  pause_log=$(journalctl --user -u hermes-dispatch --since "5 min ago" --no-pager 2>/dev/null | grep -iE "GATE PAUSE|quota.*pause|paused" | tail -1)
  if [ -n "$pause_log" ]; then
    local pause_msg
    pause_msg=$(echo "$pause_log" | sed 's/.*\(GATE PAUSE[^|]*\|paused[^|]*\).*/\1/' | head -c 100)
    add_alert "DISPATCH" "daemon PAUSED — ${pause_msg}" "high"
  fi
}

# 7. Proxy health (localhost:9099/health != "ok")
check_proxy_health() {
  local health_response
  health_response=$(curl -sf --connect-timeout 5 http://localhost:9099/health 2>/dev/null)

  if [ -z "$health_response" ]; then
    add_alert "PROXY" "localhost:9099 unreachable — API proxy down" "critical"
  elif [ "$health_response" != "ok" ]; then
    local trimmed
    trimmed=$(echo "$health_response" | tr -d ' \n\r')
    if [ "$trimmed" != "ok" ]; then
      add_alert "PROXY" "health check returned: ${trimmed}" "critical"
    fi
  fi
}

# 8. Worker crashes (from crash-monitor state if available)
check_worker_crashes() {
  local crash_log crash_cooldown recent_crashes

  crash_log="$HOME/.hermes/state/crash_prevention_alerts.log"
  if [ -f "$crash_log" ]; then
    recent_crashes=$(find "$crash_log" -newermt "-30 minutes" -type f 2>/dev/null | wc -l)
    if [ "$recent_crashes" -gt 0 ] 2>/dev/null; then
      local recent_errors
      recent_errors=$(grep -E "$(date '+%Y-%m-%d')" "$crash_log" 2>/dev/null | grep -ciE "CRASH|ERROR|KILLED|OOM" || echo 0)
      if [ "$recent_errors" -gt 0 ] 2>/dev/null; then
        add_alert "WORKERS" "${recent_errors} crash/error events in crash_prevention_alerts.log today" "high"
      fi
    fi
  fi

  local analysis_log
  analysis_log="$HOME/.hermes/logs/crash_analysis.log"
  if [ -f "$analysis_log" ]; then
    local recent_oom
    recent_oom=$(tail -50 "$analysis_log" 2>/dev/null | grep -c "OOM_DETECTED\|CRASH_DETECTED" || echo 0)
    if [ "$recent_oom" -gt 0 ] 2>/dev/null; then
      local last_entry_ts last_entry
      last_entry=$(grep "OOM_DETECTED\|CRASH_DETECTED" "$analysis_log" 2>/dev/null | tail -1)
      last_entry_ts=$(echo "$last_entry" | grep -oE '^\[[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\]' | tr -d '[]')
      if [ -n "$last_entry_ts" ]; then
        local now_ts entry_age
        now_ts=$(date '+%s')
        entry_age=$(date -d "$last_entry_ts" '+%s' 2>/dev/null)
        if [ -n "$entry_age" ] && [ $((now_ts - entry_age)) -lt 1800 ] 2>/dev/null; then
          add_alert "WORKERS" "recent crash/OOM detected: $(echo "$last_entry" | head -c 120)" "high"
        fi
      fi
    fi
  fi

  crash_cooldown="$HOME/.hermes/state/crash_cooldown.json"
  if [ -f "$crash_cooldown" ]; then
    local cooldown_content
    cooldown_content=$(cat "$crash_cooldown" 2>/dev/null)
    if [ -n "$cooldown_content" ] && echo "$cooldown_content" | grep -q "cooldown\|crash_loop" 2>/dev/null; then
      local in_cooldown
      in_cooldown=$(echo "$cooldown_content" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    if any(v for v in d.values() if isinstance(v, (str, int, float)) and 'cooldown' in str(v).lower()):
        print(1)
    elif any(v for v in d.values() if isinstance(v, bool) and v):
        print(1)
    else:
        print(0)
except:
    print(0)
" 2>/dev/null)
      if [ "$in_cooldown" -eq 1 ] 2>/dev/null; then
        add_alert "WORKERS" "crash cooldown active — worker in crash loop protection" "high"
      fi
    fi
  fi
}

# 9. VPS health (from recent vps-health-check output if available)
check_vps_health() {
  local vps_output_dir recent_output

  vps_output_dir="$HOME/.hermes/profiles/manager/cron/output"

  if [ -d "$vps_output_dir" ]; then
    local vps_dirs
    vps_dirs=$(find "$vps_output_dir" -maxdepth 1 -type d -name "*vps*" -o -name "*dual*" 2>/dev/null)
    for vdir in $vps_dirs; do
      local vps_files
      vps_files=$(find "$vdir" -name "*.md" -newermt "-1 hour" -type f 2>/dev/null | head -2)
      for f in $vps_files; do
        local vps_errors
        vps_errors=$(grep -ciE "error|fail|down|critical|unreachable|oom|container.*stop" "$f" 2>/dev/null || echo 0)
        if [ "$vps_errors" -gt 0 ] 2>/dev/null; then
          local summary
          summary=$(grep -iE "⚠️|🔴|error|fail|down|critical|unreachable" "$f" 2>/dev/null | head -2 | tr '\n' ' ' | head -c 150)
          add_alert "VPS" "health check errors: ${summary}" "high"
          break 2
        fi
      done
    done
  fi

  local dual_vps_script
  dual_vps_script="$HOME/.hermes/bot/scripts/dual-vps-watchdog.py"
  if [ -f "$dual_vps_script" ]; then
    local dual_vps_log
    dual_vps_log="$HOME/.hermes/logs/dual-vps-watchdog.log"
    if [ -f "$dual_vps_log" ]; then
      local recent_vps_errors
      recent_vps_errors=$(tail -20 "$dual_vps_log" 2>/dev/null | grep -ciE "error|fail|down|unreachable" || echo 0)
      if [ "$recent_vps_errors" -gt 0 ] 2>/dev/null; then
        local last_err
        last_err=$(grep -iE "error|fail|down|unreachable" "$dual_vps_log" 2>/dev/null | tail -1 | head -c 150)
        add_alert "VPS" "dual-vps-watchdog: ${last_err}" "high"
      fi
    fi
  fi
}

# ----------------------------------------------------------------------------
# HARDWARE CHECKS (10-15) — ALWAYS RUN, no API gate
# ----------------------------------------------------------------------------

# 10. USB peripheral changes
check_usb_peripherals() {
  local periph_script periph_output
  # Try multiple possible locations for peripheral_lock.py
  for candidate in \
    /home/c03rad0r/repos/balloon-fresh/tools/peripheral_lock.py \
    /home/c03rad0r/repos/esp32-balloon-integration-fresh/tools/peripheral_lock.py \
    /home/c03rad0r/repos/balloon-e80bench/tools/peripheral_lock.py \
    /home/c03rad0r/esp32-balloon-integration/tools/peripheral_lock.py; do
    if [ -f "$candidate" ]; then
      periph_script="$candidate"
      break
    fi
  done

  if [ -z "$periph_script" ]; then
    return  # Script not found — skip silently
  fi

  periph_output=$(python3 "$periph_script" status 2>/dev/null)
  if [ $? -ne 0 ] || [ -z "$periph_output" ]; then
    return  # Failed — skip silently
  fi

  # Normalize: compact JSON for comparison
  local current_normalized
  current_normalized=$(echo "$periph_output" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(json.dumps(d, sort_keys=True, separators=(',',':')))
except:
    print(sys.stdin.read().strip())
" 2>/dev/null)

  if [ -f "$PERIPH_STATE" ]; then
    local prev_state
    prev_state=$(cat "$PERIPH_STATE" 2>/dev/null)
    if [ "$prev_state" != "$current_normalized" ]; then
      # Peripherals changed — generate description
      local change_desc
      change_desc=$(python3 -c "
import json, sys
try:
    prev = json.loads('''${prev_state}''')
    curr = json.loads('''${current_normalized}''')
    changes = []
    prev_devs = {d.get('name',d.get('id','?')): d for d in prev.get('devices',[])} if isinstance(prev, dict) else {}
    curr_devs = {d.get('name',d.get('id','?')): d for d in curr.get('devices',[])} if isinstance(curr, dict) else {}
    for name in set(prev_devs) | set(curr_devs):
        if name in prev_devs and name not in curr_devs:
            changes.append(f'{name} disconnected')
        elif name not in prev_devs and name in curr_devs:
            changes.append(f'{name} connected')
    if not changes:
        changes.append('state changed')
    print('; '.join(changes[:5]))
except:
    print('peripheral state changed')
" 2>/dev/null)
      add_alert "USB-PERIPHERAL" "🔗 HARDWARE: USB peripheral change detected — ${change_desc}" "medium"
    fi
  fi

  # Update state file
  echo "$current_normalized" > "$PERIPH_STATE" 2>/dev/null
}

# 11. Serial port availability
check_serial_ports() {
  local current_ports
  current_ports=$(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | sort)
  current_ports=${current_ports:-}  # empty if no ports

  if [ -f "$SERIAL_STATE" ]; then
    local prev_ports
    prev_ports=$(cat "$SERIAL_STATE" 2>/dev/null)

    if [ "$prev_ports" != "$current_ports" ]; then
      local appeared disappeared change_desc
      # Compute differences
      if [ -z "$prev_ports" ] && [ -n "$current_ports" ]; then
        change_desc="ports appeared: $(echo "$current_ports" | tr '\n' ' ')"
      elif [ -n "$prev_ports" ] && [ -z "$current_ports" ]; then
        change_desc="all ports disappeared (was: $(echo "$prev_ports" | tr '\n' ' '))"
      else
        # Compute appeared/disappeared
        local appeared_list disappeared_list
        appeared_list=$(comm -13 <(echo "$prev_ports") <(echo "$current_ports") 2>/dev/null || echo "")
        disappeared_list=$(comm -23 <(echo "$prev_ports") <(echo "$current_ports") 2>/dev/null || echo "")
        change_desc=""
        [ -n "$appeared_list" ] && change_desc="appeared: $(echo "$appeared_list" | tr '\n' ' ')"
        [ -n "$disappeared_list" ] && change_desc="${change_desc}${change_desc:+; }disappeared: $(echo "$disappeared_list" | tr '\n' ' ')"
        [ -z "$change_desc" ] && change_desc="port list changed"
      fi
      add_alert "SERIAL-PORTS" "🔌 HARDWARE: Serial port change — ${change_desc}" "medium"
    fi
  fi

  # Update state file
  echo "$current_ports" > "$SERIAL_STATE" 2>/dev/null
}

# 12. Board access violations
check_board_access() {
  local board_monitor output
  board_monitor="$SCRIPT_DIR/board-access-monitor.sh"
  if [ ! -f "$board_monitor" ]; then
    return
  fi

  output=$(bash "$board_monitor" 2>/dev/null)
  if [ -n "$output" ]; then
    # Take first few lines of output
    local summary
    summary=$(echo "$output" | head -3 | tr '\n' ' ' | head -c 200)
    add_alert "BOARD-ACCESS" "🔒 HARDWARE: Board access violation — ${summary}" "high"
  fi
}

# 13. FIPS health
check_fips_health() {
  local fips_script output
  fips_script="$SCRIPT_DIR/fips-health-check.py"
  if [ ! -f "$fips_script" ]; then
    return
  fi

  output=$(python3 "$fips_script" 2>/dev/null)
  if [ -n "$output" ] && echo "$output" | grep -qiE "error|fail|critical"; then
    local summary
    summary=$(echo "$output" | grep -iE "error|fail|critical" | head -3 | tr '\n' ' ' | head -c 200)
    add_alert "FIPS-HEALTH" "📡 HARDWARE: FIPS health issue — ${summary}" "high"
  fi
}

# 14. Balloon board status (from board_watcher cron output)
check_balloon_board() {
  # Check for board_watcher output in cron output dirs
  local cron_output_dir watcher_output

  cron_output_dir="$HOME/.hermes/profiles/manager/cron/output"

  # Look for board_watcher-related cron job dirs (by scanning recent output files)
  if [ -d "$cron_output_dir" ]; then
    # Check board-access-monitor output (b6df723690d7) for board detection events
    local board_access_dir
    board_access_dir="$cron_output_dir/b6df723690d7"
    if [ -d "$board_access_dir" ]; then
      local recent_board_output
      recent_board_output=$(find "$board_access_dir" -name "*.md" -newermt "-30 minutes" -type f 2>/dev/null | head -1)
      if [ -n "$recent_board_output" ]; then
        local board_content
        board_content=$(cat "$recent_board_output" 2>/dev/null)
        if echo "$board_content" | grep -qiE "board.*detect|BOOTSEL|RP2040|firmware.*flash|error|fail"; then
          local summary
          summary=$(echo "$board_content" | grep -iE "board.*detect|BOOTSEL|RP2040|firmware.*flash|error|fail" | head -2 | tr '\n' ' ' | head -c 200)
          add_alert "BALLOON-BOARD" "🎈 HARDWARE: Balloon board — ${summary}" "medium"
        fi
      fi
    fi

    # Also check for board_watcher-specific output dirs
    local board_dirs
    board_dirs=$(find "$cron_output_dir" -maxdepth 1 -type d 2>/dev/null | while read d; do
      # Check if any recent file in this dir mentions board_watcher or BOOTSEL
      if find "$d" -name "*.md" -newermt "-30 minutes" -type f 2>/dev/null | head -1 | xargs grep -lqiE "board_watcher|BOOTSEL|balloon.*board" 2>/dev/null | head -1 >/dev/null; then
        echo "$d"
      fi
    done | head -1)

    if [ -n "$board_dirs" ]; then
      local recent_file
      recent_file=$(find "$board_dirs" -name "*.md" -newermt "-30 minutes" -type f 2>/dev/null | head -1)
      if [ -n "$recent_file" ]; then
        local board_summary
        board_summary=$(grep -iE "board_watcher|BOOTSEL|balloon.*board|error|fail" "$recent_file" 2>/dev/null | head -2 | tr '\n' ' ' | head -c 200)
        [ -n "$board_summary" ] && add_alert "BALLOON-BOARD" "🎈 HARDWARE: Balloon board — ${board_summary}" "medium"
      fi
    fi
  fi

  # Also try running board_watcher.sh directly if it exists and check its state files
  local board_watcher_script
  board_watcher_script="$SCRIPT_DIR/board_watcher.sh"
  if [ -f "$board_watcher_script" ]; then
    # Check board_watcher state files for recent board detection
    if [ -f /tmp/balloon-test-state ] && [ -f /tmp/balloon-phase ]; then
      local balloon_state balloon_phase
      balloon_state=$(cat /tmp/balloon-test-state 2>/dev/null)
      balloon_phase=$(cat /tmp/balloon-phase 2>/dev/null)
      if [ -n "$balloon_state" ] && echo "$balloon_state" | grep -qiE "error|fail|abort|timeout"; then
        add_alert "BALLOON-BOARD" "🎈 HARDWARE: Balloon board test error — $(echo "$balloon_state" | head -c 150)" "high"
      fi
    fi
  fi
}

# 15. microFIPS
check_microfips() {
  local microfips_script output
  microfips_script="$SCRIPT_DIR/microfips-monitor.sh"
  if [ ! -f "$microfips_script" ]; then
    return
  fi

  output=$(bash "$microfips_script" 2>/dev/null)
  if [ -n "$output" ] && echo "$output" | grep -qiE "anomaly|error|fail"; then
    local summary
    summary=$(echo "$output" | grep -iE "anomaly|error|fail" | head -3 | tr '\n' ' ' | head -c 200)
    add_alert "MICROFIPS" "📶 HARDWARE: microFIPS — ${summary}" "high"
  fi
}

# ----------------------------------------------------------------------------
# DEDUP: Once-per-day with severity-change detection
# ----------------------------------------------------------------------------

# Load state file
dedup_load_state() {
  if [ -f "$STATE_FILE" ]; then
    cat "$STATE_FILE" 2>/dev/null
  else
    echo '{"alerts":{}}'
  fi
}

# Get severity rank (0=low, 1=medium, 2=high, 3=critical)
# Usage: severity_rank "high" -> 2
severity_rank() {
  local sev="$1"
  case "$sev" in
    low)     echo 0 ;;
    medium)  echo 1 ;;
    high)    echo 2 ;;
    critical) echo 3 ;;
    *)       echo 1 ;;
  esac
}

# Run dedup logic using Python for JSON handling
# Input: ALERTS array (category|severity|description), state file
# Output: filtered alerts (those that should be delivered) to stdout
# Also outputs "✅ RESOLVED" lines for conditions that cleared
dedup_filter() {
  # Build JSON array of current alerts from ALERTS array
  local current_json
  current_json="["
  local first=1
  for alert in "${ALERTS[@]}"; do
    local category severity description
    IFS='|' read -r category severity description <<< "$alert"
    # Escape description for JSON
    local escaped_desc
    escaped_desc=$(echo "$description" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().strip()))" 2>/dev/null || echo "\"$description\"")
    if [ $first -eq 1 ]; then
      first=0
    else
      current_json+=","
    fi
    current_json+="{\"category\":\"$category\",\"severity\":\"$severity\",\"description\":$escaped_desc}"
  done
  current_json+="]"

  # Run the dedup logic in Python
  echo "$current_json" | python3 -c "
import sys, json, time

DAY_SECONDS = 86400

try:
    current_alerts = json.load(sys.stdin)
except:
    current_alerts = []

# Load state
try:
    with open('$STATE_FILE') as f:
        state = json.load(f)
except:
    state = {'alerts': {}}

if 'alerts' not in state:
    state['alerts'] = {}

now = time.time()
severity_order = {'low': 0, 'medium': 1, 'high': 2, 'critical': 3}

# Build current alert map: category -> {severity, description}
current_map = {}
for a in current_alerts:
    cat = a['category']
    sev = a['severity']
    desc = a['description']
    # Keep highest severity if multiple alerts for same category
    if cat in current_map:
        if severity_order.get(sev, 1) > severity_order.get(current_map[cat]['severity'], 1):
            current_map[cat] = {'severity': sev, 'description': desc}
    else:
        current_map[cat] = {'severity': sev, 'description': desc}

stored = state['alerts']
output_lines = []
resolved_lines = []
new_state = {}

# Process categories that are currently alerting
for cat, info in current_map.items():
    cur_sev = info['severity']
    cur_desc = info['description']
    cur_rank = severity_order.get(cur_sev, 1)

    if cat in stored:
        prev = stored[cat]
        prev_sev = prev.get('severity', 'medium')
        prev_rank = severity_order.get(prev_sev, 1)
        last_alerted = prev.get('last_alerted', 0)

        if cur_rank != prev_rank:
            # Severity changed -> alert immediately
            output_lines.append(f\"⚠️ {cat}: {cur_desc} [{cur_sev}]\")
            new_state[cat] = {'severity': cur_sev, 'last_alerted': now}
        elif now - last_alerted >= DAY_SECONDS:
            # Same severity, 24h elapsed -> daily reminder
            output_lines.append(f\"⚠️ {cat}: {cur_desc} [{cur_sev}]\")
            new_state[cat] = {'severity': cur_sev, 'last_alerted': now}
        else:
            # Same severity, within 24h -> suppress
            new_state[cat] = {'severity': cur_sev, 'last_alerted': last_alerted}
    else:
        # New condition -> alert immediately
        output_lines.append(f\"⚠️ {cat}: {cur_desc} [{cur_sev}]\")
        new_state[cat] = {'severity': cur_sev, 'last_alerted': now}

# Process categories that were previously alerting but are now resolved
for cat, prev in stored.items():
    if cat not in current_map:
        # Condition resolved -> alert once, then clear state
        prev_sev = prev.get('severity', 'medium')
        last_alerted = prev.get('last_alerted', 0)
        # Only send resolved notification if we actually alerted about this before
        # (last_alerted > 0 means we did)
        if last_alerted > 0:
            resolved_lines.append(f\"✅ RESOLVED: {cat} — condition cleared (was {prev_sev})\")
        # Don't add to new_state -> clears it

# Write updated state
state['alerts'] = new_state
try:
    with open('$STATE_FILE', 'w') as f:
        json.dump(state, f, indent=2, sort_keys=True)
except:
    pass

# Output: resolved lines first, then active alerts
for line in resolved_lines:
    print(line)
for line in output_lines:
    print(line)
" 2>/dev/null
}

# ----------------------------------------------------------------------------
# Run all checks — SYSTEM first, then HARDWARE (always)
# ----------------------------------------------------------------------------

# System checks
check_memory
check_swap_thrashing
check_disk
check_cpu_load
check_api_quota
check_dispatch
check_proxy_health
check_worker_crashes
check_vps_health

# Hardware checks — ALWAYS run, no API gate
check_usb_peripherals
check_serial_ports
check_board_access
check_fips_health
check_balloon_board
check_microfips

# ----------------------------------------------------------------------------
# Output with dedup (once-per-day + severity-change detection)
# ----------------------------------------------------------------------------

if [ ${#ALERTS[@]} -eq 0 ]; then
  # No active alerts — but we still need to run dedup to emit RESOLVED messages
  # for any conditions that just cleared
  RESOLVED_OUTPUT=$(dedup_filter)
  if [ -n "$RESOLVED_OUTPUT" ]; then
    echo "🚨 System Anomaly Report — $(date '+%H:%M %b %d')"
    echo ""
    echo "$RESOLVED_OUTPUT"
    echo "---"
    echo "Reported by unified-system-alert.sh (every 30 min)"
  fi
  # If no resolved output either -> empty stdout = silent
  exit 0
fi

# Run dedup filter
FILTERED_OUTPUT=$(dedup_filter)

if [ -n "$FILTERED_OUTPUT" ]; then
  echo "🚨 System Anomaly Report — $(date '+%H:%M %b %d')"
  echo ""
  echo "$FILTERED_OUTPUT"
  echo "---"
  echo "Reported by unified-system-alert.sh (every 30 min)"
fi

# If dedup suppressed everything -> empty stdout = silent
exit 0