#!/bin/bash
# test-check-resource-alerts.sh — Test suite for check-resource-alerts.sh
#
# Validates:
#   - Script exists and is executable
#   - No state file → NO_ALERTS, exit 0
#   - State file with no active alerts → NO_ALERTS, exit 0
#   - State file with active alerts → diagnostic sections present
#   - Script never crashes (always exits 0)
#
# Uses temporary state files in /tmp, cleans up after.

set -uo pipefail

SCRIPT="$HOME/.hermes/profiles/manager/scripts/check-resource-alerts.sh"
STATE_FILE="/tmp/unified-system-alert-state.json"
BACKUP_STATE=""
PASS=0
FAIL=0
TESTS_RUN=0

# ── Helpers ──────────────────────────────────────────────────────────────────

backup_state() {
  if [ -f "$STATE_FILE" ]; then
    BACKUP_STATE=$(cat "$STATE_FILE")
    cp "$STATE_FILE" "${STATE_FILE}.test-backup"
  fi
}

restore_state() {
  if [ -f "${STATE_FILE}.test-backup" ]; then
    cp "${STATE_FILE}.test-backup" "$STATE_FILE"
    rm -f "${STATE_FILE}.test-backup"
  else
    rm -f "$STATE_FILE"
  fi
}

cleanup() {
  rm -f "$STATE_FILE"
  rm -f "${STATE_FILE}.test-backup"
  # If we had a backup, restore it
  if [ -n "$BACKUP_STATE" ]; then
    echo "$BACKUP_STATE" > "$STATE_FILE"
  fi
}

trap cleanup EXIT

ok() {
  echo "  ✓ $1"
  PASS=$((PASS + 1))
  TESTS_RUN=$((TESTS_RUN + 1))
}

fail() {
  echo "  ✗ $1"
  FAIL=$((FAIL + 1))
  TESTS_RUN=$((TESTS_RUN + 1))
}

assert_contains() {
  local label="$1" haystack="$2" needle="$3"
  if echo "$haystack" | grep -qF "$needle"; then
    ok "$label"
  else
    fail "$label (missing: '$needle')"
  fi
}

assert_exit_0() {
  local label="$1" output="$2" exit_code="$3"
  if [ "$exit_code" -eq 0 ]; then
    ok "$label"
  else
    fail "$label (exit code: $exit_code)"
  fi
}

# ── Setup ────────────────────────────────────────────────────────────────────

backup_state

echo "============================================"
echo "  test-check-resource-alerts.sh"
echo "============================================"
echo ""

# ── Test 1: Script exists ────────────────────────────────────────────────────
echo "[Test] Script exists and is executable"
if [ -f "$SCRIPT" ]; then
  ok "Script file exists"
else
  fail "Script file exists (not found at $SCRIPT)"
fi

if [ -x "$SCRIPT" ]; then
  ok "Script is executable"
else
  fail "Script is executable (not executable)"
fi

# ── Test 2: No state file → NO_ALERTS ────────────────────────────────────────
echo ""
echo "[Test] No state file → NO_ALERTS, exit 0"
rm -f "$STATE_FILE"
OUTPUT=$(bash "$SCRIPT" 2>/dev/null)
EXIT_CODE=$?
assert_contains "Output contains NO_ALERTS" "$OUTPUT" "NO_ALERTS"
assert_exit_0 "Exits 0 when no state file" "$OUTPUT" "$EXIT_CODE"

# ── Test 3: State file with no active alerts → NO_ALERTS ─────────────────────
echo ""
echo "[Test] State file with no active alerts → NO_ALERTS, exit 0"
cat > "$STATE_FILE" << 'NOALERT_EOF'
{
  "timestamp": "2026-01-01T00:00:00Z",
  "alerts": {
    "memory": {"severity": null, "message": "all clear"},
    "cpu": {"severity": null, "message": "all clear"},
    "disk": {"severity": null, "message": "all clear"}
  }
}
NOALERT_EOF
OUTPUT=$(bash "$SCRIPT" 2>/dev/null)
EXIT_CODE=$?
assert_contains "Output contains NO_ALERTS" "$OUTPUT" "NO_ALERTS"
assert_exit_0 "Exits 0 when no active alerts" "$OUTPUT" "$EXIT_CODE"

# ── Test 4: State file with active alerts → diagnostic sections ──────────────
echo ""
echo "[Test] State file with active alerts → diagnostic output"
cat > "$STATE_FILE" << 'ALERT_EOF'
{
  "timestamp": "2026-01-01T00:00:00Z",
  "alerts": {
    "memory": {"severity": "critical", "message": "RAM at 95%"},
    "cpu": {"severity": "warning", "message": "CPU load high"},
    "disk": {"severity": null, "message": "disk ok"}
  }
}
ALERT_EOF
OUTPUT=$(bash "$SCRIPT" 2>/dev/null)
EXIT_CODE=$?

assert_contains "Output contains ACTIVE_ALERTS: header" "$OUTPUT" "ACTIVE_ALERTS:"
assert_contains "Output contains DIAGNOSTICS: section" "$OUTPUT" "DIAGNOSTICS:"
assert_contains "Output contains TOP_MEMORY section" "$OUTPUT" "TOP_MEMORY"
assert_contains "Output contains TOP_CPU section" "$OUTPUT" "TOP_CPU"
assert_contains "Output contains DISK_USAGE section" "$OUTPUT" "DISK_USAGE"
assert_contains "Output contains VMSTAT section" "$OUTPUT" "VMSTAT"
assert_contains "Output contains END_DIAGNOSTICS marker" "$OUTPUT" "END_DIAGNOSTICS"
assert_exit_0 "Exits 0 with active alerts" "$OUTPUT" "$EXIT_CODE"

# Verify ps output appears (not just the header — actual process lines)
if echo "$OUTPUT" | grep -q "TOP_MEMORY" && echo "$OUTPUT" | grep -qE "^(USER|root|c03rad)" ; then
  ok "TOP_MEMORY contains ps output (process lines)"
else
  fail "TOP_MEMORY contains ps output (no process lines found)"
fi

if echo "$OUTPUT" | grep -q "TOP_CPU" && echo "$OUTPUT" | grep -qE "^(USER|root|c03rad)" ; then
  ok "TOP_CPU contains ps output (process lines)"
else
  fail "TOP_CPU contains ps output (no process lines found)"
fi

# ── Test 5: State file with empty alerts dict → NO_ALERTS ────────────────────
echo ""
echo "[Test] State file with empty alerts dict → NO_ALERTS, exit 0"
cat > "$STATE_FILE" << 'EMPTY_EOF'
{
  "timestamp": "2026-01-01T00:00:00Z",
  "alerts": {}
}
EMPTY_EOF
OUTPUT=$(bash "$SCRIPT" 2>/dev/null)
EXIT_CODE=$?
assert_contains "Output contains NO_ALERTS" "$OUTPUT" "NO_ALERTS"
assert_exit_0 "Exits 0 with empty alerts" "$OUTPUT" "$EXIT_CODE"

# ── Test 6: Malformed state file → graceful NO_ALERTS ────────────────────────
echo ""
echo "[Test] Malformed state file → NO_ALERTS (graceful), exit 0"
echo "this is not json" > "$STATE_FILE"
OUTPUT=$(bash "$SCRIPT" 2>/dev/null)
EXIT_CODE=$?
assert_contains "Output contains NO_ALERTS" "$OUTPUT" "NO_ALERTS"
assert_exit_0 "Exits 0 with malformed state" "$OUTPUT" "$EXIT_CODE"

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "============================================"
echo "  RESULTS: $PASS passed, $FAIL failed ($TESTS_RUN total)"
echo "============================================"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0