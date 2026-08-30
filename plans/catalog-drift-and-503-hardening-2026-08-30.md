# Plan: Catalog Drift Detection + 503 Hardening

**Date:** 2026-08-30
**Status:** EXECUTING
**Trigger:** 503 "all providers exhausted" incident (glm-5.3) + stale-catalog bugs
  (ollama phantom models corrected 08-29; neuralwatt missing glm-5.3 rung found 08-30)

## Root causes addressed
1. routstrd daemon dead since 17:27, service **disabled** (never enabled → dies on reboot)
2. Forward tunnel :8009 was a manual ssh process → died on reboot
3. ollama_cloud_2 stale-unhealthy since Aug 28 (backoff expired, never recovered → 94%-spare pool out of rotation)
4. flat_router model filter excluded neuralwatt/deepinfra from glm-5.3 (stale PROVIDER_MODELS) → 503 despite funded rungs
5. No early warning when >50% of providers go unhealthy simultaneously

## User decisions
- Scope: **all API endpoints** (generalized drift check)
- Phantom handling: **runtime intersect** (fail-open on stale/missing snapshot)
- New models: **report for curation** (no auto-add)

## Checklist

### Phase A — Incident recovery
- [x] A1: `systemctl --user enable --now routstrd.service`
- [x] A2: Create + enable `routstr-forward-tunnel.service` (hermes:8009 → testserver2:8009)
- [x] A3: Recover ollama_cloud_2 (stale key_health row; success-dispatch reset)
- [x] A4: glm-5.3 end-to-end smoke test (expect 200)
- [x] A5: Watchdog — Signal alert when >50% providers unhealthy

### Phase B — Verified curation fixes (live-probed 2026-08-30)
- [x] B1: neuralwatt +glm-5.3 (PROVIDER_MODELS + _PROVIDER_MODEL_NAMES; $1.45 in / $4.50 out)
- [x] B2: deepinfra +glm-5.3 → zai-org/GLM-5.3 (vendor-prefixed ID, verified in 190-model catalog)
- [x] B3: z.ai glm-4.5-flash — 5-token completion probe → phantom or served-unlisted? (report only)
- [x] B4: Run test_flat_router.py after edits

### Phase C — catalog_drift_check.py (all API endpoints)
- [x] C1: Probes — ollama both keys (/v1/models + /api/tags), neuralwatt, deepinfra,
      opencode_go, z.ai, telnyx, routstr/routstrd daemons; ppq/openrouter as probe-failed
      (dead keys); best-effort library-page awareness parse
- [x] C2: Canonicalization — reverse-map upstream IDs (zai-org/GLM-5.3 → glm-5.3,
      deepseek-v4-flash:0731 → deepseek/deepseek-v4-flash) + canonicalize_model validation
- [x] C3: Evidence snapshots → ~/.hermes/bot/evidence/catalog-drift/YYYY-MM-DD/ (FR-0 format)
- [x] C4: Drift report — phantoms · missing rungs (w/ upstream pricing best-effort) ·
      translation gaps · context-registry gaps → Signal only on NEW drift (signature dedup)
- [x] C5: State file — ~/.hermes/bot/live_catalog_state.json (per-provider canonical set,
      fetched_at, catalog_complete flag, allowlist_extra for served-unlisted like glm-4.6v)
- [ ] C6: Cron — state refresh + drift gate every 6h; full report daily 04:00 UTC

### Phase D — Runtime phantom guard (kills the health-poison cascade)
- [x] D1: flat_router reads live_catalog_state.json (mtime-cached); fresh snapshot +
      catalog_complete → skip candidates not in snapshot; decision-log `phantom_catalog_guard`
- [x] D2: Fail-open — stale/missing snapshot or catalog_complete=false → current behavior
- [x] D3: Unit tests — fresh / stale / missing / incomplete-flag paths

### Phase E — Ship
- [x] E1: Update plan checklist + commit + push hermes-bot
- [x] E2: Add catalog_drift_check.py to kalman_sidecar ansible role (reproducible deploy)
- [x] E3: Signal summary to hermes-admin-setup (via drift report delivery + final summary)

## Notes
- z.ai live catalog is incomplete (glm-4.6v served-but-unlisted per flat_router:120-122) —
  intersect must respect catalog_complete flag
- Known missing rungs found in baseline (report for curation, NOT auto-add):
  z.ai: glm-4.7, glm-5, glm-5-turbo, glm-5.1, glm-5.3-flash ·
  ollama: kimi-k2.6, minimax-m2.7, mistral-large-3, nemotron-3-*, gpt-oss:20b ·
  opencode_go: ~27 more models · neuralwatt: kimi-k2.7-code ($0.95/$4.00), -fast/-flex variants
- ppq/openrouter keys dead (balance collectors failing) — probe-failed until rotated

## Rollback
- A1/A2: `systemctl --user disable routstrd.service` / rm forward-tunnel unit
- B1/B2: two-line reverts in flat_router.py / zai_proxy.py
- C: rm cron entry + rm script (state file inert)
- D: rm live_catalog_state.json → guard fail-opens to current behavior