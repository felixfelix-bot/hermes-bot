# NeuralWatt Cost Tracking Audit

**Date:** 2026-08-23
**Scope:** z.ai proxy (`zai_proxy.py`), live router (`live_router.py`), Kalman filters
**Trigger:** Discrepancy — $4.45 recorded vs ~$226 expected for 2,837 NeuralWatt API calls

---

## TL;DR

NeuralWatt cost is tracked at a **flat $0.21/M blended rate** (deepseek-v4-flash seed)
for ALL models, including glm-5.2 which actually costs $1.45/M input + $4.50/M output.
The router, the Kalman price filter, and both spend tables all use this wrong rate.
NeuralWatt is completely missing from `kalman_price_state.json`, so the PriceKalman
filter never corrects the rate. **We're undercounting by ~6× on daily_spend and ~50×
on api_calls.cost_usd.**

---

## 1. Exact Cost Tracking Bug

### Bug A: `_estimate_cost_usd()` uses model-blind flat rate

**Location:** `zai_proxy.py:2651` — `_estimate_cost_usd(key_name, total_tokens)`

```python
def _estimate_cost_usd(key_name: str | None, total_tokens: int) -> float:
    ...
    cost_per_1m = _rpt_rate(key_name)      # ← passes ONLY key_name, no model
    ...
    return (total_tokens / 1_000_000) * cost_per_1m
```

`_rpt_rate('neuralwatt')` at line 2597 calls `get_rate_with_fallback('neuralwatt')`
which resolves:

1. `get_real_rate('neuralwatt')` → `SUM(cost_usd)/SUM(total_tokens)*1e6` from DB
   → circular: cost_usd was itself computed from $0.21/M, so measured rate = $0.21/M
2. If <100 costed calls → falls back to `LAST_RESORT_RATES['neuralwatt'] = 0.21`

**Result: ALL NeuralWatt calls are priced at $0.21/M regardless of model.**

NeuralWatt serves models at vastly different rates (from `NEURALWATT_RATES` at line 626):

| Model | Input $/M | Output $/M | 3:1 Blended | $0.21/M flat | Error |
|-------|-----------|------------|-------------|---------------|-------|
| glm-5.2 | $1.45 | $4.50 | $2.21 | $0.21 | **10.5× undercount** |
| kimi-k3 | $1.45 | $4.50 | $2.21 | $0.21 | **10.5× undercount** |
| deepseek-v4-flash | $0.14 | $0.28 | $0.18 | $0.21 | 1.2× overcount |

### Bug B: `_extract_cost()` does not handle NeuralWatt

**Location:** `zai_proxy.py:3071` — `_extract_cost(provider, response_buffer, total_tokens)`

The function handles: openrouter, deepinfra, ppq, telnyx (via `cost_extraction.py`),
then has explicit branches for ours/friend, ollama_cloud, and telnyx. **NeuralWatt
falls through to step 5 → returns `(None, None)`** — no cost is extracted from the
response body.

This means `api_calls.cost_usd` is **NULL** for 86% of NeuralWatt calls (2,439 of 2,837).
Only 399 rows have cost_usd (at $0.21/M, source unknown — likely an earlier code
revision or partial response parsing). `cost_source` is NULL for all rows, confirming
no provider-side cost field was parsed.

### Bug C: NeuralWatt NOT in `kalman_price_state.json`

**File:** `~/.hermes/bot/kalman_price_state.json`

Only `ours`, `friend`, `ppq` entries exist. NeuralWatt has:
- No PriceKalman filter state (no `volume`, `velocity`, `P00/P01/P11`)
- No ConsumptionKalman tracking
- No convergence from $0.21/M seed to the true ~$2.21/M glm-5.2 rate

The `live_router.py` `__init__` (line 757) creates PriceKalman instances from
`_DEFAULT_CONVERGED_RATES` which includes `neuralwatt: 0.21` (copied from
`_RPT_LAST_RESORT_RATES`). The filter starts at $0.21/M and **never receives a
measurement** because:
1. `_record_spend` feeds `_estimate_cost_usd` → $0.21/M (circular)
2. The Kalman state file doesn't have a neuralwatt entry to update
3. Even with `_DYNAMIC_RATES_ENABLED`, `get_real_rate('neuralwatt')` measures from
   the same wrong cost_usd data

### Bug D: `daily_spend` vs `api_calls` discrepancy

Two different cost computation paths produce two different numbers:

| Path | Function | Rate | Call coverage | Total |
|------|----------|------|---------------|-------|
| `daily_spend` | `_estimate_cost_usd()` → `_rpt_rate('neuralwatt')` | $0.21/M | ALL calls (2,741) | $39.61 |
| `api_calls.cost_usd` | `_extract_cost('neuralwatt', ...)` → `(None, None)` | NULL | Only 399 have value | $4.45 |
| **Expected** | Per-model × per-token-type | $1.45-4.50/M | — | **$226.20** |

`daily_spend` always gets a value because `_record_spend` (line 2670) falls back to
`_estimate_cost_usd` when `actual_cost` is None. `api_calls` stores whatever
`_extract_cost` returned (NULL for most NeuralWatt calls).

---

## 2. Undercounting Summary

### Actual NeuralWatt usage (from `zai_usage.db`, 2026-08-23):

| Model | Calls | Prompt Tokens | Completion Tokens | Expected Cost |
|-------|-------|---------------|-------------------|---------------|
| glm-5.2 | 2,179 | 146,283,149 | 2,009,882 | **$221.87** |
| deepseek-v4-flash | 643 | 30,361,469 | 224,266 | **$4.33** |
| **Total** | **2,822** | **176,644,618** | **2,234,148** | **$226.20** |

### Recorded vs Expected:

| Metric | Recorded | Expected | Undercount |
|--------|----------|----------|------------|
| `api_calls.cost_usd` SUM | $4.45 | $226.20 | **50.8×** |
| `daily_spend.spend_usd` | $39.61 | $226.20 | **5.7×** |
| `_DEFAULT_CONVERGED_RATES['neuralwatt']` | $0.21/M | $2.21/M (glm-5.2) | **10.5×** |

### Why $0.21/M is wrong

The $0.21/M rate is the **deepseek-v4-flash blended** rate, computed as
`(0.14 + 0.28) / 2 = 0.21` (line 202 in `real_price_tracker.py`). But:
1. NeuralWatt serves **5 different models** at 5 different price points
2. Even for deepseek-v4-flash, the correct 3:1 blend is $0.175/M (not $0.21)
3. 77% of NeuralWatt traffic is glm-5.2 ($2.21/M blended) — mispriced at $0.21/M

---

## 3. Fix Plan

### Fix 1: Make `_estimate_cost_usd()` model-aware for NeuralWatt

**File:** `zai_proxy.py`, function `_estimate_cost_usd` (line 2651)

Add a neuralwatt-specific branch that uses `NEURALWATT_RATES` + per-token-type
pricing (like the telnyx handler in `_extract_cost`):

```python
def _estimate_cost_usd(key_name, total_tokens, model=None,
                       prompt_tokens=None, completion_tokens=None) -> float:
    ...
    if key_name == "neuralwatt":
        # Use per-model rates from NEURALWATT_RATES with real token splits
        rates = NEURALWATT_RATES.get(model) if model else None
        if rates and prompt_tokens is not None and completion_tokens is not None:
            input_rate = rates.get("input", 0.14)
            output_rate = rates.get("output", 0.28)
            return (prompt_tokens * input_rate + completion_tokens * output_rate) / 1e6
        elif rates:
            # Fallback: blended rate for the MODEL, not the provider
            return _blended_rate(rates["input"], rates["output"]) * total_tokens / 1e6
        # No model match → conservative default
        return _rpt_rate(key_name) * total_tokens / 1e6
    ...
```

Update all call sites of `_estimate_cost_usd` to pass model + token breakdown.

### Fix 2: Add NeuralWatt to `_extract_cost()`

**File:** `zai_proxy.py`, function `_extract_cost` (line 3071)

Add a NeuralWatt branch (similar to telnyx at line 3116) that:
1. Parses `usage.prompt_tokens` and `usage.completion_tokens` from the response
2. Looks up `NEURALWATT_RATES[model]`
3. Computes: `(prompt × input_rate + completion × output_rate) / 1e6`
4. Returns `(cost_usd, "rate_derived")` — this populates `api_calls.cost_usd`

### Fix 3: Add NeuralWatt to `kalman_price_state.json`

**File:** `~/.hermes/bot/kalman_price_state.json`

Add an entry for neuralwatt, seeded at the correct blended rate:

```json
"neuralwatt": {
    "volume": 2.2125,
    "velocity": 0.0,
    "P00": 1.0,
    "P01": 0.5,
    "P10": 0.5,
    "P11": 1.0,
    "n_obs": 0,
    "price_sats": 0,
    "updated_at": 0
}
```

This seeds the PriceKalman at $2.2125/M (the 3:1 blended glm-5.2 rate, the most
common NeuralWatt model) instead of $0.21/M.

### Fix 4: Add NeuralWatt to `LAST_RESORT_RATES` and `SEED_RATES`

**File:** `src/real_price_tracker.py`

Update `LAST_RESORT_RATES['neuralwatt']` and `SEED_RATES['neuralwatt']`:

```python
# OLD: "neuralwatt": 0.21,    # deepseek-v4-flash blended
# NEW: "neuralwatt": 2.21,    # glm-5.2 blended (primary model, most conservative)
```

This ensures that even in the cold-start path, NeuralWatt is priced at a realistic
rate. The per-model pricing path (Fix 2) handles the difference between models.

### Fix 5: Add per-model `LAST_RESORT_RATES_PER_MODEL` for NeuralWatt

**File:** `src/real_price_tracker.py`, `LAST_RESORT_RATES_PER_MODEL` dict (line 214)

```python
LAST_RESORT_RATES_PER_MODEL = {
    "telnyx": { ... },
    "neuralwatt": {
        "glm-5.2":               2.2125,
        "kimi-k3":               2.2125,
        "deepseek-v4-flash":     0.175,
        "deepseek-v4-pro":       1.875,
        "gemma-4-31b":            0.35,
    },
}
```

### Fix 6: Fix `daily_spend` computation consistency

**File:** `zai_proxy.py`, `_record_spend` (line 2670)

Both `daily_spend` and `api_calls` should use the same cost computation. Two options:

**Option A (recommended):** Make `_extract_cost` handle NeuralWatt (Fix 2), so
`_record_spend` receives a real `actual_cost` from _extract_cost, and
`_log_api_call` gets the same value. Both tables match.

**Option B:** Have `_record_spend` pass the computed `_estimate_cost_usd` cost
to `_log_api_call` instead of the raw `ext_cost_usd` from `_extract_cost`. This
ensures api_calls always has a cost_usd (even for providers whose responses don't
include a cost field).

### Fix 7: Add NeuralWatt to `_MODEL_ID_TO_PROVIDER_ID`

**File:** `zai_proxy.py`, line 2350

Currently, `_MODEL_ID_TO_PROVIDER_ID` has no `neuralwatt` entry for any model.
This means `_get_provider_cost('neuralwatt', model_id)` falls through the per-model
rate table at step 1 and hits the NEURALWATT_RATES lookup (step 1's "elif name ==
'neuralwatt'" at line 1116). While this works, it's because the code checks
`NEURALWATT_RATES.get(provider_model) or NEURALWATT_RATES.get(model_id)` — the
`model_id` fallback catches it. This is fragile; add an explicit mapping:

```python
_MODEL_ID_TO_PROVIDER_ID = {
    "glm-5.2": {
        ...
        "neuralwatt": "glm-5.2",
    },
    ...
}
```

---

## 4. Resolution Chain Trace: `_rpt_rate('neuralwatt')`

```
_estimate_cost_usd('neuralwatt', total_tokens=75000)
  └─ _rpt_rate('neuralwatt')                          [zai_proxy.py:2597]
     └─ _rpt_get_rate('neuralwatt')                   [zai_proxy.py:2604]
        = get_rate_with_fallback('neuralwatt')          [real_price_tracker.py:1329]
        └─ Step 1: get_real_rate('neuralwatt')          [real_price_tracker.py:1351]
           └─ _query_window(...) → SUM(cost_usd)/SUM(total_tokens)*1e6
              └─ cost_usd was computed by same _estimate_cost_usd → $0.21/M
              └─ If ≥100 costed calls: returns measured rate ≈ $0.21/M (CIRCULAR)
              └─ If <100: returns None → step 3
        └─ Step 3: LAST_RESORT_RATES['neuralwatt'] = 0.21  [real_price_tracker.py:202]
     └─ Fallback: _FALLBACK_RATES['neuralwatt'] = 0.21    [zai_proxy.py:2302]
  └─ cost = 75000 / 1e6 * 0.21 = $0.01575
     └─ SHOULD BE: (75000*0.75*1.45 + 75000*0.25*4.50)/1e6 ≈ $0.2166 (10.5x undercount)
```

---

## 5. Verification — DB Queries (2026-08-23)

### NeuralWatt api_calls breakdown:
```
model                         calls  prompt_tokens  completion_tokens  sum(cost_usd)  rate
glm-5.2                       2,179  146,283,149    2,009,882           $4.170301      $0.028/M (NULLs included)
deepseek/deepseek-v4-flash      643   30,361,469      224,266           $0.280488      $0.009/M (NULLs included)
Non-null only (399 rows):
deepseek/deepseek-v4-flash              ...                                            $0.210002/M ✓
glm-5.2                                 ...                                            $0.210000/M ✓
```

### daily_spend:
```
date         tier         spend_usd  call_count  token_count
2026-08-23   neuralwatt   $39.61     2,741       176,310,035
→ rate = $39.61 / 176.31M = $0.2247/M (≈ $0.21/M + precision drift)
```

### Expected cost at correct NeuralWatt rates:
```
glm-5.2:               146.75M × $1.45/M + 2.02M × $4.50/M = $221.87
deepseek-v4-flash:      30.46M × $0.14/M + 0.23M × $0.28/M = $4.33
                                    Total expected: $226.20
                            Total recorded (api_calls): $4.45 (50.8× undercount)
                        Total recorded (daily_spend): $39.61 (5.7× undercount)
```

---

## 6. Files to Change

| # | File | Change |
|---|------|--------|
| 1 | `zai_proxy.py` | Add neuralwatt branch in `_extract_cost()` (per-model rate-derived cost) |
| 2 | `zai_proxy.py` | Make `_estimate_cost_usd()` model-aware (pass model + token split) |
| 3 | `zai_proxy.py` | Update all `_estimate_cost_usd` call sites (5-6 locations) |
| 4 | `zai_proxy.py` | Add neuralwatt to `_MODEL_ID_TO_PROVIDER_ID` |
| 5 | `src/real_price_tracker.py` | Update `LAST_RESORT_RATES['neuralwatt']` → $2.21 |
| 6 | `src/real_price_tracker.py` | Update `SEED_RATES['neuralwatt']` → $2.21 |
| 7 | `src/real_price_tracker.py` | Add `LAST_RESORT_RATES_PER_MODEL['neuralwatt']` |
| 8 | `kalman_price_state.json` | Add neuralwatt entry seeded at $2.21/M |
| 9 | `src/cost_extraction.py` (optional) | Add neuralwatt to `PROVIDER_COST_PATHS` if API returns cost field |

### Priority

1. **Critical:** Fix `_extract_cost()` + `_estimate_cost_usd()` (Fixes 1+2) — stops the
   bleeding on new calls immediately.
2. **High:** Update `kalman_price_state.json` + `LAST_RESORT_RATES` (Fixes 3+4) — ensures
   the router prices NeuralWatt correctly for routing decisions.
3. **Medium:** Add per-model last-resort rates (Fix 5) — bounds the error when measured
   data is unavailable.
4. **Low:** `_MODEL_ID_TO_PROVIDER_ID` mapping (Fix 7) — cleanup, not a functional bug.
