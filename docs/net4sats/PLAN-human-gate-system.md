# Human-Gate System — Implementation Plan

## Objective
Create a multi-layer system where workers escalate decisions to the operator when they need human review, merge approval, or design sign-off. External collaborators (Endo, Arjen) can see and interact with the queue via Nostr.

## Status: ✅ ACTIVE — All layers operational

---

## Layer 1: Skill — ✅ COMPLETE
**What:** `request-human-action` skill in devops category
**Location:** `~/.hermes/profiles/manager/skills/devops/request-human-action/SKILL.md`
**Protocol:** Workers block their task, create a shadow on the human-gate board, and wait. The resolver cron unblocks them automatically when human completes the shadow.

## Layer 2: Local Kanban Board — ✅ COMPLETE
**Board:** `human-gate`
**Status:** Active with 2 pending shadows:
- `t_4a441c79` — HTTPS rebase review (feat/https-reverse-proxy)
- `t_ed61d83f` — Feed postinst PR #20 review

## Layer 3: MCP Server — ✅ COMPLETE
**Script:** `~/scripts/human-gate-mcp.py` (JSON-RPC over stdin/stdout)
**Methods:** request_review, request_merge, request_approval, list_pending, complete_action, get_digest, get_stats
**Usage by workers:** `echo '{"jsonrpc":"2.0","method":"request_review",...}' | python3 ~/scripts/human-gate-mcp.py`

## Layer 4: Nostr Kanban Sync — ✅ COMPLETE
### Outbound (local → Nostr)
- **Script:** `~/scripts/nostr-kanban-sync.sh`
- **Cron:** `nostr-kanban-sync` (every 15m, no-agent)
- **Board:** kind 30301 (`d=net4sats-human-gate`, pubkey hex)
- **Cards:** kind 30302, s-tag=humanreview

### Inbound (Nostr → local)
- **Script:** `~/scripts/nostr-kanban-inbound-sync.sh`
- **Cron:** `nostr-kanban-inbound` (every 15m, no-agent, JUST CREATED)
- **Fetches:** kind 30302 events filtered by a-tag on the board
- **Excludes:** self-published events (filters by pubkey)
- **Maps:** Nostr s-tag → local Hermes status

## Layer 5: Automation Crons — ✅ ALL COMPLETE

| Cron | Schedule | Purpose |
|---|---|---|
| human-gate-resolver | every 2m | Scans done shadows → unblocks originals |
| human-gate-digest | every 4h | Pending-items digest (silent when empty) |
| nostr-kanban-sync | every 15m | Outbound: local → Nostr kanbanstr cards |
| nostr-kanban-inbound | every 15m | Inbound: Nostr cards → local shadows |

## Makefile targets for nak (Nostr Army Knife)
**Location:** `tollgate-infrastructure-kit/Makefile`
**Targets:**
- `make nak-check` — verify nak is installed
- `make nak-pubkey` — show configured Nostr pubkey
- `make nak-board-publish` — (re)publish kanbanstr board event
- `make nak-board-sync` — outbound sync (local → Nostr)
- `make nak-board-inbound` — inbound sync (Nostr → local)
- `make nak-pending` — query pending items from Nostr relay
- `make nak-events` — show recent events
- `make nak-help` — list all nak targets

## net4sats-mvp Board Status (26 tasks)
### Done (9)
HTTPS clone, both feed index generators, feed upload, dispatcher fix, gateway-restart scripts, Playwright workflow, test coverage plan, feed strategy doc

### Blocked awaiting human review (2) — SHADOWS CREATED ✅
- `t_a22fb919` — HTTPS rebase onto v0.5.0-alpha3 (5 conflicts resolved, NOT pushed)
- `t_86ad5180` — Feed postinst PR #20 (13/13 tests pass)

### Blocked — hardware / decisions needed (4)
- `t_704eb8a8` — Playwright coverage (needs GL-MT6000 connected)
- `t_888a5908` — Bidirectional Nostr sync (inbound cron created, needs testing)
- `t_c85a703a` — Self-host kanbanstr (needs deployment target decision)
- `t_115abfc4` — E2E test of human-gate flow

### Todo (9)
HTTPS: PR review, QR redirect, lab verify, runbook update, cert integration; Feed: conWRT update, test on router; Manual runbook validation; Tollgate v0.5.0-alpha3 E2E

### Ready (1)
HTTPS validation for net4sats.lan

## How to complete a shadow (operator)
```bash
# Option 1: Via MCP
echo '{"jsonrpc":"2.0","method":"complete_action","params":{"shadow_task_id":"t_4a441c79","summary":"approved — push the rebased branch","decision":"approved"},"id":1}' | python3 ~/scripts/human-gate-mcp.py

# Option 2: Via kanban CLI
hermes kanban --board human-gate complete t_4a441c79 --summary "approved: push the rebased branch"
```
The resolver cron will unblock the original task within 2 minutes.
