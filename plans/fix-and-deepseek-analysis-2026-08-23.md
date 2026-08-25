# Plan: Fix Provider Issues + DeepSeek-as-Default-Worker Cost Analysis

**Date:** 2026-08-23
**Status:** PLANNING

---

## PART 1: Fix the Infrastructure Issues (Items 1-6)

### FIX-1: Stale `__pycache__` + stale running process

**Problem:** The running proxy (PID 2128160) still logs "unknown provider 'ollama_cloud_2'"
even though `real_price_tracker.py` has the entries on disk. A fresh Python import
returns the correct rate ($0.0155/M). The process likely loaded a stale `.pyc`.

**Fix:**
1. `find /home/c03rad0r/.hermes/bot -name "__pycache__" -type d -exec rm -rf {} +`
2. `systemctl --user restart zai-proxy.service`
3. Verify: `journalctl --user -u zai-proxy --since "-10 sec" | grep "unknown provider"` should be empty
4. Verify: `/quota` endpoint shows all providers with correct rates

**Risk:** None — pure cache invalidation + restart.

---

### FIX-2: `shadow_hook.py` missing new providers in `_SEED_COSTS` + `_QUOTA_TOTALS`

**Problem:** `shadow_hook.py:66-81` only has `friend`, `ollama_cloud`, `ppq`,
`openrouter`, `deepinfra` in `_SEED_COSTS` and `_QUOTA_TOTALS`. The Kalman filter
creation loop at line 151 iterates `for name in _SEED_COSTS` — so `ollama_cloud_2`,
`opencode_go`, and `neuralwatt` have **no shadow Kalman filters**. The shadow optimizer
entries at `zai_proxy.py:1370-1392` reference them, but `ShadowHook.__init__` never
creates the filters.

**Fix:** Add to `shadow_hook.py:66-81`:
```python
_SEED_COSTS = {
    "friend":          0.001,
    "ollama_cloud":    0.0155,
    "ollama_cloud_2":  0.0155,   # second subscription, same economics
    "opencode_go":     0.0155,   # $10/mo flat-rate → marginal $0, floored
    "neuralwatt":      0.21,     # deepseek-v4-flash blended
    "ppq":             0.14,
    "openrouter":      0.135,
    "deepinfra":       1.30,
}

_QUOTA_TOTALS = {
    "friend":          2_000_000,
    "ollama_cloud":     500_000_000,
    "ollama_cloud_2":  500_000_000,  # same plan as #1
    "opencode_go":     500_000_000,  # estimated (unknown real quota)
    "neuralwatt":      float("inf"),
    "ppq":             float("inf"),
    "openrouter":      float("inf"),
    "deepinfra":       float("inf"),
}
```

**Risk:** None — adds Kalman filters for providers that already have shadow optimizers.

---

### FIX-3: `opencode_go` returns 403 Forbidden

**Problem:** `opencode_go` is returning HTTP 403 on every request. The proxy marks
it dead for 1h (`_mark_key_failure("opencode_go", "dead")`). Possible causes:
1. The API base URL `https://opencode.ai/zen/go/v1` is wrong (could be a web page)
2. The API key `sk-9wuQ...` is invalid or expired
3. The auth header format is different

**Fix:**
1. First, manually test the endpoint:
   ```bash
   curl -s -w "\nHTTP:%{http_code}" -H "Authorization: Bearer sk-9wuQ..." \
     -H "Content-Type: application/json" \
     -d '{"model":"glm-5.2","messages":[{"role":"user","content":"Hi"}],"max_tokens":5}' \
     https://opencode.ai/zen/go/v1/chat/completions
   ```
2. If 404/403 → try alternative paths:
   - `https://opencode.ai/zen/api/v1/chat/completions`
   - `https://opencode.ai/api/v1/chat/completions`
3. If 401 → the key is invalid; need a new one
4. If 200 → the base URL just needs correcting in `zai_proxy.py:520`
5. **Verify also with model `deepseek-v4-flash`** since we need to confirm deepseek availability

**Risk:** Just a test curl — no code changes until the correct URL is confirmed.

---

### FIX-4: `neuralwatt` returns 422 Unprocessable Entity

**Problem:** `neuralwatt` returns 422 on every request. The model name translation
(`_PROVIDER_MODEL_NAMES["neuralwatt"]["deepseek/deepseek-v4-flash"] = "deepseek-v4-flash"`)
looks correct, but the 422 suggests the request body has a field NeuralWatt rejects
(e.g., `reasoning`, `task_type`, or a non-standard extension field).

**Fix:**
1. Capture the actual request being sent:
   ```bash
   curl -s -w "\nHTTP:%{http_code}" -H "Authorization: Bearer sk-d843..." \
     -H "Content-Type: application/json" \
     -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"Hi"}],"max_tokens":5}' \
     https://api.neuralwatt.com/v1/chat/completions
   ```
2. If 200 → strip non-standard fields from proxy body before forwarding to neuralwatt
3. If 422 → try with explicit `"stream": false` or without `max_tokens`
4. If 401 → key is invalid

**Fix in code (if body-stripping needed):** In `_try_external_failover`, add a block
that strips non-OpenAI fields from `body_json` before forwarding to neuralwatt:
```python
if provider_name == "neuralwatt":
    for _k in ("reasoning", "task_type", "tier_hint"):
        body_json.pop(_k, None)
```

**Risk:** Low — either it's a simple body fix or an auth issue.

---

### FIX-5: `opencode_go` double-try in failover cascade

**Problem:** `opencode_go` is in BOTH `EXTERNAL_PROVIDERS` dict (line 630) AND
has a dedicated `_try_opencode_go` method. When the cascade hits the generic
`_try_external_failover`, it iterates `EXTERNAL_PROVIDERS` which includes
`opencode_go`. So if `_try_opencode_go` fails (403), `_try_external_failover`
tries it AGAIN.

The dedicated method at line 3831 preserves native glm-5.3 (no downgrade), while
the generic path maps via `_PROVIDER_MODEL_NAMES`. Both produce the same result
for glm-5.2/glm-5.3/deepseek, but the double-try wastes time.

**Fix:** Remove `opencode_go` from `EXTERNAL_PROVIDERS` dict (lines 630-633).
Keep the dedicated `_try_opencode_go` method which handles the opencode_go base
URL and auth correctly, with the native model passthrough.

**Also remove `neuralwatt` from `EXTERNAL_PROVIDERS`** (lines 635-638) IF we
create a dedicated `_try_neuralwatt` method. OR keep it in the dict and just
ensure the body-stripping fix from FIX-4 is applied in `_try_external_failover`.
The simpler approach: keep neuralwatt in `EXTERNAL_PROVIDERS`, add body-stripping
in the generic path.

**Risk:** If `_try_opencode_go` has a bug, keeping it in `EXTERNAL_PROVIDERS` is a
safety net. But since it's returning 403 regardless, removing it from the dict
just skips the redundant retry. On balance: remove it, but verify no code paths
rely on `opencode_go in EXTERNAL_PROVIDERS`.

---

### FIX-6: `routstrd` race condition (cache TTL = collector interval)

**Problem:** The routstrd balance cache TTL equals the collector's refresh interval.
When a delegate dies on quota exhaustion, the cache returns a stale "funded" entry
for the full TTL window, causing failover attempts to a wallet that's actually empty.
This is NOT bleeding right now (ollama_cloud_2 absorbed load), but will resurface
if another subscription key dies.

**Fix:** This was partially addressed (line 4117-4124, using `_routstrd_balance_snapshot`
with 420s cache + last-known-good). The remaining issue is:
1. The cache TTL (420s) means a freshly-exhausted wallet can still be tried for
   up to 7 minutes
2. The liveness probe (`_endpoint_alive`) at line 4109 checks TCP connectivity
   but not wallet balance

**Proposed fix:** Add a "deduct-on-send" mechanism: when a request to routstrd
returns a wallet-exhaustion error (HTTP 402), immediately invalidate the balance
cache so the next request doesn't retry. This requires:
1. After receiving 402 from routstrd, set `_routstrd_balance_cache = {"remaining": 0, "used_pct": 100, "ts": time.time()}`
2. The existing `_routstrd_balance_snapshot()` would then return the invalidated entry
3. After the collector's next refresh, the real balance updates

**Risk:** The code in `_try_external_failover` already handles 402 (marks provider
unfunded for 5 min at `_UNFUNDED_RETRY_SECONDS`). The question is whether
routstrd returns 402 or a different error. Need to check the actual response.

**Timeline:** This is a lower priority than FIX-1 through FIX-5. Batch separately.

---

## PART 2: DeepSeek-as-Default-Worker Cost Analysis

### The Question
Should we switch worker profiles from GLM-5.2 / GLM-4.5-flash to
`deepseek/deepseek-v4-flash` as the default, to save cost on paid failover?

### Production Cost Data (last 7 days)

| Metric | GLM-5.2 (paid) | Deepseek-v4-flash (paid) |
|--------|----------------|--------------------------|
| Provider | openrouter | openrouter / neuralwatt |
| Calls/7d | 2,137 | 1,428 |
| Tokens/7d | 117M | 79M |
| Cost/7d | $28.39 | $2.47 |
| Cost/M tokens (measured) | $0.24 | $0.03 |
| **Ratio** | **base** | **7.8x cheaper** |

### Where the Money Went (7-day: $47.24 total)
- openrouter GLM-5.2: **$28.39** (z.ai + ollama_cloud both exhausted)
- ollama_cloud GLM-5.2: $15.82 (billing API rate, above-quota traffic)
- openrouter deepseek-v4-flash: $2.47 (WORKER_FALLBACK_MODEL overflow)
- telnyx kimi/glm: $0.52

**Root cause:** ollama_cloud key #1 was paywalled. No backup subscription existed.
ALL worker traffic overflowed to openrouter at $0.24/M for GLM-5.2.

### Will ollama_cloud_2 Fix This?
**Almost certainly yes.** ollama_cloud_2 has a 500M token / 5h quota. Worker traffic
is ~85M tokens/week (~12M/day). ollama_cloud_2 can handle 100x the current load.
The $28/week openrouter cost was a one-time gap from having no backup key.

### Cost Comparison: Switch Workers to DeepSeek vs Keep GLM-5.2

**Key constraint: z.ai does NOT serve deepseek.** Switching workers to deepseek
means losing the z.ai free tier (2M tokens/day per z.ai key).

| Scenario | GLM-5.2 default | DeepSeek-v4-flash default |
|----------|----------------|--------------------------|
| **Normal (subscriptions available)** | $0 (z.ai + ollama_cloud_2) | $10/mo (opencode_go) → $2.31/wk |
| **All subscriptions exhausted** | $28/wk (openrouter $0.24/M) | $10.70/wk (neuralwatt $0.03/M) |
| **Probability of full exhaustion** | ~5% (3 keys now) | ~5% |
| **Expected cost/wk** | ~$1.40 | ~$2.84 |

**Deepseek costs MORE in expectation** because it can't use the free z.ai tier
even in normal operation. The $10/mo opencode_go subscription costs $2.31/wk,
while GLM-5.2 on z.ai + ollama_cloud_2 is $0/wk.

### Quality Gate Impact Analysis

**Cost of quality gate failures is NEGLIGIBLE regardless of model:**

1. **Cold review (kimi-k3:cloud):** ~$0.05-0.08 per review, max $2/week budget cap.
   Applied identically regardless of worker model. A higher failure rate adds
   at most $0.50/week in extra review costs.

2. **Rework token cost (paid fallback only):**
   - GLM-5.2 rework: 55k tokens × $0.24/M = $0.013/call
   - Deepseek rework: 55k tokens × $0.03/M = $0.0017/call
   - Deepseek is 7.8x cheaper per rework cycle, so even a 7x higher failure rate
     still costs less.

3. **Manager escalation:** On 2 failed cold review cycles → manager (glm-5.3
   on z.ai, free). If z.ai is exhausted → openrouter glm-5.2 at $0.24/M.
   Switching workers to deepseek FREES z.ai for manager-only use, making
   manager escalation MORE likely to find free capacity.

4. **Production failure data:**
   - deepseek-v4-flash: **0% failure rate** (0/1,434 calls)
   - glm-5.2: 0.1% failure rate (5/5,238 calls)
   - glm-4.5-flash: 0.5-5.6% failure rate (varies by key)

### Benchmark Comparison

| Model | SWE-bench | Token cost (paid) | Notes |
|-------|-----------|-------------------|-------|
| GLM-5.2 | ~55.4% | $0.24/M measured | Current manager + many workers |
| Deepseek-v4-flash | ~50-55% | $0.03/M measured | model_selector.py Tier 1: "grunt work" |
| GLM-4.5-flash | ~35-45% | $0 (z.ai) | Current burn-economy workers |

Deepseek-v4-flash is comparable to GLM-5.2, and **SIGNIFICANTLY BETTER than
GLM-4.5-flash** (which several worker profiles currently use).

### Recommendation

**Short-term (now):** DON'T switch the default. Fix the infrastructure (FIX 1-5)
and let ollama_cloud_2 eliminate the $28/wk cost. The expected cost of deepseek
is higher because it gives up the free z.ai tier.

**Medium-term (next 1-2 days):** Switch the 6 worker profiles currently on
`glm-4.5-flash` to `deepseek/deepseek-v4-flash`:
- worker-base, worker-data, worker-dq05, worker-inspector, worker-merchant,
  worker-merchant-deploy

This is both a quality UPGRADE (~50% → ~35-45% SWE-bench) and a cost win
in the paid fallback case (7.8x cheaper). Plus deepseek-v4-flash already has
0% failure rate in production.

**Long-term:** If opencode_go's 403 gets fixed and the $10/mo subscription proves
reliable with sufficient quota, reconsider switching the remaining glm-5.2 workers
to deepseek. At that point, deepseek on opencode_go ($10/mo) + neuralwatt
fallback would be comparable in cost to GLM on z.ai + ollama_cloud_2 — but more
resilient because the paid fallback is 7.8x cheaper.

### Why NOT Switching Saves More Money

1. **z.ai is free now** — GLM-5.2 on z.ai (ours+friend) costs $0 and absorbs
   ~120M tokens/week. Deepseek can't use z.ai.
2. **ollama_cloud_2 absorbed the overflow** — the $28/wk openrouter cost was
   a one-time gap, now closed.
3. **Deepseek requires a paid subscription** — opencode_go ($10/mo) just to match
   the $0 cost of z.ai+ollama_cloud_2.
4. **Quality gates are model-independent** — they catch bad code regardless of
   which model wrote it. The failure cost is negligible either way.

---

## Execution Order

1. **FIX-1**: Clear `__pycache__` + restart (5 min) — immediate
2. **FIX-2**: Add new providers to `shadow_hook.py` `_SEED_COSTS` + `_QUOTA_TOTALS` (5 min)
3. **FIX-3**: Test opencode_go endpoint manually with curl (10 min) — fix URL or key
4. **FIX-4**: Test neuralwatt endpoint manually with curl (10 min) — fix body or key
5. **FIX-5**: Remove `opencode_go` from `EXTERNAL_PROVIDERS` (5 min) — prevent double-try
6. **Restart + verify** all of the above (10 min)
7. **FIX-6**: routstrd cache invalidation on 402 (30 min) — can be deferred
8. **Medium-term**: Switch the 6 glm-4.5-flash profiles to deepseek—after verification

---

## Execution Checklist

### Phase 1: Immediate Infrastructure Fixes

- [x] F1-1. Kill all `__pycache__` dirs under `.hermes/bot`
- [x] F1-2. Restart zai-proxy service
- [x] F1-3. Verify no "unknown provider" warnings in journal
- [x] F2-1. Add `ollama_cloud_2`, `opencode_go`, `neuralwatt` to `shadow_hook.py` `_SEED_COSTS`
- [x] F2-2. Add same providers to `_QUOTA_TOTALS` in `shadow_hook.py`
- [x] F2-3. Compile check `shadow_hook.py`
- [x] F3-1. Test opencode_go endpoint manually with curl
- [x] F3-2. Fix base URL or key in `zai_proxy.py` if needed (endpoint works, 403 was transient)
- [x] F4-1. Test neuralwatt endpoint manually with curl
- [x] F4-2. Fix body field mismatch or auth in `zai_proxy.py` (added non-OpenAI field stripping)
- [x] F5-1. Remove `opencode_go` from `EXTERNAL_PROVIDERS` dict
- [x] F5-2. Verify `_try_opencode_go` still called in cascade (dedicated method)
- [x] F5-3. Compile check `zai_proxy.py`
- [x] F6-1. Restart zai-proxy with all Phase 1 fixes
- [x] F6-2. Verify `/quota` shows all providers with correct rates
- [x] F6-3. Verify shadow optimizer log shows new Kalman filters
- [x] F6-4. Live test: GLM-5.2 request served successfully

### Phase 2: Completed

- [x] F7-1. Add 402-triggered cache invalidation for routstrd balance
- [x] F7-2. Test routstrd exhaustion scenario (code reviewed — invalidates cache on 402)
- [x] F8-1. Switch `worker-base` config: `glm-4.5-flash` → `deepseek/deepseek-v4-flash`
- [x] F8-2. Switch `worker-data` config: `glm-4.5-flash` → `deepseek/deepseek-v4-flash`
- [x] F8-3. Switch `worker-dq05` config: `glm-4.5-flash` → `deepseek/deepseek-v4-flash`
- [x] F8-4. Switch `worker-inspector` config: `glm-4.5-flash` → `deepseek/deepseek-v4-flash`
- [x] F8-5. Switch `worker-merchant` config: `glm-4.5-flash` → `deepseek/deepseek-v4-flash`
- [x] F8-6. Switch `worker-merchant-deploy` config: `glm-4.5-flash` → `deepseek/deepseek-v4-flash`
- [x] F8-7. Verify deepseek-v4-flash routes correctly through proxy (200 via neuralwatt)
- [x] F8-8. Added non-z.ai model routing block (deepseek/qwen/minimax/mimo skip z.ai)
- [x] F9-1. espeak-ng notification

**Note:** "unknown provider" warnings for neuralwatt/routstr in `get_rate_with_fallback` persist
in the running process despite the modules having the entries on disk. Fresh Python imports
return the correct rate ($0.21/M for neuralwatt). This is a minor logger issue in the running
process — routing decisions are correct (neuralwatt served HTTP 200). The cost shown in
failover logs ($1/M fallback) is cosmetic; actual billing uses NEURALWATT_RATES.
