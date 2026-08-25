# Plan: Fix Context Length Governor Registry

**Date:** 2026-08-26
**Status:** EXECUTING
**Root Cause:** `model_context_registry.json` has wrong context lengths → governor overwrites correct config with wrong values every 30 min

## Checklist

### Phase 1 — Fix the registry
- [ ] Update `model_context_registry.json` with correct context lengths
- [ ] Verify JSON is valid

### Phase 2 — Find the governor's invoker
- [ ] Search Hermes internal cron (jobs.json) for governor invocation
- [ ] Search all scripts for chained calls to the governors
- [ ] Document the invoker

### Phase 3 — Verify self-healing works correctly
- [ ] Run governor manually with fixed registry → confirm it keeps 1048576
- [ ] Wait one cron cycle → confirm no config revert
- [ ] Confirm compression threshold = 734,003

## Rollback
Revert `model_context_registry.json` to previous values. One-line change.