# Analysis: Provider Inventory + Why NeuralWatt Got Traffic Instead of Ollama Cloud

**Date:** 2026-08-25
**Status:** READ-ONLY ANALYSIS (plan mode)

## Provider Inventory: 12 providers

| # | Provider | Tier | Key Present? | Currently Working? | Effective $/M |
|---|----------|------|-------------|---------------------|---------------|
| 1 | ours (z.ai #1) | quota (T1) | yes | yes (but weekly quota 100% locked) | $0.001 |
| 2 | friend (z.ai #2) | quota (T1) | **NO** (key dead/disabled) | no | $0.001 |
| 3 | ollama_cloud (#1) | included (T4) | yes | yes | $0.001 |
| 4 | ollama_cloud_2 (#2) | included (T4) | yes (new key) | yes | $0.001 |
| 5 | opencode_go | flat (T3) | yes | yes | $0.001 |
| 6 | neuralwatt | balance (T2) | yes (new key) | yes | $1.83 |
| 7 | deepinfra | per_token (T5) | yes | yes | $1.30 |
| 8 | telnyx | per_token (T5) | yes | yes (but Kimi-only guard) | $5.40 |
| 9 | ppq | per_token (T5) | **NO** (balance $0, disabled) | no | $0.80 |
| 10 | openrouter | per_token (T5) | **NO** (negative balance) | no | $1.50 |
| 11 | routstr | per_token (T5) | yes | no (wallet exhausted) | $1.00 |
| 12 | routstrd | per_token (T5) | yes | no (wallet exhausted) | $1.00 |

**Working: 9/12** (have keys + healthy)
**Dead: 3/12** (friend key missing, ppq disabled, openrouter disabled)

Plus: oxalpha (promo OpenRouter key) — present but returning 401 (stale key)

## Why NeuralWatt Got Traffic Instead of Ollama Cloud

There were **TWO separate bugs** causing this:

### Bug 1: Death Spiral (FIXED in previous session)

The flat router was incrementing failure counts even when a key was in backoff and was never actually tried. This caused all 5 cheap providers (ours, friend, ollama_cloud, ollama_cloud_2, opencode_go) to accumulate 1843+ failures and get locked in 900s backoff. With all cheap providers unavailable, neuralwatt ($1.83/M) was the only healthy provider with a key.

**Status: FIXED** — the flat router now checks `_is_key_healthy()` before incrementing failure count.

### Bug 2: Deepseek Requests Bypass the Flat Router (NOT YET FIXED)

This is the **primary remaining issue**. Worker profiles were switched to `deepseek/deepseek-v4-flash`. When a worker sends a deepseek request:

1. The proxy hits "Step 1c-3: Non-z.ai models" early exit (line ~5112)
2. This path tries: `_try_opencode_go` → `_try_external_failover`
3. It does **NOT** try `_try_ollama_cloud_any`
4. `EXTERNAL_PROVIDERS` (used by `_try_external_failover`) does **NOT** include ollama_cloud
5. So the failover order is: opencode_go → deepinfra → telnyx → neuralwatt

**Ollama Cloud is never tried for deepseek requests** even though ollama_cloud CAN serve deepseek-v4-flash (confirmed: `deepseek-v4-flash:0731` is in the Ollama API catalog and returns HTTP 200).

The root cause is:
- `PROVIDER_MODELS["ollama_cloud"]` in `flat_router.py` does NOT include `deepseek/deepseek-v4-flash`
- So even if the request went through the flat router, ollama_cloud wouldn't be a candidate
- The early exit path (Step 1c-3) bypasses the flat router entirely
- Even in the flat router, `_PROVIDER_MODEL_NAMES` has no ollama_cloud entry for deepseek models

### Bug 3: z.ai Quota Not Gating Cost (Partially OK)

After restart, the quota_cache takes time to populate (it queries z.ai API). Until it does, `_compute_quota_health("ours")` returns 1.0 (optimistic), so the flat router sees z.ai as available ($0.001/M) and tries it first. The request gets a 429/empty, marks the key as failed, then falls through to ollama_cloud.

With the death spiral fix, this only costs one wasted round-trip (~2s) per request. The z.ai key gets a 2s backoff, and subsequent requests skip it. Once the quota cache populates (~30s after restart), the cost correctly goes to `inf` and z.ai is skipped entirely.

## Proposed Fix (for Bug 2)

### Option A: Add deepseek to ollama_cloud in the flat router (RECOMMENDED)

1. Add `deepseek/deepseek-v4-flash` and `deepseek/deepseek-v4-pro` to `PROVIDER_MODELS["ollama_cloud"]` and `PROVIDER_MODELS["ollama_cloud_2"]` in `flat_router.py`
2. Add model name translation in `_PROVIDER_MODEL_NAMES` for ollama_cloud:
   - `"deepseek/deepseek-v4-flash"` → `"deepseek-v4-flash:0731"`
   - `"deepseek/deepseek-v4-pro"` → `"deepseek-v4-pro:0813"`
3. Add `_try_ollama_cloud_any` to the non-z.ai early exit path (Step 1c-3) BEFORE `_try_opencode_go` and `_try_external_failover`

This ensures ollama_cloud ($0.001/M, included) is tried before neuralwatt ($1.83/M) for deepseek requests.

### Option B: Route deepseek through the flat router only

Remove the Step 1c-3 early exit and let all models go through the flat router. This is cleaner but riskier — it's a bigger code change and could affect routing for other non-z.ai models.

## Checklist (for implementation when approved)

- [ ] G1: Add deepseek models to PROVIDER_MODELS["ollama_cloud"] and ["ollama_cloud_2"] in flat_router.py
- [ ] G2: Add ollama_cloud model name translation to _PROVIDER_MODEL_NAMES in zai_proxy.py
- [ ] G3: Add _try_ollama_cloud_any to the Step 1c-3 early exit path (before opencode_go)
- [ ] G4: Test that deepseek requests route to ollama_cloud (HTTP 200, <2s)
- [ ] G5: Verify glm-5.2 routing still works (no regression)
- [ ] G6: Verify neuralwatt only gets traffic when ollama_cloud + opencode_go both fail
- [ ] G7: espeak-ng notification
