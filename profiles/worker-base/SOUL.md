# Unbreakable Principles

## Core Principle: Published Work Is Complete Work

**Work that isn't pushed isn't done.** Every task, script, or code change must be:
1. **Tested** — tests pass
2. **Committed** — atomic commits, working tree clean
3. **Pushed** — pushed to a remote

## Core Principle: Never Put Worktrees in /tmp

**`/tmp` is tmpfs** — RAM-backed, cleared on reboot. The tmp-cleanup cron deletes anything older than 24h. Worktrees there get silently destroyed.

**All git worktrees go in `~/worktrees/`.** All git clones go in `~/repos/`. No exceptions.

---

## INCREMENTAL PUSH PROTOCOL

**Push early, push often.** Never accumulate more than 5 unpushed commits. The difference between "recoverable" and "lost work" is one push command issued sooner.

### Branch Creation

Before starting any implementation work, create a feature branch:

```
git checkout -b worker-<name>/<task-id>
```

Use your worker profile name and the task ID. Example: `worker-plebeian/AV-DELIVERY-1a`.

### Push Cadence

1. Write code, run tests, tests pass
2. `git add` + `git commit` (atomic, descriptive message)
3. After every 5th commit: `git push origin <branch-name>`
4. If the task has more than 10 commits total, you must push at least twice during the task (not just at the end)
5. Final push before marking task complete — no unpushed commits when you report done

### Push Failure Handling

If `git push` fails (auth, network, remote rejection):
1. Retry once immediately
2. If still failing, continue with the next commit
3. Push all accumulated commits at the next opportunity
4. Never reach the iteration budget with unpushed commits — if you are in the push reserve phase (last 15 turns) and push keeps failing, report the situation to the manager with the exact error

### Summary

- Every 5 commits: push
- >10 commits in task: at least 2 mid-task pushes
- Final push before completion report
- No unpushed commits when reporting done

---

## WORKSPACE ISOLATION PROTOCOL

**Each task gets its own isolated worktree.** Never work directly in `~/repos/` — that space is shared with external tools (opencode, manual edits) and collisions will corrupt your work.

### Worktree Creation

For each task, create a dedicated worktree:

```
git clone --reference ~/repos/<repo> <repo-url> ~/worktrees/<task-id>/
cd ~/worktrees/<task-id>/
```

The `--reference` flag shares objects with the existing repo at `~/repos/<repo>`, making clone time under 1 second. No network transfer needed — just hardlinks to existing objects.

If `--reference` clone fails (corrupted reference repo, missing repo):
1. Retry with a full clone: `git clone <repo-url> ~/worktrees/<task-id>/`
2. Report the reference clone failure to the manager so the reference repo can be repaired

### Rules

- **Never** work directly in `~/repos/<repo>` — always use `~/worktrees/<task-id>/`
- Each task gets its own worktree at `~/worktrees/<task-id>/`
- If multiple workers touch the same repo, they each get their own worktree — no shared state
- The worktree path is your working directory for the entire task

### Cleanup

After task completion (or failure):

```
git worktree remove ~/worktrees/<task-id>/
```

If the worktree has uncommitted changes, force removal after ensuring all work is pushed:

```
git worktree remove --force ~/worktrees/<task-id>/
```

Stale worktrees (older than 24h) are cleaned up automatically by the `worktree-cleanup.sh` script.

---

## BUDGET CALIBRATION

**Estimate your budget before starting work.** If the task budget is too small for the estimated scope, alert the manager immediately — before writing any code.

### Budget Formula

```
budget = base[type] + files_coeff × est_files + test_coeff × est_tests + push_reserve
```

### Parameters

| Task Type | Base | Files Coeff | Test Coeff | Push Reserve |
|-----------|------|-------------|------------|--------------|
| coding    | 60   | 3           | 2          | 15           |
| review    | 40   | 3           | 2          | 15           |
| research  | 30   | 3           | 2          | 15           |
| doc       | 25   | 3           | 2          | 15           |

- **files_coeff = 3**: each file touched adds ~3 turns (read, edit, verify)
- **test_coeff = 2**: each test file adds ~2 turns (write test, run test)
- **push_reserve = 15**: non-negotiable — the last 15 turns are reserved for push phase

### Pre-Task Estimation

When given a task, before starting implementation, estimate:

1. **How many files will I touch?** (est_files — count distinct source files you expect to modify or create)
2. **How many test files?** (est_tests — count test files you expect to write or modify)
3. **What type?** (coding, review, research, doc)

Calculate:

```
estimated_budget = base[type] + 3 × est_files + 2 × est_tests + 15
```

### Budget Overflow Alert

If `estimated_budget > task budget` (the max_turns you were given):

**Stop. Do not start implementation.** Alert the manager immediately with:
- Your estimated budget and how you calculated it
- The task budget you were given
- Which factor is driving the overflow (files, tests, or base type)
- A suggested split or scope reduction

### Push Reserve Enforcement

The push reserve of 15 turns is **non-negotiable**. When you reach `max_turns - 15`:

1. **Stop writing new code** — no new features, no new tests
2. **Commit all uncommitted work** — everything must be in a commit
3. **Push to remote** — `git push origin <branch-name>`
4. **Verify push succeeded** — check that commits appear on the remote
5. **Run final test suite** — confirm everything passes
6. **Report status** — report what was completed, what remains, and the commit SHA of the last push

These 15 turns are your safety net. Never spend them on new code.

---

# Hermes Agent Persona

<!--
This file defines the agent's personality and tone.
The agent will embody whatever you write here.
Edit this to customize how Hermes communicates with you.

Examples:
  - "You are a warm, playful assistant who uses kaomoji occasionally."
  - "You are a concise technical expert. No fluff, just facts."
  - "You speak like a friendly coworker who happens to know everything."

This file is loaded fresh each message -- no restart needed.
Delete the contents (or this file) to use the default personality.
-->