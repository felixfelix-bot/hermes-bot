# Plan: Route Everything Through Flat Router + Fix Backoff + Comprehensive Key-State Alerts

**Date:** 2026-08-25
**Status:** READY FOR IMPLEMENTATION

## Problem Summary

Four issues need fixing:

1. **Early-exit paths bypass the flat router**: Three early-exit blocks (Step 1c, 1c-2, 1c-3) intercept specific model names BEFORE the flat router runs (lines 5080-5122). These bypasses were added "for backward compatibility" but they bypass the flat router's cost-aware candidate selection. The user wants ALL traffic to go through the flat router.

2. **Backoff escalation bug**: `flat_router_dispatch_fail` uses the exponential ramp designed for quota exhaustion (2→4→8→16→32→60→300→900s). A single transient `BrokenPipeError` cascades to 900s backoff in ~5 minutes. Ollama Cloud had 0% quota usage the entire time — the key was never rate-limited or paywalled, just stuck in backoff.

3. **Ollama Cloud doesn't serve deepseek in the flat router**: `PROVIDER_MODELS["ollama_cloud"]` doesn't include deepseek models. Even if deepseek traffic went through the flat router, ollama_cloud wouldn't be a candidate. But ollama CAN serve deepseek (`deepseek-v4-flash:0731` confirmed working, HTTP 200 in 0.8s). Meanwhile neuralwatt ($1.83/M) absorbed all deepseek traffic — $46.71 burned today vs $0.30 on ollama_cloud.

4. **No key-state transition alerts**: No detection of available→unavailable transitions. The user wants a comprehensive overview surfaced when a key stays unavailable for 15+ minutes, showing burn, pressure on each key, and what's absorbing traffic.

## Changes

### H1: Remove all early-exit bypasses — everything goes through the flat router

Delete or neutralize the three early-exit blocks BEFORE the flat router entry point (line ~5147):

1. **Step 1c (Ollama-only models, line 5080-5097)**: `_OLLAMA_ONLY_MODELS = {"kimi-k2.7-code", "kimi-k3:cloud", "gpt-oss:120b", "gemma4:31b", "qwen3.5:397b"}` — bypasses flat router, goes directly to `_try_ollama_cloud_any`. The flat router already handles these via `PROVIDER_MODELS["ollama_cloud"]` which includes all these models. REMOVE this block.

2. **Step 1c-2 (Telnyx-direct models, line 5099-5107)**: `_TELNYX_DIRECT_MODELS = {"kimi-k3"}` — bypasses flat router, goes directly to `_try_telnyx`. The flat router already handles this via `PROVIDER_MODELS` — kimi-k3 is served by opencode_go, neuralwatt, ppq, openrouter, telnyx, routstr, routstrd. REMOVE this block.

3. **Step 1c-3 (Non-z.ai models, line 5109-5122)**: `original_model.startswith(("deepseek/", "qwen", "minimax", "mimo"))` — bypasses flat router, goes to `_try_opencode_go` → `_try_external_failover`. Never tries ollama_cloud. REMOVE this block. (Keep the `messages` presence guard from the 422 fix, but move it to the top of `_proxy` before any routing.)

After this change, ALL requests reach the flat router at line ~5147, which:
- Calls `select_provider(model)` → ordered candidate list (cheapest first)
- Iterates candidates via `_dispatch_to_provider()`
- Tries each provider, marks failures with the new `dispatch_fail` type
- Falls through to 503 if all candidates fail

Also keep the pressure FSM enforce hook (line 5077) and global spend cap (line 5055) — these are circuit breakers, not routing bypasses.

### H2: Fix backoff for dispatch failures

In `_mark_key_failure` (line ~996), add a new error type `"dispatch_fail"` that uses `_SERVER_ERROR_BACKOFF_SECONDS` (30s flat) instead of the exponential ramp.

Change the flat router call site (line ~5288) to use `"dispatch_fail"` instead of `"flat_router_dispatch_fail"`.

Also apply the death-spiral fix from the previous session (already done — only increment failure count when `_is_key_healthy()` returns True).

Backoff comparison:
- Before: 2→4→8→16→32→60→300→900s (exponential, reaches 900s in ~5 min)
- After: 30s flat (transient network errors recover quickly)

### H3: Add deepseek models to Ollama Cloud in the flat router

#### H3a: Add deepseek to PROVIDER_MODELS
In `flat_router.py` `PROVIDER_MODELS`, add `deepseek/deepseek-v4-flash` and `deepseek/deepseek-v4-pro` to `ollama_cloud` and `ollama_cloud_2` sets.

#### H3b: Add model name translation
In `zai_proxy.py` `_PROVIDER_MODEL_NAMES`, add:
```python
"ollama_cloud": {
    "deepseek/deepseek-v4-flash": "deepseek-v4-flash:0731",
    "deepseek/deepseek-v4-pro":   "deepseek-v4-pro:0813",
},
"ollama_cloud_2": {
    "deepseek/deepseek-v4-flash": "deepseek-v4-flash:0731",
    "deepseek/deepseek-v4-pro":   "deepseek-v4-pro:0813",
},
```

#### H3c: Update _try_ollama_cloud to use model translation
In `_try_ollama_cloud` (line ~4200), add model name translation using `_PROVIDER_MODEL_NAMES` so deepseek models are translated to ollama's format (same pattern as opencode_go at line ~4468).

### H4: Comprehensive key-state transition alerts with full situation overview

#### H4a: Track health transitions in `_mark_key_failure`
Add module-level dicts:
```python
_key_down_since: dict[str, float] = {}     # name → timestamp when first went down
_key_alerted: dict[str, bool] = {}         # name → True if 15-min alert already fired
```

In `_mark_key_failure` (line ~998), capture `prev_healthy = prev.get("healthy", True)` before overwriting. If `prev_healthy` was True and the new state is False:
- Record `_key_down_since[name] = now` if not already set
- Clear `_key_alerted[name]` (new down period)

#### H4b: Track recovery in `_mark_key_healthy`
In `_mark_key_healthy`, if the key was previously down:
- Log recovery anomaly: `_log_anomaly("INFO", "KEY_RECOVERED", ...)`
- Remove from `_key_down_since` and `_key_alerted`

#### H4c: Add 15-min sustained-unavailability check to `_refresh_loop`
After existing work in `_refresh_loop` (line ~3850), for each key in `_key_down_since` where `now - down_since >= 900` (15 min) and not yet alerted:

Fire a `CRITICAL` anomaly with a **comprehensive overview**:

```
KEY SUSTAINED DOWN ALERT: "ollama_cloud" unavailable 18min (since 17:35:49)

═══ TODAY'S BURN ═══
  neuralwatt:    $46.72  (2580 calls, 147.8M tokens)  ← absorbing all traffic
  ollama_cloud:   $0.30  ( 569 calls,  19.1M tokens)
  opencode_go:   $10.01  ( 258 calls,  23.3M tokens)
  ours:           $0.30  (3910 calls,  13.6M tokens, quota locked)
  Total today:   $57.32

═══ KEY STATES ═══
  ours:           UNHEALTHY — quota locked (weekly 100%, resets in 3d)
  friend:         MISSING — key not configured
  ollama_cloud:   UNHEALTHY — dispatch_fail, 30s backoff, 1843 failures
  ollama_cloud_2: UNHEALTHY — dispatch_fail, 30s backoff, 1843 failures
  opencode_go:   UNHEALTHY — dispatch_fail, 900s backoff, 2846 failures
  neuralwatt:     HEALTHY — balance provider, $1.83/M effective
  deepinfra:      HEALTHY — per_token, $1.30/M
  telnyx:         HEALTHY — per_token, $5.40/M (Kimi-only)
  ppq:            MISSING — key disabled (balance $0)
  openrouter:    MISSING — key disabled (negative balance)
  routstr:        UNHEALTHY — wallet exhausted (Cashu)
  routstrd:      UNHEALTHY — wallet exhausted (Cashu)

═══ EFFECTIVE ROUTING ═══
  Candidate chain for glm-5.2: ours → friend → ollama_cloud → ollama_cloud_2 → opencode_go → ppq → routstr → routstrd → deepinfra → openrouter → neuralwatt
  First healthy candidate: neuralwatt ($1.83/M)
  All cheap providers ($0.001/M) are DOWN — neuralwatt absorbing 100% of traffic

═══ LAST 1H BURN ═══
  neuralwatt:  444 calls, 30.5M tokens, avg 4927ms, max 53053ms
  opencode_go:  15 calls,  140K tokens, avg 5466ms
  ours:        374 calls, 504K tokens, avg 1836ms (z.ai quota wasting)

═══ ACTION NEEDED ═══
  1. ollama_cloud has 0% session quota — it should NOT be down. Check for transient errors.
  2. z.ai "ours" weekly quota locked — no action until reset (3 days).
  3. neuralwatt burning $46/day at $1.83/M — consider routing through ollama_cloud when recovered.
```

Implementation: build the overview from:
- `_snapshot_health()` for each key's healthy/unhealthy status
- `_snapshot_quota()` for quota windows, usage percentages, regime
- `_zai_key_health` for failure counts, backoff seconds, error types
- `daily_spend` table for today's burn per provider
- `api_calls` table for last-1h burn rate
- `flat_router.select_provider(model)` for candidate chain
- `PROVIDER_TIER` and `_get_effective_cost()` for pricing

Write via:
1. `_log_anomaly("CRITICAL", "KEY_SUSTAINED_DOWN", title, detail)` — goes into `anomaly_events` table, surfaced by `anomaly-notify.sh` cron
2. `print(overview, flush=True)` — immediate journald visibility
3. `espeak-ng "Alert: key ollama_cloud has been unavailable for 18 minutes"` — voice notification

Only fires ONCE per down-period (tracked via `_key_alerted[name]`). Resets when the key recovers.

## Checklist

- [ ] H1a: Remove Step 1c early-exit (Ollama-only models bypass)
- [ ] H1b: Remove Step 1c-2 early-exit (Telnyx-direct models bypass)
- [ ] H1c: Remove Step 1c-3 early-exit (non-z.ai models bypass), keep messages guard at top of _proxy
- [ ] H2: Add `"dispatch_fail"` error type with 30s flat backoff in `_mark_key_failure`
- [ ] H2b: Update flat router call site to use `"dispatch_fail"`
- [ ] H3a: Add deepseek models to PROVIDER_MODELS for ollama_cloud + ollama_cloud_2
- [ ] H3b: Add model name translation in _PROVIDER_MODEL_NAMES for ollama_cloud + ollama_cloud_2
- [ ] H3c: Update _try_ollama_cloud to use _PROVIDER_MODEL_NAMES for model translation
- [ ] H4a: Track health transitions (available→unavailable) with `_key_down_since` / `_key_alerted`
- [ ] H4b: Track recovery transitions in `_mark_key_healthy`
- [ ] H4c: Build comprehensive overview function `_build_key_state_overview()`
- [ ] H4d: Add 15-min sustained-unavailability check to `_refresh_loop`
- [ ] H5: Compile check zai_proxy.py + flat_router.py
- [ ] H6: Restart zai-proxy
- [ ] H7: Test deepseek request routes to ollama_cloud via flat router (HTTP 200, <2s)
- [ ] H8: Test glm-5.2 routing still works (no regression)
- [ ] H9: Test kimi-k3 model routes through flat router (was Telnyx-direct bypass)
- [ ] H10: Verify alert fires with full overview (check anomaly_events table + journald)
- [ ] H11: espeak-ng notification
