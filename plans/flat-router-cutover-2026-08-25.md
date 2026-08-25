# Plan: Flat-Router Cutover + Per-Model Pressure + Transition Alerts + Repo Hygiene

**Date:** 2026-08-25
**Status:** IMPLEMENTING

## Problem Summary

1. **Early-exit paths bypass the flat router** — three blocks (Step 1c, 1c-2, 1c-3) intercept specific model names before the flat router runs, bypassing cost-aware candidate selection. All traffic must go through the flat router.

2. **Backoff escalation bug** — `flat_router_dispatch_fail` uses the exponential ramp (2→4→8→16→32→60→300→900s). A single transient `BrokenPipeError` cascades to 900s backoff in ~5 min. Dispatch failures are transient — they should use 30s flat backoff.

3. **Ollama Cloud doesn't serve deepseek in the flat router** — `PROVIDER_MODELS["ollama_cloud"]` doesn't include deepseek. Ollama CAN serve deepseek (`deepseek-v4-flash:0731` confirmed working). Meanwhile neuralwatt ($1.83/M) absorbed all deepseek traffic — $46.71 burned today.

4. **`_dispatch_external` tries ALL external providers per candidate** — each external candidate calls `_try_external_failover` which iterates ALL external providers. N candidates × 5 providers = up to 25 API attempts per request. The flat router's ordering is meaningless for external providers.

5. **No key-state transition alerts** — no detection of available→unavailable transitions. The user wants a comprehensive overview surfaced when a key stays unavailable for 15+ min.

6. **Dead keys (401) not marked unhealthy for external providers** — neuralwatt returning 401 was never marked unhealthy; the flat router kept retrying it on every request.

7. **No per-model pressure** — quota pressure is per-provider aggregated, not per-model. User wants: if GLM-5.2 burn is high on opencode_go, route glm-5.2 to ollama_cloud; if kimi-k3 burn is high on ollama, route kimi-k3 to opencode_go.

## Decisions Locked In

- **Q1**: Per-model pressure plumbing built now. OpenCode Go `allowance_remaining_usd` parsed from responses; per-(provider, model) burn share from `api_calls` table drives routing.
- **Q2**: z.ai hard-blocked for deepseek/qwen/minimax/mimo in PROVIDER_MODELS (excluded, not marked dead). Broader capability matrix deferred to ADR.
- **Q3**: 401/403 from ANY provider → `_mark_key_failure(name, "dead")` → 1h backoff.
- **Q4**: Commit current state first, then clean up dead code. Plans + ADRs included in repo.

## Checklist

### Part 0 — Git hygiene
- [ ] P0.1 Commit current zai_proxy.py state; push dr
- [ ] P0.2 Create plans/ in repo; add runtime state files to .gitignore if needed

### Part I — Backoff fix
- [ ] P1.1 Add `"dispatch_fail"` error type (30s flat) in `_mark_key_failure`
- [ ] P1.2 Change flat router failure call site to `"dispatch_fail"`

### Part II — Remove early-exit bypasses
- [ ] P2.1 Remove Step 1c (ollama-only models bypass, lines 5080-5097)
- [ ] P2.2 Remove Step 1c-2 (Telnyx-direct bypass, lines 5099-5107)
- [ ] P2.3 Remove Step 1c-3 (non-z.ai bypass, lines 5109-5122); move messages guard to top of _proxy

### Part III — Capability fixes
- [ ] P3.1 Add deepseek models to PROVIDER_MODELS for ollama_cloud + ollama_cloud_2
- [ ] P3.2 Add ollama model name translations in _PROVIDER_MODEL_NAMES; wire into _try_ollama_cloud
- [ ] P3.3 Remove deepseek from PROVIDER_MODELS for z.ai keys (ours, friend)
- [ ] P3.4 Add _try_external_single method; rewire _dispatch_external to single-provider
- [ ] P3.5 Fix 401/403 handling: _mark_key_failure(name, "dead") + continue (not raise)

### Part IV — Per-model pressure
- [ ] P4.1 Parse allowance_remaining_usd from opencode_go responses
- [ ] P4.2 Per-(provider, model) burn share from api_calls table
- [ ] P4.3 New effective-price formula: floor × (1 + scarcity) + burn_share × scarcity × premium

### Part V — Key-state transition alerts
- [ ] P5.1 _key_down_since / _key_alerted tracking in _mark_key_failure / _mark_key_healthy
- [ ] P5.2 KEY_RECOVERED anomaly on recovery
- [ ] P5.3 Situation-overview builder (burn, quota, chains, actions)
- [ ] P5.4 15-min sustained-down check in _refresh_loop + journald + espeak-ng

### Part VI — Dead-code cleanup + ADRs
- [ ] P6.1 Remove old-path cascade (lines ~5313-5852); keep shared helpers
- [ ] P6.2 Write ADR-0007 through ADR-0013 in docs/adr/
- [ ] P6.3 Commit + push

### Part VII — Verify
- [ ] P7.1 Restart proxy; smoke test glm-5.2, deepseek-v4-flash, kimi-k3, kimi-k2.7-code
- [ ] P7.2 Verify X-Provider header shows ollama_cloud/opencode_go for cheap tiers
- [ ] P7.3 Monitor 10 min: zero failover spam, zero 422s, failure counts stay low
- [ ] P7.4 espeak-ng notification