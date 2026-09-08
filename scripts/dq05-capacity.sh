#!/usr/bin/env bash
# dq05-capacity.sh — zero-token capacity probe (Phase 0, t_224f750a).
#
# Answers: "is DQ05 available and underloaded enough to offload CPU-heavy
# work?" Pure shell — NO LLM in the loop. This is a PROBE, not a scheduler.
#
# Probe order:
#   1. curl LAN (then Netbird) :9100 resource-monitor JSON  (3s timeout)
#   2. fallback: ssh dq05 (BatchMode, 3s connect) — load/nproc/mem/disk
#      in one round trip. A bare `/proc/loadavg` reply is tolerated too
#      (load-only -> RAM/disk unknown -> fail-soft LOCAL).
#
# Decision rule (ALL must hold to print OFFLOAD-OK; anything else = LOCAL,
# fail-soft):
#   local load/core          > 2.0     (local machine is stressed)
#   DQ05 load/core           < 1.0     (remote has headroom)
#   DQ05 free RAM            > 2048 MB (remote can hold the task)
#   DQ05_CAP_SELF_CONTAINED == 1       (caller asserts task is self-contained)
#
# Output: one JSON object on stdout (last line), progress on stderr.
# Keys: reachable, source(ssh|curl|none), load (1-min), load_per_core,
# free_ram_mb, free_disk_mb, decision, reason. Extra machine facts included.
#
# Env overrides (for tests / portability):
#   DQ05_CAP_LOCAL_LOAD1     default: local 1-min load from /proc/loadavg
#   DQ05_CAP_LOCAL_CORES     default: `nproc`
#   DQ05_CAP_DQ05_CORES      default: 4  (N95 proplus; schema may supply cores)
#   DQ05_CAP_CURL_HOSTS      default: "192.168.1.218 100.90.22.201" (LAN,Netbird)
#   DQ05_CAP_CURL_PORT       default: 9100
#   DQ05_CAP_CURL_PATH       default: /local
#   DQ05_CAP_CURL_TIMEOUT    default: 3
#   DQ05_CAP_SSH_TIMEOUT     default: 4
#   DQ05_CAP_SSH_HOST        default: dq05
#   DQ05_CAP_SELF_CONTAINED  default: 0  -> caller must set 1 to allow OFFLOAD-OK
set -u

# ── config ──────────────────────────────────────────────────────────────
LOCAL_LOAD1="${DQ05_CAP_LOCAL_LOAD1:-$(cut -d' ' -f1 /proc/loadavg 2>/dev/null || echo 0)}"
LOCAL_CORES="${DQ05_CAP_LOCAL_CORES:-$(nproc 2>/dev/null || echo 1)}"
DQ05_CORES="${DQ05_CAP_DQ05_CORES:-4}"
CURL_HOSTS="${DQ05_CAP_CURL_HOSTS:-192.168.1.218 100.90.22.201}"
CURL_PORT="${DQ05_CAP_CURL_PORT:-9100}"
CURL_PATH="${DQ05_CAP_CURL_PATH:-/local}"
CURL_TIMEOUT="${DQ05_CAP_CURL_TIMEOUT:-3}"
SSH_TIMEOUT="${DQ05_CAP_SSH_TIMEOUT:-4}"
SSH_HOST="${DQ05_CAP_SSH_HOST:-dq05}"
SELF_CONTAINED="${DQ05_CAP_SELF_CONTAINED:-0}"

# ── numeric sanity (never let junk env produce malformed JSON / div-by-zero) ──
# LOCAL_CORES / DQ05_CORES must be positive ints; LOCAL_LOAD1 must be a number.
if ! [[ "$LOCAL_LOAD1" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
  LOCAL_LOAD1=0
fi
if ! [[ "$LOCAL_CORES" =~ ^[0-9]+$ ]] || (( LOCAL_CORES <= 0 )); then
  LOCAL_CORES="$(nproc 2>/dev/null || echo 1)"
fi
if ! [[ "$DQ05_CORES" =~ ^[0-9]+$ ]] || (( DQ05_CORES <= 0 )); then
  DQ05_CORES=4
fi

# ── helpers ─────────────────────────────────────────────────────────────
# Extract the first value of a named numeric JSON key from a string.
#   json_num "$BODY" load1       -> 0.12   (or empty)
#   json_num "$BODY" load_avg    -> first element if the value is an array,
#                                  else the scalar value
json_num() {
  local body="$1" key="$2"
  # scalar form first: "key": 0.12
  local s
  s=$(printf '%s' "$body" | grep -oE "\"${key}\"[[:space:]]*:[[:space:]]*[0-9.]+" \
      | head -1 | grep -oE '[0-9.]+$' || true)
  if [[ -n "$s" ]]; then
    printf '%s' "$s"
    return
  fi
  # array form: "key":[0.08, ...
  printf '%s' "$body" | grep -oE "\"${key}\"[[:space:]]*:[[:space:]]*\[[0-9.]+" \
    | head -1 | grep -oE '[0-9.]+$' || true
}
# GB -> integer MB (truncating). 5.3 -> 5427 (uses awk float then floor).
gb_to_mb() {
  awk -v g="$1" 'BEGIN{printf "%d", g*1024}'
}

# ── 1. curl resource-monitor (:9100) ────────────────────────────────────
BODY=""
SRC="none"
for host in $CURL_HOSTS; do
  got=$(timeout "${CURL_TIMEOUT}" curl -s -m "${CURL_TIMEOUT}" \
        "http://${host}:${CURL_PORT}${CURL_PATH}" 2>/dev/null) || continue
  if [[ "$got" == \{* ]]; then
    BODY="$got"
    break
  fi
done

# Parse curl JSON (schema-variant tolerant).
DQ05_LOAD=""
DQ05_MEM_MB=""
DQ05_DISK_MB=""
if [[ -n "$BODY" ]]; then
  SRC="curl"
  # load: load_avg array first element, else load1 scalar
  DQ05_LOAD=$(json_num "$BODY" 'load_avg')
  [[ -z "$DQ05_LOAD" ]] && DQ05_LOAD=$(json_num "$BODY" load1)
  # optional cores in schema
  c=$(json_num "$BODY" cores)
  [[ -n "$c" ]] && DQ05_CORES="$c"
  # RAM: available_gb (GB) else avail_mb (MB)
  agb=$(json_num "$BODY" available_gb)
  if [[ -n "$agb" ]]; then
    DQ05_MEM_MB=$(gb_to_mb "$agb")
  else
    DQ05_MEM_MB=$(json_num "$BODY" avail_mb)
  fi
  # disk: free_gb (GB) else free_mb (MB)
  dgb=$(json_num "$BODY" free_gb)
  if [[ -n "$dgb" ]]; then
    DQ05_DISK_MB=$(gb_to_mb "$dgb")
  else
    DQ05_DISK_MB=$(json_num "$BODY" free_mb)
  fi
fi

# ── 2. fallback: ssh dq05 ───────────────────────────────────────────────
if [[ -z "$BODY" ]]; then
  read -r -d '' REMOTE <<'REMOTE' || true
echo LOAD=$(cut -d' ' -f1 /proc/loadavg)
echo NPROC=$(nproc)
echo MEMAVAIL_MB=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
echo DISKFREE_MB=$(df -m / | awk 'NR==2{print $4}')
REMOTE
  sshout=$(timeout "${SSH_TIMEOUT}" ssh -o BatchMode=yes -o ConnectTimeout=3 \
            "${SSH_HOST}" "$REMOTE" 2>/dev/null) || sshout=""
  if [[ -n "$sshout" ]]; then
    SRC="ssh"
    # token-tolerant: fields may be line-delimited or space-delimited
    DQ05_LOAD=$(printf '%s\n' "$sshout" | grep -oE '(^|[ =])LOAD=[0-9.]+' | head -1 | grep -oE '[0-9.]+$')
    n=$(printf '%s\n' "$sshout" | grep -oE '(^|[ =])NPROC=[0-9]+' | head -1 | grep -oE '[0-9]+$')
    [[ -n "$n" ]] && DQ05_CORES="$n"
    DQ05_MEM_MB=$(printf '%s\n' "$sshout" | grep -oE '(^|[ =])MEMAVAIL_MB=[0-9]+' | head -1 | grep -oE '[0-9]+$')
    DQ05_DISK_MB=$(printf '%s\n' "$sshout" | grep -oE '(^|[ =])DISKFREE_MB=[0-9]+' | head -1 | grep -oE '[0-9]+$')
    # tolerate bare loadavg reply (e.g. body-literal `cat /proc/loadavg`)
    if [[ -z "$DQ05_LOAD" ]]; then
      DQ05_LOAD=$(printf '%s\n' "$sshout" | grep -oE '^[0-9]+\.[0-9]+' | head -1)
    fi
  fi
fi

# ── decision ────────────────────────────────────────────────────────────
REACHABLE=false
[[ "$SRC" != "none" ]] && REACHABLE=true

# float compare helper: returns true if A > B (or >= when op passed)
gt() { awk -v a="$1" -v b="$2" 'BEGIN{exit !(a>b)}'; }

DECISION="LOCAL"
REASON=""
if [[ "$REACHABLE" == false ]]; then
  REASON="unreachable: curl and ssh both failed (fail-soft local)"
else
  local_per_core=$(awk -v l="$LOCAL_LOAD1" -v c="$LOCAL_CORES" 'BEGIN{printf "%.2f", l/c}')
  dq05_per_core=""
  if [[ -n "$DQ05_LOAD" ]] && [[ "$DQ05_CORES" =~ ^[0-9]+$ ]] && (( DQ05_CORES > 0 )); then
    dq05_per_core=$(awk -v l="$DQ05_LOAD" -v c="$DQ05_CORES" 'BEGIN{printf "%.2f", l/c}')
  fi
  failures=()
  # rule 1: local stressed
  if ! gt "$local_per_core" "2.0"; then
    failures+=("local_not_stressed(local=${local_per_core}/core<=2.0)")
  fi
  # rule 2: DQ05 has headroom
  if [[ -z "$dq05_per_core" ]] || ! gt "1.0" "$dq05_per_core"; then
    failures+=("dq05_loaded_or_unknown(load=${DQ05_LOAD:-?}/core=${dq05_per_core:-?}>=1.0)")
  fi
  # rule 3: RAM > 2GB
  if [[ -z "$DQ05_MEM_MB" ]] || ! gt "$DQ05_MEM_MB" "2048"; then
    failures+=("dq05_low_ram_or_unknown(ram_mb=${DQ05_MEM_MB:-?})")
  fi
  # rule 4: caller asserts self-contained
  if [[ "$SELF_CONTAINED" != "1" ]]; then
    failures+=("not_self_contained(DQ05_CAP_SELF_CONTAINED!=1)")
  fi
  if (( ${#failures[@]} == 0 )); then
    DECISION="OFFLOAD-OK"
  else
    REASON=$(IFS='; '; echo "${failures[*]}")
  fi
fi

# ── emit JSON (last line of stdout is the verdict) ──────────────────────
dq05_per_core_out=""
if [[ -n "$DQ05_LOAD" ]] && [[ "$DQ05_CORES" =~ ^[0-9]+$ ]] && (( DQ05_CORES > 0 )); then
  dq05_per_core_out=$(awk -v l="$DQ05_LOAD" -v c="$DQ05_CORES" 'BEGIN{printf "%.2f", l/c}')
fi
local_per_core_out=$(awk -v l="$LOCAL_LOAD1" -v c="$LOCAL_CORES" 'BEGIN{printf "%.2f", l/c}')

printf '{"reachable":%s,"source":"%s","load":%s,"load_per_core":%s,"cores":%s,"free_ram_mb":%s,"free_disk_mb":%s,"local_load1":%s,"local_cores":%s,"local_load_per_core":%s,"decision":"%s","reason":"%s"}\n' \
  "$([[ $REACHABLE == true ]] && echo true || echo false)" \
  "$SRC" \
  "${DQ05_LOAD:-null}" \
  "${dq05_per_core_out:-null}" \
  "$DQ05_CORES" \
  "${DQ05_MEM_MB:-null}" \
  "${DQ05_DISK_MB:-null}" \
  "$LOCAL_LOAD1" \
  "$LOCAL_CORES" \
  "$local_per_core_out" \
  "$DECISION" \
  "$REASON"
exit 0
