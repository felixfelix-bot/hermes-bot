# Plan: Fix Context Length Governor Registry

**Date:** 2026-08-26
**Status:** DONE
**Root Cause:** `model_context_registry.json` has wrong context lengths → governor overwrites correct config with wrong values every 30 min

## Checklist

### Phase 1 — Fix the registry
- [x] Update `model_context_registry.json` with correct context lengths
- [x] Verify JSON is valid

### Phase 2 — Find the governor's invoker
- [x] Search Hermes internal cron (jobs.json) for governor invocation
- [x] Search all scripts for chained calls to the governors
- [x] Document the invoker

### Phase 3 — Verify self-healing works correctly
- [x] Run governor manually with fixed registry → confirm it keeps 1048576
- [x] Wait one cron cycle → confirm no config revert
- [x] Confirm compression threshold = 734,003

## Rollback
Revert `model_context_registry.json` to previous values. One-line change.