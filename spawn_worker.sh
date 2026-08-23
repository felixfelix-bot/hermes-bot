#!/usr/bin/env bash
# spawn_worker — create a new worktree + reconcile a worker profile for it.
# Usage: spawn_worker.sh <repo> <branch> [worktree-path]
# Then run worktree_sync to register the profile (idempotent).
set -euo pipefail
repo="${1:?usage: spawn_worker.sh <repo> <branch> [path]}"
branch="${2:?usage: spawn_worker.sh <repo> <branch> [path]}"
wt="${3:-$HOME/$(basename "$repo")-$(echo "$branch" | tr -c 'a-zA-Z0-9' '-' | sed 's/--*/-/g; s/^-//; s/-$//')}"
git -C "$repo" worktree add -b "$branch" "$wt" 2>/dev/null || git -C "$repo" worktree add "$wt" "$branch"
echo "worktree ready: $wt  [$branch]"
# delegate profile creation to the reconciler
exec "$(dirname "$0")/worktree_sync.py"
