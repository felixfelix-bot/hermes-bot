---
name: adding-api-key-to-live-router
category: devops
description: "11-step process for adding a new API provider to the flat routing architecture with Kalman-based price discovery. Covers env vars, key loading, provider registration, cost models, optimizer setup, model mapping, balance tracking, dispatch functions, testing, and health tracking."
---

# Adding a New API Provider to the Flat Routing Architecture

## Purpose

Documents the complete process of adding a new API provider to the flat routing architecture. Ensures the provider becomes a full equal participant in Kalman-based price discovery — no caps, no manual preference, just free-market competition.

## When to Use

- Adding a new LLM API provider (OpenAI-compatible or custom) to the live router
- Onboarding a new API key for an existing provider that needs separate tracking
- Any change that introduces a new entry in `EXTERNAL_PROVIDERS`

## Prerequisites

- Access to `zai_proxy.py` (the flat router implementation)
- Access to `src/balance_collectors.py` (balance bridge implementations)
- The provider's API key and base URL
- The provider's model catalog and pricing information
- The provider's model name mappings (if different from canonical names)

## The Steps (11 + Step 2.5)

### Step 1: Add env vars

Add the provider's API key (and optional base URL / starting balance) to `~/.hermes/profiles/manager/.env`:

```bash
NEWPROVIDER_API_KEY=sk-xxx
NEWPROVIDER_BASE=https://api.newprovider.com/v1   # optional, if non-standard
NEWPROVIDER_STARTING_BALANCE=10.0                  # optional, for balance-tracked providers
```

### Step 2: Load the key in `_load_external_keys()`

Add a loader branch in `_load_external_keys()` (line 505) for the new env var:

```python
elif line.startswith("NEWPROVIDER_API_KEY=") and "newprovider" not in keys:
    keys["newprovider"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
```

### Step 2.5: Determine pricing model

Before registering the provider, determine its pricing model by answering these questions. The answers determine which pricing tier (T1-T5) the provider gets and how the router prices it.

**Questions to ask about the new provider:**

1. **Is there a fixed quota or capacity?** (weekly / monthly / unlimited)
   - z.ai: yes, weekly quota (T1)
   - NeuralWatt: yes, monthly kWh (T2)
   - opencode_go: no, unlimited (T3)
   - ollama_cloud: yes, session + weekly (T4)
   - PPQ, DeepInfra, OpenRouter: no, pay per token (T5)

2. **Does unused capacity carry over to the next period?**
   - z.ai: no, resets weekly
   - NeuralWatt: unknown — DEFAULT: no carry-over (most prepaid plans don't)
   - If yes → no time decay needed (use it whenever, it doesn't expire)
   - If no → time decay applies (use-it-or-lose-it urgency, like T1)

3. **What happens when capacity is exhausted?**
   - z.ai: unavailable (health gate returns inf)
   - NeuralWatt: pay-per-token at same rate (Phase B)
   - opencode_go: rate-limited / 429
   - ollama_cloud: unavailable until session/weekly reset

4. **Is there a price increase after exhaustion?**
   - NeuralWatt: NO — same rate, just not prepaid anymore
   - If yes → depletion penalty model (old T2, now deprecated)
   - If no → two-phase state machine (new T2): Phase A prepaid ($0.001), Phase B measured rate

5. **What is the subscription cost?** (for profitability tracking)
   - opencode_go: $10/mo
   - NeuralWatt: $100/mo
   - z.ai: $20/mo
   - per-token providers: $0 (no subscription)
   - This goes into `SUBSCRIPTION_COSTS` dict for the monthly profitability report

6. **Is there a measured rate from the API?** (for cost tracking correction)
   - NeuralWatt: yes, 0.2762 correction factor (API overcounts 3.6×)
   - This applies to `_extract_cost()` (cost tracking), NOT to routing price
   - The router should see the REAL marginal cost, not the overcounted cost

**Decision matrix:**

| Quota? | Carry-over? | After exhaustion | Tier | Pricing |
|--------|-------------|-----------------|------|---------|
| Weekly | No | Unavailable | T1 | $0.001 × time_decay (weekly) |
| Monthly | No | Pay-per-token (same rate) | T2 | Phase A: $0.001 × time_decay (monthly), Phase B: measured rate |
| Monthly | Yes | Pay-per-token (same rate) | T2 | Phase A: $0.001, Phase B: measured rate |
| None | N/A | N/A | T3/T5 | $0.001 (flat) or measured rate (per-token) |
| Session | No | Unavailable | T4 | $0.001 × time_decay (session) |

Record the answers in the provider's config and proceed to Step 3.

### Step 3: Register in `EXTERNAL_PROVIDERS` dict

Add the provider to the `EXTERNAL_PROVIDERS` dict (line 688):

```python
"newprovider": {
    "base_url": NEWPROVIDER_BASE,
    "key": NEWPROVIDER_KEY,
},
```

### Step 4: Define the cost model

Determine the cost model and set the seed rate for the PriceKalman:

- **Per-token:** Set seed to the published $/M rate. Will converge to real cost via Kalman updates.
- **Flat-rate:** Set seed to subscription-equivalent $/M = `($monthly_cost) / (estimated_monthly_tokens)`.
- **Included:** Set seed to a small positive value (e.g., $0.10/M) representing opportunity cost.

> **Never seed at $0** — causes division by zero in scarcity calculations and Kalman instability.

### Step 5: Register in the optimizer

Add the provider to the `RoutingOptimizer` (or `LiveRouter`) with its Kalman filters:

```python
optimizer.add_provider(
    "newprovider",
    PriceKalman(initial_rate=<seed_rate>),  # from Step 4
    ConsumptionKalman(),
    quota_remaining=<tokens_or_inf>,
    model_tier="<high|standard|low>",       # quality tier
    quota_total=<total_quota_or_None>,
    peak_hours_utc=None,                     # most providers have no peak
    peak_mult=1.0,
)
```

### Step 6: Add to `PROVIDER_MODELS`

Register which models the provider can serve:

```python
PROVIDER_MODELS["newprovider"] = {"glm-5.2", "kimi-k3", "deepseek-v4-flash", ...}
```

### Step 7: Add model name translation (if needed)

If the provider uses different model IDs, add to `_PROVIDER_MODEL_NAMES` (line 631):

```python
"newprovider": {
    "glm-5.2": "newprovider/glm-5.2",
    "kimi-k3": "moonshot/kimi-k3",
    ...
},
```

### Step 8: Add balance tracking (if per-token)

Create a balance bridge (mirror the PPQ/OpenRouter/NeuralWatt pattern):

1. Add a collector entry in `src/balance_collectors.py` for the new provider.
2. Add a bridge import in `zai_proxy.py` (near line 290-350).
3. Add the provider to `_snapshot_quota()` (line 1380) and `_snapshot_health()` (line 1438).

### Step 9: Add a `_try_*` dispatch function (or reuse existing)

If the provider has a standard OpenAI-compatible API (like most externals), no new function is needed — `_try_external_failover()` handles it via `EXTERNAL_PROVIDERS`.

If it has special requirements (like ollama_cloud's paywall or opencode_go's native glm-5.3), create a `_try_newprovider()` method on the Handler class.

### Step 10: Test

1. **Unit test:** Add the provider to test fixtures. Verify it appears in `select_provider()` output when healthy and is excluded when unhealthy.
2. **Live test:** Send a test request with a model the provider supports. Verify:
   - The provider appears in the candidate list with correct effective cost.
   - If it's the cheapest, the request routes to it.
   - After the request, the PriceKalman and ConsumptionKalman are updated.
   - The `api_calls` table logs the provider name and cost.
3. **Failover test:** Disable the provider (`.key_disabled_newprovider`). Verify it's excluded from candidates. Re-enable and verify it returns.

### Step 11: Add health tracking

The provider automatically gets health tracking via `_zai_key_health` and `_is_key_healthy()`. Verify:

- 429 response → `_mark_key_exhausted("newprovider")` → exponential backoff.
- 401/403 → `_mark_key_dead("newprovider")` → 1h backoff.
- 402 → `_mark_unfunded("newprovider")` → 5-min retry.
- Success → `_mark_key_healthy("newprovider")` → reset.

## Provider Metadata Summary

Each provider needs the following metadata collected and registered:

| Metadata | Source | Example |
|---|---|---|
| API key | `.env` | `NEWPROVIDER_API_KEY=sk-xxx` |
| Base URL | `.env` or constant | `https://api.newprovider.com/v1` |
| Cost model | Manual determination | per-token / flat-rate / included |
| Seed $/M rate | Cost model → calculation | $0.80/M (per-token), $0.20/M (flat-rate equiv) |
| Models available | Provider's model catalog | `{"glm-5.2", "kimi-k3", ...}` |
| Model name mapping | Provider's API docs | `{"glm-5.2": "newprovider/glm-5.2"}` |
| Quota/balance tracking | Balance bridge or hardcoded | `used_pct`, `remaining`, `total` |
| Quality tier | Model quality assessment | high / standard / low |
| Peak hours | Provider's pricing model | None (most), (6,10) for z.ai |
| Health tracking | Automatic via `_zai_key_health` | backoff on 429/403/402 |

## Common Pitfalls

1. **Forgetting to add to `PROVIDER_MODELS`:** If a provider isn't in the model registry, `select_provider()` will never route to it, even if it's the cheapest. Always register the models the provider can serve.

2. **Wrong seed rate for flat-rate providers:** Seeding at $0 makes the Kalman filter numerically unstable (division by zero in scarcity calculations). Always seed at a small positive value (subscription-equivalent rate).

3. **Missing balance bridge for per-token providers:** Without a balance bridge, `_snapshot_quota()` returns `{used_pct: 0.0, remaining: inf}` — the provider looks like it has infinite quota. This means scarcity_factor is always 1.0 and the provider never gets price-penalized for depletion. Always add a balance bridge for per-token providers.

4. **Model name mismatch:** If the provider expects `deepseek-ai/DeepSeek-V4-Pro` but you register `deepseek/deepseek-v4-pro` in `_PROVIDER_MODEL_NAMES`, requests will 404. Test with a real request to verify model name translation.

5. **Not adding to `_snapshot_health()`:** If the provider isn't in the health snapshot, the optimizer assumes it's healthy even when it's not. This can route traffic to a dead provider. Always add to `_snapshot_health()`.

6. **Peak hours on non-z.ai providers:** Only z.ai has peak pricing (UTC 6-10, 3x). Setting peak_hours on other providers makes them artificially expensive during those hours, which is wrong — they don't charge more during z.ai peak.

7. **Forgetting Kalman live updates:** If you add a provider to the optimizer but don't call `_update_kalman_after_request()` after each request, the PriceKalman stays at its seed value forever. The cost estimate never improves. Always wire up the post-request Kalman update.

8. **Quality tier misclassification:** Setting a provider to "low" tier when it serves high-quality models means it won't be considered for "high" difficulty requests. Verify the quality tier matches the models the provider actually serves.

## Verification Checklist

After completing all 11 steps, verify:

- [ ] Provider appears in `select_provider()` candidates when healthy
- [ ] Provider is excluded from candidates when disabled (`.key_disabled_newprovider`)
- [ ] Provider routes correctly when it's the cheapest option
- [ ] PriceKalman updates after a live request (seed rate converges)
- [ ] ConsumptionKalman updates after a live request
- [ ] `api_calls` table logs the provider name and cost
- [ ] `_snapshot_quota()` returns real balance data (not `inf`) for per-token providers
- [ ] `_snapshot_health()` returns correct health status
- [ ] 429 → exponential backoff triggers
- [ ] 401/403 → 1h backoff triggers
- [ ] 402 → 5-min retry triggers
- [ ] Success → health resets
- [ ] No peak_hours set (unless provider is z.ai)
- [ ] Quality tier matches actual model quality

## Constraints

- **NO CAPS on any provider** — all providers compete freely on price
- **Free market price discovery** — the Kalman filter determines real cost, not manual preference
- **Every provider is an equal participant** — no hardcoded priority ordering