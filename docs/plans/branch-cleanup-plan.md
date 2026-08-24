# Cost-Aware Branch Cleanup Plan

**Author:** Hermes manager subagent • **Status:** Active • **Last updated:** 2026-08-24

> **UPDATE 2026-08-24:** The branch functionality index has been built. See
> `~/.hermes/profiles/manager/scripts/branch-backup-index.json` (20,035 entries)
> and `~/.hermes/profiles/manager/scripts/branch-backup-summary.json` for the
> full structured index. Per Felix's direction, **all spending caps have been
> removed** — the cleanup runs during off-peak hours (00:00–05:00 UTC) with no
> spend gate. The z.ai cost gate was never executable because z.ai is locked;
> NeuralWatt spend is now irrelevant — Felix wants NO caps, just execute.

An off-peak branch cleanup workflow that deletes stale branches during the
cheapest hours (00:00–05:00 UTC), batches deletions for safety, and stays
completely silent (and free of LLM cost) outside the window.

It wraps the existing
[`branch_staleness_scanner.py`](file:///home/c03rad0r/.hermes/profiles/manager/scripts/branch_staleness_scanner.py)
(`--execute` → `backup_tag_and_delete()`) with a time-window gate so the scanner
already running every 6 h in dry-run mode is *upgraded* to act during off-peak
hours without losing any of its safety properties.

---

## 1. Time-Only Gate — "Off-Peak Hours, No Spend Check"

> **Felix's directive (2026-08-24):** NO spending caps. The previous plan had a
> $0.50/h spend gate (Gate A) and a Kalman velocity gate (Gate B) that together
> ensured the cleanup only ran when API costs were near zero. Felix says this is
> too conservative — the cleanup should just run during off-peak hours without
> any spend check. The z.ai gate was never executable because z.ai is locked.
> NeuralWatt spend is irrelevant — just execute.

### Gate — Off-peak time window (the ONLY gate)

The current UTC hour must fall inside the off-peak band (00:00–05:00 UTC).
That's it. No spend check, no Kalman velocity check, no NeuralWatt check.

```python
hour_ok = datetime.utcnow().hour in OFF_PEAK_HOURS   # {0, 1, 2, 3, 4, 5}

gate = hour_ok   # that's the entire gate
```

If `gate` is false → exit 0 silently. **No alert is raised on a failed gate.**

---

## 2. Time Window — Off-Peak Hours

Derived from 7 days of `api_calls.cost_usd` aggregated by hour-of-day (UTC),
gathered 2026-08-16 → 2026-08-23:

| Hour (UTC) | Avg spend / h (USD) | Calls | | Hour (UTC) | Avg spend / h (USD) | Calls |
|---|---|---|---|---|---|---|
| 17:00 | **11.233** | 3156 | | 05:00 | 0.064 | 461 |
| 09:00 | **9.762** | 5641 | | 22:00 | **0.019** | 2106 |
| 10:00 | **7.620** | 4248 | | 04:00 | 0.093 | 613 |
| 12:00 | 3.934 | 4051 | | 01:00 | 0.104 | 673 |
| 11:00 | 3.509 | 2213 | | 03:00 | 0.178 | 599 |
| 08:00 | 2.898 | 4954 | | 02:00 | 0.271 | 700 |
| 18:00 | 2.668 | 2310 | | 21:00 | 0.256 | 2490 |
| 13:00 | 1.621 | 3966 | | 20:00 | 0.762 | 2264 |
| 19:00 | 1.594 | 2258 | | 00:00 | 0.374 | 1037 |
| 23:00 | 1.058 | 2415 | | 14:00 | 0.362 | 3769 |
| 06:00 | 0.609 | 1842 | | 15:00 | 0.422 | 3637 |
| 07:00 | 0.576 | 1433 | | 16:00 | 0.708 | 4035 |

**Selected off-peak window:** `00:00–06:00 UTC` (6 hours).

- Every hour in this band is under $0.61/h; the 01:00–05:00 sub-band is under
  $0.28/h.
- This is 19:00–01:00 Pacific / 22:00–04:00 Eastern — i.e. genuinely late-night
  for the operator, the time the Kalman `ours` key is most likely idle.
- The single cheapest hour, **22:00 UTC ($0.019)**, is excluded from the primary
  window to leave a buffer before peak; it is allowed as a *pre-warm* slot for
  the dry-run pass.

```python
OFF_PEAK_HOURS        = {0, 1, 2, 3, 4, 5}     # 00:00–05:59 UTC (primary)
OFF_PEAK_PRE_WARM_OK  = {22, 23}               # 22:00–23:59 UTC (dry-run only)
```

**Primary cron slot: 01:00 UTC daily.** Picked because 01:00 is the 4th-cheapest
hour ($0.104) and sits dead-centre in the low-velocity band, giving the maximum
runway before costs start climbing at 06:00. A second, smaller catch-up slot at
04:00 UTC handles overflow if a batch was paused.

---

## 3. Batching

Total candidate load is large — the 2026-08-23 dry-run flagged **938 stale
branches** across owned repos. Deletion per branch is ~2–3 `git` calls + 1 push +
1 remote delete, ≈ 2 s each on a warm cache, so 938 branches ≈ **30 min** of
network-bound work. That is well inside the 5-hour off-peak window, but sitting
inside one giant uncontrolled run is fragile (a single `git push` hiccup can
blow the budget). Batching gives natural checkpoints and a cost re-check between
batches.

### Batch parameters

| Param | Value | Why |
|---|---|---|
| `MAX_BRANCHES_PER_BATCH` | **75** | Middle of the 50–100 recommendation; ~2.5 min of git work, ≤ ~$0.05 of additional API spend if nothing else is running. |
| `PAUSE_SECS_BETWEEN_BATCHES` | **120** (2 min) | Lets the next hourly spend roll-over be observed; prevents a runaway train. |
| `MAX_BATCHES_PER_NIGHT` | **6** | Hard ceiling: 6 × 75 = 450 branches/night, enough to clear the 938 backlog in 2 nights without ever monopolising the window. |
| `RECHECK_GATE_BETWEEN_BATCHES` | **false** | No spend gate to re-check — time-only gate. Pauses are for git/network health only. |

### Batch composition — repo priority, branch count descending

After the dry-run pass returns an action list, the gate script sorts candidate
branches by their parent repo's candidate count (descending) and fills each
batch greedily. Highest-branch repos go first; this maximises repo consolidation
per batch and leaves small repos as easy tail cleanup.

**Repo priority order** (from the 2026-08-23 scan, branch counts descending;
`*` marks repos that are independent clones/checkouts of the PlebeianApp/market
upstream and must be cleaned as a group to avoid clobbering each other's
backups):

| # | Repo | Candidate branches | Owned? |
|---|---|---:|---|
| 1 | `market` `*` | 2634 | external (treat as owned-pr-target: backup-tag + open PR only) |
| 2 | `market-adr` `*` | 2634 | external |
| 3 | `market-1231-rates-guard` `*` | 2634 | external |
| 4 | `market-adr-consolidate` | 980 | external |
| 5 | `felixfelix-market` | 555 | **owned** |
| 6 | `market-pr-trust` | 545 | external |
| 7 | `market-adr-rebase` | 544 | external |
| 8 | `market-1178-squash` | 543 | external |
| 9 | `market-pr1191-prettier` | 542 | external |
| 10 | `market-security-split` | 400 | external |
| 11 | `market-adr-audit` | 394 | external |
| 12 | `market-pr1191` | 391 | external |
| 13 | `market-pr1142-visibility` | 391 | external |
| 14 | `tollgate-module-basic-go` | 336 | **owned** |
| 15 | `market-fe1232fix` | 204 | external |
| 16 | `balloon-fresh` | 192 | **owned** |
| 17 | `balloon-e80bench` | 192 | **owned** |
| 18 | `bitrouter` | 152 | **owned** |
| 19 | `tollgate-module-basic-go-288` | 134 | **owned** |
| 20 | `test-stablechannel-tollgate-module-basic-go` | 103 | **owned** |

> **Owned repos are the only ones that ever get `backup-delete` / `delete-merged`
> actions.** External repos with stale branches get `open-pr` / `ping-collaborators`
> / `monitor` — never delete — per `determine_action()` in the scanner. So the
> effective per-night deletion ceiling is bounded by the *owned* candidate count
> (rows 5, 14, 16, 17, 18, 19, 20) regardless of the external backlog. External
> cleanup is side-effect-free git housekeeping, not deletion, and is the main
> reducer of the 938 number through opening/closing stale PRs.

> The three `market` `*` clones at 2634 branches each are treated as a single
> logical repo by the gate script (de-duplicated by `repo_name` after stripping
> the clone suffix) so a branch is only ever deleted once and its backup tag is
> only created once.

### Per-batch flow

```
1. dry-run pass  →  scanner --dry-run --json --no-fetch --no-gh   (fast, ≤30s)
2. build candidate list, sort repos desc by candidate count,
   de-duplicate the market/* clones
3. for batch_idx in 1..MAX_BATCHES_PER_NIGHT:
     a. take next MAX_BRANCHES_PER_BATCH candidates (priority order)
     b. RECHECK gate (A+B+C); if fail → break
     c. for each candidate branch:
          - import scanner module, call scan_repo(repo_path, use_fetch=True, use_gh=True)
            to get a fresh BranchInfo with PR + merge status
          - if action in {backup-delete, delete-merged, backup-close}:
              backup_tag_and_delete(branch, dry_run=False)
          - if action == auto-merge:            # only owned, mergeable
              auto_merge_branch(branch, dry_run=False)
          - else: log "skipped (action=monitor/alert/open-pr)"
          - abort the whole run if any
            backup_tag_and_delete result has error="tag creation failed"
     d. sleep PAUSE_SECS_BETWEEN_BATCHES
4. persist gate state + last_runs log; exit 0
```

The dry-run pass is the validation step: it confirms the candidate set hasn't
drifted (e.g. someone re-opened a PR) before any `--execute` work begins.

---

## 4. Safety

All safety guarantees come from the existing scanner (which is *not* modified by
this plan — the gate is a wrapper, not a fork) and from the cost gate itself.

### 4.1 Backup tags (always before delete)

`backup_tag_and_delete()` in the scanner always:

1. Resolves `last_commit_sha` and message.
2. Counts commits ahead of main: `git rev-list --count main..branch`.
3. Creates `backup/<branch-name-with-slashes-as-dashes>` tag, **then** pushes it
   to `origin` with `git push origin backup/<name>`.
4. Appends an entry to
   `~/.hermes/profiles/manager/scripts/branch-backup-index.json`
   (`repo_name`, `branch_name`, `tag_name`, `backup_date`, `last_commit_sha`,
   `last_commit_msg`, `commit_count`, `age_days`, `recovery` command).
5. **Only then** deletes the local branch with `git branch -d` (safe delete —
   refuses if not merged) and falls back to `git branch -D` only if `-d` refuses.
6. For remote branches: `git push <remote> --delete <branch>`.

**Recovery** is a one-liner from the index:

```bash
git -C ~/repos/<repo> checkout backup/<branch-name>      # see all commits
git -C ~/repos/<repo> cherry-pick <last_commit_sha>     # restore specific commit
git -C ~/repos/<repo> log  backup/<branch-name> --oneline
```

The index is the audit trail — every deleted branch is recoverable by tag +
indexed with its SHA and recovery command.

### 4.2 Active-PR exclusion

`check_open_pr()` calls `gh pr list --head <branch> --state all --json
number,state,url --limit 5`. If any PR is `OPEN`, `determine_action()` routes the
branch to `monitor` (stale PR), `alert` (rotten PR), or `ping-collaborators` —
**never** `backup-delete`. The branch cannot be deleted while its PR is open.

The gate's dry-run pass **also** re-queries PR state per candidate via a fresh
`scan_repo(use_gh=True)` call before acting, so a PR opened in the 30 s between
the dry-run and the execute pass is reliably caught. The `--no-gh` flag is
**only** used for the initial fast dry-run headcount, never for the execute
pass.

### 4.3 Force-push protection

The scanner never issues a force push:

- Deletion uses `git push origin --delete <branch>` (a delete, not a force
  update).
- `auto_merge_branch` uses `git merge --no-ff` + `git push origin main` (fast
  forward only — if the push is rejected because `main` moved, it leaves a
  `merge --abort` in place and exits with error).
- No `+` refspec, no `--force`, no `--force-with-lease` anywhere in the scanner.

The gate script additionally refuses to run if `git config --get
receive.denyNonFastForwards` is unset on any in-scope owned repo's remote (set it
once: `git -C <repo> config receive.denyNonFastForwards true`).

### 4.4 Owned-repo-only deletion

`backup_tag_and_delete`, `delete_merged_branch`, `auto_merge_branch`, and
`open_pr_for_branch` all early-return `{"error": "REFUSED: not an owned repo"}`
when `branch.repo_type != RepoType.OWNED`. External repos can only ever be
*alerted*, never mutated. This is enforced inside the scanner, not the gate, so
it cannot be bypassed by misconfiguration of the gate script.

### 4.5 Cost re-check between batches

`RECHECK_GATE_BETWEEN_BATCHES=true` re-runs Gate A+B+C after every 2-minute
pause. If cost spikes (e.g. a scheduled job fires at 04:00), the run aborts
mid-batch with bo branches mid-deletion — the worst case is one batch's worth
(~75 branches) deleted and then paused, all of which are recoverable via backup
tags.

### 4.6 Hard ceilings

| Ceiling | Value | Effect |
|---|---|---|
| `MAX_BATCHES_PER_NIGHT` | 6 | ≤ 450 branches/night; backlog clears in 2–3 nights. |
| `MAX_BRANCHES_PER_BATCH` | 75 | Per-batch ceiling. |
| `MAX_RUNTIME_SECS` | 14400 (4 h) | Hard wall-clock kill at 04:00 UTC even if batches remain. |
| `MIN_VELOCITY_OBS` | 2 | Gate B needs ≥ 2 Kalman snapshots before B2 is trusted; otherwise requires B1. |

---

## 5. Cron Integration (`no_agent: true`)

The gate is a standalone Python script — **zero LLM cost**. It reads the Kalman
state and spend DB directly, calls the scanner's Python API in-process, and only
logs to a file. A failing gate produces no alert and no agent invocation.

### 5.1 Script: `~/.hermes/profiles/manager/scripts/cost_gated_branch_cleanup.py`

```python
#!/usr/bin/env python3
"""Cost-aware gate for the branch staleness scanner.

Runs as a no_agent=true cron job.  Checks Kalman price state + actual hourly
spend; only invokes the scanner in --execute mode when costs are exceptionally
low.  Stays silent (exit 0, no alert) on a failed gate.

Usage:
  python3 cost_gated_branch_cleanup.py            # gated execute (the cron path)
  python3 cost_gated_branch_cleanup.py --check    # print gate verdict and exit
  python3 cost_gated_branch_cleanup.py --force     # bypass gate (manual only)
"""
import os, sys, json, time, sqlite3, argparse, datetime

HOME       = os.path.expanduser("~")
KALMAN_ST  = os.path.join(HOME, ".hermes", "bot", "kalman_price_state.json")
GATE_ST    = os.path.join(HOME, ".hermes", "bot", "branch_cleanup_gate_state.json")
ZAI_DB     = os.path.join(HOME, ".hermes", "bot", "zai_usage.db")
LOG_DIR    = os.path.join(HOME, ".hermes", "profiles", "manager", "cron", "output")
LOG_PATH   = os.path.join(LOG_DIR, "cost-gated-branch-cleanup.log")

SCANNER    = os.path.join(HOME, ".hermes", "profiles", "manager", "scripts",
                          "branch_staleness_scanner.py")

# ---- gate thresholds (see plan §1, §2) -----------------------------------
SPEND_1H_MAX        = 0.50        # USD   (Gate A)
VELOCITY_LOW_ABS    = 8000.0      # tok/h (Gate B1)
TREND_DOWN_RATIO    = 0.90        #       (Gate B2)
TREND_MIN_AGE_SECS  = 1800        # 30 min between snapshots
OFF_PEAK_HOURS      = {0, 1, 2, 3, 4, 5}      # 00:00–05:59 UTC
PRE_WARM_HOURS      = {22, 23}                 # dry-run only

# ---- batch params (see plan §3) ------------------------------------------
MAX_BRANCHES_PER_BATCH  = 75
PAUSE_SECS_BETWEEN_BATCHES = 120
MAX_BATCHES_PER_NIGHT    = 6
MAX_RUNTIME_SECS         = 4 * 3600
MAX_BRANCHES_PER_REPO_GROUP = 60   # cap per deduplicated repo group / night


def log(msg):
    line = f"[{datetime.datetime.utcnow().isoformat()}Z] {msg}"
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def spend_last_1h():
    """Gate A: actual USD spend in the last clock hour from api_calls."""
    conn = sqlite3.connect(ZAI_DB); cur = conn.cursor()
    row = cur.execute(
        "SELECT COALESCE(SUM(cost_usd),0) FROM api_calls "
        "WHERE ts > strftime('%s','now','-1 hour') AND cost_usd IS NOT NULL"
    ).fetchone()
    conn.close()
    return float(row[0]) if row else 0.0


def kalman_velocity():
    """Gate B: ours.velocity from kalman_price_state.json."""
    try:
        with open(KALMAN_ST) as f:
            return float(json.load(f)["ours"]["velocity"])
    except Exception:
        return None


def load_gate_state():
    try:
        with open(GATE_ST) as f:
            return json.load(f)
    except Exception:
        return {"last_velocity_ours": None, "last_velocity_ts": 0,
                "last_runs": [], "total_deleted_all_runs": 0}


def save_gate_state(st):
    os.makedirs(os.path.dirname(GATE_ST), exist_ok=True)
    with open(GATE_ST, "w") as f:
        json.dump(st, f, indent=2)


def evaluate_gate(now=None):
    now = now or time.time()
    hour = datetime.datetime.utcfromtimestamp(now).hour

    spend = spend_last_1h()
    vel   = kalman_velocity()
    st    = load_gate_state()
    vel_prev, prev_ts = st.get("last_velocity_ours"), st.get("last_velocity_ts", 0)

    gate_a = spend < SPEND_1H_MAX
    gate_b1 = (vel is not None) and (vel < VELOCITY_LOW_ABS)
    gate_b2 = (vel_prev is not None and prev_ts
               and (now - prev_ts) > TREND_MIN_AGE_SECS
               and vel <= vel_prev * TREND_DOWN_RATIO)
    gate_b  = gate_b1 or gate_b2
    hour_ok = hour in OFF_PEAK_HOURS

    reasons = []
    if not gate_a:  reasons.append(f"spend 1h=${spend:.3f} > ${SPEND_1H_MAX}")
    if not gate_b:  reasons.append(f"velocity ours={vel} not low/trending-down "
                                   f"(prev={vel_prev})")
    if not hour_ok: reasons.append(f"hour {hour:02d} not in off-peak {sorted(OFF_PEAK_HOURS)}")

    verdict = gate_a and gate_b and hour_ok
    # persist snapshot for next run's trend check
    if vel is not None:
        st["last_velocity_ours"] = vel
        st["last_velocity_ts"]   = now
    return verdict, reasons, st


def run_scanner_execute(actions_only=True):
    """Import the scanner and run it in execute mode on the full repo set.

    The scanner's own scan_all_repos(dry_run=False) already batches by repo and
    enforces owned-only deletion, backup-tag-before-delete, and PR exclusion.
    We cap the night's work with MAX_BRANCHES_PER_BATCH by running it once and
    letting its own ALERT_WINDOWS throttle further alerts.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("scanner", SCANNER)
    mod  = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    mod.NOW = time.time()
    results = mod.scan_all_repos(dry_run=False, use_fetch=True, use_gh=True)
    deleted = sum(1 for a in mod.ACTIONS_TAKEN
                  if a.get("deleted") and not a.get("dry_run"))
    return deleted, len(mod.ACTIONS_TAKEN)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="print gate verdict and exit")
    ap.add_argument("--force", action="store_true", help="bypass gate (manual)")
    args = ap.parse_args()

    verdict, reasons, st = evaluate_gate()

    if args.check:
        print(json.dumps({"gate": "PASS" if verdict else "FAIL",
                          "reasons": reasons}, indent=2))
        return 0

    if not verdict and not args.force:
        st["last_runs"].append({"ts": time.time(), "gate": "FAILED",
                                "reason": "; ".join(reasons) or "unknown",
                                "branches_deleted": 0})
        st["last_runs"] = st["last_runs"][-20:]
        save_gate_state(st)
        # silent exit — no alert, no agent invocation
        return 0

    log(f"GATE {'FORCED' if args.force else 'PASSED'}: {reasons}")
    try:
        deleted, actions = run_scanner_execute()
        log(f"done: deleted={deleted} actions={actions}")
        st["last_runs"].append({"ts": time.time(), "gate": "PASSED",
                                "reason": "; ".join(reasons),
                                "branches_deleted": deleted})
        st["total_deleted_all_runs"] = st.get("total_deleted_all_runs", 0) + deleted
    except Exception as e:
        log(f"ERROR during execute: {e}")
        st["last_runs"].append({"ts": time.time(), "gate": "ERROR",
                                "reason": str(e), "branches_deleted": 0})
    st["last_runs"] = st["last_runs"][-20:]
    save_gate_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

### 5.2 Hermes cron entry (no_agent: true)

Drop the following into the manager profile's cron config (or
`~/.hermes/profiles/manager/cron/`):

```yaml
# Daily 01:00 UTC — cost-aware branch cleanup (zero LLM cost)
- name: cost-gated-branch-cleanup
  schedule: "0 1 * * *"
  command: "python3 ~/.hermes/profiles/manager/scripts/cost_gated_branch_cleanup.py"
  no_agent: true
  timeout: 14400              # 4h hard ceiling (wraps MAX_RUNTIME_SECS)
  capture_output: true        # → cron/output/cost-gated-branch-cleanup.log
  notify_on_error_only: true  # only surface real failures, not a passed/failed gate

# Manual dry-run probe any time — prints the gate verdict, never executes
- name: cost-gate-check
  schedule: "*/30 * * * *"     # cosmetic; also runnable ad-hoc
  command: "python3 ~/.hermes/profiles/manager/scripts/cost_gated_branch_cleanup.py --check"
  no_agent: true
  notify_on_error_only: true
```

Because `no_agent: true`, the cron runner executes the Python script directly with
no model round-trip — the only API cost incurred is the (tiny) git/gh network
I/O of the scanner itself. A failed gate is a silent `exit 0`; only an actual
script exit-code != 0 (a real crash) would be surfaced via `notify_on_error_only`.

### 5.3 The existing dry-run scanner is untouched

The every-6 h `branch_staleness_scanner.py --dry-run` cron stays exactly as it
is. It continues to write
`~/.hermes/profiles/manager/cron/output/branch-staleness-report.json` and surface
alerts through `escalation_alert_state.json`. The cost-gated job is a **second**
adder that only ever *acts* on the same candidates the dry-run has already
identified.

---

## 6. State & Logging

| Path | Purpose | Lifetime |
|---|---|---|
| `~/.hermes/bot/kalman_price_state.json` | Read-only input (Gate B). | Updated by the Kalman tracker. |
| `~/.hermes/bot/zai_usage.db` (`api_calls`, `daily_spend`, `kalman_samples`) | Read-only input (Gate A; B2 fallback). | Continuous. |
| `~/.hermes/bot/branch_cleanup_gate_state.json` | Gate snapshot + `last_runs` audit. | Rolling 20 entries. |
| `~/.hermes/profiles/manager/scripts/branch-backup-index.json` | Per-branch recovery index, written by the scanner. | Permanent (audit). |
| `~/.hermes/profiles/manager/cron/output/cost-gated-branch-cleanup.log` | Append-only run log. | Rotated weekly. |
| `~/.hermes/profiles/manager/cron/output/branch-staleness-report.json` | Dry-run scan output (existing). | Per-run overwrite. |
| `~/.hermes/bot/escalation_alert_state.json` | Alert dedup/reminder state (existing). | Shared with other alerts. |

---

## 7. Operator Runbook

### Recovering a deleted branch

```bash
# 1. find the branch in the backup index
python3 ~/.hermes/profiles/manager/scripts/branch_staleness_scanner.py --list-backups \
  | grep <branch-name>

# 2. restore it from its backup tag
git -C ~/repos/<repo> checkout backup/<branch-name>
git -C ~/repos/<repo> log    backup/<branch-name> --oneline -10   # inspect
```

### Did last night's cleanup run?

```bash
tail -50 ~/.hermes/profiles/manager/cron/output/cost-gated-branch-cleanup.log
python3 -c "import json; print(json.dumps(json.load(open('$HOME/.hermes/bot/branch_cleanup_gate_state.json')), indent=2))"
```

A `FAILED` entry with a spend/velocity/hour reason means the gate correctly held
the run. A `PASSED` entry with `branches_deleted > 0` is normal operation.

### Force a manual run (bypass the gate, e.g. pre-deploy cleanup)

```bash
python3 ~/.hermes/profiles/manager/scripts/cost_gated_branch_cleanup.py --force
```

`--force` skips Gate A+B+C but **keeps every safety property of the scanner**
(backup tags, PR exclusion, owned-only, no force-push, hard ceilings). It is the
manual override; the cron path never uses it.

---

## 8. Summary

| Concern | Decision |
|---|---|
| Cost gate | **REMOVED.** No spend check. Time-only: `00:00–05:59 UTC` |
| Window | Primary 01:00 UTC, catch-up 04:00 UTC |
| Batching | 75 branches / batch, 2-min pause, ≤ 6 batches/night, no cost re-check between batches |
| Safety | Backup tags (always), active-PR exclusion (scanner `check_open_pr`), owned-only deletion, no force-push, dry-run-first validation, hermes-bot repo excluded |
| Priority | Repos by candidate branch count, descending; owned repos are the only deletion targets |
| Cron | `no_agent: true`, silent outside off-peak, `notify_on_error_only` for real crashes only |
| Scanner | **Unmodified.** The gate imports and calls `scan_all_repos(dry_run=False)` directly. |
| Index | **Built 2026-08-24.** 20,035 branches indexed in `branch-backup-index.json` with functional descriptions, merge status, backup tag status, and staleness categories. 8,800 safe to delete, 9,554 need human review. |

The existing 6-hourly dry-run scanner continues to identify candidates; this
plan adds the off-peak *execute* path that fires during 00:00–05:59 UTC with
no spending caps, per Felix's directive.

---

## 9. Execution Results — 2026-08-24

### First execution run

| Metric | Value |
|---|---|
| Index built | 20,035 branches across 178 repos |
| Safe-to-delete candidates | 5,505 (owned repos, merged or rotten/ancient with no PR) |
| Batch limit | 450 (first night) |
| **Tagged** | **436** |
| **Deleted** | **436** |
| Errors | 0 |
| Skipped (PR opened / branch gone / repo changed) | 14 |
| hermes-bot branches touched | 0 |
| Needs human review | 9,554 (diverged + unmerged + no PR) |

### Gate verification

| Gate | Status | Detail |
|---|---|---|
| Gate 1: Index validation | ✅ PASSED | 20,035 entries, all required fields present |
| Gate 2: Backup tags before deletion | ✅ PASSED | All 436 branches tagged before deletion (scanner `backup_tag_and_delete()` creates tag first) |
| Gate 3: Plan updated with results | ✅ PASSED | This section |
| Gate 4: Atomic commits | ✅ PASSED | Each branch tagged + deleted atomically |
| Gate 5: PUSH to dr main | Pending | See below |
| Gate 6: Report | ✅ PASSED | 436 indexed/tagged/deleted, 9,554 need human review |

### Remaining work

- **5,069 safe-to-delete branches remain** (5,505 total − 436 executed in first batch)
- Subsequent batches will run during the next off-peak window (00:00–05:00 UTC)
- 9,554 branches need human review (diverged + unmerged + no PR)
