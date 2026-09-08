# ADR-015: DQ05 Compute-Offload Capacity Probe (Phase 0) — Decision Doc

## Status

Accepted (Phase 0 — capacity probe + decision rule). Phase 1 (router
enablement, `t_bd4d83c1`) consumes this probe's schema and decision rule.

## Date

2026-09-08

## Related

- Design: `deleg_7465bae4` (compute-offload design), `deleg_5423792a` Part 2/3
- Prior art: `kanban-worker-management/references/dq05-remote-dispatch.md`
  (Architecture A — the ONLY correct model; Jul-2026 full-profile-on-DQ05
  FAILED: phantom daemon, rsync DB-revert loops, WAL corruption)
- Thresholds: `scripts/resource-gate.sh` (`MAX_LOAD 3.0`, `MIN_MEM 1500MB`)
- Kanban: `t_224f750a` (Phase 0, this doc), `t_bd4d83c1` (Phase 1)
- Live doc: `docs/P0-9-DQ05-flat-router-deploy-2026-09-08.md`

## Context

CPU-heavy work (firmware builds, test suites, data crunching) routinely OOMs
or stalls the T470 (7GB RAM, 4GB cgroup cap) while DQ05
(`c03rad0r-DQ05proplus`, N95, 10.9GB RAM, 467GB disk, always-on mini PC) sits
idle. A full Hermes profile/daemon on DQ05 was TRIED in Jul 2026 and failed
(phantom daemon, rsync DB-revert loops, WAL corruption) — Hermes dispatch is
single-host by design. The documented correct model (Architecture A) is a
**local worker agent + SSH delegation to DQ05 as a pure build server** (no
daemon, no DB, no rsync on DQ05).

Phase 0's job is a **zero-token capacity probe** answering one question: "is
DQ05 available and underloaded enough to offload CPU-heavy work?" It is a
probe and decision rule — NOT a scheduler, and NOT an install on DQ05.

Live state at authoring (2026-09-08): DQ05 reachable via Netbird
(`100.90.22.201`, LAN `192.168.1.218` timing out); resource-monitor
`:9100/local` returns hostname/cpu.load_avg/memory.available_gb/disk.free_gb
but NO core count. `ssh dq05` (BatchMode) returns loadavg + `nproc` +
MemAvailable + `df`. DQ05 nproc = 4. Current load fluctuates (0.08 idle →
~9.0 busy when P0-9 deploy/backfills run); free RAM ~5.3-5.5GB; disk ~200GB
free.

## Decision

Adopt `scripts/dq05-capacity.sh` as the canonical Phase-0 probe.

### Probe order (fail-soft, no LLM in the loop — pure shell)

1. `curl` LAN `:9100` resource-monitor JSON (3s timeout), host list LAN
   `192.168.1.218` → Netbird `100.90.22.201` (schema-variant tolerant:
   `load_avg[0]`/`load1`, `available_gb`/`avail_mb`, `free_gb`/`free_mb`,
   optional `cores`).
2. Fallback: `timeout 4 ssh -o BatchMode=yes -o ConnectTimeout=3 dq05` —
   one round trip fetching `LOAD/NPROC/MEMAVAIL_MB/DISKFREE_MB`; a bare
   `cat /proc/loadavg` reply is also tolerated (load-only → RAM/disk
   unknown → fail-soft LOCAL).

JSON output keys: `reachable`, `source` (`curl|ssh|none`), `load`,
`load_per_core`, `cores`, `free_ram_mb`, `free_disk_mb`, `local_load1`,
`local_cores`, `local_load_per_core`, `decision`, `reason`.

### Decision rule (ALL must hold → `OFFLOAD-OK`; else `LOCAL` fail-soft)

| # | Condition | Threshold | Rationale |
|---|-----------|-----------|-----------|
| 1 | local load/core | > 2.0 | Only offload when the local machine is genuinely stressed (avoids pointless remote round-trips) |
| 2 | DQ05 load/core | < 1.0 | Remote must have headroom |
| 3 | DQ05 free RAM | > 2048 MB | Remote must hold the task without swap-thrash |
| 4 | caller asserts self-contained | `DQ05_CAP_SELF_CONTAINED=1` | A pure probe cannot know task self-containment; the caller (Phase 1 router / task body) asserts it |

Any failure → `LOCAL` with machine-readable `reason`. Unreachable → `LOCAL`.

### Why zero-token shell

The offload check runs on every candidate heavy task. An LLM decision at
that frequency would burn API quota on a yes/no question and add latency.
Deterministic shell + JSON is ~free and ~instant. "Do NOT ask an LLM
'should we offload'" is a hard rule of this design.

### Why not Architecture B (rejected)

Full profile/daemon/DB on DQ05 was tried Jul 2026 and failed (phantom
daemon, one-directional rsync reverting daemon claims every 30s, WAL
corruption from concurrent rsync writes + daemon reads). Phase 0 installs
NOTHING on DQ05. Phase 1 stays on Architecture A (local worker + SSH).

### Multi-target generalization (Phase 1, `t_bd4d83c1`)

Operator extension (2026-09-08): the probe will be generalized to a
multi-target probe (DQ05 → T470 → VPS2), each emitting the same per-target
schema (`reachable/load/free_ram/free_disk`), picking the first green target,
local fail-soft if none green. This doc's JSON schema is deliberately
target-agnostic (no DQ05-specific key names) so Phase 1 can loop targets
without schema churn. VPS2/T470 are pay-per-use/service hosts — DQ05 stays
the PRIMARY target; VPS targets are opportunistic only (short/bounded tasks,
genuine spare capacity). Per-target priority and escape hatches land in Phase
1's `routing.json`.

### Thresholds note

`resource-gate.sh` uses `MAX_LOAD 3.0` (1.5 × 2 cores) and `MIN_MEM 1500MB`
for LOCAL dispatch gating. This probe's per-core framing (local > 2.0/core,
DQ05 < 1.0/core, DQ05 RAM > 2GB) is consistent but expressed per-core and
per-remote-target, since remote machines have different core counts.

## Consequences

- A heavy task's caller (or the Phase 1 sweep/router) runs
  `dq05-capacity.sh`; green + self-contained → SSH-delegate the build to
  DQ05. Red → stay local (fail-soft — no dispatch decision is ever blocked
  by probe uncertainty).
- Probe is safe to run anywhere: it only reads `/proc/loadavg`/`nproc`
  locally, curls a LAN/Netbird port, and issues one bounded BatchMode ssh.
- No secrets in the probe: hosts are env-configurable, no credentials
  embedded (relies on existing `~/.ssh/config` `dq05` alias / key).
- Phase 1 adds: router consolidation, `worker-dq05` spawn flag flip,
  delegation-body append, offload-sweep cron, board `routing.json` escape
  hatches, canary task.
