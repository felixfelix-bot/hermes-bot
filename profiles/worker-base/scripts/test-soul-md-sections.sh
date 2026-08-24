#!/bin/bash
# test-soul-md-sections.sh — validate SOUL.md contains all required sections
# Gate 1 (TDD): Run this test to verify SOUL.md has the sections added by
# the Phase 1 worker track fixes (Q2, Q4, Q5).

SOUL_MD="${1:-$HOME/.hermes/profiles/worker-base/SOUL.md}"
errors=0
passes=0

if [ ! -f "$SOUL_MD" ]; then
    echo "FAIL: SOUL.md not found at $SOUL_MD"
    exit 1
fi

check_section() {
    local section_name="$1"
    if grep -q "## ${section_name}" "$SOUL_MD"; then
        echo "PASS: section '${section_name}' found"
        passes=$((passes + 1))
    else
        echo "FAIL: section '${section_name}' NOT found"
        errors=$((errors + 1))
    fi
}

check_content() {
    local label="$1"
    local pattern="$2"
    if grep -q "$pattern" "$SOUL_MD"; then
        echo "PASS: content '${label}' found"
        passes=$((passes + 1))
    else
        echo "FAIL: content '${label}' NOT found"
        errors=$((errors + 1))
    fi
}

echo "=== Validating SOUL.md at $SOUL_MD ==="
echo ""

# --- Q2: Incremental Push Protocol ---
check_section "INCREMENTAL PUSH PROTOCOL"
check_content "Q2: push every 5 commits" "every 5"
check_content "Q2: feature branch creation" "git checkout -b worker-"
check_content "Q2: push origin branch" "git push origin"
check_content "Q2: >10 commits push twice" "at least twice"
check_content "Q2: final push before complete" "Final push before"

# --- Q4: Workspace Isolation Protocol ---
check_section "WORKSPACE ISOLATION PROTOCOL"
check_content "Q4: worktree path" "~/worktrees/<task-id>"
check_content "Q4: clone with reference" "git clone --reference"
check_content "Q4: never work in repos" "Never.*work directly in"
check_content "Q4: worktree remove cleanup" "git worktree remove"

# --- Q5: Budget Calibration ---
check_section "BUDGET CALIBRATION"
check_content "Q5: budget formula" "base\[type\].*files_coeff.*test_coeff.*push_reserve"
check_content "Q5: coding base 60" "coding.*60"
check_content "Q5: review base 40" "review.*40"
check_content "Q5: research base 30" "research.*30"
check_content "Q5: doc base 25" "doc.*25"
check_content "Q5: files_coeff=3" "files_coeff.*3"
check_content "Q5: test_coeff=2" "test_coeff.*2"
check_content "Q5: push_reserve=15" "push_reserve.*15"
check_content "Q5: estimate before starting" "before starting"
check_content "Q5: alert if over budget" "alert.*manager.*immediately"
check_content "Q5: push phase at max_turns-15" "max_turns - 15"
check_content "Q5: non-negotiable push reserve" "non-negotiable"

echo ""
echo "=== Results: $passes passed, $errors failed ==="

if [ "$errors" -gt 0 ]; then
    exit 1
else
    exit 0
fi