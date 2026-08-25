# Plan: Switch Signal Groups to GLM-5.3 + Restart Gateway

**Date:** 2026-08-26
**Status:** DONE

## Checklist

### Phase 1 — Persistent glm-5.3 default
- [x] Set `model.default: glm-5.3` in manager profile config
- [x] Verify config loads correctly at Python level

### Phase 2 — Gateway restart
- [x] Restart hermes-gateway.service
- [x] Verify service is active
- [x] Verify clean startup (no errors in journal)

### Phase 3 — Post-restart verification
- [x] Confirm compression threshold is ~734K (not 140K)
- [x] Smoke test: Signal message round-trip

## Caveat
Until z.ai weekly quota resets (~Aug 28), glm-5.3 requests mostly execute as glm-5.2 via ollama_cloud's silent model translation. Native glm-5.3 returns when z.ai keys recover or opencode_go key is restored.

## Rollback
Set `model.default: glm-5.2` + restart. Sessions persist in state.db — no data loss.