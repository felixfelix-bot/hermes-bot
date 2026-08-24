#!/usr/bin/env bash
# ============================================================================
# unified-system-alert.sh — Comprehensive system anomaly monitor
#
# Checks ALL system health indicators in one pass. Uses anomaly-only pattern:
#   empty stdout = silent (system healthy), non-empty = alert text to operator
#
# Checks:
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
# Integrates alert_dedup.py for exponential backoff on repeated alerts.
# Schedule: every 30 min, deliver=origin, no_agent=true
# ============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEDUP_PY="$SCRIPT_DIR/alert_dedup.py"
ALERTS=()

# ----------------------------------------------------------------------------
# Helper: add alert with severity
# ----------------------------------------------------------------------------
add_alert() {
  local category="$1"
  local description="$2"
  local severity="${3:-medium}"
  ALERTS+=("⚠️ ${category}: ${description} [${severity}]")
}

# ----------------------------------------------------------------------------
# 1. System memory pressure (RAM + swap)
# ----------------------------------------------------------------------------
check_memory() {
  local mem_info ram_pct swap_used swap_total
  mem_info=$(free -m 2>/dev/null)
  if [ -z "$mem_info" ]; then
    return
  fi

  ram_pct=$(echo "$mem_info" | awk '/^Mem:/ {printf "%.0f", $3*100/$2}')
  swap_used=$(echo "$mem_info" | awk '/^Swap:/ {print $3}')
  swap_total=$(echo "$mem_info" | awk '/^Swap:/ {print $2}')

  # Defaults for safety
  ram_pct=${ram_pct:-0}
  swap_used=${swap_used:-0}

  if [ "$ram_pct" -gt 90 ] 2>/dev/null; then
    add_alert "RAM" "${ram_pct}% used — critical, system may OOM" "critical"
  elif [ "$ram_pct" -gt 70 ] 2>/dev/null; then
    add_alert "RAM" "${ram_pct}% used — high (consider: pkill -f opencode to free sessions)" "high"
  fi

  # Swap > 5GB (5000 MB)
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

# ----------------------------------------------------------------------------
# 2. Swap thrashing (vmstat si/so > 1000 sustained)
# ----------------------------------------------------------------------------
check_swap_thrashing() {
  local vmstat_line si so
  # vmstat 1 2: first line is averages since boot, second is 1-second sample
  vmstat_line=$(vmstat 1 2 2>/dev/null | tail -1)
  if [ -z "$vmstat_line" ]; then
    return
  fi

  # si = column 7, so = column 8 in standard vmstat output
  si=$(echo "$vmstat_line" | awk '{print $7}')
  so=$(echo "$vmstat_line" | awk '{print $8}')
  si=${si:-0}
  so=${so:-0}

  if [ "$si" -gt 1000 ] 2>/dev/null || [ "$so" -gt 1000 ] 2>/dev/null; then
    add_alert "SWAP-THRASH" "vmstat si=${si} so=${so} — active thrashing detected" "critical"
  fi
}

# ----------------------------------------------------------------------------
# 3. Disk space (any mount > 90% used OR < 15GB free)
# ----------------------------------------------------------------------------
check_disk() {
  # df output: Filesystem Size Used Avail Use% Mounted on
  # Use -B1 to get bytes, then compute. Actually simpler: use --output
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

    # Skip non-numeric (some filesystems report differently)
    [ -z "$pct" ] && continue
    [[ ! "$pct" =~ ^[0-9]+$ ]] && continue

    avail_gb=0
    if [ -n "$avail_bytes" ] && [[ "$avail_bytes" =~ ^[0-9]+$ ]]; then
      avail_gb=$((avail_bytes / 1073741824))  # 1GB = 1073741824 bytes
    fi

    # Skip pseudo-filesystems and small system mounts
    case "$mount" in
      /dev/*|/proc/*|/sys/*|/run|/run/*|tmpfs|devtmpfs|none|/dev/shm|/boot/efi|/tmp|/var/tmp) continue ;;
    esac

    # Skip mounts smaller than 20GB total (avoid noise from tiny partitions)
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

# ----------------------------------------------------------------------------
# 4. CPU load (load_per_core > 2.0)
# ----------------------------------------------------------------------------
check_cpu_load() {
  local load_1m cpu_cores load_per_core
  load_1m=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null)
  cpu_cores=$(nproc 2>/dev/null)
  if [ -z "$load_1m" ] || [ -z "$cpu_cores" ] || [ "$cpu_cores" -eq 0 ]; then
    return
  fi

  load_per_core=$(echo "$load_1m $cpu_cores" | awk '{printf "%.1f", $1/$2}')

  # Compare using bc or awk
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

# ----------------------------------------------------------------------------
# 5. API quota (z.ai weekly >= 85%)
# ----------------------------------------------------------------------------
check_api_quota() {
  local quota_json weekly_pct
  quota_json=$(curl -sf --connect-timeout 5 http://localhost:9099/quota 2>/dev/null)
  if [ -z "$quota_json" ]; then
    return  # Proxy not running — proxy health check will catch that
  fi

  # Try to parse weekly quota percentage
  weekly_pct=$(echo "$quota_json" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    ours = d.get('ours', {})
    # Check max_pct first (highest across all windows)
    max_pct = ours.get('max_pct', 0)
    # Also check windows for weekly specifically
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

# ----------------------------------------------------------------------------
# 6. Dispatch daemon (paused or not running)
# ----------------------------------------------------------------------------
check_dispatch() {
  local daemon_active pause_log
  daemon_active=$(systemctl --user is-active hermes-dispatch 2>/dev/null)

  if [ "$daemon_active" != "active" ]; then
    add_alert "DISPATCH" "daemon not active (status: ${daemon_active:-unknown}) — tasks not dispatching" "critical"
    return
  fi

  # Check for GATE PAUSE or quota-related pauses in recent logs
  pause_log=$(journalctl --user -u hermes-dispatch --since "5 min ago" --no-pager 2>/dev/null | grep -iE "GATE PAUSE|quota.*pause|paused" | tail -1)
  if [ -n "$pause_log" ]; then
    local pause_msg
    pause_msg=$(echo "$pause_log" | sed 's/.*\(GATE PAUSE[^|]*\|paused[^|]*\).*/\1/' | head -c 100)
    add_alert "DISPATCH" "daemon PAUSED — ${pause_msg}" "high"
  fi
}

# ----------------------------------------------------------------------------
# 7. Proxy health (localhost:9099/health != "ok")
# ----------------------------------------------------------------------------
check_proxy_health() {
  local health_response
  health_response=$(curl -sf --connect-timeout 5 http://localhost:9099/health 2>/dev/null)

  if [ -z "$health_response" ]; then
    add_alert "PROXY" "localhost:9099 unreachable — API proxy down" "critical"
  elif [ "$health_response" != "ok" ]; then
    # Trim whitespace and check
    local trimmed
    trimmed=$(echo "$health_response" | tr -d ' \n\r')
    if [ "$trimmed" != "ok" ]; then
      add_alert "PROXY" "health check returned: ${trimmed}" "critical"
    fi
  fi
}

# ----------------------------------------------------------------------------
# 8. Worker crashes (from crash-monitor state if available)
# ----------------------------------------------------------------------------
check_worker_crashes() {
  local crash_log crash_cooldown recent_crashes

  # Check crash prevention alerts log for recent entries (last 30 min)
  crash_log="$HOME/.hermes/state/crash_prevention_alerts.log"
  if [ -f "$crash_log" ]; then
    recent_crashes=$(find "$crash_log" -newermt "-30 minutes" -type f 2>/dev/null | wc -l)
    if [ "$recent_crashes" -gt 0 ] 2>/dev/null; then
      # Check for recent ERROR/CRASH entries
      local recent_errors
      recent_errors=$(grep -E "$(date '+%Y-%m-%d')" "$crash_log" 2>/dev/null | grep -ciE "CRASH|ERROR|KILLED|OOM" || echo 0)
      if [ "$recent_errors" -gt 0 ] 2>/dev/null; then
        add_alert "WORKERS" "${recent_errors} crash/error events in crash_prevention_alerts.log today" "high"
      fi
    fi
  fi

  # Check crash_analysis.log for very recent OOM/kills (last 30 min only)
  local analysis_log
  analysis_log="$HOME/.hermes/logs/crash_analysis.log"
  if [ -f "$analysis_log" ]; then
    local recent_oom
    recent_oom=$(tail -50 "$analysis_log" 2>/dev/null | grep -c "OOM_DETECTED\|CRASH_DETECTED" || echo 0)
    if [ "$recent_oom" -gt 0 ] 2>/dev/null; then
      # Verify the entry is actually recent (within 30 min), not historical
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

  # Check crash_cooldown.json for active cooldown
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
    # Check if there's an active cooldown
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

# ----------------------------------------------------------------------------
# 9. VPS health (from recent vps-health-check output if available)
# ----------------------------------------------------------------------------
check_vps_health() {
  local vps_check_script vps_output_dir recent_output

  # Try to find recent vps-health-check output
  vps_output_dir="$HOME/.hermes/profiles/manager/cron/output"

  # Look for VPS-related cron output files that are recent (within 1 hour)
  # Use specific job directories rather than grepping all files for "vps"
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
          break 2  # One VPS alert is enough
        fi
      done
    done
  fi

  # Also check dual-vps-watchdog output if it exists
  local dual_vps_script
  dual_vps_script="$HOME/.hermes/bot/scripts/dual-vps-watchdog.py"
  if [ -f "$dual_vps_script" ]; then
    # Quick inline check — try running it with a dry-run flag, or check its output log
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
# Run all checks
# ----------------------------------------------------------------------------
check_memory
check_swap_thrashing
check_disk
check_cpu_load
check_api_quota
check_dispatch
check_proxy_health
check_worker_crashes
check_vps_health

# ----------------------------------------------------------------------------
# Output with dedup
# ----------------------------------------------------------------------------
if [ ${#ALERTS[@]} -eq 0 ]; then
  # All healthy — empty stdout = silent
  exit 0
fi

# Build alert text
ALERT_TEXT=""
for alert in "${ALERTS[@]}"; do
  ALERT_TEXT="${ALERT_TEXT}${alert}
"
done

# Run through alert_dedup.py if available
if [ -f "$DEDUP_PY" ]; then
  echo "$ALERT_TEXT" | python3 "$DEDUP_PY" gate --source unified-system-alert 2>/dev/null
  DEDUP_EXIT=$?
  if [ "$DEDUP_EXIT" -eq 0 ]; then
    # Notify — output the alerts
    echo "🚨 System Anomaly Report — $(date '+%H:%M %b %d')"
    echo ""
    echo "$ALERT_TEXT"
    echo "---"
    echo "Reported by unified-system-alert.sh (every 30 min)"
  fi
  # Exit 1 = suppressed by backoff (silent), 2 = error (fail-open below)
  if [ "$DEDUP_EXIT" -eq 2 ]; then
    # Dedup error — fail open, deliver anyway
    echo "🚨 System Anomaly Report — $(date '+%H:%M %b %d')"
    echo ""
    echo "$ALERT_TEXT"
    echo "---"
    echo "Reported by unified-system-alert.sh (every 30 min)"
  fi
else
  # No dedup module — output directly
  echo "🚨 System Anomaly Report — $(date '+%H:%M %b %d')"
  echo ""
  echo "$ALERT_TEXT"
  echo "---"
  echo "Reported by unified-system-alert.sh (every 30 min)"
fi

exit 0