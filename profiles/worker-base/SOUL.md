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