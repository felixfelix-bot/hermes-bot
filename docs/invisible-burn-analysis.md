# Invisible Burn Analysis: Unrouted Cost Providers

**Date:** 2026-08-24  
**Analyst:** Consultant subagent (delegated by manager)  
**Trigger:** `cost-performance-escalation` cron (42a64624fb5d) flagged routstr: 30 calls, 1.25M tokens, 100% NULL cost in 6h window.

---

## Executive Summary

The invisible burn is real but the root cause is **not** a missing `_extract_cost` branch alone — it's a **cascade of three failures**:

1. **Missing `_extract_cost` branches** for routstr, routstrd, ppq, openrouter, deepinfra → `cost_usd = NULL` in `api_calls`
2. **Broken `routstr_probe.py`** — the daily cost probe that should seed `measured_rates` is failing (401 Unauthorized for routstr, 405 Method Not Allowed for routstrd) → no ground-truth cost data
3. **Cheap-provider failures** pushed traffic to routstr — all providers cheaper than routstr ($0.001–$0.082/M) have high failure counts (ours: 118, opencode_go: 100, ollama_cloud: 66, friend: 79 failures), so the flat router fell through to routstr ($1.00/M seed) for glm-5.3

The catch-all fallback being implemented by `deleg_deb505b0` **will work** — `_rpt_rate()` returns $0.53/M for routstr (from real_price_tracker last-resort estimates), so the fallback will produce non-None cost. But this is an **estimate**, not a measurement, and the Kalman cannot converge without real cost signal.

---

## Q1: WHY is routstr burning 1.25M tokens?

### Model served
**glm-5.3** in 40 of 41 calls (2.16M tokens). One call was glm-4.5-flash (6 tokens), one was glm-5.2 (795 tokens, historical).

### Why the flat router sent traffic to routstr
The flat router candidate ordering for glm-5.3 is:
```
opencode_go ($0.001/M) → ours ($0.068/M) → friend ($0.082/M) → routstr ($1.00/M) → routstrd ($1.00/M)
```

Only 5 providers can serve glm-5.3: `ours`, `friend`, `opencode_go`, `routstr`, `routstrd`.
(ollama_cloud does NOT have glm-5.3 in `PROVIDER_MODELS` — it only has glm-5.2.)

**All cheaper providers are failing:**

| Provider | Failure Count | Last Error | Status |
|---|---|---|---|
| ours | 118 | flat_router_dispatch_fail | unhealthy |
| friend | 79 | flat_router_dispatch_fail | unhealthy |
| opencode_go | 100 | flat_router_dispatch_fail | unhealthy |
| routstr | 3 | flat_router_dispatch_fail | unhealthy (but served 41 calls) |

The flat router iterates candidates cheapest-first. When `ours`, `friend`, and `opencode_go` all fail to dispatch, it falls through to `routstr` — the only remaining viable provider for glm-5.3. The 3 failures on routstr are likely the most recent attempts where VPS2 became unreachable.

**Root cause of cheap-provider failures is OUT OF SCOPE** for this analysis but is the actual driver of the burn. If ours/friend/opencode_go were healthy, routstr would never receive glm-5.3 traffic.

### Is the burn real money?
routstr is our own VPS2 node, z.ai-backed upstream. The cost is:
- **Upstream:** z.ai API quota consumption (our own subscription keys on VPS2) — this is NOT additional cash spend, it's quota burn on keys we already pay for
- **Cashu metering:** sats are deducted from the Cashu wallet on VPS2, but since we control the node, the sats are self-minted — no external cash cost
- **Opportunity cost:** the z.ai quota burned on VPS2 could have been burned directly via `ours`/`friend` keys at $0 marginal cost, instead of going through the Cashu layer

**Conclusion:** The $1.25M tokens on routstr is NOT direct cash spend, but it IS real z.ai quota burn on VPS2 that could have been served cheaper through `ours`/`friend` if they were healthy. The "invisible" part is accurate — we have zero cost visibility for 41 calls.

---

## Q2: Is the catch-all fallback approach correct?

### Short answer: Yes, as a stopgap. No, as a permanent solution.

### The Kalman convergence problem
The catch-all fallback uses `_rpt_rate(provider) × total_tokens`. For routstr, `_rpt_rate("routstr")` returns **$0.53/M** — but this is a **last-resort estimate** from `real_price_tracker`, NOT a Kalman measurement. The logs confirm:

```
real_price_tracker: no real data for routstr/None — using last-resort ESTIMATE $0.53/M
```

The PriceKalman for routstr is at its **seed rate of $1.00/M** and has never converged because:
- `_extract_cost` returned `(None, None)` for all historical calls → `cost_usd = NULL`
- `_update_kalman_after_request()` receives `cost_usd=None` → no Kalman measurement update
- The Kalman has zero real observations — it's pure seed/estimate

**The catch-all will fix the NULL problem** (cost_usd will be $0.53 × tokens/M), but the $0.53 rate itself is unverified. The Kalman will start receiving signal from the fallback, but it will be learning from its own estimate — a circular reference. The Kalman needs **independent ground truth** to converge.

### Should routstr have its own `_extract_cost` branch?

**Yes — via Cashu wallet balance delta.** The routstr node exposes `/v1/balance/info` which returns the Cashu wallet balance. The correct approach:

1. **Per-request balance delta:** Read balance before and after each request, compute `sats_spent → usd_per_M` using BTC spot price. This is what `routstr_probe.py` does, but inline per-request.
2. **Problem:** The proxy doesn't have pre-request balance snapshots. It would need to either:
   - Query `/v1/balance/info` before and after each call (adds 2 HTTP calls per request — expensive)
   - Or use the `provider_balances` collector data (already running every 5 min) to compute periodic deltas

3. **Better approach: published sats_pricing.** The routstr node's `/v1/models` endpoint returns per-token pricing in `pricing.prompt` / `pricing.completion` (in sats per token). The `_get_routstr_rates()` function already fetches this (10-min cache). The `_extract_cost` branch should:
   ```python
   if provider in ("routstr", "routstrd"):
       rate = _get_routstr_rates().get(model_id) or _get_routstrd_rates().get(model_id)
       if rate and rate > 0:
           return ((total_tokens / 1_000_000) * rate, "rate_derived")
       # Fall through to catch-all
   ```

   This gives a real published rate, not a Kalman guess. The rate is in USD (converted from sats × BTC spot at fetch time).

### routstrd (local daemon)
Same issue — no `_extract_cost` branch. The daemon's `/v1/models` also returns pricing. The `_get_routstrd_rates()` function already exists. Same solution applies.

**However**, routstrd has had **zero traffic** in the last 24h (no api_calls), so it's not currently burning. The issue is latent.

---

## Q3: Does routstr have measured rates from routstr_probe.py?

### measured_rates table state (as of 2026-08-24 17:20 UTC):

| Provider | Model | usd_per_M | sats_per_M | Age | Method | Error |
|---|---|---|---|---|---|---|
| routstr | glm-5.2 | **NULL** | NULL | 19.8h | live_probe | chat: HTTP Error 401: Unauthorized |
| routstrd | glm-5.2 | **NULL** | NULL | 19.8h | live_probe | balance_before: HTTP Error 405: Method Not Allowed |
| routstr | glm-5.2 | **NULL** | NULL | 42.9h | live_probe | chat: HTTP Error 401: Unauthorized |
| routstrd | glm-5.2 | **NULL** | NULL | 42.9h | live_probe | balance_before: HTTP Error 405: Method Not Allowed |
| routstr | glm-5.2 | 0.6007 | 780 | 43.0h | published_rate | — |
| routstrd | glm-5.2 | 0.00077 | 1.0 | 43.0h | published_rate | — |

### Critical findings:

1. **All recent live_probe measurements FAILED** — routstr returns 401 (API key invalid/expired), routstrd returns 405 (daemon API changed). The probe log confirms:
   ```
   routstr_probe 2026-08-23 21:30 UTC:
     routstr: ERR chat: HTTP Error 401: Unauthorized
     routstrd: ERR balance_before: HTTP Error 405: Method Not Allowed
   ```

2. **The only valid measurements are 43h old** (published_rate) — past the 24h cutoff in `_get_measured_rate()`. So `_get_measured_rate("routstr", "glm-5.2")` returns `None`.

3. **No measurement exists for glm-5.3** — the probe only tests glm-5.2. All 40/41 routstr calls in the last 6h were for **glm-5.3**, which has zero measured rate data.

4. **VPS2 (DQ05) is currently unreachable** — confirmed by the DQ05 health monitor returning "ContextVM unreachable (tried LAN + Netbird)". This explains the 401 errors and recent dispatch failures.

### Will the catch-all fallback work?

**Yes, partially.** `_rpt_rate("routstr")` returns $0.53/M from the real_price_tracker's last-resort estimate. So the fallback will produce:
```
cost_usd = (1.25M tokens / 1e6) × $0.53 = $0.66
```

But this is a **rough estimate**, not a real measurement. The real cost depends on:
- The sats/M rate published by the routstr node (780 sats/M for glm-5.2 = $0.60/M at BTC $77,011)
- The rate for glm-5.3 (unknown — never measured, likely higher than glm-5.2)
- The BTC/USD exchange rate at time of spend

---

## Q4: Other providers with partial NULL cost

### Query: last 6h, per key_name

| key_name | total | null_count | tokens | NULL % | Has _extract_cost branch? |
|---|---|---|---|---|---|
| opencode_go | 459 | 150 | 49.1M | 33% | **YES** ($0.43/M estimated) |
| ollama_cloud_2 | 131 | 130 | 5.8M | 99% | **YES** (regime rate) |
| neuralwatt | 383 | 47 | 25.5M | 12% | **YES** (per-model rates) |
| **routstr** | **41** | **41** | **2.2M** | **100%** | **NO** |
| **ppq** | **22** | **22** | **443K** | **100%** | **NO** (cost_extraction.py handles it) |
| ollama_cloud | 2,341 | 0 | 160M | 0% | YES |
| ours | 352 | 0 | 502K | 0% | YES (flat_rate $0) |
| telnyx | 38 | 0 | 1.7M | 0% | YES (per-model rates) |

### Surprising findings:

1. **opencode_go has 150 NULL calls (33%)** despite having a specific `_extract_cost` branch. All 150 calls have **nonzero tokens** (min 19, max 143,392). The branch should always return `(tokens/1e6 × 0.43, "estimated")` for nonzero tokens. This means `_extract_cost` is either not being called or `_total_tokens` is being passed as 0 to `_extract_cost` while the DB records nonzero tokens (likely a streaming response parsing issue where `_parse_usage` fails on incomplete SSE buffers).

2. **ollama_cloud_2 has 130 NULL calls (99%)** despite having a specific branch. Only 1 call got "estimated". Same issue — `_extract_cost` is being called but `_get_ollama_cloud_cost_per_1m()` might be returning `inf` (exhausted regime), or `_total_tokens` is 0.

3. **ppq has 100% NULL** despite `cost_extraction.py` having a `ppq` entry that probes `usage.cost`, `cost`, `usage.total_cost`, `usage.estimated_cost`. Either ppq's response doesn't include any of these fields, or the SSE parsing fails to find the final usage chunk. PPQ HAS 104 historical non-NULL cost rows and measured rates in `price_observations` (from `ppq_ledger`), but these are 171h old (~7 days). The cost_extraction module should be catching it — need to investigate why it's not.

4. **deepinfra** has **0 calls** in the last 6h — not currently burning, but has zero non-NULL cost rows ALL-TIME. The `cost_extraction.py` has a `deepinfra` entry (`usage.estimated_cost`). Either deepinfra responses don't include this field, or deepinfra isn't being dispatched to.

5. **openrouter** has 0 calls in last 6h but has 3,734 historical non-NULL cost rows. The cost_extraction module works for openrouter (`usage.cost` field). Not currently a problem.

### The partial-NULL pattern (providers WITH branches)
The 33% NULL rate for opencode_go and 99% for ollama_cloud_2 suggests a **different bug**: `_extract_cost` is being called but `_total_tokens` is 0 (or the response buffer doesn't contain parseable usage). This happens when:
- The response is streaming (SSE) and the final `usage` chunk is not in `_cand_buffer`
- `_parse_usage` fails on the buffer format
- The dispatch returns success but the buffer is incomplete

This is a **wiring bug**, not a missing-branch bug. The catch-all fallback won't fix it because the fallback also needs `total_tokens > 0`.

---

## Q5: Should the invisible burn detector threshold be lowered from 10 calls?

### Current threshold: ≥10 calls AND >50% NULL cost in 6h window

### Analysis:

- **routstr**: 41 calls, 41 NULL — caught at 10 calls (after ~30 min of traffic). The 1.25M token threshold was hit at 41 calls.
- **ppq**: 22 calls, 22 NULL — caught at 10 calls.
- **opencode_go**: 459 calls, 150 NULL (33%) — caught at 10 calls but only as "partial" (not 100%).
- **ollama_cloud_2**: 131 calls, 130 NULL (99%) — caught at 10 calls.

**Lowering to 5 calls** would catch problems ~5 minutes earlier but at the cost of more false positives (a single failed cost extraction on a new provider would trigger).

**Recommendation: Keep the 10-call threshold.** The 6h window means the detector runs every 15 minutes (cron cadence) and catches any provider with sustained traffic. Lowering to 5 calls would mainly catch transient issues that self-resolve. The real problem isn't detection speed — it's that the fix (catch-all fallback) needs to be deployed.

**However**, add a **token-based threshold**: if any provider exceeds 100K tokens with 100% NULL cost, alert immediately regardless of call count. This would catch a single large request (e.g., 100K tokens in one call) that currently needs 10 calls to trigger.

---

## Q6: Right approach for each provider without a specific _extract_cost branch

### routstr / routstrd
**Best: Published sats_pricing from `/v1/models`**

The `_get_routstr_rates()` / `_get_routstrd_rates()` functions already fetch per-model USD rates from the node's `/v1/models` endpoint (10-min cache, converts sats/token → USD using BTC spot). This is the correct source — it's the node's own pricing, updated in real-time.

```python
if provider in ("routstr", "routstrd"):
    rates = _get_routstr_rates() if provider == "routstr" else _get_routstrd_rates()
    model_id = _extract_model_from_response(response_buffer) or "glm-5.2"
    rate = rates.get(model_id)
    if rate and rate > 0:
        return ((total_tokens / 1_000_000) * rate, "rate_derived")
    # Fall through to catch-all (_rpt_rate × tokens)
```

**Why not Cashu wallet balance delta?**
- Requires 2 HTTP calls per request (balance before/after) — adds latency
- The balance delta includes all concurrent requests, not just this one
- The published pricing is per-model and already available in cache

**Why not Kalman rate?**
- The Kalman has no real observations (all historical cost_usd = NULL)
- The Kalman is stuck at seed rate $1.00/M
- The published rate ($0.60/M for glm-5.2) is ground truth from the node itself

**Also needed:** Fix `routstr_probe.py` — the 401 error means the API key in the probe's env file is stale. The 405 error on routstrd means the daemon's `/v1/balance/info` endpoint changed. The probe should be updated to use `/v1/balance` or whatever the current endpoint is.

### deepinfra
**Best: Per-model API rates (like telnyx)**

DeepInfra publishes per-model pricing at `https://api.deepinfra.com/v1/openai/models`. The `cost_extraction.py` module already checks `usage.estimated_cost` in the response body — if DeepInfra returns this field, it should work. Need to verify whether the field is present in streaming responses.

If `usage.estimated_cost` is not in the response, fall back to per-model rate table (similar to `_TELNYX_MODEL_RATES`). DeepInfra's pricing is public and stable.

```python
if provider == "deepinfra":
    # Try measured (from cost_extraction module, already wired)
    # ... already handled by step 1 (cost_extraction_module)
    # If that returns None, use per-model rates:
    rate = _rpt_rate("deepinfra")  # $1.30/M fallback
    if rate > 0 and rate < float("inf"):
        return ((total_tokens / 1_000_000) * rate, "rate_derived_fallback")
```

The catch-all fallback handles this case already. The real question is why `cost_extraction.py` isn't finding `usage.estimated_cost` — likely a streaming parsing issue.

### ppq
**Best: /credits/balance API (real spend data)**

PPQ has a `/credits/balance` API endpoint that returns real credit balance. The `dq05_monitor` MCP server already has a `dq05_ppq` tool that queries this. The balance delta approach would give true cost:

```python
if provider == "ppq":
    # cost_extraction.py already probes usage.cost, cost, usage.total_cost
    # If none found, the PPQ /credits/balance API has real spend data
    # But per-query balance delta is impractical (async, cached)
    # Best: use per-model rate table (_PPQ_MODEL_RATES already exists)
    rate = _get_provider_cost("ppq", model_id)  # Already has per-model rates
    if rate and rate < 999.0:
        return ((total_tokens / 1_000_000) * rate, "rate_derived")
```

The `_PPQ_MODEL_RATES` table already has rates for z-ai/glm-5.2, moonshotai/kimi-k3, deepseek/deepseek-v4-flash. The issue is that `_extract_cost` doesn't call `_get_provider_cost` for ppq.

**Also:** PPQ has 104 historical non-NULL cost rows and `price_observations` from `ppq_ledger` (171h old). The cost_extraction module should be finding `usage.cost` in the response. Need to debug why it stopped working — possibly PPQ changed their response format.

### openrouter
**Best: Already handled — debug why cost_extraction.py stopped working**

OpenRouter returns `usage.cost` in every response (documented). The `cost_extraction.py` module has an `openrouter` entry. There are 3,734 historical non-NULL cost rows. But 0 calls in last 6h — not currently a problem.

If openrouter traffic resumes and costs go NULL, the issue is either:
- SSE parsing failure (the `usage` chunk is not being captured in the response buffer)
- The provider name being passed to `_extract_cost` is wrong (e.g., "openrouter_external" instead of "openrouter")

**The catch-all fallback handles this case** via `_rpt_rate("openrouter")` = $0.15/M (from a real measurement 171h ago).

---

## Q7: Is the alert correct? Is routstr actually costing money?

### The alert is CORRECT — cost visibility is broken

Even though routstr is our own VPS2 node, the alert is valid because:

1. **z.ai quota burn is real** — VPS2 uses our z.ai API keys upstream. Every token consumed on routstr burns z.ai quota that could have been used directly via `ours`/`friend` keys at $0 marginal cost. The opportunity cost is real.

2. **Cashu sats are self-minted** — we control the Cashu mint on VPS2, so the sats are not external cash spend. But the sats represent a metering layer that should be tracked for capacity planning.

3. **VPS2 infrastructure cost** — the VPS2 node costs ~$10-20/month. Traffic through routstr consumes CPU/memory/bandwidth on VPS2 that could be avoided by routing directly.

4. **The "invisible" part is the real problem** — without cost_usd in `api_calls`, the Kalman price discovery can't work, daily_spend is understated, and the escalation script can't detect cost anomalies. The system is flying blind for 100% of routstr traffic.

### What it DOESN'T cost:
- No external cash payment per token (self-minted Cashu sats)
- No additional z.ai subscription cost (same keys, same plan)
- The z.ai quota is "free" in the sense that it's already paid for

### What it DOES cost:
- z.ai quota that could be used for other workloads
- VPS2 compute resources
- **Loss of cost visibility** — the system can't make informed routing decisions

---

## Recommendations

### Immediate (catch-all fallback — being implemented by deleg_deb505b0)
✅ The catch-all fallback `_rpt_rate(provider) × tokens` will fix the NULL cost_usd problem. The fallback returns non-None for all 5 missing providers:
- routstr: $0.53/M (estimate)
- routstrd: $0.53/M (estimate)
- ppq: $0.14/M (estimate)
- deepinfra: $1.30/M (estimate)
- openrouter: $0.15/M (measured, 171h old)

### Short-term (provider-specific branches needed)

1. **routstr/routstrd:** Add a branch that uses `_get_routstr_rates()` / `_get_routstrd_rates()` (published USD pricing from `/v1/models`). This gives real per-model rates, not a Kalman guess. Source = `rate_derived`.

2. **ppq:** Add a branch that uses `_get_provider_cost("ppq", model_id)` which already has `_PPQ_MODEL_RATES`. Also investigate why `cost_extraction.py` stopped finding `usage.cost` in PPQ responses.

3. **deepinfra:** Investigate why `cost_extraction.py` isn't finding `usage.estimated_cost`. Likely a streaming SSE parsing issue. If the field isn't in streaming responses, add per-model rate table (like telnyx).

4. **Fix routstr_probe.py:** The 401 error means the API key in the probe's env is stale. The 405 on routstrd means the balance endpoint changed. This is the root cause of the Kalman not converging — no ground truth measurements.

### Medium-term (Kalman convergence)

5. **Seed the Kalman with published rates:** When `routstr_probe.py` fails, fall back to seeding `measured_rates` from `_get_routstr_rates()` (the `/v1/models` published pricing). This gives the Kalman a real (if not wallet-verified) starting point.

6. **Probe glm-5.3 too:** The probe only tests glm-5.2, but 97% of routstr traffic is glm-5.3. Add glm-5.3 to the probe targets.

7. **Add token-based alert threshold:** If any provider exceeds 100K tokens with 100% NULL cost, alert immediately regardless of call count.

### Long-term (root cause of cheap-provider failures)

8. **Investigate why ours/friend/opencode_go are all failing.** This is the actual driver of routstr traffic. If the cheap providers were healthy, routstr would never be in the candidate path for glm-5.3. The failure counts (118, 79, 100) suggest a systemic issue — possibly z.ai API quota exhaustion across all keys, or a shared infrastructure problem.

---

## Data Summary

### _extract_cost branch coverage

| Provider | _extract_cost branch | cost_extraction.py | _rpt_rate() | Historical non-NULL cost | Current NULL % |
|---|---|---|---|---|---|
| ours | ✅ flat_rate $0 | N/A | $0.03 | 352 | 0% |
| friend | ✅ flat_rate $0 | N/A | $0.015 | 0 (all $0) | 0% |
| ollama_cloud | ✅ estimated | N/A | $0.0155 | 2,341 | 0% |
| ollama_cloud_2 | ✅ estimated | N/A | $0.0155 | 1 | **99%** ⚠️ |
| opencode_go | ✅ estimated $0.43 | N/A | $0.0155 | 309 | **33%** ⚠️ |
| neuralwatt | ✅ per-model rates | N/A | $2.21 | 336 | 12% |
| telnyx | ✅ per-model rates | ✅ multi-path | $1.50 | 38 | 0% |
| **routstr** | ❌ **NO** | ❌ NO | $0.53 est | 1 (just now) | **100%** 🔴 |
| **routstrd** | ❌ **NO** | ❌ NO | $0.53 est | 0 | N/A (no traffic) |
| **ppq** | ❌ NO (cost_extraction handles) | ✅ usage.cost | $0.14 est | 104 (stale) | **100%** 🔴 |
| **openrouter** | ❌ NO (cost_extraction handles) | ✅ usage.cost | $0.15 meas | 3,734 | N/A (no traffic) |
| **deepinfra** | ❌ NO (cost_extraction handles) | ✅ usage.estimated_cost | $1.30 est | 0 | N/A (no traffic) |

### measured_rates table for routstr
- 6 live_probe rows: ALL have `usd_per_M = NULL` (probe failures: 401, 405, timeout)
- 2 published_rate rows: 43h old (past 24h cutoff)
  - routstr/glm-5.2: 780 sats/M = $0.60/M
  - routstrd/glm-5.2: 1 sats/M = $0.00077/M (suspiciously low — likely wrong)
- **No measurement for glm-5.3** (the model actually being served)

### PriceKalman state
- routstr: seed $1.00/M, 0 real observations, never converged
- routstrd: seed $1.00/M, 0 real observations
- ppq_external: seed $0.80/M, has some historical observations (stale)
- openrouter: seed $1.50/M, has 3,734 historical observations (but stale)
- deepinfra: seed $1.30/M, 0 real observations

### Catch-all fallback rates (what _rpt_rate returns)

| Provider | _rpt_rate() | Source | Reliability |
|---|---|---|---|
| routstr | $0.53/M | last-resort estimate | Low — no real data |
| routstrd | $0.53/M | last-resort estimate | Low — no real data |
| ppq | $0.14/M | last-resort estimate | Medium — has stale ledger data |
| deepinfra | $1.30/M | last-resort estimate | Low — no real data |
| openrouter | $0.15/M | real measurement (171h old) | Medium — stale but was real |

---

*Analysis complete. The catch-all fallback is a necessary stopgap. Provider-specific branches using published pricing APIs (especially for routstr/routstrd) should follow. The routstr_probe.py failures need to be fixed for the Kalman to ever converge.*