# net4sats MVP — Comprehensive Plan

## Objective
Get a working MVP for net4sats — a Bitcoin Lightning/Cashu payment-enabled WiFi router for GL-MT6000 (and other GL-iNet routers) with smooth onboarding, v0.5.0-alpha3 of tollgate, and a frictionless user setup experience.

## The Two-Tier Auto-Heal System
Following the proven FIPS pattern:

```
┌──────────────────────────────────────────────┐
│  Tier 1: net4sats-auto-heal (no_agent)       │
│  ──────────────────────────────────────      │
│  Schedule: */15 * * * * (every 15 min)       │
│  Script: net4sats_autoheal.py                │
│  Cost: ZERO tokens (script only)             │
│                                              │
│  1. Exponential backoff (15m→24h cap)        │
│  2. Stale resetter (reclaim zombie tasks)     │
│  3. Dispatch ready tasks                      │
│  4. Write ~/.hermes/state/net4sats_needs_ai.json │
│     if analysis needed                       │
│  5. Silent if nothing changed                │
└──────────────┬───────────────────────────────┘
               │ signal file (when needed)
               ▼
┌──────────────────────────────────────────────┐
│  Tier 2: net4sats AI Fallback (LLM agent)     │
│  ──────────────────────────────────────      │
│  Schedule: */30 * * * * (every 30 min)       │
│  Cost: ONLY when signal file present          │
│                                              │
│  1. Read signal file                          │
│  2. Read board state + human-gate shadows    │
│  3. Analyze blockers + recommendations        │
│  4. Report to user what needs attention      │
│  5. Silent if nothing new                     │
└──────────────────────────────────────────────┘
```

## Workstreams & Current Status

### 1. HTTPS/SSL (5 tasks)
**Goal:** TLS reverse proxy on :443, NoDogSplash stays on HTTP :2050, "Scan QR to Pay" redirect
- ✅ Clone Amperstrand fork — DONE
- ✅ Rebase feat/https-reverse-proxy onto v0.5.0-alpha3 — **DONE, awaiting your approval** (shadow: t_4a441c79)
- ◻ Open PR for human review — BLOCKED on approval above
- ◻ Add "Scan QR to Pay" redirect — TODO
- ◻ Verify on lab GL-MT6000 — BLOCKED on router connection
- ◻ Update runbook + conWRT with SSL step — TODO
- ◻ Integrate self-signed certs into configurationwizzard — TODO

### 2. OpenWrt Feed (5 tasks)
**Goal:** tollgate v0.5.0-alpha3 distributed via custom feed, auto-resolved from configurationwizzard
- ✅ Feed strategy doc — DONE
- ✅ opkg index generator — DONE
- ✅ apk index generator — DONE
- ✅ Upload indices to releases.tollgate.me + docs.net4sats.cash — DONE
- ✅ Update configurationwizzard postinst with feed registration — **DONE, awaiting your PR review** (shadow: t_ed61d83f, PR #20)
- ◻ Update conWRT flow — TODO
- ◻ Test on GL-MT6000 — BLOCKED on router connection

### 3. Test Automation (3 tasks)
**Goal:** Playwright recordings covering all runbook edge cases
- ✅ Test coverage audit — DONE
- ⊘ Playwright coverage — BLOCKED (needs GL-MT6000 physically connected)
- ◻ Manual validation on lab GL-MT6000 — TODO
- ◻ Tollgate v0.5.0-alpha3 E2E testing — TODO

### 4. Human-Gate System (4 tasks)
**Goal:** Workers escalate decisions, external collaborators see queue on Nostr
- ✅ Skill: request-human-action — COMPLETE
- ✅ MCP server: ~/scripts/human-gate-mcp.py — COMPLETE
- ✅ Nostr outbound sync — COMPLETE (cron every 15m)
- ✅ Nostr inbound sync — COMPLETE (cron every 15m)
- ✅ Makefile targets for nak — COMPLETE (8 targets)
- ✅ net4sats auto-heal cron — COMPLETE (Tier 1, no_agent, every 15m)
- ✅ net4sats AI Fallback cron — COMPLETE (Tier 2, LLM, every 30m)
- ⊘ E2E test of full flow — BLOCKED (worker crashes)
- ⊘ Self-host kanbanstr — BLOCKED (needs deploy decision)
- ⊘ Bidirectional Nostr sync — BLOCKED (inbound script exists, cron running)

### 5. Gateway Restart (1 task)
- ✅ gateway-restart.sh + cancel + cron — COMPLETE
- ✅ Embedded in Playwright deployment workflows — DONE

## What YOU Can Do Right Now (to accelerate)

### 🔴 HIGH IMPACT — takes 5 minutes each

1. **Review HTTPS rebase** — Complete shadow `t_4a441c79`:
   ```
   hermes kanban --board human-gate complete t_4a441c79 --summary "approved: push the rebased branch"
   ```
   This unblocks: HTTPS PR review + QR redirect + runbook update (4 downstream tasks)

2. **Review Feed PR #20** — Complete shadow `t_ed61d83f`:
   ```
   hermes kanban --board human-gate complete t_ed61d83f --summary "approved: PR #20 merged"
   ```
   This unblocks: Feed conWRT update + test on router (2 downstream tasks)

### 🟡 MEDIUM IMPACT — decisions needed

3. **Connect GL-MT6000 router** to power + LAN. This unblocks: Playwright recording, E2E testing, HTTPS lab verification, manual runbook validation (6 tasks)

4. **Self-host kanbanstr decision** — Deploy to GitHub Pages? Or use the Nostr MCP client directly? Either unblocks the kanbanstr self-hosting task.

## Running Infrastructure (crons)

| Cron | Schedule | Cost |
|---|---|---|
| net4sats-auto-heal | every 15m | Zero tokens (script only) |
| net4sats AI Fallback | every 30m | Tokens only when changes detected |
| human-gate-resolver | every 2m | Zero tokens |
| human-gate-digest | every 4h | Zero tokens |
| nostr-kanban-sync | every 15m | Zero tokens |
| nostr-kanban-inbound | every 15m | Zero tokens |
| kanban-stale-resetter | every 5m | Zero tokens |
| kanban-auto-assigner | every 10m | Zero tokens |

Total recurring token cost: ~48 tokens/day (2 analysis runs, assuming state changes)

## Nostr Board Access for Endo/Arjen
- Board kind: 30301, ID: `net4sats-human-gate`
- Pubkey: `e18a1d171a59d874edd336472afeb3a614d3dc83397dd097e922a99dcee02133`
- Relays: wss://relay.damus.io, wss://nos.lol
- CLI access: `make nak-pending` (from tollgate-infrastructure-kit)
- **Self-hosting kanbanstr is the ONLY viable option** for a GUI. No other Nostr client (Nostrudel, Coracle, Amethyst, Damus) fully renders kind 30301 + 30302 as a kanban board. Nostrudel has partial board support but doesn't show cards.
- Self-hosting steps: clone vivganes/kanbanstr → npm install → npm run build → serve on port 3000. Next.js app, simple to deploy to GitHub Pages or a VPS.
