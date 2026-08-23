---
name: ics-communication-protocol
description: "ICS (Incident Command System) adaptation for Hermes multi-agent communication. Defines role hierarchy, message format, escalation rules, and the single-funnel principle for human notifications."
version: 1.0.0
metadata:
  hermes:
    tags: [communication, ics, notification-filtering, multi-agent, signal]
---

# ICS Communication Protocol for Hermes

Adapts the FEMA Incident Command System (ICS) to govern how Hermes agents
communicate with human decision-makers. The core principle: **prevent
information overload by enforcing a strict communication hierarchy with a
single funnel point.**

## Why ICS

ICS was designed for wildfire response where decision-makers cannot afford to
be DDoS'd by raw operational data. The same applies to a multi-agent Hermes
setup with 80+ boards and 50+ crons — without filtering, the human is buried
in noise and misses the signals that matter.

## Role Mapping

| ICS Role | Hermes Equivalent | Signal Access |
|----------|-------------------|---------------|
| **Unified Command** (Incident Commander) | The human(s) — c08r4d0r + collaborators | Human (obviously) |
| **Liaison Officer** | `manager` profile (the ONLY profile that talks to humans proactively) | YES — sole sender |
| **Planning Section** | human-gate-digest cron (aggregates, prioritizes, formats) | Via Liaison only |
| **Operations Section** | Worker profiles (worker-admin, worker-tollgate, worker-plebeian, etc.) | NO — silent |
| **Logistics** | Monitoring crons (disk, zai, kalman, etc.) | NO — local only |

## The Three Rules

### Rule 1: Single Funnel (One Sender)
ONLY the `human-gate-digest` cron (every 2h) delivers messages to Signal.
All other crons are `deliver: local`. Workers write to kanban, never to chat.
The manager profile responds to direct human messages but does not proactively
push notifications — that's the digest's job.

**Exception:** Genuine emergency alerts (disk full, proxy down, dispatch
system broken) bypass the digest via the anomaly-notify pipeline, but still
route through the manager profile.

### Rule 2: ICS Message Format
Every human-bound message follows this format:

```
[STATUS] [PROJECT] task_id — Brief description

Action needed (if any): what the human must decide/do
```

**Status codes:**
- `[BLOCKER]` — Task is blocked, needs human decision
- `[APPROVE]` — Awaiting explicit human sign-off (gate)
- `[CRITICAL]` — System-level emergency (disk, proxy, dispatch)
- `[INFO]` — Informational summary (digest, milestones)
- `[RESOLVED]` — Previously blocked item now resolved

**Project codes:** TOLLGATE, FIPS, NET4SATS, PLEBEIAN, INFRA, BALLOON, SOVEREIGN, HUMAN-GATE

**Example:**
```
[BLOCKER] [INFRA] t_36ca6f0f — DQ05-M2a: Nostr bidirectional sync deploy needs approval

2 blocked tasks on infrastructure board depend on this.
Action: approve deploy or request changes
```

### Rule 3: Management by Exception (Silent When Healthy)
- Tasks moving Triage → Todo → In Progress → Done: SILENT
- Routine cron checks passing: SILENT
- Workers completing tasks: write summary to kanban card, SILENT
- Only escalate on: BLOCKED status, APPROVE gate reached, CRITICAL system event

## Signal Group Topology

### Command & Control (C&C) Group
- **Group:** `hermes-admin-setup` (existing)
- **Members:** Humans + manager profile (Liaison)
- **Content:** ICS-formatted digests, blockers, approval gates ONLY
- **Noise level:** Low (target: < 5 messages/day excluding direct human interaction)

### Per-Project Operations Groups
- **Groups:** `tollgate-ops`, `net4sats-ops`, `plebeian-ops`, `fips-ops`, `infra-ops`
- **Members:** Manager profile (as read-only logger) + relevant worker profiles
- **Content:** Worker logs, build outputs, test results, operational chatter
- **Noise level:** High (but humans mute these and check on-demand)
- **Purpose:** Audit trail. When debugging a specific project, humans can
  scroll through its ops group instead of searching mixed C&C history.

## Cron Delivery Matrix

| Cron | Deliver To | ICS Function |
|------|-----------|--------------|
| human-gate-digest | **origin** (Signal C&C) | Liaison — THE funnel |
| anomaly-notify | local (checked by digest) | Logistics |
| human-gate-shadow-creator | local (writes to DB) | Planning |
| blocked-task-audit | local (writes to kanban) | Planning |
| daily-unpushed-audit | local (writes to kanban) | Planning |
| quality-hooks-sweep | local (writes to kanban) | Planning |
| All monitoring crons | local | Logistics |
| All worker activity | kanban comments | Operations |

## Reaction-Based Gating (Future)

When the Liaison posts a decision request to C&C, humans can respond with
Signal emoji reactions instead of typing:
- 👍 = approve
- 👎 = reject
- 🕐 = defer (ask again in next digest)

The human-gate-resolver reads reactions via signal-cli and acts accordingly.
This keeps the C&C group clean — one message per decision, one reaction to resolve.

## Implementation Details

### human-gate-digest script
Located at `~/scripts/human-gate-digest.sh`. Updated to:
1. Read from human-gate board (blocked tasks requiring decisions)
2. Read from anomaly buffer (system issues)
3. Read from audit results (unpushed code, quality hooks)
4. Format ALL output in ICS message format
5. Deliver as a single consolidated digest every 2 hours

### Blocked task detection
`human-gate-shadow-creator.py` scans all boards every 2 minutes for blocked
tasks. Creates shadow tasks on human-gate board. Runs as `deliver: local` —
it writes to the DB only, the digest reads from the DB.

### Worker silence enforcement
Workers inherit SIGNAL_* env vars from global .env but are dispatched as
subprocesses by the kanban dispatcher. They write results to kanban task
comments and worker logs, never to Signal directly. The dispatcher runs
inside the gateway process but does not push notifications for task
progress — only the digest surfaces blocked items.
