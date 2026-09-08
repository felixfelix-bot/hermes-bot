# ADR-016: Canonical offload router + worker-dq05 enablement (Phase 1)

Status: accepted (2026-09-08/09)
Supersedes: divergent routing logic in 3 copies of `kanban_auto_assigner.py`
Related: ADR-015 (DQ05 capacity probe + offload decision rule), design deleg_5423792a

## Context

ADR-015 shipped a zero-token capacity probe (`scripts/dq05-capacity.sh`) but
the routing that SHOULD use it was dead weight. Six verified root causes
(t_bd4d83c1):

1. Routing only evaluated UNASSIGNED tasks — almost all tasks arrive
   pre-assigned, so the tasks that matter were never evaluated.
2. Board-preferred profile won when idle; a worker-dq05 offload verdict was
   fallback-only (board-preferred-over-stress bug).
3. worker-dq05 profile had `kanban.dispatch_in_gateway: false` — tasks sat
   `ready` with `Spawned: 0` forever (Jul-2026 Failure Pattern #7 still live).
4. DQ05 routing code existed in only 1 of 3 divergent assigner copies.
5. No delegation instructions appended to task bodies — even a correctly
   assigned worker would not `ssh dq05` for the heavy step.
6. Zero visibility / metric (addressed in Phase 2, t_97264fde).

## Decision

Ship one canonical module, `scripts/offload_router.py`, that owns ALL routing
decisions (zero LLM tokens, deterministic):

- `classify(title, body)` tags tasks: hardware > test > build > crunch >
  light > medium. Hardware keywords (flash/serial/pio upload/bootsel/uart/
  solder/...) force LOCAL, always.
- `load_board_rule(board)` reads per-board escape hatch
  `~/.hermes/kanban/boards/<board>/routing.json` `{"offload": off|auto|force}`.
  Missing file: excluded boards (balloon, e2e-bench, microfips, llm-routing)
  default `off`, everyone else `auto`.
- `probe.probe_targets()` probes the target pool DQ05 → T470 → VPS2 (30s TTL
  cache, curl :9100 then ssh fallback via dq05-capacity.sh). Only DQ05 has a
  dispatchable profile in Phase 1; T470/VPS2 are opportunistic/report-only
  (operator extension 2026-09-08: pay-per-use/service hosts, only offload when
  genuinely spare + short/bounded).
- `route()` decision: hardware/off-board/not-heavy → LOCAL; heavy + green
  target + local load/core > 2.0 (`auto`) or `force` → OFFLOAD to first green
  dispatchable target; probe error → LOCAL fail-soft (never stall the board).

Consolidation: `scripts/crons/kanban_auto_assigner.py` (canonical) and the
live manager profile copy delegate to offload_router; the other two divergent
copies are retired. `import-schedule-to-kanban.py` gains birth-time routing
+ idempotent delegation-body append. `scripts/offload-sweep.py` (no_agent,
5-min cron) reassigns READY heavy tasks under local stress to worker-dq05 —
it never touches running/claimed tasks.

worker-dq05 profile: `kanban.dispatch_in_gateway: true` so the gateway can
actually spawn it (root cause 3).

## HARD RULES (unchanged from ADR-015)

No rsync of live kanban DBs. No Hermes daemon on DQ05 (Architecture A: local
worker + SSH delegation). No secret in code. Routing is never a blocking
create hook; probe/sweep are zero-token.

## Consequences

- All three assigner copies converge on one decision module; keyword tables
  live in exactly one place.
- CPU-heavy builds/tests/crunch leave the T470 box when it is stressed and a
  green target exists; otherwise they run local (fail-soft).
- Phase 2 (t_97264fde) adds the visibility soak: offload-log.jsonl + weekly
  digest metric + verified escape hatches.
- Phase 3 (t_3fe1a98e, GATED) may add a DB-less remote-executor pilot only if
  Phase-1 SSH delegation proves >=2 weeks at >=99% reliability.
