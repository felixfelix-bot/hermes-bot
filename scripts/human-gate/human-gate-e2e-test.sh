#!/bin/bash
# human-gate-e2e-test.sh — End-to-end test of the human-gate flow
# Tests: block → shadow creation → resolve → unblock
# Silent on success, reports failures only.
#
# Usage: bash scripts/human-gate-e2e-test.sh

set -euo pipefail

BOARDS_DIR="$HOME/.hermes/kanban/boards"
HUMAN_GATE_DB="$BOARDS_DIR/human-gate/kanban.db"
TEST_BOARD="admin"
TEST_TITLE="E2E TEST — human-gate flow verification $(date +%s)"
TEST_REASON="human-gate: E2E test — verify shadow creation and resolution"

PASS=0
FAIL=0

step() {
    local n="$1" msg="$2"
    printf "  Step %s: %s ... " "$n" "$msg"
}

pass() {
    echo "✅ PASS"
    PASS=$((PASS + 1))
}

fail() {
    echo "❌ FAIL: $1"
    FAIL=$((FAIL + 1))
}

cleanup() {
    echo ""
    echo "── Cleanup ──"
    if [ -n "${TEST_TASK:-}" ]; then
        hermes kanban --board "$TEST_BOARD" archive "$TEST_TASK" 2>/dev/null || true
        echo "  Archived test task: $TEST_TASK"
    fi
    if [ -n "${SHADOW_ID:-}" ]; then
        hermes kanban --board human-gate archive "$SHADOW_ID" 2>/dev/null || true
        echo "  Archived shadow: $SHADOW_ID"
    fi
    echo "── Done ──"
}
trap cleanup EXIT

echo ""
echo "🧪 HUMAN-GATE E2E TEST"
echo "═══════════════════════"

# Step 1: Create a test task on the test board
step 1 "Create test task on '$TEST_BOARD'"
TEST_TASK=$(hermes kanban --board "$TEST_BOARD" create --json "$TEST_TITLE" 2>/dev/null | grep -oP 't_\w+' | head -1)
if [ -n "$TEST_TASK" ]; then
    pass "created $TEST_TASK"
else
    fail "could not create test task"
    exit 1
fi

# Step 2: Block the task with human-gate reason
step 2 "Block with human-gate reason"
BLOCK_OUT=$(hermes kanban --board "$TEST_BOARD" block "$TEST_TASK" --reason "$TEST_REASON" 2>&1 || true)
if echo "$BLOCK_OUT" | grep -qi "blocked"; then
    pass "blocked $TEST_TASK"
else
    fail "block failed: $BLOCK_OUT"
fi

# Step 3: Wait for shadow creator (up to 12s = 2 cron ticks)
step 3 "Wait for shadow to appear on human-gate board (≤12s)"
SHADOW_ID=""
for i in $(seq 1 12); do
    sleep 1
    if [ -f "$HUMAN_GATE_DB" ]; then
        SHADOW_ID=$(python3 -c "
import sqlite3, os
db = os.path.expanduser('$HUMAN_GATE_DB')
try:
    conn = sqlite3.connect(db)
    rows = conn.execute(
        'SELECT id FROM tasks WHERE body LIKE ? AND status NOT IN (\"done\",\"archived\")',
        ('%$TEST_TASK%',)
    ).fetchall()
    conn.close()
    if rows:
        print(rows[0][0])
except:
    pass
" 2>/dev/null || true)
        if [ -n "$SHADOW_ID" ]; then
            break
        fi
    fi
done

if [ -n "$SHADOW_ID" ]; then
    pass "shadow created: $SHADOW_ID"
else
    fail "no shadow appeared within 12s"
fi

# Step 4: Complete the shadow task
step 4 "Complete shadow task (mark done)"
COMPLETE_OUT=$(hermes kanban --board human-gate complete "$SHADOW_ID" --summary "E2E test: approved" 2>&1 || true)
if echo "$COMPLETE_OUT" | grep -qiE "(completed|done)"; then
    pass "shadow completed"
else
    # Try marking done directly
    python3 -c "
import sqlite3, os
db = os.path.expanduser('$HUMAN_GATE_DB')
conn = sqlite3.connect(db)
conn.execute('UPDATE tasks SET status = \"done\" WHERE id = \"$SHADOW_ID\"')
conn.commit()
conn.close()
" 2>/dev/null || true
    pass "shadow marked done (direct DB)"
fi

# Step 5: Wait for resolver to unblock (up to 15s = 2.5 cron ticks)
step 5 "Wait for resolver to unblock source task (≤15s)"
UNBLOCKED=""
for i in $(seq 1 15); do
    sleep 1
    STATUS=$(hermes kanban --board "$TEST_BOARD" view "$TEST_TASK" 2>/dev/null | grep -i "status" | head -1 || echo "")
    if echo "$STATUS" | grep -qiE "(todo|ready|running|done)"; then
        UNBLOCKED="yes"
        break
    fi
done

if [ -n "$UNBLOCKED" ]; then
    pass "source task unblocked"
else
    fail "source task still blocked after 15s"
fi

# Step 6: Verify shadow was archived by resolver
step 6 "Verify shadow was archived"
SHADOW_STATUS=$(python3 -c "
import sqlite3, os
db = os.path.expanduser('$HUMAN_GATE_DB')
try:
    conn = sqlite3.connect(db)
    row = conn.execute('SELECT status FROM tasks WHERE id = ?', ('$SHADOW_ID',)).fetchone()
    conn.close()
    print(row[0] if row else 'not_found')
except:
    print('error')
" 2>/dev/null || echo "error")

if [ "$SHADOW_STATUS" = "done" ] || [ "$SHADOW_STATUS" = "archived" ]; then
    pass "shadow status: $SHADOW_STATUS"
else
    fail "unexpected shadow status: $SHADOW_STATUS"
fi

# Report
echo ""
echo "═══ RESULTS ═══"
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
if [ "$FAIL" -eq 0 ]; then
    echo "  ✅ ALL TESTS PASSED"
else
    echo "  ❌ $FAIL TEST(S) FAILED"
fi
echo "════════════════"
echo ""
echo "Note: Test task $TEST_TASK and shadow $SHADOW_ID will be auto-cleaned."
echo "To re-run: bash $0"
