#!/usr/bin/env bash
# ab_test — measure token burn: OpenCode vs Pi on the same task + model.
# PREREQ: configure Pi's z.ai provider first (pi config / settings.json) so both
# run glm-5.2 via z.ai (isolates HARNESS overhead, not model). Run on a clean network.
# Usage: ab_test.sh <repo> <task-description>
set -euo pipefail
REPO="${1:?usage: ab_test.sh <repo> <task>}"; TASK="${2:?usage: ab_test.sh <repo> <task>}"
MODEL="glm-5.2"; WT_A="$REPO-ab-opencode"; WT_B="$REPO-ab-pi"
git -C "$REPO" worktree add --detach "$WT_A" HEAD 2>/dev/null || true
git -C "$REPO" worktree add --detach "$WT_B" HEAD 2>/dev/null || true
echo "### OpenCode leg"; ( cd "$WT_A" && timeout 180 opencode run "$TASK" ) 2>&1 | tail -5
echo "### Pi leg";        ( cd "$WT_B" && timeout 180 pi --print "$TASK" --provider zai --model "$MODEL" --no-session ) 2>&1 | tail -5
echo "### Token usage:"
echo "  opencode: query opencode.db session tokens for dir $WT_A"
echo "  pi:       re-run pi leg with --mode json and read usage block"
echo "(clean up: git -C \"$REPO\" worktree remove --force $WT_A $WT_B)"
