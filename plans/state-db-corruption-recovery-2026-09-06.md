# Manager state.db corruption — recovery plan + checklist

Incident: 2026-09-06 evening. Manager profile session store corrupted →
hermes stopped responding in Signal groups.

## Incident summary (all times IST, from gateway journal)
- 20:04 — resource-remediation-consultant cron produced degenerate
  word-salad output (glm-5.2 quality event; PAE-4 also runs
  `model_override: glm-5.2`, crashed 3×, D3 crash-breaker blocked it 21:50)
- 20:27:04 — manager state.db write hit an FTS-corruption error
  ("database disk image is malformed"); hermes attempted a one-shot
  in-place FTS rebuild ("canonical message rows are preserved")
- 20:34:02 — cron job ae6633ce89d1 hit "disk I/O error";
  20:34:26 — "FTS indexes rebuilt in place (2)", then "FTS indexes remain
  corrupt … disabled FTS sync, search temporarily uses LIKE"
- After — schema degraded past FTS: `sqlite_master` now references
  `idx_messages_session` with **no `messages` table** ("malformed database
  schema"); every READ and WRITE on the manager state.db fails:
  "Session DB creation failed (will retry next turn): database disk image
  is malformed" (~every 4s)
- Effect — the gateway aborts any turn it cannot persist ("session
  storage could not be written (the transcript would have been lost on
  restart)") → **no replies in Signal groups** (incl. admin group
  `group:V8tn…`, whose 103-msg transcript survives only in gateway RAM,
  disk=0)
- Unaffected — worker-profile state DBs (own files, healthy), the proxy,
  root `~/.hermes/state.db` (quick_check ok), hardware (0 kernel I/O
  errors; `/` at 91%, 22G free)

## Root causes / contributing factors
1. **Trigger**: interrupted/repeat in-place FTS rebuild on a logical
   corruption (exact first cause unknown — file board task) that
   escalated to schema loss
2. **Fragility**: manager state.db ballooned to **6.2 GB** with
   `sessions.auto_prune: false` — big FTS + long write windows
3. **Blast radius**: single-file session store = single point of failure
   for ALL manager surfaces (Signal groups, manager crons)

## Approved decisions (operator, 2026-09-06 ~23:45)
- Recovery strategy: **`.recover` first (history-preserving), fresh-DB
  fallback** if the recovery output is unusable
- Repair-window changes approved: **auto_prune enable + VACUUM**, and
  **24h garbage-demotion of (provider, glm-5.2) pairs** (contain the
  degenerate-output bleed)
- **Aftercare (Phase C) owned by the recovered manager**, not this session

## Known data-loss boundary
The admin-group conversation cache (103 msgs, RAM-only since 20:27) dies
with the gateway restart — unavoidable in every path (the disk copy is
unwritable/lost). `.recover` preserves everything readable on disk
(i.e. history up to ~20:27). The RAM-only delta (≈20:27–restart) is lost.

## Phase A — session storage repair
- [x] A1. `systemctl --user stop hermes-gateway` (drain ≤180s; in-flight
      worker/spawn side effects self-heal via crash-breaker retry)
- [x] A2. Backup: `cp -a state.db* ~/.hermes/bot/backups/state-db-corrupt-<ts>/`
- [x] A3. `sqlite3 <backup>/state.db ".recover" > recovered.sql`;
      assess quality (session/message row shapes, CREATE TABLE set)
- [x] A4. Build `state.db.new` from recovered.sql; `PRAGMA quick_check`
      must be ok; sanity: sessions/messages counts vs pre-incident
      expectations; **fallback to fresh DB if garbage**
- [x] A5. Install recovered DB at `~/.hermes/profiles/manager/state.db`;
      `VACUUM`; record size delta (6.2 GB → ?)
- [x] A6. Manager config: `sessions.auto_prune: true` (retention 90d +
      vacuum_after_prune already set — only the flag is off)
- [x] A7. `systemctl --user start hermes-gateway`; journal clean of
      "malformed" for ≥2 min
- [x] A8. Voice test: `HERMES_PROFILE=manager hermes -z "Reply with
      exactly: storage-ok"` → `storage-ok`; then operator sends a Signal
      test message to the admin group

## Phase B — glm-5.2 containment (during the same gateway window)
- [x] B1. Verify the router's garbage-demotion mechanism storage
      (`_garbage_cb_pair_demoted` → table/state) and write through it
- [x] B2. Demote 24h: (neuralwatt, glm-5.2), (openrouter/z-ai/glm-5.2),
      (opencode_go, glm-5.2). PAE-4 `t_d7653106` stays D3-blocked — no
      action (worker traffic continues on deepseek/kimi lanes)

## Phase C — aftercare (OWNED BY RECOVERED MANAGER — see backup-dir note)
- [ ] C1. Re-pin `pae-savings-watch` cron (currently refuses: unpinned +
      default model drifted glm-5.3 → deepseek — expected safety skip)
- [ ] C2. Re-enable the `.key_disabled_*` free lanes (ours/friend/
      ollama_cloud*/opencode_go, placed 07:22) after lane health check —
      NW per-token bleed ($11.33 daily cap exceeded) while they stay off;
      ours' weekly pacing window + friend key task t_69da44d9 apply
- [ ] C3. Board task: FTS-corruption root cause (write-path audit:
      in-place FTS rebuild, 6.2GB store, WAL/checkpoint hygiene) —
      prevent recurrence; consider periodic state.db backups
- [ ] C4. Board task: nostr reconnect bug — `NostrAdapter.connect() got
      an unexpected keyword argument 'is_reconnect'` retried every 300s
      (nostr-group messaging degraded since at least 14:39)
- [ ] C5. Worker `max_concurrent_sessions` audit (manager was mid-fix:
      worker-pae → 8 landed; many other profiles still at 1)
- [ ] C6. Signal-group acceptance test with the operator (final gate)

## Execution log (2026-09-07 00:10-01:15 IST)
- `.recover` yielded 3.9M `lost_and_found` rows; schema was unrecoverable
  (all CREATE TABLE blobs in the dump were message content, not schema)
- Rebuild strategy: canonical schema from healthy root state.db + salvage
  root-page-5 message rows (nField 18 AND 23 — 5 trailing extras discarded)
  → 339,128 messages + 9,934 synthesized sessions (sessions table itself
  was unrecoverable; source='recovered'); FTS rebuilt via insert triggers
- MID-REPAIR INCIDENT: first assembly insert hit "database or disk is full"
  (disk was 91% before recovery; artifacts added ~16 GB). Tier 0-2 cleanup
  (recovery intermediates, caches, docker prune, .platformio-b 8.6 GB,
  zstd of corrupt backup 6.2→2.3 GB) → +20 GB; reassembly then clean
- A8: `hermes -z` → "storage-ok"; message count delta (+26) proves
  persistence; journal clean since gateway start 01:11:39 (PID 1998215)
- B2 amendment: garbage-CB is in-memory-only (no sanctioned external seed
  path), so the pair demotion is a registry edit instead: glm-5.2 removed
  from neuralwatt + openrouter PROVIDER_MODELS (evidence: degenerate
  completions 23:36-23:38; PAE-4 stayed D3-blocked). opencode_go kept
  (11 clean calls, flat-rate lane). Reversible; canary gate for re-add.
- Handoff note with aftercare: backups/state-db-corrupt-20260907T0011/
  RECOVERY-NOTE.md
