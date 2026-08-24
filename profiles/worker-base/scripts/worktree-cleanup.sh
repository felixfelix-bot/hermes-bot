#!/bin/bash
# worktree-cleanup.sh — remove stale worktrees older than 24h
# Run via cron or manually. Safe to run at any time.
#
# Scans ~/worktrees/ for directories older than 24h and removes them.
# Reports what was cleaned up.

WORKTREE_DIR="${HOME}/worktrees"
MAX_AGE_HOURS=24
removed=0
skipped=0

if [ ! -d "$WORKTREE_DIR" ]; then
    echo "worktree-cleanup: ${WORKTREE_DIR} does not exist, nothing to clean"
    exit 0
fi

# Find directories older than MAX_AGE_HOURS
while IFS= read -r entry; do
    [ -z "$entry" ] && continue

    dir=$(echo "$entry" | awk '{print $1}')
    name=$(basename "$dir")

    # Skip if the directory is actually a registered git worktree with a running process
    # Check if any process has this directory as its cwd
    if fuser "$dir" >/dev/null 2>&1; then
        echo "skip: ${name} (in use by active process)"
        skipped=$((skipped + 1))
        continue
    fi

    # Try git worktree remove first (clean), fall back to rm -rf
    if git worktree remove "$dir" --force 2>/dev/null; then
        echo "removed: ${name} (git worktree remove)"
        removed=$((removed + 1))
    else
        # Not a registered worktree or git command failed — just rm
        rm -rf "$dir" 2>/dev/null
        if [ ! -d "$dir" ]; then
            echo "removed: ${name} (rm -rf)"
            removed=$((removed + 1))
        else
            echo "failed: ${name} (could not remove)"
            skipped=$((skipped + 1))
        fi
    fi
done < <(find "$WORKTREE_DIR" -maxdepth 1 -mindepth 1 -type d -mtime +"$MAX_AGE_HOURS" 2>/dev/null)

echo "worktree-cleanup: ${removed} removed, ${skipped} skipped"
exit 0